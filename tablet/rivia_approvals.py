# rivia_approvals.py: the change log and approval queue, in approvals.db.
#
# Every setting change (schedule or place) is a row in changes; app-door standing changes wait
# here as pending until approved (G7), and scheduling's write_day proposals queue here too.
# This module stores rows and runs approve / reject / list / undo. What a row MEANS is up to
# the part that owns it: setting rows go back through the settings engine (rivia_settings),
# and other kinds (write_day) are handled by the part that registered them (scheduling).
# Nothing here imports places or scheduling.
import json
import secrets
from datetime import datetime

import rivia_store as store
import rivia_time as rt

SCHEMA = """
create table if not exists changes(
    change_id text primary key, created text not null, door text, kind text,
    scope text, day text, key text, old text, new text, preview text,
    status text, decided text, reason text, request_id text);
"""
MIGRATE = [("changes", "order by rowid")]
PENDING_EXPIRE_S = 24 * 3600
DUPLICATE_S = 60
UNDO_S = 24 * 3600
COLS = ("change_id", "created", "door", "kind", "scope", "day", "key", "old", "new", "preview",
        "status", "decided", "reason", "request_id")

# kind -> {"approve": fn(row, now, now_s) -> data, "expires_s": fn(row) -> epoch,
#          "too_late": fn(row, now_s) -> bool, "public": fn(row, out) -> None}
KINDS = {}


class SettingsError(Exception):
    def __init__(self, code, msg, *, step="settings", extra=None):
        super().__init__(msg)
        self.code = code
        self.step = step
        self.extra = extra or {}


def register_kind(kind, **handlers):
    KINDS[kind] = handlers


def db_open():
    """approvals.db (the changes table), migrated once from scheduler.db."""
    return store.connect("approvals", SCHEMA, migrate=MIGRATE)


def new_id():
    return "chg_" + secrets.token_hex(8)


def load_value(text):
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        try:
            return int(text)
        except (TypeError, ValueError):
            return float(text)


def log_change(con, **row):
    con.execute(
        "insert into changes(change_id, created, door, kind, scope, day, key, old, new, preview, status, decided, reason, request_id) "
        "values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (row["change_id"], row["created"], row.get("door"), row.get("kind") or "setting",
         row.get("scope"), row.get("day"), row.get("key"),
         row.get("old"), row.get("new"), row.get("preview"),
         row.get("status"), row.get("decided"), row.get("reason"), row.get("request_id")),
    )


def asked_of(door):
    d = (door or "").strip().lower()
    if d in ("app", "admin"):
        return d
    return "system"


def row_created_s(row):
    try:
        return rt.to_epoch(row[1], "created")
    except Exception:
        return 0


def preview_obj(row):
    raw = row[9] if row is not None and len(row) > 9 else None
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _kind_of(row):
    return "write_day" if (row[3] == "write_day" or row[6] == "write_day") else (row[3] or "setting")


def expires_s(row):
    h = KINDS.get(_kind_of(row)) or {}
    if h.get("expires_s"):
        return h["expires_s"](row)
    return row_created_s(row) + PENDING_EXPIRE_S


def expires_iso(row):
    return rt.iso(expires_s(row))


def is_expired(row, now_s):
    h = KINDS.get(_kind_of(row)) or {}
    if h.get("too_late"):
        return h["too_late"](row, now_s)
    return row_created_s(row) + PENDING_EXPIRE_S <= now_s


def expire_pending(con, now_s):
    """Mark pending rows past their cutoff as expired. Returns how many flipped."""
    rows = list(con.execute("select * from changes where status='pending'"))
    n = 0
    decided = rt.iso(now_s)
    for row in rows:
        if is_expired(row, now_s):
            con.execute("update changes set status='expired', decided=? where change_id=?",
                        (decided, row[0]))
            n += 1
    return n


def find_pending(con, door, key, value_s, scope, day, request_id, now_s):
    expire_pending(con, now_s)
    if request_id:
        row = con.execute(
            "select * from changes where request_id=? and status='pending' "
            "order by created desc, rowid desc limit 1",
            (request_id,),
        ).fetchone()
        if row:
            return row
    row = con.execute(
        "select * from changes where status='pending' and door=? and key=? and new=? "
        "and ifnull(scope,'')=? and ifnull(day,'')=? "
        "order by created desc, rowid desc limit 1",
        (door, key, value_s, scope or "", day or ""),
    ).fetchone()
    return row


def find_duplicate(con, door, key, value_s, scope, day, request_id, now_s):
    if request_id:
        row = con.execute(
            "select * from changes where request_id=? order by created desc, rowid desc limit 1",
            (request_id,),
        ).fetchone()
        if row:
            return row
    since = rt.iso(now_s - DUPLICATE_S)
    row = con.execute(
        "select * from changes where door=? and key=? and new=? and ifnull(scope,'')=? "
        "and ifnull(day,'')=? and created>=? order by created desc, rowid desc limit 1",
        (door, key, value_s, scope or "", day or "", since),
    ).fetchone()
    return row


def change_public(row):
    out = {
        "change_id": row[0], "created": row[1], "door": row[2], "asked": asked_of(row[2]),
        "kind": row[3], "scope": row[4], "day": row[5], "key": row[6],
        "old": load_value(row[7]) if row[7] is not None else None,
        "value": load_value(row[8]) if row[8] is not None else None,
        "status": row[10], "decided": row[11], "reason": row[12], "request_id": row[13],
        "expires": expires_iso(row) if row[10] in ("pending", "expired") else None,
    }
    h = KINDS.get(_kind_of(row)) or {}
    if h.get("public"):
        h["public"](row, out)
    return out


def load_change(con, change_id):
    cid = (change_id or "").strip()
    if not cid:
        raise SettingsError("bad_change_id", "A change_id is required.", step="input")
    row = con.execute("select * from changes where change_id=?", (cid,)).fetchone()
    if not row:
        raise SettingsError("bad_change_id", "No change with id %s." % cid, step="input")
    return row


def _now(now):
    now = now or datetime.now(rt.TZ)
    return now, (int(now.timestamp()) if hasattr(now, "timestamp") else rt.now_s())


def approve_change_sync(change_id, now=None, con=None):
    """Re-run every check and apply a pending change once. A repeat is refused.
    con is accepted for old callers and ignored: the queue lives in approvals.db."""
    now, now_s = _now(now)
    acon = db_open()
    try:
        expire_pending(acon, now_s)
        row = load_change(acon, change_id)  # re-read after expiry so a stale pending shows as expired
    finally:
        acon.close()
    cid, status = row[0], row[10]
    if status == "applied":
        raise SettingsError("already_approved", "Change %s has already been approved." % cid, step="settings")
    if status == "expired":
        raise SettingsError("expired", "Change %s has expired." % cid, step="settings")
    if status != "pending":
        raise SettingsError("not_pending", "Change %s cannot be approved (status is %s)." % (cid, status),
                            step="settings")
    kind = _kind_of(row)
    if kind != "setting":
        h = KINDS.get(kind) or {}
        if not h.get("approve"):
            raise SettingsError("unavailable", "The part that handles %s changes is not running." % kind,
                                step="settings")
        return h["approve"](row, now, now_s)
    import rivia_settings as settings  # the settings engine applies setting rows
    return settings.approve_setting(row, now, now_s)


def reject_change_sync(change_id, now=None, con=None):
    """Reject a pending change. Values stay as they are."""
    now, now_s = _now(now)
    acon = db_open()
    try:
        expire_pending(acon, now_s)
        row = load_change(acon, change_id)
        cid, created, door, kind, scope, day_s, key, old_s, new_s, preview, status, decided, reason, request_id = row
        if status == "rejected":
            raise SettingsError("already_rejected", "Change %s has already been rejected." % cid, step="settings")
        if status == "expired":
            raise SettingsError("expired", "Change %s has expired." % cid, step="settings")
        if status != "pending":
            raise SettingsError("not_pending", "Change %s cannot be rejected (status is %s)." % (cid, status),
                                step="settings")
        acon.execute("update changes set status='rejected', decided=? where change_id=?", (rt.iso(now_s), cid))
        return {
            "change_id": cid, "key": key, "scope": scope, "day": day_s,
            "old": load_value(old_s) if old_s is not None else None,
            "value": load_value(new_s) if new_s is not None else None,
            "status": "rejected", "asked": asked_of(door), "reason": reason,
        }
    finally:
        acon.close()


def list_changes_sync(status=None, now=None, con=None, limit=100):
    """Pending and recent changes. Expired pending items are marked expired. Includes who asked."""
    now, now_s = _now(now)
    try:
        limit_n = int(limit)
    except (TypeError, ValueError):
        limit_n = 100
    limit_n = max(1, min(500, limit_n))
    acon = db_open()
    try:
        expire_pending(acon, now_s)
        want = (status or "").strip().lower() or None
        if want:
            rows = list(acon.execute("select * from changes where status=? order by created desc, rowid desc limit ?",
                                     (want, limit_n)))
        else:
            rows = list(acon.execute("select * from changes order by created desc, rowid desc limit ?", (limit_n,)))
        return {"changes": [change_public(r) for r in rows]}
    finally:
        acon.close()


def undo_change_sync(change_id=None, door="app", now=None, con=None):
    """Undo a setting change (the latest from this door in 24 h when no id is given)."""
    import rivia_settings as settings
    return settings.undo_change(change_id=change_id, door=door, now=now)


def settings_log():
    """Every change, oldest first (rivia sched settings-log)."""
    acon = db_open()
    try:
        rows = list(acon.execute(
            "select change_id, created, door, scope, day, key, old, new, status, reason from changes "
            "order by created, rowid"))
    finally:
        acon.close()
    cols = ["change_id", "created", "door", "scope", "day", "key", "old", "new", "status", "reason"]
    return [dict(zip(cols, r)) for r in rows]
