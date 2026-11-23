# rivia_plan_score.py: how well Rivia's day plans matched Jordan's real day (emotion spec, Step 5.1). DRAFT.
#
# Jordan (Nov 23): "right now scheduling my day as accurately as possible should be the goal".
# This file turns the scheduler's own records into numbers per day. It only reads.
#
#   arrival_miss_min    THE MAIN ONE. Middle value of |when the plan said Jordan arrives - when he really arrived|.
#                       0 is perfect. Jordan (Nov 23): "Arrival time should be the plan."
#   ride_est_miss_min   second. Middle value of |her planned ride minutes - the real ride minutes|, clean rides only.
#                       Jordan: "Good ride estimates should be more like the second metric."
#   ride_est_wins       a plus: rides where her estimate was closer to the real ride than Jordan's own hand estimate.
#                       Jordan: "If im wrong and she is right then thats a plus". Never negative.
#   pin_hit_rate        of the arrivals her plan expected, the share that came in at the right place.
#   plan_held           of the stops and rides in the first plan she wrote, the share Jordan did not rearrange.
#   rule_breaks         how many of the day's written plans broke one of Jordan's rules. Should be 0.
#
# One dict per day (the shape 5.2 reads):
#   {date, arrival_miss_min, ride_est_miss_min, ride_est_wins, pin_hit_rate, plan_held, rule_breaks,
#    missing: [score names with no data], why: {score name: plain reason it is missing},
#    detail: {counts behind each number}}
#
# Missing is never a miss. A score with nothing to judge is None and its name goes in `missing`.
#   - Gym and work (hmc) arrivals are unreliable from the phone (Jordan, Nov 18: they never notified). The Drive
#     backup shows some rides to and from them were still logged (Nov 16 to 18). So a logged ride there counts,
#     and a missing one is skipped, never counted against her.
#   - Scheduling is parked (Jordan, Nov 21) and the 5:30 run is off, so recent days have no written plan: all the
#     scores are missing for those days.
#   - The server keeps only the LAST arrival, not a history. Places come from logged rides (the app attaches a ride
#     when Jordan left another place within 3 hours). An arrival call with no ride has no place, so an expected
#     arrival that might be one of those is "unsure" and left out, not a miss.
#
# Where it reads (all opened read-only: sqlite "mode=ro", never created, never written):
#   approvals.db  changes        kind='write_day' rows: her day plans (preview JSON: blocks, shift, problems)
#                                 and every setting change, with the door it came from
#   places.db     rides          real rides: from, to, departed, arrived, minutes, used (clean or not)
#   core.db       calls          app_arrival calls (time and ok only; no place)
#   scheduler.db  settings,      Jordan's schedule rules (farm time, workout floor, arrive early, ...)
#                 overrides      and his one-day changes to them
#
# What it never does: write any file, open emotion.db, body.db, auth.db or ~/.rivia, call Gemini or the network,
# change her mood, or act. Rivia only reports (Jordan, Nov 23). It imports no Rivia module, so it cannot start
# scheduling, the planner or a tool by accident.
import json
import os
import sqlite3
import statistics
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")
SCORES = ("arrival_miss_min", "ride_est_miss_min", "ride_est_wins", "pin_hit_rate", "plan_held", "rule_breaks")

# Places whose phone arrivals are unreliable (Jordan, Nov 18: home and farm notify fine; gym and work never did).
# A ride or arrival logged there counts; one that is missing there is skipped, not a miss.
UNRELIABLE = frozenset({"gym", "hmc"})
JORDAN_DOORS = frozenset({"app", "admin", "termux", "web"})  # every door but "system" is Jordan
ARRIVAL_MATCH_MIN = 90   # an arrival this close to the planned time counts for that stop (place matters, not lateness)
RIDE_MATCH_MIN = 120     # a ride that left this close to the planned leave time is that ride
SCORED_KINDS = ("stop", "ride")  # plan_held counts these blocks; "detail" (sleep, flex, arrive early) is padding

# Jordan's rule defaults, same as the scheduler's catalog (rivia_sched.py) at rivia-hub 23bd731.
RULE_DEFAULTS = {"farm_minutes": 45, "workout_floor_minutes": 60, "prework_floor_minutes": 15,
                 "arrive_early_minutes": 10}
# Jordan's own hand ride estimates, minutes, either direction: rivia_planner.MEASURED_LEGS at 23bd731.
# The planner uses these until it has learned a ride from 3 clean trips. ride_est_wins compares hers against them.
HAND_ESTIMATES = {"farm|home": 5, "farm|gym": 20, "farm|hmc": 20, "gym|hmc": 5, "hmc|home": 25, "home|pf": 17}
# Posted gym hours, same as rivia_planner.GYM_HOURS at 23bd731 (hour of day).
GYM_HOURS = {
    "gym": {"weekdayOpen": 5, "weekdayClose": 20, "weekendOpen": 8, "weekendClose": 12},
    "pf": {"weekdayOpen": 5, "weekdayClose": 21, "weekendOpen": 7, "weekendClose": 19},
}


def rivia_dir():
    return os.path.expanduser(os.environ.get("RIVIA_DIR", "~/rivia"))


# ---------- reading, read-only ----------

def _ro(path):
    """Open an existing SQLite file read-only. None if it is not there (never creates one)."""
    if not os.path.isfile(path):
        return None
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)


def _rows(folder, name, sql, args=()):
    """Rows from <name>.db, or None if the file or table is missing or unreadable."""
    try:
        con = _ro(os.path.join(folder, name + ".db"))
    except sqlite3.Error:
        return None
    if con is None:
        return None
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _epoch(v):
    """Epoch seconds from epoch s or ms, an ISO time (no offset = New York), or a {epoch, iso} dict. None if unclear."""
    if isinstance(v, dict):
        v = v.get("epoch", v.get("iso"))
    if v is None or isinstance(v, bool) or v == "":
        return None
    if isinstance(v, (int, float)):
        n = float(v)
        return int(n / 1000.0) if n > 1e11 else int(n)
    try:
        d = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            return _epoch(float(v))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=TZ)
    return int(d.timestamp())


def _local(s):
    return datetime.fromtimestamp(s, TZ)


def _day_bounds(day_s):
    d = date.fromisoformat(day_s)
    start = datetime(d.year, d.month, d.day, tzinfo=TZ)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def _json(text):
    try:
        return json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return None


def read_day(day_s, folder=None):
    """Everything 5.1 needs for one day, as plain lists. Missing files give None (not an empty list)."""
    folder = folder or rivia_dir()
    lo, hi = _day_bounds(day_s)
    pad = 12 * 3600  # rides and calls around midnight belong to the day whose plan expected them
    changes = _rows(folder, "approvals",
                    "select change_id, created, door, kind, scope, day, key, preview, status, decided "
                    "from changes where day = ? order by created, rowid", (day_s,))
    rides = _rows(folder, "places",
                  "select ride_id, from_place, to_place, departed, arrived, minutes, used from rides")
    calls = _rows(folder, "core",
                  "select ts, ok from calls where tool = 'app_arrival' or action = 'sched.arrival'")
    settings = _rows(folder, "scheduler", "select key, value from settings")
    overrides = _rows(folder, "scheduler", "select key, value from overrides where day = ?", (day_s,))

    def near(t):
        return t is not None and lo - pad <= t < hi + pad

    out = {"day": day_s, "changes": None, "rides": None, "arrival_calls": None, "rules": dict(RULE_DEFAULTS)}
    if changes is not None:
        out["changes"] = [dict(zip(("change_id", "created", "door", "kind", "scope", "day", "key", "preview",
                                    "status", "decided"), r)) for r in changes]
    if rides is not None:
        out["rides"] = []
        for rid, frm, to, dep, arr, mins, used in rides:
            dep_s, arr_s = _epoch(dep), _epoch(arr)
            if near(arr_s) or near(dep_s):
                out["rides"].append({"ride_id": rid, "from": frm, "to": to, "departed": dep_s, "arrived": arr_s,
                                     "minutes": mins, "used": bool(used)})
    if calls is not None:
        out["arrival_calls"] = [{"at": _epoch(ts), "ok": bool(ok)} for ts, ok in calls if near(_epoch(ts))]
    for rows in (settings, overrides):  # a one-day override wins over the standing setting
        for key, value in rows or ():
            if key in RULE_DEFAULTS:
                v = _json(value)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out["rules"][key] = v
    return out


# ---------- plans ----------

def _blocks(preview):
    """The plan's blocks with epoch start/end, sorted. Blocks with unclear times are dropped."""
    out = []
    for b in (preview or {}).get("blocks") or []:
        if not isinstance(b, dict):
            continue
        s, e = _epoch(b.get("start")), _epoch(b.get("end"))
        if s is None or e is None:
            continue
        out.append({"title": b.get("title"), "place": b.get("place"), "kind": b.get("kind"), "start": s, "end": e})
    return sorted(out, key=lambda b: (b["start"], b["end"]))


def _rides_planned(blocks):
    """Each ride block with where it leaves from and goes to (the places of the blocks either side)."""
    out = []
    for i, b in enumerate(blocks):
        if b["kind"] != "ride":
            continue
        frm = next((p["place"] for p in reversed(blocks[:i]) if p["kind"] != "ride" and p["place"]), None)
        to = next((n["place"] for n in blocks[i + 1:] if n["kind"] != "ride" and n["place"]), None)
        out.append({"from": frm, "to": to, "leave": b["start"], "arrive": b["end"],
                    "minutes": (b["end"] - b["start"]) / 60.0})
    return out


def written_plans(changes):
    """Applied write_day rows (plans she wrote to the calendar), oldest first, each with its blocks."""
    out = []
    for c in changes or []:
        if c["kind"] != "write_day" or c["status"] != "applied":
            continue
        preview = _json(c["preview"]) or {}
        out.append({"change_id": c["change_id"], "door": c["door"], "at": _epoch(c["decided"]) or _epoch(c["created"]),
                    "preview": preview, "blocks": _blocks(preview)})
    return sorted(out, key=lambda p: p["at"] or 0)


# ---------- the scores ----------

def _hand(frm, to):
    return HAND_ESTIMATES.get("|".join(sorted((frm, to))))


def score_rides(plan, rides):
    """ride_est_miss_min: middle value of |planned - real| ride minutes, over clean rides that match a planned ride.
    ride_est_wins: of those, how many her estimate got closer than Jordan's hand estimate.
    Returns ((miss, why, detail), (wins, why, detail))."""
    misses, skipped = [], {"no_ride_logged": 0, "not_clean": 0}
    vs = {"better": 0, "same": 0, "worse": 0, "no_hand_estimate": 0}
    if rides is None:
        why = "the places file could not be read"
        return (None, why, {}), (None, why, {})
    span = _span(plan)
    free = [r for r in rides if span and r["departed"] is not None and span[0] <= r["departed"] <= span[1]]
    for leg in _rides_planned(plan["blocks"]):
        if not leg["from"] or not leg["to"]:
            skipped["no_ride_logged"] += 1
            continue
        cands = [r for r in free if r["from"] == leg["from"] and r["to"] == leg["to"] and r["departed"] is not None
                 and abs(r["departed"] - leg["leave"]) <= RIDE_MATCH_MIN * 60]
        if not cands:
            skipped["no_ride_logged"] += 1
            continue
        r = min(cands, key=lambda r: abs(r["departed"] - leg["leave"]))
        free.remove(r)
        if not r["used"] or r["minutes"] is None:
            skipped["not_clean"] += 1  # the ride log already judged it bad data (too fast, too long, ...)
            continue
        real = float(r["minutes"])
        mine = abs(real - leg["minutes"])
        misses.append(mine)
        hand = _hand(leg["from"], leg["to"])
        if hand is None:
            vs["no_hand_estimate"] += 1
        else:
            yours = abs(real - hand)
            vs["better" if mine < yours - 0.05 else "worse" if mine > yours + 0.05 else "same"] += 1
    detail = dict(skipped, scored=len(misses))
    if not misses:
        why = "no clean ride matched a planned ride she could be judged on"
        return (None, why, detail), (None, why, vs)
    return (round(statistics.median(misses), 1), None, detail), (vs["better"], None, vs)


def score_arrivals(plan, rides):
    """arrival_miss_min: middle value of |planned arrival - real arrival| in minutes. The real arrival is the ride
    on the same route that left within 2 hours of the planned time, or else a ride that arrived at the same place
    within 90 minutes. Lateness counts here (Jordan: "Arrival time should be the plan")."""
    if rides is None:
        return None, "the places file could not be read", {}
    span = _span(plan)
    free = [r for r in rides if span and r["arrived"] is not None and span[0] <= r["arrived"] <= span[1]]
    signed, skipped = [], 0
    for leg in _rides_planned(plan["blocks"]):
        if not leg["to"]:
            skipped += 1
            continue
        route = [r for r in free if r["from"] == leg["from"] and r["to"] == leg["to"] and r["departed"] is not None
                 and abs(r["departed"] - leg["leave"]) <= RIDE_MATCH_MIN * 60]
        place = [r for r in free if r["to"] == leg["to"] and abs(r["arrived"] - leg["arrive"]) <= ARRIVAL_MATCH_MIN * 60]
        cands = route or place
        if not cands:
            skipped += 1
            continue
        r = min(cands, key=lambda r: abs(r["arrived"] - leg["arrive"]))
        free.remove(r)
        signed.append((r["arrived"] - leg["arrive"]) / 60.0)
    detail = {"scored": len(signed), "no_arrival_logged": skipped,
              "late_or_early_min": [round(m, 1) for m in signed]}  # + is late, - is early
    if not signed:
        return None, "no logged arrival matched an arrival she planned", detail
    return round(statistics.median(abs(m) for m in signed), 1), None, detail


def _span(plan):
    """The stretch of time this plan's rides cover, with room either side. Rides and arrivals outside it belong
    to another day's plan (the reader pulls in 12 hours either side of midnight)."""
    legs = _rides_planned(plan["blocks"])
    if not legs:
        return None
    pad = max(ARRIVAL_MATCH_MIN, RIDE_MATCH_MIN) * 60
    return min(l["leave"] for l in legs) - pad, max(l["arrive"] for l in legs) + pad


def _unplaced_calls(rides, calls):
    """Arrival calls that no ride explains: real arrivals whose place was not saved. Each ride is paired with
    the first arrival call at or after it arrived (the phone may queue calls while offline, up to 6 hours)."""
    left = sorted(c["at"] for c in calls)
    for r in sorted(rides, key=lambda r: r["arrived"]):
        match = next((c for c in left if r["arrived"] - 120 <= c <= r["arrived"] + 6 * 3600), None)
        if match is not None:
            left.remove(match)
    return left


def _in_outage(t, window, outages):
    """True if any server outage (start, end) overlaps t +- window. 5.6 merge."""
    return any(s <= t + window and e >= t - window for s, e in outages or ())


def score_pins(plan, rides, calls, outages=None):
    """pin_hit_rate: of the arrivals the plan expected (where each planned ride ends), the share that came in
    at that place: a ride arrived there, or a ride left there during the planned stay. With no such ride, an
    arrival at gym or work, or right after leaving them, is skipped (the phone may not have seen it). Elsewhere,
    an arrival call nearby with no place saved makes it unsure; otherwise it is a miss.
    5.6 merge (counted once): outages is a list of (start, end) epoch stretches when the server was down or
    unreachable. A miss whose arrival window overlaps one is left out ("outage"), not counted against her plan,
    because Step 4 already counts the outage in her body. Hits still count: a ride that did log is real."""
    if rides is None:
        return None, "the places file could not be read", {}
    span = _span(plan)
    if span is None:
        return None, "the written plan had no rides, so no arrivals were expected", {}
    lo, hi = span
    rides = [r for r in rides if r["arrived"] is not None and lo <= r["arrived"] <= hi]
    ok_calls = [c for c in calls or [] if c["ok"] and c["at"] is not None and lo <= c["at"] <= hi]
    if not rides and not ok_calls:
        if outages and all(_in_outage(l["arrive"], ARRIVAL_MATCH_MIN * 60, outages)
                           for l in _rides_planned(plan["blocks"]) if l["to"]):
            return None, "the server was down when the arrivals were due", {"arrival_calls": 0}
        return None, "no arrivals were recorded", {"arrival_calls": 0}
    unplaced = _unplaced_calls(rides, ok_calls)
    legs = _rides_planned(plan["blocks"])
    free = list(rides)
    hits = by_leaving = misses = unsure = skipped = outage = 0
    window = ARRIVAL_MATCH_MIN * 60
    for i, leg in enumerate(legs):
        if not leg["to"]:
            skipped += 1
            continue
        cands = [r for r in free if r["to"] == leg["to"] and abs(r["arrived"] - leg["arrive"]) <= window]
        if cands:
            free.remove(min(cands, key=lambda r: abs(r["arrived"] - leg["arrive"])))
            hits += 1
            continue
        # He left that place during the planned stay: its pin fired on the way out, so he was there.
        stay_end = next((l["leave"] for l in legs[i + 1:] if l["from"] == leg["to"]), leg["arrive"])
        left = [r for r in rides if r["from"] == leg["to"] and r["departed"] is not None
                and leg["arrive"] - window <= r["departed"] <= stay_end + window]
        if left:
            hits += 1
            by_leaving += 1
            continue
        if leg["to"] in UNRELIABLE or leg["from"] in UNRELIABLE:
            skipped += 1  # the phone may never have seen him arrive or leave there
            continue
        if _in_outage(leg["arrive"], window, outages):
            outage += 1  # the server was down or unreachable around then: not her plan's miss (5.6)
            continue
        maybe = [c for c in unplaced if abs(c - leg["arrive"]) <= window]
        if maybe:
            unplaced.remove(min(maybe, key=lambda c: abs(c - leg["arrive"])))
            unsure += 1
        else:
            misses += 1
    detail = {"hits": hits, "hits_by_leaving": by_leaving, "misses": misses, "unsure": unsure,
              "unreliable_place": skipped, "outage": outage, "arrival_calls": len(ok_calls)}
    if hits + misses == 0:
        why = ("arrivals came in, but none could be tied to a place she planned" if unsure
               else "the server was down when the arrivals were due" if outage
               else "no expected arrival could be checked")
        return None, why, detail
    return round(hits / float(hits + misses), 3), None, detail


def _key(b):
    return (b["kind"], b["title"], b["place"], b["start"], b["end"])


def score_held(changes, plans):
    """plan_held: share of the first written plan's stops and rides that Jordan did not rearrange.
    Rearranged = Jordan (any door but system) changed that day after the plan was written, and the block is not
    in the last plan written that day. A proposal Jordan rejected, with nothing written, held 0."""
    if changes is None:
        return None, "the approvals file could not be read", {}
    if not plans:
        rejected = [c for c in changes if c["kind"] == "write_day" and c["status"] == "rejected"
                    and c["door"] in JORDAN_DOORS]
        if rejected:
            return 0.0, None, {"rejected_by_jordan": len(rejected)}
        return None, "no plan was written that day", {}
    first, last = plans[0], plans[-1]
    since = first["at"] or 0
    jordan = [c for c in changes if c["door"] in JORDAN_DOORS and c["status"] in ("applied", "undone")
              and (_epoch(c["decided"]) or _epoch(c["created"]) or 0) > since]
    items = [b for b in first["blocks"] if b["kind"] in SCORED_KINDS]
    if not items:
        return None, "the written plan had no stops or rides", {}
    detail = {"items": len(items), "jordan_changes": len(jordan), "plans_written": len(plans)}
    if not jordan:
        return 1.0, None, dict(detail, rearranged=0)
    if len(plans) == 1:
        return None, "you changed the day after she wrote it, but no new plan was written to compare", detail
    still = {_key(b) for b in last["blocks"]}
    moved = sum(1 for b in items if _key(b) not in still)
    return round((len(items) - moved) / float(len(items)), 3), None, dict(detail, rearranged=moved)


def rules_broken(plan, rules):
    """Which of Jordan's rules one written plan breaks (names only). Checked here, apart from the planner's own
    checks, so she is not grading herself with the same code that made the plan."""
    broken = []
    blocks = plan["blocks"]
    preview = plan["preview"]
    if preview.get("problems"):
        broken.append("planner_flagged")  # written anyway, with problems the planner itself listed
    for p, b in zip(blocks, blocks[1:]):
        if b["start"] < p["end"]:
            broken.append("overlap")
            break
    stops = [b for b in blocks if b["kind"] != "ride" and b["place"]]
    for p, b in zip(stops, stops[1:]):
        between = [r for r in blocks if r["kind"] == "ride" and p["end"] <= r["start"] and r["end"] <= b["start"]]
        if p["place"] != b["place"] and not between:
            broken.append("no_ride_between_places")
            break
    for b in blocks:
        mins = (b["end"] - b["start"]) / 60.0
        if b["kind"] == "stop" and b["place"] == "farm" and mins < rules["farm_minutes"]:
            broken.append("farm_too_short")
        if b["kind"] == "stop" and b["place"] in GYM_HOURS:
            if mins < rules["workout_floor_minutes"] + rules["prework_floor_minutes"]:
                broken.append("gym_too_short")
            h = GYM_HOURS[b["place"]]
            d = _local(b["start"])
            weekend = d.weekday() >= 5
            open_h = h["weekendOpen"] if weekend else h["weekdayOpen"]
            close_h = h["weekendClose"] if weekend else h["weekdayClose"]
            start_h = d.hour + d.minute / 60.0
            if start_h < open_h or start_h + mins / 60.0 > close_h:
                broken.append("gym_closed")
    shift = preview.get("shift") or {}
    shift_start = _epoch(shift.get("start")) if isinstance(shift, dict) else None
    if shift_start is not None:
        at_work = [b["start"] for b in blocks if b["place"] == "hmc"]
        if not at_work or min(at_work) > shift_start - rules["arrive_early_minutes"] * 60:
            broken.append("not_early_for_work")
    return sorted(set(broken))


def score_rules(plans, rules):
    if not plans:
        return None, "no plan was written that day", {}
    per = [rules_broken(p, rules) for p in plans]
    names = sorted({n for r in per for n in r})
    return sum(1 for r in per if r), None, {"plans_checked": len(plans), "rules": names}


def score_day(day_s, folder=None, data=None, outages=None):
    """Every score for one day (YYYY-MM-DD, New York). data is read_day()'s dict (tests pass their own).
    outages: (start, end) epoch stretches when the server was down (5.6 merge; the caller reads them, this file
    never opens Step 4's files). Pin misses inside one are left out."""
    data = data if data is not None else read_day(day_s, folder)
    if outages is not None:
        data = dict(data, outages=[tuple(o) for o in outages])
    out = {"date": day_s}
    out.update({name: None for name in SCORES})
    out.update({"missing": [], "why": {}, "detail": {}})
    plans = written_plans(data["changes"]) if data["changes"] is not None else []
    first = plans[0] if plans else None
    results = {"plan_held": score_held(data["changes"], plans), "rule_breaks": score_rules(plans, data["rules"])}
    if first is None:
        why = "no plan was written that day" if data["changes"] is not None else "the approvals file could not be read"
        for name in ("arrival_miss_min", "ride_est_miss_min", "ride_est_wins", "pin_hit_rate"):
            results[name] = (None, why, {})
    else:
        # Judge the plan Jordan was following: the last one written that day.
        results["arrival_miss_min"] = score_arrivals(plans[-1], data["rides"])
        results["ride_est_miss_min"], results["ride_est_wins"] = score_rides(plans[-1], data["rides"])
        results["pin_hit_rate"] = score_pins(plans[-1], data["rides"], data["arrival_calls"], data.get("outages"))
    for name in SCORES:
        value, why, detail = results[name]
        out[name] = value
        out["detail"][name] = detail
        if value is None:
            out["missing"].append(name)
            out["why"][name] = why
    return out


def score_days(first_day, last_day, folder=None):
    """One dict per day from first_day to last_day (inclusive), oldest first."""
    d, end, out = date.fromisoformat(first_day), date.fromisoformat(last_day), []
    while d <= end:
        out.append(score_day(d.isoformat(), folder))
        d += timedelta(days=1)
    return out


def main(argv):
    """python3 rivia_plan_score.py [YYYY-MM-DD [YYYY-MM-DD]]  (default: the last 7 days). Prints JSON. Read-only."""
    today = datetime.now(TZ).date()
    first = argv[1] if len(argv) > 1 else (today - timedelta(days=7)).isoformat()
    last = argv[2] if len(argv) > 2 else (argv[1] if len(argv) > 1 else (today - timedelta(days=1)).isoformat())
    print(json.dumps(score_days(first, last), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
