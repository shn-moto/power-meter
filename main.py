import json
import time
import sys

import tinytuya

from config import ConfigError, load_config


def build_device() -> tinytuya.Device:
    settings = load_config()
    device = tinytuya.Device(
        settings["device_id"],
        settings["ip_address"],
        settings["local_key"],
    )
    device.set_version(float(settings["version"]))
    return device


def main() -> int:
    try:
        device = build_device()
        payload = device.status()
    except ConfigError as error:
        print(error)
        print(
            "Set TUYA_DEVICE_ID, TUYA_LOCAL_KEY, TUYA_IP_ADDRESS, and optionally TUYA_VERSION."
        )
        return 2
    except Exception as error:
        print(f"Connection failed: {error}")
        return 1

    if not isinstance(payload, dict):
        print("Unexpected response:")
        print(payload)
        return 1

    print(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("Device response:")
    print(json.dumps(payload, indent=2, ensure_ascii=False))

    dps = payload.get("dps")
    if not dps:
        print("Connection succeeded, but no DPS data was returned.")
        return 1

    print("DPS data is available.")
    return 0


if __name__ == "__main__":
    sys.exit(main())