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
    # Weekdays only — the user controls weekend charging manually so they
    # can use the rig under different patterns (longer naps + lighter loads
    # vs the weekday baseline assumed by the heuristic). Cron weekday field:
    # 0 = Sunday, 1-5 = Mon-Fri, 6 = Saturday. So 1-5 fires Mon-Fri 02:00,
    # skipping the Sat 02:00 (= Fri→Sat night) and Sun 02:00 (Sat→Sun night)
    # runs that the user vetoed.
    default_cron = "0 2 * * 1-5"
    default_config = {
        "latitude": 49.82,
        "longitude": 19.06,
        # Huawei powerbank nameplate "working zone" rating — already accounts
        # for the safe-discharge / safe-charge margin baked into the BMS
        # cutoffs. Less than the raw V_nom × Ah = 72 × 40 = 2880 Wh figure
        # on purpose; using 2880 in the math makes the night top-up think
        # there's headroom that doesn't exist.
        "battery_capacity_wh": 2530,
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
        # Recalibrated 2026-06-27 after the user re-aimed the panels: on a
        # clear-sky day the rig delivered 1.60 kWh vs the previous formula's
        # 1.05 kWh estimate (35 % under-prediction → battery hit 100 % by
        # 13:00 and clipped 4 h of generation). The angle tweak + wider
        # effective production window means both knobs need a nudge:
        # 5 h × 350 W × 0.85 = 1487 Wh, which reproduces 2026-06-27's
        # 1.6 kWh comfortably and leaves a tiny safety margin on the
        # high-derate side.
        "solar_derating_factor": 0.85,
        # Wider effective window after the angle optimisation — panels now
        # contribute meaningfully from ~10:00 to ~16:00 instead of the
        # earlier 11:00-15:00. 5 h is closer to what we actually integrate.
        "max_sunshine_hours": 5,
        # 5 h × 350 W × 0.85 ≈ 1487 Wh is now the base expectation, so the
        # absolute cap of 1500 Wh is essentially the same as a normal clear
        # day. A freak shoulder-heavy day could plausibly nudge above this
        # but very rarely — keep 1500 as the ceiling.
        "max_expected_solar_wh": 1500,
        "expected_daily_load_w": 200,
        "load_window_hours": 14,
        "safety_floor_soc": 30,
        # Floor 75 % = sunny-day ceiling (panels will push the last 20-25 %);
        # max_target_soc = 95 % is the absolute cap, used on fully cloudy
        # forecasts when the panels won't make any meaningful contribution.
        # The actual nightly ceiling is *interpolated* between these two
        # against the day's expected solar (see `solar_aware_ceiling` in
        # run()): bright forecast → 75-80 %, half-cloudy → ~88 %, fully
        # cloudy → 95 %. Bonus: LiFePO4 cycle life is meaningfully better
        # with daily peaks below 90 % than at 100 %.
        "min_target_soc": 75,
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
        max_sun_hours = float(cfg.get("max_sunshine_hours") or sunshine_hours or 0)
        capped_sun_hours = min(sunshine_hours, max_sun_hours) if max_sun_hours > 0 else sunshine_hours
        raw_expected = capped_sun_hours * peak_w * derating
        max_expected = float(cfg.get("max_expected_solar_wh") or 0)
        expected_solar_wh = min(raw_expected, max_expected) if max_expected > 0 else raw_expected
        cap_notes = []
        if capped_sun_hours < sunshine_hours:
            cap_notes.append(f"часы {sunshine_hours:.1f}→{capped_sun_hours:.1f}")
        if max_expected > 0 and raw_expected > max_expected:
            cap_notes.append(f"Вт·ч {raw_expected:.0f}→{max_expected:.0f}")
        suffix = f" [cap: {', '.join(cap_notes)}]" if cap_notes else ""
        log.append(
            f"Ожид. генерация: ~{expected_solar_wh:.0f} Вт·ч "
            f"(peak {peak_w:.0f} Вт × {capped_sun_hours:.1f} ч × {derating:.2f}){suffix}."
        )

        expected_consumption_wh = float(cfg["expected_daily_load_w"]) * float(cfg["load_window_hours"])
        log.append(f"Ожид. потребление: {expected_consumption_wh:.0f} Вт·ч.")

        capacity_wh = max(float(cfg["battery_capacity_wh"]), 1.0)
        net_need_wh = expected_consumption_wh - expected_solar_wh
        safety_floor = float(cfg["safety_floor_soc"])
        # Battery must hold enough at sunrise so that (current - safety_floor)
        # capacity covers the net evening draw.
        target = safety_floor + max(0.0, net_need_wh) / capacity_wh * 100.0
        # Solar-aware shrinkage of the upper bound. Truly cloudy days can
        # safely fill the battery up to max_target_soc (95 %) because the
        # panels won't push it past 100 %; bright forecast days need
        # headroom so the next afternoon's gain lands inside 100 %.
        # Linear interpolation: 0 Wh expected → 95 % ceiling, configured
        # max_expected_solar_wh → 80 % ceiling.
        max_solar_ref = max(float(cfg.get("max_expected_solar_wh") or 1500.0), 1.0)
        solar_fraction = min(1.0, max(0.0, expected_solar_wh / max_solar_ref))
        reserve_pct = 5.0 + 15.0 * solar_fraction
        solar_aware_ceiling = 100.0 - reserve_pct
        effective_max = min(float(cfg["max_target_soc"]), solar_aware_ceiling)
        target = max(float(cfg["min_target_soc"]), min(effective_max, target))
        log.append(
            f"Потолок: {effective_max:.0f}% "
            f"(резерв под солнце {reserve_pct:.0f}%, "
            f"абс. макс {float(cfg['max_target_soc']):.0f}%)."
        )
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
