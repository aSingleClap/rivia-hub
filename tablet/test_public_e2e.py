"""End-to-end: the real MCP server behind the OAuth gate, over HTTP.
Runs on a spare local port with throwaway secrets; never touches ~/rivia/auth.db.
    python test_public_e2e.py
"""
import asyncio, base64, hashlib, os, secrets, socket, sys, tempfile, threading, time

_salt = secrets.token_bytes(16)
_tmp = tempfile.mkdtemp()
os.environ.update(RIVIA_DIR=os.path.join(_tmp, "rivia"),
                  RIVIA_DOTDIR=_tmp,
                  RIVIA_DB=os.path.join(_tmp, "e2e.db"),
                  RIVIA_ISSUER="https://mcp.rivia.observer",
                  RIVIA_JWT_KEY=secrets.token_hex(32),
                  RIVIA_PASS_HASH=_salt.hex() + "$" + hashlib.scrypt(b"x" * 16, salt=_salt, n=2**14, r=8, p=1).hex(),
                  RIVIA_TOTP_SECRET=base64.b32encode(secrets.token_bytes(20)).decode().rstrip("="),
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
URL = f"http://127.0.0.1:{PORT}/mcp"

# a client registered the way Grok Bot does, and a token as the consent flow would issue
with ra.db() as c:
    c.execute("insert into clients values(?,?,?,?)", ("rivia-e2e", '["https://www.cursor.com/agents/mcp/oauth/callback"]', "Grok Bot", time.time()))
token = ra.issue_tokens("rivia-e2e")["access_token"]


async def main():
    try:
        urllib.request.urlopen(urllib.request.Request(URL, data=b"{}", method="POST"), timeout=5)
        ok(False, "no token -> 401")
    except urllib.error.HTTPError as e:
        ok(e.code == 401 and "resource_metadata" in (e.headers.get("www-authenticate") or ""), "no token -> 401 + discovery pointer")
    async with Client(URL, auth=token) as client:
        names = [t.name for t in await client.list_tools()]
        ok(sorted(names) == ["get_tablet_battery", "post_reply"], f"tools/list through the gate: {names}")
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
