"""Emotion spec Step 5.6: Step 5 in one piece, in shadow. Fake clock, throwaway RIVIA_DIR, never touches ~/rivia.
  1. outages: found from Step 4's readings (no reading for 5+ minutes, or the tunnel check failing); unknown when
     the readings don't cover the day
  2. counted once: a pin miss while the server was down is left out of the day's score and gap
  3. 5.1 -> 5.2: arrival time fills the rides slot; her estimate and the plus are logged, not weighted
  4. a day is scored once, about half a day after it ends; parked scheduling stores a hole, never a 0
  5. 5.3 -> 5.4: names lined up; flat and low reads content, flat and high reads focused, growing reads worried
  6. the collector runs it right after 4.9's dials, can switch it off, and keeps collecting when it breaks
  7. it never writes her emotion: no emotion.db, only body.db; refuses emotion.db; no fear words
  8. goals (5.5): no money cap by default, RIVIA_GOAL_COST_USD sets one, records and log rows kept in body.db
    python test_progress.py
"""
import json, os, sqlite3, sys, tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
RIVIA = os.path.join(TMP, "rivia")
os.makedirs(RIVIA, exist_ok=True)
os.environ.update(HOME=TMP, RIVIA_DIR=RIVIA, RIVIA_HOME=RIVIA)
for k in ("RIVIA_BODY_SHADOW", "RIVIA_PLAN_SHADOW", "RIVIA_GOAL_COST_USD"):
    os.environ.pop(k, None)
sys.path.insert(0, HERE)

import rivia_time as rt  # noqa: E402
import rivia_normal as normal  # noqa: E402
import rivia_body_collect as bc  # noqa: E402
import rivia_emotion as emo  # noqa: E402
import rivia_progress as pr  # noqa: E402
import rivia_plan_score as ps  # noqa: E402
import rivia_goal_limits as gl  # noqa: E402

TZ = ps.TZ
MIN, HOUR, DAY = 60, 3600, 86400
COUNT = {"n": 0}
BAD = {"fear", "distressed", "sadness", "anger"}


def ok(cond, label):
    assert cond, label
    COUNT["n"] += 1
    print("  ok ·", label)


def t(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return int(datetime.fromisoformat(day).replace(hour=h, minute=m, tzinfo=TZ).timestamp())


def iso(s):
    return datetime.fromtimestamp(s, TZ).isoformat()


# ---------- a tiny scheduler folder (the tablet's tables, as 5.1's test copies them) ----------
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
SCHED = os.path.join(TMP, "sched")
os.makedirs(SCHED)
for name, schema in SCHEMAS.items():
    c = sqlite3.connect(os.path.join(SCHED, name + ".db"))
    c.executescript(schema)
    c.commit()
    c.close()


def db(name, sql, args=()):
    c = sqlite3.connect(os.path.join(SCHED, name + ".db"))
    c.execute(sql, args)
    c.commit()
    c.close()


def when(s):
    return {"epoch": s, "local": "", "iso": iso(s)}


def farm_day(day):
    """home -> farm 11:10-11:55 -> home. Two expected arrivals: farm and home."""
    b = lambda title, place, kind, s, e: {"title": title, "place": place, "kind": kind, "start": when(s),
                                          "end": when(e)}
    blocks = [b("Home flex", "home", "detail", t(day, "10:00"), t(day, "11:05")),
              b("Ride: home to the farm", None, "ride", t(day, "11:05"), t(day, "11:10")),
              b("Farm", "farm", "stop", t(day, "11:10"), t(day, "11:55")),
              b("Ride: farm to home", None, "ride", t(day, "11:55"), t(day, "12:00")),
              b("Home", "home", "stop", t(day, "12:00"), t(day, "13:00"))]
    pv = {"kind": "write_day", "day": day, "status": "planned", "blocks": blocks, "problems": []}
    db("approvals", "insert into changes(change_id, created, door, kind, scope, day, key, preview, status, decided) "
       "values(?,?,?,?,?,?,?,?,?,?)", ("chg_" + day, iso(t(day, "05:30")), "system", "write_day", "day", day,
                                       "write_day", json.dumps(pv), "applied", iso(t(day, "05:31"))))


def ride(rid, frm, to, dep, arr):
    day = iso(dep)[:10]
    db("places", "insert into rides(ride_id, day, from_place, to_place, departed, arrived, minutes, used) "
       "values(?,?,?,?,?,?,?,?)", (rid, day, frm, to, iso(dep), iso(arr), round((arr - dep) / 6) / 10, 1))
    db("core", "insert into calls(ts, door, tool, action, ok, code, ms) values(?,?,?,?,?,?,?)",
       (iso(arr), "app", "app_arrival", "sched.arrival", 1, None, 10))


def reads(con, lo, hi, skip=(), tunnel_down=()):
    """One 4.2 reading a minute from lo to hi, leaving out the `skip` stretches (server down)."""
    at = lo
    while at <= hi:
        if not any(s <= at < e for s, e in skip):
            tun = 100.0 if any(s <= at < e for s, e in tunnel_down) else 0.0
            con.execute("insert or replace into reads values(?,?,?,?,?,?,?,?)",
                        (at, 5, 1, None, json.dumps({"tunnel_unreachable": tun}), "{}", 1, "[]"))
        at += 60


con = bc.db_open()  # body.db with 4.2's reads table

print("1. outages from Step 4's readings")
A, B = "2026-12-01", "2026-12-02"
lo, hi = t(A, "00:00") - pr.COVER_PAD_S, t(B, "00:00") + pr.COVER_PAD_S
reads(con, lo - HOUR, hi + HOUR, skip=[(t(A, "11:00"), t(A, "11:40"))], tunnel_down=[(t(A, "16:00"), t(A, "16:03"))])
w = pr.outage_windows(con, lo, hi, now=hi + DAY)
ok(any(s <= t(A, "11:00") and e >= t(A, "11:39") for s, e in w), "40 minutes with no reading is an outage")
ok(any(s <= t(A, "16:01") <= e for s, e in w), "a failed tunnel check is an outage (the phone couldn't get through)")
ok(len(w) == 2, "nothing else is: %r" % (w,))
ok(pr.outage_windows(con, lo - 5 * DAY, hi, now=hi + DAY) is None, "readings that don't cover the day: unknown")
empty = sqlite3.connect(":memory:")
ok(pr.outage_windows(empty, lo, hi, now=hi) is None, "no readings table at all: unknown, no crash")
short = pr.outage_windows(con, lo, hi + 2 * DAY, now=hi + 2 * DAY)
ok(short is not None and any(e == hi + 2 * DAY for s, e in short), "no reading since the last one: down since then")

print("2. counted once: a miss while the server was down is left out")
farm_day(A)
ride("a1", "home", "pf", t(A, "11:04"), t(A, "11:20"))   # went to pf, not the farm: a pin miss at farm time
ride("a2", "pf", "home", t(A, "11:56"), t(A, "12:12"))   # home arrival logged: a hit
row = pr.score_and_store(con, A, now=t(B, "13:00"), folder=SCHED)
pins = row["scores"]["detail"]["pin_hit_rate"]
ok(row["outage_info"] == "left out" and pins["outage"] == 1 and pins["misses"] == 0,
   "the farm miss fell inside the outage and was left out")
ok(row["scores"]["pin_hit_rate"] == 1.0, "pins read 1.0 from the home arrival alone")
plain = ps.score_day(A, SCHED)
ok(plain["pin_hit_rate"] == 0.5, "without the outage the same day reads 0.5 (one hit, one miss)")
ok(row["gap"] < pr.gapmod.daily_gap(plain)["gap"], "so the day's gap is smaller: the outage isn't her plan's fault")
ok(row["outage_minutes"] and row["outage_minutes"] > 40, "the outage minutes are on the day's row")

print("3. 5.1 -> 5.2 names")
gd = row["gap_detail"]
ok("arrival_miss_min" in gd["used"], "arrival time is one of the four weighted scores")
ok(set(gd["logged"]) == {"ride_est_miss_min", "ride_est_wins"}, "her estimate score and the plus are logged")
ok("ride_est_miss_min" not in gd["misses"] and "ride_est_wins" not in gd["misses"], "and not weighted")
ok(set(pr.gapmod.SCORES) <= set(ps.SCORES), "every weighted 5.2 score is one 5.1 makes")

print("4. scored once, at the right time; parked days stay holes")
con.execute("delete from plan_days")
C = "2026-12-03"
ok(pr.due_days(con, t(C, "11:59"))[-1] == A, "at 11:59 yesterday isn't due yet (the ride home may still come in)")
ok(pr.due_days(con, t(C, "12:00"))[-1] == B, "at noon it is")
ok(B not in pr.due_days(con, t(B, "23:00")), "today is never scored")
res = pr.shadow_tick(con, t(C, "12:30"), folder=SCHED)
ok(res["scored"] == ["2026-11-30", A, B], "three finished days scored, oldest first: %r" % res["scored"])
again = pr.shadow_tick(con, t(C, "12:31"), folder=SCHED)
ok(again["scored"] == [], "and never scored twice")
ok(dict(pr.gaps(con, t(C, "12:31")))[B] is None, "a day with no plan (scheduling parked) is a hole, not 0")
ok(res["trend"]["direction"] == "not_enough_data" and res["mood"]["share"] == [0.0, 0.0, 0.0],
   "too few scored days: no trend, no pull")
nofolder = sqlite3.connect(":memory:")
r0 = pr.shadow_tick(nofolder, t(C, "12:30"), folder=os.path.join(TMP, "nothing-here"))
ok(r0["trend"]["direction"] == "not_enough_data" and not os.path.exists(os.path.join(TMP, "nothing-here")),
   "no scheduler files at all: holes, nothing created")

print("5. 5.3 -> 5.4")
ok(pr.reading_for_mood({"direction": "flat", "strength": 0.0, "level": 0.1}, 5)["standing"] == "good", "flat low: good")
ok(pr.reading_for_mood({"direction": "flat", "strength": 0.0, "level": 0.4}, 5)["standing"] == "bad", "flat high: bad")
ok(pr.reading_for_mood({"direction": "growing", "strength": 0.5, "level": 0.4}, 5)["standing"] is None,
   "only flat gets a standing")


def run_week(gaps, body=(0.0, 0.0, 0.0), hours=6):
    m = sqlite3.connect(":memory:")
    m.executescript(pr.SCHEMA)
    days = ["2026-12-%02d" % d for d in range(1, len(gaps) + 1)]
    for d, g in zip(days, gaps):
        m.execute("insert into plan_days values(?,?,?,?,?,?,?,?)", (d, 0, "{}", g, "{}", "none", "[]", 0))
    now = t("2026-12-%02d" % (len(gaps) + 1), "13:00")
    out = None
    for i in range(hours * 4):  # 15-minute ticks
        out = pr.shadow_tick(m, now + i * 15 * MIN, body_share=body, folder=SCHED)
    return out


flat_good = run_week([0.10, 0.11, 0.09, 0.10, 0.11, 0.10, 0.10])
ok(flat_good["trend"]["direction"] == "flat" and flat_good["mood"]["kind"] == "flat_good", "steady and low: on track")
ok(flat_good["mood"]["would_read"] == "faint contentment", "reads faint content at most: %s"
   % flat_good["mood"]["would_read"])
ok(flat_good["mood"]["strength"] > 0.5, "steady days count as steady for 5.4 (5.3 gives flat no strength)")
flat_bad = run_week([0.45, 0.46, 0.44, 0.45, 0.46, 0.45, 0.45])
ok(flat_bad["mood"]["kind"] == "flat_bad" and flat_bad["mood"]["would_read"] in ("neutral", "faint focused"),
   "steady and high: off, focused at most faint: %s" % flat_bad["mood"]["would_read"])
noisy = run_week([0.10, 0.40, 0.12, 0.38, 0.11, 0.42, 0.13])
ok(noisy["trend"]["direction"] == "flat" and noisy["mood"]["strength"] < 0.5,
   "a day-to-day zigzag is not 'steady' (%.2f)" % noisy["mood"]["strength"])
ok(pr.reading_for_mood({"direction": "flat", "strength": 0.0, "level": 0.1, "slope_per_week": 0.0,
                        "days_used": ["d"] * 7}, 5)["strength"] == 1.0, "perfectly level week: fully steady")
ok(pr.reading_for_mood({"direction": "flat", "strength": 0.0, "level": 0.1, "slope_per_week": 0.06,
                        "days_used": ["d"] * 7}, 5)["strength"] == 0.0, "flat only because it's noisy: not steady")
growing = run_week([0.05, 0.10, 0.18, 0.25, 0.33, 0.40, 0.48])
ok(growing["trend"]["direction"] == "growing" and "worried" in growing["mood"]["would_read"],
   "misses growing: %s" % growing["mood"]["would_read"])
ok(growing["mood"]["share"][1] <= 1e-9, "concern adds no energy (never pushier)")
shrink = run_week([0.48, 0.40, 0.33, 0.25, 0.18, 0.10, 0.05])
ok(shrink["trend"]["direction"] == "shrinking" and "relief" in shrink["mood"]["would_read"],
   "misses shrinking: %s" % shrink["mood"]["would_read"])
worried_body = tuple(x * 0.25 for x in pr.mood._unit(pr.mood.DIRECTIONS["growing"]))
both = run_week([0.05, 0.10, 0.18, 0.25, 0.33, 0.40, 0.48], body=worried_body)
ok(both["mood"]["share"] == [0.0, 0.0, 0.0], "body already at its 0.25 ceiling the same way: the trend adds nothing")
for r in (flat_good, flat_bad, growing, shrink, both):
    ok(not any(b in r["mood"]["would_read"] or b in r["mood"]["with_body_reads"] for b in BAD),
       "no fear, distressed, sad or angry: %s / %s" % (r["mood"]["would_read"], r["mood"]["with_body_reads"]))

print("6. the collector runs it after the dials")
HEALTHY = {"response_time": 200.0, "temperature": 30.0, "memory_used": 55.0, "storage_free": 60.0, "battery": 100.0,
           "error_rate": 0.0, "tool_failure_rate": 0.0, "failed_connections": 0.0, "tunnel_unreachable": 0.0,
           "restarts": 0.0}


def fake_read(cursor=None, window_s=None):
    return dict(HEALTHY), {}, None


c2 = bc.db_open()
now = t("2026-12-10", "13:00")
rt.now_s = lambda: now
before = set(os.listdir(RIVIA))
out = bc.collect_once(con=c2, now=now, read=fake_read)
ok(out["ok"] and "shadow" in out and "plan" in out and "plan_error" not in out, "one tick: dials, then the plan")
ok(c2.execute("select count(*) from trend_mood where at = ?", (now,)).fetchone()[0] == 1, "trend row in body.db")
tm_row = c2.execute("select body_share from trend_mood where at = ?", (now,)).fetchone()[0]
ok(json.loads(tm_row) == [round(x, 4) for x in out["shadow"]["share"]], "it got this tick's body pull (counted once)")
ok(set(os.listdir(RIVIA)) - before <= {"body.db", "body.db-wal", "body.db-shm"}, "it wrote nothing but body.db")
ok(not os.path.exists(os.path.join(RIVIA, "emotion.db")), "no emotion.db")
os.environ["RIVIA_PLAN_SHADOW"] = "0"
out = bc.collect_once(con=c2, now=now + 60, read=fake_read)
ok(out["ok"] and "plan" not in out, "RIVIA_PLAN_SHADOW=0 turns it off; collecting goes on")
os.environ.pop("RIVIA_PLAN_SHADOW")
real_tick = pr.shadow_tick
pr.shadow_tick = lambda *a, **k: 1 / 0
out = bc.collect_once(con=c2, now=now + 120, read=fake_read)
pr.shadow_tick = real_tick
ok(out["ok"] and out.get("plan_error") == "ZeroDivisionError" and out.get("shadow"),
   "a broken plan tick is noted; the reading and the dials still land")
ok(c2.execute("select count(*) from reads where at = ?", (now + 120,)).fetchone()[0] == 1, "the reading was stored")

print("7. never her emotion")
e = sqlite3.connect(os.path.join(TMP, "emotion.db"))
try:
    pr.shadow_tick(e, now)
    ok(False, "should refuse emotion.db")
except ValueError:
    ok(True, "refuses an emotion.db connection")
ok(e.execute("select count(*) from sqlite_master").fetchone()[0] == 0, "and wrote nothing there")
src = open(os.path.join(HERE, "rivia_progress.py")).read()
code = "\n".join(l.split("#")[0] for l in src.splitlines())
import re  # noqa: E402
ok("sqlite3" not in code and not re.search(r"(?<![\w.])open\(", code), "opens no file itself")
ok(not any(w in code for w in ("rivia_google", "rivia_gcal", "rivia_auth", "rivia_applogin", "rivia_tools",
                               "rivia_admin", "urllib", "requests", "socket", ".rivia")),
   "no sign-in, Google, tool or network module")
ok("scared" not in src.lower() and "fear" not in src.lower().replace("no fear", ""), "no fear words")

print("8. goals (5.5)")
g = pr.start_goal(c2, "g1", "plan_run", instruction="plan tomorrow", stop_when={"kind": "check_passes",
                  "check": "rules_ok"}, now=now)
ok(g["caps"]["cost_usd"] is None, "no money cap by default (Jordan hasn't decided)")
ok(pr.load_goal(c2, "g1")["fingerprint"] == g["fingerprint"], "the goal record is kept in body.db")
os.environ["RIVIA_GOAL_COST_USD"] = "0.50"
g2 = pr.start_goal(c2, "g2", "plan_run", instruction="plan tomorrow", stop_when={"kind": "check_passes",
                   "check": "rules_ok"}, now=now)
ok(g2["caps"]["cost_usd"] == 0.5, "the setting adds a money cap")
for bad in ("lots", "-1", "nan", "0"):
    os.environ["RIVIA_GOAL_COST_USD"] = bad
    ok(pr.cost_cap() is None, "a bad setting %r means no cap, never a guess" % bad)
os.environ["RIVIA_GOAL_COST_USD"] = "99"
ok(pr.cost_cap() == gl.HARD_CEILING["cost_usd"], "never past 5.5's hard ceiling")
os.environ.pop("RIVIA_GOAL_COST_USD")
lr = pr.log_goal(c2, g, {"action": "stop", "why": "time_cap", "outcome": {"why": "ran out of time for this one"}},
                 now + 200)
ok(lr["read_back"] == "plan tomorrow" and c2.execute("select count(*) from goal_log").fetchone()[0] == 1,
   "a goal log row is kept, with Jordan's words read back")
try:
    gl.new_goal("g3", "plan_run", instruction="move my day", stop_when={"kind": "check_passes", "check": "x"},
                now=now, read_only=False)
    ok(False, "a writing goal should be refused")
except gl.GoalError as ge:
    ok(ge.code == "report_only", "a goal that would change anything is still refused (report only)")

print("\ntest_progress: %d checks passed" % COUNT["n"])
