# Changelog

## 0.2.0

- `forecast`: one week ahead at a pace with every window refilling at its reset, in three modes
  (`surplus`, `on_track`, `short`) and the first dry stretch (`dry_at`, `dry_until`, `dry_hours`).
- `pace_band`: the floor (lowest pace that wastes nothing) and the ceiling (highest pace that
  never runs the pool dry) in points per hour.
- `Config.short_min_h`: how many dry hours make a `short` (default 1).

## 0.1.0

First release.

- `decide`: project what each account loses at the measured (or average) pace, which account to
  drain first, burn or normal, and the secondary-window-first rule with carry-over.
- `Account`, `Config`, `project`, `pace_from_history`, `average_pace`.
- `quotapilot.adapters.claude_swap`: read `cswap list --json` into accounts.
