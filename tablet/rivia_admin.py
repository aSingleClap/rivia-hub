# rivia_admin.py: fixed tools on the /admin MCP door. Not rows, not renameable,
# never registered on the bot or app doors. save_tool / delete_tool / test_tool
# come later. list_changes / approve_change / reject_change / undo_change are
# the approval queue.
import os
import signal
import threading
from datetime import datetime
from pathlib import Path

import rivia_gcal as gcal
import rivia_planner as rp
import rivia_sched as sched
import rivia_tools as tools

ADMIN_TOOLS = (
    "hub_status",
    "list_actions",
    "list_tools",
    "list_calls",
    "get_settings",
    "set_setting",
    "restart_server",
    "list_changes",
    "approve_change",
    "reject_change",
    "undo_change",
)

# Tests replace this so calling restart_server does not kill the test process.
# The live tool starts a short timer, then SIGTERM, and the watchdog in rivia.sh
# brings the server back (Yrden expects /health 200 in about 90 s).
_restart_impl = None

PS = Path("/sys/class/power_supply/battery")


def _battery():
    try:
        level = int((PS / "capacity").read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        status = (PS / "status").read_text().strip()
    except OSError:
        status = "unknown"
    return {"battery_percent": level, "status": status}


def _google_status():
    try:
        tokens = gcal.load_tokens()
    except Exception:
        tokens = None
    if not tokens:
        return {"signed_in": False, "scopes_complete": False}
    payload = gcal.scopes_payload(tokens=tokens)
    return {
        "signed_in": True,
        "scopes_complete": bool(payload.get("scopes_complete")),
        "needs_relogin": not bool(payload.get("scopes_complete")),
    }


def _backup_status():
    out = {"last_backup": None}
    try:
        con = rp.db_open()
        try:
            out["last_backup"] = rp.get_meta(con, "drive_uploaded_changes")
        finally:
            con.close()
    except Exception:
        pass
    return out


def _selftest_status():
    try:
        ok = sched.selftest_ok()
    except Exception:
        ok = False
    return {"selftest_ok": bool(ok)}


def hub_status() -> dict:
    """Tablet hub status: clock, selftest, Google sign-in, last backup, battery."""
    now = datetime.now(gcal.TZ)
    data = {
        "ok": True,
        "time": now.strftime("%Y-%m-%d %H:%M"),
        "tz": "America/New_York",
    }
    data.update(_selftest_status())
    data.update(_google_status())
    data.update(_backup_status())
    data.update(gcal.clock_status())
    batt = _battery()
    if batt:
        data["battery"] = batt
    return data


def list_actions() -> dict:
    """The scheduler action catalog (name, doors, description). Admin functions are not actions."""
    rows = []
    for name, info in sched.ACTIONS.items():
        rows.append({
            "action": name,
            "doors": list(info.get("doors") or ()),
            "kind": info.get("kind"),
            "lockable": list(info.get("lockable") or ()),
            "time_bearing": bool(info.get("time_bearing")),
            "description": info.get("description") or "",
        })
    return {"ok": True, "actions": rows}


def list_tools() -> dict:
    """Rows in the tool table (app-door tools). Admin tools are not rows."""
    return {"ok": True, "tools": tools.list_tool_rows()}


def list_calls(limit: int = 100) -> dict:
    """Recent tool calls from the calls table, newest first."""
    try:
        limit_n = int(limit)
    except (TypeError, ValueError):
        limit_n = 100
    if limit_n < 1:
        limit_n = 1
    if limit_n > 500:
        limit_n = 500
    return {"ok": True, "calls": tools.list_call_rows(limit=limit_n)}


def get_settings(group: str = "all", day: str | None = None) -> dict:
    """Current schedule settings, including pins. group defaults to all."""
    if not sched.selftest_ok():
        return sched.fail(
            "selftest_failed",
            "Planner selftest failed at startup. Scheduler actions are blocked until it passes.",
            "planner",
        )
    try:
        data = sched.get_settings_sync(group=group or "all", day=day)
        return sched.ok_reply("settings", data)
    except sched.SettingsError as e:
        return sched._settings_fail(e)
    except Exception as e:  # noqa: BLE001
        return sched.fail("internal", repr(e), "settings")


def set_setting(key: str, value: str | int | float | dict, scope: str = "standing",
                day: str | None = None, reason: str | None = None) -> dict:
    """Apply a setting now, including standing values and pins. This door applies at once."""
    if not sched.selftest_ok():
        return sched.fail(
            "selftest_failed",
            "Planner selftest failed at startup. Scheduler actions are blocked until it passes.",
            "planner",
        )
    try:
        data = sched.change_setting_sync(
            key, value, scope, day=day, reason=reason, door="admin",
        )
        kind = "change" if data.get("status") == "applied" else "pending"
        return sched.ok_reply(kind, data, ref=data.get("change_id"))
    except sched.SettingsError as e:
        return sched._settings_fail(e)
    except Exception as e:  # noqa: BLE001
        return sched.fail("internal", repr(e), "settings")


def _default_restart():
    def _go():
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Timer(0.4, _go).start()


def schedule_restart():
    fn = _restart_impl or _default_restart
    fn()


def restart_server() -> dict:
    """Exit so the watchdog in rivia.sh can start the server again (about a minute)."""
    schedule_restart()
    return {
        "ok": True,
        "restarting": True,
        "message": "Server is restarting. /health should return 200 again in about a minute.",
    }


def list_changes(status: str | None = None, limit: int = 100) -> dict:
    """Pending and recent setting changes, including who asked (app, admin, or system)."""
    if not sched.selftest_ok():
        return sched.fail(
            "selftest_failed",
            "Planner selftest failed at startup. Scheduler actions are blocked until it passes.",
            "planner",
        )
    try:
        data = sched.list_changes_sync(status=status, limit=limit)
        return sched.ok_reply("changes", data)
    except sched.SettingsError as e:
        return sched._settings_fail(e)
    except Exception as e:  # noqa: BLE001
        return sched.fail("internal", repr(e), "settings")


def approve_change(change_id: str) -> dict:
    """Re-run every check and apply a pending change once. A second approve is refused."""
    if not sched.selftest_ok():
        return sched.fail(
            "selftest_failed",
            "Planner selftest failed at startup. Scheduler actions are blocked until it passes.",
            "planner",
        )
    try:
        data = sched.approve_change_sync(change_id)
        return sched.ok_reply("change", data, ref=data.get("change_id"))
    except sched.SettingsError as e:
        return sched._settings_fail(e)
    except Exception as e:  # noqa: BLE001
        return sched.fail("internal", repr(e), "settings")


def reject_change(change_id: str) -> dict:
    """Reject a pending change. The plan is left unchanged."""
    if not sched.selftest_ok():
        return sched.fail(
            "selftest_failed",
            "Planner selftest failed at startup. Scheduler actions are blocked until it passes.",
            "planner",
        )
    try:
        data = sched.reject_change_sync(change_id)
        return sched.ok_reply("change", data, ref=data.get("change_id"))
    except sched.SettingsError as e:
        return sched._settings_fail(e)
    except Exception as e:  # noqa: BLE001
        return sched.fail("internal", repr(e), "settings")


def undo_change(change_id: str | None = None) -> dict:
    """Undo an applied change. Standing restores from this door take effect at once."""
    if not sched.selftest_ok():
        return sched.fail(
            "selftest_failed",
            "Planner selftest failed at startup. Scheduler actions are blocked until it passes.",
            "planner",
        )
    try:
        data = sched.undo_change_sync(change_id=change_id, door="admin")
        return sched.ok_reply("undo", data, ref=data.get("change_id"))
    except sched.SettingsError as e:
        return sched._settings_fail(e)
    except Exception as e:  # noqa: BLE001
        return sched.fail("internal", repr(e), "settings")


def install_on(mcp):
    """Register the fixed admin tools on the admin FastMCP app."""
    mcp.tool()(hub_status)
    mcp.tool()(list_actions)
    mcp.tool()(list_tools)
    mcp.tool()(list_calls)
    mcp.tool()(get_settings)
    mcp.tool()(set_setting)
    mcp.tool()(restart_server)
    mcp.tool()(list_changes)
    mcp.tool()(approve_change)
    mcp.tool()(reject_change)
    mcp.tool()(undo_change)
