"""Fill an empty database with demo data so every page has something to show.

    python seed.py          # only seeds when there are no drivers yet
    python seed.py --reset  # wipes the database first
"""
import datetime as dt
import os
import random
import sys

import db

TRACKS = [  # name, city, state, good lap
    ("Norway Motorsports Park", "Sheridan", "IL", 53.0),
    ("Kart Circuit Autobahn", "Joliet", "IL", 37.6),
    ("Gateway Kartplex", "Madison", "IL", 42.2),
    ("Mid-State Kart Club", "Dawson", "IL", None),
    ("Briggs & Stratton Motorplex", "Elkhart Lake", "WI", None),
]
DRIVERS = [("Zachary Belles", "24"), ("Leonardo Rojas", "9"), ("Jesus Garza", "13"),
           ("Colin Cox", "88"), ("Logan Matusik", "517")]
SETUPS = [  # name, front/rear sprocket, FTW, RTW, front/rear psi
    ("Baseline", 17, 60, 1.5, 0.5, 30, 30),
    ("Tight track", 17, 62, 1.25, 0.5, 31, 31),
    ("Fast track", 17, 59, 1.5, 0.75, 30, 32),
    ("Hot day", 17, 61, 1.75, 0.5, 28, 29),
    ("Cold & green", 17, 60, 1.25, 0.25, 34, 34),
    ("Wet", 17, 58, 1.0, 0.0, 36, 36),
]
DRIVER_SETUP = {"24": 0, "9": 0, "13": 1, "88": 2, "517": 3}  # index into SETUPS; 24 and 9 share one


def main():
    if "--reset" in sys.argv and os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db.init_db()
    conn = db.connect()
    if conn.execute("SELECT COUNT(*) FROM drivers").fetchone()[0]:
        print("Database already has drivers; use --reset to start over.")
        return

    rng = random.Random(24)
    today = dt.date.today()
    ins = lambda table, **v: db.insert(conn, table, **v)

    tracks = [ins("tracks", name=n, city=c, state=s, target_lap=t) for n, c, s, t in TRACKS]
    targets = {tid: t[3] for tid, t in zip(tracks, TRACKS)}

    ts = db.now()
    setups = [ins("setups", name=name, front_sprocket=fs, rear_sprocket=rs, front_width=ftw, rear_width=rtw,
                  tire="Hoosier R80", front_psi=fp, rear_psi=rp, axle="Hard", caster="Neutral", camber="0°",
                  seat_position="Std", ride_height="Mid", total_weight=380, carb_jetting="#110",
                  clutch="Margay 4,500 rpm", created_at=ts, updated_at=ts)
              for name, fs, rs, ftw, rtw, fp, rp in SETUPS]

    drivers = []
    for name, number in DRIVERS:
        kid = ins("karts", name=f"#{number} Margay", kart_class="Ignite Sr", chassis="Margay Ignite K3",
                  engine="Margay Ignite 212", engine_number=f"S{rng.randint(10000, 99999)}")
        tires = [ins("tire_sets", kart_id=kid, name=f"Hoosier R80 — set {s}") for s in "AB"]
        did = ins("drivers", name=name, number=number, kart_class="Ignite Sr", kart_id=kid,
                  setup_id=setups[DRIVER_SETUP[number]], email=f"{name.split()[0].lower()}@example.com")
        drivers.append((did, kid, setups[DRIVER_SETUP[number]], tires))
    ins("karts", name="Spare Ignite", kart_class="Ignite Sr", chassis="Margay Ignite K3",
        engine="Margay Ignite 212", notes="Arrive-and-drive spare")

    norway, autobahn, gateway, midstate, briggs = tracks
    events = []
    for offset, name, kind, track in [
        (-70, "Norway Club Rd 6", "Race", norway),
        (-49, "Autobahn Open Practice", "Practice day", autobahn),
        (-42, "Autobahn Sprint Rd 7", "Race", autobahn),
        (-21, "Gateway Fall Series Rd 1", "Race", gateway),
        (-7, "Gearing test 17/60 vs 17/62", "Test day", gateway),
        (9, "Mid-State Halloween Race", "Race", midstate),
        (23, "Gateway Fall Series Rd 2", "Race", gateway),
        (51, "Briggs Winter Warm-up", "Race", briggs),
    ]:
        start = today + dt.timedelta(days=offset)
        end = start + dt.timedelta(days=1) if kind == "Race" else None
        eid = ins("events", name=name, event_type=kind, track_id=track, series="RMS Club" if kind == "Race" else None,
                  start_date=start.isoformat(), end_date=end and end.isoformat(),
                  status="Completed" if offset < 0 else "Scheduled")
        events.append((eid, start, kind, track, offset))

    for eid, start, kind, track, offset in events:
        if offset > 0:
            continue
        sessions = ["Practice", "Qualifying", "Heat", "Final"] if kind == "Race" else ["Practice", "Practice"]
        for did, kid, sid, tires in drivers:
            skill = rng.uniform(-0.15, 0.6)
            tire = tires[0] if offset < -30 else tires[1]
            for session in sessions:
                conn.execute("UPDATE tire_sets SET runs = runs + 1 WHERE id = ?", (tire,))
                tire_runs = conn.execute("SELECT runs FROM tire_sets WHERE id = ?", (tire,)).fetchone()[0]
                lap = targets[track] + skill + rng.uniform(0, 0.45) - (0.12 if session == "Final" else 0)
                ins("runs", driver_id=did, setup_id=sid, kart_id=kid, track_id=track, event_id=eid,
                    run_date=start.isoformat(), session=session, best_lap=round(lap, 3), laps=rng.choice([8, 10, 12, 15]),
                    air_temp=rng.randint(52, 84), track_temp=rng.randint(60, 112),
                    track_condition=rng.choice(db.TRACK_CONDITIONS[:3]), tire_set_id=tire, tire_runs=tire_runs)
        if kind == "Race":
            finishes = rng.sample(range(1, 19), len(drivers))
            for (did, kid, sid, _t), pos in zip(drivers, finishes):
                ins("results", event_id=eid, driver_id=did, kart_class="Ignite Sr", setup_id=sid,
                    qual_pos=max(1, pos + rng.randint(-4, 4)), finish_pos=pos,
                    best_lap=round(targets[track] + rng.uniform(-0.1, 0.5), 3), points=max(0, 50 - (pos - 1) * 3))
    conn.commit()
    print(f"Seeded {db.DB_PATH}")


if __name__ == "__main__":
    main()
