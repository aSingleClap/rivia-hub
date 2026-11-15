"""Static checks for the Server Hub (public/index.html). No network, no tokens."""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(os.path.dirname(HERE), "public", "index.html")

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

ok("sessionStorage.setItem(TOKEN_KEY, config.token)" in src
   or re.search(r"sessionStorage\.setItem\(\s*TOKEN_KEY", src),
   "the admin token is stored in sessionStorage")
ok("sessionStorage.getItem(TOKEN_KEY)" in src or "sessionStorage.getItem('hub_token')" in src,
   "the admin token is read from sessionStorage")
ok("localStorage.setItem('hub_token'" not in src
   and "localStorage.setItem(TOKEN_KEY" not in src,
   "the admin token is never written to localStorage")
ok(not re.search(r"eyJ[A-Za-z0-9_-]{8,}", src),
   "the page does not hardcode a token")
ok("https://mcp.rivia.observer" in src, "the default server URL is the tablet tunnel")

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

print("\nHUB PASS" if failed == 0 else "\nHUB FAIL (%d)" % failed)
sys.exit(1 if failed else 0)
