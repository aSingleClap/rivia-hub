# rivia_body_signals.py: where each of Rivia's body signals is read on the tablet (emotion spec, Step 4.1).
#
# DRAFT (Nov 22, not installed). Step 4 "System Health as Her Body". This file only says how to read each
# signal once, right now, and returns plain numbers. It does not decide when to read (4.2, the collector), it
# keeps no history and learns no normal (4.3), it judges nothing as good or bad (4.4, 4.5), it is not a Gemini
# tool (4.8), and it never writes anything: no database, no log, no file, no setting. Nothing imports it yet.
#
# Every reader is read-only and never raises: a source it can't read gives None ("no reading"), never 0, so a
# missing reading is never mistaken for a good one. Every path has a parameter so the tests can point it at a
# fake tablet.
#
# Signal names match the ones 4.3, 4.4 and 4.5 already use. Units are picked so 4.3's default spread floor of
# 1.0 unit means something (1 %, 1 per hour, 1 ms, 1 degree C) rather than swallowing a 0..1 fraction whole.
#
#   signal               unit          read from (on the tablet)
#   uptime_s             seconds       /proc/<server.pid>/stat start time + /proc/uptime   (shown, not a dial input)
#   restarts             per hour      ~/rivia/logs/watch.log restart lines (the watchdog's own restarts,
#                                      minus ones the hub asked for)
#   error_rate           per hour      new lines in ~/rivia/logs/server.log since the last read (tracebacks, 5xx,
#                                      "rivia: ... failed")
#   tool_failure_rate    percent       core.db calls table, body codes only (not Jordan's bad input)
#   response_time        ms (median)   core.db calls table, ms column
#   failed_connections   per hour      core.db calls (calendar_http, drive_http) + server.log outbound failures
#   tunnel_unreachable   percent       GET https://mcp.rivia.observer/health from the tablet (100 = failed,
#                                      0 = fine; averaged over time by 4.3), plus cloudflared's own edge count
#   temperature          degrees C     /sys/class/power_supply/battery/temp (1/10 degree C); read fine on the
#                                      tablet Nov 22 (177 = 17.7 C), and so were the CPU thermal zones
#   memory_used          percent       /proc/meminfo: 100 * (1 - MemAvailable / MemTotal)
#   storage_free         percent       os.statvfs(~/rivia): free space on the /data partition
#   battery              percent       /sys/class/power_supply/battery/capacity (same file get_tablet_battery reads)
#
# Parked (no reader here):
#   unanswered_question_rate   needs Step 3's "question left unanswered" event. Parked until Step 3.
#   memory_sync_age            the spec's "time since last memory sync". There is no conversation memory on the
#                              tablet yet, so there is nothing to sync. See README for the open question.
import os
import re
import sqlite3
import statistics
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")
RIVIA_DIR = os.path.expanduser(os.environ.get("RIVIA_DIR", "~/rivia"))
LOGS = os.path.join(RIVIA_DIR, "logs")
POWER = "/sys/class/power_supply/battery"
THERMAL = "/sys/class/thermal"
PROC = "/proc"
PUBLIC_HEALTH = "https://mcp.rivia.observer/health"
LOCAL_HEALTH = "http://127.0.0.1:8787/health"
CLOUDFLARED_PORTS = range(20241, 20246)  # cloudflared's default metrics ports when --metrics isn't set

# Tool-call error codes that mean her body failed, not that the request was bad. Codes like bad_value, bad_day,
# place_exists or need_estimate are Jordan (or Gemini) asking for something invalid, and she answered correctly
# by refusing, so they are not counted. Taken from every fail(...) / GcalError(...) code in tablet/ at aa64af2.
BODY_CODES = frozenset({
    "internal",                # an exception inside a tool
    "selftest_failed",         # scheduling's planner selftest failed at start
    "calendar_http",           # a Google request failed (network, timeout, or 5xx after the one retry)
    "drive_http",              # a Drive upload failed
    "calendar_token",          # the saved Google token couldn't be read or refreshed
    "calendar_login_expired",  # Google sign-in expired
    "needs_relogin",           # Google sign-in is missing a scope
    "login_timeout",
    "unreadable",              # a fixed tool (e.g. get_tablet_battery) couldn't read its source (the server/ drafts)
})
# The subset of BODY_CODES that are a failed outbound connection.
CONNECTION_CODES = frozenset({"calendar_http", "drive_http"})

# server.log lines that count as one error. uvicorn writes its own errors as "ERROR:" and access lines as
# '... "POST /app/mcp HTTP/1.1" 500 Internal Server Error'; Rivia's own code prints "rivia: <what> failed".
ERROR_LINE = re.compile(r'^ERROR:|^Traceback \(most recent call last\)|" 5\d\d |^rivia: .*\b(failed|crashed)\b')
# server.log lines that are a failed outbound connection (Google, Drive, GitHub/changelog for drift).
CONNECTION_LINE = re.compile(r'(URLError|TimeoutError|ConnectionError|GcalError|timed out|drive_http|calendar_http)')
# watch.log lines the watchdog writes when it restarts the server or the tunnel (rivia.sh watch_loop).
RESTART_LINE = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) .*restarting')
# The line restart_server writes just before a restart from the hub (server/rivia_admin.diff). The
# watchdog's next restart line within PLANNED_GRACE_S is that planned restart, not a crash. The watchdog
# checks every 60 s, so 180 s covers a slow check plus the start.
PLANNED_LINE = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) planned restart')
PLANNED_GRACE_S = 180
# How far back "right now" looks for restarts, tool failures and response time. Jordan, Nov 22 (4.3 thread):
# "15 minutes", the same span as 4.3's average, so a problem shows within about 15 minutes, not an hour.
WINDOW_S = 900
LOOKUP_TOOL = "health_status"  # 4.8's read-only lookup; its rows are not her body (4.9 merge)


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _num(text, scale=1.0):
    try:
        return float(text) * scale
    except (TypeError, ValueError):
        return None


# ---------- the server process ----------

def uptime_s(rivia_dir=None, proc=PROC):
    """Seconds since rivia_server.py started, from its pid file and /proc. None if it isn't running."""
    pid = _read(os.path.join(rivia_dir or RIVIA_DIR, "server.pid"))
    stat = _read(os.path.join(proc, str(pid), "stat")) if pid and pid.isdigit() else None
    boot = _read(os.path.join(proc, "uptime"))
    if not stat or not boot:
        return None
    try:
        start_ticks = int(stat.rsplit(")", 1)[1].split()[19])  # field 22 (starttime), after "pid (comm)"
        since_boot = float(boot.split()[0])
    except (IndexError, ValueError):
        return None
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    return round(max(0.0, since_boot - start_ticks / hz), 1)


def restarts(window_s=None, now=None, logs=None):
    """Watchdog restarts (server or tunnel) in the last window_s, as a rate per hour. None if watch.log
    is missing. A restart from the hub's restart_server is left out: restart_server writes a "planned
    restart" line first (server/rivia_admin.diff), and the restart line after it is skipped."""
    window_s = window_s or WINDOW_S
    text = _read(os.path.join(logs or LOGS, "watch.log"))
    if text is None:
        return None
    now = now or datetime.now(TZ)
    since = now - timedelta(seconds=window_s)
    n = 0
    planned_at = None
    for line in text.splitlines():
        p = PLANNED_LINE.match(line)
        m = None if p else RESTART_LINE.match(line)
        if not (p or m):
            continue
        try:
            at = datetime.strptime((p or m).group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
        except ValueError:
            continue
        if p:
            planned_at = at
            continue
        if planned_at is not None and timedelta(0) <= at - planned_at <= timedelta(seconds=PLANNED_GRACE_S):
            planned_at = None  # this restart was the planned one: one planned line covers one restart
            continue
        if since < at <= now:
            n += 1
    return round(n * 3600.0 / window_s, 3)


# ---------- server.log: errors and failed outbound connections ----------

def log_delta(cursor=None, logs=None):
    """The server.log lines written since cursor, and the new cursor. server.log lines carry no timestamp,
    so the only way to window them is "since the last read". cursor is {"inode", "offset", "at"} or None
    (first read: start at the end, count nothing). A new inode or a shorter file means the log was replaced,
    so it reads from the top. Returns (lines, cursor, elapsed_s), elapsed_s None on a first read."""
    path = os.path.join(logs or LOGS, "server.log")
    now = time.time()
    try:
        st = os.stat(path)
    except OSError:
        return [], cursor, None
    if not cursor:
        return [], {"inode": st.st_ino, "offset": st.st_size, "at": now}, None
    start = cursor["offset"]
    if st.st_ino != cursor.get("inode") or st.st_size < start:
        start = 0
    try:
        with open(path, "rb") as f:
            f.seek(start)
            raw = f.read(2_000_000)  # at most 2 MB a read, so a runaway log can't stall the server
    except OSError:
        return [], cursor, None
    lines = raw.decode("utf-8", "replace").splitlines()
    return lines, {"inode": st.st_ino, "offset": start + len(raw), "at": now}, max(1.0, now - cursor["at"])


def error_rate(lines, elapsed_s):
    """Errors per hour in the server.log lines since the last read. None on a first read."""
    if elapsed_s is None:
        return None
    n = sum(1 for ln in lines if ERROR_LINE.search(ln))
    return round(n * 3600.0 / elapsed_s, 3)


# ---------- core.db calls table: tool failures, response time, failed connections ----------

def _calls(window_s, now, rivia_dir):
    """(ok, code, ms) rows from core.db's calls table in the window, opened read-only. None if unreadable."""
    path = os.path.join(rivia_dir or RIVIA_DIR, "core.db")
    if not os.path.exists(path):
        return None
    since = (now - timedelta(seconds=window_s)).replace(microsecond=0).isoformat()
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
        try:
            # 4.9 merge: her own health_status lookups (4.8) are left out, so looking at her numbers can't
            # pull her median response time down or dilute her tool failure rate.
            return list(con.execute("select ok, code, ms from calls where ts > ? and ts <= ? and tool != ?",
                                    (since, now.replace(microsecond=0).isoformat(), LOOKUP_TOOL)))
        finally:
            con.close()
    except sqlite3.Error:
        return None


def tool_calls(window_s=None, now=None, rivia_dir=None):
    """Tool failure rate (percent, body codes only), median response time (ms) and outbound connection
    failures (count) from the row tools' call log. Each is None when there were no calls in the window.
    Today only row tools are logged (places, scheduling). With the server/ drafts the app door's fixed
    tools (get_tablet_battery, hub_status, list_tools, list_calls) and the emotion button (app_emotion) are
    logged too. A refused button press logs http_4xx, which isn't a body code, so it doesn't count."""
    now = now or datetime.now(TZ)
    window_s = window_s or WINDOW_S
    rows = _calls(window_s, now, rivia_dir)
    out = {"calls": 0, "tool_failure_rate": None, "response_time": None, "connection_failures": None}
    if rows is None:
        return out
    out["calls"] = len(rows)
    if not rows:
        return out
    body = sum(1 for ok, code, _ in rows if not ok and (code or "internal") in BODY_CODES)
    out["tool_failure_rate"] = round(100.0 * body / len(rows), 2)
    out["response_time"] = float(statistics.median(ms for _, _, ms in rows))
    out["connection_failures"] = sum(1 for ok, code, _ in rows if not ok and code in CONNECTION_CODES)
    return out


def failed_connections(calls_failed, lines, elapsed_s):
    """Failed outbound connections per hour: failed Google/Drive tool calls plus timer-job connection
    failures in server.log (the 05:30 tick, the backup and the drift check run outside any tool). None on a
    first read. Inbound 401s are left out: the public URL gets scanned by strangers, which isn't her health."""
    if elapsed_s is None:
        return None
    from_log = sum(1 for ln in lines if CONNECTION_LINE.search(ln))
    return round(((calls_failed or 0) + from_log) * 3600.0 / elapsed_s, 3)


# ---------- the Cloudflare tunnel (replaces the spec's Tailscale check) ----------

def _get(url, timeout):
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read(4096)
            return r.status, int((time.perf_counter() - t0) * 1000), body
    except urllib.error.HTTPError as e:
        return e.code, int((time.perf_counter() - t0) * 1000), b""
    except Exception:  # noqa: BLE001
        return None, int((time.perf_counter() - t0) * 1000), b""


def _alive(pidfile, want, proc=PROC):
    pid = _read(pidfile)
    if not pid or not pid.isdigit():
        return False
    cmd = _read(os.path.join(proc, pid, "cmdline"))
    return bool(cmd) and want in cmd.replace("\0", " ")


def _edge_connections(timeout, metrics_url=None, get=None):
    """cloudflared_tunnel_ha_connections from cloudflared's metrics server (normally 4). Tries metrics_url,
    else the default ports 20241-20245 on 127.0.0.1. None if none answers."""
    urls = [metrics_url] if metrics_url else ["http://127.0.0.1:%d/metrics" % p for p in CLOUDFLARED_PORTS]
    get = get or _get
    for url in urls:
        status, _, body = get(url, timeout)
        if status != 200:
            continue
        for line in body.decode("utf-8", "replace").splitlines():
            if line.startswith("cloudflared_tunnel_ha_connections"):
                return _num(line.split()[-1])
    return None


def tunnel(timeout=5, rivia_dir=None, proc=PROC, public_url=PUBLIC_HEALTH, local_url=LOCAL_HEALTH,
           metrics_url=None, get=None):
    """One tunnel check, three layers, the same order rivia.sh's watchdog uses:
      local    http://127.0.0.1:8787/health: the server itself answers
      process  cloudflared is running (tunnel.pid, and it really is cloudflared)
      edge     cloudflared's own count of live connections to Cloudflare's edge
      public   https://mcp.rivia.observer/health from the tablet: DNS, edge, tunnel and server together
    tunnel_unreachable is 100 when the public check fails while the server answers locally (the tunnel is
    the problem), 0 when the public check passes, and None when the server is down locally (that's the
    server's problem, counted in restarts and errors, not the tunnel's). 4.3 averages it into a percent.
    Note: a check from the tablet goes out to Cloudflare and back. It proves the tunnel works, not that
    Jordan's phone can reach it from wherever he is."""
    get = get or _get
    lstatus, lms, _ = get(local_url, timeout)
    pstatus, pms, _ = get(public_url, timeout)
    edge = _edge_connections(min(timeout, 2), metrics_url, get)
    local_ok, public_ok = lstatus == 200, pstatus == 200
    if public_ok:
        unreachable = 0.0
    elif local_ok:
        unreachable = 100.0
    else:
        unreachable = None
    return {
        "tunnel_unreachable": unreachable,
        "local_ok": local_ok, "local_ms": lms,
        "public_ok": public_ok, "public_ms": pms, "public_status": pstatus,
        "cloudflared_running": _alive(os.path.join(rivia_dir or RIVIA_DIR, "tunnel.pid"), "cloudflared", proc),
        "edge_connections": edge,
    }


# ---------- device vitals ----------

def temperature(power=POWER, thermal=THERMAL):
    """Battery temperature in degrees C (the kernel reports 1/10 degree C), plus the hottest readable
    thermal zone (millidegree C) as a side reading. Android may refuse some thermal zones to Termux; those
    are skipped, never guessed. The signal is the battery value: it's the one Termux is known to read."""
    batt = _num(_read(os.path.join(power, "temp")), 0.1)
    hottest = None
    try:
        zones = sorted(z for z in os.listdir(thermal) if z.startswith("thermal_zone"))
    except OSError:
        zones = []
    for z in zones:
        v = _num(_read(os.path.join(thermal, z, "temp")), 0.001)
        if v is not None and -40 < v < 150 and (hottest is None or v > hottest):  # drop sensors reporting junk
            hottest = v
    return {"temperature": None if batt is None else round(batt, 1),
            "hottest_zone_c": None if hottest is None else round(hottest, 1)}


def memory_used(proc=PROC):
    """Percent of the tablet's RAM in use, as Android sees it: 100 * (1 - MemAvailable / MemTotal)."""
    text = _read(os.path.join(proc, "meminfo"))
    if not text:
        return None
    vals = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("MemTotal:", "MemAvailable:"):
            vals[parts[0]] = _num(parts[1])
    total, avail = vals.get("MemTotal:"), vals.get("MemAvailable:")
    if not total or avail is None:
        return None
    return round(100.0 * (1 - avail / total), 2)


def storage_free(path=None):
    """Percent of the data partition that is free, measured where Rivia keeps her databases (~/rivia)."""
    try:
        st = os.statvfs(path or RIVIA_DIR)
    except OSError:
        return None
    if not st.f_blocks:
        return None
    return round(100.0 * st.f_bavail / st.f_blocks, 2)


def battery(power=POWER):
    """Battery percent, the same file rivia_battery.read_battery() reads."""
    v = _num(_read(os.path.join(power, "capacity")))
    return None if v is None or not 0 <= v <= 100 else v


# ---------- everything at once ----------

def read_all(cursor=None, window_s=None, now=None, rivia_dir=None, logs=None, proc=PROC, power=POWER,
             thermal=THERMAL, get=None):
    """One reading of every signal. Returns (signals, extras, cursor): signals is {name: number or None},
    ready for 4.3's record(); extras is the side detail for the shadow log; cursor goes back in next time.
    Takes a few seconds at worst (three HTTP checks, 5 s timeout each)."""
    now = now or datetime.now(TZ)
    window_s = window_s or WINDOW_S
    lines, cursor, elapsed = log_delta(cursor, logs)
    calls = tool_calls(window_s, now, rivia_dir)
    tun = tunnel(rivia_dir=rivia_dir, proc=proc, get=get)
    temp = temperature(power, thermal)
    signals = {
        "uptime_s": uptime_s(rivia_dir, proc),
        "restarts": restarts(window_s, now, logs),
        "error_rate": error_rate(lines, elapsed),
        "tool_failure_rate": calls["tool_failure_rate"],
        "response_time": calls["response_time"],
        "failed_connections": failed_connections(calls["connection_failures"], lines, elapsed),
        "tunnel_unreachable": tun["tunnel_unreachable"],
        "temperature": temp["temperature"],
        "memory_used": memory_used(proc),
        "storage_free": storage_free(rivia_dir),
        "battery": battery(power),
    }
    extras = {"calls": calls["calls"], "hottest_zone_c": temp["hottest_zone_c"],
              **{k: v for k, v in tun.items() if k != "tunnel_unreachable"}}
    return signals, extras, cursor


if __name__ == "__main__":  # on the tablet: python rivia_body_signals.py  (read-only, prints one reading)
    import json
    sig, extra, _ = read_all()
    print(json.dumps({"signals": sig, "extras": extra}, indent=2))
