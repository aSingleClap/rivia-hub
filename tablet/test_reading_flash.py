"""Emotion spec Step 7: the Flash read of each typed message, on the tablet. Fake Gemini; no network, no key kept.
  1. no key: no call, status no_key, the word list still scores
  2. with a key: one call to 3.8 Flash, text not stored (store=false), scores and other-reading saved
  3. every call is logged on Layer 5's model_calls (5.5), tokens counted
  4. at a limit you set on Layer 5: no call, status at_limit
  5. a Google error or an unusable answer: word list only for that message, never raises
  6. the second opinion only counts for personal messages; switched off from the card: no call
    python test_reading_flash.py
"""
import json, os, sqlite3, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ.update(HOME=TMP, RIVIA_DIR=os.path.join(TMP, "rivia"), RIVIA_NRC_LEXICON=os.path.join(TMP, "none.txt"))
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, HERE)
import baseline  # noqa: E402
import rivia_reading as rd  # noqa: E402
import rivia_reading_flash as fl  # noqa: E402

COUNT = {"n": 0}


def ok(cond, label):
    assert cond, label
    COUNT["n"] += 1
    print("  ok ·", label)


rd.CALENDAR = lambda a, b: []
baseline.PACER = baseline.Pacer(0)
SENT = []
ANSWER = {"text": json.dumps({"valence": -2, "arousal": 1, "dominance": 0, "other_reading": True}), "fail": False}


def fake_http(method, url, body, api_key, timeout=120):
    if ANSWER["fail"]:
        raise OSError("network down")
    SENT.append({"method": method, "url": url, "body": body, "key": api_key})
    return {"status": "completed", "outputs": [{"type": "text", "text": ANSWER["text"]}],
            "usage": {"total_input_tokens": 120, "total_output_tokens": 20, "total_thought_tokens": 30}}


baseline.HTTP = fake_http
T = rd.code_tune()


def db():
    return rd.prepare(sqlite3.connect(os.path.join(tempfile.mkdtemp(), "body.db"), isolation_level=None))


def take(con, mid, text, at=1_800_000_000.0):
    return rd.take({"message_id": mid, "text": text, "sent_at": at * 1000}, con=con, now=at, tune=T)


print("1. no key")
con = db()
take(con, "a", "my dad called, it was a lot")
ok(fl.read("a", "my dad called, it was a lot", con=con, tune=T) == "no_key" and not SENT, "no key: no call")
ok(rd.message(con, "a")["flash_status"] == "no_key", "and the message says why it has no Flash scores")

ok(not fl.at_limit(con, 1_800_000_000.0), "no limits set on Layer 5: nothing blocks")

print("2. with a key")
os.environ["GEMINI_API_KEY"] = "test-key-not-real"
take(con, "b", "my dad called, it was a lot")
st = fl.read("b", "my dad called, it was a lot", con=con, tune=T)
m = rd.message(con, "b")
ok(st == "ok" and len(SENT) == 1, "one call")
ok(SENT[0]["body"]["model"] == "gemini-3.8-flash" and SENT[0]["body"]["store"] is False,
   "to 3.8 Flash, with store=false")
ok(SENT[0]["url"].endswith("/interactions") and SENT[0]["key"] == "test-key-not-real", "the key goes in the header")
ok(m["flash_v"] == round(-2 / 3, 4) and m["flash_a"] == round(1 / 3, 4) and m["other_reading"] == 1,
   "scores and the other-reading yes saved")
dump = str(con.execute("select * from reading_messages").fetchall())
ok("dad" not in dump and "test-key" not in dump, "neither the text nor the key is stored")

print("3. logged on Layer 5")
calls = con.execute("select model, input_tokens, output_tokens from model_calls").fetchall()
ok(calls == [("gemini-3.8-flash", 120, 50)], "one 3.8 Flash call, thinking counted as output")

print("4. at a limit")
import rivia_model_limits as ml  # noqa: E402
real = ml.current_table
ml.current_table = lambda: ({"gemini-3.8-flash": {"rpm": 1, "tpm": None, "rpd": None}}, [])
take(con, "c", "ok")
n = len(SENT)
ok(fl.read("c", "ok", con=con, tune=T, now=__import__("time").time()) == "at_limit" and len(SENT) == n,
   "3.8 Flash at your limit: no call")
ml.current_table = real

print("5. errors")
ANSWER["fail"] = True
import time as _t  # noqa: E402
_sleep, _t.sleep = _t.sleep, lambda s: None
take(con, "d", "ok")
ok(fl.read("d", "ok", con=con, tune=T) == "failed" and rd.message(con, "d")["flash_status"] == "failed",
   "network down: failed, word list only, no raise")
ANSWER.update(fail=False, text="I can't rate that")
take(con, "e", "ok")
ok(fl.read("e", "ok", con=con, tune=T) == "unusable", "an answer that isn't scores: unusable, never guessed")
ok(fl.read("nope", "ok", con=con, tune=T) == "gone", "a message that isn't stored: nothing sent")
_t.sleep = _sleep

print("6. personal only; switched off")
ANSWER["text"] = json.dumps({"valence": 1, "arousal": 0, "dominance": 1, "other_reading": True})
take(con, "f", "move the gym to 6")
fl.read("f", "move the gym to 6", con=con, tune=T)
ok(rd.message(con, "f")["other_reading"] is None, "a scheduling message's other-reading isn't kept")
n = len(SENT)
off = dict(T, flash_on=0)
rd.take({"message_id": "g", "text": "ok", "sent_at": 1_800_000_000_000}, con=con, now=1_800_000_000.0, tune=off)
ok(rd.message(con, "g")["flash_status"] == "off", "switched off on the card: marked off")
ok(fl.read("g", "ok", con=con, tune=off) == "off" and len(SENT) == n, "and no call")
ok("Jordan writes bluntly" in fl.RATER and "short or blunt alone is not upset" in fl.RATER,
   "the rater is told blunt is your normal style")

print("\n%d checks passed" % COUNT["n"])
