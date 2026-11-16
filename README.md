# RMS Race Day

Admin web app for the RMS kart team (LO206 / Ignite Sr): drivers, races, schedule, setups and lap times.
It covers what the Discord bot's `/kart` commands do, without the parts inventory.

Flask + SQLite + Bootstrap 5. No build step.

## Run it

```bash
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python seed.py        # optional: demo data
.venv\Scripts\python app.py         # http://localhost:5000
```

`python seed.py --reset` wipes the database and reseeds it. To sign in locally, set `ADMIN_PASSWORD_HASH` first (see *Sign in*); in PowerShell use single quotes: `$env:ADMIN_PASSWORD_HASH = 'scrypt:...'`.

## Bring over the Discord bot's data

```bash
.venv\Scripts\python import_bot_db.py "C:\path\to\setup_bot\kart_setups.db"
```

Only reads the bot's file. Each Discord user becomes a driver named `Discord user 1234` with number `D1234`
(rename and renumber them on the Drivers page); their latest setup becomes their current one. Each class + chassis
becomes a kart and each track name a track. Setups and runs come across; tire sets and parts are skipped. Safe to run again.

## Sign in

There's one team login. Signed out, anyone can see the **Home** page, **Schedule**, **Standings**, **Drivers** and
**Race results** (finishes, qualifying, best laps, points and who's racing; no setups, karts, practice sessions, contacts
or tuning). Signed in, you see and can change everything.

The password is never stored in the code or database, only as a hash in an environment variable:

```bash
.venv\Scripts\python auth.py
```

It asks for the password (10+ characters) and prints `ADMIN_PASSWORD_HASH='scrypt:…'`. Set that variable where the
app runs. To change the password, make a new hash and replace it; everyone signed in is signed out.

How it's protected:
- Signed session cookie (HttpOnly, SameSite=Lax, HTTPS-only in production), lasts 30 days.
- Every form carries a CSRF token.
- 5 wrong passwords from one IP locks that IP out for 15 minutes.
- Pages send `X-Frame-Options`, `nosniff`, and HSTS in production.

## Running in production

1. Generate the password hash (above) and a secret key: `python -c "import secrets; print(secrets.token_hex(32))"`.
2. Set the environment variables:

   | Var | Value |
   |---|---|
   | `RMS_ENV` | `production` (the app refuses to start without the two below) |
   | `SECRET_KEY` | the random key |
   | `ADMIN_PASSWORD_HASH` | the hash, quoted |
   | `RMS_DB_PATH` | a path on persistent storage, e.g. `/data/rms_race_day.db` |
   | `TRUST_PROXY` | `1` when behind a host's proxy/load balancer (Render, Fly, Railway, nginx) so HTTPS and client IPs are seen correctly |
   | `RMS_TZ` | the team's time zone if not `America/Chicago`; race-day dates follow it, not the server's clock |

3. Serve it with a production server, as **one process** (the login lockout is kept in memory):

   ```bash
   waitress-serve --host 0.0.0.0 --port 8000 --threads 8 app:app
   ```

4. Put it behind HTTPS. Hosting platforms do this for you; otherwise use a reverse proxy such as Caddy.
5. Back up the database file. SQLite is a single file, so copying it (while the app is idle) is a full backup.
   The app updates the database's structure itself when it starts after an upgrade, and keeps a copy of the old file
   next to it first (`*.vN.bak.db`).
6. Don't run `seed.py` against the production database; it's demo data only and refuses to run with `RMS_ENV=production`.

## Free hosting (Oracle Cloud + a free name)

Everything here is free: an Oracle Cloud "Always Free" server, a free name from DuckDNS until you buy a domain, and
HTTPS from Let's Encrypt (Caddy handles it). `deploy/setup.sh` does the server setup in one go.

1. **A free name.** Sign in at [duckdns.org](https://www.duckdns.org) and add a subdomain, e.g. `rmsracing` →
   `rmsracing.duckdns.org`. You'll point it at the server in step 3.
2. **The server.** Sign up for Oracle Cloud's Free Tier (a card is asked for identity checks; Always Free resources
   aren't charged). Create an instance:
   - Image **Ubuntu 24.04**, shape **VM.Standard.A1.Flex** with 1 OCPU / 6 GB (if it's out of capacity, use
     **VM.Standard.E2.1.Micro**). Download the SSH key it offers.
   - On the instance's subnet, open its **security list** and add two ingress rules: source `0.0.0.0/0`, TCP,
     destination ports `80` and `443`.
3. Copy the instance's **public IP** into your DuckDNS subdomain and save.
4. **Set it up.** Connect (`ssh -i <key> ubuntu@<public IP>`), then:

   ```bash
   sudo git clone https://github.com/zmbelles/RMS-race-day-app.git /opt/rms-race-day
   ```
   ```bash
   sudo bash /opt/rms-race-day/deploy/setup.sh rmsracing.duckdns.org
   ```

   It asks for the team password, then installs everything: the app as a service, Caddy for HTTPS, the firewall
   ports and a nightly backup (3am, two weeks kept in `/var/lib/rms-race-day/backups`).
5. **Bring your data.** From your PC, copy the database up, then put it in place on the server:

   ```bash
   scp -i <key> rms_race_day.db ubuntu@<public IP>:/tmp/
   ```
   ```bash
   sudo systemctl stop rms-race-day && sudo install -o rms -g rms -m 640 /tmp/rms_race_day.db /var/lib/rms-race-day/ && sudo systemctl start rms-race-day
   ```

Then open `https://rmsracing.duckdns.org`.

- **Updating** after you push to GitHub: `sudo bash /opt/rms-race-day/deploy/update.sh`
- **Your own domain later:** add an `A` record for it pointing at the same IP, then run `setup.sh` again with the new
  name. The database, password and secret key stay as they are.
- **Logs:** `journalctl -u rms-race-day -f`. **Change the password:** delete `/etc/rms-race-day/admin_password_hash`
  and run `setup.sh` again.
- Settings live in `/etc/rms-race-day/env` (see *Configuration*); restart with `sudo systemctl restart rms-race-day`.

## Race-day commands

The black bar at the top of every page takes shorthand (press `/` to jump to it):

| Command | Does |
|---|---|
| `24 SPR 65` | #24's rear sprocket → 65 (the front is fixed) |
| `24 9 13 88 PSI 31` | the same change for several karts at once (each is logged and undone on its own) |
| `24 FTW 1.5` | front track width |
| `24 RTW 0.75` | rear track width |
| `24 PSI 30` | tire pressure (same on all four) |
| `24 FRH 4` | front ride height: shims above the spindle out of 6 (4 above, 2 below; 3 is standard) |
| `77 heavy SPR 61` | name the class when a number is used in more than one class |
| `24 TRK norway` | take #24's setup to another track (short name, or the start of the track name) |
| `24 SPR +1` · `24 PSI -0.5` | relative change |
| `24 SPR 61 PSI 30; 9 RTW 1` | several changes, several drivers |
| `UNDO` | revert the most recent change |

Keywords are case-insensitive. As you type, a preview shows exactly what will change and warns when a value is
outside the usual range (sprocket 58–63T, Hoosier 28–36 psi). Enter applies it.

Setups belong to a track and are numbered there: Gateway 1, Gateway 2, Norway 1. A change never edits a setup in place,
because other drivers may share it. The driver moves to the setup at the same track with exactly the new values, or the
app creates the next one at that track (Gateway 3) and moves them to it. `TRK` does the same at another track. Track
short names (used in setup names and `TRK`) are set on the track; renaming one renames its setups. Every change is logged on
the Dashboard, Drivers page and driver page with an undo button. Undoing a change that created a setup deletes that
setup if nothing else uses it.

Clicking a SPR / FTW / RTW / PSI value in a lineup starts the command for that driver.

### Race day

**Race day** (top of the sidebar, team only) is where a weekend is run from. Pick the race or RMS test day at the top
(it defaults to the one on now, else the next one), **Assign drivers** (who's racing, their class and kart number
there), and each driver gets a card with their current setup and their runs at that event. Click a setup value to start
a command, drag a setup from the tray on the right onto a driver (or use their *Switch setup* list) to put them on it,
and **Log run** at the top steps through every driver on the entry list for one session: the session, date and
conditions are filled in once, then each driver's best lap, laps, setup and notes, with Enter moving to the next driver.
Drivers left blank are skipped. Logging runs only happens here. Opening an event on Race day also makes it the race-day
event for the command bar.

**Day report** (on Race day, and the ✦ next to each day of the weekend) writes a day up as Markdown for an AI summary: sessions and conditions, every driver's laps (against the good lap and their teammates), setup changes with the commands used, results, their best at that track before, the full specs of every setup involved, what's still to come, and the questions to answer. Copy it or download the .md and paste it into an AI chat.

**Weekend format** (on Race day and the race page) lays out the weekend day by day: practice sessions, qualifying,
heats and the feature, with optional start times. Presets cover a club day, heats & final, and qualifying & two races.
Race day shows each session as done once everyone has a run in it, and Log run starts on the next one. Anything that
isn't practice is scored, so saving a format also sets the race's scored sessions (unless results are already in under
a session the format drops).

### Race-day entry lists

Before a race, open it (signed in) and click **Entry list**: tick who's racing, in which class, and the kart number each
runs that weekend (e.g. Zach as #524). Then **Use for race day**. Until you clear it (× next to *Race day* under the
command bar), commands read numbers from that list, so `524 PSI 31` is Zach and a number not on the list is refused.
A race with an entry list that's running today is used automatically. The dashboard lineup shows the entry list with
those numbers, and the race's results grid lists just those entries with their numbers filled in. If the same number is
in two classes at one race, add the class in commands (`77 heavy PSI 31`).


## How things relate

- **Setups** belong to a **track** and are numbered per track. **Karts** exist on their own.
- A **driver** races one class at a time, with a kart number, kart and current setup. **Swap class** on the driver page changes all four (e.g. Michael from #7 Ignite Sr to #507 LO206 Sr on his Brava). Past runs and results keep the class and number they were run with, so history and per-class standings don't move. Numbers are unique within a class. The results grid has a class per row for entering a race run in another class.
- **Retire** a driver who leaves the team: they move to *Former drivers*, drop off the lineup and results grid, and keep every record. Reinstate any time.
- A **result** records the class and kart number run that day, so a driver can be #7 in Ignite and #507 in LO206.
- In a **series**, the last session decides the round (the Final, or Heat 2 at club races). A **one-off race** (no series) sells and scores each race separately, so each counts on its own and its race page has a tab per race.
- Finishing positions show as ordinals (1st, 2nd, 13th) so they can't be mistaken for points; 1st–3rd are gold, silver, bronze.
- A **run** always belongs to a race or RMS test day, and records the driver/class, setup and kart used.
- **Tracks** have an optional "good lap". Lap times show how far they are off it.

## Pages

| Page | What's there |
|---|---|
| Dashboard | Signed in: countdown to the next event, team totals, the lineup, recent runs, setup changes, track records and latest results |
| Drivers | Lineup with each driver's current SPR / FTW / RTW / PSI and the change log. The driver page shows their class, number, setup (with **Swap class**), changes, runs and results. Public: a read-only list of current drivers (class, wins, podiums, top 5s, races) and former drivers, with no contacts, setups or reason for leaving |
| Home (public) | Signed out, the home page shows the latest season: team wins, podiums, top 5s and races, championship honours (top three in each championship, from official positions; current drivers first, by position, then former drivers; a series' *Name on honours*, e.g. "2026 Norway Motorsports Park Ignite Sr Vice-Champion"), each driver's season (former drivers listed last) and what's next. Signed in, it's the dashboard. The Schedule's Tracks card shows all-time RMS wins and podiums per track to the public, and good lap / RMS best to the team |
| Races | Drill down: **season** (2026, …) → **championship** (each series, plus *One-off races* such as the Quincy Grand Prix) → **race**. A race counts toward its year unless its *Season* is set: Daytona Kart Week (Dec 2026) opens the 2027 Ignite Road Racing Challenge. Each level shows race counts, RMS wins, best finish and tracks, with breadcrumbs back up. The race page shows every driver's finish (and points) per session, the kart number and class they ran that day, and every practice run logged there. Each race lists the sessions it scores (*Scored sessions*: e.g. `Heat 1, Heat 2` for club races, `Race 1, Race 2, Final`). **Enter results** fills in all sessions for everyone at once: per session the finish, a classification if needed (DNF, DNS, DSQ, or PEN with positions lost and the reason), points and best lap. DNF, DNS and DSQ never count as a finish; a penalised result keeps its position and shows a PEN +n chip. Practice sessions never count as a result |
| Standings | Per series, season and class (e.g. Ignite Challenge: Ignite Sr and Ignite Heavy separately). A series that runs every year gets a tab per season. Points (all sessions per round) with drop rules, e.g. best 11 of 13, or average finish when no points are recorded, plus official championship positions per season; dropped rounds are struck through. Public |
| Schedule | Upcoming races, practice days and test days grouped by month (*Show past* for history), plus the track list (good lap and RMS best for the team; wins and podiums for the public) |
| Setups | Setup cards grouped by track, showing who uses each one, side-by-side compare with differences highlighted. Adding a setup that already exists at that track points you to the existing one |
| Times | Three views so it never becomes one long list: **By race** (a collapsible card per race weekend, a row per driver and a column per session, newest first, 6 per page), **Personal bests** (driver × track grid; gold = fastest RMS lap there in that class) and **All runs** (the full log, 40 per page, with edit/delete). Filters (driver, class, track, session, date) fold away and apply to every view |


## Configuration (env vars)

| Var | Default |
|---|---|
| `RMS_DB_PATH` | `rms_race_day.db` next to `app.py` |
| `KART_CLASSES` | `Ignite Sr,Ignite Heavy,LO206 Sr` |
| `SECRET_KEY` | dev value; required in production |
| `ADMIN_PASSWORD_HASH` | none; see *Sign in* |
| `ADMIN_PASSWORD_HASH_FILE` | none; a file holding the hash instead (what `deploy/setup.sh` uses) |
| `RMS_ENV` | unset; `production` turns on secure cookies and required settings |
| `RMS_TZ` | `America/Chicago`; the team's time zone for race-day dates |
| `TRUST_PROXY` | unset; `1` behind a reverse proxy |
| `PORT` | `5000` |

## Logo

`static/img/rms-logo.png` (sidebar, transparent background) and `static/img/favicon.png` (browser tab).

## Not included

- Separate logins per person, or driver-facing views.
- Older databases: the app moves a pre-commands database aside (`rms_race_day.v1.bak.db`) and starts fresh.
- Live sharing with the Discord bot. They use separate databases; use the import script for now.
