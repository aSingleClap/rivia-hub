# rivia_gcal.py: Google sign-in and Calendar reads. Plain urllib, no Google library.
#
#   python rivia_gcal.py google-login [--write]
#   python rivia_gcal.py events <day>
#   python rivia_gcal.py signin-check
#   python rivia_gcal.py backup
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

from rivia_google import (  # noqa: F401  Google credentials and tokens (core); re-exported here
    TZ,
    SCOPE_READ,
    SCOPE_CALENDAR_APP,
    SCOPE_DRIVE_FILE,
    SCOPES_WRITE,
    SCOPE_ALL,
    AUTH_URL,
    TOKEN_URL,
    HTTP_TIMEOUT,
    CLOCK_GAP_S,
    TOKEN_SKEW_S,
    FIX_LOGIN,
    FIX_RELOGIN,
    GcalError,
    auth_url,
    token_url,
    dot_dir,
    token_path,
    load_google_env,
    client_creds,
    pkce,
    reset_clock,
    note_clock,
    clock_status,
    web_client_creds,
    creds_for,
    scope_set,
    granted_scopes,
    has_scopes,
    missing_scopes,
    scopes_payload,
    login_scope,
    require_scopes,
    fail_payload,
    http_request,
    exchange_code,
    refresh_access,
    load_tokens,
    save_tokens,
    access_token,
)

EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"
RIVIA_CALENDAR_NAME = "Rivia"
META_RIVIA_ID = "gcal_rivia_id"
DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
DRIVE_BACKUP_NAME = "rivia-scheduler.db"
DRIVE_BACKUP_MIME = "application/octet-stream"
def events_url():
    return os.environ.get("RIVIA_GOOGLE_EVENTS_URL", EVENTS_URL)


def drive_upload_url():
    return (os.environ.get("RIVIA_GOOGLE_DRIVE_UPLOAD_URL", DRIVE_UPLOAD_URL) or DRIVE_UPLOAD_URL).rstrip("/")


def drive_backup_name():
    return (os.environ.get("RIVIA_DRIVE_BACKUP_NAME") or DRIVE_BACKUP_NAME).strip() or DRIVE_BACKUP_NAME

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


def google_login(*, open_browser=True, timeout=300, stdin=None, scope=None):
    if scope is None:
        scope = login_scope()
    unknown = scope_set(scope) - set(SCOPES_WRITE)
    if unknown:
        raise GcalError(
            "bad_scope",
            "Unknown Google scope.",
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
        status = clock_status()
        status.update(scopes_payload(tokens))
        return {"ok": True, "saved": path, "scope": tokens["scope"],
                "scopes": status["scopes"], "scopes_complete": status["scopes_complete"],
                "status": status}
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
NOTIFY_SCOPE_CONTENT = "Google sign-in needs new scopes. Run rivia sched google-login --write"
SCOPE_NOTICE_DAY_KEY = "gcal_scope_notice_day"
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
        con = _sched_db()
        try:
            last = _store().get_meta(con, NOTICE_DAY_KEY)
            if last == day:
                return {"first_today": False, "reason": "already_today", "day": day}
            _store().set_meta(con, NOTICE_DAY_KEY, day)
            _store().set_meta(con, "gcal_login_notice", FIX_LOGIN)
            return {"first_today": True, "reason": "recorded", "day": day, "fix": FIX_LOGIN}
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return {"first_today": True, "reason": "no_db", "fix": FIX_LOGIN}


def post_termux_notification(content, title=NOTIFY_TITLE):
    """Post an Android notification via Termux:API. Never raises.

    If termux-notification is missing (a dev box, or Termux:API not
    installed), log and continue so the server stays up.
    """
    cmd = ["termux-notification", "--title", title, "--content", content]
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


def post_signin_expired_notification():
    return post_termux_notification(NOTIFY_CONTENT)


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


def maybe_notify_missing_scopes(now=None):
    """Once per New York day, remind the owner to re-sign in for write/Drive scopes.

    Does not treat the existing calendar read token as expired: reads keep
    working. Never raises.
    """
    day = (now or datetime.now(TZ)).date().isoformat()
    try:
        last = _meta_get(SCOPE_NOTICE_DAY_KEY)
        if last == day:
            return {"first_today": False, "reason": "already_today", "day": day, "notified": False}
        try:
            _meta_set(SCOPE_NOTICE_DAY_KEY, day)
        except Exception:  # noqa: BLE001
            pass
        sent = post_termux_notification(NOTIFY_SCOPE_CONTENT)
        return {"first_today": True, "reason": "recorded", "day": day,
                "notified": bool(sent.get("ok")), "notify": sent, "fix": FIX_RELOGIN}
    except Exception:  # noqa: BLE001
        sent = post_termux_notification(NOTIFY_SCOPE_CONTENT)
        return {"first_today": True, "reason": "no_db", "notified": bool(sent.get("ok")),
                "notify": sent, "fix": FIX_RELOGIN}


def _store():
    import rivia_store
    return rivia_store


def _sched_db():
    """scheduler.db, where scheduling keeps its Google bookkeeping (meta)."""
    import rivia_settings
    return _store().connect("scheduler", rivia_settings.SETTINGS_SCHEMA)


def _meta_get(key):
    try:
        con = _sched_db()
        try:
            return _store().get_meta(con, key)
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return None


def _meta_set(key, value):
    con = _sched_db()
    try:
        _store().set_meta(con, key, value)
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
    """Morning tick time: honor auto_write_time, else RIVIA_SIGNIN_CHECK_TIME, else 05:30."""
    try:
        import rivia_sched as sched
        return sched.auto_write_hm()
    except Exception:
        pass
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


def sched_timer_enabled():
    """RIVIA_SCHED_TIMER=0 parks scheduling's part of the daily timer: the 05:30 slot runs only
    the sign-in check and a Drive backup of core.db only (no auto-write or tomorrow proposal),
    and the 21:00 evening refresh is skipped."""
    return os.environ.get("RIVIA_SCHED_TIMER", "1").strip().lower() not in ("0", "false", "off", "no")


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
        scopes = scopes_payload()
        status = clock_status()
        status.update(scopes)
        out["status"] = status
        out["scopes"] = scopes["scopes"]
        out["scopes_complete"] = scopes["scopes_complete"]
        if scopes["scopes_complete"]:
            out["message"] = "Google sign-in is valid."
            out["needs_relogin"] = False
        else:
            out["needs_relogin"] = True
            out["message"] = (
                "Google sign-in is valid for calendar reads, but is missing "
                "Drive and calendar write scopes. Run: rivia sched google-login --write"
            )
            rec = maybe_notify_missing_scopes(now=now)
            out["notified"] = bool(rec.get("notified"))
        checked = True
    except GcalError as e:
        rec = maybe_notify_signin_expired(e, now=now)
        out["ok"] = False
        out["error"] = {"code": e.code, "msg": str(e), "step": e.step}
        out["notified"] = bool(rec.get("notified"))
        status = clock_status()
        status.update(scopes_payload())
        out["status"] = status
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
            sched_on = sched_timer_enabled()
            due = signin_check_due(now)
            if due and not sched_on:
                try:
                    run_signin_check(now=now, mark_ran=True)
                except Exception as e:  # noqa: BLE001
                    print("rivia: sign-in check failed (%s); continuing." % type(e).__name__,
                          file=sys.stderr)
                try:
                    run_drive_backup(only=("core",))
                except Exception as e:  # noqa: BLE001
                    print("rivia: daily Drive backup failed (%s); continuing." % type(e).__name__,
                          file=sys.stderr)
            elif due:
                try:
                    import rivia_sched as sched
                    sched.run_morning_tick(now=now)
                except Exception as e:  # noqa: BLE001
                    print("rivia: morning tick failed (%s); falling back to sign-in check." % type(e).__name__,
                          file=sys.stderr)
                    run_signin_check(now=now, mark_ran=True)
                    try:
                        run_drive_backup()
                    except Exception as e2:  # noqa: BLE001
                        print("rivia: daily Drive backup failed (%s); continuing." % type(e2).__name__,
                              file=sys.stderr)
            try:
                import rivia_sched as sched
                if sched_on and sched.evening_refresh_due(now):
                    sched.run_evening_refresh(now=now)
            except Exception as e:  # noqa: BLE001
                print("rivia: evening refresh failed (%s); continuing." % type(e).__name__,
                      file=sys.stderr)
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

    This is the calendar read plan_day / plan_week and the morning auto-write
    job use. Auth and refresh failures are recorded and (once per day)
    notified via Termux.
    """
    tmin, tmax = plan_window(first_day, last_day, before=before, after=after)
    try:
        return list_events_between(tmin, tmax)
    except GcalError as e:
        maybe_notify_signin_expired(e, now=now)
        raise



def _multipart_related(metadata, raw, mime):
    boundary = "rivia_" + secrets.token_hex(16)
    meta = json.dumps(metadata).encode("utf-8")
    chunks = [
        b"--" + boundary.encode("ascii") + b"\r\n",
        b"Content-Type: application/json; charset=UTF-8\r\n\r\n",
        meta + b"\r\n",
        b"--" + boundary.encode("ascii") + b"\r\n",
        ("Content-Type: %s\r\n\r\n" % mime).encode("ascii"),
        raw,
        b"\r\n--" + boundary.encode("ascii") + b"--\r\n",
    ]
    return b"".join(chunks), "multipart/related; boundary=%s" % boundary


def upload_scheduler_db(path, *, file_id=None, name=None, token=None):
    """Create or update the Drive backup file with drive.file (multipart, urllib)."""
    if token is None:
        token, _ = access_token()
    name = name or drive_backup_name()
    with open(path, "rb") as f:
        raw = f.read()
    body, content_type = _multipart_related(
        {"name": name, "mimeType": DRIVE_BACKUP_MIME}, raw, DRIVE_BACKUP_MIME)
    if file_id:
        url = "%s/%s?uploadType=multipart" % (
            drive_upload_url(), urllib.parse.quote(str(file_id), safe=""))
        method = "PATCH"
    else:
        url = "%s?uploadType=multipart" % drive_upload_url()
        method = "POST"
    status, _, resp = http_request(method, url, headers={
        "Authorization": "Bearer " + token,
        "Content-Type": content_type,
    }, data=body)
    if status in (401, 403):
        raise GcalError("needs_relogin", FIX_RELOGIN, step="auth")
    if status not in (200, 201):
        snippet = (resp or b"")[:200]
        try:
            snippet = snippet.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            snippet = repr(snippet)
        raise GcalError("drive_http",
                        "Google Drive upload failed: HTTP %s %s" % (status, snippet),
                        step="drive")
    try:
        data = json.loads(resp.decode())
    except ValueError:
        raise GcalError("drive_http", "Google Drive upload returned a non-JSON body.", step="drive")
    if not data.get("id"):
        raise GcalError("drive_http", "Google Drive upload did not return a file id.", step="drive")
    return data


# Drive backup (core split): each part's DB goes to its own Drive file. A DB is uploaded when
# its content changed since its last upload (a hash of its rows; the backup's own bookkeeping
# rows, meta keys drive_*, are left out so recording an upload never causes another one).
BACKUP_DBS = ("places", "scheduler", "approvals", "core")
DRIVE_NAMES = {"places": "rivia-places.db", "scheduler": DRIVE_BACKUP_NAME,
               "approvals": "rivia-approvals.db", "core": "rivia-core.db"}


def _open_part_db(name):
    if name == "places":
        import rivia_places
        return rivia_places.db_open()
    if name == "approvals":
        import rivia_approvals
        return rivia_approvals.db_open()
    if name == "core":
        import rivia_tools
        return rivia_tools.open_db()
    return _sched_db()


def _content_hash(con):
    h = hashlib.sha256()
    for line in con.iterdump():
        if line.startswith("INSERT INTO \"meta\" VALUES('drive_"):
            continue
        h.update(line.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _snapshot(con, name):
    """A consistent copy of one DB in ~/rivia/backup/, even if a write is in progress."""
    import sqlite3
    folder = os.path.join(_store().rivia_dir(), "backup")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name + ".db")
    tmp = path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    dst = sqlite3.connect(tmp)
    con.backup(dst)
    dst.close()
    os.replace(tmp, path)
    return path


def _drive_file_id(scon, name):
    fid = _store().get_meta(scon, "drive_file_id." + name)
    if not fid and name == "scheduler":
        fid = _store().get_meta(scon, "drive_file_id")  # the single Drive file from before the split
    return fid or None


def run_drive_backup(*, force=False, only=None):
    """Back up every part's DB (places, scheduler, approvals, core) to Drive, each to its own
    file, when it changed since its last upload. only=("core",) limits it to those names. A new, empty places.db is never uploaded over
    the Drive copy (restore_first). An existing read-only token is left in place; missing
    drive.file returns needs_relogin instead of crashing."""
    import rivia_places
    out = {"ok": True, "uploaded": False, "written": False, "files": []}
    try:
        try:
            tokens = load_tokens()
        except GcalError as e:
            if e.code in AUTH_FAIL_CODES:
                maybe_notify_signin_expired(e)
            raise
        pcon = rivia_places.db_open()
        try:
            rivia_places.need_restore(pcon)
            out["rides"] = rivia_places.ride_count(pcon)
        finally:
            pcon.close()
        scon = _sched_db()
        try:
            todo = []
            for name in BACKUP_DBS:
                if only is not None and name not in only:
                    continue
                con = _open_part_db(name)
                try:
                    digest = _content_hash(con)
                    if force or digest != _store().get_meta(scon, "drive_hash." + name):
                        todo.append((name, _snapshot(con, name), digest))
                finally:
                    con.close()
            if not todo:
                out["reason"] = out["message"] = "No new rides or changes since the last upload."
                return out
            require_scopes(SCOPE_DRIVE_FILE, tokens=tokens)
            for name, path, digest in todo:
                uploaded = upload_scheduler_db(path, file_id=_drive_file_id(scon, name), name=DRIVE_NAMES[name])
                try:
                    _store().set_meta(scon, "drive_file_id." + name, uploaded["id"])
                    _store().set_meta(scon, "drive_hash." + name, digest)
                    _store().set_meta(scon, "drive_uploaded_at", datetime.now(TZ).isoformat(timespec="seconds"))
                except Exception as e:  # noqa: BLE001
                    print("rivia: could not record the Drive upload of %s (%s); continuing." % (name, type(e).__name__),
                          file=sys.stderr)
                out["files"].append({"db": name, "name": uploaded.get("name") or DRIVE_NAMES[name],
                                     "drive_file_id": uploaded["id"], "modifiedTime": uploaded.get("modifiedTime"),
                                     "bytes": os.path.getsize(path)})
        finally:
            scon.close()
        out["uploaded"] = True
        out["message"] = "Uploaded %s to Drive." % ", ".join(f["db"] + ".db" for f in out["files"])
        return out
    except GcalError as e:
        if e.code == "needs_relogin":
            maybe_notify_missing_scopes()
        out["ok"] = False
        out["error"] = {"code": e.code, "msg": str(e), "step": e.step}
        out["status"] = clock_status()
        return out
    except rivia_places.RiviaError as e:
        out["ok"] = False
        out["error"] = {"code": e.code, "msg": str(e), "step": getattr(e, "step", None) or "planner"}
        return out
    except Exception as e:  # noqa: BLE001
        print("rivia: Drive backup failed (%s); continuing." % type(e).__name__, file=sys.stderr)
        out["ok"] = False
        out["error"] = {"code": "internal", "msg": type(e).__name__, "step": "drive"}
        return out


def cmd_backup(argv=None):
    force = bool(argv) and "--force" in argv
    return run_drive_backup(force=force)


def cmd_events(day_arg, now=None):
    reset_clock()
    day, asked = resolve_day(day_arg, now=now)
    events = list_events(day)
    status = clock_status()
    scopes = scopes_payload()
    status.update(scopes)
    return {
        "ok": True,
        "day": day.isoformat(),
        "asked": asked,
        "events": events,
        "scopes": scopes["scopes"],
        "scopes_complete": scopes["scopes_complete"],
        "status": status,
    }


def cmd_google_login(argv):
    # --write is accepted and is the documented path; both forms request
    # calendar.events.readonly + calendar.app.created + drive.file in one go.
    # An existing read-only token is left alone until this login completes.
    return google_login(scope=login_scope())


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
        print(__doc__ or "rivia_gcal.py google-login [--write] | events <day> | signin-check | backup")
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
        elif cmd == "backup":
            out = cmd_backup(argv[1:])
        else:
            raise GcalError("bad_command",
                            "Unknown command: %s. Use google-login, events <day>, signin-check, or backup." % cmd,
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


def calendar_base():
    return (os.environ.get("RIVIA_GOOGLE_CALENDAR_BASE") or CALENDAR_BASE).rstrip("/")


def _calendar_path(*parts):
    enc = [urllib.parse.quote(str(p), safe="") for p in parts]
    return calendar_base() + "/calendars/" + "/".join(enc)


def assert_not_primary(cal_id, action="write"):
    if not cal_id or str(cal_id).strip().lower() == "primary":
        raise GcalError(
            "primary_forbidden",
            "Rivia never %ss the primary calendar." % action,
        )


def _auth_headers(token, json_body=False):
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _json_call(method, url, payload=None, token=None):
    """JSON Calendar API call. Mutating calls must not target primary (enforced by callers)."""
    if token is None:
        token, _ = access_token()
    data = None
    headers = _auth_headers(token, json_body=payload is not None)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    # Record every mutating URL so tests can fail a write aimed at primary.
    if method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
        if "/calendars/primary" in url or url.rstrip("/").endswith("/calendars/primary"):
            raise GcalError("primary_forbidden", "Rivia never writes to the primary calendar.")
    status, _, body = http_request(method, url, headers=headers, data=data)
    if status in (401, 403):
        raise GcalError("needs_relogin", FIX_RELOGIN, step="auth")
    if status not in (200, 201, 204):
        snippet = (body or b"")[:200]
        try:
            snippet = snippet.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            snippet = repr(snippet)
        raise GcalError("calendar_http", "Google Calendar %s failed: HTTP %s %s" % (method, status, snippet))
    if status == 204 or not body:
        return {}
    try:
        return json.loads(body.decode())
    except ValueError:
        raise GcalError("calendar_http", "Google Calendar returned a non-JSON body.")


def event_body_from_plan(ev, day, batch_id, kind=None):
    """Turn a planner calendar_event into a Google event, tagged for later replace."""
    start = ev.get("start")
    end = ev.get("end")
    if isinstance(start, dict):
        start = start.get("dateTime") or start.get("iso") or start.get("date")
    if isinstance(end, dict):
        end = end.get("dateTime") or end.get("iso") or end.get("date")
    kind = kind or ev.get("kind")
    if not kind:
        desc = ev.get("description") or ""
        # planner tags "kind=%s" at the end of description
        if "kind=" in desc:
            kind = desc.rsplit("kind=", 1)[-1].strip(" ]")
    body = {
        "summary": ev.get("summary") or ev.get("title") or "Rivia",
        "description": ev.get("description") or ev.get("desc") or "",
        "location": ev.get("location") or "",
        "start": {"dateTime": start, "timeZone": "America/New_York"},
        "end": {"dateTime": end, "timeZone": "America/New_York"},
        "extendedProperties": {
            "private": {
                "rivia_day": day,
                "rivia_kind": kind or "",
                "rivia_batch": batch_id,
            }
        },
    }
    return body


def ensure_rivia_calendar():
    """Create the app-owned 'Rivia' calendar on first approved write; store gcal_rivia_id."""
    require_scopes(SCOPE_CALENDAR_APP)
    existing = _meta_get(META_RIVIA_ID)
    if existing:
        assert_not_primary(existing)
        return existing
    created = _json_call("POST", calendar_base() + "/calendars", {"summary": RIVIA_CALENDAR_NAME})
    cal_id = created.get("id")
    if not cal_id:
        raise GcalError("calendar_http", "Google did not return a calendar id for the new Rivia calendar.")
    assert_not_primary(cal_id)
    try:
        _meta_set(META_RIVIA_ID, cal_id)
    except Exception as e:  # noqa: BLE001
        print("rivia: could not store gcal_rivia_id (%s); continuing with the new id." % type(e).__name__,
              file=sys.stderr)
    return cal_id


def list_rivia_events(cal_id, day):
    """Events this app wrote for a day, found by the rivia_day private tag."""
    assert_not_primary(cal_id, action="read-rivia")
    require_scopes(SCOPE_CALENDAR_APP)
    token, _ = access_token()
    items = []
    page = None
    while True:
        q = {"privateExtendedProperty": "rivia_day=%s" % day, "singleEvents": "true"}
        if page:
            q["pageToken"] = page
        url = _calendar_path(cal_id, "events") + "?" + urllib.parse.urlencode(q)
        data = _json_call("GET", url, token=token)
        items.extend(data.get("items") or [])
        page = data.get("nextPageToken")
        if not page:
            break
    return items


def create_rivia_event(cal_id, body):
    assert_not_primary(cal_id)
    return _json_call("POST", _calendar_path(cal_id, "events"), body)


def delete_rivia_event(cal_id, event_id):
    assert_not_primary(cal_id)
    return _json_call("DELETE", _calendar_path(cal_id, "events", event_id))


def write_rivia_day(day, status, calendar_events, *, batch_id=None):
    """Apply an approved write_day to the app-owned Rivia calendar.

    planned: create the new tagged batch, then delete that day's older tagged
    events. If creating fails partway, the new batch is removed and the old
    plan is left alone. no_shift deletes that day's tagged events. not_written
    and under_way write nothing. Never touches primary.
    """
    require_scopes(SCOPE_CALENDAR_APP)
    day_s = day.isoformat() if hasattr(day, "isoformat") else str(day)
    if status in ("not_written", "under_way"):
        return {"written": False, "status": status, "created": 0, "deleted": 0, "calendar_id": None}
    cal_id = ensure_rivia_calendar()
    assert_not_primary(cal_id)
    batch_id = batch_id or ("batch_" + secrets.token_hex(8))
    old_items = list_rivia_events(cal_id, day_s)
    created_ids = []
    if status == "planned":
        try:
            for ev in calendar_events or []:
                body = event_body_from_plan(ev, day_s, batch_id)
                created = create_rivia_event(cal_id, body)
                eid = created.get("id")
                if not eid:
                    raise GcalError("calendar_http", "Google did not return an event id.")
                created_ids.append(eid)
        except Exception:
            for eid in created_ids:
                try:
                    delete_rivia_event(cal_id, eid)
                except Exception:  # noqa: BLE001
                    print("rivia: could not roll back a new Rivia event; continuing.", file=sys.stderr)
            raise
        # New batch is complete; now drop the older tagged events.
        deleted = 0
        for item in old_items:
            eid = item.get("id")
            if not eid or eid in created_ids:
                continue
            delete_rivia_event(cal_id, eid)
            deleted += 1
        return {"written": True, "status": status, "created": len(created_ids),
                "deleted": deleted, "calendar_id": cal_id, "batch_id": batch_id}
    if status == "no_shift":
        deleted = 0
        for item in old_items:
            eid = item.get("id")
            if not eid:
                continue
            delete_rivia_event(cal_id, eid)
            deleted += 1
        return {"written": deleted > 0, "status": status, "created": 0,
                "deleted": deleted, "calendar_id": cal_id, "batch_id": batch_id}
    raise GcalError("bad_status", "Cannot write a day with status %s." % status)




if __name__ == "__main__":
    sys.exit(main())
