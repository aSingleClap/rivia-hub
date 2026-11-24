"""Emotion spec Step 3.6 to 3.8: the shadow rewording of her finished replies, its tone limits and fact check.
Fake Gemini, throwaway folders; no network, no key kept.
  1. her reply copy is kept (plain); short, tool-only and plan replies are left alone; switched off: no call
  2. the fact check: changed numbers, times, dates, names, actions, "not"s or questions fail; moved words don't
  3. tone limits: fear words, cursing without banter, anything aimed at you, a new "I understand", too long
  4. the shadow run: Flash gets the mood words and Step 8's style line; both versions kept; every outcome said
  5. never live: for_reply() gives nothing in shadow; the card's numbers
    python test_expression.py
"""
import json, os, sqlite3, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ.update(HOME=TMP, RIVIA_DIR=os.path.join(TMP, "rivia"), RIVIA_NRC_LEXICON=os.path.join(TMP, "none.txt"))
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, HERE)
import baseline  # noqa: E402
import rivia_appraisal as ap  # noqa: E402
import rivia_expression as ex  # noqa: E402
import rivia_reading as rd  # noqa: E402
import rivia_style as st  # noqa: E402

COUNT = {"n": 0}


def ok(cond, label):
    assert cond, label
    COUNT["n"] += 1
    print("  ok ·", label)


T = {**ap.code_tune(), **ex.code_tune()}
NOW = 1_800_000_000.0
LIVE = ({k: {"v": 0.0, "a": 0.0, "d": 0.0, "updated": NOW} for k in ("emotion", "mood", "baseline")}, None)
LONG = ("I checked the gym list again and the closer one is open later on weekends, so that should be easier "
        "for you after the barn.")


def db():
    c = sqlite3.connect(os.path.join(tempfile.mkdtemp(), "body.db"), isolation_level=None)
    rd.prepare(c)
    return ex.prepare(c)


def reply(c, mid, text, tool_only=False, tune=T):
    return ex.take_reply({"message_id": mid, "text": text, "at": NOW * 1000, "tool_only": tool_only}, con=c, now=NOW,
                         tune=tune)


print("1. her reply copy")
c = db()
r = reply(c, "a", LONG)
ok(r["outcome"] == "waiting" and r["words"] > 15, "a long chatty reply waits for rewording")
ok(c.execute("select plain from expression_replies where message_id = 'a'").fetchone()[0] == LONG,
   "kept as she wrote it (Jordan: \"Keep it all\")")
ok(reply(c, "b", "Done, it's on your calendar.")["outcome"] == "skipped_short", "under 15 words: left alone")
ok(reply(c, "c", LONG, tool_only=True)["outcome"] == "skipped_tool", "a tool-only reply: left alone")
plans = ["You're at the barn until 4:30 and the gym after, " + LONG,
         LONG + " Leave by 6 pm to make it.",
         LONG + "\n1. Feed the horses\n2. Gym\n3. Home",
         LONG + " Turn left onto Chocolate Ave after the light.",
         LONG + " I moved the workout on your schedule too."]
ok([reply(c, "p%d" % i, p)["outcome"] for i, p in enumerate(plans)] == ["skipped_plan"] * 5,
   "times, a plan, a list, directions or your schedule: left alone (Jordan: \"Leave plans alone\")")
ok(reply(c, "d", LONG, tune=dict(T, reword_on=0))["outcome"] == "off", "switched off on the card: left alone")
ok(reply(c, "e", "") is None and reply(c, "", LONG) is None
   and ex.take_reply({"message_id": "f", "text": LONG, "tool_only": "yes"}, con=c) is None, "a bad copy is refused")
ok(ex.asks_him("Here's the list. Want me to send it?") and not ex.asks_him("Done? Yes. It's on the calendar now."),
   "she asked you something only if one of her last two sentences is a question")

print("2. the fact check")
p = "Your shift at Hershey starts at 7:30 tomorrow, so I moved the gym to Thursday. Want me to book a ride?"
ok(ex.fact_check(p, "Heads up: Hershey has you at 7:30 tomorrow, so the gym got moved to Thursday. Want me to book "
                    "a ride?") == [], "the same facts in other words: passes")
ok(ex.fact_check(p, p.replace("7:30", "7:00"))[0]["what"] == "numbers", "a changed time fails")
ok(ex.fact_check(p, p.replace("Thursday", "Friday"))[0]["what"] == "dates", "a changed day fails")
ok(ex.fact_check(p, p.replace("at Hershey ", ""))[0]["what"] == "names", "a dropped place fails")
ok(any(x["what"] == "actions" for x in ex.fact_check(p, p.replace("moved", "kept"))), "a changed action fails")
ok(any(x["what"] == "nots" for x in ex.fact_check("I could not reach the vet.", "I reached the vet.")),
   "a lost \"not\" fails")
ok(any(x["what"] == "questions" for x in ex.fact_check(p, p.replace("Want me to book a ride?", "I'll book a ride."))),
   "a dropped question fails")
ok(ex.fact_check("Lancaster had nothing close. Try another name?",
                 "Nothing close showed up in Lancaster. Try another name?") == [],
   "a name moved to the start of a sentence still counts as the same name")

print("3. tone limits")
plain = "That place closed early today, so the trip there won't work this time around for you, sadly."
ok(ex.tone_check(plain, plain, None, T) == [], "the plain reply passes")
ok(ex.tone_check(plain, plain + " I'm scared it'll happen again.", None, T)[0].startswith("a fear word"),
   "a fear word fails")
ok("cursing when it wasn't allowed" in ex.tone_check(plain, "Damn, that place closed early, so no trip today.",
                                                      {"curse_ok": False}, T), "cursing without banter fails")
ok(ex.tone_check(plain, "Damn, that place closed early, so no trip today.", {"curse_ok": True}, T) == [],
   "with banter on (Step 8), a curse at the problem passes")
ok("something aimed at you" in ex.tone_check(plain, "Your fault for waiting. That place closed early.", None, T),
   "anything aimed at you fails")
ok(any("understand" in x for x in ex.tone_check(plain, "I understand. That place closed early.", None, T)),
   "a new \"I understand\" fails")
ok(any(x.startswith("too long") for x in ex.tone_check(plain, plain + " " + plain, None, T)), "twice as long fails")

print("4. the shadow run")
SENT = []
ANSWER = {"text": None, "fail": False}


def fake_http(method, url, body, api_key, timeout=120):
    if ANSWER["fail"]:
        raise OSError("down")
    SENT.append(body)
    return {"status": "completed", "outputs": [{"type": "text", "text": ANSWER["text"]}],
            "usage": {"total_input_tokens": 80, "total_output_tokens": 30}}


baseline.HTTP = fake_http
baseline.PACER = baseline.Pacer(0)
import time as _t  # noqa: E402
_t.sleep = lambda s: None
c = db()
reply(c, "k", LONG)
ok(ex.reword("k", con=c, now=NOW, tune=T, live=LIVE) == "no_key" and not SENT, "no key: no call, says so")
os.environ["GEMINI_API_KEY"] = "test-key-not-real"
st.prepare(c)
st._log(c, "line", NOW, {"line": "dry style, strength 2; light sass at the problem only; no cursing", "style": "dry",
                         "curse_ok": False})
ap.apply(c, [{"name": "tool_failed", "strength": 3}], NOW, T, live=LIVE)
good = ("Checked the gym list again: the closer one is open later on weekends, so that should be easier for you after "
        "the barn.")
ANSWER["text"] = json.dumps({"text": good})
reply(c, "g", LONG)
ok(ex.reword("g", con=c, now=NOW, tune=T, live=LIVE) == "passed" and len(SENT) == 1, "a clean rewrite: passed")
instr = SENT[0]["system_instruction"]
ok("dry style, strength 2" in instr and "annoyance" in instr, "Flash got Step 8's style line and her would-be mood")
ok(SENT[0]["store"] is False and SENT[0]["model"] == "gemini-3.8-flash", "3.8 Flash, store=false")
ok(any(rule in instr for rule in st.FIXED_RULES), "and Step 8's fixed rules (pushback never softened)")
row = c.execute("select plain, reworded, mood, style from expression_replies where message_id = 'g'").fetchone()
ok(row[0] == LONG and row[1] == good and row[3] == "dry", "both versions kept, with the mood and style used")
ok(c.execute("select count(*) from model_calls").fetchone()[0] == 1, "counted on Layer 5")
ANSWER["text"] = json.dumps({"text": LONG.replace("weekends", "weekdays").replace("barn", "farm")})
reply(c, "h", LONG)
ok(ex.reword("h", con=c, now=NOW, tune=T, live=LIVE) == "facts_failed", "a changed fact: the plain reply wins")
ok("facts changed" in ex.row(c, "h")["why"], "and says which kind")
ANSWER["text"] = json.dumps({"text": "Ugh, " + good + " Not that you'd ever plan ahead, you're hopeless."})
reply(c, "i", LONG)
ok(ex.reword("i", con=c, now=NOW, tune=T, live=LIVE) == "tone_failed", "aimed at you: fails the tone limits")
ANSWER["text"] = "no json here"
reply(c, "j", LONG)
ok(ex.reword("j", con=c, now=NOW, tune=T, live=LIVE) == "unusable", "an unusable answer: said, never guessed")
ANSWER["fail"] = True
reply(c, "l", LONG)
ok(ex.reword("l", con=c, now=NOW, tune=T, live=LIVE) == "failed", "Flash down: failed, never raises")
ANSWER["fail"] = False
import rivia_model_limits as ml  # noqa: E402
real = ml.current_table
ml.current_table = lambda: ({"gemini-3.8-flash": {"rpm": 1, "tpm": None, "rpd": None}}, [])
reply(c, "m", LONG)
n = len(SENT)
ok(ex.reword("m", con=c, now=_t.time(), tune=T, live=LIVE) == "at_limit" and len(SENT) == n,
   "3.8 Flash at your Layer 5 limit: no call")
ml.current_table = real
ok(ex.reword("b", con=c, now=NOW, tune=T, live=LIVE) == "gone" and ex.reword("g", con=c, now=NOW, tune=T) == "passed",
   "an unknown reply: nothing sent; one already done isn't redone")

print("5. never live")
ok(ex.for_reply(LONG) is None, "for_reply() gives nothing in shadow: her plain reply stands")
v = ex.view(con=c, now=NOW, tune=T)
ok(v["outcomes_14d"]["passed"] == 1 and v["pass_rate"] == round(1 / 3, 3), "the card: outcomes and pass rate")
ok(v["added_wait_ms"]["median"] is not None and v["spot_check"][0]["plain"], "the wait it would add, and spot-checks")
ok(v["shadow"] and v["kept_replies"] >= 7, "kept, shadow")

print("\n%d checks passed" % COUNT["n"])
