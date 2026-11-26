"""The injection line (Step 1.6, live since Nov 26): build_line() turns a state into "mood: <label>, intensity N
of 4" matching label(), with no raw numbers and no style; neutral gives the empty line; for_gemini() returns the
line live, "" with Layer 1 switched off or in shadow, logs one row per call, and never raises; only the app route
(rivia_emotion_app, GET /app/emotion/line) imports it.
Throwaway RIVIA_DIR; never touches ~/rivia.
    python test_inject.py
"""
import ast, os, re, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ["RIVIA_DIR"] = os.path.join(TMP, "rivia")
sys.path.insert(0, HERE)

import rivia_emotion as emo  # noqa: E402  (after RIVIA_DIR is set)
# Step 2 (2.9 merge): decay is on, so the state moves between two reads a second apart, and a nudge starts
# from the faded value. Every check in this file is about Step 1 behaviour (storing, clamping, the log, the
# label, the doors), which assumes no time passes, so the clock is stopped for this file. Nothing else
# changes. Decay is tested in test_decay.py and test_step2_acceptance.py.
import rivia_time as _rt  # noqa: E402
_STOPPED = _rt.now_s()
_rt.now_s = lambda: _STOPPED
import rivia_inject as inj  # noqa: E402

pts = emo.REFERENCE_POINTS


def ok(cond, label):
    assert cond, label
    print("  ok ·", label)


def scaled(name, s):
    return tuple(s * x for x in pts[name])


print("1. the line matches the label")
ok(inj.SHADOW is False, "live: shadow is off")
ok(set(inj.INTENSITY) == {w for _, w in emo.INTENSITY_BANDS} and sorted(inj.INTENSITY.values()) == [1, 2, 3, 4],
   "every intensity band has a number, 1 to 4")
ok(not hasattr(inj, "STYLE") and not hasattr(inj, "ADJECTIVE"), "no style placeholders (Step 8 owns style)")
b = inj.build_line(*scaled("anger", 0.5))
lab = emo.label(*scaled("anger", 0.5))
ok(lab["intensity"] == "mild" and b["line"] == "mood: anger, intensity 2 of 4",
   "half-strength anger: %r (the new format)" % b["line"])
ok("style" not in b and "style" not in b["line"], "no style in the line or the dict")
fmt = re.compile(r"^mood: [a-z]+, intensity [1-4] of 4$")
seen = 0
for name in pts:
    for s in (0.2, 0.4, 0.6, 0.8, 1.0):
        p = scaled(name, s)
        lab, b = emo.label(*p), inj.build_line(*p)
        if lab["name"] == "neutral":
            assert b["line"] == inj.NEUTRAL_LINE
            continue
        level = inj.INTENSITY[lab["intensity"]]
        assert (b["name"], b["intensity"], b["level"]) == (lab["name"], lab["intensity"], level), (name, s)
        assert b["line"] == "mood: %s, intensity %d of 4" % (lab["name"], level), b["line"]
        assert fmt.match(b["line"]), b["line"]
        seen += 1
ok(seen >= 40, "label and intensity N agree with label() for every point at 5 strengths (%d lines)" % seen)
ok(not re.search(r"\d\.\d|-\d", inj.build_line(*pts["distressed"])["line"]), "no raw V, A, D numbers in the line")
ok(inj.build_line(1.5, 0, 0) == inj.build_line(1, 0, 0), "out-of-range input is clamped like label()")

print("2. neutral gives the plain line")
for p in ((0, 0, 0), (0.05, -0.04, 0.06), (0.14, 0, 0)):
    b = inj.build_line(*p)
    assert (b["line"], b["name"], b["level"]) == (inj.NEUTRAL_LINE, "neutral", 0), (p, b)
ok(inj.NEUTRAL_LINE == "", "neutral line is empty (inject nothing)")
ok(inj.build_line(0.15, 0, 0)["line"] != inj.NEUTRAL_LINE, "the plain line ends where label()'s neutral ends")

print("3. live: for_gemini() hands over the line, one log row per call")
wrong = []
for name in pts:
    for s in (0.1, 0.3, 0.6, 1.0):
        emo.write(*scaled(name, s), cause="1.6 test", door="code")
        out = inj.for_gemini()
        if out != inj.build_line(*scaled(name, s))["line"]:
            wrong.append((name, s, out))
emo.write(1, 1, 1, cause="corner", door="code")
corner = inj.for_gemini()
ok(not wrong and corner == inj.build_line(1, 1, 1)["line"] != "",
   "for_gemini() returned the built line for all %d states, corner included" % (len(pts) * 4 + 1))
log = inj.recent(len(pts) * 4 + 1)
ok(len(log) == len(pts) * 4 + 1 and not any(r["shadow"] for r in log), "every call logged one row, marked live")
ok(log[0]["line"] == corner and (log[0]["v"], log[0]["a"], log[0]["d"]) == (1, 1, 1),
   "the log holds the line that went out and the state it came from: %r" % log[0]["line"])
n0 = len(inj.recent(10_000))
inj.for_gemini()
ok(len(inj.recent(10_000)) == n0 + 1, "one for_gemini() call writes exactly one row")
emo.write(*scaled("anger", 0.5), cause="1.6 test", door="code")
r = inj.current()
ok(r["line"] == "mood: anger, intensity 2 of 4" and r["shadow"] is False
   and inj.recent(1)[0]["line"] == r["line"], "current() returns and logs the line for the admin view")
before = emo.read()
inj.for_gemini(); inj.current(); inj.recent()
ok(emo.read() == before, "building and logging never changes the state")

print("4. for_gemini() never breaks a turn")
real_read = emo.read_all  # live now, so this has to break the read the line really uses
emo.read_all = lambda con=None: (_ for _ in ()).throw(RuntimeError("db gone"))
try:
    ok(inj.for_gemini() == "", "a failing read returns \"\", not an error")
finally:
    emo.read_all = real_read
ok(inj.for_gemini() != "", "(and the line is back once the read works)")
real_toggles = inj.layer_on
inj.layer_on = lambda: (_ for _ in ()).throw(ValueError("switches broken"))
try:
    ok(inj.for_gemini() == "", "a failing switch read returns \"\", not an error")
finally:
    inj.layer_on = real_toggles
real_log = inj._log
inj._log = lambda *a: (_ for _ in ()).throw(OSError("disk full"))
try:
    ok(inj.for_gemini() == "", "a failing log write returns \"\", not an error")
finally:
    inj._log = real_log

print("5. neutral, Layer 1's switch, and shadow")
ok(inj.for_gemini() == "mood: anger, intensity 2 of 4", "live: the line goes out")
emo.write(0.05, 0, 0, cause="1.6 test", door="code")
ok(inj.for_gemini() == "" and inj.recent(1)[0]["line"] == "" and inj.recent(1)[0]["name"] == "neutral",
   "neutral injects nothing (and logs the empty line)")
emo.write(*scaled("anger", 0.5), cause="1.6 test", door="code")
import rivia_toggles as tg  # noqa: E402
tg.set_switch("l1", False, door="app_button", note="test")
try:
    ok(not inj.layer_on() and inj.for_gemini() == "", "Layer 1 switched off: \"\"")
    last = inj.recent(1)[0]
    ok(last["line"] == "" and last["name"] == "anger" and last["shadow"] is False,
       "and the row logs the empty line that went out, with the label it would have said")
finally:
    tg.set_switch("l1", True, door="app_button", note="test")
ok(inj.for_gemini() == "mood: anger, intensity 2 of 4", "Layer 1 back on: the line again")
inj.SHADOW = True
try:
    ok(inj.for_gemini() == "", "with SHADOW put back to True, for_gemini() returns \"\"")
    ok(inj.recent(1)[0]["shadow"] is True and inj.recent(1)[0]["line"] == "", "and the log says shadow")
finally:
    inj.SHADOW = False

print("5b. Step 2.1: the line reads the emotion layer only")
emo.write(*emo.REFERENCE_POINTS["anger"], cause="anger", door="code")
emo.write(*emo.REFERENCE_POINTS["joy"], cause="joy mood", door="code", layer="mood")
emo.write(*emo.REFERENCE_POINTS["calm"], cause="calm baseline", door="code", layer="baseline")
ok(inj.current()["name"] == "anger", "with mood at joy and baseline at calm, the line still names emotion's anger")
out = inj.for_gemini()
ok(out == inj.current()["line"] and out.startswith("mood: anger, "), "and for_gemini() names emotion's anger too: %r" % out)

print("6. only the app route hands the line out")
src = open(os.path.join(HERE, "rivia_inject.py")).read()
imports = {n.names[0].name if isinstance(n, ast.Import) else n.module
           for n in ast.walk(ast.parse(src)) if isinstance(n, (ast.Import, ast.ImportFrom))}
ok(imports == {"rivia_emotion", "rivia_store", "rivia_time", "rivia_toggles"},
   "rivia_inject imports only the emotion state, helpers and the switches")
others = [f for f in os.listdir(HERE) if f.endswith((".py", ".patch")) and not f.startswith("test_")
          and f != "rivia_inject.py"]
users = [f for f in others if "rivia_inject" in open(os.path.join(HERE, f)).read()]
ok(users == ["rivia_emotion_app.py"], "only the app route (rivia_emotion_app) uses rivia_inject: %s" % users)
ok(re.search(r"^SHADOW = False\b", src, re.M), "SHADOW = False as written in the file (live)")
left = [f for f in os.listdir(os.environ["RIVIA_DIR"]) if f not in (tg.FILE_NAME, tg.LOG_NAME)]  # section 5's switch
ok(left and all(f.startswith("emotion.db") for f in left), "logs go to emotion.db only: %s" % sorted(left))

print("PASS")
