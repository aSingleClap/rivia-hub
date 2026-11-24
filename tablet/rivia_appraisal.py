# rivia_appraisal.py: what just happened, and how far it would move her (emotion spec, Step 3.1 to 3.5).
# DRAFT, Nov 24, not installed. Shadow only: every push here lands on a would-be copy of her mood kept in body.db.
# Her real mood (emotion.db) is only ever read, never written, and nothing here reaches her replies.
#
# Jordan's decisions (Nov 24, relayed):
#   "Split": the message read and the mood push run on the tablet, inside the Flash call Step 7 already makes for each
#   typed message (no extra call, no wait; the push lands in time for her NEXT reply). Rewording runs separately
#   (rivia_expression).
#   "The longer list": the event menu below.
#   Starting numbers: per-message limit 0.3 per mood number on the fast (emotion) layer; the slow (mood) layer soaks
#   10% of each push; strength 1/2/3 = a third / two thirds / the whole push; the biggest event at full strength equals
#   the limit; cursing at her and a thumbs down are a third of the biggest; her question counts as unanswered after
#   6 h, not during a calendar HMC shift; Flash over its Layer 5 limit = message events skipped, code events still count.
#   Carried rules: no fear anywhere (she can be distressed or annoyed); annoyed, never disgust; anger at problems,
#   never at Jordan; "The problems shouldnt hit her twice" / "Count it once".
#
# Pieces in this file:
#   3.1 event menu        code-detected: tool failed, tool worked, thumbs up, thumbs down, plan held, plan missed (hers
#                         only), her question went unanswered. Flash-picked from his typed message: thanked her,
#                         corrected her, frustrated at something, cursed at her (a glitch, never an attack; she never
#                         brings it up), personal disclosure, good news, bad news.
#   3.2 preset pushes     each event has a fixed direction (which way each mood number moves) and a size (a share of the
#                         limit). Strength 1-3 scales it. Flash only ever picks the event and strength, never numbers.
#   3.3 limit + mood soak all pushes from one message (or one tick of code events) are added, then each number is held
#                         to the limit. The slow layer takes a share. Decay is Step 2's own maths (rivia_emotion.decayed).
#   3.4 message read      events_from(): the "events" Step 7's Flash answer now carries. Hooked from rivia_reading_flash.
#   3.5 caution flags     Step 4's dials (double-check, pause, blind spots) and Step 6's area cautions, gathered into the
#                         one line an answering model would get. Logged only; for_answer() returns "" in shadow.
#
# Writes body.db only (tables appraisal_*). Never emotion.db (refused by _guard), never core.db (read-only), never a
# sign-in file. Never an MCP tool, never on a door Gemini can reach. His message text is never seen here: only the
# event names Flash returned.
import json
import math
import os
import sqlite3
import sys
import time

import rivia_reading as reading

SHADOW = True  # True: would-be mood and logs only. Going live means pushes through rivia_emotion.nudge(door="code").

# ---------- starting numbers (all adjustable in the Layer 3 card, rivia_appraisal_settings) ----------
LIMIT = 0.30            # 3.3 most any one message (or tick) can move each mood number on the fast layer
MOOD_SOAK = 0.10        # 3.3 the slow layer takes this share of each push
SHARE_1 = 1.0 / 3       # 3.2 strength 1 = this share of the event's push
SHARE_2 = 2.0 / 3       # 3.2 strength 2 (strength 3 is always the whole push)
UNANSWERED_HOURS = 6    # 3.1 her question counts as unanswered after this long (never during a work shift)
MESSAGE_EVENTS_ON = 1   # 3.4 Flash-picked events on (1) or off (0); code events always run
MAX_EVENTS = 3          # 3.4 at most this many events taken from one message
TONE_CAP = 3            # 3.7 outward strength never above this for an unpleasant mood: 1 faint, 2 mild, 3 moderate,
                        #     4 strong (her inner would-be mood can be stronger)
KEEP_DAYS = 120         # appraisal log rows kept this long

# 3.1 + 3.2: the event menu. direction = how each number moves (v pleasant, a energy, d in control), the biggest part
# is 1. size = share of the limit at full strength (strength 3). Fixed directions; sizes are on the card.
# No direction ever lowers "in control" while raising energy and lowering pleasantness: that corner is fear.
EVENTS = {
    # code-detected
    "tool_failed":   {"kind": "code", "dir": (-1.0, 0.75, 0.25), "size": 2 / 3,
                      "words": "a tool of hers failed (annoyed at the problem)"},
    "tool_worked":   {"kind": "code", "dir": (0.5, 0.0, 1.0), "size": 0.2, "words": "her tools worked"},
    "thumbs_up":     {"kind": "code", "dir": (1.0, 0.25, 0.5), "size": 2 / 3, "words": "you gave a thumbs up"},
    "thumbs_down":   {"kind": "code", "dir": (-1.0, 0.5, 0.0), "size": 1 / 3, "words": "you gave a thumbs down"},
    "plan_held":     {"kind": "code", "dir": (1.0, 0.0, 1.0), "size": 0.5, "words": "her plan held"},
    "plan_missed":   {"kind": "code", "dir": (-1.0, 0.5, 0.0), "size": 0.5, "words": "her plan missed (her part)"},
    "unanswered":    {"kind": "code", "dir": (-1.0, -0.5, 0.0), "size": 1 / 6,
                      "words": "her question went unanswered"},
    # Flash-picked from his typed message
    "thanked":       {"kind": "message", "dir": (1.0, 0.2, 0.4), "size": 0.8, "words": "you thanked her"},
    "corrected":     {"kind": "message", "dir": (-1.0, 0.5, 0.0), "size": 0.5, "words": "you corrected her"},
    "frustrated":    {"kind": "message", "dir": (-1.0, 0.75, 0.25), "size": 1 / 3,
                      "words": "you sounded frustrated at something"},
    "cursed_at_her": {"kind": "message", "dir": (-1.0, 0.25, 0.0), "size": 1 / 3,
                      "words": "rough words at her (a glitch, never brought up)"},
    "personal":      {"kind": "message", "dir": (1.0, 0.5, 0.0), "size": 1 / 3, "words": "you shared something personal"},
    "good_news":     {"kind": "message", "dir": (1.0, 0.5, 0.15), "size": 1.0, "words": "good news"},
    "bad_news":      {"kind": "message", "dir": (-1.0, -0.25, 0.0), "size": 2 / 3, "words": "bad news"},
}
MESSAGE_EVENTS = tuple(n for n, e in EVENTS.items() if e["kind"] == "message")
CODE_EVENTS = tuple(n for n, e in EVENTS.items() if e["kind"] == "code")
# Step 6's words copy and Flash can hear the same thing in one message: count it once.
SAME_AS_WORDS = {"thanked": 1.0, "corrected": -1.0}

# What Flash is told (added to Step 7's rater, same call). It only picks names and strengths.
FLASH_EVENTS = (
    "\nAlso list what this message is, from this menu only, with a strength 1 (slight) to 3 (clear):\n"
    "thanked: he thanks or praises the assistant.\n"
    "corrected: he says the assistant got something wrong.\n"
    "frustrated: he sounds frustrated or annoyed at something (not at the assistant).\n"
    "cursed_at_her: he swears at or insults the assistant itself.\n"
    "personal: he shares something personal about his life or feelings.\n"
    "good_news: he shares good news.\n"
    "bad_news: he shares bad news.\n"
    "Leave events empty when none clearly fits. Swearing about a situation is not cursed_at_her.\n"
)
FLASH_EVENTS_SCHEMA = {
    "type": "array", "maxItems": MAX_EVENTS,
    "items": {"type": "object",
              "properties": {"event": {"type": "string", "enum": list(MESSAGE_EVENTS)},
                             "strength": {"type": "integer", "minimum": 1, "maximum": 3}},
              "required": ["event", "strength"]},
}

TONE_WORDS = {1: "faint", 2: "mild", 3: "moderate", 4: "strong"}
TONE_LEVEL = {"neutral": 0, "faint": 1, "mild": 2, "moderate": 3, "strong": 4}

SCHEMA = """
create table if not exists appraisal_events(
    id integer primary key autoincrement, at real not null, day text not null, name text not null,
    kind text not null, strength integer not null, ref text not null unique, message_id text,
    counted integer not null, why text);
create index if not exists appraisal_events_at on appraisal_events(at);
create table if not exists appraisal_state(
    layer text primary key, v real not null, a real not null, d real not null, updated real not null);
create table if not exists appraisal_meta(key text primary key, value text not null);
create table if not exists appraisal_log(
    id integer primary key autoincrement, at real not null, day text not null, kind text not null,
    message_id text, row text not null);
create index if not exists appraisal_log_at on appraisal_log(at);
"""
LOG_KINDS = ("push", "skip", "caution", "sync", "not_counted")
CODE_DOOR = "code"   # every other door that changed her mood is a hand change (your app button, admin)


class AppraisalError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _guard(con):
    for _, name, path in con.execute("pragma database_list").fetchall():
        if name == "main" and path and path.replace("\\", "/").rsplit("/", 1)[-1] == "emotion.db":
            raise AppraisalError("wrong_db", "Step 3 shadow rows never go in emotion.db")


def prepare(con):
    _guard(con)
    con.executescript(SCHEMA)
    return con


def code_tune():
    t = {"limit": LIMIT, "mood_soak": MOOD_SOAK, "share_1": SHARE_1, "share_2": SHARE_2,
         "unanswered_hours": UNANSWERED_HOURS, "message_events_on": MESSAGE_EVENTS_ON, "tone_cap": TONE_CAP}
    for n, e in EVENTS.items():
        t["size_" + n] = e["size"]
    return t


def _tune(values=None):
    """The starting numbers, or Jordan's edits from the Layer 3 card. Never raises."""
    if values is not None:
        return values
    try:
        import rivia_appraisal_settings as aps
        return aps.tune()
    except Exception as e:  # noqa: BLE001  (a broken settings file must not stop Step 3)
        print("rivia: appraisal settings unreadable (%s); using the code's numbers." % type(e).__name__,
              file=sys.stderr)
        return code_tune()


def _meta(con, key, default=None):
    r = con.execute("select value from appraisal_meta where key = ?", (key,)).fetchone()
    return r[0] if r else default


def _set_meta(con, key, value):
    con.execute("insert or replace into appraisal_meta(key, value) values(?,?)", (key, str(value)))


def _log(con, kind, at, row, message_id=None):
    con.execute("insert into appraisal_log(at, day, kind, message_id, row) values(?,?,?,?,?)",
                (at, reading._day(at), kind, message_id, json.dumps(row, sort_keys=True)))


def log_rows(con, since, kinds=None):
    out = []
    for i, at, kind, mid, row in con.execute("select id, at, kind, message_id, row from appraisal_log where at >= ? "
                                             "order by id", (since,)).fetchall():
        if kinds and kind not in kinds:
            continue
        out.append({"id": i, "at": at, "kind": kind, "message_id": mid, **json.loads(row)})
    return out


# ---------- 3.2 preset pushes ----------

def share(strength, t):
    return {1: t["share_1"], 2: t["share_2"], 3: 1.0}[int(strength)]


def push_for(name, strength, t):
    """One event's push (dv, da, dd) before the per-message limit."""
    e = EVENTS[name]
    k = t["size_" + name] * t["limit"] * share(strength, t)
    return tuple(round(x * k, 6) for x in e["dir"])


def no_fear(direction):
    """The fear corner: less pleasant, more energy, less in control at once. No event may point there."""
    dv, da, dd = direction
    return not (dv < 0 and da > 0 and dd < 0)


# ---------- 3.3 the would-be mood ----------

def live_rows(rivia_dir=None):
    """Her real layers as stored, read-only (never created, never migrated). Zeros if emotion.db isn't there."""
    import rivia_store as store
    path = os.path.join(rivia_dir, "emotion.db") if rivia_dir else store.db_path("emotion")
    zero = {"v": 0.0, "a": 0.0, "d": 0.0, "updated": 0}
    if not os.path.exists(path):
        return {"emotion": dict(zero), "mood": dict(zero), "baseline": dict(zero)}, None
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
    try:
        rows = {r[0]: {"v": r[1], "a": r[2], "d": r[3], "updated": r[4]} for r in con.execute(
            "select layer, v, a, d, updated from state where layer in ('emotion','mood','baseline')")}
        try:
            hand = con.execute("select max(id) from change_log where door != ?", (CODE_DOOR,)).fetchone()[0]
        except sqlite3.Error:
            hand = None
    finally:
        con.close()
    for layer in ("emotion", "mood", "baseline"):
        rows.setdefault(layer, dict(zero))
    return rows, hand


def _decayed(rows, now):
    import rivia_emotion as emo
    return emo.decayed(rows, now)


def would_be(con, now, live=None):
    """The would-be emotion and mood now, decay included, against her real baseline. The copy starts from her real
    mood, and starts again from it whenever Jordan (or admin) changes her mood by hand, so a hand change is never
    fought by an old shadow push. Returns ({layer: {v, a, d, updated}}, synced)."""
    rows, hand = live if live is not None else live_rows()
    mine = {r[0]: {"v": r[1], "a": r[2], "d": r[3], "updated": r[4]} for r in con.execute(
        "select layer, v, a, d, updated from appraisal_state")}
    last_hand = _meta(con, "hand_id")
    synced = False
    if "emotion" not in mine or "mood" not in mine or (hand is not None and str(hand) != last_hand):
        now_live = _decayed(rows, now)
        mine = {layer: dict(now_live[layer], updated=now) for layer in ("emotion", "mood")}
        _set_meta(con, "hand_id", "" if hand is None else hand)
        synced = True
    full = {"emotion": mine["emotion"], "mood": mine["mood"], "baseline": rows["baseline"]}
    return _decayed(full, now), synced


def _save(con, state, now):
    for layer in ("emotion", "mood"):
        s = state[layer]
        con.execute("insert or replace into appraisal_state(layer, v, a, d, updated) values(?,?,?,?,?)",
                    (layer, s["v"], s["a"], s["d"], now))


def _clamp(x, lim):
    return max(-lim, min(lim, x))


def combine(pushes, t):
    """Adds every push from one message (or tick), holds each number to the limit. Returns (capped, raw, limited)."""
    raw = [round(sum(p[i] for p in pushes), 6) for i in range(3)]
    capped = [round(_clamp(x, t["limit"]), 6) for x in raw]
    return tuple(capped), tuple(raw), capped != raw


def outward(state, t):
    """3.7's cap on how her would-be mood would show: an unpleasant mood never shows stronger than the tone cap,
    whatever she'd feel inside. Never a fear word (rivia_emotion never names one)."""
    import rivia_emotion as emo
    e, b = state["emotion"], state["baseline"]
    lab = emo.label(e["v"], e["a"], e["d"], rest=(b["v"], b["a"], b["d"]))
    inner = lab["intensity"]
    shown = inner
    if e["v"] < b["v"] and TONE_LEVEL.get(inner, 0) > int(t["tone_cap"]):
        shown = TONE_WORDS[int(t["tone_cap"])]
    words = "neutral" if lab["name"] == "neutral" else "%s %s" % (shown, lab["name"])
    return {"name": lab["name"], "inner": inner, "shown": shown, "words": words, "capped": shown != inner}


def apply(con, events, now, t, message_id=None, live=None, cause="message"):
    """3.3: adds the counted events' pushes to the would-be mood. events: [{"name", "strength", ...}]."""
    counted = [ev for ev in events if ev.get("counted", True)]
    if not counted:
        return None
    state, synced = would_be(con, now, live)
    if synced:
        _log(con, "sync", now, {"why": "started from her real mood (first push, or her mood was changed by hand)"},
             message_id)
    pushes = [push_for(ev["name"], ev["strength"], t) for ev in counted]
    capped, raw, limited = combine(pushes, t)
    before = outward(state, t)
    new = {layer: dict(state[layer]) for layer in state}
    for i, k in enumerate("vad"):
        new["emotion"][k] = round(max(-1.0, min(1.0, state["emotion"][k] + capped[i])), 6)
        new["mood"][k] = round(max(-1.0, min(1.0, state["mood"][k] + capped[i] * t["mood_soak"])), 6)
    _save(con, new, now)
    after = outward(new, t)
    row = {"cause": cause, "events": [{"name": ev["name"], "strength": ev["strength"]} for ev in counted],
           "push": list(capped), "raw": list(raw), "limited": limited, "before": before["words"],
           "after": after["words"], "shown_capped": after["capped"],
           "emotion": [new["emotion"][k] for k in "vad"], "mood": [new["mood"][k] for k in "vad"], "shadow": SHADOW}
    _log(con, "push", now, row, message_id)
    return row


def _record(con, now, name, strength, ref, message_id=None, counted=True, why=None, at=None):
    """One event row. ref is unique, so the same thing is never counted twice. Returns False if already seen."""
    at = now if at is None else at
    try:
        con.execute("insert into appraisal_events(at, day, name, kind, strength, ref, message_id, counted, why) "
                    "values(?,?,?,?,?,?,?,?,?)", (at, reading._day(at), name, EVENTS[name]["kind"], int(strength), ref,
                                                  message_id, 1 if counted else 0, why))
    except sqlite3.IntegrityError:
        return False
    return True


# ---------- 3.4 the message read (Flash, inside Step 7's call) ----------

def events_from(text):
    """The events Flash listed. Unknown names, bad strengths and repeats are dropped (never guessed)."""
    try:
        import re
        m = re.search(r"\{.*\}", text or "", re.S)
        obj = json.loads(m.group(0) if m else text)
        items = obj.get("events") or []
    except Exception:  # noqa: BLE001
        return []
    out, seen = [], set()
    if not isinstance(items, list):
        return []
    for it in items:
        if not isinstance(it, dict):
            continue
        name, s = it.get("event"), it.get("strength")
        if name not in MESSAGE_EVENTS or name in seen or isinstance(s, bool) or not isinstance(s, int) \
                or not 1 <= s <= 3:
            continue
        seen.add(name)
        out.append({"name": name, "strength": s})
        if len(out) >= MAX_EVENTS:
            break
    return out


def _words_feedback(con, message_id):
    """Step 6's reading of the same message (good/wrong), if it heard one. Never raises."""
    try:
        r = con.execute("select value from trust_feedback where source = 'words' and message_id = ? "
                        "order by id desc limit 1", (message_id,)).fetchone()
        return r[0] if r else None
    except sqlite3.Error:
        return None


def from_flash(con, message_id, status, raw_text, now=None, tune=None, live=None):
    """Called by rivia_reading_flash after Step 7's Flash read. Never raises (Step 7 must never fail on Step 3)."""
    now = time.time() if now is None else now
    try:
        t = _tune(tune)
        prepare(con)
        m = reading.message(con, message_id)
        at = m["at"] if m else now
        if not int(t["message_events_on"]):
            _log(con, "skip", now, {"why": "message events are switched off on the Layer 3 card"}, message_id)
            return None
        if status != "ok":
            why = {"at_limit": "3.8 Flash was at your Layer 5 limit", "no_key": "no Gemini key on the tablet",
                   "off": "the Flash read is off on the Layer 7 card", "failed": "the Flash call failed",
                   "unusable": "Flash's answer couldn't be used"}.get(status, status)
            _log(con, "skip", now, {"why": why + ": message events skipped, code events still count"}, message_id)
            return None
        evs = events_from(raw_text)
        heard = _words_feedback(con, message_id)
        for ev in evs:
            dup = ev["name"] in SAME_AS_WORDS and heard == SAME_AS_WORDS[ev["name"]]
            ev["counted"] = not dup
            fresh = _record(con, now, ev["name"], ev["strength"], "msg:%s:%s" % (message_id, ev["name"]), message_id,
                            counted=not dup, why="Step 6 already heard this in your words" if dup else None, at=at)
            if not fresh:
                ev["counted"] = False
            if dup:
                _log(con, "not_counted", now, {"name": ev["name"], "why": "counted once: Step 6 heard it too"},
                     message_id)
        row = apply(con, evs, now, t, message_id=message_id, live=live) if evs else None
        caution(con, now, message_id)
        return row
    except Exception as e:  # noqa: BLE001
        print("rivia: Step 3 skipped a message's events (%s)" % type(e).__name__, file=sys.stderr)
        return None


# ---------- code-detected events (one tick of the body collector) ----------

def _core_calls(since_iso, now_iso, rivia_dir=None):
    """Her own tool calls in (since, now], read-only from core.db. The app's own routes (app_*) and her health
    lookup are left out: they aren't her doing work. Only failures that are hers (Step 4's body codes) count."""
    import rivia_body_signals as bs
    path = os.path.join(rivia_dir or bs.RIVIA_DIR, "core.db")
    if not os.path.exists(path):
        return []
    try:
        c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
        try:
            rows = c.execute("select ok, code, tool from calls where ts > ? and ts <= ?",
                             (since_iso, now_iso)).fetchall()
        finally:
            c.close()
    except sqlite3.Error:
        return []
    return [(ok, code) for ok, code, tool in rows if not str(tool).startswith("app_") and tool != bs.LOOKUP_TOOL
            and (ok or (code or "internal") in bs.BODY_CODES)]


def _shift_at(con, at):
    """True if a stored calendar shift covers `at`, False if the day is known and it doesn't, None if unknown.
    Uses only what Step 7 already stored (no calendar read from the timer job)."""
    days = {reading._day(at), reading._day(at - 86400)}
    found = False
    for day in days:
        r = con.execute("select shifts, ok from reading_shifts where day = ?", (day,)).fetchone()
        if r is None or not r[1]:
            if day == reading._day(at):
                return None
            continue
        found = True
        for s, e in json.loads(r[0]):
            if s <= at < e:
                return True
    return False if found else None


def _unanswered(con, now, t):
    """Her replies that asked him something and got nothing back in time. Needs her replies copied to the tablet
    (rivia_expression, the app's new reply copy). Each reply is judged once."""
    try:
        rows = con.execute("select message_id, at from expression_replies where asks = 1 and judged = 0 and at <= ?",
                           (now - t["unanswered_hours"] * 3600,)).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for mid, at in rows:
        end = at + t["unanswered_hours"] * 3600
        answered = con.execute("select 1 from reading_messages where at > ? and at <= ? limit 1",
                               (at, end)).fetchone() is not None
        shift = _shift_at(con, end)
        verdict = "answered" if answered else ("at_work" if shift else ("unknown" if shift is None else "unanswered"))
        con.execute("update expression_replies set judged = 1, asked_result = ? where message_id = ?", (verdict, mid))
        if verdict == "unanswered":
            out.append(mid)
    return out


def tick(con, now=None, tune=None, live=None, rivia_dir=None):
    """Code-detected events since the last tick, all added as one push (so a busy minute can't stack past the
    limit). Runs inside the body collector after Step 6. Never raises; returns a short summary."""
    now = time.time() if now is None else now
    out = {"events": [], "push": None}
    try:
        t = _tune(tune)
        prepare(con)
        reading.prepare(con)
        last = _meta(con, "tick_at")
        if last is None:              # first run: start from now, never replay history
            _set_meta(con, "tick_at", now)
            _set_meta(con, "feedback_id", _max(con, "select max(id) from trust_feedback"))
            _set_meta(con, "judged_at", _max(con, "select max(judged_at) from trust_predictions"))
            return out
        last = float(last)
        import rivia_time as rt
        evs = []
        calls = _core_calls(rt.iso(int(last)), rt.iso(int(now)), rivia_dir)
        worked = sum(1 for ok, _ in calls if ok)
        failed = len(calls) - worked
        if failed:
            evs.append({"name": "tool_failed", "strength": min(3, failed), "ref": "calls:%d:f" % int(now)})
        if worked:
            evs.append({"name": "tool_worked", "strength": min(3, worked), "ref": "calls:%d:w" % int(now)})
        fb_last = int(_meta(con, "feedback_id", 0) or 0)
        for fid, source, value, mid in _rows(con, "select id, source, value, message_id from trust_feedback "
                                                  "where id > ? and source in ('thumb','words') order by id",
                                             (fb_last,)):
            fb_last = max(fb_last, fid)
            if source == "thumb":
                evs.append({"name": "thumbs_up" if value > 0 else "thumbs_down", "strength": 3,
                            "ref": "fb:%d" % fid, "message_id": mid})
            else:
                name = "thanked" if value > 0 else "corrected"
                if mid and con.execute("select 1 from appraisal_events where ref = ?",
                                       ("msg:%s:%s" % (mid, name),)).fetchone():
                    _log(con, "not_counted", now, {"name": name, "why": "counted once: Flash heard it too"}, mid)
                    continue
                evs.append({"name": name, "strength": 2, "ref": "fb:%d" % fid, "message_id": mid})
        j_last = float(_meta(con, "judged_at", 0) or 0)
        held = missed = 0
        for pid, result, owner, judged in _rows(con, "select pred_id, result, owner, judged_at from trust_predictions "
                                                     "where judged_at > ? order by judged_at", (j_last,)):
            j_last = max(j_last, judged)
            if result == "hit":
                held += 1
            elif result == "miss" and owner == "rivia":
                missed += 1
            elif result == "miss":
                _log(con, "not_counted", now, {"name": "plan_missed", "why": "the miss was %s's, not hers" % owner})
        if held:
            evs.append({"name": "plan_held", "strength": min(3, held), "ref": "plans:%d:h" % int(now)})
        if missed:
            evs.append({"name": "plan_missed", "strength": min(3, missed), "ref": "plans:%d:m" % int(now)})
        for mid in _unanswered(con, now, t):
            evs.append({"name": "unanswered", "strength": 1, "ref": "ask:%s" % mid, "message_id": mid})
        kept = [ev for ev in evs if _record(con, now, ev["name"], ev["strength"], ev["ref"], ev.get("message_id"))]
        _set_meta(con, "tick_at", now)
        _set_meta(con, "feedback_id", fb_last)
        _set_meta(con, "judged_at", j_last)
        out["events"] = [{"name": e["name"], "strength": e["strength"]} for e in kept]
        if kept:
            out["push"] = apply(con, kept, now, t, live=live, cause="tick")
        con.execute("delete from appraisal_log where at < ?", (now - KEEP_DAYS * 86400,))
        con.execute("delete from appraisal_events where at < ?", (now - KEEP_DAYS * 86400,))
        return out
    except Exception as e:  # noqa: BLE001
        print("rivia: Step 3 tick skipped (%s)" % type(e).__name__, file=sys.stderr)
        out["error"] = type(e).__name__
        return out


def _rows(con, sql, args):
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error:   # a table a later step makes isn't there yet
        return []


def _max(con, sql):
    try:
        v = con.execute(sql).fetchone()[0]
        return 0 if v is None else v
    except sqlite3.Error:
        return 0


def enabled():
    return os.environ.get("RIVIA_APPRAISAL_SHADOW", "1") != "0"


# ---------- 3.5 caution flags ----------

def caution_lines(con):
    """The caution flags an answering model would get: Step 4's dials and Step 6's area cautions. Code-made only."""
    lines = []
    try:
        r = con.execute("select double_check, pause, blind_spots from dials order by at desc limit 1").fetchone()
    except sqlite3.Error:
        r = None
    if r:
        if r[0]:
            lines.append("double-check before acting")
        if r[1]:
            lines.append("pause before anything that can't be undone")
        try:
            spots = json.loads(r[2]) if r[2] else []
        except ValueError:
            spots = []
        for s in spots or []:
            lines.append("blind spot: %s" % s)
    try:
        import rivia_trust_shadow as ts
        st = ts.last_state(con) or {}
        lines += [str(x) for x in st.get("caution") or []]
    except Exception:  # noqa: BLE001  (no Step 6 state yet)
        pass
    seen, out = set(), []
    for ln in lines:
        if ln not in seen:
            seen.add(ln)
            out.append(ln)
    return out


def caution(con, now, message_id=None):
    lines = caution_lines(con)
    if lines:
        _log(con, "caution", now, {"lines": lines, "shadow": SHADOW}, message_id)
    return lines


def for_answer(con=None):
    """The one door the answering model may use for caution flags. "" in shadow and on any error."""
    if SHADOW:
        return ""
    try:
        con, own = reading._open(con)
        try:
            lines = caution_lines(con)
        finally:
            if own:
                con.close()
        return ("Caution (check before answering): " + "; ".join(lines)) if lines else ""
    except Exception:  # noqa: BLE001
        return ""


# ---------- what the Layer 3 card and `rivia appraisal` show ----------

def view(con=None, now=None, tune=None, live=None):
    """Everything Layer 3's card shows about 3.1 to 3.5. Event names, numbers and code's own words; no text of his."""
    now = time.time() if now is None else now
    t = _tune(tune)
    con, own = reading._open(con)
    try:
        prepare(con)
        has_state = con.execute("select count(*) from appraisal_state").fetchone()[0] == 2
        state = would_be(con, now, live)[0] if has_state else None
        pushes = log_rows(con, now - 14 * 86400, ("push",))
        skips = log_rows(con, now - 14 * 86400, ("skip", "not_counted"))
        cautions = log_rows(con, now - 14 * 86400, ("caution",))
        counts = {}
        for name, n in con.execute("select name, count(*) from appraisal_events where at >= ? and counted = 1 "
                                   "group by name", (now - 14 * 86400,)).fetchall():
            counts[name] = n
    finally:
        if own:
            con.close()
    try:
        rows, _ = live if live is not None else live_rows()
        real = outward(_decayed(rows, now), t)["words"]
    except Exception:  # noqa: BLE001
        real = None
    return {
        "shadow": SHADOW,
        "would_be": outward(state, t) if state else None,
        "would_be_numbers": {layer: {k: round(state[layer][k], 3) for k in "vad"} for layer in ("emotion", "mood")}
        if state else None,
        "real_mood": real,
        "note": None if pushes else "no events yet: message events come from the Flash read of your typed messages "
                                    "(Step 7), code events from her tool calls, thumbs and plan checks",
        "recent_pushes": pushes[-10:],
        "event_counts_14d": counts,
        "skips": skips[-10:],
        "caution": cautions[-1]["lines"] if cautions else [],
        "events": [{"name": n, "kind": e["kind"], "words": e["words"]} for n, e in EVENTS.items()],
    }


def main(argv=None):
    v = view()
    print("Step 3 (shadow): what just happened, and how far it would move her")
    if v["note"]:
        print("  " + v["note"])
    print("  would-be mood: %s" % ((v["would_be"] or {}).get("words") or "not started"))
    print("  her real mood: %s" % v["real_mood"])
    for n, c in sorted(v["event_counts_14d"].items()):
        print("  last 14 days: %s x%d" % (n, c))
    if v["caution"]:
        print("  caution: " + "; ".join(v["caution"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
