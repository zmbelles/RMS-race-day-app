"""RMS Race Day — admin web app for drivers, races, schedule, setups and lap times."""
import datetime as dt
import os
import sqlite3
from collections import OrderedDict

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from markupsafe import Markup, escape

import auth
import commands
import db
from db import fmt_lap, fmt_num, now, parse_lap, query

app = Flask(__name__)
auth.init_app(app)
app.teardown_appcontext(db.close_db)
app.jinja_env.filters["lap"] = fmt_lap
app.jinja_env.filters["num"] = fmt_num
app.jinja_env.filters["record"] = lambda row: dict(row) if row is not None else {}
app.jinja_env.filters["shims"] = commands.shims  # 3 -> '3/3' front ride height shims above/below
app.jinja_env.filters["spec"] = commands.spec_label  # setup values in one line
app.jinja_env.filters["weekday"] = lambda iso: dt.date.fromisoformat(iso).strftime("%a %b %#d" if os.name == "nt" else "%a %b %-d")
app.jinja_env.filters["season"] = lambda e: int(e["season"] or e["start_date"][:4])
app.jinja_env.filters["nice_date"] = lambda iso: dt.date.fromisoformat(iso).strftime("%b %#d, %Y" if os.name == "nt" else "%b %-d, %Y") if iso else "—"
app.jinja_env.filters["ago"] = lambda ts: dt.datetime.fromtimestamp(ts, db.TZ).strftime("%b %#d, %#I:%M %p" if os.name == "nt" else "%b %-d, %-I:%M %p")


@app.template_filter("ordinal")
def ordinal(n):
    """13 -> '13th', 1 -> '1st', 22 -> '22nd': makes a finishing position read as a place, not points."""
    if n is None:
        return "—"
    n = int(n)
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


@app.template_filter("delta")
def delta(lap, target):
    if lap is None or target is None:
        return ""
    d = lap - target
    return Markup(f'<span class="delta {"up" if d > 0 else "down"}">{"+" if d > 0 else "−"}{abs(d):.3f}</span>')



# ---------------------------------------------------------------- shared SQL

DRIVER_STATS = """
       (SELECT COUNT(DISTINCT event_id) FROM results WHERE driver_id = d.id) AS race_count,
       (SELECT COUNT(*) FROM results WHERE driver_id = d.id AND main = 1 AND finish_pos = 1 AND dnf = 0) AS wins,
       (SELECT COUNT(*) FROM results WHERE driver_id = d.id AND main = 1 AND finish_pos <= 3 AND dnf = 0) AS podiums,
       (SELECT COUNT(*) FROM results WHERE driver_id = d.id AND main = 1 AND finish_pos <= 5 AND dnf = 0) AS top5s"""

# One row per driver, with their current kart number and class.
DRIVER_SQL = """
SELECT d.*,
       (SELECT number FROM entries WHERE driver_id = d.id ORDER BY active DESC, id LIMIT 1) AS number,
       (SELECT kart_class FROM entries WHERE driver_id = d.id ORDER BY active DESC, id LIMIT 1) AS kart_class,
       (SELECT GROUP_CONCAT(kart_class || ' #' || number, ' · ') FROM entries WHERE driver_id = d.id AND active = 1) AS classes,
       (SELECT MIN(best_lap) FROM runs WHERE driver_id = d.id) AS best_lap,""" + DRIVER_STATS + """
FROM drivers d
"""

# One row per driver's entry (class, number, kart, setup): the lineup. `id` is the driver's id so links and the
# driver form work; entry_id is the entry. number_shared > 1 means another class uses the same number.
ENTRY_SQL = """
SELECT en.driver_id AS id, en.id AS entry_id, en.driver_id, en.number, en.kart_class, en.kart_id, en.setup_id,
       en.active AS entry_active, d.name, d.active, d.phone, d.email, d.discord_id, d.notes, d.left_on, d.left_note,
       s.name AS setup_name, s.front_sprocket, s.rear_sprocket, s.front_width, s.rear_width, s.psi, s.tire,
       s.front_shims_above, k.name AS kart_name, k.chassis, k.engine,
       (SELECT COUNT(*) FROM entries e2 JOIN drivers d2 ON d2.id = e2.driver_id
         WHERE e2.number = en.number AND e2.active = 1 AND d2.active = 1) AS number_shared,""" + DRIVER_STATS + """
FROM entries en JOIN drivers d ON d.id = en.driver_id JOIN setups s ON s.id = en.setup_id JOIN karts k ON k.id = en.kart_id
"""
ENTRY_ORDER = " ORDER BY CAST(en.number AS INTEGER), en.number, en.kart_class"

SETUP_SQL = """
SELECT s.*, t.name AS track_name,
       (SELECT MIN(best_lap) FROM runs WHERE setup_id = s.id) AS best_lap,
       (SELECT COUNT(*) FROM runs WHERE setup_id = s.id) AS run_count,
       (SELECT GROUP_CONCAT(number, ',') FROM entries WHERE setup_id = s.id) AS used_by
FROM setups s LEFT JOIN tracks t ON t.id = s.track_id
"""
SETUP_ORDER = " ORDER BY t.name IS NULL, t.name, s.track_seq"

RUN_SQL = """
SELECT r.*, d.name AS driver_name, COALESCE(r.kart_number, en.number) AS driver_number,
       COALESCE(r.kart_class, en.kart_class, k.kart_class) AS kart_class,
       k.name AS kart_name, s.name AS setup_name, s.front_sprocket, s.rear_sprocket,
       t.name AS track_name, t.target_lap, e.name AS event_name
FROM runs r
JOIN drivers d ON d.id = r.driver_id
LEFT JOIN entries en ON en.id = r.entry_id
LEFT JOIN setups s ON s.id = r.setup_id
LEFT JOIN karts k ON k.id = r.kart_id
LEFT JOIN tracks t ON t.id = r.track_id
LEFT JOIN events e ON e.id = r.event_id
"""

RESULT_SQL = """
SELECT res.*, d.name AS driver_name, d.active AS driver_active,
       COALESCE(NULLIF(res.kart_number, ''), en.number) AS driver_number,
       s.name AS setup_name, e.name AS event_name, e.start_date, e.sessions AS event_sessions, t.name AS track_name
FROM results res
JOIN drivers d ON d.id = res.driver_id
LEFT JOIN entries en ON en.id = res.entry_id
JOIN events e ON e.id = res.event_id
LEFT JOIN tracks t ON t.id = e.track_id
LEFT JOIN setups s ON s.id = res.setup_id
"""

EVENT_SQL = "SELECT e.*, t.name AS track_name FROM events e LEFT JOIN tracks t ON t.id = e.track_id"

CHANGE_SQL = """
SELECT c.*, d.name AS driver_name, en.number AS driver_number, en.kart_class, fs.name AS from_name, ts.name AS to_name,
       (en.setup_id = c.to_setup_id AND c.id = (SELECT MAX(id) FROM setup_changes WHERE entry_id = c.entry_id)) AS undoable
FROM setup_changes c
JOIN drivers d ON d.id = c.driver_id
LEFT JOIN entries en ON en.id = c.entry_id
LEFT JOIN setups fs ON fs.id = c.from_setup_id
LEFT JOIN setups ts ON ts.id = c.to_setup_id
"""


def today():
    return db.local_now().date().isoformat()


# ---------------------------------------------------------------- forms
# Each form is rendered as a Bootstrap modal by templates/_forms.html and
# posted to /save/<key>. Options for selects are resolved per request.


def F(name, label, type="text", col=6, **kw):
    return dict(name=name, label=label, type=type, col=col, **kw)


FORMS = {
    "driver": dict(table="drivers", title="Driver", fields=[
        F("name", "Name", required=True, col=12),
        F("phone", "Phone"),
        F("email", "Email", type="email"),
        F("discord_id", "Discord user ID"),
        F("notes", "Notes", type="textarea", col=12),
    ]),
    # Adding a driver also adds their entry: the class, number, kart and setup they race.
    "driver_new": dict(table="drivers", title="Driver", fields=[
        F("name", "Name", required=True, col=12),
        F("kart_class", "Class", type="select", options="classes", required=True, col=4, section="Class & kart"),
        F("number", "Kart #", required=True, col=3),
        F("kart_id", "Kart", type="select", options="karts_new", required=True, col=5),
        F("setup_id", "Current setup", type="select", options="setups", required=True, col=12),
        F("phone", "Phone", section="Contact"),
        F("email", "Email", type="email"),
        F("notes", "Notes", type="textarea", col=12),
    ]),
    # A driver races one class at a time: swapping class changes their class, number, kart and setup.
    # Past runs and results keep the class and number they were run with.
    "entry": dict(table="entries", title="Class, number & kart", fields=[
        F("driver_id", "Driver", type="select", options="drivers", required=True, col=12),
        F("kart_class", "Class", type="select", options="classes", required=True, col=7),
        F("number", "Kart #", required=True, col=5, placeholder="77"),
        F("kart_id", "Kart", type="select", options="karts_new", required=True, col=12),
        F("setup_id", "Current setup", type="select", options="setups", required=True, col=12),
    ]),
    "kart": dict(table="karts", title="Kart", fields=[
        F("name", "Kart name", required=True, placeholder="e.g. #24 Ignite K3"),
        F("kart_class", "Class", type="select", options="classes", required=True),
        F("chassis", "Chassis", placeholder="Margay Ignite K3 or Margay Brava"),
        F("engine", "Engine", placeholder="Margay Ignite 212"),
        F("engine_number", "Engine / seal #"),
        F("notes", "Notes", type="textarea", col=12),
    ]),
    "track": dict(table="tracks", title="Track", fields=[
        F("name", "Track name", required=True, col=8),
        F("short_name", "Short name", placeholder="Gateway", col=4),
        F("city", "City", col=5),
        F("state", "State", col=3),
        F("target_lap", "Good lap", type="lap", placeholder="53.000", col=4),
        F("notes", "Notes", type="textarea", col=12),
    ]),
    "event": dict(table="events", title="Event", fields=[
        F("name", "Event name", required=True, col=8),
        F("event_type", "Type", type="select", options="event_types", required=True, col=4),
        F("track_id", "Track", type="select", options="tracks"),
        F("series", "Series", placeholder="Club, regional, national…"),
        F("sessions", "Scored sessions", placeholder="Heat, Final", col=8),
        F("season", "Season", type="number", placeholder="year of the dates", col=4),
        F("start_date", "Start date", type="date", required=True, col=4),
        F("end_date", "End date", type="date", col=4),
        F("status", "Status", type="select", options="event_statuses", required=True, col=4),
        F("notes", "Notes", type="textarea", col=12),
    ]),
    "setup": dict(table="setups", title="Setup", size="xl", fields=[
        F("track_id", "Track", type="select", options="tracks", required=True, col=6, section="Track"),
        F("front_sprocket", "Front sprocket (T)", type="number", required=True, default=17, col=3, section="Gearing & track width"),
        F("rear_sprocket", "Rear sprocket (T)", type="number", required=True, col=3),
        F("front_width", "Front track width (in)", type="number", step="0.01", col=3),
        F("rear_width", "Rear track width (in)", type="number", step="0.01", col=3),
        F("tire", "Tire", col=8, section="Tires", placeholder="Hoosier R80"),
        F("psi", "Tire pressure (PSI)", type="number", step="0.1", col=4),
        F("axle", "Axle", col=4, section="Chassis"),
        F("caster", "Caster", col=4),
        F("camber", "Camber", col=4),
        F("seat_position", "Seat position", col=4),
        F("front_shims_above", "Front ride height (shims)", type="select", options="shims", default=3, col=4),
        F("total_weight", "Total weight (lb)", type="number", step="0.1", col=4, section="Weight & engine"),
        F("ballast", "Ballast", col=8),
        F("carb_jetting", "Carb jetting"),
        F("clutch", "Clutch"),
        F("notes", "Notes", type="textarea", col=12, section="Notes"),
    ]),
    "run": dict(table="runs", title="Run", fields=[
        F("entry_id", "Driver", type="select", options="entries", required=True, col=5),
        F("setup_id", "Setup", type="select", options="setups", col=7),
        F("event_id", "Race / test day", type="select", options="run_events", required=True, col=12),
        F("run_date", "Date", type="date", required=True, default=today, col=4),
        F("session", "Session", list="run-sessions", options="sessions", placeholder="Practice 2", col=4),
        F("kart_id", "Kart", type="select", options="karts", col=4),
        F("best_lap", "Best lap", type="lap", placeholder="45.123 or 1:02.345", col=4),
        F("laps", "Laps", type="number", col=4),
        F("track_condition", "Track condition", type="select", options="track_conditions", col=4),
        F("air_temp", "Air temp (°F)", type="number", step="0.1", col=4),
        F("track_temp", "Track temp (°F)", type="number", step="0.1", col=4),
        F("notes", "Notes", type="textarea", col=12),
    ]),
    "result": dict(table="results", title="Race result", fields=[
        F("event_id", "Race", type="select", options="races", required=True, col=8),
        F("session", "Session", type="select", options="result_sessions", required=True, default="Heat 2", col=4),
        F("entry_id", "Driver", type="select", options="entries", required=True, col=5),
        F("kart_class", "Class run", type="select", options="classes", col=4),
        F("kart_number", "Kart # that day", col=3, placeholder="their #"),
        F("setup_id", "Setup used", type="select", options="setups", col=12),
        F("qual_pos", "Qualified", type="number", col=3),
        F("finish_pos", "Finished", type="number", col=3),
        F("best_lap", "Best lap", type="lap", col=3),
        F("points", "Points", type="number", step="0.5", col=3),
        F("classification", "Classification", type="select", options="classifications", col=4),
        F("penalty_positions", "Penalty: positions lost", type="number", col=3),
        F("penalty_reason", "Penalty reason", col=5, placeholder="Jumped start, contact…"),
        F("notes", "Notes", type="textarea", col=12),
    ]),
}
FORMS["series"] = dict(table="series", title="Series", fields=[
    F("name", "Series name", required=True, col=8),
    F("title", "Name on honours", col=12, placeholder="Norway Motorsports Park → 2026 Norway Motorsports Park Ignite Sr Champion"),
    F("best_of", "Rounds that count", type="number", col=4, placeholder="all"),
    F("notes", "Notes", type="textarea", col=12),
])
FORMS["series_position"] = dict(table="series_positions", title="Championship position", fields=[
    F("series", "Series", required=True, col=8),
    F("season", "Season", type="number", required=True, col=4),
    F("driver_id", "Driver", type="select", options="drivers", required=True, col=6),
    F("kart_class", "Class", type="select", options="classes", col=3),
    F("position", "Finished", type="number", required=True, col=3),
])
TEXT_OPTIONS = {"classifications", "classes", "sessions", "track_conditions", "event_types", "event_statuses", "result_sessions"}


def form_options():
    """(value, label, fill) tuples for every select used by FORMS; fill pre-fills other fields when picked."""
    simple = lambda xs: [(x, x, None) for x in xs]
    karts = [(r["id"], f"{r['name']} ({r['kart_class']})", None)
             for r in query("SELECT * FROM karts ORDER BY name")]
    return {
        **{name: simple(values) for name, values in (
            ("classes", db.CLASSES), ("sessions", db.SESSIONS), ("track_conditions", db.TRACK_CONDITIONS),
            ("event_types", db.EVENT_TYPES), ("event_statuses", db.EVENT_STATUSES),
            ("result_sessions", db.RESULT_SESSIONS))},
        "classifications": [(k, label, None) for k, label in db.CLASSIFICATIONS.items()],
        "drivers": [(r["id"], r["name"] + ("" if r["active"] else " (former)"), None)
                    for r in query("SELECT * FROM drivers ORDER BY active DESC, name")],
        # Picking a driver fills in their current setup and kart.
        "entries": [(r["entry_id"], f"#{r['number']} {r['name']} · {r['kart_class']}"
                     + ("" if r["active"] else " (former)"),
                     {"setup_id": r["setup_id"], "kart_id": r["kart_id"]})
                    for r in query(ENTRY_SQL + " ORDER BY d.active DESC, en.active DESC, CAST(en.number AS INTEGER), en.kart_class")],
        "shims": [(i, f"{i} above · {db.SHIMS - i} below" + (" (standard)" if i == db.SHIMS // 2 else ""), None)
                  for i in range(db.SHIMS + 1)],
        "karts": karts,
        "karts_new": [("new", "＋ New kart for this entry", None)] + karts,
        "setups": [(r["id"], f"{r['name']} — {commands.spec_label(r)}"
                    + (f"  (#{r['used_by'].replace(',', ', #')})" if r["used_by"] else ""), None)
                   for r in query(SETUP_SQL + SETUP_ORDER)],
        "tracks": [(r["id"], r["name"], None) for r in query("SELECT * FROM tracks ORDER BY name")],
        "run_events": [(r["id"], f"{r['start_date']} · {r['name']}", None)
                       for r in query(f"SELECT * FROM events WHERE event_type IN ({','.join('?' * len(db.RUN_EVENT_TYPES))}) "
                                      "ORDER BY start_date DESC", db.RUN_EVENT_TYPES)],
        "races": [(r["id"], f"{r['start_date']} · {r['name']}", None)
                  for r in query("SELECT * FROM events WHERE event_type = 'Race' ORDER BY start_date DESC")],
    }


ROSTER_SQL = """
SELECT ee.id AS roster_id, ee.number AS race_number, en.id AS entry_id, en.number AS entry_number, en.kart_class,
       en.driver_id, d.name, k.name AS kart_name, s.name AS setup_name,
       (SELECT COUNT(*) FROM event_entries x WHERE x.event_id = ee.event_id AND x.number = ee.number) AS number_shared
FROM event_entries ee JOIN entries en ON en.id = ee.entry_id JOIN drivers d ON d.id = en.driver_id
JOIN karts k ON k.id = en.kart_id JOIN setups s ON s.id = en.setup_id
WHERE ee.event_id = ?
ORDER BY CAST(ee.number AS INTEGER), ee.number, en.kart_class
"""


WEEKEND_SQL = "SELECT * FROM weekend_sessions WHERE event_id = ? ORDER BY day, seq"


def event_days(event):
    """Every date the event runs, e.g. ['2026-12-27', '2026-12-28', '2026-12-29']."""
    start = dt.date.fromisoformat(event["start_date"])
    end = dt.date.fromisoformat(event["end_date"] or event["start_date"])
    return [(start + dt.timedelta(days=d)).isoformat() for d in range(max(0, (end - start).days) + 1)]


def weekend(event_id):
    """The weekend format grouped by day: [(day, [sessions in running order])]."""
    days = OrderedDict()
    for s in query(WEEKEND_SQL, (event_id,)):
        days.setdefault(s["day"], []).append(s)
    return list(days.items())


def raceday_event():
    """The race whose entry list the command bar uses: the one picked on its race page this session,
    else a race with an entry list that's running today."""
    has_list = "EXISTS (SELECT 1 FROM event_entries WHERE event_id = e.id)"
    chosen = session.get("raceday")
    if chosen:
        row = query(EVENT_SQL + f" WHERE e.id = ? AND {has_list}", (chosen,), one=True)
        if row:
            return row
    return query(EVENT_SQL + f" WHERE ? BETWEEN e.start_date AND COALESCE(e.end_date, e.start_date) "
                 f"AND e.status != 'Cancelled' AND {has_list} ORDER BY e.start_date LIMIT 1", (today(),), one=True)


@app.context_processor
def inject_globals():
    return dict(FORMS=FORMS, form_options=form_options, today=today(), classes=db.CLASSES,
                keywords=commands.KEYWORDS,
                raceday=raceday_event() if g.get("admin") else None)


def coerce(field, raw):
    raw = (raw or "").strip()
    kind = field["type"]
    if kind == "check":
        return 1 if raw else 0
    if raw == "":
        if field.get("required"):
            raise ValueError(f"{field['label']} is required")
        return None
    if kind == "lap":
        return parse_lap(raw)
    if kind == "number":
        try:
            num = float(raw)
        except ValueError:
            raise ValueError(f"{field['label']} must be a number")
        return int(num) if num.is_integer() and field.get("step") is None else num
    if kind == "select" and field["options"] not in TEXT_OPTIONS:
        return int(raw)
    return raw


def back(default="dashboard"):
    """Back to the page the form was on: its 'next' field, else the referring page on this site."""
    nxt = request.form.get("next") or request.referrer or ""
    if nxt.startswith(request.host_url):
        nxt = "/" + nxt[len(request.host_url):]
    return redirect(auth.safe_next(nxt) or url_for(default))



@app.post("/save/<key>")
def save(key):
    spec = FORMS.get(key) or abort(404)
    table = spec["table"]
    record_id = request.form.get("id", type=int)
    conn = db.get_db()
    try:
        fields = spec["fields"]
        new_kart = key in ("driver_new", "entry") and request.form.get("kart_id") == "new"
        if new_kart:
            fields = [f for f in fields if f["name"] != "kart_id"]
        values = {f["name"]: coerce(f, request.form.get(f["name"])) for f in fields}
        if new_kart:
            values["kart_id"] = db.insert(conn, "karts", name=f"#{values['number']} kart", kart_class=values["kart_class"])
        if key in ("run", "result"):
            entry = query("SELECT * FROM entries WHERE id = ?", (values["entry_id"],), one=True) or abort(400)
            values["driver_id"] = entry["driver_id"]
            if key == "result":
                values["kart_class"] = values["kart_class"] or entry["kart_class"]
                values.update(classify(values["classification"], values["penalty_positions"], values["penalty_reason"]))
                values["kart_number"] = values["kart_number"] or entry["number"]
        if key == "driver_new":
            # A new driver plus their first entry.
            entry_values = {k: values.pop(k) for k in ("kart_class", "number", "kart_id", "setup_id")}
            driver_id = db.insert(conn, "drivers", **values)
            db.insert(conn, "entries", driver_id=driver_id, **entry_values)
            conn.commit()
            flash(f"Driver added: #{entry_values['number']} {values['name']} ({entry_values['kart_class']})", "success")
            return back()
    except ValueError as exc:
        flash(str(exc), "danger")
        return back()

    old = query(f"SELECT * FROM {table} WHERE id = ?", (record_id,), one=True) if record_id else None
    if record_id and old is None:
        abort(404)

    if key == "run":
        # Every run belongs to a race or RMS test day; its track is that event's track.
        event = query("SELECT track_id FROM events WHERE id = ?", (values["event_id"],), one=True)
        if event is None:
            flash("That race or test day no longer exists.", "danger")
            return back()
        values["track_id"] = event["track_id"]
        if old is None or old["entry_id"] != values["entry_id"]:
            # A run keeps the class and number it was run with, even after the driver swaps class.
            entry = query("SELECT * FROM entries WHERE id = ?", (values["entry_id"],), one=True)
            values.update(kart_class=entry["kart_class"], kart_number=entry["number"])
    if key == "result":
        event = query("SELECT * FROM events WHERE id = ?", (values["event_id"],), one=True)
        values["main"] = is_main(event, values["session"], event_sessions(event))
    if key == "track" and not values["short_name"]:
        values["short_name"] = db.default_short_name(values["name"])
    if key == "setup":
        values["updated_at"] = now()
        match = commands.find_setup(conn, values, values["track_id"])
        if match and match["id"] != record_id:
                flash(Markup(f'That setup already exists: <a href="{url_for("setup_detail", setup_id=match["id"])}">'
                             f'{escape(match["name"])}</a>'), "warning")
                return back()

    try:
        if old:
            cols = ", ".join(f"{c} = ?" for c in values)
            conn.execute(f"UPDATE {table} SET {cols} WHERE id = ?", (*values.values(), record_id))
            if key == "setup" and old["track_id"] != values["track_id"]:
                db.number_setup(conn, record_id, values["track_id"])  # moved track: next number there
            if key == "track":
                db.rename_track_setups(conn, record_id)
            if key == "event":
                recompute_main(conn, record_id)
            flash(f"{spec['title']} updated", "success")
        else:
            if key == "setup":
                values["created_at"] = values["updated_at"]
            new_id = db.insert(conn, table, **values)
            if key == "setup":
                flash(f"Setup added as {db.number_setup(conn, new_id, values['track_id'])}", "success")
            else:
                flash(f"{spec['title']} added", "success")
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        msg = ("That kart number is already taken in that class" if "entries.kart_class" in str(exc)
               else "That driver already has a class; swap it on their page" if "entries.driver_id" in str(exc)
               else "That driver already has a position in this series; edit it instead" if "series_positions" in str(exc)
               else f"Couldn't save: {exc}")
        flash(msg, "danger")
    return back()


@app.post("/delete/<key>/<int:record_id>")
def delete(key, record_id):
    spec = FORMS.get(key) or abort(404)
    conn = db.get_db()
    try:
        conn.execute(f"DELETE FROM {spec['table']} WHERE id = ?", (record_id,))
        conn.commit()
        flash(f"{spec['title']} deleted", "secondary")
    except sqlite3.IntegrityError:
        conn.rollback()
        if key == "track":
            n = query("SELECT COUNT(*) FROM setups WHERE track_id = ?", (record_id,), one=True)[0]
            flash(f"Can't delete: {n} setup{'s' if n != 1 else ''} belong to this track. Delete or move them first.", "danger")
            return back()
        col = "setup_id" if key == "setup" else "kart_id"
        users = query(f"SELECT number FROM entries WHERE {col} = ?", (record_id,))
        flash(f"Can't delete: driver{'s' if len(users) != 1 else ''} "
              f"{', '.join('#' + u['number'] for u in users)} still use{'s' if len(users) == 1 else ''} it. "
              f"Move them to another {spec['title'].lower()} first.", "danger")
        return back()
    return back()


# ---------------------------------------------------------------- race-day commands

def outcome_html(o):
    who = f"<strong>#{escape(o.driver['number'])} {escape(o.driver['name'])}</strong> · " if o.driver else ""
    changes = " · ".join(f"{escape(label)} {escape(a)} → <strong>{escape(b)}</strong>" for label, a, b in o.changes)
    body = escape(o.message) if not changes else Markup(changes) + Markup(" · ") + escape(o.message.split(" → ", 1)[-1])
    warn = "".join(f'<div class="small"><i class="bi bi-exclamation-triangle"></i> {escape(w)}</div>' for w in o.warnings)
    return Markup(f'<code class="me-2">{escape(o.line)}</code>{who}{body}{warn}')


@app.post("/cmd")
def run_command():
    event = raceday_event()
    for o in commands.run(db.get_db(), request.form.get("q", ""), event_id=event["id"] if event else None):
        cat = "danger" if not o.ok else "warning" if o.warnings else "secondary" if o.kind == "noop" else "success"
        flash(outcome_html(o), cat)
    return back()


@app.get("/cmd/preview")
def preview_command():
    event = raceday_event()
    outcomes = commands.run(db.get_db(), request.args.get("q", ""), dry_run=True, event_id=event["id"] if event else None)
    return jsonify([o.as_dict() for o in outcomes])


@app.post("/drivers/<int:driver_id>/retire")
def driver_retire(driver_id):
    """Take a driver off the team. Results, standings, runs and setup history all stay."""
    driver = query("SELECT * FROM drivers WHERE id = ?", (driver_id,), one=True) or abort(404)
    left_on = (request.form.get("left_on") or "").strip() or None
    note = (request.form.get("left_note") or "").strip() or None
    conn = db.get_db()
    conn.execute("UPDATE drivers SET active = 0, left_on = ?, left_note = ? WHERE id = ?", (left_on, note, driver_id))
    conn.commit()
    flash(f"{driver['name']} retired. Their results and records are kept.", "secondary")
    return back()


@app.post("/drivers/<int:driver_id>/reinstate")
def driver_reinstate(driver_id):
    driver = query("SELECT * FROM drivers WHERE id = ?", (driver_id,), one=True) or abort(404)
    conn = db.get_db()
    conn.execute("UPDATE drivers SET active = 1, left_on = NULL, left_note = NULL WHERE id = ?", (driver_id,))
    conn.commit()
    flash(f"{driver['name']} is back on the team.", "success")
    return back()


@app.post("/races/<int:event_id>/entry-list")
def race_entry_list_save(event_id):
    """Save who races this event, in which class, and the kart number each runs that weekend."""
    event = query("SELECT * FROM events WHERE id = ?", (event_id,), one=True) or abort(404)
    picked = {}
    for entry_id in request.form.getlist("entry_id", type=int):
        if request.form.get(f"in_{entry_id}"):
            entry = query("SELECT * FROM entries WHERE id = ?", (entry_id,), one=True)
            if entry:
                picked[entry_id] = (entry, (request.form.get(f"num_{entry_id}") or "").strip().lstrip("#") or entry["number"])
    clash = {}
    for entry, number in picked.values():
        clash.setdefault((entry["kart_class"], number), []).append(entry)
    dupes = [f"#{n} {c}" for (c, n), es in clash.items() if len(es) > 1]
    if dupes:
        flash(f"Nothing saved: {', '.join(dupes)} is used twice in the same class at this race.", "danger")
        return back()
    conn = db.get_db()
    conn.execute("DELETE FROM event_entries WHERE event_id = ?", (event_id,))
    for entry_id, (entry, number) in picked.items():
        db.insert(conn, "event_entries", event_id=event_id, entry_id=entry_id, number=number)
    conn.commit()
    shared = [f"#{n}" for n in {n for _, n in picked.values()} if sum(1 for _, x in picked.values() if x == n) > 1]
    flash(f"Entry list saved: {len(picked)} entr{'y' if len(picked) == 1 else 'ies'} for {event['name']}."
          + (f" {', '.join(shared)} is in two classes; add the class in commands (e.g. “77 heavy”)." if shared else ""),
          "warning" if shared else "success")
    return back()


@app.post("/races/<int:event_id>/raceday")
def race_set_raceday(event_id):
    query("SELECT id FROM events WHERE id = ?", (event_id,), one=True) or abort(404)
    session["raceday"] = event_id
    flash("Race day set: the command bar now reads kart numbers from this race's entry list.", "success")
    return back()


@app.post("/raceday/clear")
def raceday_clear():
    session.pop("raceday", None)
    flash("Race day cleared: the command bar uses each entry's usual number.", "secondary")
    return back()


@app.get("/raceday")
def raceday():
    """Race day: pick a race or RMS test day, assign drivers, and work from a card per driver with their current setup,
    their runs there and a Log run button. Setups can be dragged onto a card. Opening an event here makes it the
    race-day event, so the command bar reads its kart numbers."""
    types = db.RUN_EVENT_TYPES
    events = query(EVENT_SQL + f" WHERE e.event_type IN ({','.join('?' * len(types))}) AND e.status != 'Cancelled' "
                   "ORDER BY e.start_date DESC", types)
    chosen = request.args.get("event", type=int)
    event = next((e for e in events if e["id"] == chosen), None) if chosen else None
    if event is None:
        current = raceday_event() or query(EVENT_SQL + f" WHERE e.event_type IN ({','.join('?' * len(types))}) AND "
                                           "e.status != 'Cancelled' AND COALESCE(e.end_date, e.start_date) >= ? "
                                           "ORDER BY e.start_date LIMIT 1", (*types, today()), one=True)
        event = current or (events[0] if events else None)
    if event is None:
        return render_template("raceday.html", event=None, events=events)
    session["raceday"] = event["id"]
    roster = query(ROSTER_SQL, (event["id"],))
    fmt = query(WEEKEND_SQL, (event["id"],))
    # Where the weekend is: a session is done once every driver on the entry list has a run logged in it.
    logged = {}
    for r in query("SELECT session, entry_id FROM runs WHERE event_id = ?", (event["id"],)):
        logged.setdefault(r["session"], set()).add(r["entry_id"])
    everyone = {r["entry_id"] for r in roster}
    done = {s["name"] for s in fmt if everyone and everyone <= logged.get(s["name"], set())}
    up_next = next((s for s in fmt if s["name"] not in done), None)
    cards = []
    for r in roster:
        entry = query(ENTRY_SQL + " WHERE en.id = ?", (r["entry_id"],), one=True)
        setup = query(SETUP_SQL + " WHERE s.id = ?", (entry["setup_id"],), one=True)
        runs = query(RUN_SQL + " WHERE r.event_id = ? AND r.entry_id = ? ORDER BY r.run_date DESC, r.id DESC",
                     (event["id"], r["entry_id"]))
        # What to type for this driver here: their number at this race, plus the class if another class shares it.
        cmd = r["race_number"] + (f" {r['kart_class'].lower()}" if r["number_shared"] > 1 else "")
        # Best lap already logged per session, so Log run can say "already in for Practice 2".
        logged_laps = {}
        for run in runs:
            if run["session"] and run["best_lap"] is not None:
                logged_laps[run["session"]] = min(run["best_lap"], logged_laps.get(run["session"], run["best_lap"]))
        cards.append(dict(roster=r, entry=entry, setup=setup, runs=runs, cmd=cmd,
                          logged={s: fmt_lap(v) for s, v in logged_laps.items()}))
    on_list = {r["entry_id"]: r["race_number"] for r in roster}
    candidates = query(ENTRY_SQL + " WHERE (d.active = 1 AND en.active = 1) OR en.id IN "
                       "(SELECT entry_id FROM event_entries WHERE event_id = ?)" + ENTRY_ORDER, (event["id"],))
    # The setup tray: this track's setups first, then the rest.
    tray = query(SETUP_SQL + " ORDER BY s.track_id IS NOT ?, t.name, s.track_seq", (event["track_id"],))
    target = query("SELECT target_lap FROM tracks WHERE id = ?", (event["track_id"],), one=True)
    # The team's best at this track, from any visit: fastest lap (practice or race) and best race finish.
    best_lap = query(f"SELECT * FROM ({LAPS_SQL}) WHERE track_id = ? ORDER BY best_lap, lap_date LIMIT 1",
                     (event["track_id"],), one=True)
    best_result = query(RESULT_SQL + " WHERE e.track_id = ? AND res.main = 1 AND res.dnf = 0 AND res.finish_pos IS NOT NULL "
                        "ORDER BY res.finish_pos, e.start_date DESC LIMIT 1", (event["track_id"],), one=True)
    in_progress = event["start_date"] <= today() <= (event["end_date"] or event["start_date"])
    return render_template("raceday.html", event=event, events=events, cards=cards, on_list=on_list,
                           candidates=candidates, tray=tray, target=target["target_lap"] if target else None,
                           best_lap=best_lap, best_result=best_result,
                           run_date=today() if in_progress else event["start_date"],
                           weekend=weekend(event["id"]), days=event_days(event), done=done, up_next=up_next,
                           session_names=[s["name"] for s in fmt] or db.SESSIONS,
                           conditions=db.TRACK_CONDITIONS, kinds=db.SESSION_KINDS,
                           show_roster=request.args.get("roster") == "1" or not roster)


@app.post("/raceday/<int:event_id>/runs")
def raceday_log_runs(event_id):
    """Log run on race day: one session, one run per driver on the entry list. Drivers left blank are skipped."""
    event = query("SELECT * FROM events WHERE id = ?", (event_id,), one=True) or abort(404)
    form = request.form
    shared = dict(event_id=event_id, track_id=event["track_id"], session=form.get("session", "").strip() or None,
                  run_date=form.get("run_date") or today(), track_condition=form.get("track_condition") or None,
                  air_temp=form.get("air_temp", type=float), track_temp=form.get("track_temp", type=float))
    conn = db.get_db()
    saved, skipped = [], 0
    try:
        for entry_id in form.getlist("entry_id", type=int):
            lap = form.get(f"best_lap_{entry_id}", "").strip()
            laps_text = form.get(f"laps_{entry_id}", "").strip()
            try:
                laps = int(float(laps_text)) if laps_text else None
            except ValueError:
                raise ValueError(f"Laps '{laps_text}' should be a number")
            notes = form.get(f"notes_{entry_id}", "").strip()
            if not (lap or laps or notes):
                skipped += 1
                continue
            entry = query(ENTRY_SQL + " WHERE en.id = ?", (entry_id,), one=True) or abort(400)
            on_list = query("SELECT number FROM event_entries WHERE event_id = ? AND entry_id = ?", (event_id, entry_id), one=True)
            db.insert(conn, "runs", driver_id=entry["driver_id"], entry_id=entry_id, kart_id=entry["kart_id"],
                      kart_class=entry["kart_class"], kart_number=on_list["number"] if on_list else entry["number"],
                      setup_id=form.get(f"setup_{entry_id}", type=int) or entry["setup_id"],
                      best_lap=parse_lap(lap), laps=laps, notes=notes or None, **shared)
            saved.append(entry["name"].split()[0])
    except ValueError as exc:
        conn.rollback()
        flash(f"Nothing saved: {exc}", "danger")
        return back("raceday")
    conn.commit()
    what = f" for {shared['session']}" if shared["session"] else ""
    if saved:
        flash(f"Logged {len(saved)} run{'s' if len(saved) != 1 else ''}{what}: {', '.join(saved)}"
              + (f" · {skipped} left blank" if skipped else ""), "success")
    else:
        flash("Nothing logged: every driver was left blank.", "secondary")
    return back("raceday")

# ---------------------------------------------------------------- day report
# A day written up as Markdown to paste into an AI chat for a summary and setup suggestions: what ran, every
# driver's laps, setups and setup changes, results, their history at the track, what's still to come, then the ask.

SETUP_FIELDS = [("rear_sprocket", "Rear sprocket", "T"), ("front_sprocket", "Front sprocket", "T"),
                ("front_width", "Front track width", " in"), ("rear_width", "Rear track width", " in"),
                ("psi", "Tire pressure (all four)", " psi"), ("front_shims_above", "Front ride height", ""),
                ("tire", "Tire", ""), ("axle", "Axle", ""), ("caster", "Caster", ""), ("camber", "Camber", ""),
                ("seat_position", "Seat", ""), ("total_weight", "Total weight", " lb"), ("ballast", "Ballast", ""),
                ("carb_jetting", "Carb jetting", ""), ("clutch", "Clutch", ""), ("notes", "Notes", "")]


def setup_spec(s):
    parts = []
    for col, label, unit in SETUP_FIELDS:
        value = s[col]
        if value is None or value == "":
            continue
        if col == "front_shims_above":
            text = f"{value} shims above / {db.SHIMS - value} below the spindle"
        else:
            text = (fmt_num(value) if isinstance(value, (int, float)) else str(value)) + unit
        parts.append(f"{label} {text}")
    return "; ".join(parts)


def gap(lap, ref):
    return f"{lap - ref:+.3f}" if lap is not None and ref is not None else "—"


def cell(text):
    return (str(text) if text not in (None, "") else "—").replace("|", "/").replace("\n", " ")


def day_report(event, day):
    days = event_days(event)
    fmt = query(WEEKEND_SQL, (event["id"],))
    on_day = [s for s in fmt if s["day"] == day]
    later = [s for s in fmt if s["day"] > day]
    track = query("SELECT * FROM tracks WHERE id = ?", (event["track_id"],), one=True)
    target = track["target_lap"] if track else None
    roster = {r["entry_id"]: r for r in query(ROSTER_SQL, (event["id"],))}
    runs = query(RUN_SQL + " WHERE r.event_id = ? AND r.run_date = ? ORDER BY r.id", (event["id"], day))
    # Results go with the day their session runs on; with no weekend format, with the last day.
    names_on_day = {s["name"] for s in on_day}
    results = [r for r in query(RESULT_SQL + " WHERE res.event_id = ? ORDER BY res.finish_pos IS NULL, res.finish_pos",
                                (event["id"],))
               if r["session"] in names_on_day or (not fmt and day == days[-1])]
    start = dt.datetime.fromisoformat(day).replace(tzinfo=db.TZ).timestamp()
    end = (dt.datetime.fromisoformat(day) + dt.timedelta(days=1)).replace(tzinfo=db.TZ).timestamp()
    entry_ids = list(roster) + [x["entry_id"] for x in runs + results if x["entry_id"] not in roster]
    entry_ids = list(dict.fromkeys(e for e in entry_ids if e))
    changes = [c for c in query(CHANGE_SQL + " WHERE c.created_at >= ? AND c.created_at < ? ORDER BY c.id",
                                (start, end)) if c["entry_id"] in entry_ids]
    entries = {e["entry_id"]: e for e in query(
        ENTRY_SQL + f" WHERE en.id IN ({','.join('?' * len(entry_ids))})", entry_ids)} if entry_ids else {}
    # Each driver's setup at the end of the day: their last change that day, else what they last ran on,
    # else (for today or a day still to come) their current setup.
    end_setup = {}
    for entry_id, e in entries.items():
        last_change = [c for c in changes if c["entry_id"] == entry_id]
        last_run = [r for r in runs if r["entry_id"] == entry_id and r["setup_id"]]
        end_setup[entry_id] = (last_change[-1]["to_setup_id"] if last_change else last_run[-1]["setup_id"] if last_run
                               else e["setup_id"] if day >= today() else None)

    # Every setup that came up today, listed once in full.
    setup_ids = {r["setup_id"] for r in runs + results} | set(end_setup.values())
    setup_ids |= {c["from_setup_id"] for c in changes} | {c["to_setup_id"] for c in changes}
    setup_ids.discard(None)
    setups = {s["id"]: s for s in query(SETUP_SQL + f" WHERE s.id IN ({','.join('?' * len(setup_ids))})",
                                        list(setup_ids))} if setup_ids else {}
    name = lambda sid: setups[sid]["name"] if sid in setups else "—"

    # Fastest RMS lap per (session, class), for gaps between teammates.
    fastest = {}
    for r in runs:
        if r["best_lap"] is not None:
            key = (r["session"], r["kart_class"])
            fastest[key] = min(r["best_lap"], fastest.get(key, r["best_lap"]))

    when = dt.date.fromisoformat(day)
    out = [f"# RMS day report: {event['name']}, {when.strftime('%A %b')} {when.day}, {when.year}", ""]
    series = event["series"] or "one-off race"
    out.append(f"- **Event:** {event['name']} ({event['event_type']}; {series}"
               + (f", counts toward the {event['season']} season" if event["season"] else "") + ")")
    out.append(f"- **Track:** {track['name'] if track else 'TBD'}"
               + (f", good lap {fmt_lap(target)}" if target else ""))
    out.append(f"- **Day:** {days.index(day) + 1} of {len(days)}"
               + (f" ({days[0]} to {days[-1]})" if len(days) > 1 else ""))
    fmt_line = lambda ss: ", ".join(s["name"] + (f" {s['start_time']}" if s["start_time"] else "") for s in ss)
    ran = list(dict.fromkeys(r["session"] for r in runs if r["session"]))
    out.append(f"- **Sessions today:** " + (fmt_line(on_day) if on_day else
                                           (", ".join(ran) + " (from runs logged; no weekend format set)") if ran
                                           else "no weekend format set"))
    if later:
        out.append(f"- **Still to come:** " + "; ".join(
            f"{dt.date.fromisoformat(d).strftime('%a')} {fmt_line([s for s in later if s['day'] == d])}"
            for d in dict.fromkeys(s["day"] for s in later)))
    out += ["", "How to read this: lap times are in seconds (m:ss.sss over a minute). \"vs good lap\" compares to the "
            "track's reference lap; \"vs fastest\" compares to the quickest RMS driver in the same class and session. "
            "Setups are named by track and number (\"Daytona 2\"); changes were made with race-day commands "
            "(SPR rear sprocket, FTW/RTW front/rear track width in inches, PSI one pressure for all four tires, "
            "FRH front ride height in shims above the spindle out of 6, 3/3 standard). The front sprocket is locked "
            "in Ignite Sr.", ""]

    # Conditions, per session, from what was logged.
    conditions = OrderedDict()
    for r in runs:
        c = conditions.setdefault(r["session"] or "Run", {"track": None, "air": None, "temp": None})
        c["track"] = c["track"] or r["track_condition"]
        c["air"] = c["air"] if c["air"] is not None else r["air_temp"]
        c["temp"] = c["temp"] if c["temp"] is not None else r["track_temp"]
    if conditions:
        out += ["## Conditions", "", "| Session | Track | Air °F | Track °F |", "|---|---|---|---|"]
        out += [f"| {s} | {cell(c['track'])} | {cell(fmt_num(c['air']) if c['air'] is not None else None)} | "
                f"{cell(fmt_num(c['temp']) if c['temp'] is not None else None)} |" for s, c in conditions.items()]
        out.append("")

    out += ["## Drivers", ""]
    if not entries:
        out += ["No drivers on the entry list and nothing logged this day.", ""]
    for entry_id in entry_ids:
        e = entries.get(entry_id)
        if not e:
            continue
        number = roster[entry_id]["race_number"] if entry_id in roster else e["number"]
        out.append(f"### #{number} {e['name']} · {e['kart_class']} · {e['kart_name']}")
        mine = [r for r in runs if r["entry_id"] == entry_id]
        my_changes = [c for c in changes if c["entry_id"] == entry_id]
        order = {s: i for i, s in enumerate(event_sessions(event, results))}
        my_results = sorted((r for r in results if r["entry_id"] == entry_id), key=lambda r: order.get(r["session"], 99))
        if end_setup.get(entry_id):
            out.append(f"- **Setup at end of day:** {name(end_setup[entry_id])}")
        if my_changes:
            out.append("- **Setup changes today:** " + "; ".join(
                f"{dt.datetime.fromtimestamp(c['created_at'], db.TZ).strftime('%H:%M')} `{c['command']}` "
                f"({name(c['from_setup_id'])} → {name(c['to_setup_id'])})" for c in my_changes))
        else:
            out.append("- **Setup changes today:** none")
        if mine:
            out += ["", "| Session | Best lap | vs good lap | vs fastest | Laps | Setup | Notes |",
                    "|---|---|---|---|---|---|---|"]
            for r in mine:
                out.append(f"| {cell(r['session'])} | {fmt_lap(r['best_lap'])} | {gap(r['best_lap'], target)} | "
                           f"{gap(r['best_lap'], fastest.get((r['session'], r['kart_class'])))} | {cell(r['laps'])} | "
                           f"{name(r['setup_id'])} | {cell(r['notes'])} |")
            out.append("")
        else:
            out.append("- **Runs today:** none logged")
        for r in my_results:
            bits = [f"qualified P{r['qual_pos']}" if r["qual_pos"] else None,
                    (r["classification"] or "DNF") if r["dnf"] else (f"finished P{r['finish_pos']}" if r["finish_pos"] else None),
                    (f"penalty: {r['penalty_positions'] or '?'} positions lost"
                     + (f" ({r['penalty_reason']})" if r["penalty_reason"] else "")) if r["classification"] == "PEN" else None,
                    f"best {fmt_lap(r['best_lap'])}" if r["best_lap"] else None,
                    f"{fmt_num(r['points'])} pts" if r["points"] is not None else None,
                    r["notes"]]
            out.append(f"- **{r['session']} result:** " + (", ".join(b for b in bits if b) or "entered, no details"))
        before = query(RUN_SQL + " WHERE r.track_id = ? AND r.entry_id = ? AND r.best_lap IS NOT NULL "
                       "AND r.run_date < ? ORDER BY r.best_lap LIMIT 1", (event["track_id"], entry_id, event["start_date"]),
                       one=True)
        out.append(f"- **Best here before this event:** " + (
            f"{fmt_lap(before['best_lap'])} on {before['run_date']} ({before['setup_name'] or 'setup unknown'})"
            if before else "no earlier laps logged at this track"))
        out.append("")

    if setups:
        out += ["## Setups referenced", ""]
        out += [f"- **{s['name']}:** {setup_spec(s)}" for s in sorted(setups.values(), key=lambda s: s["name"])]
        out.append("")

    ahead = (" for " + ", ".join(s["name"] for s in later[:4])) if later else " for the next time we race here"
    out += ["## What I'd like from you", "",
            "You're helping a karting team review the day. Using only the data above:",
            "1. Summarize the day in a few sentences: pace, who improved, what the conditions did.",
            "2. For each driver, say what seemed to work and what didn't, tying lap times to the setup changes "
            "they made.",
            f"3. Suggest setup changes{ahead} (specific values where you can, e.g. SPR 61 or PSI 30), "
            "with your reasoning and how confident you are.",
            "4. Flag anything odd or missing in the data that we should check or log next time.",
            "Keep it practical and short enough to read in the pits."]
    return "\n".join(out) + "\n"


def report_days(event):
    """Each event day, and the day to open on: today if the event is on, else the last day with runs."""
    days = event_days(event)
    logged = [r[0] for r in query("SELECT DISTINCT run_date FROM runs WHERE event_id = ? ORDER BY 1", (event["id"],))]
    default = today() if today() in days else next((d for d in reversed(days) if d in logged), days[0])
    return days, logged, default


@app.get("/raceday/<int:event_id>/report")
def raceday_report(event_id):
    """The day report page, or the Markdown file itself with ?download=1."""
    event = query("SELECT * FROM events WHERE id = ?", (event_id,), one=True) or abort(404)
    days, logged, default = report_days(event)
    day = request.args.get("day") if request.args.get("day") in days else default
    text = day_report(event, day)
    if request.args.get("download"):
        slug = "".join(ch if ch.isalnum() else "-" for ch in event["name"].lower()).strip("-")
        return app.response_class(text, mimetype="text/markdown", headers={
            "Content-Disposition": f'attachment; filename="rms-{slug}-{day}.md"'})
    return render_template("day_report.html", event=event, days=days, logged=logged, day=day, text=text)


@app.post("/races/<int:event_id>/format")
def race_format_save(event_id):
    """Save the weekend format: each session's day, time, name and type, in running order."""
    event = query("SELECT * FROM events WHERE id = ?", (event_id,), one=True) or abort(404)
    form = request.form
    # The editor also sets the race's dates, so a session can go on a day the race didn't cover before.
    start, end = form.get("start_date") or event["start_date"], form.get("end_date") or None
    try:
        dt.date.fromisoformat(start)
        if end:
            dt.date.fromisoformat(end)
    except ValueError:
        flash("Those dates don't look right.", "danger")
        return back()
    if end and end < start:
        flash("The last day can't be before the first day.", "danger")
        return back()
    end = None if end == start else end
    event = {**dict(event), "start_date": start, "end_date": end}
    days = event_days(event)
    rows = [(day if day in days else days[0], time_.strip() or None, name.strip(),
             kind if kind in db.SESSION_KINDS else db.session_kind(name))
            for day, time_, name, kind in zip(form.getlist("day"), form.getlist("time"), form.getlist("name"),
                                              form.getlist("kind")) if name.strip()]
    conn = db.get_db()
    conn.execute("UPDATE events SET start_date = ?, end_date = ? WHERE id = ?", (start, end, event_id))
    conn.execute("DELETE FROM weekend_sessions WHERE event_id = ?", (event_id,))
    for seq, (day, time_, name, kind) in enumerate(rows):
        db.insert(conn, "weekend_sessions", event_id=event_id, day=day, start_time=time_, name=name, kind=kind, seq=seq)
    # Everything but practice is scored, so results follow the format, unless results are already
    # in under a session the format no longer lists.
    scored = list(dict.fromkeys(name for day, _, name, kind in sorted(rows, key=lambda r: r[0]) if kind != "Practice"))
    listed = {name for _, _, name, _ in rows}
    have = {r[0] for r in query("SELECT DISTINCT session FROM results WHERE event_id = ?", (event_id,))}
    if scored and have <= listed:
        conn.execute("UPDATE events SET sessions = ? WHERE id = ?", (", ".join(scored), event_id))
        recompute_main(conn, event_id)
    elif scored:
        flash(f"Results are already in for {', '.join(sorted(have - listed))}, so the scored sessions "
              "were left as they were.", "warning")
    conn.commit()
    flash(f"Weekend format saved: {len(rows)} session{'s' if len(rows) != 1 else ''} over "
          f"{len({r[0] for r in rows})} day{'s' if len({r[0] for r in rows}) != 1 else ''}" if rows
          else "Weekend format cleared", "success")
    return back()


@app.post("/raceday/assign-setup")
def raceday_assign_setup():
    conn = db.get_db()
    try:
        flash(commands.assign_setup(conn, request.form.get("entry_id", type=int), request.form.get("setup_id", type=int)),
              "success")
        conn.commit()
    except commands.CommandError as exc:
        conn.rollback()
        flash(str(exc), "danger")
    return back("raceday")


@app.post("/changes/<int:change_id>/undo")
def undo_change(change_id):
    conn = db.get_db()
    try:
        flash(outcome_html(commands.undo(conn, change_id)), "success")
        conn.commit()
    except commands.CommandError as exc:
        conn.rollback()
        flash(str(exc), "danger")
    return back()


# ---------------------------------------------------------------- pages

HONOURS = {1: "Champion", 2: "Vice-Champion", 3: "P3"}


def season_summary():
    """The latest season with results, for the public home page: the team's wins, podiums and top 5s, each driver's,
    and championship honours (top three). Former drivers are listed, but after the current ones."""
    season = query(f"SELECT MAX({SEASON_SQL}) FROM events e JOIN results res ON res.event_id = e.id "
                   "WHERE e.status = 'Completed'", one=True)[0]
    if not season:
        return None
    stat = lambda cond: f"SUM(CASE WHEN res.dnf = 0 AND res.finish_pos {cond} THEN 1 ELSE 0 END)"
    drivers = query(f"""
        SELECT d.id, d.name, d.active, {stat('= 1')} AS wins, {stat('<= 3')} AS podiums, {stat('<= 5')} AS top5s,
               COUNT(DISTINCT res.event_id) AS races
        FROM results res JOIN events e ON e.id = res.event_id JOIN drivers d ON d.id = res.driver_id
        WHERE res.main = 1 AND {SEASON_SQL} = ?
        GROUP BY d.id ORDER BY d.active DESC, wins DESC, podiums DESC, top5s DESC, races DESC, d.name""", (season,))
    honours = [dict(r, honour=HONOURS[r["position"]]) for r in query(f"""
        SELECT sp.position, sp.kart_class, d.name, d.active, COALESCE(NULLIF(s.title, ''), sp.series) AS championship
        FROM series_positions sp JOIN drivers d ON d.id = sp.driver_id LEFT JOIN series s ON s.name = sp.series
        WHERE sp.position <= 3 AND sp.season = ?
        ORDER BY d.active DESC, sp.position, (SELECT MIN(start_date) FROM events WHERE series = sp.series)""", (season,))]
    totals = {k: sum(d[k] or 0 for d in drivers) for k in ("wins", "podiums", "top5s")}
    totals["races"] = query(f"SELECT COUNT(DISTINCT e.id) FROM events e JOIN results res ON res.event_id = e.id "
                            f"WHERE e.event_type = 'Race' AND {SEASON_SQL} = ?", (season,), one=True)[0]
    return dict(season=season, drivers=drivers, honours=honours, totals=totals)


@app.get("/")
def dashboard():
    if not g.admin:
        # Public home: the season so far, championship honours and what's next.
        upcoming = query(EVENT_SQL + " WHERE COALESCE(e.end_date, e.start_date) >= ? AND e.status != 'Cancelled' "
                         "AND e.event_type = 'Race' ORDER BY e.start_date LIMIT 3", (today(),))
        return render_template("home.html", summary=season_summary(), upcoming=upcoming)
    stats = dict(
        drivers=query("SELECT COUNT(*) FROM drivers WHERE active = 1", one=True)[0],
        races=query("SELECT COUNT(*) FROM events WHERE event_type = 'Race'", one=True)[0],
        setups=query("SELECT COUNT(*) FROM setups", one=True)[0],
        runs=query("SELECT COUNT(*) FROM runs", one=True)[0],
    )
    upcoming = query(EVENT_SQL + " WHERE COALESCE(e.end_date, e.start_date) >= ? AND e.status != 'Cancelled' "
                     "ORDER BY e.start_date LIMIT 5", (today(),))
    recent = query(RUN_SQL + " ORDER BY r.run_date DESC, r.id DESC LIMIT 8")
    changes = query(CHANGE_SQL + " ORDER BY c.id DESC LIMIT 6")
    lineup = query(ENTRY_SQL + " WHERE d.active = 1 AND en.active = 1" + ENTRY_ORDER)
    race = raceday_event()
    if race:
        # On race day the lineup is the entry list, with the numbers run at this race.
        roster = {r["entry_id"]: r for r in query(ROSTER_SQL, (race["id"],))}
        lineup = sorted(({**dict(e), "number": roster[e["entry_id"]]["race_number"],
                          "number_shared": roster[e["entry_id"]]["number_shared"]}
                         for e in query(ENTRY_SQL + " WHERE en.id IN (SELECT entry_id FROM event_entries WHERE event_id = ?)",
                                        (race["id"],))),
                        key=lambda e: (int(e["number"]) if e["number"].isdigit() else 10**6, e["number"]))
    results = query(RESULT_SQL + " WHERE res.main = 1 AND (res.finish_pos IS NOT NULL OR res.dnf = 1) "
                    "ORDER BY e.start_date DESC, res.dnf, res.finish_pos LIMIT 6")
    next_event = upcoming[0] if upcoming else None
    days_out = (dt.date.fromisoformat(next_event["start_date"]) - db.local_now().date()).days if next_event else None
    return render_template("dashboard.html", stats=stats, upcoming=upcoming, recent=recent, records=track_records()[:6],
                           changes=changes, lineup=lineup, next_event=next_event, days_out=days_out,
                           results=results)


session_list = db.session_list
main_session = db.main_session


def classify(classification, penalty_positions=None, penalty_reason=None):
    """Result columns for a classification: DNF/DNS/DSQ are out (dnf = 1); penalty details only go with PEN."""
    classification = classification if classification in db.CLASSIFICATIONS else None
    pen = classification == "PEN"
    return dict(classification=classification, dnf=int(classification in db.OUT),
                penalty_positions=penalty_positions if pen else None, penalty_reason=(penalty_reason or None) if pen else None)


def is_main(event, session, sessions):
    """Whether a session's result is the race's result: the deciding session, or at a one-off every race
    (practice never counts as a result)."""
    if db.session_kind(session) == "Practice":
        return 0
    return int(separate_races(event) or session == main_session(sessions))


def recompute_main(conn, event_id):
    """Re-mark which results decide the race after its sessions or series change (see is_main)."""
    event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if event is None:
        return
    rows = conn.execute("SELECT id, session FROM results WHERE event_id = ?", (event_id,)).fetchall()
    sessions = event_sessions(event, rows)
    for r in rows:
        conn.execute("UPDATE results SET main = ? WHERE id = ?", (is_main(event, r["session"], sessions), r["id"]))


def separate_races(event):
    """One-off races (no series) sell and score each race on its own: every session is its own result,
    so each counts toward wins and best finish. A series round is decided by its last session."""
    return not (event["series"] or "").strip()


def event_sessions(event, rows=()):
    """Sessions a race scores, in running order, e.g. ['Heat 1', 'Heat 2'] or ['Race 1', 'Race 2', 'Final'].
    A session that has results but isn't listed goes just before the deciding one."""
    listed = session_list(event["sessions"])
    extra = [s for s in dict.fromkeys(r["session"] for r in rows) if s not in listed]
    cut = listed.index(main_session(listed))
    return listed[:cut] + extra + listed[cut:]


def by_round(rows, key):
    """Fold result rows (one per session) into one entry per driver/entry or per race; key is a column name
    ('entry_id', 'event_id') or a function of the row:
    {'sessions': {name: row}, 'ordered': [(name, row)] in running order, 'final': the deciding session's row
     (Final, or Heat 2 at a club race), 'row': that, else the last session, 'points': every session,
     'scored': any points recorded}."""
    out = OrderedDict()
    for r in rows:
        entry = out.setdefault(key(r) if callable(key) else r[key], {"sessions": {}, "points": 0, "scored": False})
        entry["sessions"][r["session"]] = r
        entry["points"] += r["points"] or 0
        entry["scored"] = entry["scored"] or r["points"] is not None
    for entry in out.values():
        first = next(iter(entry["sessions"].values()))
        listed = session_list(first["event_sessions"])
        order = {s: i for i, s in enumerate(listed)}
        late = order.get(main_session(listed), len(listed)) - 0.5
        entry["ordered"] = sorted(entry["sessions"].items(), key=lambda kv: (order.get(kv[0], late), kv[0]))
        mains = [r for _, r in entry["ordered"] if r["main"]]
        entry["separate"] = len(mains) > 1  # one-off: several races that each stand on their own
        # The result that represents the round: the deciding session, or at a one-off the best of its races.
        entry["final"] = min(mains, key=lambda r: (r["dnf"], r["finish_pos"] or 999)) if mains else None
        entry["row"] = entry["final"] or entry["ordered"][-1][1]
        laps = [r["best_lap"] for r in entry["sessions"].values() if r["best_lap"]]
        entry["best_lap"] = min(laps) if laps else None  # fastest lap in any session, qualifying included
    return list(out.values())


def finish_order(entry):
    final = entry["final"]
    earlier = [row["finish_pos"] or 999 for _, row in reversed(entry["ordered"])]
    return (final is None, bool(final and final["dnf"]), (final and final["finish_pos"]) or 999, earlier)


# Every lap time the team has at a track: practice runs and race results (a result's best lap counts too).
LAPS_SQL = """
SELECT r.track_id, t.name AS track_name, t.target_lap, COALESCE(r.kart_class, en.kart_class, k.kart_class) AS kart_class,
       r.driver_id, d.name AS driver_name, COALESCE(r.kart_number, en.number) AS driver_number, r.best_lap,
       r.run_date AS lap_date
FROM runs r JOIN drivers d ON d.id = r.driver_id LEFT JOIN entries en ON en.id = r.entry_id
LEFT JOIN karts k ON k.id = r.kart_id LEFT JOIN tracks t ON t.id = r.track_id
WHERE r.best_lap IS NOT NULL AND r.track_id IS NOT NULL
UNION ALL
SELECT e.track_id, t.name, t.target_lap, res.kart_class, res.driver_id, d.name,
       COALESCE(NULLIF(res.kart_number, ''), en.number), res.best_lap, e.start_date
FROM results res JOIN events e ON e.id = res.event_id JOIN drivers d ON d.id = res.driver_id
LEFT JOIN entries en ON en.id = res.entry_id LEFT JOIN tracks t ON t.id = e.track_id
WHERE res.best_lap IS NOT NULL AND e.track_id IS NOT NULL
"""


def track_records(kart_class=None):
    sql = (f"SELECT * FROM ({LAPS_SQL})" + (" WHERE kart_class = ?" if kart_class else "") + " ORDER BY best_lap")
    best = OrderedDict()
    for r in query(sql, (kart_class,) if kart_class else ()):
        best.setdefault((r["track_id"], r["kart_class"]), r)
    return list(best.values())


@app.get("/drivers")
def drivers():
    # One row per driver with their class; setup, kart and history are on the driver page.
    current = query(DRIVER_SQL + " WHERE d.active = 1 ORDER BY CAST(number AS INTEGER), number")
    former = query(DRIVER_SQL + " WHERE d.active = 0 ORDER BY d.name")
    if not g.admin:
        # Public: names, class and results only; never contacts, setups or why someone left.
        return render_template("drivers_public.html", drivers=current, former=former)
    classes = {}
    for e in query("SELECT * FROM entries WHERE active = 1 ORDER BY id"):
        classes.setdefault(e["driver_id"], []).append(e)
    changes = query(CHANGE_SQL + " ORDER BY c.id DESC LIMIT 12")
    return render_template("drivers.html", drivers=current, former=former, classes=classes, changes=changes)


@app.get("/drivers/<int:driver_id>")
def driver_detail(driver_id):
    driver = query(DRIVER_SQL + " WHERE d.id = ?", (driver_id,), one=True) or abort(404)
    entries = query(ENTRY_SQL + " WHERE en.driver_id = ? ORDER BY en.active DESC, en.id", (driver_id,))
    setups = {s["id"]: s for s in query(SETUP_SQL + " WHERE s.id IN (SELECT setup_id FROM entries WHERE driver_id = ?)",
                                        (driver_id,))}
    changes = query(CHANGE_SQL + " WHERE c.driver_id = ? ORDER BY c.id DESC LIMIT 20", (driver_id,))
    runs = query(RUN_SQL + " WHERE r.driver_id = ? ORDER BY r.run_date DESC, r.id DESC LIMIT 25", (driver_id,))
    results = by_round(query(RESULT_SQL + " WHERE res.driver_id = ? ORDER BY e.start_date DESC, res.session", (driver_id,)),
                       lambda r: (r["event_id"], r["entry_id"]))
    return render_template("driver_detail.html", driver=driver, entries=entries, setups=setups,
                           changes=changes, runs=runs, results=results)


ONE_OFF = "one-off"  # stands in for races outside any series, e.g. the Quincy Grand Prix

# The season a race counts toward: its own season if set (Daytona in Dec 2026 opens the 2027 season), else its year.
SEASON_SQL = "COALESCE(CAST(e.season AS TEXT), substr(e.start_date, 1, 4))"

RACE_SQL = """
SELECT e.*, t.name AS track_name, t.short_name AS track_short, {season} AS year,
       COALESCE(NULLIF(e.series, ''), '{one_off}') AS championship,
       (SELECT COUNT(DISTINCT COALESCE(entry_id, -driver_id)) FROM results WHERE event_id = e.id) AS entries,
       (SELECT MIN(finish_pos) FROM results WHERE event_id = e.id AND main = 1 AND dnf = 0) AS best_finish,
       (SELECT COUNT(*) FROM results WHERE event_id = e.id AND main = 1 AND finish_pos = 1 AND dnf = 0) AS wins,
       (SELECT d.name FROM results r JOIN drivers d ON d.id = r.driver_id
         WHERE r.event_id = e.id AND r.main = 1 AND r.dnf = 0
         ORDER BY r.finish_pos IS NULL, r.finish_pos LIMIT 1) AS top_driver,
       (SELECT MIN(best_lap) FROM results WHERE event_id = e.id) AS fast_lap
FROM events e LEFT JOIN tracks t ON t.id = e.track_id
WHERE e.event_type = 'Race'
""".replace("{one_off}", ONE_OFF).replace("{season}", SEASON_SQL)


def championship_label(key):
    return "One-off races" if key == ONE_OFF else key


def summarize(rows):
    """Roll a list of races up into one summary: counts, dates, tracks, RMS best finish and wins."""
    finishes = [r["best_finish"] for r in rows if r["best_finish"]]
    return dict(races=len(rows), completed=sum(1 for r in rows if r["status"] == "Completed"),
                first=min(r["start_date"] for r in rows), last=max(r["end_date"] or r["start_date"] for r in rows),
                tracks=list(dict.fromkeys(r["track_short"] or r["track_name"] or "TBD" for r in rows)),
                best=min(finishes) if finishes else None, wins=sum(r["wins"] for r in rows),
                upcoming=next((r for r in sorted(rows, key=lambda r: r["start_date"])
                               if (r["end_date"] or r["start_date"]) >= today() and r["status"] != "Cancelled"), None))


@app.get("/races")
def races():
    """Races, level 1: pick a season."""
    by_year = OrderedDict()
    for r in query(RACE_SQL + " ORDER BY year DESC, e.start_date DESC"):
        by_year.setdefault(r["year"], []).append(r)
    seasons = [dict(year=y, championships=len({r["championship"] for r in rows}), **summarize(rows))
               for y, rows in by_year.items()]
    return render_template("races.html", seasons=seasons)


@app.get("/races/season/<int:year>")
def races_season(year):
    """Races, level 2: the championships (and one-off races) in a season."""
    rows = query(RACE_SQL + f" AND {SEASON_SQL} = ? ORDER BY e.start_date", (str(year),))
    if not rows:
        abort(404)
    groups = OrderedDict()
    for r in rows:
        groups.setdefault(r["championship"], []).append(r)
    with_standings = {r[0] for r in query("SELECT DISTINCT e.series FROM events e JOIN results r ON r.event_id = e.id "
                                          "WHERE e.status = 'Completed'")}
    championships = [dict(key=k, name=championship_label(k), standings=k in with_standings, **summarize(v))
                     for k, v in groups.items()]
    # Series first, in the order their seasons start; one-off races last.
    championships.sort(key=lambda c: (c["key"] == ONE_OFF, c["first"]))
    years = [r[0] for r in query(f"SELECT DISTINCT {SEASON_SQL} FROM events e WHERE e.event_type = 'Race' "
                                 "ORDER BY 1 DESC")]
    return render_template("season.html", year=year, years=years, championships=championships, one_off=ONE_OFF)


@app.get("/races/season/<int:year>/<championship>")
def races_championship(year, championship):
    """Races, level 3: every race in one championship that season, in running order."""
    where = " AND COALESCE(e.series, '') = ''" if championship == ONE_OFF else " AND e.series = ?"
    args = (str(year),) + (() if championship == ONE_OFF else (championship,))
    rows = query(RACE_SQL + f" AND {SEASON_SQL} = ?" + where + " ORDER BY e.start_date", args)
    if not rows:
        abort(404)
    summary = summarize(rows)
    has_standings = championship != ONE_OFF and any(r["status"] == "Completed" and r["entries"] for r in rows)
    return render_template("championship.html", year=year, key=championship, name=championship_label(championship),
                           races=rows, summary=summary, has_standings=has_standings, one_off=ONE_OFF)


@app.get("/races/<int:event_id>")
def race_detail(event_id):
    event = query(EVENT_SQL + " WHERE e.id = ?", (event_id,), one=True) or abort(404)
    rows = query(RESULT_SQL + " WHERE res.event_id = ? ORDER BY res.session", (event_id,))
    results = sorted(by_round(rows, lambda r: r["entry_id"] or -r["driver_id"]), key=finish_order)
    sessions = event_sessions(event, rows)
    main = main_session(sessions)  # qualifying, best lap and DNF go with the deciding session
    separate = separate_races(event)
    # One-off races: one tab per race, each its own result sheet.
    by_session = [(s, sorted(((e, e["sessions"][s]) for e in results if s in e["sessions"]),
                             key=lambda pair: (pair[1]["dnf"], pair[1]["finish_pos"] or 999)))
                  for s in sessions] if separate else []
    scored = any(e["scored"] for e in results)
    if not g.admin:
        # Public view: who's racing and the results. Setups, karts, practice sessions and target laps stay with the team.
        racing = [dict(number=r["race_number"], name=r["name"], kart_class=r["kart_class"])
                  for r in query(ROSTER_SQL, (event_id,))]
        return render_template("race_detail.html", event=event, results=results, sessions=sessions, main=main,
                               scored=scored, separate=separate, by_session=by_session, weekend=weekend(event_id),
                               racing=racing, runs=[], grid=[], setups=[], target=None, enter=False)
    roster = query(ROSTER_SQL, (event_id,))
    on_list = {r["entry_id"]: r["race_number"] for r in roster}
    # Entry list editor: every current entry, plus anyone already on the list.
    candidates = query(ENTRY_SQL + " WHERE (d.active = 1 AND en.active = 1) OR en.id IN "
                       "(SELECT entry_id FROM event_entries WHERE event_id = ?)" + ENTRY_ORDER, (event_id,))
    runs = query(RUN_SQL + " WHERE r.event_id = ? ORDER BY r.run_date, r.session, r.best_lap", (event_id,))
    target = query("SELECT target_lap FROM tracks WHERE id = ?", (event["track_id"],), one=True)
    # One grid row per entry on the entry list (else every current entry), plus anyone already in the results.
    by_entry = {r["row"]["entry_id"]: r for r in results}
    who = ("en.id IN (SELECT entry_id FROM event_entries WHERE event_id = :e)" if roster
           else "(d.active = 1 AND en.active = 1)")
    grid = [(e, by_entry.get(e["entry_id"], {"sessions": {}, "final": None, "row": None})) for e in query(
        ENTRY_SQL + f" WHERE {who} OR en.id IN (SELECT entry_id FROM results WHERE event_id = :e)" + ENTRY_ORDER,
        {"e": event_id})]
    setups = [(s["id"], f"{s['name']} — {commands.spec_label(s)}")
              for s in query(SETUP_SQL + " ORDER BY s.track_id IS NOT ?, t.name, s.track_seq", (event["track_id"],))]
    return render_template("race_detail.html", event=event, results=results, sessions=sessions, main=main,
                           scored=scored, separate=separate, by_session=by_session, runs=runs, grid=grid, setups=setups,
                           roster=roster, on_list=on_list, candidates=candidates, show_roster=request.args.get("roster") == "1",
                           weekend=weekend(event_id), days=event_days(event), kinds=db.SESSION_KINDS,
                           classifications=list(db.CLASSIFICATIONS), classes=db.CLASSES,
                           target=target["target_lap"] if target else None, enter=request.args.get("enter") == "1")


@app.post("/races/<int:event_id>/results")
def race_results_save(event_id):
    """Save the whole results grid: every scored session for every driver. Leaving a session blank clears it."""
    event = query("SELECT * FROM events WHERE id = ?", (event_id,), one=True) or abort(404)
    sessions = event_sessions(event, query("SELECT session FROM results WHERE event_id = ?", (event_id,)))
    main = main_session(sessions)
    conn = db.get_db()
    form = request.form
    saved = cleared = 0
    try:
        for entry_id in form.getlist("entry_id", type=int):
            entry = query("SELECT * FROM entries WHERE id = ?", (entry_id,), one=True)
            if entry is None:
                continue
            get = lambda name: (form.get(f"{name}_{entry_id}") or "").strip()
            num = lambda name, cast=int: cast(float(get(name))) if get(name) else None
            kart_class = get("class") if get("class") in db.CLASSES else entry["kart_class"]
            shared = dict(setup_id=num("setup"), kart_class=kart_class, kart_number=get("kart") or entry["number"])
            entered = {}
            for i, session in enumerate(sessions):
                row = dict(finish_pos=num(f"pos{i}"), points=num(f"pts{i}", float), best_lap=parse_lap(get(f"lap{i}")),
                           main=is_main(event, session, sessions),
                           **classify(get(f"cls{i}"), num(f"penpos{i}"), get(f"penwhy{i}")), **shared)
                if session == main:
                    row["qual_pos"] = num("qual")
                entered[session] = row
            for session, row in entered.items():
                existing = query("SELECT id FROM results WHERE event_id = ? AND entry_id = ? AND session = ?",
                                 (event_id, entry_id, session), one=True)
                filled = any(row.get(k) is not None for k in ("qual_pos", "finish_pos", "points", "best_lap", "classification"))
                if not filled:
                    if existing:
                        conn.execute("DELETE FROM results WHERE id = ?", (existing["id"],))
                        cleared += 1
                    continue
                if existing:
                    conn.execute(f"UPDATE results SET {', '.join(f'{k} = ?' for k in row)} WHERE id = ?",
                                 (*row.values(), existing["id"]))
                else:
                    db.insert(conn, "results", event_id=event_id, driver_id=entry["driver_id"], entry_id=entry_id,
                              session=session, **row)
                saved += 1
    except ValueError as exc:
        conn.rollback()
        flash(f"Nothing saved: {exc}", "danger")
        return redirect(url_for("race_detail", event_id=event_id, enter=1))
    conn.commit()
    flash(f"Saved {saved} result{'s' if saved != 1 else ''}" + (f", cleared {cleared}" if cleared else ""), "success")
    return redirect(url_for("race_detail", event_id=event_id))


def series_classes(series, season):
    return [r[0] for r in query(f"SELECT DISTINCT r.kart_class FROM results r JOIN events e ON e.id = r.event_id "
                                f"WHERE e.series = ? AND {SEASON_SQL} = ? AND e.status = 'Completed' "
                                "AND r.kart_class IS NOT NULL ORDER BY r.kart_class", (series, str(season)))]


def series_standings(series, season, kart_class=None):
    """Standings for one series over its completed rounds.

    With points recorded ('points' mode): heat + final points per round, with the series' drop rule. Missed rounds
    score 0 and are dropped first; ties drop the earliest round.
    With finishes only ('finish' mode): each round's final finish (or the last session run), ranked by average finish.
    Official championship positions, when entered, set the order in either mode. Each class is its own table,
    and each season: a series that runs every year starts again."""
    rounds = query(f"SELECT e.id, e.name, e.start_date FROM events e WHERE e.series = ? AND {SEASON_SQL} = ? "
                   "AND e.status = 'Completed' ORDER BY e.start_date", (series, str(season)))
    rule = query("SELECT * FROM series WHERE name = ?", (series,), one=True)
    rows = query(RESULT_SQL + f" WHERE e.series = ? AND {SEASON_SQL} = ? AND e.status = 'Completed'"
                 + (" AND res.kart_class = ?" if kart_class else ""),
                 (series, str(season), kart_class) if kart_class else (series, str(season)))
    mode = "points" if any((r["points"] or 0) > 0 for r in rows) else "finish"
    # Club series run two heats per round; show them side by side.
    lists = {tuple(session_list(r["event_sessions"])) for r in rows}
    split = list(lists.pop()) if len(lists) == 1 and list(next(iter(lists))) == ["Heat 1", "Heat 2"] else None
    best_of = min(rule["best_of"], len(rounds)) if mode == "points" and rule and rule["best_of"] else len(rounds)

    per_driver = OrderedDict()
    for r in rows:
        per_driver.setdefault(r["driver_id"], []).append(r)
    drivers = {d["id"]: d for d in query("SELECT id, name, active FROM drivers")}
    official = {r["driver_id"]: r for r in query("SELECT * FROM series_positions WHERE series = ? AND season = ? AND "
                                                  "(kart_class IS NULL OR kart_class IS ?)", (series, season, kart_class))}
    for driver_id in official:
        per_driver.setdefault(driver_id, [])  # placed in the championship even with no rounds entered here

    table = []
    for driver_id, driver_rows in per_driver.items():
        by_event = {e["row"]["event_id"]: e for e in by_round(driver_rows, "event_id")}
        entries = [by_event.get(e["id"]) for e in rounds]  # None = missed
        row = dict(driver=drivers[driver_id], official=official.get(driver_id), dropped=set(),
                   raced=sum(1 for x in entries if x),
                   # the number run in this series (e.g. #507 in LO206), else their usual one
                   number=driver_rows[-1]["driver_number"] if driver_rows else (query(
                       "SELECT number FROM entries WHERE driver_id = ? ORDER BY kart_class IS NOT ?, id LIMIT 1",
                       (driver_id, kart_class), one=True) or {"number": "?"})["number"])
        # Average of each round's final finish (or the last session run, e.g. a prefinal).
        finishes = [x["row"]["finish_pos"] for x in entries if x and x["row"]["finish_pos"]]
        row["average"] = sum(finishes) / len(finishes) if finishes else None
        # What each round cell shows: the final finish (or the last session run), with that round's points.
        row["finish_cells"] = [dict(pos=x["row"]["finish_pos"], dnf=x["row"]["dnf"], notes=x["row"]["notes"],
                                    classification=x["row"]["classification"],
                                    penalty_positions=x["row"]["penalty_positions"],
                                    penalty_reason=x["row"]["penalty_reason"],
                                    session=x["row"]["session"],
                                    main=x["row"]["main"], points=x["points"] if x["scored"] else None) if x else None
                               for x in entries]
        # Club series show both heats per round: [(heat 1 row, heat 2 row)] (None = didn't run it).
        if split:
            row["heat_cells"] = [tuple(x["sessions"].get(s) if x else None for s in split) for x in entries]
            row["heat_averages"] = []
            for i, s in enumerate(split):
                pos = [c[i]["finish_pos"] for c in row["heat_cells"] if c[i] and c[i]["finish_pos"]]
                row["heat_averages"].append(sum(pos) / len(pos) if pos else None)
        if mode == "points":
            cells = [x["points"] if x else None for x in entries]
            if row["raced"]:
                ranked = sorted(range(len(cells)), key=lambda i: (cells[i] or 0, i))
                row["dropped"] = set(ranked[:len(cells) - best_of])
            row.update(cells=cells, total=sum(c or 0 for c in cells),
                       counted=sum(c or 0 for i, c in enumerate(cells) if i not in row["dropped"]))
        else:
            cells = row["finish_cells"]
            row.update(cells=cells, best=min(finishes) if finishes else None,
                       wins=sum(1 for c in cells if c and c["pos"] == 1 and c["main"]))
        table.append(row)

    def order(r):
        placed = (r["official"] is None, r["official"]["position"] if r["official"] else 0)
        if mode == "points":
            return placed + (-r["counted"], -r["total"], r["driver"]["name"])
        # No points, so no real championship order: most rounds raced first, then average finish.
        return placed + (-r["raced"], r["average"] is None, r["average"] or 0, r["driver"]["name"])
    table.sort(key=order)
    return rounds, table, best_of, rule, mode, split


@app.get("/standings")
def standings():
    # One tab per series and season, newest season first; a series that runs every year gets a tab per year.
    tabs = [(r[0], int(r[1])) for r in query(
        f"SELECT e.series, {SEASON_SQL} AS yr FROM events e JOIN results r ON r.event_id = e.id "
        "WHERE e.status = 'Completed' AND e.series IS NOT NULL AND e.series != '' "
        "GROUP BY e.series, yr ORDER BY yr DESC, MIN(e.start_date)")]
    repeats = {s for s, _ in tabs if sum(1 for x, _ in tabs if x == s) > 1}
    wanted = request.args.get("series")
    season = request.args.get("season", type=int)
    current = next((t for t in tabs if t[0] == wanted and (season is None or t[1] == season)), tabs[0] if tabs else None)
    series, season = current if current else (None, None)
    classes = series_classes(series, season) if current else []
    kart_class = request.args.get("class") if request.args.get("class") in classes else (classes[0] if classes else None)
    rounds, table, best_of, rule, mode, split = (series_standings(series, season, kart_class) if current
                                                 else ([], [], 0, None, "points", None))
    return render_template("standings.html", tabs=tabs, repeats=repeats, current=series, season=season, rounds=rounds,
                           table=table, best_of=best_of, rule=rule, mode=mode, split=split, classes=classes,
                           kart_class=kart_class)


@app.get("/schedule")
def schedule():
    past = request.args.get("past") == "1"
    sql = EVENT_SQL + ("" if past else " WHERE COALESCE(e.end_date, e.start_date) >= ?") + " ORDER BY e.start_date"
    months = OrderedDict()
    for e in query(sql, () if past else (today(),)):
        d = dt.date.fromisoformat(e["start_date"])
        months.setdefault(d.strftime("%B %Y"), []).append((d, e))
    finishes = lambda cond: (f"(SELECT COUNT(*) FROM results res JOIN events e ON e.id = res.event_id WHERE e.track_id = t.id "
                             f"AND res.main = 1 AND res.dnf = 0 AND res.finish_pos {cond})")
    tracks = query(f"""SELECT t.*, best.best_lap,
                             {finishes('= 1')} AS wins, {finishes('<= 3')} AS podiums,
                             (SELECT COUNT(*) FROM events WHERE track_id = t.id) AS event_count
                      FROM tracks t
                      LEFT JOIN (SELECT track_id, MIN(best_lap) AS best_lap FROM ({LAPS_SQL}) GROUP BY track_id) best
                        ON best.track_id = t.id
                      ORDER BY t.name""")
    return render_template("schedule.html", months=months, past=past, tracks=tracks)


@app.get("/setups")
def setups():
    f = {k: request.args.get(k, "") for k in ("q", "used", "rear", "track")}
    where, args = [], []
    if f["track"]:
        where.append("s.track_id = ?"); args.append(f["track"])
    if f["q"]:
        where.append("(s.name LIKE ? OR s.notes LIKE ? OR s.tire LIKE ?)"); args += [f"%{f['q']}%"] * 3
    if f["used"] == "1":
        where.append("EXISTS (SELECT 1 FROM entries WHERE setup_id = s.id)")
    if f["rear"]:
        where.append("s.rear_sprocket = ?"); args.append(f["rear"])
    rows = query(SETUP_SQL + (" WHERE " + " AND ".join(where) if where else "")
                 + SETUP_ORDER, args)
    rears = [r[0] for r in query("SELECT DISTINCT rear_sprocket FROM setups ORDER BY 1")]
    tracks = query("SELECT id, name FROM tracks ORDER BY name")
    return render_template("setups.html", setups=rows, f=f, rears=rears, tracks=tracks)


@app.get("/setups/<int:setup_id>")
def setup_detail(setup_id):
    setup = query(SETUP_SQL + " WHERE s.id = ?", (setup_id,), one=True) or abort(404)
    runs = query(RUN_SQL + " WHERE r.setup_id = ? ORDER BY r.run_date DESC, r.id DESC", (setup_id,))
    drivers = query(ENTRY_SQL + " WHERE en.setup_id = ?" + ENTRY_ORDER, (setup_id,))
    changes = query(CHANGE_SQL + " WHERE c.from_setup_id = ? OR c.to_setup_id = ? ORDER BY c.id DESC LIMIT 15",
                    (setup_id, setup_id))
    return render_template("setup_detail.html", s=setup, runs=runs, drivers=drivers, changes=changes)


COMPARE_ROWS = [
    ("Track", "track_name"), ("Used by", "used_by"), ("Front sprocket", "front_sprocket"), ("Rear sprocket", "rear_sprocket"),
    ("Front track width", "front_width"), ("Rear track width", "rear_width"), ("Tire", "tire"),
    ("PSI", "psi"), ("Axle", "axle"), ("Caster", "caster"),
    ("Camber", "camber"), ("Seat", "seat_position"), ("Front ride height (above/below)", "front_shims_above"),
    ("Weight", "total_weight"), ("Ballast", "ballast"), ("Carb jetting", "carb_jetting"), ("Clutch", "clutch"),
    ("Best lap", "best_lap"), ("Runs", "run_count"),
]


@app.get("/setups/compare")
def setup_compare():
    ids = request.args.getlist("ids", type=int)
    chosen = [s for s in (query(SETUP_SQL + " WHERE s.id = ?", (i,), one=True) for i in ids) if s]
    table = []
    for label, key in COMPARE_ROWS:
        if key == "best_lap":
            vals = [fmt_lap(s["best_lap"]) for s in chosen]
        elif key == "front_shims_above":
            vals = [commands.shims(s[key]) for s in chosen]
        elif key == "used_by":
            vals = [", ".join("#" + n for n in s["used_by"].split(",")) if s["used_by"] else "—" for s in chosen]
        else:
            vals = [fmt_num(s[key]) if s[key] not in (None, "") else "—" for s in chosen]
        table.append((label, vals, len(set(map(str, vals))) > 1))
    options = query(SETUP_SQL + SETUP_ORDER)
    return render_template("setup_compare.html", chosen=chosen, table=table, options=options, ids=ids)


TIMES_VIEWS = {"races": "By race", "bests": "Personal bests", "log": "All runs"}
SESSION_ORDER = {s: i for i, s in enumerate(db.SESSION_KINDS)}


def page_of(items, per_page):
    """(this page's items, page number, number of pages) from ?page=."""
    pages = max(1, -(-len(items) // per_page))
    page = min(max(request.args.get("page", 1, type=int), 1), pages)
    return items[(page - 1) * per_page: page * per_page], page, pages


@app.get("/times")
def times():
    """Lap times three ways, so nothing turns into one endless list: a card per race weekend (sessions side by side),
    a driver × track grid of personal bests, or the paged run log."""
    view = request.args.get("view") if request.args.get("view") in TIMES_VIEWS else "races"
    f = {k: request.args.get(k, "") for k in ("driver", "kart_class", "track", "session", "since")}
    where, args = [], []
    for key, col in (("driver", "d.id"), ("kart_class", "COALESCE(r.kart_class, en.kart_class, k.kart_class)"),
                     ("track", "r.track_id"), ("session", "r.session")):
        if f[key]:
            where.append(f"{col} = ?"); args.append(f[key])
    if f["since"]:
        where.append("r.run_date >= ?"); args.append(f["since"])
    runs = query(RUN_SQL + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY r.run_date DESC, r.id DESC", args)
    best = {}  # (driver, track, class) -> personal best lap
    for r in runs:
        key = (r["driver_id"], r["track_id"], r["kart_class"])
        if r["best_lap"] is not None and r["best_lap"] < best.get(key, float("inf")):
            best[key] = r["best_lap"]
    ctx = dict(view=view, views=TIMES_VIEWS, f=f, best=best, sessions=db.SESSIONS, total=len(runs),
               drivers=query("SELECT id, name FROM drivers ORDER BY active DESC, name"),
               tracks=query("SELECT id, name FROM tracks ORDER BY name"))

    if view == "races":
        # One card per race weekend (or track day): a row per driver/class, a column per session.
        formats = {}
        for s in query("SELECT event_id, name FROM weekend_sessions ORDER BY day, seq"):
            formats.setdefault(s["event_id"], {}).setdefault(s["name"], len(formats.get(s["event_id"], {})))
        groups = OrderedDict()
        for r in runs:
            groups.setdefault(r["event_id"] or f"{r['track_id']}:{r['run_date']}", []).append(r)
        cards = []
        for rs in groups.values():
            order = formats.get(rs[0]["event_id"], {})
            names = sorted({r["session"] or "Run" for r in rs},
                           key=lambda s: (order.get(s, 99), SESSION_ORDER.get(db.session_kind(s), 99), s))
            rows = OrderedDict()
            for r in rs:
                row = rows.setdefault((r["driver_id"], r["kart_class"]), {"run": r, "cells": {}, "best": None})
                cell = row["cells"].get(r["session"] or "Run")
                if r["best_lap"] is not None and (cell is None or r["best_lap"] < cell["best_lap"]):
                    row["cells"][r["session"] or "Run"] = r
                if r["best_lap"] is not None and (row["best"] is None or r["best_lap"] < row["best"]):
                    row["best"] = r["best_lap"]
            ordered = sorted(rows.values(), key=lambda x: (x["best"] is None, x["best"] or 0))
            top = ordered[0] if ordered and ordered[0]["best"] is not None else None
            first = rs[-1]
            temps = [r["air_temp"] for r in rs if r["air_temp"] is not None]
            cards.append(dict(title=first["event_name"] or f"{first['track_name'] or 'Track'} day", event_id=first["event_id"],
                              track=first["track_name"], target=first["target_lap"],
                              first=min(r["run_date"] for r in rs), last=max(r["run_date"] for r in rs),
                              sessions=names, rows=ordered, top=top, runs=len(rs),
                              air=(min(temps), max(temps)) if temps else None))
        ctx["cards"], ctx["page"], ctx["pages"] = page_of(cards, 6)
    elif view == "bests":
        # Driver × track grid of personal bests; the fastest RMS lap at each track (per class) is gold.
        cols = OrderedDict()
        for r in sorted(runs, key=lambda r: r["track_name"] or ""):
            if r["track_id"]:
                cols.setdefault(r["track_id"], {"name": r["track_name"], "target": r["target_lap"]})
        rows = OrderedDict()
        for (driver_id, track_id, kart_class), lap in best.items():
            if track_id is None:
                continue
            run = next(r for r in runs if r["driver_id"] == driver_id and r["kart_class"] == kart_class)
            rows.setdefault((driver_id, kart_class), {"run": run, "laps": {}})["laps"][track_id] = lap
        fastest = {}
        for (driver_id, kart_class), row in rows.items():
            for track_id, lap in row["laps"].items():
                if lap < fastest.get((track_id, kart_class), float("inf")):
                    fastest[(track_id, kart_class)] = lap
        ctx.update(columns=cols, rows=sorted(rows.values(), key=lambda r: (r["run"]["kart_class"], r["run"]["driver_name"])),
                   fastest=fastest)
    else:
        ctx["runs"], ctx["page"], ctx["pages"] = page_of(runs, 40)
    return render_template("times.html", **ctx)


db.init_db()

if __name__ == "__main__":
    # Local development only; production runs under waitress (see README).
    app.run(debug=not auth.PRODUCTION, port=int(os.getenv("PORT", "5000")))
