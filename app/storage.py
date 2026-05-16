import calendar
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from config import AppConfig, TuyaDeviceConfig


RUSSIAN_MONTH_LABELS_SHORT = {
    1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
    7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек",
}

RUSSIAN_MONTH_LABELS_FULL = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}


MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles" / "devices"


@dataclass(slots=True)
class DeviceSample:
    device_id: str
    captured_at: datetime
    power_w: float
    raw_dps: dict[str, Any]
    source: str = "live"


_connection_pool: ConnectionPool | None = None
_connection_pool_url: str | None = None


def init_connection_pool(database_url: str, *, min_size: int = 1, max_size: int = 10) -> None:
    global _connection_pool, _connection_pool_url

    if _connection_pool is not None and _connection_pool_url == database_url:
        return

    close_connection_pool()
    _connection_pool = ConnectionPool(
        conninfo=database_url,
        kwargs={"row_factory": dict_row},
        min_size=min_size,
        max_size=max_size,
        open=False,
    )
    _connection_pool.open(wait=True)
    _connection_pool_url = database_url


def close_connection_pool() -> None:
    global _connection_pool, _connection_pool_url

    if _connection_pool is not None:
        _connection_pool.close()
    _connection_pool = None
    _connection_pool_url = None


def _connect(database_url: str):
    if _connection_pool is not None and _connection_pool_url == database_url:
        return _connection_pool.connection()
    return psycopg.connect(database_url, row_factory=dict_row)


def _normalize_username(username: str) -> str:
    return str(username or "").strip().lower()


def get_user_by_username(config: AppConfig, username: str) -> dict[str, Any] | None:
    normalized_username = _normalize_username(username)
    if not normalized_username:
        return None

    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, username, password_hash, is_admin, created_at, updated_at
                FROM app_users
                WHERE username = %s
                """,
                (normalized_username,),
            )
            return cursor.fetchone()


def create_user(
    config: AppConfig,
    *,
    username: str,
    password_hash: str,
    is_admin: bool = False,
) -> dict[str, Any] | None:
    normalized_username = _normalize_username(username)
    if not normalized_username:
        raise ValueError("Username is required")
    if not password_hash:
        raise ValueError("Password hash is required")

    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO app_users (username, password_hash, is_admin, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT DO NOTHING
                RETURNING id, username, password_hash, is_admin, created_at, updated_at
                """,
                (normalized_username, password_hash, is_admin),
            )
            user = cursor.fetchone()
        connection.commit()

    return user


def _split_sql_statements(text: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        current.append(line)
        if line.rstrip().endswith(";"):
            stmt = "\n".join(current).strip()
            if stmt and stmt != ";":
                statements.append(stmt)
            current = []
    tail = "\n".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def apply_migrations(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cursor.execute("SELECT version FROM schema_migrations")
            applied: set[str] = {row[0] for row in cursor.fetchall()}

    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = path.stem
        if version in applied:
            continue

        statements = _split_sql_statements(path.read_text(encoding="utf-8"))
        with psycopg.connect(database_url, autocommit=True) as connection:
            with connection.cursor() as cursor:
                for statement in statements:
                    cursor.execute(statement)
                cursor.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)",
                    (version,),
                )


def init_db(config: AppConfig) -> None:
    apply_migrations(config.database_url)


def sync_devices(config: AppConfig, devices: list[TuyaDeviceConfig]) -> None:
    if not devices:
        return
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO devices (
                    name, room, device_id, category_code, device_kind,
                    is_energy_meter, product_id, product_name, icon, onboarding_source, updated_at,
                    total_power_dps_key, total_power_scale, power_type, visualized_codes
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s, %s)
                ON CONFLICT(device_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    room = EXCLUDED.room,
                    device_kind = EXCLUDED.device_kind,
                    is_energy_meter = EXCLUDED.is_energy_meter,
                    onboarding_source = EXCLUDED.onboarding_source,
                    updated_at = NOW(),
                    total_power_dps_key = EXCLUDED.total_power_dps_key,
                    total_power_scale = EXCLUDED.total_power_scale,
                    power_type = EXCLUDED.power_type,
                    visualized_codes = EXCLUDED.visualized_codes
                """,
                [
                    (
                        device.name,
                        device.room,
                        device.device_id,
                        None,
                        "meter",
                        True,
                        None,
                        None,
                        None,
                        "config",
                        device.total_power_dps_key or None,
                        device.total_power_scale,
                        device.power_type,
                        Jsonb(list(device.visualized_codes)),
                    )
                    for device in devices
                ],
            )
            cursor.executemany(
                """
                INSERT INTO device_connections (
                    device_id, local_key, ip_address, version, total_power_dps_key, total_power_scale, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT(device_id) DO UPDATE SET
                    local_key = EXCLUDED.local_key,
                    ip_address = CASE
                        WHEN EXCLUDED.ip_address <> '' THEN EXCLUDED.ip_address
                        ELSE device_connections.ip_address
                    END,
                    version = CASE
                        WHEN EXCLUDED.ip_address <> '' THEN EXCLUDED.version
                        ELSE device_connections.version
                    END,
                    total_power_dps_key = EXCLUDED.total_power_dps_key,
                    total_power_scale = EXCLUDED.total_power_scale,
                    updated_at = NOW()
                """,
                [
                    (
                        device.device_id,
                        device.local_key,
                        device.ip_address,
                        device.version,
                        device.total_power_dps_key or None,
                        device.total_power_scale,
                    )
                    for device in devices
                ],
            )
        connection.commit()


def upsert_managed_device(
    config: AppConfig,
    *,
    name: str,
    room: str,
    device_id: str,
    category_code: str | None,
    device_kind: str,
    is_energy_meter: bool,
    is_charger: bool,
    product_id: str | None,
    product_name: str | None,
    icon: str | None,
    onboarding_source: str,
    local_key: str,
    ip_address: str,
    version: float,
    total_power_dps_key: str | None,
    total_power_scale: float,
    power_type: str = "total",
    visualized_codes: list[str] | tuple[str, ...],
    capabilities: list[dict[str, Any]],
) -> None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO devices (
                    name, room, device_id, category_code, device_kind,
                    is_energy_meter, is_charger, product_id, product_name, icon, onboarding_source, updated_at,
                    total_power_dps_key, total_power_scale, power_type, visualized_codes
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s, %s)
                ON CONFLICT(device_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    room = CASE
                        WHEN EXCLUDED.onboarding_source = 'profile' THEN EXCLUDED.room
                        WHEN devices.room = '' OR devices.room = 'Без комнаты' THEN EXCLUDED.room
                        ELSE devices.room
                    END,
                    category_code = EXCLUDED.category_code,
                    device_kind = EXCLUDED.device_kind,
                    is_energy_meter = EXCLUDED.is_energy_meter,
                    is_charger = EXCLUDED.is_charger,
                    product_id = EXCLUDED.product_id,
                    product_name = EXCLUDED.product_name,
                    icon = EXCLUDED.icon,
                    onboarding_source = EXCLUDED.onboarding_source,
                    updated_at = NOW(),
                    total_power_dps_key = EXCLUDED.total_power_dps_key,
                    total_power_scale = EXCLUDED.total_power_scale,
                    power_type = EXCLUDED.power_type,
                    visualized_codes = EXCLUDED.visualized_codes
                """,
                (
                    name,
                    room,
                    device_id,
                    category_code,
                    device_kind,
                    is_energy_meter,
                    is_charger,
                    product_id,
                    product_name,
                    icon,
                    onboarding_source,
                    total_power_dps_key,
                    total_power_scale,
                    power_type,
                    Jsonb(list(visualized_codes)),
                ),
            )
            cursor.execute(
                """
                INSERT INTO device_connections (
                    device_id, local_key, ip_address, version, total_power_dps_key, total_power_scale, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT(device_id) DO UPDATE SET
                    local_key = EXCLUDED.local_key,
                    ip_address = CASE
                        WHEN EXCLUDED.ip_address <> '' THEN EXCLUDED.ip_address
                        ELSE device_connections.ip_address
                    END,
                    version = CASE
                        WHEN EXCLUDED.ip_address <> '' THEN EXCLUDED.version
                        ELSE device_connections.version
                    END,
                    total_power_dps_key = EXCLUDED.total_power_dps_key,
                    total_power_scale = EXCLUDED.total_power_scale,
                    updated_at = NOW()
                """,
                (
                    device_id,
                    local_key,
                    ip_address,
                    version,
                    total_power_dps_key,
                    total_power_scale,
                ),
            )
            cursor.execute("DELETE FROM device_capabilities WHERE device_id = %s", (device_id,))
            if capabilities:
                cursor.executemany(
                    """
                    INSERT INTO device_capabilities (
                        device_id, capability_source, capability_code, capability_name,
                        value_type, dp_id, values_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            device_id,
                            capability.get("capability_source") or "status",
                            capability.get("capability_code") or "unknown",
                            capability.get("capability_name"),
                            capability.get("value_type"),
                            capability.get("dp_id"),
                            Jsonb(capability.get("values_json") or {}),
                        )
                        for capability in capabilities
                    ],
                )
        connection.commit()


def refresh_managed_device_cloud_data(
    config: AppConfig,
    *,
    device_id: str,
    name: str,
    room: str,
    category_code: str | None,
    device_kind: str,
    is_energy_meter: bool,
    product_id: str | None,
    product_name: str | None,
    icon: str | None,
    onboarding_source: str,
    total_power_dps_key: str | None,
    total_power_scale: float,
    power_type: str = "total",
    visualized_codes: list[str] | tuple[str, ...],
    capabilities: list[dict[str, Any]],
) -> None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE devices
                SET name = %s,
                    room = %s,
                    category_code = %s,
                    device_kind = %s,
                    is_energy_meter = %s,
                    product_id = %s,
                    product_name = %s,
                    icon = %s,
                    onboarding_source = %s,
                    updated_at = NOW(),
                    total_power_dps_key = %s,
                    total_power_scale = %s,
                    power_type = %s,
                    visualized_codes = %s
                WHERE device_id = %s
                """,
                (
                    name,
                    room,
                    category_code,
                    device_kind,
                    is_energy_meter,
                    product_id,
                    product_name,
                    icon,
                    onboarding_source,
                    total_power_dps_key,
                    total_power_scale,
                    power_type,
                    Jsonb(list(visualized_codes)),
                    device_id,
                ),
            )
            cursor.execute(
                """
                UPDATE device_connections
                SET total_power_dps_key = %s,
                    total_power_scale = %s,
                    updated_at = NOW()
                WHERE device_id = %s
                """,
                (
                    total_power_dps_key,
                    total_power_scale,
                    device_id,
                ),
            )
            cursor.execute("DELETE FROM device_capabilities WHERE device_id = %s", (device_id,))
            if capabilities:
                cursor.executemany(
                    """
                    INSERT INTO device_capabilities (
                        device_id, capability_source, capability_code, capability_name,
                        value_type, dp_id, values_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            device_id,
                            capability.get("capability_source") or "status",
                            capability.get("capability_code") or "unknown",
                            capability.get("capability_name"),
                            capability.get("value_type"),
                            capability.get("dp_id"),
                            Jsonb(capability.get("values_json") or {}),
                        )
                        for capability in capabilities
                    ],
                )
        connection.commit()


def save_sample(config: AppConfig, sample: DeviceSample) -> None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO samples (device_id, captured_at, power_w, raw_dps, source)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (device_id, captured_at, source) DO UPDATE SET
                    power_w = EXCLUDED.power_w,
                    raw_dps = EXCLUDED.raw_dps
                """,
                (
                    sample.device_id,
                    sample.captured_at,
                    sample.power_w,
                    Jsonb(sample.raw_dps),
                    sample.source,
                ),
            )
        connection.commit()
    _refresh_aggregate_windows(config.database_url, sample.captured_at)


def save_samples_batch(config: AppConfig, samples: list[DeviceSample]) -> None:
    if not samples:
        return
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO samples (device_id, captured_at, power_w, raw_dps, source)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (device_id, captured_at, source) DO UPDATE SET
                    power_w = EXCLUDED.power_w,
                    raw_dps = EXCLUDED.raw_dps
                """,
                [
                    (
                        sample.device_id,
                        sample.captured_at,
                        sample.power_w,
                        Jsonb(sample.raw_dps),
                        sample.source,
                    )
                    for sample in samples
                ],
            )
        connection.commit()
    captured_at_values = [sample.captured_at for sample in samples]
    _refresh_aggregate_windows(config.database_url, min(captured_at_values), max(captured_at_values))


def _start_of_hour(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)


def _start_of_day(value: datetime) -> datetime:
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def _start_of_month(value: datetime) -> datetime:
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _start_of_next_month(value: datetime) -> datetime:
    month_start = _start_of_month(value)
    if month_start.month == 12:
        return month_start.replace(year=month_start.year + 1, month=1)
    return month_start.replace(month=month_start.month + 1)


def _refresh_aggregate_windows(database_url: str, captured_at_start: datetime, captured_at_end: datetime | None = None) -> None:
    end_value = captured_at_end or captured_at_start
    hourly_start = _start_of_hour(captured_at_start)
    hourly_end = _start_of_hour(end_value) + timedelta(hours=1)
    daily_start = _start_of_day(captured_at_start)
    daily_end = _start_of_day(end_value) + timedelta(days=1)
    monthly_start = _start_of_month(captured_at_start)
    monthly_end = _start_of_next_month(end_value)

    with psycopg.connect(database_url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("CALL refresh_continuous_aggregate('samples_hourly', %s, %s)", (hourly_start, hourly_end))
            cursor.execute("CALL refresh_continuous_aggregate('samples_daily', %s, %s)", (daily_start, daily_end))
            cursor.execute("CALL refresh_continuous_aggregate('samples_monthly', %s, %s)", (monthly_start, monthly_end))


def upsert_device_profile(
    config: AppConfig,
    *,
    device_id: str,
    profile_version: int,
    source_path: str,
    payload: dict[str, Any],
    content_hash: str,
) -> None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO device_profiles (
                    device_id, profile_version, source_path, payload, content_hash, loaded_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (device_id) DO UPDATE SET
                    profile_version = EXCLUDED.profile_version,
                    source_path = EXCLUDED.source_path,
                    payload = EXCLUDED.payload,
                    content_hash = EXCLUDED.content_hash,
                    loaded_at = NOW(),
                    updated_at = NOW()
                """,
                (
                    device_id,
                    profile_version,
                    source_path,
                    Jsonb(payload),
                    content_hash,
                ),
            )
        connection.commit()


def get_device_profile(config: AppConfig, device_id: str) -> dict[str, Any] | None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT device_id, profile_version, source_path, payload, content_hash, loaded_at, updated_at
                FROM device_profiles
                WHERE device_id = %s
                """,
                (device_id,),
            )
            return cursor.fetchone()


def list_device_profiles(config: AppConfig) -> list[dict[str, Any]]:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT device_id, profile_version, source_path, payload, content_hash, loaded_at, updated_at
                FROM device_profiles
                ORDER BY device_id ASC
                """
            )
            return cursor.fetchall()


def _profile_file_paths(profiles_dir: Path) -> list[Path]:
    if not profiles_dir.exists():
        return []
    return sorted(
        path
        for path in profiles_dir.glob("*.json")
        if path.is_file() and not path.name.startswith("_")
    )


def _require_profile_object(value: Any, *, context: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    raise ValueError(f"{context} must be an object")


def _require_profile_list(value: Any, *, context: str) -> list[Any]:
    if isinstance(value, list):
        return value
    raise ValueError(f"{context} must be a list")


def _profile_power_scale(payload: dict[str, Any], dp_id: str | None) -> float:
    if not dp_id:
        return 1.0
    for item in _require_profile_list(payload.get("dps"), context="dps"):
        dps_item = _require_profile_object(item, context="dps entry")
        if str(dps_item.get("dp_id") or "") != dp_id:
            continue
        scale_digits = int(dps_item.get("scale_digits", 0) or 0)
        return float(10 ** scale_digits) if scale_digits > 0 else 1.0
    return 1.0


def _profile_capabilities(payload: dict[str, Any]) -> list[dict[str, Any]]:
    capabilities: list[dict[str, Any]] = []
    for item in _require_profile_list(payload.get("dps"), context="dps"):
        dps_item = _require_profile_object(item, context="dps entry")
        values_json: dict[str, Any] = {}
        for key in ("unit", "min", "max", "step"):
            value = dps_item.get(key)
            if value not in (None, ""):
                values_json[key] = value
        scale_digits = int(dps_item.get("scale_digits", 0) or 0)
        if scale_digits > 0:
            values_json["scale"] = scale_digits
        enum_values = dps_item.get("enum_values")
        if enum_values:
            values_json["range"] = enum_values

        lan_section = dps_item.get("lan") if isinstance(dps_item.get("lan"), dict) else {}
        request_mode = lan_section.get("request_mode")
        if isinstance(request_mode, str) and request_mode:
            values_json["request_mode"] = request_mode

        dp_id_raw = str(dps_item.get("dp_id") or "")
        capabilities.append(
            {
                "capability_source": str(dps_item.get("source_group") or "profile"),
                "capability_code": str(dps_item.get("code") or dp_id_raw or "unknown"),
                "capability_name": str(
                    dps_item.get("display_label")
                    or dps_item.get("name")
                    or dps_item.get("code")
                    or dp_id_raw
                ),
                "value_type": dps_item.get("value_type"),
                "dp_id": int(dp_id_raw) if dp_id_raw.isdigit() else None,
                "values_json": values_json,
            }
        )
    return capabilities


def _validate_profile_document(path: Path, payload: dict[str, Any]) -> tuple[str, int]:
    profile_version = int(payload.get("profile_version", 0) or 0)
    if profile_version <= 0:
        raise ValueError(f"{path.name}: profile_version must be a positive integer")

    device = _require_profile_object(payload.get("device"), context=f"{path.name} device")
    connection = _require_profile_object(payload.get("connection"), context=f"{path.name} connection")
    summary = _require_profile_object(payload.get("summary"), context=f"{path.name} summary")
    _require_profile_list(payload.get("dps"), context=f"{path.name} dps")
    _require_profile_list(payload.get("controls"), context=f"{path.name} controls")

    device_id = str(device.get("device_id") or "").strip()
    if not device_id:
        raise ValueError(f"{path.name}: device.device_id is required")
    if path.stem != device_id:
        raise ValueError(f"{path.name}: file name must match device.device_id")

    device_name = str(device.get("name") or "").strip()
    if not device_name:
        raise ValueError(f"{path.name}: device.name is required")

    if "local_key" not in connection:
        raise ValueError(f"{path.name}: connection.local_key is required")
    if "local_ip" not in connection:
        raise ValueError(f"{path.name}: connection.local_ip is required")
    if "protocol_version" not in connection:
        raise ValueError(f"{path.name}: connection.protocol_version is required")

    default_power_mode = str(summary.get("default_power_mode") or "").strip().lower()
    if default_power_mode not in {"total", "current"}:
        raise ValueError(f"{path.name}: summary.default_power_mode must be 'total' or 'current'")

    if summary.get("default_power_dps_key") in (None, ""):
        raise ValueError(f"{path.name}: summary.default_power_dps_key is required")

    visualized_codes = summary.get("default_visualized_codes")
    if not isinstance(visualized_codes, list):
        raise ValueError(f"{path.name}: summary.default_visualized_codes must be a list")

    return device_id, profile_version


def _load_profile_document(path: Path) -> tuple[str, int, dict[str, Any], str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{path.name}: invalid JSON: {error}") from error

    payload = _require_profile_object(payload, context=path.name)
    device_id, profile_version = _validate_profile_document(path, payload)
    canonical_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    content_hash = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
    return device_id, profile_version, payload, content_hash


def materialize_device_profile(
    config: AppConfig,
    *,
    source_path: Path,
    payload: dict[str, Any],
) -> None:
    device = _require_profile_object(payload.get("device"), context="device")
    connection = _require_profile_object(payload.get("connection"), context="connection")
    summary = _require_profile_object(payload.get("summary"), context="summary")

    device_id = str(device.get("device_id") or "").strip()
    visualized_codes = [str(code) for code in summary.get("default_visualized_codes") or []]
    total_power_dps_key = str(summary.get("default_power_dps_key") or "").strip() or None
    power_type = str(summary.get("default_power_mode") or "total").strip().lower() or "total"

    upsert_managed_device(
        config,
        name=str(device.get("name") or "").strip(),
        room=str(device.get("room") or ""),
        device_id=device_id,
        category_code=str(device.get("category_code") or "").strip() or None,
        device_kind=str(device.get("device_kind") or "meter"),
        is_energy_meter=bool(device.get("is_energy_meter", True)),
        is_charger=bool(device.get("is_charger", False)),
        product_id=str(device.get("product_id") or "").strip() or None,
        product_name=str(device.get("product_name") or "").strip() or None,
        icon=str(device.get("icon") or "").strip() or None,
        onboarding_source="profile",
        local_key=str(connection.get("local_key") or ""),
        ip_address=str(connection.get("local_ip") or ""),
        version=float(connection.get("protocol_version") or 3.3),
        total_power_dps_key=total_power_dps_key,
        total_power_scale=_profile_power_scale(payload, total_power_dps_key),
        power_type=power_type,
        visualized_codes=visualized_codes,
        capabilities=_profile_capabilities(payload),
    )

def sync_device_profiles_from_disk(config: AppConfig, profiles_dir: Path | None = None) -> list[str]:
    root = profiles_dir or PROFILES_DIR
    loaded_device_ids: list[str] = []
    seen_device_ids: set[str] = set()

    for path in _profile_file_paths(root):
        device_id, profile_version, payload, content_hash = _load_profile_document(path)
        if device_id in seen_device_ids:
            raise ValueError(f"Duplicate profile device_id detected: {device_id}")
        seen_device_ids.add(device_id)

        relative_path = path.relative_to(root.parent.parent).as_posix()
        upsert_device_profile(
            config,
            device_id=device_id,
            profile_version=profile_version,
            source_path=relative_path,
            payload=payload,
            content_hash=content_hash,
        )
        materialize_device_profile(config, source_path=path, payload=payload)
        loaded_device_ids.append(device_id)

    return loaded_device_ids


def _parse_dt(value: str) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _normalize_json_field(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    return {}


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_voltage_value(value: Any) -> float | None:
    voltage = _coerce_float(value)
    if voltage is None:
        return None
    if abs(voltage) >= 1000:
        return voltage / 10.0
    return voltage


def _read_reference_voltage(raw_dps: dict[str, Any] | None) -> float | None:
    payload = _normalize_json_field(raw_dps)
    if not payload:
        return None

    direct_voltage = _normalize_voltage_value(payload.get("6"))
    if direct_voltage is not None:
        return direct_voltage

    phase_voltages = [
        _normalize_voltage_value(payload.get(key))
        for key in ("107", "108", "109")
        if payload.get(key) is not None
    ]
    phase_voltages = [value for value in phase_voltages if value is not None and value > 0]
    if not phase_voltages:
        return None
    return sum(phase_voltages) / len(phase_voltages)


def _normalize_power_by_measurements(
    power_w: float,
    raw_dps: dict[str, Any] | None,
) -> float:
    payload = _normalize_json_field(raw_dps)
    if not payload:
        return power_w

    current_raw = _coerce_float(payload.get("4"))
    measured_voltage = _read_reference_voltage(payload)
    if current_raw is None or measured_voltage is None or current_raw <= 0 or measured_voltage <= 0:
        return power_w

    current_a = current_raw / 1000.0 if current_raw > 10 else current_raw
    apparent_power_w = current_a * measured_voltage
    if apparent_power_w <= 0:
        return power_w

    if power_w > apparent_power_w * 3 and (power_w / 10.0) <= apparent_power_w * 1.6:
        return power_w / 10.0

    return power_w


def _normalize_sample_power_w(power_w: float, raw_dps: dict[str, Any] | None = None) -> float:
    power_w = _normalize_power_by_measurements(power_w, raw_dps)
    reference_voltage = _read_reference_voltage(raw_dps)
    if power_w > 5000 and reference_voltage is not None and 180 <= reference_voltage <= 260:
        return power_w / 10.0
    return power_w


def _format_display_datetime(config: AppConfig, value: datetime | str | None) -> str | None:
    if value is None:
        return None

    dt = _parse_dt(value) if not isinstance(value, datetime) else value
    try:
        local_dt = dt.astimezone(ZoneInfo(config.timezone))
    except ZoneInfoNotFoundError:
        local_dt = dt
    return local_dt.strftime("%d.%m.%Y %H:%M:%S")


def _get_timezone(config: AppConfig) -> ZoneInfo:
    try:
        return ZoneInfo(config.timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def get_device_rows(config: AppConfig) -> list[dict[str, Any]]:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.name, d.room, d.device_kind, d.is_energy_meter, d.is_charger,
                        d.product_name, d.category_code, d.device_id,
                        d.total_power_dps_key, d.visualized_codes, d.power_type,
                       COALESCE(c.ip_address, '') AS ip_address,
                       (COALESCE(c.ip_address, '') <> '') AS connection_ready
                FROM devices d
                LEFT JOIN device_connections c ON c.device_id = d.device_id
                ORDER BY name
                """
            )
            return cursor.fetchall()


def get_device_row(config: AppConfig, device_id: str) -> dict[str, Any] | None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                                    SELECT d.name, d.room, d.device_id, d.device_kind, d.is_energy_meter, d.is_charger,
                        d.product_name, d.category_code, d.product_id, d.icon,
                        d.total_power_dps_key, d.visualized_codes, d.power_type,
                       COALESCE(c.ip_address, '') AS ip_address,
                       (COALESCE(c.ip_address, '') <> '') AS connection_ready
                FROM devices d
                LEFT JOIN device_connections c ON c.device_id = d.device_id
                WHERE d.device_id = %s
                """,
                (device_id,),
            )
            return cursor.fetchone()


def update_device_summary_config(
    config: AppConfig,
    device_id: str,
    *,
    total_power_dps_key: str | None,
    total_power_scale: float,
    visualized_codes: list[str] | tuple[str, ...],
    power_type: str,
) -> None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE devices
                SET total_power_dps_key = %s,
                    visualized_codes = %s,
                    power_type = %s,
                    updated_at = NOW()
                WHERE device_id = %s
                """,
                (
                    total_power_dps_key,
                    Jsonb(list(visualized_codes)),
                    power_type,
                    device_id,
                ),
            )
            cursor.execute(
                """
                UPDATE device_connections
                SET total_power_dps_key = %s,
                    total_power_scale = %s,
                    updated_at = NOW()
                WHERE device_id = %s
                """,
                (
                    total_power_dps_key,
                    total_power_scale,
                    device_id,
                ),
            )
        connection.commit()


def update_device_connection_endpoint(
    config: AppConfig,
    device_id: str,
    *,
    ip_address: str,
    version: float,
) -> None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE device_connections
                SET ip_address = %s,
                    version = %s,
                    updated_at = NOW()
                WHERE device_id = %s
                """,
                (
                    ip_address,
                    version,
                    device_id,
                ),
            )
        connection.commit()


def delete_managed_device(config: AppConfig, device_id: str) -> None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM devices WHERE device_id = %s", (device_id,))
        connection.commit()


def get_device_capabilities(config: AppConfig, device_id: str) -> list[dict[str, Any]]:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT capability_source, capability_code, capability_name, value_type, dp_id, values_json
                FROM device_capabilities
                WHERE device_id = %s
                ORDER BY dp_id ASC NULLS LAST, capability_source DESC, capability_code ASC
                """,
                (device_id,),
            )
            return cursor.fetchall()


def replace_device_capabilities(config: AppConfig, device_id: str, capabilities: list[dict[str, Any]]) -> None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM device_capabilities WHERE device_id = %s", (device_id,))
            if capabilities:
                cursor.executemany(
                    """
                    INSERT INTO device_capabilities (
                        device_id, capability_source, capability_code, capability_name,
                        value_type, dp_id, values_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            device_id,
                            capability.get("capability_source") or "status",
                            capability.get("capability_code") or "unknown",
                            capability.get("capability_name"),
                            capability.get("value_type"),
                            capability.get("dp_id"),
                            Jsonb(capability.get("values_json") or {}),
                        )
                        for capability in capabilities
                    ],
                )
        connection.commit()


def _load_dps_request_modes(cursor, device_ids: list[str]) -> dict[str, dict[str, str]]:
    if not device_ids:
        return {}
    cursor.execute(
        """
        SELECT device_id, dp_id, values_json
        FROM device_capabilities
        WHERE device_id = ANY(%s) AND dp_id IS NOT NULL
        """,
        (device_ids,),
    )
    result: dict[str, dict[str, str]] = {}
    for row in cursor.fetchall():
        request_mode = (row.get("values_json") or {}).get("request_mode")
        if not isinstance(request_mode, str) or not request_mode:
            continue
        device_id = str(row.get("device_id") or "")
        dp_id = str(row.get("dp_id") or "")
        if not device_id or not dp_id:
            continue
        result.setdefault(device_id, {})[dp_id] = request_mode
    return result


def get_control_device(config: AppConfig, device_id: str) -> TuyaDeviceConfig | None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.name, d.room, d.device_id,
                       c.local_key, c.ip_address, c.version, c.total_power_dps_key, c.total_power_scale,
                      d.visualized_codes, d.power_type
                FROM devices d
                JOIN device_connections c ON c.device_id = d.device_id
                WHERE d.device_id = %s
                """,
                (device_id,),
            )
            row = cursor.fetchone()
            request_modes = _load_dps_request_modes(cursor, [device_id]).get(device_id, {})

    if not row:
        return None

    return TuyaDeviceConfig(
        name=row["name"],
        room=row["room"],
        device_id=row["device_id"],
        local_key=row["local_key"],
        ip_address=row["ip_address"],
        version=float(row["version"]),
        total_power_dps_key=str(row["total_power_dps_key"] or ""),
        total_power_scale=float(row["total_power_scale"] or 1),
        visualized_codes=tuple(str(key) for key in (row["visualized_codes"] or [])),
        power_type=str(row.get("power_type") or "total"),
        dps_request_modes=request_modes,
    )


def get_device_by_id(config: AppConfig, device_id: str) -> dict[str, Any] | None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT name, room, device_id FROM devices WHERE device_id = %s",
                (device_id,),
            )
            return cursor.fetchone()


def get_polling_devices(config: AppConfig) -> list[TuyaDeviceConfig]:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.name, d.room, d.device_id,
                       c.local_key, c.ip_address, c.version, c.total_power_dps_key, c.total_power_scale,
                      d.visualized_codes, d.power_type
                FROM devices d
                JOIN device_connections c ON c.device_id = d.device_id
                WHERE c.local_key <> '' AND c.ip_address <> ''
                ORDER BY d.name
                """
            )
            rows = cursor.fetchall()
            request_modes_by_device = _load_dps_request_modes(
                cursor, [row["device_id"] for row in rows]
            )

    devices: list[TuyaDeviceConfig] = []
    for row in rows:
        devices.append(
            TuyaDeviceConfig(
                name=row["name"],
                room=row["room"],
                device_id=row["device_id"],
                local_key=row["local_key"],
                ip_address=row["ip_address"],
                version=float(row["version"]),
                total_power_dps_key=str(row["total_power_dps_key"] or ""),
                total_power_scale=float(row["total_power_scale"] or 1),
                visualized_codes=tuple(str(key) for key in (row["visualized_codes"] or [])),
                power_type=str(row.get("power_type") or "total"),
                dps_request_modes=request_modes_by_device.get(row["device_id"], {}),
            )
        )
    return devices


def get_known_local_ips(config: AppConfig) -> list[str]:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT ip_address FROM device_connections WHERE ip_address <> '' ORDER BY ip_address"
            )
            return [row["ip_address"] for row in cursor.fetchall()]


def get_samples(config: AppConfig, device_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT captured_at, power_w, raw_dps
                FROM samples
                WHERE device_id = %s AND captured_at >= %s AND captured_at <= %s
                ORDER BY captured_at ASC
                """,
                (device_id, start, end),
            )
            return cursor.fetchall()


def get_latest_sample(config: AppConfig, device_id: str) -> dict[str, Any] | None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT captured_at, power_w, raw_dps
                FROM samples
                WHERE device_id = %s
                ORDER BY captured_at DESC
                LIMIT 1
                """,
                (device_id,),
            )
            return cursor.fetchone()


def get_recent_raw_dps_samples(config: AppConfig, device_id: str, limit: int = 12) -> list[dict[str, Any]]:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT raw_dps
                FROM samples
                WHERE device_id = %s
                ORDER BY captured_at DESC
                LIMIT %s
                """,
                (device_id, max(int(limit or 1), 1)),
            )
            return cursor.fetchall()


def _bucket_start(dt: datetime, bucket: str) -> datetime:
    if bucket == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    if bucket == "day":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket == "month":
        return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


def _bucket_duration_hours(bucket: str) -> float:
    if bucket == "hour":
        return 1.0
    if bucket == "day":
        return 24.0
    if bucket == "month":
        return 24.0 * 30.0
    return 0.25


def _next_bucket_start(dt: datetime, bucket: str) -> datetime:
    if bucket == "hour":
        return dt + timedelta(hours=1)
    if bucket == "day":
        return dt + timedelta(days=1)
    if bucket == "month":
        if dt.month == 12:
            return dt.replace(year=dt.year + 1, month=1, day=1)
        return dt.replace(month=dt.month + 1, day=1)
    return dt + timedelta(minutes=15)


def _build_fixed_bucket_sequence(start: datetime, period: str, bucket: str) -> list[datetime]:
    if period == "day":
        day_start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        return [day_start + timedelta(hours=hour) for hour in range(24)]

    if period == "week":
        week_start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        return [week_start + timedelta(days=day_index) for day_index in range(7)]

    if period == "month":
        month_start = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        days_in_month = calendar.monthrange(month_start.year, month_start.month)[1]
        return [month_start + timedelta(days=day_index) for day_index in range(days_in_month)]

    if period == "year":
        year_start = start.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return [year_start.replace(month=month) for month in range(1, 13)]

    return []


def _build_custom_bucket_sequence(start: datetime, end: datetime, bucket: str) -> list[datetime]:
    sequence: list[datetime] = []
    current = _bucket_start(start, bucket)
    last = _bucket_start(end, bucket)
    while current <= last:
        sequence.append(current)
        current = _next_bucket_start(current, bucket)
    return sequence


def _format_axis_label(dt: datetime, bucket: str, period: str) -> str:
    if bucket == "hour":
        return dt.strftime("%H")
    if bucket == "day":
        if period == "week":
            return dt.strftime("%d.%m")
        return str(dt.day)
    return RUSSIAN_MONTH_LABELS_SHORT[dt.month]


def _format_tooltip_label(dt: datetime, bucket: str, period: str) -> str:
    if bucket == "hour":
        return dt.strftime("%d.%m.%Y %H:00")
    if bucket == "day":
        return dt.strftime("%d.%m.%Y")
    return f"{RUSSIAN_MONTH_LABELS_FULL[dt.month]} {dt.year}"


_CAGG_VIEWS = {
    "hour": "samples_hourly",
    "day": "samples_daily",
    "month": "samples_monthly",
}


def _read_energy_counter_kwh(raw_dps: dict[str, Any] | None, dp_key: str, scale_divisor: float) -> float | None:
    if not raw_dps:
        return None
    raw_value = raw_dps.get(dp_key)
    if raw_value is None:
        return None
    try:
        return float(raw_value) / max(scale_divisor, 1.0)
    except (TypeError, ValueError):
        return None


def _get_energy_counter_meta_from_capabilities(capabilities: list[dict[str, Any]]) -> tuple[str, float] | None:
    for capability in capabilities:
        code = str(capability.get("capability_code") or "")
        if code not in {"total_forward_energy", "add_ele"}:
            continue
        dp_id = capability.get("dp_id")
        if dp_id is None:
            continue
        values_json = capability.get("values_json") or {}
        scale_digits = int(values_json.get("scale", 0) or 0)
        return str(dp_id), float(10 ** scale_digits) if scale_digits > 0 else 1.0
    return None


def _get_energy_counter_meta(
    config: AppConfig,
    device_id: str,
    capabilities: list[dict[str, Any]] | None = None,
) -> tuple[str, float] | None:
    control_device = get_control_device(config, device_id)
    if control_device and str(control_device.power_type or "total").strip().lower() == "current":
        return None
    if control_device and control_device.total_power_dps_key:
        return control_device.total_power_dps_key, max(float(control_device.total_power_scale or 1.0), 1.0)

    if capabilities is not None:
        return _get_energy_counter_meta_from_capabilities(capabilities)
    return _get_energy_counter_meta_from_capabilities(get_device_capabilities(config, device_id))


def _read_aggregate_rows(
    config: AppConfig,
    device_ids: list[str],
    bucket: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    if not device_ids:
        return []
    view = _CAGG_VIEWS.get(bucket)
    if view is None:
        return []
    aggregate_columns = (
        "avg_power_w, peak_power_w, sample_count, energy_wh, "
        "last_power_w, "
        "first_raw_dps, last_raw_dps, first_captured_at, last_captured_at"
        if view == "samples_monthly"
        else
        "avg_power_w, peak_power_w, sample_count, energy_wh, "
        "last_power_w, first_raw_dps, last_raw_dps, first_captured_at, last_captured_at"
    )
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT
                    device_id, bucket,
                    {aggregate_columns}
                FROM {view}
                WHERE device_id = ANY(%s) AND bucket >= %s AND bucket <= %s
                ORDER BY device_id ASC, bucket ASC
                """,
                (device_ids, start, end),
            )
            return cursor.fetchall()


def _bucket_energy_wh(row: dict[str, Any], bucket: str) -> float:
    energy_wh = _coerce_float(row.get("energy_wh"))
    if energy_wh is not None:
        return energy_wh
    avg_power_w = _coerce_float(row.get("avg_power_w")) or 0.0
    return avg_power_w * _bucket_duration_hours(bucket)


def _counter_bar_energies_kwh(
    bucket_rows: list[dict[str, Any]],
    energy_counter_meta: tuple[str, float],
) -> list[float]:
    """For counter devices, per-bucket energy = last_kwh - prev_bucket_last_kwh.
    Anchoring on the previous bucket's last counter (instead of this bucket's
    first counter) makes the bars telescope: sum equals last_of_last_bucket
    minus first_of_first_bucket. Inter-bucket counter increments are no longer
    lost between adjacent buckets."""
    dp_key, scale_divisor = energy_counter_meta
    energies: list[float] = []
    prev_last_kwh: float | None = None
    for row in bucket_rows:
        last_kwh = _read_energy_counter_kwh(
            _normalize_json_field(row.get("last_raw_dps")), dp_key, scale_divisor
        )
        first_kwh = _read_energy_counter_kwh(
            _normalize_json_field(row.get("first_raw_dps")), dp_key, scale_divisor
        )
        if last_kwh is None:
            energies.append(0.0)
            continue
        anchor_kwh = prev_last_kwh if prev_last_kwh is not None else first_kwh
        if anchor_kwh is None or last_kwh < anchor_kwh:
            energies.append(0.0)
        else:
            energies.append(last_kwh - anchor_kwh)
        prev_last_kwh = last_kwh
    return energies


def _aggregate_energy_wh(
    bucket_rows: list[dict[str, Any]],
    bucket: str,
    energy_counter_meta: tuple[str, float] | None,
) -> float:
    if not bucket_rows:
        return 0.0

    if energy_counter_meta:
        return sum(_counter_bar_energies_kwh(bucket_rows, energy_counter_meta)) * 1000.0

    return sum(_bucket_energy_wh(row, bucket) for row in bucket_rows)


def _build_chart_series_from_aggregate(
    bucket_rows: list[dict[str, Any]],
    bucket: str,
    energy_counter_meta: tuple[str, float] | None,
) -> list[dict[str, Any]]:
    series: list[dict[str, Any]] = []
    if energy_counter_meta:
        counter_energies = _counter_bar_energies_kwh(bucket_rows, energy_counter_meta)
    else:
        counter_energies = []

    for index, row in enumerate(bucket_rows):
        if energy_counter_meta:
            energy_kwh = counter_energies[index]
        else:
            energy_kwh = _bucket_energy_wh(row, bucket) / 1000.0

        avg_power_kw = (_coerce_float(row.get("avg_power_w")) or 0.0) / 1000.0
        timestamp_dt = _parse_dt(row["bucket"]) if not isinstance(row["bucket"], datetime) else row["bucket"]
        series.append(
            {
                "timestamp": timestamp_dt.isoformat(),
                "energy_kwh": round(energy_kwh, 4),
                "avg_power_kw": round(avg_power_kw, 4),
            }
        )

    return series


def _normalize_bucket_for_timezone(config: AppConfig, value: datetime, bucket: str) -> datetime:
    local_dt = value.astimezone(_get_timezone(config))
    if bucket == "hour":
        return local_dt.replace(minute=0, second=0, microsecond=0)
    if bucket == "day":
        return local_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket == "month":
        return local_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return local_dt.replace(minute=(local_dt.minute // 15) * 15, second=0, microsecond=0)


def _prepare_chart_series(
    config: AppConfig,
    rows_by_device: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    period: str,
    bucket: str,
    energy_counter_meta: tuple[str, float] | None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    base_series = _build_chart_series_from_aggregate(rows_by_device, bucket, energy_counter_meta)
    use_power_chart = (
        energy_counter_meta is None
        and
        any(float(item.get("avg_power_kw") or 0.0) > 0.0 for item in base_series)
        and not any(float(item.get("energy_kwh") or 0.0) > 0.0 for item in base_series)
    )

    chart = {
        "metric": "avg_power_kw" if use_power_chart else "energy_kwh",
        "unit": "кВт" if use_power_chart else "кВт·ч",
        "label": "Средняя мощность" if use_power_chart else "Потребление",
        "bucket": bucket,
        "period": period,
    }

    chart_series_by_bucket = {}
    for item in base_series:
        bucket_dt = _normalize_bucket_for_timezone(config, _parse_dt(item["timestamp"]), bucket)
        chart_series_by_bucket[bucket_dt] = {
            **item,
            "timestamp": bucket_dt.isoformat(),
            "chart_value": round(
                float(item.get("avg_power_kw") if use_power_chart else item.get("energy_kwh") or 0.0),
                4,
            ),
        }

    if period in {"day", "week", "month", "year"}:
        bucket_sequence = _build_fixed_bucket_sequence(start, period, bucket)
    else:
        bucket_sequence = _build_custom_bucket_sequence(start, end, bucket)

    filled_series: list[dict[str, Any]] = []
    for current in bucket_sequence:
        filled_series.append(
            chart_series_by_bucket.get(
                current,
                {
                    "timestamp": current.isoformat(),
                    "energy_kwh": 0.0,
                    "avg_power_kw": 0.0,
                    "chart_value": 0.0,
                },
            )
        )

    for item in filled_series:
        bucket_dt = _parse_dt(item["timestamp"])
        item["axis_label"] = _format_axis_label(bucket_dt, bucket, period)
        item["tooltip_label"] = _format_tooltip_label(bucket_dt, bucket, period)

    return filled_series, chart


def _row_from_live_sample(sample: DeviceSample) -> dict[str, Any]:
    return {
        "captured_at": sample.captured_at,
        "power_w": sample.power_w,
        "raw_dps": sample.raw_dps,
    }


def get_sample_age_seconds(captured_at: datetime | None, now: datetime) -> int | None:
    if captured_at is None:
        return None
    return max(int((now - captured_at).total_seconds()), 0)


def get_sample_status(captured_at: datetime | None, now: datetime) -> str:
    age_seconds = get_sample_age_seconds(captured_at, now)
    if age_seconds is None:
        return "error"
    if age_seconds > 3600:
        return "error"
    if age_seconds > 60:
        return "warning"
    return "ok"


def _build_energy_counter_meta_by_device(rows: list[dict[str, Any]]) -> dict[str, tuple[str, float]]:
    metadata: dict[str, tuple[str, float]] = {}
    for row in rows:
        device_id = str(row.get("device_id") or "")
        if not device_id or device_id in metadata:
            continue
        if str(row.get("power_type") or "total").strip().lower() == "current":
            continue
        dp_id = str(row.get("total_power_dps_key") or "").strip()
        if not dp_id:
            continue
        scale_divisor = max(float(row.get("total_power_scale") or 1.0), 1.0)
        metadata[device_id] = (dp_id, scale_divisor)
    return metadata


def _group_aggregate_rows_by_device(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("device_id") or "")].append(row)
    return grouped


def _get_dashboard_summary_context(
    config: AppConfig,
    month_start: datetime,
    now: datetime,
) -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, list[dict[str, Any]]],
    dict[str, tuple[str, float]],
]:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.name, d.room, d.device_kind, d.is_energy_meter, d.is_charger,
                    d.product_name, d.category_code, d.device_id,
                      d.power_type,
                       COALESCE(c.ip_address, '') AS ip_address,
                      (COALESCE(c.ip_address, '') <> '') AS connection_ready,
                      c.total_power_dps_key,
                      c.total_power_scale
                FROM devices d
                LEFT JOIN device_connections c ON c.device_id = d.device_id
                ORDER BY name
                """
            )
            device_rows = cursor.fetchall()
            device_ids = [str(row.get("device_id") or "") for row in device_rows if row.get("device_id")]
            energy_device_ids = [
                str(row.get("device_id") or "")
                for row in device_rows
                if row.get("device_id") and row.get("is_energy_meter")
            ]
            if not device_ids:
                return [], {}, {}, {}

            cursor.execute(
                """
                SELECT DISTINCT ON (device_id) device_id, captured_at, power_w, raw_dps
                FROM samples
                WHERE device_id = ANY(%s)
                ORDER BY device_id ASC, captured_at DESC
                """,
                (device_ids,),
            )
            latest_rows = cursor.fetchall()

            if not energy_device_ids:
                return device_rows, {str(row.get("device_id") or ""): row for row in latest_rows if row.get("device_id")}, {}, {}

            cursor.execute(
                """
                SELECT device_id, bucket,
                      avg_power_w, peak_power_w, sample_count, energy_wh,
                      last_power_w,
                       first_raw_dps, last_raw_dps,
                       first_captured_at, last_captured_at
                FROM samples_daily
                WHERE device_id = ANY(%s) AND bucket >= %s AND bucket <= %s
                ORDER BY device_id ASC, bucket ASC
                """,
                (energy_device_ids, month_start, now),
            )
            daily_rows = cursor.fetchall()

    return (
        device_rows,
        {str(row.get("device_id") or ""): row for row in latest_rows if row.get("device_id")},
        _group_aggregate_rows_by_device(daily_rows),
        _build_energy_counter_meta_by_device(device_rows),
    )


def get_dashboard_summary(
    config: AppConfig,
    month_start: datetime,
    now: datetime,
    live_samples: dict[str, DeviceSample] | None = None,
) -> dict[str, Any]:
    devices = []
    sensor_devices = []
    total_energy_wh = 0.0
    total_power_w = 0.0
    online_device_count = 0
    live_samples = live_samples or {}

    (
        device_rows,
        latest_by_device,
        daily_rows_by_device,
        energy_counter_meta_by_device,
    ) = _get_dashboard_summary_context(config, month_start, now)

    for device in device_rows:
        device_id = str(device.get("device_id") or "")
        live_sample = live_samples.get(device_id)
        latest = latest_by_device.get(device_id)

        if live_sample:
            last_seen = _format_display_datetime(config, live_sample.captured_at)
            raw_dps = live_sample.raw_dps
            effective_captured_at = live_sample.captured_at
        else:
            last_seen = _format_display_datetime(config, latest["captured_at"]) if latest else None
            raw_dps = _normalize_json_field(latest["raw_dps"]) if latest else {}
            effective_captured_at = _parse_dt(latest["captured_at"]) if latest else None

        last_seen_status = get_sample_status(effective_captured_at, now)
        if last_seen_status == "ok":
            online_device_count += 1

        last_seen_age_seconds = get_sample_age_seconds(effective_captured_at, now)

        base_entry = {
            "name": device["name"],
            "room": device["room"],
            "device_id": device.get("device_id"),
            "device_kind": device.get("device_kind"),
            "connection_ready": bool(device.get("connection_ready")),
            "ip_address": str(device.get("ip_address") or "").strip() or None,
            "last_seen": last_seen,
            "last_seen_age_seconds": last_seen_age_seconds,
            "last_seen_status": last_seen_status,
            "raw_dps": raw_dps,
        }

        if device.get("is_energy_meter"):
            bucket_rows = daily_rows_by_device.get(device_id, [])
            energy_counter_meta = energy_counter_meta_by_device.get(device_id)
            device_energy_wh = _aggregate_energy_wh(
                bucket_rows,
                "day",
                energy_counter_meta,
            )
            total_energy_wh += device_energy_wh
            device_entry: dict[str, Any] = {
                **base_entry,
                "month_energy_kwh": round(device_energy_wh / 1000.0, 3),
            }
            if energy_counter_meta is None:
                current_power_w = (
                    _normalize_sample_power_w(float(latest["power_w"]), latest.get("raw_dps"))
                    if latest and latest.get("power_w") is not None
                    else 0.0
                )
                device_entry["current_power_kw"] = round(current_power_w / 1000.0, 3)
                total_power_w += current_power_w
            devices.append(device_entry)
            continue

        sensor_devices.append(base_entry)

    return {
        "home_name": config.home_name,
        "month_energy_kwh": round(total_energy_wh / 1000.0, 3),
        "estimated_cost": round((total_energy_wh / 1000.0) * config.tariff_per_kwh, 2),
        "device_count": online_device_count,
        "devices": devices,
        "sensor_devices": sensor_devices,
    }


def _build_device_stats_result(
    config: AppConfig,
    device_id: str,
    bucket_rows: list[dict[str, Any]],
    latest: dict[str, Any] | None,
    start: datetime,
    end: datetime,
    period: str,
    bucket: str,
    capabilities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    energy_counter_meta = _get_energy_counter_meta(config, device_id, capabilities)

    chart_series, chart = _prepare_chart_series(config, bucket_rows, start, end, period, bucket, energy_counter_meta)
    total_energy_wh = _aggregate_energy_wh(bucket_rows, bucket, energy_counter_meta)
    duration_hours = max((end - start).total_seconds() / 3600.0, 0.0)

    if bucket_rows:
        if energy_counter_meta:
            average_power_w = (total_energy_wh / duration_hours) if duration_hours > 0 else 0.0
            peak_power_w = 0.0
        else:
            weighted_power_sum = sum(
                (_coerce_float(r.get("avg_power_w")) or 0.0) * (int(r.get("sample_count") or 0) or 1)
                for r in bucket_rows
            )
            weight_total = sum(int(r.get("sample_count") or 0) or 1 for r in bucket_rows)
            average_power_w = weighted_power_sum / max(weight_total, 1)
            peak_power_w = max((_coerce_float(r.get("peak_power_w")) or 0.0) for r in bucket_rows)
        sample_count = sum(int(r.get("sample_count") or 0) for r in bucket_rows)
    else:
        average_power_w = 0.0
        peak_power_w = 0.0
        sample_count = 0

    latest_captured_at = _parse_dt(latest["captured_at"]) if latest else None
    latest_power_w = (
        None if energy_counter_meta else _normalize_sample_power_w(float(latest["power_w"]), latest.get("raw_dps"))
    ) if latest else None

    return {
        "summary": {
            "energy_kwh": round(total_energy_wh / 1000.0, 3),
            "average_power_kw": round(average_power_w / 1000.0, 3),
            "peak_power_kw": round(peak_power_w / 1000.0, 3),
            "latest_power_w": round(latest_power_w, 1) if latest_power_w is not None else None,
            "sample_count": sample_count,
            "latest_sample": _format_display_datetime(config, latest["captured_at"]) if latest else None,
            "latest_sample_age_seconds": get_sample_age_seconds(latest_captured_at, end),
            "latest_sample_status": get_sample_status(latest_captured_at, end),
            "latest_raw_dps": _normalize_json_field(latest["raw_dps"]) if latest else {},
        },
        "series": chart_series,
        "chart": chart,
    }


def get_device_stats(
    config: AppConfig,
    device_id: str,
    start: datetime,
    end: datetime,
    period: str,
    bucket: str,
) -> dict[str, Any]:
    bucket_rows = _read_aggregate_rows(config, [device_id], bucket, start, end)
    latest = get_latest_sample(config, device_id)
    return _build_device_stats_result(config, device_id, bucket_rows, latest, start, end, period, bucket)


def get_device_context_and_stats(
    config: AppConfig,
    device_id: str,
    start: datetime,
    end: datetime,
    period: str,
    bucket: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any] | None]:
    view = _CAGG_VIEWS.get(bucket, "samples_hourly")
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT name, room, device_id, device_kind, is_energy_meter, is_charger,
                      product_name, category_code, product_id, icon,
                      total_power_dps_key, visualized_codes
                FROM devices
                WHERE device_id = %s
                """,
                (device_id,),
            )
            device = cursor.fetchone()
            if not device:
                return None, [], None

            cursor.execute(
                """
                SELECT capability_source, capability_code, capability_name, value_type, dp_id, values_json
                FROM device_capabilities
                WHERE device_id = %s
                ORDER BY dp_id ASC NULLS LAST, capability_source DESC, capability_code ASC
                """,
                (device_id,),
            )
            capabilities = cursor.fetchall()

            cursor.execute(
                f"""
                SELECT bucket,
                      avg_power_w, peak_power_w, sample_count, energy_wh,
                      last_power_w,
                       first_raw_dps, last_raw_dps,
                       first_captured_at, last_captured_at
                FROM {view}
                WHERE device_id = %s AND bucket >= %s AND bucket <= %s
                ORDER BY bucket ASC
                """,
                (device_id, start, end),
            )
            bucket_rows = cursor.fetchall()

            cursor.execute(
                """
                SELECT captured_at, power_w, raw_dps
                FROM samples
                WHERE device_id = %s
                ORDER BY captured_at DESC
                LIMIT 1
                """,
                (device_id,),
            )
            latest = cursor.fetchone()

    stats = _build_device_stats_result(config, device_id, bucket_rows, latest, start, end, period, bucket, capabilities)
    return device, capabilities, stats


CHARGER_IDLE_THRESHOLD_W = 50.0      # below this is treated as "not charging"
CHARGER_SESSION_GAP_SECONDS = 300.0  # idle gap above this splits sessions
CHARGER_SAMPLE_GAP_SECONDS = 60.0    # consecutive samples wider than this are not integrated
CHARGER_DELTA_MIN_DT = 1.0           # ignore zero/tiny dt when computing delta power
CHARGER_DELTA_MAX_DT = 600.0         # don't compute delta over gaps wider than this


def _charger_power_series_from_samples(
    samples: list[dict[str, Any]],
    energy_counter_meta: tuple[str, float] | None,
) -> list[tuple[datetime, float]]:
    """Build instantaneous power (W) time series.
    For current-type devices: power_w as-is from each sample.
    For counter-type: delta(counter)/delta(t) between consecutive samples, timestamped at the END of each interval."""
    if not samples:
        return []
    if energy_counter_meta is None:
        return [(row["captured_at"], float(row["power_w"] or 0)) for row in samples]

    dp_key, scale_divisor = energy_counter_meta
    series: list[tuple[datetime, float]] = []
    prev_counter: float | None = None
    prev_ts: datetime | None = None
    for row in samples:
        raw_dps = _normalize_json_field(row.get("raw_dps"))
        counter_kwh = _read_energy_counter_kwh(raw_dps, dp_key, scale_divisor)
        ts = row["captured_at"]
        if counter_kwh is None:
            continue
        if prev_counter is not None and prev_ts is not None:
            dt_s = (ts - prev_ts).total_seconds()
            if CHARGER_DELTA_MIN_DT <= dt_s <= CHARGER_DELTA_MAX_DT:
                d_kwh = counter_kwh - prev_counter
                if d_kwh < 0:
                    d_kwh = 0.0
                power_w = d_kwh * 3600.0 * 1000.0 / dt_s
                series.append((ts, power_w))
        prev_counter = counter_kwh
        prev_ts = ts
    return series


def _detect_charger_sessions(
    power_series: list[tuple[datetime, float]],
) -> list[dict[str, Any]]:
    """Split the power series into sessions separated by idle gaps."""
    sessions: list[dict[str, Any]] = []
    current_points: list[tuple[datetime, float]] = []
    last_active_ts: datetime | None = None

    def flush() -> None:
        if len(current_points) < 2:
            current_points.clear()
            return
        start_ts = current_points[0][0]
        end_ts = current_points[-1][0]
        energy_wh = 0.0
        peak_w = 0.0
        for (t1, p1), (t2, p2) in zip(current_points, current_points[1:]):
            dt_s = (t2 - t1).total_seconds()
            if 0 < dt_s <= CHARGER_SAMPLE_GAP_SECONDS:
                energy_wh += (p1 + p2) / 2.0 * dt_s / 3600.0
            peak_w = max(peak_w, p1, p2)
        duration_s = (end_ts - start_ts).total_seconds()
        sessions.append(
            {
                "start": start_ts,
                "end": end_ts,
                "duration_seconds": duration_s,
                "energy_kwh": round(energy_wh / 1000.0, 3),
                "peak_power_kw": round(peak_w / 1000.0, 3),
                "avg_power_kw": round(energy_wh / max(duration_s / 3600.0, 1e-6) / 1000.0, 3) if duration_s > 0 else 0.0,
            }
        )
        current_points.clear()

    for ts, power_w in power_series:
        if power_w >= CHARGER_IDLE_THRESHOLD_W:
            if last_active_ts is not None and (ts - last_active_ts).total_seconds() > CHARGER_SESSION_GAP_SECONDS:
                flush()
            current_points.append((ts, power_w))
            last_active_ts = ts
        else:
            if last_active_ts is not None and (ts - last_active_ts).total_seconds() > CHARGER_SESSION_GAP_SECONDS:
                flush()
                last_active_ts = None
    flush()
    return sessions


def get_charger_day_stats(
    config: AppConfig,
    device_id: str,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """For is_charger devices, build a line-style series of instantaneous power
    over the period plus a per-session breakdown."""
    capabilities = get_device_capabilities(config, device_id)
    energy_counter_meta = _get_energy_counter_meta(config, device_id, capabilities)

    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            if energy_counter_meta is not None:
                # Need raw_dps to read counter for delta computation
                cursor.execute(
                    """
                    SELECT captured_at, power_w, raw_dps
                    FROM samples
                    WHERE device_id = %s AND captured_at >= %s AND captured_at <= %s
                    ORDER BY captured_at ASC
                    """,
                    (device_id, start, end),
                )
            else:
                cursor.execute(
                    """
                    SELECT captured_at, power_w
                    FROM samples
                    WHERE device_id = %s AND captured_at >= %s AND captured_at <= %s
                    ORDER BY captured_at ASC
                    """,
                    (device_id, start, end),
                )
            sample_rows = cursor.fetchall()

    power_series = _charger_power_series_from_samples(sample_rows, energy_counter_meta)
    sessions = _detect_charger_sessions(power_series)

    series_points = [
        {
            "timestamp": ts.isoformat(),
            "power_kw": round(power_w / 1000.0, 3),
        }
        for ts, power_w in power_series
    ]

    total_energy_kwh = sum(session["energy_kwh"] for session in sessions)
    peak_power_kw = max((session["peak_power_kw"] for session in sessions), default=0.0)
    avg_power_w = sum(p for _, p in power_series) / len(power_series) if power_series else 0.0

    return {
        "chart": {
            "kind": "line",
            "label": "Мгновенная мощность",
            "unit": "кВт",
            "bucket": "raw",
            "period": "day",
        },
        "series": series_points,
        "sessions": [
            {
                "start": s["start"].isoformat(),
                "end": s["end"].isoformat(),
                "duration_seconds": int(s["duration_seconds"]),
                "energy_kwh": s["energy_kwh"],
                "avg_power_kw": s["avg_power_kw"],
                "peak_power_kw": s["peak_power_kw"],
            }
            for s in sessions
        ],
        "summary": {
            "energy_kwh": round(total_energy_kwh, 3),
            "peak_power_kw": round(peak_power_kw, 3),
            "avg_power_w": round(avg_power_w, 1),
            "sample_count": len(power_series),
            "session_count": len(sessions),
        },
    }


def pick_bucket(start: datetime, end: datetime, period: str = "custom") -> str:
    if period == "day":
        return "hour"
    if period in {"week", "month"}:
        return "day"
    if period == "year":
        return "month"

    span = end - start
    if span <= timedelta(days=2):
        return "hour"
    if span <= timedelta(days=62):
        return "day"
    return "month"
