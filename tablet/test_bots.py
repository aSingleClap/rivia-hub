"""End-to-end: the app asks a bot, the bot answers through post_reply, the app collects it.
Real server, both doors, a fake bot webhook on this device. Throwaway secrets and databases;
never touches ~/rivia or ~/.rivia, and never calls the real Ard.
    python test_bots.py
"""
import asyncio, base64, hashlib, http.server, json, os, secrets, socket, sqlite3, subprocess, sys, tempfile, threading, time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
_salt = secrets.token_bytes(16)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


HOOK_PORT = free_port()
os.environ.update(
    RIVIA_DIR=os.path.join(TMP, "rivia"),
    RIVIA_DB=os.path.join(TMP, "auth.db"), RIVIA_DOTDIR=TMP, RIVIA_MAILBOX_DB=os.path.join(TMP, "mailbox.db"),
    RIVIA_ISSUER="https://mcp.rivia.observer", RIVIA_JWT_KEY=secrets.token_hex(32),
    RIVIA_PASS_HASH=_salt.hex() + "$" + hashlib.scrypt(b"x" * 16, salt=_salt, n=2**14, r=8, p=1).hex(),
    RIVIA_TOTP_SECRET=base64.b32encode(secrets.token_bytes(20)).decode().rstrip("="),
    RIVIA_BOTS="ard", RIVIA_BOT_ARD_URL="http://127.0.0.1:%d/hook" % HOOK_PORT, RIVIA_BOT_ARD_KEY="crsr_test_key",
    RIVIA_SIGNIN_CHECK="0",
)
sys.path.insert(0, HERE)
import uvicorn
import rivia_auth as ra
import rivia_admin as admin
import rivia_server as rs
from fastmcp import Client


def ok(cond, label):
    assert cond, label
    print("  ok ·", label)


# ── a fake Ard webhook: records each POST; answers 500 when told to ──
hits, hook_mode = [], {"status": 200}


class Hook(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        hits.append({"auth": self.headers.get("authorization"), "body": json.loads(body)})
        self.send_response(hook_mode["status"])
        self.end_headers()
        self.wfile.write(b'{"id":"run-123"}')

    def log_message(self, *a):
        pass


threading.Thread(target=http.server.HTTPServer(("127.0.0.1", HOOK_PORT), Hook).serve_forever, daemon=True).start()

PORT = free_port()
server = uvicorn.Server(uvicorn.Config(rs.public_app(), host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    if server.started:
        break
    time.sleep(0.05)
BASE = "http://127.0.0.1:%d" % PORT


def mint():
    r = subprocess.run([sys.executable, os.path.join(HERE, "rivia_auth.py"), "mint-app-token", "30"],
                       capture_output=True, text=True, env=os.environ)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


old_app_token = mint()
APP = mint()  # the second mint revokes the first
with ra.db() as c:
    c.execute("insert into clients values(?,?,?,?)",
              ("grok-test", '["https://www.cursor.com/agents/mcp/oauth/callback"]', "Grok Bot", time.time()))
BOT = ra.issue_tokens("grok-test")["access_token"]


async def call(url, token, tool, args):
    async with Client(url, auth=token) as c:
        r = await c.call_tool(tool, args, raise_on_error=False)
        return r.structured_content or {}


async def tools(url, token):
    async with Client(url, auth=token) as c:
        return {t.name: t for t in await c.list_tools()}


async def refused(url, token):
    try:
        async with Client(url, auth=token) as c:
            await c.list_tools()
        return False
    except Exception:
        return True


async def main():
    A, B = BASE + "/app/mcp", BASE + "/mcp"
    at, bt = await tools(A, APP), await tools(B, BOT)
    ok(set(at) == {"get_tablet_battery", "ask_bot", "check_reply", "ride_time", "plan_day", "get_schedule_settings", "change_schedule_setting", "undo_schedule_change", "app_places", "app_arrival", "app_log_ride"}, "app door lists ask_bot, check_reply, battery, ride_time, plan_day, settings, and app_places")
    ok("ard: Scheduling" in at["ask_bot"].description, "ask_bot tells Gemini which bots exist and what they do")
    ok("first person" in at["ask_bot"].description and "never naming the bot" in at["ask_bot"].description,
       "ask_bot tells Gemini to speak as Rivia, never naming the bot")
    ok(set(bt) == {"get_tablet_battery", "post_reply"}, "bot door lists post_reply, battery")
    ok(not (set(at) & set(admin.ADMIN_TOOLS)) and not (set(bt) & set(admin.ADMIN_TOOLS)),
       "admin tools stay off the app and bot doors")
    ok(await refused(B, APP), "app token refused on the bot door")
    ok(await refused(A, BOT), "bot token refused on the app door")
    ok(await refused(A, old_app_token), "a re-minted app token kills the old one")

    r = await call(A, APP, "ask_bot", {"bot": "ard", "request": "What does tomorrow look like? Don't write anything."})
    ticket = r.get("ticket", "")
    ok(r.get("ok") and r.get("status") == "sent" and ticket.startswith("t-"), "ask_bot returns at once with a ticket")
    h = hits[-1]
    ok(h["auth"] == "Bearer crsr_test_key" and h["body"]["ticket"] == ticket and "tomorrow" in h["body"]["request"],
       "Ard's webhook got the ticket and request, with its key")
    ok((await call(A, APP, "check_reply", {"ticket": ticket})).get("status") == "pending", "check_reply: pending")

    envelope = {"v": 1, "kind": "plan", "ok": True, "ref": ticket, "message": None, "data": {"written": False}}
    ok((await call(B, BOT, "post_reply", {"ticket": ticket, "reply": envelope})).get("stored"), "bot's post_reply stored")
    again = await call(B, BOT, "post_reply", {"ticket": ticket, "reply": envelope})
    ok(again.get("error", {}).get("code") == "already_answered", "a second reply for the same ticket is refused")
    ok((await call(B, BOT, "post_reply", {"ticket": "t-nope", "reply": envelope})).get("error", {}).get("code")
       == "unknown_ticket", "a reply to an unknown ticket is refused")
    ok((await call(B, BOT, "post_reply", {"ticket": ticket, "reply": "Sure! Tomorrow you..."})).get("error", {}).get("code")
       == "not_json", "a prose reply is refused")
    got = await call(A, APP, "check_reply", {"ticket": ticket})
    ok(got.get("status") == "answered" and got.get("reply") == envelope, "check_reply returns the bot's JSON unchanged")

    ok((await call(A, APP, "ask_bot", {"bot": "nobody", "request": "hi"})).get("error", {}).get("code") == "unknown_bot",
       "unknown bot refused")
    hook_mode["status"] = 500
    bad = await call(A, APP, "ask_bot", {"bot": "ard", "request": "plan tomorrow"})
    ok(bad.get("error", {}).get("code") == "send_failed", "a webhook that fails is reported, not hidden")
    ok((await call(A, APP, "check_reply", {"ticket": bad["ticket"]})).get("status") == "failed",
       "check_reply on a failed send says failed")
    hook_mode["status"] = 200

    slow = (await call(A, APP, "ask_bot", {"bot": "ard", "request": "plan Monday"}))["ticket"]
    con = sqlite3.connect(os.environ["RIVIA_MAILBOX_DB"])
    con.execute("update tickets set created=created-3600 where ticket=?", (slow,))
    con.commit(); con.close()
    exp = await call(A, APP, "check_reply", {"ticket": slow})
    ok(exp.get("ok") is False and exp.get("status") == "expired", "no answer in 15 minutes: expired, never guessed")
    ok((await call(B, BOT, "post_reply", {"ticket": slow, "reply": envelope})).get("stored"), "a late answer is still kept")
    ok((await call(A, APP, "check_reply", {"ticket": slow})).get("status") == "answered", "and the app can still collect it")

    # The phone's client: one plain JSON POST per call, no MCP library, no session.
    def raw(method, params, token=APP):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(A, data=body, method="POST", headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.headers.get("content-type", ""), json.loads(r.read())
    ctype, listed = raw("tools/list", {})
    ok(ctype.startswith("application/json") and {t["name"] for t in listed["result"]["tools"]} ==
       {"get_tablet_battery", "ask_bot", "check_reply", "ride_time", "plan_day", "get_schedule_settings", "change_schedule_setting", "undo_schedule_change", "app_places", "app_arrival", "app_log_ride"}, "plain POST tools/list works with no session (app client)")
    _, res = raw("tools/call", {"name": "check_reply", "arguments": {"ticket": ticket}})
    sc = res["result"].get("structuredContent") or json.loads(res["result"]["content"][0]["text"])
    ok(sc.get("status") == "answered" and sc.get("reply") == envelope, "plain POST tools/call returns the answer as JSON")


asyncio.run(main())
server.should_exit = True
print("\nBOTS PASS")
