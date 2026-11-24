"""baseline.output_text(): the reply's text from Gemini's interactions replies (fix, Nov 24).
Live replies from gemini-3.8-flash put the text in res["steps"] (a "thought" step, then a "model_output" step); the old
code read only res["outputs"], so every live Flash read came back "" and Step 7 marked it "unusable".
  1. the real reply shape (tests/fixtures/flash_reply_steps_20261124.json) gives the JSON text, and it parses
  2. the older "outputs" shapes still work; a thought-only reply, or no text at all, gives ""
  3. Step 7's Flash read ends "ok" on the real shape, and Step 3 gets its event; no network call is made
  4. Step 3's rewording and the drift check get their text through the same function
Throwaway RIVIA_DIR; never touches ~/rivia; never calls Google.
    python test_output_text.py
"""
import copy, json, os, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ.update(RIVIA_DIR=os.path.join(TMP, "rivia"), HOME=TMP, GEMINI_API_KEY="fake-not-used",
                  RIVIA_NRC_LEXICON=os.path.join(TMP, "none.txt"))
os.makedirs(os.environ["RIVIA_DIR"])
sys.path.insert(0, HERE)

import baseline  # noqa: E402

COUNT = {"n": 0}


def ok(cond, label):
    assert cond, label
    COUNT["n"] += 1
    print("  ok ·", label)


def no_network(*a, **k):
    raise AssertionError("a test tried to call Google")


baseline.HTTP = no_network
REAL = json.load(open(os.path.join(HERE, "tests", "fixtures", "flash_reply_steps_20261124.json")))
ANSWER = '{"valence":0,"arousal":0,"dominance":1,"other_reading":false,"events":[{"event":"personal","strength":1}]}'

print("1. the real reply shape")
ok([s["type"] for s in REAL["steps"]] == ["thought", "model_output"] and "outputs" not in REAL,
   "the fixture is the live shape: steps = thought, model_output; no outputs")
ok(baseline.output_text(REAL) == ANSWER, "output_text() gives the model_output text")
ok(baseline.parse_rating(baseline.output_text(REAL)) == {"v": 0.0, "a": 0.0, "d": 0.3333}, "and it parses as a rating")
two = copy.deepcopy(REAL)
two["steps"][1]["content"] = [{"type": "text", "text": '{"valence":1,'}, {"type": "thought", "text": "x"},
                              {"type": "text", "text": '"arousal":0,"dominance":0}'}]
ok(baseline.output_text(two) == '{"valence":1,"arousal":0,"dominance":0}', "several text parts are joined, others skipped")
d = copy.deepcopy(REAL)
d["steps"][1]["content"] = {"type": "text", "text": "plain"}
ok(baseline.output_text(d) == "plain", "a single text part as an object works too")

print("2. the older shapes, and nothing to read")
ok(baseline.output_text({"outputs": [{"type": "text", "text": "a"}, {"type": "thought", "text": "b"}]}) == "a",
   "outputs with a text item: still read (fallback)")
ok(baseline.output_text({"outputs": [{"content": [{"type": "text", "text": "x"}, {"type": "text", "text": "y"}]}]})
   == "xy", "outputs with content lists: still read")
ok(baseline.output_text({"outputs": [{"content": {"type": "text", "text": "z"}}]}) == "z", "outputs with a content object")
thought_only = copy.deepcopy(REAL)
thought_only["steps"] = [thought_only["steps"][0]]
ok(baseline.output_text(thought_only) == "", 'a thought-only reply gives an empty string')
ok(baseline.output_text({}) == "" and baseline.output_text({"steps": None, "outputs": None}) == "",
   'no steps and no outputs gives an empty string')
ok(baseline.output_text({"steps": [{"type": "model_output", "content": [{"type": "image"}]}]}) == "",
   'a model_output with no text gives an empty string')
ok(baseline.output_text({"steps": ["junk", {"type": "model_output"}], "outputs": [{"type": "text", "text": "o"}]})
   == "", "steps with a model_output win over outputs, even when it's empty (never mixed)")

print("3. Step 7's Flash read on the real shape (no network)")
import rivia_normal as normal  # noqa: E402
import rivia_reading as rd  # noqa: E402
import rivia_reading_flash as fl  # noqa: E402
rd.CALENDAR = lambda a, b: []
seen = {"interaction": 0}


def fake_interaction(body, key):
    seen["interaction"] += 1
    assert body["store"] is False and body["model"] == fl.MODEL
    return copy.deepcopy(REAL)


baseline.interaction = fake_interaction
row = rd.take({"message_id": "m1", "text": "heading to the gym after work", "sent_at": int(time.time() * 1000)})
ok(row is not None and row["flash_status"] == "waiting", "Step 7 stored the message (numbers only), Flash waiting")
status = fl.read("m1", "heading to the gym after work")
c = normal.db_open()
try:
    m = rd.message(c, "m1")
    ok(status == "ok" and m["flash_status"] == "ok", "the Flash read ends 'ok', not 'unusable'")
    ok(m["flash_v"] == 0.0 and m["flash_a"] == 0.0 and abs(m["flash_d"] - 0.3333) < 1e-6,
       "the read's scores are stored")
    ok(m["other_reading"] is None, "7.9's second opinion is kept only for personal messages (this one isn't)")
    ok(seen["interaction"] == 1 and c.execute("select count(*) from model_calls").fetchone()[0] == 1,
       "one (faked) call, counted once in Layer 5's usage")
    try:
        ev = c.execute("select name, strength, kind from appraisal_events where message_id = 'm1'").fetchall()
    except Exception:  # noqa: BLE001
        ev = []
    ok(ev == [("personal", 1, "message")], "Step 3 logged the message event from the same answer: %s" % ev)
finally:
    c.close()

print("4. the same function for Step 3's rewording and the drift check")
import rivia_drift  # noqa: E402
import rivia_expression as ex  # noqa: E402
ok(rivia_drift.B is baseline and "B.output_text(res)" in open(os.path.join(HERE, "rivia_drift.py")).read(),
   "rivia_drift reads replies with baseline.output_text()")
src = open(os.path.join(HERE, "rivia_expression.py")).read()
ok("baseline.output_text(res)" in src and "def output_text" not in src, "rivia_expression has no reader of its own")
got = []
real_ot = baseline.output_text


def spy(res):
    t = real_ot(res)
    got.append(t)
    return t


baseline.output_text = spy
reworded = copy.deepcopy(REAL)
reworded["steps"][1]["content"] = [{"type": "text", "text": '{"text": "reworded"}'}]
baseline.interaction = lambda body, key: reworded
c = normal.db_open()
try:
    ex.prepare(c)
    long_reply = ("This is a long friendly reply about nothing in particular that rambles on for well over fifteen "
                  "words so it qualifies.")
    r = ex.take_reply({"message_id": "r1", "text": long_reply, "at": int(time.time() * 1000), "tool_only": False},
                      con=c)
    out = ex.reword("r1", con=c) if r and r["outcome"] == "waiting" else None
finally:
    c.close()
    baseline.output_text = real_ot
ok(got == ['{"text": "reworded"}'], "Step 3's rewording got the reply's text through the same function (%s)" % out)
ok(out not in (None, "failed", "unusable"), "and the rewording got past reading the reply (outcome %s)" % out)

print("\n%d checks passed" % COUNT["n"])
