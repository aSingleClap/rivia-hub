"""Static checks for the Server Hub (public/index.html). No network, no tokens.

Skips cleanly when the hub page is not in this checkout (the tablet copy
of rivia.sh may live next to tablet/ only).
    python test_hub.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(os.path.dirname(HERE), "public", "index.html")

if not os.path.isfile(PATH):
    print("HUB SKIP · public/index.html is not present")
    sys.exit(0)

failed = 0


def ok(cond, label):
    global failed
    if cond:
        print("  ok ·", label)
    else:
        failed += 1
        print("FAIL ·", label)


src = open(PATH, encoding="utf-8").read()
ok(os.path.isfile(PATH) and len(src) > 1000, "public/index.html is present")

ok("sessionStorage.setItem(TOKEN_KEY, token)" in src
   or re.search(r"sessionStorage\.setItem\(\s*TOKEN_KEY", src),
   "the admin token can be stored in sessionStorage")
ok("tokenFromStores" in src and "sessionStorage.getItem(TOKEN_KEY)" in src,
   "the admin token is read from sessionStorage (and localStorage only if Remember is on)")
ok("function persistToken" in src and "if (remember) localStorage.setItem(TOKEN_KEY" in src,
   "Remember on this device writes the token to localStorage only when checked")
ok("rememberToken" in src and "Remember on this device" in src,
   "Connect has a Remember on this device checkbox")
ok("function forgetToken" in src and "clearStoredToken" in src
   and "localStorage.removeItem(TOKEN_KEY)" in src
   and "sessionStorage.removeItem(TOKEN_KEY)" in src,
   "Forget clears the token from sessionStorage and localStorage")
ok("if (typed) config.token = typed" in src,
   "Save does not wipe a stored token when the password field is empty after a refresh")
ok("localStorage.setItem('hub_token'" not in src,
   "the token is not written to localStorage except through persistToken / Remember")
ok(not re.search(r"eyJ[A-Za-z0-9_-]{8,}", src),
   "the page does not hardcode a token")
ok("https://mcp.rivia.observer" in src, "the default server URL is the tablet tunnel")
ok("https://hub.rivia.observer" in src,
   "errors mention the live hub origin for CORS")

ok("function restart()" in src and "restart_server" in src,
   "Restart calls the admin restart_server tool")
ok("pollHealth" in src and "/health" in src and "HEALTH_WAIT_MS" in src,
   "Restart polls /health after requesting the restart")
ok("setTimeout(() => setStatus('running'), 500)" not in src,
   "the fake 500 ms restart animation is gone")
ok("onclick=\"setStatus('running')\"" not in src and "function setStatus(" not in src,
   "the fake Start and Stop buttons are gone")

for name in ("Status", "Settings", "Pending", "Logs", "Restart"):
    ok(name in src, "the hub includes a %s card or control" % name)

ok("asked by" in src or "item.asked" in src, "Pending shows who asked")
ok("place." in src or "lat,lng" in src or "pin-grid" in src, "Settings can edit pins")
ok("/admin/mcp" in src and "adminMcpUrl" in src, "the hub talks to /admin/mcp")
ok("Connect with an admin token to see Status" in src
   or "Connect with an admin token" in src,
   "with no token the cards stay empty")
ok(".replace(/&/g, '&amp;')" in src and ".replace(/</g, '&lt;')" in src,
   "escapeHtml actually escapes markup")
ok("hasToken()" in src and "no_token" in src, "admin calls are skipped when no token is set")

ok("MCP_TIMEOUT_MS" in src and "AbortController" in src and "Timed out waiting for /admin/mcp" in src,
   "admin MCP calls have a timeout and a clear error")
ok("parseMcpBody" in src and "text/event-stream" in src and "data:" in src,
   "the hub parses JSON and SSE MCP bodies")
ok("method: 'initialize'" in src and "notifications/initialized" in src,
   "the hub sends MCP initialize and notifications/initialized")
ok("Mcp-Session-Id" in src and "mcp-session-id" in src and "Mcp-Protocol-Version" in src,
   "the hub sends and stores Mcp-Session-Id and Mcp-Protocol-Version")
ok("Health is 200. Admin status loaded." in src
   and "Health is 200, but admin status failed" in src,
   "the health banner is updated after admin status succeeds or fails, not left on Loading")
ok("mcpCall('hub_status'" in src and "mcpCall('get_settings'" in src
   and "mcpCall('list_changes'" in src and "mcpCall('list_calls'" in src
   and "mcpCall('restart_server'" in src,
   "Status, Settings, Pending, Logs, and Restart share the same MCP helper")

print("\nHUB PASS" if failed == 0 else "\nHUB FAIL (%d)" % failed)
sys.exit(1 if failed else 0)
