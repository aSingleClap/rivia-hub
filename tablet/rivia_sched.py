# rivia_sched.py: scheduling (planner, plan_day, write_day, schedule settings, Google Calendar,
# the 05:30 morning tick and 21:00 evening refresh), in scheduler.db.
#
# Since the core split, places (rivia_places), approvals (rivia_approvals) and the settings engine
# (rivia_settings) are separate. Scheduling reads from places and plugs into it (the arrival plan,
# the morning rundown, the after-ride backup); places never imports scheduling. Each action is a
# standalone FastMCP tool object; rivia_tools turns tool-table rows into door tools. The names that
# moved are re-exported below so older callers (rivia sched CLI, tests) keep working.
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Any, Union

from fastmcp.tools import tool

import rivia_approvals as approvals
import rivia_gcal as gcal
import rivia_places as places
import rivia_planner as rp
import rivia_settings as settings
import rivia_store as store
import rivia_tools as tools
from rivia_approvals import (  # noqa: F401  re-exported
    SettingsError, PENDING_EXPIRE_S, DUPLICATE_S, UNDO_S, approve_change_sync, reject_change_sync,
    list_changes_sync, expire_pending,
)
from rivia_approvals import asked_of as _asked_of, preview_obj as _preview_obj  # noqa: F401
from rivia_approvals import row_created_s as _row_created_s, expires_iso as _expires_iso  # noqa: F401
from rivia_approvals import change_public as _change_public, new_id as _new_id  # noqa: F401
from rivia_approvals import log_change as _log_change, load_value as _load_value  # noqa: F401
from rivia_places import (  # noqa: F401  re-exported
    RIDE_PLACES, PLACE_FIELDS, PLACE_DOORS, RADIUS_MIN, RADIUS_MAX, RADIUS_STEP, _PLACES_SAY_DEFAULT,
    DUP_ARRIVAL_S, PLACES_DESCRIPTION, ARRIVAL_DESCRIPTION, LOG_RIDE_DESCRIPTION, PIN_PLACE_DESCRIPTION,
    APP_OPEN_DESCRIPTION, LAST_ARRIVAL_KEY, META_LAST_DEPARTURE, META_MORNING_RUNDOWN, MORNING_FROM_H,
    RUNDOWN_CUTOFF, rewrite_places_file, places_sync, places_tool, seed_places, list_places, add_place,
    rename_place, remove_place, set_zone, pin_place_sync, pin_place_tool, cmd_pin, log_ride_sync,
    arrival_sync, app_open_sync, cmd_rides, cmd_restore_file, cmd_start_fresh, place_radius_m,
    geofence_radius_m, _con, _resolve_place_id, _custom_place_ids,
)
from rivia_places import load_last_arrival as _load_last_arrival  # noqa: F401
from rivia_places import settings_fail as _settings_fail  # noqa: F401
from rivia_settings import PENDING_MESSAGE  # noqa: F401
from rivia_settings import today_date as _today_date, as_date as _as_date  # noqa: F401
from rivia_settings import store_value as _store_value, coerce as _coerce  # noqa: F401
from rivia_tools import envelope, fail, ok_reply, FAIL_RULE, PENDING_LINE  # noqa: F401

# Cached startup selftest. None = not run yet. Tests may call set_selftest().
_selftest: dict[str, Any] | None = None


def db_open():
    """scheduler.db: schedule settings, day overrides and scheduling's bookkeeping (meta)."""
    return store.connect("scheduler", settings.SETTINGS_SCHEMA)


# Public MCP argument names (from is a Python keyword, so the function uses from_place).
RIDE_TIME_ARGS = {"from_place": "from", "to_place": "to"}

RIDE_TIME_DESCRIPTION = (
    "Ride minutes between two of Jordan's places, and where the number came from. "
    "Place ids: home, farm, gym, pf (Planet Fitness, weekends), hmc (work). "
    "Returns minutes and a source (learned from rides, or your estimate)."
)

PLAN_DAY_DESCRIPTION = (
    "What a given day looks like for Jordan: leave-home time, gym, shift, and any problems. "
    "Day is today, tomorrow, a weekday name (the next one), or YYYY-MM-DD. "
    "Reads the calendar; never writes it. Say times exactly as their spoken value."
)

PLAN_WEEK_DESCRIPTION = (
    "The next seven days for Jordan: each day's status, problems, and a short plan message. "
    "Reads the calendar; never writes it. Say times exactly as their spoken value."
)


def set_selftest(ok, detail=None):
    """Override the cached startup selftest (for tests)."""
    global _selftest
    _selftest = {"ok": bool(ok), "detail": detail}


def reset_selftest():
    global _selftest
    _selftest = None


def run_startup_selftest():
    """Run the planner selftest once and remember the result. Stored in meta when possible."""
    global _selftest
    try:
        res = rp.selftest()
        ok = bool(res.get("all_passed"))
        _selftest = {"ok": ok, "result": res}
        try:
            con = db_open()
            try:
                store.set_meta(con, "selftest", json.dumps({
                    "ok": ok, "passed": res.get("passed"), "total": res.get("total"),
                    "at": rp.iso(rp.now_s()),
                }))
            finally:
                con.close()
        except Exception:  # noqa: BLE001
            pass
        return ok
    except Exception as e:  # noqa: BLE001
        _selftest = {"ok": False, "error": repr(e)}
        return False


def selftest_ok():
    if _selftest is None:
        run_startup_selftest()
    return bool(_selftest and _selftest.get("ok"))


def _place(v, name):
    if v is None or not str(v).strip():
        raise rp.RiviaError("bad_place", "%s is required (a place id: home, farm, gym, pf, hmc)." % name)
    s = str(v).strip()
    if s not in rp.PLACES:
        raise rp.RiviaError("bad_place", "Unknown place: %s." % s)
    return s


@tool
def ride_time(from_place: str, to_place: str) -> dict:
    """Ride minutes between two of Jordan's places, and where the number came from."""
    if not selftest_ok():
        return fail("selftest_failed",
                    "Planner selftest failed at startup. Scheduler actions are blocked until it passes.",
                    "planner")
    ref = None
    try:
        a = _place(from_place, "from")
        b = _place(to_place, "to")
        ref = "%s>%s" % (a, b)
        ensure_store()
        with planner_swap():
            out = rp.cmd_legs({"pairs": [[a, b]]})
        leg = out["legs"][0]
        return ok_reply("legs", {
            "from": leg["from"], "to": leg["to"],
            "minutes": leg["minutes"], "source": leg["source"],
        }, ref=ref)
    except rp.RiviaError as e:
        if e.code == "need_maps":
            return fail("need_estimate", str(e), "planner",
                        data={"pairs": e.extra.get("pairs") or []}, ref=ref)
        step = "input" if e.code in ("bad_place", "bad_pairs") else "planner"
        return fail(e.code, str(e), step, data=dict(e.extra) if e.extra else {}, ref=ref)
    except Exception as e:  # noqa: BLE001
        return fail("internal", repr(e), "planner", ref=ref)


def _today(now=None):
    if now is None:
        return datetime.now(gcal.TZ).date()
    if hasattr(now, "date"):
        return now.date()
    return now


def _gcal_fail(err, ref=None):
    """Map a Google read failure. Never include planned times. Record auth failures."""
    gcal.record_calendar_auth_failure(err)
    return fail(err.code, str(err), err.step or "calendar", ref=ref)


def _plan_days(days, events, now=None):
    inp = {"days": [d.isoformat() if hasattr(d, "isoformat") else d for d in days], "events": events}
    if now is not None:
        inp["now"] = now
    return rp.cmd_plan(inp)


def plan_day_sync(day_arg="tomorrow", now=None):
    """Resolve the day, read primary (2 days each side), run the planner. Never writes."""
    if not selftest_ok():
        return fail("selftest_failed",
                    "Planner selftest failed at startup. Scheduler actions are blocked until it passes.",
                    "planner")
    ref = None
    try:
        day, _asked = gcal.resolve_day(day_arg or "tomorrow", now=now)
        ref = day.isoformat()
        events = gcal.events_for_plan(day, day, before=2, after=2, now=now)
        ensure_store(now=now)
        out = _plan_one(day, events, now=now)
        rec = out["days"][0]
        data = {
            "day": rec["day"], "status": rec["status"], "shift": rec["shift"],
            "gym": rec["gym"], "blocks": rec["blocks"], "problems": rec["problems"],
            "rides": out.get("rides") or [], "written": False,
        }
        return ok_reply("plan", data, ref=ref, message=rec.get("message"))
    except gcal.GcalError as e:
        return _gcal_fail(e, ref=ref)
    except rp.RiviaError as e:
        if e.code == "need_maps":
            return fail("need_estimate", str(e), "planner",
                        data={"pairs": e.extra.get("pairs") or []}, ref=ref)
        step = "input" if e.code in ("bad_day", "bad_days", "bad_events") else "planner"
        return fail(e.code, str(e), step, data=dict(e.extra) if e.extra else {}, ref=ref)
    except Exception as e:  # noqa: BLE001
        return fail("internal", repr(e), "planner", ref=ref)


def plan_week_sync(now=None):
    """Plan today through today+6. No seeded tool row. Reply has no blocks."""
    if not selftest_ok():
        return fail("selftest_failed",
                    "Planner selftest failed at startup. Scheduler actions are blocked until it passes.",
                    "planner")
    try:
        start = _today(now)
        days = [start + timedelta(days=i) for i in range(7)]
        events = gcal.events_for_plan(days[0], days[-1], before=2, after=2, now=now)
        ensure_store(now=now)
        week, ride_notes = [], []
        for day in days:
            one = _plan_one(day, events, now=now)
            rec = one["days"][0]
            week.append({
                "day": rec["day"], "status": rec["status"],
                "problems": rec["problems"], "message": rec.get("message"),
            })
            for note in one.get("rides") or []:
                if note not in ride_notes:
                    ride_notes.append(note)
        return ok_reply("plan", {"days": week, "rides": ride_notes, "written": False},
                        ref=days[0].isoformat(), message=None)
    except gcal.GcalError as e:
        return _gcal_fail(e, ref=None)
    except rp.RiviaError as e:
        if e.code == "need_maps":
            return fail("need_estimate", str(e), "planner",
                        data={"pairs": e.extra.get("pairs") or []})
        step = "input" if e.code in ("bad_day", "bad_days", "bad_events") else "planner"
        return fail(e.code, str(e), step, data=dict(e.extra) if e.extra else {})
    except Exception as e:  # noqa: BLE001
        return fail("internal", repr(e), "planner")


@tool
async def plan_day(day: str = "tomorrow") -> dict:
    """What a given day looks like for Jordan. Reads the calendar; never writes it."""
    return await asyncio.to_thread(plan_day_sync, day)


@tool
async def plan_week() -> dict:
    """The next seven days for Jordan. Reads the calendar; never writes it."""
    return await asyncio.to_thread(plan_week_sync)


# ── Settings store (step 7a) ────────────────────────────────────────────
import secrets
import sys
import threading
from contextlib import contextmanager
from datetime import date

_LOCK = threading.RLock()
_seeding = False

_CFG_DEFAULT = dict(rp.CFG)
_LEGS_DEFAULT = dict(rp.MEASURED_LEGS)
_GYM_DEFAULT = {k: dict(v) for k, v in rp.GYM_HOURS.items()}


# Section 7 ranges (accepted). A range can't be widened over the network.
SETTINGS = {
    "arrive_early_minutes":  {"planner": "bufferM",        "default": 10,   "min": 0,    "max": 30,   "type": "int",   "unit": "minutes", "label": "Arrive early",             "day": True,  "ask": True,  "group": "schedule"},
    "prework_minutes":       {"planner": "preworkM",       "default": 30,   "min": 0,    "max": 90,   "type": "int",   "unit": "minutes", "label": "Pre-work",                 "day": True,  "ask": True,  "group": "schedule"},
    "prework_floor_minutes": {"planner": "preworkFloorM",  "default": 15,   "min": 0,    "max": 60,   "type": "int",   "unit": "minutes", "label": "Pre-work floor",           "day": True,  "ask": True,  "group": "schedule"},
    "workout_minutes":       {"planner": "workoutM",       "default": 120,  "min": 30,   "max": 180,  "type": "int",   "unit": "minutes", "label": "Workout",                  "day": True,  "ask": True,  "group": "schedule"},
    "workout_floor_minutes": {"planner": "workoutFloorM",  "default": 60,   "min": 15,   "max": 120,  "type": "int",   "unit": "minutes", "label": "Workout floor",            "day": True,  "ask": True,  "group": "schedule"},
    "farm_minutes":          {"planner": "farmM",          "default": 45,   "min": 10,   "max": 180,  "type": "int",   "unit": "minutes", "label": "Farm time",                "day": True,  "ask": True,  "group": "schedule"},
    "alert_lead_minutes":    {"planner": "leadM",          "default": 5,    "min": 0,    "max": 30,   "type": "int",   "unit": "minutes", "label": "Alert lead",               "day": True,  "ask": True,  "group": "schedule"},
    "wind_down_minutes":     {"planner": "windDownM",      "default": 60,   "min": 0,    "max": 180,  "type": "int",   "unit": "minutes", "label": "Wind-down",                "day": True,  "ask": True,  "group": "schedule"},
    "wake_up_minutes":       {"planner": "wakeUpM",        "default": 60,   "min": 15,   "max": 180,  "type": "int",   "unit": "minutes", "label": "Wake-up",                  "day": True,  "ask": True,  "group": "schedule"},
    "sleep_target_hours":    {"planner": "sleepTargetH",   "default": 8,    "min": 4,    "max": 10,   "type": "int",   "unit": "hours",   "label": "Sleep target",             "day": True,  "ask": True,  "group": "schedule"},
    "place_radius_meters":   {"planner": "placeRadiusM",   "default": 400,  "min": 50,   "max": 2000, "type": "int",   "unit": "meters",  "label": "Place radius",             "day": False, "ask": False, "group": "system"},
    "auto_write_time":       {"planner": None,             "default": "05:30","min": "03:00","max": "09:00","type": "time",  "unit": "time",    "label": "Auto-write time",          "day": False, "ask": False, "group": "system", "doors": ("termux", "admin")},
    "overnight_hours":       {"planner": "overnightH",     "default": 8,    "min": 4,    "max": 16,   "type": "int",   "unit": "hours",   "label": "Overnight horizon",        "day": False, "ask": False, "group": "system"},
    "home_lookahead_hours":  {"planner": "homeLookaheadH", "default": 20,   "min": 8,    "max": 36,   "type": "int",   "unit": "hours",   "label": "Home lookahead",           "day": False, "ask": False, "group": "system"},
    "learn_keep_rides":      {"planner": "learnKeep",      "default": 10,   "min": 3,    "max": 50,   "type": "int",   "unit": "rides",   "label": "Learned rides to keep",    "day": False, "ask": False, "group": "system"},
    "learn_min_rides":       {"planner": "learnMin",       "default": 3,    "min": 1,    "max": 10,   "type": "int",   "unit": "rides",   "label": "Learned rides minimum",    "day": False, "ask": False, "group": "system"},
    "learn_rank":            {"planner": "learnRank",      "default": 0.75, "min": 0.5,  "max": 1.0,  "type": "float", "unit": "rank",    "label": "Learned ride rank",       "day": False, "ask": False, "group": "system"},
    "ride_max_hours":        {"planner": "rideMaxH",       "default": 3,    "min": 1,    "max": 8,    "type": "int",   "unit": "hours",   "label": "Ride max hours",           "day": False, "ask": False, "group": "system"},
    "clean_low_ratio":       {"planner": "cleanLow",       "default": 0.25, "min": 0.05, "max": 0.9,  "type": "float", "unit": "ratio",   "label": "Clean-ride low ratio",    "day": False, "ask": False, "group": "system"},
    "clean_high_ratio":      {"planner": "cleanHigh",      "default": 2,    "min": 1.1,  "max": 5,    "type": "float", "unit": "ratio",   "label": "Clean-ride high ratio",   "day": False, "ask": False, "group": "system"},
}

GYM_HOUR_FIELDS = {
    "weekday_open": "weekdayOpen", "weekday_close": "weekdayClose",
    "weekend_open": "weekendOpen", "weekend_close": "weekendClose",
}
SEED_ESTIMATES = (("ride_estimate.farm_pf", 17), ("ride_estimate.hmc_pf", 15))
DAY_WINDOW_DAYS = 14

WRITE_DAY_DESCRIPTION = (
    "Write Jordan's planned blocks for a day to the Rivia calendar. "
    "Always waits for approval; a pending result has not happened yet. "
    "It never writes to the primary calendar."
)
WRITE_DAY_NOTIFY = (
    "A planned Rivia day changed since its preview and was not written. "
    "Open the hub to review."
)
EVENING_HOUR, EVENING_MINUTE = 21, 0
META_EVENING_DAY = "evening_refresh_day"
DEFAULT_AUTO_WRITE = "05:30"

GET_SETTINGS_DESCRIPTION = (
    "Jordan's schedule settings for a day: each key's value, default, unit, range, "
    "and whether it comes from the default, a standing value, or a day override."
)
CHANGE_SETTING_DESCRIPTION = (
    "Change one of Jordan's schedule settings. scope is day (that date only, today "
    "through 14 days ahead) or standing. A standing change from this door is not "
    "applied until it is approved (zone sizes are the exception); the reply is pending. "
    "A pending result has not happened yet. Out-of-range or inconsistent values are "
    "refused with the reason. Zone sizes: key place.<place>.radius_m, where <place> is "
    "home, farm, gym, pf, hmc, or the name of one of his own places; scope standing; "
    "value in meters (50 to 400), or bigger / smaller for 25 m a step. A zone size "
    "applies right away; say the reply's fallback_text. Undo puts the old size back."
)
UNDO_CHANGE_DESCRIPTION = (
    "Undo a schedule setting change. With no change_id, undo the latest change made "
    "through this door in the last 24 hours. Restores the old value and logs a new change. "
    "Undoing a zone size (place.<place>.radius_m) applies right away; say the reply's fallback_text."
)


def _ride_keys():
    out = []
    for i, a in enumerate(RIDE_PLACES):
        for b in RIDE_PLACES[i + 1:]:
            out.append("ride_estimate.%s_%s" % (a, b))
    return out


def _gym_keys():
    out = []
    for gid in ("gym", "pf"):
        for slot in GYM_HOUR_FIELDS:
            out.append("gym_hours.%s.%s" % (gid, slot))
    return out


def ensure_settings_schema(con):
    con.executescript(settings.SETTINGS_SCHEMA)


# ── the schedule's own settings (an owner in the settings engine) ──────

def _sched_parse(key):
    """Spec for a schedule-owned key, or None if the key belongs to another part."""
    if key in SETTINGS:
        return dict(SETTINGS[key], key=key, family="scalar")
    if key.startswith("ride_estimate."):
        rest = key[len("ride_estimate."):]
        parts = rest.split("_")
        if len(parts) != 2 or parts[0] not in RIDE_PLACES or parts[1] not in RIDE_PLACES:
            raise SettingsError("bad_key", "Unknown setting: %s." % key, step="input")
        a, b = parts
        if a >= b:
            raise SettingsError(
                "bad_key",
                "Ride estimate place ids must be in alphabetical order (use ride_estimate.%s_%s)." % tuple(sorted((a, b))),
                step="input",
            )
        return {
            "key": key, "family": "ride", "a": a, "b": b,
            "default": None, "min": 1, "max": 180, "type": "int", "unit": "minutes",
            "label": "Ride estimate %s–%s" % (a, b), "day": False, "ask": True, "group": "schedule",
        }
    if key.startswith("gym_hours."):
        parts = key.split(".")
        if len(parts) != 3 or parts[1] not in ("gym", "pf") or parts[2] not in GYM_HOUR_FIELDS:
            raise SettingsError("bad_key", "Unknown setting: %s." % key, step="input")
        return {
            "key": key, "family": "gym", "gym_id": parts[1], "field": GYM_HOUR_FIELDS[parts[2]],
            "slot": parts[2], "default": _GYM_DEFAULT[parts[1]][GYM_HOUR_FIELDS[parts[2]]],
            "min": 0, "max": 24, "type": "float", "unit": "hour",
            "label": "%s %s" % ("Planet Fitness" if parts[1] == "pf" else "University Fitness Center", parts[2].replace("_", " ")),
            "day": False, "ask": True, "group": "schedule",
        }
    return None


def _sched_keys(con=None):
    return list(SETTINGS) + _ride_keys() + _gym_keys()


def _sched_consistency(resolved):
    def v(key):
        return resolved[key][0]

    problems = []
    if v("prework_floor_minutes") > v("prework_minutes"):
        problems.append("The pre-work floor cannot be above the pre-work target.")
    if v("workout_floor_minutes") > v("workout_minutes"):
        problems.append("The workout floor cannot be above the workout target.")
    if v("learn_min_rides") > v("learn_keep_rides"):
        problems.append("learn_min_rides cannot be above learn_keep_rides.")
    if not (v("clean_low_ratio") < 1 < v("clean_high_ratio")):
        problems.append("clean_low_ratio must be under 1 and clean_high_ratio must be over 1.")
    if v("overnight_hours") >= v("home_lookahead_hours"):
        problems.append("overnight_hours must be under home_lookahead_hours.")
    for gid, name in (("gym", "The University Fitness Center"), ("pf", "Planet Fitness")):
        for kind in ("weekday", "weekend"):
            op, cl = v("gym_hours.%s.%s_open" % (gid, kind)), v("gym_hours.%s.%s_close" % (gid, kind))
            if op is not None and cl is not None and op >= cl:
                problems.append("%s %s opening must be before closing." % (name, kind))
    return problems


def _sched_result_extra(spec, data, day):
    if spec["family"] == "ride":
        with planner_swap(day=day):
            info = _ride_in_use(spec["a"], spec["b"])
        data["in_use"] = info["in_use"]
        data["in_use_source"] = info["in_use_source"]


def _sched_item_extra(spec, item, day_s):
    if spec["family"] == "ride":
        with planner_swap(day=day_s):
            info = _ride_in_use(spec["a"], spec["b"])
        item["in_use"] = info["in_use"]
        item["in_use_source"] = info["in_use_source"]


# Older names, kept for callers from before the core split (the engine ignores con).
def _parse_key(key):
    return settings.parse_key(key)


def _standing_row(con, key):
    return settings.standing_row(con, key)


def _resolved(con, key, day=None):
    return settings.resolved(key, day=day)


def _all_resolved(con=None, day=None, extra=None):
    return settings.all_resolved(day=day, extra=extra)


def _all_setting_keys(con=None):
    return settings.all_setting_keys()


def _keys_for_group(group, con=None):
    return settings.keys_for_group(group)


def _consistency_problems(resolved):
    return settings.consistency_problems(resolved)


def change_setting_sync(key, value, scope, day=None, reason=None, request_id=None,
                        door="app", now=None, con=None):
    """The four checks, then apply, or queue an app-door standing change as pending (G7)."""
    return settings.change_setting(key, value, scope, day=day, reason=reason, request_id=request_id,
                                   door=door, now=now)


def _change_setting(con, key, value, scope, day=None, reason=None, request_id=None,
                    door="app", now=None, new_place=None):
    return settings.change_setting(key, value, scope, day=day, reason=reason, request_id=request_id,
                                   door=door, now=now, new_place=new_place)


def undo_change_sync(change_id=None, door="app", now=None, con=None):
    return approvals.undo_change_sync(change_id=change_id, door=door, now=now)


def get_settings_sync(group="schedule", day=None, now=None, con=None):
    return settings.get_settings(group=group, day=day, now=now)


def _purge_overrides(con, today=None):
    today = _today_date(today)
    yesterday = (today - timedelta(days=1)).isoformat()
    con.execute("delete from overrides where day < ?", (yesterday,))


def _snapshot():
    return {
        "cfg": dict(rp.CFG),
        "legs": dict(rp.MEASURED_LEGS),
        "gym": {k: dict(v) for k, v in rp.GYM_HOURS.items()},
        "say": {pid: p["say"] for pid, p in rp.PLACES.items()},
    }


def _restore(snap):
    rp.CFG.clear()
    rp.CFG.update(snap["cfg"])
    rp.MEASURED_LEGS.clear()
    rp.MEASURED_LEGS.update(snap["legs"])
    for gid, hours in snap["gym"].items():
        rp.GYM_HOURS[gid].clear()
        rp.GYM_HOURS[gid].update(hours)
    for pid, say in snap.get("say", {}).items():
        rp.PLACES[pid]["say"] = say


def _apply_one(key, value):
    if value is None:
        return
    spec = _parse_key(key)
    if spec["family"] == "scalar":
        planner = spec.get("planner")
        if planner:
            rp.CFG[planner] = value
    elif spec["family"] == "ride":
        rp.MEASURED_LEGS[rp.leg_key(spec["a"], spec["b"])] = value
    elif spec["family"] == "gym":
        rp.GYM_HOURS[spec["gym_id"]][spec["field"]] = value
    elif spec["family"] == "place" and spec["field"] == "name" and spec["place_id"] in rp.PLACES:
        rp.PLACES[spec["place_id"]]["say"] = value
    # place pins and zone sizes live in places.json, not in the planner module.


def _apply_resolved(resolved):
    # Start from code defaults, then overlay any non-default values.
    rp.CFG.clear()
    rp.CFG.update(_CFG_DEFAULT)
    rp.MEASURED_LEGS.clear()
    rp.MEASURED_LEGS.update(_LEGS_DEFAULT)
    for gid, hours in _GYM_DEFAULT.items():
        rp.GYM_HOURS[gid].clear()
        rp.GYM_HOURS[gid].update(hours)
    for pid, say in _PLACES_SAY_DEFAULT.items():
        rp.PLACES[pid]["say"] = say
    for key, (val, _src) in resolved.items():
        if val is not None:
            _apply_one(key, val)


@contextmanager
def planner_swap(day=None, extra=None, now=None, con=None):
    """Hold the settings lock, swap this day's values into the planner, restore after."""
    close = False
    if con is None:
        con = db_open()
        ensure_store(con)
        close = True
    try:
        with _LOCK:
            snap = _snapshot()
            try:
                _apply_resolved(_all_resolved(con, day=day, extra=extra))
                yield
            finally:
                _restore(snap)
    finally:
        if close:
            con.close()


def _shift(start_iso, end_iso, title="Work Shift - HMC"):
    start = rp.to_epoch(start_iso, "start")
    end = rp.to_epoch(end_iso, "end")
    return {"start": start, "end": end, "title": title}


def _sample_workdays():
    """Three sample workdays for the dry run (section 7)."""
    mon = _shift("2026-10-05T14:30:00-04:00", "2026-10-05T23:00:00-04:00")
    tue = _shift("2026-10-06T14:30:00-04:00", "2026-10-06T23:00:00-04:00")
    sat = _shift("2026-12-05T14:30:00-05:00", "2026-12-05T23:00:00-05:00")
    return [
        ("weekday after a day off", mon, None, "gym"),
        ("weekday after a shift", tue, mon, "gym"),
        ("Saturday at Planet Fitness", sat, None, "pf"),
    ]


def _sample_problems(shift, prev, gym_id):
    leg = rp.Legs(rides=[])
    try:
        legs, _note = rp.ride_table(gym_id, leg)
        leg.check()
    except rp.RiviaError as e:
        if e.code == "need_maps":
            return None
        raise
    plan = rp.build_workday(shift, prev, legs, gym_id)
    return list(plan["problems"]) + rp.check_day(plan["blocks"], shift)


def _dry_run(con, day, extra):
    """Return new problems the change would add on the three sample days (con is unused)."""
    return _sample_day_problems(day, extra)


def _sample_day_problems(day, extra):
    added = []
    for name, shift, prev, gym_id in _sample_workdays():
        with planner_swap(day=day):
            before = _sample_problems(shift, prev, gym_id)
        with planner_swap(day=day, extra=extra):
            after = _sample_problems(shift, prev, gym_id)
        if before is None or after is None:
            continue  # Saturday (or any sample) skipped while ride estimates are missing
        new = [p for p in after if p not in before]
        if new:
            added.append({"sample": name, "problems": new})
    return added


def _block_start_s(b):
    start = (b or {}).get("start")
    if isinstance(start, dict):
        if start.get("epoch") is not None:
            try:
                return int(start["epoch"])
            except (TypeError, ValueError):
                pass
        start = start.get("iso") or start.get("dateTime")
    if start:
        try:
            return rp.to_epoch(start, "start")
        except Exception:
            return None
    return None


def _preview_first_start(row, preview=None):
    """First leave-home / stop / ride in the preview.

    Overnight sleep (and other pre-cutoff detail blocks) do not count.
    Otherwise a 05:30 auto-write would always be too late.
    """
    preview = preview if preview is not None else _preview_obj(row)
    blocks = list(preview.get("blocks") or [])
    for b in blocks:
        if (b.get("title") or "") == "Ride: home to the farm":
            s = _block_start_s(b)
            if s is not None:
                return s
    for b in blocks:
        if b.get("kind") in ("stop", "ride"):
            s = _block_start_s(b)
            if s is not None:
                return s
    starts = []
    for b in blocks:
        s = _block_start_s(b)
        if s is not None:
            starts.append(s)
    return min(starts) if starts else None


def _write_day_too_late(row, now_s):
    """A write_day is too late once the day is over or leave-home has started."""
    day_s = row[5] if row is not None and len(row) > 5 else None
    try:
        day = date.fromisoformat(str(day_s))
    except Exception:
        return _row_created_s(row) + PENDING_EXPIRE_S <= now_s
    now_d = datetime.fromtimestamp(now_s, gcal.TZ).date()
    if now_d > day:
        return True
    first = _preview_first_start(row)
    if first is not None and now_s >= first:
        return True
    return False


def _write_day_expires_s(row):
    first = _preview_first_start(row)
    if first is not None:
        return first
    day_s = row[5]
    try:
        day = date.fromisoformat(str(day_s))
        end = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=gcal.TZ)
        return int(end.timestamp())
    except Exception:
        return _row_created_s(row) + PENDING_EXPIRE_S


def _ride_in_use(a, b):
    mins, src = rp.Legs(rides=None).info(a, b)
    yours = src == "your estimate"
    return {"in_use": bool(yours and mins is not None), "in_use_source": src, "minutes": mins}


def _seed_weekend_estimates(con, now=None):
    """Idempotent G6 seed: ride_estimate.farm_pf=17 and hmc_pf=15 as standing values."""
    global _seeding
    seeded = []
    _seeding = True
    try:
        for key, val in SEED_ESTIMATES:
            if settings.standing_row(con, key) is not None:
                continue
            data = settings.change_setting(key, val, "standing", reason="auto-seed on first start",
                                           door="system", now=now)
            seeded.append(data)
            print("rivia: seeded %s=%s (standing)" % (key, val), file=sys.stderr)
    finally:
        _seeding = False
    return seeded


def ensure_store(con=None, now=None):
    """Create settings tables, drop stale day overrides, and seed 17/15 once. Always works on
    scheduler.db (a con from another DB, passed by an older caller, is returned untouched)."""
    own = db_open()
    try:
        ensure_settings_schema(own)
        _purge_overrides(own, today=now)
        if not _seeding:
            _seed_weekend_estimates(own, now=now)
    finally:
        own.close()
    return con


def _plan_one(day, events, now=None, con=None):
    day_s = day.isoformat() if hasattr(day, "isoformat") else day
    inp = {"days": [day_s], "events": events}
    if now is not None:
        inp["now"] = now
    with planner_swap(day=day, now=now, con=con):
        return rp.cmd_plan(inp)


def get_settings_tool(group: str = "schedule", day: str | None = None) -> dict:
    if not selftest_ok():
        return fail("selftest_failed",
                    "Planner selftest failed at startup. Scheduler actions are blocked until it passes.",
                    "planner")
    try:
        data = get_settings_sync(group=group or "schedule", day=day)
        return ok_reply("settings", data, ref=data.get("day"))
    except SettingsError as e:
        return _settings_fail(e)
    except gcal.GcalError as e:
        return fail(e.code, str(e), e.step or "input")
    except Exception as e:  # noqa: BLE001
        return fail("internal", repr(e), "settings")


get_settings = tool(get_settings_tool)


def change_setting_tool(key: str, scope: str, value: Union[str, int, float],
                        day: str | None = None, reason: str | None = None,
                        request_id: str | None = None) -> dict:
    if not selftest_ok():
        return fail("selftest_failed",
                    "Planner selftest failed at startup. Scheduler actions are blocked until it passes.",
                    "planner")
    try:
        data = change_setting_sync(key, value, scope, day=day, reason=reason,
                                   request_id=request_id, door="app")
        data, alerts, message = maybe_refresh_alerts(data)
        kind = "change" if data.get("status") == "applied" else "pending"
        if data.get("status") == "pending":
            message = message or PENDING_MESSAGE
        return ok_reply(kind, data, ref=data.get("change_id"),
                        message=message, alerts=alerts, fallback_text=data.get("say"))
    except SettingsError as e:
        return _settings_fail(e)
    except Exception as e:  # noqa: BLE001
        return fail("internal", repr(e), "settings")


change_setting = tool(change_setting_tool)


def undo_change_tool(change_id: str | None = None) -> dict:
    if not selftest_ok():
        return fail("selftest_failed",
                    "Planner selftest failed at startup. Scheduler actions are blocked until it passes.",
                    "planner")
    try:
        data = undo_change_sync(change_id=change_id, door="app")
        data, alerts, message = maybe_refresh_alerts(data)
        return ok_reply("undo", data, ref=data.get("change_id"),
                        message=message, alerts=alerts, fallback_text=data.get("say"))
    except SettingsError as e:
        return _settings_fail(e)
    except Exception as e:  # noqa: BLE001
        return fail("internal", repr(e), "settings")


undo_change = tool(undo_change_tool)


def _arrival_day(t_s, now=None):
    dt = datetime.fromtimestamp(int(t_s), gcal.TZ)
    return dt.date()


# ── what scheduling plugs into places ──────────────────────────────────

def _plan_arrival_hook(place=None, title=None, coords=None, t_s=None, now=None, now_s=None, farm_done=False):
    """The leave-by plan for an arrival places has accepted. None = scheduling can't plan now
    (planner selftest failed): places then answers plainly. A failure reply is returned as is."""
    if not selftest_ok():
        return None
    try:
        is_test = str(title or "").strip().lower() == "test"
        custom = bool(place) and place not in rp.PLACES and place != "gymday"
        events = []
        if not is_test and not custom:
            day = _arrival_day(t_s, now=now)
            events = gcal.events_for_plan(day, day, before=1, after=1, now=now)
        inp = {"time": t_s, "now": now_s, "title": title or "", "events": events}
        if place:
            inp["place"] = place
        if coords not in (None, ""):
            inp["coords"] = coords
        if farm_done:
            inp["farm_done"] = True
        ensure_store(now=now)
        with planner_swap(day=_arrival_day(t_s, now=now), now=now):
            return rp.cmd_arrival(inp)
    except gcal.GcalError as e:
        return _gcal_fail(e)
    except rp.RiviaError as e:
        if e.code == "need_maps":
            return fail("need_estimate", str(e), "planner", data={"pairs": e.extra.get("pairs") or []})
        step = "input" if e.code in ("bad_place", "bad_time", "bad_coords") else "planner"
        return fail(e.code, str(e), step, data=dict(e.extra) if e.extra else {})


def morning_rundown_sync(t_s, now=None, now_s=None):
    """Build today's rundown (calendar + planner). Places marks the day as given. Raises on failure."""
    if not selftest_ok():
        raise SettingsError("selftest_failed", "Planner selftest failed at startup.", step="planner")
    now_s = now_s or t_s
    day = _arrival_day(t_s)
    events = gcal.events_for_plan(day, day, before=1, after=1, now=now)
    ensure_store(now=now)
    with planner_swap(day=day, now=now):
        rides = rp.load_rides()
        leg = rp.Legs(None, rides)
        out = rp.morning_rundown(t_s, now_s, events, leg, rp.farm_visited(rides, t_s))
        leg.check()
    return out


def _after_ride_hook(ride_out):
    return gcal.run_drive_backup()


places.set_hook("plan_arrival", _plan_arrival_hook)
places.set_hook("morning_rundown", morning_rundown_sync)
places.set_hook("after_ride", _after_ride_hook)


def replay_last_arrival(now=None):
    """Rerun the stored last arrival with current settings. Raises on failure."""
    rec = _load_last_arrival()
    if not rec:
        raise SettingsError("no_arrival", "There is no last arrival to rerun.", step="settings")
    out = arrival_sync(
        place=rec.get("place"), title=rec.get("title"), coords=rec.get("coords"),
        time=rec.get("time"), ride=None, now=now, replay=True,
    )
    if not out.get("ok"):
        err = out.get("error") or {}
        raise SettingsError(
            err.get("code") or "alerts_refresh",
            err.get("msg") or "The last arrival could not be rerun.",
            step=err.get("step") or "planner",
        )
    return out


def maybe_refresh_alerts(data, now=None):
    """If a day change or undo applies today, rerun the last arrival.

    The change itself already stands. On failure: alerts is null and
    data.alerts_refresh is 'failed'. Returns (data, alerts, message).
    """
    data = dict(data or {})
    today = _today_date(now).isoformat()
    applies = data.get("scope") == "day" and data.get("day") == today
    if not applies or data.get("duplicate") or data.get("status") not in (None, "applied"):
        return data, None, None
    if _load_last_arrival() is None:
        return data, None, None
    try:
        arr = replay_last_arrival(now=now)
        data["alerts_refresh"] = "ok"
        return data, arr.get("alerts"), arr.get("message")
    except Exception:  # noqa: BLE001
        data["alerts_refresh"] = "failed"
        return data, None, None


def invoke_app_tool(name, args=None):
    """Call a seeded app-door tool locally (rivia sched call)."""
    import rivia_tools as tools
    name = str(name or "").strip()
    if not name:
        raise SettingsError("bad_tool", "usage: rivia sched call <tool> [json]", step="input")
    rows = [r for r in tools.load_rows(door="app", enabled_only=True) if r["name"] == name]
    if not rows:
        raise SettingsError("bad_tool", "No app-door tool named %s." % name, step="input")
    row = rows[0]
    action = row["action"]
    if action not in tools.ACTIONS:
        raise SettingsError("bad_tool", "Tool %s has no action." % name, step="input")
    info = tools.ACTIONS[action]
    preset = row.get("preset") or {}
    if isinstance(preset, str):
        preset = json.loads(preset or "{}")
    merged = dict(preset)
    merged.update(args or {})
    kwargs = {}
    for py, pub in (info.get("py_params") or {}).items():
        if pub in merged and merged[pub] is not None:
            kwargs[py] = merged[pub]
    fn = info["fn"]
    result = fn(**kwargs)
    if hasattr(result, "__await__"):
        result = asyncio.run(result)
    return result


def cmd_call(name, raw_json="{}"):
    raw = raw_json if raw_json is not None else "{}"
    raw = str(raw).strip() or "{}"
    try:
        args = json.loads(raw)
    except ValueError as e:
        raise SettingsError("bad_json", "Arguments must be a JSON object: %s" % e, step="input") from e
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise SettingsError("bad_json", "Arguments must be a JSON object.", step="input")
    return invoke_app_tool(name, args)


# Only actions that exist now belong here (G5: seed a row only once its action works).

def _parse_hm(raw, default=(5, 30)):
    try:
        h, m = str(raw).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass
    return default


def auto_write_hm(con=None):
    """Hour and minute of the morning tick, from auto_write_time, else env, else 05:30."""
    raw = None
    own = None
    try:
        own = db_open()
        row = own.execute("select value from settings where key=?", ("auto_write_time",)).fetchone()
        if row and row[0] is not None:
            raw = _load_value(row[0])
    except Exception:
        raw = None
    finally:
        if own is not None:
            try:
                own.close()
            except Exception:
                pass
    if not raw:
        raw = os.environ.get("RIVIA_SIGNIN_CHECK_TIME") or DEFAULT_AUTO_WRITE
    return _parse_hm(raw)


def _plan_record(day, now=None, con=None):
    events = gcal.events_for_plan(day, day, before=2, after=2, now=now)
    ensure_store(now=now)
    out = _plan_one(day, events, now=now)
    return out["days"][0], out


def _preview_from_rec(rec):
    return {
        "kind": "write_day",
        "day": rec.get("day"),
        "status": rec.get("status"),
        "blocks": rec.get("blocks") or [],
        "calendar_events": rec.get("calendar_events") or [],
        "shift": rec.get("shift"),
        "problems": rec.get("problems") or [],
    }


def _block_fingerprint(preview):
    preview = preview or {}
    blocks = []
    for b in preview.get("blocks") or []:
        start, end = b.get("start"), b.get("end")
        start_s = start.get("iso") if isinstance(start, dict) else start
        end_s = end.get("iso") if isinstance(end, dict) else end
        blocks.append((b.get("kind"), b.get("title"), b.get("place"), start_s, end_s))
    return (preview.get("status"), tuple(blocks))


def _find_pending_write_day(con, day_s, now_s):
    expire_pending(con, now_s)
    return con.execute(
        "select * from changes where status='pending' and kind='write_day' and ifnull(day,'')=? "
        "order by created desc, rowid desc limit 1",
        (day_s or "",),
    ).fetchone()


def _rivia_events_exist(day_s):
    cal_id = gcal._meta_get(gcal.META_RIVIA_ID)
    if not cal_id or str(cal_id).strip().lower() == "primary":
        return False
    try:
        return bool(gcal.list_rivia_events(cal_id, day_s))
    except Exception:
        return False


def _write_day_pending_result(row, *, duplicate=False):
    preview = _preview_obj(row)
    return {
        "change_id": row[0],
        "key": "write_day",
        "kind": "write_day",
        "day": row[5],
        "status": row[10],
        "preview": preview,
        "asked": _asked_of(row[2]),
        "expires": _expires_iso(row),
        "reason": row[12],
        "duplicate": duplicate,
        "written": False,
        "request_id": row[13],
    }


def _queue_write_day(con, rec, *, door, now_s, reason=None, request_id=None):
    preview = _preview_from_rec(rec)
    change_id = _new_id()
    created = rp.iso(now_s)
    _log_change(
        con, change_id=change_id, created=created, door=door, kind="write_day",
        scope="day", day=rec["day"], key="write_day", old=None,
        new=None, preview=json.dumps(preview), status="pending", decided=None,
        reason=reason, request_id=request_id,
    )
    row = con.execute("select * from changes where change_id=?", (change_id,)).fetchone()
    return _write_day_pending_result(row, duplicate=False)


def write_day_sync(day_arg="tomorrow", request_id=None, door="app", now=None, con=None, reason=None):
    """Always queue a write_day with a preview of the blocks. Never writes here.
    con is accepted for older callers and ignored: the queue lives in approvals.db."""
    ensure_store(now=now)
    con = approvals.db_open()
    close = True
    try:
        now = now or datetime.now(gcal.TZ)
        now_s = int(now.timestamp()) if hasattr(now, "timestamp") else rp.now_s()
        day, _asked = gcal.resolve_day(day_arg or "tomorrow", now=now)
        rec, _out = _plan_record(day, now=now, con=con)
        pending = _find_pending_write_day(con, rec["day"], now_s)
        if pending:
            return _write_day_pending_result(pending, duplicate=True)
        if request_id:
            existing = con.execute(
                "select * from changes where request_id=? order by created desc, rowid desc limit 1",
                (request_id,),
            ).fetchone()
            if existing and existing[10] == "pending" and existing[3] == "write_day":
                return _write_day_pending_result(existing, duplicate=True)
        return _queue_write_day(con, rec, door=door or "app", now_s=now_s,
                                reason=reason, request_id=request_id)
    finally:
        if close:
            con.close()


def _stable_plan_now(day):
    """Plan from just before the calendar day so overnight sleep is not under_way."""
    return datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=gcal.TZ) - timedelta(seconds=1)


def _selftest_guard(spec=None, door=None):
    """Scheduling's settings and write_day approvals wait while the planner selftest fails
    (system seeding is let through). Places is never blocked by this."""
    if door == "system":
        return
    if not selftest_ok():
        raise SettingsError("selftest_failed",
                            "Planner selftest failed at startup. Scheduler actions are blocked until it passes.",
                            step="planner")


def _approve_write_day_kind(row, now, now_s):
    """approvals calls this to approve a write_day row."""
    _selftest_guard()
    con = approvals.db_open()
    try:
        return _approve_write_day(con, row, now, now_s)
    finally:
        con.close()


def _write_day_public(row, out):
    out["preview"] = _preview_obj(row)


approvals.register_kind("write_day", approve=_approve_write_day_kind, expires_s=lambda row: _write_day_expires_s(row),
                        too_late=lambda row, now_s: _write_day_too_late(row, now_s), public=_write_day_public)


def _approve_write_day(con, row, now, now_s):
    cid, created, door, kind, scope, day_s, key, old_s, new_s, preview_s, status, decided, reason, request_id = row
    preview = _preview_obj(row)
    day = date.fromisoformat(str(day_s))
    # Re-plan at the start of the day so overnight sleep does not flip the
    # status to under_way (which would drop calendar_events). Catch-up is
    # refused separately once leave-home has started.
    rec, _out = _plan_record(day, now=_stable_plan_now(day), con=con)
    if _write_day_too_late(row, now_s):
        raise SettingsError(
            "under_way",
            "The day has already started; nothing was written.",
            step="write",
        )
    fresh = _preview_from_rec(rec)
    if _block_fingerprint(preview) != _block_fingerprint(fresh):
        raise SettingsError(
            "changed_since_preview",
            "The plan changed since this preview; nothing was written.",
            step="write",
            extra={"preview": preview, "current": fresh, "day": day_s},
        )
    if rec["status"] == "not_written":
        # Preview agreed it was not_written: still write nothing.
        decided_s = rp.iso(now_s)
        con.execute("update changes set status='applied', decided=? where change_id=?", (decided_s, cid))
        return {
            "change_id": cid, "key": "write_day", "kind": "write_day", "day": day_s,
            "status": "applied", "written": False, "plan_status": rec["status"],
            "created": 0, "deleted": 0, "asked": _asked_of(door), "reason": reason,
        }
    result = gcal.write_rivia_day(
        day_s, rec["status"], rec.get("calendar_events") or [],
    )
    decided_s = rp.iso(now_s)
    con.execute("update changes set status='applied', decided=? where change_id=?", (decided_s, cid))
    data = {
        "change_id": cid, "key": "write_day", "kind": "write_day", "day": day_s,
        "status": "applied", "written": bool(result.get("written")),
        "plan_status": rec["status"], "created": result.get("created") or 0,
        "deleted": result.get("deleted") or 0,
        "calendar_id": result.get("calendar_id"),
        "batch_id": result.get("batch_id"),
        "asked": _asked_of(door), "reason": reason, "approved": True,
    }
    return data


def write_day_tool(day: str = "tomorrow", request_id: str | None = None) -> dict:
    if not selftest_ok():
        return fail("selftest_failed",
                    "Planner selftest failed at startup. Scheduler actions are blocked until it passes.",
                    "planner")
    try:
        data = write_day_sync(day_arg=day or "tomorrow", request_id=request_id, door="app")
        return ok_reply("pending", data, ref=data.get("day") or data.get("change_id"),
                        message=PENDING_MESSAGE)
    except SettingsError as e:
        return _settings_fail(e)
    except gcal.GcalError as e:
        return fail(e.code, str(e), e.step or "calendar")
    except rp.RiviaError as e:
        if e.code == "need_maps":
            return fail("need_estimate", str(e), "planner",
                        data={"pairs": e.extra.get("pairs") or []})
        step = "input" if e.code in ("bad_day", "bad_days", "bad_events") else "planner"
        return fail(e.code, str(e), step, data=dict(e.extra) if e.extra else {})
    except Exception as e:  # noqa: BLE001
        return fail("internal", repr(e), "write")


write_day = tool(write_day_tool)


def _cutoff_for_day(day, con=None):
    h, m = auto_write_hm(con=con)
    return datetime(day.year, day.month, day.day, h, m, 0, tzinfo=gcal.TZ)


def auto_write_due(now=None, con=None):
    """Auto-approve each still-pending write_day whose cutoff has passed.

    Standing changes are never auto-approved. If the plan changed since the
    preview, write nothing, mark the item failed, and notify.
    """
    ensure_store(now=now)
    con = approvals.db_open()
    close = True
    results = []
    try:
        now = now or datetime.now(gcal.TZ)
        if now.tzinfo is None:
            now = now.replace(tzinfo=gcal.TZ)
        now = now.astimezone(gcal.TZ)
        now_s = int(now.timestamp())
        expire_pending(con, now_s)
        rows = list(con.execute(
            "select * from changes where status='pending' and kind='write_day' order by created, rowid"
        ))
        for row in rows:
            day_s = row[5]
            try:
                day = date.fromisoformat(str(day_s))
            except Exception:
                continue
            if now < _cutoff_for_day(day, con=con):
                continue
            if _write_day_too_late(row, now_s):
                continue
            try:
                data = approve_change_sync(row[0], now=now, con=con)
                results.append({"change_id": row[0], "day": day_s, "ok": True,
                                "written": bool(data.get("written")), "data": data})
            except SettingsError as e:
                if e.code == "changed_since_preview":
                    decided_s = rp.iso(now_s)
                    con.execute(
                        "update changes set status='failed', decided=?, reason=? where change_id=?",
                        (decided_s, e.code, row[0]),
                    )
                    gcal.post_termux_notification(WRITE_DAY_NOTIFY)
                results.append({"change_id": row[0], "day": day_s, "ok": False,
                                "error": e.code, "msg": str(e)})
            except gcal.GcalError as e:
                results.append({"change_id": row[0], "day": day_s, "ok": False,
                                "error": e.code, "msg": str(e)})
            except Exception as e:  # noqa: BLE001
                print("rivia: auto-write of %s failed (%s); continuing." % (row[0], type(e).__name__),
                      file=sys.stderr)
                results.append({"change_id": row[0], "day": day_s, "ok": False,
                                "error": "internal", "msg": type(e).__name__})
        return results
    finally:
        if close:
            con.close()


def propose_tomorrow(now=None, con=None):
    """D2: create a system write_day for tomorrow, or keep an existing pending one.

    Propose nothing if there is nothing to write or delete.
    """
    ensure_store(now=now)
    con = approvals.db_open()
    close = True
    try:
        now = now or datetime.now(gcal.TZ)
        if now.tzinfo is None:
            now = now.replace(tzinfo=gcal.TZ)
        now = now.astimezone(gcal.TZ)
        now_s = int(now.timestamp())
        tomorrow = now.date() + timedelta(days=1)
        day_s = tomorrow.isoformat()
        existing = _find_pending_write_day(con, day_s, now_s)
        if existing:
            return {"proposed": False, "kept": existing[0], "day": day_s,
                    "status": "pending"}
        rec, _out = _plan_record(tomorrow, now=now, con=con)
        has_events = _rivia_events_exist(day_s)
        if rec["status"] == "planned":
            pass
        elif rec["status"] == "no_shift" and has_events:
            pass
        else:
            return {"proposed": False, "reason": rec.get("status") or "empty",
                    "day": day_s}
        data = _queue_write_day(
            con, rec, door="system", now_s=now_s,
            reason="auto proposal for tomorrow",
        )
        return {"proposed": True, "change_id": data["change_id"], "day": day_s,
                "plan_status": rec["status"], "asked": "system"}
    finally:
        if close:
            con.close()


def evening_refresh_due(now, last_ran_day=None):
    if now.tzinfo is None:
        now = now.replace(tzinfo=gcal.TZ)
    now = now.astimezone(gcal.TZ)
    cutoff = now.replace(hour=EVENING_HOUR, minute=EVENING_MINUTE, second=0, microsecond=0)
    if now < cutoff:
        return False
    last = last_ran_day if last_ran_day is not None else gcal._meta_get(META_EVENING_DAY)
    return last != now.date().isoformat()


def mark_evening_refresh_ran(day_s):
    try:
        gcal._meta_set(META_EVENING_DAY, day_s)
    except Exception as e:  # noqa: BLE001
        print("rivia: could not record evening refresh (%s); continuing." % type(e).__name__,
              file=sys.stderr)


def run_evening_refresh(now=None, con=None):
    """D5: at 21:00 ET, re-plan tomorrow. If it differs, supersede the pending item."""
    ensure_store(now=now)
    con = approvals.db_open()
    close = True
    try:
        now = now or datetime.now(gcal.TZ)
        if now.tzinfo is None:
            now = now.replace(tzinfo=gcal.TZ)
        now = now.astimezone(gcal.TZ)
        now_s = int(now.timestamp())
        tomorrow = now.date() + timedelta(days=1)
        day_s = tomorrow.isoformat()
        rec, _out = _plan_record(tomorrow, now=now, con=con)
        fresh = _preview_from_rec(rec)
        pending = _find_pending_write_day(con, day_s, now_s)
        mark_evening_refresh_ran(now.date().isoformat())
        has_events = _rivia_events_exist(day_s)
        worthwhile = rec["status"] == "planned" or (rec["status"] == "no_shift" and has_events)
        if pending is None:
            if not worthwhile:
                return {"superseded": False, "proposed": False, "day": day_s,
                        "reason": rec.get("status")}
            data = _queue_write_day(
                con, rec, door="system", now_s=now_s,
                reason="evening refresh proposal",
            )
            return {"superseded": False, "proposed": True,
                    "change_id": data["change_id"], "day": day_s}
        old = _preview_obj(pending)
        if _block_fingerprint(old) == _block_fingerprint(fresh):
            return {"superseded": False, "proposed": False,
                    "change_id": pending[0], "day": day_s}
        decided_s = rp.iso(now_s)
        con.execute(
            "update changes set status='superseded', decided=? where change_id=?",
            (decided_s, pending[0]),
        )
        if not worthwhile:
            return {"superseded": True, "proposed": False, "old": pending[0],
                    "day": day_s, "reason": rec.get("status")}
        data = _queue_write_day(
            con, rec, door="system", now_s=now_s,
            reason="evening refresh; previous proposal superseded",
        )
        return {"superseded": True, "proposed": True, "old": pending[0],
                "change_id": data["change_id"], "day": day_s}
    finally:
        if close:
            con.close()


def run_morning_tick(now=None):
    """Sign-in check, auto-write due write_days, propose tomorrow, Drive backup."""
    now = now or datetime.now(gcal.TZ)
    if getattr(now, "tzinfo", None) is None:
        now = now.replace(tzinfo=gcal.TZ)
    now = now.astimezone(gcal.TZ)
    out = {"ok": True, "day": now.date().isoformat(),
           "signin": None, "auto_write": [], "propose": None, "backup": None}
    try:
        out["signin"] = gcal.run_signin_check(now=now, mark_ran=True)
    except Exception as e:  # noqa: BLE001
        print("rivia: sign-in check failed (%s); continuing." % type(e).__name__, file=sys.stderr)
        out["signin"] = {"ok": False, "error": type(e).__name__}
    try:
        out["auto_write"] = auto_write_due(now=now)
    except Exception as e:  # noqa: BLE001
        print("rivia: auto-write failed (%s); continuing." % type(e).__name__, file=sys.stderr)
        out["auto_write"] = [{"ok": False, "error": type(e).__name__}]
    try:
        out["propose"] = propose_tomorrow(now=now)
    except Exception as e:  # noqa: BLE001
        print("rivia: tomorrow proposal failed (%s); continuing." % type(e).__name__, file=sys.stderr)
        out["propose"] = {"proposed": False, "error": type(e).__name__}
    try:
        out["backup"] = gcal.run_drive_backup()
    except Exception as e:  # noqa: BLE001
        print("rivia: daily Drive backup failed (%s); continuing." % type(e).__name__, file=sys.stderr)
        out["backup"] = {"ok": False, "error": type(e).__name__}
    return out


def cmd_morning_tick():
    out = run_morning_tick()
    out = dict(out)
    out["ok"] = True
    return out


def cmd_evening_refresh():
    out = run_evening_refresh()
    out = dict(out)
    out["ok"] = True
    return out


ACTIONS = {
    "sched.ride_time": {
        "fn": ride_time,
        "lockable": ("from", "to"),
        "py_params": RIDE_TIME_ARGS,
        "doors": ("app", "bot"),
        "kind": "legs",
        "time_bearing": False,
        "description": RIDE_TIME_DESCRIPTION,
    },
    "sched.plan_day": {
        "fn": plan_day,
        "lockable": ("day",),
        "py_params": {"day": "day"},
        "doors": ("app", "bot"),
        "kind": "plan",
        "time_bearing": True,
        "description": PLAN_DAY_DESCRIPTION,
    },
    "sched.plan_week": {
        "fn": plan_week,
        "lockable": (),
        "py_params": {},
        "doors": ("app", "bot"),
        "kind": "plan",
        "time_bearing": True,
        "description": PLAN_WEEK_DESCRIPTION,
    },
    "sched.get_settings": {
        "fn": get_settings,
        "lockable": ("group", "day"),
        "py_params": {"group": "group", "day": "day"},
        "doors": ("app",),
        "kind": "settings",
        "time_bearing": False,
        "description": GET_SETTINGS_DESCRIPTION,
    },
    "sched.change_setting": {
        "fn": change_setting,
        "lockable": ("key", "scope", "value", "day"),
        "py_params": {"key": "key", "scope": "scope", "value": "value", "day": "day",
                      "reason": "reason", "request_id": "request_id"},
        "doors": ("app",),
        "kind": "change",
        "time_bearing": False,
        "description": CHANGE_SETTING_DESCRIPTION,
    },
    "sched.undo_change": {
        "fn": undo_change,
        "lockable": ("change_id",),
        "py_params": {"change_id": "change_id"},
        "doors": ("app",),
        "kind": "undo",
        "time_bearing": False,
        "description": UNDO_CHANGE_DESCRIPTION,
    },
    "sched.write_day": {
        "fn": write_day,
        "lockable": ("day",),
        "py_params": {"day": "day", "request_id": "request_id"},
        "doors": ("app",),
        "kind": "pending",
        "time_bearing": True,
        "description": WRITE_DAY_DESCRIPTION,
    },
}
ACTIONS["sched.change_setting"]["pending_line"] = True
ACTIONS["sched.write_day"]["pending_line"] = True

# Seeded on first start, only for actions that exist now (G5). plan_week has no seeded row.
SEED = (
    {"name": "ride_time", "door": "app", "action": "sched.ride_time",
     "preset": {}, "description": RIDE_TIME_DESCRIPTION, "model": 1, "enabled": 1},
    {"name": "plan_day", "door": "app", "action": "sched.plan_day",
     "preset": {}, "description": PLAN_DAY_DESCRIPTION, "model": 1, "enabled": 1},
    {"name": "get_schedule_settings", "door": "app", "action": "sched.get_settings",
     "preset": {"group": "schedule"}, "description": GET_SETTINGS_DESCRIPTION, "model": 1, "enabled": 1},
    {"name": "change_schedule_setting", "door": "app", "action": "sched.change_setting",
     "preset": {}, "description": CHANGE_SETTING_DESCRIPTION, "model": 1, "enabled": 1},
    {"name": "undo_schedule_change", "door": "app", "action": "sched.undo_change",
     "preset": {}, "description": UNDO_CHANGE_DESCRIPTION, "model": 1, "enabled": 1},
    {"name": "write_day", "door": "app", "action": "sched.write_day",
     "preset": {}, "description": WRITE_DAY_DESCRIPTION, "model": 1, "enabled": 1},
)
# Seeded descriptions that a later version rewrote (refreshed only if a row still holds one exactly).
OLD_DESCRIPTIONS = {
    "change_schedule_setting": (
        "Change one of Jordan's schedule settings. scope is day (that date only, today "
        "through 14 days ahead) or standing. A standing change from this door is not "
        "applied until it is approved; the reply is pending. A pending result has not "
        "happened yet. Out-of-range or inconsistent values are refused with the reason.",
        "Change one of Jordan's schedule settings. scope is day (that date only, today "
        "through 14 days ahead) or standing. A standing change from this door is not "
        "applied. Out-of-range or inconsistent values are refused with the reason.",
    ),
    "undo_schedule_change": (
        "Undo a schedule setting change. With no change_id, undo the latest change made "
        "through this door in the last 24 hours. Restores the old value and logs a new change.",
    ),
}
tools.register(ACTIONS, SEED, OLD_DESCRIPTIONS)

settings.register_owner({
    "name": "scheduling", "open": db_open, "parse": _sched_parse, "keys": _sched_keys,
    "consistency": _sched_consistency, "dry_run": _sample_day_problems,
    "result_extra": _sched_result_extra, "item_extra": _sched_item_extra,
    "resolve_day": lambda text, now=None: gcal.resolve_day(text, now=now),
    "guard": _selftest_guard,
})


def cmd_approve(change_id):
    out = approve_change_sync(change_id)
    out = dict(out)
    out["ok"] = True
    return out


def cmd_reject(change_id):
    out = reject_change_sync(change_id)
    out = dict(out)
    out["ok"] = True
    return out


_PLACE_TOOLS = ("app_places", "app_pin_place", "app_arrival", "app_open", "app_log_ride")


def main(argv=None):
    import sys as _sys
    argv = list(_sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "settings"
    if cmd in ("-h", "--help"):
        print("rivia_sched.py settings [day] | settings-log | rides | restore <file> | start-fresh | pin <place> <lat> <lng> | places | seed-places | add-place <name> <lat> <lng> [meters] | rename-place <place> <name> | remove-place <place> | zone <place> <meters|bigger|smaller> | exclude-ride <ride_id> <reason> | include-ride <ride_id> | call <tool> [json] | approve <id> | reject <id> | morning-tick | evening-refresh")
        return 2
    try:
        # restore / rides / start-fresh are Termux recovery commands; they must
        # work even if selftest failed.
        if cmd == "rides":
            print(json.dumps(cmd_rides(), indent=1))
            return 0
        if cmd == "restore":
            if len(argv) < 2:
                print("usage: rivia sched restore <file>", file=_sys.stderr)
                return 2
            out = cmd_restore_file(argv[1])
            print(json.dumps(out, indent=1))
            return 0
        if cmd == "start-fresh":
            out = cmd_start_fresh()
            print(json.dumps(out, indent=1))
            return 0
        if cmd in ("exclude-ride", "include-ride"):
            if len(argv) < (3 if cmd == "exclude-ride" else 2):
                print('usage: rivia sched exclude-ride <ride_id> "<reason>" | include-ride <ride_id>', file=_sys.stderr)
                return 2
            out = rp.exclude_ride(argv[1], " ".join(argv[2:])) if cmd == "exclude-ride" else rp.include_ride(argv[1])
            print(json.dumps({"ok": True, **out}, indent=1))
            return 0
        # Places commands work even when scheduling's planner selftest failed.
        if cmd == "pin":
            if len(argv) < 4:
                print("usage: rivia sched pin <place> <lat> <lng>", file=_sys.stderr)
                return 2
            out = cmd_pin(argv[1], argv[2], argv[3])
            print(json.dumps(out, indent=1))
            return 0
        usage = {"add-place": (4, "add-place <name> <lat> <lng> [meters]"), "rename-place": (3, "rename-place <place> <name>"),
                 "remove-place": (2, "remove-place <place>"), "zone": (3, "zone <place> <meters|bigger|smaller>")}
        if cmd in usage and len(argv) < usage[cmd][0]:
            print("usage: rivia sched %s" % usage[cmd][1], file=_sys.stderr)
            return 2
        place_cmds = {
            "places": lambda: list_places(),
            "seed-places": lambda: seed_places(),
            "add-place": lambda: add_place(argv[1], argv[2], argv[3], argv[4] if len(argv) > 4 else None),
            "rename-place": lambda: rename_place(argv[1], " ".join(argv[2:])),
            "remove-place": lambda: remove_place(argv[1]),
            "zone": lambda: set_zone(argv[1], argv[2]),
        }
        if cmd in place_cmds:
            out = place_cmds[cmd]()
            print(json.dumps({"ok": True, **out}, indent=1))
            return 0
        if cmd == "settings-log":
            print(json.dumps({"ok": True, "changes": approvals.settings_log()}, indent=1))
            return 0
        if cmd == "approve":
            if len(argv) < 2:
                print("usage: rivia sched approve <change_id>", file=_sys.stderr)
                return 2
            out = cmd_approve(argv[1])
            print(json.dumps(out, indent=1))
            return 0
        if cmd == "reject":
            if len(argv) < 2:
                print("usage: rivia sched reject <change_id>", file=_sys.stderr)
                return 2
            out = cmd_reject(argv[1])
            print(json.dumps(out, indent=1))
            return 0
        if cmd == "call" and len(argv) > 1 and argv[1] in _PLACE_TOOLS:
            out = cmd_call(argv[1], argv[2] if len(argv) > 2 else "{}")
            print(json.dumps(out, indent=1))
            return 0 if out.get("ok") else 1
        if not selftest_ok():
            print(json.dumps({"ok": False, "error": {"code": "selftest_failed", "msg": "planner selftest failed", "step": "planner"}}, indent=1))
            return 1
        if cmd == "call":
            if len(argv) < 2:
                print("usage: rivia sched call <tool> [json]", file=_sys.stderr)
                return 2
            out = cmd_call(argv[1], argv[2] if len(argv) > 2 else "{}")
            print(json.dumps(out, indent=1))
            return 0 if out.get("ok") else 1
        if cmd == "settings":
            day = argv[1] if len(argv) > 1 else None
            data = get_settings_sync(group="schedule", day=day)
            print(json.dumps({"ok": True, **data}, indent=1))
            return 0
        if cmd == "morning-tick":
            out = cmd_morning_tick()
            print(json.dumps(out, indent=1))
            return 0
        if cmd == "evening-refresh":
            out = cmd_evening_refresh()
            print(json.dumps(out, indent=1))
            return 0
        print("unknown command: %s (use settings, settings-log, rides, restore <file>, start-fresh, pin, places, seed-places, add-place, rename-place, remove-place, zone, exclude-ride, include-ride, call, approve, reject, morning-tick, or evening-refresh)" % cmd, file=_sys.stderr)
        return 2
    except SettingsError as e:
        print(json.dumps({"ok": False, "error": {"code": e.code, "msg": str(e), "step": e.step}}, indent=1))
        return 1
    except rp.RiviaError as e:
        print(json.dumps({"ok": False, "error": {"code": e.code, "msg": str(e), "step": "restore"}}, indent=1))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
