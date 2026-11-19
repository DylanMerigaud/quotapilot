"""Live contract: the adapter against the claude-swap release users install today.

These tests import the real `claude_swap` package and run the real `cswap` binary, so they skip
when claude-swap is absent. The weekly contract workflow installs the newest claude-swap from
PyPI and sets QUOTAPILOT_CONTRACT=1, which turns the skip into a failure.

The rows are built by claude-swap's own `--list --json` serializer (`json_output.account_row`),
from usage it reads back with its own `usage_from_json`, so a renamed key, a moved window or a
new status upstream fails here before a user meets it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from quotapilot import decide
from quotapilot.adapters import claude_swap as adapter

if os.environ.get("QUOTAPILOT_CONTRACT"):
    import claude_swap.json_output as upstream
else:
    upstream = pytest.importorskip("claude_swap.json_output")

NOW = datetime(2026, 11, 18, 13, 27, tzinfo=timezone.utc)

USAGE_JSON: dict[str, Any] = {
    "fiveHour": {"pct": 12, "resetsAt": "2026-11-18T17:00:00Z"},
    "sevenDay": {"pct": 62.5, "resetsAt": "2026-11-22T17:00:00Z"},
    "scoped": [
        {"name": "Opus", "pct": 30, "resetsAt": "2026-11-24T17:00:00Z"},
        {"name": "Sonnet", "pct": 26, "resetsAt": "2026-11-24T18:00:00Z"},
    ],
}


def _row(number: int, entry: Any, active: bool = False) -> dict[str, Any]:
    row: dict[str, Any] = upstream.account_row(
        number, f"a{number}@example.com", "", "", active, entry, usage_fetched_at=NOW.timestamp()
    )
    return row


def _payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    # Through JSON text, as `cswap list --json` hands it to a script.
    out: dict[str, Any] = json.loads(
        json.dumps(
            {"schemaVersion": upstream.SCHEMA_VERSION, "activeAccountNumber": 1, "accounts": rows}
        )
    )
    return out


def test_schema_version_is_the_one_the_adapter_reads():
    assert upstream.SCHEMA_VERSION == 1


def test_an_ok_row_gives_the_weekly_window_and_the_scoped_secondary():
    usage = upstream.usage_from_json(USAGE_JSON)
    accs = adapter.parse_list(_payload([_row(1, usage, active=True)]), NOW)
    assert len(accs) == 1
    one = accs[0]
    assert one.name == "1" and one.usable
    assert one.used_pct == 62.5
    assert one.reset == datetime(2026, 11, 22, 17, tzinfo=timezone.utc)
    assert one.secondary_pct == 30
    assert one.secondary_reset == datetime(2026, 11, 24, 17, tzinfo=timezone.utc)


def test_secondary_name_picks_the_named_scoped_window():
    usage = upstream.usage_from_json(USAGE_JSON)
    accs = adapter.parse_list(_payload([_row(1, usage)]), NOW, secondary_name="Sonnet")
    assert accs[0].secondary_pct == 26
    assert accs[0].secondary_reset == datetime(2026, 11, 24, 18, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "sentinel",
    [
        upstream.USAGE_TOKEN_EXPIRED,
        upstream.USAGE_API_KEY,
        upstream.USAGE_NO_CREDENTIALS,
        upstream.USAGE_RELOGIN_REQUIRED,
        None,
    ],
)
def test_an_unread_row_is_not_usable(sentinel: Any):
    accs = adapter.parse_list(_payload([_row(2, sentinel)]), NOW)
    assert not accs[0].usable and accs[0].used_pct is None and accs[0].reset is None


def test_a_weekly_window_without_a_percent_is_not_usable():
    week = {"pct": None, "resetsAt": "2026-11-22T17:00:00Z"}
    usage = upstream.usage_from_json({"sevenDay": week})
    accs = adapter.parse_list(_payload([_row(3, usage)]), NOW)
    assert not accs[0].usable and accs[0].used_pct is None


def test_decide_runs_on_a_payload_built_upstream():
    usage = upstream.usage_from_json(USAGE_JSON)
    accs = adapter.parse_list(_payload([_row(1, usage, active=True), _row(2, None)]), NOW)
    result = decide(accs, [], NOW)
    assert result["state"] in ("burn", "normal")
    assert result["drain_first"] == "1"


def test_the_cswap_binary_answers_list_json(tmp_path: Path):
    binary = shutil.which("cswap")
    if binary is None:
        if os.environ.get("QUOTAPILOT_CONTRACT"):
            pytest.fail("cswap is not on PATH")
        pytest.skip("cswap is not on PATH")
    env = dict(os.environ)
    env.update(
        HOME=str(tmp_path),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_DATA_HOME=str(tmp_path / "data"),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
        XDG_STATE_HOME=str(tmp_path / "state"),
    )

    def run(*args: Any, **kwargs: Any) -> Any:
        return subprocess.run(*args, env=env, **kwargs)

    assert adapter.read(run=run, binary=binary, timeout=120) == []
