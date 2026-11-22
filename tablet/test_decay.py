"""Step 2.2: decay on read. Emotion decays toward mood (10-minute half-life), mood toward baseline (4 hours),
worked out from the time since each row was stored, whenever the state is read. No timer, survives restarts,
reads never write, and a change starts from the decayed state.
The clock is faked by replacing rivia_time.now_s, so no test waits. Throwaway RIVIA_DIR; never touches ~/rivia.
    python test_decay.py
"""
import ast, math, os, random, sqlite3, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ["RIVIA_DIR"] = os.path.join(TMP, "rivia")
sys.path.insert(0, HERE)

import rivia_store as store  # noqa: E402  (after RIVIA_DIR is set)
import rivia_time as rt  # noqa: E402
import rivia_emotion as emo  # noqa: E402

CLOCK = {"t": 1_800_000_000.0}   # whole seconds throughout, so stored timestamps lose nothing
rt.now_s = lambda: CLOCK["t"]
MIN, HOUR = 60, 3600
EH, MH = emo.EMOTION_HALF_LIFE_S, emo.MOOD_HALF_LIFE_S


def ok(cond, label):
    assert cond, label
    print("  ok ·", label)


def near(x, y, tol=1e-9):
    return abs(x - y) <= tol


def later(seconds):
    CLOCK["t"] += seconds


def put(layer, v, a, d):
    """Test setup only: set a stored row directly, stamped now (2.1/2.4 own the real ways to set mood and baseline)."""
    con = emo.db_open()
    con.execute("update state set v=?, a=?, d=?, updated=? where layer=?", (v, a, d, rt.iso(rt.now_s()), layer))
    con.close()


def stored():
    con = sqlite3.connect(os.path.join(store.rivia_dir(), "emotion.db"))
    rows = {r[0]: r[1:] for r in con.execute("select layer, v, a, d, updated from state")}
    n = con.execute("select count(*) from change_log").fetchone()[0]
    con.close()
    return rows, n


def reset(e=(0, 0, 0), m=(0, 0, 0), b=(0, 0, 0)):
    for layer, p in (("baseline", b), ("mood", m), ("emotion", e)):
        put(layer, *p)


def integrate(e, m, b, seconds, steps=200000):
    """Independent check: step the two equations forward numerically (RK4) instead of using the formula."""
    ke, km = math.log(2) / EH, math.log(2) / MH
    h = seconds / steps

    def f(e_, m_):
        return -ke * (e_ - m_), -km * (m_ - b)
    for _ in range(steps):
        k1 = f(e, m)
        k2 = f(e + h / 2 * k1[0], m + h / 2 * k1[1])
        k3 = f(e + h / 2 * k2[0], m + h / 2 * k2[1])
        k4 = f(e + h * k3[0], m + h * k3[1])
        e += h / 6 * (k1[0] + 2 * k2[0] + 2 * k3[0] + k4[0])
        m += h / 6 * (k1[1] + 2 * k2[1] + 2 * k3[1] + k4[1])
    return e, m


print("1. the settings")
ok(EH == 10 * MIN and MH == 4 * HOUR, "half-lives start at the spec's 10 minutes (emotion) and 4 hours (mood)")
s = emo.read_all()
ok(set(s) == {"emotion", "mood", "baseline"}, "three layers to read (2.1's rows; stand-in here)")
ok(all((s[layer]["v"], s[layer]["a"], s[layer]["d"]) == (0, 0, 0) for layer in s), "a fresh DB reads 0, 0, 0 on every layer")
later(5 * HOUR)
ok(all((emo.read_all()[layer]["v"], emo.read_all()[layer]["a"]) == (0, 0) for layer in s), "and stays exactly 0 as time passes")

print("2. emotion fades toward mood, 10-minute half-life")
reset(e=(0.8, 0.4, -0.6))
later(10 * MIN)
r = emo.read()
ok(near(r["v"], 0.4) and near(r["a"], 0.2) and near(r["d"], -0.3), "10 minutes: halfway to mood on every axis (0.8 -> 0.4)")
later(10 * MIN)
ok(near(emo.read()["v"], 0.2, 1e-3), "20 minutes: a quarter left (0.2; mood itself has barely moved, it's at 0)")
reset(e=(0.8, 0, 0), m=(0.5, 0, 0), b=(0.5, 0, 0))
later(10 * MIN)
ok(near(emo.read()["v"], 0.65), "it fades toward mood, not toward 0: 0.8 with mood 0.5 is 0.65 after 10 minutes")
reset(e=(-0.6, 0, 0), m=(0.2, 0, 0), b=(0.2, 0, 0))
later(10 * MIN)
ok(near(emo.read()["v"], -0.2), "from below too: -0.6 with mood 0.2 is -0.2 after 10 minutes")

print("3. mood fades toward baseline, 4-hour half-life")
reset(e=(0.6, 0.6, 0.6), m=(0.6, 0.6, 0.6), b=(0.1, 0.1, 0.1))
later(4 * HOUR)
s = emo.read_all()
ok(near(s["mood"]["v"], 0.35), "4 hours: mood halfway to baseline (0.6 -> 0.35 with baseline 0.1)")
ok(s["baseline"]["v"] == 0.1, "baseline does not decay (its drift is 2.6/2.7)")
ok(s["mood"]["v"] < s["emotion"]["v"] < 0.6, "emotion follows mood down and lags just above it")
later(-4 * HOUR)
reset(e=(0.6, 0.6, 0.6), m=(0.6, 0.6, 0.6), b=(0.1, 0.1, 0.1))
later(30 * MIN)
ok(emo.read()["v"] > 0.55, "half an hour after a spike shared by mood, emotion is still high (mood holds it up)")

print("4. the formula matches a step-by-step simulation")
for e0, m0, b0, secs in ((0.9, -0.3, 0.1, 25 * MIN), (-1.0, 1.0, 0.0, 3 * HOUR), (0.2, 0.8, -0.5, 9 * HOUR)):
    reset(e=(e0, 0, 0), m=(m0, 0, 0), b=(b0, 0, 0))
    later(secs)
    s = emo.read_all()
    want_e, want_m = integrate(e0, m0, b0, secs)
    ok(near(s["emotion"]["v"], want_e, 1e-7) and near(s["mood"]["v"], want_m, 1e-7),
       "e %.1f, mood %.1f, base %.1f after %d min: %.5f / %.5f" % (e0, m0, b0, secs // 60, want_e, want_m))

print("5. reads never write, and how often she's read changes nothing")
reset(e=(0.7, -0.4, 0.3), m=(0.3, 0.1, 0.0), b=(0.05, 0.0, 0.1))
before = stored()
one_jump_at = CLOCK["t"] + 2 * HOUR
for _ in range(120):
    later(MIN)
    emo.read(); emo.read_all(); emo.read(layer="mood"); emo.label(*(emo.read()[k] for k in "vad"))
ok(stored() == before, "120 reads over 2 hours: stored rows and change log byte for byte unchanged")
ok(CLOCK["t"] == one_jump_at, "(clock check)")
many = emo.read_all()
CLOCK["t"] -= 2 * HOUR
reset(e=(0.7, -0.4, 0.3), m=(0.3, 0.1, 0.0), b=(0.05, 0.0, 0.1))
later(2 * HOUR)
ok(emo.read_all() == many,
   "reading every minute lands exactly where one read after 2 hours does")

print("6. no timer: works across restarts")
src = open(os.path.join(HERE, "rivia_emotion.py")).read()
mods = {n.names[0].name if isinstance(n, ast.Import) else n.module
        for n in ast.walk(ast.parse(src)) if isinstance(n, (ast.Import, ast.ImportFrom))}
ok(not mods & {"threading", "sched", "asyncio", "time", "signal", "multiprocessing"},
   "rivia_emotion starts no thread, loop or timer (imports: %s)" % ", ".join(sorted(mods)))
reset(e=(0.8, 0, 0))
later(10 * MIN)
for name in [m for m in sys.modules if m in ("rivia_emotion",)]:
    del sys.modules[name]
import rivia_emotion as emo2  # noqa: E402  (a fresh import, as after a server restart)
ok(near(emo2.read()["v"], 0.4, 1e-3), "after a 'restart' (fresh import, new connection) the 10 minutes still counted")
emo = emo2
reset(e=(0.8, 0, 0))
later(3 * 24 * HOUR)
ok(abs(emo.read()["v"]) < 1e-9, "server off for 3 days: she has cooled all the way to mood and baseline")

print("7. a change starts from where she is now")
reset(e=(0.8, 0, 0))
n0 = stored()[1]
later(10 * MIN)
r = emo.nudge(0.1, 0, 0, cause="test", door="code")
ok(near(r["old"]["v"], 0.4) and near(r["new"]["v"], 0.5), "nudge +0.1 after 10 minutes: 0.4 -> 0.5, not 0.8 -> 0.9")
row = emo.history(1)[0]
ok(near(row["old_v"], 0.4) and near(row["new_v"], 0.5) and stored()[1] == n0 + 1,
   "the change log's old value is her decayed state; one row, no extra row for the decay")
ok(stored()[0]["emotion"][0] == row["new_v"] and stored()[0]["emotion"][3] == rt.iso(CLOCK["t"]),
   "the change is written back with the change's own time")
later(10 * MIN)
ok(near(emo.read()["v"], 0.25, 1e-3), "and decays from there: 0.5 -> 0.25 ten minutes later")
reset(e=(0.8, 0, 0))
later(10 * MIN)
r = emo.write(0.3, 0.3, 0.3, cause="test", door="code")
ok(near(r["old"]["v"], 0.4) and r["new"]["v"] == 0.3, "write() reports the decayed old state too")

print("8. which rows a change touches")
reset(e=(0.8, 0, 0), m=(0.4, 0, 0), b=(0.0, 0, 0))
mood_row = stored()[0]["mood"]
later(10 * MIN)
emo.nudge(0.05, 0, 0, cause="test", door="code")
ok(stored()[0]["mood"] == mood_row, "an emotion change leaves the mood row alone (as in Step 1 and 2.1)")
reset(e=(0.8, 0, 0), m=(0.4, 0, 0), b=(0.0, 0, 0))
later(10 * MIN)
e_now = emo.read()["v"]
emo.write(-0.5, 0, 0, cause="test", door="code", layer="mood")
rows = stored()[0]
ok(near(rows["emotion"][0], e_now) and rows["emotion"][3] == rows["mood"][3] == rt.iso(CLOCK["t"]),
   "a mood change first stores emotion at its decayed value, so emotion keeps the path it was on")
later(10 * MIN)
ok(near(emo.read()["v"], integrate(e_now, -0.5, 0.0, 10 * MIN)[0], 1e-7), "then emotion heads for the new mood")
reset(e=(0.8, 0, 0), m=(0.4, 0, 0), b=(0.0, 0, 0))
later(HOUR)
cur = emo.read_all()
emo.write(0.2, 0, 0, cause="test", door="code", layer="baseline")
rows = stored()[0]
ok(near(rows["emotion"][0], cur["emotion"]["v"]) and near(rows["mood"][0], cur["mood"]["v"]),
   "a baseline change first stores emotion and mood at their decayed values")

print("9. edges")
reset(e=(0.8, 0, 0))
later(-HOUR)
ok(emo.read()["v"] == 0.8, "clock set back an hour: no decay, and no un-decay")
later(HOUR)
reset(e=(0.8, 0, 0))
later(10 ** 9)
r = emo.read()
ok(abs(r["v"]) < 1e-12 and math.isfinite(r["v"]), "clock jumps ~30 years ahead: settles at baseline, no overflow")
random.seed(7)
inside = True
for _ in range(500):
    reset(e=tuple(random.uniform(-1, 1) for _ in "vad"), m=tuple(random.choice((-1, 1)) for _ in "vad"),
          b=tuple(random.choice((-1, 1)) for _ in "vad"))
    later(random.choice((1, 37, 600, 5000, 40000)))
    inside &= all(-1 <= x[k] <= 1 for x in emo.read_all().values() for k in "vad")
ok(inside, "500 random states pushed to the corners stay inside -1..+1")
reset(e=(0.8, 0, 0))
row_time = stored()[0]["emotion"][3]
later(10 * MIN)
ok(emo.read()["updated"] == row_time, "updated still means when the layer last changed, not when it was read")

print("10. tuning")
reset(e=(0.8, 0, 0))
later(10 * MIN)
emo.EMOTION_HALF_LIFE_S = 5 * MIN
ok(near(emo.read()["v"], 0.2, 1e-3), "half-life changed to 5 minutes: takes effect on the next read (0.8 -> 0.2 in 10 min)")
emo.EMOTION_HALF_LIFE_S = float("inf")
ok(emo.read()["v"] == 0.8, "infinite half-life turns emotion decay off")
emo.EMOTION_HALF_LIFE_S = emo.MOOD_HALF_LIFE_S = 30 * MIN
reset(e=(0.9, 0, 0), m=(0.5, 0, 0), b=(0, 0, 0))
later(45 * MIN)
ke = math.log(2) / (30 * MIN)
ok(near(emo.read()["v"], 0.9 * math.exp(-ke * 2700) + 0.5 * ke * 2700 * math.exp(-ke * 2700), 1e-12),
   "equal half-lives use the limit formula (no divide by zero)")
for bad in (0, -5, float("nan"), True, "10m"):
    emo.EMOTION_HALF_LIFE_S = bad
    try:
        emo.read()
        refused = False
    except emo.EmotionError as e:
        refused = e.code == "bad_config"
    ok(refused, "half-life %r is refused as bad_config" % (bad,))
emo.EMOTION_HALF_LIFE_S, emo.MOOD_HALF_LIFE_S = EH, MH

print("11. what the label sees")
reset(e=emo.REFERENCE_POINTS["anger"])
lab = emo.label(*(emo.read()[k] for k in "vad"))
ok((lab["name"], lab["intensity"]) == ("anger", "moderate"), "anger's coordinates read moderate anger at first")
later(10 * MIN)
lab = emo.label(*(emo.read()[k] for k in "vad"))
ok((lab["name"], lab["intensity"]) == ("anger", "mild"), "10 minutes later: mild anger (halfway to a neutral mood)")
later(30 * MIN)
ok(emo.label(*(emo.read()[k] for k in "vad"))["name"] == "neutral", "40 minutes later: neutral")

print("\nPASS")
