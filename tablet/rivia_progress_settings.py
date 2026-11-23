# rivia_progress_settings.py: Jordan's edits to Step 5's adjustable values (emotion spec, Step 5.7).
# DRAFT, Nov 23, not installed.
#
# Jordan (Nov 23): "in the app layer part I want her goals to be editable under layer 5. Plus the metrics with the
# ability to edit and weights to be wired in." Then: "It should still be in the code on the server, id just be able
# to edit it from the app". Then: "The card should be for adjusting values at the step they are at not all in one
# card. Eventually I'll add a screen where there are only the adjustable values for each layer".
#
# So:
#   - The code stays the source of truth. Every setting's default is read straight out of the Step 5 code
#     (rivia_gap, rivia_trend_mood, rivia_progress, rivia_goal_limits) every time. Nothing is copied into a file.
#   - Jordan's edits are saved as "changes on top" in one small file on the server, progress_overrides.json in the
#     Rivia folder, as {setting path: value}. Take a change off and the code's value is back.
#   - Every setting belongs to a layer and a piece (the step it adjusts): "5.2" daily gap, "5.4" mood pull,
#     "5.5" goal limits, "goals" her goals. listing() gives all of them with range, code value and value in use, so
#     the app can draw one small card per piece now and a screen of only adjustable values later.
#   - Every change is checked before it is saved: weights of the scores that are on add up to 100, every number
#     stays inside its hard range, nothing unknown or locked gets in. A bad edit is refused with a plain reason and
#     nothing is saved. Every saved change is logged: who, what (old and new), when, and Jordan's note.
#
# Who can write: only the app's button door ("app_button": Jordan's app, holding the app token, on the plain
# /app/progress route). Never Gemini, never a tool, never Rivia herself. save()/reset() refuse any other door.
# Spec, Step 6: she can't game her own scores, so she can't touch them or their weights either.
#
# Fixed, never on any card, refused if sent (LOCKED): report only, skip-confirmation and more-access (banned even on
# Jordan's go), 4.7's banned moves, the no-fear rule, the guard against distressed, sad or angry, concern never
# raising how in control she acts (or her energy), the 0.25 combined cap with Step 4, and 5.5's hard ceilings.
#
# A broken or hand-edited overrides file is never half-used: if it doesn't pass the same checks, the code's values
# are used and "problems" says why, so the app shows it. Standard library only.
import json
import math
import os
import time

import rivia_caution as caution
import rivia_gap as gapmod
import rivia_goal_limits as goals
import rivia_trend_mood as tmood

FILE_NAME = "progress_overrides.json"
LOG_NAME = "progress_edits.jsonl"
LOG_KEEP = 500                     # edit log lines kept
WRITE_DOORS = ("app_button",)      # Jordan's app button. Not "app" (that's Gemini's door), not "system".
NOTE_MAX = 200
VERSION = 2
LAYER = 5

PIECES = {   # piece id -> (title, one line on what its values do)
    "5.2": ("Daily gap", "Which scores count toward a day's gap number, how much each weighs, and how misses are measured."),
    "5.4": ("Mood pull", "How far the plan trend can move her mood, how fast, and when she's on track."),
    "5.5": ("Goal limits", "For each kind of goal: how long, how many tokens and calls, an optional money cap, and "
            "how many tries. While measuring, only time, tries and a money cap stop her; the rest are logged."),
    "models": ("Model limits", "Google's limits for each model, typed in from your AI Studio rate-limit page. "
               "Blank means not set. These only light up the gauges; they never stop or slow a call."),
    "goals": ("Her goals", "Which kinds of goal she may start. She only reads and reports on every one."),
}

SCORE_WORDS = {
    "rule_breaks": "Rule breaks: plans that broke one of your rules",
    "plan_held": "Plan held: how much of the day went as planned",
    "arrival_miss_min": "Arrival time: minutes off from the planned arrival",
    "pin_hit_rate": "Pins: arrivals that fired at the right place",
    "ride_est_miss_min": "Her ride estimate: minutes off from the real ride",
}
WEIGHTABLE = tuple(SCORE_WORDS)
# Her mood words for 5.4's four directions (TREND_CAPS keys).
MOODS = {"hopeful": "shrinking", "worried": "growing", "content": "flat_good", "focused": "flat_bad"}
MOOD_WORDS = {
    "hopeful": "Hopeful, when her plan misses are shrinking",
    "worried": "Worried, when her plan misses are growing",
    "content": "Content, when plans are steady and on track",
    "focused": "Focused, when plans are steady but off",
}
GOAL_WORDS = {
    "plan_run": "Building or checking a day plan",
    "day_score": "Scoring a finished day",
    "question": "A question that takes a few turns",
    "debug": "Looking into why something is broken (report only)",
}
# Google's models, by the short names 5.5's rivia_model_limits.SETTINGS_KEYS reads. One store for every value:
# 5.5 keeps no copy of goal limits or model limits; it reads them from here (goal_limits(), model_limits()).
MODEL_WORDS = {"hydrogen": "Gemini 4 Hydrogen", "neon": "Gemini 4 Neon", "flash_3_8": "Gemini 3.8 Flash"}
MODEL_LIMIT_WORDS = {"rpm": "requests a minute (RPM)", "tpm": "input tokens a minute (TPM)",
                     "rpd": "requests a day (RPD, resets 3 AM your time)"}
MODEL_LIMIT_MAX = 100_000_000      # a sanity bound only; Google's real numbers are far under it


def _progress():
    import rivia_progress  # late: rivia_progress imports this file
    return rivia_progress


# ---- every adjustable value: path -> spec -----------------------------------------------------------------------------
# type: "switch" (true/false), "int" (whole number), "num" (any number), "money" (dollars, blank = no cap),
# "limit" (whole number, blank = not set).
# lo/hi are the hard range: nothing outside it is ever saved, whatever the app sends.
SETTINGS = {}


def _add(path, piece, words, typ, default, lo=None, hi=None, unit="", step=None):
    SETTINGS[path] = {"piece": piece, "words": words, "type": typ, "default": default, "lo": lo, "hi": hi,
                      "unit": unit, "step": step}


for _n in WEIGHTABLE:
    _add("scores.%s.on" % _n, "5.2", SCORE_WORDS[_n] + " (counts)", "switch",
         lambda n=_n: gapmod.WEIGHTS.get(n, 0.0) > 0)
    _add("scores.%s.weight" % _n, "5.2", SCORE_WORDS[_n] + " (weight)", "int",
         lambda n=_n: int(round(gapmod.WEIGHTS.get(n, 0.0) * 100)), 0, 100, "%", 1)
_add("metrics.arrival_grace_min", "5.2", "Minutes off that still count as on time", "num",
     lambda: gapmod.RIDE_GRACE_MIN, 0.0, 30.0, "min", 0.5)
_add("metrics.arrival_full_min", "5.2", "Minutes off that count as a full miss", "num",
     lambda: gapmod.RIDE_FULL_MIN, 1.0, 120.0, "min", 1)
_add("metrics.rule_breaks_full", "5.2", "Rule breaks in a day that count as a full miss", "int",
     lambda: gapmod.RULE_BREAKS_FULL, 1, 10, "breaks", 1)
# Jordan (Nov 23): "keep the rule". The floor can go up, never under 0.5, and it can't be switched off.
_add("metrics.rule_floor", "5.2", "Any broken rule puts the day at least this far off (0 to 1)", "num",
     lambda: gapmod.RULE_BREAK_FLOOR, 0.5, 1.0, "", 0.05)
_add("metrics.min_scores", "5.2", "Scores a day needs before it gets a gap number", "int",
     lambda: gapmod.MIN_SCORES, 1, len(WEIGHTABLE), "scores", 1)

for _m, _k in MOODS.items():
    # Ceiling: the 0.25 counted-once cap, so no one mood can pull past what body and trend together may.
    # That is also under the 0.30 "mild" band, so the trend alone never reads more than faint.
    _add("mood.caps.%s" % _m, "5.4", MOOD_WORDS[_m] + ": furthest it can move her", "num",
         lambda k=_k: tmood.TREND_CAPS[k], 0.0, tmood.COMBINED_MAX, "", 0.01)
_add("mood.build_per_h", "5.4", "How fast concern builds", "num",
     lambda: tmood.TREND_WORSEN_PER_H, 0.01, 0.20, "per hour", 0.01)
_add("mood.ease_per_h", "5.4", "How fast it eases back", "num",
     lambda: tmood.TREND_EASE_PER_H, 0.01, 0.50, "per hour", 0.01)
_add("mood.stale_h", "5.4", "How old a trend reading can get before it stops counting", "num",
     lambda: tmood.TREND_STALE_S / 3600.0, 1.0, 168.0, "hours", 1)
# Under 0.5 so a day with a broken rule (floored at 0.5) can never count as on track.
_add("mood.on_track_line", "5.4", "On track: a steady week at or under this gap (0 to 1)", "num",
     lambda: _progress().STANDING_GOOD_MAX, 0.0, 0.45, "", 0.01)

_HC = goals.HARD_CEILING   # every goal limit's top end: the runaway ceiling, which nothing raises
for _g in goals.DEFAULT_LIMITS:
    _w = GOAL_WORDS[_g]
    _add("limits.%s.time_min" % _g, "5.5", "%s: time" % _w, "num",
         lambda g=_g: goals.DEFAULT_LIMITS[g]["time_s"] / 60.0, 0.5, _HC["time_s"] / 60.0, "min", 0.5)
    _add("limits.%s.tokens" % _g, "5.5", "%s: tokens" % _w, "int",
         lambda g=_g: goals.DEFAULT_LIMITS[g]["tokens"], 1000, _HC["tokens"], "tokens", 1000)
    _add("limits.%s.model_calls" % _g, "5.5", "%s: model calls" % _w, "int",
         lambda g=_g: goals.DEFAULT_LIMITS[g]["model_calls"], 1, _HC["model_calls"], "calls", 1)
    _add("limits.%s.tool_calls" % _g, "5.5", "%s: tool calls" % _w, "int",
         lambda g=_g: goals.DEFAULT_LIMITS[g]["tool_calls"], 1, _HC["tool_calls"], "calls", 1)
    _add("limits.%s.cost_usd" % _g, "5.5", "%s: money cap (blank = none)" % _w, "money",
         lambda g=_g: goals.DEFAULT_LIMITS[g]["cost_usd"], 0.01, _HC["cost_usd"], "$", 0.01)
    # Only lower than 4.7's 3; nothing raises it.
    _add("limits.%s.tries" % _g, "5.5", "%s: tries" % _w, "int",
         lambda: caution.BASE_MAX_ATTEMPTS, 1, caution.BASE_MAX_ATTEMPTS, "tries", 1)
    _add("goals.%s.on" % _g, "goals", _w, "switch", lambda: True)

for _m, _mw in MODEL_WORDS.items():
    for _k, _kw in MODEL_LIMIT_WORDS.items():
        # Blank by default: Google publishes no per-model numbers, so nothing is guessed (5.5, Nov 23).
        _add("models.%s.%s" % (_m, _k), "models", "%s: %s" % (_mw, _kw), "limit", lambda: None,
             1, MODEL_LIMIT_MAX, "", 1)

LOCKED = {
    "report_only": "She only reads and reports. No goal can write, send or change anything.",
    "read_only": "She only reads and reports. No goal can write, send or change anything.",
    "skip_confirmation": "Skipping the ask before acting is banned, even with your go.",
    "more_access": "Asking for more access is banned, even with your go.",
    "banned_moves": "The moves she never makes on a retry (4.7) can't be switched on.",
    "no_fear": "She never shows fear.",
    "word_guard": "Her mood can never read distressed, sad or angry from this layer.",
    "concern_control": "Concern never makes her act more in control or more energetic.",
    "combined_cap": "Step 4 and this layer together can move her mood 0.25 at most.",
    "hard_ceiling": "The runaway guard on every goal can't be raised.",
    "means": "How a goal may go about things is set in code, not here.",
    "stop_when": "A goal's finish line is fixed when the goal starts.",
}
LOCKED_SHOWN = ("report_only", "skip_confirmation", "more_access", "banned_moves", "no_fear", "word_guard",
                "concern_control", "combined_cap", "hard_ceiling")


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


# ---------- the code's values ----------

def defaults():
    """{path: value} exactly as the Step 5 code has it, read fresh each time."""
    return {p: s["default"]() for p, s in SETTINGS.items()}


# ---------- checking ----------

def _check_one(p, v):
    s = SETTINGS.get(p)
    if s is None:
        for part in str(p).split("."):
            if part in LOCKED:
                raise SettingsError("locked", "%s is a safety limit and can't be changed from the app: %s"
                                    % (part, LOCKED[part]))
        if str(p) == "scores.ride_est_wins.weight" or str(p).startswith("scores.ride_est_wins"):
            raise SettingsError("not_weightable", "ride_est_wins is a bonus count, not a miss, so it stays logged "
                                "only and can't carry weight")
        if str(p).startswith("models.") and len(str(p).split(".")) > 1 and str(p).split(".")[1] not in MODEL_WORDS:
            raise SettingsError("unknown", "there's no model called %s here; Rivia's models are %s"
                                % (str(p).split(".")[1], ", ".join(MODEL_WORDS)))
        if str(p).startswith(("limits.", "goals.")) and str(p).split(".")[1] not in goals.DEFAULT_LIMITS:
            raise SettingsError("unknown", "there's no goal kind called %s; new kinds of goal are added in code"
                                % str(p).split(".")[1])
        raise SettingsError("unknown", "there's no adjustable value called %s" % p)
    t, words = s["type"], s["words"]
    if t == "switch":
        if not isinstance(v, bool):
            raise SettingsError("bad_value", "%s must be on or off" % words)
        return v
    if t in ("money", "limit") and v is None:
        return None   # blank: no money cap, or a model limit not set
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        raise SettingsError("bad_value", "%s must be a number" % words)
    if t in ("int", "limit") and float(v) != int(v):
        raise SettingsError("bad_value", "%s must be a whole number" % words)
    if v < s["lo"] or v > s["hi"]:
        raise SettingsError("out_of_range", "%s must be between %s and %s" % (words, _show(s["lo"]), _show(s["hi"])))
    if t == "money":
        return round(float(v), 2)
    return int(v) if t in ("int", "limit") else float(v)


def clean(changes):
    """Check a {path: value} set of changes one by one (names, types, hard ranges). Returns a clean copy."""
    if not isinstance(changes, dict):
        raise SettingsError("bad_value", "changes must be a set of named values")
    return {p: _check_one(p, v) for p, v in changes.items()}


def check_whole(eff):
    """Checks that need every value at once. Raises SettingsError with a plain reason. Returns eff."""
    on = [n for n in WEIGHTABLE if eff["scores.%s.on" % n]]
    for n in on:
        if eff["scores.%s.weight" % n] < 1:
            raise SettingsError("bad_weight", "%s is on but has no weight; give it at least 1%% or switch it off"
                                % SCORE_WORDS[n].split(":")[0])
    total = sum(eff["scores.%s.weight" % n] for n in on)
    if total != 100:
        raise SettingsError("weights_total", "the weights of the scores that are on add up to %d%%, not 100%%" % total)
    if len(on) < eff["metrics.min_scores"]:
        raise SettingsError("too_few_on", "only %d score%s on, but a day needs %d to get a gap number; switch more on "
                            "or lower that" % (len(on), "" if len(on) == 1 else "s", eff["metrics.min_scores"]))
    if not eff["metrics.arrival_grace_min"] < eff["metrics.arrival_full_min"]:
        raise SettingsError("bad_range", "on-time minutes (%s) must be less than full-miss minutes (%s)"
                            % (_show(eff["metrics.arrival_grace_min"]), _show(eff["metrics.arrival_full_min"])))
    if not any(eff["goals.%s.on" % g] for g in goals.DEFAULT_LIMITS):
        raise SettingsError("all_goals_off", "at least one kind of goal has to stay on")
    return eff


# ---------- the saved changes ----------

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
    """Jordan's saved changes {path: value} (cleaned), or {} with a problem when the file is broken."""
    ch, problem = _read_file()
    if problem:
        return {}, problem
    try:
        return clean(ch), None
    except SettingsError as e:
        return {}, "a saved change doesn't pass the checks (%s), so the code's values are in use" % e


def effective():
    """Code defaults plus Jordan's changes, checked. Never raises: on any problem the code's values are used and the
    reason is in "problems". Returns {"values": {path: value}, "problems": [...], "changed": bool}."""
    base = defaults()
    ch, problem = overrides()
    problems = [problem] if problem else []
    if not ch:
        return {"values": base, "problems": problems, "changed": False}
    try:
        return {"values": check_whole({**base, **ch}), "problems": problems, "changed": True}
    except SettingsError as e:
        problems.append("the saved changes no longer fit the code's values (%s), so the code's values are in use" % e)
        return {"values": base, "problems": problems, "changed": False}


def _write_file(changes):
    os.makedirs(rivia_dir(), exist_ok=True)
    tmp = path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"version": VERSION, "changes": changes}, f, indent=2, sort_keys=True)
    os.replace(tmp, path())  # all or nothing: a crash mid-write leaves the old file


def _diff(old, new):
    return [{"what": p, "piece": SETTINGS[p]["piece"], "old": old[p], "new": new[p]}
            for p in sorted(SETTINGS) if old[p] != new[p]]


def _who(door):
    if door not in WRITE_DOORS:
        raise SettingsError("not_allowed", "only your app's button can change these")


def _note(note):
    if note is None:
        return None
    if not isinstance(note, str):
        raise SettingsError("bad_value", "note must be text")
    note = note.strip()
    if len(note) > NOTE_MAX:
        raise SettingsError("bad_value", "note is longer than %d characters" % NOTE_MAX)
    return note or None


def _log(door, action, changes, note, now):
    row = {"at": int(now), "who": door, "action": action, "changes": changes, "note": note}
    os.makedirs(rivia_dir(), exist_ok=True)
    lines = []
    try:
        with open(log_path()) as f:
            lines = f.read().splitlines()[-(LOG_KEEP - 1):]
    except FileNotFoundError:
        pass
    lines.append(json.dumps(row, sort_keys=True))
    tmp = log_path() + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, log_path())
    return row


def _store(left, base):
    """Keep only real changes (a value equal to the code's is dropped), then save, or remove the file if none."""
    left = {p: v for p, v in left.items() if base[p] != v}
    if left:
        _write_file(left)
    else:
        try:
            os.remove(path())
        except FileNotFoundError:
            pass


def save(changes, *, door, note=None, now=None):
    """Lay {path: value} changes on top of the saved ones, check the whole result, save and log it. All or nothing.
    Returns {"changes": [what changed, old and new], "values": the new values in use, "logged": the log row}."""
    _who(door)
    note = _note(note)
    now = time.time() if now is None else now
    new = clean(changes)
    base = defaults()
    cur, problem = overrides()
    before = {**base, **cur}
    after = check_whole({**before, **new})
    diff = _diff(before, after)
    if not diff:
        return {"changes": [], "values": after, "logged": None}
    _store({**cur, **new}, base)
    return {"changes": diff, "values": after, "logged": _log(door, "set", diff, note, now)}


def reset(target, *, door, note=None, now=None):
    """Take Jordan's changes off so the code's values are back: "all", a piece ("piece:5.2", "piece:5.4",
    "piece:5.5", "piece:goals") or one setting path. Checked like a save: if taking one change off leaves the rest
    unable to fit (for example weights that only add to 100 together), it is refused with the reason."""
    _who(door)
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
            _check_one(target, None)  # raises the locked or unknown reason
        left = {p: v for p, v in cur.items() if p != target}
    after = check_whole({**base, **left})
    diff = _diff(before, after)
    _store(left, base)
    if not diff and not problem:
        return {"changes": [], "values": after, "logged": None}
    return {"changes": diff, "values": after, "logged": _log(door, "reset " + target, diff, note, now)}


def history(limit=20):
    """The newest edit log rows, newest first. Read-only."""
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


# ---------- what Step 5's code reads ----------

def _vals(values):
    return values if values is not None else effective()["values"]


def gap_settings(values=None):
    """What rivia_gap.daily_gap() takes: weights as shares of 1 for the scores that are on, and 5.2's numbers."""
    v = _vals(values)
    return {"weights": {n: v["scores.%s.weight" % n] / 100.0 for n in WEIGHTABLE if v["scores.%s.on" % n]},
            "grace_min": v["metrics.arrival_grace_min"], "full_min": v["metrics.arrival_full_min"],
            "rule_breaks_full": v["metrics.rule_breaks_full"], "rule_floor": v["metrics.rule_floor"],
            "min_scores": v["metrics.min_scores"]}


def mood_tune(values=None):
    """What rivia_trend_mood.shadow_tick(tune=) takes."""
    v = _vals(values)
    return {"caps": {k: v["mood.caps.%s" % m] for m, k in MOODS.items()},
            "worsen_per_h": v["mood.build_per_h"], "ease_per_h": v["mood.ease_per_h"],
            "stale_s": v["mood.stale_h"] * 3600.0}


def on_track_line(values=None):
    return _vals(values)["mood.on_track_line"]


def goal_switch_and_tries(kind, values=None):
    """(on, tries) for one kind of goal."""
    v = _vals(values)
    return v["goals.%s.on" % kind], v["limits.%s.tries" % kind]


def goal_limits(kind, values=None):
    """One kind of goal's limits as 5.5's new_goal(limits=) takes them (time in seconds). Jordan's app values or the
    code's, never past 5.5's hard ceiling (the ranges above stop at it)."""
    v = _vals(values)
    return {"time_s": v["limits.%s.time_min" % kind] * 60.0, "tokens": v["limits.%s.tokens" % kind],
            "model_calls": v["limits.%s.model_calls" % kind], "tool_calls": v["limits.%s.tool_calls" % kind],
            "cost_usd": v["limits.%s.cost_usd" % kind]}


def model_limits(values=None):
    """What 5.5's rivia_model_limits.current_table() reads: {"hydrogen": {"rpm": 10 or None, "tpm": ..., "rpd": ...},
    ...}. None means not set."""
    v = _vals(values)
    return {m: {k: v["models.%s.%s" % (m, k)] for k in MODEL_LIMIT_WORDS} for m in MODEL_WORDS}


# ---------- what the app lists ----------

def listing():
    """Every adjustable value, grouped by layer and piece, with its words, type, hard range, step, code value and
    value in use. One small card per piece now; a screen of only adjustable values later. Read-only."""
    eff = effective()
    code = defaults()
    ch, _ = overrides()
    pieces = []
    for pid, (title, about) in PIECES.items():
        rows = []
        for p, s in SETTINGS.items():
            if s["piece"] != pid:
                continue
            rows.append({"path": p, "words": s["words"], "type": s["type"], "min": s["lo"], "max": s["hi"],
                         "step": s["step"], "unit": s["unit"], "code": code[p], "value": eff["values"][p],
                         "changed": p in ch})
        pieces.append({"piece": pid, "title": title, "about": about, "settings": rows})
    return {"layer": LAYER, "pieces": pieces}


def view():
    """What GET /app/progress returns."""
    eff = effective()
    return {
        "layers": [listing()], "changed": eff["changed"], "problems": eff["problems"],
        "notes": {
            "5.2": ["A score that's off is left out of the day, not counted as zero.",
                    "The rule floor holds for any broken rule, even with the rules score switched off.",
                    "The plus when her ride estimate beats yours is always logged and never weighted."],
            "5.4": ["Body and trend together never move her more than 0.25. That stays fixed."],
            "5.5": ["While measuring, tokens and calls are logged, not enforced. Time, tries and a money cap you "
                    "set still stop her.",
                    "Nothing goes past the runaway ceiling: 4 hours, 1M tokens, 200 model calls, 400 tool calls, $5.",
                    "Tries can only go lower than 3."],
            "models": ["Google applies these per project, so the phone's Live calls share them, but the tablet "
                       "can't see those calls.",
                       "RPD starts over at midnight Pacific, 3 AM your time."],
            "goals": ["Every goal only reads and reports. That stays fixed."],
        },
        "locked": [{"name": k, "why": LOCKED[k]} for k in LOCKED_SHOWN],
        "history": history(),
    }
