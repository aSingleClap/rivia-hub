"""Step 4.4: the stress dial. Signals off her normal -> stress 0..1 -> a pull toward annoyance.
No clock waits: times are passed in. No database: the dial writes nothing.
The label checks use Step 2's real label() from step2/2.9/merged (found next to this file at install, or via
RIVIA_EMOTION_DIR); rivia_store and rivia_time are stubbed only if the real ones aren't importable, since
label() never touches them.
    python test_stress.py
"""
import ast, math, os, sys, types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
MERGED = os.environ.get("RIVIA_EMOTION_DIR", os.path.join(HERE, "..", "..", "step2", "2.9", "merged"))
sys.path.insert(1, os.path.abspath(MERGED))
for mod in ("rivia_store", "rivia_time"):
    try:
        __import__(mod)
    except ImportError:
        sys.modules[mod] = types.ModuleType(mod)

import rivia_stress as st  # noqa: E402
import rivia_emotion as emo  # noqa: E402

MIN, HOUR = 60, 3600
T0 = 1_800_000_000.0
COUNT = {"n": 0}


def ok(cond, label):
    assert cond, label
    COUNT["n"] += 1
    print("  ok ·", label)


def raises(fn):
    try:
        fn()
    except st.StressError:
        return True
    return False


def hold(devs, seconds, state=None, step=5 * MIN):
    """Feed the same readings every `step` seconds for `seconds`, like the collector would."""
    s = state or st.new_state(T0)
    t = s["updated"]
    s = st.advance(s, devs, t)
    end = t + seconds
    while t < end:
        t = min(end, t + step)
        s = st.advance(s, devs, t)
    return s


def name_at(stress, rest=(0.0, 0.0, 0.0)):
    dv, da, dd = st.pull(stress)
    lab = emo.label(rest[0] + dv, rest[1] + da, rest[2] + dd, rest=rest)
    return lab["name"], lab["intensity"]


print("1. One reading")
ok(st.raw_stress({})["raw"] == 0, "no readings: no stress")
ok(st.raw_stress({n: 0.0 for n in st.SIGNALS})["raw"] == 0, "every signal at her normal: no stress")
ok(st.contribution("response_time", 1.0) == 0, "inside the deadband (1 spread): nothing")
ok(st.contribution("response_time", 1.5) > 0, "just past the deadband: something")
ok(st.contribution("response_time", -4) == 0, "faster than normal: nothing, never negative")
ok(st.contribution("battery", 4) == 0 and st.contribution("battery", -4) > 0,
   "battery: only lower than normal counts")
ok(st.contribution("storage_free", -4) > 0, "storage: only less free space counts")
# 4.9 merge (Jordan, Nov 22, 7:33 PM: "The problems shouldnt hit her twice"): breakage is confidence's (4.5) only.
ok(set(st.SIGNALS) == {"response_time", "temperature", "memory_used", "storage_free", "battery"},
   "stress keeps slowness and load only: response time, temperature, memory, storage, battery")
brk = st.raw_stress({"error_rate": 9, "tool_failure_rate": 9, "failed_connections": 9, "tunnel_unreachable": 100,
                     "restarts": 9})
ok(brk["raw"] == 0 and brk["parts"] == {} and brk["ignored"] == [],
   "errors, tool failures, failed connections, the tunnel and restarts add no stress, skipped quietly")
for n, (_, share) in st.SIGNALS.items():
    ok(st.contribution(n, 1e6 if st.SIGNALS[n][0] == "up" else -1e6) <= share + 1e-12,  # shares clamp to 1
       "%s alone never passes its max share %.2f" % (n, share))
worst = {n: (100 if n in st.SHARE_SIGNALS else 50 if side == "up" else -50) for n, (side, _) in st.SIGNALS.items()}
r = st.raw_stress(worst)["raw"]
want = 1 - math.prod(1 - share for _, share in st.SIGNALS.values())
ok(abs(r - want) < 1e-6 and r < 1.0, "everything at its worst: the noisy-OR of the shares, never past 1 (%.4f)" % r)
two = st.raw_stress({"temperature": 5, "response_time": 5})["raw"]
c1, c2 = st.contribution("temperature", 5), st.contribution("response_time", 5)
ok(max(c1, c2) < two < c1 + c2, "two unrelated signals together: more than either, less than the plain sum")
r = st.raw_stress({"memory_sync_age": 9})
ok(r["raw"] == 0 and r["ignored"] == [], "memory sync is off for now: skipped quietly (Jordan, Nov 22)")
r = st.raw_stress({"response_time": 3, "cpu_load": 9, "battery": None})
ok(r["ignored"] == ["cpu_load"], "unknown signal is reported, not silently used or dropped")
ok(set(r["parts"]) == {"response_time"}, "None reads as no news")
ok(raises(lambda: st.raw_stress({"response_time": float("nan")})), "NaN refused")
ok(raises(lambda: st.raw_stress({"response_time": True})), "a bool is not a deviation")
ok(raises(lambda: st.raw_stress({"response_time": "3"})), "a string is not a deviation")

print("2. Over time")
bad = {"response_time": 6, "temperature": 6}
R = st.raw_stress(bad)["raw"]
s10 = hold(bad, 10 * MIN)
ok(abs(s10["load"] - R / 2) < 0.03, "10 minutes of trouble: load about halfway to the reading (%.3f of %.3f)"
   % (s10["load"], R))
blip = st.stress(hold(bad, 10 * MIN))
long = st.stress(hold(bad, 6 * HOUR))
ok(long > 2.5 * blip, "6 hours of the same trouble counts far more than a 10-minute blip (%.2f vs %.2f)"
   % (long, blip))
sm = {"response_time": 2.5}
Rs, steady = st.raw_stress(sm)["raw"], st.stress(hold(sm, 24 * HOUR))
ok(abs(steady - 1.5 * Rs) < 0.02, "a day of steady small trouble ends up about 1.5x its raw reading (%.2f, raw %.2f)"
   % (steady, Rs))
up = hold(bad, 40 * MIN)
peak = st.stress(up)
down = hold({}, 40 * MIN, state=up)
ok(st.stress(down) > peak / 2, "after 40 calm minutes she has let go of less than half: slower down than up")
gap_state = st.advance(hold(bad, 1 * HOUR), bad, T0 + 1 * HOUR)
held = st.stress(gap_state, T0 + 1 * HOUR + 30 * MIN)
gapped = st.stress(gap_state, T0 + 1 * HOUR + 8 * HOUR)
ok(gapped < held, "a long gap with no readings (server off) relaxes her instead of holding the last reading")
ok(st.stress(st.new_state(T0), T0 + 7 * 24 * HOUR) == 0, "a calm dial stays calm across a week of nothing")
s = hold(bad, 30 * MIN)
back = st.advance(s, bad, s["updated"] - HOUR)
ok(back["load"] == s["load"] and back["updated"] == s["updated"], "clock going backwards spends no time")
snap = dict(s)
st.advance(s, {}, s["updated"] + HOUR)
st.stress(s, s["updated"] + HOUR)
st.reading(s, s["updated"] + HOUR)
ok(s == snap, "advance, stress and reading never change the state they are given")
ok(st.stress(s, s["updated"]) == st.stress(s), "reading as of now, with no time passed, matches the stored reading")

print("3. The pull")
ok(st.pull(0) == (0.0, 0.0, 0.0), "no stress, no pull")
dv, da, dd = st.pull(1)
ok(abs(math.hypot(dv, da, dd) - st.PULL_FULL) < 1e-12, "full stress pulls PULL_FULL (%.2f)" % st.PULL_FULL)
ok(dv < 0 and da > 0 and dd == 0, "full stress: valence down, arousal up, dominance untouched (4.5's)")
ok(st.pull(5) == st.pull(1) and st.pull(-1) == st.pull(0), "stress outside 0..1 is held to it")
ok(raises(lambda: st.pull(float("inf"))), "infinite stress refused")
ok(name_at(0.3) == ("neutral", "neutral"), "light stress alone reads neutral on a neutral baseline")
ok(name_at(1.0) == ("annoyance", "mild"), "full stress alone reads mildly annoyed, no stronger")
names = {name_at(i / 100)[0] for i in range(101)}
ok(names <= {"neutral", "annoyance"}, "at every stress level it reads neutral or annoyance (got %s)"
   % sorted(names))
spec_literal = (-0.1, 0.3, 0.0)
lab = emo.label(*spec_literal)
ok(lab["name"] == "anger", "why not the spec's literal arousal-led pull: (-0.1, +0.3) reads %s" % lab["name"])

print("4. Running slow: a blip shows nothing, a long slow spell reads annoyed")
# 4.9 merge: Jordan's Nov 21 "small persistent errors" rule was carried by errors, which now belong to 4.5.
# What stress shows now is slowness. Small slowness all afternoon stays neutral (asked Jordan in the 4.9 thread).
small = {"response_time": 2.5}
ok(name_at(st.stress(hold(small, 10 * MIN)))[0] == "neutral", "a small slow bump for 10 minutes: nothing shows")
ok(name_at(st.stress(hold(small, 6 * HOUR)))[0] == "neutral",
   "the same small slowness all afternoon: still neutral (errors no longer carry the annoyed rule)")
slow = {"response_time": 6, "memory_used": 4}
after = name_at(st.stress(hold(slow, 6 * HOUR)))
ok(after[0] == "annoyance" and after[1] in ("faint", "mild"),
   "clearly slow and loaded all afternoon: %s %s" % (after[1], after[0]))
one = {"temperature": 2.5}
ok(name_at(st.stress(hold(one, 12 * HOUR)))[0] == "neutral", "a warm tablet alone, all day: still neutral")

print("5. Reactivity (numbers for Step 3; nothing here applies them)")
ok(st.reactivity(0) == {"negative": 1.0, "positive": 1.0}, "no stress: events land as tuned")
g = st.reactivity(1)
ok(g["negative"] == st.NEGATIVE_GAIN_FULL and g["positive"] == st.POSITIVE_GAIN_FULL,
   "full stress: bad events land harder, good ones a little softer")
ok(all(st.reactivity(i / 20)["positive"] >= st.POSITIVE_FLOOR for i in range(21)),
   "positive never drops below the floor")
saved = st.POSITIVE_GAIN_FULL
st.POSITIVE_GAIN_FULL = -5.0
ok(st.reactivity(1)["positive"] == st.POSITIVE_FLOOR, "even a bad tuning can't take her humor away")
st.POSITIVE_GAIN_FULL = saved

print("6. Audit")
s = hold({"response_time": 6, "temperature": 3, "battery": -2}, 30 * MIN)
ok(st.cause(s) == "stress: slow responses, tablet heat", "cause names the two biggest contributors")
ok(st.cause(hold({}, 30 * MIN)) is None, "nothing off: no cause")
rd = st.reading(s)
ok(set(rd) == {"stress", "load", "wear", "raw", "pull", "reactivity", "cause", "ignored"},
   "reading() gives everything 4.9 would log")
src = open(os.path.join(HERE, "rivia_stress.py")).read()
imports = {a.name for n in ast.walk(ast.parse(src)) if isinstance(n, (ast.Import, ast.ImportFrom))
           for a in n.names}
ok(imports == {"math"}, "imports only math: no database, no emotion state, no Gemini")

print("\n%d checks passed" % COUNT["n"])
