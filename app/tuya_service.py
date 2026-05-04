from datetime import datetime, timezone
from typing import Any, Optional, Tuple, Dict, List

import tinytuya

from config import TuyaDeviceConfig


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


def fetch_status(device_config: TuyaDeviceConfig) -> dict[str, Any]:
    device = tinytuya.Device(
        device_config.device_id,
        device_config.ip_address,
        device_config.local_key,
    )
    device.set_version(device_config.version)
    return device.status()


def _merge_missing_visualized_codes(device_config: TuyaDeviceConfig, dps: dict[str, Any]) -> dict[str, Any]:
    selected_indices = sorted({int(code) for code in device_config.visualized_codes if str(code).isdigit()})
    if not selected_indices:
        return dps

    missing_indices = [index for index in selected_indices if str(index) not in dps]
    if not missing_indices:
        return dps

    extra_dps, _ = request_dps_by_index(
        device_id=device_config.device_id,
        ip_address=device_config.ip_address,
        local_key=device_config.local_key,
        dps_indices=missing_indices,
        version=device_config.version,
        dev_type="default",
        timeout=5.0,
    )
    merged = dict(dps)
    merged.update({str(key): value for key, value in extra_dps.items()})
    return merged


def extract_metrics(device_config: TuyaDeviceConfig, payload: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    dps = payload.get("dps")
    if not isinstance(dps, dict):
        raise ValueError("Device payload does not contain DPS data")

    dps = _merge_missing_visualized_codes(device_config, dps)

    if not device_config.total_power_dps_key:
        raise ValueError("Total power DPS key is not configured")

    if dps.get(device_config.total_power_dps_key) is None:
        raise ValueError("Selected total power DPS key is missing in device payload")

    power_raw = dps.get(device_config.total_power_dps_key)
    power_w = float(power_raw) / device_config.total_power_scale if power_raw is not None else 0.0
    return power_w, dps


def build_sample(device_config: TuyaDeviceConfig) -> tuple[datetime, float, dict[str, Any]]:
    payload = fetch_status(device_config)
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