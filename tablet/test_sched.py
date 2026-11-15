"""Scheduler step 2: tool table, ride_time on the app door, call log.
Throwaway RIVIA_DIR; never touches ~/rivia or ~/.rivia.
    python test_sched.py
"""
import asyncio, base64, hashlib, os, secrets, socket, sqlite3, subprocess, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
_salt = secrets.token_bytes(16)
os.environ.update(
    RIVIA_DIR=os.path.join(TMP, "rivia"),
    RIVIA_DB=os.path.join(TMP, "auth.db"), RIVIA_MAILBOX_DB=os.path.join(TMP, "mailbox.db"),
    RIVIA_ISSUER="https://mcp.rivia.observer", RIVIA_JWT_KEY=secrets.token_hex(32),
    RIVIA_PASS_HASH=_salt.hex() + "$" + hashlib.scrypt(b"x" * 16, salt=_salt, n=2**14, r=8, p=1).hex(),
    RIVIA_TOTP_SECRET=base64.b32encode(secrets.token_bytes(20)).decode().rstrip("="),
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

missing = sched.ride_time("farm", "pf")
ok(missing["ok"] is False and missing["kind"] == "error", "no source: ok false, kind error")
ok(missing["error"]["code"] == "need_estimate" and missing["error"]["step"] == "planner",
   "need_maps is mapped to need_estimate (step planner)")
ok(missing["message"] is None, "failure message is null")
ok("minutes" not in missing["data"] and "minutes" not in (missing.get("error") or {}),
   "need_estimate never returns a number")
ok(isinstance(missing["data"].get("pairs"), list) and missing["data"]["pairs"],
   "need_estimate lists the pairs")
ok(missing["data"]["pairs"][0]["from"] == "farm" and missing["data"]["pairs"][0]["to"] == "pf",
   "listed pair is farm>pf")

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
ok(rows == [("ride_time", "app", "sched.ride_time", 1, 1)], "seeded tools: only ride_time on the app door")
# second open does not duplicate
tools.open_db().close()
con = tools.open_db()
try:
    n = con.execute("select count(*) from tools").fetchone()[0]
finally:
    con.close()
ok(n == 1, "seed is idempotent")


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
    ok("ride_time" not in bt, "bot door has no scheduler tools")
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

    need = await call(A, APP, "ride_time", {"from": "farm", "to": "pf"})
    ok(need.get("ok") is False and (need.get("error") or {}).get("code") == "need_estimate",
       "MCP need_estimate for farm>pf")
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


asyncio.run(main())
server.should_exit = True
print("\nSCHED PASS")
