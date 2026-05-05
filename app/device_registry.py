import ipaddress
import json
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import tinytuya

from app.storage import (
    get_control_device,
    get_device_capabilities,
    get_device_by_id,
    get_latest_sample,
    replace_device_capabilities,
    get_device_row,
    get_known_local_ips,
    refresh_managed_device_cloud_data,
    update_device_connection_endpoint,
    upsert_managed_device,
)
from app.tuya_model import (
    build_model_property_index,
    extract_model_properties,
    get_model_scale_divisor,
    merge_values_json_with_model,
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


def _extract_total_power_profile(dps_info: dict[str, Any]) -> tuple[str | None, float]:
    total_power_dps_key: str | None = None
    total_power_scale = 1.0

    for item in _iter_dps_definitions(dps_info):
        code = str(item.get("code") or "")
        dp_id = item.get("dp_id")
        if dp_id is None:
            continue
        key = str(dp_id)
        values = _parse_values(item.get("values"))
        scale = int(values.get("scale", 0) or 0)
        if total_power_dps_key is None and code in {"add_ele", "total_forward_energy"}:
            total_power_dps_key = key
            total_power_scale = float(10**scale) if scale > 0 else 1.0

    return total_power_dps_key, total_power_scale


def _complete_total_power_profile(
    total_power_dps_key: str | None,
    total_power_scale: float,
    raw_dps: Any,
) -> tuple[str | None, float]:
    return total_power_dps_key, total_power_scale


def _complete_existing_power_profile(
    config: AppConfig,
    device_id: str,
    total_power_dps_key: str | None,
    total_power_scale: float,
) -> tuple[str | None, float]:
    latest_sample = get_latest_sample(config, device_id)
    if latest_sample:
        total_power_dps_key, total_power_scale = _complete_total_power_profile(
            total_power_dps_key,
            total_power_scale,
            latest_sample.get("raw_dps"),
        )
    if total_power_dps_key:
        return total_power_dps_key, total_power_scale

    control_device = get_control_device(config, device_id)
    if not control_device or not control_device.ip_address:
        return total_power_dps_key, total_power_scale

    try:
        payload = fetch_status(control_device)
    except Exception:
        return total_power_dps_key, total_power_scale

    return _complete_total_power_profile(total_power_dps_key, total_power_scale, payload.get("dps"))


def _default_visualized_codes(
    capabilities: list[dict[str, Any]],
    total_power_dps_key: str | None,
) -> list[str]:
    preferred_codes = [
        "cur_power",
        "cur_current",
        "cur_voltage",
        "add_ele",
        "total_forward_energy",
    ]
    selected: list[str] = []

    for preferred_code in preferred_codes:
        for capability in capabilities:
            if str(capability.get("capability_code") or "") != preferred_code:
                continue
            dp_id = capability.get("dp_id")
            if dp_id is None:
                continue
            key = str(dp_id)
            if key not in selected:
                selected.append(key)

    if total_power_dps_key and total_power_dps_key not in selected:
        selected.append(total_power_dps_key)

    return selected


def _build_summary_options(device_model: dict[str, Any], capabilities: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    options: list[dict[str, Any]] = []

    for item in extract_model_properties(device_model):
        dp_id = item.get("abilityId")
        if dp_id is None:
            continue
        type_spec = item.get("typeSpec") or {}
        options.append(
            {
                "dp_id": str(dp_id),
                "code": str(item.get("code") or ""),
                "name": str(item.get("name") or item.get("code") or dp_id),
                "value_type": str(type_spec.get("type") or ""),
                "unit": str(type_spec.get("unit") or ""),
                "scale": int(type_spec.get("scale", 0) or 0),
            }
        )

    if not options:
        for capability in capabilities:
            dp_id = capability.get("dp_id")
            if dp_id is None:
                continue
            values_json = capability.get("values_json") or {}
            options.append(
                {
                    "dp_id": str(dp_id),
                    "code": str(capability.get("capability_code") or ""),
                    "name": str(capability.get("capability_name") or capability.get("capability_code") or dp_id),
                    "value_type": str(capability.get("value_type") or ""),
                    "unit": str(values_json.get("unit") or ""),
                    "scale": int(values_json.get("scale", 0) or 0),
                }
            )

    options.sort(key=lambda item: int(item["dp_id"]) if str(item.get("dp_id") or "").isdigit() else 999999)
    power_options = [
        item for item in options
        if item.get("value_type") in {"value", "Integer", "integer"}
    ]
    return power_options, options


def _build_capabilities(dps_info: dict[str, Any], device_model: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    property_index = build_model_property_index(device_model or {})
    capabilities: list[dict[str, Any]] = []
    seen_dp_ids: set[str] = set()
    for source in ("functions", "status"):
        for item in dps_info.get(source) or []:
            dp_id = item.get("dp_id")
            model_property = property_index.get(str(dp_id)) if dp_id is not None else None
            if dp_id is not None:
                seen_dp_ids.add(str(dp_id))
            capabilities.append(
                {
                    "capability_source": source,
                    "capability_code": str(item.get("code") or "unknown"),
                    "capability_name": str(
                        item.get("name")
                        or (model_property or {}).get("name")
                        or item.get("code")
                        or ""
                    ),
                    "value_type": item.get("type") or ((model_property or {}).get("typeSpec") or {}).get("type"),
                    "dp_id": dp_id,
                    "values_json": merge_values_json_with_model(_parse_values(item.get("values")), model_property),
                }
            )

    for dp_id, model_property in property_index.items():
        if dp_id in seen_dp_ids:
            continue
        capabilities.append(
            {
                "capability_source": "model",
                "capability_code": str(model_property.get("code") or "unknown"),
                "capability_name": str(model_property.get("name") or model_property.get("code") or ""),
                "value_type": ((model_property.get("typeSpec") or {}).get("type") or ""),
                "dp_id": model_property.get("abilityId"),
                "values_json": merge_values_json_with_model({}, model_property),
            }
        )
    return capabilities


def _resolve_total_power_dp_from_capabilities(capabilities: list[dict[str, Any]]) -> str | None:
    for capability in capabilities:
        code = str(capability.get("capability_code") or "")
        dp_id = capability.get("dp_id")
        if dp_id is None or code not in {"add_ele", "total_forward_energy"}:
            continue
        return str(dp_id)
    return None


def _resolve_scale_divisor_for_dp(
    capabilities: list[dict[str, Any]],
    device_model: dict[str, Any],
    dp_id: str | None,
) -> float | None:
    if not dp_id:
        return None

    model_scale = get_model_scale_divisor(device_model, dp_id)
    if model_scale is not None:
        return model_scale

    for capability in capabilities:
        if str(capability.get("dp_id") or "") != dp_id:
            continue
        values_json = capability.get("values_json") or {}
        scale_digits = int(values_json.get("scale", 0) or 0)
        return float(10 ** scale_digits) if scale_digits > 0 else 1.0

    return None


def _known_subnets(config: AppConfig) -> list[ipaddress.IPv4Network]:
    raw_ips = set(get_known_local_ips(config))
    subnets: list[ipaddress.IPv4Network] = []

    for raw_subnet in config.local_discovery_subnets:
        try:
            network = ipaddress.ip_network(raw_subnet, strict=False)
        except ValueError:
            continue
        if isinstance(network, ipaddress.IPv4Network) and network.is_private and network not in subnets:
            subnets.append(network)

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


def lookup_device_local_ip(config: AppConfig, device_id: str) -> dict[str, Any]:
    clean_device_id = device_id.strip()
    if not clean_device_id:
        raise ConfigError("Укажите device ID")

    stored = get_device_row(config, clean_device_id)
    if not stored:
        raise ConfigError("Устройство с таким device ID не найдено в системе. Сначала добавьте для него профиль.")

    control_device = get_control_device(config, clean_device_id)
    if not control_device:
        raise ConfigError("Для устройства не найдены параметры локального подключения.")

    local_key = str(control_device.local_key or "").strip()
    if not local_key:
        raise ConfigError("Для устройства не задан local key, локальный поиск IP невозможен.")

    capabilities = get_device_capabilities(config, clean_device_id)
    ip_address = str(control_device.ip_address or "").strip()
    version = float(control_device.version or 3.5)
    connection_ready = bool(ip_address)
    connection_message = ""

    try:
        resolved_ip_address, resolved_version = _discover_local_endpoint(config, clean_device_id, local_key)
        ip_address = resolved_ip_address
        version = resolved_version
        connection_ready = True
        update_device_connection_endpoint(
            config,
            clean_device_id,
            ip_address=ip_address,
            version=version,
        )
    except ConfigError as error:
        if str(error) != LOCAL_DISCOVERY_ERROR_MESSAGE:
            raise
        if connection_ready:
            connection_message = "Не удалось подтвердить новый IP, использую сохраненный локальный адрес."
        else:
            connection_message = "Локальный IP по device ID не найден. Устройство останется в облачном режиме."

    power_options, visualization_options = _build_summary_options({}, capabilities)
    summary_total_power_dps_key = str(stored.get("total_power_dps_key") or control_device.total_power_dps_key or "").strip() or None
    summary_visualized_codes = [str(code) for code in (stored.get("visualized_codes") or control_device.visualized_codes or [])]
    summary_power_type = str(stored.get("power_type") or control_device.power_type or "total").strip().lower() or "total"
    device_kind = str(stored.get("device_kind") or "sensor")

    return {
        "name": stored.get("name") or control_device.name,
        "device_id": clean_device_id,
        "device_kind": device_kind,
        "device_kind_label": DEVICE_KIND_LABELS.get(device_kind, device_kind),
        "is_energy_meter": bool(stored.get("is_energy_meter")),
        "ip_address": ip_address or None,
        "version": version if connection_ready else None,
        "product_name": stored.get("product_name"),
        "category_code": stored.get("category_code"),
        "capability_count": len(capabilities),
        "connection_ready": connection_ready,
        "connection_message": connection_message,
        "summary_config": {
            "total_power_dps_key": summary_total_power_dps_key,
            "visualized_codes": summary_visualized_codes,
            "power_type": summary_power_type,
            "power_options": power_options,
            "visualization_options": visualization_options,
        },
    }
