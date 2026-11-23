# rivia_confidence.py: the confidence dial (emotion spec, Step 4.5). DRAFT, Nov 22, not installed.
#
# Spec, Step 4 "Two dials": "Confidence: sets dominance. When it is low she double-checks and flags blind
# spots." Confidence here is code's answer to "can she trust what she sees and reach what she needs", worked
# out from her body's health signals measured against her own normal (4.3). It is never her own estimate:
# the spec's Step 7 says self-reported confidence is poorly calibrated, and Xiong et al. (2023), "Can LLMs
# Express Their Uncertainty?", arXiv:2306.13063, found LLMs "tend to be overconfident" when they state it.
#
# What this file does:
#   reading(devs)  ->  {"confidence", "pull", "double_check", "blind_spots", "parts", ...}
#     confidence    0..1. 1.0 means every confidence signal is at or better than her normal. It never goes
#                   above 1: a better-than-usual day does not make her surer than usual (the spec's "emotion
#                   only increases caution"). It falls as signals get worse than her normal or go stale.
#     pull          {"dd": <= 0}: how far it wants her dominance below where it would otherwise be, before any
#                   cap. Zero until confidence falls under WORRY_BELOW, so only a big drop reads as "worried". 4.6 caps the body layer's total pull and decides which layer it lands on; 4.9 logs it
#                   in shadow mode. This file never writes the emotion state.
#     double_check  True while confidence is low (with hysteresis, so it doesn't flap at the line). It is a
#                   flag other code reads (Step 3.5's caution flags, 4.7). The behaviour itself comes later.
#     blind_spots   the signals behind the drop, worst first, each with why: "worse" (worse than her normal),
#                   "stale" (no fresh sample), "down" (a reachability check failing). Always worked out, so
#                   the log shows them; flag_blind_spots says when she should bring them up, which is only
#                   while double_check is on, and only to explain a choice (spec Step 1: "information, not mood").
#     unlearned, untracked   signals with no normal yet (4.3's warm-up) and signals 4.3 doesn't report. Free.
#
# Input: 4.3's rivia_normal.normals() as it stands in step4/4.3/ (draft, Nov 22), through adapt_43().
#
# Pure: reads only what it is passed, no DB, no network, no timer, no Gemini. The caller (4.9) passes the
# previous double_check so the hysteresis has memory; nothing here persists.
#
# Every number below is a starting guess to tune by feel, like the rest of the spec.
import math

# Which signals feed confidence, and how much one of them can take away on its own (0..1). Jordan (Nov 22):
# "The problems shouldnt hit her twice", and stress "should be more for if things are running slow that shouldnt
# be and she doesnt know why". So everything that BREAKS (fails, refuses, goes down, restarts) is confidence's,
# and only slowness and load (response times, temperature, memory, storage, battery) are stress's (4.4). No
# signal is in both. Names match 4.1's list; a name 4.3 doesn't report is "untracked" and costs nothing.
WEIGHTS = {
    "tool_failure_rate": 0.45,    # her tools failing is the most direct sign her answers may be wrong
    "failed_connections": 0.35,   # MCP clients or upstream APIs refusing her
    "error_rate": 0.25,           # errors in her own logs
    "tunnel_unreachable": 0.35,   # the Cloudflare tunnel (the spec says Tailscale); down = she can't be reached
    "restarts": 0.25,             # the server restarting on its own: something broke and she may have lost track
}
# One cause, counted once. A Google outage shows up as tool failures, failed connections and errors at the same
# time (4.1). Within a group only the costliest signal counts, the same rule Jordan said yes to for 4.4
# ("Yes go with your plan for the google issues"). The others are still listed as blind spots.
GROUPS = (("tool_failure_rate", "failed_connections", "error_rate"),)
# Parked: memory sync (Jordan, Nov 22, in 4.4: "Keeo memory sync off for now"). She has no memory to sync yet.
# A parked name is skipped quietly. To bring it back, give it a weight in WEIGHTS (0.30 was the draft's).
PARKED = ("memory_sync_age",)
# Which side of her normal is bad. 4.3 reports deviation signed as the signal reads (direction only), so each
# dial says which way is bad. All of these are bad going up.
BAD_SIDE = {name: "up" for name in WEIGHTS}
# Reachability checks, read from 4.3's "recent" (the share of checks in the last 15 minutes that failed, in percent)
# instead of the deviation. A rolling normal can't judge these: with errors usually 0, 4.3's spread floor is 1.0,
# so a tunnel down for the whole window reads deviation +1.0, inside the dead zone, i.e. normal. And a tunnel that
# drops often would learn "down" as her normal. Down is bad however often it happens, and counts from day one.
REACHABILITY = ("tunnel_unreachable",)

DEAD_ZONE = 1.0   # within 1 of her usual spread worse than normal counts as normal: ordinary wobble (4.4 uses 1.0 too)
FULL_AT = 3.0     # 3 spreads worse than normal counts as fully bad for that signal
STALE_PENALTY = 0.6       # a stale signal (4.3: no sample in the last 15 minutes) costs this share of its
                          # weight: not knowing is bad, but not as bad as knowing it's broken
UNLEARNED_PENALTY = 0.0   # no normal yet (4.3's warm-up, 3 days): listed, costs nothing, so her first days
                          # after install don't read as one long low-confidence stretch

MAX_DOMINANCE_PULL = 0.30   # at confidence 0 she wants dominance this far down. Same size as the spec's
                            # per-turn emotion cap; 4.6 caps the body's total lower (0.25).
WORRY_BELOW = 0.70          # Jordan (Nov 22): worried only when confidence drops a lot. Above this line the
                            # dial pulls nothing, so a small dip leaves annoyed as annoyed. Below it, the pull
                            # grows to MAX_DOMINANCE_PULL at confidence 0. Same line as double_check.
DOUBLE_CHECK_BELOW = 0.70   # double_check turns on when confidence drops under this...
DOUBLE_CHECK_CLEAR = 0.80   # ...and turns off again only once it is back above this


class ConfidenceError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _num(name, x, *, allow_none=False):
    if x is None and allow_none:
        return None
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise ConfidenceError("bad_value", "%s must be a finite number, got %r" % (name, x))
    return float(x)


def _flag(name, x):
    if not isinstance(x, bool):
        raise ConfidenceError("bad_value", "%s must be True or False, got %r" % (name, x))
    return x


def adapt_43(raw):
    """The one place that knows 4.3's output shape: rivia_normal.normals(), {signal: reading}, where a reading
    has (among others) "deviation" (signed, None until trusted), "trusted", "stale", "recent", "at",
    "last_at" and "warmup": {"done"}. Returns, per signal this dial uses:
    {"worse": deviation turned so positive = worse (None until trusted), "stale", "learned", "recent",
     "age_s": seconds since the newest sample, or None if there has never been one}.
    If 4.3's shape changes, change only this function."""
    if not isinstance(raw, dict):
        raise ConfidenceError("bad_value", "expected a dict of signals, got %r" % (type(raw).__name__,))
    out = {}
    for name, s in raw.items():
        if name not in WEIGHTS:
            continue  # stress-only, parked or unknown signals are not this dial's business
        if not isinstance(s, dict):
            raise ConfidenceError("bad_value", "%s: expected 4.3's reading (a dict), got %r" % (name, s))
        stale = _flag(name + ".stale", s.get("stale"))
        trusted = _flag(name + ".trusted", s.get("trusted"))
        warm = s.get("warmup")
        learned = _flag(name + ".warmup.done", warm.get("done") if isinstance(warm, dict) else None)
        dev = _num(name + ".deviation", s.get("deviation"), allow_none=True)
        if trusted and dev is None:
            raise ConfidenceError("bad_value", "%s: trusted but no deviation" % name)
        recent = _num(name + ".recent", s.get("recent"), allow_none=True)
        at = _num(name + ".at", s.get("at"))
        last = _num(name + ".last_at", s.get("last_at"), allow_none=True)
        age = None if last is None else at - last
        if age is not None and age < 0:
            raise ConfidenceError("bad_value", "%s: newest sample is after the reading's time" % name)
        worse = None
        if trusted:
            worse = dev if BAD_SIDE[name] == "up" else -dev
        out[name] = {"worse": worse, "stale": stale, "learned": learned, "recent": recent, "age_s": age}
    return out


def _badness(name, s):
    """How bad one reported signal is, 0..1 of its weight, and why: (badness, why) where why is None,
    "worse", "stale", "down" or "unlearned"."""
    if s["stale"] or s["recent"] is None:
        return STALE_PENALTY, "stale"
    if name in REACHABILITY:
        # the share of recent checks that failed. 4.3 gives it in percent (0..100, 4.1's unit); the 4.5 draft read
        # it as 0..1, so one failed check in 15 minutes (6.7%) cost the full weight. Fixed in the 4.9 merge.
        x = min(1.0, max(0.0, s["recent"] / 100.0))
        return x, ("down" if x > 0 else None)
    if s["worse"] is None:
        return UNLEARNED_PENALTY, "unlearned"
    x = (s["worse"] - DEAD_ZONE) / (FULL_AT - DEAD_ZONE)  # better than normal lands at 0, never below
    x = min(1.0, max(0.0, x))
    return x, ("worse" if x > 0 else None)


def confidence(devs):
    """0..1 from adapt_43's output, and the parts behind it: (c, parts), parts {signal: {"badness", "why",
    "cost"}}. Combined as a noisy-OR: c = product of (1 - weight * badness). Each bad signal takes its share
    of what's left, so several bad signals compound but c never goes below 0, and one signal alone can take
    at most its weight. Within each of GROUPS only the costliest signal counts ("counted"), so one outage that
    shows up three ways costs once. A signal 4.3 doesn't report at all is "untracked" and costs nothing (4.1 may not
    collect it yet); one it reports with no recent sample is "stale" and does cost."""
    parts = {}
    for name, w in WEIGHTS.items():
        s = devs.get(name)
        b, why = (0.0, "untracked") if s is None else _badness(name, s)
        parts[name] = {"badness": b, "why": why, "cost": w * b, "counted": True}
    for group in GROUPS:  # one cause, counted once: only the costliest of the group counts
        members = [n for n in group if n in parts]
        top = max(members, key=lambda n: (parts[n]["cost"], -members.index(n)), default=None)
        for n in members:
            parts[n]["counted"] = n == top
    c = 1.0
    for p in parts.values():
        if p["counted"]:
            c *= 1.0 - p["cost"]
    return c, parts


def reading(raw, *, prev_double_check=False):
    """The dial's whole reading from 4.3's per-signal deviations (raw). prev_double_check is the last
    reading's double_check (False at start), for the hysteresis. Reads only."""
    if not isinstance(prev_double_check, bool):
        raise ConfidenceError("bad_value", "prev_double_check must be True or False")
    devs = adapt_43(raw)
    c, parts = confidence(devs)
    if prev_double_check:
        double_check = c <= DOUBLE_CHECK_CLEAR
    else:
        double_check = c < DOUBLE_CHECK_BELOW
    blind = sorted(((p["cost"], n, p["why"]) for n, p in parts.items()
                    if p["why"] in ("worse", "stale", "down")), reverse=True)
    blind_spots = [{"signal": n, "why": why, "cost": round(cost, 3)} for cost, n, why in blind]
    unlearned = sorted(n for n, p in parts.items() if p["why"] == "unlearned")
    untracked = sorted(n for n, p in parts.items() if p["why"] == "untracked")
    dd = -MAX_DOMINANCE_PULL * max(0.0, WORRY_BELOW - c) / WORRY_BELOW
    return {
        "confidence": c,
        "pull": {"dv": 0.0, "da": 0.0, "dd": dd if dd < 0 else 0.0},
        "double_check": double_check,
        "flag_blind_spots": double_check and bool(blind_spots),
        "blind_spots": blind_spots,
        "unlearned": unlearned,
        "untracked": untracked,
        "parts": parts,
    }


def caution_flags(r):
    """What other code reads from a reading (Step 3.5 caution flags, 4.7): only ever more caution.
    Returns a list of plain names, empty when confidence is normal."""
    flags = []
    if r["double_check"]:
        flags.append("verify_before_answering")
    if r["flag_blind_spots"]:
        flags.append("mention_blind_spots")
    return flags
