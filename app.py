"""RMS Race Day — admin web app for drivers, races, schedule, garage, setups and lap times."""
import datetime as dt
import os
from collections import OrderedDict

from flask import Flask, abort, flash, redirect, render_template, request, url_for

import db
from db import fmt_lap, now, parse_lap, query

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "rms-dev-only")
app.teardown_appcontext(db.close_db)
app.jinja_env.filters["lap"] = fmt_lap
app.jinja_env.filters["record"] = lambda row: dict(row) if row is not None else {}
app.jinja_env.filters["ratio"] = lambda s: f"{s['rear_sprocket'] / s['front_sprocket']:.3f}"
app.jinja_env.filters["nice_date"] = lambda iso: dt.date.fromisoformat(iso).strftime("%b %-d, %Y" if os.name != "nt" else "%b %#d, %Y") if iso else "—"

TIRE_LIFE_RUNS = int(os.getenv("TIRE_LIFE_RUNS", "20"))

# ---------------------------------------------------------------- shared SQL

SETUP_SQL = """
SELECT s.*, k.name AS kart_name, k.kart_class, k.chassis, k.driver_id,
       d.name AS driver_name, d.number AS driver_number,
       (SELECT MIN(best_lap) FROM runs WHERE setup_id = s.id) AS best_lap,
       (SELECT COUNT(*) FROM runs WHERE setup_id = s.id) AS run_count
FROM setups s JOIN karts k ON k.id = s.kart_id JOIN drivers d ON d.id = k.driver_id
"""

RUN_SQL = """
SELECT r.*, s.name AS setup_name, s.track, s.front_sprocket, s.rear_sprocket, s.kart_id,
       k.kart_class, k.name AS kart_name, d.id AS driver_id, d.name AS driver_name,
       d.number AS driver_number, t.name AS tire_name, e.name AS event_name
FROM runs r
JOIN setups s ON s.id = r.setup_id
JOIN karts k ON k.id = s.kart_id
JOIN drivers d ON d.id = k.driver_id
LEFT JOIN tire_sets t ON t.id = r.tire_set_id
LEFT JOIN events e ON e.id = r.event_id
"""

RESULT_SQL = """
SELECT res.*, d.name AS driver_name, d.number AS driver_number, s.name AS setup_name,
       e.name AS event_name, e.start_date, e.track
FROM results res
JOIN drivers d ON d.id = res.driver_id
JOIN events e ON e.id = res.event_id
LEFT JOIN setups s ON s.id = res.setup_id
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
        F("number", "Kart #", col=4),
        F("phone", "Phone"),
        F("email", "Email", type="email"),
        F("discord_id", "Discord user ID"),
        F("active", "Active", type="check", default=1),
        F("notes", "Notes", type="textarea", col=12),
    ]),
    "kart": dict(table="karts", title="Kart", fields=[
        F("driver_id", "Driver", type="select", options="drivers", required=True),
        F("kart_class", "Class", type="select", options="classes", required=True),
        F("name", "Kart name", required=True, placeholder="e.g. #84 Margay"),
        F("chassis", "Chassis", placeholder="Margay Ignite K3"),
        F("engine", "Engine", placeholder="Briggs LO206"),
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
    "event": dict(table="events", title="Event", fields=[
        F("name", "Event name", required=True, col=8),
        F("event_type", "Type", type="select", options="event_types", required=True, col=4),
        F("track", "Track", list="tracks"),
        F("series", "Series", placeholder="Club, regional, national…"),
        F("start_date", "Start date", type="date", required=True, col=4),
        F("end_date", "End date", type="date", col=4),
        F("status", "Status", type="select", options="event_statuses", required=True, col=4),
        F("notes", "Notes", type="textarea", col=12),
    ]),
    "setup": dict(table="setups", title="Setup", size="xl", fields=[
        F("kart_id", "Kart", type="select", options="karts", required=True, section="Basics"),
        F("name", "Setup name", required=True),
        F("track", "Track", list="tracks"),
        F("conditions", "Conditions", placeholder="Hot & sunny, rubbered in"),
        F("front_sprocket", "Front sprocket (T)", type="number", required=True, col=3, section="Gearing & track width"),
        F("rear_sprocket", "Rear sprocket (T)", type="number", required=True, col=3),
        F("front_width", "Front width (in)", type="number", step="0.01", col=3),
        F("rear_width", "Rear width (in)", type="number", step="0.01", col=3),
        F("tire", "Tire", col=6, section="Tires"),
        F("front_psi", "Front PSI", type="number", step="0.1", col=3),
        F("rear_psi", "Rear PSI", type="number", step="0.1", col=3),
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
        F("setup_id", "Setup", type="select", options="setups", required=True, col=12),
        F("run_date", "Date", type="date", required=True, default=today, col=4),
        F("session", "Session", type="select", options="sessions", col=4),
        F("event_id", "Event", type="select", options="events", col=4),
        F("best_lap", "Best lap", type="lap", placeholder="45.123 or 1:02.345", col=4),
        F("laps", "Laps", type="number", col=4),
        F("track_condition", "Track condition", type="select", options="track_conditions", col=4),
        F("air_temp", "Air temp (°F)", type="number", step="0.1", col=4),
        F("track_temp", "Track temp (°F)", type="number", step="0.1", col=4),
        F("tire_set_id", "Tire set", type="select", options="tire_sets", filter_by="setup_id", col=4),
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


def form_options():
    """(value, label, filter_key) tuples for every select used by FORMS."""
    simple = lambda xs: [(x, x, None) for x in xs]
    return {
        "classes": simple(db.CLASSES),
        "sessions": simple(db.SESSIONS),
        "track_conditions": simple(db.TRACK_CONDITIONS),
        "event_types": simple(db.EVENT_TYPES),
        "event_statuses": simple(db.EVENT_STATUSES),
        "drivers": [(r["id"], f"{r['name']}" + (f" #{r['number']}" if r["number"] else ""), None)
                    for r in query("SELECT * FROM drivers ORDER BY active DESC, name")],
        "karts": [(r["id"], f"{r['driver_name']} — {r['name']} ({r['kart_class']})", None)
                  for r in query("SELECT k.*, d.name AS driver_name FROM karts k JOIN drivers d ON d.id = k.driver_id ORDER BY d.name, k.name")],
        "setups": [(r["id"], f"{r['driver_name']} — {r['name']}" + (f" @ {r['track']}" if r["track"] else ""), r["kart_id"])
                   for r in query(SETUP_SQL + " ORDER BY d.name, s.name")],
        "events": [(r["id"], f"{r['start_date']} · {r['name']}", None)
                   for r in query("SELECT * FROM events ORDER BY start_date DESC")],
        "races": [(r["id"], f"{r['start_date']} · {r['name']}", None)
                  for r in query("SELECT * FROM events WHERE event_type = 'Race' ORDER BY start_date DESC")],
        "tire_sets": [(r["id"], f"{r['name']} ({r['runs']} runs)", r["kart_id"])
                      for r in query("SELECT * FROM tire_sets WHERE retired = 0 ORDER BY name")],
    }


@app.context_processor
def inject_globals():
    tracks = sorted({r[0] for r in query(
        "SELECT track FROM setups WHERE track IS NOT NULL AND track != '' "
        "UNION SELECT track FROM events WHERE track IS NOT NULL AND track != ''")})
    return dict(FORMS=FORMS, form_options=form_options, tracks=tracks, today=today(),
                classes=db.CLASSES, tire_life=TIRE_LIFE_RUNS)


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
    if kind == "select" and field["options"] not in ("classes", "sessions", "track_conditions", "event_types", "event_statuses"):
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
    try:
        values = {f["name"]: coerce(f, request.form.get(f["name"])) for f in spec["fields"]}
    except ValueError as exc:
        flash(str(exc), "danger")
        return back()

    conn = db.get_db()
    old = query(f"SELECT * FROM {table} WHERE id = ?", (record_id,), one=True) if record_id else None
    if record_id and old is None:
        abort(404)

    if key == "run":
        # Each run puts one more run on the tire set; tire_runs snapshots the age at that run.
        old_tire = old["tire_set_id"] if old else None
        if values["tire_set_id"] != old_tire:
            adjust_tire(old_tire, -1)
            values["tire_runs"] = adjust_tire(values["tire_set_id"], 1)
    if key == "setup":
        values["updated_at"] = now()

    if old:
        cols = ", ".join(f"{c} = ?" for c in values)
        conn.execute(f"UPDATE {table} SET {cols} WHERE id = ?", (*values.values(), record_id))
        flash(f"{spec['title']} updated", "success")
    else:
        values["created_at"] = now()
        if key == "setup":
            values["updated_at"] = values["created_at"]
        cols = ", ".join(values)
        conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(values))})", tuple(values.values()))
        flash(f"{spec['title']} added", "success")
    conn.commit()
    return back()


@app.post("/delete/<key>/<int:record_id>")
def delete(key, record_id):
    spec = FORMS.get(key) or abort(404)
    conn = db.get_db()
    if key == "run":
        row = query("SELECT tire_set_id FROM runs WHERE id = ?", (record_id,), one=True)
        if row:
            adjust_tire(row["tire_set_id"], -1)
    conn.execute(f"DELETE FROM {spec['table']} WHERE id = ?", (record_id,))
    conn.commit()
    flash(f"{spec['title']} deleted", "secondary")
    nxt = request.form.get("next")
    return redirect(nxt) if nxt else back()


# ---------------------------------------------------------------- pages

@app.get("/")
def dashboard():
    stats = dict(
        drivers=query("SELECT COUNT(*) FROM drivers WHERE active = 1", one=True)[0],
        karts=query("SELECT COUNT(*) FROM karts", one=True)[0],
        setups=query("SELECT COUNT(*) FROM setups", one=True)[0],
        runs=query("SELECT COUNT(*) FROM runs", one=True)[0],
    )
    upcoming = query("SELECT * FROM events WHERE COALESCE(end_date, start_date) >= ? AND status != 'Cancelled' "
                     "ORDER BY start_date LIMIT 5", (today(),))
    recent = query(RUN_SQL + " ORDER BY r.run_date DESC, r.id DESC LIMIT 8")
    records = track_records()[:6]
    results = query(RESULT_SQL + " WHERE res.finish_pos IS NOT NULL ORDER BY e.start_date DESC, res.finish_pos LIMIT 6")
    worn = query("SELECT t.*, k.name AS kart_name, d.name AS driver_name FROM tire_sets t "
                 "JOIN karts k ON k.id = t.kart_id JOIN drivers d ON d.id = k.driver_id "
                 "WHERE t.retired = 0 AND t.runs >= ? ORDER BY t.runs DESC LIMIT 5", (int(TIRE_LIFE_RUNS * 0.75),))
    next_event = upcoming[0] if upcoming else None
    days_out = (dt.date.fromisoformat(next_event["start_date"]) - dt.date.today()).days if next_event else None
    return render_template("dashboard.html", stats=stats, upcoming=upcoming, recent=recent, records=records,
                           results=results, worn=worn, next_event=next_event, days_out=days_out)


def track_records(kart_class=None):
    sql = (RUN_SQL + " WHERE r.best_lap IS NOT NULL" + (" AND k.kart_class = ?" if kart_class else "")
           + " ORDER BY r.best_lap")
    best = OrderedDict()
    for r in query(sql, (kart_class,) if kart_class else ()):
        best.setdefault((r["track"] or "Unknown track", r["kart_class"]), r)
    return list(best.values())


@app.get("/drivers")
def drivers():
    rows = query("""
        SELECT d.*,
               (SELECT COUNT(*) FROM karts WHERE driver_id = d.id) AS kart_count,
               (SELECT GROUP_CONCAT(DISTINCT kart_class) FROM karts WHERE driver_id = d.id) AS classes,
               (SELECT COUNT(*) FROM setups s JOIN karts k ON k.id = s.kart_id WHERE k.driver_id = d.id) AS setup_count,
               (SELECT COUNT(*) FROM results WHERE driver_id = d.id) AS race_count,
               (SELECT COUNT(*) FROM results WHERE driver_id = d.id AND finish_pos = 1) AS wins,
               (SELECT COUNT(*) FROM results WHERE driver_id = d.id AND finish_pos <= 3) AS podiums
        FROM drivers d ORDER BY d.active DESC, d.name""")
    return render_template("drivers.html", drivers=rows)


@app.get("/drivers/<int:driver_id>")
def driver_detail(driver_id):
    driver = query("SELECT * FROM drivers WHERE id = ?", (driver_id,), one=True) or abort(404)
    karts = query("SELECT * FROM karts WHERE driver_id = ? ORDER BY name", (driver_id,))
    setups = query(SETUP_SQL + " WHERE k.driver_id = ? ORDER BY s.updated_at DESC", (driver_id,))
    runs = query(RUN_SQL + " WHERE d.id = ? ORDER BY r.run_date DESC, r.id DESC LIMIT 25", (driver_id,))
    results = query(RESULT_SQL + " WHERE res.driver_id = ? ORDER BY e.start_date DESC", (driver_id,))
    return render_template("driver_detail.html", driver=driver, karts=karts, setups=setups, runs=runs, results=results)


@app.get("/races")
def races():
    show = request.args.get("show", "all")
    where = "e.event_type = 'Race'"
    if show == "upcoming":
        where += f" AND COALESCE(e.end_date, e.start_date) >= '{today()}'"
    elif show == "completed":
        where += " AND e.status = 'Completed'"
    rows = query(f"""
        SELECT e.*,
               (SELECT COUNT(*) FROM results WHERE event_id = e.id) AS entries,
               (SELECT MIN(finish_pos) FROM results WHERE event_id = e.id) AS best_finish,
               (SELECT d.name FROM results r JOIN drivers d ON d.id = r.driver_id
                 WHERE r.event_id = e.id ORDER BY r.finish_pos IS NULL, r.finish_pos LIMIT 1) AS top_driver,
               (SELECT MIN(best_lap) FROM results WHERE event_id = e.id) AS fast_lap
        FROM events e WHERE {where} ORDER BY e.start_date DESC""")
    return render_template("races.html", races=rows, show=show)


@app.get("/races/<int:event_id>")
def race_detail(event_id):
    event = query("SELECT * FROM events WHERE id = ?", (event_id,), one=True) or abort(404)
    results = query(RESULT_SQL + " WHERE res.event_id = ? ORDER BY res.kart_class, res.dnf, "
                    "res.finish_pos IS NULL, res.finish_pos", (event_id,))
    runs = query(RUN_SQL + " WHERE r.event_id = ? ORDER BY r.run_date, r.session, r.best_lap", (event_id,))
    return render_template("race_detail.html", event=event, results=results, runs=runs)


@app.get("/schedule")
def schedule():
    past = request.args.get("past") == "1"
    sql = "SELECT * FROM events " + ("" if past else "WHERE COALESCE(end_date, start_date) >= ? ") + "ORDER BY start_date"
    rows = query(sql, () if past else (today(),))
    months = OrderedDict()
    for e in rows:
        d = dt.date.fromisoformat(e["start_date"])
        months.setdefault(d.strftime("%B %Y"), []).append((d, e))
    return render_template("schedule.html", months=months, past=past)


@app.get("/garage")
def garage():
    karts = query("""
        SELECT k.*, d.name AS driver_name, d.number AS driver_number,
               (SELECT COUNT(*) FROM setups WHERE kart_id = k.id) AS setup_count,
               (SELECT MIN(r.best_lap) FROM runs r JOIN setups s ON s.id = r.setup_id WHERE s.kart_id = k.id) AS best_lap
        FROM karts k JOIN drivers d ON d.id = k.driver_id ORDER BY d.name, k.name""")
    tires = {}
    for t in query("SELECT * FROM tire_sets ORDER BY retired, runs"):
        tires.setdefault(t["kart_id"], []).append(t)
    by_driver = OrderedDict()
    for k in karts:
        by_driver.setdefault((k["driver_id"], k["driver_name"], k["driver_number"]), []).append(k)
    return render_template("garage.html", by_driver=by_driver, tires=tires)


@app.get("/setups")
def setups():
    f = {k: request.args.get(k, "") for k in ("driver", "kart_class", "track", "q")}
    where, args = [], []
    if f["driver"]:
        where.append("k.driver_id = ?"); args.append(f["driver"])
    if f["kart_class"]:
        where.append("k.kart_class = ?"); args.append(f["kart_class"])
    if f["track"]:
        where.append("s.track = ?"); args.append(f["track"])
    if f["q"]:
        where.append("(s.name LIKE ? OR s.notes LIKE ? OR k.chassis LIKE ?)"); args += [f"%{f['q']}%"] * 3
    rows = query(SETUP_SQL + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY s.updated_at DESC", args)
    drivers = query("SELECT id, name FROM drivers ORDER BY name")
    return render_template("setups.html", setups=rows, f=f, drivers=drivers)


@app.get("/setups/<int:setup_id>")
def setup_detail(setup_id):
    setup = query(SETUP_SQL + " WHERE s.id = ?", (setup_id,), one=True) or abort(404)
    runs = query(RUN_SQL + " WHERE r.setup_id = ? ORDER BY r.run_date DESC, r.id DESC", (setup_id,))
    return render_template("setup_detail.html", s=setup, runs=runs)


@app.post("/setups/<int:setup_id>/clone")
def setup_clone(setup_id):
    src = query("SELECT * FROM setups WHERE id = ?", (setup_id,), one=True) or abort(404)
    values = {k: src[k] for k in src.keys() if k != "id"}
    values["name"] = request.form.get("name") or f"{src['name']} (copy)"
    values["created_at"] = values["updated_at"] = now()
    conn = db.get_db()
    cur = conn.execute(f"INSERT INTO setups ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                       tuple(values.values()))
    conn.commit()
    flash(f"Cloned to “{values['name']}”", "success")
    return redirect(url_for("setup_detail", setup_id=cur.lastrowid))


COMPARE_ROWS = [
    ("Driver", "driver_name"), ("Kart", "kart_name"), ("Class", "kart_class"), ("Chassis", "chassis"),
    ("Track", "track"), ("Front sprocket", "front_sprocket"), ("Rear sprocket", "rear_sprocket"),
    ("Gear ratio", "ratio"), ("Front width", "front_width"), ("Rear width", "rear_width"), ("Tire", "tire"),
    ("Front PSI", "front_psi"), ("Rear PSI", "rear_psi"), ("Axle", "axle"), ("Caster", "caster"),
    ("Camber", "camber"), ("Seat", "seat_position"), ("Ride height", "ride_height"),
    ("Weight", "total_weight"), ("Ballast", "ballast"), ("Carb jetting", "carb_jetting"), ("Clutch", "clutch"),
    ("Conditions", "conditions"), ("Best lap", "best_lap"), ("Runs", "run_count"),
]


@app.get("/setups/compare")
def setup_compare():
    ids = request.args.getlist("ids", type=int)
    chosen = [query(SETUP_SQL + " WHERE s.id = ?", (i,), one=True) for i in ids]
    chosen = [s for s in chosen if s]
    table = []
    for label, key in COMPARE_ROWS:
        if key == "ratio":
            vals = [f"{s['rear_sprocket'] / s['front_sprocket']:.3f}" for s in chosen]
        elif key == "best_lap":
            vals = [fmt_lap(s["best_lap"]) for s in chosen]
        else:
            vals = ["—" if s[key] in (None, "") else s[key] for s in chosen]
        table.append((label, vals, len(set(map(str, vals))) > 1))
    options = query(SETUP_SQL + " ORDER BY d.name, s.name")
    return render_template("setup_compare.html", chosen=chosen, table=table, options=options, ids=ids)


@app.get("/times")
def times():
    f = {k: request.args.get(k, "") for k in ("driver", "kart_class", "track", "session", "since")}
    where, args = [], []
    for key, col in (("driver", "d.id"), ("kart_class", "k.kart_class"), ("track", "s.track"), ("session", "r.session")):
        if f[key]:
            where.append(f"{col} = ?"); args.append(f[key])
    if f["since"]:
        where.append("r.run_date >= ?"); args.append(f["since"])
    runs = query(RUN_SQL + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY r.run_date DESC, r.id DESC", args)
    best_by_setup = {}
    for r in runs:
        if r["best_lap"] is not None and r["best_lap"] < best_by_setup.get(r["setup_id"], float("inf")):
            best_by_setup[r["setup_id"]] = r["best_lap"]
    drivers = query("SELECT id, name FROM drivers ORDER BY name")
    return render_template("times.html", runs=runs, f=f, drivers=drivers, records=track_records(f["kart_class"] or None),
                           best_by_setup=best_by_setup, sessions=db.SESSIONS)


db.init_db()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.getenv("PORT", "5000")))
