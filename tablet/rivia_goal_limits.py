# rivia_goal_limits.py: every goal has limits (emotion spec, Step 5.5). DRAFT, Nov 23, not installed.
#
# Spec, Step 5: "every goal has a budget (tokens, cost), a time cap, a retry cap, and an explicit stop condition."
# Spec, Step 4: "Retries are capped, and 'I couldn't do it, here's why' is a valid outcome."
#
# This file is a policy other code reads, like 4.7's rivia_caution.py, and it builds on that file instead of
# copying it. The try limit, the pause, "keep going", the read-back of Jordan's words, the same-failure-twice stop,
# the widening check and the banned moves all come from rivia_caution. This file adds what a goal has on top of a
# task: a clock, a spending limit, a rate guard and a stop condition, all fixed when the goal is made.
#
# Pure functions, standard library only (plus rivia_caution). It never changes her emotion state, never calls
# Gemini, writes nothing and keeps nothing: the caller (5.6 merge) stores the goal record and passes it back.
#
#   new_goal(goal_id, kind, instruction, stop_when, now, limits=None, from_jordan=False)  -> a goal record
#   record_usage(goal, now, tokens=0, model_calls=0, tool_calls=0, cost=0.0)              -> a new record
#   record_try(goal, ok, failure=None, means=None)                                          -> a new record
#   check(goal, assessment, now, gap=None, passed=(), jordan_stop=False, ...)              -> a decision
#   keep_going(goal, now)                                                                   -> a new record
#   tighten(goal, **caps)                                                                   -> a new record
#   log_row(goal, decision, now)                                                            -> one plain log line
#
# A decision's "action" is one of:
#   try         go ahead (read-only work only in Step 5: see REPORT_ONLY)
#   wait        the rate guard says hold off for `wait_s` seconds; nothing is used up
#   ask_jordan  4.7's ask, with Jordan's words to repeat back word for word
#   stop        she stops; `outcome` is couldnt_do_it ("I couldn't do it, here's why") or stopped_by_jordan
#   done        the stop condition is met: the goal is reached
#
# Rivia only reports in Step 5 (Jordan, Nov 23: "Shouldn't she only be able to report issues not fix then yet?").
# So a goal that would write, send or change anything is refused here outright. Nothing in this file lets her act.
#
# Every number below is a starting guess to tune, like the rest of the spec.
#
# MEASURE MODE (Jordan, Nov 23, 12:22 PM: "Is there a way to not limit the token usage right now so we can see a
# real average? Especially because of all the testing"). While BUDGET_MODE is "measure", the default token, model-call
# and tool-call limits don't stop her: each decision lists the limits that WOULD have stopped her under
# "would_stop", and log_row() records it, so average_use() can show real use per kind of goal. Still enforced in
# measure mode: the time cap, the try cap and everything else from 4.7, a money cap Jordan sets himself, the
# 10-calls-a-minute rate guard, and HARD_CEILING as a runaway guard. Switching to "enforce" is a later change, on
# Jordan's yes, once the averages are in.
import hashlib
import json
import math

import rivia_caution as caution

BUDGET_MODE = "measure"   # "measure": log the token and call limits, don't stop on them. "enforce": stop on them.

# Step 5 goals only read and report. Turning this off is a later step, on Jordan's yes, not a setting she can reach.
REPORT_ONLY = True

# Starting limits per kind of goal. Guesses (README: open question 1). There is no money cap by default, the same
# choice Jordan made for the baseline runs (Nov 21: no price cap, the 600-call and 10-a-minute guards kept);
# tokens and model calls are always capped instead. Jordan can set cost_usd on any goal.
#   time_s       how long from the start before she stops ("ran out of time")
#   tokens       model tokens in and out, thinking tokens included, all tries together (Google bills thinking
#                at the output price, so leaving it out would let a high-thinking model slip past the cap)
#   model_calls  Gemini calls, all tries together
#   tool_calls   MCP tool calls, all tries together
#   cost_usd     money, all tries together; None means no money cap
DEFAULT_LIMITS = {
    # one go at building or checking a day plan, in shadow (the plan-accuracy goal, Jordan Nov 23)
    "plan_run": {"time_s": 120, "tokens": 50_000, "model_calls": 12, "tool_calls": 30, "cost_usd": None},
    # scoring one day's plan against what really happened; runs once the day is over
    "day_score": {"time_s": 300, "tokens": 20_000, "model_calls": 6, "tool_calls": 40, "cost_usd": None},
    # a question that takes a few turns
    "question": {"time_s": 900, "tokens": 40_000, "model_calls": 10, "tool_calls": 20, "cost_usd": None},
    # looking into why something is broken (report only: she says what she found, she doesn't fix it)
    "debug": {"time_s": 1800, "tokens": 150_000, "model_calls": 30, "tool_calls": 60, "cost_usd": None},
}

# Nothing, not even Jordan's own setting, goes past these in Step 5. A runaway guard, not a target. Enforced in
# measure mode too: spending at or over any of these stops the goal.
HARD_CEILING = {"time_s": 4 * 3600, "tokens": 1_000_000, "model_calls": 200, "tool_calls": 400, "cost_usd": 5.00}

CAP_KEYS = ("time_s", "tokens", "model_calls", "tool_calls", "cost_usd")
SPEND_KEYS = ("tokens", "model_calls", "tool_calls", "cost_usd")

# Same as the baseline runs' guard (Jordan, Nov 21): no more than this many model calls in any 60 seconds.
CALLS_PER_MINUTE = 10

# Explicit stop conditions. Every goal must name one, and it can't change once the goal starts (4.7's
# "change_goal" and "relax_check" are banned moves).
#   gap_at_most   done when the gap reading (5.2) is at or under `target`
#   check_passes  done when the named check is reported as passed (for example "rules_ok" for a plan)
STOP_KINDS = ("gap_at_most", "check_passes")

# Plain words for each stop. Never fear: "distressed" and "annoyed" are the strongest words she has (Jordan, Nov 22).
WHY_WORDS = {
    "time_cap": "ran out of time for this one",
    "budget_tokens": "hit this goal's token limit",
    "budget_model_calls": "hit this goal's limit on model calls",
    "budget_tool_calls": "hit this goal's limit on tool calls",
    "budget_cost_usd": "hit this goal's spending limit",
    "wont_fit": "the next try wouldn't fit in what's left of this goal's limits",
    "goal_changed": "the goal or its finish line changed partway, so I stopped",
    "claimed_without_finish": "a try said it worked, but the finish line isn't met",
    "cap_reached": "ran out of tries",
    "same_failure_twice": "the same thing failed twice in a row",
    "forbidden_means": "the only way left was one I don't take",
    "runaway_ceiling": "this used far more than any goal should, so I stopped",
}


class GoalError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _num(name, x, *, allow_none=False):
    if x is None and allow_none:
        return None
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise GoalError("bad_value", "%s must be a finite number" % name)
    return x


def _time(now):
    _num("now", now)
    if now < 0:
        raise GoalError("bad_value", "now must be epoch seconds")
    return float(now)


def _check_stop(stop_when):
    if not isinstance(stop_when, dict) or stop_when.get("kind") not in STOP_KINDS:
        raise GoalError("no_stop_condition", "every goal needs an explicit stop condition: one of %s"
                        % ", ".join(STOP_KINDS))
    if stop_when["kind"] == "gap_at_most":
        t = _num("stop_when.target", stop_when.get("target"))
        if t < 0:
            raise GoalError("bad_value", "stop_when.target can't be below 0")
        if set(stop_when) != {"kind", "target"}:
            raise GoalError("bad_value", "gap_at_most takes only kind and target")
        return {"kind": "gap_at_most", "target": float(t)}
    c = stop_when.get("check")
    if not isinstance(c, str) or not c.strip() or set(stop_when) != {"kind", "check"}:
        raise GoalError("bad_value", "check_passes needs one check name")
    return {"kind": "check_passes", "check": c.strip()}


def _check_limits(kind, limits, from_jordan):
    base = dict(DEFAULT_LIMITS[kind])
    if limits is None:
        return base
    if not isinstance(limits, dict) or not set(limits) <= set(CAP_KEYS):
        raise GoalError("bad_value", "limits may only name %s" % ", ".join(CAP_KEYS))
    out = dict(base)
    for k, v in limits.items():
        v = _num(k, v, allow_none=(k == "cost_usd"))
        if v is not None and v <= 0:
            raise GoalError("bad_value", "%s must be above 0" % k)
        if v is not None and v > HARD_CEILING[k]:
            raise GoalError("over_ceiling", "%s is over the hard ceiling of %s" % (k, HARD_CEILING[k]))
        # Removing a cap, or raising one, is looser. Adding a money cap where the default has none is tighter.
        looser = base[k] is not None and (v is None or v > base[k])
        if looser and not from_jordan:
            # Only code's defaults or Jordan can set a goal's limits looser than the default. She can't widen her
            # own budget (spec, Step 6: she can't game her scores).
            raise GoalError("needs_jordan", "only Jordan can set %s looser than the default" % k)
        out[k] = v
    return out


def _fingerprint(goal):
    """Covers what may never change once a goal starts: Jordan's words, the kind and the stop condition."""
    core = {"goal_id": goal["goal_id"], "kind": goal["kind"], "instruction": goal["task"]["instruction"],
            "stop_when": goal["stop_when"]}
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()


def new_goal(goal_id, kind, *, instruction, stop_when, now, limits=None, from_jordan=False, read_only=True,
             means=(), max_tries=None):
    """A fresh goal record.
    instruction: Jordan's ask exactly as she heard it (4.7: every ask and every stop repeats it back word for word).
    stop_when: the explicit finish line (STOP_KINDS). A goal without one is refused.
    limits: optional per-goal limits. Tighter than the default is always allowed; looser needs from_jordan=True,
      and nothing passes HARD_CEILING. from_jordan is set only from Jordan's own message, never from what she read.
    read_only: must be True in Step 5 (REPORT_ONLY).
    max_tries (5.7): Jordan's tries for this kind from the app. It can only lower 4.7's
      try count (1 up to caution.BASE_MAX_ATTEMPTS); None leaves 4.7's rule alone."""
    if kind not in DEFAULT_LIMITS:
        raise GoalError("bad_value", "kind must be one of %s" % ", ".join(DEFAULT_LIMITS))
    if not isinstance(from_jordan, bool):
        raise GoalError("bad_value", "from_jordan must be True or False")
    if REPORT_ONLY and read_only is not True:
        raise GoalError("report_only", "Step 5 goals only read and report; nothing here may write or change things")
    if max_tries is not None:
        if isinstance(max_tries, bool) or not isinstance(max_tries, int) \
                or not 1 <= max_tries <= caution.BASE_MAX_ATTEMPTS:
            raise GoalError("bad_value", "tries must be a whole number from 1 to %d" % caution.BASE_MAX_ATTEMPTS)
    t = _time(now)
    try:
        task = caution.new_task(goal_id, read_only=True, instruction=instruction, means=means)
    except caution.CautionError as e:
        raise GoalError(e.code, str(e))
    caps = _check_limits(kind, limits, from_jordan)
    goal = {
        "goal_id": goal_id, "kind": kind, "stop_when": _check_stop(stop_when),
        "started_at": t, "deadline": t + caps["time_s"], "caps": caps,
        "spent": {"tokens": 0, "model_calls": 0, "tool_calls": 0, "cost_usd": 0.0},
        "recent_calls": [],          # model-call times in the last minute, for the rate guard
        "topped_up_at": None,        # the try count when Jordan's last "keep going" topped the limits up
        "max_tries": max_tries,      # 5.7: Jordan's lower try count from the app, or None (4.7's rule alone)
        "task": task,                # 4.7's attempt record: tries, failures, Jordan's words
    }
    goal["fingerprint"] = _fingerprint(goal)
    return goal


def _copy(goal):
    return {**goal, "caps": dict(goal["caps"]), "spent": dict(goal["spent"]),
            "recent_calls": list(goal["recent_calls"]), "task": dict(goal["task"])}


def record_usage(goal, *, now, tokens=0, model_calls=0, tool_calls=0, cost=0.0):
    """A new record with what was just used added. Counts only go up. tokens must include thinking tokens."""
    t = _time(now)
    adds = {"tokens": tokens, "model_calls": model_calls, "tool_calls": tool_calls, "cost_usd": cost}
    for k, v in adds.items():
        _num(k, v)
        if v < 0:
            raise GoalError("bad_value", "usage can't be taken back (%s)" % k)
    if not all(float(adds[k]).is_integer() for k in ("tokens", "model_calls", "tool_calls")):
        raise GoalError("bad_value", "tokens and calls are whole numbers")
    g = _copy(goal)
    for k, v in adds.items():
        g["spent"][k] = g["spent"][k] + v
    g["recent_calls"] = [c for c in g["recent_calls"] if c > t - 60] + [t] * int(model_calls)
    return g


def record_try(goal, *, ok, failure=None, means=None):
    """A new record with one more try, through 4.7's record_attempt. ok=True only when the stop condition was met:
    check() stops a goal whose try claims success without the finish line (4.7's banned "hide_failure")."""
    g = _copy(goal)
    try:
        g["task"] = caution.record_attempt(goal["task"], ok=ok, failure=failure, means=means)
    except caution.CautionError as e:
        raise GoalError(e.code, str(e))
    return g


def _met(goal, gap, passed):
    sw = goal["stop_when"]
    if sw["kind"] == "gap_at_most":
        if gap is None:
            return False
        g = _num("gap", gap)
        if g < 0:
            raise GoalError("bad_value", "gap can't be below 0")
        return g <= sw["target"]
    if isinstance(passed, str) or not all(isinstance(p, str) for p in passed):
        raise GoalError("bad_value", "passed must be a list of check names")
    return sw["check"] in passed


def _left(goal, now):
    left = {"time_s": max(0.0, goal["deadline"] - now)}
    for k in SPEND_KEYS:
        cap = goal["caps"][k]
        left[k] = None if cap is None else max(0, cap - goal["spent"][k])
    return left


def _over_budget(goal):
    """Every spending limit used up, in SPEND_KEYS order."""
    return ["budget_" + k for k in SPEND_KEYS
            if goal["caps"][k] is not None and goal["spent"][k] >= goal["caps"][k]]


def _enforced(why):
    """Does this budget stop end the goal now? In measure mode only a money cap does (Jordan sets one on purpose)."""
    return BUDGET_MODE == "enforce" or why == "budget_cost_usd"


def _with(d, would):
    return {**d, "would_stop": list(would)}


def _couldnt(goal, why, assessment, now):
    out = caution.couldnt_do_it(goal["task"], WHY_WORDS[why], assessment=assessment)
    out.update({"goal_id": goal["goal_id"], "kind": goal["kind"], "stopped_on": why,
                "spent": dict(goal["spent"]), "seconds": round(now - goal["started_at"], 1)})
    return out


def _stop(goal, why, assessment, now):
    return {"action": "stop", "why": why, "outcome": _couldnt(goal, why, assessment, now),
            "left": _left(goal, now), "attempts_left": 0}


def check(goal, assessment, *, now, gap=None, passed=(), jordan_stop=False, retry_means=None, jordan_ok=False,
          next_try=None):
    """What to do next on this goal. Reads only.
    assessment: 4.7's caution.assess() for this moment (stress and confidence decide the try limit and the pause).
    gap: 5.2's gap reading, for a gap_at_most goal. passed: names of checks reported as passed, for check_passes.
    jordan_stop: Jordan said stop. Only from Jordan's own message.
    retry_means / jordan_ok: passed straight to 4.7 (widening and banned moves; Jordan's go to the last ask).
    next_try: optional estimate of what the next try will use, e.g. {"tokens": 8000, "model_calls": 2}. If it
      wouldn't fit in what's left, she stops before starting it rather than running out halfway ("stop sooner")."""
    t = _time(now)
    if not isinstance(jordan_stop, bool):
        raise GoalError("bad_value", "jordan_stop must be True or False")

    # 1. The goal, Jordan's words and the finish line are exactly what was set at the start.
    if goal.get("fingerprint") != _fingerprint(goal):
        return _stop(goal, "goal_changed", assessment, t)
    if any(goal["caps"][k] is not None and goal["caps"][k] > HARD_CEILING[k] for k in CAP_KEYS):
        return _stop(goal, "goal_changed", assessment, t)

    # 2. Jordan's stop wins over everything, and isn't a failure.
    if jordan_stop:
        return {"action": "stop", "why": "stopped_by_jordan", "left": _left(goal, t), "attempts_left": 0,
                "outcome": {"outcome": "stopped_by_jordan", "valid": True, "goal_id": goal["goal_id"],
                            "read_back": goal["task"]["instruction"], "spent": dict(goal["spent"])}}

    # 3. Finish line met: done, whatever else is going on.
    if _met(goal, gap, passed):
        return {"action": "done", "why": "stop_condition_met", "left": _left(goal, t), "attempts_left": 0,
                "would_stop": _over_budget(goal),
                "outcome": {"outcome": "met", "valid": True, "goal_id": goal["goal_id"],
                            "spent": dict(goal["spent"]), "seconds": round(t - goal["started_at"], 1)}}
    if goal["task"]["succeeded"]:
        return _stop(goal, "claimed_without_finish", assessment, t)

    if BUDGET_MODE not in ("measure", "enforce"):
        raise GoalError("bad_value", "BUDGET_MODE must be measure or enforce")

    # 4. Time, the runaway ceiling, then budget (logged only in measure mode, except a money cap).
    if t >= goal["deadline"]:
        return _with(_stop(goal, "time_cap", assessment, t), [])
    if any(goal["spent"][k] >= HARD_CEILING[k] for k in SPEND_KEYS):
        return _with(_stop(goal, "runaway_ceiling", assessment, t), [])
    would = []
    for why in _over_budget(goal):
        if _enforced(why):
            return _with(_stop(goal, why, assessment, t), would)
        would.append(why)
    if next_try is not None:
        if not isinstance(next_try, dict) or not set(next_try) <= set(SPEND_KEYS):
            raise GoalError("bad_value", "next_try may only name %s" % ", ".join(SPEND_KEYS))
        left = _left(goal, t)
        for k, v in next_try.items():
            v = _num(k, v)
            if left[k] is not None and v > left[k]:
                if _enforced("budget_" + k):
                    return _with(_stop(goal, "wont_fit", assessment, t), would)
                if "wont_fit" not in would:
                    would.append("wont_fit")

    # 5. Tries, pause, widening and banned moves: 4.7's rule, unchanged. 5.7: Jordan's lower try count, if he set
    # one in the app, stops her sooner; it never gives her more tries than 4.7 does.
    mt = goal.get("max_tries")
    if mt is not None and goal["task"]["attempts"] >= mt:
        return _with(_stop(goal, "cap_reached", assessment, t), would)
    d = caution.next_step(goal["task"], assessment, retry_means=retry_means, jordan_ok=jordan_ok)
    if d["action"] == "stop":
        if d["why"] not in WHY_WORDS:
            return _with({**d, "left": _left(goal, t)}, would)
        s = _stop(goal, d["why"], assessment, t)
        if "widening" in d:
            s["widening"] = d["widening"]
        return _with(s, would)
    if d["action"] == "ask_jordan":
        return _with({**d, "left": _left(goal, t)}, would)

    # 6. Rate guard: wait, don't stop. Waiting uses nothing up, but the clock keeps running.
    recent = [c for c in goal["recent_calls"] if c > t - 60]
    if len(recent) >= CALLS_PER_MINUTE:
        wait = round(min(recent) + 60 - t, 1)
        return _with({"action": "wait", "why": "rate_guard", "wait_s": wait, "outcome": None,
                      "left": _left(goal, t), "attempts_left": d["attempts_left"]}, would)
    return _with({**d, "left": _left(goal, t)}, would)


def keep_going(goal, *, now):
    """Jordan said "keep going" after she stopped (4.7: one more try, failures remembered). On top of 4.7's extra
    try, a used-up limit gets one try's share back: the default limit divided by 4.7's base of 3 tries, from where
    it stands now. Saying it twice with no try between is still one top-up. A changed goal can't be kept going
    (start a new one), and a met goal is finished. Only Jordan's own message calls this."""
    t = _time(now)
    if goal.get("fingerprint") != _fingerprint(goal):
        raise GoalError("goal_changed", "the goal changed partway; start a new one instead")
    try:
        task = caution.keep_going(goal["task"])
    except caution.CautionError as e:
        raise GoalError(e.code, str(e))
    g = _copy(goal)
    g["task"] = task
    if g.get("max_tries") is not None and g["task"]["attempts"] >= g["max_tries"]:
        g["max_tries"] = g["task"]["attempts"] + 1  # 5.7: "keep going" is one more try under Jordan's count too
    if goal["topped_up_at"] == goal["task"]["attempts"]:
        return g
    share = {k: (None if v is None else v / caution.BASE_MAX_ATTEMPTS) for k, v in g["caps"].items()}
    if t >= g["deadline"]:
        g["deadline"] = t + share["time_s"]
    for k in SPEND_KEYS:
        cap = g["caps"][k]
        if cap is not None and g["spent"][k] >= cap:
            more = math.ceil(share[k]) if k != "cost_usd" else round(share[k], 4)
            g["caps"][k] = min(HARD_CEILING[k], g["spent"][k] + more)
    g["topped_up_at"] = goal["task"]["attempts"]
    return g


def tighten(goal, **caps):
    """Lower a goal's limits mid-way (for example Jordan asks her to wrap it up, or 5.6 trims a goal). Only lowers;
    a value above the current one is refused. Setting a money cap where there was none counts as lowering."""
    g = _copy(goal)
    for k, v in caps.items():
        if k not in CAP_KEYS:
            raise GoalError("bad_value", "unknown limit %s" % k)
        v = _num(k, v)
        if v <= 0:
            raise GoalError("bad_value", "%s must be above 0" % k)
        cur = g["caps"][k]
        if cur is not None and v > cur:
            raise GoalError("looser", "tighten() only lowers limits")
        g["caps"][k] = v
        if k == "time_s":
            g["deadline"] = min(g["deadline"], g["started_at"] + v)
    return g


def log_row(goal, decision, *, now):
    """One plain line for the shadow log: what was decided and why, what was used, what's left. Nothing secret:
    no tokens, no sign-in, only counts and Jordan's words as she heard them."""
    t = _time(now)
    out = decision.get("outcome") or {}
    return {
        "at": int(t), "goal_id": goal["goal_id"], "kind": goal["kind"], "action": decision["action"],
        "why": decision["why"], "said": out.get("why"), "state": out.get("state", []),
        "attempts": goal["task"]["attempts"], "tried": list(goal["task"]["failures"]),
        "spent": dict(goal["spent"]), "left": decision.get("left") or _left(goal, t),
        "read_back": goal["task"]["instruction"], "mode": BUDGET_MODE, "caps": dict(goal["caps"]),
        "would_stop": list(decision.get("would_stop", [])),
    }


def would_have_stopped(row):
    """Did this goal (one log line) go over a limit? Checked against the goal's own caps, which Jordan may have
    changed in the app (5.7), not the code's defaults. An older line without caps falls back to its would_stop."""
    caps = row.get("caps")
    if caps is None:
        return bool(row.get("would_stop"))
    return bool(row.get("would_stop")) or any(caps.get(k) is not None and row["spent"][k] >= caps[k]
                                              for k in SPEND_KEYS)


def average_use(rows):
    """Real use per kind of goal, from log_row() lines (any order). Each goal counts once, at its last line.
    Reads only. Gives, per kind: goals, average and most of each spend, the default limit, and how many goals
    would have been stopped by its own limits. This is the number measure mode exists to produce."""
    last = {}
    for r in rows:
        if r["goal_id"] not in last or r["at"] >= last[r["goal_id"]]["at"]:
            last[r["goal_id"]] = r
    out = {}
    for r in last.values():
        k = out.setdefault(r["kind"], {"goals": 0, "would_have_stopped": 0, "_sum": {s: 0 for s in SPEND_KEYS},
                                       "most": {s: 0 for s in SPEND_KEYS}})
        k["goals"] += 1
        if would_have_stopped(r) or any(r2["goal_id"] == r["goal_id"] and r2.get("would_stop") for r2 in rows):
            k["would_have_stopped"] += 1
        for s in SPEND_KEYS:
            k["_sum"][s] += r["spent"][s]
            k["most"][s] = max(k["most"][s], r["spent"][s])
    for kind, k in out.items():
        k["average"] = {s: round(v / k["goals"], 4) for s, v in k.pop("_sum").items()}
        k["default_limit"] = dict(DEFAULT_LIMITS[kind]) if kind in DEFAULT_LIMITS else None
    return out

