"""Step 2.3: the label and the injection line read the decayed EMOTION layer, and her normal (the baseline)
counts as neutral (Jordan, Nov 21).

Runs on the 2.2 decay draft (rivia_emotion.py here is 2.2's, plus 2.3's label change), with a faked clock.
  1. label, injection line and GET /app/emotion name the emotion layer, not mood or baseline
  2. a spike shows, then the lingering mood, then neutral
  3. one emotion fading only steps down
  4. a fade onto a different mood passes through the words in between
  5. shadow mode holds through a fade
  6. her normal counts as neutral, even when the baseline isn't 0, 0, 0
  7. with a 0, 0, 0 baseline nothing changes from Step 1

Lives in tests/ so Step 1's test_inject.py import scan (which counts any top-level file naming the
injection module, test files included; an open Step 1 item) doesn't trip on it.
Throwaway RIVIA_DIR; never touches ~/rivia. Needs rivia_store and rivia_time (rivia-hub tablet/):
    PYTHONPATH=<rivia-hub>/tablet python tests/test_label_layers.py
"""
import asyncio, itertools, json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DRAFT = os.path.dirname(HERE)
TMP = tempfile.mkdtemp()
os.environ["RIVIA_DIR"] = os.path.join(TMP, "rivia")
sys.path.insert(0, DRAFT)

import rivia_time as rt  # noqa: E402
CLOCK = {"t": float(int(rt.now_s()))}
rt.now_s = lambda: CLOCK["t"]

import rivia_emotion as emo  # noqa: E402  (after RIVIA_DIR is set)
import rivia_inject as inj  # noqa: E402
import rivia_emotion_app as app  # noqa: E402
from starlette.requests import Request  # noqa: E402

P = emo.REFERENCE_POINTS
Z = (0.0, 0.0, 0.0)
TIMES = (0, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 240, 480, 720, 1440)  # minutes


def ok(cond, label):
    assert cond, label
    print("  ok ·", label)


def sc(name, s):
    return tuple(s * x for x in P[name])


def setup(emotion, mood, baseline):
    """Set all three layers now (baseline first: a lower layer's write settles the ones above it)."""
    for layer, p in (("baseline", baseline), ("mood", mood), ("emotion", emotion)):
        emo.write(*p, cause="2.3 test", door="code", layer=layer)
    return CLOCK["t"]


def after(t0, minutes):
    CLOCK["t"] = t0 + minutes * 60


def vad(s):
    return s["v"], s["a"], s["d"]


def word_now():
    c = inj.current()
    return c["name"] if c["name"] == "neutral" else "%s %s" % (c["intensity"], c["name"])


def get_view():
    req = Request({"type": "http", "method": "GET", "path": app.PATH, "headers": [], "query_string": b""})
    return json.loads(asyncio.run(app.emotion(req)).body)["state"]


def path(emotion, mood, baseline):
    t0, out = setup(emotion, mood, baseline), []
    for m in TIMES:
        after(t0, m)
        w = word_now()
        if not out or out[-1] != w:
            out.append(w)
    return out


print("1. the label names the emotion layer")
setup(sc("anger", 0.5), sc("joy", 0.8), sc("calm", 0.6))
cur, view = inj.current(), get_view()
ok(inj.LABEL_LAYER == "emotion" and cur["layer"] == "emotion" and cur["name"] == "anger",
   "emotion=anger, mood=joy, baseline=calm: the line says %r" % cur["line"])
ok((view["label"], view["intensity"]) == (cur["name"], cur["intensity"]), "GET /app/emotion shows the same word and intensity")
ok(abs(inj.recent(1)[0]["v"] - sc("anger", 0.5)[0]) < 1e-9, "the shadow log stores the emotion layer's numbers")

print("2. a spike shows, then the lingering mood, then neutral (baseline 0, 0, 0)")
t0, seq = setup(P["anger"], sc("anger", 0.4), Z), []
for m in TIMES:
    after(t0, m)
    seq.append((m, inj.current()))
print("     " + "\n     ".join("%4d min  %s" % (m, c["line"] or "(nothing)") for m, c in seq))
at = dict(seq)
ok((at[0]["name"], at[0]["intensity"]) == ("anger", "moderate"), "fresh spike reads moderate anger")
ok(at[60]["name"] == "anger" and at[60]["intensity"] in ("faint", "mild"), "an hour on, the mood still shows: %r" % at[60]["line"])
ok(at[1440]["line"] == "", "a day on she reads neutral and injects nothing")

print("3. one emotion fading only steps down (baseline 0, 0, 0)")
rank = {"neutral": 0, "faint": 1, "mild": 2, "moderate": 3, "strong": 4}
for name in P:
    t0, levels = setup(P[name], Z, Z), []
    for m in TIMES:
        after(t0, m)
        c = inj.current()
        assert c["name"] in (name, "neutral"), (name, m, c)
        levels.append(rank[c["intensity"]])
    assert levels == sorted(levels, reverse=True), (name, levels)
ok(True, "all %d reference emotions keep their word and only step down" % len(P))

print("4. a fade onto a different mood passes through the words in between")
a2c = path(P["anger"], sc("contentment", 0.5), Z)
s2j = path(P["sadness"], sc("joy", 0.4), Z)
print("     anger onto a content mood:  " + " -> ".join(a2c))
print("     sadness onto a happy mood:  " + " -> ".join(s2j))
ok(a2c[0].endswith("anger") and any(w.endswith("contentment") for w in a2c), "anger fades into the content mood")
ok("neutral" in s2j[1:-1], "sadness onto joy passes briefly through neutral")

print("5. shadow mode holds through a fade")
leaked = []
for e, m in ((P["anger"], sc("contentment", 0.5)), (P["joy"], Z), (P["fear"], sc("sadness", 0.5))):
    t0 = setup(e, m, Z)
    for mins in TIMES:
        after(t0, mins)
        if inj.for_gemini() != "":
            leaked.append((e, mins))
ok(inj.SHADOW is True and not leaked, "for_gemini() returned \"\" at every point of three fades")

print("6. her normal counts as neutral (Jordan, Nov 21)")
for s in (0.25, 0.4, 0.8):
    base = sc("contentment", s)
    t0 = setup(base, base, base)
    after(t0, 1440)
    ok(inj.current()["line"] == "" and get_view()["label"] == "neutral",
       "baseline at %d%% of content, at rest: no line, the dial reads neutral" % (s * 100))
base = sc("contentment", 0.4)
a2n = path(P["anger"], base, base)
print("     anger spike, baseline mild content:  " + " -> ".join(a2n))
ok(a2n[0].endswith("anger") and a2n[-1] == "neutral", "an anger spike fades back to neutral, not to 'mildly content'")
t0 = setup(base, sc("joy", 0.6), base)
after(t0, 60)  # emotion has risen to the mood by now
mood_word = word_now()
ok(mood_word != "neutral", "a lingering mood away from her baseline is still named: %r" % mood_word)
b = emo.read(layer="baseline")
near = (b["v"] + 0.1, b["a"], b["d"])
far = (b["v"] - 0.3, b["a"], b["d"])
ok(emo.label(*near, rest=vad(b))["name"] == "neutral", "0.1 from her baseline reads neutral")
ok(emo.label(*far, rest=vad(b))["name"] != "neutral", "0.3 from her baseline is named: %s" % emo.label(*far, rest=vad(b))["name"])
emo.set_anchor(*base, cause="2.3 test", door="code")  # 2.9 merge: past 2.7's limits, the way a measured baseline goes in
emo.write(*base, cause="2.3 test", door="code")
status, reply = app.handle({"nudge": {"dv": 0.05}})
ok(status == 200 and reply["new"]["label"] == "neutral", "a small dial nudge near her baseline still reads neutral in the reply")

print("7. with a 0, 0, 0 baseline nothing changes from Step 1")
grid = [x / 10 for x in range(-10, 11, 2)]
same = all(emo.label(v, a, d, rest=Z) == emo.label(v, a, d) for v, a, d in itertools.product(grid, repeat=3))
ok(same, "label(rest=0,0,0) equals Step 1's label() at all %d grid points" % len(grid) ** 3)

print("PASS")
