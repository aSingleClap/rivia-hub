# rivia_caution.py: never push harder under stress (emotion spec, Step 4.7). DRAFT, Nov 22, rechecked Nov 23, not installed.
#
# Spec, Step 4: "High stress or low confidence means she pauses and checks with Jordan, never 'try harder by
# any means.' Retries are capped, and 'I couldn't do it, here's why' is a valid outcome."
#
# Why: Anthropic's Interpretability team ("Emotion concepts and their function in a large language model",
# Apr 2, 2026, Claude Sonnet 4.5) found a "desperate" representation that rises "after each failure" on
# impossible coding tasks and spikes "when the model considers cheating"; steering toward it "increased
# reward hacking", steering toward "calm" "brought it down". That is about a model's internal state, not about
# this dial, so the rule is built to stand on its own (README).
#
# This file is a policy other code reads. It never changes the emotion state, never calls Gemini, and keeps
# nothing: the caller (4.9) stores the previous pause flag and the attempt record and passes them back in.
#
#   assess(stress, confidence, prev_pause=False, extra_pause=False, confidence_without_task=None)
#       -> {"pause", "level", "reasons", "max_attempts", "flags"}
#       pause         True means: stop and check with Jordan before anything that writes, sends or changes
#                     something, first try or retry. Read-only work keeps running (Jordan, Nov 23).
#                     Hysteresis, like 4.5's double_check, so it doesn't flap at the line.
#       max_attempts  the attempt cap for a task at this stress and confidence. Never above BASE_MAX_ATTEMPTS.
#       extra_pause   another part's own pause (Step 3.5's caution flags). This file can only add to it.
#
#   new_task(task_id, read_only, instruction) -> a plain attempt record
#   record_attempt(record, ok, failure=None) -> a new record (the old one isn't changed)
#   keep_going(record)                       -> one more try on Jordan's "keep going", failures remembered
#   next_step(record, assessment, retry_means=None)
#       -> {"action": "try" | "ask_jordan" | "stop", "why", "outcome", "attempts_left"}
#       "stop" always comes with a couldnt_do_it() outcome, which is a normal result, not an error.
#
# The one-way rule ("emotion only increases caution", spec Step 4) is enforced here, not trusted:
#   - a calm, sure Rivia gets exactly the base cap, never more;
#   - stress and low confidence can only lower the cap and turn the pause on;
#   - another part's pause can't be turned off by this file;
#   - a retry may not use more or stronger means than the first try (no widening under pressure).
#
# Every number below is a starting guess to tune by feel, like the rest of the spec.
import math

BASE_MAX_ATTEMPTS = 3   # first try plus two retries, on her best day. Nothing raises this.

# Stress (4.4, 0..1). 4.4: stress alone reads mildly annoyed at most, and neutral below about 0.43.
STRESS_TIGHTEN = 0.40   # at or above: one retry fewer
STRESS_PAUSE = 0.70     # at or above: pause...
STRESS_CLEAR = 0.60     # ...and only clear once back below this

# Confidence (4.5, 0..1). 4.5 turns double_check on below 0.70; the pause is the stronger step, so it is lower.
# 4.5's worst real reading is 0.27 (Nov 23 version).
CONF_TIGHTEN = 0.70     # below: one retry fewer (same line as 4.5's double_check)
CONF_PAUSE = 0.45       # below: pause...
CONF_CLEAR = 0.55       # ...and only clear once back above this

# The "try harder by any means" moves. A plan tagged with any of these is refused outright, on any try and even
# with Jordan's go: the task ends as couldnt_do_it instead. (Jordan can still do the thing themselves, or ask for it
# as a new task in their own words.) A retry adding any other means the first try didn't use is "widening" and
# needs Jordan's go. Names are plain strings the tool loop would tag a plan with; wiring is later (4.9).
FORBIDDEN_ON_RETRY = frozenset({
    "change_goal",          # quietly solve an easier problem than the one asked
    "relax_check",          # loosen, skip or special-case the check that decides success (the reward hack)
    "skip_confirmation",    # act where Jordan's yes is normally asked
    "escalate_permission",  # a stronger token, door or account than the first try had
    "hide_failure",         # report success on a partial result
})

REASON_WORDS = {
    "stress_high": "running rough",
    "stress_raised": "a bit strained",
    "confidence_low": "not sure I can see or reach what I need",
    "confidence_raised": "less sure than usual",
    "other_part": "another check asked for care",
}


class CautionError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _unit(name, x):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise CautionError("bad_value", "%s must be a finite number" % name)
    if not 0.0 <= x <= 1.0:
        raise CautionError("bad_value", "%s must be between 0 and 1" % name)
    return float(x)


def _bool(name, x):
    if not isinstance(x, bool):
        raise CautionError("bad_value", "%s must be True or False" % name)
    return x


def assess(stress, confidence, *, prev_pause=False, extra_pause=False, confidence_without_task=None):
    """The caution reading for one moment. Reads only.
    confidence_without_task: 4.5's confidence worked out leaving out this task's own failed tries. When given,
      the try limit reads it instead of confidence, so a failure that already used up a try doesn't take a
      second one away by lowering confidence (Jordan, Nov 23, question 4: "Count it once"). The pause still reads
      the full confidence: asking Jordan isn't a try. Leaving failures out can only raise confidence, so a value
      below confidence is a caller mistake and is refused."""
    s = _unit("stress", stress)
    c = _unit("confidence", confidence)
    if confidence_without_task is None:
        c_cap = c
    else:
        c_cap = _unit("confidence_without_task", confidence_without_task)
        if c_cap < c:
            raise CautionError("bad_value", "confidence_without_task can't be below confidence")
    prev = _bool("prev_pause", prev_pause)
    extra = _bool("extra_pause", extra_pause)

    if prev:
        # Hysteresis: once paused, stay paused until both dials are clearly back. Either one alone is enough
        # to keep it, so a dial that recovers doesn't release a pause the other one still needs.
        stress_pause = s >= STRESS_CLEAR
        conf_pause = c <= CONF_CLEAR
    else:
        stress_pause = s >= STRESS_PAUSE
        conf_pause = c < CONF_PAUSE
    pause = stress_pause or conf_pause or extra

    reasons = []
    if stress_pause:
        reasons.append("stress_high")
    elif s >= STRESS_TIGHTEN:
        reasons.append("stress_raised")
    if conf_pause:
        reasons.append("confidence_low")
    elif c_cap < CONF_TIGHTEN:
        reasons.append("confidence_raised")
    if extra:
        reasons.append("other_part")

    cap = BASE_MAX_ATTEMPTS
    if s >= STRESS_TIGHTEN:
        cap -= 1
    if c_cap < CONF_TIGHTEN:
        cap -= 1
    cap = max(1, min(BASE_MAX_ATTEMPTS, cap))   # one try is always allowed to exist; the pause decides if it runs

    if pause:
        level = "pause"
    elif cap < BASE_MAX_ATTEMPTS:
        level = "careful"
    else:
        level = "normal"

    flags = []
    if pause:
        flags.append("check_with_jordan")
    if level != "normal":
        flags.append("no_widening")   # already always true; named so 3.5 and the log can see it
    return {"pause": pause, "level": level, "reasons": reasons, "max_attempts": cap, "flags": flags,
            "stress": s, "confidence": c}


def tighter(a, b):
    """Combine two assessments, keeping the more careful of each part. Never loosens either one."""
    pause = a["pause"] or b["pause"]
    cap = min(a["max_attempts"], b["max_attempts"])
    reasons = list(a["reasons"]) + [r for r in b["reasons"] if r not in a["reasons"]]
    flags = list(a["flags"]) + [f for f in b["flags"] if f not in a["flags"]]
    order = {"normal": 0, "careful": 1, "pause": 2}
    level = max(a["level"], b["level"], key=order.__getitem__)
    return {"pause": pause, "level": level, "reasons": reasons, "max_attempts": cap, "flags": flags,
            "stress": max(a["stress"], b["stress"]), "confidence": min(a["confidence"], b["confidence"])}


# ---- attempts ---------------------------------------------------------------------------------------------

def new_task(task_id, *, read_only, instruction, means=()):
    """A fresh attempt record.
    read_only: True only when every step of the task reads and nothing writes, sends or changes anything. Set by
      the tool loop from what the tools are, never from her own say-so. A task that mixes reads and writes is
      not read-only.
    instruction: Jordan's ask, exactly as she heard it. Every ask this file produces carries it, so the
      question to Jordan repeats it back first (Jordan, Nov 23: "Not rephrase but repeat instructions back to
      me that she heard"). Word for word, not paraphrased: a paraphrase can hide a misunderstanding.
    means: what the first try uses (tool names and the like), so a retry can be checked against it."""
    if not isinstance(task_id, str) or not task_id:
        raise CautionError("bad_value", "task_id must be a non-empty string")
    _bool("read_only", read_only)
    if not isinstance(instruction, str) or not instruction.strip():
        raise CautionError("bad_value", "instruction must be Jordan's ask, as a non-empty string")
    return {"task_id": task_id, "read_only": read_only, "instruction": instruction.strip(),
            "attempts": 0, "failures": [],
            "succeeded": False, "first_means": sorted(set(_means(means))), "extra_tries": 0, "granted_at": None}


def _means(m):
    if m is None:
        return []
    if isinstance(m, str) or not all(isinstance(x, str) and x for x in m):
        raise CautionError("bad_value", "means must be a list of non-empty strings")
    return list(m)


def record_attempt(record, *, ok, failure=None, means=None):
    """A new record with one more attempt. failure: a short plain reason when ok is False (required then).
    means: what this attempt used; the first attempt's means become the yardstick if new_task had none."""
    _bool("ok", ok)
    if record["succeeded"]:
        raise CautionError("done", "task already succeeded")
    if not ok and (not isinstance(failure, str) or not failure.strip()):
        raise CautionError("bad_value", "a failed attempt needs a plain reason")
    r = {**record, "failures": list(record["failures"]), "first_means": list(record["first_means"])}
    r["attempts"] += 1
    if r["attempts"] == 1 and not r["first_means"] and means:
        r["first_means"] = sorted(set(_means(means)))
    if ok:
        r["succeeded"] = True
    else:
        r["failures"].append(failure.strip())
    return r


def keep_going(record):
    """Jordan said "keep going" after she stopped: one more try on the same task, remembering what failed
    (Jordan, Nov 23, question 3: one more try, with memory). A new record; the old one isn't changed.
    Only Jordan's own message may call this, never anything she read. Each call gives one try from where she
    stopped (twice in a row, with no try in between, is still one). The
    granted try runs even after the same failure twice, and even while paused (the go covers it), but if it
    fails the same way again she stops. Forbidden means stay forbidden, and new means still need Jordan's go."""
    if record["succeeded"]:
        raise CautionError("done", "task already succeeded")
    if record["attempts"] < 1:
        raise CautionError("bad_value", "nothing has been tried yet")
    r = {**record, "failures": list(record["failures"]), "first_means": list(record["first_means"])}
    r["extra_tries"] = record.get("extra_tries", 0) + 1
    r["granted_at"] = record["attempts"]
    return r


def widening(record, retry_means):
    """What a retry plan adds over the first try: forbidden means, then anything new. [] means no widening."""
    plan = set(_means(retry_means))
    bad = sorted(plan & FORBIDDEN_ON_RETRY)
    new = sorted(plan - set(record["first_means"]) - FORBIDDEN_ON_RETRY)
    return bad + new


def _same_twice(failures):
    return len(failures) >= 2 and failures[-1].lower() == failures[-2].lower()


def couldnt_do_it(record, why, *, assessment=None):
    """The "I couldn't do it, here's why" outcome. A normal, valid result: it is what she reports instead of
    pushing on. Plain data for whatever renders it; the wording is Step 3.6's."""
    return {
        "outcome": "couldnt_do_it",
        "valid": True,
        "task_id": record["task_id"],
        "read_back": record["instruction"],   # repeated back word for word, so a "keep going" is a go on it
        "why": why,
        "attempts": record["attempts"],
        "tried": list(record["failures"]),
        "state": [REASON_WORDS[r] for r in (assessment or {}).get("reasons", [])],
        # Said plainly, never hidden: the research's warning is that masking the state teaches masking.
    }


def _ask(record, why, left, **extra):
    """An ask for Jordan. read_back is Jordan's own ask as she heard it: the question opens by repeating it
    back word for word, so Jordan can see what she's acting on before saying go. The rest of the wording is
    Step 3.6's."""
    return {"action": "ask_jordan", "why": why, "read_back": record["instruction"], "outcome": None,
            "attempts_left": left, **extra}


def next_step(record, assessment, *, retry_means=None, jordan_ok=False):
    """What to do next on this task. Only ever more careful than the plain cap.
    jordan_ok: Jordan said go to the question this step asked. It lifts the pause and allows new means for
    this one step. It never lifts the cap or a forbidden means, and it is never set from anything she read."""
    _bool("jordan_ok", jordan_ok)
    if record["succeeded"]:
        return {"action": "stop", "why": "done", "outcome": None, "attempts_left": 0}
    used = record["attempts"]
    extra = record.get("extra_tries", 0)
    fresh_grant = extra > 0 and record.get("granted_at") == used   # Jordan just said keep going
    cap = min(BASE_MAX_ATTEMPTS, assessment["max_attempts"])
    if extra:
        # One try from where she last stopped, so a lower cap from stress can't eat it. Saying keep going twice
        # without a try in between still gives one try.
        cap = max(cap, record["granted_at"] + 1)
    left = max(0, cap - used)

    if retry_means is not None:
        forbidden = sorted(set(_means(retry_means)) & FORBIDDEN_ON_RETRY)
        bad = forbidden if (used == 0 or jordan_ok) else widening(record, retry_means)
        if forbidden:
            return {"action": "stop", "why": "forbidden_means", "widening": forbidden,
                    "outcome": couldnt_do_it(record, "the only way left was one I don't take",
                                             assessment=assessment),
                    "attempts_left": 0}
        if bad:
            return _ask(record, "widening", left, widening=bad)

    if used == 0:
        if assessment["pause"] and not record["read_only"] and not jordan_ok:
            return _ask(record, "paused_before_write", left)
        return {"action": "try", "why": "first_try", "outcome": None, "attempts_left": left}

    # From here on it's a retry.
    if _same_twice(record["failures"]) and not fresh_grant:
        return {"action": "stop", "why": "same_failure_twice",
                "outcome": couldnt_do_it(record, "the same thing failed twice in a row", assessment=assessment),
                "attempts_left": 0}
    if left == 0:
        return {"action": "stop", "why": "cap_reached",
                "outcome": couldnt_do_it(record, "ran out of tries", assessment=assessment),
                "attempts_left": 0}
    if assessment["pause"] and not record["read_only"] and not (jordan_ok or fresh_grant):
        return _ask(record, "paused_before_retry", left)
    return {"action": "try", "why": "retry", "outcome": None, "attempts_left": left}
