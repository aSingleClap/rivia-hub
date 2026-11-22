"""Rivia's three state layers (emotion spec, Step 2.1): emotion.db holds emotion, mood and baseline rows, each
starting at 0, 0, 0. Opening an installed Step 1 DB adds mood and baseline without touching the emotion row
or its change log. Every layer is clamped, logged and door-checked like emotion; a write to one layer never
moves another; read(), write() and nudge() still mean emotion by default; the phone app's dial still moves
only emotion. (test_inject.py checks that the injection line still reads only emotion.)
Throwaway RIVIA_DIR; never touches ~/rivia.
    python test_layers.py
"""
import importlib.util, os, sqlite3, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ["RIVIA_DIR"] = os.path.join(TMP, "rivia")
sys.path.insert(0, HERE)

import rivia_store as store  # noqa: E402  (after RIVIA_DIR is set)
import rivia_emotion as emo  # noqa: E402

# 2.7: these checks are about the layers, not the baseline drift limits, so the limits are opened wide here
# (2.1's checks move the baseline by 0.1 and past +1 in one go). test_drift_limits.py tests the limits.
emo.BASELINE_MAX_FROM_ANCHOR = emo.BASELINE_MAX_PER_DAY = emo.BASELINE_MAX_PER_WEEK = 10.0
# 2.9 merge: decay is on (2.2), so a layer moves between two reads a second apart. These checks are about
# storing the layers, which assumes no time passes, so the clock is stopped for this file. test_decay.py and
# test_step2_acceptance.py test decay.
import rivia_time as _rt  # noqa: E402
_STOPPED = _rt.now_s()
_rt.now_s = lambda: _STOPPED

try:
    import rivia_emotion_app as eapp  # noqa: E402  (needs starlette; skipped without it)
except ImportError:
    eapp = None

ZERO = (0, 0, 0)


def ok(cond, label):
    assert cond, label
    print("  ok ·", label)


def refused(code, fn):
    try:
        fn()
    except emo.EmotionError as e:
        return e.code == code
    return False


def vad(s):
    return (s["v"], s["a"], s["d"])


def db_path():
    return os.path.join(store.rivia_dir(), "emotion.db")


def log_count():
    con = sqlite3.connect(db_path())
    try:
        return con.execute("select count(*) from change_log").fetchone()[0]
    finally:
        con.close()


print("1. a fresh emotion.db")
ok(emo.LAYERS == ("emotion", "mood", "baseline"), "three layers: emotion, mood, baseline")
allv = emo.read_all()
ok(sorted(allv) == ["baseline", "emotion", "mood"], "read_all returns every layer")
ok(all(vad(allv[k]) == ZERO for k in emo.LAYERS), "all three start at 0, 0, 0")
ok(vad(emo.read()) == ZERO and emo.read() == allv["emotion"], "read() with no layer is still the emotion row")
ok(all(emo.read(layer=k) == allv[k] for k in emo.LAYERS), "read(layer=) reads each row")
ok(log_count() == 0, "adding rows writes no change_log row")
ok(sorted(os.listdir(store.rivia_dir())) == ["emotion.db"], "still only emotion.db")

print("2. opening an installed Step 1 emotion.db")
# Build the DB with the installed Step 1 module itself (step1/rivia_emotion.py, as on the tablet at 7f23ad1),
# loaded under another name: one emotion row moved by a real Step 1 write, with its change log row.
STEP1 = os.environ.get("RIVIA_STEP1_DIR", os.path.join(HERE, "tests", "fixtures", "step1"))  # 2.9 merge: a frozen copy, so it runs on the tablet too
os.remove(db_path())
spec = importlib.util.spec_from_file_location("rivia_emotion_step1", os.path.join(STEP1, "rivia_emotion.py"))
emo1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(emo1)
ok(not hasattr(emo1, "LAYERS"), "the Step 1 module is the one-layer version")
emo1.write(0.4, -0.2, 0.1, cause="manual", door="code")
step1_row = emo1.read()
con = sqlite3.connect(db_path())
ok([r[0] for r in con.execute("select layer from state")] == ["emotion"], "Step 1 made one row")
con.close()
allv = emo.read_all()
ok(allv["emotion"] == step1_row, "the emotion row and its last-updated time are kept exactly")
ok(vad(allv["mood"]) == ZERO and vad(allv["baseline"]) == ZERO, "mood and baseline appear at 0, 0, 0")
ok(log_count() == 1 and emo.history()[0]["cause"] == "manual", "the Step 1 change log is kept, and no row is added")

print("3. each layer is written on its own")
before = emo.read_all()
r = emo.write(0.3, 0.2, -0.1, cause="mood test", door="code", layer="mood")
after = emo.read_all()
ok(vad(after["mood"]) == (0.3, 0.2, -0.1) and vad(r["new"]) == (0.3, 0.2, -0.1), "write(layer='mood') sets mood")
ok(after["emotion"] == before["emotion"] and after["baseline"] == before["baseline"], "emotion and baseline untouched")
r = emo.nudge(dv=0.1, cause="baseline test", door="admin", layer="baseline")
after = emo.read_all()
ok(vad(after["baseline"]) == (0.1, 0, 0), "nudge(layer='baseline') moves baseline")
ok(after["emotion"] == before["emotion"] and vad(after["mood"]) == (0.3, 0.2, -0.1), "emotion and mood untouched")
emo.write(-0.5, 0.5, 0.0, cause="emotion default", door="code")
after = emo.read_all()
ok(vad(after["emotion"]) == (-0.5, 0.5, 0.0), "write() with no layer still writes emotion")
ok(vad(after["mood"]) == (0.3, 0.2, -0.1) and vad(after["baseline"]) == (0.1, 0, 0), "and only emotion")

print("4. clamping (1.2) on every layer")
for layer in emo.LAYERS:
    r = emo.write(1.5, -3, 0.25, cause="overshoot", door="code", layer=layer)
    ok(vad(emo.read(layer=layer)) == (1.0, -1.0, 0.25) and r["clamped"] == ["v", "a"],
       "%s: past +1 stored as +1, below -1 as -1, clamped axes reported" % layer)
    r = emo.nudge(dv=0.5, da=-0.5, cause="nudge past edge", door="code", layer=layer)
    ok(vad(r["new"]) == (1.0, -1.0, 0.25) and r["clamped"] == ["v", "a"], "%s: a nudge past an edge clamps too" % layer)

print("5. the change log (1.7) names the layer")
rows = emo.history(6)
ok([r["layer"] for r in rows] == ["baseline", "baseline", "mood", "mood", "emotion", "emotion"],
   "each change is logged under its own layer")
n = log_count()
emo.write(0.2, 0.2, 0.2, cause="one row", door="code", layer="mood")
ok(log_count() == n + 1, "a write to one layer adds exactly one log row")

print("6. refusals leave every layer alone")
before = emo.read_all()
n = log_count()
ok(refused("bad_layer", lambda: emo.write(0, 0, 0, cause="x", door="code", layer="anger")), "unknown layer refused")
ok(refused("bad_layer", lambda: emo.nudge(dv=0.1, cause="x", door="code", layer=None)), "no layer (None) refused")
ok(refused("bad_layer", lambda: emo.read(layer="Mood")), "layer names are exact (Mood refused)")
ok(refused("not_allowed", lambda: emo.write(0, 0, 0, cause="x", door="app", layer="mood")), "app door refused on mood")
ok(refused("not_allowed", lambda: emo.write(0, 0, 0, cause="x", door="app", layer="baseline")),
   "app door refused on baseline")
ok(refused("no_cause", lambda: emo.write(0, 0, 0, cause=" ", door="code", layer="baseline")), "empty cause refused")
ok(refused("bad_value", lambda: emo.write(float("nan"), 0, 0, cause="x", door="code", layer="mood")), "NaN refused")
ok(emo.read_all() == before and log_count() == n, "state and change log unchanged after every refusal")

print("7. reopening never resets a layer")
before = emo.read_all()
emo.db_open().close()
emo.db_open().close()
ok(emo.read_all() == before, "two more opens keep every row as it was")

print("8. the phone app's dial moves emotion; mood and baseline wait behind the lock (2.4)")
if eapp is None:
    print("  skip · starlette not installed")
else:
    before = emo.read_all()
    status, reply = eapp.handle({"set": {"v": 0.1, "a": 0.1, "d": 0.1}})
    after = emo.read_all()
    ok(status == 200 and vad(after["emotion"]) == (0.1, 0.1, 0.1), "set moves emotion")
    ok(after["mood"] == before["mood"] and after["baseline"] == before["baseline"], "mood and baseline untouched")
    status, reply = eapp.handle({"set": {"v": 0, "a": 0, "d": 0}, "layer": "mood", "tag": "test"})
    ok(status == 400 and reply["code"] == "locked" and emo.read_all() == after,
       "2.4: mood is locked until the app unlocks it (400) and nothing moves; test_dial.py covers the rest")
    status, reply = eapp.handle({"nudge": {"dv": 0.1, "layer": "baseline"}})
    ok(status == 400 and emo.read_all() == after, "a layer inside the nudge is refused too")

print("PASS")
