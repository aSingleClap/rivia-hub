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

# ── app door cannot set a pin ──
try:
    sched.change_setting_sync("place.farm.pin", PINS["farm"], "standing", door="app")
    app_err = None
except sched.SettingsError as e:
    app_err = e
ok(app_err is not None and app_err.code == "bad_key",
   "the app door cannot change a place pin")

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

# Did not touch the box home database or a real places.json
ok(os.path.abspath(rp.db_file()).startswith(os.path.abspath(TMP)),
   "the test database lives under the throwaway folder, not ~/rivia")
ok(os.path.abspath(rp.PLACES_FILE).startswith(os.path.abspath(TMP)),
   "the test places.json lives under the throwaway folder")

print("\nPLACES PASS")
