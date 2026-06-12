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


def register(cls: type[BaseAutomation]) -> type[BaseAutomation]:
    if not cls.slug:
        raise ValueError(f"{cls.__name__} is missing a slug")
    if cls.slug in REGISTRY:
        raise ValueError(f"Duplicate automation slug: {cls.slug}")
    REGISTRY[cls.slug] = cls
    logger.info("Registered automation: %s (%s)", cls.slug, cls.device_type)
    return cls
