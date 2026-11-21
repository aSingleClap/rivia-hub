"""Step 1 acceptance tests (Step 1.8), from the spec's Step 1 item 8:
    "Set her to anger's exact coordinates and check it reads angry. Push a value past +1 and check it clamps.
     Set her near 0 and check it reads neutral."
Each is run end to end: set the state through the real write path (write() or nudge()), read it back, label it,
and check the change log agrees. Both label measures are checked by flipping the module
setting LABEL_BY itself (not only the by= argument), since that setting is what the log uses.
The injection line is left to test_inject.py, which also checks no other file imports it.

Already covered elsewhere, so not repeated here:
  anger   test_emotion.py s5 "anger is R&M's angry, exactly"; s6 "every reference point at its exact coordinates
          reads its own name (by direction / by distance)" (label() on raw points); s6 "spec test: state set to
          anger's exact coordinates reads moderate anger" (default measure only); test_changelog.py s2 "anger's
          coordinates log as moderate anger"; test_inject.py s1 "half-strength anger: 'mildly angry ...'".
  clamp   test_emotion.py s3b (write past +1 and below -1 on v and a stores the bound); test_changelog.py s4
          (write past the edges flags v, a with raw kept; nudge past +1 flags v; exactly on an edge is not clamped);
          test_emotion_app.py "a nudge past -1 stops at -1 and says so" (app button over HTTP; needs rivia-hub files).
  neutral test_emotion.py s6 "spec test: state near 0 reads neutral" (default measure), "exactly 0 reads neutral",
          "neutral ends at NEUTRAL_BELOW" (one axis); test_inject.py s2 (neutral gives the empty line).

Throwaway RIVIA_DIR; never touches ~/rivia.
    python test_acceptance.py
"""
import math, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ["RIVIA_DIR"] = os.path.join(TMP, "rivia")
sys.path.insert(0, HERE)

import rivia_emotion as emo  # noqa: E402  (after RIVIA_DIR is set)

pts = emo.REFERENCE_POINTS
ANGER = pts["anger"]
MODES = ("direction", "distance")
DEFAULT_MODE = emo.LABEL_BY


def ok(cond, label):
    assert cond, label
    print("  ok ·", label)


def state():
    s = emo.read()
    return s["v"], s["a"], s["d"]


def last():
    return emo.history(1)[0]


def under(mode, fn):
    """Run fn with the module setting LABEL_BY flipped to mode, then put it back."""
    emo.LABEL_BY = mode
    try:
        return fn()
    finally:
        emo.LABEL_BY = DEFAULT_MODE


def sphere(r, n=200):
    """n points spread evenly on a sphere of radius r around 0 (Fibonacci lattice)."""
    g = math.pi * (3 - math.sqrt(5))
    for i in range(n):
        z = 1 - 2 * (i + 0.5) / n
        k = math.sqrt(1 - z * z)
        yield (r * k * math.cos(g * i), r * k * math.sin(g * i), r * z)


print("1. anger's exact coordinates read angry (spec test 1)")
for mode in MODES:
    def run():
        emo.write(0, 0, 0, cause="acceptance reset", door="code")
        r = emo.write(*ANGER, cause="acceptance: anger", door="code")
        s = state()
        lab = emo.label(*s)  # no by=: uses the setting
        return r, s, lab, last()
    r, s, lab, row = under(mode, run)
    ok(s == ANGER and lab["name"] == "anger" and lab["gap"] < 1e-9,
       "LABEL_BY=%s: stored exactly as written, reads anger, gap 0" % mode)
    ok(row["new_label"] == "%s anger" % lab["intensity"] and row["id"] == r["log_id"],
       "LABEL_BY=%s: change log says %r" % (mode, row["new_label"]))

for mode in MODES:
    def run():
        emo.write(0, 0, 0, cause="acceptance reset", door="code")
        for _ in range(3):  # three equal steps, as appraisal would arrive (3 x 0.17 = 0.51 etc.)
            emo.nudge(*(x / 3 for x in ANGER), cause="acceptance: anger by nudges", door="code")
        return state(), emo.label(*state())
    s, lab = under(mode, run)
    ok(lab["name"] == "anger" and math.dist(s, ANGER) < 1e-9 and last()["clamped"] == [],
       "LABEL_BY=%s: reached by three nudges instead of one write, still anger (float drift %.1e)"
       % (mode, math.dist(s, ANGER)))

for mode in MODES:
    near = [tuple(a + d for a, d in zip(ANGER, (dv, da, dd)))
            for dv in (-0.02, 0, 0.02) for da in (-0.02, 0, 0.02) for dd in (-0.02, 0, 0.02)]
    names = {emo.label(*p, by=mode)["name"] for p in near}
    ranked = sorted((math.dist(*(map(emo._unit, (ANGER, q))) if mode == "direction" else (ANGER, q)), n)
                    for n, q in pts.items() if n != "anger")
    ok(names == {"anger"},
       "LABEL_BY=%s: every point within 0.02 per axis of anger reads anger, so the test is not on a knife edge "
       "(nearest rival %s at %.2f)" % (mode, ranked[0][1], ranked[0][0]))

print("2. a value past +1 or below -1 clamps, and the log flags it (spec test 2)")
emo.write(0, 0, 0, cause="acceptance reset", door="code")
r = emo.write(0.2, 0.3, 1.4, cause="acceptance: d past +1", door="code")
row = last()
ok(state() == (0.2, 0.3, 1.0) and r["clamped"] == row["clamped"] == ["d"] and row["raw_d"] == 1.4,
   "d past +1: stored 1, raw 1.4 kept, flagged d (the other files test v and a only)")
emo.write(0.2, 0.3, -1.4, cause="acceptance: d below -1", door="code")
ok(state()[2] == -1.0 and last()["clamped"] == ["d"] and last()["raw_d"] == -1.4, "d below -1: stored -1, flagged d")
emo.write(-0.9, 0, 0, cause="acceptance reset", door="code")
r = emo.nudge(-0.3, 0, 0, cause="acceptance: push below -1", door="code")
row = last()
ok(state()[0] == -1.0 and abs(row["raw_v"] - (-1.2)) < 1e-12 and row["clamped"] == ["v"] == r["clamped"],
   "nudge below -1 through the core path: stored -1, raw -1.2 kept, flagged v")
emo.write(2, -2, 2, cause="acceptance: every axis past", door="code")
row = last()
ok(state() == (1.0, -1.0, 1.0) and row["clamped"] == ["v", "a", "d"] and (row["raw_v"], row["raw_a"], row["raw_d"]) == (2, -2, 2),
   "all three axes past at once: stored at the corner, flagged v, a, d, raws kept")
for mode in MODES:
    ok(under(mode, lambda: emo.label(2, -2, 2)) == under(mode, lambda: emo.label(1, -1, 1)),
       "LABEL_BY=%s: a clamped state labels the same as the bound it was held to" % mode)
ok(row["new_label"] == "%s %s" % (emo.label(1, -1, 1)["intensity"], emo.label(1, -1, 1)["name"]),
   "the log's new label is the clamped state's label (%r)" % row["new_label"])

emo.write(0.9, 0, 0, cause="acceptance reset", door="code")
n0 = len(emo.history(10 ** 6))
for _ in range(5):
    emo.nudge(0.3, 0, 0, cause="acceptance: keeps overshooting", door="code")
rows = emo.history(5)
ok(len(emo.history(10 ** 6)) == n0 + 5 and all(r["clamped"] == ["v"] for r in rows[:4])
   and all(abs(r["raw_v"] - 1.3) < 1e-12 for r in rows[:4]),
   "five pushes at the edge: five rows, each later one flagged v with raw 1.3, so a stuck overshoot is visible")
emo.nudge(-0.1, 0, 0, cause="acceptance: back off", door="code")
ok(abs(state()[0] - 0.9) < 1e-12 and last()["clamped"] == [],
   "a nudge back from the edge starts at the stored 1, not the raw value (no hidden overshoot builds up)")

try:
    emo.write(1e308, 0, 0, cause="acceptance: huge but finite", door="code")
    huge_ok = state()[0] == 1.0 and last()["clamped"] == ["v"] and last()["raw_v"] == 1e308
except emo.EmotionError:
    huge_ok = False
ok(huge_ok, "a huge finite value (1e308) clamps to 1 like any other overshoot, raw kept in the log")
try:
    emo.nudge(-1.7e308, 0, 0, cause="acceptance: huge nudge", door="code")
    huge_ok = state()[0] == -1.0 and last()["clamped"] == ["v"]
except emo.EmotionError:
    huge_ok = False
ok(huge_ok, "a huge finite nudge (-1.7e308) clamps to -1 (old is held in -1..+1, so the sum never overflows)")

print("3. near 0 reads neutral (spec test 3)")
for mode in MODES:
    def run():
        emo.write(-0.06, 0.05, -0.04, cause="acceptance: near 0", door="code")
        return emo.label(*state()), last()
    lab, row = under(mode, run)
    ok(lab["name"] == lab["intensity"] == "neutral" and lab["gap"] is None and row["new_label"] == "neutral",
       "LABEL_BY=%s: set near 0 on the negative side, reads neutral, logs neutral" % mode)
for mode in MODES:
    inside = {emo.label(*p, by=mode)["name"] for p in sphere(emo.NEUTRAL_BELOW - 0.001)}
    outside = {emo.label(*p, by=mode)["name"] for p in sphere(emo.NEUTRAL_BELOW + 0.001)}
    ok(inside == {"neutral"} and "neutral" not in outside,
       "LABEL_BY=%s: 200 directions at %.3f all read neutral; at %.3f none do (%d names)"
       % (mode, emo.NEUTRAL_BELOW - 0.001, emo.NEUTRAL_BELOW + 0.001, len(outside)))
ok(emo.label(0.1, 0.1, 0.1)["name"] != "neutral",
   "neutral is distance from 0, not per axis: 0.1 on every axis (0.17 out) is not neutral")
for mode in MODES:
    def run():
        emo.write(*ANGER, cause="acceptance: angry", door="code")
        for _ in range(9):  # fade toward 0 in ninths, the way Step 2's decay will move her
            emo.nudge(*(-x / 9 for x in ANGER), cause="acceptance: fading", door="code")
        return state(), emo.label(*state())
    s, lab = under(mode, run)
    ok(lab["name"] == "neutral" and math.hypot(*s) < 1e-9,
       "LABEL_BY=%s: anger faded back to 0 by nudges reads neutral" % mode)
words = []
for k in range(9, 0, -1):  # anger at 9/9 .. 1/9 strength
    words.append(emo.label(*(k / 9 * x for x in ANGER))["name"])
ok(words[-1] == "neutral" and set(words[:-1]) == {"anger"},
   "on the way down (default measure) she reads anger at every step until neutral; never another emotion")

ok(emo.LABEL_BY == DEFAULT_MODE == "direction", "LABEL_BY put back to its default (direction)")
print("PASS")
