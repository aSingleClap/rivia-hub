"""write_day, auto-write, tomorrow proposal, and the 21:00 refresh.

Throwaway RIVIA_DIR / tokens; never touches ~/rivia or ~/.rivia.
A write aimed at the primary calendar fails the test.
    python test_write_day.py
"""
import base64, hashlib, json, os, re, secrets, tempfile, threading, time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
DOT = os.path.join(TMP, "dot-rivia")
os.makedirs(DOT, mode=0o700)
_salt = secrets.token_bytes(16)
os.environ.update(
    RIVIA_DIR=os.path.join(TMP, "rivia"),
    RIVIA_DOTDIR=DOT,
    RIVIA_GOOGLE_TOKEN_PATH=os.path.join(DOT, "google_token.json"),
    RIVIA_GOOGLE_CLIENT_ID="test-client-id",
    RIVIA_GOOGLE_CLIENT_SECRET="test-client-secret",
    RIVIA_DB=os.path.join(TMP, "auth.db"),
    RIVIA_ISSUER="https://mcp.rivia.observer",
    RIVIA_JWT_KEY=secrets.token_hex(32),
    RIVIA_PASS_HASH=_salt.hex() + "$" + hashlib.scrypt(b"x" * 16, salt=_salt, n=2**14, r=8, p=1).hex(),
    RIVIA_TOTP_SECRET=base64.b32encode(secrets.token_bytes(20)).decode().rstrip("="),
    RIVIA_SIGNIN_CHECK="0",
)
import sys
sys.path.insert(0, HERE)

import rivia_gcal as gcal
import rivia_planner as rp
import rivia_sched as sched
import rivia_approvals as approvals
import rivia_tools as tools


def ok(cond, label):
    assert cond, label
    print("  ok ·", label)


NOW = datetime(2026, 12, 4, 20, 0, tzinfo=gcal.TZ)  # Friday evening; tomorrow is Sat Dec 5
SAT = "2026-12-05"
SHIFT_SAT = {
    "summary": "Work Shift - HMC", "status": "confirmed",
    "start": {"dateTime": "2026-12-05T14:30:00-05:00"},
    "end": {"dateTime": "2026-12-05T23:00:00-05:00"},
    "location": "500 University Dr, Hershey, PA 17033",
}
SHIFT_SAT_LATE = {
    "summary": "Work Shift - HMC", "status": "confirmed",
    "start": {"dateTime": "2026-12-05T16:00:00-05:00"},
    "end": {"dateTime": "2026-12-05T23:30:00-05:00"},
    "location": "500 University Dr, Hershey, PA 17033",
}


class FakeCal:
    def __init__(self):
        self.primary = [dict(SHIFT_SAT)]
        self.calendars = {}
        self.events = {}  # cal_id -> {eid: body}
        self.writes = []  # (method, path)
        self.fail_create_after = None
        self.creates = 0
        self.notifications = []

    def record(self, method, path):
        rec = (method, path)
        self.writes.append(rec)
        return "/calendars/primary" in path


fake = FakeCal()


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw.decode()) if raw else {}

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if path.startswith("/calendar/v3/calendars/primary/events"):
            self._send(200, {"items": list(fake.primary)})
            return
        m = re.match(r"^/calendar/v3/calendars/([^/]+)/events/?$", path)
        if m:
            cal = unquote(m.group(1))
            if cal == "primary":
                self._send(200, {"items": list(fake.primary)})
                return
            qs = parse_qs(u.query)
            want = None
            for item in qs.get("privateExtendedProperty") or []:
                if item.startswith("rivia_day="):
                    want = item.split("=", 1)[1]
            items = list((fake.events.get(cal) or {}).values())
            if want is not None:
                items = [
                    ev for ev in items
                    if ((ev.get("extendedProperties") or {}).get("private") or {}).get("rivia_day") == want
                ]
            self._send(200, {"items": items})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        body = self._read()
        if path.rstrip("/") == "/calendar/v3/calendars":
            if fake.record("POST", path):
                self._send(403, {"error": "primary forbidden"})
                return
            cal_id = "rivia-cal-1"
            fake.calendars[cal_id] = {"id": cal_id, "summary": body.get("summary") or "Rivia"}
            fake.events.setdefault(cal_id, {})
            self._send(200, fake.calendars[cal_id])
            return
        m = re.match(r"^/calendar/v3/calendars/([^/]+)/events/?$", path)
        if m:
            cal = unquote(m.group(1))
            if fake.record("POST", path) or cal == "primary":
                self._send(403, {"error": "primary forbidden"})
                return
            if fake.fail_create_after is not None and fake.creates >= fake.fail_create_after:
                self._send(500, {"error": {"code": 500, "message": "injected"}})
                return
            fake.creates += 1
            eid = "ev_%s" % secrets.token_hex(4)
            ev = dict(body)
            ev["id"] = eid
            fake.events.setdefault(cal, {})[eid] = ev
            self._send(200, ev)
            return
        # Drive or anything else
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        u = urlparse(self.path)
        path = u.path
        m = re.match(r"^/calendar/v3/calendars/([^/]+)/events/([^/]+)/?$", path)
        if not m:
            self._send(404, {"error": "not found"})
            return
        cal, eid = unquote(m.group(1)), unquote(m.group(2))
        if fake.record("DELETE", path) or cal == "primary":
            self._send(403, {"error": "primary forbidden"})
            return
        (fake.events.get(cal) or {}).pop(eid, None)
        self.send_response(204)
        self.end_headers()

    def do_PUT(self):
        fake.record("PUT", urlparse(self.path).path)
        self._send(403, {"error": "primary forbidden"})

    def do_PATCH(self):
        fake.record("PATCH", urlparse(self.path).path)
        self._send(403, {"error": "primary forbidden"})

    def log_message(self, *a):
        pass


httpd = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
PORT = httpd.server_address[1]
BASE = "http://127.0.0.1:%d/calendar/v3" % PORT
os.environ["RIVIA_GOOGLE_EVENTS_URL"] = BASE + "/calendars/primary/events"
os.environ["RIVIA_GOOGLE_CALENDAR_BASE"] = BASE
os.environ["RIVIA_GOOGLE_TOKEN_URL"] = "http://127.0.0.1:%d/token" % PORT

gcal.save_tokens({
    "refresh_token": "refresh-good",
    "access_token": "access-good",
    "expiry": int(time.time() + 3600),
    "scope": " ".join(gcal.SCOPES_WRITE),
})


def no_primary_writes(label):
    bad = [w for w in fake.writes if "/calendars/primary" in w[1]]
    ok(not bad, "%s: no writes to primary (%s)" % (label, bad))


def notify(content, title=None):
    fake.notifications.append(content)
    return {"ok": True}


gcal.post_termux_notification = notify


def clear_meta(*keys):
    con = gcal._sched_db()  # scheduling's bookkeeping lives in scheduler.db
    try:
        for k in keys:
            try:
                con.execute("delete from meta where key=?", (k,))
            except Exception:
                try:
                    rp.set_meta(con, k, "")
                except Exception:
                    pass
        con.commit()
    finally:
        con.close()


def wipe_write_days():
    con = approvals.db_open()
    try:
        con.execute("delete from changes where kind='write_day' or key='write_day'")
        con.commit()
    finally:
        con.close()


def reset(shift=None):
    fake.primary = [dict(shift or SHIFT_SAT)]
    fake.calendars = {}
    fake.events = {}
    fake.writes = []
    fake.fail_create_after = None
    fake.creates = 0
    fake.notifications = []
    wipe_write_days()
    clear_meta(gcal.META_RIVIA_ID, sched.META_EVENING_DAY, "last_signin_check_day")


sched.set_selftest(True)
sched.ensure_store(now=NOW)
tools.open_db().close()


# ── 1. write_day is always pending; nothing is written until approve ──
reset()
pending = sched.write_day_sync("tomorrow", door="app", now=NOW)
ok(pending["status"] == "pending" and pending["written"] is False, "write_day returns pending, written=False")
ok(pending["day"] == SAT and pending.get("preview"), "pending preview is for Saturday")
ok((pending["preview"] or {}).get("status") == "planned", "preview status is planned")
ok((pending["preview"] or {}).get("blocks"), "preview includes blocks")
ok(not fake.calendars and not fake.writes, "nothing is written to Google before approval")
cid = pending["change_id"]

# duplicate keeps the same pending item
again = sched.write_day_sync("tomorrow", door="app", now=NOW)
ok(again["duplicate"] is True and again["change_id"] == cid, "a second write_day is a duplicate pending")
ok(not fake.writes, "duplicate does not write")

# tool envelope
env = sched.write_day("tomorrow")
# tool uses now=real clock by default; still should be pending or need day=
# Use the sync path for clock-sensitive cases. The tool exists and is app-only.
ok(sched.ACTIONS["sched.write_day"]["doors"] == ("app",), "write_day action is app door only")
ok("bot" not in sched.ACTIONS["sched.write_day"]["doors"], "write_day is off the bot door")
ok("write_day" in {r[0] for r in tools.open_db().execute("select name from tools")},
   "write_day is seeded on the app door")


# ── 2. approve writes the Rivia calendar only; primary is untouched ──
applied = sched.approve_change_sync(cid, now=NOW)
ok(applied["status"] == "applied" and applied["written"] is True, "approve writes the planned day")
ok(applied.get("created", 0) >= 1, "at least one Rivia event was created")
ok(applied.get("calendar_id") == "rivia-cal-1", "events went to the new Rivia calendar")
ok(gcal._meta_get(gcal.META_RIVIA_ID) == "rivia-cal-1", "gcal_rivia_id is stored")
rivia_items = list((fake.events.get("rivia-cal-1") or {}).values())
ok(len(rivia_items) == applied["created"], "Rivia calendar holds the new batch")
ok(all(((ev.get("extendedProperties") or {}).get("private") or {}).get("rivia_day") == SAT
       for ev in rivia_items), "new events are tagged rivia_day")
ok(all(((ev.get("extendedProperties") or {}).get("private") or {}).get("rivia_batch")
       for ev in rivia_items), "new events are tagged rivia_batch")
no_primary_writes("first approve")
ok(fake.primary == [SHIFT_SAT] or fake.primary[0]["start"] == SHIFT_SAT["start"],
   "primary events were not modified")


# ── 3. approving again is refused and creates nothing new ──
n_before = len(fake.events.get("rivia-cal-1") or {})
try:
    sched.approve_change_sync(cid, now=NOW)
    raise AssertionError("second approve should have been refused")
except sched.SettingsError as e:
    ok(e.code == "already_approved", "second approve is already_approved")
ok(len(fake.events.get("rivia-cal-1") or {}) == n_before, "second approve created no events")
no_primary_writes("second approve")


# ── 4. a changed plan is refused with changed_since_preview; nothing new is written ──
reset()
pending = sched.write_day_sync(SAT, door="app", now=NOW)
n_writes = len(fake.writes)
fake.primary = [dict(SHIFT_SAT_LATE)]
try:
    sched.approve_change_sync(pending["change_id"], now=NOW)
    raise AssertionError("changed plan should have been refused")
except sched.SettingsError as e:
    ok(e.code == "changed_since_preview", "approve after a calendar change is changed_since_preview")
ok(not fake.calendars and len(fake.writes) == n_writes, "changed_since_preview wrote nothing")
no_primary_writes("changed_since_preview")


# ── 5. 05:30 proposes tomorrow as a system item; an existing pending is kept ──
reset()
fri_530 = datetime(2026, 12, 4, 5, 30, tzinfo=gcal.TZ)
# Friday 05:30 proposes Saturday
prop = sched.propose_tomorrow(now=fri_530)
ok(prop.get("proposed") is True and prop.get("day") == SAT, "05:30 proposes tomorrow")
ok(prop.get("asked") == "system", "the proposal is asked=system")
row = None
con = approvals.db_open()
try:
    row = con.execute("select door, kind, status from changes where change_id=?",
                      (prop["change_id"],)).fetchone()
finally:
    con.close()
ok(row and row[0] == "system" and row[1] == "write_day" and row[2] == "pending",
   "proposal is a pending system write_day")
kept = sched.propose_tomorrow(now=fri_530)
ok(kept.get("proposed") is False and kept.get("kept") == prop["change_id"],
   "a second proposal keeps the existing pending item")

# nothing to write or delete → propose nothing
reset()
fake.primary = []
empty = sched.propose_tomorrow(now=fri_530)
ok(empty.get("proposed") is False, "no_shift with no Rivia events proposes nothing")


# ── 6. 21:00 refresh supersedes a stale proposal when the plan changes ──
reset()
queued = sched.write_day_sync(SAT, door="system", now=NOW)
eve = datetime(2026, 12, 4, 21, 0, tzinfo=gcal.TZ)
same = sched.run_evening_refresh(now=eve)
ok(same.get("superseded") is False and same.get("change_id") == queued["change_id"],
   "21:00 keeps the proposal when the plan is unchanged")
fake.primary = [dict(SHIFT_SAT_LATE)]
again_eve = datetime(2026, 12, 4, 21, 1, tzinfo=gcal.TZ)
# already marked as ran at 21:00; call the refresh body anyway by clearing the mark
clear_meta(sched.META_EVENING_DAY)
changed = sched.run_evening_refresh(now=again_eve)
ok(changed.get("superseded") is True and changed.get("proposed") is True,
   "21:00 supersedes when tomorrow's plan changed")
ok(changed.get("old") == queued["change_id"] and changed.get("change_id") != queued["change_id"],
   "the new preview replaces the old pending item")
con = approvals.db_open()
try:
    old_st = con.execute("select status from changes where change_id=?",
                         (queued["change_id"],)).fetchone()[0]
    new_st = con.execute("select status, door from changes where change_id=?",
                         (changed["change_id"],)).fetchone()
finally:
    con.close()
ok(old_st == "superseded" and new_st[0] == "pending" and new_st[1] == "system",
   "old item is superseded; new item is pending system")
no_primary_writes("evening refresh")


# ── 7. auto-write at cutoff writes the pending day; standing is never auto-approved ──
reset()
pending = sched.write_day_sync(SAT, door="app", now=NOW)
# a standing change also pending
standing = sched.change_setting_sync(
    "arrive_early_minutes", 11, "standing", door="app", now=NOW,
)
ok(standing["status"] == "pending", "standing change is pending")
sat_530 = datetime(2026, 12, 5, 5, 30, tzinfo=gcal.TZ)
results = sched.auto_write_due(now=sat_530)
ok(any(r.get("ok") and r.get("change_id") == pending["change_id"] and r.get("written") for r in results),
   "05:30 auto-writes the pending write_day")
con = approvals.db_open()
try:
    st_status = con.execute("select status from changes where change_id=?",
                            (standing["change_id"],)).fetchone()[0]
    wd_status = con.execute("select status from changes where change_id=?",
                            (pending["change_id"],)).fetchone()[0]
finally:
    con.close()
ok(st_status == "pending", "standing changes are never auto-approved")
ok(wd_status == "applied", "the write_day was applied by the morning tick")
no_primary_writes("auto-write")


# ── 8. after 21:00, a further calendar change makes 05:30 refuse and notify ──
reset()
queued = sched.write_day_sync(SAT, door="system", now=NOW)
clear_meta(sched.META_EVENING_DAY)
sched.run_evening_refresh(now=datetime(2026, 12, 4, 21, 0, tzinfo=gcal.TZ))
# change the shift after the evening refresh, do not refresh again
fake.primary = [dict(SHIFT_SAT_LATE)]
fake.notifications = []
results = sched.auto_write_due(now=sat_530)
hit = [r for r in results if r.get("change_id") == queued["change_id"] or r.get("error") == "changed_since_preview"]
ok(hit and hit[0]["ok"] is False and hit[0]["error"] == "changed_since_preview",
   "05:30 refuses a stale preview with changed_since_preview")
ok(not fake.events, "nothing was written after a stale preview")
ok(any("changed since its preview" in n for n in fake.notifications),
   "Termux is notified when auto-write refuses a stale preview")
con = approvals.db_open()
try:
    st = con.execute("select status, reason from changes where change_id=?",
                     (queued["change_id"],)).fetchone()
finally:
    con.close()
ok(st[0] == "failed" and st[1] == "changed_since_preview", "stale item is marked failed")
no_primary_writes("stale auto-write")


# ── 9. catch-up after a missed 05:30, if the first block has not started ──
reset()
pending = sched.write_day_sync(SAT, door="app", now=NOW)
sat_0800 = datetime(2026, 12, 5, 8, 0, tzinfo=gcal.TZ)
results = sched.auto_write_due(now=sat_0800)
ok(any(r.get("ok") and r.get("written") for r in results),
   "a missed 05:30 is caught up before the first block")
no_primary_writes("catch-up")


# ── 10. catch-up is skipped once the first block has started ──
reset()
pending = sched.write_day_sync(SAT, door="app", now=NOW)
first = sched._preview_first_start(
    # rebuild a row-like preview
    (None,) * 9 + (json.dumps(pending["preview"]),),
    pending["preview"],
)
ok(first is not None, "preview has a leave-home / first action start")
late = datetime.fromtimestamp(first + 60, gcal.TZ)
results = sched.auto_write_due(now=late)
ok(not any(r.get("change_id") == pending["change_id"] for r in results),
   "catch-up does not write after the first block has started")
ok(not fake.events, "no events written after the day is under way")
con = approvals.db_open()
try:
    # expire_pending should have flipped it
    st = con.execute("select status from changes where change_id=?",
                     (pending["change_id"],)).fetchone()[0]
finally:
    con.close()
ok(st == "expired", "a write_day past its first block expires rather than writing")
no_primary_writes("too late")


# ── 11. no_shift deletes that day's tagged events ──
reset()
# first write a planned Saturday
pending = sched.write_day_sync(SAT, door="app", now=NOW)
sched.approve_change_sync(pending["change_id"], now=NOW)
ok(fake.events.get("rivia-cal-1"), "Rivia events exist before no_shift")
# now the day has no work shift; queue and approve a no_shift write
fake.primary = []
# use a Friday evening now still before Saturday
pending2 = sched.write_day_sync(SAT, door="app", now=NOW)
ok((pending2["preview"] or {}).get("status") == "no_shift", "preview is no_shift")
applied = sched.approve_change_sync(pending2["change_id"], now=NOW)
ok(applied.get("deleted", 0) >= 1 and not (fake.events.get("rivia-cal-1") or {}),
   "no_shift deletes that day's tagged Rivia events")
no_primary_writes("no_shift delete")


# ── 12. writing to primary is refused; the test fails if a write is aimed there ──
reset()
# plant a primary id in meta and call write_rivia_day directly
rp_con = gcal._sched_db()
try:
    rp.set_meta(rp_con, gcal.META_RIVIA_ID, "primary")
finally:
    rp_con.close()
try:
    gcal.write_rivia_day(SAT, "planned", [{
        "summary": "x",
        "start": "2026-12-05T10:00:00-05:00",
        "end": "2026-12-05T11:00:00-05:00",
        "kind": "gym",
    }])
    raise AssertionError("write_rivia_day must refuse primary")
except gcal.GcalError as e:
    ok(e.code == "primary_forbidden", "write_rivia_day refuses a primary calendar id")
no_primary_writes("primary id planted")


# ── 13. a partial create rolls the new batch back and leaves the old plan ──
reset()
pending = sched.write_day_sync(SAT, door="app", now=NOW)
sched.approve_change_sync(pending["change_id"], now=NOW)
old_ids = set((fake.events.get("rivia-cal-1") or {}).keys())
ok(old_ids, "an old batch exists")
# queue a new write after a shift change (will be a new fingerprint; we
# approve after replacing the stored preview so approve proceeds, then
# inject a mid-create failure)
fake.primary = [dict(SHIFT_SAT_LATE)]
pending3 = sched.write_day_sync(SAT, door="app", now=NOW)
# fail the second create
fake.fail_create_after = fake.creates + 1
try:
    sched.approve_change_sync(pending3["change_id"], now=NOW)
    raise AssertionError("injected create failure should have raised")
except (gcal.GcalError, sched.SettingsError) as e:
    ok(True, "partial create raised %s" % getattr(e, "code", type(e).__name__))
# old events remain; leftover new-batch events must be gone
left = fake.events.get("rivia-cal-1") or {}
ok(set(left.keys()) == old_ids, "rollback leaves the old batch and drops the new one")
no_primary_writes("rollback")


# ── 14. cutoff wins over the 24 h expiry for write_day ──
reset()
pending = sched.write_day_sync(SAT, door="app", now=NOW)
standing = sched.change_setting_sync(
    "arrive_early_minutes", 12, "standing", door="app", now=NOW,
)
old_created = rp.iso(int(NOW.timestamp()) - 25 * 3600)
con = approvals.db_open()
try:
    con.execute("update changes set created=? where change_id=?", (old_created, pending["change_id"]))
    con.execute("update changes set created=? where change_id=?", (old_created, standing["change_id"]))
    con.commit()
    # Friday 20:00 is still before Saturday 05:30 and before the first block
    sched.expire_pending(con, int(NOW.timestamp()))
    wd = con.execute("select status from changes where change_id=?",
                     (pending["change_id"],)).fetchone()[0]
    st = con.execute("select status from changes where change_id=?",
                     (standing["change_id"],)).fetchone()[0]
finally:
    con.close()
ok(wd == "pending", "write_day stays pending after 24 h if it is still eligible")
ok(st == "expired", "a standing change still expires after 24 h")


# ── 15. auto_write_time is honored instead of a hard-coded 05:30 ──
# apply as termux so it is not queued
changed_t = sched.change_setting_sync(
    "auto_write_time", "06:15", "standing", door="termux", now=NOW,
)
ok(changed_t.get("status") == "applied" or changed_t.get("value") == "06:15",
   "auto_write_time can be set from termux")
ok(gcal.signin_check_at_hm() == (6, 15), "morning tick reads auto_write_time")
ok(not gcal.signin_check_due(datetime(2026, 12, 5, 5, 30, tzinfo=gcal.TZ),
                             last_ran_day="2026-12-04"),
   "05:30 is not due when auto_write_time is 06:15")
ok(gcal.signin_check_due(datetime(2026, 12, 5, 6, 15, tzinfo=gcal.TZ),
                         last_ran_day="2026-12-04"),
   "06:15 is due when auto_write_time is 06:15")
# restore default so later ticks stay at 05:30
sched.change_setting_sync("auto_write_time", "05:30", "standing", door="termux", now=NOW)
ok(gcal.signin_check_at_hm() == (5, 30), "auto_write_time restored to 05:30")


# ── 16. morning tick order: sign-in, auto-write, propose tomorrow, backup ──
reset()
# leave a Saturday write_day pending and run the tick at Saturday 05:30
pending = sched.write_day_sync(SAT, door="app", now=NOW)
# tomorrow from Saturday 05:30 is Sunday — no shift, so propose nothing unless events
order = []
_real_signin = gcal.run_signin_check
_real_auto = sched.auto_write_due
_real_prop = sched.propose_tomorrow
_real_bak = gcal.run_drive_backup


def _signin(*a, **k):
    order.append("signin")
    return _real_signin(*a, **k)


def _auto(*a, **k):
    order.append("auto_write")
    return _real_auto(*a, **k)


def _prop(*a, **k):
    order.append("propose")
    return _real_prop(*a, **k)


def _bak(*a, **k):
    order.append("backup")
    return {"ok": True, "skipped": True}


gcal.run_signin_check = _signin
sched.auto_write_due = _auto
sched.propose_tomorrow = _prop
gcal.run_drive_backup = _bak
try:
    tick = sched.run_morning_tick(now=sat_530)
finally:
    gcal.run_signin_check = _real_signin
    sched.auto_write_due = _real_auto
    sched.propose_tomorrow = _real_prop
    gcal.run_drive_backup = _real_bak
ok(order == ["signin", "auto_write", "propose", "backup"],
   "morning tick order is sign-in, auto-write, propose tomorrow, backup")
ok(tick.get("auto_write") is not None and tick.get("propose") is not None,
   "morning tick returns auto_write and propose results")
no_primary_writes("morning tick")


# ── 17. pending list never auto-approves a standing change (already in 7) ──
# also: approving tomorrow's proposal before 05:30 writes at once
reset()
prop = sched.propose_tomorrow(now=NOW)  # Friday 20:00 proposes Saturday
ok(prop.get("proposed"), "proposal exists before cutoff")
applied = sched.approve_change_sync(prop["change_id"], now=NOW)
ok(applied.get("written") and applied.get("status") == "applied",
   "approving tomorrow's proposal before 05:30 writes it at once")
no_primary_writes("early approve")


httpd.shutdown()
print("WRITE_DAY PASS")
