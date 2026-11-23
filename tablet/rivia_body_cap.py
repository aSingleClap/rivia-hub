# rivia_body_cap.py: caps on how far her body (the server's health) can move her mood (emotion spec, Step 4.6).
#
# DRAFT (Nov 22), not installed. Base: rivia-hub main aa64af2 (Step 2 installed, PR #27). Standard library plus
# rivia_emotion and rivia_time; no schema change, no Gemini, no timer of its own.
#
# The spec (Step 4, "Caps"): limit this layer's total influence, to avoid a hypochondriac assistant.
# Jordan (Nov 21, 2.2 thread): "I think the mood should be mostly tied to how she is running." Small persistent
# errors nudge her toward annoyed, and the bias is lopsided: "like not having enough sleep or being hungry makes
# you more irritable but not remove your ability to find something funny or smile." The label list says
# annoyed, not disgust (Jordan, Nov 21).
#
# What this file does with that:
#   1. The body moves MOOD only, never emotion. Emotion fades toward mood (10-minute half-life), so the body
#      shows in her emotion at rest, and any spike from a real event (Step 3) still lands on top of it.
#   2. One score for how she is running. The two dials' pulls (4.4 stress, 4.5 confidence) are added and split
#      into the parts that mean "running badly" (valence down, arousal up, dominance down) and "running well"
#      (the opposite signs). Bad minus good gives one number: positive means running badly.
#   3. Running badly points at annoyed or worried, nothing else. The valence and arousal part of the pull (slow
#      running, 4.4) is laid along Russell & Mehrabian's annoyed point (-0.28, +0.17, +0.04). The dominance part
#      (things breaking and confidence under 0.70, 4.5) passes through as itself, so a big confidence drop reads
#      "worried" (Jordan in 4.5, Nov 22: "Yes add worried"; the point is 4.5's worried_label.patch). That only
#      happens when the label list has a "worried" point: without it, dominance-down reads fear, so it is folded
#      into annoyed instead. Either way the body can make her read faintly annoyed, faintly worried or nothing,
#      never afraid, sad or angry (a plain per-axis box reads sadness or anger at its corners; see README).
#      The dials' own readings still drive behavior (4.4 shorter replies, 4.5 double-checking, 4.7); this only
#      decides what the body does to her mood.
#   4. Running well barely shows: at most BODY_MAX_GOOD (0.10) toward pleasant, under the 0.15 neutral band, so
#      a healthy server is her normal, not a reason to be cheerful.
#   5. Caps against a hypochondriac (starting guesses, tune by feel):
#        BODY_MAX_PULL     0.25  total pull from her baseline. Under the 0.30 "mild" band, so the body alone reads
#                                at most "faint annoyance". It is also the humor headroom: see below.
#        BODY_DEADBAND     0.05  a pull smaller than this counts as nothing (noise in the dials).
#        BODY_WORSEN_PER_H 0.10  how fast the pull may grow: the full 0.25 takes 2.5 hours of bad running, so a
#                                blip never shows and only persistent trouble does.
#        BODY_EASE_PER_H   0.25  how fast it may shrink once things are fixed: faster, like irritability fading
#                                after you eat. (Mood's own 4-hour fade runs on top.)
#        BODY_MIN_WRITE    0.02  smaller corrections are not written, so a steady state writes a few rows an
#                                hour, not one per tick (change_log has no pruning yet).
#        BODY_MAX_STEP_S   15 m  one write earns at most 15 minutes' worth of movement, however long since the
#                                last one, so a quiet day followed by a spike still climbs at the worsen rate.
#   6. The cap is on the TOTAL. The body's share of mood is worked out from its own change_log rows (cause
#      BODY_CAUSE, layer mood), each faded by mood's half-life since it was written, so it is exactly what is
#      left of the body's pushes in her mood now. Each tick writes only the difference between that share and
#      the new (capped, rate-limited) target. However long the server runs badly, the body's share never passes
#      BODY_MAX_PULL, and Jordan's hand changes and Step 3's mood coupling are not counted against it.
#   7. While Jordan has a hand test open on mood (2.4), the body writes nothing, so end_test puts her back where
#      the test found her.
#
# Humor headroom (Jordan's asymmetry, measured): Step 3 caps a turn at 0.3 per axis. From the full body pull
# (0.25), one capped joy turn lands at valence +0.10 above her baseline: pleasant, and the label reads "focused"
# because her arousal is already up; a second turn reads "moderate joy". Jordan (Nov 22): "Irritable humor can come
# off more dry in addition to focused depending on how irritated she is." So the cap stays 0.25, and irritation()
# gives how irritated she is (0 to 1) for whatever picks her style later (Step 3's tone rules, Step 8's style map).
#
# Not here: the dials themselves (4.4, 4.5), the collector and its timing (4.2), her own normal (4.3), "never
# push harder" (4.7), shadow mode (4.9). tick() defaults to a dry run; it writes only with write=True.
import math

import rivia_emotion as emo
import rivia_time as rt

BODY_CAUSE = "body"            # change_log.cause on every row this file writes
BODY_DOOR = "code"             # never "app": Gemini never moves her state
BODY_LAYER = "mood"            # the only layer the body moves

BODY_MAX_PULL = 0.25           # total pull from baseline, running badly (VAD distance)
BODY_MAX_GOOD = 0.10           # total pull from baseline, running well
BODY_DEADBAND = 0.05           # net pulls under this are treated as 0
BODY_WORSEN_PER_H = 0.10       # the pull may grow at most this much per hour
BODY_EASE_PER_H = 0.25         # and shrink at most this much per hour
BODY_MIN_WRITE = 0.02          # corrections smaller than this are not written
BODY_MAX_STEP_S = 15 * 60      # a tick earns at most this much time's worth of movement, so a long quiet spell
                               # (no body rows) never lets one spike jump straight to the cap
BODY_LEDGER_HALF_LIVES = 20    # body rows older than this many mood half-lives (80 h) count as gone (under 1e-6 left)
BODY_LEDGER_MAX_ROWS = 5000    # safety stop for the ledger query

IRRITABLE = emo.RM1977["annoyance"]  # the direction slow running points: R&M annoyed
WORRIED = "worried"                  # 4.5's label point; dominance-down passes through only when it exists


class BodyCapError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _vec(name, p):
    if isinstance(p, dict):  # 4.5's reading()["pull"] is {"dv", "da", "dd"}; a missing axis counts as 0
        if set(p) - {"dv", "da", "dd"}:
            raise BodyCapError("bad_value", "%s has unknown keys %r" % (name, sorted(set(p) - {"dv", "da", "dd"})))
        p = (p.get("dv", 0.0), p.get("da", 0.0), p.get("dd", 0.0))
    try:
        p = tuple(p)
    except TypeError:
        raise BodyCapError("bad_value", "%s must be (dv, da, dd), got %r" % (name, p))
    if len(p) != 3:
        raise BodyCapError("bad_value", "%s must be (dv, da, dd), got %r" % (name, p))
    for x in p:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
            raise BodyCapError("bad_value", "%s must hold finite numbers, got %r" % (name, p))
    return tuple(float(x) for x in p)


def _unit(p):
    n = math.hypot(*p)
    return tuple(x / n for x in p)


def _scale(p, k):
    return tuple(x * k for x in p)


def worry_known():
    """True when the label list has 4.5's "worried" point, so a dominance drop can show as itself."""
    return WORRIED in emo.REFERENCE_POINTS


def split(pull):
    """One VAD pull as (bad vector, good vector). Bad: valence down and arousal up, laid along annoyed, plus
    dominance down as itself (folded into annoyed when there is no worried label). Good: the opposite signs."""
    v, a, d = pull
    drop = max(0.0, -d)
    annoy = math.hypot(max(0.0, -v), max(0.0, a))
    if not worry_known():
        annoy, drop = math.hypot(annoy, drop), 0.0
    u = _unit(IRRITABLE)
    bad = (annoy * u[0], annoy * u[1], annoy * u[2] - drop)
    good = (max(0.0, v), max(0.0, -a), max(0.0, d))
    return bad, good


def target(*pulls):
    """The capped body pull on mood, as (dv, da, dd) from her baseline, for the dials' pulls (any number; 4.4's
    pull() gives a tuple, 4.5's reading()["pull"] a dict). Each pull is a level ("where she'd sit while this
    holds"), not a per-tick step. Pure: no database, no clock."""
    ps = [_vec("pull %d" % i, p) for i, p in enumerate(pulls)]
    total = tuple(sum(p[i] for p in ps) for i in range(3)) if ps else (0.0, 0.0, 0.0)
    bad, good = split(total)
    mb, mg = math.hypot(*bad), math.hypot(*good)
    net = mb - mg  # how she's running: positive is badly
    if abs(net) < BODY_DEADBAND:
        return (0.0, 0.0, 0.0)
    if net > 0:  # running badly: annoyed and/or worried, at most BODY_MAX_PULL in total
        return _scale(bad, min(net, BODY_MAX_PULL) / mb)
    # running well: its own direction, at most BODY_MAX_GOOD
    return _scale(_unit(good), min(-net, BODY_MAX_GOOD))


def step_toward(share, goal, seconds):
    """Move the body's share toward goal, no faster than the worsen/ease rates allow over `seconds`.
    Growing (goal further from 0 than share) uses BODY_WORSEN_PER_H, anything else BODY_EASE_PER_H. Pure."""
    share, goal = _vec("share", share), _vec("goal", goal)
    if seconds < 0 or not math.isfinite(seconds):
        raise BodyCapError("bad_value", "seconds must be a finite number >= 0, got %r" % (seconds,))
    gap = math.dist(share, goal)
    if gap == 0:
        return goal
    rate = BODY_WORSEN_PER_H if math.hypot(*goal) > math.hypot(*share) else BODY_EASE_PER_H
    room = rate * seconds / 3600.0
    if room >= gap:
        return goal
    k = room / gap
    return tuple(s + k * (g - s) for s, g in zip(share, goal))


def _mood_rate():
    return emo._rate("MOOD_HALF_LIFE_S", emo.MOOD_HALF_LIFE_S)


def share(con, now_s):
    """What is left of the body's pushes in her mood at now_s: the sum of every body row's change (new minus
    old, so clamping is already in it), each faded by mood's half-life since it was written. Also returns the
    time of the newest body row (None if none). Reads only."""
    km = _mood_rate()
    horizon = BODY_LEDGER_HALF_LIVES * emo.MOOD_HALF_LIFE_S if km else float("inf")
    total = [0.0, 0.0, 0.0]
    newest = None
    rows = con.execute(
        "select at, old_v, old_a, old_d, new_v, new_a, new_d from change_log"
        " where layer=? and cause=? order by id desc limit ?",
        (BODY_LAYER, BODY_CAUSE, BODY_LEDGER_MAX_ROWS)).fetchall()
    for at, ov, oa, od, nv, na, nd in rows:
        t = rt.to_epoch(at)
        age = max(0.0, now_s - t)
        if age > horizon:
            break  # newest first, so every later row is older still
        newest = t if newest is None else max(newest, t)
        k = math.exp(-km * age)
        for i, (o, n) in enumerate(((ov, nv), (oa, na), (od, nd))):
            total[i] += (n - o) * k
    return tuple(total), newest


def irritation(con=None):
    """How much the body is weighing on her now, for the style rules: {"level" 0..1 (share of BODY_MAX_PULL),
    "kind" "annoyed", "worried" or None}. Jordan (Nov 22): irritable humor comes off drier the more irritated she
    is. Reads only."""
    own = con is None
    con = con or emo.db_open()
    try:
        cur, _ = share(con, rt.now_s())
    finally:
        if own:
            con.close()
    m = math.hypot(*cur)
    if m < BODY_DEADBAND or cur[0] >= 0:
        return {"level": 0.0, "kind": None}
    name = emo.label(*_scale(_unit(cur), 0.5))["name"]  # by direction: a faint share would read "neutral"
    kind = "worried" if name == WORRIED else "annoyed"
    return {"level": min(1.0, m / BODY_MAX_PULL), "kind": kind}


def _test_open(con):
    m = emo.manual_marker(BODY_LAYER, con=con)
    return bool(m and m["tag"] == "test")


def tick(*pulls, con=None, write=False, note=None):
    """One body update. pulls are the dials' current VAD pulls (4.4 stress, 4.5 confidence). Works out the
    capped target, the body's current share of mood, how far the share may move since the last body row, and
    the correction. Writes it to mood (door code, cause body) only with write=True and only when it is at least
    BODY_MIN_WRITE and no hand test is open on mood. Returns a dict: target, share, next, delta, wrote (bool),
    skipped (None or a reason), log_id. One collector runs ticks (4.2); two at once could both correct."""
    goal = target(*pulls)
    own = con is None
    con = con or emo.db_open()
    try:
        now = rt.now_s()
        cur, last = share(con, now)
        # Movement allowed since the last body row, but never more than BODY_MAX_STEP_S worth (also for the first).
        since = BODY_MAX_STEP_S if last is None else min(BODY_MAX_STEP_S, max(0.0, now - last))
        nxt = step_toward(cur, goal, since)
        delta = tuple(n - c for n, c in zip(nxt, cur))
        out = {"target": goal, "share": cur, "next": nxt, "delta": delta, "wrote": False, "skipped": None,
               "log_id": None}
        if math.hypot(*delta) < BODY_MIN_WRITE:
            out["skipped"] = "too_small"
        elif _test_open(con):
            out["skipped"] = "hand_test_open"
        elif not write:
            out["skipped"] = "dry_run"
        else:
            words = note or "pull %+.2f %+.2f %+.2f" % goal
            r = emo.nudge(*delta, cause=BODY_CAUSE, door=BODY_DOOR, note=words, con=con, layer=BODY_LAYER)
            out.update(wrote=True, log_id=r["log_id"])
        return out
    finally:
        if own:
            con.close()
