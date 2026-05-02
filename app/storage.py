import calendar
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from config import AppConfig, TuyaDeviceConfig


RUSSIAN_MONTH_LABELS_SHORT = {
    1: "янв",
    2: "фев",
    3: "мар",
    4: "апр",
    5: "май",
    6: "июн",
    7: "июл",
    8: "авг",
    9: "сен",
    10: "окт",
    11: "ноя",
    12: "дек",
}

RUSSIAN_MONTH_LABELS_FULL = {
    1: "Январь",
    2: "Февраль",
    3: "Март",
    4: "Апрель",
    5: "Май",
    6: "Июнь",
    7: "Июль",
    8: "Август",
    9: "Сентябрь",
    10: "Октябрь",
    11: "Ноябрь",
    12: "Декабрь",
}


@dataclass(slots=True)
class DeviceSample:
    device_id: str
    captured_at: datetime
    power_w: float
    voltage_v: float | None
    raw_dps: dict[str, Any]
    source: str = "live"
    source_event_id: str | None = None


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


def init_db(config: AppConfig) -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS devices (
            slug TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            room TEXT NOT NULL,
            image_label TEXT NOT NULL,
            image_id TEXT,
            device_id TEXT NOT NULL,
            power_dps_key TEXT NOT NULL,
            power_scale DOUBLE PRECISION NOT NULL,
            voltage_dps_keys JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS category_code TEXT",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS image_id TEXT",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS device_kind TEXT NOT NULL DEFAULT 'switch'",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS is_energy_meter BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS product_id TEXT",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS product_name TEXT",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS icon TEXT",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS onboarding_source TEXT NOT NULL DEFAULT 'config'",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_device_id_unique ON devices(device_id)",
        """
        CREATE TABLE IF NOT EXISTS device_connections (
            device_id TEXT PRIMARY KEY REFERENCES devices(device_id) ON DELETE CASCADE,
            local_key TEXT NOT NULL,
            ip_address TEXT NOT NULL,
            version DOUBLE PRECISION NOT NULL DEFAULT 3.5,
            power_dps_key TEXT,
            power_scale DOUBLE PRECISION NOT NULL DEFAULT 1,
            voltage_dps_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS device_capabilities (
            id BIGSERIAL PRIMARY KEY,
            device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
            capability_source TEXT NOT NULL,
            capability_code TEXT NOT NULL,
            capability_name TEXT,
            value_type TEXT,
            dp_id INTEGER,
            values_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS samples (
            id BIGSERIAL PRIMARY KEY,
            device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
            captured_at TIMESTAMPTZ NOT NULL,
            power_w DOUBLE PRECISION NOT NULL,
            voltage_v DOUBLE PRECISION,
            raw_dps JSONB NOT NULL,
            source TEXT NOT NULL DEFAULT 'live',
            source_event_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS device_events (
            id BIGSERIAL PRIMARY KEY,
            device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
            event_at TIMESTAMPTZ NOT NULL,
            event_type TEXT,
            event_code TEXT,
            source_event_id TEXT,
            payload JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS device_cloud_artifacts (
            id BIGSERIAL PRIMARY KEY,
            device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
            artifact_type TEXT NOT NULL,
            payload JSONB NOT NULL,
            fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_device_connections_ip ON device_connections(ip_address)",
        "ALTER TABLE device_connections ADD COLUMN IF NOT EXISTS device_id TEXT",
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'device_connections' AND column_name = 'device_slug'
            ) THEN
                UPDATE device_connections AS dc
                SET device_id = d.device_id
                FROM devices AS d
                WHERE dc.device_id IS NULL AND dc.device_slug = d.slug;

                ALTER TABLE device_connections DROP COLUMN device_slug CASCADE;
            END IF;
        END
        $$
        """,
        "ALTER TABLE device_connections ALTER COLUMN device_id SET NOT NULL",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'device_connections_device_id_fkey'
            ) THEN
                ALTER TABLE device_connections
                ADD CONSTRAINT device_connections_device_id_fkey
                FOREIGN KEY (device_id) REFERENCES devices(device_id) ON DELETE CASCADE;
            END IF;

            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'device_connections_pkey'
            ) THEN
                ALTER TABLE device_connections
                ADD CONSTRAINT device_connections_pkey PRIMARY KEY (device_id);
            END IF;
        END
        $$
        """,
        "ALTER TABLE device_capabilities ADD COLUMN IF NOT EXISTS device_id TEXT",
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'device_capabilities' AND column_name = 'device_slug'
            ) THEN
                UPDATE device_capabilities AS dc
                SET device_id = d.device_id
                FROM devices AS d
                WHERE dc.device_id IS NULL AND dc.device_slug = d.slug;

                ALTER TABLE device_capabilities DROP COLUMN device_slug CASCADE;
            END IF;
        END
        $$
        """,
        "ALTER TABLE device_capabilities ALTER COLUMN device_id SET NOT NULL",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'device_capabilities_device_id_fkey'
            ) THEN
                ALTER TABLE device_capabilities
                ADD CONSTRAINT device_capabilities_device_id_fkey
                FOREIGN KEY (device_id) REFERENCES devices(device_id) ON DELETE CASCADE;
            END IF;
        END
        $$
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_device_capabilities_unique ON device_capabilities(device_id, capability_source, capability_code)",
        "CREATE INDEX IF NOT EXISTS idx_device_capabilities_device ON device_capabilities(device_id)",
        "ALTER TABLE samples ADD COLUMN IF NOT EXISTS device_id TEXT",
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'samples' AND column_name = 'device_slug'
            ) THEN
                UPDATE samples AS s
                SET device_id = d.device_id
                FROM devices AS d
                WHERE s.device_id IS NULL AND s.device_slug = d.slug;

                ALTER TABLE samples DROP COLUMN device_slug CASCADE;
            END IF;
        END
        $$
        """,
        "ALTER TABLE samples ALTER COLUMN device_id SET NOT NULL",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'samples_device_id_fkey'
            ) THEN
                ALTER TABLE samples
                ADD CONSTRAINT samples_device_id_fkey
                FOREIGN KEY (device_id) REFERENCES devices(device_id) ON DELETE CASCADE;
            END IF;
        END
        $$
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_samples_device_time_source ON samples(device_id, captured_at, source)",
        "CREATE INDEX IF NOT EXISTS idx_samples_device_time ON samples(device_id, captured_at)",
        "CREATE INDEX IF NOT EXISTS idx_samples_device_time_desc ON samples(device_id, captured_at DESC)",
        "ALTER TABLE device_events ADD COLUMN IF NOT EXISTS device_id TEXT",
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'device_events' AND column_name = 'device_slug'
            ) THEN
                UPDATE device_events AS de
                SET device_id = d.device_id
                FROM devices AS d
                WHERE de.device_id IS NULL AND de.device_slug = d.slug;

                ALTER TABLE device_events DROP COLUMN device_slug CASCADE;
            END IF;
        END
        $$
        """,
        "ALTER TABLE device_events ALTER COLUMN device_id SET NOT NULL",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'device_events_device_id_fkey'
            ) THEN
                ALTER TABLE device_events
                ADD CONSTRAINT device_events_device_id_fkey
                FOREIGN KEY (device_id) REFERENCES devices(device_id) ON DELETE CASCADE;
            END IF;
        END
        $$
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_device_events_unique ON device_events(device_id, source_event_id)",
        "CREATE INDEX IF NOT EXISTS idx_device_events_device_time ON device_events(device_id, event_at)",
        "ALTER TABLE device_cloud_artifacts ADD COLUMN IF NOT EXISTS device_id TEXT",
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'device_cloud_artifacts' AND column_name = 'device_slug'
            ) THEN
                UPDATE device_cloud_artifacts AS dca
                SET device_id = d.device_id
                FROM devices AS d
                WHERE dca.device_id IS NULL AND dca.device_slug = d.slug;

                ALTER TABLE device_cloud_artifacts DROP COLUMN device_slug CASCADE;
            END IF;
        END
        $$
        """,
        "ALTER TABLE device_cloud_artifacts ALTER COLUMN device_id SET NOT NULL",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'device_cloud_artifacts_device_id_fkey'
            ) THEN
                ALTER TABLE device_cloud_artifacts
                ADD CONSTRAINT device_cloud_artifacts_device_id_fkey
                FOREIGN KEY (device_id) REFERENCES devices(device_id) ON DELETE CASCADE;
            END IF;
        END
        $$
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_device_cloud_artifacts_unique ON device_cloud_artifacts(device_id, artifact_type)",
        "CREATE INDEX IF NOT EXISTS idx_device_cloud_artifacts_type ON device_cloud_artifacts(device_id, artifact_type)",
    ]
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
        connection.commit()


def sync_devices(config: AppConfig, devices: list[TuyaDeviceConfig]) -> None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO devices (
                    slug, name, room, image_label, image_id, device_id, category_code, device_kind,
                    is_energy_meter, product_id, product_name, icon, onboarding_source, updated_at,
                    power_dps_key, power_scale, voltage_dps_keys
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s)
                ON CONFLICT(slug) DO UPDATE SET
                    device_id = EXCLUDED.device_id,
                    image_id = COALESCE(devices.image_id, EXCLUDED.image_id),
                    device_kind = EXCLUDED.device_kind,
                    is_energy_meter = EXCLUDED.is_energy_meter,
                    onboarding_source = EXCLUDED.onboarding_source,
                    updated_at = NOW(),
                    power_dps_key = EXCLUDED.power_dps_key,
                    power_scale = EXCLUDED.power_scale,
                    voltage_dps_keys = EXCLUDED.voltage_dps_keys
                """,
                [
                    (
                        device.slug,
                        device.name,
                        device.room,
                        device.image_label,
                        device.image_id,
                        device.device_id,
                        None,
                        "meter",
                        True,
                        None,
                        None,
                        None,
                        "config",
                        device.power_dps_key,
                        device.power_scale,
                        Jsonb(list(device.voltage_dps_keys)),
                    )
                    for device in devices
                ],
            )
            cursor.executemany(
                """
                INSERT INTO device_connections (
                    device_id, local_key, ip_address, version, power_dps_key, power_scale, voltage_dps_keys, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
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
                    power_dps_key = EXCLUDED.power_dps_key,
                    power_scale = EXCLUDED.power_scale,
                    voltage_dps_keys = EXCLUDED.voltage_dps_keys,
                    updated_at = NOW()
                """,
                [
                    (
                        device.device_id,
                        device.local_key,
                        device.ip_address,
                        device.version,
                        device.power_dps_key,
                        device.power_scale,
                        Jsonb(list(device.voltage_dps_keys)),
                    )
                    for device in devices
                ],
            )
        connection.commit()


def upsert_managed_device(
    config: AppConfig,
    *,
    slug: str,
    name: str,
    room: str,
    image_label: str,
    image_id: str | None,
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
    power_dps_key: str | None,
    power_scale: float,
    voltage_dps_keys: list[str] | tuple[str, ...],
    capabilities: list[dict[str, Any]],
) -> None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO devices (
                    slug, name, room, image_label, image_id, device_id, category_code, device_kind,
                    is_energy_meter, product_id, product_name, icon, onboarding_source, updated_at,
                    power_dps_key, power_scale, voltage_dps_keys
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s)
                ON CONFLICT(slug) DO UPDATE SET
                    room = CASE
                        WHEN devices.room = '' OR devices.room = 'Без комнаты' THEN EXCLUDED.room
                        ELSE devices.room
                    END,
                    image_id = COALESCE(devices.image_id, EXCLUDED.image_id),
                    device_id = EXCLUDED.device_id,
                    category_code = EXCLUDED.category_code,
                    device_kind = EXCLUDED.device_kind,
                    is_energy_meter = EXCLUDED.is_energy_meter,
                    product_id = EXCLUDED.product_id,
                    product_name = EXCLUDED.product_name,
                    icon = EXCLUDED.icon,
                    onboarding_source = EXCLUDED.onboarding_source,
                    updated_at = NOW(),
                    power_dps_key = EXCLUDED.power_dps_key,
                    power_scale = EXCLUDED.power_scale,
                    voltage_dps_keys = EXCLUDED.voltage_dps_keys
                """,
                (
                    slug,
                    name,
                    room,
                    image_label,
                    image_id,
                    device_id,
                    category_code,
                    device_kind,
                    is_energy_meter,
                    product_id,
                    product_name,
                    icon,
                    onboarding_source,
                    power_dps_key,
                    power_scale,
                    Jsonb(list(voltage_dps_keys)),
                ),
            )
            cursor.execute(
                """
                INSERT INTO device_connections (
                    device_id, local_key, ip_address, version, power_dps_key, power_scale, voltage_dps_keys, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
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
                    power_dps_key = EXCLUDED.power_dps_key,
                    power_scale = EXCLUDED.power_scale,
                    voltage_dps_keys = EXCLUDED.voltage_dps_keys,
                    updated_at = NOW()
                """,
                (
                    device_id,
                    local_key,
                    ip_address,
                    version,
                    power_dps_key,
                    power_scale,
                    Jsonb(list(voltage_dps_keys)),
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
            INSERT INTO samples (device_id, captured_at, power_w, voltage_v, raw_dps, source, source_event_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (device_id, captured_at, source) DO UPDATE SET
                power_w = EXCLUDED.power_w,
                voltage_v = EXCLUDED.voltage_v,
                raw_dps = EXCLUDED.raw_dps,
                source_event_id = EXCLUDED.source_event_id
            """,
            (
                sample.device_id,
                sample.captured_at,
                sample.power_w,
                sample.voltage_v,
                Jsonb(sample.raw_dps),
                sample.source,
                sample.source_event_id,
            ),
        )
        connection.commit()


def save_device_event(
    config: AppConfig,
    *,
    device_id: str,
    event_at: datetime,
    event_type: str | None,
    event_code: str | None,
    source_event_id: str | None,
    payload: dict[str, Any],
) -> None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO device_events (device_id, event_at, event_type, event_code, source_event_id, payload)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (device_id, source_event_id) DO UPDATE SET
                    event_at = EXCLUDED.event_at,
                    event_type = EXCLUDED.event_type,
                    event_code = EXCLUDED.event_code,
                    payload = EXCLUDED.payload
                """,
                (
                    device_id,
                    event_at,
                    event_type,
                    event_code,
                    source_event_id,
                    Jsonb(payload),
                ),
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


def _normalize_power_by_measurements(
    power_w: float,
    voltage_v: float | None,
    raw_dps: dict[str, Any] | None,
) -> float:
    payload = _normalize_json_field(raw_dps)
    if not payload:
        return power_w

    current_raw = _coerce_float(payload.get("4"))
    measured_voltage = voltage_v if voltage_v is not None else _normalize_voltage_value(payload.get("6"))
    if current_raw is None or measured_voltage is None or current_raw <= 0 or measured_voltage <= 0:
        return power_w

    current_a = current_raw / 1000.0 if current_raw > 10 else current_raw
    apparent_power_w = current_a * measured_voltage
    if apparent_power_w <= 0:
        return power_w

    if power_w > apparent_power_w * 3 and (power_w / 10.0) <= apparent_power_w * 1.6:
        return power_w / 10.0

    return power_w


def _normalize_sample_power_w(power_w: float, voltage_v: float | None, raw_dps: dict[str, Any] | None = None) -> float:
    power_w = _normalize_power_by_measurements(power_w, voltage_v, raw_dps)
    if power_w > 5000 and voltage_v is not None and 180 <= voltage_v <= 260:
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


def get_device_rows(config: AppConfig) -> list[dict[str, Any]]:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.slug, d.name, d.room, d.image_label, d.device_kind, d.is_energy_meter,
                      d.product_name, d.category_code, d.image_id, d.device_id,
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
                  SELECT slug, name, room, image_label, image_id, device_id, device_kind, is_energy_meter,
                       product_name, category_code, product_id, icon
                FROM devices WHERE device_id = %s
                """,
                (device_id,),
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
                  SELECT d.slug, d.name, d.room, d.image_label, d.image_id, d.device_id,
                       c.local_key, c.ip_address, c.version, c.power_dps_key,
                       c.power_scale, c.voltage_dps_keys
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
        slug=row["slug"],
        name=row["name"],
        room=row["room"],
        image_label=row["image_label"],
        device_id=row["device_id"],
        local_key=row["local_key"],
        ip_address=row["ip_address"],
        version=float(row["version"]),
        power_dps_key=str(row["power_dps_key"] or ""),
        power_scale=float(row["power_scale"] or 1),
        voltage_dps_keys=tuple(str(key) for key in (row["voltage_dps_keys"] or [])),
        image_id=str(row.get("image_id") or "").strip() or None,
    )


def get_device_by_id(config: AppConfig, device_id: str) -> dict[str, Any] | None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT slug, name, room, image_label, image_id, device_id FROM devices WHERE device_id = %s",
                (device_id,),
            )
            return cursor.fetchone()


def get_polling_devices(config: AppConfig) -> list[TuyaDeviceConfig]:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                  SELECT d.slug, d.name, d.room, d.image_label, d.image_id, d.device_id,
                       c.local_key, c.ip_address, c.version, c.power_dps_key,
                       c.power_scale, c.voltage_dps_keys
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
                slug=row["slug"],
                name=row["name"],
                room=row["room"],
                image_label=row["image_label"],
                device_id=row["device_id"],
                local_key=row["local_key"],
                ip_address=row["ip_address"],
                version=float(row["version"]),
                power_dps_key=str(row["power_dps_key"] or ""),
                power_scale=float(row["power_scale"] or 1),
                voltage_dps_keys=tuple(str(key) for key in (row["voltage_dps_keys"] or [])),
                image_id=str(row.get("image_id") or "").strip() or None,
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
            SELECT captured_at, power_w, voltage_v, raw_dps
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
            SELECT captured_at, power_w, voltage_v, raw_dps
            FROM samples
            WHERE device_id = %s
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (device_id,),
        )
            return cursor.fetchone()


def _integrate_energy_wh(rows: list[dict[str, Any]]) -> float:
    if len(rows) < 2:
        return 0.0

    total_wh = 0.0
    for current, following in zip(rows, rows[1:]):
        current_dt = _parse_dt(current["captured_at"])
        next_dt = _parse_dt(following["captured_at"])
        hours = max((next_dt - current_dt).total_seconds(), 0) / 3600.0
        total_wh += _normalize_sample_power_w(float(current["power_w"]), current.get("voltage_v"), current.get("raw_dps")) * hours
    return total_wh


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


def _build_series(rows: list[dict[str, Any]], bucket: str) -> list[dict[str, Any]]:
    if len(rows) < 2:
        return []

    grouped: dict[datetime, dict[str, float]] = defaultdict(lambda: {"energy_wh": 0.0, "power_sum": 0.0, "count": 0})
    for current, following in zip(rows, rows[1:]):
        current_dt = _parse_dt(current["captured_at"])
        next_dt = _parse_dt(following["captured_at"])
        hours = max((next_dt - current_dt).total_seconds(), 0) / 3600.0
        group = grouped[_bucket_start(current_dt, bucket)]
        power_w = _normalize_sample_power_w(float(current["power_w"]), current.get("voltage_v"), current.get("raw_dps"))
        group["energy_wh"] += power_w * hours
        group["power_sum"] += power_w
        group["count"] += 1

    return [
        {
            "timestamp": bucket_start.isoformat(),
            "energy_kwh": round(values["energy_wh"] / 1000.0, 4),
            "avg_power_kw": round((values["power_sum"] / max(values["count"], 1)) / 1000.0, 4),
        }
        for bucket_start, values in sorted(grouped.items())
    ]


def _read_energy_counter_kwh(raw_dps: dict[str, Any], dp_key: str, scale: int) -> float | None:
    raw_value = raw_dps.get(dp_key)
    if raw_value is None:
        return None

    try:
        return float(raw_value) / (10 ** scale)
    except (TypeError, ValueError):
        return None


def _integrate_energy_counter_kwh(rows: list[dict[str, Any]], dp_key: str, scale: int) -> float | None:
    if len(rows) < 2:
        return None

    total_kwh = 0.0
    has_counter_pairs = False
    for current, following in zip(rows, rows[1:]):
        current_raw_dps = _normalize_json_field(current["raw_dps"])
        following_raw_dps = _normalize_json_field(following["raw_dps"])
        current_kwh = _read_energy_counter_kwh(current_raw_dps, dp_key, scale)
        following_kwh = _read_energy_counter_kwh(following_raw_dps, dp_key, scale)
        if current_kwh is None or following_kwh is None:
            continue

        has_counter_pairs = True
        total_kwh += max(following_kwh - current_kwh, 0.0)

    if not has_counter_pairs:
        return None
    return total_kwh


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


def _build_series_from_energy_counter(
    rows: list[dict[str, Any]],
    bucket: str,
    dp_key: str,
    scale: int,
) -> list[dict[str, Any]]:
    if len(rows) < 2:
        return []

    grouped: dict[datetime, dict[str, float]] = defaultdict(lambda: {"energy_kwh": 0.0})
    bucket_hours = _bucket_duration_hours(bucket)

    for current, following in zip(rows, rows[1:]):
        current_dt = _parse_dt(current["captured_at"])
        group = grouped[_bucket_start(current_dt, bucket)]

        delta_kwh = _integrate_energy_counter_kwh([current, following], dp_key, scale)
        if delta_kwh is None:
            continue
        group["energy_kwh"] += delta_kwh

    return [
        {
            "timestamp": bucket_start.isoformat(),
            "energy_kwh": round(values["energy_kwh"], 4),
            "avg_power_kw": round(values["energy_kwh"] / bucket_hours, 4),
        }
        for bucket_start, values in sorted(grouped.items())
    ]


def _calculate_energy_wh(
    config: AppConfig,
    device_id: str,
    rows: list[dict[str, Any]],
    capabilities: list[dict[str, Any]] | None = None,
    energy_counter_meta: tuple[str, int] | None = None,
) -> float:
    integrated_wh = _integrate_energy_wh(rows)
    energy_counter_meta = energy_counter_meta or _get_energy_counter_meta(config, device_id, capabilities)
    if not energy_counter_meta:
        return integrated_wh

    counter_kwh = _integrate_energy_counter_kwh(rows, *energy_counter_meta)
    if counter_kwh is None:
        return integrated_wh
    return counter_kwh * 1000.0


def _row_from_live_sample(sample: DeviceSample) -> dict[str, Any]:
    return {
        "captured_at": sample.captured_at,
        "power_w": sample.power_w,
        "voltage_v": sample.voltage_v,
        "raw_dps": sample.raw_dps,
    }


def _merge_live_sample(rows: list[dict[str, Any]], live_sample: DeviceSample | None) -> list[dict[str, Any]]:
    if not live_sample:
        return rows

    if not rows:
        return [_row_from_live_sample(live_sample)]

    latest_dt = _parse_dt(rows[-1]["captured_at"])
    if live_sample.captured_at <= latest_dt:
        return rows

    return [*rows, _row_from_live_sample(live_sample)]


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


def _group_rows_by_device(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
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
                SELECT d.slug, d.name, d.room, d.image_label, d.device_kind, d.is_energy_meter,
                      d.product_name, d.category_code, d.image_id, d.device_id,
                       COALESCE(c.ip_address, '') AS ip_address,
                       (COALESCE(c.ip_address, '') <> '') AS connection_ready
                FROM devices d
                LEFT JOIN device_connections c ON c.device_id = d.device_id
                ORDER BY name
                """
            )
            device_rows = [row for row in cursor.fetchall() if row.get("is_energy_meter")]
            device_ids = [str(row.get("device_id") or "") for row in device_rows if row.get("device_id")]
            if not device_ids:
                return device_rows, {}, {}, {}

            cursor.execute(
                """
                SELECT device_id, capability_code, dp_id, values_json
                FROM device_capabilities
                WHERE device_id = ANY(%s) AND capability_code IN ('total_forward_energy', 'add_ele')
                ORDER BY device_id ASC, dp_id ASC NULLS LAST, capability_code ASC
                """,
                (device_ids,),
            )
            energy_counter_rows = cursor.fetchall()

            cursor.execute(
                """
                SELECT DISTINCT ON (device_id) device_id, captured_at, power_w, voltage_v, raw_dps
                FROM samples
                WHERE device_id = ANY(%s)
                ORDER BY device_id ASC, captured_at DESC
                """,
                (device_ids,),
            )
            latest_rows = cursor.fetchall()

            cursor.execute(
                """
                SELECT device_id, captured_at, power_w, voltage_v, raw_dps
                FROM samples
                WHERE device_id = ANY(%s) AND captured_at >= %s AND captured_at <= %s
                ORDER BY device_id ASC, captured_at ASC
                """,
                (device_ids, month_start, now),
            )
            month_rows = cursor.fetchall()

    return (
        device_rows,
        {str(row.get("device_id") or ""): row for row in latest_rows if row.get("device_id")},
        _group_rows_by_device(month_rows),
        _build_energy_counter_meta_by_device(energy_counter_rows),
    )


def _prepare_chart_series(
    config: AppConfig,
    device_id: str,
    rows: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    period: str,
    bucket: str,
    series: list[dict[str, Any]],
    energy_counter_meta: tuple[str, int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    chart_metric = "energy_kwh"
    chart = {
        "metric": chart_metric,
        "unit": "кВт·ч",
        "label": "Потребление",
        "bucket": bucket,
        "period": period,
    }

    max_chart_value = max((float(item.get(chart_metric) or 0.0) for item in series), default=0.0)
    energy_counter_meta = energy_counter_meta or _get_energy_counter_meta(config, device_id)
    if energy_counter_meta:
        fallback_series = _build_series_from_energy_counter(rows, bucket, *energy_counter_meta)
        fallback_max = max((float(item.get("energy_kwh") or 0.0) for item in fallback_series), default=0.0)
        if fallback_series and fallback_max > 0.0:
            series = fallback_series

    chart_series_by_bucket = {
        _parse_dt(item["timestamp"]): {
            **item,
            "chart_value": round(float(item.get(chart_metric) or 0.0), 4),
        }
        for item in series
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


def get_dashboard_summary(
    config: AppConfig,
    month_start: datetime,
    now: datetime,
    live_samples: dict[str, DeviceSample] | None = None,
) -> dict[str, Any]:
    devices = []
    total_energy_wh = 0.0
    total_power_w = 0.0
    online_device_count = 0
    live_samples = live_samples or {}

    device_rows, latest_by_device, month_rows_by_device, energy_counter_meta_by_device = _get_dashboard_summary_context(
        config,
        month_start,
        now,
    )

    for device in device_rows:

        device_id = str(device.get("device_id") or "")
        live_sample = live_samples.get(device_id)
        latest = latest_by_device.get(device_id)
        samples = _merge_live_sample(month_rows_by_device.get(device_id, []), live_sample)
        device_energy_wh = _calculate_energy_wh(
            config,
            device_id,
            samples,
            energy_counter_meta=energy_counter_meta_by_device.get(device_id),
        )
        total_energy_wh += device_energy_wh

        if live_sample:
            current_power_w = _normalize_sample_power_w(float(live_sample.power_w), live_sample.voltage_v, live_sample.raw_dps)
            last_seen = _format_display_datetime(config, live_sample.captured_at)
            raw_dps = live_sample.raw_dps
            effective_captured_at = live_sample.captured_at
        else:
            current_power_w = _normalize_sample_power_w(float(latest["power_w"]), latest.get("voltage_v"), latest.get("raw_dps")) if latest else 0.0
            last_seen = _format_display_datetime(config, latest["captured_at"]) if latest else None
            raw_dps = _normalize_json_field(latest["raw_dps"]) if latest else {}
            effective_captured_at = _parse_dt(latest["captured_at"]) if latest else None

        last_seen_status = get_sample_status(effective_captured_at, now)
        if last_seen_status == "ok":
            online_device_count += 1

        total_power_w += current_power_w
        last_seen_age_seconds = get_sample_age_seconds(effective_captured_at, now)
        devices.append(
            {
                "slug": device["slug"],
                "name": device["name"],
                "room": device["room"],
                "image_label": device["image_label"],
                "image_id": device.get("image_id"),
                "device_id": device.get("device_id"),
                "current_power_kw": round(current_power_w / 1000.0, 3),
                "month_energy_kwh": round(device_energy_wh / 1000.0, 3),
                "last_seen": last_seen,
                "last_seen_age_seconds": last_seen_age_seconds,
                "last_seen_status": last_seen_status,
                "raw_dps": raw_dps,
            }
        )

    return {
        "home_name": config.home_name,
        "month_energy_kwh": round(total_energy_wh / 1000.0, 3),
        "current_power_kw": round(total_power_w / 1000.0, 3),
        "estimated_cost": round((total_energy_wh / 1000.0) * config.tariff_per_kwh, 2),
        "device_count": online_device_count,
        "devices": devices,
    }


def get_device_stats(
    config: AppConfig,
    device_id: str,
    start: datetime,
    end: datetime,
    period: str,
    bucket: str,
) -> dict[str, Any]:
    rows = get_samples(config, device_id, start, end)
    latest = get_latest_sample(config, device_id)
    return _build_device_stats_result(config, device_id, rows, latest, start, end, period, bucket)


def _build_device_stats_result(
    config: AppConfig,
    device_id: str,
    rows: list[dict[str, Any]],
    latest: dict[str, Any] | None,
    start: datetime,
    end: datetime,
    period: str,
    bucket: str,
    capabilities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    series = _build_series(rows, bucket)
    energy_counter_meta = _get_energy_counter_meta(config, device_id, capabilities)
    chart_series, chart = _prepare_chart_series(config, device_id, rows, start, end, period, bucket, series, energy_counter_meta)
    total_energy_wh = _calculate_energy_wh(config, device_id, rows, capabilities, energy_counter_meta)
    normalized_powers = [
        _normalize_sample_power_w(float(row["power_w"]), row.get("voltage_v"), row.get("raw_dps"))
        for row in rows
    ]
    average_power_w = sum(normalized_powers) / max(len(normalized_powers), 1) if normalized_powers else 0.0
    peak_power_w = max(normalized_powers, default=0.0)
    voltages = [float(row["voltage_v"]) for row in rows if row["voltage_v"] is not None]
    latest_captured_at = _parse_dt(latest["captured_at"]) if latest else None
    latest_power_w = _normalize_sample_power_w(float(latest["power_w"]), latest.get("voltage_v"), latest.get("raw_dps")) if latest else None
    latest_voltage_v = float(latest["voltage_v"]) if latest and latest["voltage_v"] is not None else None

    return {
        "summary": {
            "energy_kwh": round(total_energy_wh / 1000.0, 3),
            "average_power_kw": round(average_power_w / 1000.0, 3),
            "peak_power_kw": round(peak_power_w / 1000.0, 3),
            "latest_power_w": round(latest_power_w, 1) if latest_power_w is not None else None,
            "latest_voltage_v": round(latest_voltage_v, 1) if latest_voltage_v is not None else None,
            "average_voltage_v": round(sum(voltages) / len(voltages), 1) if voltages else None,
            "sample_count": len(rows),
            "latest_sample": _format_display_datetime(config, latest["captured_at"]) if latest else None,
            "latest_sample_age_seconds": get_sample_age_seconds(latest_captured_at, end),
            "latest_sample_status": get_sample_status(latest_captured_at, end),
            "latest_raw_dps": _normalize_json_field(latest["raw_dps"]) if latest else {},
        },
        "series": chart_series,
        "chart": chart,
    }


def get_device_context_and_stats(
    config: AppConfig,
    device_id: str,
    start: datetime,
    end: datetime,
    period: str,
    bucket: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any] | None]:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT slug, name, room, image_label, image_id, device_id, device_kind, is_energy_meter,
                       product_name, category_code, product_id, icon
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
                """
                SELECT captured_at, power_w, voltage_v, raw_dps
                FROM samples
                WHERE device_id = %s AND captured_at >= %s AND captured_at <= %s
                ORDER BY captured_at ASC
                """,
                (device_id, start, end),
            )
            rows = cursor.fetchall()

            cursor.execute(
                """
                SELECT captured_at, power_w, voltage_v, raw_dps
                FROM samples
                WHERE device_id = %s
                ORDER BY captured_at DESC
                LIMIT 1
                """,
                (device_id,),
            )
            latest = cursor.fetchone()

    stats = _build_device_stats_result(config, device_id, rows, latest, start, end, period, bucket, capabilities)
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