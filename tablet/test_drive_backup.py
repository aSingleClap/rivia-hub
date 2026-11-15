"""Calendar write scopes and automatic Drive backup of scheduler.db.
Throwaway RIVIA_DIR / tokens; never touches ~/rivia or ~/.rivia.
    python test_drive_backup.py
"""
import base64, hashlib, json, os, secrets, sys, tempfile, threading, time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
DOT = os.path.join(TMP, "dot-rivia")
os.makedirs(DOT, mode=0o700)
_salt = secrets.token_bytes(16)
os.environ.update(
    RIVIA_DIR=os.path.join(TMP, "rivia"),
    RIVIA_DOTDIR=DOT,
    RIVIA_GOOGLE_TOKEN_PATH=os.path.join(DOT, "google_token.json"),
    RIVIA_GOOGLE_CLIENT_ID="test-client-id",
    RIVIA_GOOGLE_CLIENT_SECRET="test-client-secret",
    RIVIA_DB=os.path.join(TMP, "auth.db"),
    RIVIA_ISSUER="https://mcp.rivia.observer",
    RIVIA_JWT_KEY=secrets.token_hex(32),
    RIVIA_PASS_HASH=_salt.hex() + "$" + hashlib.scrypt(b"x" * 16, salt=_salt, n=2**14, r=8, p=1).hex(),
    RIVIA_TOTP_SECRET=base64.b32encode(secrets.token_bytes(20)).decode().rstrip("="),
    RIVIA_SIGNIN_CHECK="0",
)
sys.path.insert(0, HERE)

import rivia_gcal as g
import rivia_planner as rp
import rivia_sched as sched


def ok(cond, label):
    assert cond, label
    print("  ok ·", label)


def ride(rid, frm="farm", to="gym", start="2026-11-09T09:00:00-05:00", minutes=19):
    s = rp.to_epoch(start)
    return {
        "ride_id": rid, "from": frm, "to": to,
        "departed": s, "arrived": s + minutes * 60,
        "distance_km": 8.1, "avg_kmh": 25.6, "polyline": "abc",
    }


class FakeGoogle:
    def __init__(self):
        self.event_hits = 0
        self.items = [{
            "summary": "Work Shift - HMC",
            "status": "confirmed",
            "start": {"dateTime": "2026-11-16T14:30:00-05:00"},
            "end": {"dateTime": "2026-11-16T22:30:00-05:00"},
        }]
        self.files = {}
        self.uploads = []  # (method, file_id)
        self.next_id = 1
        self.clock = 0
        self.reject_drive = False

    def _modified(self):
        self.clock += 1
        return "2026-11-15T12:00:%02d.000Z" % self.clock

    def handle_token(self, body):
        form = {k: (v[-1] if v else "") for k, v in parse_qs(body.decode()).items()}
        if form.get("grant_type") == "refresh_token" and form.get("refresh_token") == "refresh-good":
            return 200, {
                "access_token": "access-good", "expires_in": 3600,
                "token_type": "Bearer",
            }
        return 400, {"error": "invalid_grant", "error_description": "expired or revoked"}

    def handle_events(self, headers):
        self.event_hits += 1
        auth = headers.get("Authorization") or headers.get("authorization") or ""
        if not auth.startswith("Bearer "):
            return 401, {"error": {"code": 401, "status": "UNAUTHENTICATED"}}
        return 200, {"items": list(self.items)}

    def handle_drive(self, method, path, headers, body):
        auth = headers.get("Authorization") or headers.get("authorization") or ""
        if not auth.startswith("Bearer "):
            return 401, {"error": {"code": 401, "status": "UNAUTHENTICATED"}}
        if self.reject_drive:
            return 403, {"error": {"code": 403, "status": "PERMISSION_DENIED"}}
        parsed = urlparse(path)
        rest = parsed.path
        prefix = "/upload/drive/v3/files"
        if not rest.startswith(prefix):
            return 404, {"error": "not found"}
        tail = rest[len(prefix):].lstrip("/")
        file_id = tail or None
        if method == "POST":
            if file_id:
                return 400, {"error": "POST does not take a file id"}
            file_id = "drive-file-%d" % self.next_id
            self.next_id += 1
        elif method == "PATCH":
            if not file_id or file_id not in self.files:
                return 404, {"error": "file not found"}
        else:
            return 405, {"error": "method"}
        modified = self._modified()
        rec = {
            "id": file_id,
            "name": g.DRIVE_BACKUP_NAME,
            "modifiedTime": modified,
            "bytes": len(body or b""),
        }
        self.files[file_id] = rec
        self.uploads.append({"method": method, "id": file_id, "modifiedTime": modified})
        return 200, {"id": file_id, "name": rec["name"], "modifiedTime": modified}


fake = FakeGoogle()


class Handler(BaseHTTPRequestHandler):
    def date_time_string(self, timestamp=None):
        import email.utils
        return email.utils.formatdate(usegmt=True)

    def _send(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n)

    def do_POST(self):
        body = self._read()
        if self.path.startswith("/token"):
            status, obj = fake.handle_token(body)
            self._send(status, obj)
            return
        if "/upload/drive/" in self.path:
            status, obj = fake.handle_drive("POST", self.path, self.headers, body)
            self._send(status, obj)
            return
        self._send(404, {"error": "no post"})

    def do_PATCH(self):
        body = self._read()
        if "/upload/drive/" in self.path:
            status, obj = fake.handle_drive("PATCH", self.path, self.headers, body)
            self._send(status, obj)
            return
        self._send(404, {"error": "no patch"})

    def do_GET(self):
        if self.path.startswith("/events"):
            status, obj = fake.handle_events(self.headers)
            self._send(status, obj)
            return
        self._send(404, {"error": "no get"})

    def log_message(self, *a):
        pass


httpd = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
port = httpd.server_address[1]
os.environ["RIVIA_GOOGLE_TOKEN_URL"] = "http://127.0.0.1:%d/token" % port
os.environ["RIVIA_GOOGLE_EVENTS_URL"] = "http://127.0.0.1:%d/events" % port
os.environ["RIVIA_GOOGLE_DRIVE_UPLOAD_URL"] = "http://127.0.0.1:%d/upload/drive/v3/files" % port


def save_token(scope):
    g.save_tokens({
        "refresh_token": "refresh-good",
        "access_token": "access-good",
        "expiry": time.time() + 3600,
        "scope": scope,
        "token_type": "Bearer",
    })
    g.reset_clock()


def sh_sched(*args):
    import subprocess
    env = os.environ.copy()
    env["PATH"] = "/tmp/rivia-test-venv/bin:" + env.get("PATH", "")
    return subprocess.run(
        ["bash", os.path.join(HERE, "rivia.sh"), "sched", *args],
        capture_output=True, text=True, env=env, cwd=HERE,
    )


# ── scopes: google-login requests all three; a read-only token still reads ──
ok(g.login_scope() == g.SCOPE_ALL, "google-login requests all three scopes")
ok(set(g.SCOPE_ALL.split()) == set(g.SCOPES_WRITE), "SCOPE_ALL is the write set")
save_token(g.SCOPE_READ)
events = g.cmd_events("2026-11-16")
ok(events["ok"] is True and events["events"], "events still work with a read-only token")
ok(g.SCOPE_READ in (events.get("scopes") or [])
   and events.get("scopes_complete") is False,
   "events status lists the granted read scope and is not complete")
ok(fake.event_hits >= 1, "the calendar read reached the mocked Google API")

# backup with a read-only token is a clear needs-re-sign-in error, not a crash
# (start-fresh first so a missing-scope error is not hidden by restore_first)
fresh = sched.cmd_start_fresh()
ok(fresh["ok"] and fresh["fresh"] is False and fresh["rides"] == 0,
   "start-fresh unlocks an empty database for backup tests")
crashed = False
try:
    denied = g.run_drive_backup()
except Exception as e:  # noqa: BLE001
    crashed = type(e).__name__
    denied = None
ok(crashed is False, "backup with a read-only token does not crash")
ok(denied and denied["ok"] is False and denied["error"]["code"] == "needs_relogin",
   "backup without Drive scope returns needs_relogin")
ok("google-login --write" in denied["error"]["msg"],
   "the needs-re-sign-in error tells you to run google-login --write")
ok(denied.get("uploaded") is False and not fake.uploads,
   "a missing-scope backup does not upload")
# reads still work after the refused backup; the read-only token was not overwritten
events2 = g.cmd_events("tomorrow")
ok(events2["ok"] is True, "calendar reads keep working after a refused backup")
saved = json.load(open(os.environ["RIVIA_GOOGLE_TOKEN_PATH"]))
ok(saved["refresh_token"] == "refresh-good" and g.SCOPE_DRIVE_FILE not in (saved.get("scope") or ""),
   "the live read-only token was left in place")

# ── with all three scopes, backup creates one Drive file even with 0 rides ──
save_token(g.SCOPE_ALL)
first = g.run_drive_backup()
ok(first["ok"] is True and first["uploaded"] is True, "first backup uploads")
ok(first.get("rides") == 0, "first backup after start-fresh has 0 rides")
fid = first["drive_file_id"]
ok(fid and fid in fake.files, "Drive created a file and we stored its id")
ok(len(fake.uploads) == 1 and fake.uploads[0]["method"] == "POST",
   "the first upload is a create (POST)")
con = rp.db_open()
try:
    stored = rp.get_meta(con, "drive_file_id")
finally:
    con.close()
ok(stored == fid, "drive_file_id is stored in planner meta")

# run again with no change: no new rides, no upload
again = g.run_drive_backup()
ok(again["ok"] is True and again["uploaded"] is False, "second backup does not upload")
ok("no new rides" in (again.get("message") or again.get("reason") or "").lower(),
   "second backup says no new rides")
ok(len(fake.uploads) == 1, "no second Drive request when nothing changed")

# log one test ride; same file id gets a newer modified time
logged = rp.cmd_log_ride(ride("test-ride-1"))
ok(logged["stored"] is True, "logged one test ride in the throwaway database")
ok(sched.cmd_rides()["rides"] == 1, "ride count is 1 after the test ride")
updated = g.run_drive_backup()
ok(updated["ok"] is True and updated["uploaded"] is True, "backup after a new ride uploads")
ok(updated["drive_file_id"] == fid, "the update uses the same Drive file id")
ok(len(fake.uploads) == 2 and fake.uploads[1]["method"] == "PATCH",
   "the second upload updates the existing file (PATCH)")
ok(updated["modifiedTime"] != first["modifiedTime"]
   and updated["modifiedTime"] > first["modifiedTime"],
   "the same file id has a newer modified time")

# ── settings / changes-table growth forces an upload with no new rides ──
# Mark the current upload, then insert a changes row without logging a ride.
marked_uploads = len(fake.uploads)
import rivia_sched as _sched
con = rp.db_open()
try:
    _sched.ensure_store(con)
    con.execute(
        "insert into changes(change_id, created, door, key, new, status) "
        "values (?,?,?,?,?,?)",
        ("chg-1", "2026-11-15T10:00:00-05:00", "app", "workout_minutes", "90", "applied"),
    )
    con.commit()
finally:
    con.close()
forced = g.run_drive_backup()
ok(forced["ok"] is True and forced["uploaded"] is True,
   "a new changes-table row forces an upload even with no new rides")
ok(forced["drive_file_id"] == fid, "the forced upload still updates the same file")
ok(len(fake.uploads) == marked_uploads + 1, "changes-table growth caused one more Drive upload")

# force again with no further change should say no new rides (uploaded_change + stamp match)
stable = g.run_drive_backup()
ok(stable["ok"] is True and stable["uploaded"] is False
   and "no new rides" in (stable.get("message") or "").lower(),
   "after the forced upload, a quiet run is again a no-op")

# ── a fresh database (no start-fresh) is restore_first, not a crash ──
os.remove(rp.db_file())
ok(sched.cmd_rides()["fresh"] is True, "wiped database is fresh again")
save_token(g.SCOPE_ALL)
blocked = g.run_drive_backup()
ok(blocked["ok"] is False and blocked["error"]["code"] == "restore_first",
   "backup on a fresh database is restore_first")
ok(blocked.get("uploaded") is False, "a refused fresh-db backup does not upload")

# ── CLI: rivia sched backup ──
# Unlock again and push once from the wrapper.
sched.cmd_start_fresh()
save_token(g.SCOPE_ALL)
# After wipe + start-fresh, drive_file_id is gone; this creates a new file.
cli = sh_sched("backup")
ok(cli.returncode == 0, "rivia sched backup exits 0: %s" % cli.stderr)
cli_out = json.loads(cli.stdout)
ok(cli_out["ok"] is True and cli_out["uploaded"] is True and cli_out.get("drive_file_id"),
   "rivia sched backup uploads on a newly unlocked database")
cli2 = sh_sched("backup")
ok(cli2.returncode == 0, "rivia sched backup again exits 0")
cli2_out = json.loads(cli2.stdout)
ok(cli2_out["ok"] is True and cli2_out["uploaded"] is False
   and "no new rides" in (cli2_out.get("message") or "").lower(),
   "rivia sched backup with no change reports no new rides")

# rivia.sh routes the command
sh = open(os.path.join(HERE, "rivia.sh")).read()
ok("backup) sched_gcal" in sh, "rivia.sh has rivia sched backup")

# Did not touch the box home database
ok(os.path.abspath(rp.db_file()).startswith(os.path.abspath(TMP)),
   "the test database lives under the throwaway folder, not ~/rivia")
ok(os.path.abspath(g.token_path()).startswith(os.path.abspath(TMP)),
   "the test token lives under the throwaway folder, not ~/.rivia")

httpd.shutdown()
print("\nDRIVE BACKUP PASS")
