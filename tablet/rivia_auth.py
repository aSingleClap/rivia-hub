"""
RIVIA router — OAuth 2.1 layer in front of the MCP server (client: Grok Bot).

Implements what remote MCP clients require before they will call a tool:
  RFC 9728  protected resource metadata   /.well-known/oauth-protected-resource[/mcp]
  RFC 8414  auth server metadata          /.well-known/oauth-authorization-server
  RFC 7591  dynamic client registration   /register
  RFC 7636  PKCE (S256) auth code flow    /authorize, /token
plus a bearer gate (pure ASGI, safe for streaming) in front of everything else.

Human gate: the consent page requires passphrase + TOTP. DCR is open by design
(Grok Bot needs it), so the consent login is the ONLY thing stopping a stranger
from minting a token. Do not weaken it. The consent page names the client and
where the code will be sent: only approve a flow you started yourself.

Clients: Grok Bot registers with DCR (Cursor callbacks). grok.com connectors
identify with a client ID metadata document (CIMD): client_id is the URL
https://grok.com/oauth/mcp-client.json. That one is pinned below with the
redirects it publishes; nothing is fetched at runtime, and no other URL
client_id is accepted.

Redirects: only URIs on the allowlist (RIVIA_REDIRECTS, default = Grok Bot's
Cursor callbacks + grok.com's) plus http loopback are accepted at /register. Anything else
is dropped and logged; `python rivia_auth.py registrations` shows exactly what
a client asked for, so a new client's callback can be added to RIVIA_REDIRECTS.

App sign-in: POST /app/login {"server_auth_code": ...} (no bearer, no CORS, about 5 a
minute) turns the app's Google sign-in into the tablet's Google token plus a new app token
(rivia_applogin.py). Only RIVIA_LOGIN_EMAIL's Google account is accepted.

Browser clients (CORS): a web page can only call this server if its origin is
listed in RIVIA_CORS_ORIGINS="https://origin1,https://origin2" (exact origins:
scheme + host + optional port, no path). Unset = no CORS headers, as before.
CORS only tells the browser which pages may read replies; the bearer token is
still what grants access. Never put "*" or "null" in that list.

Deps (pure Python, Termux-friendly):
  pip install starlette uvicorn pyjwt python-multipart

Setup:
  python rivia_auth.py setup          # prints env lines + TOTP URI
Run (bind to localhost; cloudflared routes mcp.rivia.observer -> here):
  uvicorn rivia_auth:app --host 127.0.0.1 --port 8787
Kill switch:
  python rivia_auth.py revoke-all
See what clients tried to register (and which redirects were refused):
  python rivia_auth.py registrations
Tokens (no browser sign-in):
  python rivia_auth.py mint-token app [days]     # also: mint-app-token [days]
  python rivia_auth.py mint-token admin [days]   # 7 days default; saved under ~/.rivia, never printed
  python rivia_auth.py revoke-all                # voids every token, including the stored admin token
The app token works only on /app/...; the admin token only on /admin/...; bot tokens only the rest.
"""
import base64
import hashlib
import hmac
import tempfile
import html
import json
import os
import re
import secrets
import sqlite3
import struct
import sys
import time
from urllib.parse import urlencode, urlparse

import jwt
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route

# ---------- config ----------
ISSUER = os.environ.get("RIVIA_ISSUER", "https://mcp.rivia.observer").rstrip("/")
RESOURCE = ISSUER + "/mcp"
SIGNING_KEY = os.environ.get("RIVIA_JWT_KEY", "")
PASS_HASH = os.environ.get("RIVIA_PASS_HASH", "")
TOTP_SECRET = os.environ.get("RIVIA_TOTP_SECRET", "")
DB_PATH = os.environ.get("RIVIA_DB", os.path.expanduser("~/rivia/auth.db"))
# Long-lived access tokens: some connector backends don't refresh reliably and
# need remove + re-add after expiry. Revocation is the safety valve, not expiry.
ACCESS_TTL = int(os.environ.get("RIVIA_ACCESS_TTL", 30 * 86400))
REFRESH_TTL = int(os.environ.get("RIVIA_REFRESH_TTL", 90 * 86400))
CODE_TTL = 120
SCOPE = "rivia"
MAX_CLIENTS = 50
LOCKOUT_FAILS, LOCKOUT_WINDOW = 5, 900
# Redirect allowlist. Exact URIs; an entry ending in "*" matches as a prefix.
# Default: Grok Bot's callbacks (it runs on Cursor's connector backend) and the
# grok.com / console.x.ai connector callbacks (from grok.com/oauth/mcp-client.json).
# Override with RIVIA_REDIRECTS="uri1,uri2,..." (replaces the default list).
GROK_WEB_REDIRECTS = (
    "https://grok.com/connectors-oauth-exchange-code/",
    "https://console.x.ai/connectors-oauth-exchange-code/",
)
DEFAULT_REDIRECTS = (
    "https://www.cursor.com/agents/mcp/oauth/callback",
    "https://www.cursor.com/bot/mcp/oauth/callback",
    "cursor://anysphere.cursor-mcp/oauth/callback",
    *GROK_WEB_REDIRECTS,
)
REDIRECTS = tuple(u.strip() for u in os.environ.get("RIVIA_REDIRECTS", "").split(",") if u.strip()) \
    or DEFAULT_REDIRECTS
# http://localhost / 127.0.0.1 / [::1] on any port: the code lands on the device
# that started the flow, so this can't hand a token to a remote stranger.
ALLOW_LOOPBACK = os.environ.get("RIVIA_ALLOW_LOOPBACK", "1") == "1"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
# CIMD clients we accept, pinned (client_id URL -> name, redirects). Pinned on
# purpose: fetching arbitrary client_id URLs from the tablet would be an SSRF
# hole. If xAI changes its document, update this entry.
PINNED_CLIENTS = {
    "https://grok.com/oauth/mcp-client.json": {"name": "Grok", "redirect_uris": list(GROK_WEB_REDIRECTS)},
}
if os.environ.get("RIVIA_ALLOW_GROK_WEB", "1") != "1":
    PINNED_CLIENTS = {}

OPEN_PREFIXES = ("/.well-known/", "/authorize", "/token", "/register", "/health", "/app/login")
APP_CLIENT = "rivia-app"  # the Rivia app's client id; it never uses the OAuth flow
ADMIN_CLIENT = "rivia-admin"
APP_PREFIX = "/app/"
ADMIN_PREFIX = "/admin/"
# The app's Google sign-in (rivia_applogin.py). The only open path on a door, and only exactly
# this path: /app/login/x, /app/loginx and /app/mcp still need a bearer. No CORS on it.
APP_LOGIN_PATH = "/app/login"
# A token whose client_id is in this table opens only its own prefix.
# Every other token (bots) opens only the rest — never /app/ or /admin/.
DOORS = {APP_CLIENT: APP_PREFIX, ADMIN_CLIENT: ADMIN_PREFIX}
ADMIN_TOKEN_DAYS = 7
APP_TOKEN_DAYS = 365
# Browser origins allowed to call this server (CORS). Exact origins, comma-separated,
# scheme + host + optional port, no path. Put only the Server Hub origin here, e.g.
# RIVIA_CORS_ORIGINS="https://hub.rivia.observer". Empty = CORS off (the hub
# cannot call /admin/mcp from a browser until this is set). "*" and "null" are
# dropped on purpose: either would let any page (or any sandboxed frame) read
# replies from this server.
CORS_HEADERS = [
    "authorization", "content-type", "accept",
    "mcp-session-id", "mcp-protocol-version",
]


def cors_origins():
    """Exact origins from RIVIA_CORS_ORIGINS, dropping '*' and 'null'."""
    return [o for o in (x.strip().rstrip("/") for x in
                        os.environ.get("RIVIA_CORS_ORIGINS", "").split(","))
            if o and o not in ("*", "null") and o.lower() != "null"]


def dot_dir():
    return os.environ.get("RIVIA_DOTDIR") or os.path.expanduser("~/.rivia")


def admin_token_path():
    return os.environ.get("RIVIA_ADMIN_TOKEN_PATH") or os.path.join(dot_dir(), "admin_token")


def _sha256(s: str) -> bytes:
    return hashlib.sha256((s or "").encode()).digest()


def tokens_equal(a, b) -> bool:
    """Constant-time compare for bearer / stored tokens (equal-length SHA-256 digests)."""
    return hmac.compare_digest(_sha256(a or ""), _sha256(b or ""))


def load_admin_token():
    try:
        with open(admin_token_path(), encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None


def admin_token_matches(presented: str) -> bool:
    """True only if presented equals the token stored under ~/.rivia (constant time)."""
    stored = load_admin_token()
    # Always compare so a missing file is not a faster rejection.
    dummy = "missing-admin-token"
    ok = tokens_equal(stored if stored is not None else dummy, (presented or "").strip())
    return bool(stored) and ok


def store_admin_token(token: str) -> str:
    """Write the admin token to ~/.rivia/admin_token (mode 600). Never log the value."""
    path = admin_token_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(prefix=".admin_token.", dir=directory or ".")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def clear_admin_token_file():
    path = admin_token_path()
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def is_open_path(path: str, method: str = "") -> bool:
    """No bearer needed. Under a door (/app/, /admin/) only a POST to APP_LOGIN_PATH itself is
    open: any other method there would fall through to the door's MCP app, so it needs a bearer."""
    if path.startswith(APP_PREFIX) or path.startswith(ADMIN_PREFIX):
        return path == APP_LOGIN_PATH and method == "POST"
    return path.startswith(OPEN_PREFIXES)


def path_fits_door(path: str, prefix: str) -> bool:
    p = path if path.endswith("/") else path + "/"
    return p.startswith(prefix)


def token_fits_path(client_id, path) -> bool:
    """True if this client_id is allowed to open path."""
    allowed = DOORS.get(client_id)
    if allowed is not None:
        return path_fits_door(path, allowed)
    return not any(path_fits_door(path, prefix) for prefix in DOORS.values())


# ---------- storage ----------
def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)
    c.execute("pragma journal_mode=wal")
    return c


def init_db():
    with db() as c:
        c.executescript(
            """
            create table if not exists clients(client_id text primary key,
                redirect_uris text, name text, created real);
            create table if not exists codes(code text primary key, client_id text,
                redirect_uri text, challenge text, expires real, used integer default 0);
            create table if not exists tokens(jti text primary key, client_id text,
                kind text, expires real, revoked integer default 0);
            create table if not exists failures(ts real);
            create table if not exists registrations(ts real, client_name text,
                accepted text, refused text, client_id text);
            """
        )


# ---------- helpers ----------
def valid_redirect(uri: str) -> bool:
    if not isinstance(uri, str) or len(uri) > 2000 or any(c in uri for c in "\r\n\t "):
        return False
    try:
        u = urlparse(uri)
    except ValueError:
        return False
    if u.fragment:
        return False
    if ALLOW_LOOPBACK and u.scheme == "http" and (u.hostname or "") in LOOPBACK_HOSTS:
        return True
    for allowed in REDIRECTS:
        if allowed.endswith("*"):
            if uri.startswith(allowed[:-1]):
                return True
        elif uri == allowed:
            return True
    return False


def redirect_label(uri: str) -> str:
    """Where the code goes, in words, for the consent page."""
    u = urlparse(uri)
    if u.scheme in ("http", "https"):
        return u.hostname or uri
    return u.scheme + "://" + (u.netloc or "")


def resource_ok(res) -> bool:
    # Lenient: any resource on our origin (clients send whatever URL was pasted).
    if not res:
        return True
    try:
        u = urlparse(res)
    except ValueError:
        return False
    return f"{u.scheme}://{u.netloc}" == ISSUER


def get_client(client_id):
    if client_id in PINNED_CLIENTS:
        return list(PINNED_CLIENTS[client_id]["redirect_uris"])
    with db() as c:
        row = c.execute(
            "select redirect_uris from clients where client_id=?", (client_id,)
        ).fetchone()
    return json.loads(row[0]) if row else None


def client_name(client_id) -> str:
    if client_id in PINNED_CLIENTS:
        return PINNED_CLIENTS[client_id]["name"]
    with db() as c:
        row = c.execute("select name from clients where client_id=?", (client_id,)).fetchone()
    return (row[0] if row else "") or "an unnamed client"


def log_registration(name, accepted, refused, cid):
    with db() as c:
        c.execute("insert into registrations values(?,?,?,?,?)",
                  (time.time(), name, json.dumps(accepted), json.dumps(refused), cid))
        c.execute("delete from registrations where rowid not in "
                  "(select rowid from registrations order by ts desc limit 200)")
    if refused:
        print(f"[rivia_auth] /register from {name!r}: refused redirects {refused}; "
              f"accepted {accepted}", file=sys.stderr)


def hash_pass(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + "$" + h.hex()


def check_pass(pw: str) -> bool:
    try:
        salt_hex, h_hex = PASS_HASH.split("$")
    except ValueError:
        return False
    h = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(h.hex(), h_hex)


def totp_now(secret: str, t: float | None = None) -> str:
    """RFC 6238 TOTP (SHA1, 6 digits, 30 s) — same as every authenticator app."""
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    counter = int((time.time() if t is None else t) // 30)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 15
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_ok(code: str) -> bool:
    now = time.time()
    return any(hmac.compare_digest(totp_now(TOTP_SECRET, now + d), code or "") for d in (-30, 0, 30))


def locked_out() -> bool:
    with db() as c:
        c.execute("delete from failures where ts<?", (time.time() - LOCKOUT_WINDOW,))
        return c.execute("select count(*) from failures").fetchone()[0] >= LOCKOUT_FAILS


def record_failure():
    with db() as c:
        c.execute("insert into failures values(?)", (time.time(),))


def issue_tokens(client_id):
    now = int(time.time())
    out = {}
    with db() as c:
        for kind, ttl in (("access", ACCESS_TTL), ("refresh", REFRESH_TTL)):
            jti = secrets.token_urlsafe(16)
            claims = {"iss": ISSUER, "aud": RESOURCE, "sub": "owner", "client_id": client_id,
                      "jti": jti, "iat": now, "exp": now + ttl, "kind": kind, "scope": SCOPE}
            out[kind] = jwt.encode(claims, SIGNING_KEY, algorithm="HS256")
            c.execute("insert into tokens values(?,?,?,?,0)", (jti, client_id, kind, now + ttl))
    return {"access_token": out["access"], "token_type": "Bearer", "expires_in": ACCESS_TTL,
            "refresh_token": out["refresh"], "scope": SCOPE}


def decode(token, kind):
    try:
        claims = jwt.decode(token, SIGNING_KEY, algorithms=["HS256"],
                            audience=RESOURCE, issuer=ISSUER)
    except jwt.PyJWTError:
        return None
    if claims.get("kind") != kind:
        return None
    with db() as c:
        row = c.execute("select revoked from tokens where jti=?", (claims["jti"],)).fetchone()
    return claims if row and row[0] == 0 else None


def oauth_error(err, desc="", status=400):
    return JSONResponse({"error": err, "error_description": desc}, status_code=status,
                        headers={"Cache-Control": "no-store"})


# Consent page CSP deliberately has NO form-action directive: OAuth relays
# may redirect to a second origin and form-action blocks that hop in Chrome.
PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}
CSS = ("body{font:16px system-ui;max-width:420px;margin:40px auto;padding:0 16px}"
       "input{display:block;width:100%;padding:10px;margin:8px 0 16px;font:inherit;box-sizing:border-box}"
       "button{padding:10px 18px;font:inherit;margin-right:8px}")


def page(body, status=200):
    return HTMLResponse(f"<!doctype html><meta name=viewport content='width=device-width'>"
                        f"<title>RIVIA</title><style>{CSS}</style>{body}",
                        status_code=status, headers=PAGE_HEADERS)


def bounce(url):
    # 200 + meta refresh instead of 302: not a form-submission redirect, so the
    # relay's second hop can't be blocked by CSP. Link as fallback.
    u = html.escape(url, quote=True)
    return page(f"<meta http-equiv=refresh content='0;url={u}'><p><a href='{u}'>Continue</a></p>")


# ---------- endpoints ----------
async def prm(request: Request):
    return JSONResponse({"resource": RESOURCE, "authorization_servers": [ISSUER],
                         "bearer_methods_supported": ["header"], "scopes_supported": [SCOPE]})


async def asm(request: Request):
    return JSONResponse({
        "issuer": ISSUER,
        "authorization_endpoint": ISSUER + "/authorize",
        "token_endpoint": ISSUER + "/token",
        "registration_endpoint": ISSUER + "/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [SCOPE],
        "client_id_metadata_document_supported": bool(PINNED_CLIENTS),
    })


async def register(request: Request):
    try:
        body = await request.json()
    except Exception:
        return oauth_error("invalid_client_metadata", "bad json")
    if not isinstance(body, dict):
        return oauth_error("invalid_client_metadata", "bad json")
    raw = body.get("redirect_uris") or []
    if not isinstance(raw, list):
        raw = []
    raw = [str(u)[:2000] for u in raw[:20]]
    name = str(body.get("client_name", ""))[:100]
    # RFC 7591 3.2.1: the server may replace requested metadata. Keep only the
    # allowed URIs; the client can only ever be sent to those.
    uris = [u for u in raw if valid_redirect(u)]
    refused = [u for u in raw if u not in uris]
    if not uris:
        log_registration(name, [], refused, None)
        return oauth_error("invalid_redirect_uri", "no allowed redirect_uri")
    with db() as c:
        if c.execute("select count(*) from clients").fetchone()[0] >= MAX_CLIENTS:
            return oauth_error("invalid_client_metadata", "client limit reached")
        cid = "rivia-" + secrets.token_urlsafe(24)
        c.execute("insert into clients values(?,?,?,?)",
                  (cid, json.dumps(uris), name, time.time()))
    log_registration(name, uris, refused, cid)
    return JSONResponse({"client_id": cid, "client_id_issued_at": int(time.time()),
                         "redirect_uris": uris, "client_name": name,
                         "token_endpoint_auth_method": "none",
                         "grant_types": ["authorization_code", "refresh_token"],
                         "response_types": ["code"]}, status_code=201)


def _validate_authz(p):
    """Returns (error_page_or_None, redirect_uri)."""
    uris = get_client(p.get("client_id", ""))
    ru = p.get("redirect_uri", "")
    if uris is None:
        return page("<p>Unknown client.</p>", 400), None
    if ru not in uris:
        return page("<p>Redirect URI mismatch.</p>", 400), None
    if (p.get("response_type") != "code" or not p.get("code_challenge")
            or p.get("code_challenge_method") != "S256" or not resource_ok(p.get("resource"))):
        q = urlencode({"error": "invalid_request", "state": p.get("state", "")})
        return bounce(f"{ru}{'&' if '?' in ru else '?'}{q}"), None
    return None, ru


FIELDS = ("client_id", "redirect_uri", "response_type", "code_challenge",
          "code_challenge_method", "state", "resource", "scope")


async def authorize(request: Request):
    p = dict(request.query_params) if request.method == "GET" else dict(await request.form())
    err, ru = _validate_authz(p)
    if err:
        return err
    sep = "&" if "?" in ru else "?"

    if request.method == "GET":
        hidden = "".join(f"<input type=hidden name={k} value='{html.escape(p.get(k, ''), quote=True)}'>"
                         for k in FIELDS)
        who = html.escape(client_name(p.get("client_id", "")))
        where = html.escape(redirect_label(ru))
        return page(f"<h2>Connect {who} to RIVIA?</h2>"
                    f"<p>The sign-in code goes to <b>{where}</b>. "
                    "Only allow this if you just added RIVIA as a connector yourself.</p>"
                    "<p>This grants access to RIVIA's tools.</p>"
                    f"<form method=post>{hidden}"
                    "<label>Passphrase<input type=password name=pw autocomplete=current-password></label>"
                    "<label>Authenticator code<input name=otp inputmode=numeric autocomplete=one-time-code></label>"
                    "<button name=act value=allow>Allow</button><button name=act value=deny>Deny</button>"
                    "</form>")

    state = p.get("state", "")
    if p.get("act") != "allow":
        return bounce(ru + sep + urlencode({"error": "access_denied", "state": state}))
    if locked_out():
        return page("<p>Too many failed attempts. Try again in 15 minutes.</p>", 429)
    if not (check_pass(p.get("pw", "")) and totp_ok(p.get("otp", ""))):
        record_failure()
        return page("<p>Wrong passphrase or code. <a href='javascript:history.back()'>Back</a></p>", 401)

    code = secrets.token_urlsafe(32)
    with db() as c:
        c.execute("insert into codes values(?,?,?,?,?,0)",
                  (code, p["client_id"], ru, p["code_challenge"], time.time() + CODE_TTL))
    return bounce(ru + sep + urlencode({"code": code, "state": state}))


async def token(request: Request):
    f = dict(await request.form())
    if not resource_ok(f.get("resource") or request.query_params.get("resource")):
        return oauth_error("invalid_target")
    client_id = f.get("client_id")
    auth = request.headers.get("authorization", "")
    if not client_id and auth.lower().startswith("basic "):
        try:
            client_id = base64.b64decode(auth[6:]).decode().split(":", 1)[0]
        except Exception:
            pass
    if not client_id or get_client(client_id) is None:
        return oauth_error("invalid_client", status=401)

    gt = f.get("grant_type")
    if gt == "authorization_code":
        with db() as c:
            row = c.execute("select client_id, redirect_uri, challenge, expires, used from codes "
                            "where code=?", (f.get("code", ""),)).fetchone()
            if row:
                c.execute("update codes set used=1 where code=?", (f["code"],))
        if not row or row[4] or row[3] < time.time() or row[0] != client_id \
                or row[1] != f.get("redirect_uri"):
            return oauth_error("invalid_grant")
        verifier = f.get("code_verifier", "")
        calc = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if not hmac.compare_digest(calc, row[2]):
            return oauth_error("invalid_grant", "pkce")
        return JSONResponse(issue_tokens(client_id), headers={"Cache-Control": "no-store"})

    if gt == "refresh_token":
        claims = decode(f.get("refresh_token", ""), "refresh")
        if not claims or claims["client_id"] != client_id:
            return oauth_error("invalid_grant")
        with db() as c:  # rotate
            c.execute("update tokens set revoked=1 where jti=?", (claims["jti"],))
        return JSONResponse(issue_tokens(client_id), headers={"Cache-Control": "no-store"})

    return oauth_error("unsupported_grant_type")


async def health(request: Request):
    return JSONResponse({"ok": True})


async def app_login(request: Request):
    """POST /app/login: Google sign-in in the app -> the tablet's Google token + the app token."""
    import rivia_applogin  # loaded on first use; keeps the Google code out of this module
    return await rivia_applogin.handle(request, mint_door_token)


async def echo_stub(scope, receive, send):
    """Placeholder until the real MCP handler is mounted at /mcp."""
    req = Request(scope, receive)
    await JSONResponse({"ok": True, "sub": scope["state"]["claims"]["sub"],
                        "path": req.url.path})(scope, receive, send)


# ---------- bearer gate (pure ASGI, doesn't buffer SSE) ----------
class BearerGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or is_open_path(scope["path"], scope.get("method", "")):
            return await self.app(scope, receive, send)
        auth = dict(scope["headers"]).get(b"authorization", b"").decode()
        claims = decode(auth[7:], "access") if auth[:7].lower() == "bearer " else None
        if not claims:
            # Clients probe / and /mcp without a token; this header is how they discover us.
            # A tokenless DELETE (session teardown) gets the same 401; that is expected.
            hdr = f'Bearer resource_metadata="{ISSUER}/.well-known/oauth-protected-resource/mcp"'
            return await Response(status_code=401, headers={"WWW-Authenticate": hdr})(scope, receive, send)
        # Door table: an app or admin token opens only its own prefix; every
        # other token (bots) opens only the rest. Using either reserved token
        # on the wrong door is 403 wrong_door.
        if not token_fits_path(claims.get("client_id"), scope["path"]):
            return await JSONResponse({"error": "wrong_door",
                                       "error_description": "this token isn't for this path"},
                                      status_code=403)(scope, receive, send)
        # Admin bearer must also match the token stored on the tablet (mode 600),
        # compared in constant time. A leftover JWT after revoke or a mint that
        # never wrote the file is rejected as 401, not logged.
        if claims.get("client_id") == ADMIN_CLIENT:
            raw = auth[7:] if auth[:7].lower() == "bearer " else ""
            if not admin_token_matches(raw):
                hdr = f'Bearer resource_metadata="{ISSUER}/.well-known/oauth-protected-resource/mcp"'
                return await Response(status_code=401, headers={"WWW-Authenticate": hdr})(scope, receive, send)
        scope.setdefault("state", {})["claims"] = claims
        await self.app(scope, receive, send)


def with_cors(app):
    """Wrap the gated app so listed browser origins can call it. Sits OUTSIDE
    the bearer gate: a browser's preflight (OPTIONS) carries no token, so it
    has to be answered before the gate would turn it away with a 401. It also
    puts the CORS headers on the gate's own 401/403, so a web page sees the
    real status instead of a blank CORS failure. Pure ASGI, doesn't buffer SSE."""
    origins = cors_origins()
    if not origins:
        return app
    from starlette.middleware.cors import CORSMiddleware
    cors = CORSMiddleware(
        app,
        allow_origins=origins,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=CORS_HEADERS,
        expose_headers=["mcp-session-id", "mcp-protocol-version", "www-authenticate"],
        max_age=600,
    )

    async def no_cors_for_login(scope, receive, send):
        # The app's sign-in is for the phone, never a web page: no CORS headers, no preflight.
        if scope["type"] == "http" and scope["path"] == APP_LOGIN_PATH:
            return await app(scope, receive, send)
        return await cors(scope, receive, send)
    return no_cors_for_login


def build_app(mcp_app=None, lifespan=None, app_mcp=None, admin_mcp=None):
    """mcp_app: an ASGI app that serves MCP at /mcp, e.g. FastMCP's
    mcp.http_app(path="/mcp"). app_mcp (optional): the same for the Rivia app,
    mounted under /app (so it serves /app/mcp). admin_mcp (optional): the same
    for the admin door, mounted under /admin (so it serves /admin/mcp). Pass a
    lifespan that starts every mounted MCP app, or the session managers never start."""
    if not (SIGNING_KEY and PASS_HASH and TOTP_SECRET):
        raise SystemExit("Missing RIVIA_JWT_KEY / RIVIA_PASS_HASH / RIVIA_TOTP_SECRET. Run: python rivia_auth.py setup")
    init_db()
    tail = ([Mount("/", app=mcp_app)] if mcp_app else
            [Mount("/mcp", app=echo_stub), Mount("/", app=echo_stub)])
    if admin_mcp:
        tail = [Mount("/admin", app=admin_mcp)] + tail
    if app_mcp:
        tail = [Mount("/app", app=app_mcp)] + tail
    routes = [
        Route("/.well-known/oauth-protected-resource{rest:path}", prm),
        Route("/.well-known/oauth-authorization-server{rest:path}", asm),
        Route("/.well-known/openid-configuration", asm),
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize, methods=["GET", "POST"]),
        Route("/token", token, methods=["POST"]),
        Route("/health", health),
        Route(APP_LOGIN_PATH, app_login, methods=["POST"]),
        *tail,
    ]
    return with_cors(BearerGate(Starlette(routes=routes, lifespan=lifespan)))



def mint_door_token(kind, days=None):
    """Mint a door token. kind is 'app' or 'admin'. Minting one kind revokes only
    that kind. The admin token is written under ~/.rivia (mode 600) and is not
    printed by the CLI; this function returns it for in-process callers (tests).
    """
    if not SIGNING_KEY:
        raise SystemExit("Load ~/.rivia/.env first (RIVIA_JWT_KEY is missing).")
    kind = (kind or "").strip().lower()
    if kind not in ("app", "admin"):
        raise SystemExit("usage: python rivia_auth.py mint-token <app|admin> [days]")
    client_id = APP_CLIENT if kind == "app" else ADMIN_CLIENT
    label = "Rivia app" if kind == "app" else "Rivia admin"
    if days is None:
        days = APP_TOKEN_DAYS if kind == "app" else ADMIN_TOKEN_DAYS
    days = int(days)
    if days < 1:
        raise SystemExit("days must be at least 1")
    init_db()
    now = int(time.time())
    with db() as c:
        c.execute("insert or ignore into clients values(?,?,?,?)", (client_id, "[]", label, now))
        old = c.execute("update tokens set revoked=1 where client_id=? and revoked=0",
                        (client_id,)).rowcount
        jti = secrets.token_urlsafe(16)
        claims = {"iss": ISSUER, "aud": RESOURCE, "sub": "owner", "client_id": client_id, "jti": jti,
                  "iat": now, "exp": now + days * 86400, "kind": "access", "scope": SCOPE}
        c.execute("insert into tokens values(?,?,?,?,0)", (jti, client_id, "access", now + days * 86400))
    token = jwt.encode(claims, SIGNING_KEY, algorithm="HS256")
    path = None
    if kind == "admin":
        path = store_admin_token(token)
    return {"kind": kind, "token": token, "days": days, "revoked": old, "path": path, "client_id": client_id}


# ---------- CLI ----------
def _cli():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "setup":
        import getpass
        pw = getpass.getpass("Consent passphrase (16+ chars): ")
        if len(pw) < 16 or pw != getpass.getpass("Again: "):
            raise SystemExit("Too short or mismatch.")
        totp = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
        # Single-quoted: the hash contains '$', which bash would expand when sourcing.
        print("# put these in ~/.rivia/.env (chmod 600), never in git")
        print(f"RIVIA_ISSUER='{ISSUER}'")
        print(f"RIVIA_JWT_KEY='{secrets.token_hex(32)}'")
        print(f"RIVIA_PASS_HASH='{hash_pass(pw)}'")
        print(f"RIVIA_TOTP_SECRET='{totp}'")
        print("# authenticator: add RIVIA_TOTP_SECRET as a time-based key", file=sys.stderr)
    elif cmd == "revoke-all":
        init_db()
        with db() as c:
            n = c.execute("update tokens set revoked=1 where revoked=0").rowcount
        clear_admin_token_file()
        print(f"Revoked {n} tokens and cleared the stored admin token. Remove and re-add the connector in Grok Bot.")
    elif cmd in ("mint-app-token", "mint-token"):
        if cmd == "mint-app-token":
            kind = "app"
            days_arg = sys.argv[2] if len(sys.argv) > 2 else None
        else:
            if len(sys.argv) < 3:
                raise SystemExit("usage: python rivia_auth.py mint-token <app|admin> [days]")
            kind = (sys.argv[2] or "").strip().lower()
            days_arg = sys.argv[3] if len(sys.argv) > 3 else None
        days = int(days_arg) if days_arg is not None else None
        info = mint_door_token(kind, days)
        if kind == "app":
            print(info["token"])
            print("# App token, valid %d days, only for %s/app/mcp. Revoked %d older app token(s)."
                  % (info["days"], ISSUER, info["revoked"]), file=sys.stderr)
            print("# Paste it into the app's Dev settings. Don't share it; anyone with it can ask your bots.",
                  file=sys.stderr)
        else:
            # Never print the admin token. The file is the only copy this command writes.
            print("Saved admin token to %s (mode 600). Valid %d days, only for /admin/mcp. "
                  "Revoked %d older admin token(s). The token is not printed."
                  % (info["path"], info["days"], info["revoked"]))
    elif cmd == "prune-clients":
        init_db()
        with db() as c:
            n = c.execute("delete from clients where client_id not in (?, ?) and client_id not in "
                          "(select distinct client_id from tokens where revoked=0)",
                          (APP_CLIENT, ADMIN_CLIENT)).rowcount
        print(f"Removed {n} unused clients.")
    elif cmd == "status":
        init_db()
        with db() as c:
            print("clients:", c.execute("select count(*) from clients").fetchone()[0])
            print("live tokens:", c.execute("select count(*) from tokens where revoked=0 and expires>?",
                                            (time.time(),)).fetchone()[0])
        print("redirect allowlist:", ", ".join(REDIRECTS), "+ loopback" if ALLOW_LOOPBACK else "")
        print("pinned CIMD clients:", ", ".join(PINNED_CLIENTS) or "-")
        print("browser origins (CORS):", ", ".join(cors_origins()) or "none (CORS off)")
        print("admin token file:", "present (not shown)" if load_admin_token() else "none")
    elif cmd == "registrations":
        init_db()
        with db() as c:
            rows = c.execute("select ts, client_name, accepted, refused, client_id from registrations "
                             "order by ts desc limit 10").fetchall()
        if not rows:
            print("No registrations yet.")
        for ts, name, acc, ref, cid in rows:
            print(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)), repr(name),
                  "->", cid or "REFUSED")
            print("   accepted:", ", ".join(json.loads(acc)) or "-")
            print("   refused: ", ", ".join(json.loads(ref)) or "-")
    else:
        print(__doc__)


if __name__ == "__main__":
    _cli()
else:
    app = build_app() if SIGNING_KEY else None
