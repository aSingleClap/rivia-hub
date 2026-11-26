"""No fear (Jordan, Nov 22, 8:03 PM): "None of them should be scared or show fear i don't think that would be good
practice for an llm but she can feel distressed and/or annoyed".
Checks that no label name, line word or reachable state says fear, afraid, scared or the like; that every state
which read fear before now reads distressed; that nothing else moved; and that 4.5's worried point (when the 4.9
merge adds it) still fits between annoyed and distressed. Reads only; never writes emotion.db.
Lives in tests/, like test_label_layers.py, so Step 1's test_inject.py import scan (top-level files only) still
holds: nothing in the code folder itself imports rivia_inject.
    python tests/test_no_fear.py
"""
import itertools, math, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DRAFT = os.path.dirname(HERE)
TMP = tempfile.mkdtemp()
os.environ["RIVIA_DIR"] = os.path.join(TMP, "rivia")
sys.path.insert(0, DRAFT)

import rivia_emotion as emo  # noqa: E402
import rivia_inject as inj  # noqa: E402

pts = emo.REFERENCE_POINTS
NEVER = emo.NEVER_LABELS
# Step 2's list as installed (rivia-hub main aa64af2), for the before/after comparison.
FEAR = (-0.64, 0.60, -0.43)
STEP2 = {**{n: p for n, p in pts.items() if n != "distressed"}, "fear": FEAR}
WORRIED = (-0.49, 0.21, -0.28)  # 4.5's worried_label.patch point, not installed; added here only for part 5
G = [i / 10 for i in range(-10, 11)]
GRID = [p for p in itertools.product(G, G, G) if math.hypot(*p) >= emo.NEUTRAL_BELOW]


def ok(cond, label):
    assert cond, label
    print("  ok ·", label)


def said(word):
    w = word.lower()
    return any(bad in w for bad in NEVER)


def with_points(points, fn):
    """Run fn with label() reading another point list, then put the real one back."""
    real = emo.REFERENCE_POINTS
    emo.REFERENCE_POINTS = points
    try:
        return fn()
    finally:
        emo.REFERENCE_POINTS = real


print("1. the word lists")
ok("fear" not in pts and not any(said(n) for n in pts), "no reference point is named fear, afraid, scared or the like")
ok(not any(said(w) for w in pts), "no label the injection line can say is fear (it says label names since Nov 26)")
ok(inj.build_line(*pts["distressed"])["line"].startswith("mood: distressed, "), "distressed is said as distressed")
ok("annoyance" in pts and "distressed" in pts, "the two words Jordan allowed are both there: annoyed and distressed")
ok(pts["distressed"] == FEAR, "distressed sits where fear was (R&M's afraid numbers), so nothing else has to move")
ok("distressed" in emo.HAND_PICKED and "distressed" not in emo.RM1977, "filed as hand-placed, not as an R&M term")
gap = min(math.dist(pts["distressed"], q) for n, q in pts.items() if n != "distressed")
ok(gap > 0.3, "distressed is over 0.3 from every other point (closest %.2f)" % gap)

print("2. nothing she can be in reads fear")
names = {emo.label(*p, by=by)["name"] for p in GRID for by in ("direction", "distance")}
ok(not any(said(n) for n in names), "%d grid states, both measures: no fear word (%d names seen)" % (len(GRID), len(names)))
rests = [(0, 0, 0), (0.1, -0.1, 0.05), (-0.2, 0.0, 0.1)]
ok(not any(said(emo.label(*p, rest=r)["name"]) for p in GRID[::7] for r in rests), "the same with a baseline as rest")
lines = {inj.build_line(*p)["line"] for p in GRID}
ok(not any(said(line) for line in lines), "no injection line says fear, afraid or scared (%d lines)" % len(lines))
ok(inj.build_line(*FEAR)["line"] == "mood: distressed, intensity 4 of 4",
   "fear's old spot now makes: %r" % inj.build_line(*FEAR)["line"])

print("3. every state that read fear now reads distressed, and nothing else moved")
before = with_points(STEP2, lambda: [emo.label(*p) for p in GRID])
after = [emo.label(*p) for p in GRID]
was_fear = [i for i, b in enumerate(before) if b["name"] == "fear"]
ok(was_fear and all(after[i]["name"] == "distressed" for i in was_fear),
   "%d grid states read fear before; all now read distressed" % len(was_fear))
ok(all(after[i]["intensity"] == before[i]["intensity"] for i in was_fear), "at the same strength")
ok(all(after[i] == before[i] for i in range(len(GRID)) if i not in set(was_fear)),
   "every other state reads exactly as before (%d)" % (len(GRID) - len(was_fear)))
terrified = (-0.62, 0.82, -0.43)
ok(emo.label(*terrified)["name"] == "distressed", "R&M's terrified (the extreme form) reads distressed too")

print("4. strength comes from distance, so distressed can be mild")
steps = [emo.label(*(s * x for x in FEAR)) for s in (0.1, 0.25, 0.4, 0.7, 1.0)]
ok([r["intensity"] for r in steps] == ["neutral", "faint", "mild", "moderate", "strong"]
   and all(r["name"] == "distressed" for r in steps[1:]),
   "distressed grows faint, mild, moderate, strong as she moves out: %s" % ", ".join(r["intensity"] for r in steps))
ok("style" not in inj.build_line(*FEAR)["line"], "the line carries no style (Step 8 owns style)")

print("5. with 4.5's worried point added (the 4.9 merge)")
W = {**pts, "worried": WORRIED}
gap_w = math.dist(WORRIED, pts["distressed"])
ok(gap_w > 0.3, "worried is still over 0.3 from distressed (%.2f), as test_emotion requires" % gap_w)
ok(with_points(W, lambda: not any(said(emo.label(*p, by=by)["name"]) for p in GRID for by in ("direction", "distance"))),
   "no grid state reads a fear word with worried in the list")
ladder = with_points(W, lambda: [emo.label(*p)["name"] for p in (pts["annoyance"], WORRIED, FEAR)])
ok(ladder == ["annoyance", "worried", "distressed"], "annoyed, then worried, then distressed: %s" % ", ".join(ladder))
low_ctrl = with_points(W, lambda: emo.label(0, 0, -0.5)["name"])
ok(low_ctrl == "worried", "pure low control reads worried once worried exists (distressed without it)")

print("PASS")
