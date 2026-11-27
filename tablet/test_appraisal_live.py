"""Emotion spec Step 3 going live (Nov 26, Jordan "Draft it"): her reading of your messages, her tool results and your
thumbs now nudge her REAL mood. Throwaway RIVIA_DIR, fake Gemini; never touches ~/rivia, no network, no key kept.
  1. the switch: SHADOW is off in the code; caution flags (3.5) and rewording (3.6) stay shadow
  2. a push lands on her real mood: fast layer takes it, slow layer 10%, logged at the code door
  3. the 0.3 limit holds on her real mood; a Step 3 push is never mistaken for a hand change
  4. Layer 1 off: nothing moves; Layer 2 off: only her fast mood moves
  5. your dial still wins: the next push starts from where you set her
  6. it fades: about half an hour after a correction she's back to neutral
  7. through the real Flash read and the timer: one Flash call, real mood moves; your text never stored
  8. never fear, and a failure never raises
    python test_appraisal_live.py
"""
import json, os, sqlite3, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ.update(HOME=TMP, RIVIA_DIR=os.path.join(TMP, "rivia"), RIVIA_NRC_LEXICON=os.path.join(TMP, "none.txt"))
os.environ.pop("GEMINI_API_KEY", None)
os.makedirs(os.path.join(TMP, "rivia"), exist_ok=True)
sys.path.insert(0, HERE)
import baseline  # noqa: E402
import rivia_appraisal as ap  # noqa: E402
import rivia_emotion as emo  # noqa: E402
import rivia_expression as ex  # noqa: E402
import rivia_reading as rd  # noqa: E402
import rivia_reading_flash as fl  # noqa: E402
import rivia_satisfaction as sat  # noqa: E402
import rivia_toggles as toggles  # noqa: E402
import rivia_trust_shadow as tsh  # noqa: E402
import rivia_body_shadow as bsh  # noqa: E402

COUNT = {"n": 0}


def ok(cond, label):
    assert cond, label
    COUNT["n"] += 1
    print("  ok ·", label)


rd.CALENDAR = lambda a, b: []
T = {**ap.code_tune(), **ex.code_tune()}
T7 = rd.code_tune()
EMO = os.path.join(TMP, "rivia", "emotion.db")


def db():
    c = sqlite3.connect(os.path.join(tempfile.mkdtemp(), "body.db"), isolation_level=None)
    rd.prepare(c)
    ap.prepare(c)
    c.executescript(sat.SCHEMA + tsh.SCHEMA + bsh.DIALS_SCHEMA)
    ex.prepare(c)
    return c


def real():
    return emo.read_all()


def step3_rows():
    con = emo.db_open()
    try:
        return con.execute("select layer, door, cause, note from change_log where cause like 'step 3%' "
                           "order by id").fetchall()
    finally:
        con.close()


def calm():
    """Put her back at her normal by hand (the dial's door), so each check starts clean."""
    emo.write(0, 0, 0, cause="test reset", door="app_button", layer="mood")
    emo.write(0, 0, 0, cause="test reset", door="app_button")


def flash_answer(events, v=0, a=0, d=0):
    return json.dumps({"valence": v, "arousal": a, "dominance": d, "other_reading": False, "events": events})


print("1. the switch")
ok(ap.SHADOW is False, "Step 3's mood pushes are live in the code")
ok(ap.CAUTION_SHADOW is True and ap.for_answer() == "", "caution flags (3.5) still shadow: nothing handed to her")
ok(ex.SHADOW is True, "rewording (3.6) still shadow: no extra Flash calls")
ok(ap.LIMIT == 0.30 and ap.MOOD_SOAK == 0.10, "Jordan's numbers: 0.3 per message, 10% to her slower mood")
ok("Live:" in toggles.NOTES["l3"], "the Layer 3 switch says it's live")

print("2. a push lands on her real mood")
calm()
c = db()
before = real()
row = ap.apply(c, [{"name": "good_news", "strength": 3}], time.time(), T)
after = real()
ok(row and row["shadow"] is False and len(row["log_ids"]) == 2, "one push, two log rows (fast and slow mood)")
ok(abs(after["emotion"]["v"] - before["emotion"]["v"] - 0.3) < 0.01, "her real fast mood took the push (+0.3)")
ok(abs(after["mood"]["v"] - before["mood"]["v"] - 0.03) < 0.005, "her real slower mood took 10% of it")
rows = step3_rows()
ok([r[0] for r in rows] == ["emotion", "mood"] and all(r[1] == "code" for r in rows)
   and "good_news" in rows[0][3], "logged at the code door, never the app's or Gemini's, naming the event")
mirror = dict((r[0], r[1]) for r in c.execute("select layer, v from appraisal_state"))
ok(abs(mirror["emotion"] - after["emotion"]["v"]) < 0.01, "the old practice copy mirrors her real mood")
ok(row["after"].endswith("joy"), "she reads as joy (%s)" % row["after"])
v = ap.view(c)
ok(v["shadow"] is False and v["would_be"]["words"] == v["real_mood"], "the Layer 3 card shows her real mood, live")

print("3. the limit, and never a hand change")
calm()
c = db()
_, hand0 = ap.live_rows()
v0 = real()["emotion"]
row = ap.apply(c, [{"name": "good_news", "strength": 3}, {"name": "thanked", "strength": 3},
                   {"name": "personal", "strength": 3}], time.time(), T)
v1 = real()["emotion"]
ok(row["limited"] and all(abs(v1[k] - v0[k]) <= 0.30 + 1e-3 for k in "vad"),
   "three big events in one message: her real mood moves at most 0.3 per number")
_, hand1 = ap.live_rows()
ok(hand1 == hand0, "a Step 3 push doesn't count as you changing her by hand")

print("4. the layer switches")
calm()
c = db()
n0 = len(step3_rows())
toggles.set_switch("l1", False, door="app_button")
ok(ap.apply(c, [{"name": "thanked", "strength": 3}], time.time(), T) is None and len(step3_rows()) == n0,
   "Layer 1 off: nothing moves")
ok("Layer 1 is switched off" in ap.log_rows(c, 0, ("skip",))[-1]["why"], "and the log says why")
toggles.set_switch("l1", True, door="app_button")
toggles.set_switch("l2", False, door="app_button")
row = ap.apply(c, [{"name": "thanked", "strength": 3}], time.time(), T)
ok(row and not row["mood_moved"] and [r[0] for r in step3_rows()[n0:]] == ["emotion"],
   "Layer 2 off: only her fast mood moves")
toggles.set_switch("l2", True, door="app_button")

print("5. your dial still wins")
calm()
c = db()
ap.apply(c, [{"name": "corrected", "strength": 3}], time.time(), T)
emo.write(0.5, 0.1, 0.2, cause="test dial", door="app_button")
row = ap.apply(c, [{"name": "thanked", "strength": 1}], time.time(), T)
e = real()["emotion"]
ok(abs(e["v"] - (0.5 + 0.08)) < 0.02, "the next push starts from where you put her (0.5), not from Step 3's last")

print("6. it fades")
calm()
c = db()
now = time.time()
for i in range(3):
    ap.apply(c, [{"name": "corrected", "strength": 3}], now, T)
rows = ap.live_rows()[0]
lab_now = emo.label(*[emo.decayed(rows, now)["emotion"][k] for k in "vad"],
                    rest=tuple(rows["baseline"][k] for k in "vad"))
later = emo.decayed(rows, now + 45 * 60)
lab_later = emo.label(*[later["emotion"][k] for k in "vad"], rest=tuple(rows["baseline"][k] for k in "vad"))
ok(lab_now["name"] == "annoyance" and lab_now["intensity"] in ("faint", "mild"),
   "three corrections in a row: %s annoyance" % lab_now["intensity"])
ok(lab_later["name"] == "neutral", "45 minutes later: neutral again")

print("7. through the real Flash read and the timer")
SENT = []


def fake_http(method, url, body, api_key, timeout=120):
    SENT.append(body)
    return {"status": "completed", "outputs": [{"type": "text", "text": flash_answer(
        [{"event": "thanked", "strength": 3}], v=2, a=1, d=1)}],
            "usage": {"total_input_tokens": 100, "total_output_tokens": 20}}


baseline.HTTP = fake_http
baseline.PACER = baseline.Pacer(0)
os.environ["GEMINI_API_KEY"] = "test-key-not-real"
calm()
c = db()
v0 = real()["emotion"]["v"]
rd.take({"message_id": "f1", "text": "thanks that worked secretword", "sent_at": time.time() * 1000}, con=c,
        now=time.time(), tune=T7)
st_ = fl.read("f1", "thanks that worked secretword", con=c, tune=T7)
ok(st_ == "ok" and len(SENT) == 1, "one Flash call, the same one Step 7 already makes")
ok(real()["emotion"]["v"] > v0 + 0.2, "your thanks moved her real mood")
dump = "".join(str(c.execute("select * from %s" % t_).fetchall()) for t_ in
               ("appraisal_events", "appraisal_log", "appraisal_state", "reading_messages"))
con = emo.db_open()
dump += str(con.execute("select * from change_log").fetchall())
con.close()
ok("secretword" not in dump, "your text isn't stored anywhere, her change log included")
calm()
c = db()
core = os.path.join(TMP, "core_t")
os.makedirs(core)
k = sqlite3.connect(os.path.join(core, "core.db"))
k.execute("create table calls(ts text not null, door text not null, tool text not null, action text not null, "
          "ok integer not null, code text, ms integer not null)")
k.commit()
t0 = time.time()
ap.tick(c, t0, T, rivia_dir=core)
v0 = real()["emotion"]["v"]
sat.add(c, "thumb", 1.0, door="app_button", day=rd._day(t0), now=t0 + 1)
res = ap.tick(c, t0 + 2, T, rivia_dir=core)
ok([e_["name"] for e_ in res["events"]] == ["thumbs_up"] and real()["emotion"]["v"] > v0 + 0.15,
   "your thumbs up, on the minute timer, moved her real mood")

print("8. never fear, never raises")
calm()
c = db()
for name in ap.EVENTS:
    for s in (1, 2, 3):
        ap.apply(c, [{"name": name, "strength": s}], time.time(), T)
        rows = ap.live_rows()[0]
        lab = emo.label(*[rows["emotion"][k] for k in "vad"], rest=tuple(rows["baseline"][k] for k in "vad"))
        assert lab["name"] not in ("fear", "disgust"), (name, s, lab)
ok(True, "every event at every strength, back to back: never fear, never disgust")
real_nudge = emo.nudge


def broken(*a, **kw):
    raise emo.EmotionError("internal", "test failure")


emo.nudge = broken
rd.take({"message_id": "f2", "text": "thanks", "sent_at": time.time() * 1000}, con=c, now=time.time(), tune=T7)
ok(ap.from_flash(c, "f2", "ok", flash_answer([{"event": "thanked", "strength": 2}]), tune=T) is None,
   "her mood can't be written: the message's events are skipped, Step 7 carries on")
emo.nudge = real_nudge

print("\n%d checks passed" % COUNT["n"])
