# rivia_area_confidence.py: how sure she is in each area, after her own misses (emotion spec, Step 6.4 and 6.5).
# DRAFT.
#
# Spec, Step 6: "If it was her miss: lower her confidence for that category only, so she hedges more there and
# nowhere else." "Decay: The confidence penalty fades over time, so one miss doesn't make her timid for weeks."
# Tuning list: "Confidence penalty decay: about 1 week".
#
# 6.4 confidence(): one number per area, 1.0 = her normal, never above it. Each day she missed in an area (6.2 says
#     the miss was hers and counted, or you gave a thumbs down there, 6.3) takes one penalty off that area. The
#     penalty fades by half every 7 days. It can't go under the floor, so she never freezes up in an area.
# 6.5 flags(): an area under 0.70 gets a "double-check" flag, in plain words ("double-check the ride between the farm
#     and home"). It clears once the area is back over 0.80, so it doesn't flicker. Same lines as Step 4.5's dial.
#     Shadow only: the flag is logged, and nothing reaches her replies. Using it is for when expression is on.
#
# Counted once (Jordan, Nov 22: "The problems shouldnt hit her twice"):
#   - One penalty per area per day, however many misses or thumbs down that day (a late arrival AND a thumbs down
#     about it are one mistake).
#   - Only misses 6.2 says are hers count. Outages and bad ride data are chance; your late leaving is yours.
#   - This is separate from Step 4's body confidence (things breaking right now). An outage is chance here, so it
#     never lowers both.
#   - It adds no mood. Step 5 already moves her mood from the same days.
#
# Emotion only adds caution: nothing here can make her surer than normal or skip a check. Pure functions.
import rivia_predict as predict

PENALTY = 0.15            # one day's miss in an area
HALF_LIFE_DAYS = 7.0      # spec tuning list: about 1 week
FLOOR = 0.30              # an area never reads lower than this
FLAG_BELOW = 0.70         # Step 4.5's double-check line
FLAG_CLEAR = 0.80         # Step 4.5's clear line
TUNE = {"penalty": PENALTY, "half_life_days": HALF_LIFE_DAYS, "floor": FLOOR, "flag_below": FLAG_BELOW,
        "flag_clear": FLAG_CLEAR}


def strikes(verdicts, corrections, since=None):
    """{(day, area): at} for each day and area with her own counted miss or a correction from Jordan.
    verdicts: stored 6.2 rows with day, area, result, owner, counted, judged_at. corrections: 6.3 down signals.
    At is when it was learned (judged or sent). One per area per day."""
    out = {}
    for v in verdicts:
        if v["result"] == "miss" and v["owner"] == "rivia" and v["counted"]:
            at = v.get("judged_at") or 0
            if since is not None and at < since:
                continue
            k = (v["day"], v["area"])
            out[k] = min(out.get(k, at), at)
    for c in corrections:
        if since is not None and c["at"] < since:
            continue
        k = (c["day"], c["area"])
        out[k] = min(out.get(k, c["at"]), c["at"])
    return out


def confidence(verdicts, corrections, now, tune=None, since=None):
    """{area: {"value", "strikes", "words"}} for every area that has a strike still fading. Areas with no strike are
    at 1.0 and not listed."""
    t = dict(TUNE, **(tune or {}))
    per = {}
    for (day, area), at in strikes(verdicts, corrections, since).items():
        age = max(0.0, now - at)
        per.setdefault(area, []).append(t["penalty"] * 0.5 ** (age / (t["half_life_days"] * 86400.0)))
    out = {}
    for area, pens in per.items():
        total = sum(pens)
        if total < 0.005:
            continue
        out[area] = {"value": round(max(t["floor"], 1.0 - total), 3), "strikes": len(pens),
                     "words": predict.area_words(area)}
    return out


def flags(conf, prev=(), tune=None):
    """Areas flagged for double-checking. prev: areas flagged last time (for the clear line). Returns a list of
    {"area", "value", "say"} sorted lowest first. "say" is what she'd do, in words; it goes to the log only."""
    t = dict(TUNE, **(tune or {}))
    prev = set(prev or ())
    out = []
    for area, c in conf.items():
        v = c["value"]
        if v < t["flag_below"] or (area in prev and v < t["flag_clear"]):
            out.append({"area": area, "value": v, "say": "double-check %s" % c["words"]})
    return sorted(out, key=lambda f: (f["value"], f["area"]))


def caution_lines(fl):
    """The caution flags Step 3's business pass would get (spec: code-generated caution flags only, "verify before
    answering"). Built here, logged only; nothing feeds them to her yet."""
    return ["verify before answering about %s" % predict.area_words(f["area"]) for f in fl]
