# Contributing

Bug reports with the numbers that surprise you are the most useful thing: the accounts
(used percent and reset times), the history samples and the `Config` you passed.

Changes are made here. A change is released as a new version, and the projects that use
quotapilot bump their pin.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check . && mypy quotapilot
```

## Rules

- `quotapilot/core.py` imports the standard library only. No runtime dependency.
- Python 3.9 compatible.
- Every datetime is timezone-aware.
- A change to the algorithm comes with a test that pins the numbers.
