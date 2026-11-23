"""Emotion spec Step 6, all pieces together in shadow. Fake clock, throwaway RIVIA_DIR, never touches ~/rivia.
  1. Step 6 judges each day Step 5 scored, once; no plan_days table yet is fine
  2. a quiet good day is recorded at half; a day you thumbed isn't quiet
  3. her ride misses lower that area, and a flag goes on (logged) once it's under 0.70
  4. trust and satisfaction stay separate; nothing is ready on the code's lines after a few days
  5. with lower lines (from the app) she'd bring an unlock up, logged at most once a week
  6. an unlock you switched on pauses itself when trust falls, logged
  7. the off switch: every unlock killed, trust starts over from that moment
  8. the collector runs it after Step 5, can switch it off, keeps collecting when it breaks; body.db only
  9. refuses emotion.db; no fear words in anything it logs
    python test_trust_shadow.py
"""
import json, os, sqlite3, sys, tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
RIVIA = os.path.join(TMP, "rivia")
os.makedirs(RIVIA, exist_ok=True)
os.environ.update(HOME=TMP, RIVIA_DIR=RIVIA, RIVIA_HOME=RIVIA)
for k in ("RIVIA_BODY_SHADOW", "RIVIA_PLAN_SHADOW", "RIVIA_TRUST_SHADOW", "RIVIA_TRUST_KILL"):
    os.environ.pop(k, None)
sys.path.insert(0, HERE)

import rivia_time as rt  # noqa: E402
import rivia_body_collect as bc  # noqa: E402
import rivia_plan_score as ps  # noqa: E402
import rivia_progress as pr  # noqa: E402
import rivia_satisfaction as sat  # noqa: E402
import rivia_trust as trust  # noqa: E402
import rivia_trust_settings as tset  # noqa: E402
import rivia_trust_shadow as ts  # noqa: E402

TZ = ps.TZ
MIN, HOUR, DAY = 60, 3600, 86400
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
for name, schema in SCHEMAS.items():
    c = sqlite3.connect(os.path.join(RIVIA, name + ".db"))
    c.executescript(schema)
    c.commit()
    c.close()


def db(name, sql, args=()):
    c = sqlite3.connect(os.path.join(RIVIA, name + ".db"))
    c.execute(sql, args)
    c.commit()
    c.close()


def when(s):
    return {"epoch": s, "local": "", "iso": iso(s)}


def farm_day(day):
    b = lambda title, place, kind, s, e: {"title": title, "place": place, "kind": kind, "start": when(s),  # noqa
                                          "end": when(e)}
    blocks = [b("Home flex", "home", "detail", t(day, "10:00"), t(day, "11:05")),
              b("Ride", None, "ride", t(day, "11:05"), t(day, "11:10")),
              b("Farm", "farm", "stop", t(day, "11:10"), t(day, "11:55")),
              b("Ride", None, "ride", t(day, "11:55"), t(day, "12:00")),
              b("Home", "home", "stop", t(day, "12:00"), t(day, "13:00"))]
    pv = {"kind": "write_day", "day": day, "status": "planned", "blocks": blocks, "problems": []}
    db("approvals", "insert into changes(change_id, created, door, kind, scope, day, key, preview, status, decided) "
       "values(?,?,?,?,?,?,?,?,?,?)", ("chg_" + day, iso(t(day, "05:30")), "system", "write_day", "day", day,
                                       "write_day", json.dumps(pv), "applied", iso(t(day, "05:31"))))


def ride(rid, frm, to, dep, arr):
    db("places", "insert into rides(ride_id, day, from_place, to_place, departed, arrived, minutes, used) "
       "values(?,?,?,?,?,?,?,?)", (rid, iso(dep)[:10], frm, to, iso(dep), iso(arr), round((arr - dep) / 6) / 10, 1))
    db("core", "insert into calls(ts, door, tool, action, ok, code, ms) values(?,?,?,?,?,?,?)",
       (iso(arr), "app", "app_arrival", "sched.arrival", 1, None, 10))


def good(day):
    farm_day(day)
    ride("g1" + day, "home", "farm", t(day, "11:05"), t(day, "11:11"))
    ride("g2" + day, "farm", "home", t(day, "11:55"), t(day, "12:01"))


def slow(day):
    """Her ride estimate 5 min, the ride takes 20: her miss, both ways."""
    farm_day(day)
    ride("s1" + day, "home", "farm", t(day, "11:05"), t(day, "11:25"))
    ride("s2" + day, "farm", "home", t(day, "11:55"), t(day, "12:15"))


con = bc.db_open()

print("1. judged once, after Step 5")
ok(ts.shadow_tick(sqlite3.connect(":memory:"), t("2026-12-01", "12:00"))["judged"] == [],
   "no plan_days table yet (Step 5 never ran): nothing judged, no crash")
A, B, C = "2026-12-01", "2026-12-02", "2026-12-03"
good(A)
slow(B)
good(C)
for d in (A, B, C):
    pr.score_and_store(con, d, now=t(d, "23:59") + 13 * HOUR, folder=RIVIA)
sat.add(con, "thumb", 1, door="app_button", day=C, now=t(C, "18:00"), message_id="m-c", text="Nice")
T1 = t("2026-12-04", "13:00")
r = ts.shadow_tick(con, T1)
ok(r["judged"] == [A, B, C], "the three scored days judged, oldest first: %r" % r["judged"])
ok(ts.shadow_tick(con, T1 + 60)["judged"] == [], "and never judged twice")
days = {d["day"]: d for d in ts.recent_days(con)}
ok(days[A]["summary"]["hits"] == 7, "the good day: 7 hits")
ok(days[B]["summary"]["counted_misses"] == 2 and days[B]["summary"]["same_mistake"] == 2,
   "the slow day: 2 ride misses counted, the 2 late arrivals from them not counted again")
ok(all(v["why"] for v in ts.day_detail(con, B)), "every judgment has a plain reason")

print("2. quiet days")
ok(days[A]["quiet"] and not days[B]["quiet"] and not days[C]["quiet"],
   "A quiet-good; B had misses; C you gave a thumbs up (your words count instead)")
ok(r["satisfaction"]["counts"] == {"thumb_up": 1, "thumb_down": 0, "words_good": 0, "words_wrong": 0, "quiet": 1},
   "satisfaction saw one thumb and one quiet day")
ok(abs(r["satisfaction"]["good"] - 1.5) < 0.05, "the quiet day counted half: %s" % r["satisfaction"]["good"])

print("3. sure by area, flags")
ok(abs(r["confidence"]["ride:farm|home"]["value"] - 0.85) < 0.01, "one bad ride day: that route to 0.85")
ok(set(r["confidence"]) == {"ride:farm|home"}, "only that area moved")
ok(r["flags"] == [], "not flagged yet")
D, E = "2026-12-04", "2026-12-05"
slow(D)
slow(E)
for d in (D, E):
    pr.score_and_store(con, d, now=t(d, "23:59") + 13 * HOUR, folder=RIVIA)
T2 = t("2026-12-06", "13:00")
r = ts.shadow_tick(con, T2)
ok([f["area"] for f in r["flags"]] == ["ride:farm|home"], "three bad ride days: flagged: %r" % r["flags"])
ok(r["caution"] == ["verify before answering about %s" % r["flags"][0]["say"][len("double-check "):]],
   "caution line built, logged only")
ok(any(a["kind"] == "flag_on" for a in trust.actions(con, 50)), "the flag going on is in the log, with why")

print("4. two separate gates")
ok(r["trust"]["value"] < 0.6 and r["trust"]["days"] == 5, "five days: trust %.2f, nowhere near an unlock"
   % r["trust"]["value"])
ok(all(u["state"] == "earning" for u in r["ladder"].values()), "nothing ready on the code's lines")
ok(not any(a["kind"] == "would_bring_up" for a in trust.actions(con, 50)), "so she'd bring nothing up")

print("5. ready, with lower lines from your app")
for d in ("2026-12-06", "2026-12-07"):
    good(d)
    pr.score_and_store(con, d, now=t(d, "23:59") + 13 * HOUR, folder=RIVIA)
T3 = t("2026-12-08", "13:00")
tset.save({"unlock.speak_up.trust": 0.30, "unlock.speak_up.satisfaction": 0.5, "unlock.speak_up.days": 7},
          door="app_button", note="test")
r = ts.shadow_tick(con, T3 + 60)
ok(r["ladder"]["speak_up"]["state"] == "ready", "speak_up ready: %r" % r["ladder"]["speak_up"])
ups = trust.actions(con, 50, kind="would_bring_up")
ok(len(ups) == 1 and ups[0]["reason"].endswith("?"), "she'd bring it up, as a question: %s" % ups[0]["reason"])
ts.shadow_tick(con, T3 + DAY)
ok(len(trust.actions(con, 50, kind="would_bring_up")) == 1, "not again the next day (once a week)")
ts.shadow_tick(con, T3 + 8 * DAY)
ok(len(trust.actions(con, 50, kind="would_bring_up")) == 2, "again a week later, if it's still met")

print("6. paused by itself")
trust.set_unlock("speak_up", True, door="app_button", now=T3 + 8 * DAY + 60, con=con)
r = ts.shadow_tick(con, T3 + 8 * DAY + 120)
ok(r["ladder"]["speak_up"]["state"] == "active", "you switched it on: active")
tset.save({"unlock.speak_up.trust": 0.95}, door="app_button")
r = ts.shadow_tick(con, T3 + 8 * DAY + 180)
ok(r["ladder"]["speak_up"]["state"] == "paused", "under its trust line now: paused")
ok(trust.actions(con, 1, kind="paused")[0]["what"] == "speak_up", "the pause is logged with why")
tset.save({"unlock.speak_up.trust": 0.30}, door="app_button")
r = ts.shadow_tick(con, T3 + 8 * DAY + 240)
ok(r["ladder"]["speak_up"]["state"] == "active" and trust.actions(con, 1, kind="resumed"), "and resumes, logged")

print("7. the off switch")
K = T3 + 9 * DAY
trust.set_kill(True, door="app_button", now=K, con=con)
r = ts.shadow_tick(con, K + 60)
ok(r["killed"] and all(u["state"] == "killed" for u in r["ladder"].values()), "every unlock killed")
ok(r["trust"]["value"] == 0.2 and r["trust"]["predictions"] == 0, "trust starts over at 0.20")
ok(r["confidence"] == {} and r["satisfaction"]["value"] == 0.5, "areas and satisfaction start over too")
ok(con.execute("select count(*) from trust_predictions").fetchone()[0] > 0, "earlier judgments are kept, not deleted")
trust.set_kill(False, door="app_button", now=K + 120, con=con)
ok(not ts.shadow_tick(con, K + 180)["killed"], "cleared: unlocks can come back once their lines are met again")
n = con.execute("select count(*) from trust_state").fetchone()[0]
ts.shadow_tick(con, K + 240)
ok(con.execute("select count(*) from trust_state").fetchone()[0] == n, "no change: no new state row")
ts.shadow_tick(con, K + 240 + HOUR)
ok(con.execute("select count(*) from trust_state").fetchone()[0] == n + 1, "but one an hour anyway")

print("8. the collector")
HEALTHY = {"response_time": 200.0, "temperature": 30.0, "memory_used": 55.0, "storage_free": 60.0, "battery": 100.0,
           "error_rate": 0.0, "tool_failure_rate": 0.0, "failed_connections": 0.0, "tunnel_unreachable": 0.0,
           "restarts": 0.0}
fake_read = lambda cursor=None, window_s=None: (dict(HEALTHY), {}, None)  # noqa: E731
F = "2026-12-20"
good("2026-12-19")
now = t(F, "13:00")
rt.now_s = lambda: now
before = set(os.listdir(RIVIA))
out = bc.collect_once(con=con, now=now, read=fake_read)
ok(out["ok"] and "plan" in out and "trust" in out and "trust_error" not in out, "one tick: dials, plan, then trust")
ok("2026-12-19" in out["trust"]["judged"], "the day Step 5 scored this tick was judged in the same tick")
ok(set(os.listdir(RIVIA)) - before <= {"body.db", "body.db-wal", "body.db-shm"}, "it wrote nothing but body.db")
os.environ["RIVIA_TRUST_SHADOW"] = "0"
out = bc.collect_once(con=con, now=now + 60, read=fake_read)
ok(out["ok"] and "trust" not in out and "plan" in out, "RIVIA_TRUST_SHADOW=0 turns Step 6 off; the rest goes on")
os.environ.pop("RIVIA_TRUST_SHADOW")
real = ts.shadow_tick
ts.shadow_tick = lambda *a, **k: 1 / 0
out = bc.collect_once(con=con, now=now + 120, read=fake_read)
ts.shadow_tick = real
ok(out["ok"] and out.get("trust_error") == "ZeroDivisionError" and "plan" in out, "a broken Step 6 tick is noted; "
   "the reading, dials and plan still land")

print("9. guards")
e = sqlite3.connect(os.path.join(TMP, "emotion.db"))
try:
    ts.shadow_tick(e, now)
    ok(False, "refuse emotion.db")
except ValueError:
    ok(True, "refuses an emotion.db connection")
ok(not os.path.exists(os.path.join(RIVIA, "emotion.db")), "no emotion.db in the Rivia folder")
logged = json.dumps(trust.actions(con, 500)).lower() + json.dumps(ts.last_state(con)).lower()
ok(not any(w in logged for w in ("fear", "scared", "afraid")), "no fear words in anything logged")

print("\n%d checks passed" % COUNT["n"])
