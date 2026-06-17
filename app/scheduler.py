"""Per-minute cron scheduler that fires enabled automations and records the
outcome back to the DB."""
from __future__ import annotations

import asyncio
import logging
import zoneinfo
from datetime import datetime, timedelta

from croniter import croniter
from fastapi import FastAPI

from app.automations import REGISTRY, AutomationContext, AutomationResult
from app.storage import (
    get_automation,
    list_automations,
    record_automation_run,
    update_automation_next_run,
    upsert_automation,
)
from config import AppConfig

logger = logging.getLogger(__name__)


def _next_fire_time(cron_schedule: str, now: datetime) -> datetime | None:
    try:
        itr = croniter(cron_schedule, now)
        return itr.get_next(datetime)
    except Exception:
        logger.exception("Bad cron schedule %r", cron_schedule)
        return None


def seed_automations(config: AppConfig) -> None:
    """Insert any new automation discovered in code, leave user-bound rows
    untouched."""
    for slug, cls in REGISTRY.items():
        upsert_automation(
            config,
            slug=slug,
            name=cls.name,
            description=cls.description,
            device_type=cls.device_type,
            default_cron=cls.default_cron,
            default_config=cls.default_config,
        )
    # Prime next_run_at for any row missing it so the scheduler has a target.
    now = datetime.now(zoneinfo.ZoneInfo("UTC"))
    for row in list_automations(config):
        if row.get("next_run_at") is None and row.get("cron_schedule"):
            update_automation_next_run(
                config, row["slug"], _next_fire_time(row["cron_schedule"], now)
            )


async def _run_one(app: FastAPI, slug: str) -> None:
    config: AppConfig = app.state.app_config
    row = get_automation(config, slug)
    if not row:
        return
    cls = REGISTRY.get(slug)
    if cls is None:
        logger.warning("Scheduled automation %r isn't in REGISTRY", slug)
        return

    async def _invoke(device_id: str, function_code: str, value):  # noqa: ANN001
        return await app.state.invoke_device_function(device_id, function_code, value)

    ctx = AutomationContext(
        config=config,
        bound_device_id=row.get("bound_device_id"),
        config_json=row.get("config_json") or {},
        invoke_device_function=_invoke,
    )
    try:
        result: AutomationResult = await cls().run(ctx)
    except Exception as exc:
        logger.exception("Automation %s crashed", slug)
        result = AutomationResult(status="error", log=f"Crash: {exc}")

    next_at = _next_fire_time(row.get("cron_schedule") or cls.default_cron,
                              datetime.now(zoneinfo.ZoneInfo("UTC")))
    record_automation_run(
        config, slug, status=result.status, log=result.log, next_run_at=next_at
    )


async def scheduler_loop(app: FastAPI) -> None:
    config: AppConfig = app.state.app_config
    seed_automations(config)
    logger.info("Scheduler loop started; %d automation(s) registered", len(REGISTRY))

    while True:
        try:
            tz = zoneinfo.ZoneInfo("UTC")
            now = datetime.now(tz)
            running: set[str] = app.state.automation_running_slugs
            for row in list_automations(config):
                if not row.get("enabled"):
                    continue
                slug = row["slug"]
                if slug in running:
                    continue  # already in flight (could be hours for charger)
                next_at = row.get("next_run_at")
                if next_at is None:
                    update_automation_next_run(
                        config, slug, _next_fire_time(row["cron_schedule"], now)
                    )
                    continue
                if next_at.tzinfo is None:
                    next_at = next_at.replace(tzinfo=tz)
                if next_at <= now:
                    running.add(slug)

                    async def _run_and_release(s=slug):
                        try:
                            await _run_one(app, s)
                        finally:
                            running.discard(s)

                    asyncio.create_task(_run_and_release())
        except Exception:
            logger.exception("Scheduler iteration failed")
        await asyncio.sleep(30)  # check twice per minute is enough


async def invoke_device_function_via_app(app: FastAPI, device_id: str, function_code: str, value):  # noqa: ANN001
    """Reuses the same dispatch the /api function endpoint uses, so an
    automation flipping switch_1 goes through every safety check
    (control_type validation, sub-device cid handling, etc.)."""
    from app.storage import get_control_device, get_device_capabilities
    from app.web import _apply_device_command, _build_device_functions, SUPPORTED_CONTROL_TYPES  # local import to avoid cycle
    import tinytuya

    config: AppConfig = app.state.app_config
    device = get_control_device(config, device_id)
    if not device or not device.ip_address:
        logger.warning("Automation can't drive %s: no control device or IP", device_id)
        return False
    capabilities = get_device_capabilities(config, device_id)
    functions = _build_device_functions(capabilities)
    function = next((f for f in functions if f["code"] == function_code), None)
    if not function:
        logger.warning("Automation requested unknown function %s on %s", function_code, device_id)
        return False

    if device.gateway_device_id:
        tt = tinytuya.OutletDevice(
            dev_id=device.gateway_device_id,
            address=device.ip_address,
            local_key=device.local_key,
            cid=device.cid or device.device_id,
        )
    else:
        tt = tinytuya.Device(device.device_id, device.ip_address, device.local_key)
    tt.set_version(device.version)
    tt.set_socketTimeout(2.0)
    tt.set_socketRetryLimit(1)

    control_type = SUPPORTED_CONTROL_TYPES.get(function_code)
    if control_type == "toggle":
        coerced = bool(value)
    elif control_type in ("timer", "slider"):
        coerced = int(value)
    elif control_type == "enum":
        coerced = str(value)
    else:
        coerced = value
    # Sub-devices behind the Zigbee gateway, and ordinary Wi-Fi plugs after a
    # router restart, occasionally drop a single packet. Three attempts with
    # exponential-ish back-off turns a transient blip into a no-op instead of
    # a failed automation.
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            await asyncio.to_thread(_apply_device_command, tt, function_code, function["dp_id"], coerced)
            if attempt:
                logger.info("Automation dispatch for %s/%s succeeded on retry %d", device_id, function_code, attempt)
            return True
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Automation dispatch attempt %d/3 failed for %s/%s: %s",
                attempt + 1, device_id, function_code, exc,
            )
            await asyncio.sleep(1.5 * (attempt + 1))
    logger.error("Automation dispatch giving up on %s/%s after 3 attempts: %s", device_id, function_code, last_error)
    return False
