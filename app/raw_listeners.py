"""Background listeners that hold a persistent Tuya LAN socket per device
and feed spontaneous DPS pushes into a shared in-memory store.

Used for breaker-class devices (Tesla wallbox, 3-phase stove) whose phase
packet DPS (6/7/8) only appear via spontaneous updates — the regular poll
loop never sees them because it opens a fresh socket per cycle and the
device doesn't include phase packets in synchronous `status()` replies.

For listener-managed devices the regular poll loop SKIPS them (otherwise
its fresh-socket status() queries would collide with the persistent
session and both sides would get Err 905 "Device Unreachable"). The
listener doubles as the device's poll source: it writes DeviceSample
rows to the DB at the same cadence the main poll loop uses (controlled
by AppConfig.sample_write_interval_seconds).

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

from config import AppConfig, TuyaDeviceConfig

LOGGER = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 9.0
RECEIVE_TIMEOUT_SECONDS = 2.0
RECONNECT_BACKOFF_SECONDS = 8.0
STATUS_REFRESH_INTERVAL_SECONDS = 45.0
PHASE_TRIGGER_INTERVAL_SECONDS = 15.0
PHASE_PROBE_INDICES_1P = (7,)        # querying DPS 7 wakes DPS 6 on single-phase Tesla
PHASE_PROBE_INDICES_3P = (6, 7, 8)   # query each phase to surface its packet on 3-phase stove


def has_trick678_request_mode(device: TuyaDeviceConfig) -> bool:
    for mode in (device.dps_request_modes or {}).values():
        if isinstance(mode, str) and mode.startswith("trick678_"):
            return True
    return False


@dataclass
class RawDpsSnapshot:
    raw_dps: dict[str, Any]
    captured_at: datetime


class RawListener(threading.Thread):
    def __init__(
        self,
        device: TuyaDeviceConfig,
        app_config: AppConfig,
        store: dict[str, RawDpsSnapshot],
        live_samples: dict[str, Any],
    ) -> None:
        super().__init__(daemon=True, name=f"raw-listener-{device.device_id}")
        self._device = device
        self._app_config = app_config
        self._store = store
        self._live_samples = live_samples
        self._stop = threading.Event()
        self._last_saved_at: datetime | None = None
        self._merged: dict[str, Any] = {}

    def stop(self) -> None:
        self._stop.set()

    def _absorb(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        dps = payload.get("dps")
        if not isinstance(dps, dict) or not dps:
            return
        self._merged.update(dps)
        captured = datetime.now(timezone.utc)
        self._store[self._device.device_id] = RawDpsSnapshot(
            raw_dps=dict(self._merged),
            captured_at=captured,
        )
        self._maybe_save_sample(captured)
        self._update_live_sample(captured)

    def _update_live_sample(self, captured_at: datetime) -> None:
        from app.tuya_service import extract_metrics
        from app.storage import DeviceSample

        try:
            power_w, _ = extract_metrics(self._device, {"dps": dict(self._merged)})
        except Exception:
            return
        self._live_samples[self._device.device_id] = DeviceSample(
            device_id=self._device.device_id,
            captured_at=captured_at,
            power_w=power_w,
            raw_dps=dict(self._merged),
        )

    def _maybe_save_sample(self, captured_at: datetime) -> None:
        from app.tuya_service import extract_metrics
        from app.storage import DeviceSample, save_sample

        interval = max(int(self._app_config.sample_write_interval_seconds or 5), 1)
        if (
            self._last_saved_at is not None
            and (captured_at - self._last_saved_at).total_seconds() < interval
        ):
            return
        try:
            power_w, _ = extract_metrics(self._device, {"dps": dict(self._merged)})
        except Exception:
            return
        sample = DeviceSample(
            device_id=self._device.device_id,
            captured_at=captured_at,
            power_w=power_w,
            raw_dps=dict(self._merged),
        )
        try:
            save_sample(self._app_config, sample)
        except Exception:
            LOGGER.debug("listener save_sample failed for %s", self._device.device_id, exc_info=True)
            return
        self._last_saved_at = captured_at

    def _phase_probe_indices(self) -> tuple[int, ...]:
        modes = set((self._device.dps_request_modes or {}).values())
        if "trick678_3P" in modes:
            return PHASE_PROBE_INDICES_3P
        if "trick678_1P" in modes:
            return PHASE_PROBE_INDICES_1P
        return ()

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
        last_phase_trigger = 0.0
        phase_probe_round = 0
        probe_indices = self._phase_probe_indices()
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
            if probe_indices and now - last_phase_trigger >= PHASE_TRIGGER_INTERVAL_SECONDS:
                probe = probe_indices[phase_probe_round % len(probe_indices)]
                phase_probe_round += 1
                try:
                    probe_response = client.updatedps(index=[probe], nowait=False)
                except Exception:
                    LOGGER.warning("phase probe failed for %s", self._device.device_id, exc_info=True)
                    break
                LOGGER.info(
                    "raw listener %s probe DPS %s -> %r",
                    self._device.device_id, probe, probe_response,
                )
                self._absorb(probe_response)
                last_phase_trigger = now

        try:
            client.close()
        except Exception:
            pass

    def run(self) -> None:
        # Use warning level so the message survives the default uvicorn log
        # filter; lets us confirm listeners actually spawn.
        LOGGER.warning("raw listener starting for %s", self._device.device_id)
        while not self._stop.is_set():
            try:
                self._session()
            except Exception:
                LOGGER.exception("raw listener session crashed for %s", self._device.device_id)
            if self._stop.is_set():
                break
            self._stop.wait(RECONNECT_BACKOFF_SECONDS)
        LOGGER.warning("raw listener stopped for %s", self._device.device_id)


def select_listener_devices(devices: list[TuyaDeviceConfig]) -> list[TuyaDeviceConfig]:
    """Pick devices that benefit from a persistent listener — currently any
    breaker that declares a `trick678_*` request_mode in its profile."""
    selected: list[TuyaDeviceConfig] = []
    for device in devices:
        if not device.ip_address or not device.local_key:
            continue
        if has_trick678_request_mode(device):
            selected.append(device)
    return selected
