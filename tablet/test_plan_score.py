"""Emotion spec Step 5.1: the plan-accuracy scores, on sample days in a fake tablet folder.
    python3 test_plan_score.py
Builds approvals.db, places.db, core.db and scheduler.db with the same tables as rivia-hub 23bd731, then scores them.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
RIVIA = os.path.join(TMP, "rivia")
os.makedirs(RIVIA)
os.environ["RIVIA_DIR"] = RIVIA
sys.path.insert(0, HERE)

import rivia_plan_score as ps  # noqa: E402

TZ = ZoneInfo("America/New_York")
failed = 0
checks = 0


def ok(cond, label):
    global failed, checks
    checks += 1
    print(("  ok · " if cond else "FAIL · ") + label)
    if not cond:
        failed += 1


# ---------- the tablet's tables (copied from rivia-hub 23bd731) ----------
SCHEMAS = {
    "approvals": """create table changes(change_id text primary key, created text not null, door text, kind text,
        scope text, day text, key text, old text, new text, preview text, status text, decided text, reason text,
        request_id text);""",
    "places": """create table rides(ride_id text primary key, day text, from_place text, to_place text,
        departed text, arrived text, left_local text, arrived_local text, minutes real, used integer, note text,
        distance_km real, avg_kmh real, polyline text, logged text);""",
    "core": """create table calls(ts text not null, door text not null, tool text not null, action text not null,
        ok integer not null, code text, ms integer not null);""",
    "scheduler": """create table settings(key text primary key, value text not null, updated text not null,
        change_id text);
        create table overrides(day text not null, key text not null, value text not null, created text not null,
        change_id text, primary key (day, key));""",
}


def make_dbs(folder):
    for name, schema in SCHEMAS.items():
        con = sqlite3.connect(os.path.join(folder, name + ".db"))
        con.executescript(schema)
        con.commit()
        con.close()


def db(name, sql, args=()):
    con = sqlite3.connect(os.path.join(RIVIA, name + ".db"))
    con.execute(sql, args)
    con.commit()
    con.close()


def t(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    d = datetime.fromisoformat(day).replace(hour=h, minute=m, tzinfo=TZ)
    return int(d.timestamp())


def iso(s):
    return datetime.fromtimestamp(s, TZ).isoformat()


def when(s):
    return {"epoch": s, "local": "", "iso": iso(s)}


def block(title, place, kind, start, end):
    return {"title": title, "place": place, "kind": kind, "start": when(start), "end": when(end), "desc": ""}


def workday(day, farm=("11:10", "11:55"), gym_id="gym", gym=("12:15", "14:45"), shift=("15:00", "23:30"),
            problems=None):
    """A weekday plan like build_workday's: home, farm, gym, work, home."""
    fs, fe = t(day, farm[0]), t(day, farm[1])
    gs, ge = t(day, gym[0]), t(day, gym[1])
    ss, se = t(day, shift[0]), t(day, shift[1])
    blocks = [
        block("Home flex", "home", "detail", fs - 3600, fs - 300),
        block("Ride: home to the farm", None, "ride", fs - 300, fs),
        block("Farm", "farm", "stop", fs, fe),
        block("Ride: farm to the gym", None, "ride", fe, gs),
        block("Gym", gym_id, "stop", gs, ge),
        block("Ride: gym to work", None, "ride", ge, ge + 300),
        block("Arrive early", "hmc", "detail", ge + 300, ss),
        block("Ride: work to home", None, "ride", se, se + 1500),
        block("Home", "home", "stop", se + 1500, se + 1500 + 3600),
    ]
    return {"kind": "write_day", "day": day, "status": "planned", "blocks": blocks,
            "shift": {"title": "Shift", "start": when(ss), "end": when(se)}, "problems": problems or []}


_n = [0]


def write_plan(day, preview, door="system", status="applied", decided=None):
    _n[0] += 1
    created = t(day, "05:30") - 86400 + _n[0]
    db("approvals", "insert into changes(change_id, created, door, kind, scope, day, key, preview, status, decided) "
       "values(?,?,?,?,?,?,?,?,?,?)",
       ("chg_%d" % _n[0], iso(created), door, "write_day", "day", day, "write_day", json.dumps(preview), status,
        iso(decided if decided is not None else created + 60)))


def day_change(day, key, at, door="app"):
    _n[0] += 1
    db("approvals", "insert into changes(change_id, created, door, kind, scope, day, key, new, status, decided) "
       "values(?,?,?,?,?,?,?,?,?,?)", ("chg_%d" % _n[0], iso(at), door, "setting", "day", day, key, "30",
                                     "applied", iso(at)))


def ride(rid, frm, to, dep, arr, used=1):
    db("places", "insert into rides(ride_id, day, from_place, to_place, departed, arrived, minutes, used) "
       "values(?,?,?,?,?,?,?,?)", (rid, iso(dep)[:10], frm, to, iso(dep), iso(arr), round((arr - dep) / 6) / 10, used))
    call(arr)


def call(at, ok_=1):
    db("core", "insert into calls(ts, door, tool, action, ok, code, ms) values(?,?,?,?,?,?,?)",
       (iso(at), "app", "app_arrival", "sched.arrival", ok_, None, 120))


make_dbs(RIVIA)

# ---------- sample days ----------
D1, D2, D3, D4, D5, D6, D7, D8 = ("2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06",
                                  "2026-11-09", "2026-11-10", "2026-11-11")  # Mon..Fri, Mon..Wed

# D1: a good day. Home to farm took 5.4 min (planned 5). Farm arrival on time. Gym and work never fire, and the
# arrival home after work comes with no ride (the work exit never fired), so it is skipped too.
write_plan(D1, workday(D1))
ride("r1", "home", "farm", t(D1, "11:06"), t(D1, "11:06") + 324)
call(t(D1, "23:55"))  # home arrival, no ride attached

# D2: went to the wrong place. A ride home -> pf around farm time: the farm arrival is a miss.
write_plan(D2, workday(D2))
ride("r2", "home", "pf", t(D2, "11:05"), t(D2, "11:22"))

# D3: nothing at all (scheduling parked, 5:30 run off).

# D4: Jordan rearranged. He sets a 30-min farm for the day; a second plan from the app moves the farm and gym.
write_plan(D4, workday(D4))
day_change(D4, "farm_minutes", t(D4, "09:00"))
db("scheduler", "insert into overrides(day, key, value, created) values(?,?,?,?)", (D4, "farm_minutes", "30", iso(0)))
write_plan(D4, workday(D4, farm=("10:40", "11:10"), gym=("11:30", "14:45")), door="app",
           decided=t(D4, "09:01"))

# D5: a written plan that breaks rules: farm 30 min (rule 45), gym 7 to 8:45 PM (closes 8 PM), and a problem list.
write_plan(D5, workday(D5, farm=("18:10", "18:40"), gym=("19:00", "20:45"), shift=("21:00", "23:30"),
                       problems=["Gap between Farm and Gym."]))

# D6: Jordan rejected her proposal; nothing written.
write_plan(D6, workday(D6), door="app", status="rejected")

# D7: same short farm as D5, but Jordan's one-day override says 30 min is fine: no break. Unclean ride only.
db("scheduler", "insert into overrides(day, key, value, created) values(?,?,?,?)", (D7, "farm_minutes", "30", iso(0)))
db("scheduler", "insert into settings(key, value, updated) values(?,?,?)", ("farm_minutes", "45", iso(0)))
write_plan(D7, workday(D7, farm=("11:10", "11:40"), gym=("12:00", "14:45")))
ride("r7", "home", "farm", t(D7, "11:05"), t(D7, "11:05") + 60 * 40, used=0)  # 40 min for a 5 min ride: not clean

# D8: plan written, but the phone sent no arrivals at all.
write_plan(D8, workday(D8))

# D9: an early ride farm -> home has a place; an arrival call at farm time came with no ride. The farm arrival
# might be that call, so it is unsure, and with nothing else to check pin hits are missing.
D9 = "2026-11-12"
write_plan(D9, workday(D9))
ride("r9", "farm", "home", t(D9, "09:00"), t(D9, "09:06"))
call(t(D9, "11:11"))

# D10: a Saturday with Planet Fitness. Jordan left home 50 min late (not her miss); farm -> pf took 26 min,
# planned 20. Both arrivals came in at the right place.
D10 = "2026-11-14"
write_plan(D10, workday(D10, gym_id="pf"))
ride("r10a", "home", "farm", t(D10, "11:55"), t(D10, "11:55") + 300)
ride("r10b", "farm", "pf", t(D10, "12:40"), t(D10, "12:40") + 26 * 60)

# D11: no home -> farm ride logged, but he rode farm -> gym (12 min, planned 20) and gym -> work (5 min).
D11 = "2026-11-16"
write_plan(D11, workday(D11))
ride("r11a", "farm", "gym", t(D11, "11:58"), t(D11, "11:58") + 12 * 60)
ride("r11b", "gym", "hmc", t(D11, "14:46"), t(D11, "14:46") + 5 * 60)

# D12: she has learned farm -> gym is 12 min (your hand estimate is 20). It took 11.
D12 = "2026-11-17"
write_plan(D12, workday(D12, gym=("12:07", "14:45")))
ride("r12", "farm", "gym", t(D12, "11:56"), t(D12, "11:56") + 11 * 60)


def fingerprint(folder):
    out = {}
    for f in sorted(os.listdir(folder)):
        with open(os.path.join(folder, f), "rb") as fh:
            out[f] = (hashlib.sha256(fh.read()).hexdigest(), os.path.getmtime(os.path.join(folder, f)))
    return out


before = fingerprint(RIVIA)
days = ps.score_days(D1, D12)
by = {d["date"]: d for d in days}
after = fingerprint(RIVIA)

print("shape")
ok(len(days) == 16 and days[0]["date"] == D1 and days[-1]["date"] == D12, "one dict per day, oldest first")
ok(all(set(d) == {"date", "missing", "why", "detail"} | set(ps.SCORES) for d in days), "every day has the agreed keys")
ok(ps.SCORES[0] == "arrival_miss_min", "arrival time against the plan is the main score")
ok(all(set(d["missing"]) == set(d["why"]) for d in days), "every missing score says why")
ok(all((d[s] is None) == (s in d["missing"]) for d in days for s in ps.SCORES), "None exactly when missing")

print("D1 good day")
d = by[D1]
ok(d["ride_est_miss_min"] == 0.4, "ride miss 0.4 min (5.4 real vs 5 planned), got %r" % d["ride_est_miss_min"])
ok(d["pin_hit_rate"] == 1.0, "farm arrival hit; got %r" % d["pin_hit_rate"])
ok(d["arrival_miss_min"] == 1.4, "farm arrival 1.4 min after the plan; got %r" % d["arrival_miss_min"])
ok(d["ride_est_wins"] == 0 and d["detail"]["ride_est_wins"]["same"] == 1, "her 5 min equals your 5 min: no plus")
ok(d["detail"]["pin_hit_rate"]["unreliable_place"] == 3, "missing gym, work, and home-after-work arrivals skipped")
ok(d["detail"]["ride_est_miss_min"]["no_ride_logged"] == 3, "planned rides with no ride logged are skipped, not misses")
ok(d["plan_held"] == 1.0 and d["rule_breaks"] == 0, "plan held, no rules broken")

print("D2 wrong place")
d = by[D2]
ok(d["pin_hit_rate"] == 0.0, "pf instead of the farm is a miss; got %r" % d["pin_hit_rate"])
ok(d["ride_est_miss_min"] is None and "ride_est_miss_min" in d["missing"], "no matching ride is missing, not a miss")
# 5.6 merge, counted once: the same day with the server down around farm time. The farm miss is left out.
down = [(t(D2, "10:40"), t(D2, "11:30"))]
o = ps.score_day(D2, RIVIA, outages=down)
ok(o["detail"]["pin_hit_rate"]["outage"] == 1 and o["detail"]["pin_hit_rate"]["misses"] == 0,
   "a pin miss while the server was down is left out, not counted")
ok(o["pin_hit_rate"] is None and o["why"]["pin_hit_rate"] == "the server was down when the arrivals were due",
   "with nothing else to judge, pins are missing and say why")
far = [(t(D2, "18:00"), t(D2, "19:00"))]
ok(ps.score_day(D2, RIVIA, outages=far)["pin_hit_rate"] == 0.0, "an outage hours away excuses nothing")
ok(ps.score_day(D2, RIVIA, outages=[])["pin_hit_rate"] == 0.0, "no outages: same as before")
ok(ps.score_day(D1, RIVIA, outages=[(t(D1, "00:00"), t(D1, "23:59"))])["pin_hit_rate"] == by[D1]["pin_hit_rate"],
   "an outage never removes a hit: a ride that logged is real")

print("D3 nothing recorded (scheduling parked)")
d = by[D3]
ok(d["missing"] == list(ps.SCORES), "every score missing")
ok(d["why"]["plan_held"] == "no plan was written that day", "says no plan was written")

print("D4 Jordan rearranged")
d = by[D4]
ok(d["plan_held"] == 0.429, "4 of 7 stops and rides moved; got %r" % d["plan_held"])
ok(d["detail"]["plan_held"]["jordan_changes"] == 2, "counts his setting change and his new plan")
ok(d["rule_breaks"] == 0, "his 30-min farm is his own rule that day, not a break; got %r" % d["rule_breaks"])

print("D5 rules broken")
d = by[D5]
ok(d["rule_breaks"] == 1, "one written plan broke rules")
r = d["detail"]["rule_breaks"]["rules"]
ok("farm_too_short" in r and "gym_closed" in r and "planner_flagged" in r, "names them: %s" % r)

print("D6 rejected")
d = by[D6]
ok(d["plan_held"] == 0.0, "a proposal Jordan rejected held 0")
ok(d["rule_breaks"] is None, "nothing written, so no rules to check")

print("D7 override + unclean ride")
d = by[D7]
ok(d["rule_breaks"] == 0, "30-min farm is fine with Jordan's override that day")
ok(d["ride_est_miss_min"] is None and d["detail"]["ride_est_miss_min"]["not_clean"] == 1,
   "an unclean ride is skipped, not counted as a 35-min miss")

print("D8 plan, no arrivals")
d = by[D8]
ok(d["pin_hit_rate"] is None and d["why"]["pin_hit_rate"] == "no arrivals were recorded", "no arrivals: missing")
ok(d["plan_held"] == 1.0 and d["rule_breaks"] == 0, "the other scores still work")

print("D9 unsure arrival")
d = by[D9]
ok(d["pin_hit_rate"] is None and d["detail"]["pin_hit_rate"]["unsure"] == 1,
   "an arrival with no place saved is unsure, not a miss")

print("D10 weekend, left late")
d = by[D10]
ok(d["pin_hit_rate"] == 1.0, "farm and Planet Fitness both hit though he left 50 min late; got %r" % d["pin_hit_rate"])
ok(d["ride_est_miss_min"] == 3.0, "middle of 0 (home to farm) and 6 (farm to pf) is 3.0; got %r" % d["ride_est_miss_min"])
ok(d["arrival_miss_min"] == 50.5, "leaving 50 min late does count in arrival time (50 and 51); got %r"
   % d["arrival_miss_min"])
ok(d["detail"]["arrival_miss_min"]["late_or_early_min"] == [50.0, 51.0], "late shows as plus minutes")
ok(d["rule_breaks"] == 0, "Saturday hours for Planet Fitness: no break")

print("D11 gym and work rides that did get logged")
d = by[D11]
ok(d["pin_hit_rate"] == 1.0 and d["detail"]["pin_hit_rate"]["hits"] == 3,
   "farm (by leaving it), gym and work all count when a ride shows them; got %r" % d["detail"]["pin_hit_rate"])
ok(d["detail"]["pin_hit_rate"]["hits_by_leaving"] == 1, "the farm counts because he rode away from it")
ok(d["arrival_miss_min"] == 3.0, "gym 5 min early, work 1 min late: middle is 3.0; got %r" % d["arrival_miss_min"])
ok(d["ride_est_miss_min"] == 4.0, "middle of 8 (farm to gym) and 0 (gym to work)... got %r" % d["ride_est_miss_min"])

print("D12 her learned estimate beats yours")
d = by[D12]
ok(d["ride_est_wins"] == 1, "farm to gym: she planned 12, you said 20, it took 11: a plus; got %r" % d["ride_est_wins"])
ok(d["ride_est_miss_min"] == 1.0 and d["arrival_miss_min"] == 0.0, "1 min off on the ride, on time at the gym")
ok(by[D2]["ride_est_wins"] is None and "ride_est_wins" in by[D2]["missing"], "no rides scored: no plus, missing")

print("read-only")
ok(before == after, "no file in the folder changed")
empty = os.path.join(TMP, "empty")
os.makedirs(empty)
e = ps.score_day(D1, empty)
ok(os.listdir(empty) == [] and e["missing"] == list(ps.SCORES), "an empty folder: nothing created, all missing")
bad = os.path.join(TMP, "bad")
os.makedirs(bad)
for n in ("approvals", "places", "core", "scheduler"):
    with open(os.path.join(bad, n + ".db"), "w") as fh:
        fh.write("not a database")
e = ps.score_day(D1, bad)
ok(e["missing"] == list(ps.SCORES), "broken files: all missing, no crash")
try:
    con = sqlite3.connect("file:%s?mode=ro" % os.path.join(RIVIA, "places.db"), uri=True)
    con.execute("insert into rides(ride_id) values('x')")
    wrote = True
except sqlite3.OperationalError:
    wrote = False
ok(not wrote, "the way it opens files refuses writes")
src = open(os.path.join(HERE, "rivia_plan_score.py")).read().lower()
code = "\n".join(l.split("#")[0] for l in src.splitlines())
ok(not any(w in code for w in ("insert ", "update ", "delete ", "create table", "drop ", "alter ")),
   "no write statement in the code")
ok(not any(w in code for w in ("emotion.db", "body.db", "auth.db", ".rivia", "import rivia", "requests", "urllib",
                               "socket")), "never names her emotion, body or sign-in files, Rivia modules or the network")
ok("scared" not in src and "fear" not in src, "no fear words")

shutil.rmtree(TMP)
print("\n%d checks, %d failed" % (checks, failed))
sys.exit(1 if failed else 0)
