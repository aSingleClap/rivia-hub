# rivia_tools.py (core): the tool table and the call log, in core.db, and the action registry.
# Rows are built with Tool.from_tool and registered on a door. Each part of Rivia (places,
# scheduling) registers its actions and seed rows here when it loads; core never imports them,
# so a part that fails to load only loses its own tools. Action ids such as "sched.arrival" are
# kept from before the core split because the stored rows name them.
import functools
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from fastmcp.tools import Tool
from fastmcp.tools.tool_transform import ArgTransform

import rivia_store as store

CALL_LIMIT = 500
TZ = ZoneInfo("America/New_York")

FAIL_RULE = ("If a tool returns ok=false, report the failure as ok=false and stop that task. "
             "Never substitute data from another device, an older answer or a guess.")
PENDING_LINE = "A pending result has not happened yet."
TIME_LINE = ("Say times exactly as their spoken value, never work one out, "
             "say every must_say fact, offer decisions_for_jordan and never pick one.")

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
MIGRATE = [("tools", ""), ("calls", "order by rowid")]

# action id -> {fn, lockable, py_params, doors, kind, time_bearing, description, pending_line}
ACTIONS = {}
# seed rows by name; a part registers them with its actions ("insert or ignore" on every open)
SEED = {}
# seeded descriptions a later version rewrote: refreshed only if the row still holds one exactly
OLD_DESCRIPTIONS = {}


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


def register(actions, seed_rows=(), old_descriptions=None):
    """Add a part's actions and seed rows (called by rivia_places / rivia_sched when they load)."""
    ACTIONS.update(actions)
    for row in seed_rows:
        SEED[row["name"]] = row
    for name, texts in (old_descriptions or {}).items():
        OLD_DESCRIPTIONS[name] = tuple(texts)


def _now_iso():
    return datetime.now(TZ).replace(microsecond=0).isoformat()


_installed: set[str] = set()


def append_fixed_lines(description, action):
    parts = [description.strip(), FAIL_RULE]
    info = ACTIONS.get(action) or {}
    if info.get("time_bearing"):
        parts.append(TIME_LINE)
    if info.get("pending_line"):
        line = PENDING_LINE
        blob = " ".join(parts)
        if line.lower() not in blob.lower():
            parts.append(line)
    return " ".join(parts)


def ensure_schema(con):
    con.executescript(SCHEMA)


def seed_tools(con):
    now = _now_iso()
    for row in SEED.values():
        con.execute(
            "insert or ignore into tools(name, door, action, preset, description, model, enabled, updated) "
            "values(?,?,?,?,?,?,?,?)",
            (row["name"], row["door"], row["action"], json.dumps(row["preset"], separators=(",", ":")),
             row["description"], 1 if row["model"] else 0, 1 if row["enabled"] else 0, now),
        )
        for old_text in OLD_DESCRIPTIONS.get(row["name"], ()):
            con.execute(
                "update tools set description=?, updated=? where door=? and name=? and description=?",
                (row["description"], now, row["door"], row["name"], old_text),
            )


def open_db():
    """core.db (tools + calls), migrated once from scheduler.db, with the seed rows filled in."""
    con = store.connect("core", SCHEMA, migrate=MIGRATE)
    seed_tools(con)
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
            (_now_iso(), door, tool, action, 1 if ok else 0, code, int(ms)),
        )
        con.execute(
            "delete from calls where rowid not in (select rowid from calls order by rowid desc limit ?)",
            (CALL_LIMIT,),
        )
    finally:
        con.close()


def log_fixed(fn, door, name=None):
    """Wrap a fixed tool (not a row) so its calls go in the call log like row tools: ok, error code and ms.
    Emotion spec Step 4.1 (Jordan, Nov 22: "They can be logged especially for response time and failures").
    The action is "fixed.<name>". A reply with ok=false logs its error code; an error that isn't a
    {"code": ...} dict logs "unreadable"; an exception logs "internal" and is raised as before. Logging
    never changes the reply and never raises."""
    name = name or fn.__name__

    @functools.wraps(fn)
    def run(*args, **kwargs):
        t0 = time.perf_counter()
        ok, code = False, "internal"
        try:
            result = fn(*args, **kwargs)
            if isinstance(result, dict) and "ok" in result:
                ok = bool(result.get("ok"))
                err = result.get("error")
                code = None if ok else ((err.get("code") if isinstance(err, dict) else None) or "unreadable")
            else:
                ok, code = True, None
            return result
        finally:
            try:
                log_call(door, name, "fixed." + name, ok, code, int((time.perf_counter() - t0) * 1000))
            except Exception:  # noqa: BLE001
                pass

    return run


def _transforms(action, preset):
    info = ACTIONS[action]
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
    if action not in ACTIONS:
        raise KeyError("unknown action: %s" % action)
    info = ACTIONS[action]
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
    for row in load_rows(door=door, enabled_only=True):
        if row["action"] not in ACTIONS:
            continue  # its part did not load (for example scheduling); the rest still installs
        t = _wrap_logged(tool_from_row(row), row["door"], row["name"], row["action"])
        mcp.add_tool(t)
    _installed.add(door)


def reset_install():
    """Allow tests to install onto a fresh FastMCP app in the same process."""
    _installed.clear()
