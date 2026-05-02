import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from config import AppConfig, TuyaDeviceConfig


@dataclass(slots=True)
class DeviceSample:
    device_slug: str
    captured_at: datetime
    power_w: float
    voltage_v: float | None
    raw_dps: dict[str, Any]
    source: str = "live"
    source_event_id: str | None = None


def _connect(database_url: str) -> psycopg.Connection:
    return psycopg.connect(database_url, row_factory=dict_row)


def init_db(config: AppConfig) -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS devices (
            slug TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            room TEXT NOT NULL,
            image_label TEXT NOT NULL,
            device_id TEXT NOT NULL,
            power_dps_key TEXT NOT NULL,
            power_scale DOUBLE PRECISION NOT NULL,
            voltage_dps_keys JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS category_code TEXT",
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
            device_slug TEXT PRIMARY KEY REFERENCES devices(slug) ON DELETE CASCADE,
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
            device_slug TEXT NOT NULL REFERENCES devices(slug) ON DELETE CASCADE,
            capability_source TEXT NOT NULL,
            capability_code TEXT NOT NULL,
            capability_name TEXT,
            value_type TEXT,
            dp_id INTEGER,
            values_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (device_slug, capability_source, capability_code)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS samples (
            id BIGSERIAL PRIMARY KEY,
            device_slug TEXT NOT NULL REFERENCES devices(slug) ON DELETE CASCADE,
            captured_at TIMESTAMPTZ NOT NULL,
            power_w DOUBLE PRECISION NOT NULL,
            voltage_v DOUBLE PRECISION,
            raw_dps JSONB NOT NULL,
            source TEXT NOT NULL DEFAULT 'live',
            source_event_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (device_slug, captured_at, source)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS device_events (
            id BIGSERIAL PRIMARY KEY,
            device_slug TEXT NOT NULL REFERENCES devices(slug) ON DELETE CASCADE,
            event_at TIMESTAMPTZ NOT NULL,
            event_type TEXT,
            event_code TEXT,
            source_event_id TEXT,
            payload JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (device_slug, source_event_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS device_cloud_artifacts (
            id BIGSERIAL PRIMARY KEY,
            device_slug TEXT NOT NULL REFERENCES devices(slug) ON DELETE CASCADE,
            artifact_type TEXT NOT NULL,
            payload JSONB NOT NULL,
            fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (device_slug, artifact_type)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_device_connections_ip ON device_connections(ip_address)",
        "CREATE INDEX IF NOT EXISTS idx_device_capabilities_device ON device_capabilities(device_slug)",
        "CREATE INDEX IF NOT EXISTS idx_samples_device_time ON samples(device_slug, captured_at)",
        "CREATE INDEX IF NOT EXISTS idx_device_events_device_time ON device_events(device_slug, event_at)",
        "CREATE INDEX IF NOT EXISTS idx_device_cloud_artifacts_type ON device_cloud_artifacts(device_slug, artifact_type)",
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
                    slug, name, room, image_label, device_id, category_code, device_kind,
                    is_energy_meter, product_id, product_name, icon, onboarding_source, updated_at,
                    power_dps_key, power_scale, voltage_dps_keys
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s)
                ON CONFLICT(slug) DO NOTHING
                """,
                [
                    (
                        device.slug,
                        device.name,
                        device.room,
                        device.image_label,
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
                    device_slug, local_key, ip_address, version, power_dps_key, power_scale, voltage_dps_keys, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT(device_slug) DO NOTHING
                """,
                [
                    (
                        device.slug,
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
                    slug, name, room, image_label, device_id, category_code, device_kind,
                    is_energy_meter, product_id, product_name, icon, onboarding_source, updated_at,
                    power_dps_key, power_scale, voltage_dps_keys
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s)
                ON CONFLICT(slug) DO UPDATE SET
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
                    device_slug, local_key, ip_address, version, power_dps_key, power_scale, voltage_dps_keys, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT(device_slug) DO UPDATE SET
                    local_key = EXCLUDED.local_key,
                    ip_address = EXCLUDED.ip_address,
                    version = EXCLUDED.version,
                    power_dps_key = EXCLUDED.power_dps_key,
                    power_scale = EXCLUDED.power_scale,
                    voltage_dps_keys = EXCLUDED.voltage_dps_keys,
                    updated_at = NOW()
                """,
                (
                    slug,
                    local_key,
                    ip_address,
                    version,
                    power_dps_key,
                    power_scale,
                    Jsonb(list(voltage_dps_keys)),
                ),
            )
            cursor.execute("DELETE FROM device_capabilities WHERE device_slug = %s", (slug,))
            if capabilities:
                cursor.executemany(
                    """
                    INSERT INTO device_capabilities (
                        device_slug, capability_source, capability_code, capability_name,
                        value_type, dp_id, values_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            slug,
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
            INSERT INTO samples (device_slug, captured_at, power_w, voltage_v, raw_dps, source, source_event_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (device_slug, captured_at, source) DO UPDATE SET
                power_w = EXCLUDED.power_w,
                voltage_v = EXCLUDED.voltage_v,
                raw_dps = EXCLUDED.raw_dps,
                source_event_id = EXCLUDED.source_event_id
            """,
            (
                sample.device_slug,
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
    device_slug: str,
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
                INSERT INTO device_events (device_slug, event_at, event_type, event_code, source_event_id, payload)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (device_slug, source_event_id) DO UPDATE SET
                    event_at = EXCLUDED.event_at,
                    event_type = EXCLUDED.event_type,
                    event_code = EXCLUDED.event_code,
                    payload = EXCLUDED.payload
                """,
                (
                    device_slug,
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
    device_slug: str,
    artifact_type: str,
    payload: dict[str, Any],
) -> None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO device_cloud_artifacts (device_slug, artifact_type, payload, fetched_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (device_slug, artifact_type) DO UPDATE SET
                    payload = EXCLUDED.payload,
                    fetched_at = NOW()
                """,
                (
                    device_slug,
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
                       d.product_name, d.category_code,
                       COALESCE(c.ip_address, '') AS ip_address,
                       (COALESCE(c.ip_address, '') <> '') AS connection_ready
                FROM devices d
                LEFT JOIN device_connections c ON c.device_slug = d.slug
                ORDER BY name
                """
            )
            return cursor.fetchall()


def get_device_row(config: AppConfig, slug: str) -> dict[str, Any] | None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT slug, name, room, image_label, device_kind, is_energy_meter,
                       product_name, category_code, product_id, icon
                FROM devices WHERE slug = %s
                """,
                (slug,),
            )
            return cursor.fetchone()


def get_device_by_id(config: AppConfig, device_id: str) -> dict[str, Any] | None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT slug, name, room, image_label, device_id FROM devices WHERE device_id = %s",
                (device_id,),
            )
            return cursor.fetchone()


def get_polling_devices(config: AppConfig) -> list[TuyaDeviceConfig]:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.slug, d.name, d.room, d.image_label, d.device_id,
                       c.local_key, c.ip_address, c.version, c.power_dps_key,
                       c.power_scale, c.voltage_dps_keys
                FROM devices d
                JOIN device_connections c ON c.device_slug = d.slug
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


def get_samples(config: AppConfig, slug: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
            """
            SELECT captured_at, power_w, voltage_v, raw_dps
            FROM samples
            WHERE device_slug = %s AND captured_at >= %s AND captured_at <= %s
            ORDER BY captured_at ASC
            """,
            (slug, start, end),
        )
            return cursor.fetchall()


def get_latest_sample(config: AppConfig, slug: str) -> dict[str, Any] | None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
            """
            SELECT captured_at, power_w, voltage_v, raw_dps
            FROM samples
            WHERE device_slug = %s
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (slug,),
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
        total_wh += float(current["power_w"]) * hours
    return total_wh


def _bucket_start(dt: datetime, bucket: str) -> datetime:
    if bucket == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    if bucket == "day":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


def _build_series(rows: list[dict[str, Any]], bucket: str) -> list[dict[str, Any]]:
    if len(rows) < 2:
        return []

    grouped: dict[datetime, dict[str, float]] = defaultdict(lambda: {"energy_wh": 0.0, "power_sum": 0.0, "count": 0})
    for current, following in zip(rows, rows[1:]):
        current_dt = _parse_dt(current["captured_at"])
        next_dt = _parse_dt(following["captured_at"])
        hours = max((next_dt - current_dt).total_seconds(), 0) / 3600.0
        group = grouped[_bucket_start(current_dt, bucket)]
        power_w = float(current["power_w"])
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


def get_dashboard_summary(config: AppConfig, month_start: datetime, now: datetime) -> dict[str, Any]:
    devices = []
    total_energy_wh = 0.0
    total_power_w = 0.0

    for device in get_device_rows(config):
        if not device.get("is_energy_meter"):
            continue
        latest = get_latest_sample(config, device["slug"])
        samples = get_samples(config, device["slug"], month_start, now)
        device_energy_wh = _integrate_energy_wh(samples)
        total_energy_wh += device_energy_wh
        current_power_w = float(latest["power_w"]) if latest else 0.0
        total_power_w += current_power_w
        raw_dps = _normalize_json_field(latest["raw_dps"]) if latest else {}
        devices.append(
            {
                "slug": device["slug"],
                "name": device["name"],
                "room": device["room"],
                "image_label": device["image_label"],
                "current_power_kw": round(current_power_w / 1000.0, 3),
                "month_energy_kwh": round(device_energy_wh / 1000.0, 3),
                "last_seen": _format_display_datetime(config, latest["captured_at"]) if latest else None,
                "raw_dps": raw_dps,
            }
        )

    return {
        "home_name": config.home_name,
        "month_energy_kwh": round(total_energy_wh / 1000.0, 3),
        "current_power_kw": round(total_power_w / 1000.0, 3),
        "estimated_cost": round((total_energy_wh / 1000.0) * config.tariff_per_kwh, 2),
        "device_count": len(devices),
        "devices": devices,
    }


def get_device_stats(
    config: AppConfig,
    slug: str,
    start: datetime,
    end: datetime,
    bucket: str,
) -> dict[str, Any]:
    rows = get_samples(config, slug, start, end)
    latest = get_latest_sample(config, slug)
    total_energy_wh = _integrate_energy_wh(rows)
    average_power_w = sum(float(row["power_w"]) for row in rows) / max(len(rows), 1) if rows else 0.0
    peak_power_w = max((float(row["power_w"]) for row in rows), default=0.0)
    voltages = [float(row["voltage_v"]) for row in rows if row["voltage_v"] is not None]

    return {
        "summary": {
            "energy_kwh": round(total_energy_wh / 1000.0, 3),
            "average_power_kw": round(average_power_w / 1000.0, 3),
            "peak_power_kw": round(peak_power_w / 1000.0, 3),
            "average_voltage_v": round(sum(voltages) / len(voltages), 1) if voltages else None,
            "sample_count": len(rows),
            "latest_sample": _format_display_datetime(config, latest["captured_at"]) if latest else None,
            "latest_raw_dps": _normalize_json_field(latest["raw_dps"]) if latest else {},
        },
        "series": _build_series(rows, bucket),
    }


def pick_bucket(start: datetime, end: datetime) -> str:
    span = end - start
    if span <= timedelta(days=2):
        return "15m"
    if span <= timedelta(days=31):
        return "hour"
    return "day"