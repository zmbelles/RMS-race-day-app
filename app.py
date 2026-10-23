"""RMS Race Day — admin web app for drivers, races, schedule, garage, setups and lap times."""
import datetime as dt
import os
import sqlite3
from collections import OrderedDict

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for
from markupsafe import Markup, escape

import commands
import db
from db import fmt_lap, fmt_num, now, parse_lap, query

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "rms-dev-only")
app.teardown_appcontext(db.close_db)
app.jinja_env.filters["lap"] = fmt_lap
app.jinja_env.filters["num"] = fmt_num
app.jinja_env.filters["record"] = lambda row: dict(row) if row is not None else {}
app.jinja_env.filters["ratio"] = lambda s: f"{s['rear_sprocket'] / s['front_sprocket']:.3f}"
app.jinja_env.filters["nice_date"] = lambda iso: dt.date.fromisoformat(iso).strftime("%b %#d, %Y" if os.name == "nt" else "%b %-d, %Y") if iso else "—"
app.jinja_env.filters["ago"] = lambda ts: dt.datetime.fromtimestamp(ts).strftime("%b %#d, %#I:%M %p" if os.name == "nt" else "%b %-d, %-I:%M %p")


@app.template_filter("delta")
def delta(lap, target):
    if lap is None or target is None:
        return ""
    d = lap - target
    return Markup(f'<span class="delta {"up" if d > 0 else "down"}">{"+" if d > 0 else "−"}{abs(d):.3f}</span>')


TIRE_LIFE_RUNS = int(os.getenv("TIRE_LIFE_RUNS", "20"))

# ---------------------------------------------------------------- shared SQL

DRIVER_SQL = """
SELECT d.*, s.name AS setup_name, s.front_sprocket, s.rear_sprocket, s.front_width, s.rear_width,
       s.psi, s.tire, k.name AS kart_name, k.chassis, k.engine,
       (SELECT MIN(best_lap) FROM runs WHERE driver_id = d.id) AS best_lap,
       (SELECT COUNT(*) FROM results WHERE driver_id = d.id) AS race_count,
       (SELECT COUNT(*) FROM results WHERE driver_id = d.id AND finish_pos = 1 AND dnf = 0) AS wins,
       (SELECT COUNT(*) FROM results WHERE driver_id = d.id AND finish_pos <= 3 AND dnf = 0) AS podiums
FROM drivers d JOIN setups s ON s.id = d.setup_id JOIN karts k ON k.id = d.kart_id
"""

SETUP_SQL = """
SELECT s.*,
       (SELECT MIN(best_lap) FROM runs WHERE setup_id = s.id) AS best_lap,
       (SELECT COUNT(*) FROM runs WHERE setup_id = s.id) AS run_count,
       (SELECT GROUP_CONCAT(number, ',') FROM drivers WHERE setup_id = s.id) AS used_by
FROM setups s
"""

RUN_SQL = """
SELECT r.*, d.name AS driver_name, d.number AS driver_number, COALESCE(k.kart_class, d.kart_class) AS kart_class,
       k.name AS kart_name, s.name AS setup_name, s.front_sprocket, s.rear_sprocket,
       t.name AS track_name, t.target_lap, ts.name AS tire_name, e.name AS event_name
FROM runs r
JOIN drivers d ON d.id = r.driver_id
LEFT JOIN setups s ON s.id = r.setup_id
LEFT JOIN karts k ON k.id = r.kart_id
LEFT JOIN tracks t ON t.id = r.track_id
LEFT JOIN tire_sets ts ON ts.id = r.tire_set_id
LEFT JOIN events e ON e.id = r.event_id
"""

RESULT_SQL = """
SELECT res.*, d.name AS driver_name, d.number AS driver_number, s.name AS setup_name,
       e.name AS event_name, e.start_date, t.name AS track_name
FROM results res
JOIN drivers d ON d.id = res.driver_id
JOIN events e ON e.id = res.event_id
LEFT JOIN tracks t ON t.id = e.track_id
LEFT JOIN setups s ON s.id = res.setup_id
"""

EVENT_SQL = "SELECT e.*, t.name AS track_name FROM events e LEFT JOIN tracks t ON t.id = e.track_id"

CHANGE_SQL = """
SELECT c.*, d.name AS driver_name, d.number AS driver_number, fs.name AS from_name, ts.name AS to_name,
       (d.setup_id = c.to_setup_id AND c.id = (SELECT MAX(id) FROM setup_changes WHERE driver_id = c.driver_id)) AS undoable
FROM setup_changes c
JOIN drivers d ON d.id = c.driver_id
LEFT JOIN setups fs ON fs.id = c.from_setup_id
LEFT JOIN setups ts ON ts.id = c.to_setup_id
"""


def today():
    return dt.date.today().isoformat()


# ---------------------------------------------------------------- forms
# Each form is rendered as a Bootstrap modal by templates/_forms.html and
# posted to /save/<key>. Options for selects are resolved per request.


def F(name, label, type="text", col=6, **kw):
    return dict(name=name, label=label, type=type, col=col, **kw)


FORMS = {
    "driver": dict(table="drivers", title="Driver", fields=[
        F("name", "Name", required=True, col=8),
        F("number", "Kart #", required=True, col=4),
        F("kart_class", "Class", type="select", options="classes", required=True, col=4),
        F("kart_id", "Kart", type="select", options="karts_new", required=True, col=8),
        F("setup_id", "Current setup", type="select", options="setups", required=True, col=12),
        F("phone", "Phone"),
        F("email", "Email", type="email"),
        F("discord_id", "Discord user ID"),
        F("active", "Active", type="check", default=1),
        F("notes", "Notes", type="textarea", col=12),
    ]),
    "kart": dict(table="karts", title="Kart", fields=[
        F("name", "Kart name", required=True, placeholder="e.g. #24 Margay"),
        F("kart_class", "Class", type="select", options="classes", required=True),
        F("chassis", "Chassis", placeholder="Margay Ignite K3"),
        F("engine", "Engine", placeholder="Margay Ignite 212"),
        F("engine_number", "Engine / seal #"),
        F("notes", "Notes", type="textarea", col=12),
    ]),
    "tire_set": dict(table="tire_sets", title="Tire set", fields=[
        F("kart_id", "Kart", type="select", options="karts", required=True, col=12),
        F("name", "Name", required=True, placeholder="Hoosier R80 — set A"),
        F("runs", "Runs so far", type="number", default=0, col=3),
        F("retired", "Retired", type="check", col=3),
        F("notes", "Notes", type="textarea", col=12),
    ]),
    "track": dict(table="tracks", title="Track", fields=[
        F("name", "Track name", required=True, col=12),
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
        F("start_date", "Start date", type="date", required=True, col=4),
        F("end_date", "End date", type="date", col=4),
        F("status", "Status", type="select", options="event_statuses", required=True, col=4),
        F("notes", "Notes", type="textarea", col=12),
    ]),
    "setup": dict(table="setups", title="Setup", size="xl", fields=[
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
        F("ride_height", "Ride height", col=4),
        F("total_weight", "Total weight (lb)", type="number", step="0.1", col=4, section="Weight & engine"),
        F("ballast", "Ballast", col=8),
        F("carb_jetting", "Carb jetting"),
        F("clutch", "Clutch"),
        F("notes", "Notes", type="textarea", col=12, section="Notes"),
    ]),
    "run": dict(table="runs", title="Run", fields=[
        F("driver_id", "Driver", type="select", options="drivers", required=True, col=4),
        F("setup_id", "Setup", type="select", options="setups", col=8),
        F("event_id", "Event", type="select", options="events", col=6),
        F("track_id", "Track", type="select", options="tracks", col=6),
        F("run_date", "Date", type="date", required=True, default=today, col=4),
        F("session", "Session", type="select", options="sessions", col=4),
        F("kart_id", "Kart", type="select", options="karts", col=4),
        F("best_lap", "Best lap", type="lap", placeholder="45.123 or 1:02.345", col=4),
        F("laps", "Laps", type="number", col=4),
        F("track_condition", "Track condition", type="select", options="track_conditions", col=4),
        F("air_temp", "Air temp (°F)", type="number", step="0.1", col=4),
        F("track_temp", "Track temp (°F)", type="number", step="0.1", col=4),
        F("tire_set_id", "Tire set", type="select", options="tire_sets", filter_by="kart_id", col=4),
        F("notes", "Notes", type="textarea", col=12),
    ]),
    "result": dict(table="results", title="Race result", fields=[
        F("event_id", "Race", type="select", options="races", required=True, col=12),
        F("driver_id", "Driver", type="select", options="drivers", required=True),
        F("kart_class", "Class", type="select", options="classes"),
        F("setup_id", "Setup used", type="select", options="setups", col=12),
        F("qual_pos", "Qualified", type="number", col=3),
        F("finish_pos", "Finished", type="number", col=3),
        F("best_lap", "Best lap", type="lap", col=3),
        F("points", "Points", type="number", step="0.5", col=3),
        F("dnf", "DNF", type="check"),
        F("notes", "Notes", type="textarea", col=12),
    ]),
}
TEXT_OPTIONS = {"classes", "sessions", "track_conditions", "event_types", "event_statuses"}


def form_options():
    """(value, label, filter_key, fill) tuples for every select used by FORMS.
    filter_key pairs a child option with its parent (tire set → kart); fill pre-fills other fields."""
    simple = lambda xs: [(x, x, None, None) for x in xs]
    karts = [(r["id"], f"{r['name']} ({r['kart_class']})", r["id"], None)
             for r in query("SELECT * FROM karts ORDER BY name")]
    return {
        **{name: simple(values) for name, values in (
            ("classes", db.CLASSES), ("sessions", db.SESSIONS), ("track_conditions", db.TRACK_CONDITIONS),
            ("event_types", db.EVENT_TYPES), ("event_statuses", db.EVENT_STATUSES))},
        "drivers": [(r["id"], f"#{r['number']} {r['name']}", None,
                     {"setup_id": r["setup_id"], "kart_id": r["kart_id"], "kart_class": r["kart_class"]})
                    for r in query("SELECT * FROM drivers ORDER BY active DESC, CAST(number AS INTEGER), number")],
        "karts": karts,
        "karts_new": [("new", "＋ New kart for this driver", None, None)] + karts,
        "setups": [(r["id"], f"{r['name']}" + (f"  — used by #{r['used_by'].replace(',', ', #')}" if r["used_by"] else ""), None, None)
                   for r in query(SETUP_SQL + " ORDER BY s.name")],
        "tracks": [(r["id"], r["name"], None, None) for r in query("SELECT * FROM tracks ORDER BY name")],
        "events": [(r["id"], f"{r['start_date']} · {r['name']}", None, {"track_id": r["track_id"]})
                   for r in query("SELECT * FROM events ORDER BY start_date DESC")],
        "races": [(r["id"], f"{r['start_date']} · {r['name']}", None, None)
                  for r in query("SELECT * FROM events WHERE event_type = 'Race' ORDER BY start_date DESC")],
        "tire_sets": [(r["id"], f"{r['name']} ({r['runs']} runs)", r["kart_id"], None)
                      for r in query("SELECT * FROM tire_sets WHERE retired = 0 ORDER BY name")],
    }


@app.context_processor
def inject_globals():
    return dict(FORMS=FORMS, form_options=form_options, today=today(), classes=db.CLASSES,
                tire_life=TIRE_LIFE_RUNS, keywords=commands.KEYWORDS)


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
    nxt = request.form.get("next") or request.referrer
    return redirect(nxt if nxt and nxt.startswith(("/", request.host_url)) else url_for(default))


def adjust_tire(tire_set_id, delta):
    if tire_set_id:
        db.get_db().execute("UPDATE tire_sets SET runs = MAX(0, runs + ?) WHERE id = ?", (delta, tire_set_id))
        row = query("SELECT runs FROM tire_sets WHERE id = ?", (tire_set_id,), one=True)
        return row["runs"] if row else None


@app.post("/save/<key>")
def save(key):
    spec = FORMS.get(key) or abort(404)
    table = spec["table"]
    record_id = request.form.get("id", type=int)
    conn = db.get_db()
    try:
        fields = spec["fields"]
        new_kart = key == "driver" and request.form.get("kart_id") == "new"
        if new_kart:
            fields = [f for f in fields if f["name"] != "kart_id"]
        values = {f["name"]: coerce(f, request.form.get(f["name"])) for f in fields}
        if new_kart:
            values["kart_id"] = db.insert(conn, "karts", name=f"#{values['number']} kart", kart_class=values["kart_class"])
    except ValueError as exc:
        flash(str(exc), "danger")
        return back()

    old = query(f"SELECT * FROM {table} WHERE id = ?", (record_id,), one=True) if record_id else None
    if record_id and old is None:
        abort(404)

    if key == "run":
        # Each run puts one more run on the tire set; tire_runs snapshots the age at that run.
        old_tire = old["tire_set_id"] if old else None
        if values["tire_set_id"] != old_tire:
            adjust_tire(old_tire, -1)
            values["tire_runs"] = adjust_tire(values["tire_set_id"], 1)
        if values["track_id"] is None and values["event_id"]:
            values["track_id"] = query("SELECT track_id FROM events WHERE id = ?", (values["event_id"],), one=True)[0]
    if key == "setup":
        values["updated_at"] = now()
        if not old:
            match = commands.find_setup(conn, values)
            if match:
                flash(Markup(f'That setup already exists: <a href="{url_for("setup_detail", setup_id=match["id"])}">'
                             f'{escape(match["name"])}</a>'), "warning")
                return back()

    try:
        if old:
            cols = ", ".join(f"{c} = ?" for c in values)
            conn.execute(f"UPDATE {table} SET {cols} WHERE id = ?", (*values.values(), record_id))
            flash(f"{spec['title']} updated", "success")
        else:
            if key == "setup":
                values["created_at"] = values["updated_at"]
            db.insert(conn, table, **values)
            flash(f"{spec['title']} added", "success")
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        msg = "That kart number is already taken" if "drivers.number" in str(exc) else f"Couldn't save: {exc}"
        flash(msg, "danger")
    return back()


@app.post("/delete/<key>/<int:record_id>")
def delete(key, record_id):
    spec = FORMS.get(key) or abort(404)
    conn = db.get_db()
    if key == "run":
        row = query("SELECT tire_set_id FROM runs WHERE id = ?", (record_id,), one=True)
        if row:
            adjust_tire(row["tire_set_id"], -1)
    try:
        conn.execute(f"DELETE FROM {spec['table']} WHERE id = ?", (record_id,))
        conn.commit()
        flash(f"{spec['title']} deleted", "secondary")
    except sqlite3.IntegrityError:
        conn.rollback()
        col = "setup_id" if key == "setup" else "kart_id"
        users = query(f"SELECT number FROM drivers WHERE {col} = ?", (record_id,))
        flash(f"Can't delete: driver{'s' if len(users) != 1 else ''} "
              f"{', '.join('#' + u['number'] for u in users)} still use{'s' if len(users) == 1 else ''} it. "
              f"Move them to another {spec['title'].lower()} first.", "danger")
        return back()
    nxt = request.form.get("next")
    return redirect(nxt) if nxt else back()


# ---------------------------------------------------------------- race-day commands

def outcome_html(o):
    who = f"<strong>#{escape(o.driver['number'])} {escape(o.driver['name'])}</strong> · " if o.driver else ""
    changes = " · ".join(f"{escape(label)} {escape(a)} → <strong>{escape(b)}</strong>" for label, a, b in o.changes)
    body = escape(o.message) if not changes else Markup(changes) + Markup(" · ") + escape(o.message.split(" → ", 1)[-1])
    warn = "".join(f'<div class="small"><i class="bi bi-exclamation-triangle"></i> {escape(w)}</div>' for w in o.warnings)
    return Markup(f'<code class="me-2">{escape(o.line)}</code>{who}{body}{warn}')


@app.post("/cmd")
def run_command():
    for o in commands.run(db.get_db(), request.form.get("q", "")):
        cat = "danger" if not o.ok else "warning" if o.warnings else "secondary" if o.kind == "noop" else "success"
        flash(outcome_html(o), cat)
    return back()


@app.get("/cmd/preview")
def preview_command():
    outcomes = commands.run(db.get_db(), request.args.get("q", ""), dry_run=True)
    return jsonify([o.as_dict() for o in outcomes])


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

@app.get("/")
def dashboard():
    stats = dict(
        drivers=query("SELECT COUNT(*) FROM drivers WHERE active = 1", one=True)[0],
        karts=query("SELECT COUNT(*) FROM karts", one=True)[0],
        setups=query("SELECT COUNT(*) FROM setups", one=True)[0],
        runs=query("SELECT COUNT(*) FROM runs", one=True)[0],
    )
    upcoming = query(EVENT_SQL + " WHERE COALESCE(e.end_date, e.start_date) >= ? AND e.status != 'Cancelled' "
                     "ORDER BY e.start_date LIMIT 5", (today(),))
    recent = query(RUN_SQL + " ORDER BY r.run_date DESC, r.id DESC LIMIT 8")
    changes = query(CHANGE_SQL + " ORDER BY c.id DESC LIMIT 6")
    lineup = query(DRIVER_SQL + " WHERE d.active = 1 ORDER BY CAST(d.number AS INTEGER), d.number")
    worn = query("SELECT t.*, k.name AS kart_name FROM tire_sets t JOIN karts k ON k.id = t.kart_id "
                 "WHERE t.retired = 0 AND t.runs >= ? ORDER BY t.runs DESC LIMIT 5", (int(TIRE_LIFE_RUNS * 0.75),))
    next_event = upcoming[0] if upcoming else None
    days_out = (dt.date.fromisoformat(next_event["start_date"]) - dt.date.today()).days if next_event else None
    return render_template("dashboard.html", stats=stats, upcoming=upcoming, recent=recent, records=track_records()[:6],
                           changes=changes, lineup=lineup, worn=worn, next_event=next_event, days_out=days_out)


def track_records(kart_class=None):
    sql = (RUN_SQL + " WHERE r.best_lap IS NOT NULL AND r.track_id IS NOT NULL"
           + (" AND COALESCE(k.kart_class, d.kart_class) = ?" if kart_class else "") + " ORDER BY r.best_lap")
    best = OrderedDict()
    for r in query(sql, (kart_class,) if kart_class else ()):
        best.setdefault((r["track_id"], r["kart_class"]), r)
    return list(best.values())


@app.get("/drivers")
def drivers():
    rows = query(DRIVER_SQL + " ORDER BY d.active DESC, CAST(d.number AS INTEGER), d.number")
    changes = query(CHANGE_SQL + " ORDER BY c.id DESC LIMIT 12")
    return render_template("drivers.html", drivers=rows, changes=changes)


@app.get("/drivers/<int:driver_id>")
def driver_detail(driver_id):
    driver = query(DRIVER_SQL + " WHERE d.id = ?", (driver_id,), one=True) or abort(404)
    setup = query(SETUP_SQL + " WHERE s.id = ?", (driver["setup_id"],), one=True)
    kart = query("SELECT * FROM karts WHERE id = ?", (driver["kart_id"],), one=True)
    tires = query("SELECT * FROM tire_sets WHERE kart_id = ? ORDER BY retired, runs", (driver["kart_id"],))
    changes = query(CHANGE_SQL + " WHERE c.driver_id = ? ORDER BY c.id DESC LIMIT 20", (driver_id,))
    runs = query(RUN_SQL + " WHERE r.driver_id = ? ORDER BY r.run_date DESC, r.id DESC LIMIT 25", (driver_id,))
    results = query(RESULT_SQL + " WHERE res.driver_id = ? ORDER BY e.start_date DESC", (driver_id,))
    return render_template("driver_detail.html", driver=driver, setup=setup, kart=kart, tires=tires,
                           changes=changes, runs=runs, results=results)


@app.get("/races")
def races():
    show = request.args.get("show", "all")
    where, args = "e.event_type = 'Race'", []
    if show == "upcoming":
        where += " AND COALESCE(e.end_date, e.start_date) >= ?"
        args.append(today())
    elif show == "completed":
        where += " AND e.status = 'Completed'"
    rows = query(f"""
        SELECT e.*, t.name AS track_name,
               (SELECT COUNT(*) FROM results WHERE event_id = e.id) AS entries,
               (SELECT MIN(finish_pos) FROM results WHERE event_id = e.id AND dnf = 0) AS best_finish,
               (SELECT d.name FROM results r JOIN drivers d ON d.id = r.driver_id
                 WHERE r.event_id = e.id AND r.dnf = 0 ORDER BY r.finish_pos IS NULL, r.finish_pos LIMIT 1) AS top_driver,
               (SELECT MIN(best_lap) FROM results WHERE event_id = e.id) AS fast_lap
        FROM events e LEFT JOIN tracks t ON t.id = e.track_id WHERE {where} ORDER BY e.start_date DESC""", args)
    return render_template("races.html", races=rows, show=show)


@app.get("/races/<int:event_id>")
def race_detail(event_id):
    event = query(EVENT_SQL + " WHERE e.id = ?", (event_id,), one=True) or abort(404)
    results = query(RESULT_SQL + " WHERE res.event_id = ? ORDER BY res.kart_class, res.dnf, "
                    "res.finish_pos IS NULL, res.finish_pos", (event_id,))
    runs = query(RUN_SQL + " WHERE r.event_id = ? ORDER BY r.run_date, r.session, r.best_lap", (event_id,))
    target = query("SELECT target_lap FROM tracks WHERE id = ?", (event["track_id"],), one=True)
    # One grid row per active driver (plus anyone already entered), prefilled with their result.
    by_driver = {r["driver_id"]: r for r in results}
    grid = [(d, by_driver.get(d["id"])) for d in query(
        "SELECT * FROM drivers WHERE active = 1 OR id IN (SELECT driver_id FROM results WHERE event_id = ?) "
        "ORDER BY CAST(number AS INTEGER), number", (event_id,))]
    setups = query("SELECT id, name FROM setups ORDER BY rear_sprocket, name")
    return render_template("race_detail.html", event=event, results=results, runs=runs, grid=grid, setups=setups,
                           target=target["target_lap"] if target else None, enter=request.args.get("enter") == "1")


@app.post("/races/<int:event_id>/results")
def race_results_save(event_id):
    """Save the whole results grid. A row with no qualifying, finish, lap or DNF clears that driver's result."""
    query("SELECT id FROM events WHERE id = ?", (event_id,), one=True) or abort(404)
    conn = db.get_db()
    form = request.form
    saved = cleared = 0
    try:
        for driver_id in form.getlist("driver_id", type=int):
            get = lambda name: (form.get(f"{name}_{driver_id}") or "").strip()
            num = lambda name, cast=int: cast(float(get(name))) if get(name) else None
            row = dict(qual_pos=num("qual"), finish_pos=num("finish"), best_lap=parse_lap(get("lap")),
                       points=num("points", float), dnf=1 if get("dnf") else 0,
                       setup_id=num("setup"), kart_class=get("class") or None)
            existing = query("SELECT id FROM results WHERE event_id = ? AND driver_id = ?", (event_id, driver_id), one=True)
            if not any(row[k] is not None for k in ("qual_pos", "finish_pos", "best_lap")) and not row["dnf"]:
                if existing:
                    conn.execute("DELETE FROM results WHERE id = ?", (existing["id"],))
                    cleared += 1
                continue
            if existing:
                conn.execute(f"UPDATE results SET {', '.join(f'{k} = ?' for k in row)} WHERE id = ?",
                             (*row.values(), existing["id"]))
            else:
                db.insert(conn, "results", event_id=event_id, driver_id=driver_id, **row)
            saved += 1
    except ValueError as exc:
        conn.rollback()
        flash(f"Nothing saved: {exc}", "danger")
        return redirect(url_for("race_detail", event_id=event_id, enter=1))
    conn.commit()
    flash(f"Saved {saved} result{'s' if saved != 1 else ''}" + (f", cleared {cleared}" if cleared else ""), "success")
    return redirect(url_for("race_detail", event_id=event_id))


@app.get("/schedule")
def schedule():
    past = request.args.get("past") == "1"
    sql = EVENT_SQL + ("" if past else " WHERE COALESCE(e.end_date, e.start_date) >= ?") + " ORDER BY e.start_date"
    months = OrderedDict()
    for e in query(sql, () if past else (today(),)):
        d = dt.date.fromisoformat(e["start_date"])
        months.setdefault(d.strftime("%B %Y"), []).append((d, e))
    tracks = query("""SELECT t.*, (SELECT MIN(best_lap) FROM runs WHERE track_id = t.id) AS best_lap,
                             (SELECT COUNT(*) FROM events WHERE track_id = t.id) AS event_count
                      FROM tracks t ORDER BY t.name""")
    return render_template("schedule.html", months=months, past=past, tracks=tracks)


@app.get("/garage")
def garage():
    karts = query("""
        SELECT k.*, (SELECT GROUP_CONCAT('#' || number || ' ' || name, ', ') FROM drivers WHERE kart_id = k.id) AS drivers,
               (SELECT MIN(best_lap) FROM runs WHERE kart_id = k.id) AS best_lap,
               (SELECT COUNT(*) FROM runs WHERE kart_id = k.id) AS run_count
        FROM karts k ORDER BY k.kart_class, k.name""")
    tires = {}
    for t in query("SELECT * FROM tire_sets ORDER BY retired, runs"):
        tires.setdefault(t["kart_id"], []).append(t)
    return render_template("garage.html", karts=karts, tires=tires)


@app.get("/setups")
def setups():
    f = {k: request.args.get(k, "") for k in ("q", "used", "rear")}
    where, args = [], []
    if f["q"]:
        where.append("(s.name LIKE ? OR s.notes LIKE ? OR s.tire LIKE ?)"); args += [f"%{f['q']}%"] * 3
    if f["used"] == "1":
        where.append("EXISTS (SELECT 1 FROM drivers WHERE setup_id = s.id)")
    if f["rear"]:
        where.append("s.rear_sprocket = ?"); args.append(f["rear"])
    rows = query(SETUP_SQL + (" WHERE " + " AND ".join(where) if where else "")
                 + " ORDER BY (SELECT COUNT(*) FROM drivers WHERE setup_id = s.id) DESC, s.updated_at DESC", args)
    rears = [r[0] for r in query("SELECT DISTINCT rear_sprocket FROM setups ORDER BY 1")]
    return render_template("setups.html", setups=rows, f=f, rears=rears)


@app.get("/setups/<int:setup_id>")
def setup_detail(setup_id):
    setup = query(SETUP_SQL + " WHERE s.id = ?", (setup_id,), one=True) or abort(404)
    runs = query(RUN_SQL + " WHERE r.setup_id = ? ORDER BY r.run_date DESC, r.id DESC", (setup_id,))
    drivers = query("SELECT * FROM drivers WHERE setup_id = ? ORDER BY CAST(number AS INTEGER)", (setup_id,))
    changes = query(CHANGE_SQL + " WHERE c.from_setup_id = ? OR c.to_setup_id = ? ORDER BY c.id DESC LIMIT 15",
                    (setup_id, setup_id))
    return render_template("setup_detail.html", s=setup, runs=runs, drivers=drivers, changes=changes)


COMPARE_ROWS = [
    ("Used by", "used_by"), ("Front sprocket", "front_sprocket"), ("Rear sprocket", "rear_sprocket"),
    ("Gear ratio", "ratio"), ("Front track width", "front_width"), ("Rear track width", "rear_width"), ("Tire", "tire"),
    ("PSI", "psi"), ("Axle", "axle"), ("Caster", "caster"),
    ("Camber", "camber"), ("Seat", "seat_position"), ("Ride height", "ride_height"),
    ("Weight", "total_weight"), ("Ballast", "ballast"), ("Carb jetting", "carb_jetting"), ("Clutch", "clutch"),
    ("Best lap", "best_lap"), ("Runs", "run_count"),
]


@app.get("/setups/compare")
def setup_compare():
    ids = request.args.getlist("ids", type=int)
    chosen = [s for s in (query(SETUP_SQL + " WHERE s.id = ?", (i,), one=True) for i in ids) if s]
    table = []
    for label, key in COMPARE_ROWS:
        if key == "ratio":
            vals = [f"{s['rear_sprocket'] / s['front_sprocket']:.3f}" for s in chosen]
        elif key == "best_lap":
            vals = [fmt_lap(s["best_lap"]) for s in chosen]
        elif key == "used_by":
            vals = [", ".join("#" + n for n in s["used_by"].split(",")) if s["used_by"] else "—" for s in chosen]
        else:
            vals = [fmt_num(s[key]) if s[key] not in (None, "") else "—" for s in chosen]
        table.append((label, vals, len(set(map(str, vals))) > 1))
    options = query(SETUP_SQL + " ORDER BY s.name")
    return render_template("setup_compare.html", chosen=chosen, table=table, options=options, ids=ids)


@app.get("/times")
def times():
    f = {k: request.args.get(k, "") for k in ("driver", "kart_class", "track", "session", "since")}
    where, args = [], []
    for key, col in (("driver", "d.id"), ("kart_class", "COALESCE(k.kart_class, d.kart_class)"),
                     ("track", "r.track_id"), ("session", "r.session")):
        if f[key]:
            where.append(f"{col} = ?"); args.append(f[key])
    if f["since"]:
        where.append("r.run_date >= ?"); args.append(f["since"])
    runs = query(RUN_SQL + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY r.run_date DESC, r.id DESC", args)
    best_by_driver_track = {}
    for r in runs:
        key = (r["driver_id"], r["track_id"])
        if r["best_lap"] is not None and r["best_lap"] < best_by_driver_track.get(key, float("inf")):
            best_by_driver_track[key] = r["best_lap"]
    return render_template("times.html", runs=runs, f=f, records=track_records(f["kart_class"] or None),
                           best=best_by_driver_track, sessions=db.SESSIONS,
                           drivers=query("SELECT id, number, name FROM drivers ORDER BY CAST(number AS INTEGER)"),
                           tracks=query("SELECT id, name FROM tracks ORDER BY name"))


db.init_db()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.getenv("PORT", "5000")))
