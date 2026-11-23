"""Step 5.3: is the plan-vs-day gap shrinking, growing or flat? All data here is fake; nothing is read or written
on disk. trend() is a pure function, so no clock faking is needed: every test passes its own `today`.
    python test_trend.py
"""
import datetime as dt
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rivia_trend as tr  # noqa: E402

TODAY = dt.date(2026, 12, 14)


def ok(cond, label):
    assert cond, label
    print("  ok ·", label)


def series(gaps, end=TODAY):
    """gaps oldest first, one per calendar day, ending on `end`. None = a day with too little data."""
    n = len(gaps)
    return [(end - dt.timedelta(days=n - 1 - i), g) for i, g in enumerate(gaps)]


def raises(code, fn):
    try:
        fn()
    except tr.TrendError as e:
        return e.code == code
    return False


print("1. not enough data")
r = tr.trend([], today=TODAY)
ok(r["direction"] == "not_enough_data" and r["strength"] == 0.0 and r["level"] is None, "no days at all")
r = tr.trend(series([0.3, 0.3, 0.3]), today=TODAY)
ok(r["direction"] == "not_enough_data" and len(r["days_used"]) == 3, "three scored days is too few")
r = tr.trend(series([0.3, None, None, 0.3, None, 0.3]), today=TODAY)
ok(r["direction"] == "not_enough_data", "empty days don't count toward the minimum")
old = series([0.6, 0.5, 0.4, 0.3, 0.2, 0.1, 0.1], end=TODAY - dt.timedelta(days=20))
r = tr.trend(old, today=TODAY)
ok(r["direction"] == "not_enough_data" and r["days_used"] == [], "weeks-old data alone gives no trend")

print("2. steady improvement and steady slide")
r = tr.trend(series([0.60, 0.55, 0.50, 0.45, 0.40, 0.35, 0.30]), today=TODAY)
ok(r["direction"] == "shrinking", "gap falling 0.05 a day reads shrinking")
ok(abs(r["slope_per_week"] - (-0.35)) < 1e-9, "slope is -0.35 a week")
ok(r["strength"] == 1.0 and r["wobble"] == 0.0, "steep and steady: full strength")
r = tr.trend(series([0.20, 0.22, 0.24, 0.26, 0.28, 0.30, 0.32]), today=TODAY)
ok(r["direction"] == "growing", "gap rising 0.02 a day reads growing")
ok(0.9 < r["strength"] <= 1.0, "0.14 a week is near full strength (%.3f)" % r["strength"])
r = tr.trend(series([0.40, 0.39, 0.38, 0.37, 0.36, 0.35, 0.34]), today=TODAY)
ok(r["direction"] == "shrinking" and r["strength"] < 0.5, "a gentle 0.07/week fall is shrinking but weak (%.3f)"
   % r["strength"])

print("3. flat")
r = tr.trend(series([0.25] * 7), today=TODAY)
ok(r["direction"] == "flat" and r["strength"] == 0.0 and r["slope_per_week"] == 0.0, "the same gap every day")
ok(r["level"] == 0.25, "level says where the gap sits (for 5.4's steady good / steady bad)")
r = tr.trend(series([0.300, 0.302, 0.304, 0.306, 0.308, 0.310, 0.312]), today=TODAY)
ok(r["direction"] == "flat", "0.014 a week is inside the flat band")

print("4. one bad day must not flip the trend")
base = [0.30] * 7
for i in range(7):
    g = list(base)
    g[i] = 0.95
    r = tr.trend(series(g), today=TODAY)
    ok(r["direction"] == "flat", "a steady week with one 0.95 day on day %d still reads flat" % (i + 1))
g = [0.60, 0.55, 0.50, 0.45, 0.40, 0.35, 0.30]
for i in range(7):
    h = list(g)
    h[i] = 0.98
    r = tr.trend(series(h), today=TODAY)
    ok(r["direction"] == "shrinking", "an improving week with one 0.98 day on day %d still reads shrinking" % (i + 1))
h = [0.20, 0.22, 0.24, 0.26, 0.28, 0.30, 0.02]
r = tr.trend(series(h), today=TODAY)
ok(r["direction"] == "growing", "a sliding week with one lucky last day still reads growing")
r = tr.trend(series([0.30, 0.30, 0.30, 0.30, 0.30, 0.95, 0.95]), today=TODAY)
ok(r["direction"] == "flat", "two bad days at the end are not a trend yet")
r = tr.trend(series([0.30, 0.30, 0.30, 0.30, 0.95, 0.95, 0.95]), today=TODAY)
ok(r["direction"] == "growing" and r["strength"] > 0.8, "a third bad day in a row does show, strongly")
r = tr.trend(series([0.95, 0.95, 0.95, 0.95, 0.30, 0.30, 0.30]), today=TODAY)
ok(r["direction"] == "shrinking", "and three good days after a bad stretch read shrinking")

print("5. empty days are skipped, not zero")
r = tr.trend(series([0.40, None, None, None, 0.40, 0.40, None, 0.40, 0.40]), today=TODAY)
ok(r["direction"] == "flat" and r["level"] == 0.40, "a steady gap with holes stays flat at 0.40")
r = tr.trend(series([0.60, None, None, 0.50, None, 0.40, None, 0.30, 0.25]), today=TODAY)
ok(r["direction"] == "shrinking", "improvement with holes reads shrinking")
ok(abs(r["slope_per_week"] - (-0.35)) < 0.05, "slope uses real calendar distance (%.3f a week)" % r["slope_per_week"])
rows = series([0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40])
missing = [rows[0], rows[2], rows[3], rows[6]]
r = tr.trend(missing, today=TODAY)
ok(r["direction"] == "flat" and len(r["days_used"]) == 4, "days left out of the list are the same as None")
both = series([0.40] * 7) + [(TODAY - dt.timedelta(days=30), 0.0)]
ok(tr.trend(both, today=TODAY)["days_used"] == tr.trend(series([0.40] * 7), today=TODAY)["days_used"],
   "a day older than 14 days is ignored")

print("6. only the newest 7 scored days count")
g = [0.10] * 7 + [0.50, 0.50, 0.50, 0.50, 0.50, 0.50, 0.50]
r = tr.trend(series(g), today=TODAY)
ok(r["direction"] == "flat" and r["level"] == 0.50 and len(r["days_used"]) == 7, "last week's level, not the jump")

print("7. noise")
flat_hits = 0
for seed in range(200):
    rnd = random.Random(seed)
    g = [min(1, max(0, rnd.gauss(0.35, 0.08))) for _ in range(7)]
    if tr.trend(series(g), today=TODAY)["direction"] == "flat":
        flat_hits += 1
ok(flat_hits >= 175, "noise around a steady gap reads flat most of the time (%d of 200)" % flat_hits)
strong = 0
for seed in range(200):
    rnd = random.Random(seed)
    g = [min(1, max(0, rnd.gauss(0.35, 0.08))) for _ in range(7)]
    r = tr.trend(series(g), today=TODAY)
    if r["direction"] != "flat" and r["strength"] > 0.5:
        strong += 1
ok(strong <= 20, "and noise rarely reads as a strong trend (%d of 200)" % strong)
caught = 0
for seed in range(200):
    rnd = random.Random(seed)
    g = [min(1, max(0, 0.60 - 0.04 * i + rnd.gauss(0, 0.05))) for i in range(7)]
    if tr.trend(series(g), today=TODAY)["direction"] == "shrinking":
        caught += 1
ok(caught >= 150, "a real 0.28/week improvement under noise is seen within one week most of the time (%d of 200)" % caught)
wrong = 0
for seed in range(200):
    rnd = random.Random(seed)
    g = [min(1, max(0, 0.60 - 0.04 * i + rnd.gauss(0, 0.05))) for i in range(7)]
    if tr.trend(series(g), today=TODAY)["direction"] == "growing":
        wrong += 1
ok(wrong == 0, "and never read backwards as growing")

print("8. inputs")
ok(tr.trend([{"day": "2026-12-%02d" % d, "gap": 0.3} for d in range(8, 15)], today="2026-12-14")["direction"]
   == "flat", "dicts and ISO date strings work")
ok(raises("bad_gap", lambda: tr.trend(series([1.2]), today=TODAY)), "a gap above 1 is refused")
ok(raises("bad_gap", lambda: tr.trend(series([-0.1]), today=TODAY)), "a gap below 0 is refused")
ok(raises("bad_gap", lambda: tr.trend(series([float("nan")]), today=TODAY)), "NaN is refused")
ok(raises("bad_gap", lambda: tr.trend(series([True]), today=TODAY)), "True is not a number here")
ok(raises("bad_gap", lambda: tr.trend(series(["0.3"]), today=TODAY)), "a string gap is refused")
ok(raises("duplicate", lambda: tr.trend([(TODAY, 0.3), (TODAY, 0.4)], today=TODAY)), "a repeated day is refused")
ok(raises("future", lambda: tr.trend([(TODAY + dt.timedelta(days=1), 0.3)], today=TODAY)), "a day after today")
ok(raises("bad_day", lambda: tr.trend([("monday", 0.3)], today=TODAY)), "a day that isn't a date")
ok(raises("bad_row", lambda: tr.trend([(TODAY, 0.3, "x")], today=TODAY)), "a row with three parts")
rows = series([0.6, 0.5, 0.4, 0.3, 0.2])
a = tr.trend(rows, today=TODAY)
b = tr.trend(list(reversed(rows)), today=TODAY)
ok(a == b, "order of the input doesn't matter")

print("9. output shape and limits")
r = tr.trend(series([0.6, 0.5, 0.4, 0.3, 0.2, 0.1, 0.05]), today=TODAY)
keys = set(tr.KEYS)
ok(set(r) == keys, "a read has the documented keys")
ok(set(tr.trend([], today=TODAY)) == keys, "and so does not_enough_data")
ok(r["direction"] in tr.DIRECTIONS and 0.0 <= r["strength"] <= 1.0, "direction is known and strength is 0..1")
ok(all(w not in r["reason"].lower() for w in ("fear", "scared", "afraid")), "no fear words")
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "rivia_trend.py")).read()
ok("import sqlite3" not in src and "rivia_store" not in src and "open(" not in src,
   "the module reads and writes nothing on disk")

print("\nall good")
