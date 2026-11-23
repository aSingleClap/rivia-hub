"""Emotion spec Step 6.6 to 6.8: trust, the unlock ladder, the off switch and the action log. Throwaway RIVIA_DIR.
  1. trust starts low (0.20), climbs slowly on her hits, falls on her misses; yours and chance don't count
  2. rules weigh most, old days fade, the off switch's fresh start ignores earlier days
  3. the ladder: ready only when trust, satisfaction and days are all met; you switch on by hand; paused by itself
  4. hard limits can't be unlocked, even from your app; only your app's button switches unlocks
  5. the off switch: app button or tablet terminal only; a broken file reads as ON; every unlock reads killed
  6. every unlock, kill and pause is in the action log with its reason; no tool module imports Step 6
  7. no fear words; refuses emotion.db
    python test_trust.py
"""
import glob, json, os, sqlite3, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ.update(HOME=TMP, RIVIA_DIR=os.path.join(TMP, "rivia"))
os.environ.pop("RIVIA_TRUST_KILL", None)
sys.path.insert(0, HERE)
import rivia_trust as tr  # noqa: E402

COUNT = {"n": 0}
DAY = 86400
NOW = 1_800_000_000


def ok(cond, label):
    assert cond, label
    COUNT["n"] += 1
    print("  ok ·", label)


def v(kind, result, owner=None, counted=True, day="d1", at=NOW):
    return {"kind": kind, "result": result, "owner": owner if result == "miss" else None, "counted": counted,
            "day": day, "judged_at": at}


def raises(fn, code):
    try:
        fn()
    except tr.TrustError as e:
        return e.code == code
    return False


print("1. trust")
ok(tr.trust([], NOW)["value"] == 0.2, "no track record: starts low, 0.20")
week = [v("arrive", "hit", day="d%d" % i) for i in range(7)]
t7 = tr.trust(week, NOW)
ok(abs(t7["value"] - 11 / 27.0) < 0.001 and t7["days"] == 7,
   "a perfect week of one arrival a day: (7+4)/(7+20) = %.2f, still under the first unlock's 0.60" % t7["value"])
ok(tr.trust(week + [v("arrive", "miss", "rivia")], NOW)["value"] < t7["value"], "her miss lowers it")
ok(tr.trust(week + [v("arrive", "miss", "jordan"), v("arrive", "miss", "chance")], NOW)["value"] == t7["value"],
   "your misses and chance don't touch it")
ok(tr.trust(week + [v("arrive", "miss", "rivia", counted=False)], NOW)["value"] == t7["value"],
   "a same-mistake miss isn't counted twice")
ok(tr.trust(week + [v("arrive", "skip")], NOW)["predictions"] == 7, "skips don't count")

print("2. weights, fade, fresh start")
kept = tr.trust([v("rule", "hit")], NOW)
broke = tr.trust([v("rule", "miss", "rivia")], NOW)
ok(broke["misses"] == 2.0 and kept["worked"] == 0.25, "a broken rule costs 2, a kept one earns 0.25 (the planner "
   "checks rules itself)")
base = [v("arrive", "hit", day="d%d" % i) for i in range(20)]
b0 = tr.trust(base, NOW)["value"]
gain = tr.trust(base + [v("arrive", "hit")], NOW)["value"] - b0
loss = b0 - tr.trust(base + [v("arrive", "miss", "rivia")], NOW)["value"]
ok(loss > 2 * gain, "at %.2f one miss costs %.3f, over twice what one hit earns (%.3f): lost faster than won"
   % (b0, loss, gain))
old = tr.trust([v("arrive", "hit", at=NOW - 30 * DAY)], NOW)
ok(abs(old["worked"] - 0.5) < 0.001, "a hit a month old counts half")
ok(tr.trust(week, NOW, since=NOW + 1)["value"] == 0.2, "after the off switch, trust starts over")
ok(tr.trust([], NOW, tune={"prior_good": 8, "prior_bad": 12})["value"] == 0.4, "where trust starts is adjustable")

print("3. the ladder")
lo, hi = {"value": 0.3, "days": 3}, {"value": 0.9, "days": 40}
sat_lo, sat_hi = {"value": 0.5}, {"value": 0.9}
lad = tr.ladder(hi, sat_hi)
ok(all(u["state"] == "ready" for u in lad.values()), "everything met: she'd bring each up")
ok(tr.ladder(hi, sat_lo)["speak_up"]["state"] == "earning", "high trust but you're not satisfied: not ready")
ok(tr.ladder({"value": 0.9, "days": 5}, sat_hi)["speak_up"]["state"] == "earning", "too few days: not ready")
ok(tr.ladder(lo, sat_hi)["speak_up"]["state"] == "earning", "satisfied but low accuracy: not ready")
ok(tr.ladder(hi, sat_hi, on={"speak_up": {}})["speak_up"]["state"] == "active", "switched on and over its lines")
ok(tr.ladder(lo, sat_hi, on={"speak_up": {}})["speak_up"]["state"] == "paused", "falls under a line: pauses itself")
ok(tr.ladder(hi, sat_hi, lines={"speak_up": {"trust": 0.95}})["speak_up"]["state"] == "earning", "lines adjustable")
words = tr.bring_up_words("speak_up", hi, sat_hi)
ok(words.endswith("?") and "Want to switch this on in the app" in words, "she'd ask, never switch: %r" % words[:60])

print("4. unlocks: you, by hand")
con = sqlite3.connect(":memory:", isolation_level=None)
ok(raises(lambda: tr.set_unlock("speak_up", True, door="gemini"), "not_allowed"), "Gemini can't switch an unlock")
ok(raises(lambda: tr.set_unlock("speak_up", True, door="termux"), "not_allowed"), "nor the terminal (app only)")
for hard in tr.HARD_LIMITS:
    ok(raises(lambda h=hard: tr.set_unlock(h, True, door="app_button"), "hard_limit"),
       "%s can't be unlocked, even by you" % hard)
ok(raises(lambda: tr.set_unlock("anything", True, door="app_button"), "unknown"), "made-up unlocks refused")
ok(raises(lambda: tr.set_unlock("speak_up", "yes", door="app_button"), "bad_value"), "on must be true or false")
on = tr.set_unlock("speak_up", True, door="app_button", note="try it", now=NOW, con=con)
ok(list(on) == ["speak_up"] and list(tr.switched_on()[0]) == ["speak_up"], "you switched speak_up on")
tr.set_unlock("speak_up", False, door="app_button", now=NOW + 5, con=con)
ok(tr.switched_on()[0] == {}, "and off again")
with open(os.path.join(TMP, "rivia", tr.UNLOCK_FILE), "w") as f:
    f.write("{broken")
ok(tr.switched_on()[0] == {} and tr.switched_on()[1], "a broken unlock file means nothing is on")

print("5. the off switch")
ok(tr.kill_state()["killed"] is False, "off by default")
ok(raises(lambda: tr.set_kill(True, door="gemini"), "not_allowed"), "she can't reach the off switch")
k = tr.set_kill(True, door="termux", now=NOW, con=con)
ok(k["killed"] and k["since"] == NOW, "`rivia trust kill` on the tablet works and starts trust over from now")
ok(all(u["state"] == "killed" for u in tr.ladder(hi, sat_hi, on={"speak_up": {}}, killed=True).values()),
   "while it's on, every unlock reads killed")
k = tr.set_kill(False, door="app_button", now=NOW + 60, con=con)
ok(not k["killed"] and k["since"] == NOW, "clearing it keeps the fresh start")
os.environ["RIVIA_TRUST_KILL"] = "1"
ok(tr.kill_state()["killed"], "RIVIA_TRUST_KILL=1 also works")
os.environ.pop("RIVIA_TRUST_KILL")
with open(os.path.join(TMP, "rivia", tr.KILL_FILE), "w") as f:
    f.write("not json")
ok(tr.kill_state()["killed"], "a broken off-switch file reads as ON: when in doubt, no unlocks")

print("6. the log, and who imports this")
acts = tr.actions(con, 50)
kinds = [a["kind"] for a in acts]
ok({"unlock_on", "unlock_off", "killed", "unkilled"} <= set(kinds), "every switch is logged: %r" % kinds)
ok(all(a["reason"] and a["who"].startswith("jordan") for a in acts), "each with a reason and who did it")
ok(next(a for a in acts if a["kind"] == "unlock_on")["reason"] == "try it", "your note is the reason")
step6 = ("rivia_trust", "rivia_predict", "rivia_satisfaction", "rivia_area_confidence", "rivia_trust_shadow",
         "rivia_trust_settings", "rivia_trust_app")
for p in ("rivia_tools.py", "rivia_health_tool.py", "rivia_sched.py"):
    path = os.path.join(HERE, p)
    if os.path.exists(path):
        src = open(path).read()
        ok(not any("import %s" % m in src for m in step6), "%s (her tools) imports no Step 6 code" % p)

print("7. guards")
d = tempfile.mkdtemp()
try:
    tr.log_action(sqlite3.connect(os.path.join(d, "emotion.db")), "x", "y", "z", "code", NOW)
    ok(False, "emotion.db refused")
except ValueError:
    ok(True, "emotion.db is refused")
for f in glob.glob(os.path.join(HERE, "rivia_trust*.py")) + [os.path.join(HERE, "rivia_predict.py")]:
    s = open(f).read().lower()
    ok(not any(w in s for w in ("scared", "afraid", "fearful")), "%s: no fear words" % os.path.basename(f))
ok(json.dumps(tr.UNLOCKS), "the unlock list is plain data")

print("\n%d checks passed" % COUNT["n"])
