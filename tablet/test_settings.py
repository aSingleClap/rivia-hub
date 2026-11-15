"""Scheduler 7a: settings store, 17/15 seed, change_setting / undo_change.
Throwaway RIVIA_DIR; never touches ~/rivia or ~/.rivia.
    python test_settings.py
"""
import base64, hashlib, json, os, secrets, sqlite3, tempfile, threading, time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

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
sys_path_set = True
import sys
sys.path.insert(0, HERE)

import rivia_planner as rp
import rivia_sched as sched
import rivia_tools as tools
import rivia_gcal as gcal


def ok(cond, label):
    assert cond, label
    print("  ok ·", label)


NOW = datetime(2026, 12, 4, 20, 0, tzinfo=gcal.TZ)  # Friday evening; tomorrow is Sat Dec 5
SAT = "2026-12-05"
SUN = "2026-12-06"


def changes(con=None):
    close = False
    if con is None:
        con = rp.db_open()
        close = True
    try:
        return list(con.execute(
            "select change_id, key, new, status, door, reason from changes order by rowid"))
    finally:
        if close:
            con.close()


def standing(key, con=None):
    close = False
    if con is None:
        con = rp.db_open()
        close = True
    try:
        row = con.execute("select value from settings where key=?", (key,)).fetchone()
        return None if not row else json.loads(row[0])
    finally:
        if close:
            con.close()


# ── first start seeds 17/15 through the same checks; a second start is a no-op ──
sched.set_selftest(True)
seeded = sched.ensure_store()
ok(standing("ride_estimate.farm_pf") == 17 and standing("ride_estimate.hmc_pf") == 15,
   "first start seeds farm_pf=17 and hmc_pf=15")
rows = changes()
ok(len(rows) == 2 and {r[1] for r in rows} == {"ride_estimate.farm_pf", "ride_estimate.hmc_pf"},
   "seed is logged in changes (one row per key)")
ok(all(r[3] == "applied" and r[4] == "system" and "auto-seed" in (r[5] or "") for r in rows),
   "seed changes are applied, door=system, reason auto-seed")
ids = [r[0] for r in rows]

sched.ensure_store()
ok(standing("ride_estimate.farm_pf") == 17 and standing("ride_estimate.hmc_pf") == 15,
   "second start leaves the seeded values in place")
ok([r[0] for r in changes()] == ids, "second start logs no new change")

# existing value is never overwritten
con = rp.db_open()
try:
    sched.change_setting_sync("ride_estimate.farm_pf", 20, "standing", door="system",
                              reason="owner set 20", con=con, now=NOW)
finally:
    con.close()
ok(standing("ride_estimate.farm_pf") == 20, "an existing standing value can be updated by the system door")
n_after_update = len(changes())
sched.ensure_store()
ok(standing("ride_estimate.farm_pf") == 20, "a later start never overwrites an existing value")
ok(len(changes()) == n_after_update, "a later start does not re-seed an existing key")
# put 17 back for the weekend plans
con = rp.db_open()
try:
    sched.change_setting_sync("ride_estimate.farm_pf", 17, "standing", door="system",
                              reason="restore seed for tests", con=con, now=NOW)
finally:
    con.close()


# ── migrate an existing scheduler.db that has no settings tables ──
MIG = os.path.join(TMP, "legacy")
os.makedirs(MIG)
legacy_path = os.path.join(MIG, "scheduler.db")
leg = sqlite3.connect(legacy_path)
leg.executescript(rp.SCHEMA)
leg.execute("insert into meta values('created','already-there')")
leg.commit()
leg.close()
old_dir = rp.RIVIA_DIR
rp.RIVIA_DIR = MIG
try:
    ok("settings" not in [r[0] for r in sqlite3.connect(legacy_path).execute(
        "select name from sqlite_master where type='table'").fetchall()],
       "legacy db has no settings table yet")
    sched.ensure_store()
    con = rp.db_open()
    try:
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        ok({"settings", "overrides", "changes"} <= tables, "migration adds settings, overrides, and changes")
        ok(standing("ride_estimate.farm_pf", con) == 17 and standing("ride_estimate.hmc_pf", con) == 15,
           "migration seeds 17 and 15 on the existing db")
        ok(con.execute("select value from meta where key='created'").fetchone()[0] == "already-there",
           "migration leaves existing meta alone")
    finally:
        con.close()
finally:
    rp.RIVIA_DIR = old_dir


# ── ride_time uses the seeded estimates ──
farm_pf = sched.ride_time("farm", "pf")
ok(farm_pf["ok"] is True and farm_pf["data"]["minutes"] == 17
   and farm_pf["data"]["source"] == "your estimate",
   "ride_time farm>pf is 17 minutes (your estimate)")
hmc_pf = sched.ride_time("hmc", "pf")
ok(hmc_pf["ok"] is True and hmc_pf["data"]["minutes"] == 15
   and hmc_pf["data"]["source"] == "your estimate",
   "ride_time hmc>pf is 15 minutes (your estimate)")


# ── fake Google with Sat Dec 5 and Sun Dec 6 2:30-11:00 PM ET shifts ──
def shift_item(day):
    return {
        "summary": "Work Shift - HMC", "status": "confirmed",
        "start": {"dateTime": day + "T14:30:00-05:00"},
        "end": {"dateTime": day + "T23:00:00-05:00"},
        "location": "500 University Dr, Hershey, PA 17033",
    }


class FakeG:
    def __init__(self):
        self.items = [shift_item(SAT), shift_item(SUN)]
        self.grant_error = None


fake = FakeG()


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

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = {k: (v[-1] if v else "") for k, v in parse_qs(self.rfile.read(n).decode()).items()}
        if fake.grant_error:
            self._send(400, {"error": fake.grant_error})
            return
        if form.get("grant_type") == "refresh_token" and form.get("refresh_token") == "refresh-good":
            self._send(200, {"access_token": "access-good", "expires_in": 3600, "token_type": "Bearer"})
            return
        self._send(400, {"error": "invalid_grant"})

    def do_GET(self):
        self._send(200, {"items": list(fake.items)})

    def log_message(self, *a):
        pass


httpd = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
port = httpd.server_address[1]
os.environ["RIVIA_GOOGLE_TOKEN_URL"] = "http://127.0.0.1:%d/token" % port
os.environ["RIVIA_GOOGLE_EVENTS_URL"] = "http://127.0.0.1:%d/events" % port
gcal.save_tokens({"refresh_token": "refresh-good", "access_token": "access-good",
                  "expiry": time.time() + 3600, "scope": gcal.SCOPE_READ, "token_type": "Bearer"})
gcal.reset_clock()


def gym_minutes(plan):
    for b in plan["data"]["blocks"]:
        if b.get("place") in ("gym", "pf") and b.get("kind") == "stop":
            return int((b["end"]["epoch"] - b["start"]["epoch"]) // 60)
    return None


def rides_text(plan):
    return " ".join(plan["data"].get("rides") or [])


sat = sched.plan_day_sync(SAT, now=NOW)
ok(sat["ok"] is True and sat["data"]["status"] == "planned" and sat["data"]["written"] is False,
   "Sat Dec 5 plans without need_estimate (status planned)")
ok(sat["data"]["gym"] == "pf" or (sat["data"].get("gym") or {}).get("id") == "pf",
   "Sat Dec 5 uses Planet Fitness")
text = rides_text(sat)
ok("17" in text and "15" in text and "your estimate" in text,
   "Sat Dec 5 uses 17 and 15 minutes (your estimate)")
ok(sat["error"] is None, "Sat Dec 5 has no error")

sun = sched.plan_day_sync(SUN, now=NOW)
ok(sun["ok"] is True and sun["data"]["status"] == "planned",
   "Sun Dec 6 plans without need_estimate (status planned)")
ok("17" in rides_text(sun) and "15" in rides_text(sun) and "your estimate" in rides_text(sun),
   "Sun Dec 6 uses 17 and 15 minutes (your estimate)")
default_gym_m = gym_minutes(sat)
ok(default_gym_m == 150, "default Saturday gym block is 120+30 minutes (got %s)" % default_gym_m)


# ── out of range and floor-above-target are refused ──
def refused(fn):
    try:
        fn()
        return None
    except sched.SettingsError as e:
        return e


err = refused(lambda: sched.change_setting_sync(
    "workout_minutes", 200, "day", day=SAT, door="app", now=NOW, reason="too long"))
ok(err is not None and err.code == "out_of_range" and "200" in str(err),
   "out-of-range value is refused with the reason")

err = refused(lambda: sched.change_setting_sync(
    "workout_minutes", 45, "day", day=SAT, door="app", now=NOW, reason="floor above target"))
ok(err is not None and err.code == "inconsistent" and "floor" in str(err).lower(),
   "floor-above-target is refused with the reason")

# tool wrappers say the same thing
bad = sched.change_setting_tool("workout_minutes", "day", 200, day=SAT)
ok(bad["ok"] is False and bad["error"]["code"] == "out_of_range" and bad["error"]["step"] == "settings",
   "change_setting tool returns out_of_range, step settings")


# ── day change: tomorrow's workout 90 applies only to tomorrow; undo restores ──
# now is Friday Dec 4, tomorrow is Sat Dec 5
chg = sched.change_setting_sync("workout_minutes", 90, "day", day="tomorrow",
                                door="app", now=NOW, reason="make tomorrow's workout 90 minutes",
                                request_id="req-workout-90")
ok(chg["status"] == "applied" and chg["source"] == "day" and chg["value"] == 90
   and chg["day"] == SAT and chg["duplicate"] is False,
   "day change applies now for tomorrow only (source=day)")

sat90 = sched.plan_day_sync("tomorrow", now=NOW)
ok(sat90["ok"] is True and gym_minutes(sat90) == 120,
   "plan_day for tomorrow shows the 90-minute workout (gym block 90+30)")
sun120 = sched.plan_day_sync(SUN, now=NOW)
ok(gym_minutes(sun120) == 150, "the day change does not apply to Sunday")

# get_settings reports the override
got = sched.get_settings_sync(group="schedule", day=SAT, now=NOW)
w = next(s for s in got["settings"] if s["key"] == "workout_minutes")
ok(w["value"] == 90 and w["source"] == "day" and w["default"] == 120,
   "get_settings says tomorrow's workout comes from a day override")
got_sun = sched.get_settings_sync(group="schedule", day=SUN, now=NOW)
w_sun = next(s for s in got_sun["settings"] if s["key"] == "workout_minutes")
ok(w_sun["value"] == 120 and w_sun["source"] == "default",
   "get_settings for Sunday still shows the default workout")
farm = next(s for s in got["settings"] if s["key"] == "ride_estimate.farm_pf")
ok(farm["value"] == 17 and farm["source"] == "standing" and farm.get("in_use") is True,
   "get_settings shows farm_pf=17 as a standing value in use")

# duplicate: same request_id, or a repeat within 60s
dup = sched.change_setting_sync("workout_minutes", 90, "day", day="tomorrow",
                                door="app", now=NOW, request_id="req-workout-90")
ok(dup["duplicate"] is True and dup["change_id"] == chg["change_id"],
   "the same request_id returns duplicate: true and leaves one row")
n_workout = len([r for r in changes() if r[1] == "workout_minutes" and r[3] == "applied"])
ok(n_workout == 1, "a duplicate request leaves a single applied workout_minutes row")

# undo that (no id = latest from this door within 24h)
undone = sched.undo_change_sync(door="app", now=NOW)
ok(undone.get("undo_of") == chg["change_id"] and undone["source"] == "default"
   and undone["value"] == 120,
   "undo that restores the default workout")
sat_again = sched.plan_day_sync(SAT, now=NOW)
ok(gym_minutes(sat_again) == 150, "after undo, Saturday is back to the 120-minute workout")


# ── a standing change from the app door is pending and changes no plan (G7) ──
pend_sync = sched.change_setting_sync(
    "workout_minutes", 90, "standing", door="app", now=NOW, reason="standing from Labs")
ok(pend_sync.get("status") == "pending" and pend_sync.get("asked") == "app"
   and pend_sync.get("change_id"),
   "a standing change from the app door is queued as pending")
# tool form
pend = sched.change_setting_tool("workout_minutes", "standing", 90)
ok(pend["ok"] is True and pend["kind"] == "pending"
   and pend["message"] == sched.PENDING_MESSAGE
   and (pend.get("data") or {}).get("status") == "pending"
   and (pend.get("data") or {}).get("change_id") == pend_sync["change_id"],
   "standing change tool returns waiting for approval and the same pending item")
ok(sched.PENDING_LINE.lower() in sched.CHANGE_SETTING_DESCRIPTION.lower(),
   "the change-setting description includes the pending-result line")
ok(standing("workout_minutes") is None, "a pending standing change is not written yet")
ok(standing("ride_estimate.farm_pf") == 17, "seeded estimates are still 17/15")
ok(gym_minutes(sched.plan_day_sync(SAT, now=NOW)) == 150,
   "a pending standing change leaves Saturday's plan unchanged")

# day out of the 14-day window
err = refused(lambda: sched.change_setting_sync(
    "workout_minutes", 90, "day", day="2026-11-01", door="app", now=NOW))
ok(err is not None and err.code == "bad_day", "a day change in the past is refused")

# get_settings / settings-log CLI shape
cli = sched.get_settings_sync(group="schedule", day=SAT, now=NOW)
ok(cli["day"] == SAT and any(s["key"] == "workout_minutes" for s in cli["settings"]),
   "get_settings returns the schedule group for the day")

# tools seed on the app door (idempotent with existing rows)
con = tools.open_db()
try:
    names = {r[0] for r in con.execute("select name from tools where door='app'")}
finally:
    con.close()
ok({"get_schedule_settings", "change_schedule_setting", "undo_schedule_change"} <= names,
   "settings tools are seeded on the app door")

print("\nSETTINGS PASS")
