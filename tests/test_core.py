"""quotapilot core: projection of what each account-week loses, drain first, burn or normal."""

from datetime import datetime, timedelta, timezone

import pytest

from quotapilot import Account, Config, average_pace, decide, pace_from_history, project

UTC = timezone.utc
NOW = datetime(2026, 11, 18, 13, 27, tzinfo=UTC)
EM, EN = chr(0x2014), chr(0x2013)
REF = Config(units_per_week=3600, max_units_per_h=140, burn_floor_units=36)


def _d(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def snapshot(tz=UTC):
    def z(s):
        return _d(s).astimezone(tz)

    return [
        Account("1", 99, None, usable=False),
        Account("2", 8, z("2026-11-23T23:00:00Z"), 7, z("2026-11-23T23:00:00Z")),
        Account("3", 99, z("2026-11-18T23:00:00Z"), 36, z("2026-11-18T23:00:00Z")),
        Account("4", 62, z("2026-11-22T17:00:00Z"), 26, z("2026-11-22T17:00:00Z")),
    ]


def flat_history(name, pct_a, pct_b, now=NOW):
    return [(now - timedelta(hours=7), name, pct_a), (now, name, pct_b)]


def test_snapshot_without_history_uses_average():
    out = decide(snapshot(), [], NOW, config=REF)
    assert out["pace_source"] == "average"
    assert out["drain_first"] == "3"
    assert [a["name"] for a in out["accounts"]] == ["3", "4", "2"]
    assert out["secondary_first"] is None
    assert out["state"] in ("burn", "normal")


def test_project_at_pace_one_by_hand():
    rows = {r["name"]: r for r in project(snapshot(), 1.0, NOW, REF)}
    assert rows["3"]["lost_pct"] == 0 and rows["3"]["empty_at"] is not None
    assert rows["4"]["room_pct"] == 38 and rows["4"]["lost_pct"] == 0
    t = NOW + timedelta(hours=1) + timedelta(hours=38)
    avail = (_d("2026-11-23T23:00:00Z") - t).total_seconds() / 3600
    expect = 92 - avail
    assert rows["2"]["room_pct"] == 92
    assert rows["2"]["lost_pct"] == pytest.approx(expect, abs=0.06)
    assert rows["2"]["lost_units"] == pytest.approx(expect / 100 * 3600, abs=2.5)
    assert rows["2"]["empty_at"] is None


def test_pace_zero_loses_every_room_and_burns():
    hist = flat_history("2", 8, 8)
    out = decide(snapshot(), hist, NOW, config=REF)
    assert out["pace_source"] == "measured" and out["pace_pct_h"] == 0
    assert out["state"] == "burn"
    for a in out["accounts"]:
        assert a["lost_pct"] == a["room_pct"] and a["empty_at"] is None
    assert out["lost_units"] == round((1 + 38 + 92) / 100 * 3600)


def test_reset_in_the_past_loses_nothing():
    acc = [Account("9", 80, NOW - timedelta(hours=2))]
    row = project(acc, 0.0, NOW, REF)[0]
    assert row["room_pct"] == 0 and row["lost_pct"] == 0 and row["lost_units"] == 0


def test_pace_from_history():
    now = NOW
    h = timedelta(hours=1)
    assert pace_from_history([(now - 5 * h, "a", 1), (now, "a", 9)], now) is None
    ex = [
        (now - 10 * h, "a", 10),
        (now - 5 * h, "a", 14),
        (now, "a", 20),
        (now - 10 * h, "b", 50),
        (now - 4 * h, "b", 53),
    ]
    assert pace_from_history(ex, now) == pytest.approx(1.3)
    rs = [(now - 8 * h, "a", 99), (now - 4 * h, "a", 3), (now, "a", 5)]
    assert pace_from_history(rs, now) == pytest.approx(0.25)
    old = [(now - 30 * h, "a", 0), (now - 7 * h, "a", 10), (now, "a", 17)]
    assert pace_from_history(old, now) == pytest.approx(1.0)
    assert pace_from_history([], now) is None


def test_average_pace():
    acc = [Account("a", 50, NOW + timedelta(hours=84)), Account("x", 9, None)]
    assert average_pace(acc, NOW) == pytest.approx(50 / 84)


def _secondary_setup(pace_delta):
    a = Account("A", 95, NOW + timedelta(hours=10))
    five = Account("5", 40, NOW + timedelta(hours=100), 2, NOW + timedelta(hours=165))
    hist = [(NOW - timedelta(hours=7), "A", 90), (NOW, "A", 90 + pace_delta)]
    return [a, five], hist


def test_secondary_only_reset_picks_account():
    accs, hist = _secondary_setup(7)
    out = decide(accs, hist, NOW)
    assert out["drain_first"] == "A"
    assert out["accounts"][0]["lost_pct"] == 0
    assert out["secondary_first"] == "5"


def test_secondary_blocked_when_drain_first_loses():
    accs, hist = _secondary_setup(0.07)
    out = decide(accs, hist, NOW)
    assert out["accounts"][0]["lost_pct"] > 0
    assert out["secondary_first"] is None


def test_secondary_drop_via_previous():
    reset = NOW + timedelta(hours=100)
    acc = [Account("5", 52, reset, 10, reset)]
    prev = {"accounts": [{"name": "5", "used_pct": 50, "secondary_pct": 80}]}
    hist = flat_history("5", 50, 57)
    assert decide(acc, hist, NOW, prev)["secondary_first"] == "5"
    assert decide(acc, hist, NOW, None)["secondary_first"] is None
    prev_dropped = {"accounts": [{"name": "5", "used_pct": 70, "secondary_pct": 80}]}
    assert decide(acc, hist, NOW, prev_dropped)["secondary_first"] is None


def test_mixed_timezones_give_the_same_result():
    plus1 = timezone(timedelta(hours=1))
    a = decide(snapshot(UTC), [], NOW, config=REF)
    b = decide(snapshot(plus1), [], NOW.astimezone(plus1), config=REF)
    assert a == b


def test_summary_is_english_utc():
    s = decide(snapshot(), flat_history("2", 8, 12), NOW, config=REF)["summary"]
    assert s == "Burn: 2051 units expire on 2 at the 23:00 UTC reset at 0.6 pts/h, drain 3 first."
    n = decide(snapshot(), [], NOW, config=REF)["summary"]
    assert n == "Normal: nothing expires at 1.7 pts/h, drain 3 first."


def test_summary_is_clean_and_short():
    for hist in ([], flat_history("2", 8, 8), flat_history("2", 8, 30)):
        s = decide(snapshot(), hist, NOW, config=REF)["summary"]
        assert EM not in s and EN not in s and len(s) < 160
        assert s.isascii()
    assert decide(snapshot(), flat_history("2", 8, 8), NOW, config=REF)["summary"].startswith(
        "Burn:"
    )


def test_defaults():
    c = Config()
    assert (c.week_hours, c.units_per_week, c.max_units_per_h, c.burn_floor_units) == (
        168,
        100.0,
        None,
        1.0,
    )
    assert (c.secondary_min_room_pct, c.secondary_drop_pct) == (10, 20)
    assert (c.min_span_h, c.history_h, c.secondary_hold_h) == (6, 24, 24)
    out = decide(snapshot(), flat_history("2", 8, 8), NOW)
    assert out["unavoidable_units"] is None
    assert out["lost_units"] == 131  # one unit is one percent point: 1 + 38 + 92


def test_matches_the_reference_config():
    # numbers computed with the original implementation on these same inputs
    out = decide(snapshot(), [], NOW, config=REF)
    assert (out["state"], out["drain_first"], out["pace_pct_h"], out["pace_source"]) == (
        "normal",
        "3",
        1.74,
        "average",
    )
    assert (out["lost_units"], out["unavoidable_units"]) == (0, 0)
    assert out["accounts"][0]["empty_at"] == "2026-11-18T14:01:30.588869+00:00"
    assert out["accounts"][2]["empty_at"] == "2026-11-21T16:47:47.141901+00:00"

    out = decide(snapshot(), flat_history("2", 8, 12), NOW, config=REF)
    assert (out["state"], out["pace_pct_h"], out["pace_source"]) == ("burn", 0.57, "measured")
    assert (out["lost_units"], out["unavoidable_units"]) == (2051, 0)
    assert out["accounts"][2]["lost_pct"] == 57.0 and out["accounts"][2]["lost_units"] == 2051.0

    one = [Account("x", 8, NOW + timedelta(hours=5))]
    out = decide(one, [], NOW, config=REF)
    assert (out["pace_pct_h"], out["lost_units"], out["unavoidable_units"]) == (0.05, 3303, 2612)
    assert out["accounts"][0]["lost_pct"] == 91.8
    out = decide(one, flat_history("x", 8, 15), NOW, config=REF)
    assert (out["pace_pct_h"], out["lost_units"], out["unavoidable_units"]) == (1.0, 3132, 2612)

    a = Account("A", 95, NOW + timedelta(hours=10))
    five = Account("5", 40, NOW + timedelta(hours=100), 2, NOW + timedelta(hours=165))
    hist = [(NOW - timedelta(hours=7), "A", 90), (NOW, "A", 97)]
    out = decide([a, five], hist, NOW, config=REF)
    assert (out["secondary_first"], out["secondary_since"]) == ("5", NOW.isoformat())
    assert (out["pace_pct_h"], out["lost_units"]) == (1.0, 0)


def test_average_pace_ignores_a_past_reset():
    acc = [
        Account("a", 50, NOW + timedelta(hours=84)),
        Account("stale", 99, NOW - timedelta(hours=2)),
    ]
    assert average_pace(acc, NOW) == pytest.approx(50 / 84)


def test_secondary_blocked_by_a_loss_that_rounds_to_zero():
    a = Account("A", 95, NOW + timedelta(hours=10))
    five = Account("5", 40, NOW + timedelta(hours=100), 2, NOW + timedelta(hours=165))
    # 7 h of history at 0.4955 pts/h: A needs 5 points in 10 h, loses about 0.045 point
    hist = [(NOW - timedelta(hours=7), "A", 90), (NOW, "A", 90 + 0.4955 * 7)]
    out = decide([a, five], hist, NOW)
    assert out["drain_first"] == "A"
    assert out["secondary_first"] is None


def _drop_scene(now):
    a = Account("A", 40, now + timedelta(hours=100), 5, now + timedelta(hours=100))
    b = Account("B", 99, now + timedelta(hours=10), 5, now + timedelta(hours=10))
    prev = {"accounts": [{"name": "A", "used_pct": 40, "secondary_pct": 60}]}
    return [a, b], prev


def test_secondary_drop_persists_across_ticks_then_expires():
    accs, prev = _drop_scene(NOW)
    hist = [(NOW - timedelta(hours=7), "B", 90), (NOW, "B", 99)]
    t1 = decide(accs, hist, NOW, previous=prev)
    assert t1["secondary_first"] == "A" and t1["secondary_since"] == NOW.isoformat()
    t1["accounts"] = [{"name": "A", "used_pct": 40, "secondary_pct": 5}]
    later = NOW + timedelta(hours=1)
    accs2, _ = _drop_scene(later)
    t2 = decide(
        accs2, [(later - timedelta(hours=7), "B", 90), (later, "B", 99)], later, previous=t1
    )
    assert t2["secondary_first"] == "A" and t2["secondary_since"] == t1["secondary_since"]
    t3t = NOW + timedelta(hours=25)
    accs3, _ = _drop_scene(t3t)
    t3 = decide(accs3, [(t3t - timedelta(hours=7), "B", 90), (t3t, "B", 99)], t3t, previous=t1)
    assert t3["secondary_first"] is None and t3["secondary_since"] is None


def test_secondary_persistence_still_checks_room():
    accs, prev = _drop_scene(NOW)
    hist = [(NOW - timedelta(hours=7), "B", 90), (NOW, "B", 99)]
    t1 = decide(accs, hist, NOW, previous=prev)
    accs[0].used_pct = 95
    t2 = decide(accs, hist, NOW + timedelta(hours=1), previous=t1)
    assert t2["secondary_first"] is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
