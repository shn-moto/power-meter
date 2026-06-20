"""Fake solar pusher — POSTs synthetic MPPT readings to /api/ingest/mppt-solar
on a fixed cadence so we can eyeball the dashboard while the real hardware is
being built. Will be deleted once the real device is online.

    python tools_push_fake_solar.py \\
        --base http://shn-linux:8484 \\
        --token WC2O3LIrOC_-vKVHuA2SLMGUwMCpZp1w \\
        --interval 10

Behaviour: a clipped sine wave that peaks at solar noon (Europe/Warsaw),
zero at night, plus a bit of ±10 % noise. The expected max wattage is set
by --peak-w (default 600).
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo


def fake_power_w(now: datetime, peak_w: float) -> float:
    """Clipped sine. Hour 6 → 0 W, hour 13 → peak, hour 20 → 0 W."""
    h = now.hour + now.minute / 60.0 + now.second / 3600.0
    if h < 6 or h > 20:
        return 0.0
    # Map 6..20 -> 0..pi so peak lands at h=13 (= 6 + 7 = midpoint).
    x = (h - 6) / 14.0 * math.pi
    base = math.sin(x) * peak_w
    jitter = 1.0 + random.uniform(-0.10, 0.10)
    return max(0.0, base * jitter)


def fake_voltage_v() -> float:
    return round(72.0 + random.uniform(-0.5, 1.5), 2)


def push_one(base: str, token: str, power_w: float, voltage_v: float) -> tuple[int, str]:
    url = f"{base.rstrip('/')}/api/ingest/mppt-solar"
    # Real hardware will only know the current — voltage is resolved server-side
    # from the battery monitor at the moment of ingest. Mirror that here so the
    # fake exercises the same code path.
    current_a = round(power_w / voltage_v, 3) if voltage_v else 0.0
    body = json.dumps({
        "current_a": current_a,
        "raw_dps": {"source": "tools_push_fake_solar"},
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "X-Ingest-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return 0, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="e.g. http://shn-linux:8484")
    parser.add_argument("--token", required=True, help="X-Ingest-Token value")
    parser.add_argument("--interval", type=float, default=10.0, help="seconds between pushes")
    parser.add_argument("--peak-w", type=float, default=600.0, help="peak solar output (W)")
    parser.add_argument("--tz", default="Europe/Warsaw")
    args = parser.parse_args()

    tz = ZoneInfo(args.tz)
    print(f"pushing fake solar to {args.base}/api/ingest/mppt-solar every {args.interval}s (Ctrl-C to stop)")
    while True:
        now = datetime.now(tz)
        power = fake_power_w(now, args.peak_w)
        voltage = fake_voltage_v()
        code, body = push_one(args.base, args.token, power, voltage)
        ts = now.strftime("%H:%M:%S")
        print(f"  {ts}  power={power:.1f} W  V={voltage:.2f}  → {code}  {body[:80]}")
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nstopped.")
        sys.exit(0)
