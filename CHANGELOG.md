# Changelog

## 0.4.0

- `plan_credits`: plan when to spend each on-demand reset credit by forward simulation of the
  pool (decision `now`, `planned`, `keep`, `let_expire`, `fallback` or `unknown`, `planned_at`,
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
