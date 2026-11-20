"""Place pins, geofence radius, and app_places.
Throwaway RIVIA_DIR; never touches ~/rivia or ~/.rivia.
    python test_places.py
"""
import base64, hashlib, json, os, secrets, subprocess, sys, tempfile

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
import rivia_approvals as approvals


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


# Well-separated pins around Middletown / Hershey (all > 300 m apart).
PINS = {
    "home": {"lat": 40.1989, "lng": -76.7317},
    "farm": {"lat": 40.1920, "lng": -76.7100},
    "gym":  {"lat": 40.2735, "lng": -76.6530},
    "hmc":  {"lat": 40.2660, "lng": -76.6755},
    "pf":   {"lat": 40.2600, "lng": -76.7100},
}
# ~200 m east of the gym pin — inside 2× the 150 m default radius.
TOO_CLOSE = {"lat": 40.2735, "lng": -76.65065}


# ── default radius and empty app_places ──
empty = sched.places_sync()
ok(empty["radius_m"] == 150, "geofence radius defaults to 150")
ok(len(empty["places"]) == 5, "app_places lists all five places")
ok({p["id"] for p in empty["places"]} == set(PINS), "the five place ids are farm, gym, hmc, home, pf")
ok(all(p["lat"] is None and p["lng"] is None and p["radius_m"] == 150 for p in empty["places"]),
   "unpinned places have no lat/lng and report radius_m 150")
ok(all(p["spoken_name"] == rp.PLACES[p["id"]]["say"] for p in empty["places"]),
   "spoken names come from the planner")

# ── pin all five through the Termux command ──
for pid, pin in PINS.items():
    out = sched.cmd_pin(pid, pin["lat"], pin["lng"])
    ok(out["ok"] is True and out["key"] == "place.%s.pin" % pid and out["scope"] == "standing",
       "pin %s is stored as a standing setting" % pid)
    ok(out["value"]["lat"] == pin["lat"] and out["value"]["lng"] == pin["lng"],
       "pin %s stores the given lat,lng" % pid)

# places.json matches the pins
path = rp.PLACES_FILE
ok(os.path.abspath(path).startswith(os.path.abspath(TMP)),
   "places.json lives under the throwaway folder, not ~/rivia")
disk = json.load(open(path))
ok(set(disk) == set(PINS), "places.json has one entry per pinned place")
for pid, pin in PINS.items():
    ok(disk[pid]["lat"] == pin["lat"] and disk[pid]["lng"] == pin["lng"],
       "places.json %s matches the pin" % pid)
ok(rp.place_pins()[ "farm"]["lat"] == PINS["farm"]["lat"],
   "the planner reads the same pins from places.json")

# ── rivia sched call app_places '{}': 5 places, each with lat, lng, radius_m 150 ──
called = sched.cmd_call("app_places", "{}")
ok(called["ok"] is True and called["kind"] == "places", "app_places returns ok places")
items = called["data"]["places"]
ok(len(items) == 5, "app_places returns 5 places")
ok(all(p["lat"] is not None and p["lng"] is not None and p["radius_m"] == 150 for p in items),
   "each place has lat, lng and radius_m 150")
by_id = {p["id"]: p for p in items}
for pid, pin in PINS.items():
    ok(by_id[pid]["lat"] == pin["lat"] and by_id[pid]["lng"] == pin["lng"],
       "app_places %s matches the stored pin" % pid)

cli = sh_sched("call", "app_places", "{}")
ok(cli.returncode == 0, "rivia sched call app_places exits 0: %s" % cli.stderr)
cli_out = json.loads(cli.stdout)
ok(cli_out["ok"] is True and len(cli_out["data"]["places"]) == 5
   and all(p["radius_m"] == 150 and p["lat"] is not None for p in cli_out["data"]["places"]),
   "rivia sched call app_places '{}' returns 5 pinned places at radius 150")

# ── a pin closer than 300 m is refused; existing pin is unchanged ──
before = json.load(open(path))
try:
    sched.cmd_pin("hmc", TOO_CLOSE["lat"], TOO_CLOSE["lng"])
    refused = None
except sched.SettingsError as e:
    refused = e
ok(refused is not None and refused.code == "inconsistent",
   "a pin closer than 300 m is refused")
ok("gym" in str(refused) and "300" in str(refused),
   "the refusal names the other pin and the 300 m minimum")
_gap = rp.meters_between(PINS["gym"], TOO_CLOSE)
ok("%.1f" % _gap in str(refused),
   "the measured pin distance is shown to one decimal, not rounded to a whole metre")
ok(json.load(open(path)) == before, "a refused pin does not rewrite places.json")
ok(sched.cmd_call("app_places", "{}")["data"]["places"]
   and {p["id"]: (p["lat"], p["lng"]) for p in sched.places_sync()["places"]}["hmc"]
   == (PINS["hmc"]["lat"], PINS["hmc"]["lng"]),
   "the existing hmc pin is unchanged")

# CLI form of the same refusal
cli_close = sh_sched("pin", "hmc", str(TOO_CLOSE["lat"]), str(TOO_CLOSE["lng"]))
ok(cli_close.returncode != 0, "rivia sched pin of a too-close pin exits non-zero")
close_out = json.loads(cli_close.stdout)
ok(close_out["ok"] is False and close_out["error"]["code"] == "inconsistent",
   "rivia sched pin reports inconsistent for a too-close pin")

# ── an app-door pin waits for approval (G7); the pin does not move yet ──
app_pin = sched.change_setting_sync("place.farm.pin", {"lat": 40.1925, "lng": -76.7100}, "standing", door="app")
ok(app_pin["status"] == "pending", "an app-door pin waits for approval")
ok(sched.places_sync()["places"][0]["id"] == "farm" and sched.places_sync()["places"][0]["lat"] == PINS["farm"]["lat"],
   "a pending pin does not move the place")
sched.reject_change_sync(app_pin["change_id"])

# ── unknown place / usage ──
try:
    sched.cmd_pin("moon", 40.0, -76.0)
    moon = None
except sched.SettingsError as e:
    moon = e
ok(moon is not None and moon.code == "bad_place", "unknown place is bad_place")
usage = sh_sched("pin", "farm")
ok(usage.returncode != 0 and "usage:" in (usage.stderr or "").lower(),
   "rivia sched pin without lat,lng prints usage")

# ── CLI pin updates places.json ──
# Move pf slightly (still far from the others) through the shell wrapper.
new_pf = {"lat": 40.2550, "lng": -76.7200}
# confirm distance from hmc still ok
ok(rp.meters_between(PINS["hmc"], new_pf) >= 300, "test pf move stays beyond 300 m")
cli_pf = sh_sched("pin", "pf", str(new_pf["lat"]), str(new_pf["lng"]))
ok(cli_pf.returncode == 0, "rivia sched pin pf exits 0")
disk = json.load(open(path))
ok(disk["pf"]["lat"] == new_pf["lat"] and disk["pf"]["lng"] == new_pf["lng"],
   "rivia sched pin rewrites places.json")

# geofence radius is Termux/admin only; tightening it can make existing pins fail
try:
    sched.change_setting_sync("geofence_radius_meters", 200, "standing", door="app")
    rad_err = None
except sched.SettingsError as e:
    rad_err = e
ok(rad_err is not None and rad_err.code == "bad_key",
   "the app door cannot change geofence_radius_meters")
# 200 m radius => 400 m minimum; hmc-pf after the move is ~3 km, still fine.
rad = sched.change_setting_sync("geofence_radius_meters", 200, "standing", door="termux")
ok(rad.get("value") == 200 and rad.get("scope") == "standing", "termux can set geofence_radius_meters")
ok(sched.places_sync()["radius_m"] == 200, "app_places reports the new radius")
# A pin 350 m from gym would now be refused (2×200=400).
near = {"lat": 40.2735, "lng": -76.6489}  # ~350 m east of gym
ok(250 < rp.meters_between(PINS["gym"], near) < 400, "near-gym test pin is between 250 and 400 m")
try:
    sched.cmd_pin("home", near["lat"], near["lng"])
    # wait, home is already pinned far away; this would MOVE home next to gym.
    near_err = None
except sched.SettingsError as e:
    near_err = e
ok(near_err is not None and near_err.code == "inconsistent",
   "raising the radius to 200 refuses a pin under 400 m from another")
# restore default radius
sched.change_setting_sync("geofence_radius_meters", 150, "standing", door="termux")
ok(sched.places_sync()["radius_m"] == 150, "radius can be set back to 150")

# ══ JOB-007: per-place zone sizes, voice resize with undo, places in the settings store ══
def changes(key=None):
    con = approvals.db_open()  # the change log lives in approvals.db since the core split
    try:
        q = "select change_id, door, key, old, new, status, reason from changes"
        rows = list(con.execute(q + (" where key=?" if key else "") + " order by rowid", (key,) if key else ()))
        return rows
    finally:
        con.close()


def zones():
    return {p["id"]: p["radius_m"] for p in sched.places_sync()["places"]}


def change(key, value):
    return sched.cmd_call("change_schedule_setting", json.dumps({"key": key, "scope": "standing", "value": value}))


def undo():
    return sched.cmd_call("undo_schedule_change", "{}")


# ── Fix 1: each place has its own zone size; unset means the default 150 m ──
ok(zones() == dict.fromkeys(PINS, 150) and not any(p["custom"] for p in sched.places_sync()["places"]),
   "every place starts at the default 150 m zone")
home60 = sched.set_zone("home", 60)
ok(home60["value"] == 60 and home60["status"] == "applied", "Termux sets home's zone to 60 m")
ok(zones() == {"home": 60, "farm": 150, "gym": 150, "hmc": 150, "pf": 150},
   "app_places: home 60 m, the other four 150 m")
ok(sched.places_sync()["radius_m"] == 150, "the top-level radius_m stays the default, for old clients")
cli_pl = json.loads(sh_sched("call", "app_places", "{}").stdout)
ok({p["id"]: p["radius_m"] for p in cli_pl["data"]["places"]} == zones(),
   "rivia sched call app_places shows the same per-place sizes")
disk = json.load(open(path))
ok(disk["home"].get("radius_m") == 60 and "radius_m" not in disk["farm"], "places.json carries home's own size")
ok(rp.place_of({"lat": PINS["home"]["lat"] + 0.0009, "lng": PINS["home"]["lng"]}, "") is None,
   "a position 100 m from the home pin is not home any more")
ok(rp.place_of({"lat": PINS["home"]["lat"] + 0.0004, "lng": PINS["home"]["lng"]}, "") == "home",
   "a position 45 m from the home pin is home")

# zones may not overlap: pins at least radius_a + radius_b apart (60 + 150 = 210 m)
near_home = {"lat": PINS["home"]["lat"] + 0.001619, "lng": PINS["home"]["lng"]}   # ~180 m north
giant_pin = {"lat": PINS["home"]["lat"] + 0.001979, "lng": PINS["home"]["lng"]}   # ~220 m north
ok(175 < rp.meters_between(PINS["home"], near_home) < 185 and 215 < rp.meters_between(PINS["home"], giant_pin) < 225,
   "test pins are about 180 m and 220 m from home")
try:
    sched.add_place("Giant", near_home["lat"], near_home["lng"])
    close_err = None
except sched.SettingsError as e:
    close_err = e
ok(close_err is not None and close_err.code == "inconsistent" and "210" in str(close_err),
   "a place 180 m from a 60 m home is refused (zones 150 + 60 would overlap)")
ok("giant" not in json.load(open(path)) and not [p for p in sched.list_places()["places"] if p["id"] == "giant"],
   "a refused place writes nothing")

# ── Fix 3: seed, add, rename, re-pin, remove ──
seed1 = sched.seed_places()
ok(len(seed1["seeded"]) == 5 and all(x["key"].endswith(".name") for x in seed1["seeded"]),
   "the first seed stores the five core names (their pins are already stored)")
n_before = len(changes())
seed2 = sched.seed_places()
ok(seed2["seeded"] == [] and len(changes()) == n_before, "a second seed changes nothing")
ok(all(r[1] == "system" and r[6] == "job-007 places seed" for r in changes() if r[6] == "job-007 places seed"),
   "the seed is logged as a system change")

added = sched.add_place("Giant", giant_pin["lat"], giant_pin["lng"])
ok(added["added"] == "giant" and added["place"]["radius_m"] == 150 and added["place"]["core"] is False,
   "Giant is added 220 m from home with the default 150 m zone")
items = {p["id"]: p for p in sched.places_sync()["places"]}
ok("giant" in items and items["giant"]["spoken_name"] == "Giant" and items["giant"]["custom"] is True
   and items["giant"]["radius_m"] == 150, "app_places lists Giant as a custom place")
ok(json.load(open(path))["giant"].get("custom") is True, "places.json lists Giant as custom")
try:
    sched.add_place("giant", 40.30, -76.80)
    dup_err = None
except sched.SettingsError as e:
    dup_err = e
ok(dup_err is not None and dup_err.code == "place_exists", "adding Giant twice is refused")

renamed = sched.rename_place("farm", "the barn")
ok(renamed["value"] == "the barn" and renamed["status"] == "applied", "Termux renames the farm")
farm_item = [p for p in sched.places_sync()["places"] if p["id"] == "farm"][0]
ok(farm_item["spoken_name"] == "the barn", "app_places speaks the new name; the id stays farm")
with sched._con() as c:
    ok(sched._resolve_place_id(c, "Barn") == "farm" and sched._resolve_place_id(c, "work") == "hmc",
       "places are found by spoken name too")
try:
    sched.rename_place("gym", "work")
    taken = None
except sched.SettingsError as e:
    taken = e
ok(taken is not None and taken.code == "place_exists", "a name already in use is refused")
sched.rename_place("farm", "the farm")

repin = {"lat": giant_pin["lat"] + 0.0002, "lng": giant_pin["lng"] + 0.0002}
out = sched.cmd_pin("Giant", repin["lat"], repin["lng"])
ok(out["ok"] and out["key"] == "place.giant.pin" and json.load(open(path))["giant"]["lat"] == repin["lat"],
   "rivia sched pin re-pins a custom place by name")

# ── Fix 2: voice resize through the app door, applied at once, with undo ──
sm = change("home", "x")  # not a place key: refused below with a plain reason
ok(sm["ok"] is False, "a key that is not a setting is refused")
small = change("place.home.radius_m", "smaller")
ok(small["ok"] and small["kind"] == "change" and small["data"]["status"] == "applied" and small["data"]["value"] == 50,
   "make home smaller: 60 - 25 is kept at the 50 m minimum, applied at once (no approval)")
ok(small["fallback_text"] == "Home is now 50 m (was 60). Say undo to put it back.",
   "the reply says the new and old size: %s" % small["fallback_text"])
again = change("place.home.radius_m", "smaller")
ok(again["ok"] is False and again["error"]["code"] == "no_change" and "50 m" in again["error"]["msg"],
   "smaller again holds at 50 m")
back = undo()
ok(back["ok"] and back["data"]["value"] == 60 and back["data"]["undo_of"] == small["data"]["change_id"]
   and back["fallback_text"] == "Home is back to 60 m.", "undo that puts home back to exactly 60 m")
ok(zones()["home"] == 60, "app_places shows home at 60 m again")
log_cli = json.loads(sh_sched("settings-log").stdout)["changes"]
home_log = [c for c in log_cli if c["key"] == "place.home.radius_m"]
ok([c["status"] for c in home_log][-2:] == ["undone", "applied"] and home_log[-1]["reason"] == "undo %s" % small["data"]["change_id"],
   "rivia sched settings-log shows the resize (undone) and the undo")
small2 = change("place.home.radius_m", "smaller")
ok(small2["ok"] and small2["data"]["value"] == 50 and small2["data"]["duplicate"] is False,
   "a second resize right after an undo is a new change, not a duplicate")
undo()

farm_big = change("place.the farm.radius_m", "bigger")
ok(farm_big["ok"] and farm_big["data"]["key"] == "place.farm.radius_m" and farm_big["data"]["value"] == 175
   and farm_big["fallback_text"] == "The farm is now 175 m (was 150). Say undo to put it back.",
   "make the farm zone bigger: 150 + 25 = 175 m (by spoken name)")
farm_back = undo()
ok(farm_back["ok"] and zones()["farm"] == 150 and farm_back["fallback_text"] == "The farm is back to 150 m."
   and "radius_m" not in json.load(open(path))["farm"], "undo puts the farm back on the default size")
for bad in (30, 500):
    r = change("place.home.radius_m", bad)
    ok(r["ok"] is False and r["error"]["code"] == "out_of_range" and "50 and 400" in r["error"]["msg"],
       "set home to %d meters is refused (50 to 400)" % bad)
sched.set_zone("farm", 390)
top = change("place.farm.radius_m", "bigger")
ok(top["ok"] and top["data"]["value"] == 400, "bigger at 390 stops at 400 m")
top2 = change("place.farm.radius_m", "bigger")
ok(top2["ok"] is False and top2["error"]["code"] == "no_change" and "400 m" in top2["error"]["msg"],
   "bigger again holds at 400 m")
sched.set_zone("farm", 150)
giant60 = change("place.Giant.radius_m", "60 m")
ok(giant60["ok"] and giant60["data"]["key"] == "place.giant.radius_m" and zones()["giant"] == 60,
   "a custom place is resized by its name")
moon = change("place.moon.radius_m", 100)
ok(moon["ok"] is False and moon["error"]["code"] == "bad_place", "an unknown place is refused")
pend = change("place.home.name", "the house")
ok(pend["ok"] and pend["data"]["status"] == "pending", "a rename from the app door waits for approval")
sched.reject_change_sync(pend["data"]["change_id"])

# ── remove: soft, custom only ──
rp.cmd_init_db({})
rp.cmd_log_ride({"ride_id": "g-1", "from": "home", "to": "giant", "departed": "2026-11-16T12:00:00-05:00",
                 "arrived": "2026-11-16T12:04:00-05:00"})
rides_before = rp.load_rides()
gone = sched.remove_place("Giant")
ok(gone["value"] == "removed" and "giant" not in zones(), "Giant is removed from app_places")
ok(json.load(open(path))["giant"].get("removed") is True, "places.json keeps Giant marked removed")
ok([p["status"] for p in sched.list_places()["places"] if p["id"] == "giant"] == ["removed"],
   "rivia sched places still lists Giant as removed")
ok(rp.load_rides() == rides_before and rides_before[0]["to"] == "giant", "removing a place never touches its rides")
for target, code in (("Giant", "no_change"), ("home", "core_place")):
    try:
        sched.remove_place(target)
        err = None
    except sched.SettingsError as e:
        err = e
    ok(err is not None and err.code == code, "remove %s is refused (%s)" % (target, code))
    if code == "core_place":
        ok(str(err) == "Home is a core place and can't be removed.", "the refusal is plain: %s" % err)
back_giant = sched.add_place("Giant", giant_pin["lat"], giant_pin["lng"])
ok(back_giant["added"] == "giant" and "giant" in zones(), "a removed place can be added back")

# ── Pin my location (app door): pending, then approved ──
lib = {"lat": 40.2300, "lng": -76.7600}
pp = sched.cmd_call("app_pin_place", json.dumps({"coords": "%s,%s" % (lib["lat"], lib["lng"]), "name": "Library"}))
ok(pp["ok"] and pp["kind"] == "pending" and pp["data"]["status"] == "pending" and pp["data"]["new_place"] is True,
   "app_pin_place with a new name waits for approval")
ok("library" not in zones(), "a pending place is not in app_places yet")
sched.approve_change_sync(pp["data"]["change_id"])
lib_item = [p for p in sched.places_sync()["places"] if p["id"] == "library"]
ok(lib_item and lib_item[0]["spoken_name"] == "Library" and lib_item[0]["lat"] == lib["lat"],
   "approving adds the place with its name and pin")
pp2 = sched.cmd_call("app_pin_place", json.dumps({"coords": "%s,%s" % (PINS["home"]["lat"] - 0.0003, PINS["home"]["lng"]),
                                                   "place": "home"}))
ok(pp2["ok"] and pp2["data"]["status"] == "pending" and pp2["data"]["place_id"] == "home" and zones()["home"] == 60
   and sched.places_sync()["places"][3]["lat"] == PINS["home"]["lat"],
   "re-pinning a core place from the app waits too: %s" % (pp2.get("error") or pp2["data"].get("status")))
sched.reject_change_sync(pp2["data"]["change_id"])
cli_places = sh_sched("places")
ok(cli_places.returncode == 0 and json.loads(cli_places.stdout)["ok"], "rivia sched places runs")

# Did not touch the box home database or a real places.json
ok(os.path.abspath(rp.db_file()).startswith(os.path.abspath(TMP)),
   "the test database lives under the throwaway folder, not ~/rivia")
ok(os.path.abspath(rp.PLACES_FILE).startswith(os.path.abspath(TMP)),
   "the test places.json lives under the throwaway folder")

print("\nPLACES PASS")
