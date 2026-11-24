# rivia_appraisal_settings.py: your edits to Step 3's adjustable values, one small card per piece (emotion spec, Step
# 3.9, Layer 3 "Reactions"). DRAFT, Nov 24, not installed.
#
# Same pattern as Layers 5 to 8 (rivia_style_settings):
#   - The code is the source of truth. Every default is read from rivia_appraisal / rivia_expression each time.
#   - Your edits are "changes on top" in appraisal_overrides.json in the Rivia folder; every saved change is logged
#     (appraisal_edits.jsonl). Take one off and the code's value is back.
#   - Every change is checked first; a bad one is refused with a plain reason and nothing is saved.
#   - Only your app's button door writes. Never Gemini, never a tool, never Rivia.
#
# Fixed, never on a card, refused if sent (LOCKED): which way each event moves her (only how far is yours); no fear;
# annoyed, never disgust; anger at problems, never at you; Flash picks events, never numbers; plans, times, directions
# and lists are never reworded; the fact check can't be switched off; your text isn't stored; report only.
import json
import math
import os
import time

import rivia_appraisal as appraisal
import rivia_expression as expression

FILE_NAME = "appraisal_overrides.json"
LOG_NAME = "appraisal_edits.jsonl"
LOG_KEEP = 500
VERSION = 1
LAYER = 3
WRITE_DOORS = ("app_button",)
NOTE_MAX = 200

PIECES = {
    "3.1": ("Events on", "Whether events Flash picks from your messages count (her own tool calls, thumbs and plan "
                         "checks always do), and when her question counts as unanswered."),
    "3.2": ("Event sizes", "How big each event's push is at full strength, as a share of the limit (1 = the limit)."),
    "3.2s": ("Strength", "How much of an event's push strength 1 and 2 give (strength 3 is the whole push)."),
    "3.3": ("Limit and mood", "The most one message can move each mood number, and how much carries into her mood."),
    "3.6": ("Rewording", "Which replies are long enough to reword, and whether the shadow rewording runs."),
    "3.7": ("Tone limits", "How strong an unpleasant mood can show, and how much longer a reworded reply may be."),
}

SETTINGS = {}


def _add(path, piece, words, typ, default, lo, hi, unit="", step=None):
    SETTINGS[path] = {"piece": piece, "words": words, "type": typ, "default": default, "lo": lo, "hi": hi,
                      "unit": unit, "step": step}


_add("events.message_on", "3.1", "Events from your messages (1 on, 0 off)", "int",
     lambda: appraisal.MESSAGE_EVENTS_ON, 0, 1, "", 1)
_add("events.unanswered_hours", "3.1", "Her question counts as unanswered after", "num",
     lambda: appraisal.UNANSWERED_HOURS, 1, 48, "hours (never during a work shift)", 0.5)
for _n, _e in appraisal.EVENTS.items():
    _add("size." + _n, "3.2", _e["words"][0].upper() + _e["words"][1:], "num",
         (lambda n=_n: appraisal.EVENTS[n]["size"]), 0.0, 1.0, "of the limit", 0.05)
_add("strength.share_1", "3.2s", "Strength 1 gives", "num", lambda: appraisal.SHARE_1, 0.05, 1.0,
     "of the push", 0.05)
_add("strength.share_2", "3.2s", "Strength 2 gives", "num", lambda: appraisal.SHARE_2, 0.05, 1.0,
     "of the push", 0.05)
_add("limit.per_message", "3.3", "One message moves each mood number at most", "num", lambda: appraisal.LIMIT,
     0.05, 0.6, "(on -1 to +1)", 0.05)
_add("limit.mood_soak", "3.3", "Her slower mood takes", "num", lambda: appraisal.MOOD_SOAK, 0.0, 0.5,
     "of each push (0.10 = 10%)", 0.01)
_add("reword.short_words", "3.6", "Replies under this many words aren't reworded", "int",
     lambda: expression.SHORT_WORDS, 3, 200, "words", 1)
_add("reword.on", "3.6", "Shadow rewording runs (1 on, 0 off; off saves Flash calls)", "int",
     lambda: expression.REWORD_ON, 0, 1, "", 1)
_add("tone.cap", "3.7", "An unpleasant mood shows at most", "int", lambda: appraisal.TONE_CAP, 1, 4,
     "(1 faint, 2 mild, 3 moderate, 4 strong)", 1)
_add("tone.length_max", "3.7", "A reworded reply may be at most", "num", lambda: expression.LENGTH_MAX, 1.0, 3.0,
     "times as long", 0.1)

LOCKED = {
    "direction": "Which way each event moves her is fixed; only how far is yours to set.",
    "no_fear": "She never shows fear (distressed or annoyed instead).",
    "disgust": "Annoyed, never disgust.",
    "anger_at_jordan": "Anger aims at the problem, never at you.",
    "flash_numbers": "Flash only picks events and strength, never numbers.",
    "plans": "Your day plan, times, directions and lists are never reworded.",
    "fact_check": "The fact check always runs; any changed fact means the plain reply.",
    "text_not_stored": "Your message text is read and dropped, never stored.",
    "report_only": "She only reads and reports.",
}

TUNE_KEYS = {"events.message_on": "message_events_on", "events.unanswered_hours": "unanswered_hours",
             "strength.share_1": "share_1", "strength.share_2": "share_2", "limit.per_message": "limit",
             "limit.mood_soak": "mood_soak", "reword.short_words": "short_words", "reword.on": "reword_on",
             "tone.cap": "tone_cap", "tone.length_max": "length_max"}
TUNE_KEYS.update({"size." + n: "size_" + n for n in appraisal.EVENTS})


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
    """Values that have to fit together: strength 1 can't give more than strength 2."""
    if eff["strength.share_1"] > eff["strength.share_2"]:
        raise SettingsError("doesnt_fit", "strength 1 can't give more of the push than strength 2")
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
    """What rivia_appraisal and rivia_expression read."""
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
    "3.1": ["Her own tool calls, your thumbs and the plan checks always count. A plan miss that was yours or "
            "chance's never counts against her mood.",
            "Over the Layer 5 limit for 3.8 Flash, events from your messages are skipped; the others still count.",
            "Her questions are only tracked once the app copies her replies to the tablet."],
    "3.2": ["Cursing at her and a thumbs down start at a third of the biggest event. Rough words count as a glitch, "
            "never an attack, and she never brings them up."],
    "3.3": ["Shadow only: these move a would-be copy of her mood kept on the tablet. Her real mood doesn't move."],
    "3.6": ["Shadow only: the app copies her finished reply after you've seen it. Your chat never shows the reworded "
            "one and never waits for it. Her replies are kept, plain and reworded, with no auto-delete."],
    "3.7": ["She can feel stronger inside than she shows. No fear words, no cursing unless banter allows it (Layer 8), "
            "nothing aimed at you."],
}


def view():
    """The adjustable values in the same shape as GET /app/style."""
    eff = effective()
    return {"layers": [listing()], "changed": eff["changed"], "problems": eff["problems"], "notes": NOTES,
            "locked": [{"name": k, "why": v} for k, v in LOCKED.items()], "history": history()}
