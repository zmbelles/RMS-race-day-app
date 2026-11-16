"""Fill an empty database with demo data so every page has something to show.

    python seed.py          # only seeds when there are no drivers yet
    python seed.py --reset  # wipes the database first

The events are RMS's 2026 races, through Daytona Kart Week in December. Results are real where we have them
(Ignite Challenge rounds 2-5 with qualifying and lap times, too):
the NMP club championship (heat + final, with points), Autobahn club races 1-6 (prefinal + final finishes) and
Route 66 rounds 1-2 (finishes). Other rounds have no results yet. Dates (other than Daytona),
setups and lap times are made up.
"""
import datetime as dt
import os
import random
import sys

import db

TRACKS = {  # key: (name, city, state, good lap)
    "norway": ("Norway Motorsports Park", "Sheridan", "IL", 53.0),
    "autobahn": ("Kart Circuit Autobahn", "Joliet", "IL", 37.6),
    "gateway": ("Gateway Kartplex", "Madison", "IL", 42.2),
    "midstate": ("Mid-State Kart Club", "Dawson", "IL", None),
    "61": ("61 Kartway", "Delmar", "IA", None),
    "newcastle": ("New Castle Motorsports Park", "New Castle", "IN", None),
    "millrite": ("Mill-Rite Raceway", "Dousman", "WI", None),
    "briggs": ("Briggs & Stratton Motorplex", "Plymouth", "WI", None),
    "daytona": ("Daytona International Speedway", "Daytona Beach", "FL", None),
    "quincy": ("Quincy Grand Prix", "Quincy", "IL", None),
}

NMP = "NMP 2026 Championship"
SERIES_RULES = {NMP: 11}  # best 11 of 13 rounds count
NMP_POSITIONS = {"9": 2, "88": 3, "13": 5, "7": 8, "517": 11, "24": 13, "57": 14}  # official championship finish (whole field)
KCA_POSITIONS = {"260": 1, "517": 2}  # Owen won the Autobahn Club championship, Logan was runner-up
# Drivers who have left the team: number -> note. Their records stay.
FORMER = {"260": "Joined another team before Autobahn Club Race 6"}

# NMP club rounds 1-13, real results: (heat 1 finish, points, heat 2 finish, points); None = missed the round.
# A finish of None with points 0 means the driver was entered but didn't score that session.
M = None
NMP_RESULTS = {
    "9": [(1, 100, 1, 211), (2, 90, 2, 193), (3, 86, 3, 186), (3, 86, 2, 192), (2, 90, 2, 191), (4, 80, 4, 177),
          (3, 86, 3, 182), (4, 80, 4, 170), (3, 86, 3, 181), (15, 38, 12, 109), (1, 100, 2, 187), (1, 100, 2, 191),
          (1, 100, 1, 210)],
    "88": [(7, 66, 11, 111), (7, 66, 6, 153), (4, 80, 5, 166), (6, 70, 4, 172), (8, 62, 5, 161), (8, 62, 7, 149),
           (5, 75, 5, 162), (5, 75, 6, 150), (5, 75, 9, 127), (4, 80, 4, 175), M, (4, 80, 4, 171), (6, 70, 9, 126)],
    "13": [(5, 75, 4, 171), (8, 62, 7, 145), (7, 66, 15, 92), (2, 90, 3, 182), M, (3, 86, 14, 99), (4, 80, 1, 212),
           M, (9, 58, 10, 119), M, (4, 80, 6, 147), (6, 70, 8, 135), (5, 75, 4, 170)],
    "7": [(4, 80, 8, 135), (5, 75, 10, 121), (10, 54, 7, 148), M, (10, 54, 9, 127), (9, 58, None, 0),
          (None, 0, 8, 136), (6, 70, 5, 160), (6, 70, 11, 111), (10, 54, 7, 147), M, (11, 50, 11, 111), M],
    "517": [(9, 58, 7, 143), (10, 54, 5, 163), (8, 62, 10, 124), (8, 62, 8, 136), (5, 75, 10, 119),
            (10, 54, 10, 125), M, M, M, (5, 75, 9, 131), M, M, M],
    "57": [(6, 70, 6, 151), (4, 80, 3, 183), (5, 75, 4, 176), (5, 75, 5, 162), M, (None, 0, None, 0), M, M, M, M, M, M, M],
    "24": [M, (9, 58, 8, 137), M, (7, 66, 9, 128), M, (11, 50, 11, 117), (11, 50, 12, 106), M, M, M, M,
           (8, 62, 10, 119), (4, 80, 5, 160)],
}

# Autobahn club (Ignite Sr), real finishes: round -> [(driver #, heat 1 (prefinal), heat 2 (final))]; no points.
KCA = "Autobahn Club"
# What each championship is called on honours: "2026 Norway Motorsports Park Ignite Sr Vice-Champion".
SERIES_TITLES = {"NMP 2026 Championship": "Norway Motorsports Park", "Autobahn Club": "KCA"}
KCA_RESULTS = {
    1: [("9", 1, 1), ("517", 2, 5), ("260", 3, 7), ("13", 4, 2)],
    2: [("517", 2, 2), ("260", 1, 1)],
    3: [("517", 2, 2), ("260", 1, 1)],
    4: [("517", 3, 2), ("260", 1, 1)],
    5: [("517", 9, 3), ("260", 7, 7)],
    6: [("517", 6, 5), ("77", 12, 6), ("24", 8, 12)],
}
# Route 66 Sprint Series (Briggs LO206 Sr), real results: round -> (sessions, [(driver key, kart # that day,
# {session: (finish, best lap)})]). Driver key is their first entry's number (#77 James, #7 Michael, #511 Alex).
R66 = "Route 66 Sprint Series"
R66_RESULTS = {
    1: ("Race 1, Race 2, Final", [
        ("77", "577", {"Race 1": (20, None), "Race 2": (32, None), "Final": (25, None)}),
        ("7", "507", {"Race 1": (29, None), "Race 2": (33, None), "Final": (30, None)})]),
    2: ("Qualifying, Heat 1, Heat 2, Final", [
        ("511", "511", {"Qualifying": (35, None), "Heat 1": (33, None), "Heat 2": (26, None),
                        "Final": (26, None)})]),  # lap times dropped: they weren't Alex's
}
# Club races run two heats (NMP calls them heat/final, KCA prefinal/final); Route 66 runs two races and a final.
# Ignite Challenge (real): round -> [(driver's Ignite Sr #, kart # that day, class, {session: (finish, best lap)})].
# Round 1 (61 Kartway) has no data; round 2 (Norway) only race 2.
IC = "Ignite Challenge"
IC_RESULTS = {
    2: [("13", "13", "Ignite Sr", {"Race 2": (23, "53.921")}),
        ("517", "517", "Ignite Sr", {"Race 2": (18, "56.173")}),
        ("260", "260", "Ignite Sr", {"Race 2": (14, "54.918")}),
        ("88", "888", "Ignite Sr", {"Race 2": (13, "54.924")}),
        ("7", "7", "Ignite Sr", {"Race 2": (12, "54.359")}),
        ("9", "99", "Ignite Sr", {"Race 2": (9, "55.155")}),
        ("77", "77", "Ignite Sr", {"Race 2": (6, "55.550")})],
    3: [("77", "77", "Ignite Heavy", {"Race 1": (1, "1:02.385"), "Race 2": (5, "1:01.975")}),
        ("88", "888", "Ignite Sr", {"Qualifying": (20, "1:03.379"), "Race 1": (12, "1:02.038"), "Race 2": (8, "1:01.628")}),
        ("260", "260", "Ignite Sr", {"Qualifying": (14, "1:03.047"), "Race 1": (15, "1:02.765"), "Race 2": (6, "1:01.615")}),
        ("517", "517", "Ignite Sr", {"Qualifying": (18, "1:03.379"), "Race 1": (14, "1:02.320"), "Race 2": (26, "1:08.652")}),
        ("7", "7", "Ignite Sr", {"Qualifying": (15, "1:03.223"), "Race 1": (16, "1:02.092"), "Race 2": (25, "1:02.183")})],
    4: [("77", "77", "Ignite Sr", {"Qualifying": (1, "54.978"), "Race 1": (2, "55.434"), "Race 2": (4, "55.560")}),
        ("88", "888", "Ignite Sr", {"Qualifying": (20, "56.092"), "Race 1": (17, "55.974"), "Race 2": (11, "55.809")}),
        ("260", "260", "Ignite Sr", {"Qualifying": (22, "57.052"), "Race 1": (19, "56.651"), "Race 2": (17, "56.244")})],
    5: [("517", "517", "Ignite Heavy", {"Qualifying": (3, "43.753"), "Race 1": (11, "1:21.951"), "Race 2": (6, "43.719")}),
        ("7", "77", "Ignite Heavy", {"Qualifying": (5, "43.842"), "Race 1": (2, "43.375"), "Race 2": (1, "43.056")}),
        ("24", "24", "Ignite Sr", {"Qualifying": (21, "43.661"), "Race 1": (15, "43.283"), "Race 2": (26, "44.913")}),
        ("88", "888", "Ignite Sr", {"Qualifying": (15, "43.282"), "Race 1": (11, "42.933"), "Race 2": (20, "43.233")}),
        ("9", "19", "Ignite Sr", {"Qualifying": (9, "42.745"), "Race 1": (9, "42.462"), "Race 2": (11, "42.485")})],
}
# Daytona Kart Week (late December) is round 1 of the next year's Ignite Road Racing Challenge.
IRR = "Ignite Road Racing Challenge"
SESSIONS = {NMP: "Heat 1, Heat 2", KCA: "Heat 1, Heat 2", R66: "Race 1, Race 2, Final", IC: "Qualifying, Race 1, Race 2"}

# 2026 season: (date, days, name, series, track)
SEASON = [
    *[(d, 1, f"NMP 2026 Championship Race {i}", NMP, "norway") for i, d in enumerate([
        "04-11", "04-25", "05-02", "05-23", "06-06", "06-20", "07-25", "08-08",
        "08-22", "09-05", "09-12", "09-19", "10-03"], start=1)],
    *[(d, 1, f"Autobahn Club Race {i}", KCA, "autobahn")
      for i, d in enumerate(["05-09", "06-13", "08-01", "08-29", "09-20", "10-04"], start=1)],
    *[(d, 2, f"Ignite Challenge · {name}", IC, t) for d, name, t in [  # rounds 1-5, in order; dates are placeholders
        ("05-16", "61 Kartway", "61"), ("06-27", "Norway", "norway"), ("07-11", "Autobahn", "autobahn"),
        ("08-15", "Mid-State", "midstate"), ("10-10", "Gateway", "gateway")]],
    # Route 66: RMS drivers ran rounds 1 and 2 only.
    ("04-18", 2, "Route 66 Sprint Series · New Castle", R66, "newcastle"),
    ("05-30", 2, "Route 66 Sprint Series · Mill-Rite", R66, "millrite"),
    ("07-26", 2, "Quincy Grand Prix", None, "quincy"),
    ("12-27", 3, "Daytona Kart Week", IRR, "daytona"),
]

# One-off races, real finishes: name -> (sessions, [(driver #, kart # that day, class, [finish per session],
# qualifying, best lap)]). Qualifying and best lap go on the first race. None means the driver wasn't entered in that
# race (one-off races sell each race separately).
ONE_OFF = {
    "Quincy Grand Prix": ("Race 1, Race 2", [
        ("24", "524", "Ignite Sr", [13, None], 19, 77.541),
        ("260", "260", "Ignite Sr", [31, 30], 17, 78.935),
        ("13", "13", "Ignite Sr", [23, 22], 32, 79.299),
    ]),
}

# Daytona Kart Week 2025 (Dec 28-30): round 1 of the 2026 Ignite Road Racing Challenge, which RMS ran as a one-off.
# (sessions, [(driver #, class, {session: (finish, best lap)})])
DAYTONA_2025 = ("Practice (Race 2), Race 2", [
    ("77", "Ignite Sr", {"Practice (Race 2)": (7, "3:25.934"), "Race 2": (9, "3:06.901")}),
    ("57", "Ignite Heavy", {"Race 2": (1, "3:10.781")}),  # no practice time
])

DRIVERS = [("Zachary Belles", "24"), ("Leonardo Rojas", "9"), ("Jesus Garza", "13"),
           ("Colin Cox", "88"), ("Logan Matusik", "517"), ("Michael Smith", "7"),
           ("Owen Neisel", "260"), ("James Recendez", "77"), ("Matthew Hartsell", "57")]
# Drivers who only race LO206 Sr (a Margay Brava): (name, number)
LO206_DRIVERS = [("Alex Koleczek", "511")]
# Other classes drivers have raced: (driver's Ignite Sr #, class, number run, kart). A driver races one class at a
# time, so these only appear in results, with the class and number run: #77 was Michael in Ignite Heavy, Logan ran
# #517 in both, James ran Ignite Heavy as #77 once (Ignite Challenge round 3), and Matthew ran Heavy at Daytona 2025.
EXTRA_CLASSES = [
    ("7", "LO206 Sr", "507", "brava"),
    ("7", "Ignite Heavy", "77", "same"),
    ("77", "LO206 Sr", "577", "brava"),
    ("517", "Ignite Heavy", "517", "same"),
    ("77", "Ignite Heavy", "77", "same"),
    ("57", "Ignite Heavy", "57", "same"),
]
# Made-up pace for lap times: seconds off a good lap, roughly following the club standings.
PACE = {"57": 0.3, "9": -0.1, "260": -0.05, "88": 0.15, "13": 0.2, "517": 0.25, "77": 0.35, "7": 0.35, "24": 0.45}

SETUPS = [  # track, rear sprocket, FTW, RTW, psi; front sprocket is fixed at 17
    ("norway", 61, 1.5, 0.5, 30), ("norway", 62, 1.25, 0.5, 31), ("norway", 60, 1.5, 0.75, 29),
    ("autobahn", 59, 1.5, 0.75, 32), ("autobahn", 60, 1.25, 0.5, 30),
    ("gateway", 60, 1.5, 0.5, 30), ("gateway", 62, 1.25, 0.5, 31),
    ("midstate", 60, 1.25, 0.25, 34),
    ("61", 61, 1.5, 0.5, 31),
    ("newcastle", 59, 1.5, 0.5, 32),
    ("millrite", 62, 1.25, 0.5, 30),
    ("quincy", 60, 1.5, 0.5, 30),
]


def main():
    if os.getenv("RMS_ENV", "").lower() == "production":
        raise SystemExit("seed.py fills the database with demo data; it won't run with RMS_ENV=production.")
    if "--reset" in sys.argv and os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db.init_db()
    conn = db.connect()
    if conn.execute("SELECT COUNT(*) FROM drivers").fetchone()[0]:
        print("Database already has drivers; use --reset to start over.")
        return

    rng = random.Random(24)
    year = dt.date.today().year
    ins = lambda table, **v: db.insert(conn, table, **v)

    tracks = {key: ins("tracks", name=n, short_name=db.default_short_name(n), city=c, state=s, target_lap=t)
              for key, (n, c, s, t) in TRACKS.items()}
    targets = {tracks[key]: t[3] for key, t in TRACKS.items()}

    ts = db.now()
    by_track = {}
    for key, rs, ftw, rtw, psi in SETUPS:
        sid = ins("setups", front_sprocket=17, rear_sprocket=rs, front_width=ftw, rear_width=rtw,
                  tire="Hoosier R80", psi=psi, axle="Hard", caster="Neutral", camber="0°",
                  seat_position="Std", front_shims_above=3, total_weight=380, carb_jetting="#110",
                  clutch="Margay 4,500 rpm", created_at=ts, updated_at=ts)
        db.number_setup(conn, sid, tracks[key])
        by_track.setdefault(tracks[key], []).append(sid)

    for name in dict.fromkeys([*SERIES_RULES, *SERIES_TITLES]):
        ins("series", name=name, best_of=SERIES_RULES.get(name), title=SERIES_TITLES.get(name))

    # Everyone sits on a Norway setup between races; #24, #9 and #88 share Norway 1.
    current = {"24": 0, "9": 0, "13": 1, "88": 0, "517": 2, "7": 1, "260": 2, "77": 1, "57": 0}
    drivers = []
    for name, number in DRIVERS:
        kid = ins("karts", name=f"#{number} Ignite K3", kart_class="Ignite Sr", chassis="Margay Ignite K3",
                  engine="Margay Ignite 212", engine_number=f"S{rng.randint(10000, 99999)}")
        did = ins("drivers", name=name, email=f"{name.split()[0].lower()}@example.com")
        setup = by_track[tracks["norway"]][current[number]]
        eid_ = ins("entries", driver_id=did, kart_class="Ignite Sr", number=number, kart_id=kid, setup_id=setup)
        drivers.append({"id": did, "number": number, "kart": kid, "setup": setup, "skill": PACE[number], 
                        "sets": 0, "entries": {"Ignite Sr": eid_}, "numbers": {"Ignite Sr": number}})
    for name, number in LO206_DRIVERS:
        kid = ins("karts", name=f"#{number} Brava", kart_class="LO206 Sr", chassis="Margay Brava", engine="Briggs LO206")
        did = ins("drivers", name=name, email=f"{name.split()[0].lower()}@example.com")
        setup = by_track[tracks["millrite"]][0]
        eid_ = ins("entries", driver_id=did, kart_class="LO206 Sr", number=number, kart_id=kid, setup_id=setup)
        drivers.append({"id": did, "number": number, "kart": kid, "setup": setup, "skill": 0.4,
                        "entries": {"LO206 Sr": eid_}, "numbers": {"LO206 Sr": number}})
    by_number = {d["number"]: d for d in drivers}
    for number, kart_class, entry_number, kart in EXTRA_CLASSES:
        d = by_number[number]
        if kart == "brava":
            ins("karts", name=f"#{entry_number} Brava", kart_class=kart_class, chassis="Margay Brava", engine="Briggs LO206")
        # Results in other classes go on the driver's one entry, with that class and number.
        d["numbers"][kart_class] = entry_number
        d["entries"][kart_class] = next(iter(d["entries"].values()))
    for d in drivers:
        if d["number"] in NMP_POSITIONS:
            ins("series_positions", series=NMP, season=year, driver_id=d["id"], kart_class="Ignite Sr",
                position=NMP_POSITIONS[d["number"]])
        if d["number"] in KCA_POSITIONS:
            ins("series_positions", series=KCA, season=year, driver_id=d["id"], kart_class="Ignite Sr",
                position=KCA_POSITIONS[d["number"]])
    ins("karts", name="Spare Ignite K3", kart_class="Ignite Sr", chassis="Margay Ignite K3",
        engine="Margay Ignite 212", notes="Arrive-and-drive spare")

    first_race_extras = {}  # (event name, driver #) -> qualifying / best lap for that event's first race
    rounds = {}
    for mmdd, days, name, series, key in sorted(SEASON):
        start = dt.date.fromisoformat(f"{year}-{mmdd}")
        end = start + dt.timedelta(days=days - 1) if days > 1 else None
        track = tracks[key]
        rnd_ = rounds.get(series, 0) + 1  # this event's round in its series
        event_sessions = ((R66_RESULTS[rnd_][0] if series == R66 and rnd_ in R66_RESULTS else SESSIONS.get(series))
                          or (ONE_OFF[name][0] if name in ONE_OFF else None))
        eid = ins("events", name=name, event_type="Race", track_id=track, series=series, sessions=event_sessions,
                  season=year + 1 if series == IRR else None,
                  start_date=start.isoformat(), end_date=end and end.isoformat(),
                  status="Completed" if (end or start) < dt.date.today() else "Scheduled")
        rnd = rounds[series] = rounds.get(series, 0) + 1
        # Each entry: driver, kart number and class that day, [(session, finish, points)]
        entries = []
        if series == NMP:
            for number, res in NMP_RESULTS.items():
                r = res[rnd - 1]
                if r:
                    entries.append((by_number[number], number, "Ignite Sr",
                                    [("Heat 1", r[0], r[1]), ("Heat 2", r[2], r[3])]))
        elif series == KCA:
            for number, prefinal, final in KCA_RESULTS.get(rnd, []):
                entries.append((by_number[number], number, "Ignite Sr",
                                [("Heat 1", prefinal, None)] + ([("Heat 2", final, None)] if final else [])))
        elif name in ONE_OFF:
            names = db.session_list(ONE_OFF[name][0])
            for number, kart, kart_class, finishes, qual, lap in ONE_OFF[name][1]:
                entries.append((by_number[number], kart, kart_class,
                                [(s, f, None) for s, f in zip(names, finishes) if f is not None]))
                first_race_extras[(name, number)] = dict(qual_pos=qual, best_lap=lap)
        elif series == IC:
            names = db.session_list(SESSIONS[IC])
            for number, kart, kart_class, by_session in IC_RESULTS.get(rnd, []):
                entries.append((by_number[number], kart, kart_class,
                                [(s, *by_session[s][:1], None, db.parse_lap(by_session[s][1])) for s in names if s in by_session]))
        elif series == R66 and rnd in R66_RESULTS:
            names, rows = R66_RESULTS[rnd]
            for number, kart, by_session in rows:
                entries.append((by_number[number], kart, "LO206 Sr",
                                [(s, by_session[s][0], None, db.parse_lap(by_session[s][1]))
                                 for s in db.session_list(names) if s in by_session]))
        target = targets[track]
        for i, (d, kart_number, kart_class, sessions) in enumerate(entries):
            sid = by_track[track][i % len(by_track[track])]  # the setup this driver ran at this track
            # Made-up practice lap times, only where we know what a good lap looks like.
            if target and kart_class == "Ignite Sr" and series in (NMP, KCA):
                for session in ["Practice", "Qualifying", "Heat", "Final"]:
                    lap = target + d["skill"] + rng.uniform(0, 0.45) - (0.12 if session == "Final" else 0)
                    ins("runs", driver_id=d["id"], entry_id=d["entries"][kart_class], setup_id=sid, kart_id=d["kart"],
                        kart_class=kart_class, kart_number=kart_number,
                        track_id=track, event_id=eid,
                        run_date=start.isoformat(), session=session, best_lap=round(lap, 3),
                        laps=rng.choice([8, 10, 12, 15]), air_temp=rng.randint(52, 88), track_temp=rng.randint(60, 118),
                        track_condition=rng.choice(db.TRACK_CONDITIONS[:3]))
            deciding = db.main_session(db.session_list(event_sessions))
            for i_session, (session, finish, points, *lap) in enumerate(sessions):
                dns = finish == "DNS"
                extras = first_race_extras.get((name, d["number"]), {}) if i_session == 0 else {}
                if lap and lap[0]:
                    extras = {**extras, "best_lap": lap[0]}
                entry_id = d["entries"][kart_class]
                ins("results", event_id=eid, driver_id=d["id"], entry_id=entry_id, session=session, kart_class=kart_class,
                    main=int(session == deciding or not series), dnf=int(dns), classification="DNS" if dns else None,
                    kart_number=kart_number, setup_id=sid,
                    finish_pos=None if dns else finish, points=points, **extras)
    names, rows = DAYTONA_2025
    eid = ins("events", name="Daytona Kart Week", event_type="Race", track_id=tracks["daytona"], sessions=names,
              season=year, start_date=f"{year - 1}-12-28", end_date=f"{year - 1}-12-30", status="Completed")
    for number, kart_class, by_session in rows:
        d = by_number[number]
        for session, (finish, lap) in by_session.items():
            ins("results", event_id=eid, driver_id=d["id"], entry_id=d["entries"][kart_class], session=session,
                kart_class=kart_class, kart_number=d["numbers"][kart_class],
                main=int(db.session_kind(session) != "Practice"), finish_pos=finish,
                best_lap=db.parse_lap(lap))
    for number, note in FORMER.items():
        conn.execute("UPDATE drivers SET active = 0, left_note = ? WHERE id = ?", (note, by_number[number]["id"]))
    conn.commit()
    print(f"Seeded {db.DB_PATH}")


if __name__ == "__main__":
    main()
