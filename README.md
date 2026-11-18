# quotapilot

You have several accounts, each with a weekly quota window that resets at its own time. Whatever
is not used before a reset is lost. quotapilot looks at how much each account has used and how
fast you burn, projects what expires unused, tells you which account to drain first, and says
whether you are in `burn` (something will expire) or `normal` (nothing will).

Pure Python, standard library only, no network, Python 3.9 and up.

```bash
pip install quotapilot
```

## Example

```python
from datetime import datetime, timedelta, timezone

from quotapilot import Account, decide

now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
accounts = [
    Account("work", used_pct=80, reset=now + timedelta(hours=20)),
    Account("personal", used_pct=10, reset=now + timedelta(hours=100)),
]
# history: (when, account name, used percent) samples of the last day, oldest first
history = [(now - timedelta(hours=8), "work", 72), (now, "work", 80)]

result = decide(accounts, history, now)
print(result["summary"])
print(result["state"], result["drain_first"], result["pace_pct_h"])
```

`decide` returns a dict: `state` (`burn` or `normal`), `drain_first`, `pace_pct_h` and
`pace_source` (`measured` from your history, else the `average` over the elapsed weeks),
`lost_units` and `unavoidable_units`, one row per account (`room_pct`, `lost_pct`, `lost_units`,
`empty_at`), and a one-line English `summary` with times in UTC.

A unit is what you decide it is. By default one unit is one percent point of one account-week.
Set `Config(units_per_week=1000, max_units_per_h=40, burn_floor_units=10)` to count in your own
units, to get `unavoidable_units` (what would still expire even at your maximum pace), and to say
how many lost units make a `burn`.

The optional secondary window (`Account.secondary_pct`, `secondary_reset`) is a second, narrower
quota some providers add, such as a per-model weekly limit. When the account to drain first loses
nothing, the account whose secondary window just reset or dropped is named in `secondary_first`
and held there for `secondary_hold_h` hours; pass the last result back as `previous=` to carry it.

## With claude-swap

quotapilot is an addon above [claude-swap](https://github.com/realiti4/claude-swap), not a
replacement. claude-swap holds your accounts and does the switching. quotapilot only decides.
Feed it the output of `cswap list --json`:

```python
from datetime import datetime, timezone

from quotapilot import decide
from quotapilot.adapters import claude_swap

# accounts = claude_swap.read()  # runs `cswap list --json` and parses it
payload = {  # the shape of `cswap list --json`
    "accounts": [
        {
            "number": 1,
            "usageStatus": "ok",
            "usage": {"sevenDay": {"pct": 42, "resetsAt": "2026-11-21T09:00:00Z"}},
        },
        {
            "number": 2,
            "usageStatus": "ok",
            "usage": {"sevenDay": {"pct": 90, "resetsAt": "2026-11-19T09:00:00Z"}},
        },
    ]
}
accounts = claude_swap.parse_list(payload)
now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
print(decide(accounts, [], now)["summary"])
```

`parse_list(data, secondary_name=...)` picks the per-model weekly window of `usage.scoped` by
name for the secondary window (the first one when omitted).

## What it does not do

- It does not switch accounts. You (or claude-swap) act on `drain_first`.
- The core makes no network call and calls no provider API. Only `claude_swap.read()` runs
  `cswap list --json`, and claude-swap fetches the usage itself.
- It stores no account, no credential and no history: you pass the samples in.

## Contributing

Changes are made in this repository, released as a new version, and the projects that use
quotapilot bump their pin. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT
