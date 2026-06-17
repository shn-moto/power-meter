"""Аварийная зарядка батареи — каждые N минут проверяет SoC; если
батарея просела ниже порога, принудительно включает зарядку до
гистерезис-целевого уровня (чтобы не дёргать реле бесконечно вокруг
одного значения)."""
from __future__ import annotations

import asyncio
import logging
from time import monotonic

from app.automations.base import (
    AutomationContext,
    AutomationResult,
    BaseAutomation,
    register,
)
from app.automations.charger_sunrise import _read_latest_soc

logger = logging.getLogger(__name__)


@register
class BatteryEmergencyCharge(BaseAutomation):
    slug = "battery-emergency-charge"
    name = "Аварийная зарядка батареи"
    description = (
        "Каждые 15 минут читает SoC батареи. Если уровень упал ниже "
        "emergency_threshold_soc (по умолчанию 20%), включает зарядку и "
        "держит её до target_soc_after_emergency (по умолчанию 30%) — "
        "гистерезис не даёт реле щёлкать на границе порога."
    )
    device_type = "charger"
    default_cron = "*/15 * * * *"
    default_config = {
        "battery_monitor_device_id": "bff9e5598e9abd78268oze",
        "emergency_threshold_soc": 20,
        "target_soc_after_emergency": 30,
        "charger_function_code": "switch_1",
        "poll_interval_seconds": 30,
        "max_charge_duration_seconds": 3 * 3600,
    }

    async def run(self, ctx: AutomationContext) -> AutomationResult:
        cfg = {**self.default_config, **(ctx.config_json or {})}
        log: list[str] = []

        if not ctx.bound_device_id:
            return AutomationResult(status="skipped", log="Зарядка не привязана.")

        battery_id = str(cfg.get("battery_monitor_device_id") or "")
        if not battery_id:
            return AutomationResult(status="error", log="В config_json не задан battery_monitor_device_id.")

        soc = _read_latest_soc(ctx.config.database_url, battery_id)
        if soc is None:
            return AutomationResult(status="error", log=f"Нет свежих сэмплов для {battery_id} — SoC не получить.")

        threshold = float(cfg["emergency_threshold_soc"])
        target = float(cfg["target_soc_after_emergency"])
        log.append(f"SoC: {soc:.0f}% (порог: {threshold:.0f}%, цель: {target:.0f}%)")

        if soc > threshold:
            log.append("SoC выше порога — действий не требуется.")
            return AutomationResult(status="ok", log="\n".join(log), details={"soc": soc})

        function_code = str(cfg.get("charger_function_code") or "switch_1")
        log.append(f"АВАРИЙНАЯ зарядка: включаю {function_code}…")
        ok = await ctx.invoke_device_function(ctx.bound_device_id, function_code, True)
        if not ok:
            return AutomationResult(status="error", log="\n".join(log + ["Не удалось включить зарядку."]))

        started_at = monotonic()
        poll = max(int(cfg["poll_interval_seconds"]), 5)
        max_duration = int(cfg["max_charge_duration_seconds"])
        last_announced_soc = soc
        final_soc = soc
        finish_reason = ""

        while True:
            await asyncio.sleep(poll)
            elapsed = monotonic() - started_at
            current = _read_latest_soc(ctx.config.database_url, battery_id)
            if current is None:
                finish_reason = "SoC потерян (Atorch не отвечает)."
                break
            final_soc = current
            if current >= target:
                finish_reason = f"Цель достигнута: SoC {current:.0f}%."
                break
            if elapsed > max_duration:
                finish_reason = f"Таймаут ({elapsed/3600:.1f} ч), SoC {current:.0f}%."
                break
            if abs(current - last_announced_soc) >= 2:
                log.append(f"  SoC {current:.0f}% (прошло {elapsed/60:.0f} мин)")
                last_announced_soc = current

        log.append(finish_reason)
        log.append("Выключаю зарядку.")
        await ctx.invoke_device_function(ctx.bound_device_id, function_code, False)
        return AutomationResult(
            status="ok",
            log="\n".join(log),
            details={
                "soc_start": soc,
                "soc_end": final_soc,
                "target_soc": target,
                "threshold_soc": threshold,
            },
        )
