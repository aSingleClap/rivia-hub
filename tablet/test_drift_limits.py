"""Rivia's baseline drift limits (emotion spec, Step 2.7). The baseline (personality) is held to a fixed anchor
(0, 0, 0 until set_anchor() puts the measured baseline there), may sit at most BASELINE_MAX_FROM_ANCHOR from it,
and may move at most BASELINE_MAX_PER_DAY in any rolling day and BASELINE_MAX_PER_WEEK in any rolling week. Every
baseline write goes through _apply(); a move past a limit is cut short and logged with the limits it hit.
There is no drift logic to test: nothing produces a drift signal until Step 3, so a steady push stands in for it.
Throwaway RIVIA_DIR and a fake clock; never touches ~/rivia.
    python test_drift_limits.py
"""
import importlib.util, math, os, re, sqlite3, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ["RIVIA_DIR"] = os.path.join(TMP, "rivia")
sys.path.insert(0, HERE)

import rivia_store as store  # noqa: E402  (after RIVIA_DIR is set)
import rivia_emotion as emo  # noqa: E402

CLOCK = [1_800_000_000.0]
emo.rt.now_s = lambda: CLOCK[0]
DAY, WEEK = emo.DAY_S, emo.WEEK_S
EPS = 1e-9
R, PER_DAY, PER_WEEK = emo.BASELINE_MAX_FROM_ANCHOR, emo.BASELINE_MAX_PER_DAY, emo.BASELINE_MAX_PER_WEEK


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


def tick(seconds):
    CLOCK[0] += seconds


def db_path():
    return os.path.join(store.rivia_dir(), "emotion.db")


def sql(q, *args):
    con = sqlite3.connect(db_path())
    try:
        return con.execute(q, args).fetchall()
    finally:
        con.close()


def log_count():
    return sql("select count(*) from change_log")[0][0]


# The test keeps its own record of where the baseline was and when, independent of baseline_track, and checks
# every limit against it after each write.
TRAIL = []        # (time, (v, a, d))
ANCHOR = [((0.0, 0.0, 0.0), None)]  # (point, set_at)


def record():
    TRAIL.append((CLOCK[0], vad(emo.read(layer="baseline"))))


def pos_at(t):
    before = [p for at, p in TRAIL if at <= t]
    return before[-1] if before else TRAIL[0][1]


def within_limits():
    t, p = TRAIL[-1]
    anchor, set_at = ANCHOR[-1]
    start = set_at if set_at is not None else TRAIL[0][0]
    return (math.dist(p, anchor) <= R + EPS
            and math.dist(p, pos_at(max(t - DAY, start))) <= PER_DAY + EPS
            and math.dist(p, pos_at(max(t - WEEK, start))) <= PER_WEEK + EPS)


print("1. a fresh emotion.db")
ok(emo.LIMITS == ("anchor", "day", "week"), "three limits: anchor distance, per day, per week")
ok(0 < PER_DAY < PER_WEEK < R, "starting picks: per day < per week < distance from anchor (%.2f, %.2f, %.2f)"
   % (PER_DAY, PER_WEEK, R))
a = emo.read_anchor()
ok((a["v"], a["a"], a["d"]) == (0, 0, 0) and a["set_at"] == CLOCK[0] and a["cause"].startswith("default"),
   "the anchor starts at 0, 0, 0 (no measured baseline yet)")
ok("limited" in [r[1] for r in sql("pragma table_info(change_log)")], "change_log has the limited column")
ok(log_count() == 0, "opening writes no change_log row")
room = emo.baseline_room()
ok(all(room[k]["used"] == 0 for k in emo.LIMITS) and room["anchor"]["max"] == R, "baseline_room: nothing used yet")
record()

print("2. one big push is cut to a day's worth and logged")
tick(60)
r = emo.write(0.1, 0, 0, cause="drift stand-in", door="code", layer="baseline")
record()
ok(abs(r["new"]["v"] - PER_DAY) < 1e-6 and r["new"]["a"] == 0 and r["new"]["d"] == 0,
   "asked for v 0.1, stored v %.4f (the day's cap), along the same line" % r["new"]["v"])
ok(r["limited"] == ["day", "week"] and r["clamped"] == [], "result names the limits the move would have crossed")
h = emo.history(1)[0]
ok(h["layer"] == "baseline" and h["limited"] == ["day", "week"] and h["raw_v"] == 0.1 and h["new_v"] == r["new"]["v"],
   "change_log row: what was asked in raw, what was stored in new, limits in limited")
ok(within_limits(), "every limit holds")

print("3. the rest of that day")
tick(3600)
n = log_count()
r = emo.nudge(dv=0.01, cause="drift stand-in", door="code", layer="baseline")
record()
ok(abs(r["new"]["v"] - r["old"]["v"]) < 1e-6 and r["limited"] == ["day"], "a further push the same day barely moves")
ok(log_count() == n + 1 and emo.history(1)[0]["limited"] == ["day"], "and is still logged as a day-limit hit")
r = emo.nudge(dv=-0.005, cause="easing back", door="code", layer="baseline")
record()
ok(r["limited"] == [] and abs(r["new"]["v"] - (r["old"]["v"] - 0.005)) < 1e-12, "moving back toward the start is free")
ok(within_limits(), "every limit holds")

print("4. a steady push for 60 days (one nudge an hour, all in one direction)")
hits = set()
v_at = {}
start = math.dist(TRAIL[-1][1], (0, 0, 0))
for hour in range(60 * 24):
    tick(3600)
    r = emo.nudge(dv=0.002, da=0.001, cause="drift stand-in", door="code", layer="baseline")
    hits.update(r["limited"])
    record()
    assert within_limits(), "limit broken at hour %d: %r" % (hour, TRAIL[-1])
    if hour % 24 == 23:
        v_at[hour // 24 + 1] = math.dist(TRAIL[-1][1], (0, 0, 0))
ok(True, "every limit held after each of 1,440 writes")
ok(v_at[7] - start <= PER_WEEK + EPS, "a week of pushing moved her %.3f further out (week cap %.2f)"
   % (v_at[7] - start, PER_WEEK))
ok(v_at[21] < R - EPS, "after three weeks still short of the edge (%.3f of %.2f)" % (v_at[21], R))
ok(abs(v_at[30] - R) < 1e-6 and abs(v_at[60] - R) < 1e-6, "by day 30 at the edge, and still exactly there at day 60")
ok(hits == {"anchor", "day", "week"} and emo.history(1)[0]["limited"] == ["anchor"],
   "all three limits were hit; at the edge only the anchor limit binds")
ok(abs(emo.baseline_room()["anchor"]["used"] - R) < 1e-6, "baseline_room shows the distance used up")
ok(sql("select count(*) from baseline_track")[0][0] <= 7 * 24 + 3,
   "baseline_track keeps about a week (%d rows), not all 1,440" % sql("select count(*) from baseline_track")[0][0])

print("5. only the baseline is limited")
before = vad(emo.read(layer="baseline"))
r1 = emo.write(0.9, 0.9, 0.9, cause="big feeling", door="code")
r2 = emo.write(-0.8, 0.5, 0.0, cause="big mood", door="code", layer="mood")
ok(vad(r1["new"]) == (0.9, 0.9, 0.9) and r1["limited"] == [], "emotion moves anywhere at once")
ok(vad(r2["new"]) == (-0.8, 0.5, 0.0) and r2["limited"] == [], "mood moves anywhere at once")
ok(vad(emo.read(layer="baseline")) == before, "neither touched the baseline")

print("6. every door is held, and clamping still comes first")
for door in emo.WRITERS:
    tick(DAY)
    r = emo.write(5, 0, 0, cause="hard push", door=door, layer="baseline")
    record()
    ok(r["clamped"] == ["v"] and "anchor" in r["limited"] and within_limits(),
       "%s: raw v 5 clamped to 1, then held at the edge" % door)

print("7. set_anchor() moves the anchor and the baseline together")
tick(DAY)
n = log_count()
r = emo.set_anchor(0.4, 0.1, 0.2, cause="measured baseline (2.6)", door="admin")
ANCHOR.append(((0.4, 0.1, 0.2), CLOCK[0]))
record()
a = emo.read_anchor()
ok((a["v"], a["a"], a["d"]) == (0.4, 0.1, 0.2) and a["cause"] == "measured baseline (2.6)", "anchor moved")
ok(vad(emo.read(layer="baseline")) == (0.4, 0.1, 0.2) and r["limited"] == [], "baseline jumped there, past every limit")
h = emo.history(2)
ok(log_count() == n + 2 and [x["layer"] for x in h] == ["baseline", "anchor"], "two log rows: anchor, then baseline")
ok((h[1]["old_v"], h[1]["new_v"]) == (0, 0.4) and h[1]["at"] == h[0]["at"], "the anchor row shows old and new anchor")
tick(60)
r = emo.nudge(dv=0.01, cause="drift stand-in", door="code", layer="baseline")
record()
ok(r["limited"] == [] and abs(r["new"]["v"] - 0.41) < 1e-12,
   "the windows restart at the anchor: the jump does not use up today's drift")
r = emo.nudge(dv=0.05, cause="drift stand-in", door="code", layer="baseline")
record()
ok(r["limited"] == ["day", "week"] and within_limits(), "and the caps apply from the new anchor")

print("8. who may move the anchor")
before = (emo.read_anchor(), emo.read_all())
n = log_count()
ok(refused("not_allowed", lambda: emo.set_anchor(0, 0, 0, cause="x", door="app_button")), "the phone app's button may not")
ok(refused("not_allowed", lambda: emo.set_anchor(0, 0, 0, cause="x", door="app")), "Gemini's door may not")
ok(refused("not_allowed", lambda: emo._apply(lambda o: (0, 0, 0), cause="x", door="code", layer="mood", _reanchor=True)),
   "only the baseline has an anchor")
ok(refused("no_cause", lambda: emo.set_anchor(0, 0, 0, cause=" ", door="admin")), "an anchor move needs a cause")
ok(refused("bad_value", lambda: emo.set_anchor(float("nan"), 0, 0, cause="x", door="admin")), "NaN refused")
ok((emo.read_anchor(), emo.read_all()) == before and log_count() == n, "nothing changed after any refusal")

print("9. a baseline already past a limit may come back, never go further")
tick(WEEK)
emo.BASELINE_MAX_FROM_ANCHOR = 0.005  # as if the limit were made tighter after she drifted
anchor = (0.4, 0.1, 0.2)
d0 = math.dist(vad(emo.read(layer="baseline")), anchor)
r = emo.nudge(dv=0.01, cause="push out", door="code", layer="baseline")
ok(r["limited"] == ["anchor"] and math.dist(vad(r["new"]), anchor) <= d0 + EPS, "outward: held where it was")
r = emo.nudge(dv=-0.01, cause="come back", door="code", layer="baseline")
ok("anchor" not in r["limited"] and math.dist(vad(r["new"]), anchor) < d0, "inward: allowed")
emo.BASELINE_MAX_FROM_ANCHOR = R

print("10. opening an older emotion.db (2.1 draft, with a Step 1 log)")
os.remove(db_path())
STEP21 = os.environ.get("RIVIA_STEP21_DIR", os.path.join(HERE, "tests", "fixtures", "2.1"))  # 2.9 merge: a frozen copy, so it runs on the tablet too
spec = importlib.util.spec_from_file_location("rivia_emotion_21", os.path.join(STEP21, "rivia_emotion.py"))
emo21 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(emo21)
emo21.write(0.3, 0, 0, cause="manual", door="code")
emo21.write(0.05, 0, 0, cause="direct", door="code", layer="baseline")  # 2.1 lets code write baseline directly
old_rows = sql("select * from change_log order by id")
ok("limited" not in [r[1] for r in sql("pragma table_info(change_log)")], "the 2.1 DB has no limited column")
ok(emo.history(5)[0]["limited"] == [] and len(emo.history(5)) == 2, "2.7 opens it; old rows read limited []")
# 2.9 merge: 2.5's five fade columns are added too, so compare the columns the 2.1 DB had.
ok([row[:len(old_rows[0])] for row in sql("select * from change_log order by id")] == old_rows, "every old log row kept exactly")
ok(sql("select v from baseline_track") == [(0.05,)] and emo.read_anchor()["cause"].startswith("default"),
   "default anchor added; the first track row is the baseline where it stood")
emo.db_open().close()
ok(len(sql("select * from baseline_track")) == 1 and len(sql("select * from baseline_anchor")) == 1,
   "opening again adds nothing")

print("11. the limits live in the one write function")
src = open(os.path.join(HERE, "rivia_emotion.py")).read()
apply_fn = re.search(r"^def _apply\(.*?(?=^def )", src, re.M | re.S).group(0)
rest = src.replace(apply_fn, "")
ok("_limit_baseline(" in apply_fn and len(re.findall(r"_limit_baseline\(", rest)) == 1,
   "_limit_baseline is called only from _apply (and defined once)")
ok(re.search(r"update baseline_anchor", apply_fn) and not re.search(r"update baseline_anchor", rest),
   "only _apply moves the anchor (db_open only adds the default)")
ok(not re.search(r"insert into baseline_track", rest.replace(
    re.search(r"^def db_open\(.*?(?=^def )", src, re.M | re.S).group(0), "").replace(
    re.search(r"^def _track_baseline\(.*?(?=^def )", src, re.M | re.S).group(0), "")),
   "baseline_track is written only by db_open's first row and _track_baseline")

print("12. Jordan's hand changes (2.4's tags): calibration resets the anchor, a test skips the limits")
os.remove(db_path())
emo.db_open().close()
tick(DAY)
n = log_count()
r = emo.write(0.3, -0.2, 0.1, cause="manual calibration", door="app_button", layer="baseline", tag="calibration")
a = emo.read_anchor()
ok(vad(r["new"]) == (0.3, -0.2, 0.1) and r["limited"] == [] and (a["v"], a["a"], a["d"]) == (0.3, -0.2, 0.1),
   "a calibration from the app's button moves the baseline past the limits and resets the anchor there")
ok(log_count() == n + 2 and [x["layer"] for x in emo.history(2)] == ["baseline", "anchor"]
   and emo.history(1)[0]["cause"] == "manual calibration", "logged as an anchor row and a baseline row")
tick(60)
r = emo.nudge(dv=0.01, cause="manual calibration", door="app_button", layer="baseline", tag="calibration")
ok(abs(r["new"]["v"] - 0.31) < 1e-12 and abs(emo.read_anchor()["v"] - 0.31) < 1e-12, "a calibration nudge does the same")
ok(refused("bad_tag", lambda: emo.write(0, 0, 0, cause="x", door="app_button", layer="mood", tag="calibration")),
   "mood can't be calibrated")
ok(refused("not_allowed", lambda: emo.set_anchor(0, 0, 0, cause="x", door="app_button")),
   "the app's button still can't move the anchor without the calibration tag")
ok(refused("bad_tag", lambda: emo.set_anchor(0, 0, 0, cause="x", door="admin", tag="test")), "a test can't move the anchor")
ok(refused("bad_tag", lambda: emo.write(0, 0, 0, cause="x", door="code", layer="baseline", tag="live")), "unknown tag refused")
tick(60)
track_before = sql("select count(*) from baseline_track")[0][0]
before = vad(emo.read(layer="baseline"))
r = emo.write(-0.6, 0.5, -0.3, cause="manual test", door="app_button", layer="baseline", tag="test")
ok(vad(r["new"]) == (-0.6, 0.5, -0.3), "a test moves the baseline as far as asked, past every limit")
ok(r["limited"] == ["skipped", "anchor", "day", "week"] and emo.history(1)[0]["limited"] == r["limited"],
   "logged as skipped, with the limits it would have crossed")
ok(emo.read_anchor()["v"] == 0.31, "the anchor stays put")
r = emo.write(*before, cause="manual test end", door="app_button", layer="baseline", tag="test_end")
ok(vad(r["new"]) == before and r["limited"][0] == "skipped", "the test's end puts it back, also unlimited and logged")
ok(sql("select count(*) from baseline_track")[0][0] == track_before, "test rows stay out of the speed check")
tick(60)
r = emo.nudge(dv=0.01, cause="drift stand-in", door="code", layer="baseline")
ok(r["limited"] == [] and abs(r["new"]["v"] - (before[0] + 0.01)) < 1e-12,
   "after the test, ordinary drift sees no jump (its window restarted at the last calibration)")
r = emo.write(0.9, 0.9, 0.9, cause="mood test", door="app_button", layer="mood", tag="test")
ok(r["limited"] == [], "a mood test is unlimited anyway (mood has no limits)")

print("PASS")
