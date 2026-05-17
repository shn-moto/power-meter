"""Background listeners that hold a persistent Tuya LAN socket per device
and feed spontaneous DPS pushes into a shared in-memory store.

Used for breaker-class devices (Tesla wallbox, 3-phase stove) whose phase
packet DPS (6/7/8) only appear via spontaneous updates — the regular poll
loop never sees them because it opens a fresh socket per cycle and the
device doesn't include phase packets in synchronous `status()` replies.

Listeners run in OS threads (tinytuya is sync); the rest of the server
reads `app.state.raw_dps_latest[device_id]` lock-free (single-key dict
writes are atomic in CPython)."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import tinytuya

from config import TuyaDeviceConfig

LOGGER = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 9.0
RECEIVE_TIMEOUT_SECONDS = 2.0
RECONNECT_BACKOFF_SECONDS = 8.0
STATUS_REFRESH_INTERVAL_SECONDS = 45.0


def _has_trick678_request_mode(device: TuyaDeviceConfig) -> bool:
    for mode in (device.dps_request_modes or {}).values():
        if isinstance(mode, str) and mode.startswith("trick678_"):
            return True
    return False


@dataclass
class RawDpsSnapshot:
    raw_dps: dict[str, Any]
    captured_at: datetime


class RawListener(threading.Thread):
    def __init__(self, device: TuyaDeviceConfig, store: dict[str, RawDpsSnapshot]) -> None:
        super().__init__(daemon=True, name=f"raw-listener-{device.device_id}")
        self._device = device
        self._store = store
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _absorb(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        dps = payload.get("dps")
        if not isinstance(dps, dict) or not dps:
            return
        prev = self._store.get(self._device.device_id)
        merged = dict(prev.raw_dps) if prev else {}
        merged.update(dps)
        self._store[self._device.device_id] = RawDpsSnapshot(
            raw_dps=merged,
            captured_at=datetime.now(timezone.utc),
        )

    def _session(self) -> None:
        client = tinytuya.Device(
            self._device.device_id,
            self._device.ip_address,
            self._device.local_key,
            connection_timeout=5.0,
        )
        client.set_version(self._device.version)
        client.set_socketTimeout(RECEIVE_TIMEOUT_SECONDS)
        client.set_socketRetryLimit(0)
        client.set_socketPersistent(True)

        # Prime the connection so the device starts pushing phase packets.
        initial = client.status()
        self._absorb(initial)

        last_heartbeat = time.monotonic()
        last_status = time.monotonic()
        while not self._stop.is_set():
            try:
                data = client.receive()
            except Exception:
                LOGGER.debug("raw listener receive() failed for %s", self._device.device_id, exc_info=True)
                break
            if data:
                self._absorb(data)
            now = time.monotonic()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                try:
                    client.heartbeat(nowait=True)
                except Exception:
                    LOGGER.debug("heartbeat failed for %s", self._device.device_id, exc_info=True)
                    break
                last_heartbeat = now
            if now - last_status >= STATUS_REFRESH_INTERVAL_SECONDS:
                try:
                    refreshed = client.status()
                    self._absorb(refreshed)
                except Exception:
                    LOGGER.debug("status refresh failed for %s", self._device.device_id, exc_info=True)
                    break
                last_status = now

        try:
            client.close()
        except Exception:
            pass

    def run(self) -> None:
        LOGGER.info("raw listener starting for %s", self._device.device_id)
        while not self._stop.is_set():
            try:
                self._session()
            except Exception:
                LOGGER.exception("raw listener session crashed for %s", self._device.device_id)
            if self._stop.is_set():
                break
            self._stop.wait(RECONNECT_BACKOFF_SECONDS)
        LOGGER.info("raw listener stopped for %s", self._device.device_id)


def select_listener_devices(devices: list[TuyaDeviceConfig]) -> list[TuyaDeviceConfig]:
    """Pick devices that benefit from a persistent listener — currently any
    breaker that declares a `trick678_*` request_mode in its profile."""
    selected: list[TuyaDeviceConfig] = []
    for device in devices:
        if not device.ip_address or not device.local_key:
            continue
        if _has_trick678_request_mode(device):
            selected.append(device)
    return selected
