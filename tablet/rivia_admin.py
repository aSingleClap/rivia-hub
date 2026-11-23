# rivia_admin.py (core): fixed tools on the /admin MCP door. Not rows, not renameable.
# hub_status, list_tools and list_calls are read-only and are also put on the app door
# (install_read_only_on); set_setting, restart_server and the approval tools stay admin-only.
# Scheduling is imported only inside the tools that need it, so the hub keeps working (and can
# still approve place changes) when scheduling failed to load.
import os
import signal
import sqlite3
import threading
from datetime import datetime

import rivia_approvals as approvals
import rivia_battery as battery
import rivia_google as google
import rivia_settings as settings
import rivia_store as store
import rivia_tools as tools
from rivia_tools import fail, ok_reply

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
READ_ONLY_APP_TOOLS = ("hub_status", "list_tools", "list_calls")

# Tests replace this so calling restart_server does not kill the test process.
# The live tool starts a short timer, then SIGTERM, and the watchdog in rivia.sh
# brings the server back (Yrden expects /health 200 in about 90 s).
_restart_impl = None


def _sched():
    """The scheduling module, or None if it is not running."""
    try:
        import rivia_sched
        return rivia_sched
    except Exception:  # noqa: BLE001
        return None


def _battery():
    return battery.read_battery()


def _google_status():
    try:
        tokens = google.load_tokens()
    except Exception:
        tokens = None
    if not tokens:
        return {"signed_in": False, "scopes_complete": False}
    payload = google.scopes_payload(tokens=tokens)
    return {
        "signed_in": True,
        "scopes_complete": bool(payload.get("scopes_complete")),
        "needs_relogin": not bool(payload.get("scopes_complete")),
    }


def _scheduler_meta(key):
    path = store.scheduler_db_path()
    if not os.path.exists(path):
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        try:
            row = con.execute("select value from meta where key=?", (key,)).fetchone()
            return row[0] if row else None
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _backup_status():
    return {"last_backup": _scheduler_meta("drive_uploaded_at") or _scheduler_meta("drive_uploaded_changes")}


def _selftest_status():
    sched = _sched()
    try:
        ok = bool(sched and sched.selftest_ok())
    except Exception:
        ok = False
    return {"selftest_ok": ok, "scheduling": "running" if sched else "unavailable"}


def hub_status() -> dict:
    """Tablet hub status: clock, selftest, Google sign-in, last backup, battery."""
    now = datetime.now(google.TZ)
    data = {
        "ok": True,
        "time": now.strftime("%Y-%m-%d %H:%M"),
        "tz": "America/New_York",
    }
    data.update(_selftest_status())
    data.update(_google_status())
    data.update(_backup_status())
    data.update(google.clock_status())
    batt = _battery()
    if batt:
        data["battery"] = batt
    return data


def list_actions() -> dict:
    """The scheduler action catalog (name, doors, description). Admin functions are not actions."""
    rows = []
    for name, info in tools.ACTIONS.items():
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


def _settings_fail(err):
    data = dict(err.extra) if getattr(err, "extra", None) else {}
    return fail(err.code, str(err), getattr(err, "step", "settings"), data=data)


def get_settings(group: str = "all", day: str | None = None) -> dict:
    """Current settings, including pins. group defaults to all."""
    try:
        return ok_reply("settings", settings.get_settings(group=group or "all", day=day))
    except approvals.SettingsError as e:
        return _settings_fail(e)
    except Exception as e:  # noqa: BLE001
        return fail("internal", repr(e), "settings")


def set_setting(key: str, value: str | int | float | dict, scope: str = "standing",
                day: str | None = None, reason: str | None = None) -> dict:
    """Apply a setting now, including standing values and pins. This door applies at once."""
    try:
        data = settings.change_setting(key, value, scope, day=day, reason=reason, door="admin")
        kind = "change" if data.get("status") == "applied" else "pending"
        return ok_reply(kind, data, ref=data.get("change_id"))
    except approvals.SettingsError as e:
        return _settings_fail(e)
    except Exception as e:  # noqa: BLE001
        return fail("internal", repr(e), "settings")


PLANNED_RESTART_LINE = "planned restart (restart_server from the hub)"


def note_planned_restart():
    """Write one line to watch.log before a restart from the hub (emotion spec Step 4.1, Jordan: "Yes").
    The watchdog then logs "server or tunnel down; restarting" like a crash; this line comes first, so the
    body-signal reader can leave that restart out. Same format as the watchdog's own lines. Never raises."""
    try:
        logs = os.path.join(store.rivia_dir(), "logs")
        os.makedirs(logs, exist_ok=True)
        with open(os.path.join(logs, "watch.log"), "a") as f:
            f.write("%s %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), PLANNED_RESTART_LINE))
    except OSError:
        pass


def _default_restart():
    note_planned_restart()

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
    try:
        return ok_reply("changes", approvals.list_changes_sync(status=status, limit=limit))
    except approvals.SettingsError as e:
        return _settings_fail(e)
    except Exception as e:  # noqa: BLE001
        return fail("internal", repr(e), "settings")


def approve_change(change_id: str) -> dict:
    """Re-run every check and apply a pending change once. A second approve is refused."""
    try:
        data = approvals.approve_change_sync(change_id)
        kind = "write" if data.get("key") == "write_day" or data.get("kind") == "write_day" else "change"
        return ok_reply(kind, data, ref=data.get("change_id") or data.get("day"))
    except approvals.SettingsError as e:
        return _settings_fail(e)
    except google.GcalError as e:
        return fail(e.code, str(e), e.step or "calendar")
    except Exception as e:  # noqa: BLE001
        return fail("internal", repr(e), "settings")


def reject_change(change_id: str) -> dict:
    """Reject a pending change. The plan is left unchanged."""
    try:
        data = approvals.reject_change_sync(change_id)
        return ok_reply("change", data, ref=data.get("change_id"))
    except approvals.SettingsError as e:
        return _settings_fail(e)
    except Exception as e:  # noqa: BLE001
        return fail("internal", repr(e), "settings")


def undo_change(change_id: str | None = None) -> dict:
    """Undo an applied change. Standing restores from this door take effect at once."""
    try:
        data = approvals.undo_change_sync(change_id=change_id, door="admin")
        return ok_reply("undo", data, ref=data.get("change_id"))
    except approvals.SettingsError as e:
        return _settings_fail(e)
    except Exception as e:  # noqa: BLE001
        return fail("internal", repr(e), "settings")


def install_read_only_on(mcp, door="app"):
    """hub_status, list_tools and list_calls on another door (the app door), read only. Each call goes in
    the call log (emotion spec Step 4.1), so her response time and failures include them. The admin door's
    copies are the Server Hub's, not hers, and stay unlogged."""
    mcp.tool()(tools.log_fixed(hub_status, door))
    mcp.tool()(tools.log_fixed(list_tools, door))
    mcp.tool()(tools.log_fixed(list_calls, door))


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
