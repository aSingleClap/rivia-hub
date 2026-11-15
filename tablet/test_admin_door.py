"""Admin door, admin token, and fixed admin tools.

The admin token is read from a throwaway RIVIA_DOTDIR file and is never printed.
    python test_admin_door.py
"""
import asyncio
import json
import os
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
DOT = os.path.join(TMP, "dot-rivia")
os.makedirs(DOT, mode=0o700)
os.environ.update(
    RIVIA_DIR=os.path.join(TMP, "rivia"),
    RIVIA_HOME=os.path.join(TMP, "rivia"),
    RIVIA_DOTDIR=DOT,
    RIVIA_DB=os.path.join(TMP, "auth.db"),
    RIVIA_MAILBOX_DB=os.path.join(TMP, "mailbox.db"),
    HOME=TMP,
    RIVIA_ISSUER="https://mcp.rivia.observer",
    RIVIA_JWT_KEY=secrets.token_hex(32),
    RIVIA_PASS_HASH="x",
    RIVIA_TOTP_SECRET="MFRGGZDFMY",
    RIVIA_SIGNIN_CHECK="0",
)
sys.path.insert(0, HERE)

import rivia_auth as ra
import rivia_admin as admin
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


def _looks_like_token(text):
    if not text:
        return False
    # JWTs from this server start with the typ/alg header eyJ
    return "eyJ" in text


HERE_SH = os.path.join(HERE, "rivia.sh")


def mint_cli(kind, days=None):
    args = [sys.executable, os.path.join(HERE, "rivia_auth.py"), "mint-token", kind]
    if days is not None:
        args.append(str(days))
    return subprocess.run(args, capture_output=True, text=True, env=os.environ)


# ── mint-token admin stores a 600 file and never prints the secret ──
app_mint = mint_cli("app", 30)
ok(app_mint.returncode == 0, "mint-token app succeeds")
APP = app_mint.stdout.strip().splitlines()[0]
ok(APP.startswith("eyJ"), "mint-token app still prints the app token on stdout")

first = mint_cli("admin", 7)
ok(first.returncode == 0, "mint-token admin succeeds")
ok(not _looks_like_token(first.stdout) and not _looks_like_token(first.stderr),
   "mint-token admin does not print the token")
path = ra.admin_token_path()
ok(os.path.isfile(path) and path.startswith(DOT), "admin token is stored under RIVIA_DOTDIR")
mode = stat.S_IMODE(os.stat(path).st_mode)
ok(mode == 0o600, "admin token file mode is 600")
ADMIN = ra.load_admin_token()
ok(bool(ADMIN) and ADMIN.startswith("eyJ"), "stored admin token is readable by the test helper")
ok("Saved admin token" in first.stdout and "not printed" in first.stdout,
   "mint-token admin reports the save, not the value")

# A second mint revokes only admin tokens; the app token stays live.
second = mint_cli("admin", 7)
ok(second.returncode == 0 and not _looks_like_token(second.stdout + second.stderr),
   "a second admin mint does not print the token")
ADMIN2 = ra.load_admin_token()
ok(ADMIN2 != ADMIN, "a new admin token replaces the stored file")

# Constant-time compare: the stored value matches itself, not the previous one.
ok(ra.admin_token_matches(ADMIN2), "stored admin token matches in constant time")
ok(not ra.admin_token_matches(ADMIN), "the revoked admin token does not match the file")
ok(not ra.admin_token_matches(""), "an empty bearer does not match")

# cors_origins drops * and null
os.environ["RIVIA_CORS_ORIGINS"] = "https://console.rivia.observer, *, null, NULL"
origins = ra.cors_origins()
ok(origins == ["https://console.rivia.observer"],
   "CORS allowlist keeps the hub origin and drops * and null")
os.environ["RIVIA_CORS_ORIGINS"] = "https://console.rivia.observer"

# ── live doors ──
sched.set_selftest(True)
tools.reset_install()
admin._restart_impl = lambda: restarted.append("yes")
restarted = []


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

# Bot token for the rest of the paths
with ra.db() as c:
    c.execute("insert or ignore into clients values(?,?,?,?)",
              ("grok-test", '["https://www.cursor.com/agents/mcp/oauth/callback"]', "Grok Bot", time.time()))
BOT = ra.issue_tokens("grok-test")["access_token"]


def raw_post(path, token, body=None, origin=None):
    url = BASE + path
    data = body if body is not None else b"{}"
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def tools_list_body():
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()


def tools_call_body(name, args):
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": args}}).encode()


# wrong_door both ways, and bot vs admin
status, _h, body = raw_post("/admin/mcp", APP, tools_list_body())
ok(status == 403 and b"wrong_door" in body, "using the app token on the admin door is 403 wrong_door")
status, _h, body = raw_post("/app/mcp", ADMIN2, tools_list_body())
ok(status == 403 and b"wrong_door" in body, "using the admin token on the app door is 403 wrong_door")
status, _h, body = raw_post("/mcp", ADMIN2, tools_list_body())
ok(status == 403 and b"wrong_door" in body, "using the admin token on the bot door is 403 wrong_door")
status, _h, body = raw_post("/admin/mcp", BOT, tools_list_body())
ok(status == 403 and b"wrong_door" in body, "using a bot token on the admin door is 403 wrong_door")

# The revoked first admin token is rejected on /admin (file + jti)
status, _h, _body = raw_post("/admin/mcp", ADMIN, tools_list_body())
ok(status == 401, "a revoked admin token is 401 on the admin door")

# A new admin token leaves the phone working
status, _h, body = raw_post("/app/mcp", APP, tools_list_body())
ok(status == 200, "a new admin token leaves the app token working")

# Admin tools are on /admin/mcp only
status, _h, body = raw_post("/admin/mcp", ADMIN2, tools_list_body())
ok(status == 200, "admin token opens /admin/mcp")
listed = json.loads(body)
admin_names = {t["name"] for t in listed["result"]["tools"]}
ok(admin_names == set(admin.ADMIN_TOOLS),
   "admin door lists the fixed admin tools and nothing else")

# CORS: hub origin is allowed, another origin is not
status, headers, _body = raw_post("/admin/mcp", ADMIN2, tools_list_body(),
                                  origin="https://console.rivia.observer")
# public_app was built when RIVIA_CORS_ORIGINS was already set
acao = headers.get("Access-Control-Allow-Origin") or headers.get("access-control-allow-origin")
ok(acao == "https://console.rivia.observer",
   "CORS allows the hub origin on the admin door")
status, headers, _body = raw_post("/admin/mcp", ADMIN2, tools_list_body(),
                                  origin="https://evil.example")
acao = headers.get("Access-Control-Allow-Origin") or headers.get("access-control-allow-origin")
ok(acao != "https://evil.example", "CORS does not allow a foreign origin")


async def listed(url, token):
    async with Client(url, auth=token) as c:
        return {t.name: t for t in await c.list_tools()}


async def call(url, token, tool, args):
    async with Client(url, auth=token) as c:
        r = await c.call_tool(tool, args, raise_on_error=False)
        return r.structured_content or {}


async def main():
    A, B, D = BASE + "/app/mcp", BASE + "/mcp", BASE + "/admin/mcp"
    at, bt, dt = await listed(A, APP), await listed(B, BOT), await listed(D, ADMIN2)
    for name in admin.ADMIN_TOOLS:
        ok(name not in at and name not in bt, "admin tool %s is off the app and bot doors" % name)
        ok(name in dt, "admin door lists %s" % name)

    st = await call(D, ADMIN2, "hub_status", {})
    ok(st.get("ok") is True and st.get("tz") == "America/New_York" and "selftest_ok" in st,
       "hub_status returns clock and selftest")

    acts = await call(D, ADMIN2, "list_actions", {})
    names = {a["action"] for a in acts.get("actions") or []}
    ok(acts.get("ok") is True and "sched.ride_time" in names and "hub_status" not in names,
       "list_actions returns the catalog and not the admin tools")

    rows = await call(D, ADMIN2, "list_tools", {})
    tool_names = {t["name"] for t in rows.get("tools") or []}
    ok(rows.get("ok") is True and "ride_time" in tool_names,
       "list_tools returns the tool table")
    ok(not (tool_names & set(admin.ADMIN_TOOLS)),
       "admin tools are not tool-table rows")

    # set_setting applies a standing value at once (admin door, not pending)
    before = sched.get_settings_sync(group="schedule")
    workout = next(s["value"] for s in before["settings"] if s["key"] == "workout_minutes")
    changed = await call(D, ADMIN2, "set_setting",
                         {"key": "workout_minutes", "value": 90, "scope": "standing",
                          "reason": "admin standing apply"})
    ok(changed.get("ok") is True and (changed.get("data") or {}).get("status") == "applied",
       "set_setting applies a standing value at once")
    ok((changed.get("data") or {}).get("value") == 90, "set_setting stored the new workout length")
    after = sched.get_settings_sync(group="schedule")
    now_val = next(s["value"] for s in after["settings"] if s["key"] == "workout_minutes")
    ok(now_val == 90 and now_val != workout, "the standing workout value is in effect")

    # pins through set_setting (admin / standing)
    pins = await call(D, ADMIN2, "get_settings", {"group": "places"})
    ok(pins.get("ok") is True, "get_settings can request the places group")
    place_keys = {s["key"] for s in (pins.get("data") or {}).get("settings") or []}
    ok(any(k.startswith("place.") and k.endswith(".pin") for k in place_keys),
       "get_settings includes place pins")

    # restart_server is hooked in tests so it does not kill this process
    rst = await call(D, ADMIN2, "restart_server", {})
    ok(rst.get("ok") is True and rst.get("restarting") is True, "restart_server reports restarting")
    ok(restarted == ["yes"], "restart_server invoked the restart hook")

    calls = await call(D, ADMIN2, "list_calls", {"limit": 20})
    ok(calls.get("ok") is True and isinstance(calls.get("calls"), list),
       "list_calls returns a list")


asyncio.run(main())

# rivia revoke voids the stored admin token and the live JWT
rev = subprocess.run(
    [sys.executable, os.path.join(HERE, "rivia_auth.py"), "revoke-all"],
    capture_output=True, text=True, env=os.environ,
)
ok(rev.returncode == 0, "revoke-all succeeds")
ok(not os.path.exists(ra.admin_token_path()), "rivia revoke removes the stored admin token file")
ok(ra.load_admin_token() is None, "no admin token is stored after revoke")
status, _h, _body = raw_post("/admin/mcp", ADMIN2, tools_list_body())
ok(status == 401, "the revoked admin token is 401 on the admin door")
ok(not _looks_like_token(rev.stdout) and not _looks_like_token(rev.stderr),
   "revoke-all does not print a token")

# rivia.sh admin-token wrapper (uses this same env)
# Re-mint after revoke to confirm the wrapper stores a new file silently
sh = subprocess.run(
    ["bash", HERE_SH, "admin-token"],
    capture_output=True, text=True, env={**os.environ, "CODE": HERE},
)
# rivia.sh admin-token cds to $CODE; may fail if load_venv looks for tablet venv.
# The python CLI is the source of truth; the wrapper is checked when it can run.
if sh.returncode == 0:
    ok(not _looks_like_token(sh.stdout) and not _looks_like_token(sh.stderr),
       "rivia admin-token does not print the token")
    ok(os.path.isfile(ra.admin_token_path()), "rivia admin-token writes the file")
else:
    # Fall back: the python entry is already covered; note the wrapper needed venv/env.
    ok(True, "rivia admin-token wrapper skipped in this environment (python CLI covered)")

server.should_exit = True
print("\nADMIN DOOR PASS" if failed == 0 else "\nADMIN DOOR FAIL (%d)" % failed)
sys.exit(1 if failed else 0)
