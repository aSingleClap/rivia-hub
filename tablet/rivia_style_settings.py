# rivia_style_settings.py: your edits to Step 8's adjustable values, one small card per piece (emotion spec, Step
# 8.11). DRAFT, Nov 24, not installed.
#
# Same pattern as Layers 5, 6 and 7 (rivia_reading_settings):
#   - The code is the source of truth. Every default is read from rivia_style each time.
#   - Your edits are "changes on top" in style_overrides.json in the Rivia folder; every saved change is logged
#     (style_edits.jsonl). Take one off and the code's value is back.
#   - Every change is checked first; a bad one is refused with a plain reason and nothing is saved.
#   - Only your app's button door writes. Never Gemini, never a tool, never Rivia.
#
# Fixed, never on a card, refused if sent (LOCKED): style never changes whether she pushes back, warns you or says
# no; she never matches your energy; no cursing while you're stressed or cursing; anger and sass aim at the problem,
# never at you; no fear; your text isn't stored; report only.
import json
import math
import os
import time

import rivia_style as style

FILE_NAME = "style_overrides.json"
LOG_NAME = "style_edits.jsonl"
LOG_KEEP = 500
VERSION = 1
LAYER = 8
WRITE_DOORS = ("app_button",)
NOTE_MAX = 200

PIECES = {
    "8.1": ("Your energy", "What counts as rapid-fire and as shouting, and when swearing or caps are just your normal."),
    "8.2": ("Steady, then calmer", "How she eases down when you're worked up, how far, and when she's back to usual."),
    "8.2b": ("Warmth", "How much warmer she can get toward what you're saying."),
    "8.3": ("Sass switch", "How irritated she has to be before light sass turns into dry with no sass."),
    "8.4": ("Strength", "The highest strength each style can reach (0 is the lightest)."),
    "8.6": ("Banter", "Cursing on or off. Off unless you turn it on."),
    "8.7": ("Learning", "How many reactions in a row move a style, how far, and how long \"knock it off\" lasts."),
    "8.8": ("Joke cooldown", "How long before the same bit can come back."),
}

SETTINGS = {}


def _add(path, piece, words, typ, default, lo, hi, unit="", step=None):
    SETTINGS[path] = {"piece": piece, "words": words, "type": typ, "default": default, "lo": lo, "hi": hi,
                      "unit": unit, "step": step}


_add("energy.rapid_count", "8.1", "Rapid-fire is at least", "int", lambda: style.RAPID_COUNT, 2, 10, "messages", 1)
_add("energy.rapid_seconds", "8.1", "...within", "int", lambda: style.RAPID_SECONDS, 30, 600, "seconds", 10)
_add("energy.caps_words", "8.1", "Shouting is at least", "int", lambda: style.CAPS_WORDS, 1, 10,
     "ALL-CAPS words in a message", 1)
_add("energy.usual_share", "8.1", "Swearing or caps count as your normal once they're in at least", "num",
     lambda: style.USUAL_SHARE, 0.05, 1.0, "of your messages there (0.30 = 30%)", 0.05)
_add("ease.energy_step", "8.2", "One energy step is", "num", lambda: style.ENERGY_STEP, 0.10, 0.50,
     "of her arousal (on -1 to +1)", 0.05)
_add("ease.messages", "8.2", "She eases down over", "int", lambda: style.EASE_MESSAGES, 1, 10, "messages", 1)
_add("ease.calm_steps", "8.2", "...to this many steps calmer than her usual", "int", lambda: style.CALM_STEPS, 0, 2,
     "steps", 1)
_add("ease.back_messages", "8.2", "Back to usual after", "int", lambda: style.BACK_MESSAGES, 1, 10,
     "normal messages in a row", 1)
_add("ease.back_minutes", "8.2", "...or after", "int", lambda: style.BACK_MINUTES, 5, 240, "quiet minutes", 5)
_add("warmth.cap", "8.2b", "Warmer by at most", "int", lambda: style.WARMTH_CAP, 0, 1, "step", 1)
_add("sass.switch", "8.3", "Dry with no sass from irritation", "num", lambda: style.SASS_SWITCH, 0.1, 1.0,
     "(0 to 1)", 0.05)
_add("strength.dry", "8.4", "Dry up to", "int", lambda: style.RANGE["dry"], 0, 3, "", 1)
_add("strength.crisp", "8.4", "Crisp up to", "int", lambda: style.RANGE["crisp"], 0, 3, "", 1)
_add("strength.deadpan", "8.4", "Deadpan up to", "int", lambda: style.RANGE["deadpan"], 0, 3, "", 1)
_add("strength.playful", "8.4", "Playful up to", "int", lambda: style.RANGE["playful"], 0, 3, "", 1)
_add("banter.on", "8.6", "Cursing in banter (1 on, 0 off)", "int", lambda: style.BANTER_ON, 0, 1, "", 1)
_add("learn.same", "8.7", "A style moves after", "int", lambda: style.LEARN_SAME, 2, 10,
     "of the same reaction in a row", 1)
_add("learn.max", "8.7", "Learning moves a style at most", "int", lambda: style.LEARN_MAX, 0, 3, "steps", 1)
_add("learn.mute_hours", "8.7", "\"Knock it off\" turns that style off for", "int", lambda: style.MUTE_HOURS, 1, 168,
     "hours", 1)
_add("bits.days", "8.8", "The same bit can't come back for", "int", lambda: style.BIT_DAYS, 1, 60, "days", 1)

LOCKED = {
    "pushback": "Style never changes whether she disagrees, warns you or says no.",
    "caution": "Reactions never touch caution, trust or her mood.",
    "match_energy": "She never matches your energy.",
    "curse_back": "No cursing while you're stressed or cursing.",
    "anger_at_jordan": "Anger and sass aim at the problem, never at you.",
    "no_fear": "She never shows fear.",
    "text_not_stored": "Your message text is read and dropped, never stored.",
    "report_only": "She only reads and reports.",
}

# Where each setting lands in rivia_style's tune() dict.
TUNE_KEYS = {
    "energy.rapid_count": "rapid_count", "energy.rapid_seconds": "rapid_seconds", "energy.caps_words": "caps_words",
    "energy.usual_share": "usual_share", "ease.energy_step": "energy_step", "ease.messages": "ease_messages",
    "ease.calm_steps": "calm_steps", "ease.back_messages": "back_messages", "ease.back_minutes": "back_minutes",
    "warmth.cap": "warmth_cap", "sass.switch": "sass_switch", "strength.dry": "range_dry",
    "strength.crisp": "range_crisp", "strength.deadpan": "range_deadpan", "strength.playful": "range_playful",
    "banter.on": "banter_on", "learn.same": "learn_same", "learn.max": "learn_max",
    "learn.mute_hours": "mute_hours", "bits.days": "bit_days",
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
    # Nothing on Layer 8 has to fit with another value: every range above already holds it safe.
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
    """What rivia_style reads."""
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
    "8.1": ["Your text is read on the tablet and dropped. Only counts are kept (how many swears, caps words).",
            "Worked up is judged against your normal from Step 7. Until a situation has enough messages, only "
            "you saying you're annoyed or stressed counts."],
    "8.2": ["She never rises with you. She holds her own level first, then eases down. 0 calm steps means she "
            "only holds."],
    "8.2b": ["Warmth never means matching your energy or cursing back."],
    "8.3": ["Below the switch: dry with light sass at the problem. Above it: dry, no sass, more focused."],
    "8.4": ["When you're stressed, every style is held to 1 and playful turns plain."],
    "8.6": ["Even on, never while you're stressed or cursing, and only playful or light-sass dry, aimed at things."],
    "8.7": ["Shadow only: her replies aren't styled yet, so nothing is learned. A correction never counts as a "
            "reaction to style."],
    "8.8": ["Shadow only: nothing is used yet, so nothing is cooling down."],
}


def view():
    """The adjustable values in the same shape as GET /app/reading."""
    eff = effective()
    return {"layers": [listing()], "changed": eff["changed"], "problems": eff["problems"], "notes": NOTES,
            "locked": [{"name": k, "why": v} for k, v in LOCKED.items()], "history": history()}
