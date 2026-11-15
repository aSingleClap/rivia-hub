"""Google sign-in and calendar reads, against a fake Google on loopback.
Throwaway token dir; never touches ~/.rivia or the real Calendar API.
    python test_gcal.py
"""
import io, json, os, secrets, socket, sys, tempfile, threading, time
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
from urllib.error import URLError

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
DOT = os.path.join(TMP, "dot-rivia")
os.makedirs(DOT, mode=0o700)
os.environ.update(
    RIVIA_DOTDIR=DOT,
    RIVIA_GOOGLE_TOKEN_PATH=os.path.join(DOT, "google_token.json"),
    RIVIA_GOOGLE_CLIENT_ID="test-client-id",
    RIVIA_GOOGLE_CLIENT_SECRET="test-client-secret",
    # HOME also pointed at TMP so a bug that ignores RIVIA_DOTDIR still cannot
    # write a real ~/.rivia/google_token.json.
    HOME=TMP,
)
sys.path.insert(0, HERE)

import rivia_gcal as g


def ok(cond, label):
    assert cond, label
    print("  ok ·", label)


# ── unit: days, events, paste ──
now = datetime(2026, 11, 15, 2, 30, tzinfo=g.TZ)  # Sunday
d, label = g.resolve_day("tomorrow", now=now)
ok(d.isoformat() == "2026-11-16" and label == "tomorrow", "tomorrow is 2026-11-16 from Sunday the 15th")
d, label = g.resolve_day("today", now=now)
ok(d.isoformat() == "2026-11-15" and label == "today", "today")
d, label = g.resolve_day("monday", now=now)
ok(d.isoformat() == "2026-11-16" and label == "monday", "weekday name is the next one (Mon after Sun)")
d, label = g.resolve_day("Sunday", now=now)
ok(d.isoformat() == "2026-11-15", "weekday name includes today")
d, label = g.resolve_day("2026-12-05", now=now)
ok(d.isoformat() == "2026-12-05" and label == "2026-12-05", "YYYY-MM-DD")
try:
    g.resolve_day("nope", now=now)
    ok(False, "bad day refused")
except g.GcalError as e:
    ok(e.code == "bad_day" and e.step == "input", "bad day is bad_day, step input")

tmin, tmax = g.day_window(d)
ok(tmin.startswith("2026-12-05T00:00:00") and tmax.startswith("2026-12-06T00:00:00"),
   "window is midnight to next midnight in America/New_York")

timed = g.norm_google_event({
    "summary": "Work Shift - HMC",
    "status": "confirmed",
    "start": {"dateTime": "2026-11-16T14:30:00-05:00"},
    "end": {"dateTime": "2026-11-16T23:00:00-05:00"},
    "location": "500 University Dr, Hershey, PA 17033",
})
ok(timed == {
    "title": "Work Shift - HMC",
    "start": "2026-11-16T14:30:00-05:00",
    "end": "2026-11-16T23:00:00-05:00",
    "location": "500 University Dr, Hershey, PA 17033",
    "all_day": False,
}, "timed event maps to title/start/end/location/all_day")
allday = g.norm_google_event({
    "summary": "Day off",
    "start": {"date": "2026-11-16"},
    "end": {"date": "2026-11-17"},
})
ok(allday["all_day"] is True and allday["title"] == "Day off" and allday["start"] == "2026-11-16",
   "all-day event uses dates")
ok(g.norm_google_event({"status": "cancelled", "summary": "gone",
                        "start": {"dateTime": "2026-11-16T14:30:00-05:00"},
                        "end": {"dateTime": "2026-11-16T15:00:00-05:00"}}) is None,
   "cancelled events are dropped")

p = g.parse_redirect("http://127.0.0.1:12345/?code=abc&state=xyz")
ok(p["code"] == "abc" and p["state"] == "xyz" and not p["error"], "parse full redirect URL")
p = g.parse_redirect("code=abc&state=xyz")
ok(p["code"] == "abc" and p["state"] == "xyz", "parse bare query")
p = g.parse_redirect("http://127.0.0.1:9/?error=access_denied&state=xyz")
ok(p["error"] == "access_denied", "parse error redirect")

v, ch = g.pkce()
ok(len(v) >= 43 and len(ch) == 43, "PKCE verifier and S256 challenge lengths")


# ── fake Google ──
class FakeGoogle:
    def __init__(self):
        self.token_hits = 0
        self.event_hits = 0
        self.fail_events = 0  # number of initial 500s to serve
        self.grant_error = None
        self.clock_offset_s = 0
        self.pages = None  # or list of item lists
        self.items = []
        self.codes = {}  # code -> {verifier, redirect_uri, refresh, access}
        self.refresh_tokens = {"refresh-good": {"access": "access-good"}}
        self.last_query = None
        self._srv = None

    def date_hdr(self):
        return formatdate(timeval=time.time() + self.clock_offset_s, usegmt=True)

    def handle_token(self, body):
        self.token_hits += 1
        form = {k: (v[-1] if v else "") for k, v in parse_qs(body.decode()).items()}
        if self.grant_error:
            return 400, {"error": self.grant_error, "error_description": "denied"}
        if form.get("grant_type") == "authorization_code":
            rec = self.codes.get(form.get("code"))
            if not rec or rec["verifier"] != form.get("code_verifier"):
                return 400, {"error": "invalid_grant", "error_description": "bad code or verifier"}
            if rec["redirect_uri"] != form.get("redirect_uri"):
                return 400, {"error": "redirect_uri_mismatch"}
            if form.get("client_id") != "test-client-id" or form.get("client_secret") != "test-client-secret":
                return 401, {"error": "invalid_client"}
            self.refresh_tokens[rec["refresh"]] = {"access": rec["access"]}
            return 200, {
                "access_token": rec["access"], "refresh_token": rec["refresh"],
                "expires_in": 3600, "token_type": "Bearer",
                "scope": g.SCOPE_READ,
            }
        if form.get("grant_type") == "refresh_token":
            rec = self.refresh_tokens.get(form.get("refresh_token"))
            if not rec:
                return 400, {"error": "invalid_grant", "error_description": "expired or revoked"}
            rec["access"] = "access-refreshed-%d" % self.token_hits
            return 200, {
                "access_token": rec["access"], "expires_in": 3600,
                "token_type": "Bearer", "scope": g.SCOPE_READ,
            }
        return 400, {"error": "unsupported_grant_type"}

    def handle_events(self, qs, headers):
        self.event_hits += 1
        self.last_query = qs
        auth = headers.get("Authorization") or headers.get("authorization") or ""
        if not auth.startswith("Bearer "):
            return 401, {"error": {"code": 401, "status": "UNAUTHENTICATED"}}
        if self.fail_events > 0:
            self.fail_events -= 1
            return 500, {"error": {"code": 500, "message": "temporary"}}
        page = (qs.get("pageToken") or [None])[0]
        if self.pages is not None:
            idx = int(page or 0)
            items = self.pages[idx]
            nxt = str(idx + 1) if idx + 1 < len(self.pages) else None
            out = {"items": items}
            if nxt:
                out["nextPageToken"] = nxt
            return 200, out
        return 200, {"items": list(self.items)}


fake = FakeGoogle()


def _header_map(handler):
    return {k: v for k, v in handler.headers.items()}


class Handler(BaseHTTPRequestHandler):
    # send_response() would add its own Date (the real clock). Override it so
    # tests can shift Google's clock and check the 2-minute gap.
    def date_time_string(self, timestamp=None):
        return fake.date_hdr()

    def _send(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        if self.path.startswith("/token"):
            status, obj = fake.handle_token(body)
            self._send(status, obj)
            return
        self._send(404, {"error": "not found"})

    def do_GET(self):
        u = urlparse(self.path)
        if u.path.startswith("/calendar/v3/calendars/primary/events"):
            status, obj = fake.handle_events(parse_qs(u.query), _header_map(self))
            self._send(status, obj)
            return
        self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


httpd = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
PORT = httpd.server_address[1]
os.environ["RIVIA_GOOGLE_TOKEN_URL"] = "http://127.0.0.1:%d/token" % PORT
os.environ["RIVIA_GOOGLE_EVENTS_URL"] = "http://127.0.0.1:%d/calendar/v3/calendars/primary/events" % PORT
os.environ["RIVIA_GOOGLE_AUTH_URL"] = "http://127.0.0.1:%d/auth" % PORT
g.reset_clock()


def seed_token(refresh="refresh-good", access="access-good", expiry=None):
    fake.refresh_tokens[refresh] = {"access": access}
    return g.save_tokens({
        "refresh_token": refresh,
        "access_token": access,
        "expiry": int(expiry if expiry is not None else time.time() + 3600),
        "scope": g.SCOPE_READ,
        "token_type": "Bearer",
    })


# ── events: tomorrow prints tomorrow, pagination, cancelled, retry, clock, invalid_grant ──
shift = {
    "summary": "Work Shift - HMC",
    "status": "confirmed",
    "start": {"dateTime": "2026-11-16T14:30:00-05:00"},
    "end": {"dateTime": "2026-11-16T23:00:00-05:00"},
    "location": "500 University Dr",
}
gym = {
    "summary": "Gym",
    "start": {"dateTime": "2026-11-16T11:00:00-05:00"},
    "end": {"dateTime": "2026-11-16T12:30:00-05:00"},
    "location": "",
}
cancelled = {
    "summary": "Cancelled meeting",
    "status": "cancelled",
    "start": {"dateTime": "2026-11-16T09:00:00-05:00"},
    "end": {"dateTime": "2026-11-16T10:00:00-05:00"},
}
seed_token()
fake.items = [shift, cancelled]
# Freeze "now" by calling cmd_events with now= Sunday 15th, asking tomorrow
out = g.cmd_events("tomorrow", now=now)
ok(out["ok"] is True and out["asked"] == "tomorrow" and out["day"] == "2026-11-16",
   "events tomorrow prints tomorrow (2026-11-16)")
ok(len(out["events"]) == 1 and out["events"][0]["title"] == "Work Shift - HMC",
   "cancelled events are not listed")
ok(out["events"][0]["all_day"] is False and out["events"][0]["start"].startswith("2026-11-16"),
   "shift start is on tomorrow")
q = fake.last_query
ok((q.get("singleEvents") or [""])[0] == "true" and (q.get("orderBy") or [""])[0] == "startTime",
   "request uses singleEvents=true and orderBy=startTime")
ok((q.get("timeMin") or [""])[0].startswith("2026-11-16T00:00:00") and
   (q.get("timeMax") or [""])[0].startswith("2026-11-17T00:00:00"),
   "timeMin/timeMax cover tomorrow in America/New_York")
ok("clock_gap_s" in out["status"] and "clock_ok" in out["status"],
   "events reply includes status.clock_gap_s and clock_ok")

# pagination
fake.pages = [[shift], [gym]]
fake.items = []
out = g.cmd_events("2026-11-16", now=now)
ok(len(out["events"]) == 2 and {e["title"] for e in out["events"]} == {"Work Shift - HMC", "Gym"},
   "events follow nextPageToken")
fake.pages = None

# retry on 5xx
fake.fail_events = 1
fake.event_hits = 0
fake.items = [shift]
out = g.cmd_events("tomorrow", now=now)
ok(out["ok"] and fake.event_hits == 2, "one retry on HTTP 5xx, then success")

# clock gap over 2 minutes is reported in status
fake.clock_offset_s = 180
g.reset_clock()
out = g.cmd_events("tomorrow", now=now)
ok(out["status"]["clock_ok"] is False and abs(out["status"]["clock_gap_s"]) >= 120,
   "clock gap over 2 minutes is reported in status (gap=%s)" % out["status"]["clock_gap_s"])
fake.clock_offset_s = 0
g.reset_clock()
out = g.cmd_events("tomorrow", now=now)
ok(out["status"]["clock_ok"] is True, "clock gap under 2 minutes is ok")

# invalid_grant on refresh
g.reset_clock()
seed_token(refresh="refresh-dead", access="stale", expiry=time.time() - 120)
fake.grant_error = "invalid_grant"
try:
    g.cmd_events("tomorrow", now=now)
    ok(False, "expired grant should fail")
except g.GcalError as e:
    ok(e.code == "calendar_login_expired" and "rivia sched google-login" in str(e),
       "invalid_grant becomes calendar_login_expired with the fix command")
    payload = g.fail_payload(e)
    ok(payload["ok"] is False and payload["error"]["step"] == "calendar",
       "expired login is a calendar-step failure")
fake.grant_error = None

# ── login: auth URL, loopback + PKCE, state check, paste fallback, mode 600 ──
# Drop any leftover token from the events tests so login writes a fresh one.
try:
    os.remove(os.environ["RIVIA_GOOGLE_TOKEN_PATH"])
except OSError:
    pass

# Authorize URL shape
verifier, challenge = g.pkce()
state = "state-xyz"
url = g.authorize_url("http://127.0.0.1:9/", state, challenge)
u = urlparse(url)
qs = parse_qs(u.query)
ok(qs["response_type"] == ["code"] and qs["code_challenge_method"] == ["S256"], "auth URL: code + S256")
ok(qs["code_challenge"] == [challenge] and qs["state"] == [state], "auth URL carries challenge and state")
ok(qs["redirect_uri"] == ["http://127.0.0.1:9/"] and qs["access_type"] == ["offline"],
   "auth URL: loopback redirect and access_type=offline")
ok(qs["scope"] == [g.SCOPE_READ] and qs["prompt"] == ["consent"],
   "auth URL: readonly calendar scope and prompt=consent")
ok("test-client-secret" not in url and "test-client-id" in url, "auth URL has the client id, never the secret")

# Loopback success: start login in a thread, hit 127.0.0.1 with code+state
login_out = {}
login_err = {}


def run_login():
    try:
        login_out["r"] = g.google_login(open_browser=False, timeout=10, stdin=io.StringIO(""))
    except Exception as e:  # noqa: BLE001
        login_err["e"] = e


# We need the port/state/verifier from the running login. Hook start_loopback
# by wrapping google_login's internals: call the pieces ourselves for the
# protocol test, then also run the full function with a patched wait.

# Full-function test: intercept authorize + wait by driving the real loopback.
# Patch wait_for_redirect to first GET the printed redirect. Easier: start_loopback
# ourselves (already used by google_login). We'll wrap start_loopback to publish the URI.

held = {}
real_start = g.start_loopback


def start_and_hold(queue):
    server, redirect_uri = real_start(queue)
    held["server"] = server
    held["redirect"] = redirect_uri
    held["queue"] = queue
    return server, redirect_uri


g.start_loopback = start_and_hold

# Also capture authorize_url args so we can hit the loopback with the real state.
auth_held = {}
real_authorize = g.authorize_url


def authorize_and_hold(redirect_uri, state, challenge, scope=g.SCOPE_READ):
    auth_held.update(redirect_uri=redirect_uri, state=state, challenge=challenge)
    # Register the one-time code the fake token endpoint will accept, bound to this PKCE.
    # We don't know the verifier here (it's inside google_login). Patch exchange later
    # after we also hook pkce().
    return real_authorize(redirect_uri, state, challenge, scope=scope)


pkce_held = {}
real_pkce = g.pkce


def pkce_and_hold():
    v, c = real_pkce()
    pkce_held["verifier"] = v
    pkce_held["challenge"] = c
    return v, c


g.authorize_url = authorize_and_hold
g.pkce = pkce_and_hold

th = threading.Thread(target=run_login, daemon=True)
th.start()
for _ in range(50):
    if held.get("redirect") and auth_held.get("state") and pkce_held.get("verifier"):
        break
    time.sleep(0.05)
ok(held.get("redirect", "").startswith("http://127.0.0.1:"), "login listens on 127.0.0.1")
code = "code-loopback-1"
fake.codes[code] = {
    "verifier": pkce_held["verifier"],
    "redirect_uri": held["redirect"],
    "refresh": "refresh-login",
    "access": "access-login",
}
# Simulate the browser coming back to the loopback listener.
import urllib.request
hit = held["redirect"] + "?" + "code=%s&state=%s" % (code, auth_held["state"])
with urllib.request.urlopen(hit, timeout=5) as r:
    page = r.read().decode()
ok("Signed in" in page, "loopback page tells the user they can close the tab")
th.join(timeout=5)
ok("r" in login_out, "google-login finished: %s" % login_err)
path = login_out["r"]["saved"]
ok(login_out["r"]["ok"] is True and path == os.environ["RIVIA_GOOGLE_TOKEN_PATH"], "token saved under RIVIA_DOTDIR")
mode = os.stat(path).st_mode & 0o777
ok(mode == 0o600, "google_token.json mode is 600 (got %o)" % mode)
saved = json.load(open(path))
ok(saved["refresh_token"] == "refresh-login" and saved["access_token"] == "access-login",
   "saved token holds the refresh token from the exchange")
# The real home ~/.rivia was not written (HOME is TMP; also RIVIA_DOTDIR is TMP/dot-rivia)
ok(not os.path.exists(os.path.expanduser("~/.rivia/google_token.json")) or
   os.path.abspath(os.path.expanduser("~/.rivia/google_token.json")).startswith(os.path.abspath(TMP)),
   "login did not write a token outside the throwaway dir")

# State mismatch is refused (and does not save a new token)
os.remove(path)
login_out.clear(); login_err.clear(); held.clear(); auth_held.clear(); pkce_held.clear()
th = threading.Thread(target=run_login, daemon=True)
th.start()
for _ in range(50):
    if held.get("redirect") and auth_held.get("state"):
        break
    time.sleep(0.05)
hit = held["redirect"] + "?code=x&state=WRONG"
urllib.request.urlopen(hit, timeout=5).read()
th.join(timeout=5)
err = login_err.get("e")
ok(isinstance(err, g.GcalError) and err.code == "bad_state", "state mismatch is refused (%s)" % err)
ok(not os.path.exists(os.environ["RIVIA_GOOGLE_TOKEN_PATH"]), "failed login does not write a token file")

# Paste fallback: no HTTP hit; a pasted address completes the login
login_out.clear(); login_err.clear(); held.clear(); auth_held.clear(); pkce_held.clear()
paste_box = {"fh": None}


def run_login_paste():
    # Wait until we know the state, then the parent writes the line. Use a pipe.
    try:
        login_out["r"] = g.google_login(open_browser=False, timeout=10, stdin=paste_box["fh"])
    except Exception as e:  # noqa: BLE001
        login_err["e"] = e


# Use a pipe so readline() blocks until we write the paste.
rfd, wfd = os.pipe()
paste_box["fh"] = io.open(rfd, "r")
th = threading.Thread(target=run_login_paste, daemon=True)
th.start()
for _ in range(50):
    if held.get("redirect") and auth_held.get("state") and pkce_held.get("verifier"):
        break
    time.sleep(0.05)
code = "code-paste-1"
fake.codes[code] = {
    "verifier": pkce_held["verifier"],
    "redirect_uri": held["redirect"],
    "refresh": "refresh-paste",
    "access": "access-paste",
}
paste = held["redirect"] + " ?code=%s&state=%s\n" % (code, auth_held["state"])
# trim space typo? use a clean URL
paste = "%s?code=%s&state=%s\n" % (held["redirect"], code, auth_held["state"])
os.write(wfd, paste.encode())
os.close(wfd)
th.join(timeout=5)
ok("r" in login_out, "paste fallback completed login: %s" % login_err)
ok(login_out["r"]["ok"] and json.load(open(login_out["r"]["saved"]))["refresh_token"] == "refresh-paste",
   "paste fallback saved the refresh token")
mode = os.stat(login_out["r"]["saved"]).st_mode & 0o777
ok(mode == 0o600, "paste-fallback token is also mode 600")

# --write is step 8, not this ticket
try:
    g.cmd_google_login(["--write"])
    ok(False, "--write should be refused")
except g.GcalError as e:
    ok(e.code == "bad_scope" and e.step == "input", "google-login --write is reserved for step 8")

# CLI: events tomorrow via main(), mocked now by patching datetime? cmd path uses resolve_day(now=None).
# Instead call main after seeding events and a valid token with a real "tomorrow"
# relative to America/New_York. Just check the CLI wiring for a bad command and for
# events with an explicit date (no clock dependency).
seed_token()
fake.items = [shift]
rc = g.main(["events", "2026-11-16"])
ok(rc == 0, "cli events YYYY-MM-DD exits 0")

g.start_loopback = real_start
g.authorize_url = real_authorize
g.pkce = real_pkce
httpd.shutdown()
print("\nGCAL PASS")
