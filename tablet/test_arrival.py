"""Arrival, log_ride, same-day alert rerun, and post-ride Drive backup.
Throwaway RIVIA_DIR / tokens; never touches ~/rivia or ~/.rivia.
    python test_arrival.py
"""
import base64, hashlib, json, os, secrets, subprocess, sys, tempfile, threading, time
from datetime import datetime, timedelta
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

import rivia_gcal as gcal
import rivia_planner as rp
import rivia_sched as sched


def ok(cond, label):
    assert cond, label
    print("  ok ·", label)


def sh_sched(*args):
    env = os.environ.copy()
    env["PATH"] = "/tmp/rivia-test-venv/bin:" + env.get("PATH", "")
    return subprocess.run(
        ["bash", os.path.join(HERE, "rivia.sh"), "sched", *args],
        capture_output=True, text=True, env=env, cwd=HERE,
    )


NOW = datetime(2026, 11, 16, 8, 55, tzinfo=gcal.TZ)  # Monday morning
ARRIVED = "2026-11-16T08:55:00-05:00"
SHIFT_ITEM = {
    "summary": "Work Shift - HMC", "status": "confirmed",
    "start": {"dateTime": "2026-11-16T14:30:00-05:00"},
    "end": {"dateTime": "2026-11-16T23:00:00-05:00"},
    "location": "500 University Dr, Hershey, PA 17033",
}
RIDE = {
    "ride_id": "ride-farm-1",
    "from": "home",
    "to": "farm",
    "departed": "2026-11-16T08:40:00-05:00",
    "arrived": ARRIVED,
    "distance_km": 3.2,
    "avg_kmh": 22.0,
}


class FakeGoogle:
    def __init__(self):
        self.items = [SHIFT_ITEM]
        self.files = {}
        self.uploads = []
        self.next_id = 1
        self.clock = 0

    def handle_token(self, body):
        form = {k: (v[-1] if v else "") for k, v in parse_qs(body.decode()).items()}
        if form.get("grant_type") == "refresh_token" and form.get("refresh_token") == "refresh-good":
            return 200, {"access_token": "access-good", "expires_in": 3600, "token_type": "Bearer"}
        return 400, {"error": "invalid_grant"}

    def handle_events(self, headers):
        auth = headers.get("Authorization") or headers.get("authorization") or ""
        if not auth.startswith("Bearer "):
            return 401, {"error": {"code": 401}}
        return 200, {"items": list(self.items)}

    def handle_drive(self, method, path, headers, body):
        auth = headers.get("Authorization") or headers.get("authorization") or ""
        if not auth.startswith("Bearer "):
            return 401, {"error": {"code": 401}}
        rest = urlparse(path).path
        prefix = "/upload/drive/v3/files"
        tail = rest[len(prefix):].lstrip("/") if rest.startswith(prefix) else None
        file_id = tail or None
        if method == "POST":
            file_id = "drive-file-%d" % self.next_id
            self.next_id += 1
        self.clock += 1
        modified = "2026-11-16T12:00:%02d.000Z" % self.clock
        rec = {"id": file_id, "name": "rivia-scheduler.db", "modifiedTime": modified}
        self.files[file_id] = rec
        self.uploads.append({"method": method, "id": file_id})
        return 200, rec


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
            self._send(*fake.handle_token(body))
        elif "/upload/drive/" in self.path:
            self._send(*fake.handle_drive("POST", self.path, self.headers, body))
        else:
            self._send(404, {"error": "no post"})

    def do_PATCH(self):
        body = self._read()
        if "/upload/drive/" in self.path:
            self._send(*fake.handle_drive("PATCH", self.path, self.headers, body))
        else:
            self._send(404, {"error": "no patch"})

    def do_GET(self):
        if self.path.startswith("/events") or "/calendar/" in self.path:
            self._send(*fake.handle_events(self.headers))
        else:
            self._send(404, {"error": "no get"})

    def log_message(self, *a):
        pass


httpd = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
port = httpd.server_address[1]
os.environ["RIVIA_GOOGLE_TOKEN_URL"] = "http://127.0.0.1:%d/token" % port
os.environ["RIVIA_GOOGLE_EVENTS_URL"] = "http://127.0.0.1:%d/events" % port
os.environ["RIVIA_GOOGLE_DRIVE_UPLOAD_URL"] = "http://127.0.0.1:%d/upload/drive/v3/files" % port
gcal.save_tokens({
    "refresh_token": "refresh-good", "access_token": "access-good",
    "expiry": time.time() + 3600, "scope": gcal.SCOPE_ALL, "token_type": "Bearer",
})

# ── 0 rides, start-fresh unlocks log_ride / backup ──
ok(sched.cmd_rides()["rides"] == 0 and sched.cmd_rides()["fresh"] is True,
   "a new database has 0 rides and is still fresh")
fresh = sched.cmd_start_fresh()
ok(fresh["ok"] and fresh["rides"] == 0 and fresh["fresh"] is False,
   "start-fresh unlocks the empty database")

# ── title "test": an alert 2 minutes out ──
test = sched.arrival_sync(title="test", now=NOW)
ok(test["ok"] is True and test["kind"] == "arrival", "app_arrival title=test succeeds")
alerts = test.get("alerts") or []
ok(len(alerts) == 1, "title=test returns one alert")
at = (alerts[0].get("at") or {}).get("epoch")
expect = int(NOW.timestamp()) + 120
ok(at == expect, "the test alert is 2 minutes out (got %s, expected %s)" % (at, expect))
ok("test" in (test.get("fallback_text") or "").lower()
   or "test" in str(test.get("message") or "").lower(),
   "the test arrival mentions the plumbing test")
cli_test = sh_sched("call", "app_arrival", json.dumps({"title": "test"}))
ok(cli_test.returncode == 0, "rivia sched call app_arrival title=test exits 0")
cli_test_out = json.loads(cli_test.stdout)
ok(cli_test_out["ok"] is True and cli_test_out.get("alerts")
   and (cli_test_out["alerts"][0].get("at") or {}).get("epoch"),
   "rivia sched call app_arrival with title test returns an alert")

# ── place farm: leave-by message and alerts ──
farm = sched.arrival_sync(place="farm", time=ARRIVED, now=NOW)
ok(farm["ok"] is True and farm["kind"] == "arrival", "app_arrival at the farm succeeds")
ok(farm["data"].get("place") == "farm", "farm arrival reports place farm")
ok(farm.get("message") and farm.get("fallback_text")
   and "leave" in (farm.get("fallback_text") or "").lower(),
   "farm arrival returns a leave-by message")
ok(isinstance(farm.get("alerts"), list) and farm["alerts"],
   "farm arrival returns leave-by alerts")
leave0 = farm["alerts"]
fallback0 = farm["fallback_text"]
message0 = farm["message"]

# ── same arrival with a ride, twice: duplicate, count +1 ──
before = sched.cmd_rides()["rides"]
ok(before == 0, "still 0 rides before the first logged ride")
first = sched.arrival_sync(place="farm", time=ARRIVED, now=NOW, ride=RIDE)
ok(first["ok"] is True, "arrival with a ride succeeds")
ride1 = first["data"].get("ride") or {}
ok(ride1.get("stored") is True and ride1.get("duplicate") is False,
   "the first ride is stored")
ok(ride1.get("backup") and ride1["backup"].get("uploaded") is True,
   "a Drive backup ran after the new ride")
ok(len(fake.uploads) >= 1, "Drive received the backup upload")
ok(sched.cmd_rides()["rides"] == 1, "ride count is 1 after the first log")

uploads_after_first = len(fake.uploads)
second = sched.arrival_sync(place="farm", time=ARRIVED, now=NOW, ride=RIDE)
ok(second["ok"] is True, "repeating the same arrival still succeeds")
ride2 = second["data"].get("ride") or {}
ok(ride2.get("duplicate") is True and ride2.get("stored") is False,
   "the second log of the same ride_id is duplicate")
ok(sched.cmd_rides()["rides"] == 1, "ride count goes up by exactly 1")
ok(len(fake.uploads) == uploads_after_first,
   "a duplicate ride does not trigger another Drive upload")

# also through app_log_ride
cli_dup = sh_sched("call", "app_log_ride", json.dumps({
    "ride_id": RIDE["ride_id"], "from": "home", "to": "farm",
    "departed": RIDE["departed"], "arrived": RIDE["arrived"],
}))
ok(cli_dup.returncode == 0, "rivia sched call app_log_ride exits 0")
dup_out = json.loads(cli_dup.stdout)
ok(dup_out["ok"] is True and dup_out["data"].get("duplicate") is True,
   "app_log_ride of the same ride_id is duplicate")
ok(sched.cmd_rides()["rides"] == 1, "app_log_ride duplicate leaves the count at 1")

# ── a day change to farm_minutes returns new alerts; undo restores the old ones ──
changed = sched.change_setting_sync(
    "farm_minutes", 30, "day", day="2026-11-16", door="app", now=NOW,
)
ok(changed["value"] == 30 and changed["day"] == "2026-11-16",
   "today's farm_minutes is 30")
refreshed, new_alerts, new_msg = sched.maybe_refresh_alerts(changed, now=NOW)
ok(refreshed.get("alerts_refresh") == "ok", "the day change reran the last arrival")
ok(isinstance(new_alerts, list) and new_alerts, "the rerun returns new alerts")
ok(new_alerts != leave0, "shortening the farm changes the leave-by alerts")
ok(new_msg != message0, "shortening the farm changes the arrival message")

# A failed rerun leaves the change in place and reports alerts_refresh=failed
# (force a failure by temporarily hiding the last-arrival record after the change).
undone = sched.undo_change_sync(change_id=changed["change_id"], door="app", now=NOW)
ok(undone.get("undo_of") == changed["change_id"] or undone.get("value") == 45,
   "undo restores the previous farm_minutes")
restored, old_alerts, old_msg = sched.maybe_refresh_alerts(undone, now=NOW)
ok(restored.get("alerts_refresh") == "ok", "undo also reruns the last arrival")
ok(old_alerts == leave0, "undo returns the original alerts")

# failed refresh: no last arrival
con = rp.db_open()
try:
    rp.set_meta(con, sched.LAST_ARRIVAL_KEY, "")
finally:
    con.close()
again = sched.change_setting_sync(
    "farm_minutes", 20, "day", day="2026-11-16", door="app", now=NOW,
)
# last arrival cleared — no refresh attempted (alerts stay null, change stands)
data, alerts, _msg = sched.maybe_refresh_alerts(again, now=NOW)
ok(again["value"] == 20, "the day change still stands when a rerun cannot run")
ok(alerts is None and "alerts_refresh" not in data,
   "no last arrival: alerts stay null and the change is not marked failed")

# restore a last arrival and make the replay fail
sched.arrival_sync(place="farm", time=ARRIVED, now=NOW)
# Break the stored record so replay fails, then change a setting.
con = rp.db_open()
try:
    rp.set_meta(con, sched.LAST_ARRIVAL_KEY, json.dumps({"place": "not-a-place", "title": "", "time": ARRIVED}))
finally:
    con.close()
broke = sched.change_setting_sync(
    "farm_minutes", 40, "day", day="2026-11-16", door="app", now=NOW,
)
data, alerts, _msg = sched.maybe_refresh_alerts(broke, now=NOW)
ok(broke["value"] == 40, "the change stands when the rerun fails")
ok(alerts is None and data.get("alerts_refresh") == "failed",
   "a failed rerun returns alerts null and data.alerts_refresh=failed")

# ══ JOB-007 ══
def at(hh, mm, ss=0):
    return datetime(2026, 11, 16, hh, mm, ss, tzinfo=gcal.TZ)


# ── home after the farm: the gym is next (ride_estimate.gym_home = 25) ──
sched.change_setting_sync("ride_estimate.gym_home", 25, "standing", door="termux", now=NOW)
home = sched.arrival_sync(place="home", time="2026-11-16T09:30:00-05:00", now=at(9, 30))
f = (home.get("message") or {}).get("facts") or {}
ok(home["ok"] and f.get("next_stop", {}).get("id") == "gym" and f["leave_at"]["spoken"] == "11:20",
   "home after the farm ride: next stop the gym, leave by 11:20 (%s)" % home.get("fallback_text"))
ok(home["fallback_text"] == "Home. Next stop the gym; leave by 11:20." and home["alerts"]
   and home["alerts"][0]["id"].startswith("leave-gym"), "the leave-by alert is for the gym")

# ── Nov 16 replay: 4 queued farm arrivals flushed 10 s apart, 25 min late ──
plans = []
real_cmd_arrival = rp.cmd_arrival


def counting(inp):
    plans.append(inp)
    return real_cmd_arrival(inp)


rp.cmd_arrival = counting
try:
    burst = [sched.arrival_sync(place="farm", time=t, now=at(11, 26, 51) + timedelta(seconds=dt))
             for dt, t in ((0, "2026-11-16T11:01:00-05:00"), (1, "2026-11-16T11:01:20-05:00"),
                           (5, "2026-11-16T11:01:40-05:00"), (10, "2026-11-16T11:02:00-05:00"))]
    ok(all(b["ok"] for b in burst), "every queued arrival still answers ok")
    ok(len(plans) <= 1, "the burst makes at most one plan (made %d)" % len(plans))
    ok(all(b["alerts"] is None for b in burst), "the burst leaves the alerts alone (alerts null)")
    ok(all(b["message"]["delivery"]["say"] is False and b["fallback_text"] == "" for b in burst),
       "the burst says nothing")
    ok([b["data"]["ignored"] for b in burst] == ["late", "duplicate", "duplicate", "duplicate"],
       "the first is late (25 min), the rest are repeats: %s" % [b["data"]["ignored"] for b in burst])
    ok(burst[0]["data"]["late_minutes"] == 25, "late_minutes is 25")
    last = sched._load_last_arrival()
    ok(last["place"] == "farm" and last["time"] == "2026-11-16T11:01:00-05:00",
       "the late arrival is still the newest one on record")
    older = sched.arrival_sync(place="gym", time="2026-11-16T10:50:00-05:00", now=at(11, 27, 30))
    ok(older["data"].get("ignored") == "older" and older["alerts"] is None, "an out-of-order older arrival is ignored")
    fresh = sched.arrival_sync(place="gym", time="2026-11-16T11:44:00-05:00", now=at(11, 44, 8))
    ok(fresh["ok"] and "ignored" not in fresh["data"] and fresh["message"]["delivery"]["say"] is True
       and isinstance(fresh["alerts"], list) and fresh["alerts"], "a fresh on-time arrival still speaks and sets alerts")
    ok(len(plans) == 1, "only the fresh arrival was planned")
    sched.change_setting_sync("arrival_late_minutes", 30, "standing", door="termux", now=NOW)
    work = sched.arrival_sync(place="hmc", time="2026-11-16T12:20:00-05:00", now=at(12, 45))
    ok("ignored" not in work["data"] and work["alerts"] == [] and len(plans) == 2,
       "arrival_late_minutes is a setting: at 30, a 25-minute-late arrival is planned")
    sched.change_setting_sync("arrival_late_minutes", 10, "standing", door="termux", now=NOW)
finally:
    rp.cmd_arrival = real_cmd_arrival

# ── a custom place: named on arrival, keeps the alerts; its rides are kept, not learned ──
sched.add_place("Giant", 40.2018, -76.7200)
g = sched.arrival_sync(place="giant", time="2026-11-16T13:00:00-05:00", now=at(13, 0, 5))
ok(g["ok"] and g["data"]["place"] == "giant" and g["fallback_text"] == "Arrived at Giant." and g["alerts"] is None,
   "arriving at Giant says Giant and keeps the alerts")
g2 = sched.arrival_sync(place="giant", time="2026-11-16T13:40:00-05:00", now=at(13, 40, 5), ride={
    "ride_id": "ride-giant-1", "from": "hmc", "to": "giant",
    "departed": "2026-11-16T13:30:00-05:00", "arrived": "2026-11-16T13:40:00-05:00"})
gr = g2["data"].get("ride") or {}
ok(g2["ok"] and gr.get("stored") is True and gr.get("used") is False and gr["note"].startswith("A custom place"),
   "a ride to Giant is stored but not learned from")
n_rides = sched.cmd_rides()["rides"]
sched.remove_place("giant")
g3 = sched.arrival_sync(place="giant", time="2026-11-16T14:30:00-05:00", now=at(14, 30, 5),
                        left_at="2026-11-16T14:00:00-05:00")  # he left Giant and came back
ok(g3["ok"] and g3["fallback_text"] == "Arrived at Giant." and sched.cmd_rides()["rides"] == n_rides,
   "a removed place is still named (the phone may lag a day) and its rides stay")

# ── exclude-ride / include-ride (Termux) ──
orig = [r for r in rp.load_rides() if r["ride_id"] == RIDE["ride_id"]][0]
ex = sh_sched("exclude-ride", RIDE["ride_id"], "test: geofence fired late")
exo = json.loads(ex.stdout)
ok(ex.returncode == 0 and exo["ok"] and exo["used"] is False and exo["note"] == "excluded: test: geofence fired late",
   "rivia sched exclude-ride marks the ride, never deletes it")
mid = [r for r in rp.load_rides() if r["ride_id"] == RIDE["ride_id"]][0]
ok(mid["used"] is False and mid["note"].startswith("excluded:") and sched.cmd_rides()["rides"] == n_rides,
   "the ride is still stored, now not learned from")
inc = sh_sched("include-ride", RIDE["ride_id"])
back = [r for r in rp.load_rides() if r["ride_id"] == RIDE["ride_id"]][0]
ok(inc.returncode == 0 and back["used"] == orig["used"] and back["note"] == orig["note"],
   "rivia sched include-ride puts the ride back exactly as it was")
usage = sh_sched("exclude-ride", RIDE["ride_id"])
ok(usage.returncode != 0 and "usage" in (usage.stderr or "").lower(), "exclude-ride needs a reason")

# ══ JOB-007b: no repeat welcome-home on app open; the good-morning rundown once a day ══
import contextlib, io


def on(d, hh, mm, ss=0):
    return datetime(2026, 11, d, hh, mm, ss, tzinfo=gcal.TZ)


def home_at(t, **kw):
    return sched.arrival_sync(place="home", time=t.isoformat(), now=t + timedelta(seconds=2), **kw)


def says(r):
    return r["ok"] and r["message"]["delivery"]["say"] is True and bool(r["fallback_text"])


def quiet(r, reason):
    return (r["ok"] and r["data"].get("ignored") == reason and r["message"]["delivery"]["say"] is False
            and r["alerts"] is None and r["fallback_text"] == "")


fake.items = [{
    "summary": "Work Shift - HMC", "status": "confirmed",
    "start": {"dateTime": "2026-11-17T14:30:00-05:00"}, "end": {"dateTime": "2026-11-17T23:00:00-05:00"},
    "location": "500 University Dr, Hershey, PA 17033",
}, {
    "summary": "Dentist", "status": "confirmed",
    "start": {"dateTime": "2026-11-17T09:00:00-05:00"}, "end": {"dateTime": "2026-11-17T09:30:00-05:00"},
}]
real = home_at(on(16, 23, 48))
ok(says(real) and "ignored" not in real["data"], "a real arrival home (from another place) is welcomed")
err = io.StringIO()
with contextlib.redirect_stderr(err):
    night = home_at(on(17, 0, 38))
ok(quiet(night, "repeat"), "opening the app at 12:38 AM at home: no welcome-home (repeat, before 4 AM)")
ok("repeat_arrival_ignored" in err.getvalue(), "the repeat is logged as repeat_arrival_ignored")
morning = home_at(on(17, 8, 4))
ok(says(morning) and morning["data"].get("rundown") is True and "ignored" not in morning["data"],
   "first open after 4 AM at home: the good-morning rundown")
ok(morning["fallback_text"].startswith("Good morning, Jordan. Work 2:30 PM to 11:00 PM.")
   and "Dentist at 9:00 AM" in morning["fallback_text"] and "Leave for the farm by 10:35." in morning["fallback_text"],
   "the rundown has the shift, the calendar, and the next stop with its leave time: %s" % morning["fallback_text"])
ok(morning["message"]["situation"] == "morning_rundown" and "Home." not in morning["fallback_text"],
   "no welcome-home in the same reply")
ok(isinstance(morning["alerts"], list) and morning["alerts"] and morning["alerts"][0]["id"].startswith("leave-farm"),
   "the rundown sets the leave-by alert for the next stop")
again = [home_at(on(17, 8, 6)), home_at(on(17, 8, 12))]
ok(all(quiet(r, "repeat") for r in again), "the 8:06 and 8:12 opens are quiet")
ok(sched._load_last_arrival()["time"] == "2026-11-16T23:48:00-05:00", "repeats never move the last real arrival")
opened = sched.app_open_sync(now=on(17, 9, 0))
ok(opened["ok"] and opened["kind"] == "open" and opened["data"] == {"rundown": False, "reason": "already_given"}
   and opened["message"]["delivery"]["say"] is False, "app_open later the same day is quiet")

farm2 = sched.arrival_sync(place="farm", time=on(17, 10, 40).isoformat(), now=on(17, 10, 40, 2))
back_home = home_at(on(17, 12, 0))
ok(says(farm2) and says(back_home) and "ignored" not in back_home["data"], "arrival at the farm, then home: both spoken")
left = home_at(on(17, 13, 0), left_at=on(17, 12, 30).isoformat())
ok(says(left) and "ignored" not in left["data"], "home, left (left_at), home again: the return is welcomed")
ok(quiet(home_at(on(17, 13, 45)), "repeat"), "home again with no departure since: quiet")

fake.items = []
day_off = sched.app_open_sync(now=on(18, 7, 0))
ok(day_off["ok"] and day_off["data"].get("rundown") is True
   and day_off["fallback_text"] == "Good morning, Jordan. Day off today, nothing on the calendar.",
   "the next day, app_open gives the rundown again (a day off is one line)")
ok(sched.app_open_sync(now=on(18, 7, 5))["data"]["reason"] == "already_given", "once a day only")
ok(sched.app_open_sync(now=on(19, 3, 0))["data"]["reason"] == "before_morning", "nothing before 4 AM")
sched.arrival_sync(place="farm", time=on(19, 9, 0).isoformat(), now=on(19, 9, 0, 2))
ok(sched.app_open_sync(now=on(19, 9, 5))["data"]["reason"] == "not_home", "no rundown away from home")
cli_open = sh_sched("call", "app_open", "{}")
ok(cli_open.returncode == 0 and json.loads(cli_open.stdout)["kind"] == "open", "rivia sched call app_open answers")

# an explicit place="gymday" is stored as the real gym, so the same-place guard sees the app's re-sent ENTER
fake.items = []
g1 = sched.arrival_sync(place="gymday", time=on(20, 11, 43).isoformat(), now=on(20, 11, 43, 2))
ok(says(g1) and sched._load_last_arrival()["place"] == "gym", "gymday arrival: last_arrival holds the resolved gym id")
g2 = sched.arrival_sync(place="gymday", time=on(20, 13, 39).isoformat(), now=on(20, 13, 39, 2))
ok(quiet(g2, "repeat"), "gymday re-sent ENTER with no departure: quiet (1:39 PM repeat)")

# the rundown cutoff: 2:30 PM. Inside the window it is given, outside it is silent and NOT marked given.
fake.items = []
sched.arrival_sync(place="home", time=on(23, 7, 30).isoformat(), now=on(23, 7, 30, 2))
o_1429 = sched.app_open_sync(now=on(23, 14, 29))
ok(o_1429["data"].get("rundown") is True and o_1429["message"]["delivery"]["say"] is True, "app_open at 2:29 PM: the rundown")
sched.rp.set_meta(sched.rp.db_open(), sched.META_MORNING_RUNDOWN, "")  # not given, to test the cutoff
o_1430 = sched.app_open_sync(now=on(23, 14, 30))
ok(o_1430["data"] == {"rundown": False, "reason": "after_cutoff"} and o_1430["message"]["delivery"]["say"] is False
   and o_1430["fallback_text"] == "", "app_open at 2:30 PM: silent, no rundown")
ok(sched.rp.get_meta(sched.rp.db_open(), sched.META_MORNING_RUNDOWN) != "2026-11-23", "2:30 PM open does not mark the day given")
o_2300 = sched.app_open_sync(now=on(23, 23, 0))
ok(o_2300["data"]["reason"] == "after_cutoff" and o_2300["message"]["delivery"]["say"] is False, "app_open at 11:00 PM: silent")
rep = home_at(on(23, 23, 5))
ok(quiet(rep, "repeat") and rep["data"].get("rundown") is None, "a repeat arrival home at 11:05 PM: quiet, no Good morning")
ok(sched.rp.get_meta(sched.rp.db_open(), sched.META_MORNING_RUNDOWN) != "2026-11-23", "11 PM still not marked given")
nxt = sched.app_open_sync(now=on(24, 4, 1))
ok(nxt["data"].get("rundown") is True and nxt["message"]["delivery"]["say"] is True, "next day 4:01 AM: the rundown works normally")
ok(sched.app_open_sync(now=on(24, 14, 29, 59))["data"]["reason"] == "already_given", "once given, 2:29 PM is already_given")

# Did not touch the box home database
ok(os.path.abspath(rp.db_file()).startswith(os.path.abspath(TMP)),
   "the test database lives under the throwaway folder, not ~/rivia")
ok(os.path.abspath(gcal.token_path()).startswith(os.path.abspath(TMP)),
   "the test token lives under the throwaway folder, not ~/.rivia")

httpd.shutdown()
print("\nARRIVAL PASS")
