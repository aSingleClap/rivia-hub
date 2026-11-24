# rivia_expression.py: how her finished reply WOULD be reworded for her mood and style (emotion spec, Step 3.6 to 3.8).
# DRAFT, Nov 24, not installed. Shadow only: the app copies each finished typed reply of hers to the tablet AFTER you've
# seen it (POST /app/appraisal/reply). The tablet works out the reworded version, checks it, and keeps both. Your chat
# never shows the reworded one and never waits for it. When this is switched on later, it moves into the app
# (Jordan, Nov 24: "Split").
#
# Jordan's decisions (Nov 24, relayed):
#   "Keep it all": her reply text, plain and reworded, stays on the tablet with no auto-delete, for spot-checks. Your
#   own message text is still never kept (Steps 6 and 7).
#   "Leave plans alone": short replies, tool-only replies, and anything with your day plan, times, directions or step
#   lists are never reworded.
#   Starting numbers: short = under 15 words; outward tone never stronger than "moderate" for an unpleasant mood.
#
# Pieces in this file:
#   3.6 rewording    Flash (3.8, same key and Layer 5 limits as Step 7) gets her plain reply, the would-be mood words
#                    (Step 3.3, capped by 3.7) and Step 8's would-be style line, and rewrites the tone only.
#   3.7 tone limits  code checks on the reworded text: no fear words, no cursing unless Step 8's banter rule allowed
#                    it, nothing aimed at you, no new "I understand", not much longer than the plain one.
#   3.8 fact check   code compares numbers, times, dates, names and places, actions, "not"s and questions between the
#                    plain and the reworded reply. Any difference = the plain reply wins.
#
# Writes body.db only (table expression_replies, appraisal_log). Never emotion.db. Never an MCP tool. Research behind
# the fact check: Mukherjee, Ojha & Dusek 2024, "Are Large Language Models Actually Good at Text Style Transfer?"
# (arXiv 2406.05885): style rewrites by LLMs kept only part of the content (content similarity about 81% for GPT-3.5,
# 64-71% for smaller open models), so a rewrite is never trusted without a check.
import json
import re
import sys
import time
from collections import Counter

import rivia_appraisal as appraisal
import rivia_reading as reading

SHADOW = True   # True: work out, check and keep; for_reply() returns None. Going live is a later step, by hand.

# ---------- starting numbers (all adjustable in the Layer 3 card) ----------
SHORT_WORDS = 15        # 3.6 replies under this many words are never reworded
REWORD_ON = 1           # 3.6 the shadow rewording runs (1) or not (0); off saves Flash calls
LENGTH_MAX = 1.6        # 3.7 the reworded reply may be at most this many times as long (in words)
TEXT_MAX = 6000         # longest reply kept (characters); the app sends at most this

MODEL = "gemini-3.8-flash"

# 3.6 "Leave plans alone": anything that looks like his day plan, a time, directions or a list of steps.
PLAN_PATTERNS = (
    r"\b\d{1,2}:\d{2}\b", r"\b\d{1,2}\s?(?:am|pm|a\.m\.|p\.m\.)\b", r"(?m)^\s*(?:\d+[.)]|[-*•])\s+",
    r"\b(?:turn (?:left|right)|head (?:north|south|east|west)|take exit|exit \d+|merge onto|continue on)\b",
    r"\b(?:leave (?:at|by|around)|arrive (?:at|by|around)|be there by|get there by|pick(?:ed)? up at)\b",
    r"\b(?:schedule|shift|calendar|your day|today'?s plan|tomorrow'?s plan|day plan|itinerary|route|commute|"
    r"workout|pin(?:ned)?|ride (?:time|there|home))\b",
)

# 3.7 tone limits
AT_JORDAN = (r"\byou(?:'re| are)\s+(?:so\s+|such an?\s+|an?\s+)?(?:idiot|stupid|dumb|lazy|useless|ridiculous|"
             r"annoying|pathetic|clueless|hopeless|impossible)\b",
             r"\byour (?:fault|problem|own fault)\b", r"\bwhat(?:'s| is) wrong with you\b", r"\bshut up\b",
             r"\bget it together\b", r"\bgrow up\b")
UNDERSTAND = r"\bi (?:completely |totally |really )?understand\b"

# 3.8 fact check
MONTHS = ("january february march april june july august september october november december jan feb mar apr "
          "jun jul aug sep sept oct nov dec").split()
DAYS = ("monday tuesday wednesday thursday friday saturday sunday mondays tuesdays wednesdays thursdays fridays "
        "saturdays sundays today tomorrow tonight yesterday weekend weekends weekday weekdays morning afternoon "
        "evening night noon midnight").split()
# His places, in lower case (the ones Steps 5 and 6 score him on, plus everyday ones). Names in capitals are caught too.
PLACES = ("farm barn stable horses horse gym work hmc hershey home house apartment vet store office school "
          "hospital doctor dentist bank").split()
ACTIONS = ("book booked cancel cancelled canceled move moved delete deleted add added send sent schedule scheduled "
           "reschedule rescheduled remind reminded call called text texted email emailed pay paid buy bought leave "
           "left arrive arrived start started stop stopped set turn turned open opened close closed check checked "
           "update updated remove removed create created save saved share shared drive drove walk walked").split()
NEGATIONS = re.compile(r"\b(?:not|no|never|none|nothing|nobody|neither|nor|cannot|can't|don't|doesn't|didn't|won't|"
                       r"wouldn't|shouldn't|isn't|aren't|wasn't|weren't|haven't|hasn't|hadn't|couldn't)\b")
NUMBER = re.compile(r"\d+(?:[.,:/]\d+)*")
WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*")
SENTENCE_START = re.compile(r"(?:^|[.!?]\s+|\n\s*)([A-Z][A-Za-z'’-]*)")

REWRITER = (
    "You restyle one reply an AI assistant named Rivia already wrote to Jordan. Change only the tone and wording. "
    "Keep every fact, number, time, date, name, place, step and action exactly as written. Do not add or remove "
    "information, warnings, disagreements, questions or offers. Keep it about the same length. "
    "Never sound afraid. Never aim anger, sass or blame at Jordan; any edge is aimed at the problem. "
    "Never curse unless the style line says cursing is ok. Do not say \"I understand\". "
    "Answer with JSON only: {\"text\": \"the restyled reply\"}."
)
SCHEMA_OUT = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

TABLE = """
create table if not exists expression_replies(
    message_id text primary key, at real not null, received real not null, words integer not null,
    tool_only integer not null, asks integer not null, judged integer not null default 0, asked_result text,
    plain text not null, reworded text, outcome text not null, why text, changed text, ms integer,
    mood text, style text, checked_at real);
create index if not exists expression_replies_at on expression_replies(at);
"""
OUTCOMES = ("waiting", "skipped_short", "skipped_tool", "skipped_plan", "off", "no_key", "at_limit", "failed",
            "unusable", "tone_failed", "facts_failed", "passed")


def prepare(con):
    appraisal.prepare(con)
    con.executescript(TABLE)
    return con


def code_tune():
    return {"short_words": SHORT_WORDS, "reword_on": REWORD_ON, "length_max": LENGTH_MAX}


def _tune(values=None):
    if values is not None:
        return values
    try:
        import rivia_appraisal_settings as aps
        return aps.tune()
    except Exception as e:  # noqa: BLE001
        print("rivia: appraisal settings unreadable (%s); using the code's numbers." % type(e).__name__,
              file=sys.stderr)
        return {**appraisal.code_tune(), **code_tune()}


# ---------- taking her reply ----------

def asks_him(text):
    """True if one of her last two sentences is a question to him."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if p.strip()]
    return any(p.endswith("?") for p in parts[-2:])


def plan_like(text):
    t = reading._clean(text)
    return any(re.search(p, t) for p in PLAN_PATTERNS)


def skip_reason(text, tool_only, t):
    if tool_only:
        return "skipped_tool", "a tool-only reply"
    if reading.count_words(text) < int(t["short_words"]):
        return "skipped_short", "under %d words" % int(t["short_words"])
    if plan_like(text):
        return "skipped_plan", "has a time, a plan, directions or a list (left alone)"
    if not int(t["reword_on"]):
        return "off", "rewording is switched off on the Layer 3 card"
    return None, None


def take_reply(body, con=None, now=None, tune=None):
    """Her finished reply, copied by the app after you saw it. Stored as it is (kept, Jordan: "Keep it all").
    Returns the stored row (without the text) or None for a bad copy. Never raises on bad input."""
    now = time.time() if now is None else now
    if not isinstance(body, dict):
        return None
    mid, text, tool_only = body.get("message_id"), body.get("text"), body.get("tool_only", False)
    if not isinstance(mid, str) or not mid or len(mid) > 80 or not isinstance(text, str) or not text.strip() \
            or not isinstance(tool_only, bool):
        return None
    text = text[:TEXT_MAX]
    t = _tune(tune)
    con, own = reading._open(con)
    try:
        prepare(con)
        at, _ = reading.when_sent({"sent_at": body.get("at")}, now)
        old = con.execute("select at from expression_replies where message_id = ?", (mid,)).fetchone()
        if old:
            at = old[0]
        outcome, why = skip_reason(text, tool_only, t)
        con.execute("insert or replace into expression_replies(message_id, at, received, words, tool_only, asks, "
                    "plain, outcome, why) values(?,?,?,?,?,?,?,?,?)",
                    (mid, at, now, reading.count_words(text), int(tool_only), int(asks_him(text)), text,
                     outcome or "waiting", why))
        return row(con, mid)
    finally:
        if own:
            con.close()


COLS = ("message_id", "at", "words", "tool_only", "asks", "asked_result", "outcome", "why", "changed", "ms", "mood",
        "style")


def row(con, mid):
    r = con.execute("select %s from expression_replies where message_id = ?" % ", ".join(COLS), (mid,)).fetchone()
    if not r:
        return None
    out = dict(zip(COLS, r))
    out["changed"] = json.loads(out["changed"]) if out["changed"] else []
    return out


# ---------- 3.6 rewording ----------

def style_line(con):
    """Step 8's latest would-be style line, read straight from its log (read-only, shadow use only). When rewording
    goes live it must use rivia_style.for_expression() instead, which is the one live door."""
    try:
        r = con.execute("select row from style_log where kind = 'line' order by id desc limit 1").fetchone()
        return json.loads(r[0]) if r else None
    except Exception:  # noqa: BLE001  (Step 8 not there yet: plain style)
        return None


def mood_words(con, now, t, live=None):
    try:
        if con.execute("select count(*) from appraisal_state").fetchone()[0] != 2:
            return "neutral"
        state, _ = appraisal.would_be(con, now, live)
        return appraisal.outward(state, t)["words"]
    except Exception:  # noqa: BLE001
        return "neutral"


def fixed_rules():
    try:
        import rivia_style as style
        return list(style.FIXED_RULES)
    except Exception:  # noqa: BLE001
        return []


def body_for(plain, mood, style, model=MODEL):
    sline = (style or {}).get("line") or "plain style; no cursing"
    rules = "\n".join("- " + r for r in fixed_rules())
    instr = REWRITER + "\nTone: %s (show it lightly; never stronger than that).\nStyle: %s.\n%s" % (mood, sline, rules)
    return {"model": model, "system_instruction": instr,
            "input": [{"type": "user_input", "content": "Reply to restyle:\n<<<\n" + plain + "\n>>>"}],
            "store": False,
            "response_format": {"type": "text", "mime_type": "application/json", "schema": SCHEMA_OUT},
            "generation_config": {}}


def parse(text):
    try:
        m = re.search(r"\{.*\}", text or "", re.S)
        out = json.loads(m.group(0) if m else text).get("text")
    except Exception:  # noqa: BLE001
        return None
    return out.strip() if isinstance(out, str) and out.strip() else None


# ---------- 3.7 tone limits ----------

def tone_check(plain, reworded, style, t):
    """Problems with the reworded reply's tone, as plain words. [] means it passes."""
    import rivia_emotion as emo
    p, r = reading._clean(plain), reading._clean(reworded)
    out = []
    for w in emo.NEVER_LABELS:
        if re.search(r"\b%s\b" % w, r) and not re.search(r"\b%s\b" % w, p):
            out.append("a fear word (%s)" % w)
    try:
        import rivia_style as st
        if st.swear_count(reworded) > st.swear_count(plain) and not (style or {}).get("curse_ok"):
            out.append("cursing when it wasn't allowed")
    except Exception:  # noqa: BLE001
        pass
    for pat in AT_JORDAN:
        if re.search(pat, r) and not re.search(pat, p):
            out.append("something aimed at you")
            break
    if re.search(UNDERSTAND, r) and not re.search(UNDERSTAND, p):
        out.append("a new \"I understand\"")
    pw, rw = max(1, reading.count_words(plain)), reading.count_words(reworded)
    if rw > pw * float(t["length_max"]):
        out.append("too long (%d words for %d)" % (rw, pw))
    return out


# ---------- 3.8 fact check ----------

def name_words(text):
    """Capitalized words that aren't just starting a sentence: names and places."""
    starts = {m.group(1) for m in SENTENCE_START.finditer(text or "")}
    return {w.lower().strip("'\u2019") for w in WORD.findall(text or "")
            if w[0].isupper() and w not in starts and w not in ("I", "I'm", "I'll", "I've", "I'd")}


def _names(text, vocab):
    low = Counter(w.strip("'\u2019") for w in WORD.findall(reading._clean(text)))
    return Counter({v: low[v] for v in vocab if low[v]})


def facts(text, vocab=None):
    """What the fact check compares. Counters, so a dropped or added copy of the same thing shows too."""
    low = reading._clean(text)
    words = WORD.findall(low)
    return {
        "numbers": Counter(NUMBER.findall(low)),
        "dates": Counter(w for w in words if w in MONTHS or w in DAYS),
        "names": _names(text, name_words(text) if vocab is None else vocab),
        "places": Counter(w for w in words if w in PLACES),
        "actions": Counter(w for w in words if w in ACTIONS),
        "nots": Counter({"not": len(NEGATIONS.findall(low))}),
        "questions": Counter({"?": (text or "").count("?")}),
    }


def fact_check(plain, reworded):
    """3.8: every difference between the two, as [{"what", "plain", "reworded"}]. [] means the facts match."""
    vocab = name_words(plain) | name_words(reworded)   # a name moved to the start of a sentence still counts
    a, b = facts(plain, vocab), facts(reworded, vocab)
    out = []
    for kind in a:
        if +a[kind] == +b[kind]:
            continue
        gone = sorted((a[kind] - b[kind]).elements())
        new = sorted((b[kind] - a[kind]).elements())
        out.append({"what": kind, "missing": gone[:10], "added": new[:10]})
    return out


# ---------- the shadow run ----------

def reword(message_id, con=None, now=None, tune=None, live=None):
    """Works out, checks and keeps the reworded reply. Returns the outcome. Never raises."""
    now = time.time() if now is None else now
    con, own = reading._open(con)
    try:
        t = _tune(tune)
        prepare(con)
        r = con.execute("select plain, outcome from expression_replies where message_id = ?", (message_id,)).fetchone()
        if not r:
            return "gone"
        plain, outcome = r
        if outcome != "waiting":
            return outcome
        import rivia_reading_flash as fl
        mood = mood_words(con, now, t, live)
        style = style_line(con)
        sname = (style or {}).get("style")
        reworded, changed, why, ms = None, [], None, None
        import rivia_toggles as toggles
        if not toggles.runs("l3.6"):   # 3.6 switched off (or Layer 3, or the Flash read): no rewording call
            outcome, why = "off", "3.6 rewording is switched off"
        elif fl.key() is None:
            outcome, why = "no_key", "no Gemini key on the tablet"
        elif fl.at_limit(con, now):
            outcome, why = "at_limit", "3.8 Flash was at your Layer 5 limit"
        else:
            t0 = time.perf_counter()
            try:
                import baseline
                import rivia_usage as usage
                res = baseline.interaction(body_for(plain, mood, style), fl.key())
                tin, tout = fl._tokens(res)
                usage.record_call(con, MODEL, time.time(), input_tokens=tin, output_tokens=tout)
                reworded = parse(baseline.output_text(res))
                ms = int((time.perf_counter() - t0) * 1000)
            except Exception as e:  # noqa: BLE001
                print("rivia: shadow rewording failed (%s)" % type(e).__name__, file=sys.stderr)
                outcome, why = "failed", "the Flash call failed"
            else:
                if reworded is None:
                    outcome, why = "unusable", "Flash's answer couldn't be used"
                else:
                    tone = tone_check(plain, reworded, style, t)
                    changed = fact_check(plain, reworded)
                    if tone:
                        outcome, why = "tone_failed", "; ".join(tone)
                    elif changed:
                        outcome, why = "facts_failed", "facts changed: " + ", ".join(c["what"] for c in changed)
                    else:
                        outcome, why = "passed", None
        con.execute("update expression_replies set reworded = ?, outcome = ?, why = ?, changed = ?, ms = ?, mood = ?, "
                    "style = ?, checked_at = ? where message_id = ?",
                    (reworded, outcome, why, json.dumps(changed) if changed else None, ms, mood, sname, now,
                     message_id))
        return outcome
    except Exception as e:  # noqa: BLE001
        print("rivia: shadow rewording skipped a reply (%s)" % type(e).__name__, file=sys.stderr)
        return "failed"
    finally:
        if own:
            con.close()


def for_reply(plain, con=None):
    """The one door a live rewording would use. None in shadow (her plain reply stands) and on any error."""
    if SHADOW:
        return None
    return None   # live use moves into the app (Jordan: "Split"); built when it's switched on


# ---------- what the Layer 3 card shows ----------

def view(con=None, now=None, tune=None):
    """3.6 to 3.9 for the Layer 3 card: outcomes, the added wait it would cost, and the last few for spot-checks."""
    now = time.time() if now is None else now
    con, own = reading._open(con)
    try:
        prepare(con)
        counts = dict(con.execute("select outcome, count(*) from expression_replies where at >= ? group by outcome",
                                  (now - 14 * 86400,)).fetchall())
        ms = sorted(m for (m,) in con.execute("select ms from expression_replies where at >= ? and ms is not null",
                                              (now - 14 * 86400,)).fetchall())
        recent = con.execute("select message_id, at, plain, reworded, outcome, why, mood, style from "
                             "expression_replies where outcome not in ('waiting','skipped_short','skipped_tool') "
                             "order by at desc limit 10").fetchall()
        asked = dict(con.execute("select asked_result, count(*) from expression_replies where asks = 1 and at >= ? "
                                 "and judged = 1 group by asked_result", (now - 14 * 86400,)).fetchall())
        total = con.execute("select count(*) from expression_replies").fetchone()[0]
    finally:
        if own:
            con.close()
    tried = sum(counts.get(k, 0) for k in ("passed", "tone_failed", "facts_failed"))
    return {
        "shadow": SHADOW,
        "outcomes_14d": counts,
        "pass_rate": round(counts.get("passed", 0) / tried, 3) if tried else None,
        "added_wait_ms": {"median": ms[len(ms) // 2] if ms else None, "slowest": ms[-1] if ms else None},
        "spot_check": [{"message_id": m, "at": a, "plain": p, "reworded": r, "outcome": o, "why": w, "mood": md,
                        "style": s} for m, a, p, r, o, w, md, s in recent],
        "questions_14d": asked,
        "kept_replies": total,
        "note": None if total else "no replies yet: the app copies each finished typed reply of hers here after you "
                                   "see it (needs the app change in the Step 3 passes)",
    }
