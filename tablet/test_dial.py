"""What the phone app's dial moves (emotion spec, Step 2.4). Jordan (Nov 21): emotion as before; mood and
baseline as buttons for testing, behind a lock, every change clearly marked manual and tagged as a test or a
lasting change ("calibration", baseline only).
  1. GET keeps Step 1's "state" (emotion, what app v55.0 reads) and adds layers, dial and lock
  2. emotion: set and nudge as before, no lock, no tag; mood and baseline untouched
  3. the lock: mood and baseline refuse every change while it is closed; unlock opens it for 10 minutes,
     lock closes it, and it closes itself
  4. tags: mood and baseline need one; mood takes only test, baseline test or calibration; emotion none.
     Logged in change_log.tag and the cause ("manual test", "manual calibration"), door app_button
  5. end_test puts a layer back where it was before the test began, works while locked, and clears the
     marker; a calibration stays and shows as the marker
  6. the tag column is added to an existing log without touching its rows
  7. HAND_LAYERS: taking mood and baseline out makes the dial emotion-only again, refused as in 2.1;
     a broken setting fails loudly
  8. over HTTP (plain Starlette, no auth; test_emotion_app.py covers the token gate)
Throwaway RIVIA_DIR; never touches ~/rivia.
    python test_dial.py
"""
import json, os, socket, sqlite3, sys, tempfile, threading, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ["RIVIA_DIR"] = os.path.join(TMP, "rivia")
sys.path.insert(0, HERE)

import rivia_store as store  # noqa: E402  (after RIVIA_DIR is set)
import rivia_emotion as emo  # noqa: E402
# 2.9 merge: decay is on (2.2), so the state moves between two reads a second apart. These checks are
# about what the dial moves, which assumes no time passes, so the clock is stopped for this file. test_decay.py and
# test_step2_acceptance.py test decay.
import rivia_time as _rt  # noqa: E402
_STOPPED = _rt.now_s()
_rt.now_s = lambda: _STOPPED
import rivia_emotion_app as eapp  # noqa: E402

V55_STATE = {"v", "a", "d", "updated", "label", "intensity", "magnitude"}  # Step 1's _view keys
V55_POST = {"ok", "old", "new", "clamped"}
failed = 0


def ok(cond, label):
    global failed
    print(("  ok · " if cond else "FAIL · ") + label)
    failed += not cond


def vad(s):
    return (s["v"], s["a"], s["d"])


def close(p, q):
    return all(abs(x - y) < 1e-9 for x, y in zip(p, q))


def rows():
    return emo.read_all()


def nlog():
    return len(emo.history(100000))


# mood and baseline start somewhere other than 0 so "untouched" and "put back" mean something. Door "code"
# stands in for the code (2.2, 2.6) that moves them on its own.
emo.write(0.4, -0.2, 0.1, cause="test setup", door="code", layer="mood")
emo.write(0.2, -0.1, 0.05, cause="test setup", door="code", layer="baseline")
MOOD0, BASE0 = vad(rows()["mood"]), vad(rows()["baseline"])

print("1. GET")
g = eapp.get_reply()
ok(g["ok"] is True and set(g["state"]) == V55_STATE, "state keeps Step 1's keys (app v55.0 needs no change)")
ok(vad(g["state"]) == (0, 0, 0) and g["state"]["label"] == "neutral", "state is emotion, at 0, 0, 0")
ok(sorted(g["layers"]) == sorted(emo.LAYERS) and all(set(x) == V55_STATE | {"manual"} for x in g["layers"].values()),
   "layers shows emotion, mood and baseline, each with its label and a manual field")
ok({k: v for k, v in g["layers"]["emotion"].items() if k != "manual"} == g["state"], "layers.emotion matches state")
ok(all(x["manual"] is None for x in g["layers"].values()), "no hand change yet: every manual field is empty")
ok(g["dial"] == {"moves": ["emotion", "mood", "baseline"], "locked": ["mood", "baseline"], "read_only": [],
                 "tags": {"mood": ["test"], "baseline": ["test", "calibration"]}},
   "dial: all three move, mood and baseline behind the lock, with their tags")
ok(g["lock"] == {"locked": True, "seconds_left": 0, "layers": ["mood", "baseline"]}, "the lock starts closed")
n = nlog()
eapp.get_reply()
ok(nlog() == n and vad(rows()["mood"]) == MOOD0, "GET writes nothing")

print("2. emotion, as before")
st, r = eapp.handle({"set": {"v": 0.5, "a": 0.3, "d": -0.2}, "note": "dial test"})
ok(st == 200 and V55_POST <= set(r) and set(r["new"]) == V55_STATE, "set: 200 with ok, old, new, clamped as before")
ok(r["layer"] == "emotion" and r["tag"] is None, "the reply names the layer and no tag (added fields)")
ok(vad(rows()["emotion"]) == (0.5, 0.3, -0.2), "emotion moved, with the lock closed")
st, r = eapp.handle({"nudge": {"dv": 0.8}})
ok(st == 200 and r["new"]["v"] == 1 and r["clamped"] == ["v"], "nudge past +1 clamps and says so")
ok(vad(rows()["mood"]) == MOOD0 and vad(rows()["baseline"]) == BASE0, "mood and baseline untouched")
ok(all(x["layer"] == "emotion" and x["door"] == "app_button" and x["cause"] == "manual" and x["tag"] is None
       for x in emo.history(2)), "logged on emotion, door app_button, cause manual, no tag")
st, r = eapp.handle({"nudge": {"dv": -0.1}, "tag": "test"})
ok(st == 400 and r["code"] == "bad_tag", "emotion takes no tag")

print("3. the lock")
before, n = rows(), nlog()
locked = [eapp.handle({"set": {"v": 0, "a": 0, "d": 0}, "layer": "mood", "tag": "test"}),
          eapp.handle({"nudge": {"dv": 0.1}, "layer": "baseline", "tag": "calibration"}),
          eapp.handle({"nudge": {"dv": 0.1}, "layer": "baseline", "tag": "test"})]
ok(all(st == 400 and r["code"] == "locked" for st, r in locked), "closed: every mood and baseline change is 400 locked")
ok(rows() == before and nlog() == n, "nothing moved and nothing was logged")
st, r = eapp.handle({"unlock": True})
ok(st == 200 and r["lock"]["locked"] is False and 595 <= r["lock"]["seconds_left"] <= 600, "unlock opens it for 10 minutes")
ok(eapp.get_reply()["lock"]["locked"] is False, "GET shows it open")
st, r = eapp.handle({"lock": True})
ok(st == 200 and r["lock"]["locked"] is True, "lock closes it")
ok(eapp.handle({"nudge": {"dv": 0.1}, "layer": "mood", "tag": "test"})[1]["code"] == "locked", "closed again: refused")
eapp.handle({"unlock": True})
eapp._lock["until"] = time.monotonic() - 1  # as if 10 minutes had passed
ok(eapp.get_reply()["lock"]["locked"] is True and
   eapp.handle({"nudge": {"dv": 0.1}, "layer": "mood", "tag": "test"})[1]["code"] == "locked",
   "it closes itself when the time runs out")
bad = [{"unlock": False}, {"unlock": "yes"}, {"unlock": True, "layer": "mood"}, {"lock": True, "nudge": {"dv": 1}}]
ok(all(eapp.handle(b)[0] == 400 for b in bad) and eapp.get_reply()["lock"]["locked"], "malformed lock presses: 400, still closed")
ok(rows() == before and nlog() == n, "the lock presses changed no state and wrote no log row")

print("4. tags")
eapp.handle({"unlock": True})
st, r = eapp.handle({"nudge": {"dv": 0.1}, "layer": "mood"})
ok(st == 400 and r["code"] == "tag_needed", "mood with no tag: tag_needed")
st, r = eapp.handle({"nudge": {"dv": 0.1}, "layer": "mood", "tag": "calibration"})
ok(st == 400 and r["code"] == "bad_tag", "mood can't be calibrated (only tested)")
st, r = eapp.handle({"nudge": {"dv": 0.1}, "layer": "baseline", "tag": "forever"})
ok(st == 400 and r["code"] == "bad_tag", "a made-up tag: bad_tag")
ok(rows() == before and nlog() == n, "refused tags moved nothing")

st, r = eapp.handle({"set": {"v": -0.5, "a": 0.4, "d": 0}, "layer": "mood", "tag": "test", "note": "grumpy test"})
ok(st == 200 and r["layer"] == "mood" and r["tag"] == "test" and vad(rows()["mood"]) == (-0.5, 0.4, 0), "mood test: moves mood")
last = emo.history(1)[0]
ok(last["layer"] == "mood" and last["tag"] == "test" and last["cause"] == "manual test" and last["door"] == "app_button"
   and last["note"] == "grumpy test", "logged: layer mood, tag test, cause 'manual test', door app_button, note")
st, r = eapp.handle({"nudge": {"dv": -0.2}, "layer": "mood", "tag": "test"})
ok(st == 200 and close(vad(rows()["mood"]), (-0.7, 0.4, 0)), "a second mood test step")
ok(vad(rows()["baseline"]) == BASE0 and vad(rows()["emotion"]) == vad(before["emotion"]), "baseline and emotion untouched")
m = eapp.get_reply()["layers"]["mood"]["manual"]
ok(m is not None and m["tag"] == "test" and m["since"] <= m["at"], "GET marks mood as on a test, with when it began")

print("5. ending a test, and calibration")
eapp.handle({"lock": True})
st, r = eapp.handle({"end_test": "mood", "note": "done"})
ok(st == 200 and r["tag"] == "test_end" and close(vad(rows()["mood"]), MOOD0),
   "end_test (while locked) puts mood back where it was before the test")
last = emo.history(1)[0]
ok(last["tag"] == "test_end" and last["cause"] == "manual test end" and last["door"] == "app_button", "logged as test_end")
ok(eapp.get_reply()["layers"]["mood"]["manual"] is None, "the mood marker is gone")
st, r = eapp.handle({"end_test": "mood"})
ok(st == 400 and r["code"] == "no_test", "no open test: no_test")
ok(eapp.handle({"end_test": "emotion"})[1]["code"] == "bad_layer", "emotion has no test to end")

eapp.handle({"unlock": True})
st, r = eapp.handle({"set": {"v": 0.3, "a": -0.2, "d": 0.1}, "layer": "baseline", "tag": "calibration"})
ok(st == 200 and r["tag"] == "calibration" and vad(rows()["baseline"]) == (0.3, -0.2, 0.1), "baseline calibration: moves baseline")
ok(emo.history(1)[0]["cause"] == "manual calibration", "logged as manual calibration")
CAL = vad(rows()["baseline"])
m = eapp.get_reply()["layers"]["baseline"]["manual"]
ok(m["tag"] == "calibration", "GET marks baseline as calibrated by hand")
eapp.handle({"nudge": {"dv": 0.2}, "layer": "baseline", "tag": "test"})
eapp.handle({"nudge": {"da": 0.2}, "layer": "baseline", "tag": "test"})
ok(eapp.get_reply()["layers"]["baseline"]["manual"]["tag"] == "test", "a test on top shows as a test")
st, r = eapp.handle({"end_test": "baseline"})
ok(st == 200 and close(vad(rows()["baseline"]), CAL), "end_test goes back to the calibrated value, not the old one")
ok(eapp.get_reply()["layers"]["baseline"]["manual"]["tag"] == "calibration", "and the marker shows the calibration again")
emo.write(0.31, -0.2, 0.1, cause="drift stand-in", door="code", layer="baseline")
ok(eapp.get_reply()["layers"]["baseline"]["manual"]["tag"] == "calibration", "an automatic change later keeps the marker")
eapp.handle({"lock": True})

print("6. the tag column on an existing log")
con = sqlite3.connect(os.path.join(store.rivia_dir(), "emotion.db"))
cols = [r[1] for r in con.execute("pragma table_info(change_log)")]
con.close()
ok("tag" in cols, "change_log has a tag column")
OLD = os.path.join(TMP, "old")
os.makedirs(OLD)
MAIN_DIR, store.RIVIA_DIR = store.RIVIA_DIR, OLD  # rivia_store reads RIVIA_DIR once, at import
# a log as Step 1 makes it (no tag column), written by Step 1's own module loaded under another name
import importlib.util  # noqa: E402
STEP1 = os.environ.get("RIVIA_STEP1_DIR", os.path.join(HERE, "tests", "fixtures", "step1"))  # 2.9 merge: a frozen copy, so it runs on the tablet too
spec = importlib.util.spec_from_file_location("rivia_emotion_step1", os.path.join(STEP1, "rivia_emotion.py"))
step1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(step1)
step1.write(0.2, 0, 0, cause="manual", door="app_button")
c2 = emo.db_open()
c2.close()
old_rows = emo.history(10)
ok(len(old_rows) == 1 and old_rows[0]["tag"] is None and old_rows[0]["new_v"] == 0.2,
   "opening a log without the column adds it; the old row stays, tag empty")
ok(emo._migrate_tag_column(emo.db_open()) is False, "opening again adds nothing")
store.RIVIA_DIR = MAIN_DIR

print("7. HAND_LAYERS")
eapp.HAND_LAYERS = ("emotion",)
g = eapp.get_reply()
ok(g["dial"]["moves"] == ["emotion"] and g["dial"]["read_only"] == ["mood", "baseline"], "emotion only: GET says so")
before, n = rows(), nlog()
res = [eapp.handle(b) for b in ({"set": {"v": 0, "a": 0, "d": 0}, "layer": "mood"}, {"unlock": True},
                                {"end_test": "baseline"}, {"nudge": {"dv": 0.1}, "tag": "test"})]
ok(all(st == 400 and r["code"] == "bad_request" for st, r in res), "layer, tag, unlock, end_test: 400 bad_request, as in 2.1")
ok(rows() == before and nlog() == n, "nothing moved")
eapp.HAND_LAYERS = ("emotion", "mood")
eapp.handle({"unlock": True})
st, r = eapp.handle({"nudge": {"dv": 0.1}, "layer": "baseline", "tag": "test"})
ok(st == 400 and r["code"] == "read_only", "baseline taken out: read_only even unlocked")
eapp.handle({"lock": True})
for bad in [(), ("mood",), ("emotion", "core"), ("emotion", "emotion")]:
    eapp.HAND_LAYERS = bad
    try:
        eapp.handle({"nudge": {"dv": 0.1}})
        loud = False
    except RuntimeError:
        loud = True
    ok(loud, "HAND_LAYERS=%r fails loudly" % (bad,))
eapp.HAND_LAYERS = ("emotion", "mood", "baseline")
ok(rows() == before, "none of that moved anything")

print("8. over HTTP")
from starlette.applications import Starlette  # noqa: E402
import uvicorn  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


port = free_port()
server = uvicorn.Server(uvicorn.Config(Starlette(routes=eapp.routes()), host="127.0.0.1", port=port, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(200):
    if server.started:
        break
    time.sleep(0.05)
BASE_URL = "http://127.0.0.1:%d" % port


def http(body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE_URL + eapp.PATH, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


st, g = http()
ok(st == 200 and set(g) == {"ok", "state", "layers", "dial", "lock"}, "GET over HTTP: ok, state, layers, dial, lock")
st, r = http({"nudge": {"da": -0.1}})
ok(st == 200 and V55_POST <= set(r), "emotion nudge over HTTP keeps v55.0's keys")
st, r = http({"nudge": {"da": -0.1}, "layer": "mood", "tag": "test"})
ok(st == 400 and set(r) == {"ok", "code", "error"} and r["code"] == "locked", "locked refusal over HTTP keeps {ok, code, error}")
st, r = http({"unlock": True})
st2, r2 = http({"nudge": {"da": -0.1}, "layer": "mood", "tag": "test"})
ok(st == 200 and st2 == 200 and r2["tag"] == "test", "unlock then a mood test, over HTTP")
http({"end_test": "mood"})
http({"lock": True})
eapp.HAND_LAYERS = ("mood",)
st, r = http()
ok(st == 500 and r["code"] == "internal", "a broken HAND_LAYERS gives 500, not a wider dial")
eapp.HAND_LAYERS = ("emotion", "mood", "baseline")
server.should_exit = True

print("PASS" if not failed else "FAILED: %d" % failed)
sys.exit(1 if failed else 0)
