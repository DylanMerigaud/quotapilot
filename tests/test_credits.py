"""quotapilot advise_credits: when to spend an on-demand reset credit."""

from datetime import datetime, timedelta, timezone

import pytest

import quotapilot
from quotapilot import Account, Credit, advise_credits

UTC = timezone.utc
NOW = datetime(2026, 11, 18, 22, 0, tzinfo=UTC)


def h(n):
    return NOW + timedelta(hours=n)


def _d(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def one(acc, credit, **kw):
    out = advise_credits([acc] if acc else [], [credit], NOW, **kw)
    assert len(out) == 1
    return out[0]


def full(**kw):
    return Credit("A", "c", "full", **{"expires": h(500), **kw})


def sec(**kw):
    return Credit("A", "s", "secondary", **{"expires": h(500), **kw})


def test_version_and_exports():
    assert quotapilot.__version__ == "0.4.0"
    assert "advise_credits" in quotapilot.__all__ and "Credit" in quotapilot.__all__


def test_live_case_all_wait():
    accounts = [
        Account("2", 18, _d("2026-11-23T23:00:00Z"), 7, _d("2026-11-23T23:00:00Z")),
        Account("4", 85, _d("2026-11-22T17:00:00Z"), 38, _d("2026-11-22T17:00:00Z")),
    ]
    credits = [
        Credit("2", "full-reset-2611", "full", _d("2026-12-10T22:00:00Z")),
        Credit("2", "launch-2610-fable", "secondary", _d("2027-03-09T07:59:00Z")),
        Credit("4", "full-reset-2611", "full", _d("2026-12-10T22:00:00Z")),
    ]
    out = advise_credits(accounts, credits, NOW)
    assert [(r["verdict"], r["reason"]) for r in out] == [("wait", "not_spent")] * 3
    assert [(r["account"], r["id"]) for r in out] == [
        ("2", "full-reset-2611"),
        ("4", "full-reset-2611"),
        ("2", "launch-2610-fable"),
    ]


def test_unread_reasons():
    assert one(None, full())["verdict"] == "unknown"
    r = one(Account("A", 100, h(80), usable=False), full())
    assert (r["verdict"], r["reason"], r["gain_pct"], r["hours_to_reset"]) == (
        "unknown",
        "account_unread",
        0.0,
        None,
    )
    assert one(Account("A", None, None), full())["reason"] == "account_unread"
    # a secondary credit does not need the main reading to be known
    r = one(Account("A", None, None, 100, h(80)), sec())
    assert (r["verdict"], r["reason"]) == ("wait", "weekly_spent")


def test_not_usable():
    r = one(Account("A", 100, h(80)), full(usable=False))
    assert (r["verdict"], r["reason"]) == ("wait", "not_usable")


def test_expiry():
    soon = full(expires=h(5))
    r = one(Account("A", 0, h(80)), soon)
    assert (r["verdict"], r["reason"], r["gain_pct"]) == ("wait", "expires_no_gain", 0.0)
    r = one(Account("A", 40, h(80)), soon)
    assert (r["verdict"], r["reason"], r["gain_pct"]) == ("now", "expires_soon", 40.0)
    assert r["hours_to_expiry"] == 5.0
    r = one(Account("A", 0, h(80), 30, h(80)), sec(expires=h(24)))
    assert (r["verdict"], r["reason"], r["gain_pct"]) == ("now", "expires_soon", 30.0)


def test_full_rules():
    a = Account("A", 100, h(80))
    r = one(a, full(), mode="on_track")
    assert (r["verdict"], r["reason"], r["gain_pct"]) == ("now", "spent_far_reset", 100.0)
    assert r["hours_to_reset"] == 80.0
    assert one(Account("A", 98.9, h(80)), full())["reason"] == "not_spent"
    assert one(Account("A", 100, h(10)), full(), mode="on_track")["reason"] == "natural_reset_soon"
    assert one(a, full(), mode="surplus")["reason"] == "pool_surplus"
    assert one(Account("A", 100, None), full())["reason"] == "spent_far_reset"


def test_stale_natural_reset_counts_as_refilled():
    r = one(Account("A", 100, h(-3)), full())
    assert (r["reason"], r["gain_pct"], r["hours_to_reset"]) == ("not_spent", 0.0, 0.0)
    r = one(Account("A", 20, h(80), 100, h(-1)), sec(), demand={"A": True})
    assert (r["reason"], r["gain_pct"]) == ("not_spent", 0.0)


def test_secondary_rules():
    a = Account("A", 50, h(100), 100, h(80))
    r = one(a, sec(), demand={"A": True})
    assert (r["verdict"], r["reason"], r["gain_pct"]) == (
        "now",
        "secondary_spent_weekly_room",
        100.0,
    )
    assert one(a, sec())["verdict"] == "now"  # demand not checked
    assert one(a, sec(), demand={"A": False})["reason"] == "no_demand"
    assert one(a, sec(), demand={})["reason"] == "no_demand"
    assert one(Account("A", 50, h(100)), sec())["reason"] == "no_window"
    assert one(Account("A", 50, h(100), 60, h(80)), sec())["reason"] == "not_spent"
    assert one(Account("A", 95, h(100), 100, h(80)), sec())["reason"] == "weekly_spent"
    assert one(Account("A", 90, h(100), 100, h(80)), sec())["verdict"] == "now"
    assert one(Account("A", 50, h(100), 100, h(10)), sec())["reason"] == "natural_reset_soon"


def test_ordering_and_skipped():
    accounts = [Account("A", 100, h(80)), Account("B", 100, h(80))]
    credits = [
        Credit("B", "x", "full", h(300)),
        Credit("A", "z", "full", h(300)),
        Credit("A", "y", "full", h(300)),
        Credit("B", "early", "full", h(100)),
        Credit("A", "none", "full", h(100), left=0),
        Credit("A", "gone", "full", h(-1)),
        Credit("A", "now", "full", NOW),
    ]
    out = advise_credits(accounts, credits, NOW)
    assert [(r["account"], r["id"]) for r in out] == [
        ("B", "early"),
        ("A", "y"),
        ("A", "z"),
        ("B", "x"),
    ]
    assert out[0]["expires"] == h(100).isoformat()


def test_unknown_kind_and_naive():
    with pytest.raises(ValueError):
        advise_credits([Account("A", 1, h(5))], [Credit("A", "c", "weird", h(50))], NOW)
    with pytest.raises(ValueError):
        advise_credits([], [], datetime(2026, 11, 18))
    with pytest.raises(ValueError):
        advise_credits([Account("A", 1, datetime(2026, 11, 20))], [full()], NOW)
    with pytest.raises(ValueError):
        advise_credits([], [Credit("A", "c", "full", datetime(2026, 12, 1))], NOW)
