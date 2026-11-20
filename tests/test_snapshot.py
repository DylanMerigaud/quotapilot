"""Snapshot JSON, merge across machines, and the command line."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from quotapilot import Account, Credit, Snapshot, merge, snapshot_dumps, snapshot_loads
from quotapilot.snapshot import from_json, to_json

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
RESET = NOW + timedelta(days=3)


def acc(name: str, pct: float | None, fetched: datetime | None = None, **kw: Any) -> Account:
    return Account(name, pct, RESET if pct is not None else None, fetched_at=fetched, **kw)


def snap(source: str, at: datetime, accounts: list[Account], **kw: Any) -> Snapshot:
    return Snapshot(at=at, source=source, accounts=accounts, **kw)


def test_round_trip():
    s = Snapshot(
        at=NOW,
        source="box",
        accounts=[acc("1", 40.0, NOW, secondary_pct=5.0, secondary_reset=RESET, stale=True)],
        samples=[(NOW - timedelta(hours=1), "1", 38.0), (NOW, "1", None)],
        credits=[Credit("1", "c1", "full", RESET, left=2)],
    )
    back = snapshot_loads(snapshot_dumps(s))
    assert back == s
    assert to_json(s)["schema"] == 1


def test_naive_datetime_is_an_error_naming_the_field():
    obj = to_json(snap("a", NOW, [acc("1", 10.0)]))
    obj["accounts"][0]["reset"] = "2026-11-23T12:00:00"
    with pytest.raises(ValueError, match="reset of 1"):
        from_json(obj)
    obj = to_json(snap("a", NOW, []))
    obj["at"] = "2026-11-20T12:00:00"
    with pytest.raises(ValueError, match="at"):
        from_json(obj)
    with pytest.raises(ValueError, match="fetched_at of 1"):
        to_json(snap("a", NOW, [acc("1", 10.0, datetime(2026, 11, 20, 1, 0))]))


def test_bad_schema_and_shape_raise_value_error():
    with pytest.raises(ValueError, match="schema"):
        from_json({"schema": 2})
    with pytest.raises(ValueError):
        from_json({"schema": 1, "at": NOW.isoformat()})
    with pytest.raises(ValueError):
        snapshot_loads("[1]")


def test_merge_keeps_the_freshest_usable_reading_per_account():
    old = snap("server", NOW - timedelta(minutes=30), [acc("1", 50.0), acc("2", 10.0)])
    new = snap("laptop", NOW - timedelta(minutes=5), [acc("1", 55.0)])
    m = merge([old, new], NOW)
    by = {a.name: a for a in m.accounts}
    assert by["1"].used_pct == 55.0
    assert by["2"].used_pct == 10.0 and by["2"].usable  # read only on the server, counted
    assert m.source == "server+laptop" and m.at == new.at


def test_merge_uses_fetched_at_over_snapshot_at():
    a = snap("a", NOW, [acc("1", 50.0, NOW - timedelta(hours=2))])
    b = snap("b", NOW - timedelta(hours=1), [acc("1", 60.0)])
    assert merge([a, b], NOW).accounts[0].used_pct == 60.0


def test_merge_prefers_usable_then_latest_unusable():
    bad_new = snap("a", NOW, [acc("1", None, usable=False)])
    good_old = snap("b", NOW - timedelta(hours=3), [acc("1", 30.0)])
    assert merge([bad_new, good_old], NOW).accounts[0].used_pct == 30.0
    bad_old = snap("c", NOW - timedelta(hours=1), [acc("1", None, usable=False)])
    m = merge([bad_old, bad_new], NOW)
    assert not m.accounts[0].usable


def test_stale_reading_within_the_window_counts_and_past_it_does_not():
    fresh = acc("1", 40.0, NOW - timedelta(minutes=50), stale=True)
    assert merge([snap("a", NOW, [fresh])], NOW).accounts[0].usable
    old = acc("1", 40.0, NOW - timedelta(minutes=61), stale=True)
    (kept,) = merge([snap("a", NOW, [old])], NOW).accounts
    assert not kept.usable and kept.used_pct == 40.0


def test_stale_loses_to_a_later_non_stale_reading():
    stale = acc("1", 40.0, NOW - timedelta(minutes=20), stale=True)
    live = acc("1", 45.0, NOW - timedelta(minutes=10))
    assert merge([snap("a", NOW, [stale]), snap("b", NOW, [live])], NOW).accounts[0].used_pct == 45


def test_samples_union_deduplicated_and_sorted():
    t1, t2 = NOW - timedelta(hours=2), NOW - timedelta(hours=1)
    a = snap("a", NOW, [], samples=[(t2, "1", 20.0), (t1, "2", 5.0)])
    b = snap("b", NOW, [], samples=[(t1, "1", 10.0), (t2, "1", 21.0)])
    assert merge([a, b], NOW).samples == [(t1, "1", 10.0), (t1, "2", 5.0), (t2, "1", 21.0)]


def test_credits_keyed_and_latest_snapshot_wins():
    c_old = Credit("1", "c", "full", RESET, left=2)
    c_new = Credit("1", "c", "full", RESET, left=1)
    other = Credit("2", "c", "full", RESET)
    a = snap("a", NOW - timedelta(hours=1), [], credits=[c_old, other])
    b = snap("b", NOW, [], credits=[c_new])
    m = merge([b, a], NOW)
    assert sorted((c.account, c.left) for c in m.credits) == [("1", 1), ("2", 1)]


def test_merge_rejects_empty_input_and_naive_now():
    with pytest.raises(ValueError):
        merge([], NOW)
    with pytest.raises(ValueError):
        merge([snap("a", NOW, [])], datetime(2026, 11, 20))


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "quotapilot", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=60,
    )


def _write(path: Path, s: Snapshot) -> None:
    path.write_text(snapshot_dumps(s), encoding="utf-8")


def _cli_snapshots() -> tuple[Snapshot, Snapshot]:
    now = datetime.now(timezone.utc)
    reset = now + timedelta(days=3)
    a = Snapshot(
        now,
        "server",
        [Account("1", 40.0, reset, fetched_at=now)],
        samples=[(now - timedelta(hours=10), "1", 20.0), (now - timedelta(hours=1), "1", 38.0)],
    )
    b = Snapshot(now, "laptop", [Account("2", 10.0, reset, fetched_at=now)])
    return a, b


def test_cli_read_two_files_prints_a_forecast(tmp_path: Path):
    a, b = _cli_snapshots()
    _write(tmp_path / "a.json", a)
    _write(tmp_path / "b.json", b)
    r = _run("read", "--source", "a.json", "--source", "b.json", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["snapshot"]["source"] == "server+laptop"
    assert {x["name"] for x in out["forecast"]["accounts"]} == {"1", "2"}
    assert out["pace_source"] == "measured" and out["pace_pct_h"] > 0
    assert out["forecast"]["mode"] in {"surplus", "on_track", "short"}


def test_cli_pace_given_and_average(tmp_path: Path):
    _, b = _cli_snapshots()
    _write(tmp_path / "b.json", b)
    r = _run("read", "--source", "b.json", "--pace", "0.5", cwd=tmp_path)
    assert json.loads(r.stdout)["pace_source"] == "given"
    r = _run("read", "--source", "b.json", cwd=tmp_path)
    assert json.loads(r.stdout)["pace_source"] == "average"


def test_cli_command_source(tmp_path: Path):
    a, b = _cli_snapshots()
    _write(tmp_path / "a.json", a)
    _write(tmp_path / "b.json", b)
    cmd = f'"{sys.executable}" -c "print(open(\'a.json\').read())"'
    r = _run("read", "--source", cmd, "--source", "b.json", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["snapshot"]["source"] == "server+laptop"


@pytest.mark.parametrize("src", ["missing_command_xyz", "exit 3", "echo not json"])
def test_cli_failed_source_exits_2_with_nothing_on_stdout(tmp_path: Path, src: str):
    a, _ = _cli_snapshots()
    _write(tmp_path / "a.json", a)
    r = _run("read", "--source", "a.json", "--source", src, cwd=tmp_path)
    assert r.returncode == 2 and r.stdout == ""
    assert r.stderr.startswith("quotapilot:") and "Traceback" not in r.stderr


def test_cli_snapshot_missing_binary_is_an_error_line(tmp_path: Path):
    r = _run("snapshot", "--binary", "no-such-cswap", cwd=tmp_path)
    assert r.returncode == 2 and r.stdout == "" and "Traceback" not in r.stderr


def test_cli_snapshot_with_a_fake_binary_and_samples(tmp_path: Path):
    now = datetime.now(timezone.utc)
    payload = {
        "accounts": [
            {
                "number": 2,
                "usageStatus": "ok",
                "usage": {
                    "sevenDay": {"pct": 30, "resetsAt": (now + timedelta(days=2)).isoformat()}
                },
                "usageFetchedAt": now.isoformat(),
            }
        ]
    }
    (tmp_path / "list.json").write_text(json.dumps(payload), encoding="utf-8")
    fake = tmp_path / "fakeswap.py"
    fake.write_text("import sys\nprint(open('list.json').read())\n", encoding="utf-8")
    wrapper = tmp_path / "fakeswap"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    samples = tmp_path / "samples.json"
    samples.write_text(json.dumps([[(now - timedelta(hours=2)).isoformat(), "2", 25]]))
    r = _run(
        "snapshot", "--binary", str(wrapper), "--source", "box", "--samples", str(samples),
        cwd=tmp_path,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    s = snapshot_loads(r.stdout)
    assert s.source == "box" and s.accounts[0].used_pct == 30 and s.accounts[0].fetched_at
    assert [x[1:] for x in s.samples] == [("2", 25.0)]
