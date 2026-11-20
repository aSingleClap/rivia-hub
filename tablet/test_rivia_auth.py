"""Door auth (rivia_auth) after the core split: app and admin tokens, the bearer gate, and the
removed bot door. Stdlib only, throwaway secrets: python test_rivia_auth.py"""
import asyncio, json, os, secrets, subprocess, sys, tempfile, time

_DOT = tempfile.mkdtemp()
os.environ.update(RIVIA_DB=os.path.join(_DOT, "t.db"), RIVIA_DOTDIR=_DOT,
                  RIVIA_JWT_KEY=secrets.token_hex(32), RIVIA_CORS_ORIGINS="https://hub.example")
for k in ("RIVIA_PASS_HASH", "RIVIA_TOTP_SECRET"):
    os.environ.pop(k, None)  # no longer needed: the consent page is gone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jwt
import rivia_auth as ra

HERE = os.path.dirname(os.path.abspath(__file__))


async def door(scope, receive, send):
    from starlette.responses import JSONResponse
    await JSONResponse({"ok": True, "path": scope["path"], "client": scope["state"]["claims"]["client_id"]})(
        scope, receive, send)


APP = ra.build_app(app_mcp=door, admin_mcp=door)


class R:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.text = status, headers, body.decode()

    def json(self):
        return json.loads(self.text)


def call(method, path, token=None, headers=None):
    hdrs = [(b"host", b"mcp.rivia.observer"), (b"content-type", b"application/json"), (b"content-length", b"2")]
    if token:
        hdrs.append((b"authorization", ("Bearer " + token).encode()))
    for k, v in (headers or {}).items():
        hdrs.append((k.lower().encode(), v.encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "https", "path": path, "raw_path": path.encode(), "query_string": b"",
             "root_path": "", "headers": hdrs, "server": ("mcp.rivia.observer", 443), "client": ("1.2.3.4", 1)}
    sent, out = [False], {"status": 0, "headers": {}, "body": b""}

    async def receive():
        if sent[0]:
            return {"type": "http.disconnect"}
        sent[0] = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(m):
        if m["type"] == "http.response.start":
            out["status"] = m["status"]
            out["headers"] = {k.decode().lower(): v.decode() for k, v in m["headers"]}
        elif m["type"] == "http.response.body":
            out["body"] += m.get("body", b"")
    asyncio.run(APP(scope, receive, send))
    return R(out["status"], out["headers"], out["body"])


def ok(cond, label):
    assert cond, label
    print("  ok ·", label)


def forged(client_id="rivia-app", kind="access", exp_in=3600, key=None, listed=True):
    now = int(time.time())
    jti = secrets.token_urlsafe(16)
    claims = {"iss": ra.ISSUER, "aud": ra.RESOURCE, "sub": "owner", "client_id": client_id, "jti": jti,
              "iat": now, "exp": now + exp_in, "kind": kind, "scope": ra.SCOPE}
    if listed:
        with ra.db() as c:
            c.execute("insert into tokens values(?,?,?,?,0)", (jti, client_id, kind, now + exp_in))
    return jwt.encode(claims, key or os.environ["RIVIA_JWT_KEY"], algorithm="HS256")


# 1. no token: the doors are 401, anything else is 404 (the bot door and OAuth are gone)
for p in ("/app/mcp", "/admin/mcp"):
    r = call("POST", p)
    ok(r.status == 401 and r.headers.get("www-authenticate") == "Bearer", "POST %s without a token -> 401" % p)
for m, p in (("POST", "/mcp"), ("GET", "/"), ("GET", "/authorize"), ("POST", "/token"), ("POST", "/register"),
             ("GET", "/.well-known/oauth-protected-resource/mcp"), ("GET", "/.well-known/oauth-authorization-server")):
    ok(call(m, p).status == 404, "%s %s -> 404 (removed)" % (m, p))
ok(call("GET", "/health").status == 200 and call("GET", "/health").json() == {"ok": True}, "/health is open")

# 2. the app token: its own door only
app = ra.mint_door_token("app")
ok(app["client_id"] == ra.APP_CLIENT and app["days"] == ra.APP_TOKEN_DAYS and app["path"] is None,
   "mint app token: 365 days, not written to a file")
r = call("POST", "/app/mcp", app["token"])
ok(r.status == 200 and r.json()["client"] == ra.APP_CLIENT, "the app token opens /app/mcp")
r = call("POST", "/admin/mcp", app["token"])
ok(r.status == 403 and r.json()["error"] == "wrong_door", "the app token on /admin/mcp -> 403 wrong_door")
ok(call("POST", "/mcp", app["token"]).status == 404, "the app token finds no bot door (404)")
app2 = ra.mint_door_token("app")
ok(app2["revoked"] == 1 and call("POST", "/app/mcp", app["token"]).status == 401,
   "a new app token revokes the old one (401)")
ok(call("POST", "/app/mcp", app2["token"]).status == 200, "the new app token works")

# 3. the admin token: also has to match the file on the tablet
adm = ra.mint_door_token("admin")
ok(adm["path"] and os.stat(adm["path"]).st_mode & 0o777 == 0o600, "admin token saved mode 600")
ok(call("POST", "/admin/mcp", adm["token"]).status == 200, "the admin token opens /admin/mcp")
ok(call("POST", "/app/mcp", adm["token"]).status == 403, "the admin token on /app/mcp -> 403")
os.remove(adm["path"])
ok(call("POST", "/admin/mcp", adm["token"]).status == 401, "an admin JWT without the stored file -> 401")
adm = ra.mint_door_token("admin")
ok(call("POST", "/app/mcp", app2["token"]).status == 200, "minting an admin token leaves the app token alone")

# 4. bad tokens
ok(call("POST", "/app/mcp", forged(exp_in=-60)).status == 401, "an expired token -> 401")
ok(call("POST", "/app/mcp", forged(kind="refresh")).status == 401, "a refresh-kind token -> 401")
ok(call("POST", "/app/mcp", forged(key=secrets.token_hex(32))).status == 401, "a token signed with another key -> 401")
ok(call("POST", "/app/mcp", forged(listed=False)).status == 401, "a token not listed in auth.db -> 401")
bot = forged(client_id="grok-test")
ok(call("POST", "/app/mcp", bot).status == 403 and call("POST", "/admin/mcp", bot).status == 403,
   "an old bot token opens neither door (403)")
ok(ra.token_fits_path("grok-test", "/mcp") is False and ra.token_fits_path(ra.APP_CLIENT, "/app/mcp"),
   "token_fits_path allows only the door's own prefix")

# 5. /app/login: POST only, no CORS (the sign-in itself is tested in test_app_login.py)
ok(ra.is_open_path("/app/login", "POST") and not ra.is_open_path("/app/login", "GET")
   and not ra.is_open_path("/app/loginx", "POST") and not ra.is_open_path("/app/mcp", "POST"),
   "only a POST to exactly /app/login is open")

# 6. CORS for the hub's origin on the doors
pre = {"origin": "https://hub.example", "access-control-request-method": "POST",
       "access-control-request-headers": "authorization,content-type"}
r = call("OPTIONS", "/admin/mcp", headers=pre)
ok(r.status == 200 and r.headers.get("access-control-allow-origin") == "https://hub.example",
   "the hub's preflight to /admin/mcp is answered")
r = call("OPTIONS", "/admin/mcp", headers=dict(pre, origin="https://evil.example"))
ok(r.headers.get("access-control-allow-origin") is None, "another origin gets no CORS headers")

# 7. revoke-all and the CLI
cli_env = dict(os.environ)
r = subprocess.run([sys.executable, os.path.join(HERE, "rivia_auth.py"), "revoke-all"],
                   capture_output=True, text=True, env=cli_env)
ok(r.returncode == 0 and "Revoked" in r.stdout, "revoke-all runs")
ok(call("POST", "/app/mcp", app2["token"]).status == 401 and call("POST", "/admin/mcp", adm["token"]).status == 401
   and not os.path.exists(adm["path"]), "revoke-all voids every token and clears the stored admin token")
r = subprocess.run([sys.executable, os.path.join(HERE, "rivia_auth.py"), "setup"], capture_output=True, text=True,
                   env=cli_env)
ok(r.returncode == 0 and "RIVIA_JWT_KEY=" in r.stdout and "TOTP" not in r.stdout and "PASS_HASH" not in r.stdout,
   "setup prints only the JWT key (no passphrase or TOTP any more)")
r = subprocess.run([sys.executable, os.path.join(HERE, "rivia_auth.py"), "mint-token", "admin"],
                   capture_output=True, text=True, env=cli_env)
ok(r.returncode == 0 and "eyJ" not in r.stdout + r.stderr, "the admin mint never prints the token")

print("\nAUTH PASS")
