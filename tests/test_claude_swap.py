"""The claude-swap adapter: `cswap list --json` into accounts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from quotapilot import decide
from quotapilot.adapters import claude_swap

NOW = datetime(2026, 11, 18, 13, 27, tzinfo=timezone.utc)

PAYLOAD: dict[str, Any] = {
    "activeAccountNumber": 1,
    "accounts": [
        {
            "number": 1,
            "email": "work@example.com",
            "active": True,
            "usageStatus": "ok",
            "usage": {
                "fiveHour": {"pct": 12, "resetsAt": "2026-11-18T17:00:00Z"},
                "sevenDay": {"pct": 62.5, "resetsAt": "2026-11-22T17:00:00Z"},
                "scoped": [
                    {"name": "Opus", "pct": 30, "resetsAt": "2026-11-24T17:00:00Z"},
                    {"name": "Sonnet", "pct": 26, "resetsAt": "2026-11-24T17:00:00+00:00"},
                ],
            },
        },
        {
            "number": 2,
            "email": "personal@example.com",
            "usageStatus": "ok",
            "usage": {"sevenDay": {"pct": 8, "resetsAt": "2026-11-23T23:00:00Z"}},
        },
        {"number": 3, "usageStatus": "error", "usage": None},
        {"number": 4, "usageStatus": "ok", "usage": {"fiveHour": {"pct": 1}}},
    ],
}


def test_parse_list_reads_the_weekly_window():
    accs = claude_swap.parse_list(PAYLOAD, NOW)
    assert [a.name for a in accs] == ["1", "2", "3", "4"]
    one = accs[0]
    assert one.used_pct == 62.5 and one.reset == datetime(2026, 11, 22, 17, tzinfo=timezone.utc)
    assert one.usable and one.secondary_pct == 30
    assert accs[1].used_pct == 8 and accs[1].usable


def test_unreadable_usage_is_not_usable():
    accs = claude_swap.parse_list(PAYLOAD, NOW)
    for a in accs[2:]:
        assert not a.usable and a.used_pct is None and a.reset is None


def test_secondary_window_by_name_or_first():
    named = claude_swap.parse_list(PAYLOAD, NOW, secondary_name="Sonnet")[0]
    assert named.secondary_pct == 26
    assert named.secondary_reset == datetime(2026, 11, 24, 17, tzinfo=timezone.utc)
    first = claude_swap.parse_list(PAYLOAD, NOW)[0]
    assert first.secondary_pct == 30
    missing = claude_swap.parse_list(PAYLOAD, NOW, secondary_name="Haiku")[0]
    assert missing.secondary_pct is None and missing.secondary_reset is None


def test_a_bare_list_of_rows_is_accepted():
    assert [a.name for a in claude_swap.parse_list(PAYLOAD["accounts"], NOW)] == [
        "1",
        "2",
        "3",
        "4",
    ]


def test_naive_stamp_is_read_as_local_time():
    at = claude_swap.parse_at("2026-11-22 17:00:00")
    assert at is not None and at.tzinfo is not None
    assert claude_swap.parse_at("nope") is None and claude_swap.parse_at(None) is None


def test_malformed_rows_raise():
    with pytest.raises(KeyError):
        claude_swap.parse_list({"accounts": [{"usage": {}}]}, NOW)
    bad = {"accounts": [{"number": 1, "usageStatus": "ok", "usage": [1]}]}
    with pytest.raises(TypeError):
        claude_swap.parse_list(bad, NOW)


def test_parsed_accounts_feed_decide():
    out = decide(claude_swap.parse_list(PAYLOAD, NOW), [], NOW)
    assert out["drain_first"] == "1"
    assert [r["name"] for r in out["accounts"]] == ["1", "2"]


def _runner(stdout: str, returncode: int = 0, stderr: str = ""):
    calls: list[list[str]] = []

    def run(cmd: list[str], **kwargs: Any) -> Any:
        calls.append(cmd)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    return run, calls


def test_read_runs_cswap_list_json():
    run, calls = _runner(json.dumps(PAYLOAD))
    accs = claude_swap.read(run=run)
    assert calls == [["cswap", "list", "--json"]]
    assert len(accs) == 4
    run, calls = _runner(json.dumps(PAYLOAD))
    claude_swap.read(run=run, binary="/opt/bin/cswap")
    assert calls[0][0] == "/opt/bin/cswap"


def test_read_failures_are_runtime_errors():
    run, _ = _runner("", returncode=2, stderr="boom")
    with pytest.raises(RuntimeError, match="exit 2: boom"):
        claude_swap.read(run=run)
    run, _ = _runner("not json")
    with pytest.raises(RuntimeError, match="bad json"):
        claude_swap.read(run=run)
    run, _ = _runner("{}")
    with pytest.raises(RuntimeError, match="bad json"):
        claude_swap.read(run=run)

    def broken(cmd: list[str], **kwargs: Any) -> Any:
        raise FileNotFoundError("cswap")

    with pytest.raises(RuntimeError, match="cswap list failed"):
        claude_swap.read(run=broken)


def _failed_row(good_at: str | None, status: str = "error") -> dict[str, Any]:
    row: dict[str, Any] = {"number": 7, "usageStatus": status, "usage": None}
    if good_at is not None:
        row["lastGoodUsage"] = {
            "sevenDay": {"pct": 40.0, "resetsAt": "2026-11-22T17:00:00Z"},
            "scoped": [{"name": "Fable", "pct": 20.0, "resetsAt": "2026-11-22T17:00:00Z"}],
        }
        row["lastGoodFetchedAt"] = good_at
    return row


def test_last_good_within_the_window_is_read_stale():
    (a,) = claude_swap.parse_list([_failed_row("2026-11-18T12:40:00Z")], NOW)
    assert a.usable and a.stale and a.used_pct == 40 and a.secondary_pct == 20
    assert a.fetched_at == datetime(2026, 11, 18, 12, 40, tzinfo=timezone.utc)


def test_last_good_past_the_window_stays_unusable():
    (a,) = claude_swap.parse_list([_failed_row("2026-11-18T12:00:00Z")], NOW)
    assert not a.usable and not a.stale and a.used_pct is None


def test_last_good_window_follows_the_config():
    from quotapilot import Config

    row = _failed_row("2026-11-18T12:00:00Z")
    (a,) = claude_swap.parse_list([row], NOW, config=Config(last_good_max_min=120))
    assert a.usable and a.stale


def test_last_good_needs_now_and_a_stamp():
    assert not claude_swap.parse_list([_failed_row("2026-11-18T13:20:00Z")])[0].usable
    assert not claude_swap.parse_list([_failed_row(None)], NOW)[0].usable


def test_ok_row_takes_fetched_at_from_usage_fetched_at():
    row = dict(PAYLOAD["accounts"][1], usageFetchedAt="2026-11-18T13:25:00Z")
    (a,) = claude_swap.parse_list([row], NOW)
    assert a.fetched_at == datetime(2026, 11, 18, 13, 25, tzinfo=timezone.utc) and not a.stale
    assert claude_swap.parse_list([PAYLOAD["accounts"][1]], NOW)[0].fetched_at is None
