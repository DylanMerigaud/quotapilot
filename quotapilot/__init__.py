"""quotapilot: where to burn the weekly quota of several accounts so the least expires."""

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

__version__ = "0.3.0"

__all__ = [
    "Account",
    "Config",
    "Credit",
    "advise_credits",
    "average_pace",
    "decide",
    "forecast",
    "pace_band",
    "pace_from_history",
    "project",
]
