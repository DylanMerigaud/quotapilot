"""quotapilot plan_credits and pace_profile: when to spend an on-demand reset, by simulation."""

import random
from datetime import datetime, timedelta, timezone

import pytest

from quotapilot import Account, Config, Credit, pace_profile, plan_credits
from quotapilot.plan import _full_value, _Track, _window

UTC = timezone.utc
NOW = datetime(2026, 11, 19, 0, 0, tzinfo=UTC)
CFG = Config()


def h(n):
    return NOW + timedelta(hours=n)


def off(row):
    return (datetime.fromisoformat(row["planned_at"]) - NOW).total_seconds() / 3600


def plan(accounts, credits, pace, **kw):
    return plan_credits(accounts, credits, pace, NOW, **kw)


def first(out):
    return out["credits"][0]


def full(exp, **kw):
    return Credit("A", "c", "full", h(exp), **kw)


def test_a_ceiling_far_reset_spends_now():
    r = first(plan([Account("A", 100, h(96))], [full(300)], 1.5))
    assert r["decision"] == "now" and r["reason"] == "pool_dry"
    assert r["value_pct"] == pytest.approx(99, abs=0.5)
    assert off(r) <= 0.25


def test_b_natural_reset_soon_plans_next_week():
    r = first(plan([Account("A", 100, h(3))], [full(300)], 1.5))
    assert r["now_value_pct"] == pytest.approx(4.5, abs=0.5)
    assert r["decision"] == "planned"
    assert off(r) > 3 and r["value_pct"] > 4.5
    print("b", r["decision"], off(r), r["value_pct"])


def test_c_waits_until_b_is_drained():
    accounts = [Account("A", 100, h(100)), Account("B", 39, h(150))]
    r = first(plan(accounts, [full(300)], 2))
    assert r["decision"] == "planned" and r["reason"] == "pool_dry"
    assert off(r) == pytest.approx(30, abs=0.5)
    assert r["now_value_pct"] == pytest.approx(r["value_pct"], abs=0.5)
    print("c", r["decision"], off(r), r["value_pct"])


def test_d_expiring_with_the_pool_running_dry():
    accounts = [Account("A", 80, h(120)), Account("B", 100, h(300))]
    r = first(plan(accounts, [full(20)], 2))
    assert r["decision"] in ("planned", "now") and r["value_pct"] >= 1
    assert off(r) < 19.01


def test_e_nothing_to_recover():
    accounts = [Account("A", 10, h(300)), Account("B", 100, h(300))]
    accounts[1] = Account("B", 0, h(300))
    r = first(plan(accounts, [full(20)], 1))
    assert (r["decision"], r["reason"]) == ("let_expire", "nothing_to_recover")
    assert r["planned_at"] is None and r["value_pct"] == 0


def test_f_secondary():
    acc = [Account("A", 10, h(100), 100, h(100))]
    sc = Credit("A", "s", "secondary", h(200))
    r = first(plan(acc, [sc], 1, secondary_pace=0))
    assert r["reason"] == "no_secondary_work" and r["decision"] in ("let_expire", "keep")
    r = first(plan(acc, [sc], 1, secondary_pace=None))
    assert (r["decision"], r["reason"]) == ("fallback", "no_secondary_history")
    r = first(plan(acc, [sc], 1, secondary_pace=1.0))
    assert r["decision"] == "now" and r["value_pct"] > 50


def test_f2_secondary_blocked_by_weekly_ceiling():
    acc = [Account("A", 100, h(100), 100, h(100))]
    sc = Credit("A", "s", "secondary", h(400))
    r = first(plan(acc, [sc], 1, secondary_pace=1.0))
    # weekly is at its ceiling for 100 h: the secondary reset is worth nothing until it refills
    assert r["decision"] != "now" or r["value_pct"] < 1


def test_g_keep_date_versus_restart_window():
    acc = [Account("A", 100, h(3))]
    r = first(plan(acc, [full(30)], 3))
    assert r["decision"] == "planned" and r["now_value_pct"] == pytest.approx(9, abs=0.5)
    print("g keep", r["decision"], off(r), r["value_pct"])
    cfg = Config(credit_semantics="restart_window")
    acc = [Account("A", 100, h(100))]
    r = first(plan(acc, [full(30)], 3, config=cfg))
    assert r["decision"] == "now" and r["value_pct"] > 50
    # the same account under keep_date: its own reset is 100 h away, the credit expires in 30 h
    k = first(plan(acc, [full(30)], 3))
    assert k["decision"] == "now" and k["value_pct"] == pytest.approx(99, abs=0.5)
    print("g restart", r["decision"], off(r), r["value_pct"])


def test_h_second_credit_sees_the_first_spend():
    accounts = [Account("A", 100, h(100)), Account("B", 100, h(100))]
    credits = [Credit("A", "a", "full", h(300)), Credit("B", "b", "full", h(310))]
    out = plan(accounts, credits, 1)["credits"]
    assert out[0]["decision"] == "now"
    assert out[1]["decision"] == "planned" and off(out[1]) > 10


def test_i_forecast_covered_hours():
    out = plan([Account("A", 100, h(100))], [full(300)], 1.5)
    f = out["forecast"]
    assert f["baseline_mode"] == "short" and f["covered_hours"] > 0
    assert f["covered_by"] == ["c"]
    assert f["dry_hours"] < f["baseline_dry_hours"]


def _history(days, hour_of_day=16):
    out = []
    used = 0.0
    start = NOW - timedelta(days=days)
    for d in range(days):
        for hh in range(24):
            at = start + timedelta(days=d, hours=hh)
            out.append((at, "A", used))
            if hh == hour_of_day:
                used += 5
    return out


def test_j_pace_profile():
    assert pace_profile(_history(2), NOW) is None
    p = pace_profile(_history(4), NOW)
    assert p is not None and len(p) == 24
    assert max(range(24), key=lambda i: p[i]) == 16
    assert sum(p) / 24 == pytest.approx(1.0, abs=1e-9)
    assert all(v > 0 for v in p)
    flat = [(NOW - timedelta(days=4, hours=-i), "A", 1.0) for i in range(97)]
    assert pace_profile(flat, NOW) is None


def test_j2_profile_shapes_the_plan():
    prof = [0.05] * 24
    prof[16] = 24 - 0.05 * 23
    out = plan_credits([Account("A", 100, h(96))], [full(300)], 1.5, NOW, profile=prof)
    assert first(out)["decision"] in ("now", "planned")


def test_k_determinism():
    accounts = [Account("A", 100, h(100)), Account("B", 30, h(150))]
    credits = [full(300), Credit("B", "b", "full", h(200))]
    assert plan(accounts, credits, 2) == plan(accounts, credits, 2)


def test_naive_datetimes_raise():
    with pytest.raises(ValueError):
        plan_credits([], [], 1, datetime(2026, 11, 19))
    with pytest.raises(ValueError):
        plan_credits([Account("A", 1, datetime(2026, 11, 20))], [], 1, NOW)
    with pytest.raises(ValueError):
        pace_profile([], datetime(2026, 11, 19))


def test_unknown_not_usable_and_dead_credits():
    accounts = [Account("A", 100, h(100)), Account("U", None, None)]
    credits = [
        Credit("U", "u", "full", h(50)),
        Credit("A", "nu", "full", h(60), usable=False),
        Credit("A", "gone", "full", h(70), left=0),
        Credit("A", "old", "full", h(-1)),
    ]
    out = plan(accounts, credits, 1)["credits"]
    assert [(r["id"], r["decision"], r["reason"]) for r in out] == [
        ("u", "unknown", "account_unread"),
        ("nu", "keep", "not_usable"),
    ]


def test_long_expiry_no_gain_is_keep():
    r = first(plan([Account("A", 0, h(100))], [full(2000)], 0.1))
    assert (r["decision"], r["reason"]) == ("keep", "no_gain_at_pace")


@pytest.mark.parametrize("seed", range(40))
def test_property_full_credit(seed):
    rng = random.Random(seed)
    cfg = Config(plan_step_h=0.5)
    accounts = [
        Account(
            n,
            rng.uniform(0, 100),
            h(rng.uniform(1, 167)),
        )
        for n in ("A", "B", "C")[: rng.randint(1, 3)]
    ]
    pace = rng.uniform(0.3, 4)
    exp = rng.uniform(10, 400)
    c = Credit("A", "c", "full", h(exp))
    row = first(plan_credits(accounts, [c], pace, NOW, config=cfg))
    if row["decision"] in ("now", "planned"):
        assert row["value_pct"] >= cfg.plan_min_value_pct
    track = _Track([w for w in (_window(a, False) for a in accounts) if w], pace, None, NOW, cfg)
    end = min(h(cfg.plan_horizon_h), c.expires + timedelta(hours=cfg.week_hours))
    best = 0.0
    t = 0.0
    while t <= exp - cfg.plan_expiry_guard_h:
        best = max(best, _full_value(track, c, h(t), [], end))
        t += 6
    if best >= cfg.plan_min_value_pct + 1.0:
        assert row["decision"] in ("now", "planned")
        assert row["value_pct"] >= best - 1.0


def test_r1_tie_set_never_below_the_minimum():
    r = first(plan([Account("A", 100, h(1.0))], [Credit("A", "c", "full", h(3.75))], 0.6))
    assert r["decision"] == "planned" and r["value_pct"] >= 1.0
    assert off(r) == pytest.approx(2.75, abs=0.25)


def test_r2_candidates_include_other_planned_spends():
    accounts = [Account("A", 100, h(120)), Account("B", 76, h(160)), Account("C", 100, h(18))]
    credits = [Credit("B", "b", "full", h(58)), Credit("A", "a", "full", h(310))]
    out = plan(accounts, credits, 2)["credits"]
    assert out[0]["id"] == "b" and off(out[0]) == pytest.approx(11.5, abs=0.25)
    assert off(out[1]) == pytest.approx(11.5, abs=0.5) and out[1]["value_pct"] > 90


def test_r3_now_is_always_a_candidate():
    accounts = [Account("A", 90, h(150)), Account("B", 10, h(127)), Account("C", 100, h(30))]
    cfg = Config(credit_semantics="restart_window")
    r = first(plan(accounts, [Credit("A", "c", "full", h(390))], 2.7, config=cfg))
    assert r["decision"] == "now" and r["value_pct"] == pytest.approx(r["now_value_pct"], abs=0.1)
    assert r["value_pct"] > 70


def test_r4_let_expire_only_near_expiry():
    acc = [Account("A", 0, h(100))]
    near = first(plan(acc, [full(20)], 0.1))
    far = first(plan(acc, [full(300)], 0.1))
    assert (near["decision"], near["reason"]) == ("let_expire", "nothing_to_recover")
    assert (far["decision"], far["reason"]) == ("keep", "no_gain_at_pace")
    sacc = [Account("A", 10, h(100), 100, h(100))]
    s = first(plan(sacc, [Credit("A", "s", "secondary", h(300))], 1, secondary_pace=0))
    assert (s["decision"], s["reason"]) == ("keep", "no_secondary_work")
    s = first(plan(sacc, [Credit("A", "s", "secondary", h(20))], 1, secondary_pace=0))
    assert (s["decision"], s["reason"]) == ("let_expire", "no_secondary_work")


def test_r5_validation():
    a = [Account("A", 100, h(10))]
    with pytest.raises(ValueError):
        pace_profile([(datetime(2026, 11, 18), "A", 1.0)], NOW)
    for bad in (None, -1.0):
        with pytest.raises(ValueError):
            plan_credits(a, [], bad, NOW)
    with pytest.raises(ValueError):
        plan_credits(a, [], 1.0, NOW, profile=[1.0] * 23)


def test_r5_floor_holds_after_renormalisation():
    cfg = Config(profile_floor=0.3)
    p = pace_profile(_history(5), NOW, cfg)
    assert min(p) >= 0.3 - 1e-9 and sum(p) / 24 == pytest.approx(1.0, abs=1e-9)


def test_r5_increment_spread_over_the_interval():
    # one +6 jump between 02:30 and 04:30 touches hours 2, 3 and 4 (30, 60, 30 minutes)
    base = NOW - timedelta(days=4)
    s = [(base, "A", 0.0), (base + timedelta(hours=2, minutes=30), "A", 0.0)]
    s += [(base + timedelta(hours=4, minutes=30), "A", 6.0), (NOW, "A", 6.0)]
    p = pace_profile(s, NOW)
    assert p[3] == pytest.approx(2 * p[2]) and p[2] == pytest.approx(p[4])


def test_r5_covered_by_lists_full_credits_only():
    acc = [Account("A", 100, h(100), 100, h(100))]
    cs = [Credit("A", "f", "full", h(300)), Credit("A", "s", "secondary", h(310))]
    out = plan(acc, cs, 1.5, secondary_pace=1.0)
    assert out["forecast"]["covered_by"] == ["f"]


def test_r6_second_credit_matches_grid_max():
    cfg = Config(plan_step_h=0.5)
    checked = 0
    for seed in range(40):
        rng = random.Random(1000 + seed)
        accounts = [Account(n, rng.uniform(0, 100), h(rng.uniform(1, 167))) for n in ("A", "B")]
        pace = rng.uniform(0.3, 4)
        e1, e2 = sorted(rng.uniform(10, 400) for _ in range(2))
        c1, c2 = Credit("A", "c1", "full", h(e1)), Credit("B", "c2", "full", h(e2))
        rows = plan_credits(accounts, [c1, c2], pace, NOW, config=cfg)["credits"]
        planned = []
        if rows[0]["planned_at"]:
            planned = [("A", datetime.fromisoformat(rows[0]["planned_at"]), "full")]
        track = _Track(
            [w for w in (_window(a, False) for a in accounts) if w], pace, None, NOW, cfg
        )
        end = min(h(cfg.plan_horizon_h), c2.expires + timedelta(hours=cfg.week_hours))
        times = [t * 6.0 for t in range(int((e2 - 1) // 6) + 1)]
        if planned:
            times.append(_hours_of(planned[0][1]))
        best = max(_full_value(track, c2, h(t), planned, end) for t in times if t <= e2 - 1.0)
        if best >= cfg.plan_min_value_pct + 1.0:
            checked += 1
            assert rows[1]["decision"] in ("now", "planned")
            assert rows[1]["value_pct"] >= best - 1.0
    assert checked > 5


def _hours_of(dt):
    return (dt - NOW).total_seconds() / 3600
