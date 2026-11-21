# rivia_emotion.py: Rivia's emotion state (emotion spec, Step 1.1), in emotion.db.
#
#   emotion.db   state: one row per layer (v, a, d each -1..+1, and when it last changed)
#
# Step 1 has one layer, "emotion", starting at 0, 0, 0. Step 2 adds "mood" and "baseline" as
# more rows in the same table, so it needs no schema change.
#
# _apply() is the only code that changes the state; write() (absolute) and nudge() (deltas, 1.5)
# both go through it. Later Step 1 items hook in there and nowhere else: clamping (1.2) holds each
# axis to -1..+1, and the change log (1.7) writes its row inside the same transaction. Gemini never
# writes this state: _apply() refuses any door but code, admin and app_button, and no MCP tool on any
# door may call it. 1.5's manual control is the phone app's button, a plain HTTP route (/app/emotion in
# rivia_emotion_app.py) that sits behind the app token but outside the MCP tool list, so Gemini never
# sees it. Nothing here imports places, scheduling or core.
# label() (1.4) turns a V, A, D point into a word and an intensity; it only reads.
#
#   emotion.db   change_log (1.7): one row per state change, append-only (triggers refuse update and
#                delete). Old and new V, A, D, the raw requested values (before clamping) plus which axes
#                were clamped, old and new label, cause, note, door and time. Written by _apply() inside
#                the same transaction as the state write, so a change and its row land together or not at
#                all. history() reads it. No pruning yet: it grows by one row per change (Step 3 means up
#                to one per turn), and emotion.db has no Drive backup.
import math

import rivia_store as store
import rivia_time as rt

SCHEMA = """
create table if not exists state(
    layer text primary key, v real not null, a real not null, d real not null, updated text not null);
create table if not exists change_log(
    id integer primary key, at text not null, layer text not null,
    old_v real not null, old_a real not null, old_d real not null,
    new_v real not null, new_a real not null, new_d real not null,
    raw_v real not null, raw_a real not null, raw_d real not null,
    clamped text not null,
    old_label text, new_label text,
    cause text not null, note text, door text not null);
create trigger if not exists change_log_no_update before update on change_log
    begin select raise(abort, 'change_log is append-only'); end;
create trigger if not exists change_log_no_delete before delete on change_log
    begin select raise(abort, 'change_log is append-only'); end;
"""
# change_log.clamped: the clamped axes joined, e.g. "" (none), "v" or "v,a". raw_* are what was asked
# for (for a nudge, old + delta); new_* are what was stored. The triggers make the table append-only;
# a later pruning step has to drop them on purpose.
LAYER = "emotion"
WRITERS = ("code", "admin", "app_button")  # never "app": Gemini's MCP tools use that label.
# door is a label the caller passes, not proof: app_button is passed only by rivia_emotion_app.py.

# 1.3: reference points the label function (1.4) measures against, as (v, a, d), each -1..+1.
# Russell & Mehrabian (1977), "Evidence for a three-factor theory of emotions", J. Research in
# Personality 11:273-294, as quoted in Table 4 of Guo & Choi (2021), "Enhancing Cognitive Models of
# Emotions with Representation Learning", arXiv:2104.10117 (original table not read directly).
# Their axis is pleasure; here it is v. Each name maps to the R&M term in brackets.
# Cross-checked (Nov 21) against two other reprints: Gebhard (2005) "ALMA - A Layered Model of Affect",
# Table 2, and arXiv:2106.15846, Table 1. Anger, disgusted, surprise, afraid and terrified match exactly;
# sad is -0.63 in 2106.15846 vs -0.64 here (unresolved, immaterial). Joyful, content and annoyed
# were read only in Guo & Choi. No source states R&M's scale; all three use the values directly as -1..+1.
RM1977 = {
    "joy": (0.76, 0.48, 0.35),           # [joyful]
    "anger": (-0.51, 0.59, 0.25),        # [angry]
    "fear": (-0.64, 0.60, -0.43),        # [afraid]; [terrified] -0.62, 0.82, -0.43 is the extreme form
    "sadness": (-0.64, -0.27, -0.33),    # [sad]
    "surprise": (0.40, 0.67, -0.13),     # [surprised]; note R&M put surprise on the pleasant side
    "annoyance": (-0.28, 0.17, 0.04),    # [annoyed]; replaced [disgusted] -0.60, 0.35, 0.11 at Jordan's ask (Nov 21)
    "contentment": (0.86, 0.20, 0.62),   # [content]: in R&M's table, so sourced, not hand-picked
}
# Hand-picked, not from any table; starting guesses to tune by feel, like the spec's other numbers.
HAND_PICKED = {
    "relief": (0.60, -0.20, 0.05),   # pleasant, arousal dropping after a threat passes, control neutral
    "calm": (0.40, -0.60, 0.20),     # mildly pleasant, low arousal, slightly in control
    "focused": (0.10, 0.40, 0.40),   # near-neutral feeling, engaged, in control: "steady and bad, working on it"
}
REFERENCE_POINTS = {**RM1977, **HAND_PICKED}

# 1.4: label tuning, as distance from 0, 0, 0 (the cube's corners are sqrt(3) = 1.73 out). Starting guesses.
# The reference points sit 0.33 (annoyance) to 1.08 (contentment) out, so every point at its own
# coordinates reads at least mild; joy, fear and contentment read strong, anger moderate.
NEUTRAL_BELOW = 0.15  # under this she is "neutral": half a capped turn's nudge (0.3), and noise after decay
INTENSITY_BANDS = (   # (from this distance up, word); ascending
    (NEUTRAL_BELOW, "faint"),
    (0.30, "mild"),   # 0.30 so R&M's annoyed (0.33 out) reads mild, like the spec's "mildly irritated"
    (0.60, "moderate"),
    (0.85, "strong"),
)
# How "nearest" is measured. "direction" (default) compares the angle from 0 only, so the word says which
# emotion and the distance says how strong, as the spec splits them. "distance" is the spec read literally
# (plain distance to each point); it lets the points nearest 0 swallow faded states, e.g. faded joy
# reads focused. Flip here if direction reads wrong in practice.
LABEL_BY = "direction"
# Labels she should rarely get: named only when her state points within this many degrees of the point,
# otherwise the next nearest name wins, e.g. {"name": 8.0}. Empty: disgust, the one rare label, was
# replaced by annoyance at Jordan's ask (Nov 21).
RARE_WITHIN_DEG = {}


class EmotionError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _now():
    return rt.iso(rt.now_s())


def db_open():
    """emotion.db, with the emotion row at 0, 0, 0 the first time. Never resets an existing row."""
    con = store.connect("emotion", SCHEMA)
    con.execute("insert or ignore into state(layer, v, a, d, updated) values(?,0,0,0,?)", (LAYER, _now()))
    return con


def _row(con, layer):
    r = con.execute("select v, a, d, updated from state where layer=?", (layer,)).fetchone()
    if r is None:
        raise EmotionError("no_state", "no %s row in emotion.db" % layer)
    return {"v": r[0], "a": r[1], "d": r[2], "updated": r[3]}


def read(con=None):
    """The current state: {"v", "a", "d", "updated"}. Step 2's decay will apply here, on read."""
    own = con is None
    con = con or db_open()
    try:
        return _row(con, LAYER)
    finally:
        if own:
            con.close()


def _finite(name, x):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise EmotionError("bad_value", "%s must be a finite number, got %r" % (name, x))
    return float(x)


def _axis(name, x):
    return min(1.0, max(-1.0, _finite(name, x)))  # 1.2: clamp to -1..+1 (NaN and infinity still refused)


def _apply(step, *, cause, door, note=None, con=None):
    """The one place the state changes. step(old) returns the raw new (v, a, d); this checks the door
    and cause, clamps, and stores, with the read and the write in one transaction, so a nudge never
    loses a change made at the same moment. Returns {"old", "new", "clamped"}; clamped lists the
    axes whose raw value was past an edge. 1.7: every change also writes one change_log row in the same
    transaction (returned as "log_id"); if that insert fails the state change rolls back with it, so no
    change goes unlogged. All-or-nothing; safe with two processes."""
    if door not in WRITERS:
        raise EmotionError("not_allowed", "door %r may not change Rivia's state" % (door,))
    if not isinstance(cause, str) or not cause.strip():
        raise EmotionError("no_cause", "every change needs a cause")
    if note is not None and not isinstance(note, str):
        raise EmotionError("bad_value", "note must be text, got %r" % (note,))
    note = note.strip() if note and note.strip() else None
    own = con is None
    con = con or db_open()
    try:
        con.execute("begin immediate")
        try:
            old = _row(con, LAYER)
            raw = dict(zip("vad", (_finite(k, x) for k, x in zip("vad", step(old)))))
            new = {k: _axis(k, raw[k]) for k in "vad"}
            new["updated"] = _now()
            clamped = [k for k in "vad" if raw[k] != new[k]]
            con.execute("update state set v=?, a=?, d=?, updated=? where layer=?",
                        (new["v"], new["a"], new["d"], new["updated"], LAYER))
            log_id = con.execute(
                "insert into change_log(at, layer, old_v, old_a, old_d, new_v, new_a, new_d, raw_v, raw_a, raw_d,"
                " clamped, old_label, new_label, cause, note, door) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new["updated"], LAYER, old["v"], old["a"], old["d"], new["v"], new["a"], new["d"],
                 raw["v"], raw["a"], raw["d"], ",".join(clamped), _label_text(old), _label_text(new),
                 cause.strip(), note, door)).lastrowid
            con.execute("commit")
        except Exception:
            try:
                con.execute("rollback")
            except Exception:
                pass
            raise
        return {"old": old, "new": new, "clamped": clamped, "log_id": log_id}
    finally:
        if own:
            con.close()


def write(v, a, d, *, cause, door, note=None, con=None):
    """Set the state to v, a, d (each clamped to -1..+1). cause says why and note adds detail (both go in
    the change log); door is who asked. Returns {"old", "new", "clamped", "log_id"}."""
    vad = (_finite("v", v), _finite("a", a), _finite("d", d))  # refuse bad values before opening the DB
    return _apply(lambda old: vad, cause=cause, door=door, note=note, con=con)


def nudge(dv=0, da=0, dd=0, *, cause, door, note=None, con=None):
    """Move the state by dv, da, dd from wherever it is now (1.5); the sum is clamped like write().
    A nudge of all zeros is refused as no_change. Returns {"old", "new", "clamped", "log_id"}."""
    delta = (_finite("dv", dv), _finite("da", da), _finite("dd", dd))
    if not any(delta):
        raise EmotionError("no_change", "a nudge needs at least one non-zero delta")
    return _apply(lambda old: (old["v"] + delta[0], old["a"] + delta[1], old["d"] + delta[2]),
                  cause=cause, door=door, note=note, con=con)


LOG_KEYS = ("id", "at", "layer", "old_v", "old_a", "old_d", "new_v", "new_a", "new_d",
            "raw_v", "raw_a", "raw_d", "clamped", "old_label", "new_label", "cause", "note", "door")


def history(n=20, con=None):
    """The last n change_log rows, newest first, as dicts (LOG_KEYS; clamped as a list of axes).
    Reads only."""
    own = con is None
    con = con or db_open()
    try:
        rows = con.execute("select %s from change_log order by id desc limit ?" % ", ".join(LOG_KEYS),
                           (int(n),)).fetchall()
    finally:
        if own:
            con.close()
    out = [dict(zip(LOG_KEYS, r)) for r in rows]
    for r in out:
        r["clamped"] = r["clamped"].split(",") if r["clamped"] else []
    return out


def _unit(p):
    n = math.hypot(*p)
    return tuple(x / n for x in p)


def label(v, a, d, *, by=None):
    """Name a VAD point: {"name", "intensity", "magnitude", "gap"}. magnitude is the distance from 0;
    gap is how far the point is from the named reference point, by the same measure (0 = exactly on it).
    Near 0 the name and intensity are both "neutral" and gap is None. Reads only; never writes."""
    p = (_axis("v", v), _axis("a", a), _axis("d", d))
    by = by or LABEL_BY
    if by not in ("direction", "distance"):
        raise EmotionError("bad_value", "by must be direction or distance, got %r" % (by,))
    mag = math.hypot(*p)
    if mag < NEUTRAL_BELOW:
        return {"name": "neutral", "intensity": "neutral", "magnitude": mag, "gap": None}
    up = _unit(p)
    ref = {n: q for n, q in REFERENCE_POINTS.items()  # rare labels only inside their narrow cone, either measure
           if n not in RARE_WITHIN_DEG or math.degrees(math.acos(max(-1.0, min(1.0, sum(
               x * y for x, y in zip(up, _unit(q))))))) <= RARE_WITHIN_DEG[n]}
    if by == "direction":
        p, ref = up, {n: _unit(q) for n, q in ref.items()}
    gap, name = min((math.dist(p, q), n) for n, q in ref.items())  # ties go to the first name A-Z
    intensity = [w for lo, w in INTENSITY_BANDS if mag >= lo][-1]
    return {"name": name, "intensity": intensity, "magnitude": mag, "gap": gap}


def _label_text(s):
    """label() as one phrase for the change log ("moderate anger", "neutral"). The label is a convenience
    for reading the log, so if it ever fails the row stores None rather than blocking the state change."""
    try:
        lab = label(s["v"], s["a"], s["d"])
    except Exception:  # noqa: BLE001
        return None
    return "neutral" if lab["name"] == "neutral" else "%s %s" % (lab["intensity"], lab["name"])
