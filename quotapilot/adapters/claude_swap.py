"""Read the accounts of claude-swap (https://github.com/realiti4/claude-swap).

claude-swap holds the accounts and switches between them. This module only turns the JSON of
`cswap list --json` into `quotapilot.Account` rows. The primary window is the seven-day one; the
secondary window is the per-model weekly limit listed under `usage.scoped`.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..core import DEFAULT, Account, Config


def parse_at(text: Any) -> datetime | None:
    """An ISO stamp as an aware datetime (a naive one is read as local time), None if unreadable."""
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.astimezone()


def _window(usage: dict[str, Any] | None, key: str) -> tuple[float | None, datetime | None]:
    w = (usage or {}).get(key)
    if not isinstance(w, dict) or w.get("pct") is None:
        return None, None
    return float(w["pct"]), parse_at(w.get("resetsAt"))


def parse_list(
    data: dict[str, Any] | list[Any],
    now: datetime | None = None,
    secondary_name: str | None = None,
    config: Config = DEFAULT,
) -> list[Account]:
    """The accounts of a `cswap list --json` payload (the object, or just its `accounts` list).

    `secondary_name` picks the window of `usage.scoped` (the per-model weekly limits) that becomes
    the secondary window, by its `name`. None takes the first scoped window that has a percent.
    A row whose `usageStatus` is not `ok` but carries `lastGoodUsage` and `lastGoodFetchedAt` is
    read from them, flagged `stale`, when `now` is given and that fetch is at most
    `config.last_good_max_min` minutes old; otherwise it stays unusable. A row read with status
    `ok` takes `fetched_at` from `usageFetchedAt` when present.
    Raises ValueError, KeyError or TypeError on a malformed row.
    """
    rows = data["accounts"] if isinstance(data, dict) else data
    accounts: list[Account] = []
    for r in rows:
        name = str(r["number"])
        stale = False
        fetched_at: datetime | None = None
        if r.get("usageStatus") == "ok":
            usage = r.get("usage")
            fetched_at = parse_at(r["usageFetchedAt"]) if r.get("usageFetchedAt") else None
        else:
            usage = None
            good_at = parse_at(r["lastGoodFetchedAt"]) if r.get("lastGoodFetchedAt") else None
            good = r.get("lastGoodUsage")
            if (
                now is not None
                and good_at is not None
                and isinstance(good, dict)
                and now - good_at <= timedelta(minutes=config.last_good_max_min)
            ):
                usage, stale, fetched_at = good, True, good_at
        if usage is not None and not isinstance(usage, dict):
            raise TypeError(f"usage of {name} is {type(usage).__name__}")
        pct, reset = _window(usage, "sevenDay")
        secondary_pct: float | None = None
        secondary_reset: datetime | None = None
        for s in (usage or {}).get("scoped") or []:
            if s.get("pct") is None or (secondary_name and s.get("name") != secondary_name):
                continue
            secondary_pct, secondary_reset = float(s["pct"]), parse_at(s.get("resetsAt"))
            if not secondary_name:
                break
        accounts.append(
            Account(
                name=name,
                used_pct=pct,
                reset=reset,
                secondary_pct=secondary_pct,
                secondary_reset=secondary_reset,
                usable=usage is not None and pct is not None,
                fetched_at=fetched_at,
                stale=stale,
            )
        )
    return accounts


def read(
    run: Callable[..., Any] = subprocess.run,
    binary: str = "cswap",
    timeout: float = 120,
    secondary_name: str | None = None,
    config: Config = DEFAULT,
) -> list[Account]:
    """Run `cswap list --json` and parse it. RuntimeError when it fails or is unreadable."""
    try:
        out = run([binary, "list", "--json"], capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        raise RuntimeError(f"cswap list failed: {e}") from e
    if out.returncode != 0:
        raise RuntimeError(f"cswap list exit {out.returncode}: {(out.stderr or '').strip()[:120]}")
    try:
        data = json.loads(out.stdout)
        return parse_list(data, datetime.now(timezone.utc), secondary_name, config)
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise RuntimeError(f"cswap list bad json: {type(e).__name__}: {e}") from e
