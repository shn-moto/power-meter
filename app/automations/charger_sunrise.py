"""Charger Sunrise — picks an overnight target SoC for the battery based on
weather forecast for Bielsko-Biała and historical solar generation +
consumption stats, then drives the bound charger switch to reach it.

Phase 3 will fill in the body. This phase ships only the registration so
the framework has at least one real automation to wire the UI against.
"""
from __future__ import annotations

import logging
from typing import Any

from app.automations.base import (
    AutomationContext,
    AutomationResult,
    BaseAutomation,
    register,
)

logger = logging.getLogger(__name__)


@register
class ChargerSunrise(BaseAutomation):
    slug = "charger-sunrise"
    name = "Зарядка батареи к утру"
    description = (
        "В 02:00 проверяет прогноз погоды по Bielsko-Biała, статистику "
        "генерации солнца и потребления с батареи, определяет целевой SoC и "
        "включает зарядку до его достижения."
    )
    device_type = "charger"
    default_cron = "0 2 * * *"
    default_config = {
        "latitude": 49.82,
        "longitude": 19.06,
        "battery_capacity_wh": 2880,
        "expected_daily_load_w": 200,
        "load_window_hours": 14,
        "min_target_soc": 30,
        "max_target_soc": 95,
    }

    async def run(self, ctx: AutomationContext) -> AutomationResult:
        if not ctx.bound_device_id:
            return AutomationResult(status="skipped", log="No charger bound.")
        # TODO Phase 3:
        # - read latest SoC from samples table for the battery monitor
        # - fetch open-meteo forecast for tomorrow's cloud cover
        # - compute target SoC from expected load - expected solar generation
        # - if current SoC < target: turn switch_1 ON, poll until target
        # - emit detailed log
        return AutomationResult(
            status="ok",
            log="Phase 2 stub: framework alive, decision logic pending.",
            details={"bound_device_id": ctx.bound_device_id},
        )
