from datetime import datetime, timezone
from typing import Any

import tinytuya

from config import TuyaDeviceConfig


def _normalize_voltage(raw_value: Any) -> float:
    voltage = float(raw_value)
    # Some Tuya sockets report voltage in decivolts even when the schema scale is 0.
    if abs(voltage) >= 1000:
        return voltage / 10.0
    return voltage


def _normalize_power(power_w: float, voltage_v: float | None) -> float:
    # Some Tuya socket firmwares occasionally report cur_power 10x too high.
    if power_w > 5000 and voltage_v is not None and 180 <= voltage_v <= 260:
        return power_w / 10.0
    return power_w


def _normalize_power_by_measurements(dps: dict[str, Any], power_w: float, voltage_v: float | None) -> float:
    try:
        current_raw = float(dps.get("4"))
    except (TypeError, ValueError):
        current_raw = None

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


def extract_metrics(device_config: TuyaDeviceConfig, payload: dict[str, Any]) -> tuple[float, float | None, dict[str, Any]]:
    dps = payload.get("dps")
    if not isinstance(dps, dict):
        raise ValueError("Device payload does not contain DPS data")

    power_raw = dps.get(device_config.power_dps_key, 0)
    power_w = float(power_raw) / device_config.power_scale if power_raw is not None else 0.0
    voltage_values = [
        _normalize_voltage(dps[key])
        for key in device_config.voltage_dps_keys
        if key in dps and dps[key] is not None
    ]
    voltage_v = sum(voltage_values) / len(voltage_values) if voltage_values else None
    power_w = _normalize_power_by_measurements(dps, power_w, voltage_v)
    power_w = _normalize_power(power_w, voltage_v)
    return power_w, voltage_v, dps


def build_sample(device_config: TuyaDeviceConfig) -> tuple[datetime, float, float | None, dict[str, Any]]:
    payload = fetch_status(device_config)
    power_w, voltage_v, raw_dps = extract_metrics(device_config, payload)
    return datetime.now(timezone.utc), power_w, voltage_v, raw_dps