"""quotapilot capacity_view and expired_room: the cross account picture of the pool."""

from datetime import datetime, timedelta, timezone

import pytest

from quotapilot import Account, capacity_view, expired_room

UTC = timezone.utc
NOW = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)


def h(n):
    return NOW + timedelta(hours=n)


def pool(n=4):
    """The 4 account pool of the hub's 20 November picture (nothing expires in the week)."""
    rows = [("4", 99, 56), ("2", 83, 86), ("1", 4, 128), ("3", 0, 134)]
    return [Account(name, used, h(reset)) for name, used, reset in rows[:n]]


def test_rounding_four_accounts_at_pace_1_6():
    v = capacity_view(pool(), 1.6, NOW)
    assert v["counted"] == 4
    assert v["capacity_pct_h"] == 2.36
    assert v["unused_pct_h"] == 0.76
    assert v["unused_week_pct"] == 128
    assert v["accounts_worth"] == round(128 / 99, 2)
    assert v["pace_pct_h"] == 1.6


def test_balanced():
    v = capacity_view(pool(), 2.22, NOW)
    assert v["verdict"] == "balanced"
    assert v["unused_pct_h"] == 0.14 and v["unused_week_pct"] == 24
    assert v["weeks_over"] == 0 and v["drop"] is None and v["after"] == []


def test_short():
    v = capacity_view(pool(), 3.0, NOW)
    assert v["verdict"] == "short"
    assert v["short_by_pct_h"] == pytest.approx(0.64)


def test_consume_more_one_week_only():
    v = capacity_view(pool(), 1.6, NOW)
    assert v["verdict"] == "consume_more" and v["weeks_over"] == 1 and v["drop"] is None
    v2 = capacity_view(pool(), 1.6, NOW, prior_pace=2.2)
    assert v2["verdict"] == "consume_more" and v2["weeks_over"] == 1


def test_consume_more_from_lost_units():
    accounts = [Account("A", 0, h(5)), Account("B", 99, h(160))]
    v = capacity_view(accounts, 1.0, NOW)
    assert v["unused_week_pct"] < 99 and v["lost_units"] >= 1.0
    assert v["verdict"] == "consume_more" and v["weeks_over"] == 0


def test_drop_one_names_the_account_with_most_expired_room_and_its_price():
    v = capacity_view(
        pool(),
        1.6,
        NOW,
        prior_pace=1.6,
        expired={"1": 95.0, "2": 10.0, "3": 80.0, "4": 5.0},
        prices={"1": 200.0, "3": 100.0},
    )
    assert v["verdict"] == "drop_one" and v["weeks_over"] == 2
    assert v["drop"] == {"account": "1", "expired_pct": 95.0, "saves_per_month": 200.0}


def test_drop_one_ties_by_name_and_no_price():
    v = capacity_view(pool(), 1.6, NOW, prior_pace=1.6, expired={"3": 50.0, "2": 50.0})
    assert v["drop"] == {"account": "2", "expired_pct": 50.0, "saves_per_month": None}


def test_drop_one_without_expired_has_no_drop():
    v = capacity_view(pool(), 1.6, NOW, prior_pace=1.6)
    assert v["verdict"] == "drop_one" and v["drop"] is None
    v = capacity_view(pool(), 1.6, NOW, prior_pace=1.6, expired={})
    assert v["drop"] is None


def test_after_one_and_two_end_dates():
    ends = {"4": h(300), "3": h(300), "2": h(600)}
    v = capacity_view(pool(), 1.2, NOW, ends=ends)
    assert [e["at"] for e in v["after"]] == [h(300).isoformat(), h(600).isoformat()]
    first, second = v["after"]
    assert first["without"] == ["3", "4"] and first["capacity_pct_h"] == 1.18
    assert first["verdict"] == "short" and first["short_by_pct_h"] == 0.02
    assert second["without"] == ["2", "3", "4"] and second["verdict"] == "short"
    one = capacity_view(pool(), 1.0, NOW, ends={"4": h(300)})
    assert len(one["after"]) == 1 and one["after"][0]["verdict"] == "balanced"
    assert one["after"][0]["short_by_pct_h"] is None


def test_an_end_date_in_the_past_is_ignored():
    assert capacity_view(pool(), 1.0, NOW, ends={"4": h(-5)})["after"] == []


def test_no_counted_account():
    v = capacity_view([], 1.0, NOW)
    assert v["counted"] == 0 and v["capacity_pct_h"] == 0.0 and v["verdict"] == "short"
    assert v["unused_pct_h"] == -1.0 and v["short_by_pct_h"] == 1.0
    assert capacity_view([], 0.0, NOW)["verdict"] == "balanced"


def test_negative_pace_and_naive_datetimes_raise():
    with pytest.raises(ValueError):
        capacity_view(pool(), -1, NOW)
    with pytest.raises(ValueError):
        capacity_view(pool(), 1, datetime(2026, 11, 20))
    with pytest.raises(ValueError):
        capacity_view(pool(), 1, NOW, ends={"4": datetime(2026, 12, 1)})


def test_expired_room_two_resets():
    s = [
        (h(-300), "A", 60.0),
        (h(-250), "A", 80.0),
        (h(-240), "A", 5.0),
        (h(-100), "A", 40.0),
        (h(-90), "A", 2.0),
    ]
    assert expired_room(s, NOW) == {"A": round((99 - 80) + (99 - 40), 1)}


def test_expired_room_no_reset_and_full_reset():
    s = [(h(-50), "A", 10.0), (h(-40), "A", 30.0), (h(-30), "B", 99.0), (h(-20), "B", 0.0)]
    assert expired_room(s, NOW) == {"B": 0.0}


def test_expired_room_only_the_last_window_hours():
    s = [(h(-400), "A", 90.0), (h(-390), "A", 0.0)]
    assert expired_room(s, NOW) == {}


def test_exports():
    import quotapilot

    assert "capacity_view" in quotapilot.__all__ and "expired_room" in quotapilot.__all__
