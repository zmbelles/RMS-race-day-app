"""SQLite storage for the RMS Race Day app."""
import os
import re
import sqlite3
import time

from flask import g

DB_PATH = os.getenv("RMS_DB_PATH", os.path.join(os.path.dirname(__file__), "rms_race_day.db"))

CLASSES = [c.strip() for c in os.getenv("KART_CLASSES", "LO206,Ignite Sr").split(",") if c.strip()]
SESSIONS = ["Practice", "Qualifying", "Heat", "Prefinal", "Final"]
TRACK_CONDITIONS = ["Green", "Rubbered in", "Dusty", "Damp", "Wet"]
EVENT_TYPES = ["Race", "Practice day", "Test day"]
EVENT_STATUSES = ["Scheduled", "Completed", "Cancelled"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS drivers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    number     TEXT,
    phone      TEXT,
    email      TEXT,
    discord_id TEXT,
    notes      TEXT,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS karts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id     INTEGER NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    name          TEXT    NOT NULL,
    kart_class    TEXT    NOT NULL,
    chassis       TEXT,
    engine        TEXT,
    engine_number TEXT,
    notes         TEXT,
    created_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tire_sets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kart_id    INTEGER NOT NULL REFERENCES karts(id) ON DELETE CASCADE,
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
    track      TEXT,
    series     TEXT,
    start_date TEXT    NOT NULL,
    end_date   TEXT,
    status     TEXT    NOT NULL DEFAULT 'Scheduled',
    notes      TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS setups (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kart_id        INTEGER NOT NULL REFERENCES karts(id) ON DELETE CASCADE,
    name           TEXT    NOT NULL,
    track          TEXT,
    front_sprocket INTEGER NOT NULL,
    rear_sprocket  INTEGER NOT NULL,
    front_width    REAL,
    rear_width     REAL,
    tire           TEXT,
    front_psi      REAL,
    rear_psi       REAL,
    axle           TEXT,
    caster         TEXT,
    camber         TEXT,
    seat_position  TEXT,
    ride_height    TEXT,
    total_weight   REAL,
    ballast        TEXT,
    carb_jetting   TEXT,
    clutch         TEXT,
    conditions     TEXT,
    notes          TEXT,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    setup_id        INTEGER NOT NULL REFERENCES setups(id) ON DELETE CASCADE,
    event_id        INTEGER REFERENCES events(id) ON DELETE SET NULL,
    run_date        TEXT    NOT NULL,
    session         TEXT,
    best_lap        REAL,
    laps            INTEGER,
    air_temp        REAL,
    track_temp      REAL,
    track_condition TEXT,
    tire_set_id     INTEGER REFERENCES tire_sets(id) ON DELETE SET NULL,
    tire_runs       INTEGER,
    notes           TEXT,
    created_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS results (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    driver_id  INTEGER NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    kart_class TEXT,
    setup_id   INTEGER REFERENCES setups(id) ON DELETE SET NULL,
    qual_pos   INTEGER,
    finish_pos INTEGER,
    best_lap   REAL,
    points     REAL,
    dnf        INTEGER NOT NULL DEFAULT 0,
    notes      TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_karts_driver ON karts(driver_id);
CREATE INDEX IF NOT EXISTS idx_setups_kart ON setups(kart_id);
CREATE INDEX IF NOT EXISTS idx_runs_setup ON runs(setup_id);
CREATE INDEX IF NOT EXISTS idx_results_event ON results(event_id);
"""


def connect(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path=DB_PATH):
    with connect(path) as conn:
        conn.executescript(SCHEMA)


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
