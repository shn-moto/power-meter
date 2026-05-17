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

PHASE_TRIGGER_INTERVAL_SECONDS = 5.0
PROBE_SOCKET_TIMEOUT_SECONDS = 2.0
PROBE_RETRY_LIMIT = 1
PROBE_BACKOFF_AFTER_ERROR_SECONDS = 4.0

PHASE_PROBE_INDICES = (6, 7, 8)   # probe all three each cycle and absorb whatever the device responds with


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
        # Diagnostic stats — read snapshot-style from outside via stats().
        self._cycles_total = 0
        self._cycles_with_dps = 0
        self._absorb_total = 0
        self._value_change_total = 0
        self._last_change_at: datetime | None = None
        self._last_probe_at: datetime | None = None

    def stop(self) -> None:
        self._stop.set()

    def stats(self) -> dict[str, Any]:
        return {
            "device_id": self._device.device_id,
            "cycles_total": self._cycles_total,
            "cycles_with_dps": self._cycles_with_dps,
            "absorb_total": self._absorb_total,
            "value_change_total": self._value_change_total,
            "last_change_at": self._last_change_at.isoformat() if self._last_change_at else None,
            "last_probe_at": self._last_probe_at.isoformat() if self._last_probe_at else None,
            "current_dps_keys": sorted(self._merged.keys()),
        }

    def _absorb(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        dps = payload.get("dps")
        if not isinstance(dps, dict) or not dps:
            return
        self._absorb_total += 1
        changed = False
        for key, value in dps.items():
            if self._merged.get(key) != value:
                changed = True
                self._merged[key] = value
        now = datetime.now(timezone.utc)
        if changed:
            self._value_change_total += 1
            self._last_change_at = now
        self._store[self._device.device_id] = RawDpsSnapshot(
            raw_dps=dict(self._merged),
            captured_at=now,
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
        client.set_socketRetryLimit(PROBE_RETRY_LIMIT)
        return client

    def _probe_cycle(self) -> bool:
        """Acquire the LAN lock once and probe DPS 6, 7, 8 in sequence on the
        same fresh socket. Absorb every DPS the device returns from any of
        the three queries. Returns True if at least one query produced a dps
        dict."""
        self._cycles_total += 1
        self._last_probe_at = datetime.now(timezone.utc)
        acquired = False
        if self._lan_lock is not None:
            acquired = self._lan_lock.acquire(timeout=5.0)
            if not acquired:
                LOGGER.warning("phase probe for %s could not acquire LAN lock",
                               self._device.device_id)
                return False
        # Give the device a moment to settle after the poll loop's last
        # close — Tuya breakers reject a fresh TCP connect that arrives too
        # quickly with Err 905.
        time.sleep(0.3)
        client = self._open_client()
        any_dps = False
        try:
            # Skip the status() prep — the poll loop already negotiated a
            # session moments ago, doing it again wastes 1-3 seconds per
            # cycle and we don't need the status payload here.
            for probe_index in PHASE_PROBE_INDICES:
                try:
                    response = client.updatedps(index=[probe_index], nowait=False)
                except Exception:
                    LOGGER.warning("phase probe error for %s code %s",
                                   self._device.device_id, probe_index, exc_info=True)
                    continue
                if not isinstance(response, dict):
                    LOGGER.warning("probe %s code %s -> non-dict %r",
                                   self._device.device_id, probe_index, response)
                    continue
                dps = response.get("dps")
                if isinstance(dps, dict) and dps:
                    LOGGER.warning("probe %s code %s OK dps=%s",
                                   self._device.device_id, probe_index, sorted(dps.keys()))
                    self._absorb(response)
                    any_dps = True
                else:
                    LOGGER.warning("probe %s code %s -> %r",
                                   self._device.device_id, probe_index, response)
            if any_dps:
                self._cycles_with_dps += 1
        except Exception:
            LOGGER.warning("probe cycle crashed for %s", self._device.device_id, exc_info=True)
        finally:
            try:
                client.close()
            except Exception:
                pass
            if acquired and self._lan_lock is not None:
                self._lan_lock.release()
        return any_dps

    def run(self) -> None:
        LOGGER.warning("raw listener starting for %s", self._device.device_id)
        backoff_seconds = 0.0
        while not self._stop.is_set():
            if backoff_seconds > 0:
                if self._stop.wait(backoff_seconds):
                    break
                backoff_seconds = 0.0
            ok = self._probe_cycle()
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
