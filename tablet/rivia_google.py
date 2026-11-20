# rivia_google.py: Google sign-in credentials and tokens, shared by core and scheduling.
# Core uses it for the app's Google sign-in (POST /app/login); scheduling (rivia_gcal.py) uses
# it for Calendar and Drive. Plain urllib, no Google library. Never print or log token values.
#
# Client id/secret come from the environment (rivia.sh loads ~/.rivia/.env). The refresh token
# is saved to ~/.rivia/google_token.json, mode 600.
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")

SCOPE_READ = "https://www.googleapis.com/auth/calendar.events.readonly"
SCOPE_CALENDAR_APP = "https://www.googleapis.com/auth/calendar.app.created"
SCOPE_DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
SCOPES_WRITE = (SCOPE_READ, SCOPE_CALENDAR_APP, SCOPE_DRIVE_FILE)
SCOPE_ALL = " ".join(SCOPES_WRITE)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
HTTP_TIMEOUT = 10
CLOCK_GAP_S = 120
TOKEN_SKEW_S = 60

FIX_LOGIN = "Google sign-in expired or was revoked. Run: rivia sched google-login"
FIX_RELOGIN = (
    "Google sign-in is missing Drive and calendar write scopes. "
    "Run: rivia sched google-login --write"
)


class GcalError(Exception):
    def __init__(self, code, msg, step="calendar", **extra):
        super().__init__(msg)
        self.code = code
        self.step = step
        self.extra = extra


def auth_url():
    return os.environ.get("RIVIA_GOOGLE_AUTH_URL", AUTH_URL)


def token_url():
    return os.environ.get("RIVIA_GOOGLE_TOKEN_URL", TOKEN_URL)


def dot_dir():
    override = os.environ.get("RIVIA_DOTDIR")
    if override:
        return override
    return os.path.expanduser("~/.rivia")


def token_path():
    override = os.environ.get("RIVIA_GOOGLE_TOKEN_PATH")
    if override:
        return override
    return os.path.join(dot_dir(), "google_token.json")


def _parse_env_file(path, out):
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if k.startswith("RIVIA_GOOGLE_") and k not in out:
            out[k] = v


def load_google_env():
    """Fill RIVIA_GOOGLE_* from ~/.rivia/.env or ~/rivia/.env if they are not already set."""
    if os.environ.get("RIVIA_GOOGLE_CLIENT_ID") and os.environ.get("RIVIA_GOOGLE_CLIENT_SECRET") \
            and os.environ.get("RIVIA_GOOGLE_WEB_CLIENT_ID") and os.environ.get("RIVIA_GOOGLE_WEB_CLIENT_SECRET"):
        return
    found = {}
    _parse_env_file(os.path.join(dot_dir(), ".env"), found)
    _parse_env_file(os.path.expanduser(os.path.join(os.environ.get("RIVIA_DIR", "~/rivia"), ".env")), found)
    for k, v in found.items():
        os.environ.setdefault(k, v)


def client_creds():
    load_google_env()
    cid = (os.environ.get("RIVIA_GOOGLE_CLIENT_ID") or "").strip()
    sec = (os.environ.get("RIVIA_GOOGLE_CLIENT_SECRET") or "").strip()
    if not cid or not sec:
        raise GcalError(
            "not_configured",
            "Missing RIVIA_GOOGLE_CLIENT_ID or RIVIA_GOOGLE_CLIENT_SECRET. "
            "Quen puts them in ~/.rivia/.env (never paste them in chat).",
            step="input",
        )
    return cid, sec


def pkce():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# Last observed clock gap (Google Date header minus local now), seconds, or None.
_clock_gap_s = None


def reset_clock():
    global _clock_gap_s
    _clock_gap_s = None


def note_clock(date_header):
    """Compare Google's Date header with the local clock. A gap over 2 minutes is reported in status."""
    global _clock_gap_s
    if not date_header:
        return
    try:
        google_t = datetime.strptime(date_header.strip(), "%a, %d %b %Y %H:%M:%S GMT")
        # HTTP dates are GMT
        from datetime import timezone
        google_s = google_t.replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return
    _clock_gap_s = google_s - time.time()


def clock_status():
    gap = _clock_gap_s
    if gap is None:
        return {"clock_gap_s": None, "clock_ok": True}
    return {"clock_gap_s": int(round(gap)), "clock_ok": abs(gap) <= CLOCK_GAP_S}


def web_client_creds():
    """(client_id, client_secret, client_tag) for the app's Google sign-in (POST /app/login).
    The Android app's serverClientId must be a Web application client. google-login uses a
    random 127.0.0.1 port, which only a Desktop client allows, so a separate Web client can be
    set as RIVIA_GOOGLE_WEB_CLIENT_ID / _SECRET; tokens it issues are saved with client "web" so
    refresh uses the same client. Unset: the usual RIVIA_GOOGLE_CLIENT_ID / _SECRET (tag None)."""
    load_google_env()
    cid = (os.environ.get("RIVIA_GOOGLE_WEB_CLIENT_ID") or "").strip()
    sec = (os.environ.get("RIVIA_GOOGLE_WEB_CLIENT_SECRET") or "").strip()
    if cid and sec:
        return cid, sec, "web"
    cid, sec = client_creds()
    return cid, sec, None


def creds_for(tokens):
    """The client that issued a saved token: the Web client for a "web" token, else the usual one."""
    if (tokens or {}).get("client") == "web":
        cid, sec, tag = web_client_creds()
        if tag == "web":
            return cid, sec
        raise GcalError("calendar_login_expired",
                        "This Google token came from the app sign-in, but RIVIA_GOOGLE_WEB_CLIENT_ID / "
                        "_SECRET are not set. Sign in again in the app, or run: rivia sched google-login")
    return client_creds()


def scope_set(scope):
    if not scope:
        return set()
    return set(str(scope).replace(",", " ").split())


def granted_scopes(tokens=None):
    if tokens is None:
        try:
            tokens = load_tokens()
        except GcalError:
            return set()
    return scope_set(tokens.get("scope") or "")


def has_scopes(*needed, tokens=None):
    have = granted_scopes(tokens)
    return set(needed) <= have


def missing_scopes(*needed, tokens=None):
    have = granted_scopes(tokens)
    return [s for s in needed if s not in have]


def scopes_payload(tokens=None):
    """Granted OAuth scopes on the saved token. Safe if no token is present."""
    if tokens is None:
        try:
            tokens = load_tokens()
        except GcalError:
            return {"scopes": [], "scopes_complete": False}
    scopes = sorted(granted_scopes(tokens))
    return {"scopes": scopes, "scopes_complete": has_scopes(*SCOPES_WRITE, tokens=tokens)}


def login_scope():
    """Scopes requested by rivia sched google-login (and google-login --write)."""
    return SCOPE_ALL


def require_scopes(*needed, tokens=None):
    if missing_scopes(*needed, tokens=tokens):
        raise GcalError("needs_relogin", FIX_RELOGIN, step="auth")


def fail_payload(err: GcalError):
    out = {"ok": False, "error": {"code": err.code, "msg": str(err), "step": err.step},
           "status": clock_status()}
    if err.extra:
        out["error"].update(err.extra)
    return out


def _http(method, url, headers=None, data=None, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            note_clock(r.headers.get("Date"))
            return r.status, dict(r.headers), body
    except urllib.error.HTTPError as e:
        body = e.read() if e.fp else b""
        note_clock(e.headers.get("Date") if e.headers else None)
        return e.code, dict(e.headers or {}), body


def _is_timeout(exc):
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, socket.timeout):
        return True
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return True
        if "timed out" in str(reason).lower():
            return True
    return False


def http_request(method, url, headers=None, data=None, timeout=HTTP_TIMEOUT):
    """One request, with a single retry on timeout or 5xx."""
    last = None
    for attempt in (1, 2):
        try:
            status, hdrs, body = _http(method, url, headers=headers, data=data, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt == 1 and _is_timeout(e):
                continue
            raise GcalError("calendar_http", "Google request failed: %s" % e)
        if status >= 500 and attempt == 1:
            last = status
            continue
        return status, hdrs, body
    if isinstance(last, int):
        raise GcalError("calendar_http", "Google returned HTTP %s after a retry." % last)
    raise GcalError("calendar_http", "Google request timed out after a retry.")


def _token_error(body):
    try:
        data = json.loads(body.decode() if isinstance(body, (bytes, bytearray)) else body)
    except ValueError:
        data = {}
    err = data.get("error")
    if err == "invalid_grant":
        raise GcalError("calendar_login_expired", FIX_LOGIN)
    msg = data.get("error_description") or err or (body[:200] if body else "token request failed")
    raise GcalError("calendar_token", "Google token request failed: %s" % msg)


def exchange_code(code, verifier, redirect_uri):
    cid, sec = client_creds()
    payload = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": cid,
        "client_secret": sec,
        "code_verifier": verifier,
    }).encode()
    status, _, body = http_request("POST", token_url(),
                                   headers={"Content-Type": "application/x-www-form-urlencoded"},
                                   data=payload)
    if status != 200:
        _token_error(body)
    data = json.loads(body.decode())
    if not data.get("refresh_token"):
        raise GcalError(
            "no_refresh_token",
            "Google did not return a refresh token. Revoke Rivia in Google Account "
            "permissions and run: rivia sched google-login",
        )
    return data


def refresh_access(tokens):
    cid, sec = creds_for(tokens)
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise GcalError("calendar_login_expired", FIX_LOGIN)
    payload = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": cid,
        "client_secret": sec,
    }).encode()
    status, _, body = http_request("POST", token_url(),
                                   headers={"Content-Type": "application/x-www-form-urlencoded"},
                                   data=payload)
    if status != 200:
        _token_error(body)
    data = json.loads(body.decode())
    tokens = dict(tokens)
    tokens["access_token"] = data["access_token"]
    expires_in = int(data.get("expires_in") or 3600)
    tokens["expiry"] = int(time.time()) + expires_in
    if data.get("refresh_token"):
        tokens["refresh_token"] = data["refresh_token"]
    if data.get("scope"):
        tokens["scope"] = data["scope"]
    save_tokens(tokens)
    return tokens


def load_tokens():
    path = token_path()
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        raise GcalError("calendar_login_expired",
                        "No Google token yet. Run: rivia sched google-login")
    except (OSError, ValueError) as e:
        raise GcalError("calendar_token", "Could not read %s: %s" % (path, e))
    if not isinstance(data, dict) or not data.get("refresh_token"):
        raise GcalError("calendar_login_expired", FIX_LOGIN)
    return data


def save_tokens(tokens):
    path = token_path()
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    os.chmod(os.path.dirname(os.path.abspath(path)), 0o700)
    tmp = path + ".tmp"
    payload = {
        "refresh_token": tokens["refresh_token"],
        "access_token": tokens.get("access_token") or "",
        "expiry": int(tokens.get("expiry") or 0),
        "scope": tokens.get("scope") or SCOPE_READ,
        "token_type": tokens.get("token_type") or "Bearer",
    }
    if tokens.get("client") == "web":  # issued to the Web client by the app's sign-in
        payload["client"] = "web"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=1)
            f.write("\n")
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return path


def access_token():
    tokens = load_tokens()
    expiry = int(tokens.get("expiry") or 0)
    if tokens.get("access_token") and expiry > time.time() + TOKEN_SKEW_S:
        return tokens["access_token"], tokens
    tokens = refresh_access(tokens)
    return tokens["access_token"], tokens
