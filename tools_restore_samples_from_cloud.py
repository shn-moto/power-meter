"""Recover lost raw samples for a device whose history was destroyed.

Tuya cloud retains ~7 days of DP change events. We pull them and synthesise
a dense timeline (one sample every TICK_SECONDS) using carry-forward of the
last known cur_power / cur_voltage / cur_current / add_ele values, so the
samples_hourly aggregate formula (avg_power * span_factor) reconstructs
energy correctly — like the LAN poller would have done if it had been running.

    python tools_restore_samples_from_cloud.py \\
        --source-device-id <cloud_id_of_lost_device> \\
        --target-device-id <device_id_to_write_into> \\
        --hours-back 168 \\
        [--dry-run]

Idempotent: ON CONFLICT (device_id, captured_at, source) DO NOTHING with
source='cloud-restore', so re-running merges without duplicates and never
overwrites real LAN samples.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

import psycopg
import tinytuya

from config import load_app_config, load_cloud_config

TICK_SECONDS = 30          # synthetic-sample cadence (= dense enough for cagg energy formula)
ENERGY_METER_CODES = {"cur_power", "cur_voltage", "cur_current", "add_ele", "switch"}


def fetch_cloud_events(device_id: str, hours_back: int) -> list[dict]:
    cfg = load_cloud_config(required=True)
    cloud = tinytuya.Cloud(
        apiRegion=cfg.region,
        apiKey=cfg.api_key,
        apiSecret=cfg.api_secret,
        apiDeviceID=cfg.api_device_id or device_id,
    )
    end_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    start_ms = end_ms - hours_back * 3600 * 1000
    # size=0 makes tinytuya follow pagination (see source: while loop condition
    # `not want_size or len(logs) < size` — passing size=100 stops after page 1).
    resp = cloud.getdevicelog(
        device_id, start=start_ms, end=end_ms, evtype="7", size=0, max_fetches=500
    )
    if not isinstance(resp, dict) or not resp.get("success", True):
        raise RuntimeError(f"cloud error: {resp}")
    logs = (resp.get("result") or {}).get("logs") or []
    return [ev for ev in logs if ev.get("code") in ENERGY_METER_CODES]


def build_synthetic_samples(
    events: list[dict], hours_back: int
) -> tuple[list[tuple[datetime, float, dict]], dict]:
    """Walk events forward, carry-forwarding DP state, emit one synthetic
    sample per TICK_SECONDS interval starting at the first known cur_power.

    Returns (samples, stats) where samples is a list of (captured_at,
    power_w, raw_dps) tuples.
    """
    events = sorted(events, key=lambda e: int(e.get("event_time", 0)))
    if not events:
        return [], {"events": 0}

    state: dict[str, int] = {}                # current DP raw values
    first_ts_ms: int | None = None
    last_event_ts_ms = int(events[-1]["event_time"])

    samples: list[tuple[datetime, float, dict]] = []
    ev_idx = 0
    cur_ms = int(events[0]["event_time"])

    # Walk one tick at a time. At each tick, apply all events that happened
    # before this tick, then emit a sample reflecting current state.
    while cur_ms <= last_event_ts_ms:
        while ev_idx < len(events) and int(events[ev_idx]["event_time"]) <= cur_ms:
            ev = events[ev_idx]
            try:
                state[ev["code"]] = int(ev["value"]) if str(ev["value"]).lstrip("-").isdigit() else ev["value"]
            except (KeyError, TypeError, ValueError):
                state[ev["code"]] = ev.get("value")
            ev_idx += 1

        if "cur_power" in state:
            if first_ts_ms is None:
                first_ts_ms = cur_ms
            raw_value = state["cur_power"]
            if isinstance(raw_value, (int, float)):
                power_w = float(raw_value) / 10.0       # dp 19 scale_digits=1
            else:
                power_w = 0.0
            raw_dps = {}
            # Tuya DP ids for Smart Plug socket: 1 switch, 17 add_ele, 18 cur_current, 19 cur_power, 20 cur_voltage
            code_to_dp = {
                "switch": "1", "add_ele": "17", "cur_current": "18",
                "cur_power": "19", "cur_voltage": "20",
            }
            for code, dp_id in code_to_dp.items():
                if code in state:
                    raw_dps[dp_id] = state[code]
            captured_at = datetime.fromtimestamp(cur_ms / 1000, tz=timezone.utc)
            samples.append((captured_at, power_w, raw_dps))

        cur_ms += TICK_SECONDS * 1000

    stats = {
        "events": len(events),
        "first_event_utc": datetime.fromtimestamp(int(events[0]["event_time"]) / 1000, tz=timezone.utc).isoformat(),
        "last_event_utc": datetime.fromtimestamp(last_event_ts_ms / 1000, tz=timezone.utc).isoformat(),
        "synth_samples": len(samples),
    }
    return samples, stats


def insert_samples(
    db_url: str,
    target_device_id: str,
    samples: list[tuple[datetime, float, dict]],
) -> int:
    if not samples:
        return 0
    inserted = 0
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO samples (device_id, captured_at, power_w, raw_dps, source)
                VALUES (%s, %s, %s, %s, 'cloud-restore')
                ON CONFLICT (device_id, captured_at, source) DO NOTHING
                """,
                [
                    (target_device_id, ts, power_w, json.dumps(raw_dps))
                    for ts, power_w, raw_dps in samples
                ],
            )
            inserted = cur.rowcount
        conn.commit()
    return inserted


def refresh_caggs(db_url: str, start: datetime, end: datetime) -> None:
    # cont-agg refresh must run outside a transaction; use autocommit.
    # Each cagg has a different bucket size; the refresh window must cover
    # at least one full bucket, so we widen per view.
    windows = {
        "samples_hourly":  (start - timedelta(hours=2),  end + timedelta(hours=2)),
        "samples_daily":   (start.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2),
                            end + timedelta(days=2)),
        "samples_monthly": (start.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(days=60),
                            end + timedelta(days=60)),
    }
    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            for view, (s, e) in windows.items():
                cur.execute("CALL refresh_continuous_aggregate(%s, %s, %s)", (view, s, e))
                print(f"  refreshed {view}: {s.isoformat()}  ..  {e.isoformat()}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-device-id", required=True)
    parser.add_argument("--target-device-id", required=True)
    parser.add_argument("--hours-back", type=int, default=168)
    parser.add_argument(
        "--start-from-utc",
        help="ISO UTC timestamp; drop any synthetic samples earlier than this "
             "(use to avoid overlapping with real samples of another device on "
             "the same id, e.g. Linux Server before the plug move).",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print(f"fetching cloud events for {args.source_device_id} (last {args.hours_back}h)…")
    events = fetch_cloud_events(args.source_device_id, args.hours_back)
    samples, stats = build_synthetic_samples(events, args.hours_back)
    if args.start_from_utc:
        cutoff = datetime.fromisoformat(args.start_from_utc.replace("Z", "+00:00"))
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        before = len(samples)
        samples = [s for s in samples if s[0] >= cutoff]
        print(f"start-from-utc cutoff: {cutoff.isoformat()} — dropped {before - len(samples)} samples before cutoff")
    print(f"events: {stats['events']}")
    if stats["events"]:
        print(f"first event UTC: {stats['first_event_utc']}")
        print(f"last  event UTC: {stats['last_event_utc']}")
    print(f"synthetic samples ({TICK_SECONDS}s tick): {stats['synth_samples']}")

    if not samples:
        print("nothing to insert.")
        return 0

    span_start = samples[0][0]
    span_end = samples[-1][0]
    print(f"sample range: {span_start.isoformat()}  ..  {span_end.isoformat()}")
    print(f"target device_id: {args.target_device_id} (source='cloud-restore')")

    if args.dry_run:
        print("\nDRY RUN — not writing. First 5 samples:")
        for ts, p, raw in samples[:5]:
            print(f"  {ts.isoformat()}  power_w={p:.1f}  raw_dps={raw}")
        print("Last 5 samples:")
        for ts, p, raw in samples[-5:]:
            print(f"  {ts.isoformat()}  power_w={p:.1f}  raw_dps={raw}")
        return 0

    cfg = load_app_config()
    inserted = insert_samples(cfg.database_url, args.target_device_id, samples)
    print(f"\ninserted {inserted} new rows (skipped {len(samples) - inserted} duplicates).")

    refresh_start = span_start - timedelta(hours=1)
    refresh_end = span_end + timedelta(hours=1)
    print(f"refreshing continuous aggregates over {refresh_start.isoformat()}  ..  {refresh_end.isoformat()}")
    refresh_caggs(cfg.database_url, refresh_start, refresh_end)
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
