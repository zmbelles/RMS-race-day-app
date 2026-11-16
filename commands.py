"""Race-day shorthand: '24 SPR 65' sets driver #24's rear sprocket to 65.

    <number> [class] <KEYWORD> <value> [<KEYWORD> <value> ...]

    SPR  rear sprocket      65   (the front is fixed)
    FTW  front track width  1.5
    RTW  rear track width   0.75
    PSI  tire pressure      30   (same on all four tires)
    FRH  front ride height  3    (shims above the spindle, 0-6; the rest go below, 3 = standard)
    TRK  track              norway  (take the setup to another track)

List several karts to change them all at once: '24 9 13 88 PSI 31'.
Numbers are per class: when a number is used in more than one class, name the class after it,
e.g. '77 heavy SPR 61' or '517 sr PSI 30'. Values can be relative: '24 SPR +1', '9 PSI -0.5'. Separate several commands with ';' or new
lines. 'UNDO' reverts the most recent change.

Setups belong to a track ("Gateway 3"). A change never edits the driver's setup in place, because
other drivers may share it. The driver moves to the setup at the same track with exactly the new
values, or to a new one numbered next at that track. TRK does the same at a different track.
"""
import re
from dataclasses import dataclass, field

import db
from db import fmt_num

# Columns that define what a setup physically is. Two setups with equal values here are the same setup.
TUNING = ["front_sprocket", "rear_sprocket", "front_width", "rear_width", "tire", "psi",
          "axle", "caster", "camber", "seat_position", "front_shims_above", "total_weight", "ballast",
          "carb_jetting", "clutch"]

LABELS = {"front_sprocket": "Front sprocket", "rear_sprocket": "Rear sprocket", "front_width": "Front track width",
          "rear_width": "Rear track width", "psi": "PSI", "front_shims_above": "Front ride height (shims above)"}


@dataclass
class Keyword:
    label: str
    field: str
    kind: type
    limits: tuple      # hard limits: outside is an error
    typical: tuple = None  # outside is a warning


KEYWORDS = {
    "SPR": Keyword("Rear sprocket", "rear_sprocket", int, (8, 99), (58, 63)),
    "FTW": Keyword("Front track width", "front_width", float, (0, 100)),
    "RTW": Keyword("Rear track width", "rear_width", float, (0, 100)),
    "PSI": Keyword("Tire pressure", "psi", float, (0, 80), (28, 36)),
    "FRH": Keyword("Front ride height", "front_shims_above", int, (0, db.SHIMS)),
    "TRK": Keyword("Track", "track_id", str, None),
}

VALUE_RE = re.compile(r"^([+-])?(\d+(?:\.\d+)?)$")
GLUED_RE = re.compile(r"^([A-Za-z]+)([+-]?\d.*)$")


class CommandError(ValueError):
    pass


@dataclass
class Outcome:
    line: str
    ok: bool = True
    kind: str = ""            # switch | create | noop | undo | error
    message: str = ""
    driver: dict = None
    changes: list = field(default_factory=list)   # (label, old, new)
    warnings: list = field(default_factory=list)
    setup_id: int = None
    setup_name: str = ""

    def as_dict(self):
        return self.__dict__


def split_commands(text):
    return [part.strip() for part in re.split(r"[;\n]+", text or "") if part.strip()]


def tokenize(line):
    tokens = []
    for tok in line.replace(",", " ").split():
        m = GLUED_RE.match(tok)
        tokens += [m.group(1), m.group(2)] if m and tok.upper()[:3] in KEYWORDS else [tok]
    return tokens


NUMBER_RE = re.compile(r"^#?[A-Za-z]?\d+[A-Za-z]?$")  # 24, #77, D1234 (not class words like 'lo206')


def parse(line):
    """'24 SPR 65 PSI 30' -> ([('24', '')], [('SPR', '65'), ('PSI', '30')]);
    '24 9 77 heavy PSI 31' -> ([('24', ''), ('9', ''), ('77', 'heavy')], [('PSI', '31')]).
    Every kart number before the first keyword gets the same changes; class words follow the number they belong to."""
    tokens = tokenize(line)
    if not tokens:
        raise CommandError("Empty command")
    targets = []
    rest = list(tokens)
    while rest and rest[0].upper() not in KEYWORDS:
        tok = rest.pop(0)
        if NUMBER_RE.match(tok):
            targets.append([tok.lstrip("#"), []])
        elif targets:
            targets[-1][1].append(tok)  # class words for the number before it, e.g. '77 heavy'
        else:
            raise CommandError("Start with the driver's kart number, e.g. 24 SPR 65")
    if not targets:
        raise CommandError("Start with the driver's kart number, e.g. 24 SPR 65")
    if len(rest) % 2:
        last = rest[-1].upper()
        raise CommandError(f"{last} needs a value" if last in KEYWORDS else f"Expected KEYWORD value, got '{rest[-1]}'")
    pairs = []
    for kw, raw in zip(rest[::2], rest[1::2]):
        kw = kw.upper()
        if kw not in KEYWORDS:
            raise CommandError(f"Unknown keyword '{kw}'. Use {', '.join(KEYWORDS)}")
        pairs.append((kw, raw))
    if len({n for n, _ in targets}) < len(targets) and not all(h for _, h in targets):
        seen = [n for n, _ in targets]
        dup = next(n for n in seen if seen.count(n) > 1)
        raise CommandError(f"#{dup} is listed twice; add the class to each, e.g. “{dup} sr {dup} heavy”")
    return [(n, " ".join(h)) for n, h in targets], pairs


def find_entry(conn, number, hint="", event_id=None):
    """The entry racing this number, narrowed by a class hint ('heavy', 'sr', '206') when the number is
    used in more than one class. With a race-day event, numbers come from that race's entry list only.
    Raises CommandError when there is no single match."""
    if event_id:
        rows = conn.execute("SELECT e.*, ee.number AS race_number, d.name, d.active AS driver_active, ev.name AS event_name "
                            "FROM event_entries ee JOIN entries e ON e.id = ee.entry_id JOIN drivers d ON d.id = e.driver_id "
                            "JOIN events ev ON ev.id = ee.event_id WHERE ee.event_id = ? AND ee.number = ?",
                            (event_id, number)).fetchall()
        if hint:
            rows = [r for r in rows if all(w.lower() in r["kart_class"].lower() for w in hint.split())]
        if not rows:
            race = conn.execute("SELECT name FROM events WHERE id = ?", (event_id,)).fetchone()
            raise CommandError(f"No #{number}{' ' + hint if hint else ''} on the {race['name'] if race else 'race-day'} "
                               "entry list; add them on the race page")
        if len(rows) > 1:
            options = " and ".join(f"#{number} {r['kart_class']} ({r['name']})" for r in rows)
            raise CommandError(f"#{number} is {options} at this race; add the class, "
                               f"e.g. “{number} {rows[1]['kart_class'].split()[-1].lower()} SPR 61”")
        entry = dict(rows[0])
        entry["number"] = entry["race_number"]  # the number they run at this race
        return entry
    rows = conn.execute("SELECT e.*, d.name, d.active AS driver_active FROM entries e JOIN drivers d ON d.id = e.driver_id "
                        "WHERE e.number = ? ORDER BY d.active DESC, e.active DESC, e.id", (number,)).fetchall()
    if hint:
        rows = [r for r in rows if all(w.lower() in r["kart_class"].lower() for w in hint.split())]
        if not rows:
            raise CommandError(f"No #{number} in a class matching “{hint}”")
    if not rows:
        raise CommandError(f"No driver with number #{number}")
    current = [r for r in rows if r["driver_active"] and r["active"]]
    if len(current) > 1:
        options = " and ".join(f"#{number} {r['kart_class']} ({r['name']})" for r in current)
        example = current[1]["kart_class"].split()[-1].lower()
        raise CommandError(f"#{number} is {options}; add the class, e.g. “{number} {example} SPR 61”")
    entry = (current or rows)[0]
    if not entry["driver_active"]:
        raise CommandError(f"#{number} {entry['name']} has left the team; reinstate them on their driver page first")
    if not entry["active"]:
        raise CommandError(f"#{number} {entry['name']} no longer races {entry['kart_class']}")
    return entry


def new_values(current, pairs):
    """Apply keyword pairs to a setup row. Returns (values, warnings)."""
    values = {c: current[c] for c in TUNING}
    warnings = []
    for kw, raw in pairs:
        if kw == "TRK":
            continue
        spec, col = KEYWORDS[kw], KEYWORDS[kw].field
        m = VALUE_RE.match(raw)
        if not m:
            raise CommandError(f"{kw} value '{raw}' isn't a number")
        sign, text = m.groups()
        num = float(text)
        if spec.kind is int:
            if not num.is_integer():
                raise CommandError(f"{kw} needs a whole number of teeth")
            num = int(num)
        if sign:
            if values[col] is None:
                raise CommandError(f"{LABELS[col]} isn't set yet, so '{raw}' has nothing to add to")
            num = values[col] + (num if sign == "+" else -num)
        lo, hi = spec.limits
        if not lo <= num <= hi:
            raise CommandError(f"{LABELS[col]} {fmt_num(num)} is outside {lo}–{hi}")
        values[col] = round(num, 3) if spec.kind is float else num
        if spec.typical and num != current[col] and not spec.typical[0] <= num <= spec.typical[1]:
            unit = "T" if kw == "SPR" else " psi"
            warnings.append(f"{LABELS[col]} {fmt_num(num)}{unit} is outside the usual {spec.typical[0]}–{spec.typical[1]}{unit}")
    return values, warnings


def find_setup(conn, values, track_id):
    """The setup at this track with exactly these values, if there is one."""
    where = " AND ".join(f"{c} IS ?" for c in TUNING)
    return conn.execute(f"SELECT * FROM setups WHERE track_id IS ? AND {where} ORDER BY track_seq LIMIT 1",
                        [track_id] + [values[c] for c in TUNING]).fetchone()


def find_track(conn, text):
    """'norway', 'Mid-State' or 'gate' -> track row. Matches the short name, then the start of either name."""
    t = text.strip().lower()
    rows = conn.execute("SELECT * FROM tracks ORDER BY name").fetchall()
    exact = [r for r in rows if (r["short_name"] or "").lower() == t or r["name"].lower() == t]
    loose = exact or [r for r in rows if (r["short_name"] or "").lower().startswith(t) or r["name"].lower().startswith(t)]
    if len(loose) == 1:
        return loose[0]
    names = ", ".join(r["short_name"] or r["name"] for r in rows)
    raise CommandError(f"“{text}” matches {'more than one track' if loose else 'no track'}. Tracks: {names}")


def spec_label(s):
    """Setup values in one line: '60T · FTW 1.5 · RTW 0.5 · 30 psi'."""
    parts = [f"{s['rear_sprocket']}T"]
    for label, col in (("FTW", "front_width"), ("RTW", "rear_width")):
        if s[col] is not None:
            parts.append(f"{label} {fmt_num(s[col])}")
    if s["psi"] is not None:
        parts.append(f"{fmt_num(s['psi'])} psi")
    if "front_shims_above" in s.keys() and s["front_shims_above"] is not None:
        parts.append(f"FRH {shims(s['front_shims_above'])}")
    return " · ".join(parts)


def shims(above):
    """3 -> '3/3' (shims above / below the spindle)."""
    return "—" if above is None else f"{above}/{db.SHIMS - above}"


def apply_line(conn, line, event_id=None):
    """Run one command line; a line naming several karts gives one outcome per kart."""
    if line.strip().upper() == "UNDO":
        return [undo(conn)]
    targets, pairs = parse(line)
    if len(targets) == 1:
        return [apply_target(conn, line, *targets[0], pairs, event_id=event_id)]
    changes = " ".join(f"{kw} {raw}" for kw, raw in pairs)
    outcomes = []
    for number, hint in targets:
        own = f"{number}{' ' + hint if hint else ''} {changes}".strip()  # logged and undone per kart
        try:
            outcomes.append(apply_target(conn, own, number, hint, pairs, event_id=event_id))
        except CommandError as exc:
            outcomes.append(Outcome(line=own, ok=False, kind="error", message=str(exc)))
    return outcomes


def apply_target(conn, line, number, hint, pairs, event_id=None):
    entry = find_entry(conn, number, hint, event_id)
    current = conn.execute("SELECT * FROM setups WHERE id = ?", (entry["setup_id"],)).fetchone()
    out = Outcome(line=line, driver={"id": entry["driver_id"], "name": entry["name"], "number": entry["number"],
                                     "kart_class": entry["kart_class"]})
    if not pairs:
        out.kind, out.setup_id, out.setup_name = "noop", current["id"], current["name"]
        out.message = f"#{number} {entry['name']} ({entry['kart_class']}) is on {current['name']}"
        return out

    values, out.warnings = new_values(current, pairs)
    track_id = current["track_id"]
    show = lambda c, v: shims(v) if c == "front_shims_above" else fmt_num(v)
    out.changes = [(LABELS[c].replace(" (shims above)", ""), show(c, current[c]), show(c, values[c]))
                   for c in TUNING if values[c] != current[c]]
    for kw, raw in pairs:
        if kw == "TRK":
            track = find_track(conn, raw)
            if track["id"] != track_id:
                out.changes.insert(0, ("Track", db.track_short(conn, track_id) if track_id else "none",
                                       db.track_short(conn, track["id"])))
                track_id = track["id"]
    if not out.changes:
        out.kind, out.setup_id, out.setup_name = "noop", current["id"], current["name"]
        out.message = f"#{number} already runs those values"
        return out

    match = find_setup(conn, values, track_id)
    if match:
        target_id, out.kind, out.setup_name = match["id"], "switch", match["name"]
        out.message = f"#{number} → existing setup {match['name']}"
    else:
        ts = db.now()
        target_id = db.insert(conn, "setups", created_at=ts, updated_at=ts,
                              notes=f"Made from {current['name']} with “{line}”", **values)
        out.kind = "create"
        out.setup_name = db.number_setup(conn, target_id, track_id)
        out.message = f"#{number} → new setup {out.setup_name}"
    out.setup_id = target_id
    conn.execute("UPDATE entries SET setup_id = ? WHERE id = ?", (target_id, entry["id"]))
    db.insert(conn, "setup_changes", driver_id=entry["driver_id"], entry_id=entry["id"], command=line,
              from_setup_id=current["id"], to_setup_id=target_id, created_setup=int(out.kind == "create"))
    return out


def assign_setup(conn, entry_id, setup_id, how="Race day"):
    """Put an entry on a chosen setup (dragged or picked on Race day). Logged like a command, so UNDO reverts it."""
    entry = conn.execute("SELECT e.*, d.name FROM entries e JOIN drivers d ON d.id = e.driver_id WHERE e.id = ?",
                         (entry_id,)).fetchone()
    setup = conn.execute("SELECT * FROM setups WHERE id = ?", (setup_id,)).fetchone()
    if entry is None or setup is None:
        raise CommandError("That driver or setup no longer exists")
    if entry["setup_id"] == setup_id:
        return f"{entry['name']} is already on {setup['name']}"
    old = conn.execute("SELECT name FROM setups WHERE id = ?", (entry["setup_id"],)).fetchone()
    conn.execute("UPDATE entries SET setup_id = ? WHERE id = ?", (setup_id, entry_id))
    db.insert(conn, "setup_changes", driver_id=entry["driver_id"], entry_id=entry_id,
              command=f"{how}: {setup['name']}", from_setup_id=entry["setup_id"], to_setup_id=setup_id, created_setup=0)
    return f"{entry['name']} ({entry['kart_class']}): {old['name'] if old else '—'} → {setup['name']}"


def undo(conn, change_id=None):
    sql = ("SELECT c.*, en.number, d.name, en.setup_id AS on_setup FROM setup_changes c "
           "JOIN entries en ON en.id = c.entry_id JOIN drivers d ON d.id = c.driver_id ")
    change = conn.execute(sql + ("WHERE c.id = ?" if change_id else "ORDER BY c.id DESC LIMIT 1"),
                          (change_id,) if change_id else ()).fetchone()
    if change is None:
        raise CommandError("Nothing to undo")
    if change["from_setup_id"] is None:
        raise CommandError("The setup this change came from was deleted")
    latest = conn.execute("SELECT MAX(id) FROM setup_changes WHERE entry_id = ?", (change["entry_id"],)).fetchone()[0]
    if latest != change["id"] or change["on_setup"] != change["to_setup_id"]:
        raise CommandError(f"#{change['number']} has changed setup since “{change['command']}”; undo the later change first")
    conn.execute("UPDATE entries SET setup_id = ? WHERE id = ?", (change["from_setup_id"], change["entry_id"]))
    conn.execute("DELETE FROM setup_changes WHERE id = ?", (change["id"],))
    if change["created_setup"]:
        # Remove the setup the change created, unless something else now relies on it.
        sid = change["to_setup_id"]
        used = conn.execute(
            "SELECT (SELECT COUNT(*) FROM entries WHERE setup_id = ?) + (SELECT COUNT(*) FROM runs WHERE setup_id = ?)"
            " + (SELECT COUNT(*) FROM results WHERE setup_id = ?)"
            " + (SELECT COUNT(*) FROM setup_changes WHERE from_setup_id = ? OR to_setup_id = ?)",
            (sid, sid, sid, sid, sid)).fetchone()[0]
        if not used:
            conn.execute("DELETE FROM setups WHERE id = ?", (sid,))
    back = conn.execute("SELECT name FROM setups WHERE id = ?", (change["from_setup_id"],)).fetchone()
    return Outcome(line="UNDO", kind="undo", driver={"id": change["driver_id"], "name": change["name"], "number": change["number"]},
                   setup_id=change["from_setup_id"], setup_name=back["name"],
                   message=f"Undid “{change['command']}”: #{change['number']} back on {back['name']}")


def run(conn, text, dry_run=False, event_id=None):
    """Run every command in text. With dry_run, report what would happen and change nothing.
    event_id: the race-day event whose entry list decides who each number is."""
    outcomes = []
    for line in split_commands(text):
        try:
            outcomes.extend(apply_line(conn, line, event_id))
        except CommandError as exc:
            outcomes.append(Outcome(line=line, ok=False, kind="error", message=str(exc)))
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return outcomes
