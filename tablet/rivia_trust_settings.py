# rivia_trust_settings.py: your edits to Step 6's adjustable values, one small card per piece (emotion spec, Step
# 6.9). DRAFT.
#
# Jordan (Nov 23): adjustable values "at the step they are at not all in one card", and "It should still be in the
# code on the server, id just be able to edit it from the app". Same pattern as Layer 5 (rivia_progress_settings):
#   - The code is the source of truth. Every default is read from the Step 6 code each time.
#   - Your edits are "changes on top" in trust_overrides.json in the Rivia folder. Take one off and the code's value
#     is back. Every saved change is logged (trust_edits.jsonl): what, old and new, when, your note.
#   - Every change is checked first; a bad one is refused with a plain reason and nothing is saved.
#   - Only your app's button door writes. Never Gemini, never a tool, never Rivia (spec: she can't game her scores,
#     so she can't touch how they're worked out either).
#
# Fixed, never on a card, refused if sent (LOCKED): the hard limits, who can switch an unlock on, the off switch,
# that she can't write trust, that misses only count when they're hers, report only, and no fear.
import json
import math
import os
import time

import rivia_area_confidence as aconf
import rivia_predict as predict
import rivia_satisfaction as sat
import rivia_trust as trust

FILE_NAME = "trust_overrides.json"
LOG_NAME = "trust_edits.jsonl"
LOG_KEEP = 500
VERSION = 1
LAYER = 6
WRITE_DOORS = ("app_button",)
NOTE_MAX = 200

PIECES = {
    "6.2": ("Hit or miss", "How close her plan has to be to count as right."),
    "6.3": ("Your satisfaction", "How much your words and a quiet good day count, and how fast old marks fade."),
    "6.4": ("Sure by area", "How much one bad day in an area costs her, how fast it fades, and the lowest it goes."),
    "6.5": ("Double-check flags", "When an area gets a double-check flag, and when it clears."),
    "6.6": ("Trust", "How much each kind of prediction counts, where trust starts, and how fast old days fade."),
    "6.7": ("Unlock lines", "For each unlock: the trust, satisfaction and days she needs before she can bring it up."),
}

SETTINGS = {}


def _add(path, piece, words, typ, default, lo, hi, unit="", step=None):
    SETTINGS[path] = {"piece": piece, "words": words, "type": typ, "default": default, "lo": lo, "hi": hi,
                      "unit": unit, "step": step}


_add("hit.arrive_min", "6.2", "Arrival counts as right within", "num", lambda: predict.ARRIVE_HIT_MIN, 1.0, 60.0,
     "min", 0.5)
_add("hit.ride_min", "6.2", "Ride estimate counts as right within", "num", lambda: predict.RIDE_HIT_MIN, 1.0, 30.0,
     "min", 0.5)
# Jordan, Nov 23: "Counts half". Capped at 1 (a quiet day can't count more than your thumbs up).
_add("sat.quiet_weight", "6.3", "A quiet day you followed and matched counts as", "num",
     lambda: sat.QUIET_WEIGHT, 0.0, 1.0, "of a thumbs up", 0.05)
_add("sat.words_weight", "6.3", "Your words (\"good plan\", \"that's wrong\") count as", "num",
     lambda: sat.WORDS_WEIGHT, 0.0, 1.0, "of a thumbs up (0 turns them off)", 0.05)
_add("sat.half_life_days", "6.3", "Your old thumbs and quiet days count half after", "num",
     lambda: sat.HALF_LIFE_DAYS, 3.0, 180.0, "days", 1)
_add("area.penalty", "6.4", "One bad day in an area costs", "num", lambda: aconf.PENALTY, 0.01, 0.50, "", 0.01)
_add("area.half_life_days", "6.4", "That cost fades by half after", "num", lambda: aconf.HALF_LIFE_DAYS, 1.0, 30.0,
     "days", 0.5)
_add("area.floor", "6.4", "Lowest an area can read", "num", lambda: aconf.FLOOR, 0.10, 0.90, "", 0.05)
_add("flag.below", "6.5", "Double-check flag under", "num", lambda: aconf.FLAG_BELOW, 0.20, 0.95, "", 0.01)
_add("flag.clear", "6.5", "Flag clears over", "num", lambda: aconf.FLAG_CLEAR, 0.25, 1.0, "", 0.01)
for _k, _w in (("rule", "A plan keeping your rules"), ("arrive", "A right arrival time"),
               ("ride", "A right ride estimate"), ("pin", "A pin that held")):
    _add("trust.hit.%s" % _k, "6.6", "%s earns" % _w, "num", lambda k=_k: trust.KIND_WEIGHTS[k], 0.0, 5.0, "", 0.05)
for _k, _w in (("rule", "A plan breaking a rule"), ("arrive", "An arrival she got wrong"),
               ("ride", "A ride estimate she got wrong"), ("pin", "A pin she got wrong")):
    _add("trust.miss.%s" % _k, "6.6", "%s costs" % _w, "num", lambda k=_k: trust.MISS_WEIGHTS[k], 0.0, 5.0, "", 0.05)
_add("trust.start", "6.6", "Trust starts at", "num",
     lambda: round(trust.PRIOR_GOOD / (trust.PRIOR_GOOD + trust.PRIOR_BAD), 2), 0.05, 0.50, "", 0.05)
_add("trust.half_life_days", "6.6", "Old days count half after", "num", lambda: trust.HALF_LIFE_DAYS, 7.0, 180.0,
     "days", 1)
for _u, _spec in trust.UNLOCKS.items():
    _add("unlock.%s.trust" % _u, "6.7", "%s: trust needed" % _spec["words"], "num",
         lambda u=_u: trust.UNLOCKS[u]["trust"], 0.30, 0.99, "", 0.01)
    _add("unlock.%s.satisfaction" % _u, "6.7", "%s: your satisfaction needed" % _spec["words"], "num",
         lambda u=_u: trust.UNLOCKS[u]["satisfaction"], 0.50, 0.99, "", 0.01)
    _add("unlock.%s.days" % _u, "6.7", "%s: scored days needed" % _spec["words"], "int",
         lambda u=_u: trust.UNLOCKS[u]["days"], 7, 365, "days", 1)

LOCKED = {
    "hard_limits": "Spending money, messaging as you, deleting data, editing herself and root are never unlockable.",
    "who_unlocks": "Only you switch an unlock on. She can only bring it up.",
    "kill_switch": "The off switch stays out of her reach.",
    "she_writes_trust": "She never writes her own trust, satisfaction or confidence.",
    "only_her_misses": "Only misses that were hers count against her.",
    "report_only": "She only reads and reports.",
    "no_fear": "She never shows fear.",
}


class SettingsError(ValueError):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def rivia_dir():
    return os.path.expanduser(os.environ.get("RIVIA_DIR", "~/rivia"))


def path():
    return os.path.join(rivia_dir(), FILE_NAME)


def log_path():
    return os.path.join(rivia_dir(), LOG_NAME)


def _show(x):
    return ("%d" % x) if float(x).is_integer() else ("%g" % x)


def defaults():
    return {p: s["default"]() for p, s in SETTINGS.items()}


def _check_one(p, v):
    s = SETTINGS.get(p)
    if s is None:
        for part in str(p).replace(":", ".").split("."):
            if part in LOCKED or part in trust.HARD_LIMITS:
                why = LOCKED.get(part) or trust.HARD_LIMITS[part]
                raise SettingsError("locked", "%s is a safety limit and can't be changed from the app: %s" % (p, why))
        raise SettingsError("unknown", "there's no adjustable value called %s" % p)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        raise SettingsError("bad_value", "%s must be a number" % s["words"])
    if s["type"] == "int" and float(v) != int(v):
        raise SettingsError("bad_value", "%s must be a whole number" % s["words"])
    if v < s["lo"] or v > s["hi"]:
        raise SettingsError("out_of_range", "%s must be between %s and %s" % (s["words"], _show(s["lo"]),
                                                                              _show(s["hi"])))
    return int(v) if s["type"] == "int" else float(v)


def clean(changes):
    if not isinstance(changes, dict):
        raise SettingsError("bad_value", "changes must be a set of named values")
    return {p: _check_one(p, v) for p, v in changes.items()}


def check_whole(eff):
    if not eff["flag.clear"] > eff["flag.below"]:
        raise SettingsError("bad_range", "the flag has to clear higher (%s) than it starts (%s), or it flickers"
                            % (_show(eff["flag.clear"]), _show(eff["flag.below"])))
    if not eff["area.floor"] < eff["flag.below"]:
        raise SettingsError("bad_range", "the lowest an area can read (%s) has to be under the flag line (%s), or "
                            "no area could ever be flagged" % (_show(eff["area.floor"]), _show(eff["flag.below"])))
    if not any(eff["trust.hit.%s" % k] > 0 for k in trust.KIND_WEIGHTS):
        raise SettingsError("all_zero", "at least one kind of prediction has to count toward trust")
    for u in trust.UNLOCKS:
        if eff["unlock.%s.trust" % u] <= eff["trust.start"]:
            raise SettingsError("bad_range", "an unlock's trust line has to be above where trust starts (%s)"
                                % _show(eff["trust.start"]))
    return eff


def _read_file():
    try:
        with open(path()) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as e:
        return {}, "the saved changes file can't be read (%s), so the code's values are in use" % type(e).__name__
    if not isinstance(raw, dict) or raw.get("version") != VERSION or not isinstance(raw.get("changes"), dict):
        return {}, "the saved changes file isn't in the right shape, so the code's values are in use"
    return raw["changes"], None


def overrides():
    ch, problem = _read_file()
    if problem:
        return {}, problem
    try:
        return clean(ch), None
    except SettingsError as e:
        return {}, "a saved change doesn't pass the checks (%s), so the code's values are in use" % e


def effective():
    """Never raises. {"values", "problems", "changed"}."""
    base = defaults()
    ch, problem = overrides()
    problems = [problem] if problem else []
    if not ch:
        return {"values": base, "problems": problems, "changed": False}
    try:
        return {"values": check_whole({**base, **ch}), "problems": problems, "changed": True}
    except SettingsError as e:
        problems.append("the saved changes no longer fit together (%s), so the code's values are in use" % e)
        return {"values": base, "problems": problems, "changed": False}


def _note(note):
    if note is None:
        return None
    if not isinstance(note, str) or len(note.strip()) > NOTE_MAX:
        raise SettingsError("bad_value", "note must be text up to %d characters" % NOTE_MAX)
    return note.strip() or None


def _log(door, action, diff, note, now):
    row = {"at": int(now), "who": door, "action": action, "changes": diff, "note": note}
    os.makedirs(rivia_dir(), exist_ok=True)
    try:
        with open(log_path()) as f:
            lines = f.read().splitlines()[-(LOG_KEEP - 1):]
    except FileNotFoundError:
        lines = []
    lines.append(json.dumps(row, sort_keys=True))
    with open(log_path() + ".tmp", "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(log_path() + ".tmp", log_path())
    return row


def _store(left, base):
    left = {p: v for p, v in left.items() if base[p] != v}
    if left:
        os.makedirs(rivia_dir(), exist_ok=True)
        with open(path() + ".tmp", "w") as f:
            json.dump({"version": VERSION, "changes": left}, f, indent=2, sort_keys=True)
        os.replace(path() + ".tmp", path())
    else:
        try:
            os.remove(path())
        except FileNotFoundError:
            pass


def _diff(old, new):
    return [{"what": p, "piece": SETTINGS[p]["piece"], "old": old[p], "new": new[p]}
            for p in sorted(SETTINGS) if old[p] != new[p]]


def save(changes, *, door, note=None, now=None):
    if door not in WRITE_DOORS:
        raise SettingsError("not_allowed", "only your app's button can change these")
    note = _note(note)
    now = time.time() if now is None else now
    new = clean(changes)
    base = defaults()
    cur, _ = overrides()
    before = {**base, **cur}
    after = check_whole({**before, **new})
    diff = _diff(before, after)
    if not diff:
        return {"changes": [], "values": after, "logged": None}
    _store({**cur, **new}, base)
    return {"changes": diff, "values": after, "logged": _log(door, "set", diff, note, now)}


def reset(target, *, door, note=None, now=None):
    if door not in WRITE_DOORS:
        raise SettingsError("not_allowed", "only your app's button can change these")
    note = _note(note)
    now = time.time() if now is None else now
    if not isinstance(target, str) or not target:
        raise SettingsError("bad_value", "say what to put back: all, a piece, or one setting")
    base = defaults()
    cur, problem = overrides()
    before = {**base, **cur}
    if target == "all":
        left = {}
    elif target.startswith("piece:"):
        piece = target[len("piece:"):]
        if piece not in PIECES:
            raise SettingsError("unknown", "there's no piece called %s (it takes %s)" % (piece, ", ".join(PIECES)))
        left = {p: v for p, v in cur.items() if SETTINGS[p]["piece"] != piece}
    else:
        if target not in SETTINGS:
            _check_one(target, None)
        left = {p: v for p, v in cur.items() if p != target}
    after = check_whole({**base, **left})
    diff = _diff(before, after)
    _store(left, base)
    if not diff and not problem:
        return {"changes": [], "values": after, "logged": None}
    return {"changes": diff, "values": after, "logged": _log(door, "reset " + target, diff, note, now)}


def history(limit=20):
    try:
        with open(log_path()) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for ln in reversed(lines[-int(limit):]):
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


# ---------- what Step 6's code reads ----------

def _vals(values):
    return values if values is not None else effective()["values"]


def hit_lines(values=None):
    v = _vals(values)
    return {"arrive_hit_min": v["hit.arrive_min"], "ride_hit_min": v["hit.ride_min"]}


def sat_tune(values=None):
    v = _vals(values)
    return {"quiet_weight": v["sat.quiet_weight"], "words_weight": v["sat.words_weight"],
            "half_life_days": v["sat.half_life_days"]}


def area_tune(values=None):
    v = _vals(values)
    return {"penalty": v["area.penalty"], "half_life_days": v["area.half_life_days"], "floor": v["area.floor"],
            "flag_below": v["flag.below"], "flag_clear": v["flag.clear"]}


def trust_tune(values=None):
    v = _vals(values)
    s = v["trust.start"]
    # Keep the code's total prior (5) and move where it starts: start = good / (good + bad).
    total = trust.PRIOR_GOOD + trust.PRIOR_BAD
    return {"weights": {k: v["trust.hit.%s" % k] for k in trust.KIND_WEIGHTS},
            "miss_weights": {k: v["trust.miss.%s" % k] for k in trust.MISS_WEIGHTS},
            "prior_good": s * total, "prior_bad": (1 - s) * total, "half_life_days": v["trust.half_life_days"]}


def unlock_lines(values=None):
    v = _vals(values)
    return {u: {"trust": v["unlock.%s.trust" % u], "satisfaction": v["unlock.%s.satisfaction" % u],
                "days": v["unlock.%s.days" % u]} for u in trust.UNLOCKS}


def listing():
    eff = effective()
    code = defaults()
    ch, _ = overrides()
    pieces = []
    for pid, (title, about) in PIECES.items():
        rows = [{"path": p, "words": s["words"], "type": s["type"], "min": s["lo"], "max": s["hi"], "step": s["step"],
                 "unit": s["unit"], "code": code[p], "value": eff["values"][p], "changed": p in ch}
                for p, s in SETTINGS.items() if s["piece"] == pid]
        pieces.append({"piece": pid, "title": title, "about": about, "settings": rows})
    return {"layer": LAYER, "pieces": pieces}


NOTES = {
    "6.2": ["Gym and work arrivals are skipped until their arrival logs are reliable.",
            "A late arrival caused by her ride estimate is counted once, on the ride."],
    "6.3": ["Your thumbs, your words and quiet days count. Edits never count against her.",
            "Tablet code reads your words for praise or a correction, never Rivia. Unsure means it doesn't count.",
            "A thumb or your words on a day take that day's quiet signal off, so a day never counts twice."],
    "6.4": ["Only misses that were hers count. Outages are chance; your late leaving is yours."],
    "6.5": ["Shadow only: a flag is logged and never reaches her replies yet."],
    "6.6": ["A miss always costs at least as much as a hit earns, by default.",
            "She never writes her own trust."],
    "6.7": ["Only you switch an unlock on. Spending money, messaging as you, deleting data, editing herself and "
            "root are never on the list."],
}


def view():
    """The adjustable values in the same shape as GET /app/progress, so the app's small card (AdjustableValuesCard)
    draws Layer 6's cards with no new parsing."""
    eff = effective()
    return {"layers": [listing()], "changed": eff["changed"], "problems": eff["problems"], "notes": NOTES,
            "locked": [{"name": k, "why": v} for k, v in LOCKED.items()], "history": history()}
