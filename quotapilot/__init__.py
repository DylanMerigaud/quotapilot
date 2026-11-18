"""quotapilot: where to burn the weekly quota of several accounts so the least expires."""

from .core import (
    Account,
    Config,
    average_pace,
    decide,
    pace_from_history,
    project,
)

__version__ = "0.1.0"

__all__ = [
    "Account",
    "Config",
    "average_pace",
    "decide",
    "pace_from_history",
    "project",
]
