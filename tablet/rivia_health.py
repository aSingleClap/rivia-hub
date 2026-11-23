# rivia_health.py: Rivia's read-only health readers (emotion spec, Step 4.8). Draft, Nov 23, not installed.
#
# Jordan, Nov 22 (4.8 thread): "Let's keep it internal I still want to be able to check on the status of things if
# I ask her". So everything here is internal code on no Gemini door. The one thing she can see is status_summary(),
# through the read-only lookup in rivia_health_tool.py, and it only hands back numbers and short codes.
#
# The readers:
#   recent_errors()      the last few failed tool calls (from core.db) and a count of error lines in server.log.
#                        No raw log text ever leaves this file: server.log holds strangers' scan traffic.
#   device_vitals()      temperature, memory, storage, battery (4.1's readers).
#   connection_check()   the Cloudflare tunnel (4.1's four-layer check) plus Tailscale. LIVE network check, internal
#                        only (the collector, `rivia health`). The lookup never calls it.
#   tailscale_status()   Jordan, Nov 22: "I still use it". Information only: it never feeds a dial.
#   status_summary()     what she reads when Jordan asks: the collector's last saved reading plus the local reads
#                        above. No network call, so a down tunnel can't make the lookup itself fail.
#
# Rules this file keeps (each one tested):
#   - Read-only. It opens core.db and body.db read-only and writes no file. It starts, stops and signals nothing.
#   - Never raises. A source it can't read gives None or "unknown", never 0 and never a guess.
#   - It doesn't move 4.2's server.log cursor, so reading errors here can't hide them from the dials.
#   - It returns no emotion words at all: numbers and short codes only.
#   - Every value is marked source "code": she never supplies a number here (4.5: confidence comes from code).
import fcntl
import ipaddress
import json
import os
import re
import socket
import sqlite3
import struct
import time

import rivia_body_signals as body  # 4.1

RIVIA_DIR = body.RIVIA_DIR
LOGS = body.LOGS
LOG_TAIL_BYTES = 64 * 1024      # how much of the end of server.log recent_errors looks at
ERRORS_LIMIT = 10               # "the last few log entries" (spec): at most this many failed calls listed
READING_STALE_S = 5 * 60        # the collector reads once a minute; a saved reading older than this is "stale"
TAILSCALE_STALE_S = 15 * 60     # a supervisor status file older than this says nothing
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")  # the range Tailscale gives its devices
TAILSCALE_FILE = os.path.join(RIVIA_DIR, "state", "tailscale.json")
SIOCGIFADDR = 0x8915            # Linux ioctl: an interface's IPv4 address
LOOKUP_TOOL = "health_status"   # the lookup's tool name; 4.9 keeps it out of 4.1's tool numbers (README)

SAFE_NAME = re.compile(r"^[a-z0-9_.]{1,64}$")
EXC_TYPE = re.compile(r"\b([A-Z][A-Za-z0-9]{2,40}(?:Error|Exception|Timeout))\b")
LOG_KINDS = {
    "error_lines": re.compile(r"^ERROR:"),
    "tracebacks": re.compile(r"^Traceback \(most recent call last\)"),
    "server_5xx": re.compile(r'" 5\d\d '),
    "rivia_failed": re.compile(r"^rivia: .*\b(failed|crashed)\b"),
}
# Refusals: the request was wrong and she correctly said no. 4.7 (Jordan): these are the only failures that could be
# her own mistake, but the log can't tell whether the bad value came from Jordan's words or from her (README).
APP_CODES_PREFIX = "http_4"


def _safe(name):
    return name if isinstance(name, str) and SAFE_NAME.match(name) else "unreadable"


def _ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)


def failure_kind(code):
    """body: something broke (counts toward confidence today, 4.1's list). app: the app's own refused request (not
    hers). refused: she turned down a bad request (Jordan's typo or her mistake; can't tell yet)."""
    code = code or "internal"
    if code in body.BODY_CODES:
        return "body"
    if code.startswith(APP_CODES_PREFIX):
        return "app"
    return "refused"


# ---------- recent errors ----------

def _failed_calls(limit, rivia_dir):
    path = os.path.join(rivia_dir or RIVIA_DIR, "core.db")
    if not os.path.exists(path):
        return None
    try:
        con = _ro(path)
        try:
            rows = con.execute("select ts, tool, code, ms from calls where ok = 0 and tool != ? "
                               "order by rowid desc limit ?", (LOOKUP_TOOL, int(limit))).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    out = []
    for ts, tool, code, ms in rows:
        code = _safe(code or "internal")
        out.append({"at": ts if isinstance(ts, str) and len(ts) <= 40 else None, "tool": _safe(tool),
                    "code": code, "kind": failure_kind(code), "ms": ms if isinstance(ms, int) else None})
    return out


def _log_tail(logs):
    path = os.path.join(logs or LOGS, "server.log")
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL_BYTES))
            raw = f.read(LOG_TAIL_BYTES)
    except OSError:
        return None
    lines = raw.decode("utf-8", "replace").splitlines()
    if size > LOG_TAIL_BYTES and lines:
        lines = lines[1:]  # the first line is probably cut in half
    return lines


def recent_errors(limit=ERRORS_LIMIT, rivia_dir=None, logs=None):
    """The last few failed tool calls, newest first, and what kinds of error lines sit at the end of server.log.
    Only code-made words come out: tool names, error codes, counts, and Python exception type names (which are
    matched by pattern, so a stranger's text can't pass as one). Never the log lines themselves."""
    calls = _failed_calls(limit, rivia_dir)
    lines = _log_tail(logs)
    log = None
    if lines is not None:
        log = {k: sum(1 for ln in lines if rx.search(ln)) for k, rx in LOG_KINDS.items()}
        types = []
        for ln in lines:
            if ln.startswith(" ") or not (LOG_KINDS["error_lines"].search(ln) or ln.split(":", 1)[0].endswith(
                    ("Error", "Exception", "Timeout"))):
                continue
            m = EXC_TYPE.search(ln.split(":", 1)[0])
            if m and m.group(1) not in types:
                types.append(m.group(1))
        log["exception_types"] = types[-5:]
        log["lines_looked_at"] = len(lines)
    return {"failed_calls": calls, "log": log, "source": "code"}


# ---------- device vitals ----------

def device_vitals(power=body.POWER, thermal=body.THERMAL, proc=body.PROC, rivia_dir=None):
    temp = body.temperature(power, thermal)
    return {"temperature_c": temp["temperature"], "hottest_zone_c": temp["hottest_zone_c"],
            "memory_used_pct": body.memory_used(proc), "storage_free_pct": body.storage_free(rivia_dir),
            "battery_pct": body.battery(power), "source": "code"}


# ---------- Tailscale ----------

def _interfaces_ipv4():
    """{interface name: IPv4 address} for every interface with one, without root: if_nameindex plus one ioctl each.
    Not tried on the tablet yet (README). Raises nothing; an interface that refuses is skipped."""
    out = {}
    try:
        names = [n for _, n in socket.if_nameindex()]
    except OSError:
        return None
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for name in names:
            try:
                res = fcntl.ioctl(s.fileno(), SIOCGIFADDR, struct.pack("256s", name[:15].encode()))
                out[name] = socket.inet_ntoa(res[20:24])
            except OSError:
                continue
    finally:
        s.close()
    return out


def _from_file(path, now):
    try:
        with open(path) as f:
            data = json.loads(f.read(512))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    up, at = data.get("up"), data.get("checked_at")
    if not isinstance(up, bool) or isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    if not (now - TAILSCALE_STALE_S <= at <= now + 60):
        return None  # too old (or from the future) to say anything
    return {"state": "up" if up else "down", "how": "supervisor_file", "checked_at": int(at)}


def tailscale_status(now=None, interfaces=None, status_file=None):
    """Is the tablet on Tailscale right now? Information only (Jordan: "I still use it"); never a dial input.
    1. Look for an interface with an address in 100.64.0.0/10, the range Tailscale hands out. Found: up.
    2. Otherwise, if the root supervisor writes ~/rivia/state/tailscale.json (README), use it while it's fresh.
    3. Otherwise unknown. Interfaces readable but none in the range, and no fresh file: down.
    Limit: "up" means the tablet has its Tailscale address, not that your other devices can reach it."""
    now = now if now is not None else time.time()
    try:
        found = (interfaces or _interfaces_ipv4)()
    except Exception:  # noqa: BLE001
        found = None
    if found:
        for name, ip in sorted(found.items()):
            try:
                if ipaddress.ip_address(ip) in TAILSCALE_NET:
                    return {"state": "up", "how": "interface", "interface": _safe(name.lower()), "checked_at": int(now)}
            except ValueError:
                continue
    from_file = _from_file(status_file or TAILSCALE_FILE, now)
    if from_file:
        return from_file
    if found is not None:
        return {"state": "down", "how": "interface", "checked_at": int(now)}
    return {"state": "unknown", "how": None, "checked_at": None}


# ---------- connection check (live, internal only) ----------

def connection_check(get=None, rivia_dir=None, proc=body.PROC, now=None, interfaces=None, status_file=None):
    """The tunnel, four layers (4.1), plus Tailscale. Makes network calls: for the collector and `rivia health`,
    never for the lookup."""
    tun = body.tunnel(rivia_dir=rivia_dir, proc=proc, get=get)
    return {"tunnel": _tunnel_words(tun), "tunnel_detail": tun,
            "tailscale": tailscale_status(now, interfaces, status_file), "source": "code"}


def _tunnel_words(t):
    if not t:
        return "unknown"
    if t.get("public_ok"):
        return "working"
    if t.get("local_ok"):
        return "down"          # server fine, tunnel not
    if t.get("local_ok") is False:
        return "server_down"   # not the tunnel's fault (4.1)
    return "unknown"


# ---------- what she reads when Jordan asks ----------

def _last_reading(body_db, now):
    path = body_db or os.path.join(RIVIA_DIR, "body.db")
    if not os.path.exists(path):
        return None
    try:
        con = _ro(path)
        try:
            row = con.execute("select at, ok, signals, extras from reads order by at desc limit 1").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    at, okay, sig, extra = row
    try:
        sig, extra = json.loads(sig), json.loads(extra)
    except (TypeError, ValueError):
        return None
    return {"at": at, "age_s": max(0, int(now - at)), "ok": bool(okay),
            "signals": sig if isinstance(sig, dict) else {}, "extras": extra if isinstance(extra, dict) else {}}


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def status_summary(now=None, body_db=None, rivia_dir=None, logs=None, interfaces=None, status_file=None):
    """The answer to "how's the server?". The collector's last reading (4.2, once a minute), the last few failed
    calls, and Tailscale. Local reads only: no network call, nothing written. Never raises."""
    now = now if now is not None else time.time()
    out = {"source": "code", "reading": "none", "reading_age_s": None}
    try:
        last = _last_reading(body_db, now)
    except Exception:  # noqa: BLE001
        last = None
    if last:
        s, x = last["signals"], last["extras"]
        out["reading"] = "fresh" if last["age_s"] <= READING_STALE_S and last["ok"] else "stale"
        out["reading_age_s"] = last["age_s"]
        out["server"] = {"uptime_s": _num(s.get("uptime_s")), "restarts_per_hour": _num(s.get("restarts")),
                         "errors_per_hour": _num(s.get("error_rate")),
                         "tool_failure_pct": _num(s.get("tool_failure_rate")),
                         "response_ms": _num(s.get("response_time")),
                         "failed_connections_per_hour": _num(s.get("failed_connections"))}
        out["vitals"] = {"temperature_c": _num(s.get("temperature")), "memory_used_pct": _num(s.get("memory_used")),
                         "storage_free_pct": _num(s.get("storage_free")), "battery_pct": _num(s.get("battery"))}
        tun = {"public_ok": x.get("public_ok"), "local_ok": x.get("local_ok")}
        out["tunnel"] = _tunnel_words(tun if s.get("tunnel_unreachable") is not None or x.get("local_ok") is False
                                      else None)
        out["edge_connections"] = _num(x.get("edge_connections"))
    else:
        out["tunnel"] = "unknown"
    try:
        out["recent_errors"] = recent_errors(rivia_dir=rivia_dir, logs=logs)
    except Exception:  # noqa: BLE001
        out["recent_errors"] = {"failed_calls": None, "log": None, "source": "code"}
    try:
        out["tailscale"] = tailscale_status(now, interfaces, status_file)
    except Exception:  # noqa: BLE001
        out["tailscale"] = {"state": "unknown", "how": None, "checked_at": None}
    return out


if __name__ == "__main__":  # on the tablet: python rivia_health.py [live]   (read-only; "live" adds the network check)
    import sys
    print(json.dumps(status_summary(), indent=2, default=str))
    if sys.argv[1:] == ["live"]:
        print(json.dumps(connection_check(), indent=2, default=str))
