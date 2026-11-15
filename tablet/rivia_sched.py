# rivia_sched.py: scheduler actions. Each action is a standalone FastMCP tool
# object, never registered itself. rivia_tools.py turns tool-table rows into
# door tools with Tool.from_tool.
import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

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
        out = _plan_days([day], events, now=now)
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
        out = _plan_days(days, events, now=now)
        week = []
        for rec in out["days"]:
            week.append({
                "day": rec["day"], "status": rec["status"],
                "problems": rec["problems"], "message": rec.get("message"),
            })
        return ok_reply("plan", {"days": week, "written": False},
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
}
