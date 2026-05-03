from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg

from config import load_app_config


@dataclass(slots=True)
class RepairStats:
    device_id: str
    device_name: str
    sample_count: int
    first_at: datetime
    last_at: datetime


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair historical breaker samples that were saved with an incorrect power scale."
    )
    parser.add_argument(
        "--device-id",
        help="Repair only one device_id instead of all affected breakers.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show how many samples would be repaired without changing the database.",
    )
    return parser.parse_args()


def _candidate_filter_sql(device_id: str | None) -> tuple[str, tuple[object, ...]]:
    if not device_id:
        return "", ()
    return "AND s.device_id = %s", (device_id,)


def _find_repair_candidates(connection: psycopg.Connection, device_id: str | None) -> list[RepairStats]:
    device_filter_sql, params = _candidate_filter_sql(device_id)
    query = f"""
        SELECT s.device_id,
               d.name AS device_name,
               count(*) AS sample_count,
               min(s.captured_at) AS first_at,
               max(s.captured_at) AS last_at
        FROM samples s
        JOIN devices d ON d.device_id = s.device_id
        LEFT JOIN device_connections dc ON dc.device_id = s.device_id
        WHERE d.category_code = 'dlq'
          AND COALESCE(dc.power_dps_key, d.power_dps_key) = '102'
          AND COALESCE(dc.power_scale, d.power_scale, 1) = 100
          AND s.raw_dps ? '102'
          AND abs(s.power_w - ((s.raw_dps->>'102')::double precision)) < 0.0001
          AND abs(s.power_w - ((s.raw_dps->>'102')::double precision / 100.0)) >= 0.0001
          {device_filter_sql}
        GROUP BY s.device_id, d.name
        ORDER BY min(s.captured_at) ASC
    """
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        rows = cursor.fetchall()
    return [RepairStats(**row) for row in rows]


def _repair_samples(connection: psycopg.Connection, device_id: str | None) -> list[RepairStats]:
    candidates = _find_repair_candidates(connection, device_id)
    if not candidates:
        return []

    device_filter_sql, params = _candidate_filter_sql(device_id)
    query = f"""
        UPDATE samples s
        SET power_w = ((s.raw_dps->>'102')::double precision / 100.0)
        FROM devices d
        LEFT JOIN device_connections dc ON dc.device_id = d.device_id
        WHERE d.device_id = s.device_id
          AND d.category_code = 'dlq'
          AND COALESCE(dc.power_dps_key, d.power_dps_key) = '102'
          AND COALESCE(dc.power_scale, d.power_scale, 1) = 100
          AND s.raw_dps ? '102'
          AND abs(s.power_w - ((s.raw_dps->>'102')::double precision)) < 0.0001
          AND abs(s.power_w - ((s.raw_dps->>'102')::double precision / 100.0)) >= 0.0001
          {device_filter_sql}
    """
    with connection.cursor() as cursor:
        cursor.execute(query, params)
    return candidates


def _month_start(value: datetime) -> datetime:
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month_start(value: datetime) -> datetime:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return value.replace(month=value.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)


def _refresh_aggregates(database_url: str, candidates: list[RepairStats]) -> None:
    refresh_start = min(candidate.first_at for candidate in candidates).astimezone(timezone.utc)
    refresh_end = max(candidate.last_at for candidate in candidates).astimezone(timezone.utc)

    hourly_start = refresh_start.replace(minute=0, second=0, microsecond=0)
    hourly_end = (refresh_end.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))

    daily_start = hourly_start.replace(hour=0)
    daily_end = (hourly_end.replace(hour=0) + timedelta(days=1))

    monthly_start = _month_start(daily_start)
    monthly_end = _next_month_start(daily_end)

    with psycopg.connect(database_url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("CALL refresh_continuous_aggregate('samples_hourly', %s, %s)", (hourly_start, hourly_end))
            cursor.execute("CALL refresh_continuous_aggregate('samples_daily', %s, %s)", (daily_start, daily_end))
            cursor.execute("CALL refresh_continuous_aggregate('samples_monthly', %s, %s)", (monthly_start, monthly_end))


def main() -> int:
    args = _parse_args()
    config = load_app_config()

    with psycopg.connect(config.database_url, row_factory=psycopg.rows.dict_row) as connection:
        candidates = _find_repair_candidates(connection, args.device_id)
        if not candidates:
            print("No affected samples found.")
            return 0

        total_rows = sum(candidate.sample_count for candidate in candidates)
        print(f"Found {total_rows} affected samples across {len(candidates)} device(s).")
        for candidate in candidates:
            print(
                f"- {candidate.device_name} ({candidate.device_id}): "
                f"{candidate.sample_count} samples from {candidate.first_at.isoformat()} to {candidate.last_at.isoformat()}"
            )

        if args.dry_run:
            print("Dry run only, no changes applied.")
            return 0

        repaired = _repair_samples(connection, args.device_id)
        connection.commit()

        _refresh_aggregates(config.database_url, repaired)
        print(f"Repaired {total_rows} samples and refreshed aggregates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())