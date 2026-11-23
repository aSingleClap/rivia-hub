"""Daily Google sign-in check: Termux notification, once per day, 05:30 catch-up.
Throwaway RIVIA_DIR / tokens; never touches ~/rivia or ~/.rivia.
    python test_signin_check.py
"""
import asyncio, base64, hashlib, json, os, secrets, socket, stat, sys, tempfile, threading, time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
DOT = os.path.join(TMP, "dot-rivia")
BIN = os.path.join(TMP, "bin")
LOG = os.path.join(TMP, "notify.log")
os.makedirs(DOT, mode=0o700)
os.makedirs(BIN)
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
    RIVIA_SIGNIN_CHECK="0",  # this file drives the check itself
    RIVIA_NOTIFY_LOG=LOG,
)
# Fake termux-notification ahead of PATH. Records argv; never talks to Android.
fake_bin = os.path.join(BIN, "termux-notification")
with open(fake_bin, "w") as f:
    f.write("#!/usr/bin/env python3\n")
    f.write("import json, os, sys\n")
    f.write("path = os.environ['RIVIA_NOTIFY_LOG']\n")
    f.write("with open(path, 'a') as out:\n")
    f.write("    out.write(json.dumps(sys.argv[1:]) + '\\n')\n")
os.chmod(fake_bin, os.stat(fake_bin).st_mode | stat.S_IEXEC)
os.environ["PATH"] = BIN + os.pathsep + os.environ.get("PATH", "")

sys.path.insert(0, HERE)
import rivia_gcal as gcal
import rivia_planner as rp


def ok(cond, label):
    assert cond, label
    print("  ok ·", label)


def notices():
    if not os.path.exists(LOG):
        return []
    return [json.loads(line) for line in open(LOG) if line.strip()]


def clear_notices():
    if os.path.exists(LOG):
        os.remove(LOG)


def clear_meta(*keys):
    con = gcal._sched_db()  # Google bookkeeping lives in scheduler.db
    try:
        for k in keys:
            con.execute("delete from meta where key=?", (k,))
        con.commit()
    finally:
        con.close()


class FakeGoogle:
    def __init__(self):
        self.grant_error = None
        self.items = []

    def handle_token(self, body):
        form = {k: (v[-1] if v else "") for k, v in parse_qs(body.decode()).items()}
        if self.grant_error:
            return 400, {"error": self.grant_error, "error_description": "expired or revoked"}
        if form.get("grant_type") == "refresh_token" and form.get("refresh_token") == "refresh-good":
            return 200, {"access_token": "access-good", "expires_in": 3600, "token_type": "Bearer"}
        return 400, {"error": "invalid_grant", "error_description": "expired or revoked"}

    def handle_events(self, headers):
        auth = headers.get("Authorization") or headers.get("authorization") or ""
        if not auth.startswith("Bearer "):
            return 401, {"error": {"code": 401, "status": "UNAUTHENTICATED"}}
        return 200, {"items": list(self.items)}


fake = FakeGoogle()


class Handler(BaseHTTPRequestHandler):
    def date_time_string(self, timestamp=None):
        import email.utils
        return email.utils.formatdate(usegmt=True)

    def _send(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        status, obj = fake.handle_token(self.rfile.read(n))
        self._send(status, obj)

    def do_GET(self):
        status, obj = fake.handle_events(self.headers)
        self._send(status, obj)

    def log_message(self, *a):
        pass


httpd = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
port = httpd.server_address[1]
os.environ["RIVIA_GOOGLE_TOKEN_URL"] = "http://127.0.0.1:%d/token" % port
os.environ["RIVIA_GOOGLE_EVENTS_URL"] = "http://127.0.0.1:%d/events" % port


def good_token(scope=None):
    gcal.save_tokens({"refresh_token": "refresh-good", "access_token": "access-good",
                      "expiry": time.time() + 3600,
                      "scope": scope or gcal.SCOPE_READ, "token_type": "Bearer"})
    gcal.reset_clock()
    fake.grant_error = None


def complete_token():
    good_token(scope=gcal.SCOPE_ALL)


def expired_token():
    gcal.save_tokens({"refresh_token": "refresh-dead", "access_token": "stale",
                      "expiry": time.time() - 120, "scope": gcal.SCOPE_READ, "token_type": "Bearer"})
    gcal.reset_clock()
    fake.grant_error = "invalid_grant"


NOW = datetime(2026, 11, 15, 6, 0, tzinfo=gcal.TZ)   # after 05:30
BEFORE = datetime(2026, 11, 15, 5, 0, tzinfo=gcal.TZ)
NEXT_DAY = datetime(2026, 11, 16, 6, 0, tzinfo=gcal.TZ)


# ── due / catch-up ──
ok(gcal.signin_check_due(BEFORE, last_ran_day=None) is False,
   "before 05:30 the check is not due")
ok(gcal.signin_check_due(NOW, last_ran_day=None) is True,
   "after 05:30 with no prior run, catch-up is due")
ok(gcal.signin_check_due(NOW, last_ran_day="2026-11-14") is True,
   "after 05:30, a missed yesterday run is due")
ok(gcal.signin_check_due(NOW, last_ran_day="2026-11-15") is False,
   "already ran today: not due again")
ok(gcal.signin_check_due(NEXT_DAY, last_ran_day="2026-11-15") is True,
   "next day after 05:30 is due again")


# ── calendar read still works; a read-only token is flagged for re-sign-in ──
clear_meta(gcal.NOTICE_DAY_KEY, "gcal_login_notice", gcal.CHECK_DAY_KEY, gcal.SCOPE_NOTICE_DAY_KEY)
clear_notices()
good_token()
ok(gcal.signin_check_due(NOW) is True, "due before the first successful run")
got = gcal.run_signin_check(now=NOW, mark_ran=True)
ok(got["ok"] is True and got["written"] is False,
   "read-only token: calendar read is still ok and never writes")
ok(got["needs_relogin"] is True and got.get("scopes_complete") is False,
   "read-only token is flagged as needing re-sign-in")
ok("valid" in (got.get("message") or "").lower()
   and "google-login --write" in (got.get("message") or ""),
   "message says reads are valid and to re-run google-login --write")
ok(got["notified"] is True, "05:30 flags missing write/Drive scopes once")
n = notices()
ok(len(n) == 1, "one scope-gap notification on the first check")
ok("--content" in n[0] and n[0][n[0].index("--content") + 1] == gcal.NOTIFY_SCOPE_CONTENT,
   "notification tells you to run rivia sched google-login --write")
ok(gcal.last_signin_check_day() == "2026-11-15", "successful 05:30 run is recorded")
ok(gcal.signin_check_due(NOW) is False, "catch-up does not re-run after a successful check")
# same day: flag remains, no second notification
got_again = gcal.run_signin_check(now=NOW, mark_ran=True)
ok(got_again["ok"] is True and got_again["needs_relogin"] is True
   and got_again["notified"] is False and len(notices()) == 1,
   "same-day check still flags missing scopes but does not notify again")

# ── no notification when the read is fine and all three scopes are granted ──
clear_meta(gcal.NOTICE_DAY_KEY, "gcal_login_notice", gcal.CHECK_DAY_KEY, gcal.SCOPE_NOTICE_DAY_KEY)
clear_notices()
complete_token()
got = gcal.run_signin_check(now=NOW, mark_ran=True)
ok(got["ok"] is True and got["written"] is False and got["notified"] is False,
   "complete scopes: ok, never writes, no notification")
ok(got.get("scopes_complete") is True and got.get("needs_relogin") is False,
   "complete scopes are not flagged for re-sign-in")
ok(got["day"] == "2026-11-15" and (got.get("message") or "") == "Google sign-in is valid.",
   "complete scopes report a short valid-sign-in message")
ok(set(got.get("scopes") or []) == set(gcal.SCOPES_WRITE),
   "signin-check lists all three granted scopes")
ok(notices() == [], "no notification when the read is fine and scopes are complete")
ok(gcal.last_signin_check_day() == "2026-11-15", "successful complete-scope run is recorded")


# ── fires on expired grant ──
clear_meta(gcal.NOTICE_DAY_KEY, "gcal_login_notice", gcal.CHECK_DAY_KEY, gcal.SCOPE_NOTICE_DAY_KEY)
clear_notices()
expired_token()
got = gcal.run_signin_check(now=NOW, mark_ran=True)
ok(got["ok"] is False and got["error"]["code"] == "calendar_login_expired",
   "expired grant: calendar_login_expired")
ok(got["error"]["step"] == "calendar" and got["written"] is False,
   "expired grant is step calendar and never writes")
ok(got["notified"] is True, "fires a Termux notification on expiry")
ok("14:30" not in json.dumps(got) and "leave_home" not in json.dumps(got),
   "expiry payload has no times")
n = notices()
ok(len(n) == 1, "one notification on the first expiry")
ok("--title" in n[0] and n[0][n[0].index("--title") + 1] == "Rivia",
   "notification title is Rivia")
ok("--content" in n[0] and n[0][n[0].index("--content") + 1] == gcal.NOTIFY_CONTENT,
   "notification tells you to run rivia sched google-login")
ok(gcal.last_signin_check_day() == "2026-11-15", "failed-auth run still counts as today's check")


# ── at most once per America/New_York day ──
got2 = gcal.run_signin_check(now=NOW, mark_ran=True)
ok(got2["ok"] is False and got2["notified"] is False, "second expiry the same day does not notify")
ok(len(notices()) == 1, "still only one notification that day")
# next NY day does notify again
got3 = gcal.run_signin_check(now=NEXT_DAY, mark_ran=True)
ok(got3["notified"] is True and len(notices()) == 2, "next day notifies once more")


# ── missing termux-notification does not crash ──
clear_meta(gcal.NOTICE_DAY_KEY, "gcal_login_notice", gcal.CHECK_DAY_KEY, gcal.SCOPE_NOTICE_DAY_KEY)
clear_notices()
expired_token()
saved_path = os.environ["PATH"]
os.environ["PATH"] = "/usr/bin:/bin"  # no fake binary
try:
    # hide the fake even if /usr/bin is first... actually BIN is no longer on PATH
    crashed = False
    try:
        missing = gcal.run_signin_check(now=NOW, mark_ran=True)
    except Exception as e:  # noqa: BLE001
        crashed = type(e).__name__
        missing = None
finally:
    os.environ["PATH"] = saved_path
ok(crashed is False, "missing termux-notification does not crash")
ok(missing and missing["ok"] is False and missing["error"]["code"] == "calendar_login_expired",
   "missing binary still returns calendar_login_expired")
ok(missing["notified"] is False, "missing binary: notified is false")
ok(notices() == [], "missing binary wrote no notification log")


# ── CLI: rivia sched signin-check (does not mark the 05:30 run) ──
clear_meta(gcal.NOTICE_DAY_KEY, "gcal_login_notice", gcal.CHECK_DAY_KEY, gcal.SCOPE_NOTICE_DAY_KEY)
clear_notices()
complete_token()
ok(gcal.signin_check_due(NOW) is True, "due before a manual check")
cli_ok = gcal.cmd_signin_check(now=NOW)
ok(cli_ok["ok"] is True and cli_ok["notified"] is False and cli_ok.get("scopes_complete") is True,
   "signin-check command succeeds when signed in with all scopes")
ok(gcal.last_signin_check_day() is None or gcal.last_signin_check_day() != "2026-11-15",
   "manual signin-check does not mark the 05:30 run")
ok(gcal.signin_check_due(NOW) is True, "05:30 catch-up is still due after a manual check")

expired_token()
# cmd_signin_check(now=...) — we pass now through. cmd_signin_check accepts now.
cli_bad = gcal.cmd_signin_check(now=NOW)
ok(cli_bad["ok"] is False and cli_bad["error"]["code"] == "calendar_login_expired",
   "manual signin-check reports calendar_login_expired")
ok(cli_bad["notified"] is True and len(notices()) == 1,
   "manual signin-check notifies once when expired")
# python rivia_gcal.py signin-check (uses the box clock; just check it exits cleanly)
import subprocess
r = subprocess.run([sys.executable, os.path.join(HERE, "rivia_gcal.py"), "signin-check"],
                   capture_output=True, text=True, env=os.environ, cwd=HERE)
payload = json.loads(r.stdout)
ok(r.returncode == 1 and payload.get("ok") is False
   and payload["error"]["code"] == "calendar_login_expired",
   "python rivia_gcal.py signin-check exits 1 on expiry")
# The CLI uses the box clock, so its first call may be the first of the real day.
# A second CLI call on that same day must not notify again.
r2 = subprocess.run([sys.executable, os.path.join(HERE, "rivia_gcal.py"), "signin-check"],
                    capture_output=True, text=True, env=os.environ, cwd=HERE)
payload2 = json.loads(r2.stdout)
ok(payload2.get("notified") is False, "CLI second call the same day does not notify again")


# ── in-server loop: catch-up on start, then once a day ──
clear_meta(gcal.NOTICE_DAY_KEY, "gcal_login_notice", gcal.CHECK_DAY_KEY, gcal.SCOPE_NOTICE_DAY_KEY)
clear_notices()
good_token()
gcal.mark_signin_check_ran("2026-11-14")  # yesterday; 06:00 start should catch up
ran = []
real = gcal.run_signin_check


def wrapped(now=None, *, mark_ran=True):
    ran.append({"now": now, "mark_ran": mark_ran})
    return real(now=now, mark_ran=mark_ran)


clock = {"now": NOW}
ticks = {"n": 0}


async def fake_sleep(_s):
    ticks["n"] += 1
    if ticks["n"] >= 3:
        raise asyncio.CancelledError
    # stay at 06:00 ET the first two sleeps, then jump to the next day
    if ticks["n"] == 2:
        clock["now"] = NEXT_DAY


async def run_loop():
    gcal.run_signin_check = wrapped
    try:
        try:
            await gcal.signin_check_loop(get_now=lambda: clock["now"], sleep=fake_sleep, interval_s=1)
        except asyncio.CancelledError:
            pass
    finally:
        gcal.run_signin_check = real


asyncio.run(run_loop())
ok(len(ran) == 2, "loop catch-up once at start, then again the next day (got %d)" % len(ran))
ok(ran[0]["now"] == NOW and ran[1]["now"] == NEXT_DAY,
   "first tick is today's catch-up; later tick is the next day's 05:30")
ok(gcal.last_signin_check_day() == "2026-11-16", "loop recorded the later run")


# ── RIVIA_SCHED_TIMER=0: the 05:30 slot runs only the sign-in check; 21:00 is skipped ──
import rivia_sched as _sched
gcal.mark_signin_check_ran("2026-11-14")
parked = {"tick": 0, "evening": 0, "backup": 0, "backup_kw": []}
real_tick, real_due, real_evening = _sched.run_morning_tick, _sched.evening_refresh_due, _sched.run_evening_refresh
real_backup = gcal.run_drive_backup


def fake_backup(**kw):
    parked["backup"] += 1
    parked["backup_kw"].append(kw)
    return {"ok": True}
ran.clear()
ticks["n"] = 0
clock["now"] = NOW


async def stop_after_one(_s):
    raise asyncio.CancelledError


async def run_parked(signin=None):
    gcal.run_signin_check = signin or wrapped
    gcal.run_drive_backup = fake_backup
    _sched.run_morning_tick = lambda now=None: parked.__setitem__("tick", parked["tick"] + 1)
    _sched.evening_refresh_due = lambda now=None: True
    _sched.run_evening_refresh = lambda now=None: parked.__setitem__("evening", parked["evening"] + 1)
    os.environ["RIVIA_SCHED_TIMER"] = "0"
    try:
        try:
            await gcal.signin_check_loop(get_now=lambda: clock["now"], sleep=stop_after_one, interval_s=1)
        except asyncio.CancelledError:
            pass
    finally:
        os.environ.pop("RIVIA_SCHED_TIMER", None)
        gcal.run_signin_check = real
        gcal.run_drive_backup = real_backup
        _sched.run_morning_tick, _sched.evening_refresh_due, _sched.run_evening_refresh = real_tick, real_due, real_evening


asyncio.run(run_parked())
ok(len(ran) == 1 and ran[0]["mark_ran"] is True, "parked timer: 05:30 slot still runs the sign-in check")
ok(parked["backup"] == 1 and parked["backup_kw"] == [{}],
   "parked timer: 05:30 slot runs the full Drive backup after the sign-in check")
ok(parked["tick"] == 0, "parked timer: no morning tick (no auto-write or tomorrow proposal)")
ok(parked["evening"] == 0, "parked timer: 21:00 evening refresh skipped")
ok(gcal.last_signin_check_day() == NOW.strftime("%Y-%m-%d"), "parked timer: today's run is recorded")


def broken_signin(now=None, *, mark_ran=True):
    raise RuntimeError("boom")


gcal.mark_signin_check_ran("2026-11-14")
parked.update(tick=0, evening=0, backup=0, backup_kw=[])
asyncio.run(run_parked(signin=broken_signin))
ok(parked["backup"] == 1, "parked timer: a failing sign-in check doesn't stop the Drive backup")
ok(parked["tick"] == 0 and parked["evening"] == 0, "parked timer: still no morning tick or evening refresh")

# ── rivia.sh routes the command ──
sh = open(os.path.join(HERE, "rivia.sh")).read()
ok("signin-check) sched_gcal" in sh, "rivia.sh has rivia sched signin-check")

print("\nSIGNIN CHECK PASS")
