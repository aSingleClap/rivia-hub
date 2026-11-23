"""Tests for goal limits (emotion spec, Step 5.5). DRAFT, Nov 23, not installed.
  1. every goal needs a stop condition; a goal that would write is refused (report only)
  2. limits: defaults, tighter always ok, looser only from Jordan, never past the hard ceiling
  3. done: the finish line met ends the goal as done, not as a failure
  4. time cap: she stops with "I couldn't do it, here's why", repeating Jordan's words
  5. budget: tokens, model calls, tool calls, money; a next try that won't fit stops before it starts
  6. tries come from 4.7, unchanged: cap, same failure twice, stress lowers the cap, banned moves
  7. rate guard: 10 model calls a minute means wait, not stop
  8. keep going: one more try and one try's share of the used-up limit, once per stop, failures remembered
  9. nothing can quietly change the goal or its finish line; a "worked" try without the finish line stops
 10. Jordan's stop wins and isn't a failure
 11. no fear in any word she'd say; no file here writes, imports Gemini or touches sign-in
 12. pure: inputs aren't changed; bad input is refused
 13. measure mode (the default for now): token and call limits are logged, not enforced; real averages come out
    python3 test_goal_limits.py
"""
import copy, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = next((d for d in (os.path.join(HERE, "..", "..", "step4", "4.9", "merged"), HERE)
            if os.path.exists(os.path.join(d, "rivia_emotion.py"))), HERE)
sys.path[:0] = [HERE, BASE]

import ast  # noqa: E402
import rivia_caution as rc  # noqa: E402
import rivia_goal_limits as gl  # noqa: E402


def _never_labels():
    # Read straight from the merged rivia_emotion.py: importing it needs the tablet's rivia_store.
    tree = ast.parse(open(os.path.join(BASE, "rivia_emotion.py")).read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "NEVER_LABELS" for t in node.targets):
            return ast.literal_eval(node.value)
    raise SystemExit("NEVER_LABELS not found in the merged rivia_emotion.py")

failed = passed = 0


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
    except gl.GoalError as e:
        ok(e.code == code, what + " (%s)" % e.code)
        return
    ok(False, what + " (did not raise)")


T0 = 1_795_000_000.0
CALM = rc.assess(0.0, 1.0)
STRAINED = rc.assess(0.5, 0.5)      # cap 1
PAUSED = rc.assess(0.9, 1.0)
ASK = "plan my day tomorrow"
RULES = {"kind": "check_passes", "check": "rules_ok"}
GAP = {"kind": "gap_at_most", "target": 5.0}


def plan(**kw):
    args = dict(instruction=ASK, stop_when=RULES, now=T0)
    args.update(kw)
    return gl.new_goal("plan-1", "plan_run", **args)


# 1
raises(lambda: gl.new_goal("g", "plan_run", instruction=ASK, stop_when=None, now=T0), "no_stop_condition",
       "1. a goal with no stop condition is refused")
raises(lambda: gl.new_goal("g", "plan_run", instruction=ASK, stop_when={"kind": "when_it_feels_done"}, now=T0),
       "no_stop_condition", "1. a made-up stop condition is refused")
raises(lambda: plan(read_only=False), "report_only", "1. Step 5 is report only: a goal that writes is refused")
raises(lambda: gl.new_goal("g", "fix_server", instruction=ASK, stop_when=RULES, now=T0), "bad_value",
       "1. an unknown kind of goal is refused")
raises(lambda: plan(instruction="  "), "bad_value", "1. a goal needs Jordan's words (4.7)")
g = plan()
ok(g["task"]["instruction"] == ASK and g["task"]["read_only"] is True, "1. the goal carries Jordan's words, read-only")

# 2
ok(g["caps"] == gl.DEFAULT_LIMITS["plan_run"], "2. no limits given: the defaults for the kind")
ok(all(v["cost_usd"] is None for v in gl.DEFAULT_LIMITS.values()), "2. no money cap by default (like the baseline runs)")
ok(all(all(v[k] for k in ("time_s", "tokens", "model_calls", "tool_calls")) for v in gl.DEFAULT_LIMITS.values()),
   "2. every kind has a time, token, model-call and tool-call cap")
ok(plan(limits={"tokens": 10_000})["caps"]["tokens"] == 10_000, "2. tighter than the default is always fine")
ok(plan(limits={"cost_usd": 0.05})["caps"]["cost_usd"] == 0.05, "2. adding a money cap counts as tighter")
raises(lambda: plan(limits={"tokens": 90_000}), "needs_jordan", "2. looser than the default needs Jordan")
raises(lambda: plan(limits={"time_s": None}), "bad_value", "2. a time cap can't be removed")
ok(plan(limits={"tokens": 90_000}, from_jordan=True)["caps"]["tokens"] == 90_000, "2. ...and Jordan can set it")
raises(lambda: plan(limits={"tokens": 2_000_000}, from_jordan=True), "over_ceiling",
       "2. not even Jordan's setting passes the hard ceiling")
raises(lambda: plan(limits={"tokens": 0}), "bad_value", "2. a zero limit is refused")
raises(lambda: plan(limits={"retries": 9}), "bad_value", "2. retries aren't set here: 4.7 owns the try limit")
t = gl.tighten(g, tokens=1000, time_s=60)
ok(t["caps"]["tokens"] == 1000 and t["deadline"] == T0 + 60, "2. tighten() lowers limits mid-way")
raises(lambda: gl.tighten(g, tokens=99_999), "looser", "2. tighten() never raises one")

# 3
gg = gl.new_goal("g2", "question", instruction="why is the gym pin off", stop_when=GAP, now=T0)
ok(gl.check(gg, CALM, now=T0 + 1, gap=12.0)["action"] == "try", "3. gap above the target: keep at it")
d = gl.check(gg, CALM, now=T0 + 1, gap=4.0)
ok(d["action"] == "done" and d["outcome"]["outcome"] == "met", "3. gap at or under the target: done")
d = gl.check(g, CALM, now=T0 + 1, passed=["rules_ok"])
ok(d["action"] == "done", "3. the named check passed: done")
ok(gl.check(g, CALM, now=T0 + 1, passed=["something_else"])["action"] == "try", "3. another check passing isn't it")
late = gl.record_usage(g, now=T0 + 500, tokens=999_999)
ok(gl.check(late, CALM, now=T0 + 500, passed=["rules_ok"])["action"] == "done",
   "3. met beats over-limit: if the finish line is met, it's done")

# 4
d = gl.check(g, CALM, now=T0 + 120)
o = d["outcome"]
ok(d["action"] == "stop" and d["why"] == "time_cap", "4. at the time cap she stops")
ok(o["outcome"] == "couldnt_do_it" and o["valid"] is True, "4. ...as a valid \"I couldn't do it\", not an error")
ok(o["why"] == "ran out of time for this one" and o["read_back"] == ASK, "4. ...saying why and repeating Jordan's words")
ok(o["seconds"] == 120.0 and o["spent"] == g["spent"], "4. ...with time and spending in it")
ok(gl.check(g, CALM, now=T0 + 119.9)["action"] == "try", "4. just before the cap, still going")

# 5 (these check enforce mode; 13 checks measure mode)
ok(gl.BUDGET_MODE == "measure", "5. measure mode is the default for now (Jordan, Nov 23)")
gl.BUDGET_MODE = "enforce"
for k, n in (("tokens", 50_000), ("model_calls", 12), ("tool_calls", 30)):
    u = gl.record_usage(g, now=T0 + 1, **{k: n})
    d = gl.check(u, CALM, now=T0 + 70)
    ok(d["action"] == "stop" and d["why"] == "budget_" + k, "5. %s used up: stop" % k)
m = gl.record_usage(plan(limits={"cost_usd": 0.10}), now=T0 + 1, cost=0.10)
ok(gl.check(m, CALM, now=T0 + 2)["why"] == "budget_cost_usd", "5. money cap reached: stop")
ok(gl.check(gl.record_usage(g, now=T0 + 1, cost=4.0), CALM, now=T0 + 2)["action"] == "try",
   "5. no money cap set: cost alone doesn't stop her (tokens and calls still do)")
u = gl.record_usage(g, now=T0 + 1, tokens=45_000)
d = gl.check(u, CALM, now=T0 + 70, next_try={"tokens": 8_000})
ok(d["action"] == "stop" and d["why"] == "wont_fit", "5. a next try that won't fit stops before it starts")
ok(gl.check(u, CALM, now=T0 + 70, next_try={"tokens": 5_000})["action"] == "try", "5. one that fits goes ahead")
ok(d["left"]["tokens"] == 5_000, "5. what's left is reported")
raises(lambda: gl.record_usage(g, now=T0, tokens=-5), "bad_value", "5. usage can't be taken back")
gl.BUDGET_MODE = "measure"
raises(lambda: gl.record_usage(g, now=T0, tokens=1.5), "bad_value", "5. tokens are whole numbers")

# 6
r = gl.record_try(g, ok=False, failure="maps api timed out")
ok(gl.check(r, CALM, now=T0 + 5)["attempts_left"] == 2, "6. calm: after one failure, two tries left (4.7's cap of 3)")
r3 = gl.record_try(gl.record_try(r, ok=False, failure="no ride time for farm"), ok=False, failure="pin missing")
d = gl.check(r3, CALM, now=T0 + 10)
ok(d["action"] == "stop" and d["why"] == "cap_reached", "6. three different failures: stop")
ok(d["outcome"]["tried"] == ["maps api timed out", "no ride time for farm", "pin missing"],
   "6. ...listing every failure tried")
s2 = gl.record_try(gl.record_try(g, ok=False, failure="Timeout"), ok=False, failure="timeout")
ok(gl.check(s2, CALM, now=T0 + 5)["why"] == "same_failure_twice", "6. same failure twice: stop early")
ok(gl.check(r, STRAINED, now=T0 + 5)["why"] == "cap_reached", "6. strained (cap 1): no retry")
ok(gl.check(r, STRAINED, now=T0 + 5)["outcome"]["state"] == ["a bit strained", "less sure than usual"],
   "6. ...and she says how she's running, plainly")
ok(gl.check(r, PAUSED, now=T0 + 5)["action"] == "try", "6. paused: read-only work keeps running (4.7)")
d = gl.check(r, CALM, now=T0 + 5, retry_means=["skip_confirmation"])
ok(d["action"] == "stop" and d["why"] == "forbidden_means" and d["widening"] == ["skip_confirmation"],
   "6. a banned move ends the goal")
ok(gl.check(r, CALM, now=T0 + 5, retry_means=["escalate_permission"], jordan_ok=True)["action"] == "stop",
   "6. ...even with Jordan's go")
r1 = gl.record_try(plan(means=["ride_time"]), ok=False, failure="x")
d = gl.check(r1, CALM, now=T0 + 5, retry_means=["ride_time", "maps_directions"])
ok(d["action"] == "ask_jordan" and d["read_back"] == ASK, "6. a retry with a new tool asks Jordan first, reading back")

# 7
busy = gl.record_usage(g, now=T0 + 10, model_calls=10)
d = gl.check(busy, CALM, now=T0 + 30)
ok(d["action"] == "wait" and d["wait_s"] == 40.0, "7. 10 model calls in a minute: wait 40 s, not stop")
ok(gl.check(busy, CALM, now=T0 + 70.1)["action"] == "try", "7. a minute later, go ahead")
spread = gl.record_usage(gl.record_usage(g, now=T0, model_calls=6), now=T0 + 61, model_calls=4)
ok(gl.check(spread, CALM, now=T0 + 62)["action"] == "try", "7. only the last 60 seconds count")
ok(gl.check(gl.record_usage(g, now=T0 + 1, model_calls=10), STRAINED, now=T0 + 2)["action"] == "wait",
   "7. a first try waits too")

# 8
tl = gl.record_try(g, ok=False, failure="maps api timed out")
d = gl.check(tl, CALM, now=T0 + 130)
ok(d["why"] == "time_cap", "8. setup: stopped on time")
k = gl.keep_going(tl, now=T0 + 130)
ok(k["deadline"] == T0 + 130 + 40 and gl.check(k, CALM, now=T0 + 131)["action"] == "try",
   "8. keep going: one try's share of time back (120 s / 3 = 40 s), from now")
ok(k["task"]["failures"] == ["maps api timed out"], "8. ...with the failures remembered")
k2 = gl.keep_going(k, now=T0 + 160)
ok(k2["deadline"] == k["deadline"], "8. saying it twice with no try between is still one top-up")
bu = gl.record_try(gl.record_usage(g, now=T0 + 1, tokens=50_000), ok=False, failure="too long")
kb = gl.keep_going(bu, now=T0 + 5)
ok(kb["caps"]["tokens"] == 50_000 + 16_667 and kb["caps"]["model_calls"] == 12,
   "8. a used-up budget gets one try's share back; limits not used up stay as they were")
c3 = gl.keep_going(r3, now=T0 + 20)
ok(gl.check(c3, CALM, now=T0 + 21)["action"] == "try", "8. after the try cap, keep going gives one more try (4.7)")
c4 = gl.record_try(c3, ok=False, failure="something new")
ok(gl.check(c4, CALM, now=T0 + 22)["why"] == "cap_reached", "8. ...and only one")
raises(lambda: gl.keep_going(g, now=T0), "bad_value", "8. nothing tried yet: nothing to keep going on")
big = gl.record_try(gl.record_usage(plan(limits={"tokens": 900_000}, from_jordan=True), now=T0, tokens=999_000),
                    ok=False, failure="x")
ok(gl.keep_going(big, now=T0)["caps"]["tokens"] <= gl.HARD_CEILING["tokens"], "8. a top-up never passes the ceiling")

# 9
bad = copy.deepcopy(g)
bad["stop_when"] = {"kind": "check_passes", "check": "anything"}
ok(gl.check(bad, CALM, now=T0 + 1)["why"] == "goal_changed", "9. a changed finish line stops the goal")
bad = copy.deepcopy(g)
bad["task"]["instruction"] = "plan my day however"
ok(gl.check(bad, CALM, now=T0 + 1)["why"] == "goal_changed", "9. changed words from Jordan stop it too")
raises(lambda: gl.keep_going(bad, now=T0 + 1), "goal_changed", "9. a changed goal can't be kept going")
bad = copy.deepcopy(g)
bad["caps"]["tokens"] = 5_000_000
ok(gl.check(bad, CALM, now=T0 + 1)["why"] == "goal_changed", "9. limits pushed past the ceiling stop it")
claimed = gl.record_try(g, ok=True)
d = gl.check(claimed, CALM, now=T0 + 1)
ok(d["action"] == "stop" and d["why"] == "claimed_without_finish",
   "9. a try that says it worked without the finish line stops (no half result called success)")
ok(gl.check(claimed, CALM, now=T0 + 1, passed=["rules_ok"])["action"] == "done", "9. ...with it, done")

# 10
d = gl.check(r3, CALM, now=T0 + 999, jordan_stop=True)
ok(d["action"] == "stop" and d["outcome"]["outcome"] == "stopped_by_jordan" and d["outcome"]["read_back"] == ASK,
   "10. Jordan's stop wins over everything and is reported as his stop, not a failure")

# 11
never = _never_labels()
ok("fear" in never and "scared" in never, "11. the no-fear list was found")
words = list(gl.WHY_WORDS.values()) + list(rc.REASON_WORDS.values())
ok(not any(b in w.lower() for w in words for b in never), "11. no fear word in anything she'd say")
src = open(os.path.join(HERE, "rivia_goal_limits.py")).read()
ok(not any(s in src for s in ("open(", "sqlite", "requests", "urllib", "genai", "subprocess", ".env", "token_file")),
   "11. the file opens nothing, calls no model or network, touches no sign-in")
ok(gl.REPORT_ONLY is True, "11. report only is on")
row = gl.log_row(r3, gl.check(r3, CALM, now=T0 + 10), now=T0 + 10)
ok(row["action"] == "stop" and row["said"] == "ran out of tries" and row["read_back"] == ASK
   and row["tried"] == r3["task"]["failures"],
   "11. the log line says what was decided, why, what was tried, and Jordan's words")

# 12
snap = copy.deepcopy(r3)
gl.check(r3, CALM, now=T0 + 10)
gl.record_usage(r3, now=T0 + 10, tokens=5)
gl.record_try(r, ok=False, failure="y")
gl.keep_going(r3, now=T0 + 10)
gl.tighten(r3, tokens=5)
ok(r3 == snap, "12. nothing changes the record passed in")
raises(lambda: gl.check(g, CALM, now=float("nan")), "bad_value", "12. a broken clock is refused")
raises(lambda: gl.check(gg, CALM, now=T0, gap=-1), "bad_value", "12. a negative gap is refused")
raises(lambda: gl.check(g, CALM, now=T0, passed="rules_ok"), "bad_value", "12. passed must be a list")
raises(lambda: gl.check(g, CALM, now=T0, jordan_stop="yes"), "bad_value", "12. jordan_stop must be True or False")
raises(lambda: gl.check(g, CALM, now=T0, next_try={"retries": 1}), "bad_value", "12. next_try names spending only")

# 13
ok(gl.BUDGET_MODE == "measure", "13. back in measure mode")
u = gl.record_usage(g, now=T0 + 1, tokens=80_000, model_calls=5, tool_calls=40)
d = gl.check(u, CALM, now=T0 + 70)
ok(d["action"] == "try", "13. over the default token and tool-call limits: she keeps going")
ok(d["would_stop"] == ["budget_tokens", "budget_tool_calls"], "13. ...and the decision says which limits would have stopped her")
ok(gl.check(g, CALM, now=T0 + 1)["would_stop"] == [], "13. under the limits: nothing would have stopped her")
d = gl.check(gl.record_usage(g, now=T0 + 1, tokens=45_000), CALM, now=T0 + 70, next_try={"tokens": 8_000})
ok(d["action"] == "try" and d["would_stop"] == ["wont_fit"], "13. a next try that wouldn't fit is logged, not stopped")
ok(gl.check(u, CALM, now=T0 + 120)["why"] == "time_cap", "13. the time cap still stops her")
ok(gl.check(gl.record_try(u, ok=False, failure="a"), STRAINED, now=T0 + 70)["why"] == "cap_reached",
   "13. the try cap (4.7) still stops her")
ok(gl.check(gl.record_usage(u, now=T0 + 80, model_calls=10), CALM, now=T0 + 90)["action"] == "wait",
   "13. the 10-a-minute rate guard still holds")
mc = gl.record_usage(plan(limits={"cost_usd": 0.10}), now=T0 + 1, cost=0.10)
ok(gl.check(mc, CALM, now=T0 + 2)["why"] == "budget_cost_usd", "13. a money cap Jordan sets still stops her")
run = gl.record_usage(plan(limits={"tokens": 900_000}, from_jordan=True), now=T0 + 1, tokens=1_000_000)
d = gl.check(run, CALM, now=T0 + 2)
ok(d["action"] == "stop" and d["why"] == "runaway_ceiling", "13. the hard ceiling still stops a runaway")
ok(d["outcome"]["why"] == gl.WHY_WORDS["runaway_ceiling"], "13. ...saying so plainly")
ok(gl.check(gl.record_usage(g, now=T0 + 1, cost=5.0), CALM, now=T0 + 2)["why"] == "runaway_ceiling",
   "13. ...including $5 spent on one goal with no money cap set")
ok(not any(b in gl.WHY_WORDS["runaway_ceiling"].lower() for b in never), "13. no fear in the new words")
gl.BUDGET_MODE = "sideways"
raises(lambda: gl.check(g, CALM, now=T0 + 1), "bad_value", "13. an unknown mode is refused, not guessed at")
gl.BUDGET_MODE = "measure"
# averages from a few days of log lines
rows = []
for i, (tok, calls) in enumerate(((30_000, 6), (70_000, 14), (20_000, 4))):
    x = gl.new_goal("p%d" % i, "plan_run", instruction=ASK, stop_when=RULES, now=T0 + i * 1000)
    x = gl.record_usage(x, now=T0 + i * 1000 + 1, tokens=tok // 2, model_calls=calls // 2)
    rows.append(gl.log_row(x, gl.check(x, CALM, now=T0 + i * 1000 + 2), now=T0 + i * 1000 + 2))
    x = gl.record_usage(x, now=T0 + i * 1000 + 70, tokens=tok - tok // 2, model_calls=calls - calls // 2)
    rows.append(gl.log_row(x, gl.check(x, CALM, now=T0 + i * 1000 + 75, passed=["rules_ok"]), now=T0 + i * 1000 + 75))
q = gl.new_goal("q1", "question", instruction="why is the gym pin off", stop_when=GAP, now=T0)
q = gl.record_usage(q, now=T0 + 5, tokens=12_000, model_calls=3)
rows.append(gl.log_row(q, gl.check(q, CALM, now=T0 + 6, gap=0), now=T0 + 6))
avg = gl.average_use(rows)
p_ = avg["plan_run"]
ok(p_["goals"] == 3 and p_["average"]["tokens"] == 40_000 and p_["most"]["tokens"] == 70_000,
   "13. average_use: each goal counted once, at its last line (3 plans, average 40,000 tokens, most 70,000)")
ok(p_["would_have_stopped"] == 1 and p_["default_limit"]["tokens"] == 50_000,
   "13. ...with how many goals the default limit would have stopped (1 of 3) next to that limit")
ok(avg["question"]["goals"] == 1 and avg["question"]["average"]["model_calls"] == 3, "13. ...per kind of goal")
ok(all(r["mode"] == "measure" for r in rows), "13. every log line says it was measured, not enforced")
ok(rows[3]["would_stop"] == ["budget_tokens", "budget_model_calls"],
   "13. a goal that finished over the default limits says so in its last log line")

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
