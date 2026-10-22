# RMS Race Day

Admin web app for the RMS kart team (LO206 / Ignite Sr): drivers, races, schedule, garage, setups and lap times.
It covers what the Discord bot's `/kart` commands do, without the parts inventory.

Flask + SQLite + Bootstrap 5. No build step.

## Run it

```bash
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python seed.py        # optional: demo data
.venv\Scripts\python app.py         # http://localhost:5000
```

`python seed.py --reset` wipes the database and reseeds it.

## Bring over the Discord bot's data

```bash
.venv\Scripts\python import_bot_db.py "C:\path\to\setup_bot\kart_setups.db"
```

Only reads the bot's file. Each Discord user becomes a driver named `Discord user 1234` (rename them on the Drivers page).
Each class + chassis becomes a kart. Setups, runs and tire sets come across. Other parts are skipped. Safe to run again.

## Pages

| Page | What's there |
|---|---|
| Dashboard | Countdown to the next event, team totals, recent runs, track records, latest results, tire sets near end of life |
| Drivers | Roster with classes, wins and podiums. The driver page shows karts, setups, results and runs |
| Races | Race cards with RMS entries, best finish and fast lap. The race page has results (qualifying → finish +/-) and every session logged at that event |
| Schedule | Upcoming races, practice days and test days grouped by month. Includes past events with *Show past* |
| Garage | Each driver's karts (chassis, engine, seal #) and tire sets with a run-count life bar |
| Setups | Filterable setup cards, gear calculator, clone, side-by-side compare with differences highlighted |
| Times | Run log with filters (driver, class, track, session, date) and a best-lap leaderboard per track and class |

Logging a run adds one run to the chosen tire set and records the set's age on that run. Editing or deleting the run updates the count.

## Configuration (env vars)

| Var | Default |
|---|---|
| `RMS_DB_PATH` | `rms_race_day.db` next to `app.py` |
| `KART_CLASSES` | `LO206,Ignite Sr` |
| `TIRE_LIFE_RUNS` | `20` (bar turns amber at 75%, red at 100%) |
| `SECRET_KEY` | dev value. Set a real one before hosting |
| `PORT` | `5000` |

## Logo

`static/img/rms-logo.svg` is a placeholder. Drop the real logo in at the same path (or change the `<img>` in
`templates/base.html` if it's a PNG).

## Not in the MVP

- Logins: everyone gets the admin view. Add auth before putting this on the internet.
- Driver-facing views.
- Live sharing with the Discord bot. They use separate databases; use the import script for now.
