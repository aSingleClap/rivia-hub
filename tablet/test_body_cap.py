"""Step 4.6: caps on how far her body (server health) can move her mood. Jordan's rule (Nov 21): mood mostly tied
to how she is running; persistent trouble nudges her toward annoyed, never takes away a laugh.
  1. target(): only annoyed or neutral when running badly; never past the caps; running well barely shows
  1b. with 4.5's worried label: a big confidence drop reads worried, still never fear, sad or angry
  2. step_toward(): grows slowly, eases faster, never overshoots
  3. tick(): writes mood only, door code, cause body; dry run writes nothing
  4. a week of the worst dials: the body's share, mood and emotion never pass the cap or read past faint annoyance
  5. a blip doesn't show; a long quiet spell then a spike still climbs slowly; recovery
  6. other mood movers (Step 3 coupling, Jordan's tests) are not counted against the body, and a hand test pauses it
  7. humor headroom: from the full pull, one capped joy turn still lands pleasant
  8. a measured (non-zero) baseline: the cap and direction hold from her normal; the word can differ
  9. with the 4.4 and 4.5 drafts
  10. a week of the worst dials with worried on; irritation() for the style rules
The clock is faked by replacing rivia_time.now_s. Throwaway RIVIA_DIR; never touches ~/rivia.
    python test_body_cap.py
"""
import math, os, random, sqlite3, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ["RIVIA_DIR"] = os.path.join(TMP, "rivia")
sys.path.insert(0, HERE)

import rivia_time as rt  # noqa: E402
CLOCK = {"t": 1_800_000_000}
rt.now_s = lambda: CLOCK["t"]

import rivia_store as store  # noqa: E402
import rivia_emotion as emo  # noqa: E402
import rivia_body_cap as body  # noqa: E402

MIN, HOUR, DAY = 60, 3600, 86400
NEGATIVE = {"annoyance", "anger", "distressed", "fear", "sadness"}
WORST = ((-1.0, 1.0, 0.0), (0.0, 0.0, -1.0))   # stress and confidence both as bad as a dial can say
STRESS = ((-0.10, 0.30, 0.0),)                  # a plausible stress pull: arousal up, valence a little down
HEALTHY = ((0.0, 0.0, 0.0),)
EPS = 1e-9
N = {"ok": 0}


def ok(cond, label):
    assert cond, label
    N["ok"] += 1
    print("  ok ·", label)


def refused(code, fn):
    try:
        fn()
    except body.BodyCapError as e:
        return e.code == code
    return False


def mag(p):
    return math.hypot(*p)


def vad(s):
    return (s["v"], s["a"], s["d"])


def fresh():
    path = os.path.join(store.rivia_dir(), "emotion.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    return emo.db_open()


def advance(seconds):
    CLOCK["t"] += int(seconds)


def offset(con, layer):
    now = emo.read_all(con)
    return tuple(x - y for x, y in zip(vad(now[layer]), vad(now["baseline"])))


def lab(con, layer="emotion"):
    now = emo.read_all(con)
    return emo.label(*vad(now[layer]), rest=vad(now["baseline"]))


def body_rows(con):
    return con.execute("select layer, door, cause from change_log where cause=?", (body.BODY_CAUSE,)).fetchall()


def run(con, pulls, seconds, every=5 * MIN, watch=None):
    """Tick every `every` seconds for `seconds`, writing; watch(con) is called after each tick."""
    for _ in range(int(seconds // every)):
        advance(every)
        body.tick(*pulls, con=con, write=True)
        if watch:
            watch(con)


WORRIED_POINT = (-0.49, 0.21, -0.28)   # 4.5's worried_label.patch (Jordan, Nov 22: "Yes add worried")


def worried_on(on):
    for d in (emo.HAND_PICKED, emo.REFERENCE_POINTS):
        if on:
            d["worried"] = WORRIED_POINT
        else:
            d.pop("worried", None)


# 4.9 merge: worried is now in the installed label list (worried_label.patch). Parts 1 and 1b were written
# for the list without it, so they take it off here and test the fallback first; 1b puts it back.
worried_on(False)

print("1. target()")
ok(body.target() == (0.0, 0.0, 0.0) and body.target(*HEALTHY) == (0.0, 0.0, 0.0), "no pull, no move")
ok(body.target((0.0, 0.03, 0.0)) == (0.0, 0.0, 0.0), "a pull under the deadband (0.05) counts as nothing")
t = body.target(*STRESS)
ok(emo.label(*t)["name"] in ("annoyance", "neutral") and t[0] < 0 and t[1] > 0,
   "stress points at annoyed: valence down, arousal up (%+.3f %+.3f %+.3f)" % t)
t = body.target((0.0, 0.0, -0.4))
ok(emo.label(*t)["name"] == "annoyance" and t[2] > 0,
   "low confidence alone reads annoyed, not afraid (dominance is not dragged down in mood)")
ok(emo.label(0.0, 0.2, -0.2)["name"] == "distressed",
   "(why: the same pull taken as-is, arousal up and dominance down, would read fear)")
t = body.target(*WORST)
ok(abs(mag(t) - body.BODY_MAX_PULL) < EPS, "the worst dials give exactly the cap, %.2f" % body.BODY_MAX_PULL)
ok(emo.label(*t)["name"] == "annoyance" and emo.label(*t)["intensity"] == "faint",
   "and the cap alone reads faint annoyance, never mild")
rng = random.Random(46)
labels, worst_good, over = set(), 0.0, 0
for _ in range(20000):
    pulls = [tuple(rng.uniform(-1, 1) for _ in range(3)) for _ in range(rng.choice((1, 2, 3)))]
    t = body.target(*pulls)
    m = mag(t)
    over += m > body.BODY_MAX_PULL + EPS
    if t[0] > 0 or (t[0] == 0 and t[2] > 0):  # the running-well side
        worst_good = max(worst_good, m)
    labels.add(emo.label(*t)["name"])
ok(over == 0, "20,000 random dial mixes: none passes the cap")
ok(labels <= {"neutral", "annoyance"}, "and every one reads annoyance or neutral (saw %s)" % sorted(labels))
ok(worst_good <= body.BODY_MAX_GOOD + EPS, "running well never pulls more than %.2f" % body.BODY_MAX_GOOD)
t = body.target((0.5, -0.5, 0.5))
ok(emo.label(*t)["name"] == "neutral", "so a healthy server reads as her normal, not cheerful")
ok(body.target((-0.3, 0.3, 0.0), (0.3, -0.3, 0.0)) == (0.0, 0.0, 0.0), "equal good and bad cancel out")
ok(refused("bad_value", lambda: body.target((float("nan"), 0, 0))), "NaN refused")
ok(refused("bad_value", lambda: body.target((0, 0))), "a pull that isn't three numbers refused")
ok(refused("bad_value", lambda: body.target((True, 0, 0))), "a bool refused")

print("1b. with 4.5's worried label")
ok(not body.worry_known(), "without the label patch, worried is off (so a confidence drop can't read fear)")
worried_on(True)
ok(body.worry_known(), "with it, worried is on")
SLOW = (-0.20, 0.12, 0.0)        # 4.4-style slowness pull, along annoyed
for dd, want in ((0.0, "annoyance"), (-0.05, "annoyance"), (-0.10, "worried"), (-0.19, "worried")):
    t = body.target(SLOW, (0.0, 0.0, dd))
    l = emo.label(*t)
    ok(l["name"] == want and mag(t) <= body.BODY_MAX_PULL + EPS,
       "slow plus confidence pull %+.2f: %s %s" % (dd, l["intensity"], l["name"]))
t = body.target((0.0, 0.0, -0.19))
ok(emo.label(*t)["name"] == "worried", "things breaking but not slow: %s %s" % (emo.label(*t)["intensity"],
                                                                             emo.label(*t)["name"]))
t = body.target(*WORST)
ok(abs(mag(t) - body.BODY_MAX_PULL) < EPS and emo.label(*t)["intensity"] == "faint",
   "the worst dials still give exactly the cap, faint (%s)" % emo.label(*t)["name"])
labels, over = set(), 0
for _ in range(20000):
    pulls = [tuple(rng.uniform(-1, 1) for _ in range(3)) for _ in range(rng.choice((1, 2, 3)))]
    t = body.target(*pulls)
    over += mag(t) > body.BODY_MAX_PULL + EPS
    labels.add(emo.label(*t)["name"])
ok(over == 0 and labels <= {"neutral", "annoyance", "worried"},
   "20,000 random dial mixes: none past the cap, all annoyance, worried or neutral (saw %s)" % sorted(labels))
worried_on(False)

print("2. step_toward()")
cap = body.target(*WORST)
s1 = body.step_toward((0, 0, 0), cap, HOUR)
ok(abs(mag(s1) - body.BODY_WORSEN_PER_H) < EPS, "growing: at most %.2f an hour" % body.BODY_WORSEN_PER_H)
s2 = body.step_toward(cap, (0, 0, 0), HOUR)
ok(abs(mag(cap) - mag(s2) - body.BODY_EASE_PER_H) < 1e-6 or mag(s2) == 0,
   "easing: up to %.2f an hour, faster than growing" % body.BODY_EASE_PER_H)
ok(body.step_toward((0, 0, 0), cap, 10 * DAY) == cap, "never overshoots the goal")
ok(body.step_toward(cap, cap, 0) == cap, "no time, at the goal: stays")
ok(refused("bad_value", lambda: body.step_toward((0, 0, 0), cap, -1)), "negative time refused")

print("3. tick(): what it writes")
con = fresh()
r = body.tick(*WORST, con=con)
ok(r["skipped"] == "dry_run" and not r["wrote"] and body_rows(con) == [], "a dry run (the default) writes nothing")
before = {k: dict(v) for k, v in emo.read_all(con).items()}
stored = {l: con.execute("select v, a, d, updated from state where layer=?", (l,)).fetchone() for l in ("emotion", "baseline")}
r = body.tick(*WORST, con=con, write=True)
ok(r["wrote"] and r["log_id"], "write=True writes one correction")
ok(abs(mag(r["delta"]) - body.BODY_WORSEN_PER_H * body.BODY_MAX_STEP_S / HOUR) < 1e-6,
   "the first one is small: 15 minutes' worth of the worsen rate (%.3f)" % mag(r["delta"]))
ok(body_rows(con) == [("mood", "code", "body")], "its row: layer mood, door code, cause body")
ok(all(con.execute("select v, a, d, updated from state where layer=?", (l,)).fetchone() == stored[l]
       for l in ("emotion", "baseline")), "emotion's and baseline's stored rows are untouched")
r = body.tick(*WORST, con=con, write=True)
ok(r["skipped"] == "too_small" and len(body_rows(con)) == 1, "a tick the same second has nothing worth writing")

print("4. a week of the worst dials")
con = fresh()
seen = {"share": 0.0, "mood": 0.0, "emotion": 0.0, "labels": set(), "first_hour": 0.0}
start = CLOCK["t"]


def watch(con):
    sh, _ = body.share(con, rt.now_s())
    seen["share"] = max(seen["share"], mag(sh))
    seen["mood"] = max(seen["mood"], mag(offset(con, "mood")))
    seen["emotion"] = max(seen["emotion"], mag(offset(con, "emotion")))
    l = lab(con)
    seen["labels"].add("neutral" if l["name"] == "neutral" else "%s %s" % (l["intensity"], l["name"]))
    if rt.now_s() - start <= HOUR:
        seen["first_hour"] = max(seen["first_hour"], mag(offset(con, "mood")))


run(con, WORST, 7 * DAY, watch=watch)
rows = len(body_rows(con))
ok(seen["share"] <= body.BODY_MAX_PULL + 1e-6, "the body's share never passed %.2f (max %.4f)"
   % (body.BODY_MAX_PULL, seen["share"]))
ok(seen["mood"] <= body.BODY_MAX_PULL + 1e-6, "mood never sat more than the cap from baseline (max %.4f)" % seen["mood"])
ok(seen["emotion"] <= body.BODY_MAX_PULL + 1e-6, "nor did emotion, which only follows mood (max %.4f)" % seen["emotion"])
ok(seen["labels"] <= {"neutral", "faint annoyance"}, "she read only neutral or faint annoyance (%s)"
   % sorted(seen["labels"]))
ok(seen["mood"] >= body.BODY_MAX_PULL - body.BODY_MIN_WRITE - 0.01, "but she did get there: it is persistent, so it shows")
ok(seen["first_hour"] <= body.BODY_WORSEN_PER_H + 0.03, "the first hour moved her at most about %.2f (%.3f)"
   % (body.BODY_WORSEN_PER_H, seen["first_hour"]))
ok(rows <= 7 * 24 * 6, "%d body rows in a week (%.1f an hour), not one per 5-minute tick" % (rows, rows / (7 * 24)))

print("5. blips, quiet spells, recovery")
con = fresh()
peak = {"m": 0.0}
run(con, WORST, 10 * MIN)
run(con, HEALTHY, 3 * HOUR, watch=lambda c: peak.__setitem__("m", max(peak["m"], mag(offset(c, "mood")))))
ok(mag(offset(con, "mood")) < 0.01 and lab(con)["name"] == "neutral",
   "10 minutes of the worst readings, then healthy: gone, and she read neutral throughout")
con = fresh()
advance(2 * DAY)
r = body.tick(*WORST, con=con, write=True)
ok(mag(r["delta"]) <= body.BODY_WORSEN_PER_H * body.BODY_MAX_STEP_S / HOUR + 1e-9,
   "after two quiet days a sudden spike still moves her only 15 minutes' worth")
con = fresh()
run(con, WORST, 6 * HOUR)
high = mag(offset(con, "mood"))
times = []
run(con, HEALTHY, 3 * HOUR, every=5 * MIN,
    watch=lambda c: times.append(rt.now_s()) if not times and mag(offset(c, "mood")) < body.BODY_DEADBAND else None)
ok(high > 0.2 and times, "after 6 bad hours (%.2f), healthy again: back under the deadband" % high)
took = (times[0] - (CLOCK["t"] - 3 * HOUR)) / HOUR
ok(took <= 1.25, "within %.1f hours (easing runs faster than worsening)" % took)

print("6. other mood movers, and Jordan's hand test")
con = fresh()
run(con, STRESS, 4 * HOUR)
sh_before, _ = body.share(con, rt.now_s())
emo.nudge(-0.3, 0.0, 0.0, cause="appraisal coupling", door="code", con=con, layer="mood")
sh_after, _ = body.share(con, rt.now_s())
ok(math.dist(sh_before, sh_after) < EPS, "a Step 3 style mood nudge is not counted as the body's")
r = body.tick(*STRESS, con=con, write=True)
ok(mag(r["delta"]) < body.BODY_MIN_WRITE or r["skipped"] == "too_small",
   "so the body doesn't fight it back")
emo.nudge(0.0, 0.0, 0.0001, cause="manual test", door="app_button", con=con, layer="mood", tag="test")
before_test = con.execute("select count(*) from change_log where cause=?", (body.BODY_CAUSE,)).fetchone()[0]
advance(2 * HOUR)
r = body.tick(*WORST, con=con, write=True)
ok(r["skipped"] == "hand_test_open" and
   con.execute("select count(*) from change_log where cause=?", (body.BODY_CAUSE,)).fetchone()[0] == before_test,
   "while Jordan's mood test is open the body writes nothing")
emo.end_test("mood", door="app_button", con=con)
advance(5 * MIN)
r = body.tick(*WORST, con=con, write=True)
ok(r["skipped"] != "hand_test_open", "after end_test it carries on")

print("7. humor headroom (Jordan: irritable, still able to laugh)")
con = fresh()
run(con, WORST, 2 * DAY)
j = emo.RM1977["joy"]
turn = tuple(x * 0.3 / max(abs(y) for y in j) for x in j)   # a joy-direction turn at Step 3's 0.3/axis cap
emo.nudge(*turn, cause="test joke", door="code", con=con)
now = emo.read_all(con)
l = lab(con)
dv = now["emotion"]["v"] - now["baseline"]["v"]
ok(dv > 0.05, "one capped joy turn from the full pull lands pleasant (valence %+.3f over her baseline)" % dv)
ok(l["name"] not in NEGATIVE, "and reads %s %s: not annoyed, angry, sad or afraid" % (l["intensity"], l["name"]))
emo.nudge(*turn, cause="test joke", door="code", con=con)
l2 = lab(con)
ok(l2["name"] == "joy", "a second turn reads %s joy" % l2["intensity"])
ok(l["name"] == "focused",
   "the first turn reads focused: dry humor while irritated, as Jordan wants it (Nov 22)")

print("8. a measured baseline")
con = fresh()
emo.set_anchor(0.15, -0.05, 0.10, cause="test measured baseline", door="code", con=con)
run(con, WORST, 2 * DAY)
off = offset(con, "mood")
l = lab(con)
ok(mag(off) <= body.BODY_MAX_PULL + 1e-6, "the cap is measured from her baseline, not from 0 (%.3f)" % mag(off))
lo = emo.label(*off)
ok(lo["name"] == "annoyance" and lo["intensity"] == "faint",
   "the move from her normal is still faint annoyance (%s %s)" % (lo["intensity"], lo["name"]))
ok(True, "(known gap, see README: the dial names the point itself, so with this baseline she reads %s %s;"
   " the word depends on the baseline B1 measures)" % (l["intensity"], l["name"]))
b = emo.read_all(con)["baseline"]
ok(abs(b["v"] - 0.15) < EPS and abs(b["d"] - 0.10) < EPS, "the body never moved her baseline")

print("9. with the 4.4 and 4.5 drafts (skipped if their folders aren't beside this one)")
import importlib.util  # noqa: E402


def _sibling(folder, name):
    path = os.path.join(HERE, "..", folder, name + ".py")
    if not os.path.exists(path):
        path = os.path.join(TMP, "..", folder, name + ".py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


stress_mod, conf_mod = _sibling("4.4", "rivia_stress"), _sibling("4.5", "rivia_confidence")
if stress_mod and conf_mod:
    for s_lvl, dd in ((1.0, -conf_mod.MAX_DOMINANCE_PULL), (0.5, -0.15), (0.0, -conf_mod.MAX_DOMINANCE_PULL)):
        t = body.target(stress_mod.pull(s_lvl), {"dv": 0.0, "da": 0.0, "dd": dd})
        l = emo.label(*t)
        ok(mag(t) <= body.BODY_MAX_PULL + EPS and l["name"] in ("annoyance", "neutral"),
           "stress %.1f with confidence pull %+.2f, no worried label: %s %s, not fear"
           % (s_lvl, dd, l["intensity"], l["name"]))
    worried_on(True)
    for s_lvl, dd in ((1.0, -0.19), (0.3, -0.19), (1.0, 0.0)):
        t = body.target(stress_mod.pull(s_lvl), {"dv": 0.0, "da": 0.0, "dd": dd})
        l = emo.label(*t)
        ok(l["name"] in ("annoyance", "worried", "neutral"),
           "with worried on, stress %.1f and confidence pull %+.2f: %s %s" % (s_lvl, dd, l["intensity"], l["name"]))
    worried_on(False)
    raw = tuple(x + y for x, y in zip(stress_mod.pull(1.0), (0, 0, -conf_mod.MAX_DOMINANCE_PULL)))
    ok(emo.label(*raw)["name"] != "annoyance",
       "(why: their pulls added as-is read %s at %.2f out)" % (emo.label(*raw)["name"], mag(raw)))
else:
    print("  skip · 4.4 or 4.5 draft not found")
ok(refused("bad_value", lambda: body.target({"dx": 1})), "a pull dict with an unknown key refused")

print("10. a week of the worst dials with worried on, and irritation()")
worried_on(True)
con = fresh()
ok(body.irritation(con) == {"level": 0.0, "kind": None}, "irritation() at rest: 0, nothing")
run(con, STRESS, 3 * HOUR)
irr = body.irritation(con)
ok(irr["kind"] == "annoyed" and 0.5 < irr["level"] <= 1.0,
   "after 3 slow hours: annoyed, level %.2f (drier humor the higher it is)" % irr["level"])
seen10 = {"mood": 0.0, "labels": set()}


def watch10(con):
    seen10["mood"] = max(seen10["mood"], mag(offset(con, "mood")))
    l = lab(con)
    seen10["labels"].add("neutral" if l["name"] == "neutral" else "%s %s" % (l["intensity"], l["name"]))


run(con, WORST, 7 * DAY, every=15 * MIN, watch=watch10)
ok(seen10["mood"] <= body.BODY_MAX_PULL + 1e-6, "mood never more than the cap from baseline (max %.4f)" % seen10["mood"])
ok(seen10["labels"] <= {"neutral", "faint annoyance", "faint worried"},
   "she read only %s" % sorted(seen10["labels"]))
irr = body.irritation(con)
ok(irr["kind"] == "worried" and irr["level"] > 0.9, "with things breaking too: worried, level %.2f" % irr["level"])
worried_on(False)

print("\n%d checks passed" % N["ok"])
