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
    is_generator: bool,
    is_solar_consumer: bool,
    allow_custom_automation: bool,
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
    is_gateway: bool = False,
    gateway_device_id: str | None = None,
    cid: str | None = None,
    power_correction_factor: float = 1.0,
    ingest_token: str | None = None,
    disabled: bool = False,
) -> None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO devices (
                    name, room, device_id, category_code, device_kind,
                    is_energy_meter, is_charger, is_gateway, is_generator, is_solar_consumer, allow_custom_automation,
                    product_id, product_name, icon, onboarding_source, updated_at,
                    total_power_dps_key, total_power_scale, power_type, visualized_codes, disabled
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s, %s, %s)
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
                    is_gateway = EXCLUDED.is_gateway,
                    is_generator = EXCLUDED.is_generator,
                    -- is_solar_consumer is intentionally NOT updated here:
                    -- it's user-toggleable from the dashboard checkbox, and
                    -- the value in the profile is only the initial seed.
                    allow_custom_automation = EXCLUDED.allow_custom_automation,
                    product_id = EXCLUDED.product_id,
                    product_name = EXCLUDED.product_name,
                    icon = EXCLUDED.icon,
                    onboarding_source = EXCLUDED.onboarding_source,
                    updated_at = NOW(),
                    total_power_dps_key = EXCLUDED.total_power_dps_key,
                    total_power_scale = EXCLUDED.total_power_scale,
                    power_type = EXCLUDED.power_type,
                    visualized_codes = EXCLUDED.visualized_codes,
                    disabled = EXCLUDED.disabled
                """,
                (
                    name,
                    room,
                    device_id,
                    category_code,
                    device_kind,
                    is_energy_meter,
                    is_charger,
                    is_gateway,
                    is_generator,
                    is_solar_consumer,
                    allow_custom_automation,
                    product_id,
                    product_name,
                    icon,
                    onboarding_source,
                    total_power_dps_key,
                    total_power_scale,
                    power_type,
                    Jsonb(list(visualized_codes)),
                    disabled,
                ),
            )
            cursor.execute(
                """
                INSERT INTO device_connections (
                    device_id, local_key, ip_address, version, total_power_dps_key, total_power_scale, gateway_device_id, cid, power_correction_factor, ingest_token, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT(device_id) DO UPDATE SET
                    local_key = EXCLUDED.local_key,
                    ip_address = CASE
                        WHEN EXCLUDED.ip_address <> '' THEN EXCLUDED.ip_address
                        ELSE device_connections.ip_address
                    END,
                    version = CASE
                        WHEN EXCLUDED.version > 0 THEN EXCLUDED.version
                        ELSE device_connections.version
                    END,
                    total_power_dps_key = EXCLUDED.total_power_dps_key,
                    total_power_scale = EXCLUDED.total_power_scale,
                    gateway_device_id = EXCLUDED.gateway_device_id,
                    cid = EXCLUDED.cid,
                    power_correction_factor = EXCLUDED.power_correction_factor,
                    ingest_token = EXCLUDED.ingest_token,
                    updated_at = NOW()
                """,
                (
                    device_id,
                    local_key,
                    ip_address,
                    version,
                    total_power_dps_key,
                    total_power_scale,
                    gateway_device_id,
                    cid,
                    power_correction_factor,
                    ingest_token,
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


def find_battery_monitor_device_id(config: AppConfig) -> str | None:
    """Return the device_id of the battery monitor (the device exposing a
    `state_of_charge` capability). Used to source a fresh pack voltage when an
    active-pusher device reports only current."""
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT device_id FROM device_capabilities
                WHERE capability_code = 'state_of_charge'
                LIMIT 1
                """
            )
            row = cursor.fetchone()
    return str(row.get("device_id")) if row else None


def lookup_device_by_ingest_token(config: AppConfig, token: str) -> dict[str, Any] | None:
    """Return the device row matching this push-ingest token. Used by the
    /api/ingest endpoint to authenticate active-pusher devices (gear that
    POSTs its readings to us instead of being LAN-polled). Also returns
    the device's power_correction_factor so the endpoint can scale power
    on the way in, the same way the LAN poller does in extract_metrics."""
    if not token:
        return None
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.device_id, d.name, d.is_energy_meter, d.is_generator,
                       COALESCE(c.power_correction_factor, 1.0) AS power_correction_factor
                FROM device_connections c
                JOIN devices d ON d.device_id = c.device_id
                WHERE c.ingest_token = %s
                LIMIT 1
                """,
                (token,),
            )
            row = cursor.fetchone()
    return row


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
            # If another writer is already refreshing the same window, swallow
            # the lock error: our sample is already committed, the caggs use
            # materialized_only=false so realtime computation covers the gap
            # until the next refresh wins.
            for view, w_start, w_end in (
                ("samples_hourly", hourly_start, hourly_end),
                ("samples_daily", daily_start, daily_end),
                ("samples_monthly", monthly_start, monthly_end),
            ):
                try:
                    cursor.execute(f"CALL refresh_continuous_aggregate('{view}', %s, %s)", (w_start, w_end))
                except (psycopg.errors.LockNotAvailable, psycopg.errors.InvalidParameterValue):
                    # InvalidParameterValue happens on the monthly cagg when
                    # the window is exactly one bucket wide (TimescaleDB
                    # demands strictly more). The data is committed; the cagg
                    # will catch up on the next save_sample that lands in an
                    # adjacent month.
                    pass


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

    is_energy_meter = bool(device.get("is_energy_meter", True))
    is_active_pusher = bool(str(connection.get("ingest_token") or "").strip())
    if is_energy_meter and not is_active_pusher and summary.get("default_power_dps_key") in (None, ""):
        # Active pushers (POST /api/ingest/...) deliver power_w directly, so
        # they don't need a DPS key to extract power from raw_dps.
        raise ValueError(f"{path.name}: summary.default_power_dps_key is required for energy meter devices")

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
        is_generator=bool(device.get("is_generator", False)),
        is_solar_consumer=bool(device.get("is_solar_consumer", False)),
        allow_custom_automation=bool(device.get("allow_custom_automation", False)),
        disabled=bool(device.get("disabled", False)),
        is_gateway=bool(device.get("is_gateway", False)),
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
        gateway_device_id=(str(connection.get("gateway_device_id") or "").strip() or None),
        cid=(str(connection.get("cid") or "").strip() or None),
        power_correction_factor=float(connection.get("power_correction_factor") or 1.0),
        ingest_token=(str(connection.get("ingest_token") or "").strip() or None),
    )

def sync_device_profiles_from_disk(config: AppConfig, profiles_dir: Path | None = None) -> list[str]:
    root = profiles_dir or PROFILES_DIR
    loaded_device_ids: list[str] = []
    seen_device_ids: set[str] = set()

    # Two passes — gateways first, then everything else — so a sub-device
    # row whose connection FK points at the gateway can never insert before
    # the gateway's `devices` row exists.
    profile_docs: list[tuple[Path, str, int, dict[str, Any], str, bool]] = []
    for path in _profile_file_paths(root):
        device_id, profile_version, payload, content_hash = _load_profile_document(path)
        if device_id in seen_device_ids:
            raise ValueError(f"Duplicate profile device_id detected: {device_id}")
        seen_device_ids.add(device_id)
        is_gateway = bool(((payload.get("device") or {}).get("is_gateway")))
        profile_docs.append((path, device_id, profile_version, payload, content_hash, is_gateway))

    profile_docs.sort(key=lambda item: (0 if item[5] else 1, item[1]))

    for path, device_id, profile_version, payload, content_hash, _ in profile_docs:
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
                SELECT d.name, d.room, d.device_kind, d.is_energy_meter, d.is_charger, d.is_generator,
                        d.is_solar_consumer,
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
                SELECT d.name, d.room, d.device_id, d.device_kind, d.is_energy_meter, d.is_charger, d.is_generator,
                       d.is_gateway,
                       d.product_name, d.category_code, d.product_id, d.icon,
                       d.total_power_dps_key, d.visualized_codes, d.power_type,
                       COALESCE(NULLIF(c.ip_address, ''), gc.ip_address, '') AS ip_address,
                       (COALESCE(NULLIF(c.ip_address, ''), gc.ip_address, '') <> '') AS connection_ready,
                       c.gateway_device_id,
                       gd.name AS gateway_name
                FROM devices d
                LEFT JOIN device_connections c ON c.device_id = d.device_id
                LEFT JOIN device_connections gc ON gc.device_id = c.gateway_device_id
                LEFT JOIN devices gd ON gd.device_id = c.gateway_device_id
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
                SELECT d.name, d.room, d.device_id, d.is_gateway,
                       COALESCE(NULLIF(c.local_key, ''), gc.local_key, '') AS local_key,
                       COALESCE(NULLIF(c.ip_address, ''), gc.ip_address, '') AS ip_address,
                       COALESCE(NULLIF(c.version, 0), gc.version, 3.3) AS version,
                       c.total_power_dps_key, c.total_power_scale,
                       c.gateway_device_id, c.cid,
                       COALESCE(c.power_correction_factor, 1.0) AS power_correction_factor,
                       d.visualized_codes, d.power_type
                FROM devices d
                JOIN device_connections c ON c.device_id = d.device_id
                LEFT JOIN device_connections gc ON gc.device_id = c.gateway_device_id
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
        is_gateway=bool(row.get("is_gateway")),
        gateway_device_id=row.get("gateway_device_id"),
        cid=row.get("cid"),
        power_correction_factor=float(row.get("power_correction_factor") or 1.0),
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
                SELECT d.name, d.room, d.device_id, d.is_gateway,
                       COALESCE(NULLIF(c.local_key, ''), gc.local_key, '') AS local_key,
                       COALESCE(NULLIF(c.ip_address, ''), gc.ip_address, '') AS ip_address,
                       COALESCE(NULLIF(c.version, 0), gc.version, 3.3) AS version,
                       c.total_power_dps_key, c.total_power_scale,
                       c.gateway_device_id, c.cid,
                       COALESCE(c.power_correction_factor, 1.0) AS power_correction_factor,
                       d.visualized_codes, d.power_type
                FROM devices d
                JOIN device_connections c ON c.device_id = d.device_id
                LEFT JOIN device_connections gc ON gc.device_id = c.gateway_device_id
                WHERE COALESCE(NULLIF(c.local_key, ''), gc.local_key, '') <> ''
                  AND COALESCE(NULLIF(c.ip_address, ''), gc.ip_address, '') <> ''
                  AND NOT d.disabled
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
                is_gateway=bool(row.get("is_gateway")),
                gateway_device_id=row.get("gateway_device_id"),
                cid=row.get("cid"),
                power_correction_factor=float(row.get("power_correction_factor") or 1.0),
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


def get_recent_power_trace(
    config: AppConfig,
    device_id: str,
    start: datetime,
    end: datetime,
    max_points: int = 360,
) -> list[dict[str, Any]]:
    """Recent (captured_at, power_w) samples for sparkline/line charts.
    If there are more samples than max_points we downsample by simple stride
    so the JSON payload stays small without blurring the trend."""
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT captured_at, power_w
                FROM samples
                WHERE device_id = %s AND captured_at >= %s AND captured_at <= %s
                ORDER BY captured_at ASC
                """,
                (device_id, start, end),
            )
            rows = cursor.fetchall()
    if not rows:
        return []
    stride = max(1, len(rows) // max_points) if max_points > 0 else 1
    series: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if index % stride != 0 and index != len(rows) - 1:
            continue
        captured_at = row.get("captured_at")
        if not isinstance(captured_at, datetime):
            captured_at = _parse_dt(captured_at)
        series.append(
            {
                "timestamp": captured_at.isoformat(),
                "power_kw": round(float(row.get("power_w") or 0.0) / 1000.0, 3),
            }
        )
    return series


def get_sensor_history(
    config: AppConfig,
    device_id: str,
    capabilities: list[dict[str, Any]],
    visualized_codes: list[str] | tuple[str, ...],
    start: datetime,
    end: datetime,
    max_points: int = 720,
) -> dict[str, Any]:
    """Per-DP time series for the sensor page. Bucketing is done in SQL via
    TimescaleDB's time_bucket() so we don't pull millions of raw rows for a
    one-year view. Each visualized DP becomes one series; booleans are
    coerced to 0/1 inside the SQL CASE."""
    cap_by_code: dict[str, dict[str, Any]] = {}
    for cap in capabilities:
        code = str(cap.get("capability_code") or "")
        dp_id = cap.get("dp_id")
        if not code or dp_id is None or code in cap_by_code:
            continue
        cap_by_code[code] = {
            "dp_id": str(dp_id),
            "label": str(cap.get("capability_name") or code),
            "values_json": cap.get("values_json") or {},
            "value_type": str(cap.get("value_type") or "").lower(),
        }

    ordered_codes: list[str] = []
    for code in visualized_codes:
        s = str(code)
        if s in cap_by_code and s not in ordered_codes:
            ordered_codes.append(s)
    if not ordered_codes:
        ordered_codes = list(cap_by_code.keys())
    if not ordered_codes:
        return {"bucket_seconds": 0, "series": []}

    span_seconds = max(int((end - start).total_seconds()), 1)
    # Round bucket to a sensible interval. <5s gives 0 in older Timescale,
    # and very small buckets blow up the result set.
    bucket_seconds = max(span_seconds // max_points, 5)

    # dp_id is sourced from the device profile / DB, not user input — safe
    # to interpolate. Boolean / numeric / string mix gets coerced inside the
    # CASE: 'true'/'false' → 1/0, anything that matches a number → float,
    # everything else → NULL (so AVG ignores it).
    select_cols = []
    dp_ids = [cap_by_code[c]["dp_id"] for c in ordered_codes]
    for idx, dp_id in enumerate(dp_ids):
        # JSON escape: dp ids are simple numerics, but %% protects from
        # the cursor's parameter substitution
        select_cols.append(
            f"AVG("
            f"  CASE "
            f"    WHEN raw_dps->>'{dp_id}' = 'true' THEN 1.0 "
            f"    WHEN raw_dps->>'{dp_id}' = 'false' THEN 0.0 "
            f"    WHEN raw_dps->>'{dp_id}' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
            f"      THEN (raw_dps->>'{dp_id}')::numeric "
            f"    ELSE NULL "
            f"  END"
            f") AS dp_{idx}"
        )
    sql = (
        f"SELECT time_bucket(INTERVAL '{bucket_seconds} seconds', captured_at) AS bucket, "
        + ", ".join(select_cols)
        + " FROM samples WHERE device_id = %s AND captured_at >= %s AND captured_at <= %s "
        "GROUP BY bucket ORDER BY bucket"
    )

    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, (device_id, start, end))
            rows = cursor.fetchall()

    series: list[dict[str, Any]] = []
    for idx, code in enumerate(ordered_codes):
        meta = cap_by_code[code]
        values_json = meta["values_json"]
        try:
            scale_digits = int(values_json.get("scale", 0) or 0)
        except (TypeError, ValueError):
            scale_digits = 0
        divisor = 10 ** scale_digits if scale_digits > 0 else 1
        unit = str(values_json.get("unit") or "").strip()
        is_boolean = meta["value_type"] == "boolean"
        col_name = f"dp_{idx}"
        points: list[list[float]] = []
        for row in rows:
            value = row.get(col_name)
            if value is None:
                continue
            ts = row.get("bucket")
            if not isinstance(ts, datetime):
                ts = _parse_dt(ts)
            avg = float(value) / divisor
            points.append([int(ts.timestamp() * 1000), round(avg, 4)])
        series.append(
            {
                "code": code,
                "label": meta["label"],
                "dp_id": meta["dp_id"],
                "unit": unit,
                "is_boolean": is_boolean,
                "data": points,
            }
        )

    # Inverter device gets an extra derived series — the implicit solar
    # estimate computed from the energy balance at the battery shunt:
    #   solar_W ≈ atorch_net_W + inverter_AC_W / KPD_inverter
    # KPD measured 2026-06-22 at ~93.4 % against this exact rig. Yellow
    # series on the chart matches the LCD-card solar accent across the app.
    if device_id == _INVERTER_DEVICE_ID:
        solar_points = _inverter_solar_series(
            config, start, end, bucket_seconds, divisor=100.0,
        )
        if solar_points:
            series.append({
                "code": "solar_estimate",
                "label": "Солнце",
                "dp_id": None,
                "unit": "W",
                "is_boolean": False,
                "data": solar_points,
            })

    return {
        "bucket_seconds": bucket_seconds,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "series": series,
    }


_INVERTER_DEVICE_ID = "bfef2249e8f03df7891epc"
_BATTERY_MONITOR_DEVICE_ID = "bff9e5598e9abd78268oze"
_INVERTER_KPD = 0.934
# 72 V wall charger (bf1bd578…). When it's running it pumps current into
# the battery that Atorch sees as a positive net — without subtracting
# it the computed-solar formula would credit charger output as solar.
# Charger efficiency measured against the previous Zigbee era ≈ 87.6 %.
_CHARGER_DEVICE_ID = "bf1bd578076c64ff7cjl6p"
_CHARGER_KPD = 0.876


def _inverter_solar_series(
    config: AppConfig,
    start: datetime,
    end: datetime,
    bucket_seconds: int,
    divisor: float = 100.0,
) -> list[list[float]]:
    """Bucketed implicit-solar series for the inverter device page.

    LEFT JOIN inverter and Atorch on the same time_bucket so a moment of
    inverter draw without a fresh Atorch sample (or vice versa) doesn't
    drop the bucket — fills with 0 for the missing leg, which makes the
    line continuous through DPS event gaps."""
    sql = f"""
    WITH inv AS (
      SELECT time_bucket(INTERVAL '{bucket_seconds} seconds', captured_at) AS bucket,
             AVG(power_w) AS power_w
      FROM samples
      WHERE device_id = %s AND captured_at >= %s AND captured_at <= %s
      GROUP BY bucket
    ),
    atorch AS (
      SELECT time_bucket(INTERVAL '{bucket_seconds} seconds', captured_at) AS bucket,
             AVG((raw_dps->>'19')::numeric / {divisor}) AS net_w
      FROM samples
      WHERE device_id = %s AND captured_at >= %s AND captured_at <= %s
        AND raw_dps ? '19'
      GROUP BY bucket
    ),
    charger AS (
      SELECT time_bucket(INTERVAL '{bucket_seconds} seconds', captured_at) AS bucket,
             AVG(power_w) AS power_w
      FROM samples
      WHERE device_id = %s AND captured_at >= %s AND captured_at <= %s
      GROUP BY bucket
    )
    -- INNER JOIN on (inv, atorch): solar only computed when both sensors
    -- have data in the bucket — pre-inverter periods have no inverter
    -- samples and would otherwise show Atorch alone as 'solar'.
    -- LEFT JOIN charger: when the wall charger runs, its DC contribution
    -- to the battery (≈ AC × 0.876) has to be subtracted from atorch_net,
    -- otherwise its kWh gets credited as solar on cloudy auto-charge nights.
    -- GREATEST(0, …): solar is physically non-negative.
    SELECT i.bucket,
           GREATEST(0, a.net_w + i.power_w / {_INVERTER_KPD}
                       - COALESCE(c.power_w, 0) * {_CHARGER_KPD}) AS solar_w
    FROM inv i INNER JOIN atorch a USING (bucket)
                LEFT JOIN charger c USING (bucket)
    ORDER BY 1
    """
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql,
                (_INVERTER_DEVICE_ID, start, end,
                 _BATTERY_MONITOR_DEVICE_ID, start, end,
                 _CHARGER_DEVICE_ID, start, end),
            )
            rows = cursor.fetchall()
    points: list[list[float]] = []
    for row in rows:
        ts = row.get("bucket")
        if not isinstance(ts, datetime):
            ts = _parse_dt(ts)
        value = float(row.get("solar_w") or 0.0)
        points.append([int(ts.timestamp() * 1000), round(value, 2)])
    return points


def get_battery_load_power_trace(
    config: AppConfig,
    start: datetime,
    end: datetime,
    bucket_seconds: int = 30,
    max_points: int = 720,
) -> list[dict[str, Any]]:
    """Instantaneous AC-side load on the inverter, derived from the energy
    balance at the battery shunt:

        load_W = sum(solar_generators_W) − Atorch_net_W

    Atorch's dp 19 is signed (positive when current flows INTO the battery,
    negative when out to the inverter), so this gives the true power leaving
    the system via the load path at each moment. Clamped to ≥ 0 — if the
    formula goes negative it usually means the wall charger is also charging
    (extra source we don't subtract), or just sampling jitter at low loads.

    Returns the same `{timestamp, power_kw}` shape as
    `get_solar_consumers_power_trace` so the existing device.js can render
    it as the "consumption" overlay on a generator's chart with no JS
    changes."""
    bucket_seconds = max(5, int(bucket_seconds or 30))
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT device_id FROM devices
                WHERE is_generator = TRUE AND is_energy_meter = TRUE
                """
            )
            gen_ids = [str(r.get("device_id")) for r in cursor.fetchall() if r.get("device_id")]
            cursor.execute(
                """
                SELECT device_id FROM device_capabilities
                WHERE capability_code = 'state_of_charge' LIMIT 1
                """
            )
            row = cursor.fetchone()
            monitor_id = str(row.get("device_id")) if row else None
            if not gen_ids or not monitor_id:
                return []

            cursor.execute(
                f"""
                WITH solar AS (
                  SELECT time_bucket(INTERVAL '{bucket_seconds} seconds', captured_at) AS bucket,
                         AVG(power_w) AS solar_w
                  FROM samples
                  WHERE device_id = ANY(%s)
                    AND captured_at >= %s AND captured_at <= %s
                  GROUP BY bucket
                ),
                atorch AS (
                  SELECT time_bucket(INTERVAL '{bucket_seconds} seconds', captured_at) AS bucket,
                         AVG((raw_dps->>'19')::numeric / 100.0) AS net_w
                  FROM samples
                  WHERE device_id = %s
                    AND captured_at >= %s AND captured_at <= %s
                    AND raw_dps ? '19'
                  GROUP BY bucket
                )
                SELECT s.bucket, GREATEST(0, s.solar_w - COALESCE(a.net_w, 0)) AS load_w
                FROM solar s
                LEFT JOIN atorch a USING (bucket)
                ORDER BY s.bucket
                """,
                (gen_ids, start, end, monitor_id, start, end),
            )
            rows = cursor.fetchall()

    if not rows:
        return []
    timeline = [(r["bucket"], float(r["load_w"] or 0.0)) for r in rows]
    if max_points > 0 and len(timeline) > max_points:
        stride = max(1, len(timeline) // max_points)
        timeline = [pt for i, pt in enumerate(timeline) if i % stride == 0 or i == len(timeline) - 1]
    return [
        {"timestamp": ts.isoformat(), "power_kw": round(w / 1000.0, 3)}
        for ts, w in timeline
    ]


def get_solar_consumers_power_trace(
    config: AppConfig,
    start: datetime,
    end: datetime,
    bucket_seconds: int = 30,
    max_points: int = 360,
) -> list[dict[str, Any]]:
    """Combined instantaneous draw of devices tagged is_solar_consumer over
    the given window, bucketed by bucket_seconds. Used by the dashboard
    sparkline and the generator detail page to overlay consumption against
    the panel's generation curve."""
    bucket_seconds = max(5, int(bucket_seconds or 30))
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT device_id
                FROM devices
                WHERE is_solar_consumer = TRUE AND is_energy_meter = TRUE
                """
            )
            device_ids = [str(r.get("device_id") or "") for r in cursor.fetchall() if r.get("device_id")]
            if not device_ids:
                return []
            cursor.execute(
                """
                SELECT captured_at, device_id, power_w
                FROM samples
                WHERE device_id = ANY(%s) AND captured_at >= %s AND captured_at <= %s
                ORDER BY captured_at ASC
                """,
                (device_ids, start, end),
            )
            rows = cursor.fetchall()
    if not rows:
        return []

    # bucket the timeline; within a bucket keep the latest power per device,
    # then sum across devices for that bucket.
    buckets: dict[datetime, dict[str, float]] = {}
    for row in rows:
        captured_at = row.get("captured_at")
        if not isinstance(captured_at, datetime):
            captured_at = _parse_dt(captured_at)
        epoch = int(captured_at.timestamp())
        bucket_epoch = (epoch // bucket_seconds) * bucket_seconds
        bucket_ts = datetime.fromtimestamp(bucket_epoch, tz=captured_at.tzinfo or timezone.utc)
        device_id = str(row.get("device_id") or "")
        bucket_entry = buckets.setdefault(bucket_ts, {})
        bucket_entry[device_id] = float(row.get("power_w") or 0.0)

    timeline = sorted(buckets)
    if max_points > 0 and len(timeline) > max_points:
        stride = max(1, len(timeline) // max_points)
        timeline = [ts for index, ts in enumerate(timeline) if index % stride == 0 or index == len(timeline) - 1]

    return [
        {
            "timestamp": ts.isoformat(),
            "power_kw": round(sum(buckets[ts].values()) / 1000.0, 3),
        }
        for ts in timeline
    ]


def list_automations(config: AppConfig) -> list[dict[str, Any]]:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT a.id, a.slug, a.name, a.description, a.device_type,
                       a.bound_device_id, a.cron_schedule, a.enabled,
                       a.config_json, a.last_run_at, a.next_run_at,
                       a.last_run_status, a.last_run_log,
                       d.name AS bound_device_name
                FROM automations a
                LEFT JOIN devices d ON d.device_id = a.bound_device_id
                ORDER BY a.name ASC
                """
            )
            return cursor.fetchall()


def get_automation(config: AppConfig, slug: str) -> dict[str, Any] | None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, slug, name, description, device_type,
                       bound_device_id, cron_schedule, enabled,
                       config_json, last_run_at, next_run_at,
                       last_run_status, last_run_log
                FROM automations
                WHERE slug = %s
                """,
                (slug,),
            )
            return cursor.fetchone()


def get_automation_candidates(config: AppConfig, device_type: str) -> list[dict[str, Any]]:
    """Devices marked allow_custom_automation that match the given type.
    device_type='charger' matches devices with is_charger=true; 'any' returns
    every flagged device."""
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT device_id, name, room, is_charger, is_generator, is_solar_consumer, device_kind
                FROM devices
                WHERE allow_custom_automation = TRUE
                ORDER BY name ASC
                """
            )
            rows = cursor.fetchall()
    if device_type == "any":
        return rows
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if device_type == "charger" and row.get("is_charger"):
            filtered.append(row)
        elif device_type == "generator" and row.get("is_generator"):
            filtered.append(row)
        elif device_type == row.get("device_kind"):
            filtered.append(row)
    return filtered


def upsert_automation(
    config: AppConfig,
    *,
    slug: str,
    name: str,
    description: str,
    device_type: str,
    default_cron: str,
    default_config: dict[str, Any],
) -> None:
    """Seed an automation from the registry — only inserts new rows, leaves
    user choices on existing ones intact."""
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO automations (slug, name, description, device_type, cron_schedule, config_json)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT(slug) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    device_type = EXCLUDED.device_type,
                    updated_at = NOW()
                """,
                (slug, name, description, device_type, default_cron, Jsonb(default_config)),
            )
        connection.commit()


def set_automation_bound_device(config: AppConfig, slug: str, device_id: str | None) -> bool:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE automations SET bound_device_id = %s, updated_at = NOW() WHERE slug = %s",
                (device_id, slug),
            )
            updated = cursor.rowcount > 0
        connection.commit()
    return updated


def set_automation_enabled(config: AppConfig, slug: str, enabled: bool) -> bool:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE automations SET enabled = %s, updated_at = NOW() WHERE slug = %s",
                (bool(enabled), slug),
            )
            updated = cursor.rowcount > 0
        connection.commit()
    return updated


def set_automation_cron(config: AppConfig, slug: str, cron_schedule: str) -> bool:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE automations SET cron_schedule = %s, updated_at = NOW() WHERE slug = %s",
                (cron_schedule, slug),
            )
            updated = cursor.rowcount > 0
        connection.commit()
    return updated


def record_automation_run(
    config: AppConfig,
    slug: str,
    *,
    status: str,
    log: str,
    next_run_at: datetime | None,
) -> None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE automations
                SET last_run_at = NOW(),
                    last_run_status = %s,
                    last_run_log = %s,
                    next_run_at = %s,
                    updated_at = NOW()
                WHERE slug = %s
                """,
                (status, log, next_run_at, slug),
            )
        connection.commit()


def update_automation_next_run(config: AppConfig, slug: str, next_run_at: datetime | None) -> None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE automations SET next_run_at = %s WHERE slug = %s",
                (next_run_at, slug),
            )
        connection.commit()


def set_device_solar_consumer(config: AppConfig, device_id: str, enabled: bool) -> bool:
    """Toggle the is_solar_consumer flag from the UI. Returns True if the
    row existed and was updated."""
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE devices SET is_solar_consumer = %s, updated_at = NOW() WHERE device_id = %s",
                (bool(enabled), device_id),
            )
            updated = cursor.rowcount > 0
        connection.commit()
    return updated


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


def _counter_value_at_or_before(
    counter_points: list[tuple[datetime, float]],
    target: datetime,
) -> float | None:
    """Return the last counter reading whose timestamp is <= target. Assumes
    counter_points is sorted ascending by timestamp. Used to anchor session
    energy at the device-counter value as it was at session start/end."""
    if not counter_points:
        return None
    chosen: float | None = None
    for ts, kwh in counter_points:
        if ts > target:
            break
        chosen = kwh
    return chosen


def _get_add_ele_meta_from_capabilities(capabilities: list[dict[str, Any]]) -> tuple[str, float] | None:
    """Like _get_energy_counter_meta_from_capabilities but only for `add_ele`,
    regardless of power_type. Used by current-type chargers that also expose
    a Tuya energy counter — lets us report exact session energy from the
    device's own counter (telescoping delta) instead of trapezoidal integration
    of cur_power, so the totals agree with the meter on the device itself."""
    for capability in capabilities:
        code = str(capability.get("capability_code") or "")
        if code != "add_ele":
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


def get_period_breakdown(
    config: AppConfig,
    start: datetime,
    end: datetime,
    bucket: str,
) -> list[dict[str, Any]]:
    """Aggregate energy by time bucket across all energy meter devices,
    splitting consumers from generators. Used by the month/year report."""
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.device_id, d.is_generator, d.power_type,
                       c.total_power_dps_key, c.total_power_scale
                FROM devices d
                LEFT JOIN device_connections c ON c.device_id = d.device_id
                WHERE d.is_energy_meter = TRUE
                  AND NOT d.disabled
                """
            )
            device_rows = cursor.fetchall()

    is_generator_by_device = {
        str(r.get("device_id") or ""): bool(r.get("is_generator")) for r in device_rows
    }
    counter_meta_by_device = _build_energy_counter_meta_by_device(device_rows)
    device_ids = [d for d in is_generator_by_device if d]
    if not device_ids:
        return []

    agg_rows = _read_aggregate_rows(config, device_ids, bucket, start, end)
    rows_by_device = _group_aggregate_rows_by_device(agg_rows)

    consumed_by_bucket: dict[datetime, float] = {}
    generated_by_bucket: dict[datetime, float] = {}
    for device_id, rows in rows_by_device.items():
        counter_meta = counter_meta_by_device.get(device_id)
        series = _build_chart_series_from_aggregate(rows, bucket, counter_meta)
        target = generated_by_bucket if is_generator_by_device.get(device_id) else consumed_by_bucket
        for item in series:
            ts = _parse_dt(item["timestamp"])
            ts = _normalize_bucket_for_timezone(config, ts, bucket)
            target[ts] = target.get(ts, 0.0) + float(item["energy_kwh"] or 0.0)

    timeline = sorted(set(consumed_by_bucket.keys()) | set(generated_by_bucket.keys()))
    breakdown: list[dict[str, Any]] = []
    for ts in timeline:
        consumed = consumed_by_bucket.get(ts, 0.0)
        generated = generated_by_bucket.get(ts, 0.0)
        breakdown.append(
            {
                "bucket": ts.isoformat(),
                "consumed_kwh": round(consumed, 3),
                "generated_kwh": round(generated, 3),
                "net_kwh": round(consumed - generated, 3),
            }
        )
    return breakdown


def get_implicit_solar_by_bucket(
    config: AppConfig,
    start: datetime,
    end: datetime,
    bucket: str,
) -> dict[datetime, dict[str, float]]:
    """For each bucket, compute the implicit solar generation as
       solar_kwh = max(0, atorch_discharged_kwh - charger_consumed_kwh).

    Reasoning: nothing in the project meters the solar feed directly, but the
    Atorch on the battery integrates *all* current flowing into and out of
    the pack. Over a day where SoC roughly returns to its starting point,
    everything that left the battery (discharge) had to come from somewhere
    — either the wall charger or the solar panels. Anything the load consumed
    above what the charger pulled from the wall must therefore be solar.

    This is a *lower bound* because charger has its own losses (the wall socket
    sees more kWh than what makes it into the battery) and inverter losses on
    the discharge side are not yet accounted for. The user is aware and is fine
    with the conservative number for v1.

    Returns mapping bucket_dt (TZ-aware, UTC-aligned like samples_daily) →
    {"discharged_kwh", "charged_kwh", "charger_kwh", "solar_kwh"}."""
    if bucket not in ("day", "month"):
        return {}
    interval = "1 day" if bucket == "day" else "1 month"

    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT device_id FROM device_capabilities
                WHERE capability_code = 'state_of_charge'
                LIMIT 1
                """
            )
            row = cursor.fetchone()
            monitor_id = str(row.get("device_id")) if row else None

            cursor.execute(
                """
                SELECT device_id FROM devices
                WHERE is_charger = TRUE AND is_energy_meter = TRUE
                """
            )
            charger_ids = [str(r.get("device_id")) for r in cursor.fetchall() if r.get("device_id")]

            if not monitor_id:
                return {}

            # Atorch keeps two undocumented absolute counters: dp 126 = total
            # mAh ever pumped INTO the battery, dp 127 = total mAh ever pulled
            # OUT. They're tracked on the device's internal high-rate ADC so
            # simultaneous charge+discharge does not net-cancel like dp 19
            # (signed cur_power) does. Convert to Wh per interval via the
            # *measured* dp 20 voltage on each pair (trapezoidal), not a
            # constant nominal voltage — gives accurate Wh at both low and
            # high SoC. Gaps > 60 s are dropped (Wi-Fi blip safety).
            cursor.execute(
                f"""
                WITH atorch AS (
                  SELECT captured_at,
                         (raw_dps->>'126')::numeric AS charge_mah,
                         (raw_dps->>'127')::numeric AS discharge_mah,
                         (raw_dps->>'20')::numeric / 100.0 AS voltage_v
                  FROM samples
                  WHERE device_id = %s
                    AND captured_at >= %s
                    AND captured_at <  %s
                    AND raw_dps ? '126' AND raw_dps ? '127' AND raw_dps ? '20'
                ),
                pairs AS (
                  SELECT captured_at, charge_mah, discharge_mah, voltage_v,
                         lag(captured_at)   OVER (ORDER BY captured_at) AS prev_ts,
                         lag(charge_mah)    OVER (ORDER BY captured_at) AS prev_charge,
                         lag(discharge_mah) OVER (ORDER BY captured_at) AS prev_discharge,
                         lag(voltage_v)     OVER (ORDER BY captured_at) AS prev_voltage
                  FROM atorch
                )
                SELECT time_bucket(INTERVAL '{interval}', captured_at) AS bucket,
                       SUM(CASE
                         WHEN EXTRACT(epoch FROM captured_at - prev_ts) BETWEEN 0.1 AND 60
                          AND discharge_mah >= prev_discharge
                         THEN (discharge_mah - prev_discharge) / 1000.0
                              * (voltage_v + prev_voltage) / 2.0
                         ELSE 0
                       END) AS discharged_wh,
                       SUM(CASE
                         WHEN EXTRACT(epoch FROM captured_at - prev_ts) BETWEEN 0.1 AND 60
                          AND charge_mah >= prev_charge
                         THEN (charge_mah - prev_charge) / 1000.0
                              * (voltage_v + prev_voltage) / 2.0
                         ELSE 0
                       END) AS charged_wh
                FROM pairs
                GROUP BY 1
                ORDER BY 1
                """,
                (monitor_id, start, end),
            )
            discharge_by_bucket: dict[datetime, tuple[float, float]] = {}
            for row in cursor.fetchall():
                bucket_dt = row.get("bucket")
                if bucket_dt is None:
                    continue
                discharge_by_bucket[bucket_dt] = (
                    float(row.get("discharged_wh") or 0.0) / 1000.0,
                    float(row.get("charged_wh") or 0.0) / 1000.0,
                )

            charger_by_bucket: dict[datetime, float] = {}
            if charger_ids:
                cagg_view = "samples_daily" if bucket == "day" else "samples_monthly"
                cursor.execute(
                    f"""
                    SELECT bucket, SUM(energy_wh) AS wh
                    FROM {cagg_view}
                    WHERE device_id = ANY(%s)
                      AND bucket >= %s AND bucket < %s
                    GROUP BY bucket
                    ORDER BY bucket
                    """,
                    (charger_ids, start, end),
                )
                for row in cursor.fetchall():
                    b = row.get("bucket")
                    if b is None:
                        continue
                    charger_by_bucket[b] = float(row.get("wh") or 0.0) / 1000.0

    result: dict[datetime, dict[str, float]] = {}
    for b in set(discharge_by_bucket) | set(charger_by_bucket):
        discharged, charged = discharge_by_bucket.get(b, (0.0, 0.0))
        charger = charger_by_bucket.get(b, 0.0)
        solar = max(0.0, discharged - charger)
        result[b] = {
            "discharged_kwh": round(discharged, 3),
            "charged_kwh": round(charged, 3),
            "charger_kwh": round(charger, 3),
            "solar_kwh": round(solar, 3),
        }
    return result


_COMPUTED_SOLAR_DEVICE_ID = "computed-solar"


def get_inverter_solar_kwh_period(
    config: AppConfig,
    start: datetime,
    end: datetime,
) -> float:
    """Total kWh of computed solar over the [start, end] window using the
    inverter-based formula (atorch_net_W + load_AC_W / KPD), 5-minute
    integration grid. Returns 0 for windows with no overlap between the
    two sensors — including the entire pre-inverter era, which is the
    point: BDM-era solar is *already* counted via the bdm-invertor
    device's real samples in generated_energy_kwh; this number adds the
    rest *without* double-counting."""
    sql = """
    WITH inv AS (
      SELECT time_bucket(INTERVAL '5 minutes', captured_at) AS bucket,
             AVG(power_w) AS power_w
      FROM samples
      WHERE device_id = %s AND captured_at >= %s AND captured_at <= %s
      GROUP BY bucket
    ),
    atorch AS (
      SELECT time_bucket(INTERVAL '5 minutes', captured_at) AS bucket,
             AVG((raw_dps->>'19')::numeric / 100.0) AS net_w
      FROM samples
      WHERE device_id = %s AND captured_at >= %s AND captured_at <= %s
        AND raw_dps ? '19'
      GROUP BY bucket
    ),
    charger AS (
      SELECT time_bucket(INTERVAL '5 minutes', captured_at) AS bucket,
             AVG(power_w) AS power_w
      FROM samples
      WHERE device_id = %s AND captured_at >= %s AND captured_at <= %s
      GROUP BY bucket
    )
    SELECT COALESCE(
        SUM(GREATEST(0, a.net_w + i.power_w / %s
                        - COALESCE(c.power_w, 0) * %s)) * (5.0 / 60.0) / 1000.0,
        0
    ) AS solar_kwh
    FROM inv i INNER JOIN atorch a USING (bucket)
                LEFT JOIN charger c USING (bucket)
    """
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql,
                (
                    _INVERTER_DEVICE_ID, start, end,
                    _BATTERY_MONITOR_DEVICE_ID, start, end,
                    _CHARGER_DEVICE_ID, start, end,
                    _INVERTER_KPD, _CHARGER_KPD,
                ),
            )
            row = cursor.fetchone()
    return round(float(row.get("solar_kwh") or 0.0), 3) if row else 0.0


def get_inverter_energy_breakdown(
    config: AppConfig,
    start: datetime,
    end: datetime,
    bucket_unit: str,
    tz_name: str = "Europe/Warsaw",
) -> dict[str, Any]:
    """Per-bucket load/solar/grid-delta breakdown for the inverter page on
    week/month/year. Uses the same 5-minute integration grid as
    get_inverter_solar_kwh_period, then aggregates to day or month buckets.

    `bucket_unit` is one of: 'day', 'month'. 'day' suits week/month views
    (7/30 bars); 'month' suits year view (up to 12 bars).
    """
    if bucket_unit not in {"day", "month"}:
        bucket_unit = "day"
    sql = f"""
    WITH inv_5m AS (
      SELECT time_bucket(INTERVAL '5 minutes', captured_at) AS bucket5,
             AVG(power_w) AS w
      FROM samples
      WHERE device_id = %s AND captured_at >= %s AND captured_at <= %s
      GROUP BY bucket5
    ),
    atorch_5m AS (
      SELECT time_bucket(INTERVAL '5 minutes', captured_at) AS bucket5,
             AVG((raw_dps->>'19')::numeric / 100.0) AS net_w
      FROM samples
      WHERE device_id = %s AND captured_at >= %s AND captured_at <= %s
        AND raw_dps ? '19'
      GROUP BY bucket5
    ),
    charger_5m AS (
      SELECT time_bucket(INTERVAL '5 minutes', captured_at) AS bucket5,
             AVG(power_w) AS w
      FROM samples
      WHERE device_id = %s AND captured_at >= %s AND captured_at <= %s
      GROUP BY bucket5
    )
    SELECT (date_trunc('{bucket_unit}', i.bucket5 AT TIME ZONE %s)
              AT TIME ZONE %s) AS bucket,
           SUM(i.w / 1000.0 * (5.0/60.0)) AS load_kwh,
           SUM(
               CASE WHEN a.net_w IS NOT NULL
                    THEN GREATEST(
                        0,
                        a.net_w + i.w / {_INVERTER_KPD}
                                - COALESCE(c.w, 0) * {_CHARGER_KPD}
                    )
                    ELSE 0
               END / 1000.0 * (5.0/60.0)
           ) AS solar_kwh
    FROM inv_5m i
    LEFT JOIN atorch_5m a USING (bucket5)
    LEFT JOIN charger_5m c USING (bucket5)
    GROUP BY 1
    ORDER BY 1
    """
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql,
                (
                    _INVERTER_DEVICE_ID, start, end,
                    _BATTERY_MONITOR_DEVICE_ID, start, end,
                    _CHARGER_DEVICE_ID, start, end,
                    tz_name, tz_name,
                ),
            )
            rows = cursor.fetchall()

    points: list[dict[str, Any]] = []
    total_load = 0.0
    total_solar = 0.0
    for row in rows:
        ts = row.get("bucket")
        if not isinstance(ts, datetime):
            ts = _parse_dt(ts)
        load = float(row.get("load_kwh") or 0.0)
        solar = float(row.get("solar_kwh") or 0.0)
        delta = max(0.0, load - solar)
        points.append({
            "ts": int(ts.timestamp() * 1000),
            "load_kwh": round(load, 3),
            "solar_kwh": round(solar, 3),
            "delta_kwh": round(delta, 3),
        })
        total_load += load
        total_solar += solar
    total_delta = max(0.0, total_load - total_solar)
    return {
        "bucket_unit": bucket_unit,
        "points": points,
        "totals": {
            "load_kwh": round(total_load, 3),
            "solar_kwh": round(total_solar, 3),
            "delta_kwh": round(total_delta, 3),
            "solar_share_pct": (
                round(100.0 * total_solar / total_load, 1) if total_load > 0 else 0.0
            ),
        },
    }


def get_inverter_solar_now_w(live_samples: dict[str, "DeviceSample"] | None) -> float:
    """Instantaneous computed solar wattage: atorch_net_W + load_AC_W / KPD,
    clamped to ≥ 0. Pulls from the in-memory live_samples snapshot so we
    don't hit the DB on every dashboard refresh tick.

    Returns 0.0 when either reading is unavailable (poll loop hasn't seeded
    them yet, network outage, etc.) — no point displaying a stale or
    half-computed value."""
    if not live_samples:
        return 0.0
    atorch = live_samples.get(_BATTERY_MONITOR_DEVICE_ID)
    inv = live_samples.get(_INVERTER_DEVICE_ID)
    if atorch is None or inv is None:
        return 0.0
    raw = atorch.raw_dps or {}
    raw_v = raw.get("19")
    if raw_v is None:
        return 0.0
    try:
        atorch_net_w = float(raw_v) / 100.0
    except (TypeError, ValueError):
        return 0.0
    load_ac_w = float(inv.power_w or 0.0)
    # Subtract charger DC contribution when the wall charger is running —
    # without this, every kWh from the socket would be (mis)credited as
    # solar on cloudy auto-charge nights.
    charger = live_samples.get(_CHARGER_DEVICE_ID)
    charger_dc_w = float(charger.power_w or 0.0) * _CHARGER_KPD if charger else 0.0
    return max(0.0, atorch_net_w + load_ac_w / _INVERTER_KPD - charger_dc_w)


def get_inverter_solar_trace(
    config: AppConfig,
    start: datetime,
    end: datetime,
    bucket_seconds: int = 30,
    max_points: int = 720,
) -> dict[str, list[dict[str, Any]]]:
    """Time-bucketed series for the computed solar dashboard card:
       solar = atorch_net + load_AC / KPD  (clamped ≥ 0)
       load  = load_AC

    Returns {'series': [...], 'consumers_series': [...]} in the same shape
    the dashboard generator card expects, so existing JS rendering picks
    it up without changes. INNER JOIN both legs so we don't fabricate
    solar when one sensor is missing."""
    bucket_seconds = max(5, int(bucket_seconds or 30))
    sql = f"""
    WITH inv AS (
      SELECT time_bucket(INTERVAL '{bucket_seconds} seconds', captured_at) AS bucket,
             AVG(power_w) AS power_w
      FROM samples
      WHERE device_id = %s AND captured_at >= %s AND captured_at <= %s
      GROUP BY bucket
    ),
    atorch AS (
      SELECT time_bucket(INTERVAL '{bucket_seconds} seconds', captured_at) AS bucket,
             AVG((raw_dps->>'19')::numeric / 100.0) AS net_w
      FROM samples
      WHERE device_id = %s AND captured_at >= %s AND captured_at <= %s
        AND raw_dps ? '19'
      GROUP BY bucket
    ),
    charger AS (
      SELECT time_bucket(INTERVAL '{bucket_seconds} seconds', captured_at) AS bucket,
             AVG(power_w) AS power_w
      FROM samples
      WHERE device_id = %s AND captured_at >= %s AND captured_at <= %s
      GROUP BY bucket
    )
    SELECT i.bucket,
           GREATEST(0, a.net_w + i.power_w / {_INVERTER_KPD}
                       - COALESCE(c.power_w, 0) * {_CHARGER_KPD}) AS solar_w,
           i.power_w AS load_w
    FROM inv i INNER JOIN atorch a USING (bucket)
                LEFT JOIN charger c USING (bucket)
    ORDER BY 1
    """
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql,
                (_INVERTER_DEVICE_ID, start, end,
                 _BATTERY_MONITOR_DEVICE_ID, start, end,
                 _CHARGER_DEVICE_ID, start, end),
            )
            rows = cursor.fetchall()
    series: list[dict[str, Any]] = []
    consumers_series: list[dict[str, Any]] = []
    for row in rows:
        ts = row.get("bucket")
        if not isinstance(ts, datetime):
            ts = _parse_dt(ts)
        ts_iso = ts.isoformat()
        series.append({
            "timestamp": ts_iso,
            "power_kw": round(float(row.get("solar_w") or 0.0) / 1000.0, 4),
        })
        consumers_series.append({
            "timestamp": ts_iso,
            "power_kw": round(float(row.get("load_w") or 0.0) / 1000.0, 4),
        })
    if max_points > 0 and len(series) > max_points:
        stride = max(1, len(series) // max_points)
        series = [p for i, p in enumerate(series) if i % stride == 0 or i == len(series) - 1]
        consumers_series = [p for i, p in enumerate(consumers_series) if i % stride == 0 or i == len(consumers_series) - 1]
    return {"series": series, "consumers_series": consumers_series}


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
                SELECT d.name, d.room, d.device_kind, d.is_energy_meter, d.is_charger, d.is_generator,
                    d.is_solar_consumer,
                    d.product_name, d.category_code, d.device_id,
                      d.power_type,
                       COALESCE(NULLIF(c.ip_address, ''), gc.ip_address, '') AS ip_address,
                      (COALESCE(NULLIF(c.ip_address, ''), gc.ip_address, '') <> '') AS connection_ready,
                      (c.ingest_token IS NOT NULL) AS is_pushed,
                      c.total_power_dps_key,
                      c.total_power_scale
                FROM devices d
                LEFT JOIN device_connections c ON c.device_id = d.device_id
                LEFT JOIN device_connections gc ON gc.device_id = c.gateway_device_id
                WHERE NOT d.disabled
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
    generator_devices = []
    sensor_devices = []
    consumed_energy_wh = 0.0
    generated_energy_wh = 0.0
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
            # Pushed devices (active sensors that POST their readings) are
            # treated as "always present" by the dashboard — going quiet for
            # hours is meaningful for them (no sun → no solar to report) and
            # shouldn't get them hidden under the hide-offline toggle.
            "is_pushed": bool(device.get("is_pushed")),
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
            today_local_date = now.astimezone(_get_timezone(config)).date()
            today_rows = [
                row for row in bucket_rows
                if (
                    (_parse_dt(row["bucket"]) if not isinstance(row["bucket"], datetime) else row["bucket"])
                    .astimezone(_get_timezone(config))
                    .date()
                ) == today_local_date
            ]
            day_energy_wh = _aggregate_energy_wh(today_rows, "day", energy_counter_meta)
            is_generator = bool(device.get("is_generator"))
            if is_generator:
                generated_energy_wh += device_energy_wh
            else:
                consumed_energy_wh += device_energy_wh
            device_entry: dict[str, Any] = {
                **base_entry,
                "is_generator": is_generator,
                "is_solar_consumer": bool(device.get("is_solar_consumer")),
                "month_energy_kwh": round(device_energy_wh / 1000.0, 3),
                "day_energy_kwh": round(day_energy_wh / 1000.0, 3),
            }
            if energy_counter_meta is None:
                current_power_w = (
                    _normalize_sample_power_w(float(latest["power_w"]), latest.get("raw_dps"))
                    if latest and latest.get("power_w") is not None
                    else 0.0
                )
                device_entry["current_power_kw"] = round(current_power_w / 1000.0, 3)
                if is_generator:
                    total_power_w -= current_power_w
                else:
                    total_power_w += current_power_w
            if is_generator:
                generator_devices.append(device_entry)
            else:
                devices.append(device_entry)
            continue

        sensor_devices.append(base_entry)

    net_energy_wh = consumed_energy_wh - generated_energy_wh

    # Solar energy for the month = BDM-era metered solar (already in
    # generated_energy_wh via bdm-invertor's real samples) PLUS new computed
    # solar from inverter+atorch since the rewiring. No overlap: the inner
    # join in get_inverter_solar_kwh_period yields 0 for any day without
    # inverter samples, and BDM was off-line by then.
    computed_solar_kwh = get_inverter_solar_kwh_period(config, month_start, now)
    solar_energy_kwh = round(generated_energy_wh / 1000.0 + computed_solar_kwh, 3)

    # Synthetic 'Солнце' generator card derived from atorch + inverter
    # measurements — replaces the role that the mppt-solar device used to
    # play on the dashboard. Not a real DB device; the power-trace endpoint
    # has a matching special case to compute its series on the fly.
    live_solar_w = get_inverter_solar_now_w(live_samples)
    has_inverter = any(
        str(d.get("device_id") or "") == _INVERTER_DEVICE_ID for d in device_rows
    )
    if has_inverter:
        generator_devices.append({
            "name": "Солнце",
            "room": "Зал",
            "device_id": _COMPUTED_SOLAR_DEVICE_ID,
            "device_kind": "virtual",
            "connection_ready": True,
            "ip_address": None,
            "is_pushed": True,        # treat as always-visible (hide-offline exempt)
            "last_seen": _format_display_datetime(config, now),
            "last_seen_age_seconds": 0,
            "last_seen_status": "ok",
            "raw_dps": {},
            "is_generator": True,
            "is_solar_consumer": False,
            # Synthetic 'Солнце' card shows the *new computed* solar only —
            # BDM history is the bdm-invertor card's job.
            "month_energy_kwh": computed_solar_kwh,
            "day_energy_kwh": get_inverter_solar_kwh_period(
                config,
                now.astimezone(_get_timezone(config)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                ),
                now,
            ),
            "current_power_kw": round(live_solar_w / 1000.0, 3),
        })

    return {
        "home_name": config.home_name,
        "month_energy_kwh": round(net_energy_wh / 1000.0, 3),
        "consumed_energy_kwh": round(consumed_energy_wh / 1000.0, 3),
        "generated_energy_kwh": round(generated_energy_wh / 1000.0, 3),
        "solar_energy_kwh": solar_energy_kwh,
        "estimated_cost": round((net_energy_wh / 1000.0) * config.tariff_per_kwh, 2),
        "device_count": online_device_count,
        "devices": devices,
        "generator_devices": generator_devices,
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
                      is_generator,
                      product_name, category_code, product_id, icon,
                      total_power_dps_key, visualized_codes, power_type
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
    add_ele_meta = _get_add_ele_meta_from_capabilities(capabilities)
    # We always pull raw_dps now so current-type chargers with add_ele can
    # report exact session energy from the device's own counter.
    needs_raw_dps = energy_counter_meta is not None or add_ele_meta is not None

    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            if needs_raw_dps:
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

    # If we have an add_ele counter, replace each session's trapezoidal energy
    # with the telescoping counter delta — that matches the device's internal
    # meter exactly (same physical quantity Atorch shows). Skip the override
    # when the counter delta disagrees with the trapezoidal estimate by more
    # than 2× in either direction: some Tuya plugs (e.g. 72V charger) ship a
    # glitchy add_ele that decrements between samples or under-counts heavily
    # due to DPS event-cache staleness on LAN. In those cases trapezoidal of
    # cur_power is the trustworthy source.
    if add_ele_meta is not None and sessions and sample_rows:
        dp_key, scale_divisor = add_ele_meta
        counter_points: list[tuple[datetime, float]] = []
        for row in sample_rows:
            kwh = _read_energy_counter_kwh(
                _normalize_json_field(row.get("raw_dps")), dp_key, scale_divisor
            )
            if kwh is not None:
                counter_points.append((row["captured_at"], kwh))
        if counter_points:
            for session in sessions:
                anchor_kwh = _counter_value_at_or_before(counter_points, session["start"])
                end_kwh = _counter_value_at_or_before(counter_points, session["end"])
                if anchor_kwh is None or end_kwh is None or end_kwh <= anchor_kwh:
                    continue
                delta_kwh = end_kwh - anchor_kwh
                trap_kwh = float(session["energy_kwh"] or 0.0)
                if trap_kwh > 0.01:
                    ratio = delta_kwh / trap_kwh
                    if ratio < 0.5 or ratio > 2.0:
                        continue
                session["energy_kwh"] = round(delta_kwh, 3)
                duration_h = session["duration_seconds"] / 3600.0
                if duration_h > 0:
                    session["avg_power_kw"] = round(delta_kwh / duration_h, 3)

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


METER_APARTMENTS = ("2", "3")
METER_PREPAID_KWH = 250.0


def _normalize_apartment(value: Any) -> str:
    label = str(value or "").strip()
    if label not in METER_APARTMENTS:
        raise ValueError(f"Unknown apartment: {value!r}")
    return label


def save_meter_reading(
    config: AppConfig,
    *,
    apartment: str,
    reading_at: datetime,
    reading_kwh: float,
    is_settlement: bool = False,
    note: str | None = None,
) -> dict[str, Any]:
    apt = _normalize_apartment(apartment)
    if reading_kwh is None or float(reading_kwh) < 0:
        raise ValueError("reading_kwh must be non-negative")
    if reading_at.tzinfo is None:
        reading_at = reading_at.replace(tzinfo=_get_timezone(config))
    reading_date = reading_at.astimezone(_get_timezone(config)).date()
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO meter_readings (apartment, reading_at, reading_date, reading_kwh, is_settlement, note)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (apartment, reading_at) DO UPDATE SET
                    reading_kwh = EXCLUDED.reading_kwh,
                    reading_date = EXCLUDED.reading_date,
                    is_settlement = EXCLUDED.is_settlement,
                    note = EXCLUDED.note
                RETURNING id, apartment, reading_at, reading_date, reading_kwh, is_settlement, note, created_at
                """,
                (apt, reading_at, reading_date, float(reading_kwh), bool(is_settlement), (note or None)),
            )
            return cursor.fetchone()


def delete_meter_reading(config: AppConfig, *, reading_id: int) -> None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM meter_readings WHERE id = %s", (reading_id,))


def list_meter_readings(config: AppConfig, *, limit: int = 50) -> list[dict[str, Any]]:
    tz = _get_timezone(config)
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, apartment, reading_at, reading_kwh, is_settlement, note, created_at
                FROM meter_readings
                ORDER BY reading_at DESC, apartment ASC, id DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = list(cursor.fetchall())
    for row in rows:
        if row.get("reading_at"):
            row["reading_at"] = row["reading_at"].astimezone(tz)
    return rows


def _coerce_reading_kwh(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_meter_status(config: AppConfig) -> dict[str, Any]:
    """Compute current underpayment for the paired apartments.

    For each apartment: take the most recent settlement reading and the
    most recent reading overall. Consumption since settlement =
    latest - settlement. Combined underpayment = sum_consumption -
    METER_PREPAID_KWH (clamped to zero)."""
    tz = _get_timezone(config)
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT apartment, reading_at, reading_kwh, is_settlement
                FROM meter_readings
                ORDER BY apartment ASC, reading_at DESC, id DESC
                """
            )
            rows = list(cursor.fetchall())
    for row in rows:
        if row.get("reading_at"):
            row["reading_at"] = row["reading_at"].astimezone(tz)

    by_apt: dict[str, list[dict[str, Any]]] = {apt: [] for apt in METER_APARTMENTS}
    for row in rows:
        apt = str(row.get("apartment") or "").strip()
        if apt in by_apt:
            by_apt[apt].append(row)

    apartments: list[dict[str, Any]] = []
    total_consumption = 0.0
    have_any_consumption = False
    for apt in METER_APARTMENTS:
        apt_rows = by_apt[apt]
        latest = apt_rows[0] if apt_rows else None
        settlement = next((r for r in apt_rows if r.get("is_settlement")), None)
        latest_kwh = _coerce_reading_kwh(latest["reading_kwh"]) if latest else None
        settlement_kwh = _coerce_reading_kwh(settlement["reading_kwh"]) if settlement else None
        consumption_kwh: float | None = None
        if latest_kwh is not None and settlement_kwh is not None and latest["reading_at"] >= settlement["reading_at"]:
            consumption_kwh = max(latest_kwh - settlement_kwh, 0.0)
            total_consumption += consumption_kwh
            have_any_consumption = True
        apartments.append(
            {
                "apartment": apt,
                "latest": {
                    "reading_at": latest["reading_at"].isoformat() if latest else None,
                    "reading_kwh": latest_kwh,
                } if latest else None,
                "settlement": {
                    "reading_at": settlement["reading_at"].isoformat() if settlement else None,
                    "reading_kwh": settlement_kwh,
                } if settlement else None,
                "consumption_kwh": round(consumption_kwh, 3) if consumption_kwh is not None else None,
            }
        )

    underpayment_kwh = None
    underpayment_cost = None
    if have_any_consumption:
        underpayment_kwh = round(max(total_consumption - METER_PREPAID_KWH, 0.0), 3)
        underpayment_cost = round(underpayment_kwh * float(config.tariff_per_kwh or 0.0), 2)

    return {
        "apartments": apartments,
        "prepaid_kwh": METER_PREPAID_KWH,
        "tariff_per_kwh": float(config.tariff_per_kwh or 0.0),
        "total_consumption_kwh": round(total_consumption, 3) if have_any_consumption else None,
        "underpayment_kwh": underpayment_kwh,
        "underpayment_cost": underpayment_cost,
    }


def _device_energy_kwh_for_range(
    config: AppConfig,
    device_id: str,
    start_dt: datetime,
    end_dt: datetime,
) -> tuple[float, int]:
    """Sum kWh consumed by one device between start_dt (inclusive) and
    end_dt (exclusive). Returns (kwh, hours_with_data).

    For counter-type devices (`power_type='total'`) the value is the
    counter delta between the last sample at-or-before `start_dt` and
    the last sample at-or-before `end_dt`. Offline gaps don't drop the
    counter, so this catches consumption even when the device wasn't
    online to be polled. For current-type devices we still sum hourly
    bucket energy — those buckets miss energy during offline gaps."""
    capabilities = get_device_capabilities(config, device_id)
    energy_counter_meta = _get_energy_counter_meta(config, device_id, capabilities)

    if energy_counter_meta is not None:
        dp_key, scale_divisor = energy_counter_meta
        with _connect(config.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT raw_dps FROM samples
                    WHERE device_id = %s AND captured_at <= %s
                    ORDER BY captured_at DESC LIMIT 1
                    """,
                    (device_id, start_dt),
                )
                start_row = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT raw_dps FROM samples
                    WHERE device_id = %s AND captured_at < %s
                    ORDER BY captured_at DESC LIMIT 1
                    """,
                    (device_id, end_dt),
                )
                end_row = cursor.fetchone()
                # Coverage in hours: how many of the period's hours actually
                # had a sample reach the DB?
                cursor.execute(
                    """
                    SELECT count(*) AS n FROM samples_hourly
                    WHERE device_id = %s AND bucket >= %s AND bucket < %s
                    """,
                    (device_id, start_dt, end_dt),
                )
                row = cursor.fetchone()
                hours_with_data = int((row.get("n") if row else 0) or 0)
        if not start_row or not end_row:
            return 0.0, hours_with_data
        start_counter = _read_energy_counter_kwh(
            _normalize_json_field(start_row["raw_dps"]), dp_key, scale_divisor
        )
        end_counter = _read_energy_counter_kwh(
            _normalize_json_field(end_row["raw_dps"]), dp_key, scale_divisor
        )
        if start_counter is None or end_counter is None:
            return 0.0, hours_with_data
        return max(end_counter - start_counter, 0.0), hours_with_data

    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT bucket, avg_power_w, peak_power_w, sample_count, energy_wh,
                       last_power_w, first_raw_dps, last_raw_dps,
                       first_captured_at, last_captured_at
                FROM samples_hourly
                WHERE device_id = %s AND bucket >= %s AND bucket < %s
                ORDER BY bucket ASC
                """,
                (device_id, start_dt, end_dt),
            )
            rows = cursor.fetchall()
    if not rows:
        return 0.0, 0
    kwh = _aggregate_energy_wh(rows, "hour", energy_counter_meta) / 1000.0
    return kwh, len(rows)


def get_meter_discrepancy_periods(config: AppConfig) -> list[dict[str, Any]]:
    """For each pair of consecutive moments where BOTH apartments have a
    meter reading, compute:

      meter_kwh   = (apt2_end - apt2_start) + (apt3_end - apt3_start)
      device_kwh  = Σ energy of every is_energy_meter device for the same range
      delta_kwh   = meter_kwh - device_kwh

    Boundaries are precise timestamps from `meter_readings.reading_at`
    so a manual reading taken at 19:33 is matched to device data at
    exactly that moment, not at midnight."""
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT apartment, reading_at, reading_kwh
                FROM meter_readings
                ORDER BY reading_at ASC, apartment ASC
                """
            )
            reading_rows = list(cursor.fetchall())
            cursor.execute(
                """
                SELECT device_id FROM devices WHERE is_energy_meter
                """
            )
            energy_device_ids = [str(r["device_id"]) for r in cursor.fetchall()]

    by_apt: dict[str, dict[datetime, float]] = {}
    for row in reading_rows:
        apt = str(row["apartment"])
        by_apt.setdefault(apt, {})[row["reading_at"]] = float(row["reading_kwh"])

    if not by_apt:
        return []

    common_timestamps = sorted(set.intersection(*(set(d.keys()) for d in by_apt.values())))
    if len(common_timestamps) < 2:
        return []

    periods: list[dict[str, Any]] = []
    for start_dt, end_dt in zip(common_timestamps, common_timestamps[1:]):
        meter_total = 0.0
        for apt_readings in by_apt.values():
            meter_total += apt_readings[end_dt] - apt_readings[start_dt]
        meter_total = round(meter_total, 3)

        period_hours = max(int(round((end_dt - start_dt).total_seconds() / 3600.0)), 1)
        device_total = 0.0
        device_hours: dict[str, int] = {}
        for device_id in energy_device_ids:
            kwh, hours = _device_energy_kwh_for_range(config, device_id, start_dt, end_dt)
            device_total += kwh
            device_hours[device_id] = hours
        coverage_hours = min(device_hours.values()) if device_hours else 0
        coverage_pct = round(coverage_hours * 100.0 / period_hours, 1) if period_hours else 0.0
        device_total = round(device_total, 3)

        local_tz = _get_timezone(config)
        periods.append(
            {
                "start_at": start_dt.astimezone(local_tz).isoformat(),
                "end_at": end_dt.astimezone(local_tz).isoformat(),
                "meter_kwh": meter_total,
                "device_kwh": device_total,
                "delta_kwh": round(meter_total - device_total, 3),
                "coverage_pct": coverage_pct,
            }
        )
    return periods
