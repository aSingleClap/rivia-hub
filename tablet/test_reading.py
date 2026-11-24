"""Emotion spec Step 7: reading Jordan against his own normal, in shadow. Throwaway RIVIA_DIR and body.db.
  1. message facts: numbers only, your text is never stored; clipped, corrections, what you said about how you are
  2. situation from your calendar's HMC shifts only (before, after, day off, other); calendar down = other
  3. your normal per situation: nothing until enough messages; middle value and usual swing
  4. how far off: normal, a bit off, well off, against that situation's normal
  5. pattern guesses: shown only after enough separate days; only your app button confirms or rejects
  6. ask or not: code decides; shadow never asks; daily cap; a brush-off shelves a topic; you telling her is a check
  7. never emotion.db; wording is an observation, never a feeling; no fear words; never a tool
    python test_reading.py
"""
import ast, os, sqlite3, sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ.update(HOME=TMP, RIVIA_DIR=os.path.join(TMP, "rivia"), RIVIA_NRC_LEXICON=os.path.join(TMP, "none.txt"))
sys.path.insert(0, HERE)
import baseline  # noqa: E402
import rivia_reading as rd  # noqa: E402

NY = ZoneInfo("America/New_York")
COUNT = {"n": 0}


def ok(cond, label):
    assert cond, label
    COUNT["n"] += 1
    print("  ok ·", label)


LEXF = Path(TMP) / "lex.txt"
LEXF.write_text("term\tvalence\tarousal\tdominance\n"
                "great\t0.9\t0.4\t0.5\nthanks\t0.7\t0.0\t0.2\ngood\t0.8\t0.1\t0.4\nfine\t0.3\t-0.3\t0.1\n"
                "tired\t-0.6\t-0.6\t-0.4\nwrong\t-0.7\t0.3\t0.0\nugh\t-0.7\t0.4\t-0.2\nannoying\t-0.8\t0.6\t0.0\n"
                "plan\t0.1\t0.0\t0.3\nwork\t-0.1\t0.2\t0.2\n")
LEX = baseline.Lexicon(LEXF)


def db():
    con = sqlite3.connect(os.path.join(tempfile.mkdtemp(), "body.db"), isolation_level=None)
    return rd.prepare(con)


def ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=NY).timestamp()


# Calendar: HMC shifts 2:30 to 11 PM on Mon to Fri; weekends off.
CAL_CALLS = {"n": 0, "down": False}


def fake_calendar(time_min, time_max):
    CAL_CALLS["n"] += 1
    if CAL_CALLS["down"]:
        raise RuntimeError("signed out")
    d = datetime.fromisoformat(time_min).astimezone(NY)
    out = [{"title": "Dentist", "start": d.replace(hour=9).isoformat(), "end": d.replace(hour=10).isoformat(),
            "location": "somewhere private", "all_day": False}]
    if d.weekday() < 5:
        out.append({"title": "Work Shift: HMC", "start": d.replace(hour=14, minute=30).isoformat(),
                    "end": d.replace(hour=23).isoformat(), "location": "HMC", "all_day": False})
    return out


rd.CALENDAR = fake_calendar
T = rd.code_tune()

print("1. message facts")
f = rd.facts("ok", at=1000.0, reply_at=880.0, tune=T, lex=LEX)
ok(f["words"] == 1 and f["terse"] == 1 and f["gap_min"] == 2.0, "a one-word reply 2 minutes after hers is clipped")
f = rd.facts("That's wrong, I said after work", at=1000.0, tune=T, lex=LEX)
ok(f["correction"] == 1 and f["gap_min"] is None, "a correction is heard (Step 6's word list); no reply time sent")
ok(f["lex_v"] is not None and f["lex_v"] < 0, "word-list valence reads heavier for 'wrong'")
ok(rd.said_label("Honestly I'm so tired today") == "tired", "\"I'm so tired\" is you saying how you are")
ok(rd.said_label("I'm not tired") is None, "\"I'm not tired\" isn't read as tired")
ok(rd.said_label("am I tired?") is None and rd.said_label("Am i stressed?") is None, "a question isn't a label")
ok(rd.said_label("I’m wiped") == "tired", "curly apostrophes count")
ok(rd.is_personal("my son has a game tonight") and not rd.is_personal("move the gym to 6"),
   "personal stand-in: family yes, scheduling no")
ok(rd.lexicon()[0] is None and "isn't on the tablet" in rd.lexicon()[1], "no NRC file: said plainly, no scores")

con = db()
row = rd.take({"message_id": "m1", "text": "Secret words about my week", "sent_at": ts(2026, 11, 21, 10) * 1000},
              con=con, now=ts(2026, 11, 21, 10), tune=T, lex=LEX)
dump = "\n".join(str(r) for t in ("reading_messages", "reading_log", "reading_shifts", "reading_patterns")
                 for r in con.execute("select * from %s" % t).fetchall())
ok(row and "Secret" not in dump and "week" not in dump, "your text isn't in any Step 7 table")
ok("Dentist" not in dump and "private" not in dump, "nothing but HMC shift times is kept from the calendar")
ok(rd.take({"message_id": "", "text": "x"}, con=con, tune=T) is None and rd.take("nope", con=con, tune=T) is None,
   "bad input stores nothing and doesn't raise")
row2 = rd.take({"message_id": "m1", "text": "Edited", "sent_at": ts(2026, 11, 21, 11) * 1000}, con=con,
               now=ts(2026, 11, 21, 11), tune=T, lex=LEX)
ok(row2["at"] == row["at"] and con.execute("select count(*) from reading_messages").fetchone()[0] == 1,
   "an edit replaces the message and keeps when it was first sent")
far = rd.when_sent({"sent_at": 0}, ts(2026, 11, 21, 11))
ok(far[0] == ts(2026, 11, 21, 11), "a sent time a day off the tablet's clock isn't trusted")

print("2. situation from HMC shifts")
con = db()
ok(rd.situation(con, ts(2026, 11, 23, 10), ts(2026, 11, 23, 10), T) == "before_shift", "Monday 10 AM: before shift")
ok(rd.situation(con, ts(2026, 11, 23, 16), ts(2026, 11, 23, 16), T) == "other", "during the shift: other")
ok(rd.situation(con, ts(2026, 11, 23, 23, 30), ts(2026, 11, 23, 23, 30), T) == "after_shift", "11:30 PM: after")
ok(rd.situation(con, ts(2026, 11, 24, 2), ts(2026, 11, 24, 2), T) == "after_shift",
   "2 AM after Monday's shift is still after shift")
ok(rd.situation(con, ts(2026, 11, 28, 12), ts(2026, 11, 28, 12), T) == "day_off", "Saturday: day off")
ok(rd.situation(con, ts(2026, 11, 29, 3), ts(2026, 11, 29, 3), T) == "day_off", "Sunday 3 AM after Saturday off")
n = CAL_CALLS["n"]
rd.situation(con, ts(2026, 11, 23, 10, 5), ts(2026, 11, 23, 10, 5), T)
ok(CAL_CALLS["n"] == n, "the calendar is read once per day and cached")
CAL_CALLS["down"] = True
con2 = db()
ok(rd.situation(con2, ts(2026, 11, 23, 10), ts(2026, 11, 23, 10), T) == "other", "calendar down: other")
note = con2.execute("select note from reading_shifts where ok = 0").fetchone()[0]
ok("not readable" in note, "and the reason is kept")
CAL_CALLS["down"] = False
ok(rd.situation(con2, ts(2026, 11, 23, 11), ts(2026, 11, 23, 11), T) == "before_shift",
   "after 30 minutes it tries again")

print("3. your normal per situation")
con = db()
# 30 days of messages. Days off: chatty and quick. After shifts: clipped and slow.
day0 = datetime(2026, 10, 1, tzinfo=NY)
LONG = "thanks that plan looks good I will head out after breakfast and then do the store run"
for i in range(40):
    d = day0 + timedelta(days=i)
    for k in range(3):
        if d.weekday() >= 5:
            at, text, gap = d.replace(hour=12 + k).timestamp(), LONG, 60
        else:
            at, text, gap = d.replace(hour=23, minute=15 + k * 10).timestamp(), "ok fine", 1200
            rd.take({"message_id": "b%d%d" % (i, k), "text": "sounds good for the plan today thanks a lot",
                     "sent_at": d.replace(hour=10 + k).timestamp() * 1000,
                     "reply_at": (d.replace(hour=10 + k).timestamp() - 240) * 1000},
                    con=con, now=d.replace(hour=10 + k).timestamp(), tune=T, lex=LEX)
        rd.take({"message_id": "m%d%d" % (i, k), "text": text, "sent_at": at * 1000, "reply_at": (at - gap) * 1000},
                con=con, now=at, tune=T, lex=LEX)
NOW = (day0 + timedelta(days=40)).timestamp()
norms = rd.normals(con, NOW, T)
ok(norms["after_shift"]["messages"] >= 25 and norms["after_shift"]["measures"]["words"]["enough"],
   "after shifts has enough messages for a normal")
ok(norms["after_shift"]["measures"]["words"]["middle"] == 2, "after shifts your middle length is 2 words")
ok(norms["day_off"]["measures"]["words"]["middle"] == 17, "days off it's 17 words")
ok(norms["other"]["messages"] == 0 and not norms["other"]["measures"]["words"]["enough"],
   "a situation with no messages has no normal")
T2 = dict(T, min_messages=500)
ok(not rd.normals(con, NOW, T2)["day_off"]["measures"]["words"]["enough"], "nothing counts until enough messages")

print("4. how far off")
sat_ = datetime(2026, 11, 14, 13, tzinfo=NY)   # a Saturday, after the 40 days
rn = None
for k in range(3):
    at = (sat_ + timedelta(minutes=10 * k)).timestamp()
    rd.take({"message_id": "x%d" % k, "text": "ugh", "sent_at": at * 1000, "reply_at": (at - 1500) * 1000},
            con=con, now=at, tune=T, lex=LEX)
rn = rd.read_now(con, at, T)
ok(rn["situation"] == "day_off", "Saturday messages compare to your day-off normal")
ok(rn["measures"]["words"]["zone"] == "well_off" and rn["measures"]["words"]["direction"] == "down",
   "clipped messages on a day off read well off, shorter")
ok(rn["measures"]["gap_min"]["direction"] == "up", "and slower to reply")
mon = datetime(2026, 11, 16, 23, 20, tzinfo=NY)
at = mon.timestamp()
rd.take({"message_id": "y1", "text": "ok fine", "sent_at": at * 1000, "reply_at": (at - 1200) * 1000},
        con=con, now=at, tune=T, lex=LEX)
rn = rd.read_now(con, at, T)
ok(rn["situation"] == "after_shift" and rn["measures"]["words"]["zone"] == "normal",
   "the same clipped message after a shift is your normal there")
ok(rd.zone(1.2, T) == "bit_off" and rd.zone(-2.5, T) == "well_off" and rd.zone(0.3, T) == "normal",
   "past 1 swing is a bit off, past 2 is well off")

print("5. pattern guesses")
found = rd.find_patterns(con, NOW, T)
pats = rd.patterns(con)
words_after = [p for p in pats if p["situation"] == "after_shift" and p["measure"] == "words"]
ok(words_after and words_after[0]["direction"] == "down" and words_after[0]["state"] == "proposed",
   "after shifts, shorter messages is found as a guess")
ok("After HMC shifts you're writing shorter messages" in words_after[0]["words"], "in plain words: "
   + words_after[0]["words"])
ok(words_after[0]["days_seen"] >= 10, "seen on at least 10 separate days")
ok(rd.find_patterns(con, NOW, dict(T, pattern_days=100)) == [], "a higher day count is respected")
pid = words_after[0]["id"]
try:
    rd.decide_pattern(con, pid, True, door="rivia")
    ok(False, "Rivia can't confirm")
except rd.ReadingError as e:
    ok(e.code == "not_allowed", "only your app's button confirms or rejects")
p = rd.decide_pattern(con, pid, True, door="app_button", note="yeah I'm beat after work")
ok(p["state"] == "confirmed" and p["decided_by"] == "app_button", "you confirm it")
rd.find_patterns(con, NOW + 60, T)
ok(next(x for x in rd.patterns(con) if x["id"] == pid)["state"] == "confirmed", "code never changes your decision")
try:
    rd.decide_pattern(con, 9999, False, door="app_button")
    ok(False, "unknown pattern")
except rd.ReadingError as e:
    ok(e.code == "unknown", "an unknown pattern is refused")

print("6. ask or not")
con = db()
at = ts(2026, 11, 28, 12)
r = rd.take({"message_id": "s1", "text": "I'm so tired", "sent_at": at * 1000}, con=con, now=at, tune=T, lex=LEX)
checks = rd.log_rows(con, 0, ("check",))
ok(checks and checks[-1]["said"] == "tired" and not rd.log_rows(con, 0, ("would_ask",)),
   "you said how you are: logged as a check on her read, no question")
con = db()
for k in range(4):
    at = ts(2026, 11, 28, 12, 10 * k)
    rd.take({"message_id": "u%d" % k, "text": "ugh annoying ugh", "sent_at": at * 1000}, con=con, now=at,
            tune=T, lex=LEX)
asks = rd.log_rows(con, 0, ("would_ask",))
skips = rd.log_rows(con, 0, ("skip_ask",))
ok(len(asks) == 2, "little data and heavy words: she would ask, at most 2 a day")
ok(all(a["sent"] is False for a in asks), "shadow: logged only, never sent")
ok(skips and "used up" in skips[-1]["why"], "past the cap, the skipped question is logged with why")
ok("little_data" in asks[0]["triggers"], "the reason is logged (little data)")
q = asks[0]["question"]
ok(q.endswith("Am I reading that right?") and "you seem" not in q.lower() and "feel" not in q.lower(),
   "the question says what she saw, not what you feel: " + q)
con = db()
for k in range(3):
    at = ts(2026, 11, 28, 9, 10 * k)
    rd.take({"message_id": "g%d" % k, "text": "plan work", "sent_at": at * 1000}, con=con, now=at, tune=T, lex=LEX)
ok(not rd.log_rows(con, 0, ("would_ask",)) and rd.log_rows(con, 0, ("read",))[-1]["zone"] == "weak",
   "a weak signal: nothing")
# Brush-off shelving, as it will work once asking is on (in shadow "sent" is always false, so it never fires).
con = db()
at = ts(2026, 11, 28, 12)
rd._log(con, "would_ask", at, {"question": "q", "sent": True}, "day_off", "day_off:valence:down")
ok(rd.answer_last_ask(con, "I'm fine, drop it", at + 60, at + 60) == "brushed_off", "\"drop it\" is a brush-off")
ok(rd.shelved(con, "day_off:valence:down", at + 6 * 86400, T), "the topic is left alone for 7 days")
ok(not rd.shelved(con, "day_off:valence:down", at + 8 * 86400, T), "and can come back after")
ok(rd.read_answer("yeah kinda") == "yes" and rd.read_answer("nah") == "no", "yes and no answers are read")
con = db()
at = ts(2026, 11, 28, 12)
rd._log(con, "would_ask", at, {"question": "q", "sent": False}, "day_off", "t")
ok(rd.answer_last_ask(con, "drop it", at + 60, at + 60) is None, "in shadow nothing was asked, so nothing is answered")
m = {"lex_v": 0.4, "flash_v": -0.67, "terse": 0}
ok(rd.mixed(m, T), "word list and Flash pointing opposite ways is mixed signals")
ok(not rd.mixed({"lex_v": 0.4, "flash_v": 0.67, "terse": 0}, T), "agreeing isn't mixed")

print("7. guards")
bad = sqlite3.connect(os.path.join(tempfile.mkdtemp(), "emotion.db"), isolation_level=None)
try:
    rd.prepare(bad)
    ok(False, "emotion.db refused")
except rd.ReadingError as e:
    ok(e.code == "wrong_db", "Step 7 never writes emotion.db")
src = open(os.path.join(HERE, "rivia_reading.py")).read().lower()
for w in ("scared", "afraid", "fear"):
    ok(w not in "".join(rd.WORDING_RULES).lower() and all(w not in v["up"] + v["down"] for v in rd.MEASURES.values()),
       "no '%s' in what she'd say" % w)
ok(any("I understand" in r for r in rd.WORDING_RULES), "the \"I understand\" rule is written down")
tree = ast.parse(open(os.path.join(HERE, "rivia_reading.py")).read())
mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
       {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
ok(not {m for m in mods if m.startswith(("fastmcp", "mcp", "rivia_tools"))}, "imports no tool code")
v = rd.view(con=db(), now=ts(2026, 11, 28, 12), tune=T)
ok(v["shadow"] and "none have arrived" in v["note"] and len(v["situations"]) == 4, "the card's data with nothing yet")

print("\n%d checks passed" % COUNT["n"])
