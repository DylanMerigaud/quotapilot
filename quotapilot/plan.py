"""Planning of on-demand reset credits: when to spend each one, by forward simulation.

Pure and deterministic: no clock, no randomness, no IO. Every datetime is timezone-aware.

Model (measured on Claude accounts in November 2026): a "full" credit spent at time s sets the
account's weekly used percent to 0 and KEEPS its natural reset date (`credit_semantics`
"keep_date"). With "restart_window" it also moves the reset to s + week_hours, for providers
that restart the window. A full credit also zeroes the secondary window; a "secondary" credit
zeroes the secondary window only (keeping its date), never the weekly one.

The value of a spend is the extra points the pool serves over the horizon with it than without.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence, Tuple

from .core import (
    DEFAULT,
    Account,
    Config,
    Credit,
    Sample,
    _aware,
    _hours,
    _iso,
    _mode,
)

_EPS = 1e-9

Spend = Tuple[str, datetime, str]  # (account name, at, kind)
Win = Tuple[str, float, datetime]  # (account name, used pct, reset)


def pace_profile(
    samples: Sequence[Sample], now: datetime, config: Config = DEFAULT
) -> list[float] | None:
    """Relative demand by UTC hour of day (24 floats, mean 1.0) over the last 7 days.

    Each positive increment of an account's used percent is attributed to the UTC hour of the
    later sample (a drop is a reset and is skipped) and divided by the number of clock hours of
    that hour of day inside the kept span. None when the kept samples span less than
    `profile_min_days` days or when nothing was consumed.
    """
    _aware(now, "now")
    for smp in samples:
        _aware(smp[0], "sample time")
    kept = [
        (s[0], s[1], s[2])
        for s in samples
        if s[2] is not None and timedelta(0) <= now - s[0] <= timedelta(days=7)
    ]
    if not kept:
        return None
    first = min(k[0] for k in kept)
    last = max(k[0] for k in kept)
    if _hours(last - first) < config.profile_min_days * 24:
        return None
    by_name: dict[str, list[tuple[datetime, float]]] = {}
    for at, name, pct in kept:
        assert pct is not None
        by_name.setdefault(name, []).append((at, pct))
    sums = [0.0] * 24
    for rows in by_name.values():
        rows.sort(key=lambda r: r[0])
        for (at_a, a), (at_b, b) in zip(rows, rows[1:]):
            if b - a > 0:
                _spread(sums, at_a, at_b, b - a)
    if sum(sums) <= 0:
        return None
    counts = [0] * 24
    cur = first.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    stop = last.astimezone(timezone.utc)
    while cur <= stop:
        counts[cur.hour] += 1
        cur += timedelta(hours=1)
    known = [sums[h] / counts[h] for h in range(24) if counts[h] > 0]
    fill = sum(known) / len(known)
    rate = [sums[h] / counts[h] if counts[h] > 0 else fill for h in range(24)]
    prof = _renorm(rate)
    floor = config.profile_floor
    for _ in range(24):
        low = [v < floor for v in prof]
        if not any(low):
            break
        # pin the low hours at the floor and rescale the others so the mean stays 1.0
        rest = sum(v for v, lo in zip(prof, low) if not lo)
        scale = (len(prof) - floor * sum(low)) / rest if rest > 0 else 1.0
        prof = [floor if lo else v * scale for v, lo in zip(prof, low)]
    return prof


def _spread(sums: list[float], a: datetime, b: datetime, delta: float) -> None:
    """Add `delta` to the UTC hours between a and b, proportional to the time in each hour."""
    a, b = a.astimezone(timezone.utc), b.astimezone(timezone.utc)
    total = (b - a).total_seconds()
    if total <= 0:
        sums[b.hour] += delta
        return
    cur = a
    while cur < b:
        nxt = min(b, cur.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        sums[cur.hour] += delta * (nxt - cur).total_seconds() / total
        cur = nxt


def _renorm(values: list[float]) -> list[float]:
    mean = sum(values) / len(values)
    return [v / mean for v in values]


class _Sim:
    """What one simulation recorded. Times are hours after `now`."""

    def __init__(self) -> None:
        self.consumed = 0.0
        self.dry_hours = 0.0
        self.dry: list[tuple[float, float]] = []
        self.hits: dict[str, list[float]] = {}
        self.ceiling: dict[str, list[bool]] = {}
        self.lost = 0.0  # natural loss, resets inside the first week
        self.first_loss: dict[str, float] = {}


def _simulate(
    windows: Sequence[Win],
    pace: float,
    now: datetime,
    end: datetime,
    spends: Sequence[Spend],
    profile: Sequence[float] | None,
    config: Config,
    blocked: Callable[[str, datetime], bool] | None = None,
) -> _Sim:
    """Step by step, serve the demand from the live windows in order of soonest reset.

    A window is spent at `spent_pct` (its capacity). `spends` are (account, at, kind) applied at
    the first step whose start is at or after `at`. `blocked(name, step_start)` makes a window
    serve nothing for that step.
    """
    out = _Sim()
    step = config.plan_step_h
    week = float(config.week_hours)
    cap = config.spent_pct
    span = _hours(end - now)
    n = max(0, math.ceil(span / step - 1e-9))
    # window: [name, room, reset offset, bonus]
    ws: list[list[Any]] = []
    for name, used, reset in windows:
        r = _hours(reset - now)
        if r <= 0:
            room = cap
            while r <= 0:
                r += week
        else:
            room = max(0.0, cap - used)
        ws.append([name, room, r, 0.0])
        out.hits[name] = []
        out.ceiling[name] = []
        out.first_loss.setdefault(name, 0.0)
    firsts = {w[0] for w in ws}
    pend = sorted((_hours(at - now), name, kind) for name, at, kind in spends)
    pi = 0
    base = now.astimezone(timezone.utc)
    base_h = base.hour + base.minute / 60 + base.second / 3600
    dry_open: float | None = None
    order = sorted(ws, key=lambda w: (w[2], w[0]))

    def events(t: float) -> bool:
        nonlocal pi
        changed = False
        for w in ws:
            while w[2] <= t + _EPS:
                loss = max(0.0, w[1] - w[3])
                if w[2] <= week + _EPS:
                    out.lost += loss
                if w[0] in firsts:
                    out.first_loss[w[0]] = loss
                    firsts.discard(w[0])
                w[1], w[3] = cap, 0.0
                w[2] += week
                changed = True
        while pi < len(pend) and pend[pi][0] <= t + _EPS:
            _, name, _kind = pend[pi]
            pi += 1
            for w in ws:
                if w[0] == name:
                    w[3] = max(0.0, cap - w[1])
                    w[1] = cap
                    if config.credit_semantics == "restart_window":
                        w[2] = t + week
            changed = True
        return changed

    for i in range(n):
        t0 = i * step
        dt = min(step, span - t0)
        if events(t0) or i == 0:
            order = sorted(ws, key=lambda w: (w[2], w[0]))
        for w in ws:
            out.ceiling[w[0]].append(w[1] <= _EPS)
        before = {w[0]: w[1] for w in ws}
        f = 1.0 if profile is None else profile[int((base_h + t0 + _EPS) % 24)]
        demand = pace * f * dt
        if demand > 0:
            left = demand
            start_dt = now + timedelta(hours=t0) if blocked is not None else now
            for w in order:
                if left <= _EPS:
                    break
                if w[1] <= _EPS or (blocked is not None and blocked(w[0], start_dt)):
                    continue
                take = min(w[1], left)
                w[1] -= take
                left -= take
            if left < _EPS:
                left = 0.0
            out.consumed += demand - left
            out.dry_hours += dt * (left / demand)
            if left > 0.5 * demand:
                if dry_open is None:
                    dry_open = t0
            elif dry_open is not None:
                out.dry.append((dry_open, t0))
                dry_open = None
        elif dry_open is not None:
            out.dry.append((dry_open, t0))
            dry_open = None
        for w in ws:
            if w[1] <= _EPS:
                w[1] = 0.0
                if before[w[0]] > _EPS:
                    out.hits[w[0]].append(t0 + dt)
    if dry_open is not None:
        out.dry.append((dry_open, span))
    events(span)
    return out


def _window(a: Account, secondary: bool) -> Win | None:
    if not a.usable:
        return None
    used, reset = (a.secondary_pct, a.secondary_reset) if secondary else (a.used_pct, a.reset)
    if used is None or reset is None:
        return None
    return a.name, used, reset


class _Track:
    """One simulated pool (main or secondary) and the spends planned so far on it."""

    def __init__(
        self,
        windows: list[Win],
        pace: float,
        profile: Sequence[float] | None,
        now: datetime,
        config: Config,
    ) -> None:
        self.windows = windows
        self.pace = pace
        self.profile = profile
        self.now = now
        self.config = config

    def sim(
        self,
        end: datetime,
        spends: Sequence[Spend],
        blocked: Callable[[str, datetime], bool] | None = None,
    ) -> _Sim:
        return _simulate(
            self.windows, self.pace, self.now, end, spends, self.profile, self.config, blocked
        )


def _full_value(
    track: _Track, c: Credit, at: datetime, planned: Sequence[Spend], end: datetime
) -> float:
    """Points a full credit spent at `at` adds to the points served, given `planned`."""
    base = track.sim(end, planned).consumed
    return track.sim(end, [*list(planned), (c.account, at, "full")]).consumed - base


def _choose(
    track: _Track,
    c: Credit,
    planned: Sequence[Spend],
    end: datetime,
    now_spent: bool,
    kind: str,
    blocked: Callable[[str, datetime], bool] | None,
    zero_pace_reason: str | None,
) -> dict[str, Any]:
    cfg = track.config
    now = track.now
    base = track.sim(end, planned, blocked)
    limit = _hours(c.expires - now) - cfg.plan_expiry_guard_h
    cands: dict[float, str] = {}

    def add(t: float, tag: str) -> None:
        key = round(t, 6)
        if key >= 0 and key not in cands:
            cands[key] = tag

    for s, _ in base.dry:
        if s < limit - _EPS:
            add(s, "dry")
    if now_spent:
        add(0.0, "ceiling")
    for t in base.hits.get(c.account, []):
        if t < limit - _EPS:
            add(t, "ceiling")
    add(0.0, "now")
    for _, at, _k in planned:
        t = _hours(at - now)
        if 0 <= t < limit - _EPS:
            add(t, "after_spend")
    for t in _reset_moments(track, limit):
        add(t, "after_reset")
    if limit > _EPS:
        add(limit, "last")
    cache: dict[float, float] = {}

    def value(t: float) -> float:
        key = round(t, 6)
        if key not in cache:
            spend = (c.account, now + timedelta(hours=key), kind)
            cache[key] = track.sim(end, [*list(planned), spend], blocked).consumed - base.consumed
        return cache[key]

    now_value = value(0.0)
    vals = {t: value(t) for t in cands}
    best = max(vals.values()) if vals else 0.0
    soon = _hours(c.expires - now) <= cfg.credit_expiry_margin_h
    if best < cfg.plan_min_value_pct:
        if soon:
            reason = zero_pace_reason or "nothing_to_recover"
        else:
            reason = zero_pace_reason or "no_gain_at_pace"
        return {
            "decision": "let_expire" if soon else "keep",
            "reason": reason,
            "at": None,
            "value": 0.0,
            "now_value": now_value,
        }
    ties = [t for t, v in vals.items() if v >= max(best - cfg.plan_tie_pct, cfg.plan_min_value_pct)]
    pick = min(ties, key=lambda t: (cands[t] != "dry", t))
    tag = cands[pick]
    reason = {
        "dry": "pool_dry",
        "ceiling": "ceiling_runway",
        "now": "best_now",
        "after_spend": "after_planned_spend",
        "after_reset": "after_natural_reset",
        "last": "before_expiry",
    }[tag]
    decision = "now" if pick <= cfg.plan_now_tol_h else "planned"
    return {
        "decision": decision,
        "reason": reason,
        "at": now + timedelta(hours=pick),
        "value": now_value if decision == "now" else vals[pick],
        "now_value": now_value,
    }


def _reset_moments(track: _Track, limit: float) -> list[float]:
    """The step-grid moments at or after each natural reset of any window, before `limit`."""
    cfg = track.config
    step, week = cfg.plan_step_h, float(cfg.week_hours)
    out: list[float] = []
    for _, _, reset in track.windows:
        r = _hours(reset - track.now)
        while r <= 0:
            r += week
        while r < limit - _EPS:
            out.append(math.ceil(r / step - 1e-9) * step)
            r += week
    return out


def _next_reset_h(reset: datetime, at: datetime, config: Config) -> float:
    if config.credit_semantics == "restart_window":
        return float(config.week_hours)
    week = timedelta(hours=config.week_hours)
    r = reset
    while r <= at:
        r += week
    return _hours(r - at)


def plan_credits(
    accounts: Sequence[Account],
    credits: Sequence[Credit],
    pace: float,
    now: datetime,
    profile: Sequence[float] | None = None,
    secondary_pace: float | None = None,
    mode: str | None = None,
    config: Config = DEFAULT,
) -> dict[str, Any]:
    """Plan when to spend each live on-demand reset credit, by forward simulation.

    The pool drains at `pace` points per hour (shaped by `profile`, 24 UTC hourly factors of
    mean 1.0) from the window with the soonest reset. The value of a spend is the extra points
    served over the horizon. Credits are planned in order of expiry and each plan counts the
    spends planned before it. `mode` is only passed through as `input_mode`.
    """
    _aware(now, "now")
    for acc in accounts:
        _aware(acc.reset, f"{acc.name} reset")
        _aware(acc.secondary_reset, f"{acc.name} secondary_reset")
    for c in credits:
        _aware(c.expires, f"credit {c.id} expires")
        if c.kind not in ("full", "secondary"):
            raise ValueError(f"unknown credit kind: {c.kind!r}")
    if pace is None or pace < 0:
        raise ValueError("pace must be a number of points per hour, zero or more")
    if secondary_pace is not None and secondary_pace < 0:
        raise ValueError("secondary_pace must be zero or more")
    if profile is not None and len(profile) != 24:
        raise ValueError("profile must hold 24 hourly factors")
    if config.credit_semantics not in ("keep_date", "restart_window"):
        raise ValueError(f"unknown credit_semantics: {config.credit_semantics!r}")
    by_name = {a.name: a for a in accounts}
    main_wins = [w for w in (_window(a, False) for a in accounts) if w]
    main = _Track(main_wins, pace, profile, now, config)
    sec_wins = [w for w in (_window(a, True) for a in accounts) if w]
    sec = None
    if secondary_pace is not None:
        sec = _Track(sec_wins, secondary_pace, profile, now, config)
    live = sorted(
        (c for c in credits if c.left > 0 and c.expires > now),
        key=lambda x: (x.expires, x.account, x.id),
    )
    planned_full: list[Spend] = []
    planned_all: list[Spend] = []
    rows: list[dict[str, Any]] = []
    horizon = timedelta(hours=config.plan_horizon_h)
    week = timedelta(hours=config.week_hours)
    for c in live:
        a = by_name.get(c.account)
        full = c.kind == "full"
        row: dict[str, Any] = {
            "account": c.account,
            "id": c.id,
            "kind": c.kind,
            "expires": _iso(c.expires),
            "decision": "unknown",
            "planned_at": None,
            "value_pct": 0.0,
            "now_value_pct": None,
            "reason": "account_unread",
            "hours_to_reset_at_plan": None,
            "hours_to_expiry": round(_hours(c.expires - now), 1),
        }
        rows.append(row)
        win = _window(a, not full) if a is not None else None
        if win is None:
            continue
        if not c.usable:
            row.update(decision="keep", reason="not_usable")
            continue
        if not full and sec is None:
            row.update(decision="fallback", reason="no_secondary_history")
            continue
        end = min(now + horizon, c.expires + week)
        assert a is not None
        if full:
            track = main
            blocked = None
            zero = None
            now_spent = win[1] >= config.spent_pct
            spends_in = planned_full
        else:
            assert sec is not None
            track = sec
            ceil_sim = main.sim(end, planned_full)

            def blocked(name: str, t: datetime, _s: _Sim = ceil_sim) -> bool:
                mask = _s.ceiling.get(name)
                if not mask:
                    return False
                i = min(len(mask) - 1, max(0, round(_hours(t - now) / config.plan_step_h)))
                return mask[i]

            zero = "no_secondary_work" if secondary_pace == 0 else None
            now_spent = win[1] >= config.spent_pct
            spends_in = planned_all
        res = _choose(track, c, spends_in, end, now_spent, c.kind, blocked, zero)
        at = res["at"]
        row.update(
            decision=res["decision"],
            reason=res["reason"],
            planned_at=_iso(at) if at else None,
            value_pct=round(res["value"], 1),
            now_value_pct=round(res["now_value"], 1),
        )
        if at is not None:
            _, _, reset = win
            row["hours_to_reset_at_plan"] = round(_next_reset_h(reset, at, config), 1)
            planned_all.append((c.account, at, c.kind))
            if full:
                planned_full.append((c.account, at, "full"))
    return {
        "credits": rows,
        "forecast": _week(main, planned_full, rows, now, config),
        "semantics": config.credit_semantics,
        "pace_pct_h": pace,
        "input_mode": mode,
    }


def _week(
    main: _Track,
    planned_full: Sequence[Spend],
    rows: Sequence[dict[str, Any]],
    now: datetime,
    config: Config,
) -> dict[str, Any]:
    end = now + timedelta(hours=config.week_hours)
    inside = [s for s in planned_full if s[1] < end]
    base = main.sim(end, [])
    withc = main.sim(end, inside)

    def mode_of(s: _Sim) -> str:
        return _mode(s.lost, s.dry_hours, config)

    covered_by = [
        r["id"]
        for r in rows
        if r["kind"] == "full"
        and r["planned_at"] is not None
        and datetime.fromisoformat(r["planned_at"]) < end
    ]
    dry_at = dry_until = None
    if withc.dry:
        dry_at = _iso(now + timedelta(hours=withc.dry[0][0]))
        dry_until = _iso(now + timedelta(hours=withc.dry[0][1]))
    return {
        "mode": mode_of(withc),
        "dry_at": dry_at,
        "dry_until": dry_until,
        "dry_hours": round(withc.dry_hours, 1),
        "baseline_mode": mode_of(base),
        "baseline_dry_hours": round(base.dry_hours, 1),
        "covered_hours": round(max(0.0, base.dry_hours - withc.dry_hours), 1),
        "covered_by": covered_by,
        "lost_units": round(withc.lost / 100 * config.units_per_week, 1),
    }
