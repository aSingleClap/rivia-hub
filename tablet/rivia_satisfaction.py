# rivia_satisfaction.py: your corrections and your satisfaction (emotion spec, Step 6.3). DRAFT.
#
# Spec, Step 6: "First signal to implement: user corrections ("no, that's wrong")". Trust "is computed by code from
# Jordan's confirmations and corrections; she never writes it."
#
# Jordan (Nov 23): "I will decide what she unlocks and unlock it by hand. If scores on accuracy and my satisfaction
# support earned trust then she can bring it up". Then how satisfaction is measured:
# "I will verbally confirm with her that a plan is good, i will let her know if she did something wrong, we can add
# a thumbs up and thumbs down to messages too. if I follow the schedule and my times match up with hers its safe to
# assume her plan was good so if I dont say anything then it can count as a good day too. Edits dont always
# necessarily mean she is wrong". And for a quiet day: "Counts half".
#
# So there are three kinds of signal, each kept as one row in body.db's trust_feedback table:
#   thumb   a thumbs up (+1) or down (-1) on one of her messages, from the app's button (door app_button)
#   words   you telling her "good plan" (+1) or "you did something wrong" (-1). Your chats go phone -> Google and
#           never pass the tablet, so the app sends a copy of each message you type in a Rivia chat to
#           /app/trust/words (door app_words). read_words() below, plain code with a fixed word list, decides
#           praise, correction, or neither. Rivia never decides it: she'd be grading her own work. Your message's
#           text is not kept; only what was heard ("that's wrong") goes in the note.
#   quiet   a day you said nothing, followed her plan, and your times matched hers: +0.5 (your "Counts half").
#           If a thumb or words for that day come in later, the quiet row is taken off: your words count instead.
#   edits   logged by the scheduler as always; never counted here (your "Edits dont always ... mean she is wrong")
#
# A down signal (thumbs down, or "that's wrong") is also a correction: 6.4 lowers her confidence in that area.
# Which area: for a thumbs down, the app sends her message's text and the code picks the area from the words in it
# (farm, gym, work, home, ride, rule...). No clear area means "chat" (her answers in chat).
#
# Satisfaction number: (good + 1) / (good + bad + 2), with older signals fading (half after 30 days). It starts at
# 0.5, "don't know yet", and needs real signals to move. "Evidence" is how much signal is behind it.
#
# Who can write: only the app's doors (thumbs, and your words as read by read_words) and the code's own day judge (quiet days, door "judge").
# Never Gemini, never a tool, never Rivia. She can't give herself a thumbs up.
#
# Honest limit: a quiet good day is judged from the same matched times as her accuracy (6.6). Your "Counts half"
# keeps that overlap small, but on quiet weeks satisfaction mostly repeats accuracy. Your thumbs are what make it a
# second, separate opinion.
import json
import re

SOURCES = {"thumb": ("app_button",), "words": ("app_words",), "quiet": ("judge",)}
VALUES = {"thumb": (1.0, -1.0), "words": (1.0, -1.0), "quiet": (0.5,)}
QUIET_WEIGHT = 0.5            # Jordan, Nov 23: "Counts half". Adjustable in the app (6.9).
WORDS_WEIGHT = 1.0            # your words count as much as a thumb. 0 turns words off. Adjustable in the app.
WORDS_MAX = 4000              # longest message read for praise or correction
HALF_LIFE_DAYS = 30.0         # an old signal counts half after this long
KEEP_DAYS = 120
TEXT_MAX = 500
NOTE_MAX = 200

SCHEMA = """
create table if not exists trust_feedback(
    id integer primary key autoincrement, at integer not null, day text not null, source text not null,
    value real not null, area text not null, door text not null, message_id text, text text, note text);
create unique index if not exists trust_feedback_quiet on trust_feedback(day, source) where source = 'quiet';
"""

# Words in her message -> area (6.4). First match wins, checked in this order. Whole words, lower case.
AREA_WORDS = (
    ("rules", (r"rules?", r"too short", r"overlaps?", r"arrive early", r"workout floor")),
    ("pin:farm", (r"pin at the farm", r"farm pin")),
    ("pin:home", (r"home pin", r"pin at home")),
    ("arrive:gym", (r"gym", r"workouts?", r"planet fitness", r"lifting")),
    ("arrive:hmc", (r"work", r"shift", r"hmc", r"hershey")),
    ("arrive:farm", (r"farm", r"horses?", r"barn")),
    ("arrive:home", (r"home",)),
)
PLACE_PATTERNS = (("farm", (r"farm",)), ("home", (r"home",)), ("gym", (r"gym", r"planet fitness")),
                  ("hmc", (r"work", r"hmc", r"hershey")))


def _has(t, pats):
    return any(re.search(r"\b%s\b" % p, t) for p in pats)


class FeedbackError(ValueError):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def area_from_text(text):
    """Her message's words -> an area. 'chat' when nothing clear. A ride mention makes it a ride area when two
    places are named ('ride from the farm to home' -> ride:farm|home)."""
    t = (text or "").lower()
    places = [p for p, pats in PLACE_PATTERNS if _has(t, pats)]
    if re.search(r"\bride\b|\bbike\b|\bcommute\b", t) and len(places) >= 2:
        return "ride:%s" % "|".join(sorted(places[:2]))
    for area, pats in AREA_WORDS:
        if _has(t, pats):
            return area
    return "chat"


# Your words (6.3). Plain phrases, whole words, lower case, curly quotes made straight. A message that has both a
# praise and a correction, or asks a question about them ("is that right?"), counts as neither: when unsure, nothing.
# Corrections are checked first, so "not a good plan" is a correction, not praise.
WRONG_PHRASES = (
    r"that'?s (?:just )?wrong", r"that is wrong", r"you'?re wrong", r"you are wrong", r"wrong (?:time|place|day|ride|"
    r"pin|route|order|shift|plan)", r"you (?:did|got|have) (?:something|it|that|this|that one) wrong",
    r"you did something wrong", r"(?:that'?s|that is|it'?s|this is) not right", r"(?:that|this|it) isn'?t right",
    r"that'?s incorrect", r"you messed (?:it |that |this )?up", r"you forgot", r"(?:that|this|it) doesn'?t work",
    r"not (?:a )?good plan", r"bad plan", r"(?:that|this) is off", r"that'?s off", r"you'?re off",
    r"(?:that|this|it) (?:won'?t|will not) work",
)
GOOD_PHRASES = (
    r"(?:good|great|perfect|nice|solid) plan", r"plan looks good", r"(?:that|this) works", r"(?:that|this|it) "
    r"looks good", r"looks good", r"(?:that'?s|that is|it'?s) perfect", r"perfect", r"good job", r"nice job",
    r"great job", r"well done", r"nailed it", r"spot on", r"(?:that'?s|that is) right", r"you'?re right",
    r"you are right", r"exactly right",
)


def read_words(text):
    """Your message -> (+1.0 praise, -1.0 correction, or None) and what was heard, in plain words.
    This is code, not Rivia: she never decides whether you praised her."""
    if not isinstance(text, str) or not text.strip():
        return None, "empty"
    t = re.sub(r"[‘’ʼ]", "'", text[:WORDS_MAX].lower())
    bad = next((m.group(0) for m in (re.search(r"\b%s\b" % p, t) for p in WRONG_PHRASES) if m), None)
    t_good = t
    if bad:
        t_good = t.replace(bad, " ")
    good = next((m.group(0) for m in (re.search(r"\b%s\b" % p, t_good) for p in GOOD_PHRASES) if m), None)
    if bad and good:
        return None, "heard both \"%s\" and \"%s\", so neither counts" % (good, bad)
    hit = bad or good
    if not hit:
        return None, "no praise or correction heard"
    if good and re.search(r"\b(?:not|isn'?t|wasn'?t|never|hardly)\s+(?:\w+\s+)?$", t_good[:t_good.index(good)]):
        return None, "\"not %s\" is too vague to count either way" % good
    # A question about it isn't feedback: "is that right?", "does this work?", "looks good?"
    i = t.index(hit)
    start = max(t.rfind(c, 0, i) for c in ".!?\n") + 1
    rest = re.split(r"(?<=[.!?\n])", t[i:], maxsplit=1)[0]
    asks = re.match(r"\s*(?:so\s+|and\s+|but\s+)?(?:is|isn'?t|does|doesn'?t|do|did|would|will|can|could|was|are)\b",
                    t[start:i])
    if rest.rstrip().endswith("?") or asks:
        return None, "\"%s\" was in a question, so it doesn't count" % hit
    return (-1.0 if bad else 1.0), hit


def _guard(con):
    for _, name, path in con.execute("pragma database_list").fetchall():
        if name == "main" and path and path.replace("\\", "/").rsplit("/", 1)[-1] == "emotion.db":
            raise ValueError("Step 6 rows never go in emotion.db")


def add(con, source, value, *, door, day, now, area=None, message_id=None, text=None, note=None):
    """One signal. Checked: the source's door, its allowed values, sizes. Returns the stored row as a dict."""
    _guard(con)
    con.executescript(SCHEMA)
    if source not in SOURCES:
        raise FeedbackError("bad_source", "source must be one of %s" % ", ".join(SOURCES))
    if door not in SOURCES[source]:
        raise FeedbackError("not_allowed", "%s signals only come from %s" % (source, " or ".join(SOURCES[source])))
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) not in VALUES[source]:
        raise FeedbackError("bad_value", "%s takes %s" % (source, " or ".join("%+g" % v for v in VALUES[source])))
    for name, v, cap in (("message_id", message_id, 80), ("text", text, TEXT_MAX), ("note", note, NOTE_MAX)):
        if v is not None and (not isinstance(v, str) or len(v) > cap):
            raise FeedbackError("bad_value", "%s must be text up to %d characters" % (name, cap))
    if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise FeedbackError("bad_value", "day must be YYYY-MM-DD")
    if area is None:
        area = area_from_text(text) if float(value) < 0 else "chat"
    if not isinstance(area, str) or not re.fullmatch(r"[a-z]+(:[a-z|]+)?", area) or len(area) > 40:
        raise FeedbackError("bad_value", "area must look like arrive:farm, ride:farm|home or chat")
    try:
        cur = con.execute("insert into trust_feedback(at, day, source, value, area, door, message_id, text, note) "
                          "values(?,?,?,?,?,?,?,?,?)", (int(now), day, source, float(value), area, door, message_id,
                                                         text, note))
    except Exception as e:  # noqa: BLE001  (the unique index: one quiet row a day)
        if "unique" in str(e).lower():
            raise FeedbackError("already", "that day already has its quiet-day signal")
        raise
    if source in ("thumb", "words"):
        # Your words count instead of a quiet day (you weren't quiet after all). Counted once.
        con.execute("delete from trust_feedback where day = ? and source = 'quiet'", (day,))
    con.execute("delete from trust_feedback where at < ?", (int(now) - KEEP_DAYS * 86400,))
    return {"id": cur.lastrowid, "at": int(now), "day": day, "source": source, "value": float(value), "area": area}


def rows(con, since=None):
    con.executescript(SCHEMA)
    q = "select id, at, day, source, value, area, door, message_id, note from trust_feedback"
    args = ()
    if since is not None:
        q += " where at >= ?"
        args = (int(since),)
    keys = ("id", "at", "day", "source", "value", "area", "door", "message_id", "note")
    return [dict(zip(keys, r)) for r in con.execute(q + " order by at, id", args)]


def said_anything(con, day_s):
    """True if Jordan sent any thumb or words signal for that day."""
    con.executescript(SCHEMA)
    return con.execute("select 1 from trust_feedback where day = ? and source in ('thumb','words') limit 1",
                       (day_s,)).fetchone() is not None


def is_quiet_good(verdicts, plan_held, spoke):
    """Jordan's quiet good day: he said nothing (spoke False), followed her plan (plan_held == 1.0, so nothing was
    rearranged), and his times matched hers (at least one arrival judged, every judged arrival a hit, no rule
    broken). Returns (bool, plain reason)."""
    if spoke:
        return False, "you said something that day, so your words count instead"
    if plan_held is None:
        return False, "no plan to follow that day"
    if plan_held < 1.0:
        return False, "you rearranged the plan, so it isn't counted either way"
    arrivals = [v for v in verdicts if v["kind"] == "arrive" and v["result"] in ("hit", "miss")]
    if not arrivals:
        return False, "no arrival could be checked"
    if any(v["result"] == "miss" for v in arrivals):
        return False, "an arrival was off"
    if any(v["kind"] == "rule" and v["result"] == "miss" for v in verdicts):
        return False, "a rule was broken"
    return True, "you followed the plan, said nothing, and your %d arrival%s matched" % (
        len(arrivals), "" if len(arrivals) == 1 else "s")


def _fade(age_s, half_life_days):
    return 0.5 ** (max(0.0, age_s) / (half_life_days * 86400.0))


def satisfaction(signals, now, *, quiet_weight=QUIET_WEIGHT, words_weight=WORDS_WEIGHT,
                 half_life_days=HALF_LIFE_DAYS, since=None):
    """{"value": 0..1, "good", "bad", "evidence", "counts"} from signal rows. Starts at 0.5 with no evidence.
    quiet rows are scaled by quiet_weight / 0.5, so the app's quiet weight applies to rows already stored.
    since: ignore signals before this time (after the kill switch, 6.8)."""
    good = bad = 0.0
    counts = {"thumb_up": 0, "thumb_down": 0, "words_good": 0, "words_wrong": 0, "quiet": 0}
    for s in signals:
        if since is not None and s["at"] < since:
            continue
        f = _fade(now - s["at"], half_life_days)
        v = s["value"]
        if s["source"] == "quiet":
            v = quiet_weight
            counts["quiet"] += 1
        elif s["source"] == "thumb":
            counts["thumb_up" if v > 0 else "thumb_down"] += 1
        else:
            counts["words_good" if v > 0 else "words_wrong"] += 1
            v = v * words_weight
        if v > 0:
            good += v * f
        else:
            bad += -v * f
    value = (good + 1.0) / (good + bad + 2.0)
    return {"value": round(value, 3), "good": round(good, 3), "bad": round(bad, 3),
            "evidence": round(good + bad, 3), "counts": counts}


def corrections(signals, since=None):
    """The down signals (thumbs down, "that's wrong"): 6.4 lowers her confidence in their area."""
    return [s for s in signals if s["value"] < 0 and s["source"] in ("thumb", "words")
            and (since is None or s["at"] >= since)]


if __name__ == "__main__":
    print(json.dumps({"example_area": area_from_text("The ride from the farm to home took longer")}, indent=1))
