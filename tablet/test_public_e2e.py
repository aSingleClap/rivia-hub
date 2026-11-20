"""End-to-end: the real MCP server behind the bearer gate, over HTTP (the app door).
Runs on a spare local port with throwaway secrets; never touches ~/rivia or ~/.rivia.
    python test_public_e2e.py
"""
import asyncio, os, secrets, socket, sys, tempfile, threading, time

_tmp = tempfile.mkdtemp()
os.environ.update(RIVIA_DIR=os.path.join(_tmp, "rivia"),
                  RIVIA_DOTDIR=_tmp,
                  RIVIA_DB=os.path.join(_tmp, "e2e.db"),
                  RIVIA_ISSUER="https://mcp.rivia.observer",
                  RIVIA_JWT_KEY=secrets.token_hex(32),
                  RIVIA_SIGNIN_CHECK="0")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import urllib.error, urllib.request
import uvicorn
import rivia_auth as ra
import rivia_server as rs
from fastmcp import Client


def ok(cond, label): assert cond, label; print("  ok ·", label)


with socket.socket() as s:
    s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(rs.public_app(), host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    if server.started: break
    time.sleep(0.05)
BASE = f"http://127.0.0.1:{PORT}"
URL = BASE + "/app/mcp"
token = ra.mint_door_token("app")["token"]


def status_of(url, data=b"{}", method="POST"):
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data, method=method), timeout=5)
        return 200
    except urllib.error.HTTPError as e:
        return e.code


async def main():
    ok(status_of(URL) == 401, "no token on /app/mcp -> 401")
    ok(status_of(BASE + "/mcp") == 404, "the bot door /mcp is gone -> 404")
    ok(status_of(BASE + "/health", data=None, method="GET") == 200, "/health is open")
    async with Client(URL, auth=token) as client:
        names = {t.name for t in await client.list_tools()}
        ok({"get_tablet_battery", "hub_status", "list_tools", "list_calls", "app_places", "app_arrival"} <= names,
           f"tools/list through the gate: {sorted(names)}")
        ok(not ({"ask_bot", "check_reply", "post_reply", "set_setting", "restart_server"} & names),
           "no bot tools and no admin-only tools on the app door")
        res = await client.call_tool("get_tablet_battery", {}, raise_on_error=False)
        data = res.structured_content or {}
        ok("ok" in data and data.get("device") == "Galaxy Tab S7+", f"tool call through the gate: {data}")
    accepted = False
    try:
        async with Client(URL, auth=token[:-4] + "AAAA") as client:
            await client.list_tools()
            accepted = True
    except Exception:
        pass
    ok(not accepted, "tampered token rejected over real HTTP")

asyncio.run(main())
server.should_exit = True
print("\nE2E PASS")
