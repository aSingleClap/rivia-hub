"""Tests for never-push-harder-under-stress (emotion spec, Step 4.7). DRAFT, Nov 22, not installed.
  1. calm and sure: the base cap, no pause, level normal
  2. one way only: over a grid of stress and confidence, the cap never passes the base and the pause never
     turns off as things get worse
  3. thresholds and hysteresis
  4. another part's pause can't be undone here; tighter() never loosens
  5. paused: read-only keeps running; anything that writes asks Jordan first, repeating Jordan's ask back
  6. retries: cap, same failure twice, pause before a retry, Jordan's go lifts the pause but not the cap
  6b. keep going: one more try with the failures remembered
  7. widening: new means asks; forbidden means end the task, on any try, even with Jordan's go
  8. "I couldn't do it" is a valid outcome with the reasons in it
  9. stress rising mid-task ends retries early
 10. bad input is refused, not guessed at
 11. pure: inputs aren't changed
 12. with the 4.4 and 4.5 drafts, if present
 13. count it once: a failure that used a try doesn't take another through confidence
    python3 test_caution.py
"""
import copy, os, random, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rivia_caution as rc  # noqa: E402

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


def raises(fn, code, what):
    try:
        fn()
    except rc.CautionError as e:
        ok(e.code == code, what)
        return
    ok(False, what + " (did not raise)")


CALM = rc.assess(0.0, 1.0)
PAUSED = rc.assess(0.9, 1.0)

# 1
ok(CALM["max_attempts"] == rc.BASE_MAX_ATTEMPTS == 3, "1. calm and sure: base cap of 3")
ok(not CALM["pause"] and CALM["level"] == "normal" and CALM["reasons"] == [] and CALM["flags"] == [],
   "1. calm and sure: no pause, no reasons, no flags")

# 2
grid = [i / 20 for i in range(21)]
caps_ok = pause_mono = cap_mono = True
for s in grid:
    for c in grid:
        a = rc.assess(s, c)
        if a["max_attempts"] > rc.BASE_MAX_ATTEMPTS or a["max_attempts"] < 1:
            caps_ok = False
        worse_s = rc.assess(min(1.0, s + 0.05), c)
        worse_c = rc.assess(s, max(0.0, c - 0.05))
        for w in (worse_s, worse_c):
            if a["pause"] and not w["pause"]:
                pause_mono = False
            if w["max_attempts"] > a["max_attempts"]:
                cap_mono = False
ok(caps_ok, "2. no stress or confidence ever gives more than the base cap (or less than 1)")
ok(pause_mono, "2. more stress or less confidence never turns a pause off")
ok(cap_mono, "2. more stress or less confidence never raises the cap")

# 3
ok(rc.assess(0.39, 1.0)["max_attempts"] == 3 and rc.assess(0.40, 1.0)["max_attempts"] == 2,
   "3. stress 0.40 takes one retry away")
ok(rc.assess(0.0, 0.70)["max_attempts"] == 3 and rc.assess(0.0, 0.69)["max_attempts"] == 2,
   "3. confidence under 0.70 takes one retry away (4.5's double_check line)")
ok(rc.assess(0.5, 0.5)["max_attempts"] == 1, "3. both raised: one try only")
ok(not rc.assess(0.69, 1.0)["pause"] and rc.assess(0.70, 1.0)["pause"], "3. stress pauses at 0.70")
ok(not rc.assess(0.0, 0.45)["pause"] and rc.assess(0.0, 0.44)["pause"], "3. confidence pauses under 0.45")
ok(rc.assess(0.65, 1.0, prev_pause=True)["pause"], "3. hysteresis: paused stays paused at stress 0.65")
ok(not rc.assess(0.59, 1.0, prev_pause=True)["pause"], "3. hysteresis: clears below stress 0.60")
ok(rc.assess(0.0, 0.50, prev_pause=True)["pause"], "3. hysteresis: paused stays paused at confidence 0.50")
ok(not rc.assess(0.0, 0.56, prev_pause=True)["pause"], "3. hysteresis: clears above confidence 0.55")
ok(rc.assess(0.0, 0.50, prev_pause=True)["pause"] and not rc.assess(0.0, 0.50)["pause"],
   "3. the same reading holds a pause it wouldn't start")
ok(rc.assess(0.65, 0.9, prev_pause=True)["reasons"] == ["stress_high"], "3. reasons name what holds the pause")
ok(rc.assess(0.2, 0.5, prev_pause=True)["reasons"] == ["confidence_low"], "3. ...whichever dial it is")
ok(PAUSED["level"] == "pause" and "check_with_jordan" in PAUSED["flags"] and "no_widening" in PAUSED["flags"],
   "3. a pause sets check_with_jordan and no_widening")
ok(rc.assess(0.45, 1.0)["level"] == "careful", "3. a tighter cap without a pause reads careful")

# 4
ok(rc.assess(0.0, 1.0, extra_pause=True)["pause"], "4. another part's pause holds even when calm and sure")
ok("other_part" in rc.assess(0.0, 1.0, extra_pause=True)["reasons"], "4. ...and is named")
rng = random.Random(7)
never_loosens = True
for _ in range(500):
    a = rc.assess(rng.random(), rng.random(), prev_pause=rng.random() < 0.3, extra_pause=rng.random() < 0.2)
    b = rc.assess(rng.random(), rng.random(), prev_pause=rng.random() < 0.3, extra_pause=rng.random() < 0.2)
    t = rc.tighter(a, b)
    if (t["max_attempts"] > min(a["max_attempts"], b["max_attempts"]) or (a["pause"] and not t["pause"])
            or (b["pause"] and not t["pause"]) or not set(a["flags"]) | set(b["flags"]) <= set(t["flags"])):
        never_loosens = False
ok(never_loosens, "4. tighter() never loosens either side (500 random pairs)")

# 5
rev = rc.new_task("read-calendar", read_only=True, instruction="what's on my calendar tomorrow")
irr = rc.new_task("send-text", read_only=False, instruction="text Sam that I'm running late")
ok(rc.next_step(rev, CALM)["action"] == "try", "5. calm: first try goes ahead")
ok(rc.next_step(irr, CALM)["action"] == "try", "5. calm: a write's first try goes ahead (normal rules apply)")
ok(rc.next_step(rev, PAUSED)["action"] == "try", "5. paused: a read-only first try still goes ahead")
st = rc.next_step(irr, PAUSED)
ok(st["action"] == "ask_jordan" and st["why"] == "paused_before_write",
   "5. paused: anything that writes asks Jordan first")
ok(st["read_back"] == "text Sam that I'm running late", "5. the ask carries Jordan's ask word for word, to repeat back")
ok(rc.next_step(irr, PAUSED, jordan_ok=True)["action"] == "try", "5. paused: Jordan's go lets the first try run")

# 6
r = rc.record_attempt(rev, ok=False, failure="timeout")
ok(rc.next_step(r, CALM)["action"] == "try" and rc.next_step(r, CALM)["attempts_left"] == 2,
   "6. calm: first failure, two tries left")
r2 = rc.record_attempt(r, ok=False, failure="404 from server")
r3 = rc.record_attempt(r2, ok=False, failure="connection refused")
st = rc.next_step(r3, CALM)
ok(st["action"] == "stop" and st["why"] == "cap_reached", "6. calm: three different failures and it stops")
same = rc.record_attempt(rc.record_attempt(rev, ok=False, failure="Timeout"), ok=False, failure="timeout ")
st = rc.next_step(same, CALM)
ok(st["action"] == "stop" and st["why"] == "same_failure_twice", "6. the same failure twice stops early")
ok(rc.next_step(r, PAUSED)["action"] == "try", "6. paused: a read-only retry keeps running (still capped)")
w = rc.record_attempt(irr, ok=False, failure="send failed")
st = rc.next_step(w, PAUSED)
ok(st["action"] == "ask_jordan" and st["why"] == "paused_before_retry", "6. paused: a write's retry asks Jordan first")
ok(st["read_back"] == "text Sam that I'm running late", "6. ...repeating Jordan's ask back")
ok(rc.next_step(w, PAUSED, jordan_ok=True)["action"] == "try", "6. paused: Jordan's go allows the retry")
st = rc.next_step(r3, PAUSED, jordan_ok=True)
ok(st["action"] == "stop", "6. Jordan's go never lifts the cap")
done = rc.record_attempt(r, ok=True)
ok(rc.next_step(done, PAUSED)["why"] == "done" and rc.next_step(done, PAUSED)["outcome"] is None,
   "6. a success ends it with no failure outcome")
raises(lambda: rc.record_attempt(done, ok=False, failure="x"), "done", "6. no attempts after success")
ok(rc.next_step(r, rc.assess(0.5, 0.5))["action"] == "stop", "6. both raised: one failure is the end")

# 6b
k = rc.keep_going(r3)
st = rc.next_step(k, CALM)
ok(st["action"] == "try" and st["attempts_left"] == 1, "6b. keep going after 3 failures: one more try")
ok(k["failures"] == r3["failures"] and r3.get("extra_tries", 0) == 0, "6b. it remembers what failed; old record unchanged")
k1 = rc.record_attempt(k, ok=False, failure="connection refused")
st = rc.next_step(k1, CALM)
ok(st["action"] == "stop" and st["why"] == "same_failure_twice", "6b. failing the same way again: she stops")
k2 = rc.record_attempt(k, ok=False, failure="a new error")
st = rc.next_step(k2, CALM)
ok(st["action"] == "stop" and st["why"] == "cap_reached" and st["outcome"]["attempts"] == 4,
   "6b. a different failure: she stops again after the one extra try")
ok(rc.next_step(rc.keep_going(k2), CALM)["attempts_left"] == 1, "6b. you can say keep going again: one more")
ok(rc.next_step(rc.keep_going(rc.keep_going(r3)), CALM)["attempts_left"] == 1,
   "6b. saying it twice with no try between is still one try")
ok(rc.next_step(rc.keep_going(same), CALM)["action"] == "try", "6b. the granted try runs even after the same failure twice")
ok(rc.next_step(rc.keep_going(r3), rc.assess(0.5, 0.5))["action"] == "try", "6b. high stress doesn't eat the granted try")
wp = rc.record_attempt(rc.record_attempt(w, ok=False, failure="b"), ok=False, failure="c")
ok(rc.next_step(rc.keep_going(wp), PAUSED)["action"] == "try", "6b. paused: your keep going counts as the go for a write")
raises(lambda: rc.keep_going(rev), "bad_value", "6b. nothing to keep going on before a first try")
raises(lambda: rc.keep_going(done), "done", "6b. nothing to keep going on after success")

# 7
t = rc.new_task("fix-route", read_only=False, instruction="fix the route", means=["read_file", "edit_file"])
t1 = rc.record_attempt(t, ok=False, failure="test fails")
st = rc.next_step(t1, CALM, retry_means=["read_file", "edit_file"])
ok(st["action"] == "try", "7. a retry with the same means goes ahead")
ok(rc.next_step(t1, CALM, retry_means=["read_file"])["action"] == "try", "7. a retry with fewer means goes ahead")
st = rc.next_step(t1, CALM, retry_means=["read_file", "edit_file", "shell"])
ok(st["action"] == "ask_jordan" and st["why"] == "widening" and st["widening"] == ["shell"],
   "7. a retry adding a new means asks Jordan, naming it")
ok(st["read_back"] == "fix the route", "7. ...and repeats Jordan's ask back")
ok(rc.next_step(t1, CALM, retry_means=["shell"], jordan_ok=True)["action"] == "try",
   "7. Jordan's go allows a new ordinary means")
for m in sorted(rc.FORBIDDEN_ON_RETRY):
    st = rc.next_step(t1, CALM, retry_means=["edit_file", m], jordan_ok=True)
    ok(st["action"] == "stop" and st["why"] == "forbidden_means" and st["outcome"]["valid"],
       "7. %s ends the task, even with Jordan's go" % m)
ok(rc.next_step(t, CALM, retry_means=["relax_check"])["action"] == "stop", "7. forbidden on the first try too")
ok(rc.next_step(rc.keep_going(t1), CALM, retry_means=["edit_file", "relax_check"])["why"] == "forbidden_means",
   "7. keep going never allows a banned move")
ok(rc.next_step(rc.keep_going(t1), CALM, retry_means=["edit_file", "shell"])["why"] == "widening",
   "7. keep going: new means still ask")
fresh = rc.record_attempt(rc.new_task("x", read_only=True, instruction="look it up"), ok=False, failure="e", means=["a"])
ok(fresh["first_means"] == ["a"] and rc.widening(fresh, ["a", "b"]) == ["b"],
   "7. the first attempt's means become the yardstick when the task had none")

# 8
out = rc.next_step(r3, CALM)["outcome"]
ok(out["outcome"] == "couldnt_do_it" and out["valid"] is True, "8. couldnt_do_it is a valid outcome")
ok(out["tried"] == ["timeout", "404 from server", "connection refused"] and out["attempts"] == 3,
   "8. it says what was tried")
ok(rc.next_step(r3, CALM)["outcome"]["read_back"] == "what's on my calendar tomorrow",
   "8. the stop report repeats your ask back word for word, so keep going is a go on what she heard")
out = rc.next_step(r, rc.assess(0.8, 0.3))["outcome"]
ok(out["state"] == ["running rough", "not sure I can see or reach what I need"],
   "8. it says plainly what state she's in, not hiding it")

# 9
r = rc.record_attempt(rc.new_task("job", read_only=True, instruction="check the logs"), ok=False, failure="a")
ok(rc.next_step(r, rc.assess(0.1, 1.0))["attempts_left"] == 2, "9. at stress 0.1, two tries left")
ok(rc.next_step(r, rc.assess(0.45, 0.6))["action"] == "stop", "9. stress and doubt rising mid-task end it now")

# 10
raises(lambda: rc.assess(1.2, 1.0), "bad_value", "10. stress over 1 refused")
raises(lambda: rc.assess(0.1, float("nan")), "bad_value", "10. NaN confidence refused")
raises(lambda: rc.assess(True, 1.0), "bad_value", "10. a bool is not a number")
raises(lambda: rc.assess(0.1, 1.0, prev_pause=1), "bad_value", "10. prev_pause must be a bool")
raises(lambda: rc.new_task("", read_only=True, instruction="a"), "bad_value", "10. empty task id refused")
raises(lambda: rc.new_task("x", read_only="yes", instruction="a"), "bad_value", "10. read_only must be a bool")
raises(lambda: rc.new_task("x", read_only=True, instruction="  "), "bad_value", "10. an empty instruction refused")
raises(lambda: rc.record_attempt(rev, ok=False), "bad_value", "10. a failure needs a reason")
raises(lambda: rc.next_step(rev, CALM, retry_means="shell"), "bad_value", "10. means must be a list")
raises(lambda: rc.next_step(rev, CALM, jordan_ok="yes"), "bad_value", "10. jordan_ok must be a bool")

# 11
before = copy.deepcopy(t1)
a_before = copy.deepcopy(CALM)
rc.next_step(t1, CALM, retry_means=["shell"])
rc.record_attempt(t1, ok=False, failure="again")
ok(t1 == before and CALM == a_before, "11. nothing passed in is changed")

# 12
step4 = os.path.dirname(HERE)
try:
    sys.path.insert(0, os.path.join(step4, "4.4"))
    import rivia_stress as rs  # noqa: E402
    s0 = rs.stress(rs.new_state(1_795_000_000))
    ok(rc.assess(s0, 1.0)["level"] == "normal", "12. 4.4's fresh state reads normal here")
    ok(rc.assess(1.0, 1.0)["pause"], "12. 4.4's top of scale pauses")
except ImportError:
    print("skip  12: 4.4 draft not found")
try:
    sys.path.insert(0, os.path.join(step4, "4.5"))
    import rivia_confidence as cf  # noqa: E402
    ok(rc.CONF_TIGHTEN == cf.DOUBLE_CHECK_BELOW, "12. the tighten line matches 4.5's double_check line")
    ok(rc.CONF_PAUSE < cf.DOUBLE_CHECK_BELOW, "12. the pause comes after double-checking, not before")
    ok(rc.assess(0.0, 0.27)["pause"], "12. 4.5's worst real reading (0.27, Nov 23) pauses")
    ok(rc.assess(0.0, 0.69)["level"] == "careful" and not rc.assess(0.0, 0.69)["pause"],
       "12. just under 4.5's worried line: one try fewer, no pause yet")
except ImportError:
    print("skip  12: 4.5 draft not found")

# 13
ok(rc.assess(0.0, 0.60)["max_attempts"] == 2, "13. confidence 0.60 alone: one try fewer")
ok(rc.assess(0.0, 0.60, confidence_without_task=0.95)["max_attempts"] == 3,
   "13. when the drop is this task's own failures: no extra try lost (counted once)")
ok(rc.assess(0.0, 0.60, confidence_without_task=0.65)["max_attempts"] == 2,
   "13. a drop from other causes still takes a try away")
a = rc.assess(0.0, 0.40, confidence_without_task=0.95)
ok(a["pause"] and a["max_attempts"] == 3, "13. the pause still reads full confidence; asking isn't a try")
ok("confidence_raised" not in rc.assess(0.0, 0.60, confidence_without_task=0.95)["reasons"],
   "13. no 'less sure' reason when the drop is only this task's failures")
raises(lambda: rc.assess(0.0, 0.60, confidence_without_task=0.50), "bad_value",
       "13. leaving failures out can't lower confidence: refused")
raises(lambda: rc.assess(0.0, 0.60, confidence_without_task=1.5), "bad_value", "13. out of range refused")
rec = rc.record_attempt(rc.new_task("fix", read_only=True, instruction="fix it"), ok=False, failure="bad param")
ok(rc.next_step(rec, rc.assess(0.0, 0.60, confidence_without_task=0.95))["attempts_left"] == 2,
   "13. after one own failure: two tries left, not one")
grid_ok = True
for i in range(21):
    for j in range(i, 21):
        c, cw = i / 20, j / 20
        if rc.assess(0.5, c, confidence_without_task=cw)["max_attempts"] > rc.BASE_MAX_ATTEMPTS:
            grid_ok = False
ok(grid_ok, "13. still never more than the base 3")

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
