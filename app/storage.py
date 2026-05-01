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
                slug, name, room, image_label, device_id, power_dps_key, power_scale, voltage_dps_keys
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(slug) DO NOTHING
            """,
            [
                (
                    device.slug,
                    device.name,
                    device.room,
                    device.image_label,
                    device.device_id,
                    device.power_dps_key,
                    device.power_scale,
                    Jsonb(list(device.voltage_dps_keys)),
                )
                for device in devices
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
            cursor.execute("SELECT slug, name, room, image_label FROM devices ORDER BY name")
            return cursor.fetchall()


def get_device_row(config: AppConfig, slug: str) -> dict[str, Any] | None:
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT slug, name, room, image_label FROM devices WHERE slug = %s",
                (slug,),
            )
            return cursor.fetchone()


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