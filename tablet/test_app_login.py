"""POST /app/login: the app's Google sign-in -> the tablet's Google token + the app token.
Fake Google token endpoint and throwaway secrets; never touches ~/rivia or ~/.rivia.
    python test_app_login.py
"""
import asyncio, base64, contextlib, glob, hashlib, io, json, os, secrets, stat, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode

_salt = secrets.token_bytes(16)
TMP = tempfile.mkdtemp()
MAIN_ID, MAIN_SECRET = "desktop-client.apps.googleusercontent.com", "main-secret-" + secrets.token_hex(8)
WEB_ID, WEB_SECRET = "web-client.apps.googleusercontent.com", "web-secret-" + secrets.token_hex(8)
ANDROID_ID = "android-client.apps.googleusercontent.com"
EMAIL = "owner@example.com"
HUB = "https://hub.example"
TOKEN_FILE = os.path.join(TMP, "dot", "google_token.json")
os.makedirs(os.path.dirname(TOKEN_FILE), mode=0o700)
os.environ.update(
    RIVIA_DB=os.path.join(TMP, "auth.db"), RIVIA_DOTDIR=os.path.join(TMP, "dot"),
    RIVIA_DIR=os.path.join(TMP, "rivia"),
    RIVIA_JWT_KEY=secrets.token_hex(32),
    RIVIA_PASS_HASH=_salt.hex() + "$" + hashlib.scrypt(b"x" * 16, salt=_salt, n=2**14, r=8, p=1).hex(),
    RIVIA_TOTP_SECRET=base64.b32encode(secrets.token_bytes(20)).decode().rstrip("="),
    RIVIA_GOOGLE_CLIENT_ID=MAIN_ID, RIVIA_GOOGLE_CLIENT_SECRET=MAIN_SECRET,
    RIVIA_GOOGLE_TOKEN_PATH=TOKEN_FILE, RIVIA_LOGIN_EMAIL=EMAIL, RIVIA_CORS_ORIGINS=HUB,
    RIVIA_SIGNIN_CHECK="0",
)
for k in ("RIVIA_GOOGLE_WEB_CLIENT_ID", "RIVIA_GOOGLE_WEB_CLIENT_SECRET"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jwt
import rivia_applogin as al
import rivia_auth as ra
import rivia_gcal as gcal

ALL_SCOPES = "openid https://www.googleapis.com/auth/userinfo.email " + gcal.SCOPE_ALL


def ok(cond, label):
    assert cond, label
    print("  ok ·", label)


# ── fake Google token endpoint ──
SEEN = []          # every form Google received
SECRETS_SEEN = []  # every code and token the fake handed out, to prove none reach the log


def id_token(**over):
    claims = {"iss": "https://accounts.google.com", "aud": os.environ.get("RIVIA_GOOGLE_WEB_CLIENT_ID") or MAIN_ID,
              "azp": ANDROID_ID, "sub": "1234567890", "email": EMAIL, "email_verified": True,
              "iat": int(time.time()), "exp": int(time.time()) + 3600}
    claims.update(over)
    for k in [k for k, v in claims.items() if v is None]:
        del claims[k]
    tok = jwt.encode(claims, "not-google", algorithm="HS256")
    SECRETS_SEEN.append(tok)
    return tok


def reply_for(code):
    access, refresh = "ya29.access-" + secrets.token_hex(6), "1//refresh-" + secrets.token_hex(6)
    SECRETS_SEEN.extend([access, refresh])
    good = {"access_token": access, "refresh_token": refresh, "expires_in": 3599, "token_type": "Bearer",
            "scope": ALL_SCOPES}
    kind = code.split("-")[0]
    if kind == "bad":
        return 400, {"error": "invalid_grant", "error_description": "Bad Request"}
    if kind == "good":
        return 200, dict(good, id_token=id_token())
    if kind == "otheremail":
        return 200, dict(good, id_token=id_token(email="someone@example.com"))
    if kind == "caseemail":
        return 200, dict(good, id_token=id_token(email=EMAIL.upper()))
    if kind == "unverified":
        return 200, dict(good, id_token=id_token(email_verified=False))
    if kind == "wrongaud":
        return 200, dict(good, id_token=id_token(aud=ANDROID_ID))
    if kind == "mainaud":
        return 200, dict(good, id_token=id_token(aud=MAIN_ID))
    if kind == "wrongiss":
        return 200, dict(good, id_token=id_token(iss="https://evil.example"))
    if kind == "expired":
        return 200, dict(good, id_token=id_token(exp=int(time.time()) - 3600))
    if kind == "noid":
        return 200, good
    if kind == "norefresh":
        return 200, dict({k: v for k, v in good.items() if k != "refresh_token"}, id_token=id_token())
    if kind == "fewscopes":
        return 200, dict(good, scope="openid " + gcal.SCOPE_READ, id_token=id_token())
    return 400, {"error": "invalid_request"}


class Google(BaseHTTPRequestHandler):
    def do_POST(self):
        form = {k: v[-1] for k, v in parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode(),
                                                keep_blank_values=True).items()}
        SEEN.append(form)
        if form.get("grant_type") == "refresh_token":
            access = "ya29.refreshed-" + secrets.token_hex(6)
            SECRETS_SEEN.append(access)
            status, obj = 200, {"access_token": access, "expires_in": 3599, "token_type": "Bearer"}
        else:
            status, obj = reply_for(form.get("code", ""))
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


httpd = HTTPServer(("127.0.0.1", 0), Google)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
os.environ["RIVIA_GOOGLE_TOKEN_URL"] = "http://127.0.0.1:%d/token" % httpd.server_address[1]


# ── a raw ASGI call, like test_rivia_auth.py ──
class R:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.text = status, headers, body.decode()

    def json(self):
        return json.loads(self.text)


def call(app, method, path, *, js=None, raw=None, headers=None, reset=True):
    if reset:
        al.reset_rate_limit()
    body = raw if raw is not None else (json.dumps(js).encode() if js is not None else b"")
    hdrs = [(b"host", b"mcp.rivia.observer"), (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode())]
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
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(m):
        if m["type"] == "http.response.start":
            out["status"] = m["status"]
            out["headers"] = {k.decode().lower(): v.decode() for k, v in m["headers"]}
        elif m["type"] == "http.response.body":
            out["body"] += m.get("body", b"")
    asyncio.run(app(scope, receive, send))
    return R(out["status"], out["headers"], out["body"])


async def door_stub(scope, receive, send):
    """Stands in for a door's MCP app: answers 200 once the gate let the request through."""
    from starlette.responses import JSONResponse
    await JSONResponse({"ok": True, "path": scope["path"]})(scope, receive, send)


APP = ra.build_app(app_mcp=door_stub, admin_mcp=door_stub)
LOG = io.StringIO()


def login(code, app=None, **kw):
    with contextlib.redirect_stderr(LOG):
        return call(app or APP, "POST", "/app/login", js={"server_auth_code": code}, **kw)


def code(kind):
    c = "%s-%s" % (kind, secrets.token_urlsafe(24))
    SECRETS_SEEN.append(c)
    return c


def token_bytes():
    with open(TOKEN_FILE, "rb") as f:
        return f.read()


def backups():
    return sorted(glob.glob(TOKEN_FILE + ".bak-*"))


# ── the gate: only exactly /app/login is open ──
for path in ("/app/mcp", "/app/loginx", "/app/login/x", "/admin/login"):
    ok(call(APP, "POST", path).status == 401, "POST %s still needs a bearer" % path)
for method in ("GET", "PUT", "DELETE", "OPTIONS"):
    ok(call(APP, method, "/app/login").status == 401, "%s /app/login needs a bearer (only POST is open)" % method)
for raw, label in ((b"not json", "a body that is not JSON"), (json.dumps({"server_auth_code": 5}).encode(), "a code that is not text"),
                   (json.dumps({"server_auth_code": "short"}).encode(), "a too-short code"), (b"[]", "a JSON list")):
    r = call(APP, "POST", "/app/login", raw=raw)
    ok(r.status == 400 and r.json() == {"ok": False, "error": "bad_request"}, "%s is a 400" % label)
ok(not SEEN, "bad requests never reach Google")

# ── not configured: no login email, no Google call ──
os.environ.pop("RIVIA_LOGIN_EMAIL")
r = login(code("good"))
ok(r.status == 503 and r.json()["error"] == "not_configured" and not SEEN, "no RIVIA_LOGIN_EMAIL: 503, Google is not called")
os.environ["RIVIA_LOGIN_EMAIL"] = EMAIL

# ── a good sign-in (the usual client) ──
with open(TOKEN_FILE, "w") as f:
    json.dump({"refresh_token": "old-refresh", "access_token": "", "expiry": 0,
               "scope": gcal.SCOPE_READ, "token_type": "Bearer"}, f, indent=1)
os.chmod(TOKEN_FILE, 0o600)
OLD = token_bytes()
old_app = ra.mint_door_token("app")["token"]
c = code("good")
r = login(c)
body = r.json()
ok(r.status == 200 and body["ok"] is True and isinstance(body["app_token"], str)
   and body["expires_in"] == ra.APP_TOKEN_DAYS * 86400 and set(body) == {"ok", "app_token", "expires_in"},
   "a good sign-in returns ok, app_token and expires_in (%d days)" % ra.APP_TOKEN_DAYS)
ok(r.headers.get("cache-control") == "no-store", "the reply is not cached")
form = SEEN[-1]
ok(form == {"grant_type": "authorization_code", "code": c, "client_id": MAIN_ID, "client_secret": MAIN_SECRET,
            "redirect_uri": ""}, "the code is exchanged with the client id/secret and an empty redirect_uri")
saved = json.loads(token_bytes())
ok(set(saved) == {"refresh_token", "access_token", "expiry", "scope", "token_type"} and saved["refresh_token"].startswith("1//refresh"),
   "the Google token is saved in google-login's format")
ok(stat.S_IMODE(os.stat(TOKEN_FILE).st_mode) == 0o600, "google_token.json stays mode 600")
ok(gcal.load_tokens()["refresh_token"] == saved["refresh_token"] and gcal.has_scopes(*gcal.SCOPES_WRITE),
   "the server reads it back with every google-login scope")
ok(len(backups()) == 1 and open(backups()[0], "rb").read() == OLD
   and stat.S_IMODE(os.stat(backups()[0]).st_mode) == 0o600, "the old token was backed up first (mode 600)")
claims = ra.decode(body["app_token"], "access")
ok(claims and claims["client_id"] == ra.APP_CLIENT, "app_token is a real app-door token")
ok(ra.decode(old_app, "access") is None, "the old app token is revoked, as rivia token does")
ok(call(APP, "POST", "/app/mcp", headers={"authorization": "Bearer " + body["app_token"]}).status == 200,
   "the new app token opens /app/mcp")
ok(call(APP, "POST", "/admin/mcp", headers={"authorization": "Bearer " + body["app_token"]}).status == 403,
   "and only the app door")
SECRETS_SEEN.append(body["app_token"])
refreshed = gcal.refresh_access(gcal.load_tokens())
ok(SEEN[-1]["client_id"] == MAIN_ID and "client" not in json.loads(token_bytes()),
   "a token from the usual client refreshes with the usual client")

# ── anything else: 403, the token file and app tokens untouched ──
current = token_bytes()
live_app = body["app_token"]
n_backups = len(backups())
for kind, reason in (("otheremail", "wrong_account"), ("caseemail", "wrong_account"), ("unverified", "email_not_verified"),
                     ("wrongaud", "wrong_audience"), ("wrongiss", "wrong_issuer"), ("expired", "expired_id_token"),
                     ("noid", "no_id_token"), ("norefresh", "no_refresh_token"), ("fewscopes", "missing_scopes"),
                     ("bad", "google_refused_code")):
    r = login(code(kind))
    ok(r.status == 403 and r.json() == {"ok": False, "error": reason}, "%s: 403 %s" % (kind, reason))
ok(token_bytes() == current and len(backups()) == n_backups, "a refused sign-in never touches google_token.json")
ok(ra.decode(live_app, "access") is not None, "a refused sign-in never mints or revokes an app token")

# ── a separate Web client (RIVIA_GOOGLE_WEB_CLIENT_ID / _SECRET) ──
os.environ.update(RIVIA_GOOGLE_WEB_CLIENT_ID=WEB_ID, RIVIA_GOOGLE_WEB_CLIENT_SECRET=WEB_SECRET)
r = login(code("mainaud"))
ok(r.status == 403 and r.json()["error"] == "wrong_audience", "with a Web client set, an id_token for the other client is refused")
r = login(code("good"))
ok(r.status == 200 and SEEN[-1]["client_id"] == WEB_ID and SEEN[-1]["client_secret"] == WEB_SECRET,
   "with a Web client set, the code is exchanged with the Web client")
SECRETS_SEEN.append(r.json()["app_token"])
ok(json.loads(token_bytes()).get("client") == "web", "the saved token remembers it came from the Web client")
gcal.refresh_access(gcal.load_tokens())
ok(SEEN[-1]["grant_type"] == "refresh_token" and SEEN[-1]["client_id"] == WEB_ID
   and json.loads(token_bytes()).get("client") == "web", "and refreshes with the Web client")
os.environ.pop("RIVIA_GOOGLE_WEB_CLIENT_ID")
os.environ.pop("RIVIA_GOOGLE_WEB_CLIENT_SECRET")
try:
    gcal.refresh_access(gcal.load_tokens())
    err = None
except gcal.GcalError as e:
    err = e
ok(err is not None and err.code == "calendar_login_expired", "a Web token with the Web client unset asks for a new sign-in")

# ── rate limit: about 5 a minute ──
al.reset_rate_limit()
codes_seen = [call(APP, "POST", "/app/login", raw=b"{}", reset=False).status for _ in range(6)]
ok(codes_seen == [400] * 5 + [429], "the 6th try in a minute is refused (429): %s" % codes_seen)
al.reset_rate_limit()

# ── no CORS on /app/login (CORS stays on for the rest) ──
pre = {"origin": HUB, "access-control-request-method": "POST"}
r = call(APP, "OPTIONS", "/app/login", headers=pre)
ok("access-control-allow-origin" not in r.headers and r.status == 401, "no CORS preflight for /app/login")
r = login(code("bad"), headers={"origin": HUB})
ok("access-control-allow-origin" not in r.headers, "no CORS headers on a sign-in reply")
r = call(APP, "OPTIONS", "/app/mcp", headers=pre)
ok(r.headers.get("access-control-allow-origin") == HUB, "CORS still works for the hub's own paths")
os.environ.pop("RIVIA_CORS_ORIGINS")
plain = ra.build_app(app_mcp=door_stub, admin_mcp=door_stub)
r = login(code("bad"), app=plain)
ok(r.status == 403, "with CORS off, /app/login works the same")

# ── the log: reasons only, never a code, a token or an email ──
text = LOG.getvalue()
ok("rivia: app login refused (wrong_account)" in text and "rivia: app login ok" in text, "the log says what happened")
leaks = [s for s in SECRETS_SEEN + [EMAIL, EMAIL.upper(), MAIN_SECRET, WEB_SECRET] if s and s in text]
ok(not leaks, "the log holds no code, token, secret or email")

# ── the bot sign-in, rivia token and google-login are untouched ──
ok(ra.mint_door_token("app")["client_id"] == ra.APP_CLIENT, "rivia token still mints app tokens")
ok(call(APP, "GET", "/health").status == 200, "/health is still open")
ok(all(call(APP, m, p).status == 404 for m, p in (("GET", "/authorize"), ("POST", "/token"), ("POST", "/register"),
                                               ("GET", "/.well-known/oauth-authorization-server"), ("POST", "/mcp"))),
   "the removed bot door and its OAuth endpoints answer 404")
ok(gcal.login_scope() == gcal.SCOPE_ALL, "google-login still asks for the same scopes")

httpd.shutdown()
print("\nAPP LOGIN PASS")
