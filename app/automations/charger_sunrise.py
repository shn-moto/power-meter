"""Charger Sunrise — picks an overnight target SoC for the battery based on
weather forecast for Bielsko-Biała + historical solar generation and
expected daytime consumption, then drives the bound charger switch to
reach it.

Heuristic, intentionally simple for v1 — we'll refine after a few real
nights of data.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime
from time import monotonic
from typing import Any

from app.automations.base import (
    AutomationContext,
    AutomationResult,
    BaseAutomation,
    register,
    resolve_switch_function_code,
)

logger = logging.getLogger(__name__)


def _read_latest_soc(database_url: str, device_id: str) -> float | None:
    """Pull the latest sample's raw_dps[103] and divide by 10 to get %."""
    from app.storage import _connect  # local import to avoid cycle
    with _connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT raw_dps
                FROM samples
                WHERE device_id = %s
                ORDER BY captured_at DESC
                LIMIT 1
                """,
                (device_id,),
            )
            row = cursor.fetchone()
    if not row:
        return None
    raw = row.get("raw_dps")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if not isinstance(raw, dict):
        return None
    soc_raw = raw.get("103")
    if soc_raw is None:
        return None
    try:
        return float(soc_raw) / 10.0
    except (TypeError, ValueError):
        return None


def _fetch_forecast(lat: float, lon: float) -> tuple[float, int] | None:
    """Open-meteo daily forecast — returns (sunshine_hours_tomorrow,
    cloud_cover_max_tomorrow_pct) or None on failure."""
    params = urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "daily": "cloud_cover_max,sunshine_duration",
            "timezone": "Europe/Warsaw",
            "forecast_days": 2,
        }
    )
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("Open-meteo fetch failed: %s", exc)
        return None
    daily = payload.get("daily") or {}
    sunshine_seconds_arr = daily.get("sunshine_duration") or []
    cloud_cover_arr = daily.get("cloud_cover_max") or []
    if len(sunshine_seconds_arr) < 2 or len(cloud_cover_arr) < 2:
        return None
    sunshine_hours = float(sunshine_seconds_arr[1]) / 3600.0
    cloud_cover = int(cloud_cover_arr[1])
    return sunshine_hours, cloud_cover


@register
class ChargerSunrise(BaseAutomation):
    slug = "charger-sunrise"
    name = "Зарядка батареи к утру"
    description = (
        "В 02:00 проверяет прогноз погоды по Bielsko-Biała, статистику "
        "генерации солнца и потребления с батареи, определяет целевой SoC "
        "и включает зарядку до его достижения."
    )
    device_type = "charger"
    default_cron = "0 2 * * *"
    default_config = {
        "latitude": 49.82,
        "longitude": 19.06,
        "battery_capacity_wh": 2880,
        "battery_monitor_device_id": "bff9e5598e9abd78268oze",
        # Two-panel sub-kW setup. Calibrated 2026-06-21 against the MPPT
        # current sensor (power_correction_factor 0.84): true solar-noon peak
        # observed at 13:04 Warsaw was 360 W DC into the battery, on a day
        # with intermittent clouds during the noon hour — so 350 W is a fair
        # representative peak for "clear summer day". The old 600 W came from
        # un-calibrated Atorch readings that over-stated DC output by ~2×.
        "solar_peak_w": 350,
        # Empirical derating: open-meteo's sunshine_duration counts every
        # hour the sun is "unobscured" at full weight including dawn/dusk
        # angles, but panels at 49.8°N produce maybe 20-30 % of peak during
        # those low-angle hours. Observed daily totals after MPPT calibration:
        # ~1.0-1.5 kWh against forecast 8-9 sunshine_hours → effective
        # derating ≈ 0.45 (kWh / (peak × hours)). Previous 0.7 was tuned on
        # pre-calibration Atorch data and predicted ~2× the real generation.
        "solar_derating_factor": 0.45,
        "expected_daily_load_w": 200,
        "load_window_hours": 14,
        "safety_floor_soc": 30,
        "min_target_soc": 30,
        "max_target_soc": 95,
        # Leave as null → auto-resolved per device (single-channel plug uses
        # `switch`, Zigbee sub-device uses `switch_1`). Set explicitly only to
        # force a specific code.
        "charger_function_code": None,
        "poll_interval_seconds": 30,
        "max_charge_duration_seconds": 6 * 3600,
    }

    async def run(self, ctx: AutomationContext) -> AutomationResult:
        cfg = {**self.default_config, **(ctx.config_json or {})}
        log: list[str] = []

        if not ctx.bound_device_id:
            return AutomationResult(status="skipped", log="Зарядка не привязана к скрипту.")

        battery_id = str(cfg.get("battery_monitor_device_id") or "")
        if not battery_id:
            return AutomationResult(status="error", log="В config_json не задан battery_monitor_device_id.")

        soc = _read_latest_soc(ctx.config.database_url, battery_id)
        if soc is None:
            return AutomationResult(status="error", log=f"Нет свежих сэмплов для {battery_id} — SoC не получить.")
        log.append(f"Текущий SoC: {soc:.0f}%")

        forecast = await asyncio.to_thread(_fetch_forecast, float(cfg["latitude"]), float(cfg["longitude"]))
        if forecast is None:
            # Worst-case fallback: assume zero solar generation tomorrow.
            sunshine_hours = 0.0
            cloud_cover = 100
            log.append("Прогноз погоды недоступен — считаю как полностью пасмурный день.")
        else:
            sunshine_hours, cloud_cover = forecast
            log.append(f"Прогноз: {sunshine_hours:.1f} ч прямого солнца, облачность max {cloud_cover}%.")

        peak_w = float(cfg["solar_peak_w"])
        derating = float(cfg["solar_derating_factor"])
        expected_solar_wh = sunshine_hours * peak_w * derating
        log.append(f"Ожид. генерация: ~{expected_solar_wh:.0f} Вт·ч (peak {peak_w:.0f} Вт × {derating:.2f}).")

        expected_consumption_wh = float(cfg["expected_daily_load_w"]) * float(cfg["load_window_hours"])
        log.append(f"Ожид. потребление: {expected_consumption_wh:.0f} Вт·ч.")

        capacity_wh = max(float(cfg["battery_capacity_wh"]), 1.0)
        net_need_wh = expected_consumption_wh - expected_solar_wh
        safety_floor = float(cfg["safety_floor_soc"])
        # Battery must hold enough at sunrise so that (current - safety_floor)
        # capacity covers the net evening draw.
        target = safety_floor + max(0.0, net_need_wh) / capacity_wh * 100.0
        target = max(float(cfg["min_target_soc"]), min(float(cfg["max_target_soc"]), target))
        log.append(f"Net потребность: {net_need_wh:.0f} Вт·ч → target SoC: {target:.0f}%.")

        if soc >= target:
            log.append(f"SoC {soc:.0f}% уже ≥ target {target:.0f}%. Зарядка не нужна.")
            return AutomationResult(status="ok", log="\n".join(log), details={"target_soc": target, "soc": soc})

        function_code = str(cfg.get("charger_function_code") or "").strip() or resolve_switch_function_code(ctx.config, ctx.bound_device_id)
        log.append(f"Включаю зарядку ({function_code})…")
        ok = await ctx.invoke_device_function(ctx.bound_device_id, function_code, True)
        if not ok:
            return AutomationResult(status="error", log="\n".join(log + ["Не удалось включить зарядку."]))

        started_at = monotonic()
        last_announced_soc = soc
        poll = max(int(cfg["poll_interval_seconds"]), 5)
        max_duration = int(cfg["max_charge_duration_seconds"])
        final_soc = soc
        finish_reason = ""

        while True:
            await asyncio.sleep(poll)
            elapsed = monotonic() - started_at
            current = _read_latest_soc(ctx.config.database_url, battery_id)
            if current is None:
                finish_reason = "SoC недоступен в БД."
                break
            final_soc = current
            if current >= target:
                finish_reason = f"Цель достигнута: SoC {current:.0f}%."
                break
            if elapsed > max_duration:
                finish_reason = f"Таймаут ({elapsed/3600:.1f} ч), SoC {current:.0f}%."
                break
            if abs(current - last_announced_soc) >= 5:
                log.append(f"  SoC {current:.0f}% (прошло {elapsed/60:.0f} мин)")
                last_announced_soc = current

        log.append(finish_reason)
        log.append(f"Выключаю зарядку.")
        await ctx.invoke_device_function(ctx.bound_device_id, function_code, False)
        return AutomationResult(
            status="ok",
            log="\n".join(log),
            details={
                "target_soc": target,
                "soc_start": soc,
                "soc_end": final_soc,
                "expected_solar_wh": expected_solar_wh,
                "expected_consumption_wh": expected_consumption_wh,
                "sunshine_hours": sunshine_hours,
                "cloud_cover_max_pct": cloud_cover,
            },
        )
