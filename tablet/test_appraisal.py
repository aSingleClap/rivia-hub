"""Emotion spec Step 3.1 to 3.5: events, preset pushes, the per-message limit, the message read and caution flags.
Shadow path (kept for going back; live is test_appraisal_live.py). Fake Gemini, throwaway folders; no network, no key kept.
  1. the event menu: no event points toward fear; the biggest equals the limit; cursing and thumbs down are a third
  2. Flash's events: only menu names and strengths 1-3 are taken, at most 3, never numbers
  3. pushes: strength scales them, one message is held to the limit, her slower mood takes 10%
  4. the would-be mood: starts from her real mood, starts again after a hand change, never writes emotion.db
  5. from Step 7's Flash read: events land; over the Layer 5 limit they're skipped; counted once with Step 6
  6. code events on the timer: her tool calls (hers only), thumbs, plan checks (her misses only), unanswered questions
  7. tone cap: an unpleasant mood shows at most "moderate"; caution flags gathered, never handed to her in shadow
  8. through the real Flash read: one call, events and scores, your text never stored
    python test_appraisal.py
"""
import json, os, sqlite3, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ.update(HOME=TMP, RIVIA_DIR=os.path.join(TMP, "rivia"), RIVIA_NRC_LEXICON=os.path.join(TMP, "none.txt"))
os.environ.pop("GEMINI_API_KEY", None)
os.makedirs(os.path.join(TMP, "rivia"), exist_ok=True)
sys.path.insert(0, HERE)
import baseline  # noqa: E402
import rivia_appraisal as ap  # noqa: E402
ap.SHADOW = True  # this file checks the shadow path (kept for going back); test_appraisal_live.py checks live
import rivia_reading as rd  # noqa: E402
import rivia_reading_flash as fl  # noqa: E402
import rivia_satisfaction as sat  # noqa: E402
import rivia_trust_shadow as tsh  # noqa: E402
import rivia_body_shadow as bsh  # noqa: E402
import rivia_expression as ex  # noqa: E402
import rivia_time as rt  # noqa: E402

COUNT = {"n": 0}


def ok(cond, label):
    assert cond, label
    COUNT["n"] += 1
    print("  ok ·", label)


rd.CALENDAR = lambda a, b: []
T = {**ap.code_tune(), **ex.code_tune()}
T7 = rd.code_tune()
NOW = 1_800_000_000.0
ZERO = {"v": 0.0, "a": 0.0, "d": 0.0, "updated": NOW - 3600}
LIVE = ({"emotion": dict(ZERO), "mood": dict(ZERO), "baseline": dict(ZERO)}, None)


def db():
    c = sqlite3.connect(os.path.join(tempfile.mkdtemp(), "body.db"), isolation_level=None)
    rd.prepare(c)
    ap.prepare(c)
    c.executescript(sat.SCHEMA + tsh.SCHEMA + bsh.DIALS_SCHEMA)
    ex.prepare(c)
    return c


def take(con, mid, text, at=NOW):
    return rd.take({"message_id": mid, "text": text, "sent_at": at * 1000}, con=con, now=at, tune=T7)


def flash_answer(events, v=0, a=0, d=0):
    return json.dumps({"valence": v, "arousal": a, "dominance": d, "other_reading": False, "events": events})


print("1. the event menu")
ok(all(ap.no_fear(e["dir"]) for e in ap.EVENTS.values()), "no event moves her toward fear (worse, wound up, less "
                                                          "in control at once)")
ok(all(max(abs(x) for x in e["dir"]) == 1.0 for e in ap.EVENTS.values()), "every direction's biggest part is 1")
big = max(ap.EVENTS, key=lambda n: ap.EVENTS[n]["size"])
ok(big == "good_news" and max(abs(x) for x in ap.push_for(big, 3, T)) == T["limit"],
   "the biggest event at full strength equals the limit")
ok(abs(ap.EVENTS["cursed_at_her"]["size"] - 1 / 3) < 1e-9 and abs(ap.EVENTS["thumbs_down"]["size"] - 1 / 3) < 1e-9,
   "cursing at her and a thumbs down are a third of the biggest")
ok(set(ap.MESSAGE_EVENTS) == {"thanked", "corrected", "frustrated", "cursed_at_her", "personal", "good_news",
                              "bad_news"}
   and set(ap.CODE_EVENTS) == {"tool_failed", "tool_worked", "thumbs_up", "thumbs_down", "plan_held", "plan_missed",
                               "unanswered"}, "the longer list, as Jordan picked")
ok("disgust" not in json.dumps(ap.EVENTS) and "fear" not in json.dumps(ap.EVENTS), "no disgust, no fear in the menu")
ok(fl.SCHEMA["properties"]["events"]["items"]["properties"]["event"]["enum"] == list(ap.MESSAGE_EVENTS)
   and "events" not in fl.SCHEMA["required"], "Step 7's Flash call asks for events by name (not required)")
ok("cursed_at_her" in fl.RATER and fl.RATER.rstrip().endswith("Answer with JSON only."),
   "the menu rides in Step 7's rater, same call")

print("2. Flash's events")
got = ap.events_from(json.dumps({"events": [{"event": "thanked", "strength": 2}, {"event": "fear", "strength": 3},
                                            {"event": "good_news", "strength": 7}, {"event": "thanked", "strength": 1},
                                            {"event": "personal", "strength": True},
                                            {"event": "bad_news", "strength": 1}, {"event": "corrected", "strength": 1},
                                            {"event": "frustrated", "strength": 1}]}))
ok(got == [{"name": "thanked", "strength": 2}, {"name": "bad_news", "strength": 1},
           {"name": "corrected", "strength": 1}],
   "unknown names, bad strengths and repeats dropped; at most 3 taken")
ok(ap.events_from("not json") == [] and ap.events_from(json.dumps({"events": "x"})) == [], "junk: no events")
ok(ap.events_from(json.dumps({"events": [{"event": "thanked", "strength": 2, "valence": 3}]}))
   == [{"name": "thanked", "strength": 2}], "numbers Flash adds are ignored: only name and strength")

print("3. pushes")
full = ap.push_for("thanked", 3, T)
ok(all(abs(ap.push_for("thanked", 1, T)[i] - full[i] / 3) < 1e-6 for i in range(3))
   and all(abs(ap.push_for("thanked", 2, T)[i] - full[i] * 2 / 3) < 1e-6 for i in range(3)),
   "strength 1 / 2 / 3 = a third / two thirds / the whole push")
capped, raw, limited = ap.combine([ap.push_for("good_news", 3, T), ap.push_for("thanked", 3, T)], T)
ok(limited and capped[0] == 0.3 and raw[0] > 0.3, "two big events in one message: held to the limit (0.3)")
c = db()
row = ap.apply(c, [{"name": "good_news", "strength": 3}], NOW, T, live=LIVE)
ok(abs(row["emotion"][0] - 0.3) < 1e-6 and abs(row["mood"][0] - 0.03) < 1e-6, "her slower mood takes 10% of it")
ok(row["after"].endswith(("contentment", "joy")) and row["shadow"], "she'd read happier (shadow)")

print("4. the would-be mood")
c = db()
live = ({"emotion": {"v": -0.4, "a": 0.2, "d": 0.0, "updated": NOW}, "mood": dict(ZERO),
         "baseline": dict(ZERO)}, 7)
st, synced = ap.would_be(c, NOW, live)
ok(synced and abs(st["emotion"]["v"] + 0.4) < 1e-6, "starts from her real mood")
ap.apply(c, [{"name": "thanked", "strength": 3}], NOW, T, live=live)
st, synced = ap.would_be(c, NOW, live)
ok(not synced and st["emotion"]["v"] > -0.4, "then follows its own pushes")
st, _ = ap.would_be(c, NOW + 3 * 3600, live)
ok(abs(st["emotion"]["v"]) < 0.05, "and fades with Step 2's decay")
live2 = (live[0], 9)
st, synced = ap.would_be(c, NOW, live2)
ok(synced and abs(st["emotion"]["v"] + 0.4) < 1e-6, "you change her mood by hand: the copy starts again from it")
emo_path = os.path.join(TMP, "rivia", "emotion.db")
e = sqlite3.connect(emo_path)
try:
    ap.prepare(e)
    refused = False
except ap.AppraisalError as err:
    refused = err.code == "wrong_db"
e.close()
os.remove(emo_path)
ok(refused, "Step 3's shadow rows are refused in emotion.db")
ok(ap.live_rows()[0]["emotion"]["v"] == 0.0 and not os.path.exists(emo_path),
   "no emotion.db: zeros, and reading never creates one")
import ast  # noqa: E402
calls = {n.func.attr for n in ast.walk(ast.parse(open(os.path.join(HERE, "rivia_appraisal.py")).read()))
         if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
src = open(os.path.join(HERE, "rivia_appraisal.py")).read()
shadow_src = src.replace(src[src.index("def _apply_live("):src.index("def _record(")], "")
calls = {n.func.attr for n in ast.walk(ast.parse(shadow_src)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
ok(not calls & {"nudge", "write", "_apply"}, "in shadow it never calls nudge(), write() or _apply() on her real mood "
                                             "(only _apply_live does)")

print("5. from Step 7's Flash read")
c = db()
take(c, "m1", "thanks, that worked")
r = ap.from_flash(c, "m1", "ok", flash_answer([{"event": "thanked", "strength": 2}]), now=NOW, tune=T, live=LIVE)
ok(r and r["events"] == [{"name": "thanked", "strength": 2}] and r["push"][0] > 0, "thanked: a push lands")
take(c, "m2", "ugh")
ok(ap.from_flash(c, "m2", "at_limit", None, now=NOW, tune=T, live=LIVE) is None
   and "Layer 5 limit" in ap.log_rows(c, 0, ("skip",))[-1]["why"], "Flash over your Layer 5 limit: skipped, says why")
ok(ap.from_flash(c, "m2", "ok", flash_answer([{"event": "thanked", "strength": 1}]), now=NOW,
                 tune=dict(T, message_events_on=0), live=LIVE) is None, "switched off on the card: no events")
take(c, "m3", "thats wrong, the gym is at 6")
sat.add(c, "words", -1.0, door="app_words", day=rd._day(NOW), now=NOW, message_id="m3")
r = ap.from_flash(c, "m3", "ok", flash_answer([{"event": "corrected", "strength": 2}]), now=NOW, tune=T, live=LIVE)
ok(r is None and ap.log_rows(c, 0, ("not_counted",))[-1]["name"] == "corrected",
   "Step 6 already heard the correction: counted once")
ok(ap.from_flash(c, "m1", "ok", flash_answer([{"event": "thanked", "strength": 2}]), now=NOW, tune=T, live=LIVE)
   is None, "the same message read twice: counted once")
ok(ap.from_flash(c, "m1", "ok", "{", now=NOW, tune=None, live="broken") is None, "a failure never raises")

print("6. code events on the timer")
c = db()
core = os.path.join(TMP, "core_t")
os.makedirs(core)
k = sqlite3.connect(os.path.join(core, "core.db"))
k.execute("create table calls(ts text not null, door text not null, tool text not null, action text not null, "
          "ok integer not null, code text, ms integer not null)")
first = ap.tick(c, NOW, T, live=LIVE, rivia_dir=core)
ok(first["events"] == [] and ap._meta(c, "tick_at") == str(NOW), "first tick: starts from now, nothing replayed")
calls = [(1, None, "plan_day"), (1, None, "find_place"), (0, "calendar_http", "plan_day"),
         (0, "place_exists", "add_place"), (0, "internal", "app_trust"), (1, None, "health_status")]
for okk, code, tool in calls:
    k.execute("insert into calls values(?,?,?,?,?,?,?)", (rt.iso(int(NOW + 30)), "app", tool, "x", okk, code, 50))
k.commit()
sat.add(c, "thumb", 1.0, door="app_button", day=rd._day(NOW), now=NOW + 40)
sat.add(c, "words", 1.0, door="app_words", day=rd._day(NOW), now=NOW + 40, message_id="n9")
c.execute("insert into trust_predictions values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          ("p1", "2027-01-15", "arrive", "arrive:gym", None, None, None, "{}", "hit", None, "", "{}", 1, NOW + 50))
c.execute("insert into trust_predictions values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          ("p2", "2027-01-15", "arrive", "arrive:hmc", None, None, None, "{}", "miss", "jordan", "", "{}", 1, NOW + 50))
c.execute("insert into trust_predictions values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          ("p3", "2027-01-15", "arrive", "arrive:farm", None, None, None, "{}", "miss", "rivia", "", "{}", 1, NOW + 50))
res = ap.tick(c, NOW + 60, T, live=LIVE, rivia_dir=core)
names = {e_["name"]: e_["strength"] for e_ in res["events"]}
ok(names.get("tool_worked") == 2 and names.get("tool_failed") == 1,
   "her tool calls: 2 worked, 1 failed (a refused bad input, the app's own routes and her health lookup don't count)")
ok(names.get("thumbs_up") == 3 and names.get("thanked") == 2, "your thumbs up and your praise")
ok(names.get("plan_held") == 1 and names.get("plan_missed") == 1, "plan held; only her own miss counts")
ok(any(r_["why"].startswith("the miss was jordan") for r_ in ap.log_rows(c, 0, ("not_counted",))),
   "your miss is logged, not counted against her")
ok(res["push"] and not any(abs(x) > T["limit"] + 1e-9 for x in res["push"]["push"]),
   "everything from one tick is one push, held to the limit")
ok(ap.tick(c, NOW + 120, T, live=LIVE, rivia_dir=core)["events"] == [], "the next tick doesn't count them again")
# her question, unanswered
ex.take_reply({"message_id": "r1", "text": "Want me to look for a closer gym near the farm then?", "at": NOW * 1000},
              con=c, now=NOW)
ex.take_reply({"message_id": "r2", "text": "Do you want the long version of that or just the short one?",
               "at": (NOW + 10) * 1000}, con=c, now=NOW + 10)
c.execute("insert into reading_shifts values(?,?,?,?,?)", (rd._day(NOW + 6 * 3600), "[]", 1, NOW, None))
c.execute("insert into reading_shifts values(?,?,?,?,?)", (rd._day(NOW + 6 * 3600 - 86400), "[]", 1, NOW, None))
res = ap.tick(c, NOW + 5 * 3600, T, live=LIVE, rivia_dir=core)
ok(res["events"] == [], "before 6 hours: not yet")
res = ap.tick(c, NOW + 7 * 3600, T, live=LIVE, rivia_dir=core)
ok([e_["name"] for e_ in res["events"]] == ["unanswered", "unanswered"], "after 6 hours with no answer: counted")
ex.take_reply({"message_id": "r3", "text": "Should I keep the farm pin where it is for tomorrow morning or move it?",
               "at": (NOW + 8 * 3600) * 1000}, con=c, now=NOW + 8 * 3600)
take(c, "a1", "keep it", at=NOW + 9 * 3600)
ex.take_reply({"message_id": "r4", "text": "Do you want me to add the vet visit to the shared list for the horses?",
               "at": (NOW + 8 * 3600) * 1000}, con=c, now=NOW + 8 * 3600)
c.execute("update expression_replies set at = ? where message_id = 'r4'", (NOW + 9.5 * 3600,))
c.execute("insert or replace into reading_shifts values(?,?,?,?,?)",
          (rd._day(NOW + 15.5 * 3600), json.dumps([[NOW + 15 * 3600, NOW + 20 * 3600]]), 1, NOW, None))
res = ap.tick(c, NOW + 17 * 3600, T, live=LIVE, rivia_dir=core)
judged = dict(c.execute("select message_id, asked_result from expression_replies").fetchall())
ok(judged["r3"] == "answered" and res["events"] == [], "you answered: nothing counted")
ok(judged["r4"] == "at_work", "you were at work when it came due: not counted")
dead = db()
dead.close()
ok(ap.tick(dead, NOW, T, live=LIVE, rivia_dir=core).get("error"), "a bad tick returns an error, never raises")

print("7. tone cap and caution flags")
st = {"emotion": {"v": -0.8, "a": 0.6, "d": 0.3, "updated": NOW}, "mood": dict(ZERO), "baseline": dict(ZERO)}
o = ap.outward(st, T)
ok(o["inner"] == "strong" and o["shown"] == "moderate" and o["capped"], "strong inside, shows at most moderate")
ok(ap.outward(st, dict(T, tone_cap=2))["shown"] == "mild", "the cap is yours to set")
st["emotion"] = {"v": 0.9, "a": 0.4, "d": 0.4, "updated": NOW}
ok(not ap.outward(st, T)["capped"], "a pleasant mood isn't capped")
c = db()
c.execute("insert into dials(at, double_check, pause, blind_spots) values(?,?,?,?)",
          (int(NOW), 1, 0, json.dumps(["tool_failure_rate"])))
lines = ap.caution_lines(c)
ok(lines == ["double-check before acting", "blind spot: tool_failure_rate"], "Step 4's flags gathered into one list")
ok(ap.for_answer(c) == "", "in shadow, nothing is handed to her")

print("8. through the real Flash read")
SENT = []


def fake_http(method, url, body, api_key, timeout=120):
    SENT.append(body)
    return {"status": "completed", "outputs": [{"type": "text", "text": flash_answer(
        [{"event": "bad_news", "strength": 3}, {"event": "personal", "strength": 1}], v=-2, a=0, d=-1)}],
            "usage": {"total_input_tokens": 100, "total_output_tokens": 20}}


baseline.HTTP = fake_http
baseline.PACER = baseline.Pacer(0)
os.environ["GEMINI_API_KEY"] = "test-key-not-real"
c = db()
take(c, "f1", "grandpa is back in the hospital secretword")
st_ = fl.read("f1", "grandpa is back in the hospital secretword", con=c, tune=T7, now=NOW)
pushes = ap.log_rows(c, 0, ("push",))
ok(st_ == "ok" and len(SENT) == 1, "one Flash call, the same one Step 7 makes")
ok(pushes and [e_["name"] for e_ in pushes[-1]["events"]] == ["bad_news", "personal"], "its events moved the "
                                                                                       "would-be mood")
ok(rd.message(c, "f1")["flash_v"] == round(-2 / 3, 4), "Step 7's scores are stored as before")
dump = "".join(str(c.execute("select * from %s" % t_).fetchall()) for t_ in
               ("appraisal_events", "appraisal_log", "appraisal_state", "reading_messages"))
ok("secretword" not in dump and "grandpa" not in dump, "your text isn't stored anywhere")

print("\n%d checks passed" % COUNT["n"])
