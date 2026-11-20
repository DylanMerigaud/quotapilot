"""quotapilot: where to burn the weekly quota of several accounts so the least expires."""

from .capacity import capacity_view, expired_room
from .core import (
    Account,
    Config,
    Credit,
    advise_credits,
    average_pace,
    decide,
    forecast,
    pace_band,
    pace_from_history,
    project,
)
from .plan import pace_profile, plan_credits
from .snapshot import Snapshot, merge
from .snapshot import dumps as snapshot_dumps
from .snapshot import loads as snapshot_loads

__version__ = "0.5.0"

__all__ = [
    "Account",
    "Config",
    "Credit",
    "Snapshot",
    "advise_credits",
    "average_pace",
    "capacity_view",
    "decide",
    "expired_room",
    "forecast",
    "merge",
    "pace_band",
    "pace_from_history",
    "pace_profile",
    "plan_credits",
    "project",
    "snapshot_dumps",
    "snapshot_loads",
]
