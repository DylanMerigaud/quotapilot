"""Command line: `python -m quotapilot snapshot` and `python -m quotapilot read`."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Sequence

from . import snapshot as snap
from .adapters import claude_swap
from .core import Credit, Sample, average_pace, forecast, pace_from_history


class _SourceError(Exception):
    """An expected failure: printed on stderr, exit 2."""


def _load_samples(path: str) -> list[Sample]:
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
        return [
            (snap._load_at(r[0], "sample at"), str(r[1]), None if r[2] is None else float(r[2]))
            for r in rows
        ]
    except (OSError, ValueError, TypeError, IndexError, KeyError) as e:
        raise _SourceError(f"samples file {path}: {e}") from e


def _load_credits(path: str) -> list[Credit]:
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
        if not isinstance(rows, list):
            raise ValueError("not a JSON list")
        return [snap.credit_from_json(r) for r in rows]
    except (OSError, ValueError, TypeError) as e:
        raise _SourceError(f"credits file {path}: {e}") from e


def _cmd_snapshot(args: argparse.Namespace) -> int:
    samples = _load_samples(args.samples) if args.samples else []
    credits = _load_credits(args.credits) if args.credits else []
    try:
        accounts = claude_swap.read(binary=args.binary, secondary_name=args.secondary)
    except RuntimeError as e:
        raise _SourceError(str(e)) from e
    now = datetime.now(timezone.utc)
    samples = [s for s in samples if s[0] >= now - timedelta(days=7)]  # the last 7 days
    source = args.source or socket.gethostname()
    print(
        snap.dumps(
            snap.Snapshot(
                at=now, source=source, accounts=accounts, samples=samples, credits=credits
            )
        )
    )
    return 0


def _read_source(src: str) -> snap.Snapshot:
    try:
        if os.path.isfile(src):
            with open(src, encoding="utf-8") as f:
                return snap.loads(f.read())
        out = subprocess.run(src, shell=True, capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise _SourceError(
                f"source {src!r} exit {out.returncode}: {(out.stderr or '').strip()[:120]}"
            )
        return snap.loads(out.stdout)
    except _SourceError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        raise _SourceError(f"source {src!r}: {type(e).__name__}: {e}") from e


def _cmd_read(args: argparse.Namespace) -> int:
    snapshots = [_read_source(s) for s in args.source]
    now = datetime.now(timezone.utc)
    merged = snap.merge(snapshots, now)
    if args.pace is not None:
        pace, how = args.pace, "given"
    else:
        measured = pace_from_history(merged.samples, now)
        if measured is not None:
            pace, how = measured, "measured"
        else:
            pace, how = average_pace(merged.accounts, now), "average"
    print(
        json.dumps(
            {
                "snapshot": snap.to_json(merged),
                "pace_pct_h": pace,
                "pace_source": how,
                "forecast": forecast(merged.accounts, pace, now),
            }
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quotapilot", description="Weekly quota across accounts.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("snapshot", help="read claude-swap and print a snapshot as JSON")
    p.add_argument("--source", help="name of this machine (default: its hostname)")
    p.add_argument("--binary", default="cswap", help="the claude-swap executable")
    p.add_argument("--secondary", help="name of the per-model window used as secondary")
    p.add_argument("--samples", help="JSON file of [at, name, pct] rows to carry as history")
    p.add_argument("--credits", help="JSON list of credits to carry (account, id, kind, expires)")
    p.set_defaults(func=_cmd_snapshot)
    p = sub.add_parser("read", help="merge snapshots and print the forecast as JSON")
    p.add_argument(
        "--source",
        action="append",
        required=True,
        help="a snapshot JSON file, or a shell command printing one (repeatable)",
    )
    p.add_argument("--pace", type=float, help="points per hour (default: measured, else average)")
    p.set_defaults(func=_cmd_read)
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except _SourceError as e:
        print(f"quotapilot: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"quotapilot: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
