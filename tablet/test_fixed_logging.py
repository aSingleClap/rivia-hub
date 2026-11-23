"""Emotion spec Step 4.1: the fixed app-door tools and the emotion button go in the call log, and a restart
from the hub writes its own line to watch.log first. Runs the real server on a free local port.
    python test_fixed_logging.py
"""
import json
import os
import secrets
import socket
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

import rivia_admin as admin  # noqa: E402
import rivia_auth as ra  # noqa: E402
import rivia_server as rs  # noqa: E402
import rivia_tools as tools  # noqa: E402
import uvicorn  # noqa: E402

failed = 0


def ok(cond, label):
    global failed
    print(("  ok · " if cond else "FAIL · ") + label)
    if not cond:
        failed += 1


# ---- log_fixed on its own ----
def good() -> dict:
    """doc"""
    return {"ok": True, "x": 1}


def bad_str() -> dict:
    return {"ok": False, "error": "could not read"}


def bad_code() -> dict:
    return {"ok": False, "error": {"code": "calendar_http", "msg": "m"}}


def boom() -> dict:
    raise ValueError("x")


def before():
    return len(tools.list_call_rows(limit=500))


w = tools.log_fixed(good, "app")
ok(w.__name__ == "good" and w.__doc__ == "doc", "the wrapper keeps the tool's name and description")
n0 = before()
ok(w() == {"ok": True, "x": 1}, "the reply is unchanged")
row = tools.list_call_rows(limit=1)[0]
ok(before() == n0 + 1 and row["tool"] == "good" and row["ok"] and row["code"] is None
   and row["action"] == "fixed.good" and row["door"] == "app" and row["ms"] >= 0, "a good call is logged")
tools.log_fixed(bad_str, "app")()
ok(tools.list_call_rows(limit=1)[0]["code"] == "unreadable", "an error that isn't a code dict logs unreadable")
tools.log_fixed(bad_code, "app")()
ok(tools.list_call_rows(limit=1)[0]["code"] == "calendar_http", "an error code is logged as is")
try:
    tools.log_fixed(boom, "app")()
    raised = False
except ValueError:
    raised = True
r = tools.list_call_rows(limit=1)[0]
ok(raised and r["code"] == "internal" and not r["ok"], "an exception is raised as before and logged internal")
real = tools.log_call
tools.log_call = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
ok(tools.log_fixed(good, "app")() == {"ok": True, "x": 1}, "a broken call log never breaks the tool")
tools.log_call = real

# ---- the real server ----
tools.reset_install()
admin._restart_impl = lambda: None
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(rs.public_app(), host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    if server.started:
        break
    time.sleep(0.05)
ok(server.started, "real server started")
BASE = "http://127.0.0.1:%d" % PORT
APP = ra.mint_door_token("app", 1)["token"]
ADMIN = ra.mint_door_token("admin", 1)["token"]


def post(path, token, payload, extra=None):
    h = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    h.update(extra or {})
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def get(path, token):
    req = urllib.request.Request(BASE + path, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def call(door, token, name, args=None):
    return post("/%s/mcp" % door, token, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                          "params": {"name": name, "arguments": args or {}}})


def listed(door, token):
    st, body = post("/%s/mcp" % door, token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    return {t["name"]: t for t in json.loads(body)["result"]["tools"]}


app_tools = listed("app", APP)
ok({"get_tablet_battery", "hub_status", "list_tools", "list_calls"} <= set(app_tools),
   "the app door still lists the fixed tools")
ok("limit" in json.dumps(app_tools["list_calls"].get("inputSchema", {})),
   "list_calls keeps its limit argument through the wrapper")
ok("NOT the phone" in (app_tools["get_tablet_battery"].get("description") or ""),
   "get_tablet_battery keeps its description through the wrapper")
ok(not [n for n in app_tools if "emotion" in n], "still no emotion tool on the app door")

seen = {}
for name, args in (("get_tablet_battery", {}), ("hub_status", {}), ("list_tools", {}), ("list_calls", {"limit": 5})):
    st, body = call("app", APP, name, args)
    seen[name] = st
ok(all(v == 200 for v in seen.values()), "each fixed tool answers on the app door")
rows = tools.list_call_rows(limit=50)
app_fixed = {r_["tool"] for r_ in rows if r_["door"] == "app" and r_["action"].startswith("fixed.")}
ok({"get_tablet_battery", "hub_status", "list_tools", "list_calls"} <= app_fixed,
   "every app-door fixed tool is in the call log")
batt = [r_ for r_ in rows if r_["tool"] == "get_tablet_battery"][0]
if os.path.exists("/sys/class/power_supply/battery/capacity"):
    ok(batt["ok"] is True and batt["code"] is None, "battery call logged")
else:
    ok(batt["ok"] is False and batt["code"] == "unreadable",
       "a battery the server can't read logs as a failure (no battery file on this machine)")

n1 = before()
st, _ = call("admin", ADMIN, "hub_status")
ok(st == 200 and before() == n1, "the admin door's hub_status (the Server Hub's) is not logged")

n2 = before()
st, _ = get("/app/emotion", APP)
r_ = tools.list_call_rows(limit=1)[0]
ok(st == 200 and before() == n2 + 1 and r_["tool"] == "app_emotion" and r_["door"] == "app" and r_["ok"]
   and r_["action"] == "route.emotion", "a button read is logged as app_emotion")
st, _ = post("/app/emotion", APP, {"nudge": {"dv": 0.1}, "door": "admin"})
r_ = tools.list_call_rows(limit=1)[0]
ok(st == 400 and not r_["ok"] and r_["code"] == "http_400", "a refused press logs http_400, not a failure of hers")
st, _ = get("/app/emotion", ADMIN)
ok(st == 403, "the admin token still gets 403 on the button")
ok(not any(a.startswith("route.") or a.startswith("fixed.") for a in tools.ACTIONS),
   "nothing was added to the tool table's actions")

# ---- planned restart line ----
admin.note_planned_restart()
wl = os.path.join(os.environ["RIVIA_DIR"], "logs", "watch.log")
line = open(wl).read().strip().splitlines()[-1]
ok(line.endswith(admin.PLANNED_RESTART_LINE) and len(line.split(" ", 2)[0]) == 10,
   "note_planned_restart writes a timestamped line to watch.log")
ok("restarting" not in line, "the planned line itself never reads as a restart")
st, body = call("admin", ADMIN, "restart_server")
ok(st == 200, "restart_server still answers (hooked here, so nothing exits)")

server.should_exit = True
print("FAILED: %d" % failed if failed else "all passed")
sys.exit(1 if failed else 0)
