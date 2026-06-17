"""Custom automation scripts. Each automation is a subclass of BaseAutomation
registered in REGISTRY at import time.

The DB table `automations` stores user choices (bound device, enabled flag,
cron override, last run summary). Code is the source of truth for what
exists; rows are seeded at startup and orphaned ones get logged as stale.
"""
from app.automations.base import (
    AutomationContext,
    AutomationResult,
    BaseAutomation,
    register,
    REGISTRY,
)
from app.automations import charger_sunrise  # noqa: F401 — register on import
from app.automations import battery_emergency_charge  # noqa: F401

__all__ = [
    "AutomationContext",
    "AutomationResult",
    "BaseAutomation",
    "register",
    "REGISTRY",
]
