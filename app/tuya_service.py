from datetime import datetime, timezone
from typing import Any

import tinytuya

from config import TuyaDeviceConfig


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
        float(dps[key])
        for key in device_config.voltage_dps_keys
        if key in dps and dps[key] is not None
    ]
    voltage_v = sum(voltage_values) / len(voltage_values) if voltage_values else None
    return power_w, voltage_v, dps


def build_sample(device_config: TuyaDeviceConfig) -> tuple[datetime, float, float | None, dict[str, Any]]:
    payload = fetch_status(device_config)
    power_w, voltage_v, raw_dps = extract_metrics(device_config, payload)
    return datetime.now(timezone.utc), power_w, voltage_v, raw_dps