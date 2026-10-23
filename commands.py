"""Race-day shorthand: '24 SPR 65' sets driver #24's rear sprocket to 65.

    <number> <KEYWORD> <value> [<KEYWORD> <value> ...]

    SPR  rear sprocket      65   (the front is fixed)
    FTW  front track width  1.5
    RTW  rear track width   0.75
    PSI  tire pressure      30   (same on all four tires)

Values can be relative: '24 SPR +1', '9 PSI -0.5'. Separate several commands with ';' or new
lines. 'UNDO' reverts the most recent change.

A change never edits the driver's setup in place, because other drivers may share it. The
driver moves to an existing setup with exactly the new values, or to a new setup if none matches.
"""
import re
from dataclasses import dataclass, field

import db
from db import fmt_num

# Columns that define what a setup physically is. Two setups with equal values here are the same setup.
TUNING = ["front_sprocket", "rear_sprocket", "front_width", "rear_width", "tire", "psi",
          "axle", "caster", "camber", "seat_position", "ride_height", "total_weight", "ballast",
          "carb_jetting", "clutch"]

LABELS = {"front_sprocket": "Front sprocket", "rear_sprocket": "Rear sprocket", "front_width": "Front track width",
          "rear_width": "Rear track width", "psi": "PSI"}


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


def parse(line):
    """'24 SPR 65 PSI 30' -> ('24', [('SPR', '65'), ('PSI', '30')])."""
    tokens = tokenize(line)
    if not tokens:
        raise CommandError("Empty command")
    number = tokens[0].lstrip("#")
    if not number or number.upper() in KEYWORDS:
        raise CommandError("Start with the driver's kart number, e.g. 24 SPR 65")
    rest = tokens[1:]
    if len(rest) % 2:
        hint = rest[-1].upper()
        raise CommandError(f"{hint} needs a value" if hint in KEYWORDS else f"Expected KEYWORD value, got '{rest[-1]}'")
    pairs = []
    for kw, raw in zip(rest[::2], rest[1::2]):
        kw = kw.upper()
        if kw not in KEYWORDS:
            raise CommandError(f"Unknown keyword '{kw}'. Use {', '.join(KEYWORDS)}")
        pairs.append((kw, raw))
    return number, pairs


def new_values(current, pairs):
    """Apply keyword pairs to a setup row. Returns (values, warnings)."""
    values = {c: current[c] for c in TUNING}
    warnings = []
    for kw, raw in pairs:
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


def find_setup(conn, values):
    where = " AND ".join(f"{c} IS ?" for c in TUNING)
    return conn.execute(f"SELECT * FROM setups WHERE {where} ORDER BY id LIMIT 1",
                        [values[c] for c in TUNING]).fetchone()


def apply_line(conn, line):
    if line.strip().upper() == "UNDO":
        return undo(conn)
    number, pairs = parse(line)
    driver = conn.execute("SELECT * FROM drivers WHERE number = ?", (number,)).fetchone()
    if driver is None:
        raise CommandError(f"No driver with number #{number}")
    current = conn.execute("SELECT * FROM setups WHERE id = ?", (driver["setup_id"],)).fetchone()
    out = Outcome(line=line, driver={"id": driver["id"], "name": driver["name"], "number": driver["number"]})
    if not pairs:
        out.kind, out.setup_id, out.setup_name = "noop", current["id"], current["name"]
        out.message = f"#{number} {driver['name']} is on {current['name']}"
        return out

    values, out.warnings = new_values(current, pairs)
    out.changes = [(LABELS[c], fmt_num(current[c]), fmt_num(values[c])) for c in TUNING if values[c] != current[c]]
    if not out.changes:
        out.kind, out.setup_id, out.setup_name = "noop", current["id"], current["name"]
        out.message = f"#{number} already runs those values"
        return out

    match = find_setup(conn, values)
    if match:
        target_id, out.kind, out.setup_name = match["id"], "switch", match["name"]
        out.message = f"#{number} → existing setup {match['name']}"
    else:
        ts = db.now()
        target_id = db.insert(conn, "setups", created_at=ts, updated_at=ts,
                              notes=f"Made from {current['name']} with “{line}”", **values)
        out.kind = "create"
        out.setup_name = conn.execute("SELECT name FROM setups WHERE id = ?", (target_id,)).fetchone()[0]
        out.message = f"#{number} → new setup {out.setup_name}"
    out.setup_id = target_id
    conn.execute("UPDATE drivers SET setup_id = ? WHERE id = ?", (target_id, driver["id"]))
    db.insert(conn, "setup_changes", driver_id=driver["id"], command=line, from_setup_id=current["id"],
              to_setup_id=target_id, created_setup=int(out.kind == "create"))
    return out


def undo(conn, change_id=None):
    sql = "SELECT c.*, d.number, d.name, d.setup_id AS on_setup FROM setup_changes c JOIN drivers d ON d.id = c.driver_id "
    change = conn.execute(sql + ("WHERE c.id = ?" if change_id else "ORDER BY c.id DESC LIMIT 1"),
                          (change_id,) if change_id else ()).fetchone()
    if change is None:
        raise CommandError("Nothing to undo")
    if change["from_setup_id"] is None:
        raise CommandError("The setup this change came from was deleted")
    latest = conn.execute("SELECT MAX(id) FROM setup_changes WHERE driver_id = ?", (change["driver_id"],)).fetchone()[0]
    if latest != change["id"] or change["on_setup"] != change["to_setup_id"]:
        raise CommandError(f"#{change['number']} has changed setup since “{change['command']}”; undo the later change first")
    conn.execute("UPDATE drivers SET setup_id = ? WHERE id = ?", (change["from_setup_id"], change["driver_id"]))
    conn.execute("DELETE FROM setup_changes WHERE id = ?", (change["id"],))
    if change["created_setup"]:
        # Remove the setup the change created, unless something else now relies on it.
        sid = change["to_setup_id"]
        used = conn.execute(
            "SELECT (SELECT COUNT(*) FROM drivers WHERE setup_id = ?) + (SELECT COUNT(*) FROM runs WHERE setup_id = ?)"
            " + (SELECT COUNT(*) FROM results WHERE setup_id = ?)"
            " + (SELECT COUNT(*) FROM setup_changes WHERE from_setup_id = ? OR to_setup_id = ?)",
            (sid, sid, sid, sid, sid)).fetchone()[0]
        if not used:
            conn.execute("DELETE FROM setups WHERE id = ?", (sid,))
    back = conn.execute("SELECT name FROM setups WHERE id = ?", (change["from_setup_id"],)).fetchone()
    return Outcome(line="UNDO", kind="undo", driver={"id": change["driver_id"], "name": change["name"], "number": change["number"]},
                   setup_id=change["from_setup_id"], setup_name=back["name"],
                   message=f"Undid “{change['command']}”: #{change['number']} back on {back['name']}")


def run(conn, text, dry_run=False):
    """Run every command in text. With dry_run, report what would happen and change nothing."""
    outcomes = []
    for line in split_commands(text):
        try:
            outcomes.append(apply_line(conn, line))
        except CommandError as exc:
            outcomes.append(Outcome(line=line, ok=False, kind="error", message=str(exc)))
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return outcomes
