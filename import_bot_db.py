"""Copy setups, runs and tire sets from the Discord bot's kart_setups.db into the web app.

    python import_bot_db.py path/to/kart_setups.db

Each Discord user becomes a driver (named "Discord user 1234" until you rename them),
each class + chassis they used becomes a kart. Non-tire parts are skipped — the web app
doesn't track parts inventory. Safe to re-run: setups already imported are skipped.
"""
import sqlite3
import sys

import db


def main(src_path):
    db.init_db()
    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row
    dst = db.connect()
    ts = db.now()

    def insert(table, **values):
        values.setdefault("created_at", ts)
        return dst.execute(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                           tuple(values.values())).lastrowid

    def driver_for(user_id):
        row = dst.execute("SELECT id FROM drivers WHERE discord_id = ?", (str(user_id),)).fetchone()
        return row[0] if row else insert("drivers", name=f"Discord user {str(user_id)[-4:]}", discord_id=str(user_id))

    def kart_for(driver_id, kart_class, chassis):
        row = dst.execute("SELECT id FROM karts WHERE driver_id = ? AND kart_class = ? AND COALESCE(chassis, '') = ?",
                          (driver_id, kart_class, chassis or "")).fetchone()
        return row[0] if row else insert("karts", driver_id=driver_id, kart_class=kart_class, chassis=chassis,
                                         name=chassis or kart_class)

    setup_map, kart_by_user = {}, {}
    for s in src.execute("SELECT * FROM setups ORDER BY id"):
        driver_id = driver_for(s["user_id"])
        kart_id = kart_for(driver_id, s["kart_class"], s["chassis"])
        kart_by_user.setdefault(s["user_id"], kart_id)
        existing = dst.execute("SELECT id FROM setups WHERE kart_id = ? AND name = ?", (kart_id, s["name"])).fetchone()
        if existing:
            continue
        cols = ["track", "front_sprocket", "rear_sprocket", "front_width", "rear_width", "tire", "front_psi",
                "rear_psi", "axle", "caster", "camber", "conditions", "notes", "seat_position", "ride_height",
                "total_weight", "ballast", "carb_jetting", "clutch"]
        setup_map[s["id"]] = insert("setups", kart_id=kart_id, name=s["name"], created_at=s["created_at"],
                                    updated_at=s["updated_at"], **{c: s[c] for c in cols if c in s.keys()})

    tire_map = {}
    for p in src.execute("SELECT * FROM parts WHERE category = 'Tires'"):
        kart_id = kart_by_user.get(p["user_id"]) or kart_for(driver_for(p["user_id"]), "LO206", None)
        existing = dst.execute("SELECT id FROM tire_sets WHERE kart_id = ? AND name = ?", (kart_id, p["name"])).fetchone()
        if existing:
            tire_map[p["id"]] = existing[0]
            continue
        tire_map[p["id"]] = insert("tire_sets", kart_id=kart_id, name=p["name"], runs=p["runs"],
                                   retired=p["retired"], notes=p["notes"])

    runs = 0
    for r in src.execute("SELECT * FROM runs ORDER BY id"):
        if r["setup_id"] not in setup_map:
            continue
        insert("runs", setup_id=setup_map[r["setup_id"]], run_date=r["run_date"], session=r["session"],
               best_lap=r["best_lap"], laps=r["laps"], air_temp=r["air_temp"], track_temp=r["track_temp"],
               track_condition=r["track_condition"], tire_set_id=tire_map.get(r["tire_set_id"]),
               tire_runs=r["tire_runs"], notes=r["notes"], created_at=r["created_at"])
        runs += 1
    dst.commit()
    print(f"Imported {len(setup_map)} setups, {len(tire_map)} tire sets, {runs} runs into {db.DB_PATH}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
