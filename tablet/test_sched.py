"""Scheduler step 2: tool table, ride_time on the app door, call log.
Throwaway RIVIA_DIR; never touches ~/rivia or ~/.rivia.
    python test_sched.py
"""
import asyncio, base64, hashlib, json, os, secrets, socket, sqlite3, subprocess, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
_salt = secrets.token_bytes(16)
DOT = os.path.join(TMP, "dot-rivia")
os.makedirs(DOT, mode=0o700)
os.environ.update(
    RIVIA_DIR=os.path.join(TMP, "rivia"),
    RIVIA_DOTDIR=DOT,
    RIVIA_GOOGLE_TOKEN_PATH=os.path.join(DOT, "google_token.json"),
    RIVIA_GOOGLE_CLIENT_ID="test-client-id",
    RIVIA_GOOGLE_CLIENT_SECRET="test-client-secret",
    RIVIA_DB=os.path.join(TMP, "auth.db"), RIVIA_MAILBOX_DB=os.path.join(TMP, "mailbox.db"),
    RIVIA_ISSUER="https://mcp.rivia.observer", RIVIA_JWT_KEY=secrets.token_hex(32),
    RIVIA_PASS_HASH=_salt.hex() + "$" + hashlib.scrypt(b"x" * 16, salt=_salt, n=2**14, r=8, p=1).hex(),
    RIVIA_TOTP_SECRET=base64.b32encode(secrets.token_bytes(20)).decode().rstrip("="),
    RIVIA_SIGNIN_CHECK="0",
)
sys.path.insert(0, HERE)

import rivia_planner as rp
import rivia_sched as sched
import rivia_tools as tools
import rivia_auth as ra
import rivia_server as rs
import uvicorn
from fastmcp import Client


def ok(cond, label):
    assert cond, label
    print("  ok ·", label)


# ── unit: envelope, legs match, need_estimate, selftest block ──
# Force a passing cache so these don't depend on running the full planner suite first.
sched.set_selftest(True)

farm_hmc = sched.ride_time("farm", "hmc")
legs = rp.cmd_legs({"pairs": [["farm", "hmc"]]})["legs"][0]
ok(farm_hmc["ok"] is True and farm_hmc["v"] == 1 and farm_hmc["kind"] == "legs", "ride_time envelope: ok, v=1, kind=legs")
ok(farm_hmc["error"] is None and farm_hmc["message"] is None, "success has no error or message")
ok(farm_hmc["data"]["minutes"] == legs["minutes"] and farm_hmc["data"]["source"] == legs["source"],
   "ride_time minutes/source match planner legs (%s, %s)" % (legs["minutes"], legs["source"]))
ok(farm_hmc["data"]["from"] == "farm" and farm_hmc["data"]["to"] == "hmc" and farm_hmc["ref"] == "farm>hmc",
   "ride_time ref and places")
ok(farm_hmc["data"]["source"] == "your estimate" and farm_hmc["data"]["minutes"] == 20,
   "farm>hmc uses the standing estimate (20)")

missing = sched.ride_time("gym", "pf")
ok(missing["ok"] is False and missing["kind"] == "error", "no source: ok false, kind error")
ok(missing["error"]["code"] == "need_estimate" and missing["error"]["step"] == "planner",
   "need_maps is mapped to need_estimate (step planner)")
ok(missing["message"] is None, "failure message is null")
ok("minutes" not in missing["data"] and "minutes" not in (missing.get("error") or {}),
   "need_estimate never returns a number")
ok(isinstance(missing["data"].get("pairs"), list) and missing["data"]["pairs"],
   "need_estimate lists the pairs")
ok(missing["data"]["pairs"][0]["from"] == "gym" and missing["data"]["pairs"][0]["to"] == "pf",
   "listed pair is gym>pf")

bad = sched.ride_time("farm", "moon")
ok(bad["ok"] is False and bad["error"]["code"] == "bad_place" and bad["error"]["step"] == "input",
   "unknown place: bad_place, step input")

sched.set_selftest(False, "forced")
blocked = sched.ride_time("farm", "hmc")
ok(blocked["ok"] is False and blocked["error"]["code"] == "selftest_failed" and blocked["error"]["step"] == "planner",
   "a failing selftest blocks ride_time")
sched.set_selftest(True)

# ── tool table seed (ride_time on app only) ──
con = tools.open_db()
try:
    rows = list(con.execute("select name, door, action, model, enabled from tools"))
finally:
    con.close()
ok(set(rows) == {
    ("ride_time", "app", "sched.ride_time", 1, 1),
    ("plan_day", "app", "sched.plan_day", 1, 1),
    ("get_schedule_settings", "app", "sched.get_settings", 1, 1),
    ("change_schedule_setting", "app", "sched.change_setting", 1, 1),
    ("undo_schedule_change", "app", "sched.undo_change", 1, 1),
}, "seeded tools: ride_time, plan_day, and settings tools on the app door only")
# second open does not duplicate
tools.open_db().close()
con = tools.open_db()
try:
    n = con.execute("select count(*) from tools").fetchone()[0]
finally:
    con.close()
ok(n == 5, "seed is idempotent")
ok(sched.ACTIONS["sched.get_settings"]["doors"] == ("app",), "get_settings is app door only")
ok(sched.ACTIONS["sched.change_setting"]["doors"] == ("app",), "change_setting is app door only")
ok(sched.ACTIONS["sched.undo_change"]["doors"] == ("app",), "undo_change is app door only")
ok("bot" not in sched.ACTIONS["sched.get_settings"]["doors"]
   and "bot" not in sched.ACTIONS["sched.change_setting"]["doors"]
   and "bot" not in sched.ACTIONS["sched.undo_change"]["doors"],
   "settings actions are not on the bot door")


# ── plan_day / plan_week against a fake Google on loopback ──
import email.utils
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
import rivia_gcal as gcal

SHIFT_DT = "2026-11-16T14:30:00-05:00"
SHIFT_END = "2026-11-16T23:00:00-05:00"
SHIFT_ITEM = {
    "summary": "Work Shift - HMC", "status": "confirmed",
    "start": {"dateTime": SHIFT_DT}, "end": {"dateTime": SHIFT_END},
    "location": "500 University Dr, Hershey, PA 17033",
}
SHIFT_NORM = gcal.norm_google_event(SHIFT_ITEM)
PLAN_NOW = datetime(2026, 11, 15, 2, 30, tzinfo=gcal.TZ)  # Sunday; tomorrow is the shift


class _FakeG:
    def __init__(self):
        self.items = [SHIFT_ITEM]
        self.grant_error = None
        self.fail_events = 0
        self.event_hits = 0
        self.refresh_tokens = {"refresh-good": {"access": "access-good"}}


fake_g = _FakeG()


class _GHandler(BaseHTTPRequestHandler):
    def date_time_string(self, timestamp=None):
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
        form = {k: (v[-1] if v else "") for k, v in parse_qs(self.rfile.read(n).decode()).items()}
        if fake_g.grant_error:
            self._send(400, {"error": fake_g.grant_error, "error_description": "denied"})
            return
        if form.get("grant_type") == "refresh_token":
            rec = fake_g.refresh_tokens.get(form.get("refresh_token"))
            if not rec:
                self._send(400, {"error": "invalid_grant", "error_description": "expired or revoked"})
                return
            self._send(200, {"access_token": rec["access"], "expires_in": 3600, "token_type": "Bearer"})
            return
        self._send(400, {"error": "unsupported_grant_type"})

    def do_GET(self):
        fake_g.event_hits += 1
        if fake_g.fail_events > 0:
            fake_g.fail_events -= 1
            self._send(500, {"error": {"code": 500}})
            return
        self._send(200, {"items": list(fake_g.items)})

    def log_message(self, *a):
        pass


_gsrv = HTTPServer(("127.0.0.1", 0), _GHandler)
threading.Thread(target=_gsrv.serve_forever, daemon=True).start()
_gport = _gsrv.server_address[1]
os.environ["RIVIA_GOOGLE_TOKEN_URL"] = "http://127.0.0.1:%d/token" % _gport
os.environ["RIVIA_GOOGLE_EVENTS_URL"] = "http://127.0.0.1:%d/calendar/v3/calendars/primary/events" % _gport
gcal.save_tokens({"refresh_token": "refresh-good", "access_token": "access-good",
                  "expiry": time.time() + 3600, "scope": gcal.SCOPE_READ, "token_type": "Bearer"})

direct = rp.cmd_plan({"days": ["2026-11-16"], "events": [SHIFT_NORM], "now": int(PLAN_NOW.timestamp())})
direct_rec = direct["days"][0]
direct_leave = (direct_rec["message"]["facts"]["leave_home_at"]["epoch"]
                if direct_rec.get("message") and direct_rec["message"].get("facts", {}).get("leave_home_at")
                else None)
ok(direct_leave is not None, "direct planner produced a leave-home time for the sample shift")

got = sched.plan_day_sync("tomorrow", now=PLAN_NOW)
ok(got["ok"] is True and got["kind"] == "plan" and got["data"]["written"] is False,
   "plan_day succeeds and never writes")
ok(got["data"]["day"] == "2026-11-16" and got["ref"] == "2026-11-16", "plan_day tomorrow is 2026-11-16")
ok(got["message"] and got["data"]["status"] in ("planned", "not_written", "under_way", "no_shift"),
   "plan_day has the planner message and a status")
got_leave = (got["message"]["facts"].get("leave_home_at") or {}).get("epoch")
ok(got_leave == direct_leave, "plan_day leave-home time matches the planner called directly (%s)" % got_leave)
ok("blocks" in got["data"] and "shift" in got["data"] and "gym" in got["data"] and "problems" in got["data"]
   and "rides" in got["data"], "plan_day data has day, status, shift, gym, blocks, problems, rides, written")

week = sched.plan_week_sync(now=PLAN_NOW)
ok(week["ok"] is True and len(week["data"]["days"]) == 7, "plan_week covers 7 days")
ok(week["data"]["days"][0]["day"] == "2026-11-15" and week["data"]["days"][-1]["day"] == "2026-11-21",
   "plan_week is today through today+6")
ok(all("blocks" not in d and "message" in d and "status" in d and "problems" in d for d in week["data"]["days"]),
   "plan_week days have status, problems, message, and no blocks")
ok(week["data"]["days"][1]["day"] == "2026-11-16" and week["data"]["days"][1]["status"] == got["data"]["status"],
   "plan_week Monday matches plan_day status")

# Google failure: no times, step calendar
fake_g.grant_error = "invalid_grant"
gcal.save_tokens({"refresh_token": "refresh-dead", "access_token": "stale",
                  "expiry": time.time() - 120, "scope": gcal.SCOPE_READ, "token_type": "Bearer"})
gcal.reset_clock()
# clear prior notice day so the first failure is recorded
con = tools.open_db()
try:
    con.execute("delete from meta where key in ('gcal_login_notice_day','gcal_login_notice')")
finally:
    con.close()
bad = sched.plan_day_sync("tomorrow", now=PLAN_NOW)
ok(bad["ok"] is False and bad["error"]["step"] == "calendar", "Google failure is ok:false, step calendar")
ok(bad["error"]["code"] == "calendar_login_expired", "expired grant becomes calendar_login_expired")
blob = json.dumps(bad)
ok("14:30" not in blob and "leave_home" not in blob and not (bad.get("data") or {}).get("blocks"),
   "calendar failure returns no times")

# Auth-failure hook: at most one first_today per New York day
first = gcal.record_calendar_auth_failure(gcal.GcalError("calendar_login_expired", gcal.FIX_LOGIN), now=PLAN_NOW)
# already recorded by plan_day_sync above
ok(first["first_today"] is False and first["reason"] == "already_today",
   "auth failure is recorded at most once per day")
# a non-auth error is ignored
other = gcal.record_calendar_auth_failure(gcal.GcalError("calendar_http", "boom"), now=PLAN_NOW)
ok(other["first_today"] is False and other["reason"] == "not_auth", "non-auth calendar errors are not notice-worthy")
# The sign-in notice is a Termux notification, tested in test_signin_check.py.

# Restore a good token for any later MCP plan_day call
fake_g.grant_error = None
gcal.save_tokens({"refresh_token": "refresh-good", "access_token": "access-good",
                  "expiry": time.time() + 3600, "scope": gcal.SCOPE_READ, "token_type": "Bearer"})
sched.set_selftest(True)

# ── MCP server: app door has ride_time, bot door does not; call + log + timing ──
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# public_app() will run the real startup selftest and register tools.
sched.reset_selftest()
tools.reset_install()
PORT = free_port()
server = uvicorn.Server(uvicorn.Config(rs.public_app(), host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    if server.started:
        break
    time.sleep(0.05)
ok(server.started, "test server started")
BASE = "http://127.0.0.1:%d" % PORT
APP = subprocess.run([sys.executable, os.path.join(HERE, "rivia_auth.py"), "mint-app-token", "30"],
                     capture_output=True, text=True, env=os.environ)
ok(APP.returncode == 0, "minted app token")
APP = APP.stdout.strip()
with ra.db() as c:
    c.execute("insert into clients values(?,?,?,?)",
              ("grok-test", '["https://www.cursor.com/agents/mcp/oauth/callback"]', "Grok Bot", time.time()))
BOT = ra.issue_tokens("grok-test")["access_token"]


async def listed(url, token):
    async with Client(url, auth=token) as c:
        return {t.name: t for t in await c.list_tools()}


async def call(url, token, tool, args):
    async with Client(url, auth=token) as c:
        r = await c.call_tool(tool, args, raise_on_error=False)
        return r.structured_content or {}


async def main():
    A, B = BASE + "/app/mcp", BASE + "/mcp"
    at, bt = await listed(A, APP), await listed(B, BOT)
    ok("ride_time" in at, "app door lists ride_time")
    ok("plan_day" in at, "app door lists plan_day")
    ok("get_schedule_settings" in at and "change_schedule_setting" in at and "undo_schedule_change" in at,
       "app door lists the schedule settings tools")
    ok("plan_week" not in at, "plan_week has no seeded row")
    ok("ride_time" not in bt and "plan_day" not in bt and "plan_week" not in bt
       and "get_schedule_settings" not in bt and "change_schedule_setting" not in bt
       and "undo_schedule_change" not in bt,
       "bot door has no scheduler tools")
    schema = getattr(at["ride_time"], "input_schema", None) or at["ride_time"].inputSchema
    props = (schema or {}).get("properties") or {}
    ok("from" in props and "to" in props, "ride_time schema uses public names from and to")
    ok("from_place" not in props and "to_place" not in props, "python parameter names are not exposed")
    ok(sched.FAIL_RULE.split(".")[0] in (at["ride_time"].description or ""),
       "ride_time description includes the failed-tool rule")

    t0 = time.perf_counter()
    got = await call(A, APP, "ride_time", {"from": "home", "to": "hmc"})
    ms = int((time.perf_counter() - t0) * 1000)
    expect = rp.cmd_legs({"pairs": [["home", "hmc"]]})["legs"][0]
    ok(got.get("ok") is True and got.get("kind") == "legs", "MCP ride_time succeeds")
    ok(got["data"]["minutes"] == expect["minutes"] and got["data"]["source"] == expect["source"],
       "MCP ride_time matches planner legs")
    ok(ms < 1000, "MCP ride_time returned in under a second (%d ms wall)" % ms)

    need = await call(A, APP, "ride_time", {"from": "gym", "to": "pf"})
    ok(need.get("ok") is False and (need.get("error") or {}).get("code") == "need_estimate",
       "MCP need_estimate for gym>pf")
    ok("minutes" not in (need.get("data") or {}), "MCP need_estimate has no minutes")

    con = sqlite3.connect(os.path.join(os.environ["RIVIA_DIR"], "scheduler.db"))
    calls = list(con.execute("select door, tool, action, ok, code, ms from calls order by rowid"))
    con.close()
    ok(len(calls) >= 2, "calls log has a row per ride_time call")
    last_ok = [c for c in calls if c[3] == 1]
    last_need = [c for c in calls if c[4] == "need_estimate"]
    ok(last_ok and last_ok[-1][:4] == ("app", "ride_time", "sched.ride_time", 1),
       "successful call logged (door=app, tool=ride_time, action=sched.ride_time, ok=1)")
    ok(last_need and last_need[-1][5] is not None, "need_estimate call logged with ms")
    ok(all(c[5] < 1000 for c in calls), "every logged ride_time was under 1000 ms: %s" % [c[5] for c in calls])

    t0 = time.perf_counter()
    plan = await call(A, APP, "plan_day", {"day": "tomorrow"})
    # "tomorrow" on the box clock may not be 2026-11-16; just check the tool answers quickly with the envelope
    ok(plan.get("ok") is True and plan.get("kind") == "plan", "MCP plan_day answers")
    ok(plan.get("data", {}).get("written") is False, "MCP plan_day never writes")
    ok((time.perf_counter() - t0) < 5, "MCP plan_day returned in a few seconds")


asyncio.run(main())
server.should_exit = True
print("\nSCHED PASS")
