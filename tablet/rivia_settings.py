# rivia_settings.py: the settings engine, shared by places and scheduling (a library: no DB of
# its own, no tools). Each part that owns settings registers an "owner": its keys, its DB
# (settings + overrides tables), and its own checks. Every change goes through the same four
# checks as before the core split (input, consistency, dry run, then log + apply or queue as
# pending under G7) and is logged in approvals.db. Nothing here imports places or scheduling.
import json
import re
from datetime import date, datetime, timedelta

import rivia_approvals as approvals
import rivia_time as rt
from rivia_approvals import SettingsError  # noqa: F401  (re-exported)

SETTINGS_SCHEMA = """
create table if not exists settings(
    key text primary key, value text not null, updated text not null, change_id text);
create table if not exists overrides(
    day text not null, key text not null, value text not null,
    created text not null, change_id text, primary key (day, key));
"""
DAY_WINDOW_DAYS = 14
PENDING_MESSAGE = "waiting for approval"

# Registered owners, in order. Each is a dict:
#   name, open() -> con, parse(key) -> spec or None, keys(con) -> [key]
#   optional: normalize(con, key) -> key; prepare(con, spec, value, new_place) -> (value, relative)
#   check_value(con, spec, coerced, relative); extra_for(spec, new_place) -> {key: value}
#   before_apply(con, spec) -> ctx; after_apply(con, spec, data, ctx, coerced)
#   after_undo(con, out); consistency(resolved) -> [problem]; dry_run(day_s, extra) -> [sample]
#   result_extra(spec, data, day); resolve_day(text, now) -> (date, asked); guard(spec, door)
OWNERS = []


def register_owner(owner):
    OWNERS[:] = [o for o in OWNERS if o["name"] != owner["name"]] + [owner]


def _owner(name):
    for o in OWNERS:
        if o["name"] == name:
            return o
    return None


def today_date(now=None):
    if now is None:
        return datetime.now(rt.TZ).date()
    if isinstance(now, date) and not isinstance(now, datetime):
        return now
    if hasattr(now, "astimezone"):
        return now.astimezone(rt.TZ).date()
    if hasattr(now, "date"):
        return now.date()
    return date.fromisoformat(str(now)[:10])


def as_date(day):
    if day is None:
        return None
    if isinstance(day, datetime):
        return day.astimezone(rt.TZ).date() if day.tzinfo else day.date()
    if isinstance(day, date):
        return day
    return date.fromisoformat(str(day)[:10])


def resolve_day(text, now=None):
    """'today', 'tomorrow', a weekday or YYYY-MM-DD, through whichever owner can read days."""
    for o in OWNERS:
        if o.get("resolve_day"):
            return o["resolve_day"](text, now)
    return as_date(text), str(text)


def store_value(v):
    if isinstance(v, bool):
        raise SettingsError("bad_value", "A setting value cannot be true or false.", step="input")
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return json.dumps(v, separators=(",", ":"))


load_value = approvals.load_value


def coerce_time(spec, value):
    s = str(value).strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if not m:
        raise SettingsError("bad_value", "Expected a time as HH:MM (for example 05:30).", step="input")
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise SettingsError("bad_value", "Expected a time as HH:MM (for example 05:30).", step="input")
    text = "%02d:%02d" % (hour, minute)
    lo, hi = spec.get("min"), spec.get("max")
    if (lo and text < lo) or (hi and text > hi):
        raise SettingsError(
            "out_of_range",
            "%s must be between %s and %s (got %s)." % (spec.get("label") or "This setting", lo, hi, text),
            step="settings",
        )
    return text


def coerce(spec, value):
    if spec.get("coerce"):
        return spec["coerce"](spec, value)
    typ = spec["type"]
    if typ == "time":
        return coerce_time(spec, value)
    if typ == "name":
        text = " ".join(str(value if value is not None else "").split())
        if not (1 <= len(text) <= 40) or not re.fullmatch(r"[\w '&-]+", text):
            raise SettingsError("bad_value", "A place name is 1 to 40 letters, digits, spaces, - ' or &.",
                                step="input")
        return text
    if typ == "choice":
        text = str(value if value is not None else "").strip().lower()
        if text not in spec["choices"]:
            raise SettingsError("bad_value", "Expected one of: %s." % ", ".join(spec["choices"]), step="input")
        return text
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


def parse_key(key):
    """Return a descriptor for a catalog key (with "owner"), or raise SettingsError."""
    key = (key or "").strip()
    for o in OWNERS:
        spec = o["parse"](key)
        if spec is not None:
            return dict(spec, owner=o["name"])
    raise SettingsError("bad_key", "Unknown setting: %s." % key, step="input")


def _open_for(spec):
    return _owner(spec["owner"])["open"]()


def standing_row(con, key):
    return con.execute("select value, change_id, updated from settings where key=?", (key,)).fetchone()


def override_row(con, day, key):
    return con.execute("select value, change_id, created from overrides where day=? and key=?",
                       (day, key)).fetchone()


def _resolved_on(con, spec, key, day=None):
    if day is not None:
        row = override_row(con, as_date(day).isoformat(), key)
        if row:
            return load_value(row[0]), "day"
    row = standing_row(con, key)
    if row:
        return load_value(row[0]), "standing"
    return spec["default"], "default"


def resolved(key, day=None):
    """(value, source) for a key on a day: day override, standing value, or default."""
    spec = parse_key(key)
    con = _open_for(spec)
    try:
        return _resolved_on(con, spec, spec["key"], day)
    finally:
        con.close()


def all_setting_keys():
    keys = []
    for o in OWNERS:
        con = o["open"]()
        try:
            keys += o["keys"](con)
        finally:
            con.close()
    return keys


def keys_for_group(group):
    group = (group or "schedule").strip().lower()
    keys = all_setting_keys()
    if group in ("all", "*"):
        return keys
    return [k for k in keys if parse_key(k)["group"] == group]


def _resolve_owner(o, out, day):
    con = o["open"]()
    try:
        for key in o["keys"](con):
            spec = dict(o["parse"](key), owner=o["name"])
            out[key] = _resolved_on(con, spec, key, day)
    finally:
        con.close()


def all_resolved(day=None, extra=None):
    """Every owner's settings. An owner whose DB fails is left out (its checks then don't run),
    so a broken part never blocks changes to another part's settings."""
    out = {}
    for o in OWNERS:
        try:
            _resolve_owner(o, out, day)
        except Exception as e:  # noqa: BLE001
            import sys
            print("rivia: settings of %s unavailable (%s); left out." % (o["name"], type(e).__name__),
                  file=sys.stderr)
    for key, val in (extra or {}).items():
        out[key] = (val, "proposed")
    return out


def owner_resolved(name, day=None, extra=None):
    """One owner's settings only (places uses this so it never needs scheduling's DB)."""
    out = {}
    o = _owner(name)
    if o:
        _resolve_owner(o, out, day)
    for key, val in (extra or {}).items():
        out[key] = (val, "proposed")
    return out


def consistency_problems(resolved_map):
    problems = []
    for o in OWNERS:
        if o.get("consistency"):
            mine = [k for k in resolved_map if resolved_map[k][1] != "proposed"]
            if not mine or any(_safe_owner_of(k) == o["name"] for k in mine):
                problems.extend(o["consistency"](resolved_map))
    return problems


def _safe_owner_of(key):
    for o in OWNERS:
        try:
            if o["parse"](key) is not None:
                return o["name"]
        except SettingsError:
            return o["name"]
    return None


def _dry_run(spec, day_s, extra):
    o = _owner(spec["owner"])
    return o["dry_run"](day_s, extra) if o.get("dry_run") else []


def rerun_checks(spec, day_s, extra):
    problems = consistency_problems(all_resolved(day=day_s, extra=extra))
    if problems:
        raise SettingsError("inconsistent", problems[0], step="settings", extra={"problems": problems})
    added = _dry_run(spec, day_s, extra)
    if added:
        flat = []
        for item in added:
            flat.extend(item["problems"])
        raise SettingsError(
            "breaks_plan",
            "That change would add problems on a sample day: %s" % " ".join(flat),
            step="settings", extra={"samples": added, "problems": flat},
        )


def _source_after(con, spec, key, day, scope):
    val, src = _resolved_on(con, spec, key, day=day if scope == "day" else None)
    # After a standing apply, the result describes the value that was just written.
    if scope == "standing":
        return "standing" if standing_row(con, key) else "default"
    return src


def setting_result(key, day, scope, change_id, old, value, duplicate=False, status="applied"):
    spec = parse_key(key)
    con = _open_for(spec)
    try:
        source = _source_after(con, spec, spec["key"], day, scope)
    finally:
        con.close()
    data = {
        "change_id": change_id, "key": spec["key"], "value": value, "old": old,
        "scope": scope, "day": day, "status": status, "source": source,
        "duplicate": duplicate,
    }
    o = _owner(spec["owner"])
    if o.get("result_extra"):
        o["result_extra"](spec, data, day)
    return data


def pending_result(row, *, duplicate=False):
    data = setting_result(row[6], row[5], row[4], row[0],
                          load_value(row[7]) if row[7] is not None else None,
                          load_value(row[8]) if row[8] is not None else None,
                          duplicate=duplicate, status=row[10])
    data["asked"] = approvals.asked_of(row[2])
    data["expires"] = approvals.expires_iso(row)
    data["reason"] = row[12]
    return data


def _now(now):
    now = now or datetime.now(rt.TZ)
    return now, (int(now.timestamp()) if hasattr(now, "timestamp") else rt.now_s())


def _normalize(key):
    key = (key or "").strip()
    for o in OWNERS:
        if o.get("normalize"):
            con = o["open"]()
            try:
                key = o["normalize"](con, key)
            finally:
                con.close()
    return key


def change_setting(key, value, scope, day=None, reason=None, request_id=None, door="app", now=None,
                   new_place=None):
    """The four checks, then apply (or queue as pending for an app-door standing change, G7)."""
    now, now_s = _now(now)
    today = today_date(now)
    scope = (scope or "").strip().lower()
    door = (door or "app").strip().lower()
    if scope not in ("day", "standing"):
        raise SettingsError("bad_scope", "scope must be day or standing.", step="input")
    spec = parse_key(_normalize(key))
    key = spec["key"]
    o = _owner(spec["owner"])
    if o.get("guard"):
        o["guard"](spec, door)  # e.g. scheduling refuses while its planner selftest is failing
    con = o["open"]()
    try:
        relative = False
        if o.get("prepare"):
            value, relative = o["prepare"](con, spec, value, new_place)
        coerced = coerce(spec, value)
        value_s = store_value(coerced)
        day_s = None
        if scope == "day":
            if not spec["day"]:
                raise SettingsError("bad_scope", "%s can only be a standing value, not a day change." % key, step="input")
            if not day:
                raise SettingsError("bad_day", "A day-scope change needs a day (today, tomorrow, or YYYY-MM-DD).",
                                    step="input")
            if isinstance(day, str):
                try:
                    day_d, _asked = resolve_day(day, now=now)
                except Exception as e:
                    raise SettingsError("bad_day", "Not a day: %s." % day, step="input") from e
            else:
                day_d = as_date(day)
            if day_d < today or day_d > today + timedelta(days=DAY_WINDOW_DAYS):
                raise SettingsError(
                    "bad_day",
                    "A day change must be today through %d days ahead (got %s)." % (DAY_WINDOW_DAYS, day_d.isoformat()),
                    step="input",
                )
            day_s = day_d.isoformat()

        # Check 1 continued: this door may change this key at this scope.
        allowed = spec.get("doors")
        if allowed and door not in allowed:
            raise SettingsError("bad_key", "This setting cannot be changed from the %s door." % door, step="input")
        if door == "app" and not spec.get("ask", True) and not spec["day"]:
            raise SettingsError("bad_key", "This setting cannot be changed from the app door.", step="input")
        if o.get("check_value"):
            o["check_value"](con, spec, coerced, relative)

        acon = approvals.db_open()
        try:
            pending = approvals.find_pending(acon, door, key, value_s, scope, day_s, request_id, now_s)
            dup = None if pending else approvals.find_duplicate(acon, door, key, value_s, scope, day_s, request_id, now_s)
        finally:
            acon.close()
        if pending:
            return pending_result(pending, duplicate=True)
        if dup and spec.get("dup_needs_current") and _resolved_on(con, spec, key, day_s)[0] != coerced:
            dup = None  # the value moved since (an undo, another resize): this is a new change
        if dup:
            return setting_result(key, dup[5], scope, dup[0], load_value(dup[7]) if dup[7] else None,
                                  load_value(dup[8]) if dup[8] else None, duplicate=True, status=dup[10])

        old, _old_src = _resolved_on(con, spec, key, day_s)
        extra = {key: coerced}
        if o.get("extra_for") and new_place:
            extra.update(o["extra_for"](spec, new_place))
        ctx = o["before_apply"](con, spec) if o.get("before_apply") else None

        # Checks 2 and 3: consistent with the others; no new problems on the sample days.
        rerun_checks(spec, day_s, extra)

        # Check 4: log, then apply, or queue as pending for an app-door standing change (G7).
        change_id = approvals.new_id()
        created = rt.iso(now_s)
        if door == "app" and scope == "standing" and not spec.get("app_now"):
            pv = {"asked": approvals.asked_of(door)}
            if isinstance(new_place, dict):
                pv["new_place"] = {k: v for k, v in new_place.items() if v is not None}
            acon = approvals.db_open()
            try:
                approvals.log_change(acon, change_id=change_id, created=created, door=door, kind="setting",
                                     scope=scope, day=day_s, key=key, old=store_value(old) if old is not None else None,
                                     new=value_s, preview=json.dumps(pv),
                                     status="pending", decided=None, reason=reason, request_id=request_id)
            finally:
                acon.close()
            data = setting_result(key, day_s, scope, change_id, old, coerced, duplicate=False, status="pending")
            data["asked"] = approvals.asked_of(door)
            data["expires"] = rt.iso(now_s + approvals.PENDING_EXPIRE_S)
            data["reason"] = reason
            return data
        if scope == "standing":
            con.execute("insert or replace into settings(key, value, updated, change_id) values(?,?,?,?)",
                        (key, value_s, created, change_id))
        else:
            con.execute("insert or replace into overrides(day, key, value, created, change_id) values(?,?,?,?,?)",
                        (day_s, key, value_s, created, change_id))
        data = setting_result(key, day_s, scope, change_id, old, coerced, duplicate=False)
        preview = json.dumps({k: data[k] for k in ("source", "in_use", "in_use_source") if k in data})
        acon = approvals.db_open()
        try:
            approvals.log_change(acon, change_id=change_id, created=created, door=door, kind="setting",
                                 scope=scope, day=day_s, key=key, old=store_value(old) if old is not None else None,
                                 new=value_s, preview=preview, status="applied", decided=created,
                                 reason=reason, request_id=request_id)
        finally:
            acon.close()
        data["reason"] = reason
        if o.get("after_apply"):
            o["after_apply"](con, spec, data, ctx, coerced)
        return data
    finally:
        con.close()


def _mark(cid, status, decided):
    acon = approvals.db_open()
    try:
        acon.execute("update changes set status=?, decided=? where change_id=?", (status, decided, cid))
    finally:
        acon.close()


def _log_undo(**row):
    acon = approvals.db_open()
    try:
        approvals.log_change(acon, **row)
    finally:
        acon.close()


def undo_change(change_id=None, door="app", now=None):
    """Undo a setting change (the latest applied one from this door in 24 h when no id)."""
    now, now_s = _now(now)
    door = (door or "app").strip().lower()
    cid = (change_id or "").strip() or None
    acon = approvals.db_open()
    try:
        if cid:
            row = acon.execute("select * from changes where change_id=?", (cid,)).fetchone()
            if not row:
                raise SettingsError("bad_change_id", "No change with id %s." % cid, step="input")
        else:
            since = rt.iso(now_s - approvals.UNDO_S)
            row = acon.execute(
                "select * from changes where door=? and status='applied' and created>=? "
                "order by created desc, rowid desc limit 1",
                (door, since),
            ).fetchone()
            if not row:
                raise SettingsError("no_undo", "There is no change from this door in the last 24 hours to undo.",
                                    step="settings")
    finally:
        acon.close()
    cid, created, row_door, kind, scope, day_s, key, old_s, new_s, preview, status, decided, reason, request_id = row
    if kind == "write_day" or key == "write_day":
        raise SettingsError(
            "bad_change",
            "A written day cannot be undone from this tool. Reject the pending item or write a new plan.",
            step="settings",
        )
    if status != "applied":
        raise SettingsError("bad_change", "Change %s cannot be undone (status is %s)." % (cid, status), step="settings")
    spec = parse_key(key)
    o = _owner(spec["owner"])
    out = _undo(row, spec, door, now, now_s)
    if o.get("after_undo"):
        con = o["open"]()
        try:
            out = dict(out)
            o["after_undo"](con, out)
        finally:
            con.close()
    return out


def _undo(row, spec, door, now, now_s):
    cid, created, row_door, kind, scope, day_s, key, old_s, new_s, preview, status, decided, reason, request_id = row
    old = load_value(old_s) if old_s is not None else None
    if scope == "standing":
        # Undoing a standing change through the app door is itself a standing change, so it goes
        # pending until approved, unless the key applies at once from the app (zone sizes).
        if door == "app" and not spec.get("app_now"):
            restore = old if old is not None else spec.get("default")
            out = change_setting(key, restore, "standing", reason="undo %s" % cid, door=door, now=now)
            out["undo_of"] = cid
            return out
        if old is None:
            con = _open_for(spec)
            try:
                con.execute("delete from settings where key=?", (key,))
            finally:
                con.close()
        else:
            # Restore through the same checks.
            out = change_setting(key, old, "standing", reason="undo %s" % cid, door=door, now=now)
            if spec.get("undo_marks"):
                out["undo_of"] = cid
                out.pop("say", None)
                _mark(cid, "undone", rt.iso(now_s))
            return out
    else:
        if not day_s:
            raise SettingsError("bad_change", "Change %s has no day to restore." % cid, step="settings")
        # Remove this override first. If that already restores `old` (the default or a standing
        # value), we are done; otherwise write the previous override back.
        con = _open_for(spec)
        try:
            con.execute("delete from overrides where day=? and key=?", (day_s, key))
            current, _src = _resolved_on(con, spec, key, day_s)
        finally:
            con.close()
        if old is not None and current != old:
            out = change_setting(key, old, "day", day=day_s, reason="undo %s" % cid, door=door, now=now)
            out["undo_of"] = cid
            _mark(cid, "undone", rt.iso(now_s))
            return out
        undo_id = approvals.new_id()
        created_s = rt.iso(now_s)
        _log_undo(change_id=undo_id, created=created_s, door=door, kind="undo",
                  scope=scope, day=day_s, key=key, old=new_s,
                  new=store_value(current) if current is not None else None,
                  preview=json.dumps({"undo_of": cid}), status="applied", decided=created_s,
                  reason="undo %s" % cid, request_id=None)
        _mark(cid, "undone", created_s)
        val, src = resolved(key, day=day_s)
        return {"change_id": undo_id, "key": key, "value": val, "old": load_value(new_s),
                "scope": scope, "day": day_s, "status": "applied", "source": src,
                "duplicate": False, "undo_of": cid}
    # standing restore when old is None (delete standing row)
    undo_id = approvals.new_id()
    created_s = rt.iso(now_s)
    _log_undo(change_id=undo_id, created=created_s, door=door, kind="undo",
              scope="standing", day=None, key=key, old=new_s, new=None,
              preview=json.dumps({"undo_of": cid}), status="applied", decided=created_s,
              reason="undo %s" % cid, request_id=None)
    _mark(cid, "undone", created_s)
    val, src = resolved(key, day=None)
    return {"change_id": undo_id, "key": key, "value": val, "old": load_value(new_s),
            "scope": "standing", "day": None, "status": "applied", "source": src,
            "duplicate": False, "undo_of": cid}


def approve_setting(row, now, now_s):
    """Apply a pending setting row (called by rivia_approvals.approve_change_sync)."""
    cid, created, door, kind, scope, day_s, key, old_s, new_s, preview, status, decided, reason, request_id = row
    coerced = load_value(new_s)
    spec = parse_key(key)
    o = _owner(spec["owner"])
    if o.get("guard"):
        o["guard"](spec, door)
    try:
        pv = json.loads(preview) if preview else {}
    except ValueError:
        pv = {}
    new_place = pv.get("new_place") if isinstance(pv, dict) else None
    more = o["extra_for"](spec, new_place) if (o.get("extra_for") and isinstance(new_place, dict)) else {}
    rerun_checks(spec, day_s, dict({key: coerced}, **more))
    created_s = rt.iso(now_s)
    con = o["open"]()
    try:
        for k, v in more.items():  # a new place from the app: its name (and size) come with the pin
            con.execute("insert or replace into settings(key, value, updated, change_id) values(?,?,?,?)",
                        (k, store_value(v), created_s, cid))
        if scope == "standing":
            con.execute("insert or replace into settings(key, value, updated, change_id) values(?,?,?,?)",
                        (key, new_s, created_s, cid))
        else:
            if not day_s:
                raise SettingsError("bad_change", "Change %s has no day to apply." % cid, step="settings")
            con.execute("insert or replace into overrides(day, key, value, created, change_id) values(?,?,?,?,?)",
                        (day_s, key, new_s, created_s, cid))
    finally:
        con.close()
    _mark(cid, "applied", created_s)
    data = setting_result(key, day_s, scope, cid, load_value(old_s) if old_s is not None else None,
                          coerced, duplicate=False, status="applied")
    data["asked"] = approvals.asked_of(door)
    data["reason"] = reason
    data["approved"] = True
    if o.get("after_approve"):
        con = o["open"]()
        try:
            o["after_approve"](con, spec, data)
        finally:
            con.close()
    return data


def get_settings(group="schedule", day=None, now=None):
    today = today_date(now)
    if day:
        if isinstance(day, str) and not (len(day) >= 10 and day[4] == "-"):
            day_d, _asked = resolve_day(day, now=now)
        else:
            day_d = as_date(day)
    else:
        day_d = today
    day_s = day_d.isoformat()
    items = []
    for key in keys_for_group(group):
        spec = parse_key(key)
        val, src = resolved(key, day=day_s)
        item = {
            "key": key, "label": spec["label"], "value": val,
            "default": spec["default"], "unit": spec["unit"],
            "range": [spec["min"], spec["max"]], "source": src,
            "day_change": bool(spec["day"]),
        }
        if src == "day":
            item["override_day"] = day_s
        o = _owner(spec["owner"])
        if o.get("item_extra") and val is not None:
            o["item_extra"](spec, item, day_s)
        items.append(item)
    return {"day": day_s, "group": (group or "schedule"), "settings": items}
