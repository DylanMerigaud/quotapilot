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
            if hh == hour_of_day:
                used += 5
            out.append((at, "A", used))
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
    assert (r["decision"], r["reason"]) == ("keep", "no_gain_in_horizon")


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
