"""SQLite storage for the RMS Race Day app."""
import datetime as dt
import os
import re
import shutil
import sqlite3
import time

from zoneinfo import ZoneInfo

from flask import g

DB_PATH = os.getenv("RMS_DB_PATH", os.path.join(os.path.dirname(__file__), "rms_race_day.db"))
SCHEMA_VERSION = 17

# Race-day dates and times follow the team's clock, not the server's (hosts usually run in UTC).
TZ = ZoneInfo(os.getenv("RMS_TZ", "America/Chicago"))
CLASSES = [c.strip() for c in os.getenv("KART_CLASSES", "Ignite Sr,Ignite Heavy,LO206 Sr").split(",") if c.strip()]
SESSIONS = ["Practice", "Qualifying", "Heat", "Prefinal", "Final"]
TRACK_CONDITIONS = ["Green", "Rubbered in", "Dusty", "Damp", "Wet"]
EVENT_TYPES = ["Race", "RMS test day", "Practice day"]
RUN_EVENT_TYPES = ("Race", "RMS test day")  # every run happens at one of these
EVENT_STATUSES = ["Scheduled", "Completed", "Cancelled"]
# Scored sessions. Each race lists the ones it runs, in order (events.sessions), e.g. "Race 1, Race 2, Final".
# The deciding session (results.main) is the Final if there is one, else the last listed, e.g. Heat 2 at club races.
RESULT_SESSIONS = ["Qualifying", "Heat 1", "Heat 2", "Race 1", "Race 2", "Prefinal", "Final"]
DEFAULT_SESSIONS = "Heat 1, Heat 2"
# The weekend format: every session on the schedule, day by day, practice included (weekend_sessions).
# Anything that isn't practice is scored, so saving a format also sets the race's scored sessions.
SESSION_KINDS = ["Practice", "Qualifying", "Heat", "Race", "Prefinal", "Feature"]


# A session result is classified (a plain position) unless it has one of these. DNF, DNS and DSQ are out of the
# result (results.dnf = 1, so they never count as a finish); PEN keeps its position and records what was lost and why.
CLASSIFICATIONS = {"DNF": "DNF — did not finish", "DNS": "DNS — did not start", "DSQ": "DSQ — disqualified",
                   "PEN": "PEN — penalty"}
OUT = ("DNF", "DNS", "DSQ")


def session_kind(name):
    """Guess a session's type from its name: 'Practice 2' -> Practice, 'Final' -> Feature."""
    word = (name or "").split()[0].lower() if (name or "").split() else ""
    for kind in SESSION_KINDS:
        if word.startswith(kind.lower()[:4]):
            return kind
    return "Feature" if word in ("final", "main", "feature") else "Practice"


def session_list(text):
    return [s.strip() for s in (text or DEFAULT_SESSIONS).split(",") if s.strip()]


def main_session(sessions):
    """The session that decides a race: the Final, else the last one run (Heat 2 at a club race)."""
    return "Final" if "Final" in sessions else sessions[-1]

# Setups belong to a track and are numbered per track: "Gateway 1", "Gateway 2", "Norway 1".
# A driver races one class at a time: their entry holds the class, kart number, kart and current setup, so
# race-day commands ("24 SPR 61", "77 heavy SPR 61") move them to a matching or new setup at that track.
# Numbers are unique within a class, so the same number can be used in two classes.
SHIMS = 6  # front ride height: shims split above/below the spindle; standard is 3 above, 3 below
# v3 labelled setups by their numbers; kept here only so older databases can migrate.
SETUP_LABEL = (
    "rear_sprocket || 'T'"
    " || CASE WHEN front_width IS NULL THEN '' ELSE ' · FTW ' || printf('%g', front_width) END"
    " || CASE WHEN rear_width IS NULL THEN '' ELSE ' · RTW ' || printf('%g', rear_width) END"
    " || CASE WHEN psi IS NULL THEN '' ELSE ' · ' || printf('%g', psi) || ' psi' END"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    short_name TEXT,
    city       TEXT,
    state      TEXT,
    target_lap REAL,
    notes      TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS setups (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id       INTEGER REFERENCES tracks(id) ON DELETE RESTRICT,
    track_seq      INTEGER,
    name           TEXT    NOT NULL DEFAULT '',
    front_sprocket INTEGER NOT NULL,
    rear_sprocket  INTEGER NOT NULL,
    front_width    REAL,
    rear_width     REAL,
    tire           TEXT,
    psi            REAL,
    axle           TEXT,
    caster         TEXT,
    camber         TEXT,
    seat_position  TEXT,
    ride_height    TEXT,     -- unused since v9; see front_shims_above
    front_shims_above INTEGER,
    total_weight   REAL,
    ballast        TEXT,
    carb_jetting   TEXT,
    clutch         TEXT,
    notes          TEXT,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS karts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    kart_class    TEXT    NOT NULL,
    chassis       TEXT,
    engine        TEXT,
    engine_number TEXT,
    notes         TEXT,
    created_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS drivers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    phone      TEXT,
    email      TEXT,
    discord_id TEXT,
    notes      TEXT,
    active     INTEGER NOT NULL DEFAULT 1,   -- 0 = retired / left the team; their records stay
    left_on    TEXT,
    left_note  TEXT,
    created_at INTEGER NOT NULL
);
-- One per class a driver races: Michael is #7 Ignite Sr, #507 LO206 Sr and #77 Ignite Heavy.
CREATE TABLE IF NOT EXISTS entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id  INTEGER NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    kart_class TEXT    NOT NULL,
    number     TEXT    NOT NULL,
    kart_id    INTEGER NOT NULL REFERENCES karts(id) ON DELETE RESTRICT,
    setup_id   INTEGER NOT NULL REFERENCES setups(id) ON DELETE RESTRICT,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_number ON entries(kart_class, number) WHERE active = 1;
CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_one_per_driver ON entries(driver_id);
-- No longer used (tire tracking was dropped); kept so older databases still migrate.
CREATE TABLE IF NOT EXISTS tire_sets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kart_id    INTEGER NOT NULL REFERENCES karts(id) ON DELETE CASCADE,
    position   TEXT    NOT NULL DEFAULT 'Rear',   -- Front (4.5") or Rear (7")
    name       TEXT    NOT NULL,
    runs       INTEGER NOT NULL DEFAULT 0,
    retired    INTEGER NOT NULL DEFAULT 0,
    notes      TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    event_type TEXT    NOT NULL DEFAULT 'Race',
    track_id   INTEGER REFERENCES tracks(id) ON DELETE SET NULL,
    series     TEXT,
    sessions   TEXT,
    season     INTEGER,  -- season it counts toward when not the year it runs, e.g. Daytona in Dec 2026 opens 2027
    start_date TEXT    NOT NULL,
    end_date   TEXT,
    status     TEXT    NOT NULL DEFAULT 'Scheduled',
    notes      TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS weekend_sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    day        TEXT    NOT NULL,             -- which day of the event
    start_time TEXT,                         -- optional, e.g. 09:30
    name       TEXT    NOT NULL,             -- Practice 1, Heat 2, Final…
    kind       TEXT    NOT NULL DEFAULT 'Practice',
    seq        INTEGER NOT NULL DEFAULT 0,   -- running order within the day
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_weekend_sessions ON weekend_sessions(event_id, day, seq);
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id       INTEGER NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    entry_id        INTEGER REFERENCES entries(id) ON DELETE SET NULL,
    setup_id        INTEGER REFERENCES setups(id) ON DELETE SET NULL,
    kart_id         INTEGER REFERENCES karts(id) ON DELETE SET NULL,
    track_id        INTEGER REFERENCES tracks(id) ON DELETE SET NULL,
    event_id        INTEGER REFERENCES events(id) ON DELETE SET NULL,
    run_date        TEXT    NOT NULL,
    session         TEXT,
    kart_class      TEXT,     -- class and number run, kept even after the driver swaps class
    kart_number     TEXT,
    best_lap        REAL,
    laps            INTEGER,
    air_temp        REAL,
    track_temp      REAL,
    track_condition TEXT,
    tire_set_id     INTEGER REFERENCES tire_sets(id) ON DELETE SET NULL,  -- unused since v10
    tire_runs       INTEGER,                                              -- unused since v10
    front_tire_id   INTEGER REFERENCES tire_sets(id) ON DELETE SET NULL,
    rear_tire_id    INTEGER REFERENCES tire_sets(id) ON DELETE SET NULL,
    front_tire_runs INTEGER,  -- the set's age at this run
    rear_tire_runs  INTEGER,
    notes           TEXT,
    created_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS results (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    driver_id  INTEGER NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    entry_id   INTEGER REFERENCES entries(id) ON DELETE SET NULL,
    session    TEXT    NOT NULL DEFAULT 'Final',
    kart_class TEXT,
    kart_number TEXT,
    main       INTEGER NOT NULL DEFAULT 0,
    setup_id   INTEGER REFERENCES setups(id) ON DELETE SET NULL,
    qual_pos   INTEGER,
    finish_pos INTEGER,
    best_lap   REAL,
    points     REAL,
    dnf        INTEGER NOT NULL DEFAULT 0,  -- 1 when classification is DNF, DNS or DSQ
    classification    TEXT,     -- NULL (classified), DNF, DNS, DSQ or PEN
    penalty_positions INTEGER,  -- PEN: positions lost
    penalty_reason    TEXT,     -- PEN: why
    notes      TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS setup_changes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id     INTEGER NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    entry_id      INTEGER REFERENCES entries(id) ON DELETE CASCADE,
    command       TEXT    NOT NULL,
    from_setup_id INTEGER REFERENCES setups(id) ON DELETE SET NULL,
    to_setup_id   INTEGER REFERENCES setups(id) ON DELETE SET NULL,
    created_setup INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_driver ON runs(driver_id);
CREATE INDEX IF NOT EXISTS idx_runs_setup ON runs(setup_id);
CREATE INDEX IF NOT EXISTS idx_results_event ON results(event_id);
CREATE INDEX IF NOT EXISTS idx_changes_driver ON setup_changes(driver_id);
CREATE INDEX IF NOT EXISTS idx_setups_track ON setups(track_id, track_seq);
CREATE INDEX IF NOT EXISTS idx_entries_driver ON entries(driver_id);
-- A race's entry list: which driver/class entries race it and the kart number each runs that weekend.
-- On race day the command bar reads numbers from here, so "77" means whoever is #77 at this race.
CREATE TABLE IF NOT EXISTS event_entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    entry_id   INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    number     TEXT    NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (event_id, entry_id)
);
CREATE TABLE IF NOT EXISTS series (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    title      TEXT,     -- what the championship is called on honours, e.g. "Norway Motorsports Park", "KCA"
    best_of    INTEGER,
    notes      TEXT,
    created_at INTEGER NOT NULL
);
-- Official championship finishing position in the whole field (other teams' drivers included).
CREATE TABLE IF NOT EXISTS series_positions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    series     TEXT    NOT NULL,
    season     INTEGER NOT NULL,  -- a series that runs every year has positions each season
    driver_id  INTEGER NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    kart_class TEXT,
    position   INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (series, season, driver_id)
);
"""

# v2 -> v3: one tire pressure for all four tires, and setups lose their names.
MIGRATE_V3 = f"""
ALTER TABLE setups ADD COLUMN psi REAL;
UPDATE setups SET psi = COALESCE(front_psi, rear_psi);
ALTER TABLE setups DROP COLUMN front_psi;
ALTER TABLE setups DROP COLUMN rear_psi;
ALTER TABLE setups DROP COLUMN name;
ALTER TABLE setups ADD COLUMN name TEXT GENERATED ALWAYS AS ({SETUP_LABEL}) VIRTUAL;
"""

# v3 -> v4: setups belong to a track and are named "<track short name> <n>".
MIGRATE_V4 = """
ALTER TABLE tracks ADD COLUMN short_name TEXT;
ALTER TABLE setups DROP COLUMN name;
ALTER TABLE setups ADD COLUMN track_id INTEGER REFERENCES tracks(id) ON DELETE RESTRICT;
ALTER TABLE setups ADD COLUMN track_seq INTEGER;
ALTER TABLE setups ADD COLUMN name TEXT NOT NULL DEFAULT '';
"""
# v4 -> v5: results can score a heat and a final per round. (The series table comes from SCHEMA.)
MIGRATE_V5 = """
ALTER TABLE results ADD COLUMN session TEXT NOT NULL DEFAULT 'Final';
"""

# v5 -> v6: each race lists its sessions, and results record the kart number run that day
# (a driver can carry a different number in each class).
MIGRATE_V6 = """
ALTER TABLE events ADD COLUMN sessions TEXT;
ALTER TABLE results ADD COLUMN kart_number TEXT;
"""

# v6 -> v7: flag the deciding session on each result (see main_session).
MIGRATE_V7 = "ALTER TABLE results ADD COLUMN main INTEGER NOT NULL DEFAULT 0;"


def _migrate_v7(conn):
    conn.executescript(MIGRATE_V7)
    for eid, sessions in conn.execute("SELECT id, sessions FROM events").fetchall():
        names = [r[0] for r in conn.execute("SELECT DISTINCT session FROM results WHERE event_id = ?", (eid,))]
        listed = session_list(sessions) if sessions else ["Heat", "Final"]  # v6 default
        main = "Final" if "Final" in names else main_session(listed + [n for n in names if n not in listed])
        conn.execute("UPDATE results SET main = (session = ?) WHERE event_id = ?", (main, eid))
    conn.commit()


# v7 -> v8: record when and why a driver left the team.
MIGRATE_V8 = """
ALTER TABLE drivers ADD COLUMN left_on TEXT;
ALTER TABLE drivers ADD COLUMN left_note TEXT;
"""

# v8 -> v9: drivers race one or more classes (entries); front ride height in shims.
MIGRATE_V9 = """
CREATE TABLE entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id  INTEGER NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    kart_class TEXT    NOT NULL,
    number     TEXT    NOT NULL,
    kart_id    INTEGER NOT NULL REFERENCES karts(id) ON DELETE RESTRICT,
    setup_id   INTEGER NOT NULL REFERENCES setups(id) ON DELETE RESTRICT,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    UNIQUE (kart_class, number)
);
INSERT INTO entries (driver_id, kart_class, number, kart_id, setup_id, created_at)
    SELECT id, kart_class, number, kart_id, setup_id, created_at FROM drivers;
ALTER TABLE results ADD COLUMN entry_id INTEGER REFERENCES entries(id) ON DELETE SET NULL;
ALTER TABLE runs ADD COLUMN entry_id INTEGER REFERENCES entries(id) ON DELETE SET NULL;
ALTER TABLE setup_changes ADD COLUMN entry_id INTEGER REFERENCES entries(id) ON DELETE CASCADE;
ALTER TABLE series_positions ADD COLUMN kart_class TEXT;
ALTER TABLE setups ADD COLUMN front_shims_above INTEGER;
"""
# The class/number/kart/setup columns move from drivers to entries, so rebuild drivers without them.
REBUILD_DRIVERS_V9 = """
CREATE TABLE drivers_v9 (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    phone      TEXT,
    email      TEXT,
    discord_id TEXT,
    notes      TEXT,
    active     INTEGER NOT NULL DEFAULT 1,
    left_on    TEXT,
    left_note  TEXT,
    created_at INTEGER NOT NULL
);
INSERT INTO drivers_v9 SELECT id, name, phone, email, discord_id, notes, active, left_on, left_note, created_at FROM drivers;
DROP TABLE drivers;
ALTER TABLE drivers_v9 RENAME TO drivers;
"""


def _migrate_v9(conn):
    conn.executescript(MIGRATE_V9)
    # Results in a class the driver had no entry for (e.g. #507 in LO206 Sr) get an entry of their own.
    for driver_id, kart_class, number in conn.execute(
            "SELECT DISTINCT r.driver_id, r.kart_class, COALESCE(NULLIF(r.kart_number, ''), d.number) FROM results r "
            "JOIN drivers d ON d.id = r.driver_id WHERE r.kart_class IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM entries e WHERE e.driver_id = r.driver_id AND e.kart_class = r.kart_class)").fetchall():
        base = conn.execute("SELECT kart_id, setup_id FROM drivers WHERE id = ?", (driver_id,)).fetchone()
        taken = conn.execute("SELECT 1 FROM entries WHERE kart_class = ? AND number = ?", (kart_class, number)).fetchone()
        conn.execute("INSERT INTO entries (driver_id, kart_class, number, kart_id, setup_id, created_at) VALUES (?,?,?,?,?,?)",
                     (driver_id, kart_class, f"{number}-{driver_id}" if taken else number, base[0], base[1], now()))
    conn.execute("UPDATE results SET entry_id = (SELECT e.id FROM entries e WHERE e.driver_id = results.driver_id "
                 "AND e.kart_class = COALESCE(results.kart_class, e.kart_class) ORDER BY e.id LIMIT 1)")
    # A result's kart number only needs storing when it differs from the entry's number.
    conn.execute("UPDATE results SET kart_number = NULL WHERE kart_number = (SELECT number FROM entries WHERE id = results.entry_id)")
    conn.execute("UPDATE runs SET entry_id = (SELECT MIN(id) FROM entries e WHERE e.driver_id = runs.driver_id)")
    conn.execute("UPDATE setup_changes SET entry_id = (SELECT MIN(id) FROM entries e WHERE e.driver_id = setup_changes.driver_id)")
    conn.execute("UPDATE series_positions SET kart_class = (SELECT kart_class FROM drivers WHERE id = series_positions.driver_id)")
    conn.execute("UPDATE setups SET front_shims_above = 3")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(REBUILD_DRIVERS_V9)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()


# v9 -> v10: fronts and rears are tracked as separate tire sets.
MIGRATE_V10 = """
ALTER TABLE tire_sets ADD COLUMN position TEXT NOT NULL DEFAULT 'Rear';
ALTER TABLE runs ADD COLUMN front_tire_id INTEGER REFERENCES tire_sets(id) ON DELETE SET NULL;
ALTER TABLE runs ADD COLUMN rear_tire_id INTEGER REFERENCES tire_sets(id) ON DELETE SET NULL;
ALTER TABLE runs ADD COLUMN front_tire_runs INTEGER;
ALTER TABLE runs ADD COLUMN rear_tire_runs INTEGER;
"""


def _migrate_v10(conn):
    """Old tire sets were all four tires: each becomes a rear set plus a matching front set with the same runs."""
    conn.executescript(MIGRATE_V10)
    for t in conn.execute("SELECT * FROM tire_sets").fetchall():
        front = conn.execute("INSERT INTO tire_sets (kart_id, position, name, runs, retired, notes, created_at) "
                             "VALUES (?, 'Front', ?, ?, ?, ?, ?)",
                             (t["kart_id"], t["name"], t["runs"], t["retired"], t["notes"], t["created_at"])).lastrowid
        conn.execute("UPDATE runs SET rear_tire_id = tire_set_id, rear_tire_runs = tire_runs, front_tire_id = ?, "
                     "front_tire_runs = tire_runs WHERE tire_set_id = ?", (front, t["id"]))
    conn.commit()


# v10 -> v11: a number only has to be unique among current entries in a class.
REBUILD_ENTRIES_V11 = """
CREATE TABLE entries_v11 (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id  INTEGER NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    kart_class TEXT    NOT NULL,
    number     TEXT    NOT NULL,
    kart_id    INTEGER NOT NULL REFERENCES karts(id) ON DELETE RESTRICT,
    setup_id   INTEGER NOT NULL REFERENCES setups(id) ON DELETE RESTRICT,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
INSERT INTO entries_v11 (id, driver_id, kart_class, number, kart_id, setup_id, active, created_at)
    SELECT id, driver_id, kart_class, number, kart_id, setup_id, active, created_at FROM entries;
DROP TABLE entries;
ALTER TABLE entries_v11 RENAME TO entries;
CREATE INDEX IF NOT EXISTS idx_entries_driver ON entries(driver_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_number ON entries(kart_class, number) WHERE active = 1;
"""


def _migrate_v11(conn):
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(REBUILD_ENTRIES_V11)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()


GENERIC_TRACK_WORDS = {"kart", "karting", "circuit", "club", "the", "&", "and", "motorsports", "motorplex",
                       "park", "raceway", "speedway", "kartplex", "track", "international", "grand", "prix"}


def default_short_name(track_name):
    """The distinctive words of a track name: 'Gateway Kartplex' -> 'Gateway', 'Kart Circuit Autobahn' -> 'Autobahn',
    'New Castle Motorsports Park' -> 'New Castle', '61 Kartway' -> '61 Kartway'."""
    words = track_name.split()
    while words and words[0].lower() in GENERIC_TRACK_WORDS:
        words = words[1:]
    short = []
    for w in words:
        if w.lower() in GENERIC_TRACK_WORDS:
            break
        short.append(w)
    return " ".join(short) or track_name


def track_short(conn, track_id):
    row = conn.execute("SELECT COALESCE(NULLIF(short_name, ''), name) FROM tracks WHERE id = ?", (track_id,)).fetchone()
    return row[0] if row else "Setup"


def number_setup(conn, setup_id, track_id):
    """Give a setup the next number at its track and the matching name, e.g. 'Gateway 3'."""
    seq = conn.execute("SELECT COALESCE(MAX(track_seq), 0) + 1 FROM setups WHERE track_id IS ? AND id != ?",
                       (track_id, setup_id)).fetchone()[0]
    name = f"{track_short(conn, track_id)} {seq}" if track_id else f"Setup {seq}"
    conn.execute("UPDATE setups SET track_id = ?, track_seq = ?, name = ? WHERE id = ?", (track_id, seq, name, setup_id))
    return name


def rename_track_setups(conn, track_id):
    """After a track's short name changes, rename its setups to match."""
    conn.execute("UPDATE setups SET name = ? || ' ' || track_seq WHERE track_id = ?", (track_short(conn, track_id), track_id))


def _migrate_v4(conn):
    conn.executescript(MIGRATE_V4)
    for tid, name in conn.execute("SELECT id, name FROM tracks").fetchall():
        conn.execute("UPDATE tracks SET short_name = ? WHERE id = ?", (default_short_name(name), tid))
    # Put each setup at the track it has been run at most; setups never run stay without a track.
    for (sid,) in conn.execute("SELECT id FROM setups ORDER BY id").fetchall():
        row = conn.execute("SELECT track_id FROM runs WHERE setup_id = ? AND track_id IS NOT NULL "
                           "GROUP BY track_id ORDER BY COUNT(*) DESC LIMIT 1", (sid,)).fetchone()
        number_setup(conn, sid, row[0] if row else None)
    conn.commit()


# v13 -> v14: results get a classification (DNF/DNS/DSQ/PEN) and penalty details; old DNFs carry over.
MIGRATE_V14 = """
ALTER TABLE results ADD COLUMN classification TEXT;
ALTER TABLE results ADD COLUMN penalty_positions INTEGER;
ALTER TABLE results ADD COLUMN penalty_reason TEXT;
UPDATE results SET classification = CASE WHEN UPPER(TRIM(COALESCE(notes, ''))) = 'DNS' THEN 'DNS' ELSE 'DNF' END
 WHERE dnf = 1;
UPDATE results SET notes = NULL WHERE classification = 'DNS' AND UPPER(TRIM(notes)) = 'DNS';
"""


def _migrate_v15(conn):
    """One class at a time: each driver keeps one entry (their first current one) and swaps its class, number and
    kart. Runs and results keep the class and number they were run with, so history doesn't move with a swap."""
    conn.executescript("""
ALTER TABLE runs ADD COLUMN kart_class TEXT;
ALTER TABLE runs ADD COLUMN kart_number TEXT;
UPDATE runs SET kart_class = (SELECT kart_class FROM entries WHERE id = runs.entry_id),
                kart_number = (SELECT number FROM entries WHERE id = runs.entry_id)
 WHERE entry_id IS NOT NULL;
UPDATE results SET kart_class = COALESCE(kart_class, (SELECT kart_class FROM entries WHERE id = results.entry_id)),
                   kart_number = COALESCE(NULLIF(kart_number, ''), (SELECT number FROM entries WHERE id = results.entry_id));
""")
    keep = {}
    for driver_id, entry_id in conn.execute("SELECT driver_id, id FROM entries ORDER BY driver_id, active DESC, id").fetchall():
        keep.setdefault(driver_id, entry_id)
    for old_id, driver_id in conn.execute("SELECT id, driver_id FROM entries").fetchall():
        new_id = keep[driver_id]
        if old_id == new_id:
            continue
        for table in ("runs", "results", "setup_changes"):
            conn.execute(f"UPDATE {table} SET entry_id = ? WHERE entry_id = ?", (new_id, old_id))
        conn.execute("DELETE FROM event_entries WHERE entry_id = ? AND event_id IN "
                     "(SELECT event_id FROM event_entries WHERE entry_id = ?)", (old_id, new_id))
        conn.execute("UPDATE event_entries SET entry_id = ? WHERE entry_id = ?", (new_id, old_id))
        conn.execute("DELETE FROM entries WHERE id = ?", (old_id,))
    conn.execute("UPDATE entries SET active = 1")
    conn.commit()


def _migrate_v17(conn):
    """Championship positions get a season (the latest season their series ran), so a series that runs every year
    doesn't carry one year's champions into the next."""
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript("""
CREATE TABLE series_positions_new (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    series     TEXT    NOT NULL,
    season     INTEGER NOT NULL,
    driver_id  INTEGER NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    kart_class TEXT,
    position   INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (series, season, driver_id)
);
INSERT INTO series_positions_new (id, series, season, driver_id, kart_class, position, created_at)
SELECT sp.id, sp.series,
       COALESCE((SELECT MAX(COALESCE(e.season, CAST(substr(e.start_date, 1, 4) AS INTEGER)))
                 FROM events e WHERE e.series = sp.series), CAST(strftime('%Y', 'now') AS INTEGER)),
       sp.driver_id, sp.kart_class, sp.position, sp.created_at
FROM series_positions sp;
DROP TABLE series_positions;
ALTER TABLE series_positions_new RENAME TO series_positions;
""")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()


def local_now():
    return dt.datetime.now(TZ)


def connect(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path=DB_PATH):
    conn = connect(path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    has_tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'drivers'").fetchone()[0]
    if has_tables and version == 2:
        shutil.copy(path, path.replace(".db", ".v2.bak.db"))
        with conn:
            conn.executescript(MIGRATE_V3)
        version = 3
    if has_tables and version == 3:
        shutil.copy(path, path.replace(".db", ".v3.bak.db"))
        _migrate_v4(conn)
        version = 4
    if has_tables and version == 4:
        shutil.copy(path, path.replace(".db", ".v4.bak.db"))
        with conn:
            conn.executescript(MIGRATE_V5)
        version = 5
    if has_tables and version == 5:
        shutil.copy(path, path.replace(".db", ".v5.bak.db"))
        with conn:
            conn.executescript(MIGRATE_V6)
        version = 6
    if has_tables and version == 6:
        shutil.copy(path, path.replace(".db", ".v6.bak.db"))
        _migrate_v7(conn)
        version = 7
    if has_tables and version == 7:
        shutil.copy(path, path.replace(".db", ".v7.bak.db"))
        with conn:
            conn.executescript(MIGRATE_V8)
        version = 8
    if has_tables and version == 8:
        shutil.copy(path, path.replace(".db", ".v8.bak.db"))
        _migrate_v9(conn)
        version = 9
    if has_tables and version == 9:
        shutil.copy(path, path.replace(".db", ".v9.bak.db"))
        _migrate_v10(conn)
        version = 10
    if has_tables and version == 10:
        shutil.copy(path, path.replace(".db", ".v10.bak.db"))
        _migrate_v11(conn)
        version = 11
    if has_tables and version == 11:
        version = 12  # v12 only adds weekend_sessions, created below
    if has_tables and version == 12:
        shutil.copy(path, path.replace(".db", ".v12.bak.db"))
        with conn:
            conn.execute("ALTER TABLE events ADD COLUMN season INTEGER")
        version = 13
    if has_tables and version == 13:
        shutil.copy(path, path.replace(".db", ".v13.bak.db"))
        with conn:
            conn.executescript(MIGRATE_V14)
        version = 14
    if has_tables and version == 14:
        shutil.copy(path, path.replace(".db", ".v14.bak.db"))
        _migrate_v15(conn)
        version = 15
    if has_tables and version == 15:
        shutil.copy(path, path.replace(".db", ".v15.bak.db"))
        with conn:
            conn.execute("ALTER TABLE series ADD COLUMN title TEXT")
        version = 16
    if has_tables and version == 16:
        shutil.copy(path, path.replace(".db", ".v16.bak.db"))
        _migrate_v17(conn)
        version = 17
    if has_tables and version < SCHEMA_VERSION:
        # v1 tied setups to karts and karts to drivers. Keep the old file and start fresh.
        conn.close()
        backup = path.replace(".db", f".v{version or 1}.bak.db")
        shutil.move(path, backup)
        print(f"Old database moved to {backup}; starting a new one.")
        conn = connect(path)
    with conn:
        conn.executescript(SCHEMA)
        conn.execute("UPDATE events SET event_type = 'RMS test day' WHERE event_type = 'Test day'")  # renamed
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.close()


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql, args=(), one=False):
    rows = get_db().execute(sql, args).fetchall()
    return (rows[0] if rows else None) if one else rows


def insert(conn, table, **values):
    values.setdefault("created_at", now())
    cur = conn.execute(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                       tuple(values.values()))
    return cur.lastrowid


def now():
    return int(time.time())


LAP_RE = re.compile(r"^\s*(?:(\d+):)?(\d+(?:\.\d+)?)\s*$")


def parse_lap(text):
    """'45.123' or '1:02.345' -> seconds. Blank -> None. Bad input -> ValueError."""
    if text is None or str(text).strip() == "":
        return None
    m = LAP_RE.match(str(text))
    if not m:
        raise ValueError(f"Lap time '{text}' should look like 45.123 or 1:02.345")
    return int(m.group(1) or 0) * 60 + float(m.group(2))


def fmt_lap(seconds):
    if seconds is None:
        return "—"
    minutes, secs = divmod(seconds, 60)
    return f"{int(minutes)}:{secs:06.3f}" if minutes else f"{secs:.3f}"


def fmt_num(value):
    """3.0 -> '3', 1.25 -> '1.25', None -> '—'."""
    if value is None:
        return "—"
    return f"{value:g}" if isinstance(value, float) else str(value)
