"""Reconcile per-day device energy against Tuya Cloud's own event history.

Tuya sockets buffer their `add_ele` counter internally and push it as
event-type-7 log rows to the cloud every ~10 minutes (whether the plug's
Wi-Fi is up at the time or on the next reconnect). Summing those event
values within a local day is the plug's authoritative view of daily kWh —
matches the number the Smart Life app shows and captures energy across
Wi-Fi outages that our LAN polling can't see.

Retention on Tuya's free tier is ~30 days, so this must run at least
weekly to stay ahead of retention expiry. Verified daily totals are
cached in `device_energy_daily` and consulted by
`_device_energy_kwh_for_range` when a discrepancy period covers whole
days.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import tinytuya

from config import AppConfig, TuyaCloudConfig

logger = logging.getLogger(__name__)


@dataclass
class VerificationSummary:
    device_id: str
    days_covered: int
    total_kwh: float
    latest_day: date | None
    error: str | None = None


def _cloud_client(cloud_cfg: TuyaCloudConfig, device_id: str) -> tinytuya.Cloud:
    return tinytuya.Cloud(
        apiRegion=cloud_cfg.region,
        apiKey=cloud_cfg.api_key,
        apiSecret=cloud_cfg.api_secret,
        apiDeviceID=cloud_cfg.api_device_id or device_id,
    )


def _cloud_events_add_ele(
    cloud: tinytuya.Cloud,
    device_id: str,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, Any]]:
    """Fetch all event-type-7 log rows with code=add_ele between the two
    epoch-ms timestamps. tinytuya handles pagination internally."""
    resp = cloud.getdevicelog(
        device_id,
        start=start_ms,
        end=end_ms,
        evtype="7",
        size=0,
        max_fetches=200,
    )
    if not isinstance(resp, dict) or not resp.get("success", True):
        code = resp.get("code") if isinstance(resp, dict) else None
        msg = resp.get("msg") if isinstance(resp, dict) else str(resp)
        raise RuntimeError(f"cloud getdevicelog failed: code={code} msg={msg}")
    logs = (resp.get("result") or {}).get("logs") or []
    return [ev for ev in logs if ev.get("code") == "add_ele"]


def _sum_by_local_day(
    events: list[dict[str, Any]],
    scale_divisor: float,
    tz: ZoneInfo,
) -> dict[date, float]:
    """Sum incremental add_ele values into kWh per local day. Each event's
    `value` is the raw incremental counter reading — divide by the DPS
    scale divisor to get kWh."""
    per_day: dict[date, float] = defaultdict(float)
    for ev in events:
        raw_ts = ev.get("event_time")
        raw_val = ev.get("value")
        if raw_ts is None or raw_val is None:
            continue
        try:
            ts_ms = int(raw_ts)
            increment_raw = float(raw_val)
        except (TypeError, ValueError):
            continue
        local_dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).astimezone(tz)
        per_day[local_dt.date()] += increment_raw / max(scale_divisor, 1.0)
    return dict(per_day)


def _energy_meter_devices_with_add_ele(config: AppConfig) -> list[tuple[str, float]]:
    """Return [(device_id, add_ele_scale_divisor), ...] for devices we should
    verify against cloud — is_energy_meter, not a generator, not disabled,
    and having an add_ele capability."""
    from app.storage import _connect  # local import to avoid cycles at module load
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.device_id,
                       dc.values_json
                  FROM devices d
                  JOIN device_capabilities dc USING (device_id)
                 WHERE d.is_energy_meter
                   AND NOT COALESCE(d.is_generator, false)
                   AND NOT COALESCE(d.disabled, false)
                   AND dc.capability_code = 'add_ele'
                """
            )
            rows = cursor.fetchall()
    out: list[tuple[str, float]] = []
    for row in rows:
        values = row.get("values_json") or {}
        try:
            scale_digits = int(values.get("scale", 0) or 0)
        except (TypeError, ValueError):
            scale_digits = 0
        divisor = float(10 ** scale_digits) if scale_digits > 0 else 1.0
        out.append((str(row["device_id"]), divisor))
    return out


def _upsert_daily_row(
    conn: psycopg.Connection,
    device_id: str,
    day_local: date,
    kwh_cloud: float | None,
    source: str,
    error_message: str | None = None,
) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO device_energy_daily
                (device_id, day_local, kwh_cloud, source, verified_at, error_message)
            VALUES
                (%s, %s, %s, %s, NOW(), %s)
            ON CONFLICT (device_id, day_local) DO UPDATE
              SET kwh_cloud = EXCLUDED.kwh_cloud,
                  source = EXCLUDED.source,
                  verified_at = EXCLUDED.verified_at,
                  error_message = EXCLUDED.error_message
            """,
            (device_id, day_local, kwh_cloud, source, error_message),
        )


def verify_device_energy_daily(
    config: AppConfig,
    cloud_cfg: TuyaCloudConfig,
    device_id: str,
    scale_divisor: float,
    days_back: int = 30,
    tz: ZoneInfo | None = None,
) -> VerificationSummary:
    """Fetch cloud add_ele events for `device_id` across the last
    `days_back` local days, sum per day, and upsert into device_energy_daily.
    Also records rows with kwh_cloud=0 (source=cloud) for days where the
    cloud reported nothing — the plug was truly idle."""
    tz = tz or ZoneInfo(config.timezone)
    now_local = datetime.now(tz)
    today = now_local.date()
    # Include the current day only if it's past noon, otherwise the day is
    # incomplete and the sum would be lower than final.
    end_day_exclusive = today + timedelta(days=1)
    start_day = today - timedelta(days=days_back)
    start_dt = datetime.combine(start_day, datetime.min.time(), tzinfo=tz)
    end_dt = datetime.combine(end_day_exclusive, datetime.min.time(), tzinfo=tz)

    start_ms = int(start_dt.astimezone(timezone.utc).timestamp() * 1000)
    end_ms = int(end_dt.astimezone(timezone.utc).timestamp() * 1000)

    cloud = _cloud_client(cloud_cfg, device_id)
    try:
        events = _cloud_events_add_ele(cloud, device_id, start_ms, end_ms)
    except Exception as exc:
        # Record a single 'error' row for today so operators can see the
        # verifier tried and failed; do not blow away previously verified rows.
        logger.warning("Cloud verify failed for %s: %s", device_id, exc)
        from app.storage import _connect
        with _connect(config.database_url) as conn:
            _upsert_daily_row(conn, device_id, today, None, "error", str(exc)[:500])
            conn.commit()
        return VerificationSummary(device_id, 0, 0.0, None, error=str(exc))

    per_day = _sum_by_local_day(events, scale_divisor, tz)

    # Also insert a zero row for days in the window with no events — the
    # cloud says "0 kWh this day" and that's a real, verified fact.
    from app.storage import _connect
    with _connect(config.database_url) as conn:
        d = start_day
        while d < end_day_exclusive:
            # Skip today if it's not yet finished — the day-total will
            # keep growing. Still verify all past days.
            if d == today and now_local.hour < 12:
                d += timedelta(days=1)
                continue
            kwh = per_day.get(d, 0.0)
            _upsert_daily_row(conn, device_id, d, kwh, "cloud")
            d += timedelta(days=1)
        conn.commit()

    total = sum(per_day.values())
    latest = max(per_day.keys()) if per_day else None
    return VerificationSummary(
        device_id=device_id,
        days_covered=len(per_day),
        total_kwh=round(total, 3),
        latest_day=latest,
    )


def verify_all_devices(config: AppConfig, days_back: int = 30) -> list[VerificationSummary]:
    """Sequentially verify every energy-meter device with an add_ele
    capability. Sequential (not parallel) to stay under Tuya's rate limits."""
    from config import load_cloud_config
    cloud_cfg = load_cloud_config(required=False)
    if cloud_cfg is None:
        logger.info("Cloud verification skipped: no cloud credentials configured")
        return []
    devices = _energy_meter_devices_with_add_ele(config)
    summaries: list[VerificationSummary] = []
    for device_id, scale_divisor in devices:
        try:
            summary = verify_device_energy_daily(
                config, cloud_cfg, device_id, scale_divisor, days_back=days_back
            )
            summaries.append(summary)
            logger.info(
                "Verified %s: %d days, %.3f kWh, latest=%s",
                device_id, summary.days_covered, summary.total_kwh, summary.latest_day,
            )
        except Exception as exc:
            logger.exception("Verifier crashed on %s", device_id)
            summaries.append(
                VerificationSummary(device_id, 0, 0.0, None, error=str(exc))
            )
        # Small delay between devices to avoid hammering the cloud API.
        time.sleep(0.5)
    return summaries


def get_verified_daily_kwh(
    config: AppConfig,
    device_id: str,
    start_day: date,
    end_day_inclusive: date,
) -> dict[date, float]:
    """Read verified daily kWh for a device across the local-day range
    [start_day, end_day_inclusive]. Only 'cloud'-source rows are returned;
    the caller treats missing days as unverified."""
    from app.storage import _connect
    with _connect(config.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT day_local, kwh_cloud
                  FROM device_energy_daily
                 WHERE device_id = %s
                   AND source = 'cloud'
                   AND kwh_cloud IS NOT NULL
                   AND day_local BETWEEN %s AND %s
                """,
                (device_id, start_day, end_day_inclusive),
            )
            rows = cursor.fetchall()
    return {row["day_local"]: float(row["kwh_cloud"]) for row in rows}
