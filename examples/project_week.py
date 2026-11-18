"""Project a week on three synthetic accounts, offline."""

from datetime import datetime, timedelta, timezone

from quotapilot import Account, Config, decide

now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
accounts = [
    Account("work", used_pct=95, reset=now + timedelta(hours=10)),
    Account("personal", used_pct=20, reset=now + timedelta(hours=60)),
    Account("spare", used_pct=5, reset=now + timedelta(hours=120)),
]
history = [(now - timedelta(hours=8), "work", 87), (now, "work", 95)]

result = decide(accounts, history, now, config=Config(max_units_per_h=3.0))
print(result["summary"])
for row in result["accounts"]:
    print(f"{row['name']:>9}  room {row['room_pct']:5.1f}%  lost {row['lost_pct']:5.1f}%")
print("unavoidable:", result["unavoidable_units"])
