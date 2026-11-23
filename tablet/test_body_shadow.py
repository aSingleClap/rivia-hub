"""Emotion spec Step 4.9: the body layer in shadow mode. Fake clock, throwaway RIVIA_DIR, never touches ~/rivia.
  1. warm-up: no normal learned yet, so the dials stay calm and say what they can't judge
  2. slow and loaded for hours: stress rises, the pull grows slowly, reads annoyed at most
  3. things breaking (tunnel down, tool failures): confidence drops, stress doesn't (counted once)
  4. it never writes her emotion: no emotion.db, mood untouched, only body.db
  5. the collector runs it, can switch it off, and keeps collecting when it breaks
    python test_body_shadow.py
"""
import json, math, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
RIVIA = os.path.join(TMP, "rivia")
os.makedirs(RIVIA, exist_ok=True)
os.environ.update(HOME=TMP, RIVIA_DIR=RIVIA, RIVIA_HOME=RIVIA)
os.environ.pop("RIVIA_BODY_SHADOW", None)
sys.path.insert(0, HERE)

import rivia_time as rt  # noqa: E402
import rivia_normal as normal  # noqa: E402
import rivia_body_shadow as sh  # noqa: E402
import rivia_body_collect as bc  # noqa: E402
import rivia_emotion as emo  # noqa: E402

MIN, HOUR, DAY = 60, 3600, 86400
T = {"now": 1_795_000_000}
rt.now_s = lambda: T["now"]
COUNT = {"n": 0}
NEVER = {"fear", "distressed", "sadness", "anger"}


def ok(cond, label):
    assert cond, label
    COUNT["n"] += 1
    print("  ok ·", label)


HEALTHY = {"response_time": 200.0, "temperature": 30.0, "memory_used": 55.0, "storage_free": 60.0, "battery": 100.0,
           "error_rate": 0.0, "tool_failure_rate": 0.0, "failed_connections": 0.0, "tunnel_unreachable": 0.0,
           "restarts": 0.0}


def feed(con, values, seconds, step=5 * MIN, wobble=True):
    """Store readings every `step` seconds for `seconds`, then run one shadow tick per step. Returns the last row."""
    end = T["now"] + seconds
    row, i = None, 0
    while T["now"] < end:
        T["now"] += step
        i += 1
        w = (1 if i % 2 else -1) if wobble else 0
        rows = [(n, v + (w * 0.02 * v if v else 0.0), T["now"]) for n, v in values.items()]
        normal.record_many(rows, con=con)
        row = sh.shadow_tick(con, T["now"])
    return row


con = normal.db_open()

print("1. warm-up")
r = feed(con, HEALTHY, 1 * HOUR)
ok(r["stress"] == 0 and r["confidence"] == 1.0, "first hour: no normal yet, stress 0, confidence 1")
ok(set(r["unlearned"]) >= {"tool_failure_rate", "error_rate", "restarts"},
   "she lists what she can't judge yet (%s)" % ", ".join(r["unlearned"]))
ok(r["would_read"] == "neutral" and r["share"] == [0.0, 0.0, 0.0], "nothing would move")
ok(r["max_attempts"] == 3 and r["pause"] is False, "full tries, no pause")

print("2. slow and loaded")
feed(con, HEALTHY, 4 * DAY - HOUR, step=15 * MIN)
r = feed(con, HEALTHY, 1 * HOUR)
ok(r["stress"] == 0 and r["confidence"] == 1.0 and r["unlearned"] == [], "after 4 normal days: learned, calm")
slow = dict(HEALTHY, response_time=900.0, memory_used=92.0)
r1 = feed(con, slow, 15 * MIN)
ok(r1["stress"] > 0, "15 slow minutes: stress starts (%.3f)" % r1["stress"])
ok(math.hypot(*r1["share"]) <= 0.10 * 15 / 60 + 1e-6, "the pull grows no faster than 0.10 an hour (%.3f)"
   % math.hypot(*r1["share"]))
r6 = feed(con, slow, 6 * HOUR)
ok(r6["stress"] > r1["stress"], "6 slow hours: more stress (%.2f)" % r6["stress"])
ok(r6["stress_cause"] and "slow responses" in r6["stress_cause"], "cause names it: %s" % r6["stress_cause"])
ok(math.hypot(*r6["share"]) <= 0.25 + 1e-6, "never past 4.6's 0.25 cap (%.3f)" % math.hypot(*r6["share"]))
ok(r6["would_read"].split()[-1] in ("annoyance", "neutral") and r6["would_read"].split()[0] in ("faint", "neutral"),
   "slow running reads at most faint annoyance: %s" % r6["would_read"])
ok(r6["confidence"] == 1.0, "slowness alone doesn't touch confidence (counted once)")
rb = feed(con, HEALTHY, 3 * HOUR)
ok(math.hypot(*rb["share"]) < math.hypot(*r6["share"]), "fixed: the pull eases off")

print("3. things breaking")
feed(con, HEALTHY, 1 * DAY, step=15 * MIN)
broken = dict(HEALTHY, tunnel_unreachable=100.0, tool_failure_rate=40.0, failed_connections=20.0, error_rate=30.0)
r = feed(con, broken, 30 * MIN)
ok(r["confidence"] < 0.70 and r["double_check"], "tunnel down and tools failing: confidence %.2f, double-check on"
   % r["confidence"])
ok("tunnel_unreachable" in r["blind_spots"], "the tunnel is named as a blind spot")
ok(r["stress"] < 0.05, "breakage adds no stress (counted once, in confidence): %.3f" % r["stress"])
ok(r["max_attempts"] < 3, "fewer tries allowed while unsure (%d)" % r["max_attempts"])
r = feed(con, broken, 4 * HOUR)
name = r["would_read"].split()[-1]
ok(name in ("worried", "annoyance", "neutral") and name not in NEVER, "breakage reads %s, never scared" % r["would_read"])
one = dict(HEALTHY, tunnel_unreachable=100.0 / 15)
feed(con, HEALTHY, 1 * DAY, step=15 * MIN)
r = feed(con, HEALTHY, 15 * MIN, step=MIN)
r = feed(con, one, MIN, step=MIN)
ok(r["confidence"] > 0.95, "one failed tunnel check in 15 minutes costs a little, not the full weight (%.3f)"
   % r["confidence"])

print("4. never writes her emotion")
ok(not os.path.exists(os.path.join(RIVIA, "emotion.db")), "no emotion.db was created")
src = open(os.path.join(HERE, "rivia_body_shadow.py")).read()
for word in ("emo.nudge", "emo.set", "emo.db_open", "cap.tick", "write=True"):
    ok(word not in src, "the shadow code never calls %s" % word)
rows = sh.recent(3, con=con)
ok(len(rows) == 3 and rows[0]["at"] > rows[1]["at"], "recent() gives the newest rows first")
ok(set(rows[0]) >= {"stress", "confidence", "pause", "max_attempts", "share", "would_read"}, "every row has the dials")
n = con.execute("select count(*) from dials").fetchone()[0]
oldest = con.execute("select min(at) from dials").fetchone()[0]
ok(T["now"] - oldest <= sh.DIALS_KEEP_S, "rows older than a week are pruned (%d kept)" % n)

print("5. the collector")
fake = lambda cursor=None, window_s=None: (dict(HEALTHY), {}, None)
T["now"] += MIN
out = bc.collect_once(con=con, now=T["now"], read=fake)
ok(out["ok"] and "shadow" in out and out["shadow"]["at"] == T["now"], "one collector tick writes one shadow row")
os.environ["RIVIA_BODY_SHADOW"] = "0"
T["now"] += MIN
out = bc.collect_once(con=con, now=T["now"], read=fake)
ok(out["ok"] and "shadow" not in out, "RIVIA_BODY_SHADOW=0 turns the shadow dials off; readings still stored")
os.environ.pop("RIVIA_BODY_SHADOW")
saved = sh.shadow_tick
sh.shadow_tick = lambda c, now: (_ for _ in ()).throw(RuntimeError("boom"))
T["now"] += MIN
out = bc.collect_once(con=con, now=T["now"], read=fake)
ok(out["ok"] and out.get("shadow_error") == "RuntimeError" and out["stored"] > 0,
   "a broken shadow tick is noted and the readings are still stored")
sh.shadow_tick = saved
con.close()

print("\n%d checks passed" % COUNT["n"])
