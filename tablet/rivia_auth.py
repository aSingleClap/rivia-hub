"""
RIVIA door auth (core): a bearer gate in front of the two doors.

  /app/mcp     the Rivia app   (app token: `rivia token`, or the app's Google sign-in)
  /admin/mcp   the Server Hub  (admin token: `rivia admin-token`, stored on the tablet)
  /app/login   POST only, no bearer, no CORS, about 5 a minute: the app's Google sign-in
               gives the tablet its Google token and the app a new app token (rivia_applogin.py).
               Only RIVIA_LOGIN_EMAIL's Google account is accepted.
  /health      open

Door tokens are HS256 JWTs signed with RIVIA_JWT_KEY and listed in auth.db so they can be
revoked. An app token opens only /app/..., the admin token only /admin/... (and the admin
bearer must also equal the token stored under ~/.rivia). Any other path is 404.

The Grok bot door (/mcp), its OAuth 2.1 sign-in (/authorize, /token, /register, the
.well-known documents, passphrase + TOTP consent) and the bot tools were removed in the core
split (Nov 2026); git tag oauth-bot-archive keeps that code.

Browser clients (CORS): a web page can only call this server if its origin is listed in
RIVIA_CORS_ORIGINS="https://origin1,https://origin2" (exact origins: scheme + host + optional
port, no path). Unset = no CORS headers. CORS only tells the browser which pages may read
replies; the bearer token is still what grants access. Never put "*" or "null" in that list.

  python rivia_auth.py setup                     # prints a new RIVIA_JWT_KEY line for ~/.rivia/.env
  python rivia_auth.py mint-token app [days]     # also: mint-app-token [days]
  python rivia_auth.py mint-token admin [days]   # 7 days default; saved under ~/.rivia, never printed
  python rivia_auth.py revoke-all                # voids every token, including the stored admin token
  python rivia_auth.py status
"""
import hashlib
import hmac
import os
import secrets
import sqlite3
import sys
import tempfile
import time

import jwt
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

# ---------- config ----------
ISSUER = os.environ.get("RIVIA_ISSUER", "https://mcp.rivia.observer").rstrip("/")
RESOURCE = ISSUER + "/mcp"   # the audience in every door token (kept so issued tokens stay valid)
SIGNING_KEY = os.environ.get("RIVIA_JWT_KEY", "")
DB_PATH = os.environ.get("RIVIA_DB", os.path.expanduser("~/rivia/auth.db"))
SCOPE = "rivia"

OPEN_PREFIXES = ("/health", "/app/login")
APP_CLIENT = "rivia-app"
ADMIN_CLIENT = "rivia-admin"
APP_PREFIX = "/app/"
ADMIN_PREFIX = "/admin/"
# The app's Google sign-in. The only open path on a door, and only a POST to exactly this
# path: /app/login/x, /app/loginx, GET /app/login and /app/mcp still need a bearer. No CORS.
APP_LOGIN_PATH = "/app/login"
# A token opens only its own door. No other client ids are accepted.
DOORS = {APP_CLIENT: APP_PREFIX, ADMIN_CLIENT: ADMIN_PREFIX}
ADMIN_TOKEN_DAYS = 7
APP_TOKEN_DAYS = 365
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
    except OSError:
        return None


def admin_token_matches(presented: str) -> bool:
    """True only if presented equals the token stored under ~/.rivia (constant time)."""
    stored = load_admin_token()
    dummy = "missing-admin-token"  # always compare so a missing file is not a faster rejection
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
    try:
        os.remove(admin_token_path())
    except FileNotFoundError:
        pass


def is_open_path(path: str, method: str = "") -> bool:
    """No bearer needed: /health, and a POST to exactly /app/login."""
    if path.startswith(APP_PREFIX) or path.startswith(ADMIN_PREFIX):
        return path == APP_LOGIN_PATH and method == "POST"
    return path.startswith(OPEN_PREFIXES)


def path_fits_door(path: str, prefix: str) -> bool:
    p = path if path.endswith("/") else path + "/"
    return p.startswith(prefix)


def token_fits_path(client_id, path) -> bool:
    """True if this client_id's token may open path (its own door only)."""
    allowed = DOORS.get(client_id)
    return allowed is not None and path_fits_door(path, allowed)


# ---------- storage ----------
def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)
    c.execute("pragma journal_mode=wal")
    return c


def init_db():
    # auth.db from before the core split also holds codes, failures and registrations (the bot
    # OAuth flow); they are left in place, unused.
    with db() as c:
        c.executescript(
            """
            create table if not exists clients(client_id text primary key,
                redirect_uris text, name text, created real);
            create table if not exists tokens(jti text primary key, client_id text,
                kind text, expires real, revoked integer default 0);
            """
        )


def decode(token, kind):
    try:
        claims = jwt.decode(token, SIGNING_KEY, algorithms=["HS256"], audience=RESOURCE, issuer=ISSUER)
    except jwt.PyJWTError:
        return None
    if claims.get("kind") != kind:
        return None
    with db() as c:
        row = c.execute("select revoked from tokens where jti=?", (claims["jti"],)).fetchone()
    return claims if row and row[0] == 0 else None


# ---------- endpoints ----------
async def health(request: Request):
    return JSONResponse({"ok": True})


async def app_login(request: Request):
    """POST /app/login: Google sign-in in the app -> the tablet's Google token + the app token."""
    import rivia_applogin  # loaded on first use; keeps the Google code out of this module
    return await rivia_applogin.handle(request, mint_door_token)


async def not_found(scope, receive, send):
    await JSONResponse({"error": "not_found"}, status_code=404)(scope, receive, send)


# ---------- bearer gate (pure ASGI, doesn't buffer SSE) ----------
class BearerGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or is_open_path(scope["path"], scope.get("method", "")):
            return await self.app(scope, receive, send)
        path = scope["path"]
        if not (path_fits_door(path, APP_PREFIX) or path_fits_door(path, ADMIN_PREFIX)):
            return await not_found(scope, receive, send)  # no bot door any more
        auth = dict(scope["headers"]).get(b"authorization", b"").decode()
        claims = decode(auth[7:], "access") if auth[:7].lower() == "bearer " else None
        if not claims:
            return await Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
        if not token_fits_path(claims.get("client_id"), path):
            return await JSONResponse({"error": "wrong_door",
                                       "error_description": "this token isn't for this path"},
                                      status_code=403)(scope, receive, send)
        # The admin bearer must also match the token stored on the tablet (mode 600), compared in
        # constant time. A leftover JWT after revoke or a mint that never wrote the file is 401.
        if claims.get("client_id") == ADMIN_CLIENT:
            raw = auth[7:] if auth[:7].lower() == "bearer " else ""
            if not admin_token_matches(raw):
                return await Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
        scope.setdefault("state", {})["claims"] = claims
        await self.app(scope, receive, send)


def with_cors(app):
    """Wrap the gated app so listed browser origins (the hub) can call it. Sits OUTSIDE the
    gate: a preflight (OPTIONS) carries no token. Never on /app/login. Pure ASGI."""
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


def build_app(lifespan=None, app_mcp=None, admin_mcp=None):
    """app_mcp / admin_mcp: ASGI apps serving MCP at /mcp, mounted under /app and /admin.
    Pass a lifespan that starts every mounted MCP app, or the session managers never start."""
    if not SIGNING_KEY:
        raise SystemExit("Missing RIVIA_JWT_KEY. Run: python rivia_auth.py setup")
    init_db()
    routes = [
        Route("/health", health),
        Route(APP_LOGIN_PATH, app_login, methods=["POST"]),
    ]
    if app_mcp:
        routes.append(Mount("/app", app=app_mcp))
    if admin_mcp:
        routes.append(Mount("/admin", app=admin_mcp))
    return with_cors(BearerGate(Starlette(routes=routes, lifespan=lifespan)))


def mint_door_token(kind, days=None):
    """Mint a door token. kind is 'app' or 'admin'. Minting one kind revokes only that kind.
    The admin token is written under ~/.rivia (mode 600) and is not printed by the CLI; this
    function returns it for in-process callers (tests, the app's Google sign-in)."""
    if not SIGNING_KEY:
        raise SystemExit("Load ~/.rivia/.env first (RIVIA_JWT_KEY is missing).")
    kind = (kind or "").strip().lower()
    if kind not in ("app", "admin"):
        raise SystemExit("usage: python rivia_auth.py mint-token <app|admin> [days]")
    client_id = APP_CLIENT if kind == "app" else ADMIN_CLIENT
    label = "Rivia app" if kind == "app" else "Rivia admin"
    days = int(APP_TOKEN_DAYS if kind == "app" else ADMIN_TOKEN_DAYS) if days is None else int(days)
    if days < 1:
        raise SystemExit("days must be at least 1")
    init_db()
    now = int(time.time())
    with db() as c:
        c.execute("insert or ignore into clients values(?,?,?,?)", (client_id, "[]", label, now))
        old = c.execute("update tokens set revoked=1 where client_id=? and revoked=0", (client_id,)).rowcount
        jti = secrets.token_urlsafe(16)
        claims = {"iss": ISSUER, "aud": RESOURCE, "sub": "owner", "client_id": client_id, "jti": jti,
                  "iat": now, "exp": now + days * 86400, "kind": "access", "scope": SCOPE}
        c.execute("insert into tokens values(?,?,?,?,0)", (jti, client_id, "access", now + days * 86400))
    token = jwt.encode(claims, SIGNING_KEY, algorithm="HS256")
    path = store_admin_token(token) if kind == "admin" else None
    return {"kind": kind, "token": token, "days": days, "revoked": old, "path": path, "client_id": client_id}


# ---------- CLI ----------
def _cli():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "setup":
        print("# put this in ~/.rivia/.env (chmod 600), never in git")
        print(f"RIVIA_ISSUER='{ISSUER}'")
        print(f"RIVIA_JWT_KEY='{secrets.token_hex(32)}'")
    elif cmd == "revoke-all":
        init_db()
        with db() as c:
            n = c.execute("update tokens set revoked=1 where revoked=0").rowcount
        clear_admin_token_file()
        print(f"Revoked {n} tokens and cleared the stored admin token. Sign in again in the app "
              f"(or paste a new `rivia token`) and run `rivia admin-token` for the hub.")
    elif cmd in ("mint-app-token", "mint-token"):
        if cmd == "mint-app-token":
            kind = "app"
            days_arg = sys.argv[2] if len(sys.argv) > 2 else None
        else:
            if len(sys.argv) < 3:
                raise SystemExit("usage: python rivia_auth.py mint-token <app|admin> [days]")
            kind = (sys.argv[2] or "").strip().lower()
            days_arg = sys.argv[3] if len(sys.argv) > 3 else None
        info = mint_door_token(kind, int(days_arg) if days_arg is not None else None)
        if kind == "app":
            print(info["token"])
            print("# App token, valid %d days, only for %s/app/mcp. Revoked %d older app token(s)."
                  % (info["days"], ISSUER, info["revoked"]), file=sys.stderr)
            print("# Paste it into the app's Dev settings. Don't share it.", file=sys.stderr)
        else:
            # Never print the admin token. The file is the only copy this command writes.
            print("Saved admin token to %s (mode 600). Valid %d days, only for /admin/mcp. "
                  "Revoked %d older admin token(s). The token is not printed."
                  % (info["path"], info["days"], info["revoked"]))
    elif cmd == "status":
        init_db()
        with db() as c:
            print("live tokens:", c.execute("select count(*) from tokens where revoked=0 and expires>?",
                                            (time.time(),)).fetchone()[0])
        print("browser origins (CORS):", ", ".join(cors_origins()) or "none (CORS off)")
        print("admin token file:", "present (not shown)" if load_admin_token() else "none")
    else:
        print(__doc__)


if __name__ == "__main__":
    _cli()
else:
    app = build_app() if SIGNING_KEY else None
