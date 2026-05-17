"""Background listeners that periodically probe DPS 6/7/8 on breaker
devices and dump the responses into a shared in-memory store.

The MAIN POLL LOOP is the sole writer to the samples table — listeners
do not call status(), do not touch live_samples, do not save samples.
Their job is to grab phase_a/b/c packets the device doesn't include in
synchronous status() replies and stash them so the LIVE endpoint can
serve them. The probe cadence is intentionally low (every
PHASE_TRIGGER_INTERVAL_SECONDS) to leave the device's LAN channel free
for the poll loop most of the time.

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

PHASE_TRIGGER_INTERVAL_SECONDS = 15.0
PROBE_SOCKET_TIMEOUT_SECONDS = 3.0
PROBE_BACKOFF_AFTER_ERROR_SECONDS = 8.0

PHASE_PROBE_INDICES_1P = (7,)        # querying DPS 7 wakes DPS 6 on single-phase Tesla
PHASE_PROBE_INDICES_3P = (6, 7, 8)   # rotate through each phase on the 3-phase stove


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
        store: dict[str, RawDpsSnapshot],
        lan_lock: "threading.Lock | None" = None,
    ) -> None:
        super().__init__(daemon=True, name=f"raw-listener-{device.device_id}")
        self._device = device
        self._store = store
        self._lan_lock = lan_lock
        self._stop = threading.Event()
        self._merged: dict[str, Any] = {}

    def stop(self) -> None:
        self._stop.set()

    def _phase_probe_indices(self) -> tuple[int, ...]:
        modes = set((self._device.dps_request_modes or {}).values())
        if "trick678_3P" in modes:
            return PHASE_PROBE_INDICES_3P
        if "trick678_1P" in modes:
            return PHASE_PROBE_INDICES_1P
        return ()

    def _absorb(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        dps = payload.get("dps")
        if not isinstance(dps, dict) or not dps:
            return
        self._merged.update(dps)
        self._store[self._device.device_id] = RawDpsSnapshot(
            raw_dps=dict(self._merged),
            captured_at=datetime.now(timezone.utc),
        )

    def _open_client(self) -> tinytuya.Device:
        client = tinytuya.Device(
            self._device.device_id,
            self._device.ip_address,
            self._device.local_key,
            connection_timeout=PROBE_SOCKET_TIMEOUT_SECONDS,
        )
        client.set_version(self._device.version)
        client.set_socketTimeout(PROBE_SOCKET_TIMEOUT_SECONDS)
        client.set_socketRetryLimit(0)
        return client

    def _probe_once(self, probe_index: int) -> bool:
        """Open a fresh socket, fire a single updatedps([probe_index]), absorb
        the response, close. Returns True if response carried any DPS."""
        acquired = False
        if self._lan_lock is not None:
            # Wait up to ~5 seconds for the poll loop to release the LAN
            # socket — fail-fast otherwise rather than queueing forever.
            acquired = self._lan_lock.acquire(timeout=5.0)
            if not acquired:
                LOGGER.warning("phase probe for %s could not acquire LAN lock",
                               self._device.device_id)
                return False
        client = self._open_client()
        try:
            response = client.updatedps(index=[probe_index], nowait=False)
        except Exception:
            LOGGER.warning("phase probe error for %s code %s",
                           self._device.device_id, probe_index, exc_info=True)
            return False
        finally:
            try:
                client.close()
            except Exception:
                pass
            if acquired and self._lan_lock is not None:
                self._lan_lock.release()
        if not isinstance(response, dict):
            LOGGER.warning("probe DPS %s for %s -> non-dict %r",
                        probe_index, self._device.device_id, response)
            return False
        dps = response.get("dps")
        if not isinstance(dps, dict) or not dps:
            LOGGER.warning("probe DPS %s for %s -> %r",
                        probe_index, self._device.device_id, response)
            return False
        LOGGER.warning("probe DPS %s for %s OK keys=%s",
                    probe_index, self._device.device_id, sorted(dps.keys()))
        self._absorb(response)
        return True

    def run(self) -> None:
        LOGGER.warning("raw listener starting for %s", self._device.device_id)
        probe_indices = self._phase_probe_indices()
        if not probe_indices:
            LOGGER.warning("no probe indices for %s; listener exiting", self._device.device_id)
            return
        round_index = 0
        backoff_seconds = 0.0
        while not self._stop.is_set():
            if backoff_seconds > 0:
                if self._stop.wait(backoff_seconds):
                    break
                backoff_seconds = 0.0
            probe = probe_indices[round_index % len(probe_indices)]
            round_index += 1
            ok = self._probe_once(probe)
            if not ok:
                backoff_seconds = PROBE_BACKOFF_AFTER_ERROR_SECONDS
                continue
            if self._stop.wait(PHASE_TRIGGER_INTERVAL_SECONDS):
                break
        LOGGER.warning("raw listener stopped for %s", self._device.device_id)


def select_listener_devices(devices: list[TuyaDeviceConfig]) -> list[TuyaDeviceConfig]:
    """Pick devices that need a phase-probe listener — any breaker that
    declares a `trick678_*` request_mode in its profile."""
    selected: list[TuyaDeviceConfig] = []
    for device in devices:
        if not device.ip_address or not device.local_key:
            continue
        if has_trick678_request_mode(device):
            selected.append(device)
    return selected
