"""Step 5.2: the daily gap number. Sample days from 5.1's shape -> one gap 0..1, or None when too thin.
Pure functions: no clock, no database, no network.
    python test_gap.py
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rivia_gap as g  # noqa: E402

COUNT = {"n": 0}


def ok(cond, label):
    assert cond, label
    COUNT["n"] += 1
    print("  ok ·", label)


def day(date="2026-11-24", **kw):
    d = {"date": date, "arrival_miss_min": None, "pin_hit_rate": None, "plan_held": None,
         "rule_breaks": None, "missing": []}
    d.update(kw)
    return d


def close(a, b):
    return a is not None and abs(a - b) < 1e-3


print("weights")
ok(abs(sum(g.WEIGHTS.values()) - 1) < 1e-9, "shares add up to 1")
ok(max(g.WEIGHTS, key=g.WEIGHTS.get) == "rule_breaks", "rule breaks have the biggest share")

print("perfect and worst days")
perfect = g.daily_gap(day(arrival_miss_min=0, pin_hit_rate=1.0, plan_held=1.0, rule_breaks=0))
ok(perfect["gap"] == 0.0, "everything matched -> gap 0")
worst = g.daily_gap(day(arrival_miss_min=60, pin_hit_rate=0.0, plan_held=0.0, rule_breaks=3))
ok(worst["gap"] == 1.0, "everything wrong -> gap 1")

print("ride times")
ok(g.miss_ride(2) == 0 and g.miss_ride(-2) == 0, "within 2 minutes counts as on time, early or late")
ok(g.miss_ride(15) == 1 and g.miss_ride(40) == 1, "15+ minutes off is a full miss, never past it")
ok(close(g.miss_ride(8.5), 0.5), "halfway between 2 and 15 is a half miss")
ok(g.miss_ride(-9) == g.miss_ride(9), "early counts the same as late")

print("a typical good day")
r = g.daily_gap(day(arrival_miss_min=4, pin_hit_rate=0.9, plan_held=0.8, rule_breaks=0))
# misses: ride (4-2)/13=.1538*.20 + pins .1*.15 + plan .2*.25 + rules 0 = .03077+.015+.05 = .0958
ok(close(r["gap"], 0.0958), "small misses -> small gap (0.096)")
ok(not r["floored"], "no rule break, no floor")

print("rules come first")
r = g.daily_gap(day(arrival_miss_min=0, pin_hit_rate=1.0, plan_held=1.0, rule_breaks=1))
ok(r["gap"] == g.RULE_BREAK_FLOOR and r["floored"], "one break on an otherwise perfect day -> at least 0.5")
r2 = g.daily_gap(day(arrival_miss_min=0, pin_hit_rate=1.0, plan_held=1.0, rule_breaks=2))
ok(r2["gap"] == 0.5, "two breaks: rule part is full (0.4 of the say), floor still lifts to 0.5")
r3 = g.daily_gap(day(arrival_miss_min=15, pin_hit_rate=0.5, plan_held=0.4, rule_breaks=2))
ok(r3["gap"] > 0.5 and not r3["floored"], "a bad day with breaks goes above the floor on its own")
messy_no_break = g.daily_gap(day(arrival_miss_min=15, pin_hit_rate=0.0, plan_held=0.5, rule_breaks=0))
# ride full miss .20 + pins full miss .15 + half the day rearranged .125 = .475
ok(close(messy_no_break["gap"], 0.475) and messy_no_break["gap"] < r["gap"],
   "a messy day with no rule break (0.475) still reads better than one broken rule (0.5)")

print("missing data is never a miss")
r = g.daily_gap(day(arrival_miss_min=4, pin_hit_rate=None, plan_held=0.8, rule_breaks=0))
ok("pin_hit_rate" in r["missing"] and "pin_hit_rate" not in r["misses"], "None pins are left out")
# without pins: (.03077 + .05) / .85 = .0950
ok(close(r["gap"], 0.0950), "shares of the rest scale back up (0.095)")
same = g.daily_gap(day(arrival_miss_min=4, pin_hit_rate=0.0, plan_held=0.8, rule_breaks=0, missing=["pin_hit_rate"]))
ok(same["gap"] == r["gap"], "a score listed in missing is left out even if it has a value")
no_key = {"date": "2026-11-24", "arrival_miss_min": 4, "plan_held": 0.8, "rule_breaks": 0}
ok(g.daily_gap(no_key)["gap"] == r["gap"], "a score that isn't there at all is left out the same way")
gym_hole = g.daily_gap(day(arrival_miss_min=3, pin_hit_rate=None, plan_held=1.0, rule_breaks=0,
                           missing=["pin_hit_rate"]))
ok(gym_hole["gap"] < 0.05, "gym/work arrivals not firing doesn't make a good day look bad")

print("too little data -> no reading")
empty = g.daily_gap(day(missing=["arrival_miss_min", "pin_hit_rate", "plan_held", "rule_breaks"]))
ok(empty["gap"] is None and empty["why_none"], "parked scheduling day: no gap, with a reason")
one = g.daily_gap(day(rule_breaks=0))
ok(one["gap"] is None, "only '0 rule breaks' on a day with nothing else: no gap")
one_bad = g.daily_gap(day(plan_held=0.0))
ok(one_bad["gap"] is None, "one bad score alone isn't enough either")
two = g.daily_gap(day(arrival_miss_min=4, pin_hit_rate=0.9))
ok(two["gap"] is not None, "two scores are enough")

print("bad values are left out, not counted as misses")
r = g.daily_gap(day(arrival_miss_min=4, pin_hit_rate=1.7, plan_held=0.8, rule_breaks=0))
ok(r["rejected"].get("pin_hit_rate") and "pin_hit_rate" not in r["misses"], "a share over 1 is rejected")
ok(close(r["gap"], 0.0950), "and the day reads as if pins were missing")
for bad in (float("nan"), float("inf"), "0.9", True):
    rr = g.daily_gap(day(arrival_miss_min=4, pin_hit_rate=bad, plan_held=0.8, rule_breaks=0))
    ok("pin_hit_rate" in rr["rejected"], "pins %r rejected" % (bad,))
ok("rule_breaks" in g.daily_gap(day(arrival_miss_min=1, plan_held=1, rule_breaks=-1))["rejected"],
   "negative rule breaks rejected")
ok("rule_breaks" in g.daily_gap(day(arrival_miss_min=1, plan_held=1, rule_breaks=1.5))["rejected"],
   "half a rule break rejected")
ok(g.daily_gap(day(pin_hit_rate=1.0000000001, plan_held=1))["gap"] == 0.0, "rounding slack over 1 is fine")
try:
    g.daily_gap(["not", "a", "day"])
    ok(False, "non-dict raises")
except g.GapError:
    ok(True, "a record that isn't a day raises")
try:
    g.daily_gap({"plan_held": 1})
    ok(False, "no date raises")
except g.GapError:
    ok(True, "a day with no date raises")

print("several days for 5.3")
week = [
    day("2026-11-26", arrival_miss_min=3, pin_hit_rate=1.0, plan_held=0.9, rule_breaks=0),
    day("2026-11-24", arrival_miss_min=9, pin_hit_rate=0.8, plan_held=0.6, rule_breaks=0),
    day("2026-11-25", missing=["arrival_miss_min", "pin_hit_rate", "plan_held", "rule_breaks"]),
]
out = g.daily_gaps(week)
ok([o["date"] for o in out] == ["2026-11-24", "2026-11-25", "2026-11-26"], "comes back oldest first")
ok(out[1]["gap"] is None, "the empty day stays a hole, not a zero")
ok(out[0]["gap"] > out[2]["gap"], "a better day reads a smaller gap")

print("5.6 merge: reads 5.1's real day shape")
real = {"date": "2026-11-17", "arrival_miss_min": 6.6, "ride_est_miss_min": 6.2, "ride_est_wins": 0,
        "pin_hit_rate": 1.0, "plan_held": 1.0, "rule_breaks": 0, "missing": [], "why": {}, "detail": {}}
r = g.daily_gap(real)
ok(close(r["gap"], 0.2 * (6.6 - 2) / 13), "arrival time fills the rides 20% slot (Nov 17: 0.071)")
ok(r["logged"] == {"ride_est_miss_min": 6.2, "ride_est_wins": 0}, "her estimate score and the plus are logged")
worse_est = dict(real, ride_est_miss_min=60, ride_est_wins=5)
ok(g.daily_gap(worse_est)["gap"] == r["gap"], "the estimate score and the plus carry no weight")
ok(set(g.SCORES) == {"arrival_miss_min", "pin_hit_rate", "plan_held", "rule_breaks"}, "the four weighted scores")
ok("ride_miss_min" not in g.SCORES, "the old ride_miss_min name is gone")

print("writes nothing")
src = open(os.path.join(HERE, "rivia_gap.py")).read()
for word in ("sqlite", "open(", "requests", "socket", "subprocess", "time.time"):
    ok(word not in src, "no %s in the gap code" % word)
for word in ("fear", "scared", "afraid"):
    ok(word not in src.lower(), "no '%s' in the gap code" % word)

print("\n%d checks passed" % COUNT["n"])
