import ipaddress
import json
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import tinytuya

from app.storage import (
    get_control_device,
    get_device_by_id,
    get_latest_sample,
    replace_device_capabilities,
    get_device_row,
    get_known_local_ips,
    refresh_managed_device_cloud_data,
    save_cloud_artifact,
    upsert_managed_device,
)
from app.tuya_service import fetch_status
from config import AppConfig, ConfigError, TuyaDeviceConfig, load_cloud_config


DEVICE_KIND_LABELS = {
    "meter": "Счетчик",
    "switch": "Выключатель",
    "sensor": "Датчик",
    "light": "Лампочка",
}

ENERGY_CODES = {
    "cur_power",
    "cur_current",
    "cur_voltage",
    "add_ele",
    "total_forward_energy",
    "total_reverse_energy",
}
LIGHT_CODES = {"bright_value", "temp_value", "colour_data", "colour_data_v2", "scene_data", "work_mode"}
SENSOR_CODES = {"temp_current", "humidity_value", "pir", "smoke_sensor_state", "doorcontact_state", "va_battery"}
PRIVATE_TUYA_PORTS = (6668, 6669, 7000)
DEFAULT_ROOM_NAME = "Без комнаты"
LOCAL_DISCOVERY_ERROR_MESSAGE = (
    "Не удалось найти устройство в локальной сети. Убедитесь, что сервер и устройство в одной сети, а само устройство включено."
)


def _parse_values(raw_values: Any) -> dict[str, Any]:
    if isinstance(raw_values, dict):
        return raw_values
    if isinstance(raw_values, str) and raw_values.strip():
        try:
            parsed = json.loads(raw_values)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


def _normalize_raw_dps(raw_dps: Any) -> dict[str, Any]:
    if isinstance(raw_dps, dict):
        return {str(key): value for key, value in raw_dps.items()}
    if isinstance(raw_dps, str) and raw_dps.strip():
        try:
            parsed = json.loads(raw_dps)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return {str(key): value for key, value in parsed.items()}
    return {}


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _looks_like_voltage_value(value: Any) -> bool:
    numeric_value = _coerce_float(value)
    return numeric_value is not None and 50.0 <= abs(numeric_value) <= 4000.0


def _device_name(device_v1: dict[str, Any], device_v2: dict[str, Any]) -> str:
    return (
        str(device_v2.get("custom_name") or "").strip()
        or str(device_v1.get("name") or "").strip()
        or str(device_v2.get("name") or "").strip()
        or str(device_v1.get("product_name") or "").strip()
        or "Новое устройство"
    )


def _resolve_room_name(
    cloud: tinytuya.Cloud,
    device_id: str,
    device_v1: dict[str, Any],
    device_v2: dict[str, Any],
) -> str | None:
    device_info = device_v1.get("result") or {}
    device_info_v2 = device_v2.get("result") or {}
    home_id = str(device_info_v2.get("bind_space_id") or device_info.get("owner_id") or "").strip()
    if not home_id:
        return None

    rooms_payload = cloud.cloudrequest(f"/v1.0/homes/{home_id}/rooms")
    if not isinstance(rooms_payload, dict) or not rooms_payload.get("success"):
        return None

    rooms = ((rooms_payload.get("result") or {}).get("rooms") or [])
    for room in rooms:
        room_id = room.get("room_id")
        room_name = str(room.get("name") or "").strip()
        if room_id is None or not room_name:
            continue

        room_devices_payload = cloud.cloudrequest(f"/v1.0/homes/{home_id}/rooms/{room_id}/devices")
        if not isinstance(room_devices_payload, dict) or not room_devices_payload.get("success"):
            continue

        room_devices = room_devices_payload.get("result") or []
        if any(str(item.get("id") or "").strip() == device_id for item in room_devices if isinstance(item, dict)):
            return room_name

    return None


def _classify_device(category_code: str | None, dps_info: dict[str, Any]) -> tuple[str, bool]:
    status_defs = dps_info.get("status") or []
    function_defs = dps_info.get("functions") or []
    codes = {str(item.get("code") or "") for item in status_defs + function_defs}

    is_energy_meter = any(code in ENERGY_CODES for code in codes)
    if is_energy_meter:
        return "meter", True
    if category_code in {"dj", "dd", "fwl", "fs"} or any(code in LIGHT_CODES for code in codes):
        return "light", False
    if category_code in {"wsdcg", "pir", "mcs", "ckmkzq"} or any(code in SENSOR_CODES for code in codes):
        return "sensor", False
    if "switch" in codes:
        return "switch", False
    return "sensor", False


def _iter_dps_definitions(dps_info: dict[str, Any]) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = []
    for source in ("status", "functions"):
        for item in dps_info.get(source) or []:
            if isinstance(item, dict):
                definitions.append(item)
    return definitions


def _extract_power_profile(dps_info: dict[str, Any]) -> tuple[str | None, float, list[str]]:
    power_dps_key: str | None = None
    power_scale = 1.0
    voltage_dps_keys: list[str] = []

    for item in _iter_dps_definitions(dps_info):
        code = str(item.get("code") or "")
        dp_id = item.get("dp_id")
        if dp_id is None:
            continue
        key = str(dp_id)
        values = _parse_values(item.get("values"))
        scale = int(values.get("scale", 0) or 0)
        if power_dps_key is None and code == "cur_power":
            power_dps_key = key
            power_scale = float(10**scale) if scale > 0 else 1.0
        if "voltage" in code and key not in voltage_dps_keys:
            voltage_dps_keys.append(key)

    return power_dps_key, power_scale, voltage_dps_keys


def _infer_power_profile_from_raw_dps(raw_dps: Any) -> tuple[str | None, float, list[str]]:
    normalized_dps = _normalize_raw_dps(raw_dps)
    if not normalized_dps:
        return None, 1.0, []

    power_dps_key = "102" if _coerce_float(normalized_dps.get("102")) is not None else None
    voltage_dps_keys = [
        key
        for key in ("107", "108", "109")
        if _looks_like_voltage_value(normalized_dps.get(key))
    ]
    return power_dps_key, 100.0 if power_dps_key == "102" else 1.0, voltage_dps_keys


def _complete_power_profile(
    power_dps_key: str | None,
    power_scale: float,
    voltage_dps_keys: list[str],
    raw_dps: Any,
) -> tuple[str | None, float, list[str]]:
    if power_dps_key and voltage_dps_keys:
        return power_dps_key, power_scale, voltage_dps_keys

    fallback_power_dps_key, fallback_power_scale, fallback_voltage_dps_keys = _infer_power_profile_from_raw_dps(raw_dps)
    if power_dps_key is None:
        power_dps_key = fallback_power_dps_key
        power_scale = fallback_power_scale
    if not voltage_dps_keys:
        voltage_dps_keys = fallback_voltage_dps_keys
    return power_dps_key, power_scale, voltage_dps_keys


def _complete_existing_power_profile(
    config: AppConfig,
    device_id: str,
    power_dps_key: str | None,
    power_scale: float,
    voltage_dps_keys: list[str],
) -> tuple[str | None, float, list[str]]:
    latest_sample = get_latest_sample(config, device_id)
    if latest_sample:
        power_dps_key, power_scale, voltage_dps_keys = _complete_power_profile(
            power_dps_key,
            power_scale,
            voltage_dps_keys,
            latest_sample.get("raw_dps"),
        )
    if power_dps_key and voltage_dps_keys:
        return power_dps_key, power_scale, voltage_dps_keys

    control_device = get_control_device(config, device_id)
    if not control_device or not control_device.ip_address:
        return power_dps_key, power_scale, voltage_dps_keys

    try:
        payload = fetch_status(control_device)
    except Exception:
        return power_dps_key, power_scale, voltage_dps_keys

    return _complete_power_profile(power_dps_key, power_scale, voltage_dps_keys, payload.get("dps"))


def _build_capabilities(dps_info: dict[str, Any]) -> list[dict[str, Any]]:
    capabilities: list[dict[str, Any]] = []
    for source in ("functions", "status"):
        for item in dps_info.get(source) or []:
            capabilities.append(
                {
                    "capability_source": source,
                    "capability_code": str(item.get("code") or "unknown"),
                    "capability_name": str(item.get("name") or item.get("code") or ""),
                    "value_type": item.get("type"),
                    "dp_id": item.get("dp_id"),
                    "values_json": _parse_values(item.get("values")),
                }
            )
    return capabilities


def _known_subnets(config: AppConfig) -> list[ipaddress.IPv4Network]:
    raw_ips = set(get_known_local_ips(config))
    subnets: list[ipaddress.IPv4Network] = []
    for raw_ip in sorted(raw_ips):
        try:
            ip_obj = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        if not isinstance(ip_obj, ipaddress.IPv4Address) or not ip_obj.is_private:
            continue
        network = ipaddress.ip_network(f"{ip_obj}/24", strict=False)
        if network not in subnets:
            subnets.append(network)
    return subnets


def _is_tuya_host(ip_address: str) -> bool:
    for port in PRIVATE_TUYA_PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.15)
            if sock.connect_ex((ip_address, port)) == 0:
                return True
    return False


def _candidate_ips(config: AppConfig) -> list[str]:
    direct_ips = [ip for ip in get_known_local_ips(config) if ip]
    candidates = list(dict.fromkeys(direct_ips))
    for network in _known_subnets(config):
        with ThreadPoolExecutor(max_workers=64) as executor:
            future_map = {
                executor.submit(_is_tuya_host, str(host)): str(host)
                for host in network.hosts()
                if str(host) not in candidates
            }
            for future in as_completed(future_map):
                if future.result():
                    candidates.append(future_map[future])
    return candidates


def _probe_device(ip_address: str, device_id: str, local_key: str) -> float | None:
    for version in (3.5, 3.4, 3.3):
        device = tinytuya.Device(device_id, ip_address, local_key)
        device.set_version(version)
        device.set_socketTimeout(0.7)
        device.set_socketRetryLimit(1)
        try:
            payload = device.status()
        except Exception:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("dps"), dict):
            return version
    return None


def _discover_local_endpoint(config: AppConfig, device_id: str, local_key: str) -> tuple[str, float]:
    try:
        scanned = tinytuya.deviceScan(verbose=False, color=False, poll=True, byID=True)
    except Exception:
        scanned = {}

    if isinstance(scanned, dict) and device_id in scanned:
        match = scanned[device_id]
        ip_address = str(match.get("ip") or "").strip()
        version = float(match.get("version") or 3.5)
        if ip_address:
            return ip_address, version

    for ip_address in _candidate_ips(config):
        version = _probe_device(ip_address, device_id, local_key)
        if version is not None:
            return ip_address, version

    raise ConfigError(LOCAL_DISCOVERY_ERROR_MESSAGE)


def connect_device(config: AppConfig, device_id: str) -> dict[str, Any]:
    clean_device_id = device_id.strip()
    if not clean_device_id:
        raise ConfigError("Укажите device ID")

    cloud_config = load_cloud_config(required=True)
    cloud = tinytuya.Cloud(
        apiRegion=cloud_config.region,
        apiKey=cloud_config.api_key,
        apiSecret=cloud_config.api_secret,
        apiDeviceID=cloud_config.api_device_id or clean_device_id,
    )

    device_v1 = cloud.cloudrequest(f"/v1.0/devices/{clean_device_id}")
    if not isinstance(device_v1, dict) or not device_v1.get("success"):
        raise ConfigError(f"Не удалось получить устройство из Tuya Cloud: {device_v1.get('msg') or device_v1.get('code')}")
    device_v2 = cloud.cloudrequest(f"/v2.0/cloud/thing/{clean_device_id}")
    dps_info = cloud.getdps(clean_device_id)
    if not isinstance(dps_info, dict) or not dps_info.get("success"):
        raise ConfigError(f"Не удалось получить DP-описание устройства: {dps_info.get('msg') or dps_info.get('code')}")

    device_info = device_v1.get("result") or {}
    device_info_v2 = device_v2.get("result") or {}
    dps_result = dps_info.get("result") or {}

    name = _device_name(device_info, device_info_v2)
    existing = get_device_by_id(config, clean_device_id)
    resolved_room = None
    if not existing or not (str(existing.get("room") or "").strip() and existing["room"] != DEFAULT_ROOM_NAME):
        resolved_room = _resolve_room_name(cloud, clean_device_id, device_v1, device_v2)
    if existing and str(existing.get("room") or "").strip() and existing["room"] != DEFAULT_ROOM_NAME:
        room = existing["room"]
    else:
        room = resolved_room or DEFAULT_ROOM_NAME

    kind, is_energy_meter = _classify_device(str(device_info.get("category") or device_info_v2.get("category") or ""), dps_result)
    power_dps_key, power_scale, voltage_dps_keys = _extract_power_profile(dps_result)
    capabilities = _build_capabilities(dps_result)

    if existing:
        control_device = get_control_device(config, clean_device_id)
        local_key = str(
            device_info.get("local_key")
            or device_info_v2.get("local_key")
            or (control_device.local_key if control_device else "")
            or ""
        ).strip()
        ip_address = control_device.ip_address if control_device and control_device.ip_address else ""
        version = control_device.version if control_device else 3.5
        connection_ready = bool(ip_address)
        connection_message = ""

        if not connection_ready and local_key:
            try:
                ip_address, version = _discover_local_endpoint(config, clean_device_id, local_key)
                connection_ready = True
            except ConfigError as error:
                if str(error) != LOCAL_DISCOVERY_ERROR_MESSAGE:
                    raise
                connection_message = LOCAL_DISCOVERY_ERROR_MESSAGE

        power_dps_key, power_scale, voltage_dps_keys = _complete_existing_power_profile(
            config,
            clean_device_id,
            power_dps_key,
            power_scale,
            voltage_dps_keys,
        )

        upsert_managed_device(
            config,
            device_id=clean_device_id,
            name=name,
            room=room,
            category_code=str(device_info.get("category") or device_info_v2.get("category") or "") or None,
            device_kind=kind,
            is_energy_meter=is_energy_meter,
            product_id=str(device_info.get("product_id") or device_info_v2.get("product_id") or "") or None,
            product_name=str(device_info.get("product_name") or device_info_v2.get("product_name") or device_info_v2.get("name") or "") or None,
            icon=str(device_info.get("icon") or device_info_v2.get("icon") or "") or None,
            onboarding_source="cloud",
            local_key=local_key,
            ip_address=ip_address,
            version=version,
            power_dps_key=power_dps_key,
            power_scale=power_scale,
            voltage_dps_keys=voltage_dps_keys,
            capabilities=capabilities,
        )
        save_cloud_artifact(config, device_id=clean_device_id, artifact_type="onboard_device_v1", payload=device_v1)
        if isinstance(device_v2, dict):
            save_cloud_artifact(config, device_id=clean_device_id, artifact_type="onboard_device_v2", payload=device_v2)
        save_cloud_artifact(config, device_id=clean_device_id, artifact_type="onboard_dps", payload=dps_info)

        stored = get_device_row(config, clean_device_id)
        return {
            "name": stored.get("name") if stored else name,
            "device_id": clean_device_id,
            "device_kind": kind,
            "device_kind_label": DEVICE_KIND_LABELS.get(kind, kind),
            "is_energy_meter": is_energy_meter,
            "ip_address": ip_address or None,
            "version": version if connection_ready else None,
            "product_name": stored.get("product_name") if stored else None,
            "category_code": stored.get("category_code") if stored else None,
            "capability_count": len(capabilities),
            "connection_ready": connection_ready,
            "connection_message": connection_message,
        }

    local_key = str(device_info.get("local_key") or device_info_v2.get("local_key") or "").strip()
    if not local_key:
        raise ConfigError("Tuya Cloud не вернул local key для устройства")

    ip_address = ""
    version = 3.5
    connection_ready = False
    connection_message = ""
    try:
        ip_address, version = _discover_local_endpoint(config, clean_device_id, local_key)
        connection_ready = True
    except ConfigError as error:
        if str(error) != LOCAL_DISCOVERY_ERROR_MESSAGE:
            raise
        connection_message = LOCAL_DISCOVERY_ERROR_MESSAGE

    if connection_ready:
        provisional_device = TuyaDeviceConfig(
            name=name,
            room=room,
            device_id=clean_device_id,
            local_key=local_key,
            ip_address=ip_address,
            version=version,
            power_dps_key=power_dps_key or "",
            power_scale=power_scale,
            voltage_dps_keys=tuple(voltage_dps_keys),
        )
        try:
            local_payload = fetch_status(provisional_device)
        except Exception:
            local_payload = {}
        power_dps_key, power_scale, voltage_dps_keys = _complete_power_profile(
            power_dps_key,
            power_scale,
            voltage_dps_keys,
            local_payload.get("dps") if isinstance(local_payload, dict) else None,
        )

    upsert_managed_device(
        config,
        name=name,
        room=room,
        device_id=clean_device_id,
        category_code=str(device_info.get("category") or device_info_v2.get("category") or "") or None,
        device_kind=kind,
        is_energy_meter=is_energy_meter,
        product_id=str(device_info.get("product_id") or device_info_v2.get("product_id") or "") or None,
        product_name=str(device_info.get("product_name") or device_info_v2.get("product_name") or device_info_v2.get("name") or "") or None,
        icon=str(device_info.get("icon") or device_info_v2.get("icon") or "") or None,
        onboarding_source="cloud",
        local_key=local_key,
        ip_address=ip_address,
        version=version,
        power_dps_key=power_dps_key,
        power_scale=power_scale,
        voltage_dps_keys=voltage_dps_keys,
        capabilities=capabilities,
    )

    save_cloud_artifact(config, device_id=clean_device_id, artifact_type="onboard_device_v1", payload=device_v1)
    if isinstance(device_v2, dict):
        save_cloud_artifact(config, device_id=clean_device_id, artifact_type="onboard_device_v2", payload=device_v2)
    save_cloud_artifact(config, device_id=clean_device_id, artifact_type="onboard_dps", payload=dps_info)

    stored = get_device_row(config, clean_device_id)
    return {
        "name": name,
        "device_id": clean_device_id,
        "device_kind": kind,
        "device_kind_label": DEVICE_KIND_LABELS.get(kind, kind),
        "is_energy_meter": is_energy_meter,
        "ip_address": ip_address or None,
        "version": version,
        "product_name": stored.get("product_name") if stored else None,
        "category_code": stored.get("category_code") if stored else None,
        "capability_count": len(capabilities),
        "connection_ready": connection_ready,
        "connection_message": connection_message,
    }
