"""The core split: separate DBs migrated from scheduler.db, places never importing scheduling, a
scheduling failure never blocking core or places, and the app door on a real test server.
Throwaway RIVIA_DIRs and secrets; never touches ~/rivia or ~/.rivia.
    python test_core_split.py
"""
import ast, asyncio, json, os, secrets, socket, sqlite3, subprocess, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ.update(RIVIA_DIR=os.path.join(TMP, "rivia"), RIVIA_DOTDIR=os.path.join(TMP, "dot"),
                  RIVIA_DB=os.path.join(TMP, "auth.db"), RIVIA_JWT_KEY=secrets.token_hex(32),
                  RIVIA_SIGNIN_CHECK="0", RIVIA_GOOGLE_TOKEN_PATH=os.path.join(TMP, "dot", "google_token.json"))
os.makedirs(os.environ["RIVIA_DOTDIR"], mode=0o700)
sys.path.insert(0, HERE)


def ok(cond, label):
    assert cond, label
    print("  ok ·", label)


def run_py(code, rivia_dir, extra_env=None):
    env = dict(os.environ, RIVIA_DIR=rivia_dir, **(extra_env or {}))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=HERE)
    if r.returncode != 0:
        print(r.stdout[-2000:], r.stderr[-4000:])
    return r


# ── 1. the migration out of a pre-split scheduler.db ──
LEGACY = os.path.join(TMP, "legacy")
os.makedirs(LEGACY)
src = sqlite3.connect(os.path.join(LEGACY, "scheduler.db"))
src.executescript("""
create table rides(ride_id text primary key, day text, from_place text, to_place text, departed text, arrived text,
    left_local text, arrived_local text, minutes real, used integer, note text, distance_km real, avg_kmh real,
    polyline text, logged text);
create table meta(key text primary key, value text);
create table settings(key text primary key, value text not null, updated text not null, change_id text);
create table overrides(day text not null, key text not null, value text not null, created text not null,
    change_id text, primary key (day, key));
create table changes(change_id text primary key, created text not null, door text, kind text, scope text, day text,
    key text, old text, new text, preview text, status text, decided text, reason text, request_id text);
create table tools(name text not null, door text not null, action text not null, preset text not null default '{}',
    description text not null, model integer not null default 1, enabled integer not null default 1,
    updated text not null, primary key (door, name));
create table calls(ts text not null, door text not null, tool text not null, action text not null, ok integer not null,
    code text, ms integer not null);
""")
for i, (a, b, m) in enumerate((("home", "farm", 5.0), ("farm", "gym", 19.0), ("gym", "hmc", 6.0))):
    src.execute("insert into rides values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("r%d" % i, "2026-11-16", a, b, "2026-11-16T09:00:00-05:00", "2026-11-16T09:10:00-05:00",
                 "9:00 AM", "9:10 AM", m, 1, "", None, None, None, "2026-11-16T09:10:01-05:00"))
for k, v in (("change", "5"), ("created", "2026-11-15T02:36:15-05:00"), ("last_arrival", '{"place":"home","title":"","time":"2026-11-16T23:48:00-05:00"}'),
             ("ride_excluded.r9", '{"used":true,"note":""}'), ("gcal_rivia_id", "cal-1"), ("selftest", '{"ok":true}'),
             ("drive_file_id", "legacy-file"), ("morning_rundown_date", "2026-11-17")):
    src.execute("insert into meta values(?,?)", (k, v))
for k, v in (("place.home.pin", '{"lat":40.2,"lng":-76.72}'), ("place.home.radius_m", "60"),
             ("geofence_radius_meters", "150"), ("ride_estimate.farm_pf", "17"), ("workout_minutes", "120")):
    src.execute("insert into settings values(?,?,?,?)", (k, v, "2026-11-15T07:55:11-05:00", None))
src.execute("insert into overrides values('2026-11-20','farm_minutes','30','2026-11-19T10:00:00-05:00',NULL)")
for i in range(4):
    src.execute("insert into changes(change_id, created, door, kind, key, new, status) values(?,?,?,?,?,?,?)",
                ("chg_%d" % i, "2026-11-15T09:0%d:00-05:00" % i, "termux", "setting", "place.home.pin", "1", "applied"))
src.execute("insert into tools values('app_places','app','sched.places','{}','old text',0,1,'2026-11-15')")
src.execute("insert into tools values('ride_time','app','sched.ride_time','{}','old text',1,1,'2026-11-15')")
for i in range(3):
    src.execute("insert into calls values(?,?,?,?,?,?,?)", ("2026-11-16T10:0%d:00-05:00" % i, "app", "app_arrival",
                                                             "sched.arrival", 1, None, 300))
src.commit()
src.close()

MIGRATE = """
import json, sqlite3, rivia_store as store, rivia_places as places, rivia_approvals as approvals, rivia_tools as tools
out = {}
for name, opener in (("places", places.db_open), ("approvals", approvals.db_open), ("core", tools.open_db)):
    con = opener()
    out[name] = {"report": store.migration_report(con),
                 "tables": {t: con.execute("select count(*) from %s" % t).fetchone()[0]
                            for (t,) in con.execute("select name from sqlite_master where type='table'")},
                 "meta": dict(con.execute("select key, value from meta"))}
    if name == "places":
        out[name]["settings"] = sorted(r[0] for r in con.execute("select key from settings"))
        out[name]["fresh"] = places.is_fresh(con)
    con.close()
old = sqlite3.connect(store.scheduler_db_path())
out["old"] = {t: old.execute("select count(*) from %s" % t).fetchone()[0]
              for t in ("rides", "meta", "settings", "overrides", "changes", "tools", "calls")}
print(json.dumps(out))
"""
r = run_py(MIGRATE, LEGACY)
ok(r.returncode == 0, "the new DBs open on a pre-split scheduler.db")
m1 = json.loads(r.stdout.strip().splitlines()[-1])
p = m1["places"]
ok(p["report"]["source"] == "scheduler.db" and p["report"]["counts"] == {"rides": 3, "settings": 3, "meta": 5},
   "places.db took 3 rides, 3 place settings and 5 meta rows: %s" % p["report"]["counts"])
ok(p["settings"] == ["geofence_radius_meters", "place.home.pin", "place.home.radius_m"],
   "only place-owned settings moved to places.db")
ok({"last_arrival", "change", "created", "ride_excluded.r9", "morning_rundown_date"} <= set(p["meta"])
   and not ({"gcal_rivia_id", "selftest", "drive_file_id"} & set(p["meta"])),
   "arrival memory and ride bookkeeping moved; Google bookkeeping stayed with scheduling")
ok(p["fresh"] is False, "a migrated places.db is not fresh (rides came across)")
ok(m1["approvals"]["report"]["counts"] == {"changes": 4} and m1["approvals"]["tables"]["changes"] == 4,
   "approvals.db took the 4 changes")
ok(m1["core"]["report"]["counts"] == {"tools": 2, "calls": 3} and m1["core"]["tables"]["calls"] == 3,
   "core.db took the 2 tool rows and the 3 calls")
ok(m1["old"] == {"rides": 3, "meta": 8, "settings": 5, "overrides": 1, "changes": 4, "tools": 2, "calls": 3},
   "scheduler.db itself is left exactly as it was (nothing deleted)")
r = run_py(MIGRATE, LEGACY)
m2 = json.loads(r.stdout.strip().splitlines()[-1])
ok(m2["places"]["tables"]["rides"] == 3 and m2["approvals"]["tables"]["changes"] == 4 and m2["core"]["tables"]["calls"] == 3
   and m2["places"]["report"]["at"] == p["report"]["at"], "a second open copies nothing again (runs once)")
FRESH = os.path.join(TMP, "fresh")
r = run_py(MIGRATE.replace("old = sqlite3.connect(store.scheduler_db_path())", "old = None").split("out[\"old\"]")[0]
           + "print(json.dumps(out))", FRESH)
mf = json.loads(r.stdout.strip().splitlines()[-1])
ok(mf["places"]["report"]["source"] == "none" and mf["places"]["fresh"] is True,
   "a brand-new install has a fresh places.db (restore or init-db first)")

# ── 2. places never imports scheduling; core never needs it at load time ──
SCHEDULING = {"rivia_sched", "rivia_planner", "rivia_gcal"}


def imports(path, top_level_only):
    tree = ast.parse(open(os.path.join(HERE, path)).read())
    nodes = tree.body if top_level_only else list(ast.walk(tree))
    found = set()
    for n in nodes:
        if isinstance(n, ast.Import):
            found |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            found.add(n.module)
    return found


ok(not (imports("rivia_places.py", False) & SCHEDULING), "rivia_places.py never imports scheduling")
for mod in ("rivia_store.py", "rivia_time.py", "rivia_settings.py", "rivia_approvals.py"):
    ok(not (imports(mod, False) & (SCHEDULING | {"rivia_places"})), "%s imports neither scheduling nor places" % mod)
for mod in ("rivia_tools.py", "rivia_auth.py", "rivia_applogin.py", "rivia_google.py", "rivia_battery.py",
            "rivia_admin.py", "rivia_server.py"):
    ok(not (imports(mod, True) & SCHEDULING), "core %s does not import scheduling when it loads" % mod)
ok(not os.path.exists(os.path.join(HERE, "rivia_bots.py")), "rivia_bots.py is gone")

# ── 3. scheduling fails to load: core and places keep serving ──
NO_SCHED = """
import asyncio, json, sys
sys.modules["rivia_sched"] = None      # make `import rivia_sched` fail
import rivia_places, rivia_server as rs
from fastmcp import Client
rivia_places.cmd_start_fresh()
rs.build_doors()
async def main():
    async with Client(rs.app_mcp) as c:
        names = sorted(t.name for t in await c.list_tools())
        res = {}
        for tool, args in (("app_places", {}), ("app_arrival", {"place": "farm"}),
                           ("app_log_ride", {"ride_id": "x1", "from": "home", "to": "farm",
                                             "departed": "2026-11-16T09:00:00-05:00", "arrived": "2026-11-16T09:06:00-05:00"}),
                           ("app_open", {}), ("hub_status", {}), ("get_tablet_battery", {})):
            r = await c.call_tool(tool, args, raise_on_error=False)
            res[tool] = r.structured_content or {}
    print(json.dumps({"names": names, "res": res, "loaded": rs.SCHEDULING}))
asyncio.run(main())
"""
r = run_py(NO_SCHED, os.path.join(TMP, "nosched"))
ok(r.returncode == 0, "the doors build without scheduling")
ns = json.loads(r.stdout.strip().splitlines()[-1])
ok(ns["loaded"]["loaded"] is False, "the server notes that scheduling did not load")
ok(set(ns["names"]) == {"get_tablet_battery", "hub_status", "list_tools", "list_calls", "app_places",
                        "app_arrival", "app_log_ride", "app_pin_place", "app_open"},
   "without scheduling the app door still has battery, the hub tools and the five places tools: %s" % ns["names"])
res = ns["res"]
ok(res["app_places"].get("ok") is True and len(res["app_places"]["data"]["places"]) == 5, "app_places works")
arr = res["app_arrival"]
ok(arr.get("ok") is True and arr["data"].get("planned") is False and arr["fallback_text"] == "Arrived at the farm."
   and arr["alerts"] is None and arr["message"]["delivery"]["say"] is True,
   "app_arrival is recorded and answered plainly, alarms left alone")
ok(res["app_log_ride"].get("ok") is True and res["app_log_ride"]["data"]["stored"] is True, "app_log_ride stores the ride")
ok(res["app_open"].get("ok") is True and res["app_open"]["message"]["delivery"]["say"] is False, "app_open stays quiet")
ok(res["hub_status"].get("ok") is True and res["hub_status"].get("scheduling") == "unavailable",
   "hub_status works and says scheduling is unavailable")

# ── 4. the planner selftest fails: places still works; scheduling refuses ──
import rivia_admin as admin
import rivia_places as places
import rivia_sched as sched
places.cmd_start_fresh()
sched.set_selftest(False)
a = places.arrival_sync(place="gym", time=time.time(), now=None)
ok(a["ok"] is True and a["data"].get("planned") is False and a["fallback_text"] == "Arrived at the gym.",
   "a failed planner selftest does not block arrivals")
lr = places.log_ride_sync({"ride_id": "y1", "from": "gym", "to": "hmc",
                           "departed": "2026-11-16T14:16:00-05:00", "arrived": "2026-11-16T14:21:00-05:00"})
ok(lr["ok"] is True and lr["data"]["stored"] is True, "or rides")
ok(admin.set_setting("geofence_radius_meters", 160).get("ok") is True, "or a place setting from the hub")
bad = admin.set_setting("workout_minutes", 100)
ok(bad.get("ok") is False and bad["error"]["code"] == "selftest_failed", "but schedule settings wait for the selftest")
rt_ = sched.ride_time.fn("home", "hmc") if hasattr(sched.ride_time, "fn") else sched.ride_time("home", "hmc")
ok(rt_.get("ok") is False and rt_["error"]["code"] == "selftest_failed", "and scheduling tools are blocked")
sched.set_selftest(True)
admin.set_setting("geofence_radius_meters", 150)

# ── 5. a test instance on another port: tools/list on /app/mcp, and app tools round-trip ──
import uvicorn
import rivia_auth as ra
import rivia_server as rs
import rivia_tools as tools
from fastmcp import Client

sched.reset_selftest()
tools.reset_install()
with socket.socket() as s_:
    s_.bind(("127.0.0.1", 0))
    PORT = s_.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(rs.public_app(), host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(200):
    if server.started:
        break
    time.sleep(0.05)
ok(server.started and PORT != 8787, "a test server is up on port %d (not the live 8787)" % PORT)
APP_TOKEN = ra.mint_door_token("app")["token"]
ADMIN_TOKEN = ra.mint_door_token("admin")["token"]
EXPECTED = {"get_tablet_battery", "hub_status", "list_tools", "list_calls",
            "app_places", "app_arrival", "app_log_ride", "app_pin_place", "app_open",
            "ride_time", "plan_day", "get_schedule_settings", "change_schedule_setting", "undo_schedule_change",
            "write_day"}


async def over_http():
    async with Client("http://127.0.0.1:%d/app/mcp" % PORT, auth=APP_TOKEN) as c:
        names = {t.name for t in await c.list_tools()}
        pl = (await c.call_tool("app_places", {}, raise_on_error=False)).structured_content or {}
        lt = (await c.call_tool("list_tools", {}, raise_on_error=False)).structured_content or {}
        rt2 = (await c.call_tool("ride_time", {"from": "home", "to": "hmc"}, raise_on_error=False)).structured_content or {}
        hs = (await c.call_tool("hub_status", {}, raise_on_error=False)).structured_content or {}
    async with Client("http://127.0.0.1:%d/admin/mcp" % PORT, auth=ADMIN_TOKEN) as c:
        admin_names = {t.name for t in await c.list_tools()}
    return names, pl, lt, rt2, hs, admin_names


names, pl, lt, rt2, hs, admin_names = asyncio.run(over_http())
ok(names == EXPECTED, "tools/list on /app/mcp returns exactly the expected tools: %s" % sorted(names))
ok(pl.get("ok") is True and pl.get("kind") == "places" and {p_["id"] for p_ in pl["data"]["places"]}
   == {"home", "farm", "gym", "pf", "hmc"}, "app_places round-trips over HTTP")
ok(lt.get("ok") is True and "app_arrival" in {t["name"] for t in lt["tools"]}, "list_tools (read only) round-trips")
ok(rt2.get("ok") is True and rt2["data"]["minutes"] == 25, "ride_time round-trips (home to work 25 min)")
ok(hs.get("ok") is True and hs.get("scheduling") == "running", "hub_status round-trips and says scheduling is running")
ok({"set_setting", "restart_server", "approve_change"} <= admin_names and "set_setting" not in names,
   "set_setting and restart_server stay on /admin/mcp only")
calls = tools.list_call_rows(limit=50)
ok({c_["tool"] for c_ in calls} >= {"app_places", "ride_time"} and all(c_["door"] == "app" for c_ in calls),
   "the calls land in core.db's call log")
server.should_exit = True
print("\nCORE SPLIT PASS")
