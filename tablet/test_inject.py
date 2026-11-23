"""The injection line (Step 1.6): build_line() turns a state into words that match label(), with no raw
numbers; neutral gives the plain (empty) line; in shadow mode for_gemini() returns "" for every state while
still logging the line it built; for_gemini() never raises; nothing in Step 1 wires the line into a prompt.
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
ok(inj.SHADOW is True, "shadow mode is on by default")
ok(set(inj.ADJECTIVE) == set(pts), "every reference point has a word")
ok(set(inj.INTENSITY) == {w for _, w in emo.INTENSITY_BANDS}, "every intensity band has an adverb")
ok(set(inj.STYLE) <= set(pts), "style placeholders only name real reference points")
b = inj.build_line(*scaled("anger", 0.5))
lab = emo.label(*scaled("anger", 0.5))
ok(lab["intensity"] == "mild" and b["line"] == "mildly angry, dry style, intensity 2",
   "half-strength anger: %r (the spec's format)" % b["line"])
fmt = re.compile(r"^(faintly|mildly|moderately|strongly) [a-z]+, [a-z]+ style, intensity [1-4]$")
seen = 0
for name in pts:
    for s in (0.2, 0.4, 0.6, 0.8, 1.0):
        p = scaled(name, s)
        lab, b = emo.label(*p), inj.build_line(*p)
        if lab["name"] == "neutral":
            assert b["line"] == inj.NEUTRAL_LINE
            continue
        adverb, level = inj.INTENSITY[lab["intensity"]]
        assert (b["name"], b["intensity"], b["level"]) == (lab["name"], lab["intensity"], level), (name, s)
        assert b["line"].startswith("%s %s, " % (adverb, inj.ADJECTIVE[lab["name"]])), b["line"]
        assert b["line"].endswith("intensity %d" % level), b["line"]
        assert fmt.match(b["line"]), b["line"]
        seen += 1
ok(seen >= 40, "word, adverb and intensity N agree with label() for every point at 5 strengths (%d lines)" % seen)
ok(not re.search(r"\d\.\d|-\d", inj.build_line(*pts["distressed"])["line"]), "no raw V, A, D numbers in the line")
ok(inj.build_line(*pts["sadness"])["style"] == "plain", "unlisted emotions get the plain style")
ok(inj.build_line(1.5, 0, 0) == inj.build_line(1, 0, 0), "out-of-range input is clamped like label()")

print("2. neutral gives the plain line")
for p in ((0, 0, 0), (0.05, -0.04, 0.06), (0.14, 0, 0)):
    b = inj.build_line(*p)
    assert (b["line"], b["name"], b["level"]) == (inj.NEUTRAL_LINE, "neutral", 0), (p, b)
ok(inj.NEUTRAL_LINE == "", "neutral line is empty (inject nothing)")
ok(inj.build_line(0.15, 0, 0)["line"] != inj.NEUTRAL_LINE, "the plain line ends where label()'s neutral ends")

print("3. shadow mode: nothing reaches a Gemini caller")
leaked = []
for name in pts:
    for s in (0.1, 0.3, 0.6, 1.0):
        emo.write(*scaled(name, s), cause="1.6 test", door="code")
        out = inj.for_gemini()
        if out != "":
            leaked.append((name, s, out))
emo.write(1, 1, 1, cause="corner", door="code")
leaked += [x for x in [inj.for_gemini()] if x != ""]
ok(not leaked, "for_gemini() returned \"\" for all %d states, corner included" % (len(pts) * 4 + 1))
log = inj.recent(len(pts) * 4 + 1)
ok(len(log) == len(pts) * 4 + 1 and all(r["shadow"] for r in log), "every one was still logged, marked shadow")
ok(log[0]["line"] == inj.build_line(1, 1, 1)["line"] != "" and (log[0]["v"], log[0]["a"], log[0]["d"]) == (1, 1, 1),
   "the log holds the built line and the state it came from: %r" % log[0]["line"])
emo.write(*scaled("anger", 0.5), cause="1.6 test", door="code")
r = inj.current()
ok(r["line"] == "mildly angry, dry style, intensity 2" and r["shadow"] is True
   and inj.recent(1)[0]["line"] == r["line"], "current() returns and logs the line for the admin view")
before = emo.read()
inj.for_gemini(); inj.current(); inj.recent()
ok(emo.read() == before, "building and logging never changes the state")

print("4. for_gemini() never breaks a turn")
real_read = emo.read
emo.read = lambda con=None: (_ for _ in ()).throw(RuntimeError("db gone"))
try:
    ok(inj.for_gemini() == "", "a failing read returns \"\", not an error")
finally:
    emo.read = real_read
real_log = inj._log
inj._log = lambda *a: (_ for _ in ()).throw(OSError("disk full"))
try:
    ok(inj.for_gemini() == "", "a failing log write returns \"\", not an error")
finally:
    inj._log = real_log

print("5. the switch works (then goes back on)")
inj.SHADOW = False
try:
    out = inj.for_gemini()
    ok(out == "mildly angry, dry style, intensity 2", "with SHADOW off, for_gemini() returns the line")
    ok(inj.recent(1)[0]["shadow"] is False, "and the log records that it was live")
    emo.write(0.05, 0, 0, cause="1.6 test", door="code")
    ok(inj.for_gemini() == "", "neutral still injects nothing with SHADOW off")
finally:
    inj.SHADOW = True
ok(inj.for_gemini() == "", "back in shadow mode")

print("5b. Step 2.1: the line reads the emotion layer only")
emo.write(*emo.REFERENCE_POINTS["anger"], cause="anger", door="code")
emo.write(*emo.REFERENCE_POINTS["joy"], cause="joy mood", door="code", layer="mood")
emo.write(*emo.REFERENCE_POINTS["calm"], cause="calm baseline", door="code", layer="baseline")
ok(inj.current()["name"] == "anger", "with mood at joy and baseline at calm, the line still names emotion's anger")
ok(inj.for_gemini() == "", "and in shadow mode nothing reaches Gemini")

print("6. nothing in Step 1 wires the line into a prompt")
src = open(os.path.join(HERE, "rivia_inject.py")).read()
imports = {n.names[0].name if isinstance(n, ast.Import) else n.module
           for n in ast.walk(ast.parse(src)) if isinstance(n, (ast.Import, ast.ImportFrom))}
ok(imports == {"rivia_emotion", "rivia_store", "rivia_time"}, "rivia_inject imports only the emotion state and helpers")
others = [f for f in os.listdir(HERE) if f.endswith((".py", ".patch")) and f not in ("rivia_inject.py", "test_inject.py")]
users = [f for f in others if "rivia_inject" in open(os.path.join(HERE, f)).read()]
ok(not users, "no other Step 1 file imports rivia_inject (checked %d files)" % len(others))
ok(re.search(r"^SHADOW = True\b", src, re.M), "SHADOW = True as written in the file")
ok(sorted(os.listdir(os.environ["RIVIA_DIR"])) and all(f.startswith("emotion.db") for f in os.listdir(os.environ["RIVIA_DIR"])),
   "logs go to emotion.db only")

print("PASS")
