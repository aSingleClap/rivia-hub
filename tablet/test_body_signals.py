# test_body_signals.py: checks for rivia_body_signals.py (Step 4.1 draft). Runs on a fake tablet in a temp
# folder: fake /proc, /sys battery and thermal files, logs and a core.db with the real calls table. No network:
# the HTTP checks get a fake getter. Run: python3 test_body_signals.py
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rivia_body_signals as b  # noqa: E402

FAILS = []
COUNT = [0]


def check(name, cond):
    COUNT[0] += 1
    if not cond:
        FAILS.append(name)
        print("FAIL", name)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


# The calls table exactly as rivia_tools.SCHEMA makes it (tablet/rivia_tools.py at aa64af2).
CALLS = """create table if not exists calls(
    ts text not null, door text not null, tool text not null,
    action text not null, ok integer not null, code text, ms integer not null);"""

root = tempfile.mkdtemp(prefix="body41_")
rivia = os.path.join(root, "rivia")
logs = os.path.join(rivia, "logs")
proc = os.path.join(root, "proc")
power = os.path.join(root, "power")
thermal = os.path.join(root, "thermal")
now = datetime(2026, 11, 22, 12, 0, 0, tzinfo=b.TZ)

# ---- uptime ----
hz = os.sysconf("SC_CLK_TCK")
write(os.path.join(rivia, "server.pid"), "4242\n")
fields = ["S"] + ["0"] * 18 + [str(1000 * hz)] + ["0"] * 10   # starttime (field 22) = 1000 s after boot
write(os.path.join(proc, "4242", "stat"), "4242 (python rivia_s) " + " ".join(fields))
write(os.path.join(proc, "uptime"), "4600.50 9000.00")
check("uptime is boot uptime minus server start", b.uptime_s(rivia, proc) == 3600.5)
write(os.path.join(rivia, "server.pid"), "999\n")
check("uptime None when the pid has no /proc entry", b.uptime_s(rivia, proc) is None)
write(os.path.join(rivia, "server.pid"), "4242\n")
check("uptime None with no pid file", b.uptime_s(os.path.join(root, "nowhere"), proc) is None)

# /proc/uptime unreadable (Android refuses it to apps): fall back to the boot clock. A fake proc with the
# server's stat (fake start time, 1000 s after boot) but no uptime file, and a fake boot clock.
proc_nb = os.path.join(root, "proc_noboot")
write(os.path.join(proc_nb, "4242", "stat"), "4242 (python rivia_s) " + " ".join(fields))
check("no /proc/uptime: uptime from the boot clock", b.uptime_s(rivia, proc_nb, boot_clock=lambda: 4600.5) == 3600.5)
write(os.path.join(proc_nb, "uptime"), "garbage")
check("garbled /proc/uptime: boot clock used", b.uptime_s(rivia, proc_nb, boot_clock=lambda: 1300.0) == 300.0)
os.remove(os.path.join(proc_nb, "uptime"))
check("no /proc/uptime and no boot clock: None", b.uptime_s(rivia, proc_nb, boot_clock=lambda: None) is None)


def _no_clock():
    raise OSError("no clock")


check("boot clock that fails: None, no crash", b.uptime_s(rivia, proc_nb, boot_clock=_no_clock) is None)
check("/proc/uptime still wins when readable", b.uptime_s(rivia, proc, boot_clock=lambda: 99999.0) == 3600.5)
write(os.path.join(proc_nb, "4242", "stat"), "4242 (python rivia_s) S 0 0")
check("short stat (no start time): None, no crash", b.uptime_s(rivia, proc_nb, boot_clock=lambda: 4600.5) is None)
r = b.uptime_s(rivia, os.path.join(root, "proc_real_clock"))
check("no stat for the pid: None even with the real boot clock", r is None)

# ---- restarts (watch.log, as rivia.sh's watch_loop writes it) ----
write(os.path.join(logs, "watch.log"), "\n".join([
    "2026-11-22 09:00:00 server or tunnel down; restarting",               # 3 h ago: outside a 1 h window
    "2026-11-22 11:30:00 local /health failed 3 times; restarting server",
    "watchdog: started (checks every 60s)",                                 # start output, not a restart
    "2026-11-22 11:45:00 public /health failed 3 times; restarting tunnel",
    "2026-11-22 12:30:00 server or tunnel down; restarting",               # in the future: ignored
]))
check("restarts counts the last hour only", b.restarts(3600, now, logs) == 2.0)
check("restarts is a per-hour rate", b.restarts(4 * 3600, now, logs) == 0.75)
write(os.path.join(logs, "watch.log"), "\n".join([
    "2026-11-22 11:20:00 planned restart (restart_server from the hub)",
    "2026-11-22 11:20:55 server or tunnel down; restarting",               # the planned one: skipped
    "2026-11-22 11:40:00 server or tunnel down; restarting",               # a real crash: counted
    "2026-11-22 11:50:00 planned restart (restart_server from the hub)",
    "2026-11-22 11:56:00 server or tunnel down; restarting",               # 6 min later: too late, counted
]))
check("a hub restart is left out, crashes still count", b.restarts(3600, now, logs) == 2.0)
write(os.path.join(logs, "watch.log"), "\n".join([
    "2026-11-22 11:20:00 planned restart (restart_server from the hub)",
    "2026-11-22 11:20:30 server or tunnel down; restarting",               # planned: skipped
    "2026-11-22 11:21:30 server or tunnel down; restarting",               # a second one: counted
]))
check("one planned line covers one restart only", b.restarts(3600, now, logs) == 1.0)
check("the planned line never reads as a restart", not b.RESTART_LINE.match(
    "2026-11-22 11:20:00 planned restart (restart_server from the hub)"))
check("the default window is 15 minutes (Jordan, Nov 22)", b.WINDOW_S == 900)
check("restarts uses the 15-minute window by default", b.restarts(None, now, logs) == b.restarts(900, now, logs))
check("restarts None with no watch.log", b.restarts(3600, now, os.path.join(root, "nologs")) is None)

# ---- server.log: cursor, errors, connections ----
slog = os.path.join(logs, "server.log")
write(slog, "INFO:     Started server process [4242]\n")
lines, cur, el = b.log_delta(None, logs)
check("first read counts nothing and starts at the end", lines == [] and el is None and cur["offset"] > 0)
check("error_rate None on a first read", b.error_rate(lines, el) is None)
with open(slog, "a") as f:
    f.write('INFO:     1.2.3.4:0 - "POST /app/mcp HTTP/1.1" 200 OK\n')
    f.write('INFO:     1.2.3.4:0 - "POST /app/mcp HTTP/1.1" 500 Internal Server Error\n')
    f.write("ERROR:    Exception in ASGI application\n")
    f.write("Traceback (most recent call last):\n")
    f.write("rivia: daily Drive backup failed (GcalError); continuing.\n")
    f.write('INFO:     5.6.7.8:0 - "POST /app/mcp HTTP/1.1" 401 Unauthorized\n')
    f.write("rivia: emotion button did not load (ImportError); everything else keeps running.\n")
cur["at"] -= 1800  # pretend the last read was half an hour ago
lines, cur2, el = b.log_delta(cur, logs)
check("second read sees only the new lines", len(lines) == 7)
check("elapsed time comes from the cursor", 1799 <= el <= 1801)
er = b.error_rate(lines, el)
check("500, ERROR:, Traceback and 'failed' count; 200 and 401 don't (4 in 30 min = 8/h)", abs(er - 8.0) < 0.05)
fc = b.failed_connections(1, lines, el)
check("one failed Google call + one backup GcalError line in 30 min = 4/h", abs(fc - 4.0) < 0.05)
check("failed_connections None on a first read", b.failed_connections(3, [], None) is None)
lines3, _, _ = b.log_delta(cur2, logs)
check("nothing new, nothing counted", lines3 == [])
write(slog, "short\n")  # log replaced (rotated or cleared): shorter than the cursor
lines4, _, _ = b.log_delta(cur2, logs)
check("a replaced log is read from the top", lines4 == ["short"])
check("planner selftest crash counts", b.ERROR_LINE.search("rivia: planner selftest crashed (KeyError)."))
check("'crashed' outside a rivia: line doesn't", not b.ERROR_LINE.search("user said the app crashed"))

# ---- calls table ----
con = sqlite3.connect(os.path.join(rivia, "core.db"))
con.executescript(CALLS)


def call(mins_ago, ok, code, ms):
    ts = (now - timedelta(minutes=mins_ago)).replace(microsecond=0).isoformat()
    con.execute("insert into calls values(?,?,?,?,?,?,?)", (ts, "app", "t", "a", 1 if ok else 0, code, ms))


call(5, True, None, 100)
call(10, True, None, 300)
call(15, False, "bad_day", 50)          # Jordan's bad input: not her failure
call(20, False, "calendar_http", 2000)  # Google failed: hers, and a failed connection
call(30, False, "internal", 400)        # a tool crashed: hers
call(90, False, "internal", 9999)       # outside the window
con.commit()
con.close()
tc = b.tool_calls(3600, now, rivia)
check("five calls in the window", tc["calls"] == 5)
check("failure rate counts body codes only (2 of 5 = 40%)", tc["tool_failure_rate"] == 40.0)
check("response time is the median ms", tc["response_time"] == 300.0)
check("one connection failure (calendar_http)", tc["connection_failures"] == 1)
empty = b.tool_calls(60, now - timedelta(days=1), rivia)
check("no calls in the window: None, not 0", empty["tool_failure_rate"] is None and empty["response_time"] is None)
check("no core.db: None", b.tool_calls(3600, now, os.path.join(root, "nowhere"))["tool_failure_rate"] is None)
check("every body code is a real code in tablet/ (checked list)", b.CONNECTION_CODES <= b.BODY_CODES)
con = sqlite3.connect(os.path.join(rivia, "core.db"))
call(2, False, "unreadable", 5)         # a fixed tool couldn't read its source: hers
call(3, False, "http_400", 4)           # a refused button press: not hers
con.commit()
con.close()
tc = b.tool_calls(3600, now, rivia)
check("unreadable counts as hers, a refused press doesn't (3 of 7)", tc["tool_failure_rate"] == round(300 / 7, 2))

# ---- tunnel ----
write(os.path.join(rivia, "tunnel.pid"), "777\n")
write(os.path.join(proc, "777", "cmdline"), "cloudflared\0tunnel\0run\0")
METRICS = b"# HELP x\ncloudflared_tunnel_ha_connections 4\ncloudflared_tunnel_total_requests 812\n"


def getter(local=200, public=200, metrics_port=20241):
    def get(url, timeout):
        if url == b.LOCAL_HEALTH:
            return local, 3, b'{"ok":true}'
        if url == b.PUBLIC_HEALTH:
            return public, 180, b""
        if url == "http://127.0.0.1:%d/metrics" % metrics_port:
            return 200, 2, METRICS
        return None, 1, b""
    return get


t = b.tunnel(rivia_dir=rivia, proc=proc, get=getter())
check("all fine: tunnel_unreachable 0", t["tunnel_unreachable"] == 0.0)
check("edge connections read from metrics", t["edge_connections"] == 4.0)
check("cloudflared seen running", t["cloudflared_running"] is True)
t = b.tunnel(rivia_dir=rivia, proc=proc, get=getter(public=None))
check("public fails, local fine: tunnel_unreachable 100", t["tunnel_unreachable"] == 100.0)
t = b.tunnel(rivia_dir=rivia, proc=proc, get=getter(public=502))
check("Cloudflare 502 counts as unreachable", t["tunnel_unreachable"] == 100.0)
t = b.tunnel(rivia_dir=rivia, proc=proc, get=getter(local=None, public=None))
check("server down locally: None (not the tunnel's fault)", t["tunnel_unreachable"] is None)
t = b.tunnel(rivia_dir=rivia, proc=proc, get=getter(metrics_port=20244))
check("metrics found on a later default port", t["edge_connections"] == 4.0)
t = b.tunnel(rivia_dir=rivia, proc=proc, get=getter(metrics_port=1))
check("no metrics server: edge None", t["edge_connections"] is None)
write(os.path.join(proc, "777", "cmdline"), "python\0something\0")
check("a reused pid isn't cloudflared", b.tunnel(rivia_dir=rivia, proc=proc, get=getter())["cloudflared_running"] is False)

# The real read path against a real (local) metrics page as big as cloudflared's: the line sits past the
# first 4 KB, where a 4 KB read used to cut it off and leave edge_connections None.
import http.server, threading  # noqa: E402,E401

BIG_METRICS = (b"# HELP filler x\n" + b"".join(b"go_gc_duration_seconds{quantile=\"0.%03d\"} 0.0001\n" % i
                                            for i in range(400))
               + b"cloudflared_tunnel_ha_connections 4\ncloudflared_tunnel_total_requests 812\n")
PAGES = {"/metrics": BIG_METRICS, "/nometrics": b"# HELP x\nsomething_else 1\n" * 300,
         "/garbled": b"x" * 5000 + b"\ncloudflared_tunnel_ha_connections lots\n", "/health": b'{"ok":true}'}


class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.end_headers()
        self.wfile.write(body or b"")

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % srv.server_address[1]
check("fake page puts the line past 4 KB", BIG_METRICS.index(b"cloudflared_tunnel_ha_connections") > 4096)
check("a 4 KB read misses it (the old bug)", b"cloudflared_tunnel_ha_connections" not in b._get(base + "/metrics", 2)[2])
check("edge read from a big real page", b._edge_connections(2, metrics_url=base + "/metrics") == 4.0)
t = b.tunnel(rivia_dir=rivia, proc=proc, public_url=base + "/health", local_url=base + "/health",
             metrics_url=base + "/metrics")
check("tunnel() reads edge 4 from the big page", t["edge_connections"] == 4.0 and t["tunnel_unreachable"] == 0.0)
check("page without the line: edge None", b._edge_connections(2, metrics_url=base + "/nometrics") is None)
check("garbled value: edge None, no crash", b._edge_connections(2, metrics_url=base + "/garbled") is None)
check("metrics page 404: edge None", b._edge_connections(2, metrics_url=base + "/missing") is None)
srv.shutdown()
srv.server_close()
check("metrics server gone: edge None, no crash", b._edge_connections(1, metrics_url=base + "/metrics") is None)

# ---- vitals ----
write(os.path.join(power, "temp"), "312\n")
write(os.path.join(power, "capacity"), "87\n")
write(os.path.join(thermal, "thermal_zone0", "temp"), "41500\n")
write(os.path.join(thermal, "thermal_zone1", "temp"), "-273000\n")  # broken sensor
os.makedirs(os.path.join(thermal, "thermal_zone2"))              # refused/missing temp file
tp = b.temperature(power, thermal)
check("battery temp is tenths of a degree", tp["temperature"] == 31.2)
check("hottest zone in millidegrees, junk and unreadable zones skipped", tp["hottest_zone_c"] == 41.5)
check("no thermal folder is fine", b.temperature(power, os.path.join(root, "x"))["hottest_zone_c"] is None)
check("no battery temp: None", b.temperature(os.path.join(root, "x"), thermal)["temperature"] is None)
write(os.path.join(proc, "meminfo"), "MemTotal:  8000000 kB\nMemFree: 100 kB\nMemAvailable:  2000000 kB\n")
check("memory used is 1 - available/total", b.memory_used(proc) == 75.0)
write(os.path.join(proc, "meminfo"), "MemTotal:  8000000 kB\n")
check("no MemAvailable: None", b.memory_used(proc) is None)
sf = b.storage_free(rivia)
check("storage free is a percent", sf is not None and 0 <= sf <= 100)
check("storage of a missing path: None", b.storage_free(os.path.join(root, "nowhere")) is None)
check("battery percent", b.battery(power) == 87.0)
write(os.path.join(power, "capacity"), "250\n")
check("impossible battery: None", b.battery(power) is None)
write(os.path.join(power, "capacity"), "87\n")
write(os.path.join(proc, "meminfo"), "MemTotal:  8000000 kB\nMemAvailable:  2000000 kB\n")

# ---- read_all, and it writes nothing ----


def snapshot():
    out = {}
    for d, _, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            st = os.stat(p)
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


before = snapshot()
sig, extra, cur = b.read_all(None, 3600, now, rivia, logs, proc, power, thermal, get=getter())
after = snapshot()
check("read_all changes no file on the tablet", before == after)
NAMES = {"uptime_s", "restarts", "error_rate", "tool_failure_rate", "response_time", "failed_connections",
         "tunnel_unreachable", "temperature", "memory_used", "storage_free", "battery"}
check("read_all returns exactly the 4.1 signals", set(sig) == NAMES)
check("parked signals are not read", "unanswered_question_rate" not in sig and "memory_sync_age" not in sig)
check("every value is a number or None",
      all(v is None or isinstance(v, (int, float)) for v in sig.values()))
check("names fit 4.3's signal-name rule", all(b.re.match(r"^[a-z][a-z0-9_.]{0,47}$", n) for n in NAMES))
check("first read_all: log signals None", sig["error_rate"] is None and sig["failed_connections"] is None)
check("extras carry the tunnel detail", extra["edge_connections"] == 4.0 and extra["public_ms"] == 180)

# Names 4.4 and 4.5 already expect (from their drafts, if they're next to this folder).
here = os.path.dirname(os.path.abspath(__file__))
for piece, mod in (("4.4", "rivia_stress"), ("4.5", "rivia_confidence")):
    path = os.path.join(here, "..", piece)
    if os.path.isdir(path):
        sys.path.insert(0, path)
        try:
            m = __import__(mod)
            expected = set(getattr(m, "SIGNALS", None) or getattr(m, "WEIGHTS", None) or {})
            if expected:
                missing = expected - NAMES - {"memory_sync_age"}
                check("%s's signal names all come from 4.1 (missing: %s)" % (piece, sorted(missing)), not missing)
        except Exception as e:  # noqa: BLE001
            print("note: couldn't import %s (%s); name check skipped" % (mod, type(e).__name__))

print("%d checks, %d failed" % (COUNT[0], len(FAILS)))
sys.exit(1 if FAILS else 0)
