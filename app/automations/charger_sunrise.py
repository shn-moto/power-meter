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


def _fetch_forecast(
    lat: float,
    lon: float,
    tilt_deg: float,
    azimuth_deg: float,
    window_start_hour: int,
    window_end_hour: int,
) -> dict | None:
    """Open-meteo solar forecast focused on **today's charging window** —
    the hours when the balcony actually receives direct sun.

    Full-day GTI turned out to be too noisy for this rig: the balcony
    only catches direct sun ~10:00-15:00, so mornings and evenings add
    variance without predictive value. Summing hourly GTI over the
    charging window (default 10:00-15:00) reproduces the actual daily
    yield with 3-4× tighter variance day-to-day than the 24 h sum.

    Returns a dict with:
      gti_window_wh_per_m2  — sum of hourly tilted irradiance in the
                              [window_start_hour, window_end_hour) span
      gti_full_wh_per_m2    — sum over the whole day (diagnostic)
      window_hours          — length of the window in hours
      precipitation_mm      — daily precipitation total
      precip_prob_pct       — peak precipitation probability for the day
      cloud_mean_pct        — daily-average cloud cover
      temp_max_c            — daily peak temperature
      weather_code          — WMO code (logging only)
    """
    params = urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "hourly": "global_tilted_irradiance",
            "daily": ",".join([
                "precipitation_sum",
                "precipitation_probability_max",
                "cloud_cover_mean",
                "temperature_2m_max",
                "weather_code",
            ]),
            "tilt": tilt_deg,
            "azimuth": azimuth_deg,
            "timezone": "Europe/Warsaw",
            "forecast_days": 1,
        }
    )
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("Open-meteo fetch failed: %s", exc)
        return None
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    gti = hourly.get("global_tilted_irradiance") or []
    if not gti or not times:
        return None
    gti_window = 0.0
    gti_full = 0.0
    for t, v in zip(times, gti):
        if v is None:
            continue
        val = float(v)
        gti_full += val
        try:
            hr = int(t[11:13])
        except (ValueError, IndexError):
            continue
        if window_start_hour <= hr < window_end_hour:
            gti_window += val
    daily = payload.get("daily") or {}
    precip_arr = daily.get("precipitation_sum") or []
    pprob_arr = daily.get("precipitation_probability_max") or []
    cloud_arr = daily.get("cloud_cover_mean") or []
    temp_arr = daily.get("temperature_2m_max") or []
    wc_arr = daily.get("weather_code") or []
    return {
        "gti_window_wh_per_m2": gti_window,
        "gti_full_wh_per_m2": gti_full,
        "window_hours": max(window_end_hour - window_start_hour, 1),
        "precipitation_mm": float(precip_arr[0]) if precip_arr else 0.0,
        "precip_prob_pct": int(pprob_arr[0]) if pprob_arr else 0,
        "cloud_mean_pct": int(cloud_arr[0]) if cloud_arr else 0,
        "temp_max_c": float(temp_arr[0]) if temp_arr else None,
        "weather_code": int(wc_arr[0]) if wc_arr else None,
    }


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
        # GTI-based prediction, focused on the balcony's charging window
        # (10:00-15:00 by default — the hours it sees direct sun).
        # Full-day GTI turned out too noisy: mornings/evenings contribute
        # variance without predictive value, and 2026-07-01's 02:00 run
        # under-predicted (738 Wh forecast vs battery hitting 100 % by
        # 13:16, so ≥ 1.3 kWh actual) because the storm-forecast triggered
        # a 0.5× precipitation discount that reality never applied.
        # Recalibrated 2026-07-01 against 2026-06-27 actual generation:
        # 1605 Wh measured ÷ 3989 Wh/m² (window GTI at tilt 30°,
        # azimuth -90° = south) = 0.402 Wh per (Wh/m² of window GTI).
        # open-meteo azimuth convention verified empirically: -90 = south
        # for the northern hemisphere.
        "panel_tilt_deg": 30,
        "panel_azimuth_deg": -90,
        "balcony_window_start_hour": 10,
        "balcony_window_end_hour": 15,
        "solar_calibration_wh_per_wh_per_m2": 0.40,
        # Heavy precipitation drops effective generation further — wet
        # panels reflect more, and storm clouds are usually thicker than
        # the daily-average cloud cover captures. > 5 mm forecast → halve
        # the GTI-based estimate.
        "precip_discount_threshold_mm": 5.0,
        "precip_discount_factor": 0.5,
        # 1500 Wh ceiling on the GTI-based estimate. A truly peak summer
        # day on this rig is ~1.8 kWh; staying conservative under the
        # absolute peak avoids over-confidence on freak high forecasts.
        "max_expected_solar_wh": 1500,
        # Lowered 2026-06-30 by 30 W: user moved the desktop monitor off
        # the inverter (now mains-fed), so the baseline daytime draw is
        # 170 W average instead of 200 W. Daytime window unchanged.
        "expected_daily_load_w": 170,
        "load_window_hours": 14,
        "safety_floor_soc": 30,
        # Floor 65 % = sunny-day target on the new GTI-based formula. Even
        # at 65 % start, a clear-sky day adds ~25-35 % during the noon
        # window — leaves margin against 100 % clipping. Lowered from 75
        # 2026-06-30 after the GTI forecast still over-charged: today
        # peaked at 97 % with 1.65 kWh of measured generation against a
        # 1.55 kWh forecast, so even the accurate prediction needed more
        # headroom on the SoC side.
        # max_target_soc 95 % = absolute cap for fully cloudy forecasts.
        # Dynamic ceiling interpolates: bright → 65-75 %, half-cloudy →
        # ~85 %, fully cloudy → 95 %.
        "min_target_soc": 65,
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

        tilt_deg = float(cfg.get("panel_tilt_deg", 30))
        azimuth_deg = float(cfg.get("panel_azimuth_deg", 0))
        win_start = int(cfg.get("balcony_window_start_hour", 10))
        win_end = int(cfg.get("balcony_window_end_hour", 15))
        forecast = await asyncio.to_thread(
            _fetch_forecast,
            float(cfg["latitude"]),
            float(cfg["longitude"]),
            tilt_deg,
            azimuth_deg,
            win_start,
            win_end,
        )
        if forecast is None:
            gti_window = 0.0
            gti_full = 0.0
            precip_mm = 0.0
            precip_prob = 0
            cloud_mean = 100
            temp_max_c: float | None = None
            weather_code: int | None = None
            log.append("Прогноз погоды недоступен — считаю как полностью пасмурный день.")
        else:
            gti_window = float(forecast["gti_window_wh_per_m2"])
            gti_full = float(forecast["gti_full_wh_per_m2"])
            precip_mm = float(forecast["precipitation_mm"])
            precip_prob = int(forecast["precip_prob_pct"])
            cloud_mean = int(forecast["cloud_mean_pct"])
            temp_max_c = forecast.get("temp_max_c")
            weather_code = forecast.get("weather_code")
            extras = []
            if temp_max_c is not None:
                extras.append(f"t° {temp_max_c:.0f}°")
            if weather_code is not None:
                extras.append(f"wmo {weather_code}")
            extras_str = f" ({', '.join(extras)})" if extras else ""
            log.append(
                f"Прогноз: GTI окна {win_start}-{win_end}h = {gti_window:.0f} Вт·ч/м² "
                f"(за сутки {gti_full:.0f}), облачность ср {cloud_mean}%, "
                f"осадки {precip_mm:.1f} мм ({precip_prob}%){extras_str}."
            )

        # Convert tilted-irradiance forecast to expected battery-side Wh
        # via the empirically-calibrated rig efficiency factor.
        calibration = float(cfg.get("solar_calibration_wh_per_wh_per_m2", 0.40))
        raw_expected = gti_window * calibration
        # Heavy precipitation drops effective yield beyond what the cloud-
        # cover-modulated GTI captures (wet panels, persistent storm clouds).
        precip_threshold = float(cfg.get("precip_discount_threshold_mm", 5.0))
        precip_factor = float(cfg.get("precip_discount_factor", 0.5))
        discount_applied = False
        if precip_mm >= precip_threshold:
            raw_expected *= precip_factor
            discount_applied = True
        # Safety cap on the expectation — used both as a literal upper
        # bound and as the reference value for solar_aware_ceiling below.
        max_expected = float(cfg.get("max_expected_solar_wh") or 0)
        expected_solar_wh = min(raw_expected, max_expected) if max_expected > 0 else raw_expected
        cap_notes = []
        if discount_applied:
            cap_notes.append(f"осадки ×{precip_factor:.2f}")
        if max_expected > 0 and raw_expected > max_expected:
            cap_notes.append(f"Вт·ч {raw_expected:.0f}→{max_expected:.0f}")
        suffix = f" [cap: {', '.join(cap_notes)}]" if cap_notes else ""
        log.append(
            f"Ожид. генерация: ~{expected_solar_wh:.0f} Вт·ч "
            f"(GTI окна {gti_window:.0f} × {calibration:.3f} "
            f"Вт·ч/(Вт·ч/м²)){suffix}."
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
                "gti_window_wh_per_m2": gti_window,
                "gti_full_wh_per_m2": gti_full,
                "precipitation_mm": precip_mm,
                "cloud_mean_pct": cloud_mean,
            },
        )
