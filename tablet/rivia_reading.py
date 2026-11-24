# rivia_reading.py: Rivia reading Jordan by what he does, not what he says he feels (emotion spec, Step 7).
# DRAFT, Nov 24, not installed. Shadow only: everything here works things out and logs them. Nothing reaches her
# replies, nothing moves her mood, and she never writes any of it.
#
# Jordan's decisions (Nov 24):
#   Situations come from his calendar, read-only ("Your calendar"). Giant is dropped ("Drop giant"): only HMC shifts
#   and days off. So the situations are: before an HMC shift, after an HMC shift, day off, other.
#   No mood button ("I never wanted a mood button"). His mood comes from "my vad scores and what I say", "or me
#   telling her if she asks".
#   His VAD scores: a word list (NRC VAD lexicon) and a Flash read of every typed message, on the tablet
#   ("Every message", "lets do the tablet"). Flash lives in rivia_reading_flash.py.
#
# Pieces in this file:
#   7.1 message facts   per typed message, numbers only: when, length, minutes since her reply, clipped or not,
#                       a correction or not (Step 6's word list), his word-list V/A/D, anything he said about how he
#                       is ("I'm wiped"). His text is read and dropped. It is never stored here.
#   7.2 situation       from HMC shift events on his calendar ("Work Shift: HMC"). Only shift start and end times are
#                       kept; no titles, places or other events.
#   7.3 his normal      per situation: the middle value and his usual swing for each measure, from his own messages.
#                       Empty until enough messages in that situation.
#   7.4 how far off     the last few messages against his normal there: normal, a bit off, or well off.
#   7.7 pattern guesses repeats the code spots ("after HMC shifts your messages run about 40% shorter"). Guesses until
#                       Jordan confirms or rejects them in the app. Only confirmed ones count.
#   7.8 ask or not      code, not her, decides: weak signal = nothing; unclear = she would ask; a confirmed pattern =
#                       she would quietly adjust. Daily cap, brush-off shelves a topic. In shadow it only logs.
#   7.9 second opinion  for personal messages (word list stand-in for Step 3's parked "personal disclosure" event), the
#                       Flash read also says whether another reading is likely. If so, that's a reason to ask.
# Left out on purpose: 7.5 mood button (dropped), 7.6 voice scores (not built), 7.10 mood colors how she reads him
# (needs Step 3, parked).
#
# Writes body.db only (tables reading_*). Never emotion.db, never a sign-in file. Never an MCP tool.
import json
import math
import re
import statistics
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

# ---------- starting numbers (all adjustable in the Layer 7 card, rivia_reading_settings) ----------
TERSE_WORDS = 4               # 7.1 a message this many words or fewer counts as clipped
SHIFT_TITLE = "work shift: hmc"   # 7.2 calendar events whose title starts with this are HMC shifts (fixed, not a card)
AFTER_SHIFT_HOURS = 10.0      # 7.2 "after an HMC shift" lasts this long after the shift ends
CAL_FRESH_HOURS = 6.0         # 7.2 re-read today's shifts after this long (swaps)
CAL_RETRY_MIN = 30.0          # 7.2 after a failed calendar read, wait this long before trying again
MIN_MESSAGES = 25             # 7.3 typed messages in a situation before its normal counts
KEEP_DAYS = 60                # 7.3 his normal is built from the last this many days
RUN_SIZE = 5                  # 7.4 "right now" = his last this many messages...
RUN_HOURS = 3.0               # 7.4 ...within this many hours
BIT_OFF = 1.0                 # 7.4 further from normal than his usual swing = a bit off
WELL_OFF = 2.0                # 7.4 twice his usual swing = well off
PATTERN_DAYS = 10             # 7.7 seen on this many separate days before it's shown as a guess
PATTERN_DIFF = 0.30           # 7.7 a situation has to differ by this much (30%) from his other times
ASKS_PER_DAY = 2              # 7.8 at most this many would-be questions a day
SHELVE_DAYS = 7               # 7.8 a brushed-off topic is left alone this long
STRONG_VALENCE = 0.5          # 7.8 with little data, only a reading this strong is worth a question
MIXED_FLASH_MIN = 0.33        # 7.8 the word list and Flash "disagree a lot" when they point opposite ways and Flash
                              #     rated at least 1 on its -3..3 scale

SITUATIONS = {
    "before_shift": "before an HMC shift",
    "after_shift": "after an HMC shift",
    "day_off": "a day off",
    "other": "other times (during a shift, or the calendar couldn't be read)",
}
# What each measure means, in plain words, and how it's compared.
#   kind "num":  middle value and usual swing (median and spread) of single messages
#   kind "log":  same, on minutes taken as a ratio (a 2 minute reply vs 20 is the same jump as 20 vs 200)
#   kind "rate": share of messages (clipped, corrections); swing is the normal wobble for a handful of messages
MEASURES = {
    "words":   {"kind": "num",  "words": "message length", "unit": "words", "floor": 2.0,
                "up": "writing longer messages", "down": "writing shorter messages"},
    "gap_min": {"kind": "log",  "words": "reply speed", "unit": "minutes", "floor": 0.35,
                "up": "slower to reply", "down": "quicker to reply"},
    "terse":   {"kind": "rate", "words": "clipped messages", "unit": "share", "floor": 0.10,
                "up": "more clipped", "down": "less clipped"},
    "correction": {"kind": "rate", "words": "corrections", "unit": "share", "floor": 0.10,
                   "up": "correcting me more", "down": "correcting me less"},
    "valence": {"kind": "num",  "words": "how pleasant your words read", "unit": "-1 to +1", "floor": 0.15,
                "up": "using lighter words", "down": "using heavier words"},
    "arousal": {"kind": "num",  "words": "how charged your words read", "unit": "-1 to +1", "floor": 0.15,
                "up": "using more charged words", "down": "using calmer words"},
}
WHEN = {"before_shift": "before your shift", "after_shift": "since your shift", "day_off": "today", "other": "today"}

# 7.1 what he says about how he is. Starts with "I'm" (or "I am", "feeling"), so "I'm not tired" doesn't match,
# and a question ("am I tired?") is skipped. These are the answer key, not a guess.
SAID = (
    ("tired", r"(?:i'?m|i am|feeling|feel) (?:so |really |pretty |super |kinda |dead )?(?:tired|exhausted|wiped|"
              r"beat|drained|worn out|spent|fried)"),
    ("stressed", r"(?:i'?m|i am|feeling|feel) (?:so |really |pretty |super |kinda )?(?:stressed|overwhelmed|swamped|"
                 r"slammed|under pressure)"),
    ("annoyed", r"(?:i'?m|i am|feeling|feel) (?:so |really |pretty |super |kinda )?(?:annoyed|irritated|pissed|"
                r"frustrated|fed up|aggravated|over it)"),
    ("good", r"(?:i'?m|i am|feeling|feel) (?:so |really |pretty |super )?(?:good|great|doing good|doing well|"
             r"doing great|happy|stoked)"),
    ("fine", r"(?:i'?m|i am) (?:fine|ok|okay|alright|all right)"),
)
SAID_LOW = {"tired", "stressed", "annoyed"}     # labels that agree with a heavier read
SAID_HIGH = {"good"}

# 7.9 stand-in for "this is a personal moment" (Step 3's parked "personal disclosure" event). Only decides whether the
# Flash read also gives a second opinion. Whole words, lower case.
PERSONAL = (r"i feel\b", r"i'?ve been feeling", r"my (?:son|daughter|kid|kids|baby|child|mom|mother|dad|father|"
            r"family|wife|husband|girlfriend|boyfriend|partner|brother|sister|friend)\b", r"\bmiss (?:him|her|them|"
            r"you|it)\b", r"\blonely\b", r"\bhurts?\b", r"\bgrief\b", r"\bpassed away\b", r"\bfuneral\b",
            r"\bdepressed\b", r"\banxious\b", r"\bscared\b", r"\bworried about\b", r"\bpersonal\b",
            r"\bbreak ?up\b", r"\bdivorce\b", r"\bcustody\b")

# 7.8 his answer to a question she asked (only once asking leaves shadow; in shadow she never asks).
BRUSH = (r"\bi'?m fine\b", r"\bdrop it\b", r"\bnot now\b", r"\bleave it\b", r"\bwhatever\b", r"\bdon'?t worry about "
         r"it\b", r"\bit'?s nothing\b", r"\bforget it\b", r"\bnever ?mind\b", r"\bmove on\b")
YES = (r"^\s*(?:yes|yeah|yep|yup|ya|correct|right|you'?re right|kinda|a bit|a little)\b",)
NO = (r"^\s*(?:no|nope|nah|not really|wrong)\b",)

# 7.8 wording rules. Written now, in force only when asking leaves shadow (never injected in shadow).
WORDING_RULES = [
    "Say what you saw, never what he feels: \"you've been quieter than usual since your shift. Am I reading that "
    "right?\", never \"you seem anxious\".",
    "One short question at a time.",
    "Personal things he shares: reflect it back and ask before reading anything into it.",
    "Never say \"I understand\" unless she does. Instead: \"I don't fully get that yet. What's it like for you?\"",
    "If he brushes a question off, drop it.",
]

SCHEMA = """
create table if not exists reading_messages(
    message_id text primary key, at real not null, day text not null, situation text not null,
    words integer not null, gap_min real, terse integer not null, correction integer not null,
    lex_v real, lex_a real, lex_d real, lex_matched integer not null default 0,
    flash_v real, flash_a real, flash_d real, flash_status text not null default 'waiting',
    personal integer not null default 0, other_reading integer, said text, received real not null);
create index if not exists reading_messages_at on reading_messages(at);
create table if not exists reading_shifts(
    day text primary key, shifts text not null, ok integer not null, read_at real not null, note text);
create table if not exists reading_log(
    id integer primary key autoincrement, at real not null, day text not null, kind text not null,
    situation text, topic text, row text not null);
create index if not exists reading_log_at on reading_log(at);
create table if not exists reading_patterns(
    id integer primary key autoincrement, situation text not null, measure text not null, direction text not null,
    state text not null, words text not null, size real not null, days_seen integer not null,
    found_at real not null, updated_at real not null, decided_by text, decided_at real, note text,
    unique(situation, measure, direction));
"""
LOG_KINDS = ("read", "would_ask", "would_adjust", "skip_ask", "check", "answer")
DECIDE_DOORS = ("app_button",)          # only Jordan's app button confirms or rejects a pattern
NOTE_MAX = 200


class ReadingError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _guard(con):
    for _, name, path in con.execute("pragma database_list").fetchall():
        if name == "main" and path and path.replace("\\", "/").rsplit("/", 1)[-1] == "emotion.db":
            raise ReadingError("wrong_db", "Step 7 rows never go in emotion.db")


def prepare(con):
    _guard(con)
    con.executescript(SCHEMA)
    return con


def _tune(values=None):
    """The starting numbers, or Jordan's edits from the Layer 7 card. Never raises."""
    if values is not None:
        return values
    try:
        import rivia_reading_settings as rs
        return rs.tune()
    except Exception as e:  # noqa: BLE001  (a broken settings file must not stop reading)
        print("rivia: reading settings unreadable (%s); using the code's numbers." % type(e).__name__,
              file=sys.stderr)
        return code_tune()


def code_tune():
    return {"terse_words": TERSE_WORDS, "after_shift_hours": AFTER_SHIFT_HOURS, "min_messages": MIN_MESSAGES,
            "keep_days": KEEP_DAYS, "run_size": RUN_SIZE, "run_hours": RUN_HOURS, "bit_off": BIT_OFF,
            "well_off": WELL_OFF, "pattern_days": PATTERN_DAYS, "pattern_diff": PATTERN_DIFF,
            "asks_per_day": ASKS_PER_DAY, "shelve_days": SHELVE_DAYS, "strong_valence": STRONG_VALENCE,
            "mixed_flash_min": MIXED_FLASH_MIN, "flash_on": 1}


def _day(at):
    return datetime.fromtimestamp(at, NY).date().isoformat()


def _clean(text):
    return re.sub(r"[‘’ʼ]", "'", (text or "").lower())


# ---------- 7.1 message facts ----------

WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def count_words(text):
    return len(WORD_RE.findall(_clean(text)))


def said_label(text):
    """What he said about how he is, or None. Skips a question and a negated one ("I'm not tired")."""
    t = _clean(text)
    for label, pat in SAID:
        m = re.search(r"\b" + pat + r"\b", t)
        if not m:
            continue
        rest = re.split(r"(?<=[.!?\n])", t[m.start():], maxsplit=1)[0]
        if rest.rstrip().endswith("?"):
            continue
        return label
    return None


def is_personal(text):
    t = _clean(text)
    return any(re.search(p, t) for p in PERSONAL)


_LEX = {"loaded": False, "lex": None, "problem": None}


def lexicon():
    """The NRC VAD word list, from the file Jordan downloads (its terms don't allow copying it into the code). Path:
    RIVIA_NRC_LEXICON, or ~/rivia/NRC-VAD-Lexicon-v2.1.txt. Missing = no word-list scores, said plainly."""
    if not _LEX["loaded"]:
        import os
        _LEX["loaded"] = True
        p = os.environ.get("RIVIA_NRC_LEXICON") or os.path.join(
            os.path.expanduser(os.environ.get("RIVIA_DIR", "~/rivia")), "NRC-VAD-Lexicon-v2.1.txt")
        try:
            import baseline
            from pathlib import Path
            if not os.path.isfile(p):
                raise FileNotFoundError(p)
            _LEX["lex"] = baseline.Lexicon(Path(p))
        except (Exception, SystemExit) as e:  # noqa: BLE001
            _LEX["problem"] = ("the NRC word list isn't on the tablet, so there are no word-list scores"
                               if isinstance(e, FileNotFoundError) else
                               "the NRC word list couldn't be read (%s)" % type(e).__name__)
    return _LEX["lex"], _LEX["problem"]


def lexicon_vad(text, lex=None):
    """His word-list V/A/D (-1..+1). None per axis when no word matched. Pulls toward 0; reads "not happy" as happy."""
    if lex is None:
        lex, _ = lexicon()
    if lex is None:
        return {"v": None, "a": None, "d": None, "matched": 0}
    import baseline
    s = baseline.lexicon_score(text or "", lex)
    return {"v": s["v"], "a": s["a"], "d": s["d"], "matched": s["matched"]}


def facts(text, *, at, reply_at=None, tune=None, lex=None):
    """7.1: the numbers kept for one typed message. The text itself is not in the result."""
    import rivia_satisfaction as sat
    t = _tune(tune)
    n = count_words(text)
    gap = None
    if reply_at is not None and reply_at <= at and at - reply_at <= 12 * 3600:
        gap = round((at - reply_at) / 60.0, 3)
    corr, _ = sat.read_words(text)
    lv = lexicon_vad(text, lex)
    return {"words": n, "gap_min": gap, "terse": int(n <= t["terse_words"]), "correction": int(corr == -1.0),
            "lex_v": lv["v"], "lex_a": lv["a"], "lex_d": lv["d"], "lex_matched": lv["matched"],
            "said": said_label(text), "personal": int(is_personal(text))}


# ---------- 7.2 situation ----------

def _calendar_read(time_min, time_max):
    """Read-only calendar events, through the same read the 05:30 sign-in check uses. Tests replace CALENDAR."""
    import rivia_gcal
    return rivia_gcal.list_events_between(time_min, time_max)


CALENDAR = _calendar_read


def _parse_shifts(events):
    """HMC shift times only, as (start, end) epoch seconds. Everything else about an event is dropped here."""
    out = []
    for ev in events or []:
        if not isinstance(ev, dict) or ev.get("all_day"):
            continue
        if not str(ev.get("title") or "").strip().lower().startswith(SHIFT_TITLE):
            continue
        try:
            s = datetime.fromisoformat(str(ev["start"]).replace("Z", "+00:00")).timestamp()
            e = datetime.fromisoformat(str(ev["end"]).replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            continue
        if e > s:
            out.append((s, e))
    return sorted(out)


def shifts_for(con, day, now):
    """Shift times starting on `day` (YYYY-MM-DD, New York), from the cache or one calendar read. (shifts, ok)."""
    prepare(con)
    row = con.execute("select shifts, ok, read_at from reading_shifts where day = ?", (day,)).fetchone()
    today = _day(now)
    if row:
        shifts, ok, read_at = json.loads(row[0]), bool(row[1]), row[2]
        stale = (day >= today and now - read_at > CAL_FRESH_HOURS * 3600) or \
                (not ok and now - read_at > CAL_RETRY_MIN * 60)
        if not stale:
            return [tuple(x) for x in shifts], ok
    d = datetime.fromisoformat(day).replace(tzinfo=NY)
    try:
        events = CALENDAR(d.isoformat(), (d + timedelta(days=1)).isoformat())
        shifts, ok, note = [s for s in _parse_shifts(events) if _day(s[0]) == day], True, None
    except Exception as e:  # noqa: BLE001  (calendar down or signed out: "other", say why, try again later)
        shifts, ok, note = [], False, "calendar not readable (%s)" % (getattr(e, "code", None) or type(e).__name__)
    con.execute("insert or replace into reading_shifts values(?,?,?,?,?)",
                (day, json.dumps(shifts), int(ok), now, note))
    return shifts, ok


def situation(con, at, now=None, tune=None):
    """7.2: which situation a message at `at` is in. Reads his calendar for that day and the day before."""
    now = time.time() if now is None else now
    t = _tune(tune)
    day = _day(at)
    prev = (datetime.fromisoformat(day) - timedelta(days=1)).date().isoformat()
    today, ok1 = shifts_for(con, day, now)
    yday, ok2 = shifts_for(con, prev, now)
    if not (ok1 and ok2):
        return "other"
    window = t["after_shift_hours"] * 3600
    for s, e in today + yday:
        if s <= at < e:
            return "other"               # during a shift
    ended = [e for s, e in today + yday if e <= at]
    if ended and at - max(ended) < window:
        return "after_shift"
    if any(s > at for s, e in today):
        return "before_shift"
    if not today:
        return "day_off"
    return "other"                       # a shift day, after the after-shift window


# ---------- storing a message ----------

def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def when_sent(body, now):
    """The app's sent_at / reply_at (milliseconds). A time more than a day off the tablet's clock isn't trusted."""
    def ms(k):
        v = body.get(k)
        if not _num(v):
            return None
        s = float(v) / 1000.0
        return s if abs(s - now) <= 86400 else None
    at = ms("sent_at")
    return (at if at is not None else now), ms("reply_at")


def take(body, con=None, now=None, tune=None, lex=None):
    """The words copy from /app/trust/words (Step 6.3), read for Step 7. Stores the numbers, drops the text, logs the
    read. Returns the stored row (no text) for the Flash read to fill in later. Never raises on bad input; returns
    None when there's nothing to read."""
    now = time.time() if now is None else now
    if not isinstance(body, dict):
        return None
    mid, text = body.get("message_id"), body.get("text")
    if not isinstance(mid, str) or not mid or len(mid) > 80 or not isinstance(text, str) or not text.strip():
        return None
    t = _tune(tune)
    con, own = _open(con)
    try:
        prepare(con)
        old = con.execute("select at from reading_messages where message_id = ?", (mid,)).fetchone()
        at, reply_at = when_sent(body, now)
        if old:
            at = old[0]                  # an edit is sent again with the same id: keep when it was first sent
        f = facts(text, at=at, reply_at=reply_at, tune=t, lex=lex)
        sit = situation(con, at, now, tune=t)
        flash = "waiting" if t.get("flash_on", 1) else "off"
        con.execute("insert or replace into reading_messages(message_id, at, day, situation, words, gap_min, terse, "
                    "correction, lex_v, lex_a, lex_d, lex_matched, flash_status, personal, said, received) "
                    "values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, at, _day(at), sit, f["words"], f["gap_min"], f["terse"], f["correction"], f["lex_v"],
                     f["lex_a"], f["lex_d"], f["lex_matched"], flash, f["personal"], f["said"], now))
        con.execute("delete from reading_messages where at < ?", (now - t["keep_days"] * 86400,))
        con.execute("delete from reading_log where at < ?", (now - t["keep_days"] * 86400,))
        answer_last_ask(con, text, at, now)
        judge(con, now=now, tune=t, message_id=mid)
        return message(con, mid)
    finally:
        if own:
            con.close()


def store_flash(con, message_id, vad, status, other_reading=None, now=None, tune=None):
    """The Flash read's scores for one message (rivia_reading_flash). Then the read is judged again."""
    prepare(con)
    vad = vad or {}
    con.execute("update reading_messages set flash_v = ?, flash_a = ?, flash_d = ?, flash_status = ?, "
                "other_reading = case when personal = 1 then ? else null end where message_id = ?",
                (vad.get("v"), vad.get("a"), vad.get("d"), status,
                 None if other_reading is None else int(bool(other_reading)), message_id))
    if status == "ok":
        judge(con, now=now, tune=tune, message_id=message_id)
    return message(con, message_id)


COLS = ("message_id", "at", "day", "situation", "words", "gap_min", "terse", "correction", "lex_v", "lex_a", "lex_d",
        "lex_matched", "flash_v", "flash_a", "flash_d", "flash_status", "personal", "other_reading", "said")


def _row(r):
    return dict(zip(COLS, r))


def message(con, message_id):
    r = con.execute("select %s from reading_messages where message_id = ?" % ", ".join(COLS),
                    (message_id,)).fetchone()
    return _row(r) if r else None


def messages(con, since):
    return [_row(r) for r in con.execute("select %s from reading_messages where at >= ? order by at"
                                         % ", ".join(COLS), (since,)).fetchall()]


def mood_of(m):
    """His V and A for one message: Flash and the word list averaged when both are there, else whichever is."""
    out = {}
    for ax, lk, fk in (("valence", "lex_v", "flash_v"), ("arousal", "lex_a", "flash_a")):
        xs = [m[k] for k in (lk, fk) if m.get(k) is not None]
        out[ax] = round(sum(xs) / len(xs), 4) if xs else None
    return out


def mixed(m, tune=None):
    """True when the word list and Flash point opposite ways on how pleasant the message reads, or the message is
    clipped but reads upbeat."""
    t = _tune(tune)
    lv, fv = m.get("lex_v"), m.get("flash_v")
    if lv is not None and fv is not None and abs(fv) >= t["mixed_flash_min"] and abs(lv) >= 0.05 and lv * fv < 0:
        return True
    v = mood_of(m)["valence"]
    return bool(m.get("terse")) and v is not None and v >= t["mixed_flash_min"]


def value(m, measure):
    if measure in ("valence", "arousal"):
        return mood_of(m)[measure]
    return m.get(measure)


# ---------- 7.3 his normal ----------

def _spread(xs):
    med = statistics.median(xs)
    return med, 1.4826 * statistics.median([abs(x - med) for x in xs])


def normal(msgs, measure, tune=None):
    """His normal for one measure from `msgs` (one situation): middle value and usual swing. None until enough."""
    t = _tune(tune)
    spec = MEASURES[measure]
    xs = [value(m, measure) for m in msgs]
    xs = [x for x in xs if x is not None]
    if len(xs) < t["min_messages"]:
        return {"enough": False, "count": len(xs), "need": t["min_messages"]}
    if spec["kind"] == "rate":
        p = sum(xs) / len(xs)
        return {"enough": True, "count": len(xs), "middle": round(p, 4), "rate": round(p, 4)}
    ys = [math.log(max(x, 0.05)) for x in xs] if spec["kind"] == "log" else xs
    med, sp = _spread(ys)
    return {"enough": True, "count": len(xs), "middle": round(math.exp(med) if spec["kind"] == "log" else med, 4),
            "center": med, "swing": max(sp, spec["floor"])}


def normals(con, now=None, tune=None):
    """7.3 for every situation and measure."""
    now = time.time() if now is None else now
    t = _tune(tune)
    msgs = messages(con, now - t["keep_days"] * 86400)
    out = {}
    for s in SITUATIONS:
        mine = [m for m in msgs if m["situation"] == s]
        out[s] = {"messages": len(mine), "measures": {k: normal(mine, k, t) for k in MEASURES}}
    return out


# ---------- 7.4 how far off ----------

def distance(run, norm, measure):
    """How far the last few messages are from his normal, in his usual swings. None if no normal or no data."""
    if not norm.get("enough"):
        return None
    spec = MEASURES[measure]
    xs = [value(m, measure) for m in run]
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    if spec["kind"] == "rate":
        p = norm["rate"]
        swing = max(math.sqrt(p * (1 - p) / len(xs)), spec["floor"])
        return (sum(xs) / len(xs) - p) / swing
    ys = [math.log(max(x, 0.05)) for x in xs] if spec["kind"] == "log" else xs
    return (statistics.median(ys) - norm["center"]) / norm["swing"]


def zone(d, tune=None):
    t = _tune(tune)
    if d is None:
        return "no_data"
    return "well_off" if abs(d) >= t["well_off"] else "bit_off" if abs(d) >= t["bit_off"] else "normal"


def current_run(con, now, tune=None):
    t = _tune(tune)
    msgs = messages(con, now - t["run_hours"] * 3600)
    return [m for m in msgs if m["at"] <= now][-int(t["run_size"]):]


def read_now(con, now=None, tune=None):
    """7.4: his last few messages against his normal in the situation he's in now."""
    now = time.time() if now is None else now
    t = _tune(tune)
    run = current_run(con, now, t)
    if not run:
        return None
    sit = run[-1]["situation"]
    run = [m for m in run if m["situation"] == sit]
    norms = normals(con, now, t)[sit]["measures"]
    out = {}
    for k in MEASURES:
        d = distance(run, norms[k], k)
        out[k] = {"zone": zone(d, t), "distance": None if d is None else round(d, 2),
                  "direction": None if d is None or abs(d) < t["bit_off"] else ("up" if d > 0 else "down")}
    return {"at": run[-1]["at"], "situation": sit, "messages": len(run), "measures": out,
            "enough": any(n.get("enough") for n in norms.values())}


# ---------- 7.7 pattern guesses ----------

def _middle(xs, kind):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    return sum(xs) / len(xs) if kind == "rate" else statistics.median(xs)


def _differs(a, b, spec, diff):
    """Direction ("up"/"down") if a differs from b by enough, else None. Relative for length and speed, a share
    for rates, a step on the -1..+1 scale for V and A."""
    if a is None or b is None:
        return None
    if spec["kind"] in ("num", "log") and spec["unit"] not in ("-1 to +1",):
        if b <= 0:
            return None
        r = (a - b) / b
        return ("up" if r > 0 else "down") if abs(r) >= diff else None
    step = diff / 2.0
    return ("up" if a > b else "down") if abs(a - b) >= step else None


def pattern_words(sit, measure, direction, a, b):
    spec = MEASURES[measure]
    when = {"before_shift": "Before HMC shifts", "after_shift": "After HMC shifts", "day_off": "On days off"}[sit]
    if spec["unit"] in ("words", "minutes") and b:
        pct = int(round(abs(a - b) / b * 100 / 10.0) * 10) or 10
        return "%s you're %s, about %d%% %s than other times." % (
            when, spec[direction], pct, {"words": "longer" if direction == "up" else "shorter",
                                         "minutes": "slower" if direction == "up" else "quicker"}[spec["unit"]])
    return "%s you're %s than other times." % (when, spec[direction])


def find_patterns(con, now=None, tune=None):
    """7.7: repeats worth showing Jordan as guesses. A guess needs enough messages on both sides and the same lean
    on enough separate days. Guesses are saved as "proposed"; a decided one (confirmed or rejected) is never
    changed by code, only its numbers are refreshed."""
    now = time.time() if now is None else now
    t = _tune(tune)
    prepare(con)
    msgs = messages(con, now - t["keep_days"] * 86400)
    found = []
    for sit in ("before_shift", "after_shift", "day_off"):
        mine = [m for m in msgs if m["situation"] == sit]
        rest = [m for m in msgs if m["situation"] != sit]
        if len(mine) < t["min_messages"] or len(rest) < t["min_messages"]:
            continue
        for k, spec in MEASURES.items():
            a = _middle([value(m, k) for m in mine], spec["kind"])
            b = _middle([value(m, k) for m in rest], spec["kind"])
            direction = _differs(a, b, spec, t["pattern_diff"])
            if not direction:
                continue
            days = {}
            for m in mine:
                days.setdefault(m["day"], []).append(value(m, k))
            seen = sum(1 for xs in days.values() if _differs(_middle(xs, spec["kind"]), b, spec,
                                                               t["pattern_diff"] / 2.0) == direction)
            if seen < t["pattern_days"]:
                continue
            words = pattern_words(sit, k, direction, a, b)
            size = round(a - b, 4)
            cur = con.execute("select id, state from reading_patterns where situation = ? and measure = ? and "
                              "direction = ?", (sit, k, direction)).fetchone()
            if cur:
                con.execute("update reading_patterns set words = ?, size = ?, days_seen = ?, updated_at = ? "
                            "where id = ?", (words, size, seen, now, cur[0]))
            else:
                con.execute("insert into reading_patterns(situation, measure, direction, state, words, size, "
                            "days_seen, found_at, updated_at) values(?,?,?,?,?,?,?,?,?)",
                            (sit, k, direction, "proposed", words, size, seen, now, now))
            found.append({"situation": sit, "measure": k, "direction": direction, "words": words})
    return found


PCOLS = ("id", "situation", "measure", "direction", "state", "words", "size", "days_seen", "found_at", "updated_at",
         "decided_by", "decided_at", "note")


def patterns(con, state=None):
    prepare(con)
    q = "select %s from reading_patterns" % ", ".join(PCOLS)
    rows = con.execute(q + (" where state = ?" if state else "") + " order by id",
                       (state,) if state else ()).fetchall()
    return [dict(zip(PCOLS, r)) for r in rows]


def decide_pattern(con, pattern_id, confirm, *, door, note=None, now=None):
    """Jordan confirms or rejects a guess, from the app. Only his button. Rivia can't."""
    now = time.time() if now is None else now
    if door not in DECIDE_DOORS:
        raise ReadingError("not_allowed", "only your app's button can confirm or reject a pattern")
    if isinstance(pattern_id, bool) or not isinstance(pattern_id, int):
        raise ReadingError("bad_value", "id must be a pattern's number")
    if not isinstance(confirm, bool):
        raise ReadingError("bad_value", "confirm must be true or false")
    if note is not None and (not isinstance(note, str) or len(note) > NOTE_MAX):
        raise ReadingError("bad_value", "note must be text up to %d characters" % NOTE_MAX)
    prepare(con)
    if not con.execute("select 1 from reading_patterns where id = ?", (pattern_id,)).fetchone():
        raise ReadingError("unknown", "there's no pattern %d" % pattern_id)
    con.execute("update reading_patterns set state = ?, decided_by = ?, decided_at = ?, note = ? where id = ?",
                ("confirmed" if confirm else "rejected", door, now, (note or "").strip() or None, pattern_id))
    return next(p for p in patterns(con) if p["id"] == pattern_id)


# ---------- 7.8 ask or not ----------

def _log(con, kind, at, row, situation=None, topic=None):
    assert kind in LOG_KINDS
    con.execute("insert into reading_log(at, day, kind, situation, topic, row) values(?,?,?,?,?,?)",
                (at, _day(at), kind, situation, topic, json.dumps(row, sort_keys=True)))


def log_rows(con, since, kinds=None):
    prepare(con)
    rows = con.execute("select id, at, kind, situation, topic, row from reading_log where at >= ? order by id",
                       (since,)).fetchall()
    out = [{"id": i, "at": a, "kind": k, "situation": s, "topic": tp, **json.loads(r)} for i, a, k, s, tp, r in rows]
    return [r for r in out if kinds is None or r["kind"] in kinds]


def shelved(con, topic, now, tune=None):
    t = _tune(tune)
    since = now - t["shelve_days"] * 86400
    return any(r["topic"] == topic and r.get("answer") == "brushed_off"
               for r in log_rows(con, since, ("answer",)))


def asks_today(con, now):
    day = _day(now)
    return sum(1 for r in log_rows(con, now - 86400, ("would_ask",)) if r.get("day", _day(r["at"])) == day
               and _day(r["at"]) == day)


def question(sit, measure, direction):
    """Observation, not diagnosis: what she saw, then whether she's reading it right. Never names a feeling."""
    return "You've been %s than usual %s. Am I reading that right?" % (MEASURES[measure][direction], WHEN[sit])


def judge(con, now=None, tune=None, message_id=None):
    """7.8: code decides, from objective signals, whether she'd do nothing, ask, or quietly adjust. Logs it.
    Shadow: nothing reaches her replies, and she never actually asks."""
    now = time.time() if now is None else now
    t = _tune(tune)
    prepare(con)
    last = message(con, message_id) if message_id else None
    rn = read_now(con, now, t)
    if rn is None:
        return None
    sit = rn["situation"]
    off = {k: v for k, v in rn["measures"].items() if v["zone"] in ("bit_off", "well_off")}
    confirmed = [p for p in patterns(con, "confirmed") if p["situation"] == sit]
    triggers = []
    if last and last.get("said"):
        # He told her. That's the answer key, not a reason to ask: log it as a check on the read.
        lab = last["said"]
        v = rn["measures"]["valence"]
        agree = None
        if v["direction"] == "down":
            agree = lab in SAID_LOW
        elif v["direction"] == "up":
            agree = lab in SAID_HIGH
        elif v["zone"] == "normal":
            agree = lab in ("fine",)
        _log(con, "check", now, {"said": lab, "read": {k: x["zone"] for k, x in rn["measures"].items()},
                                 "agree": agree, "note": "you said how you are; she wouldn't ask"}, sit)
        return {"zone": "told", "said": lab}
    if not rn["enough"]:
        triggers.append("little_data")
    if last and mixed(last, t):
        triggers.append("mixed_signals")
    if last and last.get("other_reading"):
        triggers.append("other_reading")
    if off and all(v["zone"] == "bit_off" for v in off.values()):
        triggers.append("only_a_bit_off")
    explained = {(p["measure"], p["direction"]) for p in confirmed}
    if off and any((k, v["direction"]) not in explained for k, v in off.items()):
        triggers.append("not_confirmed")

    strong = False
    if last and not rn["enough"]:
        v = mood_of(last)["valence"]
        strong = v is not None and v <= -t["strong_valence"]
    if confirmed:
        _log(con, "would_adjust", now, {"patterns": [p["words"] for p in confirmed],
                                        "note": "she'd quietly fit her replies to this, without saying so"}, sit)
    if not off and not strong and "other_reading" not in triggers and "mixed_signals" not in triggers:
        _log(con, "read", now, {"zone": "weak", "read": rn, "triggers": triggers}, sit)
        return {"zone": "weak", "triggers": triggers}
    if off and not triggers:
        _log(con, "read", now, {"zone": "confirmed", "read": rn}, sit)
        return {"zone": "confirmed"}
    # Ambiguous: she would ask. Pick what to ask about: the furthest-off measure, else his mood words.
    if off:
        k = max(off, key=lambda x: abs(off[x]["distance"]))
        d = off[k]["direction"]
    else:
        k, d = "valence", "down" if (mood_of(last or {})["valence"] or 0) < 0 else "up"
    topic = "%s:%s:%s" % (sit, k, d)
    q = question(sit, k, d)
    if shelved(con, topic, now, t):
        _log(con, "skip_ask", now, {"why": "you brushed this off within %g days" % t["shelve_days"],
                                    "question": q, "triggers": triggers}, sit, topic)
        return {"zone": "ambiguous", "asked": False, "why": "shelved"}
    if asks_today(con, now) >= t["asks_per_day"]:
        _log(con, "skip_ask", now, {"why": "today's %d questions are used up" % t["asks_per_day"],
                                    "question": q, "triggers": triggers}, sit, topic)
        return {"zone": "ambiguous", "asked": False, "why": "cap"}
    _log(con, "would_ask", now, {"question": q, "triggers": triggers, "read": rn, "sent": False,
                                 "note": "shadow: logged only, she didn't ask"}, sit, topic)
    return {"zone": "ambiguous", "asked": True, "question": q, "triggers": triggers}


def read_answer(text):
    """His reply to a question she asked: yes, no, brushed_off, or None (can't tell)."""
    t = _clean(text)
    if any(re.search(p, t) for p in BRUSH):
        return "brushed_off"
    if any(re.search(p, t) for p in YES):
        return "yes"
    if any(re.search(p, t) for p in NO):
        return "no"
    return None


def answer_last_ask(con, text, at, now):
    """If she actually asked (never in shadow: "sent" is always False), his next message is read as the answer and
    logged; it updates nothing else."""
    asks = [r for r in log_rows(con, at - 3 * 3600, ("would_ask",)) if r.get("sent") and r["at"] < at]
    if not asks:
        return None
    last = asks[-1]
    done = [r for r in log_rows(con, last["at"], ("answer",)) if r.get("ask_id") == last["id"]]
    if done:
        return None
    ans = read_answer(text)
    if ans is None:
        return None
    _log(con, "answer", now, {"ask_id": last["id"], "answer": ans}, last.get("situation"), last.get("topic"))
    return ans


# ---------- what the Layer 7 card and `rivia reading` show ----------

def _open(con):
    if con is not None:
        return con, False
    import rivia_normal as normal_db
    return normal_db.db_open(), True


def view(con=None, now=None, tune=None):
    """Everything the Layer 7 card shows. Numbers and code's own sentences only; none of his text."""
    now = time.time() if now is None else now
    t = _tune(tune)
    con, own = _open(con)
    try:
        prepare(con)
        found = find_patterns(con, now, t)
        norms = normals(con, now, t)
        rn = read_now(con, now, t)
        msgs = messages(con, now - 14 * 86400)
        log = log_rows(con, now - 14 * 86400)
        pats = patterns(con)
        flash_today = [m for m in msgs if m["day"] == _day(now)]
        cal = con.execute("select day, ok, note from reading_shifts order by day desc limit 1").fetchone()
    finally:
        if own:
            con.close()
    _, lex_problem = lexicon()
    checks = [r for r in log if r["kind"] == "check"]
    return {
        "ok": True, "layer": 7, "shadow": True,
        "note": None if any(n["messages"] for n in norms.values()) else
        "nothing read yet: Step 7 reads the copies of your typed Rivia messages (Step 6), and none have arrived",
        "situations": [{"id": s, "words": w, "messages": norms[s]["messages"],
                        "enough": any(x.get("enough") for x in norms[s]["measures"].values()),
                        "normal": {k: {"words": MEASURES[k]["words"], "unit": MEASURES[k]["unit"],
                                       "middle": x.get("middle"), "enough": x["enough"], "count": x["count"],
                                       "need": x.get("need")} for k, x in norms[s]["measures"].items()}}
                       for s, w in SITUATIONS.items()],
        "now": rn,
        "patterns": pats, "new_patterns": found,
        "would_ask": [r for r in log if r["kind"] == "would_ask"][-10:],
        "skipped_asks": [r for r in log if r["kind"] == "skip_ask"][-10:],
        "would_adjust": [r for r in log if r["kind"] == "would_adjust"][-5:],
        "checks": {"total": len(checks), "agree": sum(1 for r in checks if r.get("agree") is True),
                   "disagree": sum(1 for r in checks if r.get("agree") is False), "recent": checks[-10:]},
        "scores": {"word_list": "off" if lex_problem else "on", "word_list_problem": lex_problem,
                   "flash": {"on": bool(t.get("flash_on", 1)),
                             "today": {st: sum(1 for m in flash_today if m["flash_status"] == st)
                                       for st in sorted({m["flash_status"] for m in flash_today})}}},
        "calendar": None if not cal else {"last_day": cal[0], "ok": bool(cal[1]), "note": cal[2]},
        "wording_rules": WORDING_RULES,
        "left_out": ["Mood button: dropped (you, Nov 24).", "Voice scores: not built.",
                     "Mood coloring how she reads you: waits on Step 3, which is parked."],
    }


def main(argv=None):
    v = view()
    print("Step 7 (shadow): reading you")
    if v["note"]:
        print("  " + v["note"])
    for s in v["situations"]:
        print("  %-14s %3d messages%s" % (s["id"], s["messages"], "" if s["enough"] else " (not enough yet)"))
    print("  word list: %s%s" % (v["scores"]["word_list"], "" if not v["scores"]["word_list_problem"] else
                                 " (" + v["scores"]["word_list_problem"] + ")"))
    print("  Flash read: %s, today %s" % ("on" if v["scores"]["flash"]["on"] else "off", v["scores"]["flash"]["today"]))
    for p in v["patterns"]:
        print("  pattern %d [%s]: %s" % (p["id"], p["state"], p["words"]))
    for r in v["would_ask"][-3:]:
        print("  would have asked: %s" % r["question"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
