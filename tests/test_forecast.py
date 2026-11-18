"""quotapilot forecast and pace_band: windows and pace with the refills counted."""

from datetime import datetime, timedelta, timezone

import pytest

import quotapilot
from quotapilot import Account, Config, forecast, pace_band, project

UTC = timezone.utc
NOW = datetime(2026, 11, 18, 13, 27, tzinfo=UTC)
CFG = Config(units_per_week=3600, burn_floor_units=36)


def _d(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def h(n):
    return NOW + timedelta(hours=n)


def iso(dt):
    return dt.astimezone(UTC).isoformat()


def two():
    return [Account("A", 90, h(10)), Account("B", 50, h(100))]


def snapshot():
    return [
        Account("1", 99, None, usable=False),
        Account("2", 8, _d("2026-11-23T23:00:00Z"), 7, _d("2026-11-23T23:00:00Z")),
        Account("3", 99, _d("2026-11-18T23:00:00Z"), 36, _d("2026-11-18T23:00:00Z")),
        Account("4", 62, _d("2026-11-22T17:00:00Z"), 26, _d("2026-11-22T17:00:00Z")),
    ]


def test_on_track():
    out = forecast(two(), 1.0, NOW, CFG)
    assert out["mode"] == "on_track"
    assert out["dry_hours"] == 0 and out["dry_at"] is None and out["dry_until"] is None
    a, b = out["accounts"]
    assert a["name"] == "A" and a["projected_pct"] == 100 and a["empty_at"] == iso(h(10))
    assert b["projected_pct"] == 100 and b["empty_at"] == iso(h(60))
    assert out["horizon"] == iso(h(168))
    assert out["pace_pct_h"] == 1.0


def test_pace_zero_loses_everything():
    out = forecast(two(), 0.0, NOW, CFG)
    assert out["mode"] == "surplus"
    assert out["dry_at"] is None and out["dry_hours"] == 0
    for row, used in zip(out["accounts"], (90, 50)):
        assert row["lost_pct"] == 100 - used
        assert row["projected_pct"] == used
        assert row["empty_at"] is None


def test_short():
    out = forecast([Account("A", 90, h(10))], 5.0, NOW, CFG)
    assert out["mode"] == "short"
    assert out["dry_at"] == iso(h(2))
    assert out["dry_until"] == iso(h(10))
    assert out["dry_hours"] >= 8
    assert out["accounts"][0]["empty_at"] == iso(h(2))


def test_stale_reset_is_a_fresh_window():
    out = forecast([Account("S", 80, h(-2))], 1.0, NOW, CFG)
    row = out["accounts"][0]
    assert row["used_pct"] == 0
    assert row["reset"] == iso(h(-2 + 168))
    assert row["week_pct"] == pytest.approx(1.2, abs=0.05)
    assert row["empty_at"] == iso(h(100))


def test_unusable_and_missing_reset_are_skipped():
    accounts = [
        Account("off", 10, h(5), usable=False),
        Account("none", 10, None),
        Account("nopct", None, h(5)),
        Account("ok", 10, h(20)),
    ]
    out = forecast(accounts, 1.0, NOW, CFG)
    assert [r["name"] for r in out["accounts"]] == ["ok"]


def test_rows_in_order_of_first_reset():
    out = forecast(list(reversed(two())), 1.0, NOW, CFG)
    assert [r["name"] for r in out["accounts"]] == ["A", "B"]


def test_band():
    floor, ceiling = pace_band(two(), NOW, CFG)
    assert floor is not None and ceiling is not None and floor <= ceiling
    assert forecast(two(), floor + 0.01, NOW, CFG)["mode"] != "surplus"
    if floor > 0:
        assert forecast(two(), max(floor - 0.01, 0), NOW, CFG)["mode"] == "surplus"
    assert forecast(two(), ceiling + 0.01, NOW, CFG)["mode"] == "short"
    assert forecast(two(), ceiling - 0.01, NOW, CFG)["mode"] != "short"


def test_band_empty():
    assert pace_band([], NOW, CFG) == (None, None)
    assert pace_band([Account("x", 1, h(5), usable=False)], NOW, CFG) == (None, None)


@pytest.mark.parametrize("pace", [0.5, 1.0, 3.0])
def test_first_windows_match_project(pace):
    fc = forecast(snapshot(), pace, NOW, CFG)
    pj = project(snapshot(), pace, NOW, CFG)
    assert sum(r["lost_pct"] for r in fc["accounts"]) == pytest.approx(
        sum(r["lost_pct"] for r in pj), abs=0.2
    )


def test_version_and_exports():
    assert quotapilot.__version__ == "0.2.0"
    assert "forecast" in quotapilot.__all__ and "pace_band" in quotapilot.__all__


def test_tiny_pace_does_not_overflow():
    assert pace_band([Account("a", 100, h(10))], NOW)[0] is not None
    out = forecast(two(), 1e-12, NOW, CFG)
    assert out["mode"] == "surplus"


def test_rows_sorted_by_first_reset_with_stale_account():
    out = forecast([Account("A", 80, h(-2)), Account("B", 50, h(10))], 1.0, NOW, CFG)
    assert [r["name"] for r in out["accounts"]] == ["B", "A"]


def test_empty_pool_is_short():
    out = forecast([], 1.0, NOW, CFG)
    assert out["mode"] == "short"
    assert out["dry_at"] == iso(NOW) and out["dry_until"] == iso(h(168))
    assert out["dry_hours"] == 168
    assert forecast([], 0.0, NOW, CFG)["mode"] == "on_track"
    assert pace_band([], NOW, CFG) == (None, None)


def test_full_window_is_empty_now():
    out = forecast([Account("F", 100, h(10))], 1.0, NOW, CFG)
    assert out["accounts"][0]["empty_at"] == iso(NOW)
