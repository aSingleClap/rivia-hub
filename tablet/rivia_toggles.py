# rivia_toggles.py: the layer and piece switches the tablet itself checks (Jordan, Nov 24: "Toggles first").
#
# Built from server-proposal.md, server-proposal-layer8.md and BUILD.md (Nov 24). The tablet owns the switches, so a
# switch can stop work that runs on the tablet (the minute loop, Steps 5 and 6 scoring, Step 7's read and Flash call,
# Step 8's would-be style), not only hide a card on the phone.
#
#   ~/rivia/toggles.json        only Jordan's choices, on top of the code's defaults (nothing computed is saved)
#   ~/rivia/toggles_edits.jsonl one line per change: who, what, old, new, when, optional note
#
# Rules:
#   - Checked at run time: every switch is read each time its work would run, so OFF takes effect on the next tick or
#     message with no restart. state, why, off_by_parent, off_by_env, waiting and would_also_turn_off are worked out
#     on every read and never saved, so turning a parent back on restores each dependent to Jordan's last setting.
#   - No file = exactly today's behavior. A broken or half-written file falls back to the code's defaults and says so
#     in "problems".
#   - Env flags stay a master off for their own piece only (RIVIA_BODY_COLLECTOR, RIVIA_BODY_SHADOW,
#     RIVIA_PLAN_SHADOW, RIVIA_TRUST_SHADOW, RIVIA_DRIFT_WEEKLY). Changing one still needs a restart.
#   - OFF pauses work. Nothing here deletes or clears a table.
#   - Writes: temp file in the same folder, fsync, os.replace, then append the log line. Only the app's button
#     (door "app_button") can change a switch. Never an MCP tool on any door; the route lives in rivia_toggles_app.
#   - Standard library only at import time: the collector loads this, and its test checks it pulls in no sign-in,
#     tool or emotion-button code. Step 7's reading settings are imported only when the Flash switch is read or set.
import json
import os
import time

VERSION = 1
FILE_NAME = "toggles.json"
LOG_NAME = "toggles_edits.jsonl"
WRITE_DOORS = ("app_button",)
NOTE_MAX = 200

# Pieces in screen order (Layers 1 to 8). Fields: id, layer, step, title, where ("tablet" or "phone"), kind
# ("switch", "phone", "nothing", "always_on"), default (switches only), env (a master-off flag), needs [(id, kind)],
# why (fixed line for a non-switch row, or None).
P = []


def _p(id_, layer, step, title, kind="nothing", needs=(), default=None, env=None, where="tablet", why=None):
    P.append({"id": id_, "layer": layer, "step": step, "title": title, "kind": kind, "needs": list(needs),
              "default": default, "env": env, "where": where, "why": why})


H, S = "hard", "soft"
GUARD = "Always on: a safety rule, never switched off."

# Layer 1: her emotion
_p("l1", 1, None, "Layer 1: Her emotion", "switch", default=True)
_p("l1.1", 1, "1.1", "1.1 Emotion state", needs=[("l1", H)])
_p("l1.2", 1, "1.2", "1.2 Mood limits", "always_on", why=GUARD)
_p("l1.3", 1, "1.3", "1.3 Reference points", needs=[("l1", H)])
_p("l1.4", 1, "1.4", "1.4 Labels", needs=[("l1", H)])
_p("p1.5", 1, "1.5", "1.5 Manual mood dial", "phone", needs=[("l1", H)], where="phone")
_p("l1.6", 1, "1.6", "1.6 Mood line for Gemini", needs=[("l1", H)],
   why="Live: the app gets it from GET /app/emotion/line. Follows Layer 1's switch; going back to shadow is "
       "a hand edit in the code, not a switch in the app.")
_p("l1.7", 1, "1.7", "1.7 Change log", "always_on", why=GUARD)
_p("l1.8", 1, "1.8", "1.8 Acceptance checks", needs=[("l1", H)])

# Layer 2: mood and baseline
_p("l2", 2, None, "Layer 2: Mood and baseline", "switch", needs=[("l1", H)], default=True)
_p("l2.1", 2, "2.1", "2.1 Mood and baseline layers", needs=[("l2", H)])
_p("l2.2", 2, "2.2", "2.2 Fading", needs=[("l2", H)],
   why="Worked out on read from the time since the last change; pausing it would make her jump when it came back.")
_p("l2.3", 2, "2.3", "2.3 Labels against her normal", needs=[("l2", H)])
_p("p2.4", 2, "2.4", "2.4 Mood & baseline controls", "phone", needs=[("l2", H)], where="phone")
_p("l2.5", 2, "2.5", "2.5 Fade log", needs=[("l2", H)])
_p("l2.6", 2, "2.6", "2.6 Baseline research", needs=[("l2", H)])
_p("l2.7", 2, "2.7", "2.7 Drift limits", "always_on", why=GUARD)
_p("l2.8", 2, "2.8", "2.8 Weekly update check", "switch", default=False, env="RIVIA_DRIFT_WEEKLY")
_p("l2.9", 2, "2.9", "2.9 Merge checks", needs=[("l2", H)])

# Layer 3: reactions (Step 3, shadow). l3 gates the code-event tick, the Flash event menu and rewording;
# RIVIA_APPRAISAL_SHADOW stays the env master off for the tick (checked where the tick runs).
_p("l3", 3, None, "Layer 3: Reactions", "switch", default=True)
_p("l3.1", 3, "3.1", "3.1 Events", needs=[("l3", H)],
   why="Code events once a minute (RIVIA_APPRAISAL_SHADOW=0 turns them off); message events ride in the Flash read.")
_p("l3.2", 3, "3.2", "3.2 Set pushes", needs=[("l3", H)])
_p("l3.3", 3, "3.3", "3.3 Limit", needs=[("l3", H)])
_p("l3.4", 3, "3.4", "3.4 Reading your message", needs=[("l3", H), ("l7", H), ("l7.1.flash", H)],
   why="Message events ride in Step 7's Flash read.")
_p("l3.5", 3, "3.5", "3.5 Caution flags", needs=[("l3", H)])
_p("l3.6", 3, "3.6", "3.6 Rewording", "switch", needs=[("l3", H), ("l7.1.flash", H)], default=True)
_p("l3.7", 3, "3.7", "3.7 Tone limits", needs=[("l3.6", H)])
_p("l3.8", 3, "3.8", "3.8 Fact check", "always_on", why=GUARD)
_p("l3.9", 3, "3.9", "3.9 Adjust values", needs=[("l3", H)])

# Layer 4: her body
_p("l4.collect", 4, "4.1", "4.1 to 4.3 Collecting health signals", "switch", default=True,
   env="RIVIA_BODY_COLLECTOR")
_p("l4.2", 4, "4.2", "4.2 Once-a-minute reading", needs=[("l4.collect", H)])
_p("l4.3", 4, "4.3", "4.3 Her normal ranges", needs=[("l4.collect", H)])
_p("l4.dials", 4, "4.4", "4.4 to 4.6 Stress and confidence dials", "switch", needs=[("l4.collect", H)],
   default=True, env="RIVIA_BODY_SHADOW")
_p("l4.5", 4, "4.5", "4.5 Confidence", needs=[("l4.dials", H)])
_p("l4.6", 4, "4.6", "4.6 Body cap", needs=[("l4.dials", H)])
_p("l4.7", 4, "4.7", "4.7 Never push harder", "always_on", why=GUARD)
_p("l4.8", 4, "4.8", "4.8 Health check tool", "switch", needs=[("l4.collect", S)], default=True)
_p("l4.9", 4, "4.9", "4.9 Shadow mode", why="A label: Layer 4 runs in shadow.")

# Layer 5: progress trend
_p("l5", 5, None, "Layer 5: Progress trend", "switch", default=True, env="RIVIA_PLAN_SHADOW")
_p("l5.1", 5, "5.1", "5.1 Plan scores", needs=[("l5", H)])
_p("l5.2", 5, "5.2", "5.2 Daily gap", needs=[("l5", H)], why="Score kinds are switched in Layer 5's own card.")
_p("l5.3", 5, "5.3", "5.3 Trend", needs=[("l5", H)])
_p("l5.4", 5, "5.4", "5.4 Trend mood", needs=[("l5", H)])
_p("l5.5", 5, "5.5", "5.5 Goal limits and hard ceilings", "always_on", why=GUARD)
_p("l5.6", 5, "5.6", "5.6 Shadow mode", why="A label: Layer 5 runs in shadow.")
_p("l5.7", 5, "5.7", "5.7 Adjust values", needs=[("l5", H)])

# Layer 6: trust
_p("l6.1", 6, "6.1", "6.1 Her predictions", needs=[("l6.score", H)])
_p("l6.score", 6, "6.2", "Scoring her days", "switch", needs=[("l5", S)], default=True, env="RIVIA_TRUST_SHADOW")
_p("l6.words", 6, "6.3", "Your words copy", "switch", default=True)
_p("p6.3.thumbs", 6, "6.3", "Thumbs on her replies", "phone", where="phone")
_p("l6.4", 6, "6.4", "6.4 Area confidence", needs=[("l6.score", H)])
_p("l6.5", 6, "6.5", "6.5 Double-check flags", needs=[("l6.score", H)])
_p("l6.6", 6, "6.6", "6.6 Earned trust", needs=[("l6.score", H)])
_p("l6.7", 6, "6.7", "6.7 Unlocks", why="Unlocks are switched by hand in Layer 6's own card.")
_p("l6.8", 6, "6.8", "6.8 Off switch", "always_on", why=GUARD)
_p("l6.9", 6, "6.9", "6.9 Shadow mode", why="A label: Layer 6 runs in shadow.")

# Layer 7: reading you
_p("l7", 7, None, "Layer 7: Reading you", "switch", default=True)
_p("l7.1.flash", 7, "7.1", "7.1 Flash read", "switch", needs=[("l7", H)], default=True)
_p("l7.2", 7, "7.2", "7.2 Situations from your calendar", "switch", needs=[("l7", H)], default=True)
_p("l7.3", 7, "7.3", "7.3 Your normal", needs=[("l7", H)])
_p("l7.4", 7, "7.4", "7.4 Reading against your normal", needs=[("l7", H)])
_p("l7.7", 7, "7.7", "7.7 Pattern guesses", "switch", needs=[("l7", H)], default=True)
_p("l7.8", 7, "7.8", "7.8 Ask, adjust or leave it", "switch", needs=[("l7", H)], default=True)
_p("l7.9", 7, "7.9", "7.9 Second opinion", needs=[("l7.1.flash", H)], why="Part of the Flash read")
_p("l7.10", 7, "7.10", "7.10 Her mood colors her read", why="Waits on Step 3.")
_p("l7.11", 7, "7.11", "7.11 Adjust values", needs=[("l7", H)])

# Layer 8: how she sounds (server-proposal-layer8.md)
_p("l8", 8, None, "Layer 8: How she sounds", "switch", default=True,
   needs=[("l7", H), ("l1", S), ("l2", S), ("l4.collect", S), ("l7.1.flash", S), ("l7.2", S)])
_p("l8.1", 8, "8.1", "8.1 Your energy", needs=[("l8", H)])
_p("l8.2", 8, "8.2", "8.2 Steady, then calmer", needs=[("l8.1", H)])
_p("l8.2b", 8, "8.2b", "8.2b Warmth", needs=[("l8", H)])
_p("l8.3", 8, "8.3", "8.3 Style", needs=[("l8", H)])
_p("l8.4", 8, "8.4", "8.4 Strength", needs=[("l8.3", H)])
_p("l8.6", 8, "8.6", "8.6 Banter", needs=[("l8", H)],
   why="Cursing is a value in this step's Adjust values (off unless you turn it on).")
_p("l8.7", 8, "8.7", "8.7 Learning from your reactions", needs=[("l8", H)],
   why="Nothing to learn until her replies are styled (Step 3).")
_p("l8.8", 8, "8.8", "8.8 Joke cooldown", needs=[("l8", H)], why="Filled only once Step 3 styles her replies.")
_p("l8.9", 8, "8.9", "8.9 Pushback guard", "always_on", why=GUARD)

BY_ID = {p["id"]: p for p in P}
SWITCHES = [p["id"] for p in P if p["kind"] == "switch"]
DEFAULTS = {i: BY_ID[i]["default"] for i in SWITCHES}
FLASH = "l7.1.flash"           # writes through to Step 7's reading setting facts.flash_on (one value, not two)
RUNS = ("on", "waiting")       # states in which a piece's work runs
NOTES = {"l8": "Shadow: logs the style she would use. Her replies don't change until Step 3.",
         "l3": "Shadow: logs events and a would-be mood; her real mood never moves.",
         "l3.6": "Shadow: tries the rewording and keeps it for spot-checks; her real reply always stands."}


class ToggleError(Exception):
    def __init__(self, code, msg, status=400):
        super().__init__(msg)
        self.code = code
        self.status = status


# ---------- where things live ----------

def rivia_dir():
    return os.path.expanduser(os.environ.get("RIVIA_DIR", "~/rivia"))


def path():
    return os.path.join(rivia_dir(), FILE_NAME)


def log_path():
    return os.path.join(rivia_dir(), LOG_NAME)


# ---------- env flags (the same tests the modules use; test_toggles checks they agree) ----------

def _flag_on(name):
    return os.environ.get(name, "1").strip().lower() not in ("0", "false", "off", "no")


def env_allows(name):
    if name is None:
        return True
    if name == "RIVIA_DRIFT_WEEKLY":
        return os.environ.get(name, "") == "1"
    return _flag_on(name)


# ---------- Jordan's choices ----------

def _read_file():
    """({id: bool}, problem or None). Missing file: no choices. Broken file: no choices and a plain problem."""
    try:
        with open(path()) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as e:
        return {}, "the switches file can't be read (%s), so every switch is at the code's default" % type(e).__name__
    if not isinstance(raw, dict) or raw.get("version") != VERSION or not isinstance(raw.get("choices"), dict):
        return {}, "the switches file isn't in the right shape, so every switch is at the code's default"
    ch = raw["choices"]
    if any(k not in DEFAULTS or k == FLASH or not isinstance(v, bool) for k, v in ch.items()):
        return {}, "the switches file has an unknown switch or a value that isn't on/off, so every switch is at " \
                   "the code's default"
    return dict(ch), None


def _flash_value():
    """Step 7's facts.flash_on, the one value behind the 7.1 Flash switch. Never raises: on a problem, today's on."""
    try:
        import rivia_reading_settings as rs
        return bool(rs.effective()["values"].get("facts.flash_on", 1))
    except Exception:  # noqa: BLE001
        return True


def values(choices=None, flash=True):
    """{switch id: Jordan's saved setting or the code's default}. flash=False leaves the Flash switch out (it's read
    from Step 7's settings only when a state actually needs it)."""
    ch = _read_file()[0] if choices is None else choices
    out = {i: ch.get(i, DEFAULTS[i]) for i in SWITCHES if i != FLASH}
    if flash:
        out[FLASH] = _flash_value()
    return out


# ---------- states, worked out on every read ----------

def _title(i):
    return BY_ID[i]["title"]


def states(vals=None, only=None):
    """{id: (state, why)} for every piece, or for `only` and the parents it depends on. Never saved."""
    vals = values(flash=False) if vals is None else vals
    out = {}

    def value(i):
        if i not in vals:
            vals[i] = _flash_value() if i == FLASH else DEFAULTS.get(i, False)
        return vals[i]

    def st(i):
        if i in out:
            return out[i]
        p = BY_ID[i]
        hard = [n for n, k in p["needs"] if k == H]
        soft = [n for n, k in p["needs"] if k == S]
        if p["kind"] == "phone":
            r = ("phone", None)
        elif p["kind"] == "always_on":
            r = ("always_on", p["why"])
        elif p["kind"] == "nothing":
            r = ("nothing_to_switch", p["why"])
        elif not env_allows(p["env"]):
            r = ("off_by_env", "Off by env flag %s; needs a restart" % p["env"])
        else:
            down = [n for n in hard if st(n)[0] not in RUNS]
            if down:
                r = ("off_by_parent", "Off because %s is off" % _title(down[0]))
            elif not value(i):
                r = ("off", None)
            else:
                waiting = [n for n in soft if st(n)[0] not in RUNS]
                if waiting:
                    r = ("waiting", "Waiting: %s is off%s" % (_title(waiting[0]), " (and more)" if len(waiting) > 1
                                                                                      else ""))
                else:
                    r = ("on", NOTES.get(i))
        out[i] = r
        return r

    for p in ([BY_ID[only]] if only else P):
        st(p["id"])
    return out


def runs(piece_id):
    """True when the piece's work should run now. Never raises; on any problem, today's behavior (its default)."""
    try:
        return states(only=piece_id)[piece_id][0] in RUNS
    except Exception:  # noqa: BLE001
        p = BY_ID.get(piece_id)
        return bool(p and p["default"] and env_allows(p["env"]))


def _dependents(i, kind=None):
    return [p["id"] for p in P for n, k in p["needs"] if n == i and (kind is None or k == kind)]


def _would_also(i, sts):
    """Hard dependents that are on now (phone pieces included), all the way down."""
    seen, todo, out = set(), [i], []
    while todo:
        cur = todo.pop(0)
        for d in _dependents(cur, H):
            if d in seen:
                continue
            seen.add(d)
            if sts[d][0] in RUNS or sts[d][0] == "phone":
                out.append(d)
                todo.append(d)
    return out


def view():
    """The full GET /app/toggles body."""
    ch, problem = _read_file()
    vals = values(ch)
    sts = states(vals)
    pieces = []
    for p in P:
        i = p["id"]
        state, why = sts[i]
        switchable = p["kind"] in ("switch", "phone")
        pieces.append({
            "id": i, "layer": p["layer"], "step": p["step"], "title": p["title"], "where": p["where"],
            "switchable": switchable,
            "value": vals[i] if p["kind"] == "switch" else None,
            "state": state,
            "needs": [{"id": n, "kind": k} for n, k in p["needs"]],
            "needed_by": [{"id": q["id"], "kind": k} for q in P for n, k in q["needs"] if n == i],
            "why": why,
            "would_also_turn_off": _would_also(i, sts) if state in RUNS else [],
        })
    return {"ok": True, "version": VERSION, "note": None, "problems": [problem] if problem else [], "pieces": pieces}


# ---------- changing a switch ----------

def _atomic_write(obj):
    """Temp file in the same folder, fsync, os.replace. A crash before the replace leaves the old file whole."""
    os.makedirs(rivia_dir(), exist_ok=True)
    tmp = path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path())
    try:  # make the rename itself durable where the platform allows it
        fd = os.open(rivia_dir(), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _append_log(row):
    with open(log_path(), "a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _note(note):
    if note is None:
        return None
    if not isinstance(note, str):
        raise ToggleError("bad_request", "note must be text")
    note = note.strip()
    if len(note) > NOTE_MAX:
        raise ToggleError("bad_request", "note is longer than %d characters" % NOTE_MAX)
    return note or None


def set_switch(piece_id, on, *, door, note=None, now=None):
    """Turn one switch on or off for Jordan. Raises ToggleError (status 400 or 409) and saves nothing on a refusal."""
    now = time.time() if now is None else now
    if door not in WRITE_DOORS:
        raise ToggleError("not_allowed", "only your app's button can change a switch", 403)
    if not isinstance(piece_id, str) or not isinstance(on, bool):
        raise ToggleError("bad_request", "send id (text) and on (true or false)")
    note = _note(note)
    p = BY_ID.get(piece_id)
    if p is None:
        raise ToggleError("unknown_id", "there's no piece called %s" % piece_id)
    if p["kind"] == "phone":
        raise ToggleError("phone_piece", "%s is switched on the phone, not the tablet" % p["title"])
    if p["kind"] != "switch":
        raise ToggleError("not_switchable", "%s can't be switched" % p["title"])
    if on:
        if not env_allows(p["env"]):
            raise ToggleError("env_off", "Off by env flag %s; needs a restart" % p["env"], 409)
        sts = states(only=piece_id)
        down = [n for n, k in p["needs"] if k == H and sts[n][0] not in RUNS]
        if down:
            raise ToggleError("parent_off", "Turn %s on first" % _title(down[0]), 409)
    if piece_id == FLASH:  # one value: Step 7's own reading setting, logged in its own edits file
        import rivia_reading_settings as rs
        old = _flash_value()
        if old != on:
            rs.save({"facts.flash_on": 1 if on else 0}, door=door, note=note, now=now)
        return {"changes": [] if old == on else [{"what": piece_id, "old": old, "new": on}]}
    ch, problem = _read_file()
    old = ch.get(piece_id, DEFAULTS[piece_id])
    if old == on and not problem:
        return {"changes": []}
    if on == DEFAULTS[piece_id]:
        ch.pop(piece_id, None)
    else:
        ch[piece_id] = on
    _atomic_write({"version": VERSION, "choices": ch})
    row = {"at": int(now), "who": door, "action": "set", "changes": [{"what": piece_id, "old": old, "new": on}],
           "note": note}
    _append_log(row)
    return {"changes": row["changes"]}
