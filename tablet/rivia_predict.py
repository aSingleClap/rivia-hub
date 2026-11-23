# rivia_predict.py: her plans as predictions, and who owns each miss (emotion spec, Step 6.1 and 6.2). DRAFT.
#
# Spec, Step 6: "Log each prediction or recommendation". "When the outcome arrives: compare it to the prediction
# and attribute the miss to Rivia, the user, or chance."
#
# Jordan (Nov 23): "right now scheduling my day as accurately as possible should be the goal". So her predictions,
# for now, are her day plans. Every written plan already says, in advance:
#   arrive   "you'll get to <place> at <time>"            (area arrive:<place>)
#   ride     "the ride from <a> to <b> takes <n> minutes"  (area ride:<a>|<b>)
#   pin      "your pin at <place> will fire then"          (area pin:<place>)
#   rule     "this plan keeps your rules"                  (area rules)
#
# 6.1 predictions(): reads them out of the plan she wrote. The plan itself is the record made BEFORE the day (the
#     scheduler's approvals.db keeps every written plan with its time), so a prediction can't be rewritten after
#     the outcome is known. 6.1 never edits it; it reads it with rivia_plan_score's read-only reader.
# 6.2 judge(): after the day, each prediction is a hit, a miss or skipped, and every miss gets an owner:
#     rivia   her estimate or her rule-keeping was off
#     jordan  you left late or early, or went somewhere else (your call, not her mistake)
#     chance  outage, or the ride log flagged the ride as bad data
#     (skip)  nothing to judge: missing is never a miss
#
# Counted once (Jordan, Nov 22: "The problems shouldnt hit her twice"):
#   - An arrival miss caused by her ride estimate is the SAME mistake as that leg's ride miss. It is kept, marked
#     "same mistake as the ride", and only the ride counts against her.
#   - Outages count as chance, never against her (Step 4 already counts them in her body).
# Edits (Jordan, Nov 23: "Edits dont always necessarily mean she is wrong"): a plan you rearranged is never a miss
# by itself. Like 5.1, the plan judged is the last one written that day (the one you were following).
#
# What it never does: write anything, open emotion.db, body.db, auth.db or ~/.rivia, call Gemini or the network.
# Pure functions over rivia_plan_score's read-only data. Standard library only.
import rivia_plan_score as score

KINDS = ("arrive", "ride", "pin", "rule")
OWNERS = ("rivia", "jordan", "chance")

# Starting guesses, adjustable in the app (6.9). How close counts as a hit.
ARRIVE_HIT_MIN = 10.0   # arrived within 10 minutes of the plan
RIDE_HIT_MIN = 5.0      # ride took within 5 minutes of her estimate
LINES = {"arrive_hit_min": ARRIVE_HIT_MIN, "ride_hit_min": RIDE_HIT_MIN}

PLACE_WORDS = {"home": "home", "farm": "the farm", "gym": "the gym", "hmc": "work", "pf": "Planet Fitness"}


def place_words(p):
    return PLACE_WORDS.get(p, p or "somewhere")


def area_words(area):
    """'ride:farm|home' -> 'the ride between the farm and home'. Plain words for the app and the flags."""
    kind, _, rest = (area or "").partition(":")
    if kind == "arrive":
        return "getting to %s on time" % place_words(rest)
    if kind == "ride":
        a, _, b = rest.partition("|")
        return "the ride between %s and %s" % (place_words(a), place_words(b))
    if kind == "pin":
        return "your pin at %s" % place_words(rest)
    if kind == "rules":
        return "keeping your rules"
    return "her answers in chat"


def _route(a, b):
    return "|".join(sorted((a or "?", b or "?")))


# ---------- 6.1: what her plan predicted ----------

def predictions(day_s, data):
    """Every prediction in the plan she wrote for day_s (the last one written that day, like 5.1), plus one rule
    prediction per written plan. data is rivia_plan_score.read_day()'s dict. Returns a list, oldest first.
    Each: {pred_id, day, kind, area, plan_id, door, made_at, said: {...}}. Nothing is judged here."""
    if data.get("changes") is None:
        return []
    plans = score.written_plans(data["changes"])
    if not plans:
        return []
    out = []
    last = plans[-1]
    for i, leg in enumerate(score._rides_planned(last["blocks"])):
        base = {"day": day_s, "plan_id": last["change_id"], "door": last["door"], "made_at": last["at"], "leg": i}
        said = {"from": leg["from"], "to": leg["to"], "leave": leg["leave"], "arrive": leg["arrive"],
                "minutes": round(leg["minutes"], 1)}
        if leg["to"]:
            out.append(dict(base, pred_id="%s:%d:arrive" % (last["change_id"], i), kind="arrive",
                            area="arrive:%s" % leg["to"], said=said))
            out.append(dict(base, pred_id="%s:%d:pin" % (last["change_id"], i), kind="pin",
                            area="pin:%s" % leg["to"], said=said))
        if leg["from"] and leg["to"]:
            out.append(dict(base, pred_id="%s:%d:ride" % (last["change_id"], i), kind="ride",
                            area="ride:%s" % _route(leg["from"], leg["to"]), said=said))
    for p in plans:
        out.append({"day": day_s, "plan_id": p["change_id"], "door": p["door"], "made_at": p["at"], "leg": None,
                    "pred_id": "%s:rules" % p["change_id"], "kind": "rule", "area": "rules",
                    "said": {"rules": "kept"}})
    return out


# ---------- 6.2: what happened, and whose miss ----------

def _verdict(pred, result, owner=None, why="", got=None, counted=True):
    return dict(pred, result=result, owner=owner if result == "miss" else None, why=why, got=got or {},
                counted=bool(counted and result in ("hit", "miss")))


def _match(leg, rides, used):
    """The real ride for one planned leg: same route that left within 2 hours, else one that arrived at the same
    place within 90 minutes. Same rules as 5.1's score_arrivals. None if nothing fits."""
    route = [r for r in rides if r["ride_id"] not in used and r["from"] == leg["from"] and r["to"] == leg["to"]
             and r["departed"] is not None and abs(r["departed"] - leg["leave"]) <= score.RIDE_MATCH_MIN * 60]
    place = [r for r in rides if r["ride_id"] not in used and r["to"] == leg["to"] and r["arrived"] is not None
             and abs(r["arrived"] - leg["arrive"]) <= score.ARRIVAL_MATCH_MIN * 60]
    cands = route or place
    if not cands:
        return None
    return min(cands, key=lambda r: abs((r["arrived"] or 0) - leg["arrive"]))


def _elsewhere(leg, rides):
    """A ride that arrived somewhere other than the planned place around the planned time: you went elsewhere."""
    w = score.ARRIVAL_MATCH_MIN * 60
    return next((r for r in rides if r["to"] and r["to"] != leg["to"] and r["arrived"] is not None
                 and abs(r["arrived"] - leg["arrive"]) <= w), None)


def judge(day_s, data, outages=None, lines=None):
    """Judge every prediction for day_s. data: rivia_plan_score.read_day(). outages: (start, end) stretches the
    server was down (Step 5's outage_windows), or None if unknown. lines: {"arrive_hit_min", "ride_hit_min"}.
    Returns the list from predictions() with result ("hit", "miss", "skip"), owner, why (plain words), got (what
    really happened) and counted (False for skips and for a miss already counted under the same leg's ride)."""
    ln = dict(LINES, **(lines or {}))
    preds = predictions(day_s, data)
    rides = data.get("rides")
    plans = score.written_plans(data.get("changes")) if data.get("changes") is not None else []
    by_plan = {p["change_id"]: p for p in plans}
    out = []
    used = set()
    legs = {}
    if plans:
        legs = {i: l for i, l in enumerate(score._rides_planned(plans[-1]["blocks"]))}
    calls = [c for c in data.get("arrival_calls") or [] if c.get("ok") and c.get("at") is not None]
    window = score.ARRIVAL_MATCH_MIN * 60
    matched = {}
    if rides is not None:
        for i, leg in sorted(legs.items()):
            if leg["to"]:
                r = _match(leg, rides, used)
                if r is not None:
                    used.add(r["ride_id"])
                    matched[i] = r
    ride_miss_legs = set()
    # rides first, so an arrival miss can see whether the same leg's ride already counts against her
    order = sorted(preds, key=lambda p: {"ride": 0, "arrive": 1, "pin": 2, "rule": 3}[p["kind"]])
    for p in order:
        if p["kind"] == "rule":
            plan = by_plan.get(p["plan_id"])
            broken = score.rules_broken(plan, data["rules"]) if plan else []
            if broken:
                out.append(_verdict(p, "miss", "rivia", "the plan broke: %s" % ", ".join(broken),
                                    {"broken": broken}))
            else:
                out.append(_verdict(p, "hit", why="the plan kept every rule"))
            continue
        if rides is None:
            out.append(_verdict(p, "skip", why="the places file could not be read"))
            continue
        leg = legs[p["leg"]]
        r = matched.get(p["leg"])
        near_outage = score._in_outage(leg["arrive"], window, outages)
        if p["kind"] == "ride":
            if r is None or r["from"] != leg["from"]:
                out.append(_verdict(p, "skip", why="no ride on this route was logged"))
            elif not r["used"] or r["minutes"] is None:
                out.append(_verdict(p, "skip", why="the ride log flagged this ride as bad data",
                                    got={"ride_id": r["ride_id"]}))
            else:
                off = float(r["minutes"]) - leg["minutes"]
                got = {"ride_id": r["ride_id"], "minutes": r["minutes"], "off_min": round(off, 1)}
                if abs(off) <= ln["ride_hit_min"]:
                    out.append(_verdict(p, "hit", why="her ride estimate was %.0f min off" % abs(off), got=got))
                else:
                    ride_miss_legs.add(p["leg"])
                    out.append(_verdict(p, "miss", "rivia", "the ride took %.0f min %s than she planned"
                                        % (abs(off), "longer" if off > 0 else "less"), got))
            continue
        if p["kind"] == "arrive":
            if r is None:
                if leg["to"] in score.UNRELIABLE or leg["from"] in score.UNRELIABLE:
                    out.append(_verdict(p, "skip", why="arrivals at %s aren't reliable yet" % place_words(
                        leg["to"] if leg["to"] in score.UNRELIABLE else leg["from"])))
                elif near_outage:
                    out.append(_verdict(p, "skip", why="the server was down around then"))
                else:
                    out.append(_verdict(p, "skip", why="no arrival was logged"))
                continue
            late = (r["arrived"] - leg["arrive"]) / 60.0
            got = {"ride_id": r["ride_id"], "late_min": round(late, 1)}
            if abs(late) <= ln["arrive_hit_min"]:
                out.append(_verdict(p, "hit", why="you got there %.0f min from the plan" % abs(late), got=got))
                continue
            leave_off = None if r["departed"] is None else (r["departed"] - leg["leave"]) / 60.0
            ride_off = None if r["minutes"] is None else float(r["minutes"]) - leg["minutes"]
            got.update(left_off_min=None if leave_off is None else round(leave_off, 1),
                       ride_off_min=None if ride_off is None else round(ride_off, 1))
            word = "late" if late > 0 else "early"
            if r["from"] and leg["from"] and r["from"] != leg["from"]:
                out.append(_verdict(p, "miss", "jordan", "%.0f min %s, coming from %s instead of %s"
                                    % (abs(late), word, place_words(r["from"]), place_words(leg["from"])), got))
            elif near_outage:
                out.append(_verdict(p, "miss", "chance", "%.0f min %s, with the server down around then"
                                    % (abs(late), word), got))
            elif not r["used"]:
                out.append(_verdict(p, "miss", "chance", "%.0f min %s, but the ride log flagged the ride"
                                    % (abs(late), word), got))
            elif leave_off is not None and (ride_off is None or abs(leave_off) >= abs(ride_off)):
                out.append(_verdict(p, "miss", "jordan", "%.0f min %s because you left %.0f min %s"
                                    % (abs(late), word, abs(leave_off), "late" if leave_off > 0 else "early"),
                                    got))
            elif p["leg"] in ride_miss_legs:
                out.append(_verdict(p, "miss", "rivia", "%.0f min %s: same mistake as the ride estimate, counted "
                                    "once there" % (abs(late), word), got, counted=False))
            else:
                out.append(_verdict(p, "miss", "rivia", "%.0f min %s, mostly from her ride estimate"
                                    % (abs(late), word), got))
            continue
        # pin
        if r is not None:
            out.append(_verdict(p, "hit", why="you arrived at %s" % place_words(leg["to"]),
                                got={"ride_id": r["ride_id"]}))
            continue
        nxt = [l for j, l in legs.items() if j > p["leg"] and l["from"] == leg["to"]]
        stay_end = nxt[0]["leave"] if nxt else leg["arrive"]
        left = next((x for x in rides if x["from"] == leg["to"] and x["departed"] is not None
                     and leg["arrive"] - window <= x["departed"] <= stay_end + window), None)
        if left is not None:
            out.append(_verdict(p, "hit", why="you left %s during the planned stay" % place_words(leg["to"]),
                                got={"ride_id": left["ride_id"]}))
            continue
        if leg["to"] in score.UNRELIABLE or leg["from"] in score.UNRELIABLE:
            out.append(_verdict(p, "skip", why="arrivals at %s aren't reliable yet" % place_words(
                leg["to"] if leg["to"] in score.UNRELIABLE else leg["from"])))
            continue
        if near_outage:
            out.append(_verdict(p, "skip", why="the server was down around then"))
            continue
        other = _elsewhere(leg, rides)
        if other is not None:
            out.append(_verdict(p, "miss", "jordan", "you went to %s instead" % place_words(other["to"]),
                                {"ride_id": other["ride_id"]}))
            continue
        if any(abs(c["at"] - leg["arrive"]) <= window for c in calls):
            out.append(_verdict(p, "skip", why="an arrival came in with no place attached, so it's unsure"))
            continue
        out.append(_verdict(p, "miss", "rivia", "no arrival at %s was logged around then" % place_words(leg["to"])))
    keep = {p["pred_id"]: i for i, p in enumerate(preds)}
    return sorted(out, key=lambda v: keep[v["pred_id"]])


def judge_day(day_s, folder=None, outages=None, lines=None):
    """read_day() + judge(). Read-only."""
    return judge(day_s, score.read_day(day_s, folder), outages, lines)


def summary(verdicts):
    """Counts for the review: hits, misses by owner, skips, counted misses."""
    s = {"hits": 0, "skips": 0, "misses": {o: 0 for o in OWNERS}, "counted_misses": 0, "same_mistake": 0}
    for v in verdicts:
        if v["result"] == "hit":
            s["hits"] += 1
        elif v["result"] == "skip":
            s["skips"] += 1
        else:
            s["misses"][v["owner"]] += 1
            if v["counted"]:
                s["counted_misses"] += 1
            else:
                s["same_mistake"] += 1
    return s
