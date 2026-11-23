"""Tests for the confidence dial (emotion spec, Step 4.5). DRAFT, Nov 22, not installed.
  1. at her normal: confidence 1, no pull, no flags
  2. better than her normal never raises confidence or dominance (caution only goes one way)
  3. inside the dead zone counts as normal; past FULL_AT counts as fully bad; in between is a straight line
  4. one signal alone takes at most its weight; several compound but stay inside 0..1
  5. stale, down, unlearned and untracked signals
  6. the double-check flag and its hysteresis
  7. blind spots: worst first, and only brought up while double-checking
  8. the pull: dominance only, never positive, scaled by MAX_DOMINANCE_PULL
  9. bad input is refused, not guessed at
 10. pure: the same input gives the same reading, and the input isn't changed
 11. "worried": with worried_label.patch's point, small dips stay annoyed, big drops read worried, never fear
 12. no problem counts toward both dials; one outage counts once; memory sync is parked
    python test_confidence.py
"""
import copy, math, os, sys, types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rivia_confidence as rc  # noqa: E402

failed = 0
passed = 0


def ok(cond, what):
    global failed, passed
    if cond:
        passed += 1
        print("ok   ", what)
    else:
        failed += 1
        print("FAIL ", what)


NOW = 1_795_000_000


def sig(dev=0.0, trusted=True, stale=False, recent=5.0, age_s=60, learned=None):
    """One signal as 4.3's rivia_normal._read() returns it (the keys this dial reads, plus a few it ignores)."""
    learned = trusted if learned is None else learned
    return {"signal": "x", "at": NOW, "recent": None if stale else recent, "recent_n": 0 if stale else 3,
            "last_at": None if age_s is None else NOW - age_s, "stale": stale, "usual": 4.0, "spread": 1.0,
            "deviation": dev if trusted else None, "preview_deviation": dev, "trusted": trusted and not stale,
            "warmup": {"done": learned}}


def reach(share_down=0.0, dev=0.0, **kw):
    # share_down is a fraction here; 4.3 reports it in percent (4.1's unit), as the dial now expects (4.9 merge)
    return sig(dev=dev, recent=100.0 * share_down, **kw)


def normal():
    return {n: (reach() if n in rc.REACHABILITY else sig()) for n in rc.WEIGHTS}


def bad(d, n):
    """Make one signal fully bad in d."""
    if n in rc.REACHABILITY:
        d[n] = reach(1.0)
    else:
        d[n] = sig(dev=99.0)


def close(a, b, eps=1e-9):
    return abs(a - b) < eps


# 1. at her normal
r = rc.reading(normal())
ok(r["confidence"] == 1.0, "1. all signals at her normal: confidence 1.0")
ok(r["pull"] == {"dv": 0.0, "da": 0.0, "dd": 0.0}, "1. no pull at her normal")
ok(not r["double_check"] and not r["flag_blind_spots"], "1. no double-check, no blind-spot flag")
ok(r["blind_spots"] == [] and r["unlearned"] == [] and r["untracked"] == [], "1. nothing listed")
ok(rc.caution_flags(r) == [], "1. no caution flags")

# 2. better than normal
d = normal()
for n in d:
    if n not in rc.REACHABILITY:
        d[n] = sig(dev=-5.0)
r = rc.reading(d)
ok(r["confidence"] == 1.0, "2. far better than normal on every signal: still exactly 1.0, not above")
ok(r["pull"]["dd"] == 0.0, "2. no upward dominance pull from a good day")

# 3. dead zone and slope
for dev, want in ((0.5, 0.0), (rc.DEAD_ZONE, 0.0), (2.0, 0.5), (rc.FULL_AT, 1.0), (9.0, 1.0)):
    d = normal()
    d["tool_failure_rate"] = sig(dev=dev)
    b = rc.reading(d)["parts"]["tool_failure_rate"]["badness"]
    ok(close(b, want), "3. tool failures %.1f spreads worse: badness %.2f (want %.2f)" % (dev, b, want))

# 4. one alone, several together
for n, w in rc.WEIGHTS.items():
    d = normal()
    bad(d, n)
    c = rc.reading(d)["confidence"]
    ok(close(c, 1.0 - w), "4. %s fully bad alone: confidence %.2f = 1 - its weight" % (n, c))
d = normal()
for n in d:
    bad(d, n)
c = rc.reading(d)["confidence"]
counted = [max(rc.WEIGHTS[n] for n in g) for g in rc.GROUPS]
counted += [w for n, w in rc.WEIGHTS.items() if not any(n in g for g in rc.GROUPS)]
want = math.prod(1 - w for w in counted)
ok(close(c, want) and 0.0 <= c < 1.0, "4. everything fully bad: confidence %.3f, the product, inside 0..1" % c)
prev = 1.0
d = normal()
mono = True
for n in ("tool_failure_rate", "tunnel_unreachable", "restarts"):  # one from each separate cause
    bad(d, n)
    c = rc.reading(d)["confidence"]
    mono &= c < prev
    prev = c
ok(mono, "4. each extra bad signal from a separate cause lowers confidence further")

# 5. stale, down, unlearned, untracked
d = normal()
d["restarts"] = sig(stale=True, age_s=3600)
p = rc.reading(d)["parts"]["restarts"]
ok(p["why"] == "stale" and close(p["badness"], rc.STALE_PENALTY), "5. 4.3 says stale: costs STALE_PENALTY")
d = normal()
d["restarts"] = sig(stale=True, age_s=None)
ok(rc.reading(d)["parts"]["restarts"]["why"] == "stale", "5. reported but never sampled: stale")
d = normal()
d["tool_failure_rate"] = sig(dev=-3.0, stale=True)
ok(rc.reading(d)["parts"]["tool_failure_rate"]["why"] == "stale",
   "5. a good but old value is still stale: an old good number isn't evidence she's fine now")
d = normal()
d["tunnel_unreachable"] = reach(1.0)
p = rc.reading(d)["parts"]["tunnel_unreachable"]
ok(p["why"] == "down" and p["badness"] == 1.0, "5. tunnel down the whole window: fully bad")
d = normal()
d["tunnel_unreachable"] = reach(0.25)
p = rc.reading(d)["parts"]["tunnel_unreachable"]
ok(p["why"] == "down" and close(p["badness"], 0.25), "5. tunnel down a quarter of the window: a quarter bad")
d = normal()
d["tunnel_unreachable"] = reach(1.0, dev=1.0)
ok(rc.reading(d)["parts"]["tunnel_unreachable"]["badness"] == 1.0,
   "5. tunnel down with 4.3 deviation +1.0 (its spread floor): still fully bad, not inside the dead zone")
d = normal()
d["tunnel_unreachable"] = reach(1.0, trusted=False)
ok(rc.reading(d)["parts"]["tunnel_unreachable"]["badness"] == 1.0,
   "5. tunnel down before her normal is learned: counts from day one")
d = normal()
d["tunnel_unreachable"] = reach(1.0, stale=True)
ok(rc.reading(d)["parts"]["tunnel_unreachable"]["why"] == "stale", "5. no recent tunnel check: stale, not down")
d = normal()
d["failed_connections"] = sig(trusted=False)
r = rc.reading(d)
ok(r["confidence"] == 1.0 and r["unlearned"] == ["failed_connections"],
   "5. no normal learned yet: listed as unlearned, costs nothing")
fresh = {n: (reach() if n in rc.REACHABILITY else sig(dev=7.0, trusted=False)) for n in rc.WEIGHTS}
r = rc.reading(fresh)
ok(r["confidence"] == 1.0 and not r["double_check"],
   "5. first days after install (nothing learned, 4.3's preview looks bad): no false alarm")
d = normal()
del d["restarts"]
r = rc.reading(d)
ok(r["confidence"] == 1.0 and r["untracked"] == ["restarts"], "5. a signal 4.3 doesn't report: untracked, free")
r = rc.reading({})
ok(r["confidence"] == 1.0 and len(r["untracked"]) == len(rc.WEIGHTS), "5. empty input: all untracked, confidence 1")
d = normal()
d["response_time"] = sig(dev=50.0)
d["temperature"] = sig(dev=50.0)
ok(rc.reading(d)["confidence"] == 1.0, "5. stress-only signals (response time, temperature) don't touch confidence")
saved = dict(rc.BAD_SIDE)
rc.BAD_SIDE["error_rate"] = "down"
d = normal()
d["error_rate"] = sig(dev=-99.0)
ok(rc.reading(d)["parts"]["error_rate"]["badness"] == 1.0, "5. BAD_SIDE down: a big drop counts as worse")
rc.BAD_SIDE.clear(); rc.BAD_SIDE.update(saved)

# 6. double-check and hysteresis
def at(c_target):
    """A reading whose confidence is c_target, by setting tool failures alone."""
    d = normal()
    b = (1 - c_target) / rc.WEIGHTS["tool_failure_rate"]
    d["tool_failure_rate"] = sig(dev=rc.DEAD_ZONE + b * (rc.FULL_AT - rc.DEAD_ZONE))
    return d


r = rc.reading(at(0.69))
ok(r["double_check"], "6. confidence 0.69, under 0.70: double-check on")
ok(not rc.reading(at(0.71))["double_check"], "6. confidence 0.71 from off: stays off")
ok(rc.reading(at(0.75), prev_double_check=True)["double_check"], "6. 0.75 while on: stays on (hysteresis)")
ok(rc.reading(at(0.80), prev_double_check=True)["double_check"], "6. exactly 0.80 while on: stays on")
ok(not rc.reading(at(0.81), prev_double_check=True)["double_check"], "6. 0.81 while on: turns off")
seq, prev, flips = [0.69, 0.72, 0.68, 0.71, 0.73, 0.69, 0.82, 0.78, 0.81], False, 0
for c in seq:
    now = rc.reading(at(c), prev_double_check=prev)["double_check"]
    flips += now != prev
    prev = now
ok(flips == 2, "6. wobbling around 0.70: flag flips twice (on, then off above 0.80), not every reading (%d)" % flips)
ok(not rc.reading(at(0.70))["double_check"], "6. exactly 0.70 from off: stays off (strictly under)")

# 7. blind spots
d = normal()
d["tool_failure_rate"] = sig(dev=99.0)
d["restarts"] = sig(stale=True, age_s=2700)
d["error_rate"] = sig(dev=2.0)
r = rc.reading(d)
names = [b["signal"] for b in r["blind_spots"]]
ok(names == ["tool_failure_rate", "restarts", "error_rate"], "7. blind spots worst first: %s" % names)
ok([b["why"] for b in r["blind_spots"]] == ["worse", "stale", "worse"], "7. each says why")
ok(r["double_check"] and r["flag_blind_spots"], "7. low confidence: double-check and mention them")
ok(rc.caution_flags(r) == ["verify_before_answering", "mention_blind_spots"], "7. caution flags in order")
d = normal()
d["error_rate"] = sig(dev=99.0)
r = rc.reading(d)
ok(r["blind_spots"] and not r["flag_blind_spots"],
   "7. a small drop (errors alone): blind spot logged, but she doesn't bring it up")
ok(rc.caution_flags(r) == [], "7. and no caution flags")

# 8. the pull
for c_target in (1.0, 0.9, 0.75, 0.6):
    r = rc.reading(at(c_target)) if c_target < 1.0 else rc.reading(normal())
    want = -rc.MAX_DOMINANCE_PULL * max(0.0, rc.WORRY_BELOW - r["confidence"]) / rc.WORRY_BELOW
    ok(close(r["pull"]["dd"], want) and r["pull"]["dd"] <= 0, "8. confidence %.2f: dd %.3f" % (r["confidence"], r["pull"]["dd"]))
    ok(r["pull"]["dv"] == 0.0 and r["pull"]["da"] == 0.0, "8. confidence %.2f: touches dominance only" % r["confidence"])
d = normal()
for n in d:
    bad(d, n)
dd = rc.reading(d)["pull"]["dd"]
ok(-rc.MAX_DOMINANCE_PULL <= dd < 0, "8. worst case dd %.3f stays within MAX_DOMINANCE_PULL" % dd)
for c_target in (0.95, 0.85, 0.71):
    ok(rc.reading(at(c_target))["pull"]["dd"] == 0.0,
       "8. confidence %.2f, above the worry line: no pull at all (a small dip doesn't touch her mood)" % c_target)
ok(rc.reading(at(0.69))["pull"]["dd"] < 0, "8. confidence 0.69, under the worry line: the pull starts")

# 9. bad input
def refused(raw, code="bad_value", **kw):
    try:
        rc.reading(raw, **kw)
    except rc.ConfidenceError as e:
        return e.code == code
    return False


ok(refused([1, 2]), "9. not a dict: refused")
ok(refused({"tool_failure_rate": 3.0}), "9. a bare number instead of 4.3's reading: refused")
ok(refused({"tool_failure_rate": sig(dev=float("nan"))}), "9. NaN deviation: refused")
ok(refused({"tool_failure_rate": sig(dev=float("inf"))}), "9. infinite deviation: refused")
ok(refused({"tool_failure_rate": sig(dev=True)}), "9. True as a deviation: refused")
x = sig(); x["deviation"] = None
ok(refused({"tool_failure_rate": x}), "9. trusted but no deviation: refused")
x = sig(); x["last_at"] = NOW + 10
ok(refused({"tool_failure_rate": x}), "9. newest sample after the reading's time: refused")
x = sig(); x["stale"] = "no"
ok(refused({"tool_failure_rate": x}), "9. stale not a bool: refused")
x = sig(); del x["warmup"]
ok(refused({"tool_failure_rate": x}), "9. no warmup block: refused")
x = sig(); del x["at"]
ok(refused({"tool_failure_rate": x}), "9. no reading time: refused")
ok(refused(normal(), prev_double_check=None), "9. prev_double_check not a bool: refused")
ok(not refused({"made_up_signal": "anything"}), "9. a signal this dial doesn't use is ignored, not refused")

# 10. pure
d = normal()
d["tool_failure_rate"] = sig(dev=2.4)
before = copy.deepcopy(d)
a, b = rc.reading(d), rc.reading(d)
ok(a == b, "10. same input, same reading")
ok(d == before, "10. the input is not changed")
src = open(os.path.join(HERE, "rivia_confidence.py")).read()
ok(all(w not in src for w in ("import rivia_store", "import rivia_emotion", "sqlite3", "urllib", "requests",
                              "socket", "subprocess", "threading", "open(")),
   "10. imports no DB, network, emotion state or timer, and opens no file")
ok("nudge(" not in src and "write(" not in src, "10. never writes the emotion state")

# 11. "worried" (Jordan, Nov 22: "Yes add worried"), with worried_label.patch's point added to Step 2's labels.
# The body's mood pull is built here the way the dials give it: 4.4's stress along annoyed (its PULL_FULL 0.35
# and PULL_DIRECTION, copied, not imported) plus this dial's dominance pull, held to 4.6's 0.25 total.
merged = os.path.join(HERE, "..", "..", "step2", "2.9", "merged")
if os.path.exists(os.path.join(merged, "rivia_emotion.py")):
    for m in ("rivia_store", "rivia_time"):
        sys.modules.setdefault(m, types.ModuleType(m))
    sys.path.insert(0, merged)
    import rivia_emotion as emo  # noqa: E402
    before = dict(emo.REFERENCE_POINTS)
    patch = open(os.path.join(HERE, "worried_label.patch")).read()
    line = [ln for ln in patch.splitlines() if ln.startswith('+') and '"worried"' in ln][0]
    WORRIED = tuple(float(x) for x in line.split("(")[1].split(")")[0].split(","))
    emo.REFERENCE_POINTS = dict(before, worried=WORRIED)
    ok(all(emo.label(*q)["name"] == n for n, q in before.items()),
       "11. every existing label point still reads as itself with worried added")
    ok(emo.label(*WORRIED)["name"] == "worried", "11. worried's own point reads worried")
    ok(min(math.dist(WORRIED, q) for q in before.values()) > 0.3,
       "11. worried is over 0.3 from every other point (test_emotion's rule for hand-picked points)")
    ok(all(emo.label(*q, by="distance")["name"] == n for n, q in dict(before, worried=WORRIED).items()),
       "11. every point still reads as itself when labels go by plain distance too")
    ok(emo.label(*before["distressed"])["name"] == "distressed" and emo.label(-0.5, 0.5, -0.3)["name"] == "distressed",
       "11. real fear (a Step 3 event, later) still reads fear")

    def body(stress, conf):
        n = math.hypot(-0.28, 0.17)
        v, a = -0.28 / n * 0.35 * stress, 0.17 / n * 0.35 * stress
        dd = rc.reading(at(conf) if conf < 1 else normal())["pull"]["dd"] if conf >= 0.55 else \
            -rc.MAX_DOMINANCE_PULL * (rc.WORRY_BELOW - conf) / rc.WORRY_BELOW
        p = (v, a, dd)
        m = math.hypot(*p)
        return tuple(x * 0.25 / m for x in p) if m > 0.25 else p

    names = {(s_, c_): emo.label(*body(s_, c_))["name"] for s_ in (0.6, 1.0) for c_ in (1.0, 0.9, 0.8, 0.7)}
    ok(all(n == "annoyance" for n in names.values()),
       "11. stressed with confidence 0.7 or higher: annoyed stays annoyed (%s)" % sorted(set(names.values())))
    names = {(s_, c_): emo.label(*body(s_, c_))["name"] for s_ in (1.0,) for c_ in (0.5, 0.4, 0.3)}
    ok(all(n == "worried" for n in names.values()), "11. stressed with a big confidence drop: worried")
    grid = [emo.label(*body(s_, c_))["name"] for s_ in (0, 0.3, 0.6, 1.0) for c_ in (1.0, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3)]
    ok("distressed" not in grid and "fear" not in grid and "sadness" not in grid, "11. no mix of the two dials reads fear or sadness")
    ok(set(grid) <= {"neutral", "annoyance", "worried"}, "11. the body only ever reads neutral, annoyed or worried")
    emo.REFERENCE_POINTS = before
else:
    print("skip  11: merged Step 2 module not found")

# 12. one problem, one dial; one outage, once
STRESS_SIGNALS = ("response_time", "temperature", "memory_used", "storage_free", "battery")
ok(not set(rc.WEIGHTS) & set(STRESS_SIGNALS), "12. no slowness or load signal is in this dial (they're stress's)")
ok(set(rc.WEIGHTS) == {"tool_failure_rate", "failed_connections", "error_rate", "tunnel_unreachable", "restarts"},
   "12. this dial owns everything that breaks: failures, refusals, errors, tunnel, restarts")
d = normal()
for n in ("tool_failure_rate", "failed_connections", "error_rate"):
    bad(d, n)
r = rc.reading(d)
ok(close(r["confidence"], 1 - rc.WEIGHTS["tool_failure_rate"]),
   "12. a Google outage (tool failures, failed connections and errors all bad) costs once: %.2f" % r["confidence"])
ok([b["signal"] for b in r["blind_spots"]] == ["tool_failure_rate", "failed_connections", "error_rate"],
   "12. all three are still listed as blind spots")
ok([n for n, p in r["parts"].items() if p["counted"] and p["cost"] > 0] == ["tool_failure_rate"],
   "12. only the costliest of the three is counted")
d = normal()
bad(d, "error_rate")
ok(close(rc.reading(d)["confidence"], 1 - rc.WEIGHTS["error_rate"]), "12. errors alone still count on their own")
d = normal()
d["memory_sync_age"] = sig(dev=99.0)
r = rc.reading(d)
ok(r["confidence"] == 1.0 and "memory_sync_age" not in r["parts"], "12. memory sync is parked: ignored")

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
