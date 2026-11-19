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

## Three modes: surplus, on track, short

`forecast(accounts, pace, now)` walks one week ahead at a pace (points per hour) with every
window refilling at its reset, draining the live window that resets soonest. It returns `mode`:
`surplus` (the lost units reach `burn_floor_units`), `short` (the pool is dry for at least
`short_min_h` hours) or `on_track`, plus `lost_units`, the first dry stretch (`dry_at`,
`dry_until`, `dry_hours`) and one row per account (`projected_pct`, `lost_pct`, `empty_at`).
`pace_band(accounts, now)` returns `(floor, ceiling)`: the lowest pace that is not a surplus and
the highest pace that is not short.

```python
from quotapilot import forecast, pace_band

view = forecast(accounts, 1.0, now)
print(view["mode"], view["lost_units"], view["dry_at"], view["dry_hours"])
for row in view["accounts"]:
    print(row["name"], row["projected_pct"], row["empty_at"])
floor, ceiling = pace_band(accounts, now)
print(f"stay between {floor} and {ceiling} points per hour")
```

It is a reading: it never caps or throttles anything.

## On-demand reset credits

Some providers grant a credit you can spend to reset an account's quota on demand. A `Credit`
names its account, a `kind` (`"full"` clears the main weekly window, `"secondary"` clears the
secondary window only), when it expires, how many resets are `left` and whether the provider says
it is `usable` now. `advise_credits(accounts, credits, now, mode, demand)` says for each one
`now`, `wait` or `unknown`, with a `reason`.

In plain words: spend a credit when the window it clears is spent (at least `spent_pct`, 99) and
its natural reset is still far (at least `credit_min_lead_h`, 24 hours), or when the credit
expires within `credit_expiry_margin_h` (24 hours) and has something to clear. Wait otherwise:
the window is not spent, the natural reset is close, or the pool is in `surplus` (pass the
`forecast` mode as `mode`). A secondary credit also needs room left in the weekly window
(`secondary_min_room_pct`) and, when you pass `demand` (account name to whether work waits),
work waiting. An unread or unusable account gives `unknown`. A natural reset already past counts
as refilled. Credits with nothing left or already expired are skipped.

```python
from datetime import datetime, timedelta, timezone

from quotapilot import Account, Credit, advise_credits

now = datetime(2026, 11, 18, 22, 0, tzinfo=timezone.utc)
accounts = [Account("4", 100, now + timedelta(hours=80))]
credits = [Credit("4", "full-reset-2611", "full", now + timedelta(days=20))]
row = advise_credits(accounts, credits, now, mode="on_track")[0]
print(row["verdict"], row["reason"], row["gain_pct"])  # now spent_far_reset 100.0
```

## Planning on-demand resets (0.4.0)

`advise_credits` looks at one moment. `plan_credits(accounts, credits, pace, now, profile=None,
secondary_pace=None)` looks ahead: it simulates the pool step by step (`plan_step_h`, 15 minutes)
over up to `plan_horizon_h` (28 days), demand drained from the window with the soonest reset, and
says for each live credit when to spend it: `now`, `planned` (with `planned_at`), `keep`
(nothing worth recovering at this pace, so far from expiry it is kept and re-planned later),
`let_expire` (the same, but the credit expires within `credit_expiry_margin_h`, 24 hours), `fallback` (a secondary credit without a secondary pace: use `advise_credits`) or
`unknown`, with a reason.

The value of a spend at time s is the extra points the pool serves over the horizon with the
credit spent at s than without it. Candidates are the moments that can matter: now, the pool going dry,
the account hitting its ceiling, each natural reset, each spend already planned, and the last
chance before expiry. A plan under
`plan_min_value_pct` (1 point) is never made; candidates within `plan_tie_pct` of the best are
ties, settled for a dry moment first, then the earliest. Credits are planned in order of expiry
and each plan counts the spends planned before it. The result also carries a week `forecast` with
and without the planned credits (`covered_hours`, `covered_by`).

A full reset keeps the natural reset date of the account (measured on Claude accounts in
November 2026): spending it too early only gives back what the natural reset would have given.
`Config(credit_semantics="restart_window")` exists for providers that restart the window
(reset moved to the spend time plus a week). `pace_profile(history, now)` turns a few days of
samples into 24 hourly factors (mean 1.0) to pass as `profile`, so demand follows the day.

```python
from datetime import datetime, timedelta, timezone

from quotapilot import Account, Credit, plan_credits

now = datetime(2026, 11, 19, tzinfo=timezone.utc)
accounts = [Account("4", 100, now + timedelta(hours=96))]
credits = [Credit("4", "full-reset-2611", "full", now + timedelta(days=12))]
row = plan_credits(accounts, credits, 1.5, now)["credits"][0]
print(row["decision"], row["reason"], row["value_pct"])  # now pool_dry 99.0
```

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
