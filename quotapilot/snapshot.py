"""A snapshot of the accounts read on one machine, and the merge of several.

Work may run on a server while the bar is read on a laptop. Each machine writes a `Snapshot`
(JSON), and `merge` keeps the freshest usable reading per account. Stdlib only, every datetime
timezone-aware.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from .core import DEFAULT, Account, Config, Credit, Sample

SCHEMA = 1


@dataclass
class Snapshot:
    """What one machine read at `at`: accounts, the usage samples it kept, and credits."""

    at: datetime
    source: str
    accounts: list[Account]
    samples: list[Sample] = field(default_factory=list)
    credits: list[Credit] = field(default_factory=list)


def _dump_at(dt: datetime | None, what: str) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError(f"{what} must be timezone-aware")
    return dt.isoformat()


def _load_at(text: Any, what: str) -> datetime:
    if not isinstance(text, str):
        raise ValueError(f"{what} must be an ISO string")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"{what} is not an ISO datetime: {text!r}") from e
    if dt.tzinfo is None:
        raise ValueError(f"{what} must be timezone-aware: {text!r}")
    return dt


def _load_opt(text: Any, what: str) -> datetime | None:
    return None if text is None else _load_at(text, what)


def _num(value: Any, what: str) -> float | None:
    """A percent: None or a real number (a bool or a string is a ValueError)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} must be a number: {value!r}")
    return float(value)


def credit_from_json(c: Mapping[str, Any]) -> Credit:
    """One credit from its snapshot shape. ValueError on a bad shape or naive datetime."""
    try:
        return Credit(
            account=str(c["account"]),
            id=str(c["id"]),
            kind=str(c["kind"]),
            expires=_load_at(c["expires"], f"expires of credit {c['id']}"),
            left=int(c.get("left", 1)),
            usable=bool(c.get("usable", True)),
        )
    except (KeyError, TypeError, AttributeError) as e:
        raise ValueError(f"bad credit: {type(e).__name__}: {e}") from e


def to_json(snapshot: Snapshot) -> dict[str, Any]:
    """The snapshot as a JSON-ready dict (`schema` 1), every datetime an ISO string."""
    accounts = []
    for a in snapshot.accounts:
        accounts.append(
            {
                "name": a.name,
                "used_pct": a.used_pct,
                "reset": _dump_at(a.reset, f"reset of {a.name}"),
                "secondary_pct": a.secondary_pct,
                "secondary_reset": _dump_at(a.secondary_reset, f"secondary_reset of {a.name}"),
                "usable": a.usable,
                "fetched_at": _dump_at(a.fetched_at, f"fetched_at of {a.name}"),
                "stale": a.stale,
            }
        )
    credits = [
        {
            "account": c.account,
            "id": c.id,
            "kind": c.kind,
            "expires": _dump_at(c.expires, f"expires of credit {c.id}"),
            "left": c.left,
            "usable": c.usable,
        }
        for c in snapshot.credits
    ]
    return {
        "schema": SCHEMA,
        "at": _dump_at(snapshot.at, "at"),
        "source": snapshot.source,
        "accounts": accounts,
        "samples": [[_dump_at(s[0], "sample at"), s[1], s[2]] for s in snapshot.samples],
        "credits": credits,
    }


def from_json(obj: Mapping[str, Any]) -> Snapshot:
    """A snapshot from `to_json` output. ValueError on a bad schema, shape or naive datetime."""
    try:
        if obj.get("schema") != SCHEMA:
            raise ValueError(f"unsupported snapshot schema: {obj.get('schema')!r}")
        accounts = []
        for r in obj["accounts"]:
            name = str(r["name"])
            accounts.append(
                Account(
                    name=name,
                    used_pct=_num(r["used_pct"], f"used_pct of {name}"),
                    reset=_load_opt(r["reset"], f"reset of {name}"),
                    secondary_pct=_num(r.get("secondary_pct"), f"secondary_pct of {name}"),
                    secondary_reset=_load_opt(
                        r.get("secondary_reset"), f"secondary_reset of {name}"
                    ),
                    usable=bool(r["usable"]),
                    fetched_at=_load_opt(r.get("fetched_at"), f"fetched_at of {name}"),
                    stale=bool(r.get("stale", False)),
                )
            )
        samples: list[Sample] = [
            (_load_at(s[0], "sample at"), str(s[1]), None if s[2] is None else float(s[2]))
            for s in obj.get("samples", [])
        ]
        credits = [credit_from_json(c) for c in obj.get("credits", [])]
        return Snapshot(
            at=_load_at(obj["at"], "at"),
            source=str(obj["source"]),
            accounts=accounts,
            samples=samples,
            credits=credits,
        )
    except (KeyError, TypeError, IndexError, AttributeError) as e:
        raise ValueError(f"bad snapshot: {type(e).__name__}: {e}") from e


def dumps(snapshot: Snapshot) -> str:
    """The snapshot as JSON text."""
    return json.dumps(to_json(snapshot))


def loads(text: str) -> Snapshot:
    """A snapshot from JSON text. ValueError when it is not valid JSON or not a snapshot."""
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("bad snapshot: not a JSON object")
    return from_json(obj)


def merge(snapshots: Sequence[Snapshot], now: datetime, config: Config = DEFAULT) -> Snapshot:
    """One snapshot from several machines: the freshest usable reading per account wins.

    A reading's time is its `fetched_at`, or its snapshot's `at` when None. A stale reading older
    than `config.last_good_max_min` minutes from `now` turns unusable (its percent is kept for
    display). With no usable reading for an account, the latest unusable one is kept. Samples are
    the union on `(at, name)` (on a clash the one from the later snapshot in the input order is
    kept), sorted; credits are keyed by
    `(account, id)` and the latest snapshot's wins. ValueError on empty input.
    """
    if not snapshots:
        raise ValueError("merge needs at least one snapshot")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    limit = timedelta(minutes=config.last_good_max_min)
    best: dict[str, tuple[Account, datetime]] = {}
    for snap in snapshots:
        for a in snap.accounts:
            when = a.fetched_at or snap.at
            if a.stale and a.usable and now - when > limit:
                a = replace(a, usable=False)
            held = best.get(a.name)
            if held is None:
                best[a.name] = (a, when)
                continue
            old, old_when = held
            if (a.usable, when) >= (old.usable, old_when):
                best[a.name] = (a, when)
    samples: dict[tuple[datetime, str], Sample] = {}
    credits: dict[tuple[str, str], Credit] = {}
    for snap in snapshots:
        for s in snap.samples:
            samples[(s[0], s[1])] = s
    for snap in sorted(snapshots, key=lambda x: x.at):  # stable: input order breaks a tie
        for c in snap.credits:
            credits[(c.account, c.id)] = c
    return Snapshot(
        at=max(s.at for s in snapshots),
        source="+".join(s.source for s in snapshots),
        accounts=[a for a, _ in best.values()],
        samples=sorted(samples.values(), key=lambda s: (s[0], s[1])),
        credits=list(credits.values()),
    )
