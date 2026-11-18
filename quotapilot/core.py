"""Pure core of quotapilot: stdlib only, every datetime timezone-aware, any zone accepted."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence, Tuple

Sample = Tuple[datetime, str, Optional[float]]


@dataclass
class Account:
    """One account and its quota windows, as percent used and the reset instant.

    The primary window is the weekly one. The secondary window is a second, narrower window some
    providers have (for example a per-model weekly limit).
    """

    name: str
    used_pct: float | None
    reset: datetime | None
    secondary_pct: float | None = None
    secondary_reset: datetime | None = None
    usable: bool = True


@dataclass(frozen=True)
class Config:
    """Every constant of the algorithm. Defaults: one unit is one percent point of a week."""

    week_hours: float = 168
    units_per_week: float = 100.0
    max_units_per_h: float | None = None
    burn_floor_units: float = 1.0
    secondary_min_room_pct: float = 10
    secondary_drop_pct: float = 20
    min_span_h: float = 6
    history_h: float = 24
    secondary_hold_h: float = 24


DEFAULT = Config()


def _counted(accounts: Sequence[Account]) -> list[Account]:
    return [a for a in accounts if a.usable and a.used_pct is not None and a.reset is not None]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _hours(delta: timedelta) -> float:
    return delta.total_seconds() / 3600


def pace_from_history(
    samples: Sequence[Sample], now: datetime, config: Config = DEFAULT
) -> float | None:
    """Points per hour measured over the last `history_h` hours, None when the span is too short.

    Each sample is (at, account name, used percent). A drop of the percent (a reset) is skipped.
    """
    kept = [
        (s[0], s[1], s[2])
        for s in samples
        if s[2] is not None and timedelta(0) <= now - s[0] <= timedelta(hours=config.history_h)
    ]
    if not kept:
        return None
    span = _hours(max(s[0] for s in kept) - min(s[0] for s in kept))
    if span < config.min_span_h:
        return None
    by_name: dict[str, list[tuple[datetime, float]]] = {}
    for at, name, pct in kept:
        assert pct is not None
        by_name.setdefault(name, []).append((at, pct))
    total = 0.0
    for rows in by_name.values():
        rows.sort(key=lambda r: r[0])
        for (_, a), (_, b) in zip(rows, rows[1:]):
            if b - a > 0:
                total += b - a
    return total / span


def average_pace(accounts: Sequence[Account], now: datetime, config: Config = DEFAULT) -> float:
    """Points per hour averaged over the elapsed part of each account's week."""
    total = 0.0
    for a in _counted(accounts):
        assert a.reset is not None and a.used_pct is not None
        if a.reset <= now:
            continue  # stale reading: the week already refilled, its used_pct says nothing
        left = _hours(a.reset - now)
        elapsed = min(float(config.week_hours), max(1.0, config.week_hours - left))
        total += a.used_pct / elapsed
    return total


def _walk(
    accounts: Sequence[Account], pace: float, now: datetime
) -> list[tuple[Account, float, float, datetime | None]]:
    """Unrounded walk: (account, room, lost, empty_at) in reset order."""
    out: list[tuple[Account, float, float, datetime | None]] = []
    t = now
    for a in sorted(_counted(accounts), key=lambda x: x.reset):  # type: ignore[arg-type,return-value]
        assert a.reset is not None and a.used_pct is not None
        lost = 0.0
        empty_at = None
        if a.reset <= now:
            room = 0.0
        else:
            room = max(0.0, 100 - a.used_pct)
            if pace <= 0:
                lost = room
            else:
                need_h = room / pace
                avail_h = max(0.0, _hours(a.reset - t))
                if need_h <= avail_h:
                    t = t + timedelta(hours=need_h)
                    empty_at = t
                else:
                    lost = room - pace * avail_h
                    t = max(t, a.reset)
        out.append((a, room, lost, empty_at))
    return out


def project(
    accounts: Sequence[Account], pace: float, now: datetime, config: Config = DEFAULT
) -> list[dict[str, Any]]:
    """What each counted account loses at this pace, draining in reset order."""
    out: list[dict[str, Any]] = []
    for a, room, lost, empty_at in _walk(accounts, pace, now):
        assert a.reset is not None and a.used_pct is not None
        out.append(
            {
                "name": a.name,
                "used_pct": round(a.used_pct, 1),
                "reset": _iso(a.reset),
                "room_pct": round(room, 1),
                "lost_pct": round(lost, 1),
                "lost_units": round(lost / 100 * config.units_per_week, 1),
                "empty_at": _iso(empty_at) if empty_at else None,
            }
        )
    return out


def _secondary_qualifies(
    a: Account, now: datetime, previous: dict[str, Any] | None, config: Config
) -> bool:
    assert a.reset is not None and a.used_pct is not None
    if a.secondary_reset is not None:
        diff = abs(_hours(a.secondary_reset - a.reset))
        if diff > 1 and _hours(a.secondary_reset - now) > config.week_hours - 24:
            return True
    if previous and a.secondary_pct is not None:
        for p in previous.get("accounts") or []:
            if p.get("name") != a.name or p.get("secondary_pct") is None:
                continue
            prev_used = p.get("used_pct")
            if (
                p["secondary_pct"] - a.secondary_pct >= config.secondary_drop_pct
                and prev_used is not None
                and a.used_pct >= prev_used
            ):
                return True
    return False


def _secondary_carried(
    name: str, now: datetime, previous: dict[str, Any] | None, config: Config
) -> str | None:
    """The previous secondary_since of `name` when it was secondary_first less than
    `secondary_hold_h` ago."""
    if not previous or previous.get("secondary_first") != name:
        return None
    since = previous.get("secondary_since")
    try:
        at = datetime.fromisoformat(str(since))
    except (TypeError, ValueError):
        return None
    if at.tzinfo is None:
        return None
    held = timedelta(0) <= now - at < timedelta(hours=config.secondary_hold_h)
    return str(since) if held else None


def _clock(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%H:%M")


def decide(
    accounts: Sequence[Account],
    history: Sequence[Sample],
    now: datetime,
    previous: dict[str, Any] | None = None,
    config: Config = DEFAULT,
) -> dict[str, Any]:
    """Project the loss of every account, pick the account to drain first, say burn or normal.

    `previous` is the dict a former call returned; it carries the secondary-first rule over time.
    """
    measured = pace_from_history(history, now, config)
    pace = measured if measured is not None else average_pace(accounts, now, config)
    rows = project(accounts, pace, now, config)
    extra = {a.name: a for a in _counted(accounts)}
    for r in rows:
        acc = extra[r["name"]]
        r["secondary_pct"] = acc.secondary_pct
        r["secondary_reset"] = _iso(acc.secondary_reset) if acc.secondary_reset else None
    lost_units = round(sum(r["lost_units"] for r in rows))
    unavoidable: int | None = None
    if config.max_units_per_h is not None:
        max_pace = config.max_units_per_h / config.units_per_week * 100
        unavoidable = round(sum(r["lost_units"] for r in project(accounts, max_pace, now, config)))
    raw = _walk(accounts, pace, now)
    drain_i = next((i for i, w in enumerate(raw) if w[1] > 0), None)
    drain_first = rows[drain_i]["name"] if drain_i is not None else None

    secondary_first = None
    # the unrounded loss: a loss that rounds to 0.0 point is still a loss
    if drain_i is not None and raw[drain_i][2] == 0:
        cands = [
            r
            for r in rows
            if r["room_pct"] >= config.secondary_min_room_pct
            and (
                _secondary_carried(r["name"], now, previous, config)
                or _secondary_qualifies(extra[r["name"]], now, previous, config)
            )
        ]
        if cands:
            secondary_first = max(cands, key=lambda r: r["room_pct"])["name"]
    secondary_since = None
    if secondary_first:
        secondary_since = _secondary_carried(secondary_first, now, previous, config) or _iso(now)

    state = "burn" if lost_units >= config.burn_floor_units else "normal"
    tail = f", drain {drain_first} first." if drain_first else "."
    if state == "burn":
        losing = [r for r in rows if r["lost_units"] > 0]
        names = ", ".join(r["name"] for r in losing)
        at = _clock(datetime.fromisoformat(losing[0]["reset"]))
        summary = (
            f"Burn: {lost_units} units expire on {names} at the {at} UTC reset "
            f"at {pace:.1f} pts/h{tail}"
        )
    else:
        summary = f"Normal: nothing expires at {pace:.1f} pts/h{tail}"
    return {
        "state": state,
        "drain_first": drain_first,
        "secondary_first": secondary_first,
        "secondary_since": secondary_since,
        "pace_pct_h": round(pace, 2),
        "pace_source": "measured" if measured is not None else "average",
        "lost_units": lost_units,
        "unavoidable_units": unavoidable,
        "accounts": rows,
        "summary": summary,
    }
