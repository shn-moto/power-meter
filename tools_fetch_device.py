"""Pull /v2.0/cloud/thing/{device_id} and .../model from Tuya Cloud and
print enough info to build a profile JSON. Used while bootstrapping new
device profiles by hand.

    python tools_fetch_device.py <device_id> [more device_ids...]
"""
from __future__ import annotations

import json
import sys

import tinytuya

from config import load_cloud_config


def fetch(device_id: str) -> None:
    cfg = load_cloud_config(required=True)
    cloud = tinytuya.Cloud(
        apiRegion=cfg.region,
        apiKey=cfg.api_key,
        apiSecret=cfg.api_secret,
        apiDeviceID=cfg.api_device_id or device_id,
    )
    print(f"\n===== {device_id} =====")
    thing = cloud.cloudrequest(f"/v2.0/cloud/thing/{device_id}")
    print("/v2.0/cloud/thing:")
    print(json.dumps(thing, ensure_ascii=False, indent=2))
    model = cloud.cloudrequest(f"/v2.0/cloud/thing/{device_id}/model")
    print("\n/v2.0/cloud/thing/{id}/model:")
    print(json.dumps(model, ensure_ascii=False, indent=2))
    try:
        shadow = cloud.cloudrequest(f"/v2.0/cloud/thing/{device_id}/shadow/properties")
        print("\n/v2.0/cloud/thing/{id}/shadow/properties:")
        print(json.dumps(shadow, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"shadow/properties: {exc}")
    try:
        spec = cloud.cloudrequest(f"/v1.1/devices/{device_id}/specifications")
        print("\n/v1.1/devices/{id}/specifications:")
        print(json.dumps(spec, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"specifications: {exc}")
    try:
        status = cloud.cloudrequest(f"/v1.0/devices/{device_id}/status")
        print("\n/v1.0/devices/{id}/status:")
        print(json.dumps(status, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"status: {exc}")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python tools_fetch_device.py <device_id> ...", file=sys.stderr)
        return 1
    for device_id in sys.argv[1:]:
        try:
            fetch(device_id)
        except Exception as exc:
            print(f"failed {device_id}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
