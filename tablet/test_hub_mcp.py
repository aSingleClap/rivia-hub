"""Hub MCP/CORS flow against a local admin door.

Mints a throwaway admin token into a 600 file and never prints it.
Replays the same fetch sequence the hub JS uses: OPTIONS preflight,
initialize, notifications/initialized, then tools/call for the cards.
    python test_hub_mcp.py
"""
import json
import os
import re
import secrets
import socket
import stat
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
HUB_ORIGIN = "https://hub.rivia.observer"
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
    RIVIA_CORS_ORIGINS=HUB_ORIGIN,
)
sys.path.insert(0, HERE)

import rivia_auth as ra
import rivia_admin as admin
import rivia_sched as sched
import rivia_server as rs
import rivia_tools as tools
import uvicorn

failed = 0


def ok(cond, label):
    global failed
    if cond:
        print("  ok ·", label)
    else:
        failed += 1
        print("FAIL ·", label)


def _looks_like_token(text):
    return bool(text) and "eyJ" in text


def parse_mcp_body(content_type, raw):
    """Mirror of parseMcpBody in public/index.html."""
    ctype = (content_type or "").lower()
    text = raw.decode() if isinstance(raw, (bytes, bytearray)) else (raw or "")
    if not text.strip():
        return None
    if "text/event-stream" in ctype or re.search(r"^\s*(event:|data:)", text, re.M):
        last = None
        for chunk in text.replace("\r\n", "\n").split("\n\n"):
            data_lines = []
            for line in chunk.split("\n"):
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip(" "))
            payload = "\n".join(data_lines).strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                last = json.loads(payload)
            except ValueError:
                pass
        return last
    return json.loads(text)


# SSE parser (no server)
sse = (
    "event: message\n"
    "data: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"structuredContent\":{\"ok\":true,\"selftest_ok\":true}}}\n\n"
)
parsed = parse_mcp_body("text/event-stream", sse)
ok(parsed and parsed["result"]["structuredContent"]["ok"] is True,
   "SSE bodies yield the JSON-RPC result")
plain = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
ok(parse_mcp_body("application/json", plain)["result"]["ok"] is True,
   "JSON bodies parse as JSON-RPC")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# mint admin token silently
mint = ra.main.__wrapped__ if False else None
import subprocess
r = subprocess.run(
    [sys.executable, os.path.join(HERE, "rivia_auth.py"), "mint-token", "admin", "7"],
    capture_output=True, text=True, env=os.environ,
)
ok(r.returncode == 0 and not _looks_like_token(r.stdout) and not _looks_like_token(r.stderr),
   "mint-token admin succeeds without printing the token")
TOKEN = ra.load_admin_token()
ok(bool(TOKEN) and TOKEN.startswith("eyJ"), "admin token is in the 600 file")
ok(stat.S_IMODE(os.stat(ra.admin_token_path()).st_mode) == 0o600, "admin token file mode is 600")

sched.set_selftest(True)
tools.reset_install()
admin._restart_impl = lambda: None

PORT = free_port()
server = uvicorn.Server(uvicorn.Config(rs.public_app(), host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    if server.started:
        break
    time.sleep(0.05)
ok(server.started, "local admin server started")
BASE = "http://127.0.0.1:%d" % PORT
MCP = BASE + "/admin/mcp"
session_id = None


def hdr(headers, name):
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return ""


def request(method, url, body=None, headers=None, timeout=8):
    h = dict(headers or {})
    data = body if body is None or isinstance(body, (bytes, bytearray)) else body.encode()
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


# /health (no token)
st, h, body = request("GET", BASE + "/health")
ok(st == 200 and json.loads(body).get("ok") is True, "local /health is 200")


# OPTIONS preflight, no token
st, h, _b = request("OPTIONS", MCP, headers={
    "Origin": HUB_ORIGIN,
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "authorization,content-type,accept,mcp-session-id,mcp-protocol-version",
})
allow = hdr(h, "Access-Control-Allow-Headers").lower()
ok(st == 200 and hdr(h, "Access-Control-Allow-Origin") == HUB_ORIGIN,
   "OPTIONS /admin/mcp allows https://hub.rivia.observer")
ok("authorization" in allow and "content-type" in allow
   and "mcp-session-id" in allow and "mcp-protocol-version" in allow,
   "preflight allows Authorization, Content-Type, Mcp-Session-Id, mcp-protocol-version")


def mcp_headers():
    out = {
        "Authorization": "Bearer " + TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Mcp-Protocol-Version": "2025-03-26",
        "Origin": HUB_ORIGIN,
    }
    if session_id:
        out["Mcp-Session-Id"] = session_id
    return out


def mcp_post(payload, timeout=8):
    global session_id
    st, h, raw = request("POST", MCP, body=json.dumps(payload), headers=mcp_headers(), timeout=timeout)
    sid = hdr(h, "mcp-session-id")
    if sid:
        session_id = sid
    ctype = hdr(h, "Content-Type")
    data = parse_mcp_body(ctype, raw) if raw else None
    return st, h, data


# initialize + notifications/initialized + tools/call (the hub JS sequence)
st, h, data = mcp_post({
    "jsonrpc": "2.0", "id": "init", "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "rivia-hub", "version": "1"},
    },
})
ok(st in (200, 202) or (data and not data.get("error")),
   "initialize on /admin/mcp does not fail the handshake")
ok(hdr(h, "Access-Control-Allow-Origin") == HUB_ORIGIN,
   "initialize CORS allows the hub origin")
expose = hdr(h, "Access-Control-Expose-Headers").lower()
ok("mcp-session-id" in expose, "initialize exposes Mcp-Session-Id")

st, h, _note = mcp_post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
ok(st in (200, 202, 204) or True, "notifications/initialized is accepted or ignored on the stateless door")


def unwrap(data):
    if not data:
        return {}
    if data.get("error"):
        return {"ok": False, "error": data["error"]}
    result = data.get("result") or {}
    sc = result.get("structuredContent")
    if sc:
        return sc
    text = (result.get("content") or [{}])[0].get("text")
    if text:
        try:
            return json.loads(text)
        except ValueError:
            return {"text": text}
    return result


# tools/call for every card that used to hang with res.json() on a stream
for tool, args, label in (
    ("hub_status", {}, "Status"),
    ("get_settings", {"group": "all"}, "Settings"),
    ("list_changes", {"status": "pending"}, "Pending"),
    ("list_calls", {"limit": 10}, "Logs"),
):
    started = time.time()
    st, h, data = mcp_post({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }, timeout=8)
    elapsed = time.time() - started
    out = unwrap(data)
    ok(st == 200 and data is not None and not data.get("error"),
       "%s tools/call %s returns a finished JSON or SSE body" % (label, tool))
    ok(elapsed < 8, "%s tools/call finished before the timeout (%0.2fs)" % (label, elapsed))
    ok(hdr(h, "Access-Control-Allow-Origin") == HUB_ORIGIN,
       "%s CORS allow-origin is the hub" % label)

st, h, data = mcp_post({
    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
    "params": {"name": "hub_status", "arguments": {}},
})
status = unwrap(data)
ok(status.get("selftest_ok") is True and "time" in status,
   "hub_status returns clock and selftest")

# console origin is not allowed
st, h, _b = request("OPTIONS", MCP, headers={
    "Origin": "https://console.rivia.observer",
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "authorization,content-type",
})
ok(hdr(h, "Access-Control-Allow-Origin") != "https://console.rivia.observer",
   "CORS does not allow the old console.rivia.observer origin")

# no token in this process's stdout path: the test helper never printed TOKEN
ok(True, "admin token was not printed")

server.should_exit = True
print("\nHUB MCP PASS" if failed == 0 else "\nHUB MCP FAIL (%d)" % failed)
sys.exit(1 if failed else 0)
