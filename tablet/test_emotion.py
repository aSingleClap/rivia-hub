"""Rivia's emotion state (Steps 1.1 to 1.4): emotion.db starts at 0, 0, 0, keeps its row across opens,
has one writer that refuses the app door and bad values, clamps each axis to -1..+1, and touches no other DB;
reference points are in range; label() names a state, reads neutral near 0, and steps intensity up with distance.
Throwaway RIVIA_DIR; never touches ~/rivia.
    python test_emotion.py
"""
import os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ["RIVIA_DIR"] = os.path.join(TMP, "rivia")
sys.path.insert(0, HERE)

import rivia_store as store  # noqa: E402  (after RIVIA_DIR is set)
import rivia_time as rt  # noqa: E402
import rivia_emotion as emo  # noqa: E402


def ok(cond, label):
    assert cond, label
    print("  ok ·", label)


def refused(code, fn):
    try:
        fn()
    except emo.EmotionError as e:
        return e.code == code
    return False


print("1. a fresh emotion.db")
s = emo.read()
ok((s["v"], s["a"], s["d"]) == (0, 0, 0), "starts at 0, 0, 0")
ok(abs(rt.to_epoch(s["updated"]) - rt.now_s()) < 5, "has a last-updated time")
ok(sorted(os.listdir(store.rivia_dir())) == ["emotion.db"], "creates only emotion.db, not core or scheduler")

print("2. the one writer")
r = emo.write(0.4, -0.2, 0.1, cause="test nudge", door="code")
ok((r["old"]["v"], r["new"]["v"]) == (0, 0.4), "returns old and new")
ok((emo.read()["v"], emo.read()["a"], emo.read()["d"]) == (0.4, -0.2, 0.1), "stores the new values")
emo.write(-1, 1, -1, cause="corner", door="admin")
ok(emo.read()["v"] == -1, "admin door may write, edges -1 and +1 allowed")

print("3. refusals leave the state alone")
before = emo.read()
ok(refused("not_allowed", lambda: emo.write(0, 0, 0, cause="x", door="app")), "app door refused")
ok(refused("not_allowed", lambda: emo.write(0, 0, 0, cause="x", door=None)), "no door refused")
ok(refused("no_cause", lambda: emo.write(0, 0, 0, cause="  ", door="code")), "empty cause refused")
ok(refused("bad_value", lambda: emo.write(float("nan"), 0, 0, cause="x", door="code")), "NaN refused")
ok(refused("bad_value", lambda: emo.write(float("inf"), 0, 0, cause="x", door="code")), "infinity refused, not clamped")
ok(refused("bad_value", lambda: emo.write(True, 0, 0, cause="x", door="code")), "True is not a number")
ok(refused("not_allowed", lambda: emo.write(5, 5, 5, cause="x", door="app")), "app door refused even out of range")
ok(emo.read() == before, "state unchanged after every refusal")

print("3b. clamping (1.2)")
r = emo.write(1.5, -3, 0.25, cause="overshoot", door="code")
ok((r["new"]["v"], r["new"]["a"], r["new"]["d"]) == (1.0, -1.0, 0.25), "write returns the clamped values")
ok((emo.read()["v"], emo.read()["a"], emo.read()["d"]) == (1.0, -1.0, 0.25), "past +1 stored as +1, below -1 as -1")
emo.write(-1.0000001, 7, -0.999, cause="edges", door="admin")
ok((emo.read()["v"], emo.read()["a"], emo.read()["d"]) == (-1.0, 1.0, -0.999), "just past an edge clamps, -0.999 kept as is")
emo.write(-1, 1, -1, cause="corner again", door="code")
ok((emo.read()["v"], emo.read()["a"], emo.read()["d"]) == (-1, 1, -1), "edges -1 and +1 unchanged")
before = emo.read()

print("4. reopening")
con = emo.db_open()
con.close()
ok(emo.read() == before, "a second open never resets the row")
con = emo.db_open()
ok(con.execute("select count(*) from state").fetchone()[0] == 1, "one row in Step 1")
con.close()

print("5. reference points (1.3)")
import math  # noqa: E402
pts = emo.REFERENCE_POINTS
ok(all(len(p) == 3 and all(isinstance(x, float) and math.isfinite(x) and -1 <= x <= 1 for x in p)
       for p in pts.values()), "every point is three finite numbers inside -1..+1")
ok({"joy", "anger", "fear", "sadness", "surprise", "annoyance"} <= set(emo.RM1977) and "disgust" not in pts,
   "the R&M emotions, with annoyance in place of disgust (Jordan, Nov 21)")
ok({"relief", "calm", "focused"} <= set(emo.HAND_PICKED) and not set(emo.RM1977) & set(emo.HAND_PICKED),
   "custom points kept apart from the sourced ones")
ok(len(pts) == len(emo.RM1977) + len(emo.HAND_PICKED), "no name defined twice")
ok(pts["anger"] == (-0.51, 0.59, 0.25), "anger is R&M's angry, exactly (the Step 1 test sets her there)")
gap = min(math.dist(p, q) for h, p in emo.HAND_PICKED.items() for n, q in pts.items() if n != h)
ok(gap > 0.3, "each hand-picked point is over 0.3 from every other (closest %.2f)" % gap)
# Disgust (R&M's disgusted sat only 0.29 from angry) was replaced by annoyed, which is weaker but points the same way.

print("6. label function (1.4)")
L = emo.label
for by in ("direction", "distance"):
    ok(all(L(*p, by=by)["name"] == n and L(*p, by=by)["gap"] < 1e-9 for n, p in pts.items()),
       "every reference point at its exact coordinates reads its own name (by %s)" % by)
ok(all(L(*p)["intensity"] in ("mild", "moderate", "strong") for p in pts.values()),
   "every reference point reads at least mild (weakest: annoyance, %s)" % L(*pts["annoyance"])["intensity"])
emo.write(*pts["anger"], cause="spec test", door="code")
s = emo.read()
ok((L(s["v"], s["a"], s["d"])["name"], L(s["v"], s["a"], s["d"])["intensity"]) == ("anger", "moderate"),
   "spec test: state set to anger's exact coordinates reads moderate anger")
emo.write(0.05, -0.04, 0.06, cause="spec test", door="code")
s = emo.read()
ok(L(s["v"], s["a"], s["d"]) == {"name": "neutral", "intensity": "neutral", "magnitude": L(s["v"], s["a"], s["d"])["magnitude"], "gap": None},
   "spec test: state near 0 reads neutral, not its nearest point")
ok(L(0, 0, 0)["name"] == "neutral", "exactly 0 reads neutral (no divide by zero)")
ok(L(0.14, 0, 0)["name"] == "neutral" and L(0.15, 0, 0)["name"] != "neutral", "neutral ends at NEUTRAL_BELOW")
steps = [L(*(s * x for x in pts["joy"])) for s in (0.1, 0.25, 0.5, 0.75, 1.0)]
ok([r["intensity"] for r in steps] == ["neutral", "faint", "mild", "moderate", "strong"],
   "intensity steps up as joy grows: %s" % ", ".join(r["intensity"] for r in steps))
mags = [m / 100 for m in range(0, 174)]
words = [L(m / math.sqrt(3), m / math.sqrt(3), m / math.sqrt(3))["intensity"] for m in mags]
order = ["neutral", "faint", "mild", "moderate", "strong"]
ok([order.index(w) for w in words] == sorted(order.index(w) for w in words) and words[-1] == "strong",
   "intensity never steps down as distance grows, out to the corner")
ok(L(1.5, 0, 0) == L(1, 0, 0), "out-of-range input is clamped like write()")
ok(refused("bad_value", lambda: L(float("nan"), 0, 0)), "NaN refused")
ok(refused("bad_value", lambda: L(0.5, 0, 0, by="vibes")), "unknown measure refused")

print("6b. known quirks, recorded (not fixed)")
# Faded states: plain distance lets the points nearest 0 swallow them; direction keeps the word.
half_anger = tuple(0.5 * x for x in pts["anger"])
ok(L(*half_anger)["name"] == "anger", "half-strength anger reads anger")
faded_joy = tuple(0.3 * x for x in pts["joy"])
ok(L(*faded_joy, by="distance")["name"] == "focused" and L(*faded_joy)["name"] == "joy",
   "faded joy: distance says focused, direction says joy")
# anger and annoyance point 20 degrees apart: anger losing energy turns into annoyance, which is the
# natural way down (it used to turn into disgust).
drop = lambda x: (pts["anger"][0], pts["anger"][1] - x, pts["anger"][2])
ok(L(*drop(0.25))["name"] == "anger" and L(*drop(0.3))["name"] == "annoyance",
   "anger with arousal down 0.25 is still anger; down 0.3 it reads annoyance")
# surprise is pleasant in R&M, so an unpleasant surprise has no point of its own.
bad_surprise = (-pts["surprise"][0], pts["surprise"][1], pts["surprise"][2])
ok(L(*bad_surprise)["name"] == "fear" and L(*bad_surprise, by="distance")["name"] == "fear",
   "surprise with valence flipped negative reads fear")
# Directions far from every point still get the nearest name: pure low control reads fear at 64 degrees.
ok(L(0, 0, -0.5)["name"] == "fear", "pure low dominance reads fear (nothing closer)")

print("6c. annoyance replaces disgust (Jordan, Nov 21)")
ann = pts["annoyance"]
ok((L(*ann)["name"], L(*ann)["intensity"]) == ("annoyance", "mild"), "R&M's annoyed reads mild annoyance")
ok(L(-0.60, 0.35, 0.11)["name"] == "annoyance", "where disgust used to be reads annoyance (strength from distance)")
ok(all(L(*p, by=by)["name"] != "disgust" for p in pts.values() for by in ("direction", "distance")),
   "nothing reads disgust any more")

print("PASS")
