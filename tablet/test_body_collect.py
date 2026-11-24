"""Emotion spec Step 4.2: the body-signal collector. Runs on a fake tablet in a temp folder.
    python test_body_collect.py
Needs, beside it: rivia_body_collect.py (4.2), rivia_body_signals.py (4.1), rivia_normal.py (4.3), the tablet code
(rivia_store, rivia_time, and rivia_server with 4.2's start_body_collector for the server checks).

The heart of it is Jordan's condition (Nov 22): "As long as the connector doesn't sign me out then that's fine".
So most checks are about what the collector never touches: the Google token, the app and admin tokens, Google
itself, the app login route, the server process and cloudflared, and the sign-in check's own timer.
"""
import ast
import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
RIVIA = os.path.join(TMP, "rivia")
DOT = os.path.join(TMP, ".rivia")
LOGS = os.path.join(RIVIA, "logs")
for d in (RIVIA, DOT, LOGS):
    os.makedirs(d, mode=0o700, exist_ok=True)
os.environ.update(
    HOME=TMP, RIVIA_DIR=RIVIA, RIVIA_HOME=RIVIA, RIVIA_DOTDIR=DOT, RIVIA_DB=os.path.join(TMP, "auth.db"),
    RIVIA_MAILBOX_DB=os.path.join(TMP, "mailbox.db"), RIVIA_ISSUER="https://mcp.rivia.observer",
    RIVIA_JWT_KEY=secrets.token_hex(32), RIVIA_PASS_HASH="x", RIVIA_TOTP_SECRET="MFRGGZDFMY",
    RIVIA_GOOGLE_TOKEN_PATH=os.path.join(DOT, "google_token.json"),
)
os.environ.pop("RIVIA_BODY_COLLECTOR", None)
os.environ.pop("RIVIA_BODY_INTERVAL_S", None)
sys.path.insert(0, HERE)

import rivia_body_collect as bc  # noqa: E402
import rivia_body_signals as body  # noqa: E402
import rivia_normal as normal  # noqa: E402

TZ = ZoneInfo("America/New_York")
failed = 0
checks = 0


def ok(cond, label):
    global failed, checks
    checks += 1
    print(("  ok · " if cond else "FAIL · ") + label)
    if not cond:
        failed += 1


# ---------- the things that hold a sign-in, planted where the tablet keeps them ----------
SECRETS = {
    os.path.join(DOT, "google_token.json"): json.dumps({"refresh_token": "r-planted", "access_token": "a"}),
    os.path.join(DOT, "admin_token"): "planted-admin",
    os.path.join(DOT, ".env"): "RIVIA_JWT_KEY=planted\n",
}
for p, text in SECRETS.items():
    with open(p, "w") as f:
        f.write(text)
con = sqlite3.connect(os.environ["RIVIA_DB"])
con.executescript("create table tokens(jti text primary key, client_id text, kind text, expires real, "
                  "revoked integer default 0); insert into tokens values('phone','rivia-app','app',9e12,0);")
con.commit()
con.close()
SECRETS[os.environ["RIVIA_DB"]] = None


def fingerprint():
    out = {}
    for p in SECRETS:
        st = os.stat(p)
        with open(p, "rb") as f:
            out[p] = (hashlib.sha256(f.read()).hexdigest(), st.st_mtime_ns, st.st_size)
    return out


BEFORE = fingerprint()

# ---------- a fake tablet for 4.1's real readers ----------
PROC = os.path.join(TMP, "proc")
POWER = os.path.join(TMP, "power")
THERMAL = os.path.join(TMP, "thermal")
for d in (PROC, POWER, THERMAL):
    os.makedirs(d, exist_ok=True)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


write(os.path.join(PROC, "meminfo"), "MemTotal: 8000000 kB\nMemAvailable: 2000000 kB\n")
write(os.path.join(PROC, "uptime"), "5000.0 100.0\n")
write(os.path.join(POWER, "capacity"), "97\n")
write(os.path.join(POWER, "temp"), "251\n")
write(os.path.join(LOGS, "watch.log"), "")
write(os.path.join(LOGS, "server.log"), "INFO: started\n")
core = sqlite3.connect(os.path.join(RIVIA, "core.db"))
core.executescript("create table calls(id integer primary key, ts text, door text, tool text, action text, "
                   "ok integer, code text, ms integer);")
nowiso = datetime.now(TZ).replace(microsecond=0).isoformat()
core.executemany("insert into calls(ts, door, tool, action, ok, code, ms) values(?,?,?,?,?,?,?)",
                 [(nowiso, "app", "hub_status", "x", 1, None, 40), (nowiso, "app", "app_places", "x", 0, "internal", 90)])
core.commit()
core.close()

ALLOWED_URLS = {body.LOCAL_HEALTH, body.PUBLIC_HEALTH} | {
    "http://127.0.0.1:%d/metrics" % p for p in body.CLOUDFLARED_PORTS}
seen_urls = []
public_up = {"v": True}


def fake_get(url, timeout):
    seen_urls.append(url)
    if url == body.PUBLIC_HEALTH:
        return (200 if public_up["v"] else 530), 30, b"ok"
    if url == body.LOCAL_HEALTH:
        return 200, 2, b"ok"
    if url.endswith(":20241/metrics"):
        return 200, 1, b"cloudflared_tunnel_ha_connections 4\n"
    return None, 1, b""


def fake_tablet_read(cursor=None, window_s=3600):
    return body.read_all(cursor=cursor, window_s=window_s, rivia_dir=RIVIA, logs=LOGS, proc=PROC, power=POWER,
                         thermal=THERMAL, get=fake_get)


# ---------- an audit of every file, database, process and socket the collector reaches ----------
audit = {"open": [], "sqlite": [], "proc": [], "socket": []}
auditing = threading.Event()


def hook(event, args):
    if not auditing.is_set():
        return
    if event == "open":
        path, mode = args[0], args[1]
        if isinstance(path, (str, bytes)):
            audit["open"].append((os.fsdecode(path), mode, args[2] if len(args) > 2 else None))
    elif event == "sqlite3.connect":
        audit["sqlite"].append(os.fsdecode(args[0]) if isinstance(args[0], (str, bytes)) else str(args[0]))
    elif event in ("subprocess.Popen", "os.system", "os.kill", "os.exec", "os.posix_spawn", "os.spawn"):
        audit["proc"].append(event)
    elif event == "socket.connect":
        audit["socket"].append(args[1])


sys.addaudithook(hook)

print("collector: one tick on the fake tablet")
c = bc.db_open()
auditing.set()
t_first = int(time.time()) - 600  # ticks are stamped in the past, so 4.3 never sees one ahead of its clock
r1 = bc.collect_once(con=c, now=t_first, read=fake_tablet_read)
ok(r1["ok"] and r1["error"] is None, "a tick on a working tablet is ok")
dial = {k for k in r1["signals"] if k not in bc.NOT_A_DIAL}
stored = {row[0] for row in c.execute("select distinct signal from samples")}
expect = {k for k, v in r1["signals"].items() if v is not None and k not in bc.NOT_A_DIAL}
ok(stored == expect, "every signal with a reading went into 4.3, and only those (%d)" % len(stored))
ok("uptime_s" not in stored, "uptime_s is logged but not fed to 4.3 (4.1 marks it shown only)")
ok("error_rate" not in stored and r1["signals"]["error_rate"] is None,
   "the first tick counts no errors (no cursor yet), so nothing is stored for error_rate")
ok(r1["signals"]["tunnel_unreachable"] == 0.0, "tunnel up reads 0 (percent, 4.1's unit)")
ok(r1["signals"]["battery"] == 97 and r1["signals"]["temperature"] == 25.1, "battery 97 %, temperature 25.1 C")
ok(r1["signals"]["tool_failure_rate"] == 50.0, "one body failure in two calls reads 50 (percent)")
row = c.execute("select ok, stored, signals, extras from reads where at=?", (t_first,)).fetchone()
ok(row is not None and row[0] == 1 and row[1] == len(stored), "the tick is in the reads log with its stored count")
ok(json.loads(row[3]).get("edge_connections") == 4, "the extras (edge connections) are in the reads log")
ok(bc.store.get_meta(c, bc.CURSOR_KEY) is not None, "the server.log cursor is saved in body.db")

print("collector: the second tick counts new errors and a tunnel outage")
auditing.clear()  # the test's own write to server.log is not the collector's
with open(os.path.join(LOGS, "server.log"), "a") as f:
    f.write("ERROR: Exception in ASGI application\nrivia: daily Drive backup failed (URLError); continuing.\n")
auditing.set()
public_up["v"] = False
time.sleep(1.1)
r2 = bc.collect_once(con=c, now=t_first + 60, read=fake_tablet_read)
ok(r2["ok"] and r2["signals"]["error_rate"] and r2["signals"]["error_rate"] > 0, "new server.log errors are counted")
ok(r2["signals"]["failed_connections"] and r2["signals"]["failed_connections"] > 0,
   "a failed backup connection in server.log is counted")
ok(r2["signals"]["tunnel_unreachable"] == 100.0, "public check down, server up: tunnel reads 100 (percent)")
n = normal.normals(con=c)
ok(abs(n["tunnel_unreachable"]["recent"] - 50.0) < 1e-6,
   "4.3's 15-minute average of one up and one down tick is 50 (percent), so 4.4 and 4.5 get percent")
public_up["v"] = True

print("collector: the same second twice is not double counted")
before = c.execute("select count(*) from samples").fetchone()[0]
r3 = bc.collect_once(con=c, now=t_first + 60, read=fake_tablet_read)
after = c.execute("select count(*) from samples").fetchone()[0]
ok(r3["ok"] and r3["stored"] == 0 and after == before, "a replayed tick in the same second stores nothing new")
auditing.clear()

print("sign-in safety: what that ran touched")
opened = [p for p, _, _ in audit["open"]]
ok(not [p for p in opened if os.path.realpath(p).startswith(os.path.realpath(DOT))],
   "no file under ~/.rivia was opened (Google token, admin token, .env)")
ok(not [p for p in audit["sqlite"] if "auth.db" in p], "auth.db (the app and admin tokens) was never opened")
writes = [p for p, mode, flags in audit["open"]
          if (isinstance(mode, str) and any(ch in mode for ch in "wax+"))
          or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND))]
bad_writes = [p for p in writes if not os.path.basename(p).startswith("body.db")]
ok(not bad_writes, "the only file written is body.db (and its journal): %s" % (sorted(set(bad_writes)) or "none other"))
core_opens = [p for p in audit["sqlite"] if p.endswith("core.db") or "core.db?" in p]
ok(core_opens and all("mode=ro" in p for p in core_opens), "core.db is only ever opened read-only")
ok(not audit["proc"], "no process was started or signalled (no restart of the server or cloudflared)")
ok(set(seen_urls) <= ALLOWED_URLS, "it only asked the local health, the public health and cloudflared's metrics")
ok(not [u for u in seen_urls if "google" in u or "/app/login" in u or "/app/" in u or "/admin/" in u],
   "it never called Google, the app login route, or either door")
ok(fingerprint() == BEFORE, "the Google token, admin token, .env and auth.db are byte for byte unchanged")

print("sign-in safety: the real network code goes nowhere else")
asked = []


def recording_urlopen(req, timeout=None, **kw):
    asked.append(req if isinstance(req, str) else req.full_url)
    raise urllib.error.URLError("offline in the test")


real_urlopen = urllib.request.urlopen
urllib.request.urlopen = recording_urlopen
try:
    body.read_all(rivia_dir=RIVIA, logs=LOGS, proc=PROC, power=POWER, thermal=THERMAL)
finally:
    urllib.request.urlopen = real_urlopen
ok(asked and set(asked) <= ALLOWED_URLS, "4.1's own HTTP code only asks those addresses (%d asked)" % len(asked))

print("sign-in safety: what it imports")
FORBIDDEN = {"rivia_google", "rivia_gcal", "rivia_auth", "rivia_applogin", "rivia_tools", "rivia_admin",
             "rivia_sched", "rivia_server", "rivia_emotion_app"}
# 4.9 merge: rivia_emotion is now loaded, for its pure label() and the 4.6 cap maths used by the shadow dials
# (rivia_body_shadow). It is never written: the audit above checks that body.db is the only file a tick writes,
# with the shadow dials running. The check below makes sure the shadow module is the only way it comes in.
probe = subprocess.run([sys.executable, "-I", "-c",
                        "import sys; sys.path.insert(0, %r); import rivia_body_collect; "
                        "print(' '.join(sorted(m for m in sys.modules if m.startswith('rivia'))))" % HERE],
                       capture_output=True, text=True, cwd=TMP, env=dict(os.environ))
loaded = set(probe.stdout.split())
ok(probe.returncode == 0 and loaded, "rivia_body_collect imports on its own (%s)" % (" ".join(sorted(loaded)) or probe.stderr[-200:]))
ok(not (loaded & FORBIDDEN), "loading it pulls in none of the Google, sign-in, tool or emotion-button modules")
src_c = open(os.path.join(HERE, "rivia_body_collect.py")).read()
ok("rivia_emotion" not in src_c, "the collector itself never names rivia_emotion; only the shadow dials load it")


def static_imports(path):
    tree = ast.parse(open(path).read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


for mod in ("rivia_body_collect", "rivia_body_signals", "rivia_normal"):
    ok(not (static_imports(os.path.join(HERE, mod + ".py")) & FORBIDDEN),
       "%s.py has no import of a sign-in module anywhere, even inside a function" % mod)

print("collector: a bad tick never raises")


def broken_read(cursor=None, window_s=3600):
    raise OSError("disk went away")


r = bc.collect_once(con=c, now=t_first + 120, read=broken_read)
ok(r["ok"] is False and r["error"] == "OSError", "a reader that blows up gives ok False, not an exception")
ok(c.execute("select ok, error from reads where at=?", (t_first + 120,)).fetchone() == (0, "OSError"),
   "the failed tick is in the reads log too")


def nan_read(cursor=None, window_s=3600):
    return {"battery": float("nan"), "memory_used": 40.0, "uptime_s": 10.0}, {}, None


r = bc.collect_once(con=c, now=t_first + 180, read=nan_read)
ok(r["ok"] and r["stored"] == 1 and r["rejected"] == [["battery", "bad_value"]],
   "a junk value is refused by 4.3 and named in the log; the good one is stored")
os.environ["RIVIA_DIR"] = os.path.join(TMP, "not-a-dir-file")
write(os.environ["RIVIA_DIR"], "x")
_saved = bc.store.RIVIA_DIR
bc.store.RIVIA_DIR = os.path.join(os.environ["RIVIA_DIR"], "inside")
r = bc.collect_once(now=t_first + 240, read=nan_read)
ok(r["ok"] is False and r["error"].startswith("db_open"), "body.db that can't be opened gives ok False, no exception")
bc.store.RIVIA_DIR = _saved
os.environ["RIVIA_DIR"] = RIVIA

print("collector: the cursor")
bc.store.set_meta(c, bc.CURSOR_KEY, json.dumps({"inode": 1, "offset": 5, "at": t_first}))
ok(bc._load_cursor(c, t_first + 60) is not None, "a cursor from a minute ago is kept")
ok(bc._load_cursor(c, t_first + bc.CURSOR_MAX_AGE_S + 1) is None,
   "a cursor older than 15 minutes is dropped, so a long gap isn't one diluted error rate")
bc.store.set_meta(c, bc.CURSOR_KEY, "{not json")
ok(bc._load_cursor(c, t_first) is None, "a broken cursor is dropped, not an error")

print("collector: the shadow log is pruned")
c.execute("insert or replace into reads values(?,?,?,?,?,?,?,?)", (t_first - bc.READS_KEEP_S - 10, 1, 1, None, "{}", "{}", 0, "[]"))
bc.collect_once(con=c, now=t_first + 300, read=nan_read)
ok(c.execute("select count(*) from reads where at < ?", (t_first - bc.READS_KEEP_S,)).fetchone()[0] == 0,
   "reads older than 2 days are deleted on the next tick")
ok(bc.recent_reads(2, con=c)[0]["at"] == t_first + 300, "recent_reads gives the newest first")
c.close()

print("switches")
ok(bc.enabled(), "on by default (your yes, Nov 22)")
for v in ("0", "off", "false", "no", " OFF "):
    os.environ["RIVIA_BODY_COLLECTOR"] = v
    ok(not bc.enabled(), "RIVIA_BODY_COLLECTOR=%r turns it off" % v)
os.environ.pop("RIVIA_BODY_COLLECTOR")
ok(bc.interval_s() == 60, "one reading a minute by default")
os.environ["RIVIA_BODY_INTERVAL_S"] = "5"
ok(bc.interval_s() == 30, "an interval under 30 s is raised to 30")
os.environ["RIVIA_BODY_INTERVAL_S"] = "abc"
ok(bc.interval_s() == 60, "a junk interval falls back to 60")
os.environ.pop("RIVIA_BODY_INTERVAL_S")


# ---------- the timer loop ----------
def run(coro, seconds=5):
    return asyncio.run(asyncio.wait_for(coro, seconds))


print("loop: ticks, failures, a hung tick, and the event loop stays free")
logs = []
ticks = []


async def scenario_basic():
    calls = {"n": 0}

    def tick():
        calls["n"] += 1
        ticks.append(calls["n"])
        if calls["n"] == 2:
            raise RuntimeError("boom")
        if calls["n"] == 3:
            return {"ok": False, "error": "OSError"}
        return {"ok": True}

    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) > 5:
            raise asyncio.CancelledError
        await asyncio.sleep(0)

    try:
        await bc.loop(sleep=fake_sleep, tick=tick, every=60, first_delay=30, log=logs.append)
    except asyncio.CancelledError:
        pass
    return sleeps


try:
    sl = run(scenario_basic())
except asyncio.CancelledError:
    sl = None
ok(ticks[:4] == [1, 2, 3, 4], "it keeps ticking after a tick raises and after a failed tick")
ok(any("RuntimeError" in m for m in logs) and any("OSError" in m for m in logs),
   "both failures are one line each in server.log")
ok(sl is not None and sl[0] == 30, "the first tick waits 30 s after start")

logs.clear()


async def scenario_hung():
    gate = threading.Event()
    started = []

    def slow_tick():
        started.append(1)
        gate.wait(2)
        return {"ok": True}

    heartbeat = {"n": 0}

    async def beat():
        while True:
            heartbeat["n"] += 1
            await asyncio.sleep(0.01)

    hb = asyncio.create_task(beat())
    n = {"s": 0}

    async def fake_sleep(s):
        n["s"] += 1
        if n["s"] > 3:
            raise asyncio.CancelledError
        await asyncio.sleep(0.05)

    try:
        await bc.loop(sleep=fake_sleep, tick=slow_tick, every=60, first_delay=0, timeout=0.2, log=logs.append)
    except asyncio.CancelledError:
        pass
    beats = heartbeat["n"]
    hb.cancel()
    gate.set()
    return len(started), beats


try:
    started_n, beats = run(scenario_hung())
except asyncio.CancelledError:
    started_n, beats = -1, 0
ok(started_n == 1, "a hung tick is not stacked: only one worker started while it was stuck")
ok(any("took over" in m for m in logs) and any("skipped" in m for m in logs),
   "the hung tick is logged as over time, and the next one as skipped")
ok(beats > 20, "the server's event loop kept running while the tick was stuck (%d heartbeats)" % beats)


async def scenario_cancel():
    async def fake_sleep(s):
        await asyncio.sleep(10)
    t = asyncio.create_task(bc.loop(sleep=fake_sleep, tick=lambda: {"ok": True}, log=logs.append))
    await asyncio.sleep(0.05)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        return True
    return False


ok(run(scenario_cancel()), "shutdown cancels it cleanly")


ok(fingerprint() == BEFORE, "after every collector check: the Google token, admin token, .env and auth.db are unchanged")

# ---------- the server: its own task, beside the sign-in check, never inside it ----------
# (The server's own start-up writes auth.db, as it always has, so the fingerprint isn't checked past here.)
print("server: wiring")
try:
    import rivia_server as rs  # needs fastmcp (the tablet's venv)
except Exception as e:  # noqa: BLE001
    rs = None
    print("  (skipped: rivia_server would not import here: %s)" % type(e).__name__)

if rs is not None:
    async def started_task():
        t = rs.start_body_collector()
        name = t.get_name() if t else None
        if t:
            t.cancel()
        return name

    ok(run(started_task()) == "rivia-body-collector", "start_body_collector starts a task named rivia-body-collector")
    # The switches (Nov 24, BUILD.md change 1): the minute loop is split from Layer 4. RIVIA_BODY_COLLECTOR=0 stops
    # only Layer 4's reads and the dials; the loop keeps ticking for Steps 5 and 6. With all three off, nothing starts.
    os.environ["RIVIA_BODY_COLLECTOR"] = "0"
    ok(run(started_task()) == "rivia-body-collector", "RIVIA_BODY_COLLECTOR=0 still starts the loop for Steps 5 and 6")
    os.environ.update(RIVIA_PLAN_SHADOW="0", RIVIA_TRUST_SHADOW="0")
    ok(run(started_task()) is None, "RIVIA_BODY_COLLECTOR=0 with Steps 5 and 6 off by env starts nothing")
    os.environ.pop("RIVIA_PLAN_SHADOW")
    os.environ.pop("RIVIA_TRUST_SHADOW")
    os.environ.pop("RIVIA_BODY_COLLECTOR")
    sys.modules["rivia_body_collect"] = None  # makes the import inside start_body_collector fail
    ok(run(started_task()) is None, "a collector that won't import starts nothing, and the server carries on")
    sys.modules["rivia_body_collect"] = bc

    print("server: the sign-in check keeps running when the collector dies")
    import rivia_gcal as gcal  # imported only by this test, to stand in for its loop
    beat = {"n": 0}

    async def fake_signin_loop(**kw):
        while True:
            beat["n"] += 1
            await asyncio.sleep(0.01)

    async def dying_collector(**kw):
        await asyncio.sleep(0.02)
        raise RuntimeError("collector bug")

    real_signin, real_loop = gcal.signin_check_loop, bc.loop
    gcal.signin_check_loop, bc.loop = fake_signin_loop, dying_collector
    os.environ["RIVIA_SIGNIN_CHECK"] = "1"

    async def life():
        app = rs.public_app()
        inner = app
        while not hasattr(inner, "router"):  # under the CORS and bearer wrappers
            inner = inner.app
        async with inner.router.lifespan_context(inner):
            await asyncio.sleep(0.15)
            names = {t.get_name(): t for t in asyncio.all_tasks()}
            sign = names.get("rivia-signin-check")
            coll = names.get("rivia-body-collector")
            return sign is not None and not sign.done(), coll, beat["n"], sign

    try:
        alive, coll, beats, sign = run(life(), 20)
        ok(alive, "with the collector crashed, the sign-in check task is still running")
        ok(coll is None or coll.done(), "the collector's own task is the one that ended")
        ok(beats > 5, "the sign-in check kept its pace (%d beats in 0.15 s)" % beats)
        ok(sign.done(), "and shutdown still stops the sign-in check cleanly after a crashed collector")
    except Exception as e:  # noqa: BLE001
        inner_e = e.exceptions[0] if isinstance(e, BaseExceptionGroup) else e
        ok(False, "server lifespan ran (%s: %s)" % (type(inner_e).__name__, inner_e))
    finally:
        gcal.signin_check_loop, bc.loop = real_signin, real_loop


print("\n%d checks, %d failed" % (checks, failed))
sys.exit(1 if failed else 0)
