"""Fill an empty database with demo data so every page has something to show.

    python seed.py          # only seeds when there are no drivers yet
    python seed.py --reset  # wipes the database first
"""
import datetime as dt
import os
import random
import sys

import db


def main():
    if "--reset" in sys.argv and os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db.init_db()
    conn = db.connect()
    if conn.execute("SELECT COUNT(*) FROM drivers").fetchone()[0]:
        print("Database already has drivers; use --reset to start over.")
        return

    rng = random.Random(206)
    ts = db.now()
    today = dt.date.today()

    def insert(table, **values):
        values.setdefault("created_at", ts)
        cur = conn.execute(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                           tuple(values.values()))
        return cur.lastrowid

    drivers = [("Lucy Park", "84", "Ignite Sr", "Margay Ignite K3"),
               ("Sam Ortiz", "22", "LO206", "Margay Brava 4.10"),
               ("Jordan Reyes", "8", "LO206", "OTK Tony Kart Racer"),
               ("Casey Nguyen", "41", "Ignite Sr", "Margay Ignite K3"),
               ("Riley Brooks", "17", "LO206", "Kart Republic KR3")]
    karts = []
    for name, number, kart_class, chassis in drivers:
        did = insert("drivers", name=name, number=number, phone=f"314-555-01{number:0>2}",
                     email=f"{name.split()[0].lower()}@example.com")
        engine = "Briggs LO206" if kart_class == "LO206" else "Margay Ignite 212"
        kid = insert("karts", driver_id=did, name=f"#{number} {chassis.split()[0]}", kart_class=kart_class,
                     chassis=chassis, engine=engine, engine_number=f"S{rng.randint(10000, 99999)}")
        tires = [insert("tire_sets", kart_id=kid, name=f"Hoosier R80 — set {s}", runs=0) for s in "AB"]
        karts.append((did, kid, kart_class, tires))

    tracks = ["Gateway Kartplex", "Norway Motorsports Park", "Badger Kart Club", "New Castle Motorsports Park"]
    events = []
    for offset, name, kind, track in [
        (-63, "Gateway Summer Series Rd 4", "Race", tracks[0]),
        (-42, "Norway Open Practice", "Practice day", tracks[1]),
        (-35, "Norway Regional Rd 5", "Race", tracks[1]),
        (-14, "Gateway Summer Series Finale", "Race", tracks[0]),
        (-6, "Gearing test — 17/60 vs 17/62", "Test day", tracks[0]),
        (9, "Badger Fall Classic", "Race", tracks[2]),
        (23, "Gateway Turkey Bowl Practice", "Practice day", tracks[0]),
        (37, "New Castle Winter Warm-up", "Race", tracks[3]),
        (65, "Season Opener Test", "Test day", tracks[0]),
    ]:
        start = today + dt.timedelta(days=offset)
        end = start + dt.timedelta(days=1) if kind == "Race" else None
        status = "Completed" if offset < 0 else "Scheduled"
        eid = insert("events", name=name, event_type=kind, track=track, series="RMS Club" if kind == "Race" else None,
                     start_date=start.isoformat(), end_date=end and end.isoformat(), status=status)
        events.append((eid, start, kind, track, offset))

    base_lap = {tracks[0]: 43.4, tracks[1]: 52.1, tracks[2]: 48.6, tracks[3]: 61.8}
    for did, kid, kart_class, tires in karts:
        for track, rear in [(tracks[0], 60), (tracks[0], 62), (tracks[1], 61)]:
            sid = insert("setups", kart_id=kid, name=f"{track.split()[0]} {17}/{rear}", track=track,
                         front_sprocket=17, rear_sprocket=rear, front_width=rng.choice([1.25, 1.5, 1.75]),
                         rear_width=0.5, tire="Hoosier R80", front_psi=rng.choice([10, 11, 12]),
                         rear_psi=rng.choice([11, 12, 13]), axle="Medium" if kart_class == "LO206" else "Hard",
                         caster="Neutral", camber="0°", seat_position="Std", ride_height="Mid",
                         total_weight=360 if kart_class == "LO206" else 380, ballast="10 lb left seat",
                         carb_jetting="Stock" if kart_class == "LO206" else "#110", clutch="Bully 4,800 rpm",
                         updated_at=ts)
            for eid, start, kind, ev_track, offset in events:
                if ev_track != track or offset > 0:
                    continue
                for session in (["Practice", "Qualifying", "Final"] if kind == "Race" else ["Practice"]):
                    tire = tires[0] if offset < -30 else tires[1]
                    conn.execute("UPDATE tire_sets SET runs = runs + 1 WHERE id = ?", (tire,))
                    tire_runs = conn.execute("SELECT runs FROM tire_sets WHERE id = ?", (tire,)).fetchone()[0]
                    lap = base_lap[track] + rng.uniform(-0.25, 0.6) + (0.15 if rear == 62 else 0) - (0.1 if session == "Final" else 0)
                    insert("runs", setup_id=sid, event_id=eid, run_date=start.isoformat(), session=session,
                           best_lap=round(lap, 3), laps=rng.choice([8, 10, 12, 15]), air_temp=rng.randint(58, 88),
                           track_temp=rng.randint(70, 118), track_condition=rng.choice(db.TRACK_CONDITIONS[:3]),
                           tire_set_id=tire, tire_runs=tire_runs)

    for eid, start, kind, track, offset in events:
        if kind != "Race" or offset > 0:
            continue
        grid = karts[:]
        rng.shuffle(grid)
        for pos, (did, kid, kart_class, _t) in enumerate(grid, start=1):
            setup = conn.execute("SELECT id FROM setups WHERE kart_id = ? AND track = ? LIMIT 1", (kid, track)).fetchone()
            insert("results", event_id=eid, driver_id=did, kart_class=kart_class, setup_id=setup and setup[0],
                   qual_pos=rng.randint(1, 12), finish_pos=pos * 2 - 1 if pos < 4 else pos + 3,
                   best_lap=round(base_lap[track] + rng.uniform(-0.3, 0.4), 3), points=max(0, 60 - pos * 6))
    conn.commit()
    print(f"Seeded {db.DB_PATH}")


if __name__ == "__main__":
    main()
