import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


@dataclass(slots=True)
class TuyaDeviceConfig:
    slug: str
    name: str
    room: str
    image_label: str
    device_id: str
    local_key: str
    ip_address: str
    version: float
    power_dps_key: str
    power_scale: float
    voltage_dps_keys: tuple[str, ...]
    image_id: str | None = None


@dataclass(slots=True)
class AppConfig:
    home_name: str
    host: str
    port: int
    poll_interval_seconds: int
    sample_write_interval_seconds: int
    database_url: str
    timezone: str
    tariff_per_kwh: float


@dataclass(slots=True)
class TuyaCloudConfig:
    region: str
    api_key: str
    api_secret: str
    api_device_id: str | None


def _load_dotenv_file() -> dict[str, str]:
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return {}

    values: dict[str, str] = {}

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip('"').strip("'")

    return values


def _read_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    raise ConfigError(f"Environment variable {name} is required")


def _read_required_value(dotenv_values: dict[str, str], name: str) -> str:
    value = _read_value(dotenv_values, name)
    if value:
        return value
    raise ConfigError(f"Environment variable {name} is required")


def _slugify(value: str) -> str:
    normalized = "".join(char.lower() if char.isalnum() else "-" for char in value)
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return normalized.strip("-") or "device"


def _read_value(dotenv_values: dict[str, str], name: str, default: str = "") -> str:
    value = dotenv_values.get(name, os.getenv(name, default)).strip()
    return value or default


def _read_json_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ConfigError(f"Expected a list in {path.name}")
    return [item for item in data if isinstance(item, dict)]


def _parse_device_record(
    record: dict[str, Any],
    *,
    fallback_name: str,
    fallback_room: str,
    fallback_label: str,
    fallback_power_dps_key: str,
    fallback_power_scale: float,
    fallback_voltage_dps_keys: tuple[str, ...],
) -> TuyaDeviceConfig:
    name = str(record.get("name") or fallback_name).strip() or fallback_name
    slug = str(record.get("slug") or _slugify(name)).strip() or _slugify(name)
    room = str(record.get("room") or fallback_room).strip() or fallback_room
    image_label = str(record.get("image_label") or fallback_label).strip() or fallback_label
    image_id = str(record.get("image_id") or "").strip() or None
    device_id = str(record.get("device_id") or record.get("id") or "").strip()
    local_key = str(record.get("local_key") or record.get("key") or "").strip()
    ip_address = str(record.get("ip_address") or record.get("ip") or "").strip()
    version = float(record.get("version") or 3.5)
    power_dps_key = str(record.get("power_dps_key") or fallback_power_dps_key).strip()
    power_scale = float(record.get("power_scale") or fallback_power_scale)
    raw_voltage_keys = record.get("voltage_dps_keys") or fallback_voltage_dps_keys

    if isinstance(raw_voltage_keys, str):
        voltage_dps_keys = tuple(
            key.strip() for key in raw_voltage_keys.split(",") if key.strip()
        )
    else:
        voltage_dps_keys = tuple(str(key).strip() for key in raw_voltage_keys if str(key).strip())

    if not device_id or not local_key or not ip_address:
        raise ConfigError(f"Device '{name}' is missing id, local key, or ip address")

    return TuyaDeviceConfig(
        slug=slug,
        name=name,
        room=room,
        image_label=image_label,
        device_id=device_id,
        local_key=local_key,
        ip_address=ip_address,
        version=version,
        power_dps_key=power_dps_key,
        power_scale=power_scale,
        voltage_dps_keys=voltage_dps_keys,
        image_id=image_id,
    )


def load_config() -> dict[str, str]:
    dotenv_values = _load_dotenv_file()

    return {
        "device_id": _read_value(dotenv_values, "TUYA_DEVICE_ID") or _read_required("TUYA_DEVICE_ID"),
        "local_key": _read_value(dotenv_values, "TUYA_LOCAL_KEY") or _read_required("TUYA_LOCAL_KEY"),
        "ip_address": _read_value(dotenv_values, "TUYA_IP_ADDRESS") or _read_required("TUYA_IP_ADDRESS"),
        "version": _read_value(dotenv_values, "TUYA_VERSION", "3.3"),
    }


def load_app_config() -> AppConfig:
    dotenv_values = _load_dotenv_file()

    return AppConfig(
        home_name=_read_value(dotenv_values, "HOME_NAME", "Shunkov Power Hub"),
        host=_read_value(dotenv_values, "APP_HOST", "0.0.0.0"),
        port=int(_read_value(dotenv_values, "APP_PORT", "8484")),
        poll_interval_seconds=int(_read_value(dotenv_values, "POLL_INTERVAL_SECONDS", "1")),
        sample_write_interval_seconds=int(_read_value(dotenv_values, "SAMPLE_WRITE_INTERVAL_SECONDS", "5")),
        database_url=_read_required_value(dotenv_values, "DATABASE_URL"),
        timezone=_read_value(dotenv_values, "APP_TIMEZONE", "Europe/Warsaw"),
        tariff_per_kwh=float(_read_value(dotenv_values, "ENERGY_TARIFF_PER_KWH", "1.12")),
    )


def load_cloud_config(required: bool = False) -> TuyaCloudConfig | None:
    dotenv_values = _load_dotenv_file()
    api_key = _read_value(dotenv_values, "TUYA_CLOUD_API_KEY")
    api_secret = _read_value(dotenv_values, "TUYA_CLOUD_API_SECRET")
    region = _read_value(dotenv_values, "TUYA_CLOUD_API_REGION", "eu")
    api_device_id = _read_value(dotenv_values, "TUYA_CLOUD_API_DEVICE_ID") or None

    if not api_key or not api_secret:
        if required:
            raise ConfigError(
                "Set TUYA_CLOUD_API_KEY and TUYA_CLOUD_API_SECRET to run cloud history sync"
            )
        return None

    return TuyaCloudConfig(
        region=region,
        api_key=api_key,
        api_secret=api_secret,
        api_device_id=api_device_id,
    )


def load_devices() -> list[TuyaDeviceConfig]:
    dotenv_values = _load_dotenv_file()
    catalog_file = Path(_read_value(dotenv_values, "APP_DEVICES_FILE", "devices.catalog.json"))
    fallback_name = _read_value(dotenv_values, "TUYA_DEVICE_NAME", "Breaker")
    fallback_room = _read_value(dotenv_values, "TUYA_DEVICE_ROOM", "Electrical room")
    fallback_label = _read_value(dotenv_values, "TUYA_DEVICE_IMAGE_LABEL", "Smart breaker")
    fallback_power_dps_key = _read_value(dotenv_values, "TUYA_POWER_DPS_KEY", "102")
    fallback_power_scale = float(_read_value(dotenv_values, "TUYA_POWER_SCALE", "100"))
    fallback_voltage_dps_keys = tuple(
        key.strip()
        for key in _read_value(dotenv_values, "TUYA_VOLTAGE_DPS_KEYS", "107,108,109").split(",")
        if key.strip()
    )

    if catalog_file.exists():
        return [
            _parse_device_record(
                record,
                fallback_name=fallback_name,
                fallback_room=fallback_room,
                fallback_label=fallback_label,
                fallback_power_dps_key=fallback_power_dps_key,
                fallback_power_scale=fallback_power_scale,
                fallback_voltage_dps_keys=fallback_voltage_dps_keys,
            )
            for record in _read_json_file(catalog_file)
        ]

    return [
        _parse_device_record(
            {
                "name": fallback_name,
                "room": fallback_room,
                "image_label": fallback_label,
                "device_id": _read_value(dotenv_values, "TUYA_DEVICE_ID") or _read_required("TUYA_DEVICE_ID"),
                "local_key": _read_value(dotenv_values, "TUYA_LOCAL_KEY") or _read_required("TUYA_LOCAL_KEY"),
                "ip_address": _read_value(dotenv_values, "TUYA_IP_ADDRESS") or _read_required("TUYA_IP_ADDRESS"),
                "version": _read_value(dotenv_values, "TUYA_VERSION", "3.5"),
            },
            fallback_name=fallback_name,
            fallback_room=fallback_room,
            fallback_label=fallback_label,
            fallback_power_dps_key=fallback_power_dps_key,
            fallback_power_scale=fallback_power_scale,
            fallback_voltage_dps_keys=fallback_voltage_dps_keys,
        )
    ]