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

Only reads the bot's file. Each Discord user becomes a driver named `Discord user 1234` with number `D1234`
(rename and renumber them on the Drivers page); their latest setup becomes their current one. Each class + chassis
becomes a kart and each track name a track. Setups, runs and tire sets come across. Other parts are skipped. Safe to run again.

## Race-day commands

The black bar at the top of every page takes shorthand (press `/` to jump to it):

| Command | Does |
|---|---|
| `24 SPR 65` | #24's rear sprocket → 65 |
| `24 SPR 17/65` | front / rear sprocket |
| `24 FTW 1.5` | front track width |
| `24 RTW 0.75` | rear track width |
| `24 PSI 30` · `24 PSI 30/32` | all tires · front / rear |
| `24 SPR +1` · `24 PSI -0.5` | relative change |
| `24 SPR 61 PSI 30; 9 RTW 1` | several changes, several drivers |
| `UNDO` | revert the most recent change |

Keywords are case-insensitive. As you type, a preview shows exactly what will change and warns when a value is
outside the usual range (sprocket 58–63T, Hoosier 28–36 psi). Enter applies it.

A change never edits a setup in place, because other drivers may share it. The driver moves to an existing setup with
exactly the new values, or the app creates one (named from its numbers) and moves them to it. Every change is logged on
the Dashboard, Drivers page and driver page with an undo button. Undoing a change that created a setup deletes that
setup if nothing else uses it.

Clicking a SPR / FTW / RTW / PSI value in a lineup starts the command for that driver.

## How things relate

- **Setups** and **karts** exist on their own.
- Every **driver** has a number (unique), a class, a kart and a current setup. Drivers can share a setup or a kart.
- A **run** records the driver, setup, kart, track and tire set used at that time.
- **Tracks** have an optional "good lap". Lap times show how far they are off it.

## Pages

| Page | What's there |
|---|---|
| Dashboard | Countdown to the next event, team totals, recent runs, track records, latest results, tire sets near end of life |
| Drivers | Lineup with each driver's current SPR / FTW / RTW / PSI and the change log. The driver page shows their setup, kart, tires, changes, runs and results |
| Races | Race cards with RMS entries, best finish and fast lap. The race page has results (qualifying → finish +/-) and every session logged at that event |
| Schedule | Upcoming races, practice days and test days grouped by month (*Show past* for history), plus the track list with good laps |
| Garage | Karts (chassis, engine, seal #, who drives it) and tire sets with a run-count life bar |
| Setups | Setup cards showing who uses each one, gear calculator, clone, side-by-side compare with differences highlighted |
| Times | Run log with filters (driver, class, track, session, date), gap to the track's good lap, and a leaderboard per track and class |

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
- Older databases: the app moves a pre-commands database aside (`rms_race_day.v1.bak.db`) and starts fresh.
- Live sharing with the Discord bot. They use separate databases; use the import script for now.
