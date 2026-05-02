import argparse
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import tinytuya

from app.storage import init_db, save_cloud_artifact, save_device_event, sync_devices
from config import ConfigError, TuyaDeviceConfig, load_app_config, load_cloud_config, load_devices


def _event_time(event: dict[str, Any]) -> datetime:
    candidates = [event.get("event_time"), event.get("time"), event.get("t"), event.get("timestamp")]
    for candidate in candidates:
        if candidate is None:
            continue
        if isinstance(candidate, (int, float)):
            value = float(candidate)
            if value > 10_000_000_000:
                value /= 1000.0
            return datetime.fromtimestamp(value, tz=timezone.utc)
        if isinstance(candidate, str):
            text = candidate.strip()
            if text.isdigit():
                value = float(text)
                if value > 10_000_000_000:
                    value /= 1000.0
                return datetime.fromtimestamp(value, tz=timezone.utc)
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                continue
    return datetime.now(timezone.utc)


def _event_value_to_mapping(raw_value: Any) -> dict[str, Any]:
    if isinstance(raw_value, dict):
        return raw_value
    if isinstance(raw_value, str):
        text = raw_value.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
    return {}


def _decode_phase_raw(raw_value: Any) -> dict[str, Any]:
    if not isinstance(raw_value, str):
        return {}

    try:
        payload = base64.b64decode(raw_value)
    except Exception:
        return {}

    decoded = {
        "raw_base64": raw_value,
        "raw_hex": payload.hex(),
        "byte_length": len(payload),
    }
    if len(payload) >= 2:
        decoded["voltage_v"] = round(int.from_bytes(payload[:2], "big") / 10.0, 1)
    return decoded


def _make_source_event_id(device: TuyaDeviceConfig, event: dict[str, Any], index: int) -> str:
    event_time = int(_event_time(event).timestamp() * 1000)
    event_code = str(event.get("code") or event.get("dp_id") or event.get("dps_id") or "unknown")
    raw_value = event.get("value")
    digest = hashlib.sha1(
        json.dumps(raw_value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]
    return f"{device.device_id}:{event_code}:{event_time}:{index}:{digest}"


def _select_devices(devices: list[TuyaDeviceConfig], requested_device_id: str | None) -> list[TuyaDeviceConfig]:
    if not requested_device_id:
        return devices
    selected = [device for device in devices if device.device_id == requested_device_id]
    if not selected:
        raise ConfigError(f"Configured devices do not include deviceId {requested_device_id}")
    return selected


def _save_cloud_snapshots(app_config, cloud, device: TuyaDeviceConfig) -> dict[str, Any]:
    endpoints = {
        "device_info": f"/v1.0/devices/{device.device_id}",
        "specifications": f"/v1.0/devices/{device.device_id}/specifications",
        "functions": f"/v1.0/devices/{device.device_id}/functions",
        "status": f"/v1.0/devices/{device.device_id}/status",
    }
    results: dict[str, Any] = {}
    for artifact_type, path in endpoints.items():
        payload = cloud.cloudrequest(path)
        results[artifact_type] = payload
        if isinstance(payload, dict):
            save_cloud_artifact(
                app_config,
                device_id=device.device_id,
                artifact_type=artifact_type,
                payload=payload,
            )
    return results


def _probe_daily_statistics(cloud, device: TuyaDeviceConfig) -> dict[str, Any]:
    return cloud.cloudrequest(f"/v1.0/devices/{device.device_id}/statistics/days")


def main() -> int:
    parser = argparse.ArgumentParser(description="One-time Tuya Cloud history sync into PostgreSQL")
    parser.add_argument("--days", type=int, default=30, help="How many past days to request from Tuya Cloud")
    parser.add_argument("--device-id", default=None, help="Sync only the specified Tuya deviceId")
    parser.add_argument("--max-fetches", type=int, default=50, help="Maximum log pages to fetch from Tuya Cloud")
    args = parser.parse_args()

    app_config = load_app_config()
    devices = load_devices()
    cloud_config = load_cloud_config(required=True)

    init_db(app_config)
    sync_devices(app_config, devices)

    cloud = tinytuya.Cloud(
        apiRegion=cloud_config.region,
        apiKey=cloud_config.api_key,
        apiSecret=cloud_config.api_secret,
        apiDeviceID=cloud_config.api_device_id or devices[0].device_id,
    )

    start = datetime.now(timezone.utc) - timedelta(days=args.days)
    end = datetime.now(timezone.utc)
    synced_events = 0
    selected_devices = _select_devices(devices, args.device_id)

    for device in selected_devices:
        snapshot_payloads = _save_cloud_snapshots(app_config, cloud, device)
        status_codes = [item.get("code") for item in snapshot_payloads.get("status", {}).get("result", []) if isinstance(item, dict)]
        print(f"{device.name}: fetched cloud snapshots, available status codes: {', '.join(status_codes)}")

        daily_stats_result = _probe_daily_statistics(cloud, device)
        if isinstance(daily_stats_result, dict):
            save_cloud_artifact(
                app_config,
                device_id=device.device_id,
                artifact_type="statistics_days_probe",
                payload=daily_stats_result,
            )
            if not daily_stats_result.get("success"):
                print(
                    f"{device.name}: statistics/days unavailable: {daily_stats_result.get('msg') or daily_stats_result.get('code')}"
                )

        result = cloud.getdevicelog(
            deviceid=device.device_id,
            start=int(start.timestamp()),
            end=int(end.timestamp()),
            evtype=7,
            size=0,
            max_fetches=args.max_fetches,
        )
        logs = result.get("result", {}).get("logs", []) if isinstance(result, dict) else []
        print(f"{device.name}: received {len(logs)} cloud log entries")

        if isinstance(result, dict):
            save_cloud_artifact(
                app_config,
                device_id=device.device_id,
                artifact_type="device_logs_summary",
                payload={
                    "success": result.get("success"),
                    "fetches": result.get("fetches"),
                    "result": {
                        "device_id": result.get("result", {}).get("device_id"),
                        "has_next": result.get("result", {}).get("has_next"),
                        "current_row_key": result.get("result", {}).get("current_row_key"),
                        "next_row_key": result.get("result", {}).get("next_row_key"),
                        "log_count": len(logs),
                    },
                },
            )

        for index, event in enumerate(logs):
            if not isinstance(event, dict):
                continue

            source_event_id = _make_source_event_id(device, event, index)
            event_payload = dict(event)
            event_code = str(event.get("code") or event.get("dp_id") or event.get("dps_id") or "")
            if event_code.startswith("phase_"):
                event_payload["decoded"] = _decode_phase_raw(event.get("value"))

            save_device_event(
                app_config,
                device_id=device.device_id,
                event_at=_event_time(event),
                event_type=str(event.get("type") or event.get("event_type") or ""),
                event_code=event_code,
                source_event_id=source_event_id,
                payload=event_payload,
            )
            synced_events += 1

    print(f"Synced {synced_events} raw cloud events for {len(selected_devices)} device(s)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as error:
        print(error)
        raise SystemExit(2)