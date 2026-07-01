"""Daily job that syncs `device_energy_daily` from Tuya Cloud add_ele
event logs. See app/cloud_verification.py for the mechanism."""
from __future__ import annotations

import asyncio
import logging

from app.automations.base import (
    AutomationContext,
    AutomationResult,
    BaseAutomation,
    register,
)
from app.cloud_verification import verify_all_devices

logger = logging.getLogger(__name__)


@register
class EnergyVerifier(BaseAutomation):
    slug = "energy-verifier"
    name = "Верификация энергии по облаку"
    description = (
        "Каждый день в 03:00 запрашивает у Tuya Cloud историю событий "
        "add_ele за последние 30 дней и сохраняет суммы по дням. "
        "Это ground truth для расчёта расхождения со счётчиком — "
        "перекрывает периоды когда сокет был offline."
    )
    device_type = "any"
    default_cron = "0 3 * * *"
    default_config = {
        # Retention on Tuya's free tier is ~30 days. Re-verifying the whole
        # window every night keeps every day within retention up-to-date
        # (in case the cloud back-filled events during the day) and lets a
        # missed run be caught up automatically on the next fire.
        "days_back": 30,
    }

    async def run(self, ctx: AutomationContext) -> AutomationResult:
        cfg = {**self.default_config, **(ctx.config_json or {})}
        days_back = int(cfg.get("days_back") or 30)
        summaries = await asyncio.to_thread(
            verify_all_devices, ctx.config, days_back
        )
        if not summaries:
            return AutomationResult(
                status="skipped",
                log="Cloud credentials not configured — skipped.",
            )
        lines: list[str] = []
        ok_count = 0
        err_count = 0
        for s in summaries:
            if s.error:
                err_count += 1
                lines.append(f"  ✗ {s.device_id}: {s.error[:120]}")
            else:
                ok_count += 1
                latest = s.latest_day.isoformat() if s.latest_day else "—"
                lines.append(
                    f"  ✓ {s.device_id}: {s.days_covered} дней активных, "
                    f"Σ {s.total_kwh} кВт·ч, latest={latest}"
                )
        header = (
            f"Верификация завершена: {ok_count} ok, {err_count} ошибок "
            f"(окно {days_back} дней)."
        )
        return AutomationResult(
            status="error" if err_count and not ok_count else "ok",
            log=header + "\n" + "\n".join(lines),
            details={"ok_count": ok_count, "err_count": err_count},
        )
