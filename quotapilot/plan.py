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
        # points each spend gave back, by the spend's index in the `spends` given
        self.cleared: dict[int, float] = {}


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
    pend = sorted((_hours(at - now), name, kind, i) for i, (name, at, kind) in enumerate(spends))
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
            _, name, _kind, idx = pend[pi]
            pi += 1
            for w in ws:
                if w[0] == name:
                    w[3] = max(0.0, cap - w[1])
                    out.cleared[idx] = w[3]
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


_REASONS = {
    "dry": "pool_dry",
    "ceiling": "ceiling_runway",
    "now": "best_now",
    "after_spend": "after_planned_spend",
    "after_reset": "after_natural_reset",
    "last": "before_expiry",
}


def _pick(
    pool: Sequence[float],
    vals: dict[float, float],
    dry_start: float | None,
    cfg: Config,
) -> float:
    """Among the moments of `pool`, the ones within `plan_tie_pct` of the best value are ties.

    A dry stretch that starts now and a tied moment now: the earliest tied moment. Else the
    latest tied moment at or before the dry start (the option stays open), else the earliest
    tied moment. With no dry stretch at all, the latest tied moment.
    """
    best = max(vals[t] for t in pool)
    ties = sorted(t for t in pool if vals[t] >= best - cfg.plan_tie_pct)
    if dry_start is None:
        return ties[-1]
    if dry_start <= cfg.plan_now_tol_h and ties[0] <= cfg.plan_now_tol_h:
        return ties[0]
    before = [t for t in ties if t <= dry_start + _EPS]
    return before[-1] if before else ties[0]


def _choose(
    track: _Track,
    c: Credit,
    planned: Sequence[Spend],
    end: datetime,
    now_spent: bool,
    kind: str,
    blocked: Callable[[str, datetime], bool] | None,
) -> dict[str, Any]:
    """Pick when to spend `c` on `track`, or why not (the rules are in `plan_credits`)."""
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
    cache: dict[float, tuple[float, float]] = {}

    def value(t: float) -> tuple[float, float]:
        """(value, clears) of spending at `t`."""
        key = round(t, 6)
        if key not in cache:
            spend = (c.account, now + timedelta(hours=key), kind)
            sim = track.sim(end, [*list(planned), spend], blocked)
            cache[key] = (sim.consumed - base.consumed, sim.cleared.get(len(planned), 0.0))
        return cache[key]

    now_value = value(0.0)[0]
    vals = {t: value(t)[0] for t in cands}
    minimum = cfg.plan_min_value_pct
    dry_start = base.dry[0][0] if base.dry else None

    def result(
        decision: str, reason: str, t: float | None, data_t: float | None = None
    ) -> dict[str, Any]:
        """The answer; the value, what it clears and its share are those of `data_t`."""
        out: dict[str, Any] = {
            "decision": decision,
            "reason": reason,
            "at": None if t is None else now + timedelta(hours=t),
            "value": 0.0,
            "now_value": now_value,
            "clears": None,
            "share": None,
        }
        d = t if data_t is None else data_t
        if d is not None:
            out["value"], out["clears"] = vals[d], value(d)[1]
            out["share"] = _share(vals[d], out["clears"])
        return out

    if _hours(c.expires - now) <= cfg.credit_expiry_margin_h:
        # inside the margin the share is waived: a spend worth the minimum is never wasted
        if now_value >= minimum:
            return result("now", "before_expiry", 0.0)
        eligible = [t for t in vals if vals[t] >= minimum]
        if not eligible:
            return result("let_expire", "nothing_to_recover", None)
    else:
        # outside the margin: worth the minimum AND a fair share of what the spend clears
        floor = cfg.credit_min_value_share - _EPS
        eligible = [t for t in vals if vals[t] >= minimum and _share(vals[t], value(t)[1]) >= floor]
        if not eligible:
            best_t = max(vals, key=lambda t: (vals[t], -t))
            if round(vals[best_t], 1) > 0:
                return result("keep", "low_share", None, best_t)
            return result("keep", "pool_fine" if not base.dry else "no_gain_at_pace", None)
    pick = _pick(eligible, vals, dry_start, cfg)
    if pick <= cfg.plan_now_tol_h:
        return result("now", _REASONS[cands[pick]], pick)
    return result("planned", _REASONS[cands[pick]], pick)


def _share(value: float, clears: float) -> float:
    return value / clears if clears > _EPS else 0.0


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
    previous: Sequence[dict[str, Any]] | None = None,
    config: Config = DEFAULT,
) -> dict[str, Any]:
    """Plan when to spend each live on-demand reset credit, by forward simulation.

    The pool drains at `pace` points per hour (shaped by `profile`, 24 UTC hourly factors of
    mean 1.0) from the window with the soonest reset. The value of a spend is the extra points
    served over the horizon; what it CLEARS is the points it gives back (the window's used
    points at that moment); its share is value over clears. Credits are planned in order of
    expiry and each plan counts the spends planned before it.

    A full credit, in order. Not usable: keep. Outside `credit_expiry_margin_h` of its expiry
    and the pool in surplus (`mode` is "surplus", or none is passed and the plan's own one week
    baseline is surplus): keep `pool_surplus`. Outside the margin a moment is eligible when its
    value is at least `plan_min_value_pct` and its share at least `credit_min_value_share`;
    none eligible is `keep low_share` when something above 0 could be recovered, else
    `keep pool_fine` when the pool is never dry, else `keep no_gain_at_pace`. Inside the margin
    the share is waived: a now value of at least the minimum is `now before_expiry`, else the
    best later moment worth the minimum is planned, else `let_expire`. Among eligible moments
    those within `plan_tie_pct` of the best tie: a dry stretch starting now wins now, else the
    latest tie at or before the dry start (the option stays open), else the earliest.

    A secondary credit, in order: not usable keep; no `secondary_pace` fallback; a zero pace
    `keep no_secondary_work`; its window not spent `keep not_spent`; another account with
    secondary and weekly room `keep switch_instead`; its own weekly room under
    `secondary_min_room_pct` `keep weekly_spent`; then the same rules on the secondary pool.

    Each row carries `clears_pct`, `share` and, for a `now`, `now_since`: kept from the same
    credit in `previous` (the rows of an earlier call) when it was already `now`, else now.
    `mode` is also passed through as `input_mode`.
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
    surplus: bool | None = None
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
            "clears_pct": None,
            "share": None,
            "now_since": None,
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
        assert a is not None
        if full:
            if _hours(c.expires - now) > config.credit_expiry_margin_h:
                if surplus is None:
                    surplus = (mode or _baseline_mode(main, now, config)) == "surplus"
                if surplus:
                    row.update(decision="keep", reason="pool_surplus")
                    continue
        else:
            gate = _secondary_gate(a, accounts, secondary_pace, now, config)
            if gate is not None:
                row.update(decision="keep", reason=gate)
                continue
        end = min(now + horizon, c.expires + week)
        if full:
            track = main
            blocked = None
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

            now_spent = win[1] >= config.spent_pct
            spends_in = planned_all
        res = _choose(track, c, spends_in, end, now_spent, c.kind, blocked)
        at = res["at"]
        reason = res["reason"]
        if not full and res["decision"] == "now":
            reason = "secondary_spent_weekly_room"
        row.update(
            decision=res["decision"],
            reason=reason,
            planned_at=_iso(at) if at else None,
            value_pct=round(res["value"], 1),
            now_value_pct=round(res["now_value"], 1),
            clears_pct=None if res["clears"] is None else round(res["clears"], 1),
            share=None if res["share"] is None else round(res["share"], 2),
        )
        if at is not None:
            _, _, reset = win
            row["hours_to_reset_at_plan"] = round(_next_reset_h(reset, at, config), 1)
            planned_all.append((c.account, at, c.kind))
            if full:
                planned_full.append((c.account, at, "full"))
        if res["decision"] == "now":
            row["now_since"] = _now_since(row, previous, now)
    return {
        "credits": rows,
        "forecast": _week(main, planned_full, rows, now, config),
        "semantics": config.credit_semantics,
        "pace_pct_h": pace,
        "input_mode": mode,
    }


def _baseline_mode(main: _Track, now: datetime, config: Config) -> str:
    """The mode of the main pool's own one week baseline (no credit spent)."""
    sim = main.sim(now + timedelta(hours=config.week_hours), [])
    return _mode(sim.lost, sim.dry_hours, config)


def _now_since(
    row: dict[str, Any], previous: Sequence[dict[str, Any]] | None, now: datetime
) -> str:
    """When this `now` began: the earlier call's `now_since` if the same credit was already now."""
    for p in previous or []:
        if (p.get("account"), p.get("id")) != (row["account"], row["id"]):
            continue
        since = p.get("now_since")
        if p.get("decision") != "now" or not isinstance(since, str):
            continue
        try:
            parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and parsed <= now:
                return _iso(parsed)
        except (TypeError, ValueError):
            pass
    return _iso(now)


def _secondary_gate(
    a: Account,
    accounts: Sequence[Account],
    secondary_pace: float | None,
    now: datetime,
    config: Config,
) -> str | None:
    """The reason a secondary credit is kept before any simulation, or None to simulate."""
    cap = config.spent_pct
    if secondary_pace == 0:
        return "no_secondary_work"
    assert a.secondary_pct is not None and a.secondary_reset is not None
    if a.secondary_pct < cap and a.secondary_reset > now:
        return "not_spent"
    for o in accounts:
        if o.name == a.name or not o.usable or o.secondary_pct is None or o.secondary_reset is None:
            continue
        secondary_room = o.secondary_pct < cap or o.secondary_reset <= now
        weekly_room = (
            o.used_pct is not None and o.reset is not None and (o.used_pct < cap or o.reset <= now)
        )
        if secondary_room and weekly_room:
            return "switch_instead"
    if a.used_pct is not None and a.reset is not None:
        room = cap if a.reset <= now else cap - a.used_pct
        if room < config.secondary_min_room_pct:
            return "weekly_spent"
    return None


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
