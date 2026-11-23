# rivia_trend_mood.py: how the plan-accuracy trend colors her mood (emotion spec, Step 5.4).
#
# DRAFT (Nov 23), not installed. Base: rivia-hub main 91ed441 (Step 4 installed, PR #28/#29/#30). Standard library
# plus rivia_emotion (for its label points and label()) and rivia_body_cap (for its pure step_toward()).
#
# Jordan (Nov 23): her goal right now is "scheduling my day as accurately as possible". The spec (Step 5): mood comes
# from the direction the gap is moving, not from how big it is:
#     gap shrinking      -> hopeful / relieved   (label "relief")
#     gap growing        -> concerned            (label "worried", with no extra energy)
#     flat, and good     -> content              (label "contentment")
#     flat, and bad      -> focused              (label "focused")
#     not enough data    -> nothing
# 5.3 says which of those it is and how strongly (0 to 1). This file turns that into a small mood pull.
#
# Rules this follows:
#   1. Small. Her goal is not to "be emotional" (Jordan, Nov 23). Each direction has its own cap, all under the
#      0.30 "mild" band, so the trend alone reads "faint" at most. Being on track (content) reads only just faint.
#   2. Counted once (Jordan, Nov 22: "The problems shouldnt hit her twice"). Step 4's body pull (server health) and
#      this trend can share a cause: if the server is down, arrivals don't log and the plan looks worse too. So the
#      two together may never move her mood further than the body alone can (BODY_MAX_PULL, 0.25). When the body is
#      already pulling her the same way, the trend's part is trimmed to fit; when they point opposite ways nothing
#      is trimmed, because nothing is counted twice. The real fix (leave out plan misses caused by server outages)
#      belongs to 5.2's gap number; this ceiling is the backstop.
#   3. Concern never makes her pushier (4.7 "never push harder"). Concern lowers "in control" (dominance) and adds
#      no energy (arousal 0): less sure of herself means more careful. It never raises dominance or arousal.
#   4. No fear (Jordan, Nov 22). Every direction, alone or added to any body pull 4.6 allows, reads only neutral,
#      relief, worried, contentment, focused, annoyance, calm or joy. Never fear, distressed, sadness or anger.
#   5. Mostly valence (pleasant/unpleasant). Relief, concern and content lead with valence. Focused is the spec's
#      one exception: it is "engaged and in control", so it leads with arousal and dominance.
#   6. Slow. The pull grows at most TREND_WORSEN_PER_H and shrinks at most TREND_EASE_PER_H, so a flip in 5.3's
#      answer fades across rather than jumping.
#   7. Logged with its cause, in shadow. shadow_tick() writes one row to a `trend_mood` table in the database it is
#      handed (5.6 picks which; body.db fits). It refuses emotion.db and never opens it: nothing about her changes.
#      There is no switch to go live here on purpose. Going live is a separate change, on Jordan's yes.
#   8. Report only. This file moves no plan, sends nothing and calls no tool.
#
# Input from 5.3 (a dict; 5.6 lines the names up if 5.3 differs):
#     {"direction": "shrinking" | "growing" | "flat" | "not_enough_data",
#      "strength": 0..1,                  # how clearly it is moving (or how steady, for flat)
#      "standing": "good" | "bad" | None, # flat only: is the gap small (good) or large (bad)
#      "at": epoch seconds}               # optional: when 5.3 worked it out
# A flat reading with no standing, or a reading older than TREND_STALE_S, counts as not enough data.
import json
import math

import rivia_body_cap as cap
import rivia_emotion as emo

TREND_CAUSE = "trend"          # cause on every row; never mixed with 4.6's "body" rows

# Direction each trend points, as (v, a, d). Placed on existing label points, so no new label is needed.
DIRECTIONS = {
    "shrinking": emo.REFERENCE_POINTS["relief"],          # (0.60, -0.20, 0.05): hopeful, settling down
    "growing": (emo.REFERENCE_POINTS["worried"][0], 0.0,  # (-0.49, 0, -0.28): worried with no extra energy
                emo.REFERENCE_POINTS["worried"][2]),
    "flat_good": emo.REFERENCE_POINTS["contentment"],     # (0.86, 0.20, 0.62)
    "flat_bad": emo.REFERENCE_POINTS["focused"],          # (0.10, 0.40, 0.40)
}
READS_AS = {"shrinking": "relief", "growing": "worried", "flat_good": "contentment", "flat_bad": "focused"}

# Caps: the furthest the trend alone may pull her mood, at strength 1. Starting guesses, tune by feel.
TREND_CAPS = {
    "shrinking": 0.20,   # faint relief at most
    "growing": 0.20,     # faint worried at most
    "flat_good": 0.15,   # on track is her normal: right at the edge of faint contentment
    "flat_bad": 0.15,    # right at the edge of faint focused
}
TREND_DEADBAND = 0.05          # a pull under this counts as nothing
COMBINED_MAX = cap.BODY_MAX_PULL  # 0.25: body and trend together, the same way, never past what the body alone may
TREND_WORSEN_PER_H = 0.05      # growing: the full 0.20 takes 4 hours (5.3's trend is already days of data)
TREND_EASE_PER_H = 0.10        # shrinking back toward 0: twice as fast
TREND_MAX_STEP_S = 15 * 60     # one tick earns at most 15 minutes' worth of movement, however long since the last
TREND_STALE_S = 2 * 86400      # a 5.3 reading older than this counts as not enough data
TREND_KEEP_S = 7 * 86400       # a week of shadow rows

ALIASES = {"not enough data": "not_enough_data", "none": "not_enough_data", "steady": "flat"}
FORBIDDEN = ("anger", "sadness", "distressed") + emo.NEVER_LABELS

SCHEMA = """
create table if not exists trend_mood(
    at integer primary key, direction text, strength real, standing text, kind text, target text, share text,
    body_share text, trimmed real, would_read text, with_body_reads text, cause text);
"""


class TrendMoodError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _unit(p):
    n = math.hypot(*p)
    return tuple(x / n for x in p)


def _scale(p, k):
    return tuple(x * k for x in p)


def _num(name, x, lo=None, hi=None):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise TrendMoodError("bad_value", "%s must be a finite number, got %r" % (name, x))
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        raise TrendMoodError("bad_value", "%s must be %s..%s, got %r" % (name, lo, hi, x))
    return float(x)


def _tuned(tune):
    """5.7: Jordan's slider values from the app (rivia_progress_settings.mood_tune()), or this file's own when None.
    Only these four kinds of number can be tuned; the 0.25 counted-once ceiling, the word guard (never distressed,
    sad, angry or fear) and "concern never adds energy or control" are fixed below and take no setting."""
    t = {"caps": dict(TREND_CAPS), "worsen_per_h": TREND_WORSEN_PER_H, "ease_per_h": TREND_EASE_PER_H,
         "stale_s": TREND_STALE_S}
    if tune:
        t.update({k: v for k, v in tune.items() if k in t and k != "caps"})
        t["caps"].update(tune.get("caps") or {})
    # Belt and braces: whatever the app sent, no single mood goes past the counted-once ceiling.
    t["caps"] = {k: min(float(v), COMBINED_MAX) for k, v in t["caps"].items()}
    return t


def kind(reading, now=None, stale_s=None):
    """Which mood a 5.3 reading means: "shrinking", "growing", "flat_good", "flat_bad" or None (no nudge), plus
    its strength 0..1 and a short reason when it is None. Refuses readings it can't understand."""
    if reading is None:
        return None, 0.0, "no reading"
    if not isinstance(reading, dict):
        raise TrendMoodError("bad_value", "reading must be a dict, got %r" % (reading,))
    d = reading.get("direction")
    d = ALIASES.get(str(d).strip().lower(), str(d).strip().lower()) if d is not None else "not_enough_data"
    if d not in ("shrinking", "growing", "flat", "not_enough_data"):
        raise TrendMoodError("bad_value", "direction must be shrinking, growing, flat or not_enough_data, got %r"
                             % (reading.get("direction"),))
    if d == "not_enough_data":
        return None, 0.0, "not enough data"
    s = _num("strength", reading.get("strength", 0.0), 0.0, 1.0)
    at = reading.get("at")
    if at is not None and now is not None and now - _num("at", at) > (TREND_STALE_S if stale_s is None else stale_s):
        return None, 0.0, "reading is stale"
    if d == "flat":
        st = reading.get("standing")
        if st not in ("good", "bad"):
            return None, 0.0, "flat but no standing"
        d = "flat_" + st
    return d, s, None


def target(reading, now=None, tune=None):
    """The trend's own pull on mood, (dv, da, dd), before the counted-once trim. Pure. tune: see _tuned()."""
    t = _tuned(tune)
    k, s, _ = kind(reading, now, t["stale_s"])
    if k is None:
        return (0.0, 0.0, 0.0)
    m = t["caps"][k] * s
    if m < TREND_DEADBAND:
        return (0.0, 0.0, 0.0)
    return _scale(_unit(DIRECTIONS[k]), m)


def _bad_word(p):
    n = emo.label(*p)["name"]
    return n in FORBIDDEN or any(w in n for w in emo.NEVER_LABELS)


def trim(trend, body):
    """Counted once: the largest k (0..1) such that |body + k*trend| stays within COMBINED_MAX when the two point
    the same way. Opposite ways (or no body pull) are not trimmed for the ceiling.
    Then a word guard: if body + k*trend would read distressed, sad or angry (it can: hope partly cancelling a
    worried body leaves a direction nearer distressed), k is lowered until it doesn't. k = 0 is the body alone,
    which 4.6 already keeps to annoyed, worried or neutral. Returns (trimmed trend, k). Pure."""
    trend, body = cap._vec("trend", trend), cap._vec("body", body)
    tt = sum(x * x for x in trend)
    if tt == 0:
        return trend, 1.0
    k = 1.0
    bt = sum(x * y for x, y in zip(body, trend))
    if bt > 0:  # pulling the same way: hold the pair to the ceiling
        bb = sum(x * x for x in body)
        if bb >= COMBINED_MAX ** 2 - 1e-9:  # body already at the ceiling (to rounding)
            k = 0.0
        elif math.hypot(*(b + t for b, t in zip(body, trend))) > COMBINED_MAX:
            # k^2 tt + 2k bt + bb - C^2 = 0, positive root
            k = max(0.0, min(1.0, (-bt + math.sqrt(bt * bt - tt * (bb - COMBINED_MAX ** 2))) / tt))
    if k > 0 and _bad_word(tuple(b + k * t for b, t in zip(body, trend))):
        if _bad_word(body):
            k = 0.0
        else:  # body alone is fine: find (to 1%) the most trend that still reads fine
            lo, hi = 0.0, k
            while hi - lo > 0.01:
                mid = (lo + hi) / 2
                if _bad_word(tuple(b + mid * t for b, t in zip(body, trend))):
                    hi = mid
                else:
                    lo = mid
            k = lo
    return (_scale(trend, k), k) if k < 1.0 else (trend, 1.0)


def step_toward(share, goal, seconds, worsen_per_h=None, ease_per_h=None):
    """Move the trend's share toward goal at TREND_WORSEN_PER_H (growing) or TREND_EASE_PER_H (anything else).
    Same rule as 4.6's step_toward, with this file's rates. Pure."""
    share, goal = cap._vec("share", share), cap._vec("goal", goal)
    seconds = _num("seconds", seconds, 0.0)
    gap = math.dist(share, goal)
    if gap == 0:
        return goal
    worsen = TREND_WORSEN_PER_H if worsen_per_h is None else worsen_per_h
    ease = TREND_EASE_PER_H if ease_per_h is None else ease_per_h
    rate = worsen if math.hypot(*goal) > math.hypot(*share) else ease
    room = rate * seconds / 3600.0
    if room >= gap:
        return goal
    f = room / gap
    return tuple(s + f * (g - s) for s, g in zip(share, goal))


def _read(p):
    lab = emo.label(*p)  # on a neutral baseline; her real baseline (B1) isn't measured yet
    return "neutral" if lab["name"] == "neutral" else "%s %s" % (lab["intensity"], lab["name"])


def _cause(k, s, why, kk):
    words = {"shrinking": "plan misses shrinking", "growing": "plan misses growing",
             "flat_good": "plans steady and on track", "flat_bad": "plans steady but off"}
    if k is None:
        return "%s: %s" % (TREND_CAUSE, why)
    out = "%s: %s, strength %.2f" % (TREND_CAUSE, words[k], s)
    if kk < 1.0:
        out += ", trimmed to %.0f%% (body already pulling this way)" % (kk * 100)
    return out


def _guard(con):
    for _, name, path in con.execute("pragma database_list").fetchall():
        if name == "main" and path and path.replace("\\", "/").rsplit("/", 1)[-1] == "emotion.db":
            raise TrendMoodError("not_allowed", "shadow rows never go in emotion.db")


def _last(con):
    r = con.execute("select at, share from trend_mood order by at desc limit 1").fetchone()
    if not r:
        return None, (0.0, 0.0, 0.0)
    return r[0], tuple(json.loads(r[1]))


def shadow_tick(con, now, reading, body_share=(0.0, 0.0, 0.0), tune=None):
    """One shadow reading at `now` (epoch seconds). reading is 5.3's dict; body_share is what Step 4 is pulling
    her mood by right now (4.9's dials row "share"). Writes one row to trend_mood in `con` and returns it as a
    dict. Changes nothing about her. Raises TrendMoodError on input it can't read; the caller logs and skips."""
    _guard(con)
    now = int(_num("now", now))
    body = cap._vec("body_share", body_share)
    t = _tuned(tune)
    k, s, why = kind(reading, now, t["stale_s"])
    raw = target(reading, now, tune)
    goal, kk = trim(raw, body)
    con.executescript(SCHEMA)
    last_at, cur = _last(con)
    since = TREND_MAX_STEP_S if last_at is None else min(TREND_MAX_STEP_S, max(0, now - last_at))
    nxt = step_toward(cur, goal, since, t["worsen_per_h"], t["ease_per_h"])
    # The trim also holds while moving: never let the share itself push the pair past the ceiling.
    nxt, _ = trim(nxt, body)
    with_body = tuple(b + t for b, t in zip(body, nxt))
    rd = reading or {}
    row = {
        "at": now, "direction": rd.get("direction"), "strength": s, "standing": rd.get("standing"), "kind": k,
        "target": [round(x, 4) for x in goal], "share": [round(x, 4) for x in nxt],
        "body_share": [round(x, 4) for x in body], "trimmed": round(kk, 4), "would_read": _read(nxt),
        "with_body_reads": _read(with_body), "cause": _cause(k, s, why, kk),
    }
    con.execute("insert or replace into trend_mood values(?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["at"], None if row["direction"] is None else str(row["direction"]), row["strength"],
                 row["standing"] if row["standing"] in ("good", "bad") else None, row["kind"],
                 json.dumps(row["target"]), json.dumps(row["share"]), json.dumps(row["body_share"]),
                 row["trimmed"], row["would_read"], row["with_body_reads"], row["cause"]))
    con.execute("delete from trend_mood where at < ?", (now - TREND_KEEP_S,))
    con.commit()
    return row


def recent(con, limit=20):
    """The newest shadow rows, newest first, for the review. Read-only."""
    con.executescript(SCHEMA)
    keys = ("at", "direction", "strength", "standing", "kind", "target", "share", "body_share", "trimmed",
            "would_read", "with_body_reads", "cause")
    out = []
    for r in con.execute("select %s from trend_mood order by at desc limit ?" % ", ".join(keys), (int(limit),)):
        d = dict(zip(keys, r))
        for j in ("target", "share", "body_share"):
            d[j] = json.loads(d[j])
        out.append(d)
    return out
