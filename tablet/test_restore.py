"""Step 5: rivia sched start-fresh, restore, and rides.
Throwaway RIVIA_DIR; never touches ~/rivia or ~/.rivia.
    python test_restore.py
"""
import base64, hashlib, json, os, secrets, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
DOT = os.path.join(TMP, "dot-rivia")
os.makedirs(DOT, mode=0o700)
_salt = secrets.token_bytes(16)
os.environ.update(
    RIVIA_DIR=os.path.join(TMP, "rivia"),
    RIVIA_DOTDIR=DOT,
    RIVIA_DB=os.path.join(TMP, "auth.db"),
    RIVIA_ISSUER="https://mcp.rivia.observer",
    RIVIA_JWT_KEY=secrets.token_hex(32),
    RIVIA_PASS_HASH=_salt.hex() + "$" + hashlib.scrypt(b"x" * 16, salt=_salt, n=2**14, r=8, p=1).hex(),
    RIVIA_TOTP_SECRET=base64.b32encode(secrets.token_bytes(20)).decode().rstrip("="),
    RIVIA_SIGNIN_CHECK="0",
)
sys.path.insert(0, HERE)

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


def sh_sched(*args):
    env = os.environ.copy()
    env["PATH"] = "/tmp/rivia-test-venv/bin:" + env.get("PATH", "")
    r = subprocess.run(
        ["bash", os.path.join(HERE, "rivia.sh"), "sched", *args],
        capture_output=True, text=True, env=env, cwd=HERE,
    )
    return r


# ── before restore, the live database has 0 rides ──
got = sched.cmd_rides()
ok(got["ok"] is True and got["rides"] == 0, "rides before restore shows 0")
ok(got.get("fresh") is True, "a new database is still marked fresh")

cli = sh_sched("rides")
ok(cli.returncode == 0, "rivia sched rides exits 0")
payload = json.loads(cli.stdout)
ok(payload["ok"] is True and payload["rides"] == 0, "rivia sched rides reports 0 before restore")

# ── make Ard-style copies (scheduler.db and scheduler.json) in a source folder ──
# Build rides in this live db, backup, copy the backup files aside, then wipe the live db.
rp.cmd_init_db({})
N = 3
for i in range(N):
    rec = rp.cmd_log_ride(ride("r%d" % i, start="2026-11-%02dT09:00:00-05:00" % (9 + i), minutes=15 + i))
    ok(rec["stored"] is True, "logged seed ride r%d" % i)
bak = rp.cmd_backup({"force": True})
ok(bak["upload"] is True and bak["rides"] == N, "backup wrote a copy with %d rides" % N)
src = os.path.join(TMP, "ard-copy")
os.makedirs(src)
db_copy = os.path.join(src, "scheduler.db")
json_copy = os.path.join(src, "scheduler.json")
shutil.copy(bak["db_path"], db_copy)
shutil.copy(bak["json_path"], json_copy)
ok(os.path.isfile(db_copy) and os.path.isfile(json_copy), "kept scheduler.db and scheduler.json copies")

# Wipe the live database (as if the tablet were fresh) and restore from JSON.
os.remove(rp.db_file())
got = sched.cmd_rides()
ok(got["rides"] == 0 and got.get("fresh") is True, "after wiping, rides is 0 again")

out = sched.cmd_restore_file(json_copy)
ok(out["ok"] is True and out["added"] == N and out["in_copy"] == N
   and out["rides_before"] == 0 and out["rides"] == N,
   "restore from scheduler.json adds all %d rides" % N)
got = sched.cmd_rides()
ok(got["rides"] == N and got.get("fresh") is False,
   "after restore, rides equals the copy's count (%d)" % N)

# Running restore again changes nothing
again = sched.cmd_restore_file(json_copy)
ok(again["added"] == 0 and again["rides"] == N and again["rides_before"] == N,
   "restoring the JSON copy a second time adds nothing")
ok(sched.cmd_rides()["rides"] == N, "ride count is unchanged after a second restore")

# Merge never drops a ride that is only on the tablet
extra = rp.cmd_log_ride(ride("local-only", start="2026-11-20T09:00:00-05:00", minutes=22))
ok(extra["stored"] is True, "a new ride can be logged after restore")
ok(sched.cmd_rides()["rides"] == N + 1, "live database now has the extra ride")
merged = sched.cmd_restore_file(json_copy)
ok(merged["added"] == 0 and merged["rides"] == N + 1,
   "restore merges and never drops the tablet-only ride")

# Restore from scheduler.db into a wiped database
os.remove(rp.db_file())
from_db = sched.cmd_restore_file(db_copy)
ok(from_db["ok"] is True and from_db["added"] == N and from_db["rides"] == N,
   "restore from scheduler.db adds all %d rides" % N)
again_db = sched.cmd_restore_file(db_copy)
ok(again_db["added"] == 0 and again_db["rides"] == N,
   "restoring the db copy a second time adds nothing")

# CLI: rivia sched restore <file>
os.remove(rp.db_file())
cli0 = sh_sched("rides")
ok(json.loads(cli0.stdout)["rides"] == 0, "CLI rides is 0 on a fresh file")
cli_r = sh_sched("restore", json_copy)
ok(cli_r.returncode == 0, "rivia sched restore exits 0")
cli_out = json.loads(cli_r.stdout)
ok(cli_out["ok"] is True and cli_out["added"] == N and cli_out["rides"] == N,
   "rivia sched restore from JSON reports the copy's count")
cli1 = sh_sched("rides")
ok(json.loads(cli1.stdout)["rides"] == N, "rivia sched rides equals Ard's count after restore")
cli2 = sh_sched("restore", json_copy)
ok(json.loads(cli2.stdout)["added"] == 0 and json.loads(cli2.stdout)["rides"] == N,
   "rivia sched restore again changes nothing")

# Missing file is refused
bad = sh_sched("restore", os.path.join(src, "missing.db"))
ok(bad.returncode != 0, "restore of a missing file exits non-zero")
bad_payload = json.loads(bad.stdout)
ok(bad_payload["ok"] is False and bad_payload["error"]["code"] == "bad_copy",
   "a missing file is bad_copy")

# usage
usage = sh_sched("restore")
ok(usage.returncode != 0 and "usage:" in (usage.stderr or "").lower(),
   "rivia sched restore without a file prints usage")

# ── start-fresh (init-db): unlocks a new empty database; a second run is a no-op ──
os.remove(rp.db_file())
ok(sched.cmd_rides()["fresh"] is True, "a wiped database is marked fresh again")
fresh = sched.cmd_start_fresh()
ok(fresh["ok"] is True and fresh["initialized"] is True
   and fresh["rides"] == 0 and fresh["fresh"] is False,
   "start-fresh on a new database unlocks it and leaves 0 rides")
got = sched.cmd_rides()
ok(got["ok"] is True and got["rides"] == 0 and got["fresh"] is False,
   "after start-fresh, rides is 0 and fresh is false")
cli_sf = sh_sched("rides")
ok(cli_sf.returncode == 0, "rivia sched rides exits 0 after start-fresh")
cli_payload = json.loads(cli_sf.stdout)
ok(cli_payload["rides"] == 0 and cli_payload["fresh"] is False,
   "rivia sched rides shows rides: 0, fresh: false after start-fresh")

again_sf = sched.cmd_start_fresh()
ok(again_sf["initialized"] is False and again_sf["rides"] == 0
   and again_sf["fresh"] is False
   and "not a new database" in (again_sf.get("note") or "").lower()
   and "nothing changed" in (again_sf.get("note") or "").lower(),
   "start-fresh again says not a new database; nothing changed")
cli_again = sh_sched("start-fresh")
ok(cli_again.returncode == 0, "rivia sched start-fresh exits 0")
cli_again_out = json.loads(cli_again.stdout)
ok(cli_again_out["initialized"] is False
   and "not a new database" in (cli_again_out.get("note") or "").lower()
   and "nothing changed" in (cli_again_out.get("note") or "").lower(),
   "rivia sched start-fresh again reports the no-op note")

# Restoring an empty JSON copy adds 0 rides and drops none (including a tablet-only ride)
empty_json = os.path.join(TMP, "empty-copy.json")
with open(empty_json, "w") as f:
    json.dump({"rides": []}, f)
empty0 = sched.cmd_restore_file(empty_json)
ok(empty0["ok"] is True and empty0["added"] == 0 and empty0["in_copy"] == 0
   and empty0["rides_before"] == 0 and empty0["rides"] == 0,
   "restoring an empty JSON copy onto a fresh start adds 0 rides")
local = rp.cmd_log_ride(ride("keep-me", start="2026-11-22T09:00:00-05:00", minutes=18))
ok(local["stored"] is True, "a ride can be logged after start-fresh")
ok(sched.cmd_rides()["rides"] == 1, "live database has the one local ride")
empty1 = sched.cmd_restore_file(empty_json)
ok(empty1["added"] == 0 and empty1["in_copy"] == 0
   and empty1["rides_before"] == 1 and empty1["rides"] == 1,
   "restoring an empty JSON copy adds 0 rides and drops none")

# Did not touch the box home database
home_db = os.path.expanduser("/home/box/rivia/scheduler.db")
ok(os.path.abspath(rp.db_file()).startswith(os.path.abspath(TMP)),
   "the test database lives under the throwaway folder, not ~/rivia")

print("\nRESTORE PASS")
