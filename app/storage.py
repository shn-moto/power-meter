import calendar
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
                    total_power_dps_key, total_power_scale, visualized_codes
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s)
                ON CONFLICT(device_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    room = EXCLUDED.room,
                    device_kind = EXCLUDED.device_kind,
                    is_energy_meter = EXCLUDED.is_energy_meter,
                    onboarding_source = EXCLUDED.onboarding_source,
                    updated_at = NOW(),
                    total_power_dps_key = EXCLUDED.total_power_dps_key,
                    total_power_scale = EXCLUDED.total_power_scale,
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
    product_id: str | None,
    product_name: str | None,
    icon: str | None,
    onboarding_source: str,
    local_key: str,
    ip_address: str,
    version: float,
    total_power_dps_key: str | None,
    total_power_scale: float,
    visualized_codes: list[str] | tuple[str, ...],
    capabilities: list[dict[str, Any]],
) -> None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO devices (
                    name, room, device_id, category_code, device_kind,
                    is_energy_meter, product_id, product_name, icon, onboarding_source, updated_at,
                    total_power_dps_key, total_power_scale, visualized_codes
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s)
                ON CONFLICT(device_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    room = CASE
                        WHEN devices.room = '' OR devices.room = 'Без комнаты' THEN EXCLUDED.room
                        ELSE devices.room
                    END,
                    category_code = EXCLUDED.category_code,
                    device_kind = EXCLUDED.device_kind,
                    is_energy_meter = EXCLUDED.is_energy_meter,
                    product_id = EXCLUDED.product_id,
                    product_name = EXCLUDED.product_name,
                    icon = EXCLUDED.icon,
                    onboarding_source = EXCLUDED.onboarding_source,
                    updated_at = NOW(),
                    total_power_dps_key = EXCLUDED.total_power_dps_key,
                    total_power_scale = EXCLUDED.total_power_scale,
                    visualized_codes = EXCLUDED.visualized_codes
                """,
                (
                    name,
                    room,
                    device_id,
                    category_code,
                    device_kind,
                    is_energy_meter,
                    product_id,
                    product_name,
                    icon,
                    onboarding_source,
                    total_power_dps_key,
                    total_power_scale,
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


def save_cloud_artifact(
    config: AppConfig,
    *,
    device_id: str,
    artifact_type: str,
    payload: dict[str, Any],
) -> None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO device_cloud_artifacts (device_id, artifact_type, payload, fetched_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (device_id, artifact_type) DO UPDATE SET
                    payload = EXCLUDED.payload,
                    fetched_at = NOW()
                """,
                (
                    device_id,
                    artifact_type,
                    Jsonb(payload),
                ),
            )
        connection.commit()


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
                SELECT d.name, d.room, d.device_kind, d.is_energy_meter,
                        d.product_name, d.category_code, d.device_id,
                        d.total_power_dps_key, d.visualized_codes,
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
                                    SELECT d.name, d.room, d.device_id, d.device_kind, d.is_energy_meter,
                        d.product_name, d.category_code, d.product_id, d.icon,
                        d.total_power_dps_key, d.visualized_codes,
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
) -> None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE devices
                SET total_power_dps_key = %s,
                    visualized_codes = %s,
                    updated_at = NOW()
                WHERE device_id = %s
                """,
                (
                    total_power_dps_key,
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
        connection.commit()


def get_cloud_artifact(config: AppConfig, device_id: str, artifact_type: str) -> dict[str, Any] | None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT artifact_type, payload, fetched_at
                FROM device_cloud_artifacts
                WHERE device_id = %s AND artifact_type = %s
                """,
                (device_id, artifact_type),
            )
            return cursor.fetchone()


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


def get_control_device(config: AppConfig, device_id: str) -> TuyaDeviceConfig | None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                                    SELECT d.name, d.room, d.device_id,
                        c.local_key, c.ip_address, c.version, c.total_power_dps_key, c.total_power_scale,
                      d.visualized_codes
                FROM devices d
                JOIN device_connections c ON c.device_id = d.device_id
                WHERE d.device_id = %s
                """,
                (device_id,),
            )
            row = cursor.fetchone()

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
                      d.visualized_codes
                FROM devices d
                JOIN device_connections c ON c.device_id = d.device_id
                WHERE c.local_key <> '' AND c.ip_address <> ''
                ORDER BY d.name
                """
            )
            rows = cursor.fetchall()

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


def _read_energy_counter_kwh(raw_dps: dict[str, Any] | None, dp_key: str, scale: int) -> float | None:
    if not raw_dps:
        return None
    raw_value = raw_dps.get(dp_key)
    if raw_value is None:
        return None
    try:
        return float(raw_value) / (10 ** scale)
    except (TypeError, ValueError):
        return None


def _get_energy_counter_meta_from_capabilities(capabilities: list[dict[str, Any]]) -> tuple[str, int] | None:
    for capability in capabilities:
        code = str(capability.get("capability_code") or "")
        if code not in {"total_forward_energy", "add_ele"}:
            continue
        dp_id = capability.get("dp_id")
        if dp_id is None:
            continue
        values_json = capability.get("values_json") or {}
        return str(dp_id), int(values_json.get("scale", 0) or 0)
    return None


def _get_energy_counter_meta(
    config: AppConfig,
    device_id: str,
    capabilities: list[dict[str, Any]] | None = None,
) -> tuple[str, int] | None:
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


def _bucket_counter_energy_kwh(
    row: dict[str, Any],
    energy_counter_meta: tuple[str, int] | None,
) -> float | None:
    if not energy_counter_meta:
        return None

    dp_key, scale = energy_counter_meta
    first_kwh = _read_energy_counter_kwh(_normalize_json_field(row.get("first_raw_dps")), dp_key, scale)
    last_kwh = _read_energy_counter_kwh(_normalize_json_field(row.get("last_raw_dps")), dp_key, scale)
    if first_kwh is None or last_kwh is None or last_kwh < first_kwh:
        return None
    return max(last_kwh - first_kwh, 0.0)


def _estimate_bucket_energy_kwh(
    row: dict[str, Any],
    bucket: str,
    energy_counter_meta: tuple[str, int] | None,
) -> float:
    integrated_kwh = _bucket_energy_wh(row, bucket) / 1000.0
    counter_kwh = _bucket_counter_energy_kwh(row, energy_counter_meta)
    if counter_kwh is None:
        return integrated_kwh

    # Some devices update cumulative energy in sparse jumps, which collapses
    # multi-bucket charts to a single visible bar despite steady power samples.
    if integrated_kwh > 0.0 and counter_kwh < (integrated_kwh * 0.5):
        return integrated_kwh

    return counter_kwh


def _aggregate_energy_wh(
    bucket_rows: list[dict[str, Any]],
    bucket: str,
    energy_counter_meta: tuple[str, int] | None,
) -> float:
    if not bucket_rows:
        return 0.0

    return sum(
        _estimate_bucket_energy_kwh(row, bucket, energy_counter_meta) * 1000.0
        for row in bucket_rows
    )


def _build_chart_series_from_aggregate(
    bucket_rows: list[dict[str, Any]],
    bucket: str,
    energy_counter_meta: tuple[str, int] | None,
) -> list[dict[str, Any]]:
    series: list[dict[str, Any]] = []

    for row in bucket_rows:
        energy_kwh = _estimate_bucket_energy_kwh(row, bucket, energy_counter_meta)

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
    energy_counter_meta: tuple[str, int] | None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    base_series = _build_chart_series_from_aggregate(rows_by_device, bucket, energy_counter_meta)
    use_power_chart = (
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


def _build_energy_counter_meta_by_device(rows: list[dict[str, Any]]) -> dict[str, tuple[str, int]]:
    metadata: dict[str, tuple[str, int]] = {}
    for row in rows:
        device_id = str(row.get("device_id") or "")
        if not device_id or device_id in metadata:
            continue
        dp_id = row.get("dp_id")
        if dp_id is None:
            continue
        values_json = row.get("values_json") or {}
        metadata[device_id] = (str(dp_id), int(values_json.get("scale", 0) or 0))
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
    dict[str, tuple[str, int]],
]:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.name, d.room, d.device_kind, d.is_energy_meter,
                    d.product_name, d.category_code, d.device_id,
                       COALESCE(c.ip_address, '') AS ip_address,
                       (COALESCE(c.ip_address, '') <> '') AS connection_ready
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
                SELECT device_id, capability_code, dp_id, values_json
                FROM device_capabilities
                WHERE device_id = ANY(%s) AND capability_code IN ('total_forward_energy', 'add_ele')
                ORDER BY device_id ASC, dp_id ASC NULLS LAST, capability_code ASC
                """,
                (energy_device_ids,),
            )
            energy_counter_rows = cursor.fetchall()

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
        _build_energy_counter_meta_by_device(energy_counter_rows),
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

    device_rows, latest_by_device, daily_rows_by_device, energy_counter_meta_by_device = _get_dashboard_summary_context(
        config, month_start, now
    )

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
            device_energy_wh = _aggregate_energy_wh(
                bucket_rows,
                "day",
                energy_counter_meta_by_device.get(device_id),
            )
            current_power_w = 0.0
            total_energy_wh += device_energy_wh
            total_power_w += current_power_w
            devices.append(
                {
                    **base_entry,
                    "current_power_kw": round(current_power_w / 1000.0, 3),
                    "month_energy_kwh": round(device_energy_wh / 1000.0, 3),
                }
            )
            continue

        sensor_devices.append(base_entry)

    return {
        "home_name": config.home_name,
        "month_energy_kwh": round(total_energy_wh / 1000.0, 3),
        "current_power_kw": round(total_power_w / 1000.0, 3),
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
                SELECT name, room, device_id, device_kind, is_energy_meter,
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
