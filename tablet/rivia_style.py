# rivia_style.py: how Rivia would sound (emotion spec, Step 8: co-regulation and expressive style).
# DRAFT, Nov 24, not installed. Shadow only: everything here works out the tone she WOULD use and logs it. Nothing
# reaches her replies (that needs Step 3's rewording pass, which is parked), nothing moves her mood, and she never
# writes any of it.
#
# Jordan's decisions (Nov 24):
#   Irritated: "Light sass is fine at the problem dry, no sass would be like a step above that". So mildly irritated =
#   dry with light sass aimed at the problem; more irritated = dry, no sass, more focused. Earlier (Nov 22): "Irritable
#   humor can come off more dry in addition to focused depending on how irritated she is", humor "more dry but not
#   silly".
#   When he's amped: "she shouldn't get amped up with me but over 3 messages she can start to tone it down witha
#   gradual drop", ending "a bit calmer than her usual" (option 1). This replaces the spec's "close a third of the gap".
#   "She can be empathetic towards what im saying (within the bounds of an ai) maybe turn up her tone a bit but if im a
#   cursing screaming mess then she shouldnt match that": a small warmth lift, never an energy match, no cursing back.
#   Reaction learning and the joke cooldown: "Build now" (they stay empty until her replies are actually styled).
#
# Pieces in this file:
#   8.1  his energy read     per typed message: swear count, ALL-CAPS words, rapid-fire, plus Step 7's read (charged
#                            words against his normal, what he said about how he is). Text read and dropped.
#   8.2  steady, then calmer when he's worked up she holds her own level (never rises with him), then eases down over
#                            3 messages to a bit calmer than her usual, holds there, and goes back once he settles.
#   8.2b warmth              a small lift toward what he's saying, capped; never energy, never cursing back.
#   8.3  style pick          her label -> dry / playful / deadpan / crisp / plain.
#   8.4  strength            0 to 3 inside each style's allowed range.
#   8.5  his stress wins     when he's stressed: no sass, no teasing, playful drops to plain, strength at most 1.
#   8.6  banter switch       cursing is off unless Jordan turns it on; even then never when he's stressed or cursing.
#   8.7  learning            his reaction to a STYLED reply nudges that style's strength one step after a streak;
#                            "knock it off" mutes that style for a day. Shadow: no styled replies, so nothing to learn.
#   8.8  joke cooldown       bits the rewording pass used, blocked from coming back for a set time. Empty in shadow.
#   8.9  pushback guard      reactions only ever move style strength. Nothing here touches caution, trust, her mood
#                            or whether she disagrees. Corrections are about substance, so they never count as a
#                            style reaction.
#   8.10 would-be style line the words the rewording pass would get, built and logged per message; for_expression()
#                            is the only door and returns "" in shadow.
#
# Writes body.db only (tables style_*). Never emotion.db, never a sign-in file. Never an MCP tool.
import hashlib
import json
import re
import sys
import time

import rivia_reading as reading

SHADOW = True  # True: build and log only; for_expression() returns "". Turning it off is a later step, by hand.

# ---------- starting numbers (all adjustable in the Layer 8 card, rivia_style_settings) ----------
RAPID_COUNT = 3        # 8.1 this many of his messages...
RAPID_SECONDS = 120    # 8.1 ...within this many seconds = rapid-fire
CAPS_WORDS = 3         # 8.1 this many ALL-CAPS words in one message
USUAL_SHARE = 0.30     # 8.1 swearing or caps in at least this share of his messages in a situation = his normal there
ENERGY_STEP = 0.25     # 8.2 one energy step = this much arousal above or below her baseline
EASE_MESSAGES = 3      # 8.2 she eases down over this many messages
CALM_STEPS = 1         # 8.2 ...to this many steps calmer than her usual (never lower)
BACK_MESSAGES = 2      # 8.2 back to her usual after this many normal messages from him in a row...
BACK_MINUTES = 30      # 8.2 ...or this many quiet minutes
WARMTH_CAP = 1         # 8.2b warmth lift, at most this many steps
SASS_SWITCH = 0.5      # 8.3 irritation (0 to 1) from here up: dry with no sass, more focused
RANGE = {"dry": 3, "crisp": 3, "deadpan": 2, "playful": 1}   # 8.4 highest strength per style (lowest is 0)
BANTER_ON = 0          # 8.6 cursing off until Jordan turns it on
LEARN_SAME = 3         # 8.7 the same reaction this many times in a row moves strength one step
LEARN_MAX = 2          # 8.7 learning can move a style at most this many steps from where her mood puts it
MUTE_HOURS = 24        # 8.7 "knock it off" turns that style off this long
BIT_DAYS = 7           # 8.8 the same bit can't come back for this many days
LINE_KEEP_DAYS = 60    # log rows kept this long

STYLES = ("dry", "playful", "deadpan", "crisp", "plain")
# 8.3: her label -> style. No fear anywhere (Jordan, Nov 22). "worried" and "distressed" are stress-like: deadpan.
STYLE_OF = {
    "annoyance": "dry", "anger": "dry",
    "joy": "playful", "contentment": "playful",
    "distressed": "deadpan", "sadness": "deadpan", "worried": "deadpan",
    "focused": "crisp",
}
STYLE_WORDS = {
    "dry": "dry", "playful": "playful and quick", "deadpan": "deadpan and brief", "crisp": "crisp, no fluff",
    "plain": "plain",
}
INTENSITY_STRENGTH = {"neutral": 0, "faint": 1, "mild": 1, "moderate": 2, "strong": 3}
HIS_UPSET_SAID = {"annoyed", "stressed"}          # he told her he's worked up
HIS_LOW_SAID = {"annoyed", "stressed", "tired"}   # he told her he's not in a good place: no teasing

# 8.1 word lists. Whole words, lower case. Swearing is counted, never kept.
SWEARS = re.compile(r"\b(?:fuck\w*|f\*+k\w*|shit\w*|bullshit|damn\w*|goddamn\w*|hell|ass|asshole\w*|bitch\w*|"
                    r"crap\w*|piss\w*|bastard\w*|wtf|ffs|stfu)\b")
CAPS_RE = re.compile(r"\b[A-Z]{3,}\b")
NOT_SHOUTING = {"HMC", "API", "MCP", "GPS", "ETA", "USB", "LOL", "LMAO", "LMFAO", "ROFL", "HAHA", "HAHAHA", "OMG",
                "NYC", "DMV", "ATM", "TBH", "IMO", "AKA", "FYI", "ASAP", "WTF", "FFS", "GPU", "CPU", "RAM", "PDF",
                "SMS", "APK", "VPN", "SSH", "URL", "EST", "EDT"}

# 8.7 his reaction to a styled reply.
KNOCK_IT_OFF = (r"\bknock it off\b", r"\bcut (?:it|that) out\b", r"\bstop (?:it|that|with the)\b", r"\bnot funny\b",
                r"\benough with\b", r"\bdrop the (?:attitude|act|sass|jokes?)\b", r"\bquit it\b",
                r"\btone it down\b", r"\bno (?:jokes|sass)\b", r"\bnot the time\b")
PLAYS_ALONG = (r"\bha(?:ha)+\b", r"\bhah\b", r"\blol\b", r"\blmf?ao\b", r"\brofl\b", r"\bgood one\b", r"\btouch[eé]\b",
               r"\bnice\b", r"\bfunny\b", "\U0001F602", "\U0001F923", "\U0001F606", "\U0001F604")
REACTIONS = ("plays_along", "ignores", "knock_it_off")

# 8.9: what the would-be style block always says, whatever the style. Fixed, never on a card.
FIXED_RULES = [
    "Style changes wording only, never what she says or does.",
    "Disagree and push back when warranted; never soften a correction, a warning or a no to please him.",
    "Anger and sass aim at the problem, never at Jordan.",
    "Never match his energy; never curse back while he's cursing or shouting.",
    "No claims of feelings beyond a working state; no \"I understand\" unless she does.",
]

SCHEMA = """
create table if not exists style_messages(
    message_id text primary key, at real not null, day text not null, situation text not null,
    swears integer not null, caps integer not null, rapid integer not null, worked_up integer not null,
    why text, reaction text, received real not null);
create index if not exists style_messages_at on style_messages(at);
create table if not exists style_state(
    id integer primary key check (id = 1), phase text not null, start_step integer not null, step_i integer not null,
    worked_at real, calm_streak integer not null, updated real not null);
create table if not exists style_learning(
    style text primary key, adj integer not null default 0, streak_kind text, streak_n integer not null default 0,
    muted_until real, updated real not null);
create table if not exists style_bits(
    id integer primary key autoincrement, key text not null, phrase text not null, at real not null);
create index if not exists style_bits_key on style_bits(key);
create table if not exists style_log(
    id integer primary key autoincrement, at real not null, day text not null, kind text not null,
    message_id text, styled integer not null default 0, row text not null);
create index if not exists style_log_at on style_log(at);
"""
LOG_KINDS = ("line", "reaction", "learn", "bit", "repeat_bit")
BIT_MAX = 120


class StyleError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _guard(con):
    for _, name, path in con.execute("pragma database_list").fetchall():
        if name == "main" and path and path.replace("\\", "/").rsplit("/", 1)[-1] == "emotion.db":
            raise StyleError("wrong_db", "Step 8 rows never go in emotion.db")


def prepare(con):
    _guard(con)
    con.executescript(SCHEMA)
    return con


def code_tune():
    return {"rapid_count": RAPID_COUNT, "rapid_seconds": RAPID_SECONDS, "caps_words": CAPS_WORDS,
            "usual_share": USUAL_SHARE, "energy_step": ENERGY_STEP, "ease_messages": EASE_MESSAGES,
            "calm_steps": CALM_STEPS, "back_messages": BACK_MESSAGES, "back_minutes": BACK_MINUTES,
            "warmth_cap": WARMTH_CAP, "sass_switch": SASS_SWITCH, "range_dry": RANGE["dry"],
            "range_crisp": RANGE["crisp"], "range_deadpan": RANGE["deadpan"], "range_playful": RANGE["playful"],
            "banter_on": BANTER_ON, "learn_same": LEARN_SAME, "learn_max": LEARN_MAX, "mute_hours": MUTE_HOURS,
            "bit_days": BIT_DAYS}


def _tune(values=None):
    """The starting numbers, or Jordan's edits from the Layer 8 card. Never raises."""
    if values is not None:
        return values
    try:
        import rivia_style_settings as ss
        return ss.tune()
    except Exception as e:  # noqa: BLE001  (a broken settings file must not stop Step 8)
        print("rivia: style settings unreadable (%s); using the code's numbers." % type(e).__name__, file=sys.stderr)
        return code_tune()


def _range(t, style):
    return 0 if style == "plain" else int(t["range_" + style])


def _log(con, kind, at, row, message_id=None, styled=False):
    con.execute("insert into style_log(at, day, kind, message_id, styled, row) values(?,?,?,?,?,?)",
                (at, reading._day(at), kind, message_id, 1 if styled else 0, json.dumps(row, sort_keys=True)))


def log_rows(con, since, kinds=None):
    rows = con.execute("select id, at, kind, message_id, styled, row from style_log where at >= ? order by id",
                       (since,)).fetchall()
    out = []
    for i, at, kind, mid, styled, row in rows:
        if kinds and kind not in kinds:
            continue
        out.append({"id": i, "at": at, "kind": kind, "message_id": mid, "styled": bool(styled), **json.loads(row)})
    return out


# ---------- 8.1 his energy ----------

def swear_count(text):
    return len(SWEARS.findall(reading._clean(text)))


def caps_count(text):
    return sum(1 for w in CAPS_RE.findall(text or "") if w not in NOT_SHOUTING)


def rapid(con, at, t):
    n = con.execute("select count(*) from reading_messages where at > ? and at <= ?",
                    (at - t["rapid_seconds"], at)).fetchone()[0]
    return int(n >= t["rapid_count"])


def usual(con, situation, column, now, t, t7):
    """Is swearing (or caps) normal for him in this situation? Only once the situation has enough messages."""
    rows = con.execute("select %s from style_messages where situation = ? and at >= ?" % column,
                       (situation, now - t7["keep_days"] * 86400)).fetchall()
    if len(rows) < t7["min_messages"]:
        return False
    return sum(1 for (x,) in rows if x) / len(rows) >= t["usual_share"]


def his_read(con, m, facts, now, t, t7):
    """8.1: is he worked up, and is he stressed? Against his own normal (Step 7), never a fixed line.
    Before his normal exists, only his own words count."""
    rn = reading.read_now(con, now, t7)
    measures = (rn or {}).get("measures", {}) if rn and rn.get("situation") == m["situation"] else {}
    ar, va = measures.get("arousal", {}), measures.get("valence", {})
    signals = []
    if facts["swears"] and not usual(con, m["situation"], "swears", now, t, t7):
        signals.append("swearing")
    if facts["caps"] >= t["caps_words"] and not usual(con, m["situation"], "caps", now, t, t7):
        signals.append("caps")
    if facts["rapid"]:
        signals.append("rapid_fire")
    worked, why = False, None
    if ar.get("zone") == "well_off" and ar.get("direction") == "up":
        worked, why = True, "your words read well past your usual charge here"
    elif ar.get("zone") == "bit_off" and ar.get("direction") == "up" and signals:
        worked, why = True, "a bit past your usual charge, with %s" % ", ".join(s.replace("_", "-") for s in signals)
    elif m.get("said") in HIS_UPSET_SAID:
        worked, why = True, "you said you're %s" % m["said"]
    heavy = va.get("zone") == "well_off" and va.get("direction") == "down"
    stressed = worked or m.get("said") in HIS_LOW_SAID or heavy
    return {"worked_up": worked, "why": why, "stressed": bool(stressed), "signals": signals,
            "cursing_now": bool(facts["swears"]), "enough": bool(rn and rn.get("enough")),
            "personal": bool(m.get("personal")), "said": m.get("said")}


# ---------- her side ----------

def her_state():
    """Her emotion layer (decayed), labeled against her baseline, plus how irritated she is (0 to 1). Reads only."""
    import rivia_emotion as emo
    layers = emo.read_all()
    e, b = layers["emotion"], layers["baseline"]
    lab = emo.label(e["v"], e["a"], e["d"], rest=(b["v"], b["a"], b["d"]))
    irr = 0.0
    if lab["name"] in ("annoyance", "anger"):
        irr = max(0.0, min(1.0, (lab["magnitude"] - 0.15) / 0.70))
    try:
        import rivia_body_cap as body
        b_irr = body.irritation()
        if b_irr.get("kind") == "annoyed":   # the body can also weigh on her as worried; that isn't irritation
            irr = max(irr, float(b_irr["level"]))
    except Exception:  # noqa: BLE001  (no body reading: go by her label alone)
        pass
    return {"name": lab["name"], "intensity": lab["intensity"], "a": e["a"], "rest_a": b["a"],
            "irritation": round(irr, 3)}


def energy_step(her, t):
    return max(-3, min(3, int(round((her["a"] - her["rest_a"]) / t["energy_step"]))))


# ---------- 8.2 steady, then calmer ----------

def _state(con):
    r = con.execute("select phase, start_step, step_i, worked_at, calm_streak, updated from style_state "
                    "where id = 1").fetchone()
    if not r:
        return {"phase": "usual", "start_step": 0, "step_i": 0, "worked_at": None, "calm_streak": 0, "updated": 0}
    return dict(zip(("phase", "start_step", "step_i", "worked_at", "calm_streak", "updated"), r))


def _save_state(con, s, now):
    con.execute("insert or replace into style_state(id, phase, start_step, step_i, worked_at, calm_streak, updated) "
                "values(1,?,?,?,?,?,?)", (s["phase"], s["start_step"], s["step_i"], s["worked_at"],
                                          s["calm_streak"], now))


def energy_plan(s, worked_up, her_step, now, t):
    """Moves the plan on by one of his messages. Returns (new state, target step, words).
    usual:  she's at her own level (her_step).
    easing: his first worked-up message, she holds her own level (never rises with him). Each further worked-up
            message moves her one share of the way down, so after EASE_MESSAGES she's CALM_STEPS under her usual,
            never lower. Calm messages hold where she is; BACK_MESSAGES calm in a row, or BACK_MINUTES with nothing
            from him, and she's back to usual."""
    s = dict(s)
    quiet = s["updated"] and now - s["updated"] >= t["back_minutes"] * 60
    if s["phase"] == "easing" and quiet and not worked_up:
        s.update(phase="usual", step_i=0, calm_streak=0)
    if worked_up:
        if s["phase"] == "usual":
            s.update(phase="easing", start_step=her_step, step_i=0)
        else:
            s.update(start_step=min(s["start_step"], her_step), step_i=min(s["step_i"] + 1, int(t["ease_messages"])))
        s.update(worked_at=now, calm_streak=0)
    elif s["phase"] == "easing":
        s["calm_streak"] += 1
        if s["calm_streak"] >= t["back_messages"]:
            s.update(phase="usual", step_i=0, calm_streak=0)
    if s["phase"] == "usual":
        s["updated"] = now
        return s, her_step, "her usual energy"
    floor = -int(t["calm_steps"])
    n = max(1, int(t["ease_messages"]))
    start = s["start_step"]
    target = floor if start <= floor else max(floor, int(round(start + (floor - start) * min(s["step_i"], n) / n)))
    if s["step_i"] == 0:
        words = "holding steady (not rising with you)"
    elif s["step_i"] >= n:
        words = "a bit calmer than her usual"
    else:
        words = "easing down, message %d of %d" % (s["step_i"], n)
    if not worked_up:
        words += ", holding while you settle"
    s["updated"] = now
    return s, target, words


# ---------- 8.3 to 8.6 style ----------

def learned(con, style, now):
    r = con.execute("select adj, muted_until from style_learning where style = ?", (style,)).fetchone()
    if not r:
        return 0, None
    adj, muted = r
    return int(adj), (muted if muted and muted > now else None)


def pick_style(her, him, learn_adj, muted_until, t):
    """8.3 to 8.6. Returns the would-be style, strength, sass, warmth and whether a curse would be allowed."""
    style = STYLE_OF.get(her["name"], "plain")
    notes = []
    base = INTENSITY_STRENGTH.get(her["intensity"], 0)
    sass = False
    if style == "dry":
        sass = her["irritation"] < t["sass_switch"]
        base = max(base, 1 + int(round(her["irritation"] * 2)))   # drier and more focused as irritation rises
        if not sass:
            notes.append("no sass, more focused")
    if muted_until:
        notes.append("%s is off for now (you said knock it off)" % style)
        style, base, sass = "plain", 0, False
    strength = base
    if style != "plain":   # learn_adj is already held to +-learn_max by learn()
        strength = max(0, min(_range(t, style), base + learn_adj))
    if him["stressed"]:   # 8.5: his stress wins
        if style == "playful":
            style, strength = "plain", 0
            notes.append("no teasing: you're not in a good place")
        sass = False
        strength = min(strength, 1)
    warmth = 0
    if him["stressed"] or him["personal"]:
        warmth = int(min(t["warmth_cap"], 1))
    curse_ok = bool(t["banter_on"]) and not him["stressed"] and not him["cursing_now"] \
        and (style == "playful" or (style == "dry" and sass))
    return {"style": style, "strength": int(strength), "sass": sass, "warmth": warmth, "curse_ok": curse_ok,
            "notes": notes}


def line_words(p, energy_words):
    parts = [STYLE_WORDS[p["style"]] + (" style, strength %d" % p["strength"] if p["style"] != "plain" else " style")]
    if p["style"] == "dry":
        parts.append("light sass at the problem only" if p["sass"] else "no sass")
    parts.append("energy: " + energy_words)
    if p["warmth"]:
        parts.append("a little warmer toward what he said")
    parts.append("cursing ok (playful, at things)" if p["curse_ok"] else "no cursing")
    if p.get("avoid"):
        parts.append("don't reuse %d recent bit%s" % (len(p["avoid"]), "" if len(p["avoid"]) == 1 else "s"))
    return "; ".join(parts)


# ---------- 8.7 learning from his reactions ----------

def read_reaction(text):
    t = reading._clean(text)
    raw = text or ""
    if any(re.search(p, t) for p in KNOCK_IT_OFF):
        return "knock_it_off"
    if any(re.search(p, t) if p.startswith("\\") else p in raw for p in PLAYS_ALONG):
        return "plays_along"
    return "ignores"


def learn(con, style, reaction, now, t):
    """8.9: this writes style_learning only, and only a style's strength step and mute time. Nothing else."""
    if style not in RANGE or reaction not in REACTIONS:
        return None
    r = con.execute("select adj, streak_kind, streak_n from style_learning where style = ?", (style,)).fetchone()
    adj, kind, n = (r if r else (0, None, 0))
    muted = None
    moved = 0
    if reaction == "knock_it_off":
        moved = -1
        adj, kind, n = max(-int(t["learn_max"]), adj - 1), None, 0
        muted = now + t["mute_hours"] * 3600
    else:
        n = n + 1 if kind == reaction else 1
        kind = reaction
        if n >= t["learn_same"]:
            moved = 1 if reaction == "plays_along" else -1
            adj = max(-int(t["learn_max"]), min(int(t["learn_max"]), adj + moved))
            kind, n = None, 0
    con.execute("insert into style_learning(style, adj, streak_kind, streak_n, muted_until, updated) "
                "values(?,?,?,?,?,?) on conflict(style) do update set adj = excluded.adj, "
                "streak_kind = excluded.streak_kind, streak_n = excluded.streak_n, "
                "muted_until = coalesce(excluded.muted_until, style_learning.muted_until), updated = excluded.updated",
                (style, adj, kind, n, muted, now))
    return {"style": style, "reaction": reaction, "adj": adj, "moved": moved, "muted_until": muted}


def react(con, mid, text, m, now, t):
    """His reply to her last reply. Counts only when that reply was actually styled (never in shadow), and never when
    Step 6 heard a correction: a correction is about what she said, not how."""
    prev = [r for r in log_rows(con, m["at"] - 3 * 3600, ("line",)) if r["at"] < m["at"] and r["message_id"] != mid]
    if not prev:
        return None
    last = prev[-1]
    if not last["styled"] or last["style"] == "plain":
        return None
    if m.get("correction"):
        _log(con, "reaction", now, {"skipped": "a correction is about what she said, not how", "style": last["style"]},
             mid)
        return None
    reaction = read_reaction(text)
    out = learn(con, last["style"], reaction, now, t)
    _log(con, "learn" if out and out["moved"] else "reaction", now, {"style": last["style"], **(out or {})}, mid,
         styled=True)
    return out


# ---------- 8.8 joke cooldown ----------

def _bit_key(phrase):
    norm = re.sub(r"[^a-z0-9' ]+", " ", reading._clean(phrase))
    norm = re.sub(r"\s+", " ", norm).strip()
    return hashlib.sha256(norm.encode()).hexdigest()[:24], norm[:BIT_MAX]


def record_bit(con, phrase, now=None, tune=None):
    """For the rewording pass (Step 3, later): note a bit it used. Returns whether it was a repeat inside the
    cooldown (logged as repeat_bit). Only her own words are kept here, never his."""
    now = time.time() if now is None else now
    t = _tune(tune)
    prepare(con)
    if not isinstance(phrase, str) or not phrase.strip():
        raise StyleError("bad_value", "a bit needs some words")
    key, norm = _bit_key(phrase)
    repeat = is_cooling(con, phrase, now, t)
    con.execute("insert into style_bits(key, phrase, at) values(?,?,?)", (key, norm, now))
    _log(con, "repeat_bit" if repeat else "bit", now, {"phrase": norm}, styled=True)
    return repeat


def is_cooling(con, phrase, now=None, tune=None):
    now = time.time() if now is None else now
    t = _tune(tune)
    key, _ = _bit_key(phrase)
    return con.execute("select 1 from style_bits where key = ? and at > ?",
                       (key, now - t["bit_days"] * 86400)).fetchone() is not None


def cooling(con, now, t):
    rows = con.execute("select phrase, max(at) from style_bits where at > ? group by key order by 2 desc limit 20",
                       (now - t["bit_days"] * 86400,)).fetchall()
    return [r[0] for r in rows]


# ---------- 8.10 the per-message run ----------

def take(message_id, text, con=None, now=None, tune=None, her=None):
    """One of his typed messages, after Step 7 has read it (the words copy from /app/trust/words). Works out the
    would-be style for her NEXT reply and logs it. His text is read and dropped. Never raises on bad input; returns
    None when Step 7 has no row for the message."""
    now = time.time() if now is None else now
    if not isinstance(message_id, str) or not message_id or not isinstance(text, str):
        return None
    t = _tune(tune)
    t7 = reading._tune()
    con, own = reading._open(con)
    try:
        prepare(con)
        reading.prepare(con)
        m = reading.message(con, message_id)
        if m is None:
            return None
        facts = {"swears": swear_count(text), "caps": caps_count(text), "rapid": rapid(con, m["at"], t)}
        reaction = react(con, message_id, text, m, now, t)
        him = his_read(con, m, facts, now, t, t7)
        con.execute("insert or replace into style_messages(message_id, at, day, situation, swears, caps, rapid, "
                    "worked_up, why, reaction, received) values(?,?,?,?,?,?,?,?,?,?,?)",
                    (message_id, m["at"], m["day"], m["situation"], facts["swears"], facts["caps"], facts["rapid"],
                     int(him["worked_up"]), him["why"], (reaction or {}).get("reaction"), now))
        her = her if her is not None else her_state()
        step = energy_step(her, t)
        s, target, ewords = energy_plan(_state(con), him["worked_up"], step, now, t)
        _save_state(con, s, now)
        adj, muted = learned(con, STYLE_OF.get(her["name"], "plain"), now)
        p = pick_style(her, him, adj, muted, t)
        p["avoid"] = cooling(con, now, t)
        built = {"style": p["style"], "strength": p["strength"], "sass": p["sass"], "warmth": p["warmth"],
                 "curse_ok": p["curse_ok"], "energy_step": target, "her_step": step, "energy": ewords,
                 "phase": s["phase"], "him": {k: him[k] for k in ("worked_up", "why", "stressed", "signals",
                                                                   "enough")},
                 "her": {k: her[k] for k in ("name", "intensity", "irritation")}, "notes": p["notes"],
                 "avoid": len(p["avoid"]), "line": line_words(p, ewords), "shadow": SHADOW}
        _log(con, "line", now, built, message_id, styled=not SHADOW)
        con.execute("delete from style_messages where at < ?", (now - t7["keep_days"] * 86400,))
        con.execute("delete from style_log where at < ?", (now - LINE_KEEP_DAYS * 86400,))
        con.execute("delete from style_bits where at < ?", (now - max(t["bit_days"], 1) * 86400 * 2,))
        return built
    finally:
        if own:
            con.close()


def for_expression(con=None):
    """The one door a rewording pass may use: the latest would-be style line plus the fixed rules. "" in shadow and
    on any error (emotion colors how she speaks, never how she works)."""
    if SHADOW:
        return ""
    try:
        con, own = reading._open(con)
        try:
            prepare(con)
            r = con.execute("select row from style_log where kind = 'line' order by id desc limit 1").fetchone()
        finally:
            if own:
                con.close()
        if not r:
            return ""
        return json.loads(r[0])["line"] + "\n" + "\n".join(FIXED_RULES)
    except Exception:  # noqa: BLE001
        return ""


# ---------- what the Layer 8 card and `rivia style` show ----------

def view(con=None, now=None, tune=None):
    """Everything the Layer 8 card shows. Numbers and code's own sentences only; none of his text."""
    now = time.time() if now is None else now
    t = _tune(tune)
    con, own = reading._open(con)
    try:
        prepare(con)
        lines = log_rows(con, now - 14 * 86400, ("line",))
        learnings = log_rows(con, now - 14 * 86400, ("reaction", "learn"))
        msgs = con.execute("select count(*), sum(worked_up), sum(swears > 0), sum(rapid) from style_messages "
                           "where at >= ?", (now - 14 * 86400,)).fetchone()
        learn_rows = con.execute("select style, adj, streak_kind, streak_n, muted_until from style_learning "
                                 "order by style").fetchall()
        state = _state(con)
        bits = cooling(con, now, t)
    finally:
        if own:
            con.close()
    counts = {}
    for r in lines:
        counts[r["style"]] = counts.get(r["style"], 0) + 1
    return {
        "ok": True, "layer": 8, "shadow": SHADOW,
        "note": None if lines else "nothing worked out yet: Step 8 runs on the copies of your typed Rivia messages "
                                   "(after Step 7 reads them), and none have arrived",
        "latest": lines[-1] if lines else None,
        "recent": lines[-10:],
        "style_counts": counts,
        "energy": {"phase": state["phase"], "step_i": state["step_i"], "calm_streak": state["calm_streak"]},
        "messages_14d": {"total": msgs[0] or 0, "worked_up": msgs[1] or 0, "swearing": msgs[2] or 0,
                         "rapid_fire": msgs[3] or 0},
        "learning": [{"style": s, "adj": a, "streak": k, "streak_n": n,
                      "muted_until": mu if mu and mu > now else None} for s, a, k, n, mu in learn_rows],
        "reactions": learnings[-10:],
        "bits_cooling": len(bits),
        "fixed_rules": FIXED_RULES,
        "waits_on": ["Her replies only change once Step 3's rewording pass is live (parked). Until then this is "
                     "what she would have done.",
                     "Learning and the joke cooldown stay empty until her replies are actually styled.",
                     "Your energy is judged against your normal from Step 7, so it needs enough typed messages "
                     "in a situation first. Until then only what you say (\"I'm stressed\") counts."],
    }


def main(argv=None):
    v = view()
    print("Step 8 (shadow): how she would sound")
    if v["note"]:
        print("  " + v["note"])
    if v["latest"]:
        print("  latest: %s" % v["latest"]["line"])
    print("  energy plan: %s" % v["energy"]["phase"])
    print("  last 14 days: %(total)d messages, %(worked_up)d worked up, %(swearing)d with swearing, "
          "%(rapid_fire)d rapid-fire" % v["messages_14d"])
    for r in v["learning"]:
        print("  learned %s: %+d%s" % (r["style"], r["adj"], " (off for now)" if r["muted_until"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
