from datetime import datetime, timezone
import base64
import socket
from typing import Any, Optional, Tuple, Dict, List

import tinytuya

from config import TuyaDeviceConfig


PHASE_VISUAL_DPS_GROUP = (6, 7, 8)
PRIVATE_TUYA_PORTS = (6668, 6669, 7000)


def _is_tuya_host_reachable(ip_address: str, timeout: float = 0.15) -> bool:
    for port in PRIVATE_TUYA_PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            if sock.connect_ex((ip_address, port)) == 0:
                return True
    return False


def _normalize_voltage(raw_value: Any) -> float:
    voltage = float(raw_value)
    # Some Tuya sockets report voltage in decivolts even when the schema scale is 0.
    if abs(voltage) >= 1000:
        return voltage / 10.0
    return voltage


def _read_reference_voltage(dps: dict[str, Any]) -> float | None:
    direct_voltage = dps.get("6")
    if direct_voltage is not None:
        try:
            return _normalize_voltage(direct_voltage)
        except (TypeError, ValueError):
            pass

    phase_values: list[float] = []
    for key in ("107", "108", "109"):
        if dps.get(key) is None:
            continue
        try:
            phase_values.append(_normalize_voltage(dps[key]))
        except (TypeError, ValueError):
            continue
    if not phase_values:
        return None
    return sum(phase_values) / len(phase_values)


def _normalize_power(power_w: float, dps: dict[str, Any]) -> float:
    # Some Tuya socket firmwares occasionally report cur_power 10x too high.
    voltage_v = _read_reference_voltage(dps)
    if power_w > 5000 and voltage_v is not None and 180 <= voltage_v <= 260:
        return power_w / 10.0
    return power_w


def _normalize_power_by_measurements(dps: dict[str, Any], power_w: float) -> float:
    try:
        current_raw = float(dps.get("4"))
    except (TypeError, ValueError):
        current_raw = None

    voltage_v = _read_reference_voltage(dps)
    if current_raw is None or current_raw <= 0 or voltage_v is None or voltage_v <= 0:
        return power_w

    current_a = current_raw / 1000.0 if current_raw > 10 else current_raw
    apparent_power_w = current_a * voltage_v
    if apparent_power_w <= 0:
        return power_w

    if power_w > apparent_power_w * 3 and (power_w / 10.0) <= apparent_power_w * 1.6:
        return power_w / 10.0

    return power_w


def _extract_phase_packet_power_w(raw_value: Any) -> float | None:
    if not isinstance(raw_value, str) or not raw_value:
        return None

    try:
        payload = base64.b64decode(raw_value)
    except Exception:
        return None

    if len(payload) not in {8, 10}:
        return None

    return float(int.from_bytes(payload[5:8], byteorder="big", signed=False))


def _get_visualized_request_indices(
    visualized_codes: tuple[str, ...],
    dps: dict[str, Any] | None = None,
) -> list[int]:
    selected_indices = sorted({int(code) for code in visualized_codes if str(code).isdigit()})
    if not selected_indices:
        return []

    present_keys = set(dps) if dps else set()
    return [index for index in selected_indices if str(index) not in present_keys]


def _fresh_tinytuya_device(device_config: TuyaDeviceConfig, timeout: float) -> tinytuya.Device:
    d = tinytuya.Device(
        device_config.device_id,
        device_config.ip_address,
        device_config.local_key,
        connection_timeout=timeout,
    )
    d.set_version(device_config.version)
    d.set_socketTimeout(timeout)
    d.set_socketRetryLimit(0)
    return d


def _probe_dps(device_config: TuyaDeviceConfig, probe_code: int, timeout: float) -> dict[str, Any] | None:
    """Open a fresh tinytuya session per probe to avoid Err 914 from a stale session."""
    d = _fresh_tinytuya_device(device_config, timeout)
    try:
        payload = d.updatedps(index=[probe_code], nowait=False)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    dps = payload.get("dps")
    return dps if isinstance(dps, dict) else None


def _trick678_1P(device_config: TuyaDeviceConfig, requested_code: int, timeout: float = 3.0) -> Any | None:
    """Probe DPS 6/7/8 sequentially; return the value of `requested_code`
    from whichever response carries it. Used for single-phase breakers
    whose phase A packet only updates when DPS 7 or 8 is queried."""
    for probe_code in PHASE_VISUAL_DPS_GROUP:
        dps = _probe_dps(device_config, probe_code, timeout)
        if not dps:
            continue
        value = dps.get(str(requested_code))
        if value is not None:
            return value
    return None


def _trick678_3P(device_config: TuyaDeviceConfig, requested_codes: list[int], timeout: float = 3.0) -> dict[str, Any]:
    """Probe DPS 6/7/8 in three sequential queries; collect whatever DPS keys
    each response carries and return values for codes in `requested_codes`."""
    collected: dict[str, Any] = {}
    targets = {str(code) for code in requested_codes}
    for probe_code in PHASE_VISUAL_DPS_GROUP:
        dps = _probe_dps(device_config, probe_code, timeout)
        if not dps:
            continue
        for key, value in dps.items():
            if value is None:
                continue
            if str(key) in targets and str(key) not in collected:
                collected[str(key)] = value
    return collected


def _uses_current_power(device_config: TuyaDeviceConfig) -> bool:
    return str(device_config.power_type or "total").strip().lower() == "current"


def _merge_missing_visualized_codes_once(
    device: tinytuya.Device,
    device_config: TuyaDeviceConfig,
    dps: dict[str, Any],
) -> dict[str, Any]:
    missing_indices = _get_visualized_request_indices(device_config.visualized_codes, dps)
    if not missing_indices:
        return dps

    merged = dict(dps)
    request_modes = device_config.dps_request_modes or {}

    # Group missing visualized codes by their request_mode (or "default")
    by_mode: dict[str, list[int]] = {}
    for index in missing_indices:
        mode = request_modes.get(str(index), "default")
        by_mode.setdefault(mode, []).append(index)

    for mode, indices in by_mode.items():
        if mode == "trick678_1P":
            for code in indices:
                value = _trick678_1P(device_config, code)
                if value is not None:
                    merged[str(code)] = value
        elif mode == "trick678_3P":
            collected = _trick678_3P(device_config, indices)
            merged.update(collected)
        else:
            device.set_socketTimeout(0.25)
            device.set_socketRetryLimit(0)
            try:
                payload = device.updatedps(index=indices, nowait=False)
            except Exception:
                continue
            if not isinstance(payload, dict) or not isinstance(payload.get("dps"), dict):
                continue
            for index in indices:
                value = payload["dps"].get(str(index))
                if value is not None:
                    merged[str(index)] = value

    return merged


def _has_trick678_modes(device_config: TuyaDeviceConfig) -> bool:
    for mode in (device_config.dps_request_modes or {}).values():
        if isinstance(mode, str) and mode.startswith("trick678_"):
            return True
    return False


def _piggyback_phase_probes(device: tinytuya.Device, dps: dict[str, Any]) -> dict[str, Any]:
    """Probe DPS 6, 7, 8 on the SAME tinytuya.Device session that was just
    used for status(). Reusing the open socket avoids triggering Tuya's
    per-session anti-spam (which refuses fresh connections that arrive
    immediately after status())."""
    import logging
    logger = logging.getLogger(__name__)
    device.set_socketTimeout(1.5)
    device.set_socketRetryLimit(0)
    merged = dict(dps)
    for probe_index in PHASE_VISUAL_DPS_GROUP:
        try:
            response = device.updatedps(index=[probe_index], nowait=False)
        except Exception as exc:
            logger.warning("piggyback probe %s exception: %s", probe_index, exc)
            continue
        if not isinstance(response, dict):
            logger.warning("piggyback probe %s non-dict %r", probe_index, response)
            continue
        response_dps = response.get("dps")
        if isinstance(response_dps, dict) and response_dps:
            logger.warning("piggyback probe %s OK keys=%s", probe_index, sorted(response_dps.keys()))
            merged.update(response_dps)
        else:
            logger.warning("piggyback probe %s -> %r", probe_index, response)
    return merged


def fetch_status(device_config: TuyaDeviceConfig, *, include_visualized_codes: bool = False) -> dict[str, Any]:
    if not device_config.ip_address or not _is_tuya_host_reachable(device_config.ip_address):
        raise RuntimeError(f"Device {device_config.device_id} is offline")

    needs_piggyback = _has_trick678_modes(device_config)
    device = tinytuya.Device(
        device_config.device_id,
        device_config.ip_address,
        device_config.local_key,
        connection_timeout=5.0,
    )
    device.set_version(device_config.version)
    device.set_socketTimeout(5.0)
    device.set_socketRetryLimit(2)
    if needs_piggyback:
        # Keep the TCP socket alive so the follow-up updatedps probes can
        # reuse the same Tuya session — without persistence the breaker
        # rejects each fresh connect with Err 905.
        device.set_socketPersistent(True)
    try:
        payload = device.status()
        if isinstance(payload, dict) and isinstance(payload.get("dps"), dict):
            if needs_piggyback:
                payload = {
                    **payload,
                    "dps": _piggyback_phase_probes(device, payload.get("dps") or {}),
                }
            if include_visualized_codes:
                payload = {
                    **payload,
                    "dps": _merge_missing_visualized_codes_once(device, device_config, payload.get("dps") or {}),
                }
            return payload
    finally:
        if needs_piggyback:
            try:
                device.close()
            except Exception:
                pass

    return payload


def extract_metrics(device_config: TuyaDeviceConfig, payload: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    dps = payload.get("dps")
    if not isinstance(dps, dict):
        raise ValueError("Device payload does not contain DPS data")

    if not device_config.total_power_dps_key:
        raise ValueError("Power DPS key is not configured")

    if dps.get(device_config.total_power_dps_key) is None:
        raise ValueError("Selected power DPS key is missing in device payload")

    power_raw = dps.get(device_config.total_power_dps_key)
    power_scale = max(float(device_config.total_power_scale or 1.0), 1.0)

    if _uses_current_power(device_config):
        phase_packet_power_w = _extract_phase_packet_power_w(power_raw)
        if phase_packet_power_w is not None:
            return phase_packet_power_w, dps

        current_power_w = float(power_raw) / power_scale if power_raw is not None else 0.0
        current_power_w = _normalize_power(current_power_w, dps)
        current_power_w = _normalize_power_by_measurements(dps, current_power_w)
        return current_power_w, dps

    power_w = float(power_raw) / power_scale if power_raw is not None else 0.0
    return power_w, dps


def build_sample(device_config: TuyaDeviceConfig) -> tuple[datetime, float, dict[str, Any]]:
    payload = fetch_status(device_config, include_visualized_codes=False)
    power_w, raw_dps = extract_metrics(device_config, payload)
    return datetime.now(timezone.utc), power_w, raw_dps


def build_live_sample(device_config: TuyaDeviceConfig) -> tuple[datetime, float, dict[str, Any]]:
    payload = fetch_status(device_config, include_visualized_codes=True)
    power_w, raw_dps = extract_metrics(device_config, payload)
    return datetime.now(timezone.utc), power_w, raw_dps


def request_dps_by_index(
    device_id: str,
    ip_address: str,
    local_key: str,
    dps_indices: List[int],
    version: float = 3.5,
    dev_type: str = "default",
    timeout: float = 5.0,
) -> Tuple[Dict[str, Any], Optional[float]]:
    """
    Request specific DPS values from a Tuya device via LAN.

    This function queries specific DPS indices using tinytuya. The recommended
    approach is to use status() which queries all DPS values, then filter locally.
    Some devices support UPDATEDPS for targeted queries, but results vary by
    firmware version.

    Args:
        device_id: Tuya device ID
        ip_address: Local IP address of the device
        local_key: Local encryption key
        dps_indices: List of DPS indices to request (e.g. [1, 6, 7, 8, 16, 101])
        version: Protocol version (3.3, 3.4, 3.5). Default 3.5.
        dev_type: Device type ('default' or 'device22'). Default 'default'.
        timeout: Socket timeout in seconds

    Returns:
        Tuple of (payload dict with requested DPS values, detected version)

    Raises:
        RuntimeError: If unable to retrieve DPS data

    Note:
        Most Tuya devices respond best to a full status() query. The UPDATEDPS
        command (method updatedps()) may return limited or no data depending on
        device firmware. This function tries multiple approaches to maximize
        compatibility.
    """
    device = tinytuya.Device(
        device_id,
        ip_address,
        local_key,
        dev_type=dev_type,
        connection_timeout=timeout,
    )
    device.set_version(version)
    device.set_socketTimeout(timeout)
    device.set_socketRetryLimit(2)

    results = {}
    detected_version = None

    # Approach 1: Try UPDATEDPS (command 18) for targeted query
    # Some devices return data for requested indices, others return different DPS
    try:
        payload = device.updatedps(index=dps_indices, nowait=False)
        if isinstance(payload, dict) and "dps" in payload:
            # Return whatever DPS the device gave us
            results.update(payload.get("dps", {}))
            detected_version = version
            # Filter to requested indices if possible
            filtered = {str(idx): results.get(str(idx)) for idx in dps_indices if str(idx) in results}
            if filtered:
                return filtered, detected_version
            if results:
                return results, detected_version
    except Exception:
        pass

    # Approach 2: Full status() query (command 10) - most reliable
    # Query all DPS and filter locally
    try:
        payload = device.status()
        if isinstance(payload, dict):
            if "dps" in payload:
                all_dps = payload.get("dps", {})
                # Filter for requested indices
                for idx in dps_indices:
                    key = str(idx)
                    if key in all_dps:
                        results[key] = all_dps[key]
                detected_version = version
                if results:
                    return results, detected_version
                # If no requested indices found, return all DPS
                if all_dps:
                    return all_dps, detected_version
    except Exception:
        pass

    # Approach 3: Try device22 protocol
    if dev_type == "default":
        for try_type in ["device22"]:
            try:
                device2 = tinytuya.Device(
                    device_id, ip_address, local_key,
                    dev_type=try_type, connection_timeout=timeout,
                )
                device2.set_version(version)
                device2.set_socketTimeout(timeout)
                device2.set_socketRetryLimit(2)
                payload = device2.status()
                if isinstance(payload, dict) and "dps" in payload:
                    all_dps = payload.get("dps", {})
                    for idx in dps_indices:
                        key = str(idx)
                        if key in all_dps:
                            results[key] = all_dps[key]
                    detected_version = version
                    if results:
                        return results, detected_version
            except Exception:
                continue

    # Approach 4: Try UPDATEDPS with individual indices
    if not results:
        for idx in dps_indices:
            try:
                device_single = tinytuya.Device(
                    device_id, ip_address, local_key,
                    dev_type=dev_type, connection_timeout=timeout,
                )
                device_single.set_version(version)
                device_single.set_socketTimeout(timeout)
                payload = device_single.updatedps(index=[idx], nowait=False)
                if isinstance(payload, dict) and "dps" in payload:
                    results.update(payload.get("dps", {}))
                    detected_version = version
            except Exception:
                continue
        if results:
            return results, detected_version

    if not results:
        raise RuntimeError("Unable to retrieve DPS data from device")

    return results, detected_version


def extract_dps_by_index(
    device_config: TuyaDeviceConfig,
    dps_indices: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """
    Extract specific DPS values from a configured device.

    Args:
        device_config: Device configuration
        dps_indices: List of DPS indices to query. If None, queries common indices
                     from 3Fauto.json: [1, 6, 7, 8, 16, 101]

    Returns:
        Dictionary mapping DPS codes to their values
    """
    if dps_indices is None:
        dps_indices = [1, 6, 7, 8, 16, 101]

    if not device_config.ip_address:
        raise ValueError("Device IP address is not configured")

    payload, version = request_dps_by_index(
        device_id=device_config.device_id,
        ip_address=device_config.ip_address,
        local_key=device_config.local_key,
        dps_indices=dps_indices,
        version=device_config.version,
        dev_type="default",
        timeout=5.0,
    )

    return {
        "dps": payload,
        "version": version,
        "device_id": device_config.device_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }