# rivia_gcal.py: Google sign-in and Calendar reads. Plain urllib, no Google library.
#
#   python rivia_gcal.py google-login
#   python rivia_gcal.py events <day>
#   python rivia_gcal.py signin-check
#
# Client id/secret come from the environment (rivia.sh loads ~/.rivia/.env).
# The refresh token is saved to ~/.rivia/google_token.json, mode 600.
# Never print or log token values.
#
# Sign-in listens on 127.0.0.1 (PKCE + state). Chrome reaching that listener
# from the tablet is unverified; if the page doesn't load, paste the address
# from the browser bar. Google's "code on another device" flow is not used:
# it cannot request Calendar scopes.
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from queue import Empty, Queue
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")

SCOPE_READ = "https://www.googleapis.com/auth/calendar.events.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
HTTP_TIMEOUT = 10
CLOCK_GAP_S = 120
TOKEN_SKEW_S = 60

FIX_LOGIN = "Google sign-in expired or was revoked. Run: rivia sched google-login"


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


def events_url():
    return os.environ.get("RIVIA_GOOGLE_EVENTS_URL", EVENTS_URL)


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
    if os.environ.get("RIVIA_GOOGLE_CLIENT_ID") and os.environ.get("RIVIA_GOOGLE_CLIENT_SECRET"):
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
    cid, sec = client_creds()
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


WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def resolve_day(s, now=None):
    """today, tomorrow, a weekday name meaning the next one (including today), or YYYY-MM-DD.

    Returns (date, asked_label). Times are America/New_York.
    """
    raw = (s or "").strip()
    if not raw:
        raise GcalError("bad_day", "usage: rivia sched events <day>", step="input")
    key = raw.lower()
    today = (now or datetime.now(TZ)).date()
    if key in ("today",):
        return today, "today"
    if key in ("tomorrow",):
        return today + timedelta(days=1), "tomorrow"
    if key in WEEKDAYS:
        want = WEEKDAYS[key]
        ahead = (want - today.weekday()) % 7
        return today + timedelta(days=ahead), raw.lower()
    try:
        y, m, d = map(int, raw.split("-"))
        return date(y, m, d), raw
    except ValueError:
        raise GcalError(
            "bad_day",
            "Day must be today, tomorrow, a weekday name, or YYYY-MM-DD: %s" % raw,
            step="input",
        )


def day_window(day: date):
    start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=TZ)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def norm_google_event(item):
    """Map a Google Calendar event to {title, start, end, location, all_day}. None if cancelled or unusable."""
    if not isinstance(item, dict):
        return None
    if (item.get("status") or "").lower() == "cancelled":
        return None
    start, end = item.get("start") or {}, item.get("end") or {}
    title = str(item.get("summary") or item.get("title") or "")
    location = str(item.get("location") or "")
    if "date" in start and "dateTime" not in start:
        return {
            "title": title,
            "start": start.get("date"),
            "end": (end or {}).get("date") or start.get("date"),
            "location": location,
            "all_day": True,
        }
    dt_s = start.get("dateTime")
    dt_e = end.get("dateTime")
    if not dt_s or not dt_e:
        return None
    return {"title": title, "start": dt_s, "end": dt_e, "location": location, "all_day": False}


def parse_redirect(text):
    """Extract code/state/error from a pasted redirect address or a query string."""
    raw = (text or "").strip().strip("'\"")
    if not raw:
        raise GcalError("bad_redirect", "Paste the full address from the browser bar.", step="input")
    if "://" not in raw and raw.startswith("/"):
        raw = "http://127.0.0.1" + raw
    elif "://" not in raw and ("code=" in raw or "error=" in raw or raw.startswith("?")):
        raw = "http://127.0.0.1/" + (raw if raw.startswith("?") else "?" + raw)
    parts = urllib.parse.urlparse(raw)
    qs = urllib.parse.parse_qs(parts.query)
    if parts.fragment:
        # some clients put the query in the fragment
        qs_f = urllib.parse.parse_qs(parts.fragment)
        for k, v in qs_f.items():
            qs.setdefault(k, v)
    error = (qs.get("error") or [None])[0]
    code = (qs.get("code") or [None])[0]
    state = (qs.get("state") or [None])[0]
    return {"error": error, "code": code, "state": state, "raw": raw}


def authorize_url(redirect_uri, state, challenge, scope=SCOPE_READ):
    cid, _ = client_creds()
    q = urllib.parse.urlencode({
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    })
    return auth_url() + "?" + q


class _LoopbackHandler(BaseHTTPRequestHandler):
    queue: Queue
    page = ("<!doctype html><meta charset=utf-8><title>Rivia</title>"
            "<body style='font-family:sans-serif;padding:2em'><h1>Signed in to Rivia</h1>"
            "<p>You can close this tab and return to Termux.</p></body>")
    err_page = ("<!doctype html><meta charset=utf-8><title>Rivia</title>"
                "<body style='font-family:sans-serif;padding:2em'><h1>Sign-in didn't finish</h1>"
                "<p>Return to Termux for the error, or paste this address there if asked.</p></body>")

    def do_GET(self):
        parsed = parse_redirect(self.path)
        ok = bool(parsed.get("code") and parsed.get("state")) and not parsed.get("error")
        body = (self.page if ok else self.err_page).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.queue.put(("http", parsed))

    def log_message(self, *a):
        pass


def start_loopback(queue):
    class Handler(_LoopbackHandler):
        pass
    Handler.queue = queue
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    redirect_uri = "http://127.0.0.1:%d/" % port
    return server, redirect_uri


def _stdin_reader(queue, stdin):
    try:
        line = stdin.readline()
    except Exception:  # noqa: BLE001
        return
    if line:
        queue.put(("paste", line))


def wait_for_redirect(queue, timeout=300, stdin=None):
    """Wait for the loopback request or a pasted address. timeout is seconds."""
    if stdin is None:
        stdin = sys.stdin
    reader = None
    # Only start a paste reader when we can actually read a line (a TTY, or a
    # test that passed a file). Non-interactive with no stdin stays on HTTP.
    if stdin and not stdin.closed and (stdin.isatty() or stdin is not sys.stdin):
        reader = threading.Thread(target=_stdin_reader, args=(queue, stdin), daemon=True)
        reader.start()
    try:
        source, payload = queue.get(timeout=timeout)
    except Empty:
        raise GcalError("login_timeout",
                        "No sign-in came back. Re-run rivia sched google-login, keep Termux in "
                        "the foreground, and if Chrome can't load 127.0.0.1, paste the address "
                        "from the browser bar.")
    if source == "paste":
        return parse_redirect(payload)
    return payload


def google_login(*, open_browser=True, timeout=300, stdin=None, scope=SCOPE_READ):
    if scope != SCOPE_READ:
        raise GcalError(
            "bad_scope",
            "Write scopes are step 8. For now the only sign-in is calendar.events.readonly.",
            step="input",
        )
    client_creds()
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(24)
    queue: Queue = Queue()
    server, redirect_uri = start_loopback(queue)
    try:
        url = authorize_url(redirect_uri, state, challenge, scope=scope)
        print("Sign in with Google in the tablet's browser.", file=sys.stderr)
        print(url, file=sys.stderr)
        print(file=sys.stderr)
        print("Chrome reaching 127.0.0.1 from the tablet is not guaranteed. If the page",
              file=sys.stderr)
        print("doesn't load (or Android pauses Termux), copy the address from the",
              file=sys.stderr)
        print("browser bar and paste it here, then press Enter.", file=sys.stderr)
        print(file=sys.stderr)
        if open_browser:
            opened = _open_browser(url)
            if opened:
                print("Opened the link with termux-open-url (or $BROWSER).", file=sys.stderr)
            else:
                print("Could not open a browser automatically. Open the link above yourself.",
                      file=sys.stderr)
        print("Waiting for the browser or a pasted address...", file=sys.stderr)
        parsed = wait_for_redirect(queue, timeout=timeout, stdin=stdin)
        if parsed.get("error"):
            raise GcalError("login_denied", "Google returned %s. Sign-in was not completed." % parsed["error"],
                            step="input")
        if not parsed.get("code"):
            raise GcalError("bad_redirect", "That address had no code. Paste the full address from the bar.",
                            step="input")
        if parsed.get("state") != state:
            raise GcalError("bad_state", "The state on the redirect did not match. Try rivia sched google-login again.",
                            step="input")
        tokens = exchange_code(parsed["code"], verifier, redirect_uri)
        expiry = int(time.time()) + int(tokens.get("expires_in") or 3600)
        tokens["expiry"] = expiry
        tokens["scope"] = tokens.get("scope") or scope
        path = save_tokens(tokens)
        return {"ok": True, "saved": path, "scope": tokens["scope"], "status": clock_status()}
    finally:
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            server.server_close()
        except Exception:  # noqa: BLE001
            pass


def _open_browser(url):
    # Prefer Termux; never fail the login if this is missing. Shared storage is
    # noexec, but these are PATH binaries in Termux home, not in ~/storage/shared.
    for cmd in (("termux-open-url", url), ("termux-open", url)):
        try:
            import subprocess
            r = subprocess.run(cmd, timeout=5, capture_output=True)
            if r.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    browser = os.environ.get("BROWSER")
    if browser:
        try:
            import subprocess
            r = subprocess.run((browser, url), timeout=5, capture_output=True)
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    return False


# Auth failures that mean the owner needs to sign in again.
AUTH_FAIL_CODES = frozenset({"calendar_login_expired", "calendar_token", "no_refresh_token"})
NOTICE_DAY_KEY = "gcal_login_notice_day"
CHECK_DAY_KEY = "gcal_signin_check_day"
NOTIFY_TITLE = "Rivia"
NOTIFY_CONTENT = "Google sign-in expired. Run rivia sched google-login"
NOTIFY_TIMEOUT_S = 8
DEFAULT_CHECK_HOUR = 5
DEFAULT_CHECK_MINUTE = 30
DEFAULT_CHECK_INTERVAL_S = 60


def is_auth_failure(err):
    if not isinstance(err, GcalError):
        return False
    if err.code in AUTH_FAIL_CODES:
        return True
    return "invalid_grant" in str(err).lower()


def record_calendar_auth_failure(err, now=None):
    """Remember an auth/refresh failure from a calendar read (the same read
    plan_day uses, and that the 5:30 AM auto-write job should call later).

    At most one record per New York day. Returns whether this is the first
    failure today so a notifier can hook in (Termux notification, not the
    phone app — there is no unsolicited app inbox).
    """
    if not is_auth_failure(err):
        return {"first_today": False, "reason": "not_auth"}
    day = (now or datetime.now(TZ)).date().isoformat()
    try:
        import rivia_planner as rp
        con = rp.db_open()
        try:
            last = rp.get_meta(con, NOTICE_DAY_KEY)
            if last == day:
                return {"first_today": False, "reason": "already_today", "day": day}
            rp.set_meta(con, NOTICE_DAY_KEY, day)
            rp.set_meta(con, "gcal_login_notice", FIX_LOGIN)
            return {"first_today": True, "reason": "recorded", "day": day, "fix": FIX_LOGIN}
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return {"first_today": True, "reason": "no_db", "fix": FIX_LOGIN}


def post_signin_expired_notification():
    """Post an Android notification via Termux:API. Never raises.

    If termux-notification is missing (a dev box, or Termux:API not
    installed), log and continue so the server stays up.
    """
    cmd = ["termux-notification", "--title", NOTIFY_TITLE, "--content", NOTIFY_CONTENT]
    try:
        r = subprocess.run(cmd, timeout=NOTIFY_TIMEOUT_S, capture_output=True)
        if r.returncode != 0:
            print("rivia: termux-notification failed (exit %s); continuing." % r.returncode,
                  file=sys.stderr)
            return {"ok": False, "reason": "exit", "code": r.returncode}
        return {"ok": True}
    except FileNotFoundError:
        print("rivia: termux-notification not found; continuing.", file=sys.stderr)
        return {"ok": False, "reason": "missing"}
    except subprocess.TimeoutExpired:
        print("rivia: termux-notification timed out; continuing.", file=sys.stderr)
        return {"ok": False, "reason": "timeout"}
    except Exception as e:  # noqa: BLE001
        print("rivia: termux-notification failed (%s); continuing." % type(e).__name__,
              file=sys.stderr)
        return {"ok": False, "reason": "error"}


def maybe_notify_signin_expired(err, now=None):
    """Record an auth/refresh failure and, on the first one today, notify
    via termux-notification. Safe for plan_day, the 05:30 sign-in check,
    and the later 7b auto-write job. Never writes to the calendar.
    """
    rec = record_calendar_auth_failure(err, now=now)
    if not rec.get("first_today"):
        rec = dict(rec, notified=False)
        return rec
    sent = post_signin_expired_notification()
    rec = dict(rec, notified=bool(sent.get("ok")), notify=sent)
    return rec


def _meta_get(key):
    try:
        import rivia_planner as rp
        con = rp.db_open()
        try:
            return rp.get_meta(con, key)
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return None


def _meta_set(key, value):
    import rivia_planner as rp
    con = rp.db_open()
    try:
        rp.set_meta(con, key, value)
    finally:
        con.close()


def last_signin_check_day():
    return _meta_get(CHECK_DAY_KEY)


def mark_signin_check_ran(day):
    try:
        _meta_set(CHECK_DAY_KEY, day)
    except Exception:  # noqa: BLE001
        print("rivia: could not record sign-in check day; continuing.", file=sys.stderr)


def signin_check_at_hm():
    raw = (os.environ.get("RIVIA_SIGNIN_CHECK_TIME") or "05:30").strip()
    try:
        h, m = raw.split(":")
        h, m = int(h), int(m)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError(raw)
        return h, m
    except ValueError:
        return DEFAULT_CHECK_HOUR, DEFAULT_CHECK_MINUTE


def signin_check_due(now, last_ran_day=None, check_hour=None, check_minute=None):
    """True if now is at or after today's 05:30 ET and today's run was missed."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    now = now.astimezone(TZ)
    if check_hour is None or check_minute is None:
        check_hour, check_minute = signin_check_at_hm()
    cutoff = now.replace(hour=check_hour, minute=check_minute, second=0, microsecond=0)
    if now < cutoff:
        return False
    last = last_ran_day if last_ran_day is not None else last_signin_check_day()
    return last != now.date().isoformat()


def signin_check_enabled():
    return os.environ.get("RIVIA_SIGNIN_CHECK", "1").strip().lower() not in ("0", "false", "off", "no")


def plan_window(first_day, last_day, *, before=2, after=2):
    start = first_day - timedelta(days=before)
    end = last_day + timedelta(days=after + 1)
    tmin = datetime(start.year, start.month, start.day, 0, 0, 0, tzinfo=TZ).isoformat()
    tmax = datetime(end.year, end.month, end.day, 0, 0, 0, tzinfo=TZ).isoformat()
    return tmin, tmax


def run_signin_check(now=None, *, mark_ran=True):
    """Read the calendar just enough to refresh the token. Never writes.

    On calendar_login_expired or any token/refresh error, notify via
    termux-notification at most once per New York day (existing meta
    record). The 7b auto-write job can call this, or call
    maybe_notify_signin_expired after its own events_for_plan read.
    Never raises; missing termux-notification is logged and ignored.
    """
    now = now or datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    now = now.astimezone(TZ)
    today = now.date()
    out = {"ok": True, "day": today.isoformat(), "notified": False, "written": False}
    checked = False
    try:
        tmin, tmax = plan_window(today, today, before=0, after=0)
        list_events_between(tmin, tmax)
        out["message"] = "Google sign-in is valid."
        out["status"] = clock_status()
        checked = True
    except GcalError as e:
        rec = maybe_notify_signin_expired(e, now=now)
        out["ok"] = False
        out["error"] = {"code": e.code, "msg": str(e), "step": e.step}
        out["notified"] = bool(rec.get("notified"))
        out["status"] = clock_status()
        checked = True
    except Exception as e:  # noqa: BLE001
        print("rivia: sign-in check failed (%s); continuing." % type(e).__name__, file=sys.stderr)
        out["ok"] = False
        out["error"] = {"code": "internal", "msg": type(e).__name__, "step": "calendar"}
    if mark_ran and checked:
        mark_signin_check_ran(today.isoformat())
    return out


async def signin_check_loop(*, get_now=None, sleep=None, interval_s=None):
    """G1-style timer: check once a minute, catch up on start if today's
    05:30 ET run was missed (tablet was off). Cancelled on server shutdown.
    """
    get_now = get_now or (lambda: datetime.now(TZ))
    sleep = sleep or asyncio.sleep
    if interval_s is None:
        interval_s = DEFAULT_CHECK_INTERVAL_S
    while True:
        try:
            now = get_now()
            if signin_check_due(now):
                run_signin_check(now=now, mark_ran=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            print("rivia: sign-in check failed (%s); continuing." % type(e).__name__,
                  file=sys.stderr)
        await sleep(interval_s)


def list_events_between(time_min, time_max):
    """Read primary events in [time_min, time_max). time_* are RFC3339 strings."""
    token, tokens = access_token()
    items = []
    page = None
    retried_auth = False
    while True:
        q = {
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        if page:
            q["pageToken"] = page
        url = events_url() + "?" + urllib.parse.urlencode(q)
        status, _, body = http_request(
            "GET", url,
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
        )
        if status == 401 and not retried_auth:
            retried_auth = True
            tokens = refresh_access(tokens)
            token = tokens["access_token"]
            continue
        if status in (401, 403):
            try:
                data = json.loads(body.decode()) if body else {}
            except ValueError:
                data = {}
            err = (data.get("error") or {})
            reason = ""
            if isinstance(err, dict):
                reasons = err.get("errors") or []
                reason = (reasons[0] or {}).get("reason") if reasons else err.get("status") or ""
            if status == 401:
                raise GcalError("calendar_login_expired", FIX_LOGIN)
            raise GcalError("calendar_http", "Google Calendar refused the request (%s)." % (reason or status))
        if status != 200:
            raise GcalError("calendar_http", "Google Calendar returned HTTP %s." % status)
        data = json.loads(body.decode())
        items.extend(data.get("items") or [])
        page = data.get("nextPageToken")
        if not page:
            break
    events = []
    for item in items:
        ev = norm_google_event(item)
        if ev:
            events.append(ev)
    return events


def list_events(day: date):
    time_min, time_max = day_window(day)
    return list_events_between(time_min, time_max)


def events_for_plan(first_day: date, last_day: date, *, before=2, after=2, now=None):
    """Primary events from `before` days before first_day to `after` days after last_day.

    This is the calendar read plan_day / plan_week use, and the same read the
    5:30 AM ET auto-write job (step 7b, not built yet) should use. Auth and
    refresh failures are recorded and (once per day) notified via Termux.
    """
    tmin, tmax = plan_window(first_day, last_day, before=before, after=after)
    try:
        return list_events_between(tmin, tmax)
    except GcalError as e:
        maybe_notify_signin_expired(e, now=now)
        raise


def cmd_events(day_arg, now=None):
    reset_clock()
    day, asked = resolve_day(day_arg, now=now)
    events = list_events(day)
    status = clock_status()
    return {
        "ok": True,
        "day": day.isoformat(),
        "asked": asked,
        "events": events,
        "status": status,
    }


def cmd_google_login(argv):
    if "--write" in argv:
        raise GcalError(
            "bad_scope",
            "Write scopes are step 8. For now run: rivia sched google-login",
            step="input",
        )
    return google_login()


def cmd_signin_check(now=None):
    """Run the Google sign-in check now (does not wait for 05:30).

    Does not mark the daily 05:30 run, so the in-server timer still
    catches up or fires at 05:30. Notification is still at most once
    per New York day.
    """
    return run_signin_check(now=now, mark_ran=False)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__ or "rivia_gcal.py google-login | events <day> | signin-check")
        return 2
    cmd = argv[0]
    try:
        if cmd == "google-login":
            out = cmd_google_login(argv[1:])
        elif cmd == "events":
            if len(argv) < 2:
                raise GcalError("bad_day", "usage: rivia sched events <day>", step="input")
            out = cmd_events(argv[1])
        elif cmd == "signin-check":
            out = cmd_signin_check()
        else:
            raise GcalError("bad_command",
                            "Unknown command: %s. Use google-login, events <day>, or signin-check." % cmd,
                            step="input")
        print(json.dumps(out, indent=1))
        return 0 if out.get("ok") else 1
    except GcalError as e:
        print(json.dumps(fail_payload(e), indent=1))
        return 1
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False,
                          "error": {"code": "internal", "msg": repr(e), "step": "calendar"},
                          "status": clock_status()}, indent=1))
        return 1


if __name__ == "__main__":
    sys.exit(main())
