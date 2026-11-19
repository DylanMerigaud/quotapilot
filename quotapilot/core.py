"""Pure core of quotapilot: stdlib only, every datetime timezone-aware, any zone accepted."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence, Tuple

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
class Credit:
    """A provider-granted, on-demand reset of an account's windows.

    `kind` "full" clears the main (weekly) window, "secondary" clears the secondary window only.
    """

    account: str
    id: str
    kind: str
    expires: datetime
    left: int = 1
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
    short_min_h: float = 1.0
    credit_expiry_margin_h: float = 24
    credit_min_lead_h: float = 24
    spent_pct: float = 99.0
    plan_step_h: float = 0.25
    plan_horizon_h: float = 672  # 28 days
    plan_min_value_pct: float = 1.0  # a spend recovering less is never planned
    plan_tie_pct: float = 0.5  # candidates within this of the best are ties
    plan_now_tol_h: float = 0.25  # planned within this of now means "now"
    plan_expiry_guard_h: float = 1.0  # the last chance candidate is expiry minus this
    profile_min_days: float = 3.0
    profile_floor: float = 0.05
    credit_semantics: str = "keep_date"  # or "restart_window"


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


_EPS = 1e-9


class _Window:
    """One quota window of the forecast: its room, its next reset and the first window's facts."""

    def __init__(self, account: Account, room: float, reset: datetime) -> None:
        self.account = account
        self.room = room
        self.reset = reset
        self.first_reset = reset
        self.refills = 0
        self.lost = 0.0
        self.empty_at: datetime | None = None


def _forecast_raw(
    accounts: Sequence[Account], pace: float, now: datetime, config: Config
) -> tuple[list[_Window], float, datetime | None, datetime | None, float]:
    """Walk one week: (windows, total lost points, dry_at, dry_until, dry hours)."""
    week = timedelta(hours=config.week_hours)
    end = now + week
    wins: list[_Window] = []
    for a in sorted(_counted(accounts), key=lambda x: x.reset):  # type: ignore[arg-type,return-value]
        assert a.reset is not None and a.used_pct is not None
        if a.reset > now:
            wins.append(_Window(a, max(0.0, 100 - a.used_pct), a.reset))
        else:
            reset = a.reset
            while reset <= now:
                reset += week
            wins.append(_Window(a, 100.0, reset))
    wins.sort(key=lambda w: w.reset)
    for w in wins:
        if w.room <= _EPS:
            w.empty_at = now
    t = now
    lost = 0.0
    dry_at: datetime | None = None
    dry_until: datetime | None = None
    dry_h = 0.0
    if not wins and pace > 0:
        return wins, 0.0, now, end, config.week_hours
    while t < end and wins:
        nxt = min(w.reset for w in wins)
        live = [w for w in wins if w.room > _EPS]
        if live and pace > 0:
            w = min(live, key=lambda x: x.reset)
            h_next = _hours(min(nxt, end) - t)
            need = w.room / pace
            h = min(need, h_next)
            t2 = t + timedelta(hours=h)
            w.room = 0.0 if need <= h_next else max(0.0, w.room - pace * h)
            if w.room <= _EPS and w.refills == 0 and w.empty_at is None:
                w.empty_at = t2
        else:
            t2 = min(nxt, end)
            if pace > 0 and t2 > t:
                dry_h += _hours(t2 - t)
                if dry_at is None:
                    dry_at, dry_until = t, t2
                elif dry_until == t:
                    dry_until = t2
        t = t2
        for w in wins:
            if w.reset <= t:
                if w.refills == 0:
                    w.lost = w.room
                lost += w.room
                w.room = 100.0
                w.reset += week
                w.refills += 1
    return wins, lost, dry_at, dry_until, dry_h


def _mode(lost_pts: float, dry_h: float, config: Config) -> str:
    if lost_pts / 100 * config.units_per_week >= config.burn_floor_units:
        return "surplus"
    return "short" if dry_h >= config.short_min_h else "on_track"


def forecast(
    accounts: Sequence[Account], pace: float, now: datetime, config: Config = DEFAULT
) -> dict[str, Any]:
    """One week ahead at `pace` points per hour, every window refilling at its reset.

    The live window with the soonest reset is drained first. Mode is `surplus` when the lost
    units reach `burn_floor_units`, else `short` when the pool is dry for `short_min_h` hours or
    more, else `on_track`. A reading only: nothing is capped or throttled.
    """
    wins, lost, dry_at, dry_until, dry_h = _forecast_raw(accounts, pace, now, config)
    rows: list[dict[str, Any]] = []
    for w in wins:
        assert w.account.used_pct is not None
        used = w.account.used_pct if w.account.reset and w.account.reset > now else 0.0
        left_h = _hours(w.first_reset - now)
        week_pct = max(0.0, min(100.0, 100 * (1 - left_h / config.week_hours)))
        rows.append(
            {
                "name": w.account.name,
                "used_pct": round(used, 1),
                "week_pct": round(week_pct, 1),
                "projected_pct": round(100 - w.lost, 1),
                "lost_pct": round(w.lost, 1),
                "empty_at": _iso(w.empty_at) if w.empty_at else None,
                "reset": _iso(w.first_reset),
            }
        )
    return {
        "mode": _mode(lost, dry_h, config),
        "lost_units": round(lost / 100 * config.units_per_week, 1),
        "dry_at": _iso(dry_at) if dry_at else None,
        "dry_until": _iso(dry_until) if dry_until else None,
        "dry_hours": round(dry_h, 1),
        "horizon": _iso(now + timedelta(hours=config.week_hours)),
        "pace_pct_h": round(pace, 2),
        "accounts": rows,
    }


def pace_band(
    accounts: Sequence[Account], now: datetime, config: Config = DEFAULT
) -> tuple[float | None, float | None]:
    """(floor, ceiling) in points per hour: the lowest pace that is not `surplus` and the highest
    pace that is not `short`. (None, None) when no account is counted."""
    counted = _counted(accounts)
    if not counted:
        return None, None
    top = 100.0 * max(1, len(counted))

    def surplus(p: float) -> bool:
        _, lost, _, _, dry = _forecast_raw(accounts, p, now, config)
        return _mode(lost, dry, config) == "surplus"

    def short(p: float) -> bool:
        return _forecast_raw(accounts, p, now, config)[4] >= config.short_min_h

    floor = 0.0
    if surplus(0.0):
        lo, hi = 0.0, top
        for _ in range(40):
            mid = (lo + hi) / 2
            if surplus(mid):
                lo = mid
            else:
                hi = mid
        floor = hi
    ceiling = top
    if short(top):
        lo, hi = 0.0, top
        for _ in range(40):
            mid = (lo + hi) / 2
            if short(mid):
                hi = mid
            else:
                lo = mid
        ceiling = lo
    return round(floor, 2), round(ceiling, 2)


def _aware(dt: datetime | None, what: str) -> None:
    if dt is not None and dt.tzinfo is None:
        raise ValueError(f"{what} must be timezone-aware")


def _left_h(at: datetime | None, now: datetime) -> float | None:
    return None if at is None else round(max(0.0, _hours(at - now)), 1)


def _credit_verdict(
    c: Credit,
    used: float | None,
    sec: float | None,
    window_reset: datetime | None,
    unread: bool,
    now: datetime,
    mode: str | None,
    demand: Mapping[str, bool] | None,
    config: Config,
) -> tuple[str, str]:
    """(verdict, reason) of one credit, first matching rule wins."""
    full = c.kind == "full"
    gain = used if full else sec
    if unread:
        return "unknown", "account_unread"
    if not c.usable:
        return "wait", "not_usable"
    if _hours(c.expires - now) <= config.credit_expiry_margin_h:
        if gain is not None and gain >= 1:
            return "now", "expires_soon"
        return "wait", "expires_no_gain"
    lead_short = window_reset is not None and _hours(window_reset - now) < config.credit_min_lead_h
    if full:
        assert used is not None
        if used < config.spent_pct:
            return "wait", "not_spent"
        if lead_short:
            return "wait", "natural_reset_soon"
        if mode == "surplus":
            return "wait", "pool_surplus"
        return "now", "spent_far_reset"
    if sec is None:
        return "wait", "no_window"
    if sec < config.spent_pct:
        return "wait", "not_spent"
    if used is None or 100 - used < config.secondary_min_room_pct:
        return "wait", "weekly_spent"
    if lead_short:
        return "wait", "natural_reset_soon"
    if demand is not None and not demand.get(c.account, False):
        return "wait", "no_demand"
    return "now", "secondary_spent_weekly_room"


def advise_credits(
    accounts: Sequence[Account],
    credits: Sequence[Credit],
    now: datetime,
    mode: str | None = None,
    demand: Mapping[str, bool] | None = None,
    config: Config = DEFAULT,
) -> list[dict[str, Any]]:
    """Say for each on-demand reset credit whether to spend it now or wait, and why.

    A credit is worth spending when its window is spent and would otherwise stay empty for long,
    or when it is about to expire with something to clear. `mode` is a `forecast` mode: a
    `surplus` pool has quota to spare, so a full reset is not needed. `demand` maps an account
    name to whether work is waiting for it (None: not checked). A natural reset at or before
    `now` counts as refilled. Credits with nothing left or already expired are skipped.
    """
    _aware(now, "now")
    for acc in accounts:
        _aware(acc.reset, f"{acc.name} reset")
        _aware(acc.secondary_reset, f"{acc.name} secondary_reset")
    for c in credits:
        _aware(c.expires, f"credit {c.id} expires")
        if c.kind not in ("full", "secondary"):
            raise ValueError(f"unknown credit kind: {c.kind!r}")
    by_name = {acc.name: acc for acc in accounts}
    live = [c for c in credits if c.left > 0 and c.expires > now]
    out: list[dict[str, Any]] = []
    for c in sorted(live, key=lambda x: (x.expires, x.account, x.id)):
        a = by_name.get(c.account)
        full = c.kind == "full"
        used: float | None = None
        sec: float | None = None
        reset = sec_reset = None
        if a is not None:
            reset, sec_reset = a.reset, a.secondary_reset
            used = a.used_pct
            if used is not None and reset is not None and reset <= now:
                used = 0.0
            sec = a.secondary_pct
            if sec is not None and sec_reset is not None and sec_reset <= now:
                sec = 0.0
        gain_src = used if full else sec
        window_reset = reset if full else sec_reset
        unread = a is None or not a.usable or (full and used is None)
        gain = 0.0 if unread or gain_src is None else gain_src
        verdict, reason = _credit_verdict(
            c, used, sec, window_reset, unread, now, mode, demand, config
        )
        out.append(
            {
                "account": c.account,
                "id": c.id,
                "kind": c.kind,
                "expires": _iso(c.expires),
                "verdict": verdict,
                "reason": reason,
                "gain_pct": round(gain, 1),
                "hours_to_expiry": round(_hours(c.expires - now), 1),
                "hours_to_reset": None if unread else _left_h(window_reset, now),
            }
        )
    return out
