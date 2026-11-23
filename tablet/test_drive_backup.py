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
    RIVIA_CODE_DIR=os.path.join(TMP, "shared", "rivia"),
    RIVIA_CLOUDFLARED_DIR=os.path.join(TMP, "cloudflared"),
)
sys.path.insert(0, HERE)


def _w(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data if isinstance(data, bytes) else data.encode())


CODE = os.environ["RIVIA_CODE_DIR"]
_w(os.path.join(CODE, "rivia_server.py"), "print('server')\n")
_w(os.path.join(CODE, "tests", "test_x.py"), "x = 1\n")
_w(os.path.join(CODE, "docs", "notes.md"), "notes\n")
_w(os.path.join(CODE, "app-token.txt"), "eyJcode.token\n")
_w(os.path.join(CODE, "rivia_server.py.bak-old-1"), "old server\n")
_w(os.path.join(CODE, "tests", "test_x.py.bak-old-1"), "old test\n")
_w(os.path.join(CODE, "__pycache__", "rivia_server.cpython-314.pyc"), b"\0pyc")
_w(os.path.join(TMP, "shared", "rivia.bak-step1", "rivia_server.py"), "step1 server\n")
CF = os.environ["RIVIA_CLOUDFLARED_DIR"]
_w(os.path.join(CF, "config.yml"), "tunnel: abc\ncredentials-file: /x/abc.json\n")
_w(os.path.join(CF, "cert.pem"), "-----BEGIN CERT-----\n")
_w(os.path.join(CF, "abc.json"), '{"TunnelSecret": "s3cret"}')
_w(os.path.join(DOT, ".env"), "RIVIA_JWT_KEY=jwt-secret-value\nRIVIA_SCHED_TIMER=0\n")
_w(os.path.join(DOT, "admin_token"), "admin-token-value\n")
_w(os.path.join(DOT, ".env.bak-old-1"), "RIVIA_JWT_KEY=older\n")
RIVIA = os.environ["RIVIA_DIR"]
_w(os.path.join(RIVIA, "app-token.txt"), "eyJdata.token\n")
_w(os.path.join(RIVIA, "places.json"), "{}\n")
_w(os.path.join(RIVIA, "logs", "server.log"), "INFO: started\n")
_w(os.path.join(RIVIA, "g2audio", "big.apk"), b"apk")
_w(os.path.join(RIVIA, "core.db.bak-old-1"), b"SQLite format 3\0old")
_w(os.path.join(RIVIA, "progress_overrides.json"), '{"gap.weights.rule_breaks": 0.4}')
_w(os.path.join(RIVIA, "progress_edits.jsonl"), '{"at": 1, "set": {}}\n')
_w(os.path.join(RIVIA, "trust_unlocks.json"), "{}")
_w(os.path.join(RIVIA, "trust_edits.jsonl"), "")
_w(os.path.join(RIVIA, "trust_overrides.json.bak-old-1"), "{}")

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
        self.copies = []
        self.deletes = []
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
        name = g.DRIVE_BACKUP_NAME
        try:  # the multipart body starts with the JSON metadata part (name, mimeType)
            meta = (body or b"").split(b"\r\n\r\n", 1)[1].split(b"\r\n", 1)[0]
            name = json.loads(meta.decode()).get("name") or name
        except Exception:  # noqa: BLE001
            pass
        parents = None
        try:
            parents = json.loads((body or b"").split(b"\r\n\r\n", 1)[1].split(b"\r\n", 1)[0].decode()).get("parents")
        except Exception:  # noqa: BLE001
            pass
        content = b""
        try:  # the media part: after the second part's headers, up to the closing boundary
            boundary = (body or b"").split(b"\r\n", 1)[0]
            content = body.split(boundary)[2].split(b"\r\n\r\n", 1)[1][:-2]
        except Exception:  # noqa: BLE001
            pass
        old = self.files.get(file_id) or {}
        rec = {
            "id": file_id,
            "name": name,
            "modifiedTime": modified,
            "bytes": len(content),
            "content": content,
            "parents": parents if method == "POST" else old.get("parents"),
        }
        self.files[file_id] = rec
        self.uploads.append({"method": method, "id": file_id, "name": name, "modifiedTime": modified,
                             "parents": rec["parents"]})
        return 200, {"id": file_id, "name": rec["name"], "modifiedTime": modified}


    def handle_meta(self, method, path, body):
        """drive/v3/files: create a folder, copy a file, read a file's metadata."""
        rest = urlparse(path).path[len("/drive/v3/files"):].strip("/")
        meta = json.loads(body.decode() or "{}") if body else {}
        if method == "POST" and not rest:
            fid = "drive-folder-%d" % self.next_id
            self.next_id += 1
            self.files[fid] = {"id": fid, "name": meta.get("name"), "mimeType": meta.get("mimeType"),
                               "modifiedTime": self._modified(), "bytes": 0, "content": b"", "parents": None}
            return 200, {"id": fid, "name": meta.get("name")}
        if method == "POST" and rest.endswith("/copy"):
            src = self.files.get(rest[:-len("/copy")])
            if not src:
                return 404, {"error": "not found"}
            fid = "drive-copy-%d" % self.next_id
            self.next_id += 1
            self.files[fid] = dict(src, id=fid, name=meta.get("name"), parents=meta.get("parents"))
            self.copies.append({"from": src["id"], "id": fid, "name": meta.get("name"), "parents": meta.get("parents")})
            return 200, {"id": fid, "name": meta.get("name"), "size": str(src.get("bytes", 0))}
        if method == "GET" and rest in self.files:
            f = self.files[rest]
            return 200, {"id": f["id"], "name": f["name"], "modifiedTime": f.get("modifiedTime") or "2026-11-01T00:00:00.000Z",
                         "size": str(f.get("bytes", 0)), "trashed": False}
        return 404, {"error": "not found"}


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
        if self.path.startswith("/drive/v3/files"):
            status, obj = fake.handle_meta("POST", self.path, body)
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
        if self.path.startswith("/drive/v3/files"):
            status, obj = fake.handle_meta("GET", self.path, b"")
            self._send(status, obj)
            return
        self._send(404, {"error": "no get"})

    def do_DELETE(self):
        fake.deletes.append(self.path)
        self._send(405, {"error": "this test's Drive refuses deletes"})

    def log_message(self, *a):
        pass


httpd = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
port = httpd.server_address[1]
os.environ["RIVIA_GOOGLE_TOKEN_URL"] = "http://127.0.0.1:%d/token" % port
os.environ["RIVIA_GOOGLE_EVENTS_URL"] = "http://127.0.0.1:%d/events" % port
os.environ["RIVIA_GOOGLE_DRIVE_UPLOAD_URL"] = "http://127.0.0.1:%d/upload/drive/v3/files" % port
os.environ["RIVIA_GOOGLE_DRIVE_FILES_URL"] = "http://127.0.0.1:%d/drive/v3/files" % port


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
# The single Drive file from before the core split (rivia-scheduler.db) is reused for scheduler.db.
fake.files["legacy-file"] = {"id": "legacy-file", "name": g.DRIVE_BACKUP_NAME, "modifiedTime": "", "bytes": 0}
g._meta_set("drive_file_id", "legacy-file")
first = g.run_drive_backup()
ok(first["ok"] is True and first["uploaded"] is True, "first backup uploads")
ok(first.get("rides") == 0, "first backup after start-fresh has 0 rides")
by_db = {f["db"]: f for f in first["files"]}
ok(set(by_db) == {"places", "scheduler", "approvals", "core", "auth", "code", "secrets", "logs"},
   "every part's DB plus the code, secrets and logs archives are backed up: %s" % sorted(by_db))
main = {u["name"] for u in fake.uploads if not u.get("parents")}
ok(main == {"rivia-places.db", "rivia-scheduler.db", "rivia-approvals.db", "rivia-core.db", "rivia-auth.db",
            "rivia-code.tar.gz", "rivia-secrets.tar.gz", "rivia-logs.tar.gz"},
   "each goes to its own Drive file in the main backup")
ok(not os.path.exists(os.path.join(RIVIA, "emotion.db")) and not os.path.exists(os.path.join(RIVIA, "body.db")),
   "a missing emotion.db or body.db is skipped, never created")
ok(by_db["scheduler"]["drive_file_id"] == "legacy-file"
   and [u["method"] for u in fake.uploads if u["name"] == "rivia-scheduler.db"] == ["PATCH"],
   "scheduler.db updates the Drive file from before the split")
ok(sorted(u["method"] for u in fake.uploads if u["name"] != "rivia-scheduler.db" and not u.get("parents")) == ["POST"] * 7,
   "the other seven are created (POST)")
ledger_id = first.get("ledger_folder_id")
ledger = [f for f in fake.files.values() if f.get("mimeType") == "application/vnd.google-apps.folder"]
ok(len(ledger) == 1 and ledger[0]["name"] == "Rivia (History Ledger)" and ledger[0]["id"] == ledger_id,
   "the History Ledger folder is created once")
ok([c["from"] for c in fake.copies] == ["legacy-file"] and fake.copies[0]["parents"] == [ledger_id]
   and fake.copies[0]["name"].startswith("rivia-scheduler.db ("),
   "the old rivia-scheduler.db is copied into the History Ledger before it is updated")
in_ledger = sorted(u["name"] for u in fake.uploads if u.get("parents") == [ledger_id])
ok(in_ledger == sorted(["code rivia_server.py.bak-old-1", "code-tests test_x.py.bak-old-1", "data core.db.bak-old-1",
                        "data trust_overrides.json.bak-old-1", "dotdir .env.bak-old-1", "shared rivia.bak-step1.tar.gz"]),
   "every .bak* copy (and the old rivia.bak-* folder) goes into the History Ledger once: %s" % in_ledger)


def tar_names(file_id):
    import io, tarfile
    with tarfile.open(fileobj=io.BytesIO(fake.files[file_id]["content"])) as t:
        return {m.name: t.extractfile(m).read() for m in t.getmembers()}


code_tar = tar_names(by_db["code"]["drive_file_id"])
ok({"MANIFEST.txt", "code/rivia_server.py", "code/tests/test_x.py", "code/docs/notes.md",
    "config/cloudflared-config.yml", "config/places.json"} <= set(code_tar),
   "rivia-code.tar.gz has the code folder, cloudflared config.yml and places.json")
ok({"state/progress_overrides.json", "state/progress_edits.jsonl", "state/trust_unlocks.json",
    "state/trust_edits.jsonl"} <= set(code_tar) and "state/places.json" not in code_tar,
   "rivia-code.tar.gz has the app's settings and edit logs (Step 5 progress_*, Step 6 trust_*) under state/")
ok(code_tar["state/progress_overrides.json"] == b'{"gap.weights.rule_breaks": 0.4}', "the settings file goes in whole")
ok(not [n for n in code_tar if ".bak" in n or "__pycache__" in n or "token" in n or n.endswith(".pem")],
   "rivia-code.tar.gz has no .bak copies, __pycache__ or token/key files")
sec_tar = tar_names(by_db["secrets"]["drive_file_id"])
ok({"secrets/rivia-dotdir/.env", "secrets/rivia-dotdir/admin_token", "secrets/rivia-dotdir/google_token.json",
    "secrets/rivia-data/app-token.txt", "secrets/code/app-token.txt", "secrets/cloudflared/cert.pem",
    "secrets/cloudflared/abc.json", "secrets/cloudflared/config.yml"} <= set(sec_tar),
   "rivia-secrets.tar.gz has the .env, tokens, keys and tunnel credentials")
ok(b"jwt-secret-value" in sec_tar["secrets/rivia-dotdir/.env"], "the .env goes in whole, secrets included")
ok(not [n for n in sec_tar if ".bak" in n], "no .bak copies in the secrets archive (they go to the ledger)")
ok(set(tar_names(by_db["logs"]["drive_file_id"])) == {"MANIFEST.txt", "logs/server.log"},
   "rivia-logs.tar.gz has the logs")
ok(not [n for t in (code_tar, sec_tar) for n in t if "g2audio" in n], "g2audio is never backed up")
ok(not fake.deletes, "nothing in Drive is deleted")
fid = by_db["places"]["drive_file_id"]
ok(g._meta_get("drive_file_id.places") == fid and fid in fake.files, "the places file id is stored in scheduler meta")
n_first = len(fake.uploads)

# run again with no change: nothing uploads
again = g.run_drive_backup()
ok(again["ok"] is True and again["uploaded"] is False, "second backup does not upload")
ok("no new rides" in (again.get("message") or again.get("reason") or "").lower(),
   "second backup says nothing new")
ok(len(fake.uploads) == n_first, "no Drive request when nothing changed")

# log one test ride: only places.db goes up, to the same file, with a newer modified time
logged = rp.cmd_log_ride(ride("test-ride-1"))
ok(logged["stored"] is True, "logged one test ride in the throwaway database")
ok(sched.cmd_rides()["rides"] == 1, "ride count is 1 after the test ride")
updated = g.run_drive_backup()
ok(updated["ok"] is True and updated["uploaded"] is True, "backup after a new ride uploads")
ok([f["db"] for f in updated["files"]] == ["places"], "only places.db changed, so only it is uploaded")
ok(updated["files"][0]["drive_file_id"] == fid and fake.uploads[-1]["method"] == "PATCH",
   "the update uses the same Drive file id (PATCH)")
ok(updated["files"][0]["modifiedTime"] > by_db["places"]["modifiedTime"], "the same file id has a newer modified time")
ok(fake.copies[-1]["from"] == fid and fake.copies[-1]["parents"] == [ledger_id]
   and fake.copies[-1]["name"].startswith("rivia-places.db (")
   and [l["kind"] for l in updated["ledger"]] == ["previous version"],
   "the previous rivia-places.db is copied into the History Ledger first, not overwritten")
ok(not [u for u in fake.uploads[n_first:] if u.get("parents")], "a .bak copy already in the ledger is not sent again")

# ── emotion.db, body.db and any other .db in ~/rivia: read-only copies, never changed ──
import sqlite3 as _sq
for _n in ("emotion", "body", "extra"):
    _c = _sq.connect(os.path.join(RIVIA, _n + ".db"))
    _c.execute("create table t(x)")
    _c.execute("insert into t values (1)")
    _c.commit()
    _c.close()
emo_before = open(os.path.join(RIVIA, "emotion.db"), "rb").read()
more = g.run_drive_backup()
ok(more["ok"] is True and {f["db"] for f in more["files"]} == {"emotion", "body", "extra"},
   "emotion.db, body.db and another .db in ~/rivia are each backed up: %s" % [f["db"] for f in more["files"]])
ok({f["name"] for f in more["files"]} == {"rivia-emotion.db", "rivia-body.db", "rivia-extra.db"},
   "with their own Drive names")
ok(open(os.path.join(RIVIA, "emotion.db"), "rb").read() == emo_before, "emotion.db is unchanged by the backup")

# ── a new change in approvals.db uploads approvals.db only ──
import rivia_approvals as _approvals
con = _approvals.db_open()
try:
    con.execute(
        "insert into changes(change_id, created, door, key, new, status) values (?,?,?,?,?,?)",
        ("chg-1", "2026-11-15T10:00:00-05:00", "app", "workout_minutes", "90", "applied"),
    )
finally:
    con.close()
forced = g.run_drive_backup()
ok(forced["ok"] is True and [f["db"] for f in forced["files"]] == ["approvals"],
   "a new changes row uploads approvals.db, nothing else")
stable = g.run_drive_backup()
ok(stable["ok"] is True and stable["uploaded"] is False, "after that, a quiet run is again a no-op")
ok(g.run_drive_backup(force=True)["uploaded"] is True, "--force uploads anyway")
n_before = len(fake.uploads)
core_only = g.run_drive_backup(force=True, only=("core",))
ok(core_only["ok"] is True and [f["db"] for f in core_only["files"]] == ["core"],
   "only=('core',) uploads core.db and nothing else")
ok([u["name"] for u in fake.uploads[n_before:]] == ["rivia-core.db"], "only one Drive request, for rivia-core.db")

# ── a fresh places.db (no start-fresh) is restore_first, not a crash ──
os.remove(rp.db_file())
ok(sched.cmd_rides()["fresh"] is True, "wiped places.db is fresh again")
save_token(g.SCOPE_ALL)
blocked = g.run_drive_backup()
ok(blocked["ok"] is False and blocked["error"]["code"] == "restore_first",
   "backup on a fresh places.db is restore_first")
ok(blocked.get("uploaded") is False, "a refused fresh-db backup does not upload")

# ── CLI: rivia sched backup ──
sched.cmd_start_fresh()
save_token(g.SCOPE_ALL)
cli = sh_sched("backup")
ok(cli.returncode == 0, "rivia sched backup exits 0: %s" % cli.stderr)
cli_out = json.loads(cli.stdout)
ok(cli_out["ok"] is True and cli_out["uploaded"] is True and "places" in [f["db"] for f in cli_out["files"]],
   "rivia sched backup uploads the newly unlocked places.db")
cli2 = sh_sched("backup")
ok(cli2.returncode == 0, "rivia sched backup again exits 0")
cli2_out = json.loads(cli2.stdout)
ok(cli2_out["ok"] is True and cli2_out["uploaded"] is False
   and "no new rides" in (cli2_out.get("message") or "").lower(),
   "rivia sched backup with no change reports nothing new")

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
