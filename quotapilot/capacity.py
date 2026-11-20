"""The cross account view of a pool: how much it can serve against what is asked of it.

Pure and deterministic: no clock, no IO. Every datetime is timezone-aware. A reading only:
nothing here caps, throttles or cancels anything.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from .core import DEFAULT, Account, Config, Sample, _aware, _counted, _iso, forecast


def _short(unused: float) -> float | None:
    return round(-unused, 2) if unused < 0 else None


def capacity_view(
    accounts: Sequence[Account],
    pace: float,
    now: datetime,
    prices: Mapping[str, float] | None = None,
    prior_pace: float | None = None,
    expired: Mapping[str, float] | None = None,
    ends: Mapping[str, datetime] | None = None,
    config: Config = DEFAULT,
) -> dict[str, Any]:
    """What the counted accounts can serve in points an hour, against the demand `pace`.

    Capacity is the counted accounts times `spent_pct` over `week_hours`. The unused points an
    hour are capacity minus pace, and a week of them in accounts' worth. The verdict is `short`
    when the pace is above capacity; `drop_one` when a whole account's week is unused now and at
    `prior_pace` too (two weeks running; `drop` names the account of `expired` that let the most
    room expire, with what `prices` say it saves a month); `consume_more` when a whole account's
    week is unused for one week only, or the week forecast loses at least `burn_floor_units`;
    else `balanced`. `after` repeats the picture, for each date of `ends` in the future, without
    the accounts ended by then.
    """
    _aware(now, "now")
    if pace is None or pace < 0:
        raise ValueError("pace must be a number of points per hour, zero or more")
    if prior_pace is not None and prior_pace < 0:
        raise ValueError("prior_pace must be zero or more")
    for name, end in (ends or {}).items():
        _aware(end, f"{name} end")
    counted = _counted(accounts)
    cap = config.spent_pct
    week = float(config.week_hours)
    raw = len(counted) * cap / week
    unused = round(raw - pace, 2)
    unused_week = round(unused * week)
    verdict = "short" if unused < 0 else "balanced"  # no counted account: short when pace > 0
    weeks_over = 0
    drop: dict[str, Any] | None = None
    lost = forecast(accounts, pace, now, config)["lost_units"] if counted else 0.0
    if unused >= 0:
        if unused_week >= cap:
            weeks_over = 1
            if prior_pace is not None and round(round(raw - prior_pace, 2) * week) >= cap:
                verdict, weeks_over = "drop_one", 2
                drop = _drop(counted, expired, prices)
            else:
                verdict = "consume_more"
        elif lost >= config.burn_floor_units:
            verdict = "consume_more"
    return _view(
        len(counted),
        round(raw, 2),
        pace,
        unused,
        unused_week,
        round(unused_week / cap, 2),
        lost,
        verdict,
        weeks_over,
        drop,
        _after(counted, pace, now, ends, config),
    )


def _view(
    counted: int,
    capacity: float,
    pace: float,
    unused: float,
    unused_week: int,
    worth: float,
    lost: float,
    verdict: str,
    weeks_over: int,
    drop: dict[str, Any] | None,
    after: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "counted": counted,
        "capacity_pct_h": capacity,
        "pace_pct_h": pace,
        "unused_pct_h": unused,
        "unused_week_pct": unused_week,
        "accounts_worth": worth,
        "lost_units": lost,
        "verdict": verdict,
        "short_by_pct_h": _short(unused),
        "weeks_over": weeks_over,
        "drop": drop,
        "after": after,
    }


def _drop(
    counted: Sequence[Account],
    expired: Mapping[str, float] | None,
    prices: Mapping[str, float] | None,
) -> dict[str, Any] | None:
    names = {a.name for a in counted}
    rows = [(pts, name) for name, pts in (expired or {}).items() if name in names]
    if not rows:
        return None
    pts, name = min(rows, key=lambda r: (-r[0], r[1]))
    price = (prices or {}).get(name)
    return {"account": name, "expired_pct": pts, "saves_per_month": price}


def _after(
    counted: Sequence[Account],
    pace: float,
    now: datetime,
    ends: Mapping[str, datetime] | None,
    config: Config,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for date in sorted({e for e in (ends or {}).values() if e > now}):
        gone = {n for n, e in (ends or {}).items() if e <= date}
        left = [a for a in counted if a.name not in gone]
        raw = len(left) * config.spent_pct / config.week_hours
        unused = round(raw - pace, 2)
        out.append(
            {
                "at": _iso(date),
                "without": sorted(n for n in gone if any(a.name == n for a in counted)),
                "capacity_pct_h": round(raw, 2),
                "unused_pct_h": unused,
                "verdict": "short" if unused < 0 else "balanced",
                "short_by_pct_h": _short(unused),
            }
        )
    return out


def expired_room(
    samples: Sequence[Sample],
    now: datetime,
    hours: float = 336,
    config: Config = DEFAULT,
) -> dict[str, float]:
    """Points each account let expire: at every drop of its percent (a reset) in the last
    `hours` hours, `spent_pct` minus the last percent before the drop, never negative.
    Accounts with no drop are absent."""
    _aware(now, "now")
    by_name: dict[str, list[tuple[datetime, float]]] = {}
    for at, name, pct in samples:
        _aware(at, "sample time")
        if pct is not None and timedelta(0) <= now - at <= timedelta(hours=hours):
            by_name.setdefault(name, []).append((at, pct))
    out: dict[str, float] = {}
    for name, rows in by_name.items():
        rows.sort(key=lambda r: r[0])
        total, seen = 0.0, False
        for (_, a), (_, b) in zip(rows, rows[1:]):
            if b < a:
                total += max(0.0, config.spent_pct - a)
                seen = True
        if seen:
            out[name] = round(total, 1)
    return out
