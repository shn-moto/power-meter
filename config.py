import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass(slots=True)
class TuyaDeviceConfig:
    slug: str
    name: str
    room: str
    device_id: str
    local_key: str
    ip_address: str
    version: float
    power_dps_key: str
    power_scale: float
    voltage_dps_keys: tuple[str, ...]


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


def _read_required_value(dotenv_values: dict[str, str], name: str) -> str:
    value = _read_value(dotenv_values, name)
    if value:
        return value
    raise ConfigError(f"Environment variable {name} is required")


def _read_value(dotenv_values: dict[str, str], name: str, default: str = "") -> str:
    value = dotenv_values.get(name, os.getenv(name, default)).strip()
    return value or default


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
                "Set TUYA_CLOUD_API_KEY and TUYA_CLOUD_API_SECRET to connect devices from Tuya Cloud"
            )
        return None

    return TuyaCloudConfig(
        region=region,
        api_key=api_key,
        api_secret=api_secret,
        api_device_id=api_device_id,
    )
