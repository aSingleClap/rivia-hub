# rivia_applogin.py: POST /app/login. Google sign-in in the Rivia app gives the tablet its
# Google token and gives the app its app token, in one step.
#
# The app signs in with Google (offline access, serverClientId = the Web client) and posts
# {"server_auth_code": "..."}. The server exchanges the code at Google's token endpoint, checks
# the id_token (aud = the Web client, iss = Google, email_verified, email = RIVIA_LOGIN_EMAIL),
# requires a refresh_token and every scope google-login asks for, backs up and saves the Google
# token in google-login's format, then mints the app token the way `rivia token` does.
#
# The id_token is not signature-checked: it comes straight from Google's token endpoint over
# TLS in exchange for our client secret, which OpenID Connect Core 3.1.3.7 accepts in place of
# the signature. Nothing here ever logs a code, a token or an email.
import asyncio
import json
import os
import shutil
import sys
import threading
import time
import urllib.parse
from collections import deque

import jwt
from starlette.requests import Request
from starlette.responses import JSONResponse

import rivia_gcal as gcal

LOGIN_PATH = "/app/login"
RATE_MAX, RATE_WINDOW_S = 5, 60           # about 5 sign-ins a minute, for the whole server
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
CODE_MAX_LEN = 2048
# Android server auth codes are exchanged with an empty redirect_uri (Google's Android docs:
# "If you don't have a web version of your app, you can specify an empty string").
REDIRECT_URI = ""

_hits = deque()
_hits_lock = threading.Lock()


class Refused(Exception):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _log(msg):
    print("rivia: app login %s" % msg, file=sys.stderr)


def reset_rate_limit():
    """For tests."""
    with _hits_lock:
        _hits.clear()


def rate_ok(now=None):
    now = time.time() if now is None else now
    with _hits_lock:
        while _hits and _hits[0] <= now - RATE_WINDOW_S:
            _hits.popleft()
        if len(_hits) >= RATE_MAX:
            return False
        _hits.append(now)
        return True


def login_email():
    return (os.environ.get("RIVIA_LOGIN_EMAIL") or "").strip()


def _exchange(code, cid, sec):
    """Code -> Google's token response (a dict). Raises Refused; never includes the code."""
    payload = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": cid,
        "client_secret": sec,
        "redirect_uri": REDIRECT_URI,
    }).encode()
    try:
        status, _, body = gcal.http_request("POST", gcal.token_url(),
                                            headers={"Content-Type": "application/x-www-form-urlencoded"},
                                            data=payload)
    except gcal.GcalError:
        raise Refused(502, "google_unreachable")
    if status != 200:
        raise Refused(403, "google_refused_code")
    try:
        data = json.loads(body.decode())
    except (ValueError, UnicodeDecodeError):
        raise Refused(502, "google_bad_reply")
    if not isinstance(data, dict):
        raise Refused(502, "google_bad_reply")
    return data


def check_id_token(id_token, web_client_id, email, now=None):
    """The id_token Google returned with the code exchange: Raises Refused with the reason."""
    if not id_token or not isinstance(id_token, str):
        raise Refused(403, "no_id_token")
    try:
        claims = jwt.decode(id_token, options={"verify_signature": False})
    except jwt.PyJWTError:
        raise Refused(403, "bad_id_token")
    now = time.time() if now is None else now
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if web_client_id not in auds:
        raise Refused(403, "wrong_audience")
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise Refused(403, "wrong_issuer")
    try:
        if float(claims.get("exp") or 0) < now - 60:
            raise Refused(403, "expired_id_token")
    except (TypeError, ValueError):
        raise Refused(403, "bad_id_token")
    if claims.get("email_verified") not in (True, "true"):
        raise Refused(403, "email_not_verified")
    if claims.get("email") != email:
        raise Refused(403, "wrong_account")
    return claims


def _backup_token_file():
    """Copy the current google_token.json aside (mode 600) before it is replaced."""
    path = gcal.token_path()
    if not os.path.exists(path):
        return None
    dest = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = "%s.bak-%s-%d" % (path, time.strftime("%Y%m%d-%H%M%S"), n)
    shutil.copy2(path, dest)
    os.chmod(dest, 0o600)
    return dest


def login(code, mint, now=None):
    """The whole sign-in. Returns the JSON reply; raises Refused. mint(kind) is
    rivia_auth.mint_door_token, passed in so this module never imports rivia_auth."""
    email = login_email()
    if not email:
        raise Refused(503, "not_configured")
    try:
        cid, sec, client_tag = gcal.web_client_creds()
    except gcal.GcalError:
        raise Refused(503, "not_configured")
    data = _exchange(code, cid, sec)
    check_id_token(data.get("id_token"), cid, email, now=now)
    if not data.get("refresh_token") or not data.get("access_token"):
        raise Refused(403, "no_refresh_token")
    if gcal.missing_scopes(*gcal.SCOPES_WRITE, tokens={"scope": data.get("scope") or ""}):
        raise Refused(403, "missing_scopes")
    tokens = {
        "refresh_token": data["refresh_token"],
        "access_token": data["access_token"],
        "expiry": int(time.time() if now is None else now) + int(data.get("expires_in") or 3600),
        "scope": data.get("scope"),
        "token_type": data.get("token_type") or "Bearer",
    }
    if client_tag:
        tokens["client"] = client_tag
    try:
        _backup_token_file()
        gcal.save_tokens(tokens)
    except OSError:
        raise Refused(500, "token_save_failed")
    try:
        info = mint("app")
    except (Exception, SystemExit):  # noqa: BLE001
        raise Refused(500, "app_token_failed")
    return {"ok": True, "app_token": info["token"], "expires_in": int(info["days"]) * 86400}


def _reply(obj, status=200):
    return JSONResponse(obj, status_code=status, headers={"Cache-Control": "no-store"})


async def handle(request: Request, mint):
    """Starlette handler for POST /app/login (no bearer, no CORS, rate-limited)."""
    if not rate_ok():
        _log("refused (rate_limited)")
        return _reply({"ok": False, "error": "rate_limited"}, 429)
    try:
        body = json.loads((await request.body()) or b"null")
    except (ValueError, UnicodeDecodeError):
        body = None
    code = body.get("server_auth_code") if isinstance(body, dict) else None
    if not isinstance(code, str) or not (10 <= len(code.strip()) <= CODE_MAX_LEN):
        _log("refused (bad_request)")
        return _reply({"ok": False, "error": "bad_request"}, 400)
    try:
        out = await asyncio.to_thread(login, code.strip(), mint)
    except Refused as e:
        _log("refused (%s)" % e.reason)
        return _reply({"ok": False, "error": e.reason}, e.status)
    except Exception as e:  # noqa: BLE001
        _log("failed (%s)" % type(e).__name__)
        return _reply({"ok": False, "error": "internal"}, 500)
    _log("ok (Google token saved, app token minted)")
    return _reply(out)
