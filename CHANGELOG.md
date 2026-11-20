# Changelog

## 0.5.0

- The smart rule of `plan_credits`: a reset is spent only when it buys something. Three defaults
  changed, because 0.4.0 asked for a reset to recover almost nothing (a card for 2 points on an
  83 point reset): `plan_min_value_pct` 1.0 to 10.0, `credit_expiry_margin_h` 24 to 48, and the
  new `credit_min_value_share` 0.5 (a spend must recover at least half of what it clears).
  Outside the expiry margin a pool in surplus keeps the credit (`pool_surplus`); a value under the
  minimum or the share is `keep low_share`; a pool that is never dry is `keep pool_fine`; a dry
  stretch the credit cannot help is `keep no_gain_at_pace`. Inside the margin the share is waived:
  a spend worth the minimum now is `now before_expiry`, else the best later moment worth the
  minimum is planned, else `let_expire`. Ties between moments go
  toward the later one (the option stays open), unless the pool is dry now.
- A secondary (for example Fable) credit is kept with `not_spent`, `no_secondary_work`,
  `switch_instead` (another account has secondary and weekly room: switch, do not reset) or
  `weekly_spent` before any simulation, and a `now` says `secondary_spent_weekly_room`.
- Plan rows gain `clears_pct`, `share` and `now_since`; `plan_credits(..., previous=rows)` keeps
  the `now_since` of a credit that was already `now`.
- `capacity_view(accounts, pace, now, ...)`: the pool's capacity in points an hour against the
  pace, the unused points in accounts' worth, a verdict (`short`, `drop_one`, `consume_more`,
  `balanced`) and the picture after each known end date. `expired_room(samples, now)`: the
  points each account let expire at its resets.

- `Snapshot`, `merge`, `snapshot_dumps`, `snapshot_loads` (module `quotapilot.snapshot`: `to_json`,
  `from_json`, `dumps`, `loads`): what one machine read, as JSON (schema 1), and the merge of
  several: the freshest usable reading per account wins, samples are the deduplicated union,
  credits are keyed by account and id.
- `Account.fetched_at` and `Account.stale`; `Config.last_good_max_min` (60).
- claude-swap adapter: a failed row carrying `lastGoodUsage` and `lastGoodFetchedAt` is read stale
  while that reading is at most `last_good_max_min` old; `parse_list` and `read` take a `config`.
- Command line: `python -m quotapilot snapshot` (with `--credits FILE`, and `--samples` cut to the
  last 7 days) and `python -m quotapilot read --source ...`
  (a snapshot file, or a shell command printing one), which prints the merged snapshot and a
  forecast as JSON.

## 0.4.0

- `plan_credits`: plan when to spend each on-demand reset credit by forward simulation of the
  pool (decision `now`, `planned`, `keep`, `let_expire` (only within `credit_expiry_margin_h` of
  expiry), `fallback` or `unknown`, `planned_at`,
  `value_pct`, `now_value_pct`, a reason), plus a week forecast with and without the planned
  credits. A full reset keeps the natural reset date (measured on Claude accounts in November
  2026); `credit_semantics="restart_window"` exists for providers that restart the window.
- `pace_profile`: 24 hourly demand factors (mean 1.0) from the last 7 days of samples.
- `Config`: `plan_step_h` (0.25), `plan_horizon_h` (672), `plan_min_value_pct` (1.0),
  `plan_tie_pct` (0.5), `plan_now_tol_h` (0.25), `plan_expiry_guard_h` (1.0), `profile_min_days`
  (3.0), `profile_floor` (0.05), `credit_semantics` ("keep_date").

## 0.3.0

- `advise_credits`: for each on-demand reset `Credit` (a provider-granted reset of an account's
  main or secondary window), say `now` or `wait` (or `unknown` when the account is unread) with a
  reason code, the percent it would clear (`gain_pct`) and the hours to its expiry and to the
  natural reset of the window it clears.
- `Credit`: the credit (`account`, `id`, `kind` "full" or "secondary", `expires`, `left`, `usable`).
- `Config.credit_expiry_margin_h` (24), `credit_min_lead_h` (24) and `spent_pct` (99).

## 0.2.0

- `forecast`: one week ahead at a pace with every window refilling at its reset, in three modes
  (`surplus`, `on_track`, `short`) and the first dry stretch (`dry_at`, `dry_until`, `dry_hours`).
- `pace_band`: the floor (lowest pace whose lost units stay under `burn_floor_units`) and the
  ceiling (highest pace whose dry time stays under `short_min_h` hours) in points per hour.
- `Config.short_min_h`: how many dry hours make a `short` (default 1).

## 0.1.0

First release.

- `decide`: project what each account loses at the measured (or average) pace, which account to
  drain first, burn or normal, and the secondary-window-first rule with carry-over.
- `Account`, `Config`, `project`, `pace_from_history`, `average_pace`.
- `quotapilot.adapters.claude_swap`: read `cswap list --json` into accounts.
