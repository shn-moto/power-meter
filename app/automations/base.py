"""Base class + registry for automation scripts."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, ClassVar

from config import AppConfig

logger = logging.getLogger(__name__)


@dataclass
class AutomationContext:
    """Everything the run() needs from the rest of the app, passed in by the
    scheduler so the automation stays decoupled from FastAPI internals."""

    config: AppConfig
    bound_device_id: str | None
    config_json: dict[str, Any]
    # Callback the automation can use to send a Tuya command. Returns True on
    # success. Will be the same dispatch used by the function endpoint so we
    # don't reinvent it.
    invoke_device_function: Any  # async (device_id, function_code, value) -> bool


@dataclass
class AutomationResult:
    status: str  # "ok" / "error" / "skipped"
    log: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class BaseAutomation:
    """Subclasses must define class-level slug, name, description,
    device_type (one of 'charger', 'lamp', 'socket', 'any'), and
    default_cron. Override async run() with the actual logic."""

    slug: ClassVar[str] = ""
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    device_type: ClassVar[str] = "any"
    default_cron: ClassVar[str] = "0 2 * * *"  # 02:00 local time
    default_config: ClassVar[dict[str, Any]] = {}

    async def run(self, ctx: AutomationContext) -> AutomationResult:
        raise NotImplementedError


REGISTRY: dict[str, type[BaseAutomation]] = {}


def resolve_switch_function_code(config: AppConfig, device_id: str) -> str:
    """Pick the right switch function code for the bound device by looking at
    its capabilities. Single-channel Wi-Fi plugs expose `switch`; Zigbee
    sub-devices (one channel of a multi-channel breaker) expose `switch_1`.
    Falls back to `switch` if nothing matches — that's the most common case."""
    from app.storage import get_device_capabilities  # local import: storage imports config which is fine

    capabilities = get_device_capabilities(config, device_id) or []
    switch_codes: list[str] = []
    for cap in capabilities:
        code = str(cap.get("capability_code") or "").strip()
        if code.startswith("switch"):
            switch_codes.append(code)
    if "switch_1" in switch_codes:
        return "switch_1"
    if "switch" in switch_codes:
        return "switch"
    return switch_codes[0] if switch_codes else "switch"


def register(cls: type[BaseAutomation]) -> type[BaseAutomation]:
    if not cls.slug:
        raise ValueError(f"{cls.__name__} is missing a slug")
    if cls.slug in REGISTRY:
        raise ValueError(f"Duplicate automation slug: {cls.slug}")
    REGISTRY[cls.slug] = cls
    logger.info("Registered automation: %s (%s)", cls.slug, cls.device_type)
    return cls
