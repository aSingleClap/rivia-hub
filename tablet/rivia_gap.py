# rivia_gap.py: the daily gap number (emotion spec, Step 5.2). DRAFT, Nov 23, not installed.
#
# What it does: takes one day's plan scores from 5.1 and turns them into ONE number for that day, the "gap":
#   0.0 = her plans matched Jordan's real day perfectly
#   1.0 = way off
# Step 5's mood reads whether this number shrinks or grows over days (5.3), not how big it is on one day.
#
# What it does NOT do (other pieces own these):
#   - It writes nothing. No emotion.db change, no log row, no Gemini call, no timer. Plain functions only.
#   - It does not measure anything. 5.1 does; this only combines 5.1's four scores.
#   - It does not read a trend or touch mood. 5.3 (direction) and 5.4 (mood nudge) do.
#
# Jordan's goal (Nov 23, 1:43 AM): "scheduling my day as accurately as possible". The four weighted scores:
#   arrival_miss_min  minutes off from the planned arrival, middle value of the day (early or late both count).
#                  5.6 merge: this fills the rides slot. Jordan (Nov 23): "Arrival time because I might change my
#                  mode of travel sometimes". It replaced 5.1's earlier ride_miss_min.
#   pin_hit_rate   share of arrivals (0 to 1) that fired at the right place
#   plan_held      share of the day (0 to 1) that went as planned without Jordan rearranging it
#   rule_breaks    how many times a plan broke one of Jordan's rules (should always be 0)
# Logged, not weighted (5.6 merge): ride_est_miss_min (her ride estimate against the real ride) and ride_est_wins
# (rides where her estimate beat Jordan's hand estimate). They are passed through in "logged" so the shadow log
# shows them, but they don't move the gap until Jordan says they should.
#
# How they combine (a "weighted average": each score gets a share of the say, and bigger shares count more):
#   1. Each score becomes a "miss" from 0 (perfect) to 1 (as bad as it gets). See miss_*() below.
#   2. The misses are averaged, each counting by its WEIGHTS share. Only scores that are present count, and the
#      shares of the present ones are scaled back up to a whole, so a missing score never drags the day either way.
#   3. Rules come first: any rule break lifts the day's gap to at least RULE_BREAK_FLOOR, whatever else went well.
#
# Missing data is never a miss (coordinator brief, Nov 23): a score that is None, absent, or listed in "missing" is
# left out. A day with fewer than MIN_SCORES present scores gets gap None ("no reading"), not 0 and not 1. Right now
# that is common: scheduling is parked and the 5:30 run is off, and gym/work arrivals don't fire.
#
# Every number below is a starting guess to tune by feel, like the rest of the spec.
import math

# ---- How much each score counts (shares add up to 1.0) ------------------------------------------------------------
# Rules get the biggest share because Jordan's rules matter most. Plan held is next: a rearranged day is the plainest
# sign the plan was wrong. Ride times next. Pins last: a pin that misfires is mostly a phone-side problem today
# (gym/work arrivals don't fire), so it says less about her planning.
WEIGHTS = {
    "rule_breaks":    0.40,
    "plan_held":      0.25,
    "arrival_miss_min": 0.20,
    "pin_hit_rate":   0.15,
}
SCORES = tuple(WEIGHTS)
# 5.1 scores carried through to the log but given no weight (5.6 merge; ask Jordan before weighting them).
LOGGED_ONLY = ("ride_est_miss_min", "ride_est_wins")

# A day needs at least this many of the four scores to get a gap. One score alone is too thin to call a day good or
# bad (for example "0 rule breaks" on a day with no plan says nothing).
MIN_SCORES = 2

# Any rule break makes the day read at least this far off, even if everything else was perfect.
RULE_BREAK_FLOOR = 0.5
# Rule-break count that counts as a full miss: 1 break is a half miss, 2 or more a full miss.
RULE_BREAKS_FULL = 2

# Arrival times: being off by up to RIDE_GRACE_MIN minutes counts as on time (traffic lights, locking the bike).
# From there the miss climbs in a straight line and is full at RIDE_FULL_MIN minutes off.
RIDE_GRACE_MIN = 2.0
RIDE_FULL_MIN = 15.0

assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9
assert 0 < RIDE_GRACE_MIN < RIDE_FULL_MIN


class GapError(ValueError):
    """Only raised for a broken day record (not a dict, no date). Bad single scores are rejected, not raised."""


# ---- Turning each score into a miss from 0 to 1 --------------------------------------------------------------------
def miss_ride(minutes):
    m = abs(minutes)
    if m <= RIDE_GRACE_MIN:
        return 0.0
    return min(1.0, (m - RIDE_GRACE_MIN) / (RIDE_FULL_MIN - RIDE_GRACE_MIN))


def miss_pins(rate):
    return 1.0 - rate


def miss_plan(held):
    return 1.0 - held


def miss_rules(breaks):
    return min(1.0, breaks / RULE_BREAKS_FULL)


MISS = {
    "rule_breaks": miss_rules,
    "plan_held": miss_plan,
    "arrival_miss_min": miss_ride,
    "pin_hit_rate": miss_pins,
}
# 5.7: her ride estimate can be given weight from the app (Jordan's choice). It is a minutes-off score like arrival
# time, so it uses the same grace and full-miss minutes. Off by default: weight 0, logged only, as 5.6 left it.
OPTIONAL = {"ride_est_miss_min": miss_ride}

_EPS = 1e-9  # rounding slack for shares like 1.0000000002


def _check(name, x):
    """Return (clean value, None) or (None, reason). A bad value is treated as missing, never as a miss."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None, "not a number"
    x = float(x)
    if math.isnan(x) or math.isinf(x):
        return None, "not a real number"
    if name in ("pin_hit_rate", "plan_held"):
        if x < -_EPS or x > 1 + _EPS:
            return None, "share outside 0 to 1"
        return min(1.0, max(0.0, x)), None
    if name == "rule_breaks":
        if x < 0 or x != int(x):
            return None, "not a whole count of 0 or more"
        return x, None
    # arrival_miss_min / ride_est_miss_min: early or late both count, so the sign doesn't matter
    return x, None


def _settings(settings):
    """5.7: Jordan's edits from the app (rivia_progress_settings.gap_settings()), or the code's values when None.
    Only the scores in "weights" count; a score switched off is left out, never counted as a zero."""
    if settings is None:
        return {"weights": dict(WEIGHTS), "grace_min": RIDE_GRACE_MIN, "full_min": RIDE_FULL_MIN,
                "rule_breaks_full": RULE_BREAKS_FULL, "rule_floor": RULE_BREAK_FLOOR, "min_scores": MIN_SCORES}
    return settings


def _miss(name, val, st):
    if name in ("arrival_miss_min", "ride_est_miss_min"):
        m = abs(val)
        if m <= st["grace_min"]:
            return 0.0
        return min(1.0, (m - st["grace_min"]) / (st["full_min"] - st["grace_min"]))
    if name == "rule_breaks":
        return min(1.0, val / st["rule_breaks_full"])
    return MISS[name](val)


def daily_gap(day, settings=None):
    """One 5.1 day record in, one gap reading out. settings: 5.7's edits (see _settings), or None for the code's.

    Returns {
        "date": as given,
        "gap": float 0..1, or None when too little data,
        "misses": {score: miss 0..1} for the scores used,
        "used": [scores used], "missing": [scores left out as missing],
        "rejected": {score: reason} for scores with a value that couldn't be used (also left out),
        "floored": True if a rule break lifted the gap to the rule floor,
        "off": [scores Jordan switched off in the app: left out, not counted as zero],
        "logged": {score: value} for LOGGED_ONLY scores (shown in the log, never weighted),
        "why_none": plain reason when gap is None, else None,
    }
    """
    if not isinstance(day, dict):
        raise GapError("day must be a dict")
    if "date" not in day:
        raise GapError("day has no date")
    flagged = day.get("missing") or []
    if isinstance(flagged, str):
        flagged = [flagged]
    flagged = set(flagged)

    st = _settings(settings)
    weights = st["weights"]
    on = [n for n in SCORES + tuple(OPTIONAL) if weights.get(n, 0) > 0]
    off = [n for n in SCORES + tuple(OPTIONAL) if n not in on]
    misses, missing, rejected, values = {}, [], {}, {}
    for name in on:
        raw = day.get(name)
        if name in flagged or raw is None:
            missing.append(name)
            continue
        val, reason = _check(name, raw)
        if reason:
            rejected[name] = reason
            continue
        values[name] = val
        misses[name] = _miss(name, val, st)
    # The rule floor holds even when the rules score is switched off (Jordan, Nov 23: "keep the rule").
    rb = day.get("rule_breaks")
    if "rule_breaks" not in values and "rule_breaks" not in flagged and rb is not None:
        v, reason = _check("rule_breaks", rb)
        if not reason:
            values["rule_breaks"] = v

    out = {
        "date": day["date"], "gap": None, "misses": misses, "used": list(misses),
        "missing": missing, "rejected": rejected, "floored": False, "why_none": None,
        "logged": {n: day.get(n) for n in LOGGED_ONLY},
    }
    if off != ["ride_est_miss_min"]:
        out["off"] = off  # only shown when Jordan's edits differ from 5.6's default
    if len(misses) < st["min_scores"]:
        out["why_none"] = "only %d of %d scores present, need %d" % (len(misses), len(on), st["min_scores"])
        return out

    total = sum(weights[n] for n in misses)
    gap = sum(weights[n] * m for n, m in misses.items()) / total
    if values.get("rule_breaks", 0) > 0 and gap < st["rule_floor"]:
        gap = st["rule_floor"]
        out["floored"] = True
    out["gap"] = round(min(1.0, max(0.0, gap)), 4)
    return out


def daily_gaps(days, settings=None):
    """A list of 5.1 day records in, a list of readings out, oldest first. Days 5.3 can't use have gap None."""
    return sorted((daily_gap(d, settings) for d in days), key=lambda r: str(r["date"]))
