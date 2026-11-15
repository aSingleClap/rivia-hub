# rivia_tools.py: the scheduler tool table and call log in scheduler.db.
# Rows are built with Tool.from_tool and registered on a door. Seeded on the app door as actions ship (G5). plan_week has no seeded row; there is no tool editing yet. Settings rows ship with 7a-2.
import json
import time

from fastmcp.tools import Tool
from fastmcp.tools.tool_transform import ArgTransform

import rivia_planner as rp
import rivia_sched as sched

CALL_LIMIT = 500

SCHEMA = """
create table if not exists tools(
    name text not null, door text not null, action text not null,
    preset text not null default '{}', description text not null,
    model integer not null default 1, enabled integer not null default 1,
    updated text not null, primary key (door, name));
create table if not exists calls(
    ts text not null, door text not null, tool text not null,
    action text not null, ok integer not null, code text, ms integer not null);
"""

RIDE_TIME_SEED_DESCRIPTION = sched.RIDE_TIME_DESCRIPTION

# Seeded on first start, only for actions that exist now (G5).
SEED = (
    {"name": "ride_time", "door": "app", "action": "sched.ride_time",
     "preset": {}, "description": RIDE_TIME_SEED_DESCRIPTION, "model": 1, "enabled": 1},
    {"name": "plan_day", "door": "app", "action": "sched.plan_day",
     "preset": {}, "description": sched.PLAN_DAY_DESCRIPTION, "model": 1, "enabled": 1},
    {"name": "get_schedule_settings", "door": "app", "action": "sched.get_settings",
     "preset": {"group": "schedule"}, "description": sched.GET_SETTINGS_DESCRIPTION, "model": 1, "enabled": 1},
    {"name": "change_schedule_setting", "door": "app", "action": "sched.change_setting",
     "preset": {}, "description": sched.CHANGE_SETTING_DESCRIPTION, "model": 1, "enabled": 1},
    {"name": "undo_schedule_change", "door": "app", "action": "sched.undo_change",
     "preset": {}, "description": sched.UNDO_CHANGE_DESCRIPTION, "model": 1, "enabled": 1},
    {"name": "app_places", "door": "app", "action": "sched.places",
     "preset": {}, "description": sched.PLACES_DESCRIPTION, "model": 0, "enabled": 1},
    {"name": "app_arrival", "door": "app", "action": "sched.arrival",
     "preset": {}, "description": sched.ARRIVAL_DESCRIPTION, "model": 0, "enabled": 1},
    {"name": "app_log_ride", "door": "app", "action": "sched.log_ride",
     "preset": {}, "description": sched.LOG_RIDE_DESCRIPTION, "model": 0, "enabled": 1},
    {"name": "write_day", "door": "app", "action": "sched.write_day",
     "preset": {}, "description": sched.WRITE_DAY_DESCRIPTION, "model": 1, "enabled": 1},
)

_installed: set[str] = set()


def append_fixed_lines(description, action):
    parts = [description.strip(), sched.FAIL_RULE]
    info = sched.ACTIONS.get(action) or {}
    if info.get("time_bearing"):
        parts.append("Say times exactly as their spoken value, never work one out, "
                     "say every must_say fact, offer decisions_for_jordan and never pick one.")
    if action in ("sched.change_setting", "sched.write_day"):
        line = sched.PENDING_LINE
        blob = " ".join(parts)
        if line.lower() not in blob.lower():
            parts.append(line)
    return " ".join(parts)


def ensure_schema(con):
    con.executescript(SCHEMA)


def seed_tools(con):
    now = rp.iso(rp.now_s())
    for row in SEED:
        con.execute(
            "insert or ignore into tools(name, door, action, preset, description, model, enabled, updated) "
            "values(?,?,?,?,?,?,?,?)",
            (row["name"], row["door"], row["action"], json.dumps(row["preset"], separators=(",", ":")),
             row["description"], 1 if row["model"] else 0, 1 if row["enabled"] else 0, now),
        )


def open_db():
    con = rp.db_open()
    ensure_schema(con)
    seed_tools(con)
    sched.ensure_store(con)
    return con


def list_tool_rows():
    con = open_db()
    try:
        cols = ["name", "door", "action", "preset", "description", "model", "enabled", "updated"]
        rows = list(con.execute(
            "select name, door, action, preset, description, model, enabled, updated "
            "from tools order by door, name"
        ))
        out = []
        for r in rows:
            item = dict(zip(cols, r))
            preset = item.get("preset")
            if isinstance(preset, str):
                try:
                    item["preset"] = json.loads(preset or "{}")
                except ValueError:
                    item["preset"] = {}
            item["enabled"] = bool(item["enabled"])
            item["model"] = bool(item["model"])
            out.append(item)
        return out
    finally:
        con.close()


def list_call_rows(limit=100):
    con = open_db()
    try:
        cols = ["ts", "door", "tool", "action", "ok", "code", "ms"]
        rows = list(con.execute(
            "select ts, door, tool, action, ok, code, ms from calls "
            "order by ts desc, rowid desc limit ?",
            (int(limit),),
        ))
        out = []
        for r in rows:
            item = dict(zip(cols, r))
            item["ok"] = bool(item["ok"])
            out.append(item)
        return out
    finally:
        con.close()


def log_call(door, tool, action, ok, code, ms):
    con = open_db()
    try:
        con.execute(
            "insert into calls(ts, door, tool, action, ok, code, ms) values(?,?,?,?,?,?,?)",
            (rp.iso(rp.now_s()), door, tool, action, 1 if ok else 0, code, int(ms)),
        )
        con.execute(
            "delete from calls where rowid not in (select rowid from calls order by rowid desc limit ?)",
            (CALL_LIMIT,),
        )
    finally:
        con.close()


def _transforms(action, preset):
    info = sched.ACTIONS[action]
    out = {}
    for py, pub in info["py_params"].items():
        if pub in (preset or {}):
            out[py] = ArgTransform(name=pub, hide=True, default=preset[pub])
        else:
            out[py] = ArgTransform(name=pub)
    # Ignore unknown preset keys here; editing validation comes later.
    return out or None


def tool_from_row(row):
    action = row["action"]
    if action not in sched.ACTIONS:
        raise KeyError("unknown action: %s" % action)
    info = sched.ACTIONS[action]
    preset = row["preset"]
    if isinstance(preset, str):
        preset = json.loads(preset or "{}")
    desc = append_fixed_lines(row["description"], action)
    return Tool.from_tool(
        info["fn"],
        name=row["name"],
        description=desc,
        transform_args=_transforms(action, preset),
    )


def _wrap_logged(tool, door, name, action):
    orig = tool.run

    async def run(arguments):
        t0 = time.perf_counter()
        ok, code = False, "internal"
        try:
            result = await orig(arguments)
            data = result.structured_content or {}
            if isinstance(data, dict):
                ok = bool(data.get("ok"))
                err = data.get("error") or {}
                code = None if ok else err.get("code")
            else:
                ok, code = True, None
            return result
        except Exception:
            ok, code = False, "internal"
            raise
        finally:
            try:
                log_call(door, name, action, ok, code, int((time.perf_counter() - t0) * 1000))
            except Exception:  # noqa: BLE001
                pass

    tool.run = run
    return tool


def load_rows(door=None, enabled_only=True):
    con = open_db()
    try:
        q = "select name, door, action, preset, description, model, enabled, updated from tools"
        args = []
        where = []
        if door is not None:
            where.append("door=?")
            args.append(door)
        if enabled_only:
            where.append("enabled=1")
        if where:
            q += " where " + " and ".join(where)
        q += " order by name"
        rows = []
        for r in con.execute(q, args):
            rows.append({"name": r[0], "door": r[1], "action": r[2], "preset": r[3],
                         "description": r[4], "model": bool(r[5]), "enabled": bool(r[6]),
                         "updated": r[7]})
        return rows
    finally:
        con.close()


def install_on(mcp, door):
    """Build row tools for this door and add them to the FastMCP server. Idempotent per door."""
    if door in _installed:
        return
    # Selftest is checked on every action; running it here fills the cache and meta.
    sched.selftest_ok()
    for row in load_rows(door=door, enabled_only=True):
        if row["action"] not in sched.ACTIONS:
            continue
        t = _wrap_logged(tool_from_row(row), row["door"], row["name"], row["action"])
        mcp.add_tool(t)
    _installed.add(door)


def reset_install():
    """Allow tests to install onto a fresh FastMCP app in the same process."""
    _installed.clear()
