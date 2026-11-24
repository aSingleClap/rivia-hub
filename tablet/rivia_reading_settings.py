# rivia_reading_settings.py: your edits to Step 7's adjustable values, one small card per piece (emotion spec, Step
# 7.11). DRAFT, Nov 24, not installed.
#
# Same pattern as Layers 5 and 6 (rivia_trust_settings):
#   - The code is the source of truth. Every default is read from rivia_reading each time.
#   - Your edits are "changes on top" in reading_overrides.json in the Rivia folder; every saved change is logged
#     (reading_edits.jsonl). Take one off and the code's value is back.
#   - Every change is checked first; a bad one is refused with a plain reason and nothing is saved.
#   - Only your app's button door writes. Never Gemini, never a tool, never Rivia.
#
# Fixed, never on a card, refused if sent (LOCKED): she never writes how she reads you, she asks with what she saw
# and never a diagnosis, she doesn't say "I understand" unless she does, your text isn't stored, only you confirm a
# pattern, report only, and no fear.
import json
import math
import os
import time

import rivia_reading as reading

FILE_NAME = "reading_overrides.json"
LOG_NAME = "reading_edits.jsonl"
LOG_KEEP = 500
VERSION = 1
LAYER = 7
WRITE_DOORS = ("app_button",)
NOTE_MAX = 200

PIECES = {
    "7.1": ("Message facts", "What counts as a clipped message, and whether the Flash read is on."),
    "7.2": ("Situation", "How long \"after an HMC shift\" lasts."),
    "7.3": ("Your normal", "How many messages before a situation's normal counts, and how far back it looks."),
    "7.4": ("How far off", "How many recent messages are compared, and where a bit off and well off start."),
    "7.7": ("Pattern guesses", "How often a repeat has to show up, and how big it has to be, before you see it."),
    "7.8": ("Ask or not", "How many questions a day, how long a brushed-off topic is left alone, and what counts as "
                          "a strong or mixed reading."),
}

SETTINGS = {}


def _add(path, piece, words, typ, default, lo, hi, unit="", step=None):
    SETTINGS[path] = {"piece": piece, "words": words, "type": typ, "default": default, "lo": lo, "hi": hi,
                      "unit": unit, "step": step}


_add("facts.terse_words", "7.1", "A message counts as clipped at or under", "int", lambda: reading.TERSE_WORDS,
     1, 15, "words", 1)
_add("facts.flash_on", "7.1", "Flash read of each message (1 on, 0 off)", "int", lambda: 1, 0, 1, "", 1)
_add("situation.after_shift_hours", "7.2", "\"After an HMC shift\" lasts", "num",
     lambda: reading.AFTER_SHIFT_HOURS, 1.0, 16.0, "hours", 0.5)
_add("normal.min_messages", "7.3", "Messages in a situation before its normal counts", "int",
     lambda: reading.MIN_MESSAGES, 10, 200, "messages", 1)
_add("normal.keep_days", "7.3", "Your normal looks back", "int", lambda: reading.KEEP_DAYS, 14, 180, "days", 1)
_add("off.run_size", "7.4", "Compare your last", "int", lambda: reading.RUN_SIZE, 1, 20, "messages", 1)
_add("off.run_hours", "7.4", "...sent within", "num", lambda: reading.RUN_HOURS, 0.5, 12.0, "hours", 0.5)
_add("off.bit_off", "7.4", "A bit off starts past", "num", lambda: reading.BIT_OFF, 0.5, 3.0,
     "of your usual swings", 0.1)
_add("off.well_off", "7.4", "Well off starts past", "num", lambda: reading.WELL_OFF, 1.0, 5.0,
     "of your usual swings", 0.1)
_add("pattern.days", "7.7", "A pattern has to show on", "int", lambda: reading.PATTERN_DAYS, 3, 60,
     "separate days", 1)
_add("pattern.diff", "7.7", "A pattern has to be at least", "num", lambda: reading.PATTERN_DIFF, 0.10, 1.0,
     "different (0.30 = 30%)", 0.05)
_add("ask.per_day", "7.8", "Questions a day, at most", "int", lambda: reading.ASKS_PER_DAY, 0, 10, "", 1)
_add("ask.shelve_days", "7.8", "A brushed-off topic is left alone for", "int", lambda: reading.SHELVE_DAYS, 1, 60,
     "days", 1)
_add("ask.strong_valence", "7.8", "With little data, only ask when your words read heavier than", "num",
     lambda: reading.STRONG_VALENCE, 0.2, 1.0, "(on -1 to +1)", 0.05)
_add("ask.mixed_flash_min", "7.8", "Mixed signals need Flash to lean at least", "num",
     lambda: reading.MIXED_FLASH_MIN, 0.1, 1.0, "(on -1 to +1)", 0.01)

LOCKED = {
    "she_writes_reading": "She never writes how she reads you, your normal, or a pattern.",
    "observation_not_diagnosis": "A question says what she saw, never what you feel.",
    "no_false_understanding": "She doesn't say \"I understand\" unless she does.",
    "text_not_stored": "Your message text is read and dropped, never stored.",
    "who_confirms": "Only you confirm or reject a pattern.",
    "report_only": "She only reads and reports.",
    "no_fear": "She never shows fear.",
}

# Where each setting lands in rivia_reading's tune() dict.
TUNE_KEYS = {
    "facts.terse_words": "terse_words", "facts.flash_on": "flash_on",
    "situation.after_shift_hours": "after_shift_hours", "normal.min_messages": "min_messages",
    "normal.keep_days": "keep_days", "off.run_size": "run_size", "off.run_hours": "run_hours",
    "off.bit_off": "bit_off", "off.well_off": "well_off", "pattern.days": "pattern_days",
    "pattern.diff": "pattern_diff", "ask.per_day": "asks_per_day", "ask.shelve_days": "shelve_days",
    "ask.strong_valence": "strong_valence", "ask.mixed_flash_min": "mixed_flash_min",
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
            if part in LOCKED:
                raise SettingsError("locked", "%s is a safety limit and can't be changed from the app: %s"
                                    % (p, LOCKED[part]))
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
    if not eff["off.well_off"] > eff["off.bit_off"]:
        raise SettingsError("bad_range", "well off (%s) has to start further out than a bit off (%s)"
                            % (_show(eff["off.well_off"]), _show(eff["off.bit_off"])))
    if eff["normal.keep_days"] < eff["pattern.days"]:
        raise SettingsError("bad_range", "a pattern can't need more days (%s) than your normal looks back (%s)"
                            % (_show(eff["pattern.days"]), _show(eff["normal.keep_days"])))
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


def tune(values=None):
    """What rivia_reading reads."""
    v = values if values is not None else effective()["values"]
    return {TUNE_KEYS[p]: v[p] for p in SETTINGS}


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
    "7.1": ["Your text is read on the tablet and dropped. Only numbers are kept.",
            "With the Flash read on, each message is also sent to Gemini 3.8 Flash on your Google project (not "
            "stored there) and counts on Layer 5's 3.8 Flash graph and limits."],
    "7.2": ["Only shifts titled \"Work Shift: HMC\" on your calendar count. Nothing else on the calendar is kept."],
    "7.3": ["Each situation has its own normal. Until it has enough messages, nothing in it is judged."],
    "7.4": ["Only how pleasant and how charged your words read are judged, plus length, reply speed, clipped "
            "messages and corrections."],
    "7.7": ["A guess changes nothing until you confirm it."],
    "7.8": ["Shadow only: she never actually asks. Would-be questions are logged for you to check."],
}


def view():
    """The adjustable values in the same shape as GET /app/progress and /app/trust."""
    eff = effective()
    return {"layers": [listing()], "changed": eff["changed"], "problems": eff["problems"], "notes": NOTES,
            "locked": [{"name": k, "why": v} for k, v in LOCKED.items()], "history": history()}
