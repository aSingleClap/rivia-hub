"""Emotion spec Step 6.1 + 6.2: her plan's predictions, and whose miss each one was. Pure, no files.
  1. a written plan gives arrive, pin and ride predictions per leg, plus one rule prediction per written plan
  2. a good day: every prediction a hit
  3. her ride estimate was off: the ride is her miss; the late arrival from it is the same mistake, counted once
  4. you left late: the late arrival is yours, not hers
  5. the server was down: a late arrival is chance
  6. you went somewhere else: the pin miss and the late arrival after it are yours
  7. a plan breaking a rule is her miss; no plan, no predictions; an unreadable places file skips everything
  8. gym and work arrivals aren't reliable: skipped, never her miss; the hit lines are adjustable
    python test_predict.py
"""
import json, os, sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rivia_plan_score as ps  # noqa: E402
import rivia_predict as pr  # noqa: E402

TZ = ps.TZ
COUNT = {"n": 0}


def ok(cond, label):
    assert cond, label
    COUNT["n"] += 1
    print("  ok ·", label)


def t(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return int(datetime.fromisoformat(day).replace(hour=h, minute=m, tzinfo=TZ).timestamp())


def iso(s):
    return datetime.fromtimestamp(s, TZ).isoformat()


def when(s):
    return {"epoch": s, "local": "", "iso": iso(s)}


D = "2026-12-01"


def plan(day=D, cid="chg_1", farm=("11:10", "11:55"), stops=None, door="system"):
    """home -> farm -> home, like Step 5's tests. stops overrides the middle stop: (place, start, end)."""
    place, s, e = stops or ("farm", farm[0], farm[1])
    b = lambda title, pl, kind, a, z: {"title": title, "place": pl, "kind": kind, "start": when(t(day, a)),  # noqa
                                       "end": when(t(day, z))}
    leave = "%02d:%02d" % divmod(int((t(day, s) - t(day, "00:00")) / 60) - 5, 60)
    back = "%02d:%02d" % divmod(int((t(day, e) - t(day, "00:00")) / 60) + 5, 60)
    blocks = [b("Home flex", "home", "detail", "10:00", leave), b("Ride", None, "ride", leave, s),
              b(place, place, "stop", s, e), b("Ride", None, "ride", e, back), b("Home", "home", "stop", back, "14:00")]
    pv = {"kind": "write_day", "day": day, "status": "planned", "blocks": blocks, "problems": []}
    return {"change_id": cid, "created": iso(t(day, "05:30")), "door": door, "kind": "write_day", "scope": "day",
            "day": day, "key": "write_day", "preview": json.dumps(pv), "status": "applied",
            "decided": iso(t(day, "05:31"))}


def ride(rid, frm, to, dep, arr, day=D, used=True):
    a, b = t(day, dep), t(day, arr)
    return {"ride_id": rid, "from": frm, "to": to, "departed": a, "arrived": b, "minutes": (b - a) / 60.0,
            "used": used}


def data(changes, rides, calls=()):
    return {"day": D, "changes": changes, "rides": rides, "arrival_calls": list(calls),
            "rules": dict(ps.RULE_DEFAULTS)}


def by(verdicts, kind, leg=None):
    return [v for v in verdicts if v["kind"] == kind and (leg is None or v["leg"] == leg)]


print("1. predictions")
preds = pr.predictions(D, data([plan()], []))
ok(len(preds) == 7, "two legs x (arrive, pin, ride) + one rule = 7: %d" % len(preds))
ok({p["area"] for p in preds} == {"arrive:farm", "arrive:home", "pin:farm", "pin:home", "ride:farm|home", "rules"},
   "areas in place words: %r" % sorted({p["area"] for p in preds}))
ok(len(pr.predictions(D, data([plan(cid="a"), plan(cid="b")], []))) == 8, "two written plans: legs from the last "
   "one, a rule prediction for each")
ok(all(p["made_at"] == t(D, "05:31") for p in preds), "each prediction carries when the plan was written")

print("2. a good day")
good = [ride("r1", "home", "farm", "11:05", "11:11"), ride("r2", "farm", "home", "11:55", "12:01")]
v = pr.judge(D, data([plan()], good))
ok(all(x["result"] == "hit" for x in v), "every prediction a hit: %r" % [(x["kind"], x["result"]) for x in v])
ok(pr.summary(v)["hits"] == 7 and pr.summary(v)["counted_misses"] == 0, "summary: 7 hits")

print("3. her ride estimate was off: counted once")
slow = [ride("r1", "home", "farm", "11:05", "11:25"), ride("r2", "farm", "home", "11:55", "12:01")]
v = pr.judge(D, data([plan()], slow))
r0, a0 = by(v, "ride", 0)[0], by(v, "arrive", 0)[0]
ok(r0["result"] == "miss" and r0["owner"] == "rivia" and r0["counted"], "the ride is her miss, counted")
ok(a0["result"] == "miss" and a0["owner"] == "rivia" and not a0["counted"], "the late arrival is the same mistake, "
   "not counted again: %s" % a0["why"])
ok(pr.summary(v)["same_mistake"] == 1, "summary shows the one same-mistake")

print("4. you left late")
late = [ride("r1", "home", "farm", "11:30", "11:36"), ride("r2", "farm", "home", "11:55", "12:01")]
v = pr.judge(D, data([plan()], late))
a0 = by(v, "arrive", 0)[0]
ok(a0["result"] == "miss" and a0["owner"] == "jordan", "your late leaving is yours: %s" % a0["why"])
ok(by(v, "ride", 0)[0]["result"] == "hit", "her ride estimate still held")

print("5. the server was down")
v = pr.judge(D, data([plan()], late), outages=[(t(D, "11:00"), t(D, "11:40"))])
ok(by(v, "arrive", 0)[0]["owner"] == "chance", "a late arrival during an outage is chance")
none = [ride("r2", "farm", "home", "11:55", "12:01")]
v = pr.judge(D, data([plan()], none), outages=[(t(D, "11:00"), t(D, "11:40"))])
ok(by(v, "arrive", 0)[0]["result"] == "skip", "no farm arrival logged during an outage: skipped, never her miss")
ok(by(v, "pin", 0)[0]["result"] == "hit", "the pin still holds: you left the farm, so you were there")

print("6. you went somewhere else")
pf = [ride("r1", "home", "pf", "11:04", "11:20"), ride("r2", "pf", "home", "11:56", "12:12")]
v = pr.judge(D, data([plan()], pf))
ok(by(v, "pin", 0)[0]["owner"] == "jordan", "the farm pin miss is yours (you went to Planet Fitness)")
a1 = by(v, "arrive", 1)[0]
ok(a1["result"] == "miss" and a1["owner"] == "jordan", "home late after coming from somewhere else: yours: %s"
   % a1["why"])
ok(pr.summary(v)["counted_misses"] == pr.summary(v)["misses"]["jordan"], "none of it counts against her")

print("7. rules, no plan, no places file")
short = plan(farm=("11:10", "11:40"))
v = pr.judge(D, data([short], good))
rule = by(v, "rule")[0]
ok(rule["result"] == "miss" and rule["owner"] == "rivia" and "farm_too_short" in rule["why"],
   "a 30-minute farm stop breaks your 45-minute rule: her miss")
ok(pr.judge(D, data([], good)) == [] and pr.judge(D, {"day": D, "changes": None}) == [],
   "no written plan (scheduling parked): no predictions, nothing judged")
v = pr.judge(D, data([plan()], None))
ok(all(x["result"] in ("skip", "hit") for x in v) and by(v, "rule")[0]["result"] == "hit",
   "unreadable places file: legs skipped, the rule still judged")

print("8. unreliable places and adjustable lines")
gym = plan(stops=("gym", "15:00", "16:30"))
v = pr.judge(D, data([gym], []))
ok(by(v, "arrive", 0)[0]["result"] == "skip" and "aren't reliable" in by(v, "arrive", 0)[0]["why"],
   "a missing gym arrival is skipped, not hers")
ok(all(not x["counted"] for x in v if x["result"] == "skip"), "skips never count")
near = [ride("r1", "home", "farm", "11:05", "11:22"), ride("r2", "farm", "home", "11:55", "12:01")]
strict = by(pr.judge(D, data([plan()], near)), "arrive", 0)[0]
loose = by(pr.judge(D, data([plan()], near), lines={"arrive_hit_min": 15, "ride_hit_min": 15}), "arrive", 0)[0]
ok(strict["result"] == "miss" and loose["result"] == "hit", "12 minutes late: a miss at 10, a hit at 15 (your line)")
words = json.dumps([x["why"] for x in pr.judge(D, data([plan()], slow))]).lower()
ok(not any(w in words for w in ("fear", "scared", "afraid")), "no fear words")

print("\n%d checks passed" % COUNT["n"])
