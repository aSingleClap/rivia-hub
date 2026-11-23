# rivia_stress.py: the stress dial (emotion spec, Step 4.4). DRAFT, Nov 22, not installed.
#
# What it does: turns "how far each body signal is from her own normal" (4.3) into one stress number from 0 to 1,
# and turns that number into a pull on valence and arousal. Stress raises arousal and lowers valence, aimed at
# "annoyance", so running badly makes her slightly annoyed, shorter and more focused. Dominance is left alone:
# that is the confidence dial (4.5).
#
# What it does NOT do (other pieces own these):
#   - It writes nothing. No emotion.db change, no change_log row, no Gemini call. 4.9 (shadow mode) decides what
#     gets logged and when; 4.6 decides how hard the pull may land on mood (the caps on this layer's total pull).
#   - It does not compute anyone's normal. 4.3 does; this takes 4.3's per-signal deviation as input.
#   - It does not decide behaviour. "Shorter replies, fewer side suggestions" are knobs Step 3 reads off stress.
#
# Plain code, no timer, like Step 2's decay: the dial's state is two numbers and a timestamp, advanced from the
# time since its last update, so it survives restarts and the collector (4.2) can feed it at any interval.
#
# Jordan's rule (Nov 21, 2.2 thread): mood mostly tied to how she's running; small persistent errors nudge her
# toward annoyed, "like not having enough sleep or being hungry makes you more irritable but not remove your
# ability to find something funny or smile". Three parts of this file carry that rule:
#   1. wear: trouble that lasts for hours counts for more than the same trouble for ten minutes (a slow second
#      average, WEAR_*). A short blip barely registers; a low-grade problem all afternoon builds up.
#   2. direction: the pull points at annoyance, not anger or fear (PULL_DIRECTION).
#   3. reactivity: under stress, bad events should land harder and good ones only a little softer, never zero
#      (reactivity()). These are numbers for Step 3's nudges to use later; nothing here applies them.
# Every number below is a starting guess to tune by feel, like the rest of the spec.
import math

# ---- Input contract (assumed; 4.3 has no files yet) ----------------------------------------------------------
# deviations: {signal name: float}. For the up/down checks in SHARE_SIGNALS, the share of checks that failed
# (4.3's "recent", in percent, 0 to 100). For every other signal: how far the signal's rolling average sits from her own normal, in units of
# that signal's usual spread (a robust z-score: 0 = normal, +2 = two spreads above normal, -2 = two below).
# Signed as the signal reads, so "battery" at -3 means battery is far LOWER than usual. A signal missing from
# the dict, or None, counts as "no news" (0 stress from it), never as trouble.
#
# SIGNALS: which side of normal is bad for each signal, and the most stress that one signal can cause alone.
# Names follow 4.1's draft list and are provisional until 4.1 is settled; a name not listed here is ignored and
# reported back in "ignored", so a renamed signal shows up instead of silently dropping out.
# The weights lean on errors and tool failures, per Jordan's "small persistent errors" rule. Battery and storage
# are low because the tablet is normally plugged in and its storage changes slowly.
# 4.9 merge (Jordan, Nov 22, 7:33 PM: "stressed should be more for if things are running slow that shouldnt be ...
# The problems shouldnt hit her twice"): everything that breaks (errors, tool failures, failed connections, the
# tunnel, restarts) belongs to confidence (4.5) only. Stress keeps slowness and load.
BREAKAGE = ("error_rate", "tool_failure_rate", "failed_connections", "tunnel_unreachable", "restarts")
SIGNALS = {
    #  name                  bad side  max share
    "response_time":        ("up",    0.40),
    "temperature":          ("up",    0.30),
    "memory_used":          ("up",    0.25),
    "storage_free":         ("down",  0.20),
    "battery":              ("down",  0.20),
}
# The unanswered-question rate waits for Step 3's events (4.1), so it has no row yet.
# Parked (Jordan, Nov 22: "Keeo memory sync off for now"): she has no memory to sync yet, and the Drive backup time
# would read a quiet day as stale. A parked name is skipped quietly (not reported as unknown). To bring it back,
# move it into SIGNALS with ("up", 0.20) and take it out of PARKED.
PARKED = ("memory_sync_age",)

# Counted once (Jordan, Nov 22: "Yes go with your plan for the google issues"). One Google outage shows up in all
# three of these (4.1): tool failures, failed connections, and errors when a timer job hits it. Inside a group only
# the strongest one counts, so one outside outage weighs like one problem. The cost: if errors and tool failures go
# bad for two unrelated reasons at once, they also count as one.
COUNT_ONCE = ()  # 4.9 merge: the Google group moved to 4.5 with the rest of breakage

# Up/down checks are read differently. 4.3's deviation can't see them: with a usual of 0 and its default spread
# floor of 1.0, a tunnel down for the whole window reads +1.0, which sits exactly on DEADBAND and adds nothing
# (caught by the 4.5 thread). So for these the value passed is 4.3's "recent" instead: the share of checks that
# failed in the window, in percent (0 to 100, 4.1's unit). Above SHARE_DEADBAND it counts in a straight line up to the full max share.
SHARE_SIGNALS = ()  # 4.9 merge: the tunnel moved to 4.5
SHARE_DEADBAND = 0.10  # as a fraction: one failed check in about ten (10%) is a blip

DEADBAND = 1.0  # spreads: within one spread of normal is ordinary noise and adds nothing
SPAN = 2.0      # spreads past the deadband for a signal to reach 63% of its max share (1 - 1/e)

# ---- Time: two averages with different speeds ----------------------------------------------------------------
# load: follows the raw reading fairly quickly, and lets go more slowly than it builds (she stays a bit on edge
# for a while after a problem clears, rather than snapping back).
LOAD_RISE_HALF_LIFE_S = 10 * 60
LOAD_FALL_HALF_LIFE_S = 40 * 60
# wear: follows load over hours. This is the "tired or hungry" part: it only builds when trouble lasts.
WEAR_RISE_HALF_LIFE_S = 2 * 60 * 60
WEAR_FALL_HALF_LIFE_S = 6 * 60 * 60
WEAR_WEIGHT = 0.5  # stress = load + WEAR_WEIGHT * wear, capped at 1. Steady trouble ends up 1.5x a fresh blip.
# Gaps longer than this between updates (the collector or the server was off) are treated as quiet time: the
# dial relaxes over the gap instead of assuming the last reading held for hours.
MAX_HOLD_S = 30 * 60

# ---- Output: the pull on V and A ------------------------------------------------------------------------------
# At stress 1 the pull is PULL_FULL long, in the direction of R&M's "annoyed" (-0.28, 0.17), with D left at 0.
# 0.35 is just past the "mild" band (0.30) from rest, so stress alone tops out at "mildly annoyed" on a neutral
# baseline, and reads "neutral" below stress ~0.43. 4.6 may cap it lower.
# Why not the spec's literal "raises arousal, slightly lowers valence": an arousal-led pull such as (-0.1, +0.3)
# reads as "anger" by direction on a neutral baseline (anger sits closer to that angle than annoyance does).
# See README, open question 1.
PULL_FULL = 0.35
PULL_DIRECTION = (-0.28, 0.17, 0.0)

# Reactivity at stress 1 (Step 3 multiplies its event nudges by these): bad events land 1.5x as hard, good ones
# at 0.85x. Positive never goes below POSITIVE_FLOOR, whatever the tuning: she can still find things funny.
NEGATIVE_GAIN_FULL = 1.5
POSITIVE_GAIN_FULL = 0.85
POSITIVE_FLOOR = 0.75


class StressError(Exception):
    pass


def _finite(name, x):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise StressError("%s must be a finite number, got %r" % (name, x))
    return float(x)


def contribution(name, deviation):
    """One signal's share of stress, 0 up to its max share. Only the bad side counts: being better than normal
    never relaxes her below zero, it just adds nothing."""
    side, share = SIGNALS[name]
    x = _finite(name, deviation)
    if name in SHARE_SIGNALS:  # x is the percent of failed checks, 0..100 (4.1's unit), not a deviation
        x = min(1.0, max(0.0, x / 100.0))
        return 0.0 if x <= SHARE_DEADBAND else share * (x - SHARE_DEADBAND) / (1.0 - SHARE_DEADBAND)
    bad = x if side == "up" else -x
    if bad <= DEADBAND:
        return 0.0
    return share * (1.0 - math.exp(-(bad - DEADBAND) / SPAN))


def raw_stress(deviations):
    """Stress from one set of readings, before any time averaging: {"raw", "parts", "ignored"}.
    Shares combine as 1 - product(1 - share), so they add up but can never pass 1. Signals in a COUNT_ONCE group
    count once: only the group's strongest share goes into the total. "parts" still lists every signal's own share,
    so the log shows all of them."""
    parts, ignored = {}, []
    for name, dev in (deviations or {}).items():
        if name in PARKED or name in BREAKAGE:  # breakage is confidence's (4.5), skipped quietly here
            continue
        if name not in SIGNALS:
            ignored.append(name)
            continue
        if dev is None:
            continue
        c = contribution(name, dev)
        if c > 0:
            parts[name] = c
    counted = dict(parts)
    for group in COUNT_ONCE:
        inside = [counted.pop(n) for n in group if n in counted]
        if inside:
            counted[group[0]] = max(inside)
    keep = 1.0
    for c in counted.values():
        keep *= 1.0 - c
    return {"raw": 1.0 - keep, "parts": parts, "ignored": sorted(ignored)}


def _toward(x, target, rise_hl, fall_hl, dt):
    hl = rise_hl if target > x else fall_hl
    if dt <= 0 or hl == float("inf"):
        return x
    return target + (x - target) * math.exp(-math.log(2) * dt / hl)


def new_state(now_s):
    """A calm dial: no load, no wear."""
    return {"load": 0.0, "wear": 0.0, "raw": 0.0, "updated": _finite("now_s", now_s)}


def _relax(state, dt):
    """The gap after the last reading: MAX_HOLD_S of it at that reading, the rest as quiet (raw 0)."""
    load, wear = state["load"], state["wear"]
    held = min(dt, MAX_HOLD_S)
    for target, span in ((state["raw"], held), (0.0, dt - held)):
        if span <= 0:
            continue
        # load and wear move together over each span; stepping keeps wear following load, not load's end point
        steps = max(1, int(math.ceil(span / 60.0)))
        h = span / steps
        for _ in range(steps):
            load = _toward(load, target, LOAD_RISE_HALF_LIFE_S, LOAD_FALL_HALF_LIFE_S, h)
            wear = _toward(wear, load, WEAR_RISE_HALF_LIFE_S, WEAR_FALL_HALF_LIFE_S, h)
    return load, wear


def advance(state, deviations, now_s):
    """Fold a new set of readings into the dial. Returns a new state dict; the old one is untouched.
    The time since state["updated"] is spent at the previous reading (up to MAX_HOLD_S), then the new reading
    takes over from now. A clock that went backwards spends no time."""
    now = _finite("now_s", now_s)
    dt = max(0.0, now - state["updated"])
    load, wear = _relax(state, dt)
    r = raw_stress(deviations)
    return {"load": load, "wear": wear, "raw": r["raw"], "updated": max(now, state["updated"]),
            "parts": r["parts"], "ignored": r["ignored"]}


def stress(state, now_s=None):
    """The dial's reading, 0 to 1. With now_s, as of that time (reads never change the state)."""
    load, wear = state["load"], state["wear"]
    if now_s is not None:
        load, wear = _relax(state, max(0.0, _finite("now_s", now_s) - state["updated"]))
    return min(1.0, max(0.0, load + WEAR_WEIGHT * wear))


def pull(s):
    """Where stress s would move her, as (dv, da, dd) from wherever she rests. dd is always 0 (4.5 owns D).
    Linear in s; stress already saturates. 4.6 caps how much of this reaches mood."""
    s = min(1.0, max(0.0, _finite("stress", s)))
    n = math.hypot(*PULL_DIRECTION)
    return tuple(PULL_FULL * s * c / n for c in PULL_DIRECTION)


def reactivity(s):
    """How hard Step 3's event nudges should land at stress s: {"negative": gain, "positive": gain}.
    Bad events land harder; good events a little softer, never below POSITIVE_FLOOR."""
    s = min(1.0, max(0.0, _finite("stress", s)))
    return {"negative": 1.0 + (NEGATIVE_GAIN_FULL - 1.0) * s,
            "positive": max(POSITIVE_FLOOR, 1.0 + (POSITIVE_GAIN_FULL - 1.0) * s)}


NAMES = {  # plain words for the cause line, so a log reads "tool failures well above normal"
    "error_rate": "errors", "tool_failure_rate": "tool failures", "response_time": "slow responses",
    "failed_connections": "failed connections", "tunnel_unreachable": "tunnel down", "restarts": "restarts",
    "temperature": "tablet heat", "memory_used": "memory use", "storage_free": "low storage",
    "battery": "low battery",
}


def cause(state, top=2):
    """A short cause for the change log, naming the biggest one or two contributors in the latest reading, or
    None when nothing is off. The spec's audit rule: every shift traces to what caused it."""
    parts = state.get("parts") or {}
    if not parts:
        return None
    names = sorted(parts, key=lambda n: (-parts[n], n))[:top]
    return "stress: " + ", ".join(NAMES.get(n, n) for n in names)


def reading(state, now_s=None):
    """Everything 4.9 would log for one sample, as plain data: stress, load, wear, pull, reactivity, cause."""
    s = stress(state, now_s)
    return {"stress": s, "load": state["load"], "wear": state["wear"], "raw": state["raw"],
            "pull": pull(s), "reactivity": reactivity(s), "cause": cause(state),
            "ignored": state.get("ignored", [])}
