"""Copy setups and runs from the Discord bot's kart_setups.db into the web app.

    python import_bot_db.py path/to/kart_setups.db

Each Discord user becomes a driver named "Discord user 1234" with kart number "D1234"
(rename and renumber them on the Drivers page). Their most recent setup becomes their
current one. Each class + chassis becomes a kart, each track name a track, and each setup is
numbered at its track ("Gateway 1"). Tire sets and parts are skipped. Safe to re-run: things already
imported are skipped.
"""
import sqlite3
import sys

import commands
import db


def main(src_path):
    db.init_db()
    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row
    dst = db.connect()

    def get_or_insert(table, where, **values):
        cols = list(where)
        row = dst.execute(f"SELECT id FROM {table} WHERE " + " AND ".join(f"{c} IS ?" for c in cols),
                          [where[c] for c in cols]).fetchone()
        return (row[0], False) if row else (db.insert(dst, table, **where, **values), True)

    setup_map, track_of, kart_of_user, latest = {}, {}, {}, {}
    created = 0
    for s in src.execute("SELECT * FROM setups ORDER BY updated_at"):
        values = {c: (s[c] if c in s.keys() else None) for c in commands.TUNING}
        values["psi"] = s["front_psi"] if s["front_psi"] is not None else s["rear_psi"]
        notes = " · ".join(x for x in (f"Bot setup “{s['name']}”", s["notes"]) if x)
        if s["track"]:
            track_of[s["id"]] = get_or_insert("tracks", {"name": s["track"].strip()},
                                              short_name=db.default_short_name(s["track"].strip()))[0]
        values["track_id"] = track_of.get(s["id"])
        sid, new = get_or_insert("setups", values, notes=notes, created_at=s["created_at"], updated_at=s["updated_at"])
        if new:
            db.number_setup(dst, sid, values["track_id"])  # "Gateway 1", or "Setup 1" without a track
        created += new
        setup_map[s["id"]] = sid
        kart_of_user[s["user_id"]] = get_or_insert("karts", {"kart_class": s["kart_class"], "chassis": s["chassis"]},
                                                   name=s["chassis"] or s["kart_class"])[0]
        latest[s["user_id"]] = (sid, s["kart_class"])

    drivers, entry_of = {}, {}
    for user_id, (sid, kart_class) in latest.items():
        number = f"D{str(user_id)[-4:]}"
        row = dst.execute("SELECT id FROM drivers WHERE discord_id = ?", (str(user_id),)).fetchone()
        drivers[user_id] = row[0] if row else db.insert(
            dst, "drivers", name=f"Discord user {str(user_id)[-4:]}", discord_id=str(user_id))
        # One class at a time: a driver has one entry.
        entry_of[user_id] = get_or_insert("entries", {"driver_id": drivers[user_id]}, kart_class=kart_class,
                                          number=number, kart_id=kart_of_user[user_id], setup_id=sid)[0]

    # Tire sets are skipped: the app no longer tracks tire life.

    runs = 0
    for r in src.execute("SELECT * FROM runs ORDER BY id"):
        driver_id = drivers.get(r["user_id"])
        if not driver_id or r["setup_id"] not in setup_map:
            continue
        _, new = get_or_insert("runs", {"driver_id": driver_id, "setup_id": setup_map[r["setup_id"]],
                                        "run_date": r["run_date"], "best_lap": r["best_lap"]},
                               entry_id=entry_of.get(r["user_id"]),
                               kart_id=kart_of_user.get(r["user_id"]), track_id=track_of.get(r["setup_id"]),
                               session=r["session"], laps=r["laps"], air_temp=r["air_temp"], track_temp=r["track_temp"],
                               track_condition=r["track_condition"], notes=r["notes"], created_at=r["created_at"])
        runs += new
    dst.commit()
    print(f"Imported {created} new setups, {len(drivers)} drivers, {runs} new runs into {db.DB_PATH}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
