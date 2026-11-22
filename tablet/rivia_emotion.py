# rivia_emotion.py: Rivia's emotion state (emotion spec, Step 1.1), in emotion.db.
#
#   emotion.db   state: one row per layer (v, a, d each -1..+1, and when it last changed)
#
# Three layers (Step 2.1), one row each in the same table, so there is no schema change: "emotion" (fast),
# "mood" (slow) and "baseline" (personality). All start at 0, 0, 0; baseline stays there until 2.6 measures
# a real one. db_open() adds any missing row and never resets one that exists, so on an installed Step 1
# emotion.db the emotion row and its change log are kept and mood and baseline appear on the next open.
# read(), write() and nudge() still mean the emotion layer unless a caller names another with layer=.
# Clamping (1.2), the change log (1.7) and the door check hold for every layer alike. What the dial moves
# (2.4) lives in rivia_emotion_app.py; which layer the label reads (2.3) is read()'s contract, below.
#
# STEP 2 MERGED COPY (2.9, drafted Nov 21, re-merged Nov 22 after Jordan's Step 2 review, not installed):
# 2.1 layers, 2.2 decay on read, 2.3 label contract (her baseline counts as neutral), 2.4 hand-change tags,
# 2.5 fade columns in the change log and 2.7 baseline limits, in one file. step2/2.9/MERGE_NOTES.md lists
# every conflict between the separate drafts and how it was settled.
#
# 2.2 DECAY ON READ. Emotion decays toward mood (half-life EMOTION_HALF_LIFE_S, 10 minutes) and mood
# decays toward baseline (MOOD_HALF_LIFE_S, 4 hours). There is no timer: the stored rows are each layer's
# value at its "updated" time, and every read works out where she is now from the time since then
# (decayed()). So it survives restarts, and time the server was off counts like any other time.
#   - Reads never write. read(), read_all(), label() on what they return, the app's GET and the injection
#     line stay read-only, and a read gives the same answer however often she is read.
#   - Decayed values are written back only by a change, inside _apply(): the change starts from where she
#     is now (so a nudge moves her from her decayed state, and the change log's old_* are her real
#     pre-change values), and a change to a layer that another layer decays toward first stores that other
#     layer at its decayed value (a mood change settles emotion; a baseline change settles emotion and mood).
#     A change to emotion, the usual case, touches only the emotion row, as in Step 1.
#   - Settling writes no change_log row of its own (2.5: no row per fade). The next change to a settled
#     layer shows it: its fade_from_at is the settle time, later than that layer's previous logged change.
#   - Baseline does not decay. Its slow drift is Step 3; its limits are 2.7, below.
#   - Open emotion.db by hand and the rows show the state as of each row's "updated", not now.
#
# 2.5: decay is never logged as rows of its own. Each real change records, in its own row, the fade since
# the layer's stored value:
#   fade_from_v/a/d   the value as stored before this change (where she was before fading)
#   fade_from_at      when that value was stored
#   fade_toward       the layer she was fading toward ("mood" for emotion, "baseline" for mood), or None
#                     when no fade applied (rows from before 2.5, baseline, the anchor, or decay turned off)
#   old_v/a/d         where she had faded to at the moment of this change; the change starts from here
# So the fade is old minus fade_from, over at minus fade_from_at (fade() works it out), and the chain
# becomes: each row's fade_from is the previous row's new, for the same layer, unless a change to a layer
# above settled it in between (then fade_from_at is the settle time). Rows written before 2.5 hold None in
# every fade column. The columns are added to an existing emotion.db by ALTER TABLE ADD COLUMN
# (_migrate_fade_columns), which changes the table's schema only: no row is updated, deleted or rewritten,
# and the append-only triggers stay in place. _faded() works a fade out from 2.2's decay; it only reads.
#
# 2.7: hard limits on the baseline (her personality), so slow drift can never make her someone else. There is
# no drift yet (nothing produces a drift signal until Step 3); this only builds the fence it will run inside.
#   anchor     a fixed point, stored in baseline_anchor: 0, 0, 0 until set_anchor() puts the measured
#              baseline (2.6) there. set_anchor() moves the anchor and the baseline together, on purpose,
#              from the code or admin door only; it is the one way to move her past the limits.
#   distance   the baseline may sit at most BASELINE_MAX_FROM_ANCHOR from the anchor.
#   speed      in any rolling day it may end at most BASELINE_MAX_PER_DAY from where it was a day earlier, and
#              in any rolling week at most BASELINE_MAX_PER_WEEK. Both windows start no earlier than the last
#              set_anchor(), so a re-anchor does not count as drift.
# Every baseline write already goes through _apply(), so the limits live there (_limit_baseline) and hold for
# every door and every caller of write() and nudge(). A move that would cross a limit is cut short along its
# own line, to the furthest point all limits allow (possibly no move at all), and still stored and logged:
# its change_log row keeps what was asked in raw_* and names the limits it hit in the "limited" column.
# A baseline already past a limit (an older DB, or limits made tighter) may move back in or sideways, never
# further out. baseline_track keeps the baseline's recent positions with times in seconds for the speed
# check; it is not the audit log (change_log is) and keeps only a week and a little more.
# Jordan's hand changes (2.4's tags, passed as tag=): "calibration" is a lasting baseline change, so it resets
# the anchor to the new baseline, exactly like set_anchor() (and may come from the app's button). "test" and
# "test_end" (end_test putting the layer back) skip the limits: the limits are still worked out, and the
# change_log row's limited column reads "skipped" followed by any limits the move would have crossed. Test
# rows are left out of baseline_track, so after end_test the speed check sees no jump.
#
# 2.4: change_log.tag marks Jordan's hand changes to mood and baseline (the test/live tag 1.5 planned):
#   "test"         a try-out; end_test() puts the layer back where it was before the test began
#   "calibration"  a deliberate, lasting change (baseline only)
#   "test_end"     the row end_test() writes
# NULL for every other change. Added to an existing DB by ALTER TABLE (_migrate_tag_column); old rows read NULL.
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
create table if not exists baseline_anchor(
    id integer primary key check (id = 1), v real not null, a real not null, d real not null,
    set_at real not null, cause text not null);
create table if not exists baseline_track(
    id integer primary key, at real not null, v real not null, a real not null, d real not null);
"""
# change_log.clamped: the clamped axes joined, e.g. "" (none), "v" or "v,a". raw_* are what was asked
# for (for a nudge, old + delta); new_* are what was stored. The triggers make the table append-only;
# a later pruning step has to drop them on purpose.
# 2.7 adds one column, change_log.limited: the baseline limits a write hit, joined like clamped ("" for none,
# else from "anchor", "day", "week"). Added to an existing emotion.db by ALTER TABLE ADD COLUMN
# (_migrate_limited_column), which fires no trigger and rewrites no row; rows from before 2.7 read None.

# 2.5: added by _migrate_fade_columns on fresh and existing databases alike, so both end up with the same
# schema by the same path. Additive only: each is a nullable column with no default.
FADE_COLUMNS = (("fade_from_v", "real"), ("fade_from_a", "real"), ("fade_from_d", "real"),
                ("fade_from_at", "text"), ("fade_toward", "text"))
FADE_MIGRATED_KEY = "change_log_fade_columns"  # meta row: when the columns were added to this emotion.db
LAYER = "emotion"                          # the default layer for read(), write() and nudge()
LAYERS = ("emotion", "mood", "baseline")   # 2.1: one state row each, all starting at 0, 0, 0
WRITERS = ("code", "admin", "app_button")  # never "app": Gemini's MCP tools use that label.
ANCHOR_DOORS = ("code", "admin")           # 2.7: who may set_anchor(); the app's button only with tag calibration
TAGS = ("test", "calibration", "test_end")  # 2.4: change_log.tag values (NULL for untagged changes)
UNLIMITED_TAGS = ("test", "test_end")      # 2.7: these skip the baseline limits (logged as "skipped")
TAG_MIGRATED_KEY = "emotion_tag_migrated"  # 2.4: meta row: when change_log.tag was added to this emotion.db
REST_LAYER = "baseline"  # 2.3: labels read relative to this layer (her normal counts as neutral)

# 2.7: baseline limits, as straight-line distance in V, A, D (each axis -1..+1). Starting guesses to tune by feel.
BASELINE_MAX_FROM_ANCHOR = 0.20  # under the 0.30 "mild" band: from a neutral anchor she can turn at most faintly
                                 # anything, never mildly; at most a tenth of an axis's -1..+1 range
BASELINE_MAX_PER_WEEK = 0.05     # a quarter of that range: reaching the edge takes at least four weeks
BASELINE_MAX_PER_DAY = 0.02      # so one bad day can spend under half a week's allowance
DAY_S = 24 * 60 * 60
WEEK_S = 7 * DAY_S
LIMITS = ("anchor", "day", "week")  # names used in change_log.limited
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
# 2.3 (Jordan, Nov 21): her normal counts as neutral. label(rest=baseline) reads "neutral" whenever her state
# is within NEUTRAL_BELOW of her baseline, so at rest nothing is said even when the baseline isn't 0, 0, 0.
# Further out, the name and intensity are of the point itself, as in Step 1. With a baseline at 0, 0, 0 this
# is exactly Step 1's rule. Mood is not rest: a lingering mood is still named.
# How "nearest" is measured. "direction" (default) compares the angle from 0 only, so the word says which
# emotion and the distance says how strong, as the spec splits them. "distance" is the spec read literally
# (plain distance to each point); it lets the points nearest 0 swallow faded states, e.g. faded joy
# reads focused. Flip here if direction reads wrong in practice.
LABEL_BY = "direction"
# Labels she should rarely get: named only when her state points within this many degrees of the point,
# otherwise the next nearest name wins, e.g. {"name": 8.0}. Empty: disgust, the one rare label, was
# replaced by annoyance at Jordan's ask (Nov 21).
RARE_WITHIN_DEG = {}

# 2.2: decay half-lives in seconds, starting guesses from the spec ("Tuning Parameters"). Tune here.
# Any positive number works; float("inf") turns that layer's decay off. Read on every call, so a change
# takes effect on the next read without a restart.
EMOTION_HALF_LIFE_S = 10 * 60      # emotion -> mood
MOOD_HALF_LIFE_S = 4 * 60 * 60     # mood -> baseline
# A change to the layer on the left settles (writes forward to now) the layers on the right, because they
# decay toward it and their stored rows would otherwise be read against the wrong target.
SETTLES = {"emotion": (), "mood": ("emotion",), "baseline": ("emotion", "mood")}
# 2.5: the layer each one fades toward, as change_log.fade_toward records it. Baseline does not fade.
FADES_TOWARD = {"emotion": ("mood", "EMOTION_HALF_LIFE_S"), "mood": ("baseline", "MOOD_HALF_LIFE_S"),
                "baseline": (None, None)}


class EmotionError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _now():
    return rt.iso(rt.now_s())


def db_open():
    """emotion.db, with each layer's row (LAYERS) at 0, 0, 0 the first time. Never resets an existing row,
    so opening an installed Step 1 DB only adds mood and baseline. Writes no change_log row: a row
    appearing is not a change. 2.5: adds the change_log fade columns if missing. 2.7: also adds
    change_log.limited if missing, and the default anchor (0, 0, 0) with the baseline's current position as
    the first baseline_track row, if there is no anchor. 2.4: adds change_log.tag if missing."""
    con = store.connect("emotion", SCHEMA)
    try:  # 2.5: a failed migration closes the connection rather than leak it
        t = rt.now_s()
        now = rt.iso(t)
        con.executemany("insert or ignore into state(layer, v, a, d, updated) values(?,0,0,0,?)",
                        [(layer, now) for layer in LAYERS])
        _migrate_fade_columns(con)
        _migrate_limited_column(con)
        _migrate_tag_column(con)
        if con.execute("select 1 from baseline_anchor").fetchone() is None:
            con.execute("begin immediate")
            try:
                if con.execute("select 1 from baseline_anchor").fetchone() is None:  # re-checked under the lock
                    con.execute("insert into baseline_anchor(id, v, a, d, set_at, cause) values(1,0,0,0,?,?)",
                                (t, "default: no measured baseline yet"))
                    b = _row(con, "baseline")
                    con.execute("insert into baseline_track(at, v, a, d) values(?,?,?,?)", (t, b["v"], b["a"], b["d"]))
                con.execute("commit")
            except Exception:
                try:
                    con.execute("rollback")
                except Exception:
                    pass
                raise
    except Exception:
        con.close()
        raise
    return con


def _log_columns(con):
    return [r[1] for r in con.execute("pragma main.table_info(change_log)")]


def _migrate_fade_columns(con):
    """2.5: add the fade columns to change_log if any are missing. ALTER TABLE ADD COLUMN only changes the
    schema; it fires no trigger and rewrites no row, so the append-only log keeps every row as it was
    (old rows read None in the new columns). Inside one immediate transaction, re-checked after the lock,
    so two processes opening an old emotion.db at once add each column once. Returns the columns added."""
    if all(c in _log_columns(con) for c, _ in FADE_COLUMNS):
        return []
    con.execute("begin immediate")
    try:
        have = _log_columns(con)  # another process may have added them while we waited for the lock
        added = [c for c, _ in FADE_COLUMNS if c not in have]
        for c, kind in FADE_COLUMNS:
            if c in added:
                con.execute("alter table change_log add column %s %s" % (c, kind))
        if added:
            rows = con.execute("select count(*) from change_log").fetchone()[0]
            store.set_meta(con, FADE_MIGRATED_KEY, "%s; added %s; %d rows already in the log, left as they were"
                           % (_now(), ",".join(added), rows))
        con.execute("commit")
    except Exception:
        try:
            con.execute("rollback")
        except Exception:
            pass
        raise
    return added


def _migrate_limited_column(con):
    """2.7: add change_log.limited if missing. Schema only: no row is updated or deleted, so the append-only
    triggers do not fire and old rows read None. Re-checked under an immediate lock, so two processes opening
    an old emotion.db at once add it once."""
    if "limited" in _log_columns(con):
        return
    con.execute("begin immediate")
    try:
        if "limited" not in _log_columns(con):
            con.execute("alter table change_log add column limited text")
        con.execute("commit")
    except Exception:
        try:
            con.execute("rollback")
        except Exception:
            pass
        raise


def _migrate_tag_column(con):
    """2.4: add change_log.tag if it is missing. ALTER TABLE ADD COLUMN changes only the schema: no trigger
    fires and no row is rewritten, so the append-only log keeps every row (old rows read NULL). Re-checked
    inside an immediate transaction so two processes opening an old DB add it once. Returns True if added."""
    if "tag" in _log_columns(con):
        return False
    con.execute("begin immediate")
    try:
        added = "tag" not in _log_columns(con)
        if added:
            con.execute("alter table change_log add column tag text")
            rows = con.execute("select count(*) from change_log").fetchone()[0]
            store.set_meta(con, TAG_MIGRATED_KEY, "%s; added tag; %d rows already in the log, left as they were"
                           % (_now(), rows))
        con.execute("commit")
    except Exception:
        try:
            con.execute("rollback")
        except Exception:
            pass
        raise
    return added


def _layer(layer):
    if layer not in LAYERS:
        raise EmotionError("bad_layer", "layer must be one of %s, got %r" % (", ".join(LAYERS), layer))
    return layer


def _row(con, layer):
    r = con.execute("select v, a, d, updated from state where layer=?", (layer,)).fetchone()
    if r is None:
        raise EmotionError("no_state", "no %s row in emotion.db" % layer)
    return {"v": r[0], "a": r[1], "d": r[2], "updated": r[3]}


def _rows(con):
    """Every layer's stored row, in one statement so the three are one consistent snapshot."""
    got = {r[0]: {"v": r[1], "a": r[2], "d": r[3], "updated": r[4]} for r in con.execute(
        "select layer, v, a, d, updated from state where layer in (%s)" % ",".join("?" * len(LAYERS)), LAYERS)}
    for layer in LAYERS:
        if layer not in got:
            raise EmotionError("no_state", "no %s row in emotion.db" % layer)
    return got


def _rate(name, half_life):
    """Decay rate per second for a half-life in seconds (0 for infinity, i.e. no decay)."""
    if isinstance(half_life, bool) or not isinstance(half_life, (int, float)) or math.isnan(half_life) \
            or half_life <= 0:
        raise EmotionError("bad_config", "%s must be a positive number of seconds, got %r" % (name, half_life))
    return 0.0 if math.isinf(half_life) else math.log(2) / half_life


def _toward(x, target, k, dt):
    """x after dt seconds of decaying toward a fixed target at rate k."""
    if dt == 0 or k == 0:
        return x  # 2.9 merge: exactly x, not target + (x - target), which can differ in the last bit
    return target + (x - target) * math.exp(-k * dt)


def _cascade(e, m, b, ke, km, dt):
    """Emotion and mood after dt seconds, with emotion decaying toward mood while mood decays toward a
    fixed baseline. Exact solution of de/dt = -ke(e - m), dm/dt = -km(m - b), so a result does not depend
    on how many steps the time is cut into (reading at 9:00 and again at 9:10 lands where one read at
    9:10 does). Emotion lags mood, so it follows mood down rather than snapping to it."""
    if dt == 0:
        return e, m  # 2.9 merge: a read in the same second as a change returns exactly what was stored
    em, ee = math.exp(-km * dt), math.exp(-ke * dt)
    x, y = e - b, m - b
    if abs(ke - km) <= 1e-12 * max(ke, km, 1e-300):
        c = ke * dt * ee                       # equal rates: the limit of the formula below
    else:
        c = ke * (em - ee) / (ke - km)
    return b + x * ee + y * c, b + y * em


def decayed(rows, now_s):
    """rows as stored ({layer: {"v", "a", "d", "updated"}}) brought forward to now_s (seconds). Returns
    the same shape; "updated" stays each row's stored time, i.e. when that layer last changed. Pure: no
    database, no clock. A clock that has gone backwards gives the state as of the newest row, never an
    un-decayed one."""
    ke = _rate("EMOTION_HALF_LIFE_S", EMOTION_HALF_LIFE_S)
    km = _rate("MOOD_HALF_LIFE_S", MOOD_HALF_LIFE_S)
    te, tm = rt.to_epoch(rows["emotion"]["updated"]), rt.to_epoch(rows["mood"]["updated"])
    now = max(now_s, te, tm)
    out = {layer: dict(rows[layer]) for layer in rows}
    for k in "vad":
        e, m, b = rows["emotion"][k], rows["mood"][k], rows["baseline"][k]
        if te >= tm:   # the usual case: mood's row is as old as emotion's or older
            m_te = _toward(m, b, km, te - tm)
            e_now, _ = _cascade(e, m_te, b, ke, km, now - te)
        else:          # a mood row newer than emotion's, e.g. the first open after 2.1 adds it: before
            e_tm = _toward(e, m, ke, tm - te)  # mood's row existed, emotion is taken to decay toward its value
            e_now, _ = _cascade(e_tm, m, b, ke, km, now - tm)
        out["emotion"][k] = min(1.0, max(-1.0, e_now))
        out["mood"][k] = min(1.0, max(-1.0, _toward(m, b, km, now - tm)))
    return out


def read(con=None, *, layer=LAYER):
    """One layer's state now, decay applied (2.2): {"v", "a", "d", "updated"}, where updated is when that
    layer last changed. The emotion layer unless layer= names another. Reads only; never writes.
    2.3: read() with no layer is what the label, the injection line (1.6) and GET /app/emotion name;
    they pass the baseline as label(rest=) so her normal reads neutral. Never label mood or baseline alone."""
    _layer(layer)
    return read_all(con)[layer]


def read_all(con=None):
    """Every layer's state now, decay applied, {layer: {"v", "a", "d", "updated"}}, read together.
    Reads only."""
    own = con is None
    con = con or db_open()
    try:
        rows = _rows(con)
    finally:
        if own:
            con.close()
    return decayed(rows, rt.now_s())


def _faded(con, layer, row, t):
    """2.5's hook, filled by 2.2's decay: where layer has faded to by epoch second t, from its stored row (as
    last written by a change): ((v, a, d), toward), toward being the layer it fades toward, or None for no
    fade (baseline, or that layer's decay turned off). Only reads: a fade is never written back, so the
    stored rows and the change log stay the record of real changes (and 2.2's settling) only."""
    toward, knob = FADES_TOWARD[layer]
    if toward is None or _rate(knob, globals()[knob]) == 0.0:
        return (row["v"], row["a"], row["d"]), None
    now = decayed(_rows(con), t)[layer]
    return (now["v"], now["a"], now["d"]), toward


def _rest(con, layer):
    """Her baseline as (v, a, d), to label a change of layer against (2.3: her normal counts as neutral), or
    None, which labels against 0, 0, 0 as Step 1 did. None when there is no baseline row yet (Step 1's
    emotion.db) and for a change to the baseline itself, which is named as the point it is: against itself
    it would always read neutral. Read inside _apply's transaction, so it is the baseline of that moment.
    2.9 merge: the "anchor" row (2.7) is labelled as its own point too. Reads only. (2.5 log-label fix.)"""
    if layer in (REST_LAYER, "anchor"):
        return None
    r = con.execute("select v, a, d from state where layer=?", (REST_LAYER,)).fetchone()
    return tuple(r) if r is not None else None


def _finite(name, x):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise EmotionError("bad_value", "%s must be a finite number, got %r" % (name, x))
    return float(x)


def _axis(name, x):
    return min(1.0, max(-1.0, _finite(name, x)))  # 1.2: clamp to -1..+1 (NaN and infinity still refused)


def _anchor_row(con):
    r = con.execute("select v, a, d, set_at, cause from baseline_anchor where id=1").fetchone()
    if r is None:
        raise EmotionError("no_state", "no baseline anchor in emotion.db")
    return {"v": r[0], "a": r[1], "d": r[2], "set_at": r[3], "cause": r[4]}


def _position_at(con, t):
    """The baseline's position at time t (seconds) from baseline_track: the last row at or before t."""
    r = con.execute("select v, a, d from baseline_track where at <= ? order by at desc, id desc limit 1",
                    (t,)).fetchone()
    if r is None:  # t is before every row (clock moved back): the oldest known position
        r = con.execute("select v, a, d from baseline_track order by at, id limit 1").fetchone()
    return r


def _room(o, u, c, r):
    """How much of the move o -> o + u stays within distance r of c: the largest t in 0..1 with
    |o + t*u - c| <= max(r, |o - c|). The max() lets a point already past the limit move back in or
    sideways but never further out."""
    w = [x - y for x, y in zip(o, c)]
    r = max(r, math.hypot(*w))
    uu = sum(x * x for x in u)
    if uu == 0:
        return 1.0
    uw = sum(x * y for x, y in zip(u, w))
    ww = sum(x * x for x in w)
    t = (-uw + math.sqrt(max(0.0, uw * uw - uu * (ww - r * r)))) / uu  # far root of |w + t*u| = r
    return max(0.0, min(1.0, t))


def _limit_baseline(con, old, want, t):
    """2.7: cut the baseline move old -> want (both in the cube) short at the first limit it would cross.
    Returns (new point as a tuple, names of the limits hit). Called only from _apply, inside its transaction."""
    o = (old["v"], old["a"], old["d"])
    u = tuple(w - x for w, x in zip(want, o))
    anchor = _anchor_row(con)
    checks = (
        ("anchor", (anchor["v"], anchor["a"], anchor["d"]), BASELINE_MAX_FROM_ANCHOR),
        ("day", _position_at(con, max(t - DAY_S, anchor["set_at"])), BASELINE_MAX_PER_DAY),
        ("week", _position_at(con, max(t - WEEK_S, anchor["set_at"])), BASELINE_MAX_PER_WEEK),
    )
    room = {name: _room(o, u, c, r) for name, c, r in checks}
    hit = [name for name in LIMITS if room[name] < 1.0]
    if not hit:
        return tuple(want), []
    k = max(0.0, min(room.values()) * (1 - 1e-9))  # a hair inside, so rounding never creeps past the edge
    return tuple(x + k * y for x, y in zip(o, u)), hit


def _track_baseline(con, new, t):
    """Record the baseline's new position for the speed check, and drop rows the week window no longer
    needs (it keeps the last row at or before a week ago, so that position stays known)."""
    con.execute("insert into baseline_track(at, v, a, d) values(?,?,?,?)", (t, new["v"], new["a"], new["d"]))
    con.execute("delete from baseline_track where at < (select max(at) from baseline_track where at <= ?)",
                (t - WEEK_S,))


def _apply(step, *, cause, door, note=None, con=None, layer=LAYER, tag=None, _reanchor=False):
    """The one place the state changes. step(old) returns the raw new (v, a, d); this checks the door
    and cause, clamps, and stores, with the read and the write in one transaction, so a nudge never
    loses a change made at the same moment. Returns {"old", "new", "clamped", "limited"}; clamped lists the
    axes whose raw value was past an edge. 1.7: every change also writes one change_log row in the same
    transaction (returned as "log_id"); if that insert fails the state change rolls back with it, so no
    change goes unlogged. All-or-nothing; safe with two processes. 2.1: layer names the row to change
    (emotion by default). 2.2: old is the layer's decayed value now, so the change starts from where she is;
    and the layers that decay toward this one (SETTLES) are first written forward to now, in the same
    transaction, with no change_log row of their own. Nothing else writes decay back. 2.5: the row also
    records the stored value she faded from, when it was stored and the layer she faded toward (returned as
    "fade"); a fade that can't be worked out refuses the change rather than log a wrong old. 2.7: a baseline
    change is held to the drift limits (_limit_baseline) after clamping; "limited" lists the limits it hit.
    _reanchor (only set_anchor uses it) instead moves the anchor to the same point, unlimited, logged as its
    own "anchor" row. 2.4: tag (TAGS or None) is stored in change_log.tag; 2.7: "calibration" reanchors the
    same way (baseline only), "test" and "test_end" skip the limits.
    Returns {"old", "new", "clamped", "limited", "log_id", "fade"}."""
    _layer(layer)
    if door not in WRITERS:
        raise EmotionError("not_allowed", "door %r may not change Rivia's state" % (door,))
    if tag is not None and tag not in TAGS:
        raise EmotionError("bad_tag", "tag must be one of %s, got %r" % (", ".join(TAGS), tag))
    if tag == "calibration":
        if layer != "baseline":
            raise EmotionError("bad_tag", "only the baseline can be calibrated")
        _reanchor = True
    if _reanchor and tag in UNLIMITED_TAGS:
        raise EmotionError("bad_tag", "a test can't move the anchor")
    if _reanchor and (layer != "baseline" or (door not in ANCHOR_DOORS
                                              and not (door == "app_button" and tag == "calibration"))):
        raise EmotionError("not_allowed", "door %r may not move the baseline anchor" % (door,))
    if not isinstance(cause, str) or not cause.strip():
        raise EmotionError("no_cause", "every change needs a cause")
    if note is not None and not isinstance(note, str):
        raise EmotionError("bad_value", "note must be text, got %r" % (note,))
    note = note.strip() if note and note.strip() else None

    def _log(con, row, layer, old, raw, new, clamped, limited, stored=None, toward=None):
        """One change_log row (1.7, plus 2.5's fade columns, 2.7's limited and 2.4's tag), written only here.
        stored is the row the layer faded from (None for the anchor row, which never fades). Returns its id."""
        fv, fa, fd, fat = (stored["v"], stored["a"], stored["d"], stored["updated"]) if stored else (None,) * 4
        rest = _rest(con, layer)  # 2.5 log-label fix: a row says what the dial said at that moment
        return con.execute(
            "insert into change_log(at, layer, old_v, old_a, old_d, new_v, new_a, new_d, raw_v, raw_a, raw_d,"
            " clamped, old_label, new_label, cause, note, door,"
            " fade_from_v, fade_from_a, fade_from_d, fade_from_at, fade_toward, limited, tag)"
            " values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["at"], layer, old["v"], old["a"], old["d"], new["v"], new["a"], new["d"],
             raw["v"], raw["a"], raw["d"], ",".join(clamped), _label_text(old, rest), _label_text(new, rest),
             row["cause"], row["note"], row["door"], fv, fa, fd, fat, toward, ",".join(limited), tag)).lastrowid

    own = con is None
    con = con or db_open()
    try:
        con.execute("begin immediate")
        try:
            t = rt.now_s()  # one clock reading for the whole change: decay, fade, stamp and limits
            stamp = rt.iso(t)
            te = rt.to_epoch(stamp)  # decay to the stored timestamp, so nothing is lost to rounding (2.2)
            stored = _row(con, layer)
            faded, toward = _faded(con, layer, dict(stored), te)
            faded = tuple(faded)
            if len(faded) != 3:
                raise EmotionError("bad_value", "a fade must give v, a, d, got %r" % (faded,))
            if toward is not None and not isinstance(toward, str):
                raise EmotionError("bad_value", "fade toward must be a layer name, got %r" % (toward,))
            # A fade stays inside the cube by construction; checked and held there anyway, never logged as clamping.
            old = {k: _axis(k, x) for k, x in zip("vad", faded)}
            old["updated"] = stored["updated"]  # when the value she faded from was set
            raw = dict(zip("vad", (_finite(k, x) for k, x in zip("vad", step(old)))))
            cubed = {k: _axis(k, raw[k]) for k in "vad"}
            clamped = [k for k in "vad" if raw[k] != cubed[k]]
            limited = []
            if layer == "baseline" and not _reanchor:
                point, limited = _limit_baseline(con, old, tuple(cubed[k] for k in "vad"), t)
                if tag in UNLIMITED_TAGS:
                    limited = ["skipped"] + limited  # a test: logged, not held
                else:
                    cubed = dict(zip("vad", point))
            new = dict(cubed, updated=stamp)
            row = dict(at=new["updated"], cause=cause.strip(), note=note, door=door)
            if SETTLES[layer]:  # 2.2: the layers decaying toward this one keep the path they were on
                cur = decayed(_rows(con), te)
                for dep in SETTLES[layer]:
                    con.execute("update state set v=?, a=?, d=?, updated=? where layer=?",
                                (cur[dep]["v"], cur[dep]["a"], cur[dep]["d"], stamp, dep))
            if _reanchor:
                anchor = _anchor_row(con)
                con.execute("update baseline_anchor set v=?, a=?, d=?, set_at=?, cause=? where id=1",
                            (new["v"], new["a"], new["d"], t, cause.strip()))
                _log(con, row, "anchor", anchor, raw, new, clamped, [])
            con.execute("update state set v=?, a=?, d=?, updated=? where layer=?",
                        (new["v"], new["a"], new["d"], new["updated"], layer))
            if layer == "baseline" and tag not in UNLIMITED_TAGS:
                _track_baseline(con, new, t)
            log_id = _log(con, row, layer, old, raw, new, clamped, limited, stored, toward)
            con.execute("commit")
        except Exception:
            try:
                con.execute("rollback")
            except Exception:
                pass
            raise
        fade = {"from": {k: stored[k] for k in ("v", "a", "d", "updated")}, "toward": toward}
        return {"old": old, "new": new, "clamped": clamped, "limited": limited, "log_id": log_id, "fade": fade}
    finally:
        if own:
            con.close()


def set_anchor(v, a, d, *, cause, door, note=None, con=None, tag=None):
    """2.7: move the baseline's anchor, and the baseline itself, to v, a, d (clamped to -1..+1), past every
    limit, and restart the day and week windows from now. For setting the measured baseline (2.6) or a
    deliberate reset; code and admin doors only. Logs two rows in one transaction: "anchor" (old anchor to
    new) and "baseline". Returns _apply's result for the baseline row. tag="calibration" also lets the
    phone app's button in; write() or nudge() with that tag does the same thing."""
    vad = (_finite("v", v), _finite("a", a), _finite("d", d))
    return _apply(lambda old: vad, cause=cause, door=door, note=note, con=con, layer="baseline", tag=tag,
                  _reanchor=True)


def read_anchor(con=None):
    """The baseline anchor: {"v", "a", "d", "set_at" (seconds), "cause"}. Reads only."""
    own = con is None
    con = con or db_open()
    try:
        return _anchor_row(con)
    finally:
        if own:
            con.close()


def baseline_room(con=None):
    """2.7, for checking by hand: how far the baseline sits from the anchor and how much of each limit it has
    used: {"anchor": {"used", "max"}, "day": {...}, "week": {...}}, distances in V, A, D. Reads only."""
    own = con is None
    con = con or db_open()
    try:
        t = rt.now_s()
        b = _row(con, "baseline")
        p = (b["v"], b["a"], b["d"])
        anchor = _anchor_row(con)
        out = {"anchor": {"used": math.dist(p, (anchor["v"], anchor["a"], anchor["d"])),
                          "max": BASELINE_MAX_FROM_ANCHOR}}
        for name, span, cap in (("day", DAY_S, BASELINE_MAX_PER_DAY), ("week", WEEK_S, BASELINE_MAX_PER_WEEK)):
            out[name] = {"used": math.dist(p, _position_at(con, max(t - span, anchor["set_at"]))), "max": cap}
        return out
    finally:
        if own:
            con.close()


def write(v, a, d, *, cause, door, note=None, con=None, layer=LAYER, tag=None):
    """Set one layer (emotion unless layer= names another) to v, a, d, each clamped to -1..+1. cause says
    why and note adds detail (both go in the change log); door is who asked.
    Returns {"old", "new", "clamped", "limited", "log_id", "fade"}."""
    vad = (_finite("v", v), _finite("a", a), _finite("d", d))  # refuse bad values before opening the DB
    return _apply(lambda old: vad, cause=cause, door=door, note=note, con=con, layer=layer, tag=tag)


def nudge(dv=0, da=0, dd=0, *, cause, door, note=None, con=None, layer=LAYER, tag=None):
    """Move one layer (emotion unless layer= names another) by dv, da, dd from wherever it is now (1.5),
    decay included (2.2); the sum is clamped like write().
    A nudge of all zeros is refused as no_change. Returns {"old", "new", "clamped", "limited", "log_id", "fade"}."""
    delta = (_finite("dv", dv), _finite("da", da), _finite("dd", dd))
    if not any(delta):
        raise EmotionError("no_change", "a nudge needs at least one non-zero delta")
    return _apply(lambda old: (old["v"] + delta[0], old["a"] + delta[1], old["d"] + delta[2]),
                  cause=cause, door=door, note=note, con=con, layer=layer, tag=tag)


def _tagged(con, layer, door):
    """Tagged rows for one layer and door, newest first (the hand-change history manual_marker reads)."""
    return [dict(zip(("id", "at", "tag", "note", "old_v", "old_a", "old_d"), r)) for r in con.execute(
        "select id, at, tag, note, old_v, old_a, old_d from change_log where layer=? and door=? and tag is not null"
        " order by id desc", (layer, door))]


def _open_test(rows):
    """The test rows since the last calibration or test_end, oldest first ([] when no test is open)."""
    run = []
    for r in rows:
        if r["tag"] != "test":
            break
        run.append(r)
    return run[::-1]


def manual_marker(layer, door="app_button", con=None):
    """What the app shows beside a layer: None when no hand change stands on it, else
    {"tag": "test", "since", "at", "note"} while a test is open, or {"tag": "calibration", "at", "note"} for
    the latest calibration. A later automatic change (decay, Step 3) doesn't clear it. Reads only."""
    _layer(layer)
    own = con is None
    con = con or db_open()
    try:
        rows = _tagged(con, layer, door)
    finally:
        if own:
            con.close()
    run = _open_test(rows)
    if run:
        return {"tag": "test", "since": run[0]["at"], "at": run[-1]["at"], "note": run[-1]["note"]}
    cal = next((r for r in rows if r["tag"] == "calibration"), None)
    return {"tag": "calibration", "at": cal["at"], "note": cal["note"]} if cal else None


def end_test(layer, *, door, note=None, con=None):
    """Put a layer back where it was before its open test began (the old value on the test's first row),
    logged with tag "test_end" and cause "manual test end". No open test: EmotionError "no_test".
    2.9: that old value is the decayed value the test started from (2.2); the restore skips the baseline
    limits (2.7) and settles the layers below it like any change. Returns what write() returns."""
    _layer(layer)
    own = con is None
    con = con or db_open()
    try:
        run = _open_test(_tagged(con, layer, door))
        if not run:
            raise EmotionError("no_test", "no test is open on %s" % layer)
        first = run[0]
        return write(first["old_v"], first["old_a"], first["old_d"], cause="manual test end", door=door,
                     note=note, con=con, layer=layer, tag="test_end")
    finally:
        if own:
            con.close()


LOG_KEYS = ("id", "at", "layer", "old_v", "old_a", "old_d", "new_v", "new_a", "new_d",
            "raw_v", "raw_a", "raw_d", "clamped", "old_label", "new_label", "cause", "note", "door",
            "fade_from_v", "fade_from_a", "fade_from_d", "fade_from_at", "fade_toward", "limited", "tag")


def fade(row):
    """How much she faded before a logged change, from one history() row: {"dv", "da", "dd", "seconds",
    "toward"}; d* is old minus fade_from (0 when nothing faded), seconds the time since the value she faded
    from was stored. None for a row written before 2.5, which has no fade recorded. Reads only."""
    if row.get("fade_from_v") is None:
        return None
    return {"dv": row["old_v"] - row["fade_from_v"], "da": row["old_a"] - row["fade_from_a"],
            "dd": row["old_d"] - row["fade_from_d"],
            "seconds": rt.to_epoch(row["at"]) - rt.to_epoch(row["fade_from_at"]), "toward": row["fade_toward"]}


def history(n=20, con=None):
    """The last n change_log rows, newest first, as dicts (LOG_KEYS; clamped as a list of axes, limited as a
    list of limit names, both [] when empty or from before they existed). Rows from before 2.5 have None in
    the fade columns. Reads only."""
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
        r["limited"] = r["limited"].split(",") if r["limited"] else []
    return out


def _unit(p):
    n = math.hypot(*p)
    return tuple(x / n for x in p)


def label(v, a, d, *, by=None, rest=None):
    """Name a VAD point: {"name", "intensity", "magnitude", "gap"}. magnitude is the distance from 0;
    gap is how far the point is from the named reference point, by the same measure (0 = exactly on it).
    Near 0 the name and intensity are both "neutral" and gap is None. rest (2.3) is her baseline as
    (v, a, d): within NEUTRAL_BELOW of it she also reads neutral. Reads only; never writes."""
    p = (_axis("v", v), _axis("a", a), _axis("d", d))
    if rest is not None:
        r = (_axis("rest v", rest[0]), _axis("rest a", rest[1]), _axis("rest d", rest[2]))
        if math.dist(p, r) < NEUTRAL_BELOW:
            return {"name": "neutral", "intensity": "neutral", "magnitude": math.hypot(*p), "gap": None}
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


def _label_text(s, rest=None):
    """label() as one phrase for the change log ("moderate anger", "neutral"). rest is her baseline (2.3 rule:
    her normal reads neutral), so a log row says what the dial said at that moment. The label is a convenience
    for reading the log, so if it ever fails the row stores None rather than blocking the state change."""
    try:
        lab = label(s["v"], s["a"], s["d"], rest=rest)
    except Exception:  # noqa: BLE001
        return None
    return "neutral" if lab["name"] == "neutral" else "%s %s" % (lab["intensity"], lab["name"])
