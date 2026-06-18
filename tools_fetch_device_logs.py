"""Probe Tuya Cloud device-log endpoint to see what historical DP events
are still available for a given device. Read-only — does not write anything.

    python tools_fetch_device_logs.py <device_id> [hours_back=168]

Default window is 7 days (Tuya free-tier retention). The script prints:
- total event count returned,
- earliest/latest event timestamps (Europe/Warsaw),
- event count per DP code,
- a small sample of the most recent events.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import tinytuya

from config import load_cloud_config

WARSAW = timezone(timedelta(hours=2))  # CEST; display-only


def fetch_logs(device_id: str, hours_back: int) -> None:
    cfg = load_cloud_config(required=True)
    cloud = tinytuya.Cloud(
        apiRegion=cfg.region,
        apiKey=cfg.api_key,
        apiSecret=cfg.api_secret,
        apiDeviceID=cfg.api_device_id or device_id,
    )
    end_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    start_ms = end_ms - hours_back * 3600 * 1000

    print(f"\n===== {device_id}  window: last {hours_back}h =====")
    print(f"start_time={start_ms}  end_time={end_ms}")

    # tinytuya.Cloud has a built-in getdevicelog that handles signing/pagination
    # correctly. max_fetch=0 = follow pagination until has_next is false.
    resp = cloud.getdevicelog(
        device_id,
        start=start_ms,
        end=end_ms,
        evtype="7",
        size=0,             # 0 = fetch all pages (up to max_fetches*100)
        max_fetches=200,    # 200 * 100 = 20k events ceiling
    )
    if not isinstance(resp, dict) or not resp.get("success", True):
        print("\ncloud error:")
        print(json.dumps(resp, ensure_ascii=False, indent=2))
        return
    all_logs = (resp.get("result") or {}).get("logs") or []

    if not all_logs:
        print("no events returned.")
        return

    times_ms = [int(ev.get("event_time", 0)) for ev in all_logs if ev.get("event_time")]
    earliest = datetime.fromtimestamp(min(times_ms) / 1000, tz=WARSAW)
    latest = datetime.fromtimestamp(max(times_ms) / 1000, tz=WARSAW)
    print(f"\ntotal events: {len(all_logs)}")
    print(f"earliest (Europe/Warsaw): {earliest.isoformat()}")
    print(f"latest   (Europe/Warsaw): {latest.isoformat()}")

    code_counts = Counter(ev.get("code") for ev in all_logs)
    print("\nevents by DP code:")
    for code, n in code_counts.most_common():
        print(f"  {code or '<none>'}: {n}")

    print("\nsample of 10 most recent events (raw):")
    sorted_logs = sorted(all_logs, key=lambda e: int(e.get("event_time", 0)), reverse=True)
    for ev in sorted_logs[:10]:
        ts = datetime.fromtimestamp(int(ev.get("event_time", 0)) / 1000, tz=WARSAW)
        print(f"  {ts.isoformat()}  {ev.get('code'):>20}  {ev.get('value')}")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python tools_fetch_device_logs.py <device_id> [hours_back=168]", file=sys.stderr)
        return 1
    device_id = sys.argv[1]
    hours_back = int(sys.argv[2]) if len(sys.argv) >= 3 else 168
    try:
        fetch_logs(device_id, hours_back)
    except Exception as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
