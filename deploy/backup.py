"""Copy the live SQLite database safely while the app runs, keeping the newest N copies.

    python deploy/backup.py /var/lib/rms-race-day/rms_race_day.db /var/lib/rms-race-day/backups 14
"""
import datetime as dt
import pathlib
import sqlite3
import sys

src, folder, keep = sys.argv[1], pathlib.Path(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 14
folder.mkdir(parents=True, exist_ok=True)
target = folder / f"rms_race_day-{dt.datetime.now():%Y-%m-%d}.db"
with sqlite3.connect(src) as live, sqlite3.connect(target) as copy:
    live.backup(copy)  # consistent even mid-write, unlike copying the file
for old in sorted(folder.glob("rms_race_day-*.db"))[:-keep]:
    old.unlink()
print(f"Backed up to {target}")
