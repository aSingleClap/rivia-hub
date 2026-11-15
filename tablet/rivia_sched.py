# rivia_sched.py: scheduler actions. Each action is a standalone FastMCP tool
# object, never registered itself. rivia_tools.py turns tool-table rows into
# door tools with Tool.from_tool.
import asyncio
import json
import re
import os
from datetime import datetime, timedelta
from typing import Any, Union

from fastmcp.tools import tool

import rivia_planner as rp
import rivia_gcal as gcal

# Cached startup selftest. None = not run yet. Tests may call set_selftest().
_selftest: dict[str, Any] | None = None

FAIL_RULE = ("If a tool returns ok=false, report the failure as ok=false and stop that task. "
             "Never substitute data from another device, an older answer or a guess.")

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


def envelope(*, ok, kind, ref=None, message=None, data=None, alerts=None,
             fallback_text=None, error=None):
    return {"v": 1, "ok": ok, "kind": kind, "ref": ref, "message": message,
            "data": data if data is not None else {}, "alerts": alerts,
            "fallback_text": fallback_text, "error": error}


def fail(code, msg, step, *, data=None, ref=None):
    return envelope(ok=False, kind="error", ref=ref, message=None,
                    data=data if data is not None else {},
                    error={"code": code, "msg": msg, "step": step})


def ok_reply(kind, data, *, ref=None, message=None, alerts=None, fallback_text=None):
    return envelope(ok=True, kind=kind, ref=ref, message=message, data=data,
                    alerts=alerts, fallback_text=fallback_text, error=None)


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
            con = rp.db_open()
            try:
                rp.set_meta(con, "selftest", json.dumps({
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

SETTINGS_SCHEMA = """
create table if not exists settings(
    key text primary key, value text not null, updated text not null, change_id text);
create table if not exists overrides(
    day text not null, key text not null, value text not null,
    created text not null, change_id text, primary key (day, key));
create table if not exists changes(
    change_id text primary key, created text not null, door text, kind text,
    scope text, day text, key text, old text, new text, preview text,
    status text, decided text, reason text, request_id text);
"""

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
    "geofence_radius_meters":{"planner": None,             "default": 150,  "min": 100,  "max": 400,  "type": "int",   "unit": "meters",  "label": "Geofence radius",          "day": False, "ask": False, "group": "system", "doors": ("termux", "admin")},
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

RIDE_PLACES = ("farm", "gym", "hmc", "home", "pf")  # alphabetical
GYM_HOUR_FIELDS = {
    "weekday_open": "weekdayOpen", "weekday_close": "weekdayClose",
    "weekend_open": "weekendOpen", "weekend_close": "weekendClose",
}
SEED_ESTIMATES = (("ride_estimate.farm_pf", 17), ("ride_estimate.hmc_pf", 15))
DAY_WINDOW_DAYS = 14
DUPLICATE_S = 60
UNDO_S = 24 * 3600
PENDING_EXPIRE_S = 24 * 3600
PENDING_LINE = "A pending result has not happened yet."
PENDING_MESSAGE = "waiting for approval"

GET_SETTINGS_DESCRIPTION = (
    "Jordan's schedule settings for a day: each key's value, default, unit, range, "
    "and whether it comes from the default, a standing value, or a day override."
)
CHANGE_SETTING_DESCRIPTION = (
    "Change one of Jordan's schedule settings. scope is day (that date only, today "
    "through 14 days ahead) or standing. A standing change from this door is not "
    "applied until it is approved; the reply is pending. A pending result has not "
    "happened yet. Out-of-range or inconsistent values are refused with the reason."
)
UNDO_CHANGE_DESCRIPTION = (
    "Undo a schedule setting change. With no change_id, undo the latest change made "
    "through this door in the last 24 hours. Restores the old value and logs a new change."
)
PLACES_DESCRIPTION = (
    "Jordan's places for geofences: each place's id, spoken name, pin (lat, lng), "
    "and geofence radius in meters."
)
ARRIVAL_DESCRIPTION = (
    "Jordan just arrived at a place. Logs a ride if one is given (safe to repeat "
    "on the same ride_id), reads the calendar, and returns the leave-by message, "
    "alerts, and fallback_text. title 'test' returns a plumbing alert two minutes out."
)
LOG_RIDE_DESCRIPTION = (
    "Log one bike ride between two of Jordan's places. Safe to repeat on the same "
    "ride_id (returns duplicate and does not store a second copy)."
)
LAST_ARRIVAL_KEY = "last_arrival"


class SettingsError(Exception):
    def __init__(self, code, msg, *, step="settings", extra=None):
        super().__init__(msg)
        self.code = code
        self.step = step
        self.extra = extra or {}


def _today_date(now=None):
    if now is None:
        return datetime.now(gcal.TZ).date()
    if isinstance(now, date) and not isinstance(now, datetime):
        return now
    if hasattr(now, "astimezone"):
        return now.astimezone(gcal.TZ).date()
    if hasattr(now, "date"):
        return now.date()
    return date.fromisoformat(str(now)[:10])


def _as_date(day):
    if day is None:
        return None
    if isinstance(day, datetime):
        return day.astimezone(gcal.TZ).date() if day.tzinfo else day.date()
    if isinstance(day, date):
        return day
    return date.fromisoformat(str(day)[:10])


def _store_value(v):
    if isinstance(v, bool):
        raise SettingsError("bad_value", "A setting value cannot be true or false.", step="input")
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return json.dumps(v, separators=(",", ":"))


def _load_value(text):
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        try:
            return int(text)
        except (TypeError, ValueError):
            return float(text)


def _coerce_time(spec, value):
    s = str(value).strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if not m:
        raise SettingsError(
            "bad_value", "Expected a time as HH:MM (for example 05:30).", step="input",
        )
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise SettingsError(
            "bad_value", "Expected a time as HH:MM (for example 05:30).", step="input",
        )
    text = "%02d:%02d" % (hour, minute)
    lo, hi = spec.get("min"), spec.get("max")
    if (lo and text < lo) or (hi and text > hi):
        raise SettingsError(
            "out_of_range",
            "%s must be between %s and %s (got %s)."
            % (spec.get("label") or "This setting", lo, hi, text),
            step="settings",
        )
    return text


def _coerce(spec, value):
    typ = spec["type"]
    if typ == "pin":
        if isinstance(value, (list, tuple)) and len(value) == 2:
            value = {"lat": value[0], "lng": value[1]}
        try:
            parsed = rp.parse_coords(value)
        except rp.RiviaError as e:
            raise SettingsError("bad_value", str(e), step="input") from e
        if not parsed:
            raise SettingsError("bad_value", "A pin needs lat and lng.", step="input")
        return {"lat": parsed["lat"], "lng": parsed["lng"]}
    if typ == "time":
        return _coerce_time(spec, value)
    if typ == "int":
        if isinstance(value, bool) or (isinstance(value, str) and value.lower() in ("true", "false")):
            raise SettingsError("bad_value", "Expected a whole number.", step="input")
        try:
            if isinstance(value, str) and "." in value:
                f = float(value)
                if not f.is_integer():
                    raise ValueError
                value = int(f)
            else:
                value = int(value)
        except (TypeError, ValueError):
            raise SettingsError("bad_value", "Expected a whole number.", step="input") from None
    else:
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise SettingsError("bad_value", "Expected a number.", step="input") from None
        if typ == "float" and value.is_integer() and spec.get("keep_int"):
            value = int(value)
    lo, hi = spec["min"], spec["max"]
    if value < lo or value > hi:
        raise SettingsError(
            "out_of_range",
            "%s must be between %s and %s (got %s)." % (spec.get("label") or "This setting", lo, hi, value),
            step="settings",
        )
    return int(value) if typ == "int" else value


def _parse_key(key):
    """Return a descriptor for a catalog key, or raise SettingsError."""
    key = (key or "").strip()
    if key in SETTINGS:
        spec = dict(SETTINGS[key], key=key, family="scalar")
        return spec
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
        if a == b:
            raise SettingsError("bad_key", "A ride estimate needs two different places.", step="input")
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
    if key.startswith("place."):
        parts = key.split(".")
        if len(parts) != 3 or parts[1] not in RIDE_PLACES:
            raise SettingsError("bad_key", "Unknown setting: %s." % key, step="input")
        if parts[2] != "pin":
            raise SettingsError(
                "bad_key",
                "Place address and spoken name can only be changed from the web app.",
                step="input",
            )
        label = "%s pin" % (rp.PLACES[parts[1]]["say"],)
        return {
            "key": key, "family": "place", "place_id": parts[1], "field": "pin",
            "default": None, "min": None, "max": None, "type": "pin", "unit": "lat,lng",
            "label": label, "day": False, "ask": False, "group": "places",
            "doors": ("termux", "admin", "web"),
        }
    raise SettingsError("bad_key", "Unknown setting: %s." % key, step="input")


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


def _place_keys():
    return ["place.%s.pin" % pid for pid in RIDE_PLACES]


def _all_setting_keys():
    return list(SETTINGS) + _ride_keys() + _gym_keys() + _place_keys()


def _keys_for_group(group):
    group = (group or "schedule").strip().lower()
    keys = _all_setting_keys()
    if group in ("all", "*"):
        return keys
    return [k for k in keys if _parse_key(k)["group"] == group]


def ensure_settings_schema(con):
    con.executescript(SETTINGS_SCHEMA)


def _purge_overrides(con, today=None):
    today = _today_date(today)
    yesterday = (today - timedelta(days=1)).isoformat()
    con.execute("delete from overrides where day < ?", (yesterday,))


def _standing_row(con, key):
    return con.execute("select value, change_id, updated from settings where key=?", (key,)).fetchone()


def _override_row(con, day, key):
    return con.execute("select value, change_id, created from overrides where day=? and key=?",
                       (day, key)).fetchone()


def _resolved(con, key, day=None):
    """Return (value, source) for a key on a day: override, standing, or default."""
    spec = _parse_key(key)
    if day is not None:
        row = _override_row(con, _as_date(day).isoformat(), key)
        if row:
            return _load_value(row[0]), "day"
    row = _standing_row(con, key)
    if row:
        return _load_value(row[0]), "standing"
    return spec["default"], "default"


def _all_resolved(con, day=None, extra=None):
    out = {}
    for key in _all_setting_keys():
        val, src = _resolved(con, key, day=day)
        out[key] = (val, src)
    if extra:
        for key, val in extra.items():
            out[key] = (val, "proposed")
    return out


def _snapshot():
    return {
        "cfg": dict(rp.CFG),
        "legs": dict(rp.MEASURED_LEGS),
        "gym": {k: dict(v) for k, v in rp.GYM_HOURS.items()},
    }


def _restore(snap):
    rp.CFG.clear()
    rp.CFG.update(snap["cfg"])
    rp.MEASURED_LEGS.clear()
    rp.MEASURED_LEGS.update(snap["legs"])
    for gid, hours in snap["gym"].items():
        rp.GYM_HOURS[gid].clear()
        rp.GYM_HOURS[gid].update(hours)


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
    # place pins live in places.json, not in the planner module.


def _apply_resolved(resolved):
    # Start from code defaults, then overlay any non-default values.
    rp.CFG.clear()
    rp.CFG.update(_CFG_DEFAULT)
    rp.MEASURED_LEGS.clear()
    rp.MEASURED_LEGS.update(_LEGS_DEFAULT)
    for gid, hours in _GYM_DEFAULT.items():
        rp.GYM_HOURS[gid].clear()
        rp.GYM_HOURS[gid].update(hours)
    for key, (val, _src) in resolved.items():
        if val is not None:
            _apply_one(key, val)


@contextmanager
def planner_swap(day=None, extra=None, now=None, con=None):
    """Hold the settings lock, swap this day's values into the planner, restore after."""
    close = False
    if con is None:
        con = rp.db_open()
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


def _consistency_problems(resolved):
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
    problems.extend(_pin_spacing_problems(resolved))
    return problems


def geofence_radius_m(resolved=None, con=None):
    if resolved is not None and "geofence_radius_meters" in resolved:
        val = resolved["geofence_radius_meters"][0]
        return int(val if val is not None else SETTINGS["geofence_radius_meters"]["default"])
    if con is not None:
        val, _src = _resolved(con, "geofence_radius_meters")
        return int(val if val is not None else SETTINGS["geofence_radius_meters"]["default"])
    return int(SETTINGS["geofence_radius_meters"]["default"])


def _pin_spacing_problems(resolved):
    """Pins must stay at least 2× the geofence radius apart so gym and work do not overlap."""
    radius = geofence_radius_m(resolved)
    min_m = 2 * radius
    pins = {}
    for pid in RIDE_PLACES:
        val = (resolved.get("place.%s.pin" % pid) or (None, None))[0]
        if val and isinstance(val, dict) and "lat" in val and "lng" in val:
            pins[pid] = val
    problems = []
    ids = list(pins)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            d = rp.meters_between(pins[a], pins[b])
            if d < min_m:
                problems.append(
                    "The %s and %s pins are %.1f m apart; they must be at least %d m "
                    "apart (2× the %d m geofence radius)." % (a, b, d, min_m, radius)
                )
    return problems


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
    """Return new problems the change would add, or None if the Saturday sample was skipped only."""
    added = []
    for name, shift, prev, gym_id in _sample_workdays():
        with planner_swap(day=day, con=con):
            before = _sample_problems(shift, prev, gym_id)
        with planner_swap(day=day, extra=extra, con=con):
            after = _sample_problems(shift, prev, gym_id)
        if before is None or after is None:
            continue  # Saturday (or any sample) skipped while ride estimates are missing
        new = [p for p in after if p not in before]
        if new:
            added.append({"sample": name, "problems": new})
    return added


def _new_id():
    return "chg_" + secrets.token_hex(8)


def _log_change(con, **row):
    con.execute(
        "insert into changes(change_id, created, door, kind, scope, day, key, old, new, preview, status, decided, reason, request_id) "
        "values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (row["change_id"], row["created"], row.get("door"), row.get("kind") or "setting",
         row.get("scope"), row.get("day"), row.get("key"),
         row.get("old"), row.get("new"), row.get("preview"),
         row.get("status"), row.get("decided"), row.get("reason"), row.get("request_id")),
    )


def _row_to_result(row, *, duplicate=False, in_use=None):
    data = {
        "change_id": row[0], "key": row[6], "scope": row[4], "day": row[5],
        "old": _load_value(row[7]) if row[7] is not None else None,
        "value": _load_value(row[8]) if row[8] is not None else None,
        "status": row[10], "duplicate": duplicate,
        "source": None,
    }
    if row[11]:
        try:
            extra = json.loads(row[9]) if row[9] else {}
        except ValueError:
            extra = {}
        data.update({k: extra[k] for k in ("source", "in_use", "in_use_source") if k in extra})
    if in_use is not None:
        data["in_use"] = in_use
    return data


def _asked_of(door):
    d = (door or "").strip().lower()
    if d in ("app", "admin"):
        return d
    return "system"


def _row_created_s(row):
    try:
        return rp.to_epoch(row[1], "created")
    except Exception:
        return 0


def _expires_iso(row):
    return rp.iso(_row_created_s(row) + PENDING_EXPIRE_S)


def _is_expired(row, now_s):
    return _row_created_s(row) + PENDING_EXPIRE_S <= now_s


def expire_pending(con, now_s):
    """Mark pending rows older than 24 hours as expired. Returns how many flipped."""
    rows = list(con.execute("select * from changes where status='pending'"))
    n = 0
    decided = rp.iso(now_s)
    for row in rows:
        if _is_expired(row, now_s):
            con.execute("update changes set status='expired', decided=? where change_id=?",
                        (decided, row[0]))
            n += 1
    return n


def _find_pending(con, door, key, value_s, scope, day, request_id, now_s):
    expire_pending(con, now_s)
    if request_id:
        row = con.execute(
            "select * from changes where request_id=? and status='pending' "
            "order by created desc, rowid desc limit 1",
            (request_id,),
        ).fetchone()
        if row:
            return row
    day_s = day or ""
    row = con.execute(
        "select * from changes where status='pending' and door=? and key=? and new=? "
        "and ifnull(scope,'')=? and ifnull(day,'')=? "
        "order by created desc, rowid desc limit 1",
        (door, key, value_s, scope or "", day_s),
    ).fetchone()
    return row


def _find_duplicate(con, door, key, value_s, scope, day, request_id, now_s):
    if request_id:
        row = con.execute(
            "select * from changes where request_id=? order by created desc, rowid desc limit 1",
            (request_id,),
        ).fetchone()
        if row:
            return row
    since = rp.iso(now_s - DUPLICATE_S)
    day_s = day or ""
    row = con.execute(
        "select * from changes where door=? and key=? and new=? and ifnull(scope,'')=? "
        "and ifnull(day,'')=? and created>=? order by created desc, rowid desc limit 1",
        (door, key, value_s, scope or "", day_s, since),
    ).fetchone()
    return row


def _pending_result(con, row, *, duplicate=False):
    data = _setting_result(
        con, row[6], row[5], row[4], row[0],
        _load_value(row[7]) if row[7] is not None else None,
        _load_value(row[8]) if row[8] is not None else None,
        duplicate=duplicate, status=row[10],
    )
    data["asked"] = _asked_of(row[2])
    data["expires"] = _expires_iso(row)
    data["reason"] = row[12]
    return data


def _change_public(row):
    return {
        "change_id": row[0],
        "created": row[1],
        "door": row[2],
        "asked": _asked_of(row[2]),
        "kind": row[3],
        "scope": row[4],
        "day": row[5],
        "key": row[6],
        "old": _load_value(row[7]) if row[7] is not None else None,
        "value": _load_value(row[8]) if row[8] is not None else None,
        "status": row[10],
        "decided": row[11],
        "reason": row[12],
        "request_id": row[13],
        "expires": _expires_iso(row) if row[10] in ("pending", "expired") else None,
    }


def _ride_in_use(a, b):
    mins, src = rp.Legs(rides=None).info(a, b)
    yours = src == "your estimate"
    return {"in_use": bool(yours and mins is not None), "in_use_source": src, "minutes": mins}


def _source_after(con, key, day, scope):
    val, src = _resolved(con, key, day=day if scope == "day" else None)
    # After a standing apply, ignore a day override for the 'now comes from' of the standing value itself;
    # the result describes the value that was just written.
    if scope == "standing":
        row = _standing_row(con, key)
        return ("standing" if row else "default")
    return src


def _setting_result(con, key, day, scope, change_id, old, value, duplicate=False, status="applied"):
    source = _source_after(con, key, day, scope)
    data = {
        "change_id": change_id, "key": key, "value": value, "old": old,
        "scope": scope, "day": day, "status": status, "source": source,
        "duplicate": duplicate,
    }
    spec = _parse_key(key)
    if spec["family"] == "ride":
        with planner_swap(day=day, con=con):
            info = _ride_in_use(spec["a"], spec["b"])
        data["in_use"] = info["in_use"]
        data["in_use_source"] = info["in_use_source"]
    return data


def change_setting_sync(key, value, scope, day=None, reason=None, request_id=None,
                        door="app", now=None, con=None):
    """Run the four checks and, if they pass, apply a day change or a standing
    change from a door that may apply one. App-door standing changes are queued
    as pending (G7) until approved."""
    close = False
    if con is None:
        con = rp.db_open()
        ensure_store(con)
        close = True
    try:
        return _change_setting(con, key, value, scope, day=day, reason=reason,
                               request_id=request_id, door=door, now=now)
    finally:
        if close:
            con.close()


def _change_setting(con, key, value, scope, day=None, reason=None, request_id=None,
                    door="app", now=None):
    now = now or datetime.now(gcal.TZ)
    now_s = int(now.timestamp()) if hasattr(now, "timestamp") else rp.now_s()
    today = _today_date(now)
    scope = (scope or "").strip().lower()
    door = (door or "app").strip().lower()
    if scope not in ("day", "standing"):
        raise SettingsError("bad_scope", "scope must be day or standing.", step="input")
    spec = _parse_key(key)
    key = spec["key"]
    coerced = _coerce(spec, value)
    value_s = _store_value(coerced)
    day_s = None
    if scope == "day":
        if not spec["day"]:
            raise SettingsError("bad_scope", "%s can only be a standing value, not a day change." % key, step="input")
        if not day:
            raise SettingsError("bad_day", "A day-scope change needs a day (today, tomorrow, or YYYY-MM-DD).", step="input")
        if isinstance(day, str):
            try:
                day_d, _asked = gcal.resolve_day(day, now=now)
            except Exception as e:
                raise SettingsError("bad_day", "Not a day: %s." % day, step="input") from e
        else:
            day_d = _as_date(day)
        if day_d < today or day_d > today + timedelta(days=DAY_WINDOW_DAYS):
            raise SettingsError(
                "bad_day",
                "A day change must be today through %d days ahead (got %s)." % (DAY_WINDOW_DAYS, day_d.isoformat()),
                step="input",
            )
        day_s = day_d.isoformat()
    elif day:
        # standing ignores a stray day
        day_s = None

    # Check 1 continued: this door may change this key at this scope.
    allowed = spec.get("doors")
    if allowed and door not in allowed:
        raise SettingsError(
            "bad_key",
            "This setting cannot be changed from the %s door." % door,
            step="input",
        )
    if door == "app" and not spec.get("ask", True) and not spec["day"]:
        raise SettingsError("bad_key", "This setting cannot be changed from the app door.", step="input")

    pending = _find_pending(con, door, key, value_s, scope, day_s, request_id, now_s)
    if pending:
        return _pending_result(con, pending, duplicate=True)

    dup = _find_duplicate(con, door, key, value_s, scope, day_s, request_id, now_s)
    if dup:
        data = _setting_result(con, key, dup[5], scope, dup[0], _load_value(dup[7]) if dup[7] else None,
                               _load_value(dup[8]) if dup[8] else None, duplicate=True, status=dup[10])
        return data

    old, _old_src = _resolved(con, key, day=day_s)
    extra = {key: coerced}

    # Check 2: consistent with the others.
    resolved = _all_resolved(con, day=day_s, extra=extra)
    problems = _consistency_problems(resolved)
    if problems:
        raise SettingsError("inconsistent", problems[0], step="settings", extra={"problems": problems})

    # Check 3: dry run on three sample days.
    added = _dry_run(con, day_s, extra)
    if added:
        flat = []
        for item in added:
            flat.extend(item["problems"])
        raise SettingsError(
            "breaks_plan",
            "That change would add problems on a sample day: %s" % " ".join(flat),
            step="settings", extra={"samples": added, "problems": flat},
        )

    # Check 4: log, then apply — or queue as pending for an app-door standing change (G7).
    change_id = _new_id()
    created = rp.iso(now_s)
    if door == "app" and scope == "standing":
        _log_change(con, change_id=change_id, created=created, door=door, kind="setting",
                    scope=scope, day=day_s, key=key, old=_store_value(old) if old is not None else None,
                    new=value_s, preview=json.dumps({"asked": _asked_of(door)}),
                    status="pending", decided=None, reason=reason, request_id=request_id)
        data = _setting_result(con, key, day_s, scope, change_id, old, coerced,
                               duplicate=False, status="pending")
        data["asked"] = _asked_of(door)
        data["expires"] = rp.iso(now_s + PENDING_EXPIRE_S)
        data["reason"] = reason
        return data
    if scope == "standing":
        con.execute(
            "insert or replace into settings(key, value, updated, change_id) values(?,?,?,?)",
            (key, value_s, created, change_id),
        )
    else:
        con.execute(
            "insert or replace into overrides(day, key, value, created, change_id) values(?,?,?,?,?)",
            (day_s, key, value_s, created, change_id),
        )
    data = _setting_result(con, key, day_s, scope, change_id, old, coerced, duplicate=False)
    preview = json.dumps({k: data[k] for k in ("source", "in_use", "in_use_source") if k in data})
    _log_change(con, change_id=change_id, created=created, door=door, kind="setting",
                scope=scope, day=day_s, key=key, old=_store_value(old) if old is not None else None,
                new=value_s, preview=preview, status="applied", decided=created,
                reason=reason, request_id=request_id)
    data["reason"] = reason
    if spec["family"] == "place" and spec.get("field") == "pin":
        data["places_file"] = rewrite_places_file(con)
    return data


def undo_change_sync(change_id=None, door="app", now=None, con=None):
    close = False
    if con is None:
        con = rp.db_open()
        ensure_store(con)
        close = True
    try:
        out = _undo_change(con, change_id=change_id, door=door, now=now)
        key = (out or {}).get("key") or ""
        if key.startswith("place.") and key.endswith(".pin"):
            out = dict(out)
            out["places_file"] = rewrite_places_file(con)
        return out
    finally:
        if close:
            con.close()


def _undo_change(con, change_id=None, door="app", now=None):
    now = now or datetime.now(gcal.TZ)
    now_s = int(now.timestamp()) if hasattr(now, "timestamp") else rp.now_s()
    door = (door or "app").strip().lower()
    cid = (change_id or "").strip() or None
    if cid:
        row = con.execute("select * from changes where change_id=?", (cid,)).fetchone()
        if not row:
            raise SettingsError("bad_change_id", "No change with id %s." % cid, step="input")
    else:
        since = rp.iso(now_s - UNDO_S)
        row = con.execute(
            "select * from changes where door=? and status='applied' and created>=? "
            "order by created desc, rowid desc limit 1",
            (door, since),
        ).fetchone()
        if not row:
            raise SettingsError("no_undo", "There is no change from this door in the last 24 hours to undo.", step="settings")
    cid, created, row_door, kind, scope, day_s, key, old_s, new_s, preview, status, decided, reason, request_id = row
    if status != "applied":
        raise SettingsError("bad_change", "Change %s cannot be undone (status is %s)." % (cid, status), step="settings")
    # Undoing a standing change through the app door is itself a standing change,
    # so it goes pending until approved.
    old = _load_value(old_s) if old_s is not None else None
    if scope == "standing":
        if door == "app":
            restore = old if old is not None else _parse_key(key).get("default")
            out = _change_setting(con, key, restore, "standing", reason="undo %s" % cid,
                                  door=door, now=now)
            out["undo_of"] = cid
            return out
        if old is None:
            con.execute("delete from settings where key=?", (key,))
        else:
            # Restore through the same checks.
            return _change_setting(con, key, old, "standing", reason="undo %s" % cid, door=door, now=now)
    else:
        if not day_s:
            raise SettingsError("bad_change", "Change %s has no day to restore." % cid, step="settings")
        # Remove this override first. If that already restores `old` (the default
        # or a standing value), we are done; otherwise write the previous override back.
        con.execute("delete from overrides where day=? and key=?", (day_s, key))
        current, _src = _resolved(con, key, day=day_s)
        if old is not None and current != old:
            out = _change_setting(con, key, old, "day", day=day_s, reason="undo %s" % cid, door=door, now=now)
            out["undo_of"] = cid
            con.execute("update changes set status='undone', decided=? where change_id=?", (rp.iso(now_s), cid))
            return out
        undo_id = _new_id()
        created_s = rp.iso(now_s)
        _log_change(con, change_id=undo_id, created=created_s, door=door, kind="undo",
                    scope=scope, day=day_s, key=key, old=new_s, new=_store_value(current) if current is not None else None,
                    preview=json.dumps({"undo_of": cid}), status="applied", decided=created_s,
                    reason="undo %s" % cid, request_id=None)
        con.execute("update changes set status='undone', decided=? where change_id=?", (created_s, cid))
        val, src = _resolved(con, key, day=day_s)
        return {
            "change_id": undo_id, "key": key, "value": val, "old": _load_value(new_s),
            "scope": scope, "day": day_s, "status": "applied", "source": src,
            "duplicate": False, "undo_of": cid,
        }
    # standing restore when old is None (delete standing row)
    undo_id = _new_id()
    created_s = rp.iso(now_s)
    _log_change(con, change_id=undo_id, created=created_s, door=door, kind="undo",
                scope="standing", day=None, key=key, old=new_s, new=None,
                preview=json.dumps({"undo_of": cid}), status="applied", decided=created_s,
                reason="undo %s" % cid, request_id=None)
    con.execute("update changes set status='undone', decided=? where change_id=?", (created_s, cid))
    val, src = _resolved(con, key, day=None)
    return {
        "change_id": undo_id, "key": key, "value": val, "old": _load_value(new_s),
        "scope": "standing", "day": None, "status": "applied", "source": src,
        "duplicate": False, "undo_of": cid,
    }


def get_settings_sync(group="schedule", day=None, now=None, con=None):
    close = False
    if con is None:
        con = rp.db_open()
        ensure_store(con)
        close = True
    try:
        today = _today_date(now)
        day_d = None
        if day:
            if isinstance(day, str) and not (len(day) >= 10 and day[4] == "-"):
                day_d, _asked = gcal.resolve_day(day, now=now)
            else:
                day_d = _as_date(day)
        else:
            day_d = today
        day_s = day_d.isoformat()
        items = []
        for key in _keys_for_group(group):
            spec = _parse_key(key)
            val, src = _resolved(con, key, day=day_s)
            item = {
                "key": key, "label": spec["label"], "value": val,
                "default": spec["default"], "unit": spec["unit"],
                "range": [spec["min"], spec["max"]], "source": src,
                "day_change": bool(spec["day"]),
            }
            if src == "day":
                item["override_day"] = day_s
            if spec["family"] == "ride" and val is not None:
                with planner_swap(day=day_s, con=con):
                    info = _ride_in_use(spec["a"], spec["b"])
                item["in_use"] = info["in_use"]
                item["in_use_source"] = info["in_use_source"]
            items.append(item)
        return {"day": day_s, "group": (group or "schedule"), "settings": items}
    finally:
        if close:
            con.close()


def _seed_weekend_estimates(con, now=None):
    """Idempotent G6 seed: ride_estimate.farm_pf=17 and hmc_pf=15 as standing values."""
    global _seeding
    seeded = []
    _seeding = True
    try:
        for key, val in SEED_ESTIMATES:
            if _standing_row(con, key) is not None:
                continue
            data = _change_setting(con, key, val, "standing", reason="auto-seed on first start",
                                   door="system", now=now)
            seeded.append(data)
            print("rivia: seeded %s=%s (standing)" % (key, val), file=sys.stderr)
    finally:
        _seeding = False
    return seeded


def ensure_store(con=None, now=None):
    """Create settings tables, drop stale day overrides, and seed 17/15 once."""
    close = False
    if con is None:
        con = rp.db_open()
        close = True
    try:
        ensure_settings_schema(con)
        _purge_overrides(con, today=now)
        if not _seeding:
            _seed_weekend_estimates(con, now=now)
        return con
    finally:
        if close:
            con.close()


def _settings_fail(err):
    data = dict(err.extra) if getattr(err, "extra", None) else {}
    return fail(err.code, str(err), getattr(err, "step", "settings"), data=data)


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
                        message=message, alerts=alerts)
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
                        message=message, alerts=alerts)
    except SettingsError as e:
        return _settings_fail(e)
    except Exception as e:  # noqa: BLE001
        return fail("internal", repr(e), "settings")


undo_change = tool(undo_change_tool)


def rewrite_places_file(con=None):
    """Rewrite ~/rivia/places.json from standing pin settings. The planner reads that file."""
    close = False
    if con is None:
        con = rp.db_open()
        ensure_store(con)
        close = True
    try:
        pins = {}
        for pid in RIDE_PLACES:
            val, _src = _resolved(con, "place.%s.pin" % pid)
            if val and isinstance(val, dict) and "lat" in val and "lng" in val:
                pins[pid] = {"lat": val["lat"], "lng": val["lng"]}
        path = rp.PLACES_FILE
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(pins, f, indent=1)
            f.write("\n")
        os.replace(tmp, path)
        return path
    finally:
        if close:
            con.close()


def places_sync(con=None):
    """Each known place: id, spoken name, lat, lng, and the current geofence radius."""
    close = False
    if con is None:
        con = rp.db_open()
        ensure_store(con)
        close = True
    try:
        radius = geofence_radius_m(con=con)
        items = []
        for pid in RIDE_PLACES:
            pin, _src = _resolved(con, "place.%s.pin" % pid)
            items.append({
                "id": pid,
                "spoken_name": rp.PLACES[pid]["say"],
                "lat": None if not pin else pin.get("lat"),
                "lng": None if not pin else pin.get("lng"),
                "radius_m": radius,
            })
        return {"places": items, "radius_m": radius}
    finally:
        if close:
            con.close()


def places_tool() -> dict:
    if not selftest_ok():
        return fail("selftest_failed",
                    "Planner selftest failed at startup. Scheduler actions are blocked until it passes.",
                    "planner")
    try:
        data = places_sync()
        return ok_reply("places", data)
    except SettingsError as e:
        return _settings_fail(e)
    except Exception as e:  # noqa: BLE001
        return fail("internal", repr(e), "places")


places = tool(places_tool)


def _arrival_day(t_s, now=None):
    dt = datetime.fromtimestamp(int(t_s), gcal.TZ)
    return dt.date()


def _save_last_arrival(con, rec):
    rp.set_meta(con, LAST_ARRIVAL_KEY, json.dumps(rec, separators=(",", ":")))


def _load_last_arrival(con=None):
    close = False
    if con is None:
        con = rp.db_open()
        close = True
    try:
        raw = rp.get_meta(con, LAST_ARRIVAL_KEY)
        if not raw:
            return None
        rec = json.loads(raw) if isinstance(raw, str) else raw
        return rec if isinstance(rec, dict) else None
    except (TypeError, ValueError):
        return None
    finally:
        if close:
            con.close()


def _maybe_backup_ride(ride_out):
    """Trigger the PR 2 Drive backup after a newly stored ride. Never raises."""
    if not ride_out or not ride_out.get("stored") or not ride_out.get("backup_due"):
        return None
    try:
        return gcal.run_drive_backup()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": {"code": "internal", "msg": type(e).__name__, "step": "drive"}}


def log_ride_sync(ride):
    """Log one ride (dict of planner log-ride fields) and back it up if it was new."""
    if not selftest_ok():
        return fail("selftest_failed",
                    "Planner selftest failed at startup. Scheduler actions are blocked until it passes.",
                    "planner")
    if not isinstance(ride, dict):
        return fail("bad_ride", "ride must be an object with ride_id, from, to, departed and arrived.", "input")
    try:
        out = rp.cmd_log_ride(ride)
        backup = _maybe_backup_ride(out)
        if backup is not None:
            out = dict(out, backup=backup)
        return ok_reply("ride", out, ref=ride.get("ride_id"))
    except rp.RiviaError as e:
        step = "input" if e.code in ("bad_ride_id", "bad_place", "bad_time") else "planner"
        return fail(e.code, str(e), step, data=dict(e.extra) if e.extra else {}, ref=ride.get("ride_id"))
    except Exception as e:  # noqa: BLE001
        return fail("internal", repr(e), "planner", ref=ride.get("ride_id"))


def arrival_sync(place=None, title=None, coords=None, time=None, ride=None, now=None, *, replay=False):
    """Log an optional ride, read the calendar one day each side, run the planner arrival.

    Stores the last arrival (place, title, time) so a day change that applies
    today can rerun it. replay=True skips logging and does not require a new
    write to last_arrival when the stored record is reused.
    """
    if not selftest_ok():
        return fail("selftest_failed",
                    "Planner selftest failed at startup. Scheduler actions are blocked until it passes.",
                    "planner")
    ref = None
    ride_out = None
    try:
        if ride and not replay:
            logged = log_ride_sync(ride)
            if not logged.get("ok"):
                return logged
            ride_out = logged.get("data")
        now_s = int(now.timestamp()) if (now is not None and hasattr(now, "timestamp")) else rp.now_s()
        if time is None or time == "":
            t_s = now_s
        else:
            t_s = rp.to_epoch(time, "time")
        title = title or ""
        is_test = str(title).strip().lower() == "test"
        events = []
        if not is_test:
            day = _arrival_day(t_s, now=now)
            events = gcal.events_for_plan(day, day, before=1, after=1, now=now)
        inp = {"time": t_s, "now": now_s, "title": title, "events": events}
        if place:
            inp["place"] = place
        if coords not in (None, ""):
            inp["coords"] = coords
        ensure_store(now=now)
        day = _arrival_day(t_s, now=now)
        with planner_swap(day=day, now=now):
            out = rp.cmd_arrival(inp)
        if not replay:
            con = rp.db_open()
            try:
                rec = {
                    "place": place or out.get("place"),
                    "title": title,
                    "time": rp.iso(t_s),
                }
                if coords not in (None, ""):
                    rec["coords"] = coords
                _save_last_arrival(con, rec)
            finally:
                con.close()
        data = {
            "place": out.get("place"),
            "arrived": out.get("arrived"),
        }
        if ride_out is not None:
            data["ride"] = ride_out
        return ok_reply(
            "arrival", data, ref=ref,
            message=out.get("message"),
            alerts=out.get("alerts"),
            fallback_text=out.get("fallback_text"),
        )
    except gcal.GcalError as e:
        return _gcal_fail(e, ref=ref)
    except rp.RiviaError as e:
        if e.code == "need_maps":
            return fail("need_estimate", str(e), "planner",
                        data={"pairs": e.extra.get("pairs") or []}, ref=ref)
        step = "input" if e.code in ("bad_place", "bad_time", "bad_coords") else "planner"
        return fail(e.code, str(e), step, data=dict(e.extra) if e.extra else {}, ref=ref)
    except Exception as e:  # noqa: BLE001
        return fail("internal", repr(e), "planner", ref=ref)


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


def arrival_tool(place: str | None = None, title: str | None = None,
                 coords: Union[str, dict, None] = None,
                 time: Union[str, int, float, None] = None,
                 ride: dict | None = None) -> dict:
    return arrival_sync(place=place, title=title, coords=coords, time=time, ride=ride)


def log_ride_tool(ride_id: str, from_place: str | None = None, to_place: str | None = None,
                  departed: Union[str, int, float, None] = None,
                  arrived: Union[str, int, float, None] = None,
                  distance_km: Union[int, float, None] = None,
                  avg_kmh: Union[int, float, None] = None,
                  polyline: str | None = None) -> dict:
    ride = {"ride_id": ride_id, "from": from_place, "to": to_place,
            "departed": departed, "arrived": arrived,
            "distance_km": distance_km, "avg_kmh": avg_kmh, "polyline": polyline}
    ride = {k: v for k, v in ride.items() if v is not None}
    return log_ride_sync(ride)


arrival = tool(arrival_tool)
log_ride = tool(log_ride_tool)


def cmd_pin(place, lat, lng):
    """Store a standing place.<id>.pin through the normal setting checks. Termux only."""
    place = str(place or "").strip()
    if place not in rp.PLACES:
        raise SettingsError(
            "bad_place",
            "Unknown place: %s. Use home, farm, gym, pf, or hmc." % (place or "(none)",),
            step="input",
        )
    data = change_setting_sync(
        "place.%s.pin" % place, {"lat": lat, "lng": lng}, "standing", door="termux",
    )
    return {"ok": True, **data}


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
    if action not in ACTIONS:
        raise SettingsError("bad_tool", "Tool %s has no action." % name, step="input")
    info = ACTIONS[action]
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
    "sched.places": {
        "fn": places,
        "lockable": (),
        "py_params": {},
        "doors": ("app",),
        "kind": "places",
        "time_bearing": False,
        "description": PLACES_DESCRIPTION,
    },
    "sched.arrival": {
        "fn": arrival,
        "lockable": ("place", "title"),
        "py_params": {"place": "place", "title": "title", "coords": "coords",
                      "time": "time", "ride": "ride"},
        "doors": ("app",),
        "kind": "arrival",
        "time_bearing": True,
        "description": ARRIVAL_DESCRIPTION,
    },
    "sched.log_ride": {
        "fn": log_ride,
        "lockable": (),
        "py_params": {"ride_id": "ride_id", "from_place": "from", "to_place": "to",
                      "departed": "departed", "arrived": "arrived",
                      "distance_km": "distance_km", "avg_kmh": "avg_kmh",
                      "polyline": "polyline"},
        "doors": ("app",),
        "kind": "ride",
        "time_bearing": False,
        "description": LOG_RIDE_DESCRIPTION,
    },
}



def _load_change(con, change_id):
    cid = (change_id or "").strip()
    if not cid:
        raise SettingsError("bad_change_id", "A change_id is required.", step="input")
    row = con.execute("select * from changes where change_id=?", (cid,)).fetchone()
    if not row:
        raise SettingsError("bad_change_id", "No change with id %s." % cid, step="input")
    return row


def _rerun_checks(con, key, coerced, scope, day_s):
    extra = {key: coerced}
    resolved = _all_resolved(con, day=day_s, extra=extra)
    problems = _consistency_problems(resolved)
    if problems:
        raise SettingsError("inconsistent", problems[0], step="settings", extra={"problems": problems})
    added = _dry_run(con, day_s, extra)
    if added:
        flat = []
        for item in added:
            flat.extend(item["problems"])
        raise SettingsError(
            "breaks_plan",
            "That change would add problems on a sample day: %s" % " ".join(flat),
            step="settings", extra={"samples": added, "problems": flat},
        )


def approve_change_sync(change_id, now=None, con=None):
    """Re-run every check and apply a pending change once. A repeat is refused."""
    close = False
    if con is None:
        con = rp.db_open()
        ensure_store(con)
        close = True
    try:
        now = now or datetime.now(gcal.TZ)
        now_s = int(now.timestamp()) if hasattr(now, "timestamp") else rp.now_s()
        expire_pending(con, now_s)
        # Re-read after expiry so a stale pending shows as expired.
        row = _load_change(con, change_id)
        cid, created, door, kind, scope, day_s, key, old_s, new_s, preview, status, decided, reason, request_id = row
        if status == "applied":
            raise SettingsError("already_approved", "Change %s has already been approved." % cid, step="settings")
        if status == "expired":
            raise SettingsError("expired", "Change %s has expired." % cid, step="settings")
        if status != "pending":
            raise SettingsError("not_pending", "Change %s cannot be approved (status is %s)." % (cid, status),
                                step="settings")
        coerced = _load_value(new_s)
        _rerun_checks(con, key, coerced, scope, day_s)
        value_s = new_s
        created_s = rp.iso(now_s)
        if scope == "standing":
            con.execute(
                "insert or replace into settings(key, value, updated, change_id) values(?,?,?,?)",
                (key, value_s, created_s, cid),
            )
        else:
            if not day_s:
                raise SettingsError("bad_change", "Change %s has no day to apply." % cid, step="settings")
            con.execute(
                "insert or replace into overrides(day, key, value, created, change_id) values(?,?,?,?,?)",
                (day_s, key, value_s, created_s, cid),
            )
        con.execute("update changes set status='applied', decided=? where change_id=?", (created_s, cid))
        data = _setting_result(con, key, day_s, scope, cid,
                               _load_value(old_s) if old_s is not None else None,
                               coerced, duplicate=False, status="applied")
        data["asked"] = _asked_of(door)
        data["reason"] = reason
        data["approved"] = True
        spec = _parse_key(key)
        if spec["family"] == "place" and spec.get("field") == "pin":
            data["places_file"] = rewrite_places_file(con)
        return data
    finally:
        if close:
            con.close()


def reject_change_sync(change_id, now=None, con=None):
    """Reject a pending change. Values stay as they are."""
    close = False
    if con is None:
        con = rp.db_open()
        ensure_store(con)
        close = True
    try:
        now = now or datetime.now(gcal.TZ)
        now_s = int(now.timestamp()) if hasattr(now, "timestamp") else rp.now_s()
        expire_pending(con, now_s)
        row = _load_change(con, change_id)
        cid, created, door, kind, scope, day_s, key, old_s, new_s, preview, status, decided, reason, request_id = row
        if status == "rejected":
            raise SettingsError("already_rejected", "Change %s has already been rejected." % cid, step="settings")
        if status == "expired":
            raise SettingsError("expired", "Change %s has expired." % cid, step="settings")
        if status != "pending":
            raise SettingsError("not_pending", "Change %s cannot be rejected (status is %s)." % (cid, status),
                                step="settings")
        decided_s = rp.iso(now_s)
        con.execute("update changes set status='rejected', decided=? where change_id=?", (decided_s, cid))
        return {
            "change_id": cid, "key": key, "scope": scope, "day": day_s,
            "old": _load_value(old_s) if old_s is not None else None,
            "value": _load_value(new_s) if new_s is not None else None,
            "status": "rejected", "asked": _asked_of(door), "reason": reason,
        }
    finally:
        if close:
            con.close()


def list_changes_sync(status=None, now=None, con=None, limit=100):
    """Pending and recent changes. Expired pending items are marked expired. Includes who asked."""
    close = False
    if con is None:
        con = rp.db_open()
        ensure_store(con)
        close = True
    try:
        now = now or datetime.now(gcal.TZ)
        now_s = int(now.timestamp()) if hasattr(now, "timestamp") else rp.now_s()
        expire_pending(con, now_s)
        try:
            limit_n = int(limit)
        except (TypeError, ValueError):
            limit_n = 100
        if limit_n < 1:
            limit_n = 1
        if limit_n > 500:
            limit_n = 500
        want = (status or "").strip().lower() or None
        if want:
            rows = list(con.execute(
                "select * from changes where status=? order by created desc, rowid desc limit ?",
                (want, limit_n),
            ))
        else:
            rows = list(con.execute(
                "select * from changes order by created desc, rowid desc limit ?",
                (limit_n,),
            ))
        return {"changes": [_change_public(r) for r in rows]}
    finally:
        if close:
            con.close()


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


def cmd_rides():
    """Ride count in scheduler.db. Termux only; no door or tool."""
    con = rp.db_open()
    try:
        return {"ok": True, "rides": rp.ride_count(con), "fresh": rp.is_fresh(con)}
    finally:
        con.close()


def cmd_restore_file(path):
    """Restore rides from a Drive copy (scheduler.db or scheduler.json).
    Merges into the live database and never drops a ride that is already there."""
    path = os.path.abspath(os.path.expanduser(str(path or "").strip()))
    if not path or not os.path.isfile(path):
        raise rp.RiviaError("bad_copy", "No file at %s." % (path or "(none)",))
    out = rp.cmd_restore({"path": path})
    out["ok"] = True
    out["path"] = path
    return out


def cmd_start_fresh():
    """Clear the planner's fresh-database guard (init-db). Termux only; no door or tool.

    On a new, empty database this unlocks log_ride and backup. On a database
    that is already in use it is a no-op and does not drop rides.
    """
    out = rp.cmd_init_db({})
    rides = cmd_rides()
    out["ok"] = True
    out["rides"] = rides["rides"]
    out["fresh"] = rides["fresh"]
    return out


def main(argv=None):
    import sys as _sys
    argv = list(_sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "settings"
    if cmd in ("-h", "--help"):
        print("rivia_sched.py settings [day] | settings-log | rides | restore <file> | start-fresh | pin <place> <lat> <lng> | call <tool> [json] | approve <id> | reject <id>")
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
        if not selftest_ok():
            print(json.dumps({"ok": False, "error": {"code": "selftest_failed", "msg": "planner selftest failed", "step": "planner"}}, indent=1))
            return 1
        if cmd == "pin":
            if len(argv) < 4:
                print("usage: rivia sched pin <place> <lat> <lng>", file=_sys.stderr)
                return 2
            out = cmd_pin(argv[1], argv[2], argv[3])
            print(json.dumps(out, indent=1))
            return 0
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
        if cmd == "settings-log":
            con = rp.db_open()
            try:
                ensure_store(con)
                rows = list(con.execute(
                    "select change_id, created, door, scope, day, key, old, new, status, reason from changes order by created, rowid"
                ))
                cols = ["change_id", "created", "door", "scope", "day", "key", "old", "new", "status", "reason"]
                print(json.dumps({"ok": True, "changes": [dict(zip(cols, r)) for r in rows]}, indent=1))
            finally:
                con.close()
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
        print("unknown command: %s (use settings, settings-log, rides, restore <file>, start-fresh, pin, call, approve, or reject)" % cmd, file=_sys.stderr)
        return 2
    except SettingsError as e:
        print(json.dumps({"ok": False, "error": {"code": e.code, "msg": str(e), "step": e.step}}, indent=1))
        return 1
    except rp.RiviaError as e:
        print(json.dumps({"ok": False, "error": {"code": e.code, "msg": str(e), "step": "restore"}}, indent=1))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
