"""Approval queue (G7): pending standing changes, approve/reject/undo, expiry, auto_write_time.

Throwaway RIVIA_DIR / RIVIA_DOTDIR; never touches ~/rivia or ~/.rivia.
    python test_approval.py
"""
import asyncio
import base64
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
DOT = os.path.join(TMP, "dot-rivia")
os.makedirs(DOT, mode=0o700)
_salt = secrets.token_bytes(16)
os.environ.update(
    RIVIA_DIR=os.path.join(TMP, "rivia"),
    RIVIA_DOTDIR=DOT,
    HOME=TMP,
    RIVIA_GOOGLE_TOKEN_PATH=os.path.join(DOT, "google_token.json"),
    RIVIA_GOOGLE_CLIENT_ID="test-client-id",
    RIVIA_GOOGLE_CLIENT_SECRET="test-client-secret",
    RIVIA_DB=os.path.join(TMP, "auth.db"),
    RIVIA_MAILBOX_DB=os.path.join(TMP, "mailbox.db"),
    RIVIA_ISSUER="https://mcp.rivia.observer",
    RIVIA_JWT_KEY=secrets.token_hex(32),
    RIVIA_PASS_HASH=_salt.hex() + "$" + hashlib.scrypt(b"x" * 16, salt=_salt, n=2**14, r=8, p=1).hex(),
    RIVIA_TOTP_SECRET=base64.b32encode(secrets.token_bytes(20)).decode().rstrip("="),
    RIVIA_SIGNIN_CHECK="0",
)
sys.path.insert(0, HERE)

import rivia_admin as admin
import rivia_auth as ra
import rivia_gcal as gcal
import rivia_planner as rp
import rivia_sched as sched
import rivia_server as rs
import rivia_tools as tools
import uvicorn
from fastmcp import Client

failed = 0


def ok(cond, label):
    global failed
    if cond:
        print("  ok ·", label)
    else:
        failed += 1
        print("FAIL ·", label)


def refused(fn):
    try:
        fn()
        return None
    except sched.SettingsError as e:
        return e


def standing(key):
    con = sched.db_open()  # schedule settings live in scheduler.db
    try:
        row = con.execute("select value from settings where key=?", (key,)).fetchone()
        return None if not row else json.loads(row[0])
    finally:
        con.close()


def workout(day=None, now=None):
    got = sched.get_settings_sync(group="schedule", day=day or SAT, now=now or NOW)
    w = next(s for s in got["settings"] if s["key"] == "workout_minutes")
    return w["value"], w["source"]


NOW = datetime(2026, 12, 4, 20, 0, tzinfo=gcal.TZ)
SAT = "2026-12-05"
LATER = NOW + timedelta(hours=25)

sched.set_selftest(True)
sched.ensure_store()

# ── auto_write_time: default, range, admin/termux only (the 05:30 job itself is later) ──
ok(sched.SETTINGS["auto_write_time"]["default"] == "05:30", "auto_write_time defaults to 05:30")
got = sched.get_settings_sync(group="system", now=NOW)
item = next(s for s in got["settings"] if s["key"] == "auto_write_time")
ok(item["value"] == "05:30" and item["range"] == ["03:00", "09:00"],
   "auto_write_time is visible in the system group with a 03:00–09:00 range")
applied = sched.change_setting_sync("auto_write_time", "06:15", "standing", door="admin", now=NOW)
ok(applied.get("status") == "applied" and standing("auto_write_time") == "06:15",
   "admin can set auto_write_time at once")
termux = sched.change_setting_sync("auto_write_time", "07:00", "standing", door="termux", now=NOW)
ok(termux.get("status") == "applied" and standing("auto_write_time") == "07:00",
   "Termux can set auto_write_time")
err = refused(lambda: sched.change_setting_sync("auto_write_time", "06:00", "standing", door="app", now=NOW))
ok(err is not None and err.code == "bad_key", "the app door cannot change auto_write_time")
err = refused(lambda: sched.change_setting_sync("auto_write_time", "02:00", "standing", door="admin", now=NOW))
ok(err is not None and err.code == "out_of_range", "auto_write_time before 03:00 is refused")
err = refused(lambda: sched.change_setting_sync("auto_write_time", "10:00", "standing", door="admin", now=NOW))
ok(err is not None and err.code == "out_of_range", "auto_write_time after 09:00 is refused")
err = refused(lambda: sched.change_setting_sync("auto_write_time", "morning", "standing", door="admin", now=NOW))
ok(err is not None and err.code == "bad_value", "a non-HH:MM auto_write_time is refused")
# restore default for a clean standing table
sched.change_setting_sync("auto_write_time", "05:30", "standing", door="admin", now=NOW)

# ── an out-of-range standing request is refused, not queued ──
err = refused(lambda: sched.change_setting_sync(
    "workout_minutes", 200, "standing", door="app", now=NOW))
ok(err is not None and err.code == "out_of_range",
   "an out-of-range standing change is refused instead of queued")

# ── standing from Labs is pending; the plan is unchanged ──
first = sched.change_setting_sync(
    "workout_minutes", 90, "standing", door="app", now=NOW, reason="standing from Labs")
ok(first.get("status") == "pending" and first.get("asked") == "app" and first.get("change_id"),
   "a standing change from the app door is pending")
ok(first.get("value") == 90 and standing("workout_minutes") is None,
   "the pending value is not written yet")
ok(workout() == (120, "default"),
   "a pending standing change leaves Saturday's plan unchanged")

# identical request returns the same item
again = sched.change_setting_sync(
    "workout_minutes", 90, "standing", door="app", now=NOW + timedelta(minutes=5),
    reason="same request later")
ok(again.get("change_id") == first["change_id"] and again.get("duplicate") is True
   and again.get("status") == "pending",
   "a second identical request returns the same pending item")

# list_changes shows who asked
listed = sched.list_changes_sync(status="pending", now=NOW)
ids = [c["change_id"] for c in listed["changes"]]
row = next(c for c in listed["changes"] if c["change_id"] == first["change_id"])
ok(row["asked"] == "app" and row["status"] == "pending" and row["key"] == "workout_minutes",
   "list_changes shows the pending item and that the app asked")

# ── approve applies once; a second approve is refused; undo restores ──
approved = sched.approve_change_sync(first["change_id"], now=NOW + timedelta(minutes=10))
ok(approved.get("status") == "applied" and standing("workout_minutes") == 90,
   "approve applies the standing value")
ok(workout() == (90, "standing"),
   "after approve, Saturday uses the 90-minute standing workout")
err = refused(lambda: sched.approve_change_sync(first["change_id"], now=NOW + timedelta(minutes=11)))
ok(err is not None and err.code == "already_approved", "a second approve is refused")

undone = sched.undo_change_sync(change_id=first["change_id"], door="admin",
                                now=NOW + timedelta(minutes=12))
ok(standing("workout_minutes") in (None, 120)
   or undone.get("value") in (120, None),
   "admin undo restores the previous workout length")
ok(workout()[0] == 120,
   "after undo, Saturday is back to the default 120-minute workout")

# ── reject leaves values unchanged ──
pending2 = sched.change_setting_sync(
    "workout_minutes", 75, "standing", door="app", now=NOW + timedelta(minutes=20),
    reason="try 75")
rejected = sched.reject_change_sync(pending2["change_id"], now=NOW + timedelta(minutes=21))
ok(rejected.get("status") == "rejected" and standing("workout_minutes") != 75,
   "reject leaves the standing value unchanged")
err = refused(lambda: sched.approve_change_sync(pending2["change_id"], now=NOW + timedelta(minutes=22)))
ok(err is not None and err.code == "not_pending", "an already rejected change cannot be approved")

# ── expiry with a fake clock ──
pending3 = sched.change_setting_sync(
    "prework_minutes", 40, "standing", door="app", now=NOW + timedelta(minutes=30),
    reason="expire me")
cid3 = pending3["change_id"]
expired_list = sched.list_changes_sync(status="expired", now=LATER)
ok(any(c["change_id"] == cid3 and c["status"] == "expired" and c["asked"] == "app"
       for c in expired_list["changes"]),
   "an item older than 24 hours shows expired")
ok(standing("prework_minutes") is None, "an expired pending change was not applied")
err = refused(lambda: sched.approve_change_sync(cid3, now=LATER))
ok(err is not None and err.code == "expired", "approve of an expired item is refused")

# ── Termux approve of a fresh pending item ──
pending4 = sched.change_setting_sync(
    "farm_minutes", 60, "standing", door="app", now=NOW + timedelta(minutes=40))
cli = subprocess.run(
    [sys.executable, os.path.join(HERE, "rivia_sched.py"), "approve", pending4["change_id"]],
    capture_output=True, text=True, env=os.environ,
)
ok(cli.returncode == 0, "rivia sched approve exits 0")
cli_out = json.loads(cli.stdout)
ok(cli_out.get("ok") is True and cli_out.get("status") == "applied"
   and standing("farm_minutes") == 60,
   "rivia sched approve applies the pending change")
cli_r = subprocess.run(
    [sys.executable, os.path.join(HERE, "rivia_sched.py"), "reject", pending4["change_id"]],
    capture_output=True, text=True, env=os.environ,
)
# already applied, should fail
ok(cli_r.returncode != 0, "rivia sched reject of an applied change exits non-zero")

# description line on the tool
desc = tools.append_fixed_lines(sched.CHANGE_SETTING_DESCRIPTION, "sched.change_setting")
ok(sched.PENDING_LINE in desc, "the change-setting tool description includes the pending-result line")

# ── doors: app token cannot approve; queue tools live only on the admin door ──
admin._restart_impl = lambda: None
tools.reset_install()
# Keep the selftest override; public_app may rerun it.


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
server = uvicorn.Server(uvicorn.Config(rs.public_app(), host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    if server.started:
        break
    time.sleep(0.05)
ok(server.started, "test server started")
BASE = "http://127.0.0.1:%d" % PORT

app_mint = subprocess.run(
    [sys.executable, os.path.join(HERE, "rivia_auth.py"), "mint-token", "app", "30"],
    capture_output=True, text=True, env=os.environ,
)
APP = app_mint.stdout.strip().splitlines()[0]
admin_mint = subprocess.run(
    [sys.executable, os.path.join(HERE, "rivia_auth.py"), "mint-token", "admin", "7"],
    capture_output=True, text=True, env=os.environ,
)
ok(admin_mint.returncode == 0 and "eyJ" not in admin_mint.stdout and "eyJ" not in admin_mint.stderr,
   "admin mint in the approval test does not print the token")
ADMIN = ra.load_admin_token()


def raw_post(path, token, body):
    url = BASE + path
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


pending5 = sched.change_setting_sync(
    "wind_down_minutes", 90, "standing", door="app", now=NOW + timedelta(minutes=50))
call_body = json.dumps({
    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
    "params": {"name": "approve_change", "arguments": {"change_id": pending5["change_id"]}},
}).encode()
status, body = raw_post("/admin/mcp", APP, call_body)
ok(status == 403 and b"wrong_door" in body, "the app token cannot call approve on the admin door")
status, body = raw_post("/app/mcp", APP, call_body)
# approve_change is not on the app door; a tools/call must not apply the change.
ok(status == 200, "app door answers a tools/call")
text = body.decode()
ok("approve_change" not in text or "not found" in text.lower() or "unknown" in text.lower()
   or '"error"' in text,
   "the app door does not run approve_change")
ok(standing("wind_down_minutes") != 90, "the app token cannot approve a pending change")


async def listed(url, token):
    async with Client(url, auth=token) as c:
        return {t.name: t for t in await c.list_tools()}


async def call(url, token, tool, args):
    async with Client(url, auth=token) as c:
        r = await c.call_tool(tool, args, raise_on_error=False)
        return r.structured_content or {}


async def main():
    A, D = BASE + "/app/mcp", BASE + "/admin/mcp"
    at, dt = await listed(A, APP), await listed(D, ADMIN)
    queue = {"list_changes", "approve_change", "reject_change", "undo_change"}
    ok(queue <= set(dt), "admin door lists the approval-queue tools")
    ok(not (queue & set(at)), "approval-queue tools stay off the app door")
    got = await call(D, ADMIN, "approve_change", {"change_id": pending5["change_id"]})
    ok(got.get("ok") is True and (got.get("data") or {}).get("status") == "applied"
       and standing("wind_down_minutes") == 90,
       "admin approve_change applies the pending item")
    again = await call(D, ADMIN, "approve_change", {"change_id": pending5["change_id"]})
    ok(again.get("ok") is False and (again.get("error") or {}).get("code") == "already_approved",
       "admin second approve_change is refused")
    restored = await call(D, ADMIN, "undo_change", {"change_id": pending5["change_id"]})
    ok(restored.get("ok") is True and standing("wind_down_minutes") != 90,
       "admin undo_change restores the previous value")


asyncio.run(main())
server.should_exit = True
print("\nAPPROVAL PASS" if failed == 0 else "\nAPPROVAL FAIL (%d)" % failed)
sys.exit(1 if failed else 0)
