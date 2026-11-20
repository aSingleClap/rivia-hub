# rivia_store.py: where each part of Rivia keeps its data, and the one-time move out of
# scheduler.db (the core split, Nov 2026).
#
#   core.db       core:        tools (the tool table), calls (the call log)
#   places.db     places:      rides, place settings (pins, zone sizes, names), arrival memory
#   approvals.db  approvals:   changes (every setting change, pending items, write_day queue)
#   scheduler.db  scheduling:  schedule settings, day overrides, calendar/backup bookkeeping
#
# Before the split everything lived in scheduler.db. The first time a new DB is opened it
# copies its tables from scheduler.db (inside one transaction, once, recorded in its meta
# table as "migrated_from_scheduler"). The copy never deletes or changes scheduler.db; the
# old rows stay there untouched, so the pre-split code still finds its data on a rollback.
import json
import os
import sqlite3
import time

RIVIA_DIR = os.path.expanduser(os.environ.get("RIVIA_DIR", "~/rivia"))
MIGRATED_KEY = "migrated_from_scheduler"
META_SCHEMA = "create table if not exists meta(key text primary key, value text);"


def rivia_dir():
    """The data folder. Tests (and the planner selftest) point RIVIA_DIR at a throwaway folder."""
    return RIVIA_DIR


def db_path(name):
    return os.path.join(rivia_dir(), name + ".db")


def scheduler_db_path():
    return db_path("scheduler")


def connect(name, schema, migrate=None):
    """Open <name>.db (created if needed) with its schema and a meta table. migrate is a list of
    (table, where_sql) pairs to copy once from scheduler.db; None means this DB never migrates."""
    path = db_path(name)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path, timeout=10, isolation_level=None)
    con.executescript(META_SCHEMA + schema)
    if migrate is not None:
        migrate_once(con, name, migrate)
    return con


def get_meta(con, key, default=None):
    row = con.execute("select value from meta where key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(con, key, value):
    con.execute("insert or replace into meta values(?,?)", (key, str(value)))


def _columns(con, schema, table):
    return [r[1] for r in con.execute("pragma %s.table_info(%s)" % (schema, table))]


def migrate_once(con, name, specs):
    """Copy specs' rows from scheduler.db into this DB, once. Returns the counts (or None if it
    already ran). All-or-nothing, and safe with two processes opening the DB at once."""
    if get_meta(con, MIGRATED_KEY) is not None:
        return None
    src = scheduler_db_path()
    attached = os.path.exists(src) and os.path.abspath(src) != os.path.abspath(db_path(name))
    if attached:
        con.execute("attach database ? as old", (src,))  # read from only, never written
    counts = {}
    try:
        con.execute("begin immediate")
        try:
            if get_meta(con, MIGRATED_KEY) is not None:  # another process won the race
                con.execute("commit")
                return None
            if attached:
                old_tables = {r[0] for r in con.execute("select name from old.sqlite_master where type='table'")}
                for table, where in specs:
                    if table not in old_tables:
                        counts[table] = 0
                        continue
                    old_cols = set(_columns(con, "old", table))
                    cols = ", ".join(c for c in _columns(con, "main", table) if c in old_cols)
                    cur = con.execute("insert or ignore into main.%s (%s) select %s from old.%s %s"
                                      % (table, cols, cols, table, where or ""))
                    counts[table] = counts.get(table, 0) + max(cur.rowcount, 0)
            set_meta(con, MIGRATED_KEY, json.dumps(
                {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "source": "scheduler.db" if attached else "none",
                 "counts": counts}, sort_keys=True))
            con.execute("commit")
        except Exception:
            try:
                con.execute("rollback")
            except sqlite3.Error:
                pass
            raise
    finally:
        if attached:
            con.execute("detach database old")
    return counts


def migration_report(con):
    raw = get_meta(con, MIGRATED_KEY)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None
