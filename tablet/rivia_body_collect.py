# rivia_body_collect.py: the collector that reads Rivia's body signals on a timer (emotion spec, Step 4.2).
#
# DRAFT (Nov 22, not installed). Jordan, Nov 22: "As long as the connector doesn't sign me out then that's fine"
# (his yes to a timer job beside the sign-in check). So this job is built to stay away from everything that
# holds a sign-in:
#
#   never imports  rivia_google, rivia_gcal, rivia_auth, rivia_applogin, rivia_tools, rivia_admin (tested by
#                  scanning every module it loads)
#   never opens    ~/.rivia (google_token.json, admin_token), auth.db, or any Google address
#   never writes   anything but body.db (4.3's samples and buckets, plus this file's reads table and cursor)
#   never restarts the server or cloudflared, and never runs inside the sign-in check's loop
#
# What one tick does: 4.1's read_all() (read-only), then 4.3's record_many() for the dial signals, then one row in
# the reads table (the shadow log). The blocking reads run in a worker thread, so the server keeps answering
# while a slow tunnel check waits on its timeout. A tick still running when the next is due is skipped, never
# stacked. Nothing here raises: a bad tick is printed to server.log as one line and the next tick runs as usual.
#
# It is not a Gemini tool and it is on no door (4.8). It changes no mood (4.4 to 4.6 read body.db on their own).
# Step 5.6: after 4.9's dials, rivia_progress logs the plan-accuracy trend to body.db in shadow (RIVIA_PLAN_SHADOW=0
# turns it off). It reads the scheduler's files read-only and never writes them.
# Step 6: after Step 5, rivia_trust_shadow judges her plans and works out trust in shadow (RIVIA_TRUST_SHADOW=0
# switches it off).
import asyncio
import json
import os
import sys
import time

import rivia_body_signals as body
import rivia_normal as normal
try:  # Step 4.9 shadow mode: if it fails to load, the collector still collects
    import rivia_body_shadow as shadow
except Exception:  # noqa: BLE001
    shadow = None
try:  # Step 5.6 plan-accuracy trend in shadow mode: same rule, the collector never depends on it
    import rivia_progress as progress
except Exception:  # noqa: BLE001
    progress = None
try:  # Step 6 trust and accuracy in shadow mode: same rule, the collector never depends on it
    import rivia_trust_shadow as trust_shadow
except Exception:  # noqa: BLE001
    trust_shadow = None
import rivia_store as store

INTERVAL_S = 60          # one reading a minute: 15 samples in each of 4.3's 15-minute windows
MIN_INTERVAL_S = 30      # RIVIA_BODY_INTERVAL_S below this is raised to it (each tick makes 3 local/public checks)
FIRST_DELAY_S = 30       # wait after the server starts, so the first tunnel check doesn't catch the server half up
TICK_TIMEOUT_S = 45      # a tick that takes longer is given up on (the thread is left to finish and the next is skipped)
WINDOW_S = body.WINDOW_S  # 4.1's 15-minute window (Jordan, Nov 22: "15 minutes"); was 3600 in the 4.2 draft
CURSOR_MAX_AGE_S = 15 * 60  # a server.log cursor older than this is dropped (see _load_cursor)
READS_KEEP_S = 2 * 86400    # the shadow log keeps 2 days, same as 4.3's raw samples
CURSOR_KEY = "collector.cursor"

# Signals that go into 4.3. uptime_s is left out on purpose: 4.1 marks it "shown only", since it climbs all day and
# has no normal. It is still in each reads row.
NOT_A_DIAL = frozenset({"uptime_s"})

READS_SCHEMA = """
create table if not exists reads(
    at integer primary key, took_ms integer not null, ok integer not null, error text,
    signals text not null, extras text not null, stored integer not null, rejected text not null);
"""


def enabled():
    """On unless RIVIA_BODY_COLLECTOR is 0/false/off/no. Same switch style as RIVIA_SIGNIN_CHECK."""
    return os.environ.get("RIVIA_BODY_COLLECTOR", "1").strip().lower() not in ("0", "false", "off", "no")


def shadow_enabled():
    """The shadow dials run with the collector unless RIVIA_BODY_SHADOW is 0/false/off/no (Step 4.9)."""
    return os.environ.get("RIVIA_BODY_SHADOW", "1").strip().lower() not in ("0", "false", "off", "no")


def interval_s():
    raw = os.environ.get("RIVIA_BODY_INTERVAL_S", "").strip()
    try:
        v = int(raw) if raw else INTERVAL_S
    except ValueError:
        v = INTERVAL_S
    return max(MIN_INTERVAL_S, v)


def db_open():
    """body.db (4.3's tables) plus the reads table. Same file, same opener, so 4.3 stays the owner of its tables."""
    con = normal.db_open()
    con.executescript(READS_SCHEMA)
    return con


def _load_cursor(con, now_epoch):
    """The server.log cursor from the last tick, or None. A cursor older than CURSOR_MAX_AGE_S (the server was down,
    or the collector was off) is dropped, so a long gap doesn't turn into one diluted error rate over hours. The
    lines written during that gap are skipped: a crash in it is already counted by 4.1's restarts."""
    raw = store.get_meta(con, CURSOR_KEY)
    if not raw:
        return None
    try:
        cur = json.loads(raw)
        if not isinstance(cur, dict) or now_epoch - float(cur.get("at", 0)) > CURSOR_MAX_AGE_S:
            return None
        return cur
    except (ValueError, TypeError):
        return None


def collect_once(con=None, now=None, read=None):
    """One tick. Reads every signal (4.1), stores the dial signals in 4.3, logs the reading. Never raises.
    read is 4.1's read_all (tests pass a fake). Returns a summary dict, the same thing written to the reads table."""
    t0 = time.perf_counter()
    now_epoch = int(now if now is not None else time.time())
    read = read or body.read_all
    own = con is None
    out = {"at": now_epoch, "ok": False, "error": None, "signals": {}, "extras": {}, "stored": 0, "rejected": []}
    try:
        con = con or db_open()
    except Exception as e:  # noqa: BLE001
        out["error"] = "db_open: %s" % type(e).__name__
        return out
    try:
        try:
            cursor = _load_cursor(con, now_epoch)
            signals, extras, cursor = read(cursor=cursor, window_s=WINDOW_S)
            out["signals"], out["extras"] = signals, extras
            rows = [(name, value, now_epoch) for name, value in sorted(signals.items())
                    if name not in NOT_A_DIAL and value is not None]
            res = normal.record_many(rows, con=con)
            out["stored"] = res["stored"]
            out["rejected"] = [[rows[i][0], code] for i, code, _ in res["rejected"]]
            if cursor:
                store.set_meta(con, CURSOR_KEY, json.dumps(cursor))
            out["ok"] = True
            # Step 4.9: the dials in shadow mode. Logged to body.db only; her mood is never touched. A failure here
            # is noted and never stops the reading from being stored.
            if shadow is not None and shadow_enabled():
                try:
                    out["shadow"] = shadow.shadow_tick(con, now_epoch)
                except Exception as e:  # noqa: BLE001
                    out["shadow_error"] = type(e).__name__
            # Step 5.6: the plan trend, right after the dials so it sees this tick's body pull (counted once).
            # Logged to body.db only, like the dials; a failure is noted and changes nothing else.
            if progress is not None and progress.enabled():
                try:
                    share = (out.get("shadow") or {}).get("share") or (0.0, 0.0, 0.0)
                    res = progress.shadow_tick(con, now_epoch, body_share=share)
                    out["plan"] = {"scored": res["scored"], "direction": res["trend"]["direction"],
                                   "would_read": res["mood"]["would_read"]}
                except Exception as e:  # noqa: BLE001
                    out["plan_error"] = type(e).__name__
            # Step 6: judge the days Step 5 just scored, then trust, satisfaction and the unlock ladder. After
            # Step 5 so a newly scored day is there. Logged to body.db only; a failure is noted and changes nothing.
            if trust_shadow is not None and trust_shadow.enabled():
                try:
                    res6 = trust_shadow.shadow_tick(con, now_epoch)
                    out["trust"] = {"judged": res6["judged"], "trust": res6["trust"]["value"],
                                    "satisfaction": res6["satisfaction"]["value"], "killed": res6["killed"]}
                except Exception as e:  # noqa: BLE001
                    out["trust_error"] = type(e).__name__
        except Exception as e:  # noqa: BLE001
            out["error"] = type(e).__name__
        out["took_ms"] = int((time.perf_counter() - t0) * 1000)
        try:
            con.execute("insert or replace into reads values(?,?,?,?,?,?,?,?)",
                        (now_epoch, out["took_ms"], int(out["ok"]), out["error"],
                         json.dumps(out["signals"], sort_keys=True), json.dumps(out["extras"], sort_keys=True, default=str),
                         out["stored"], json.dumps(out["rejected"])))
            con.execute("delete from reads where at < ?", (now_epoch - READS_KEEP_S,))
        except Exception as e:  # noqa: BLE001
            out["error"] = out["error"] or "reads_log: %s" % type(e).__name__
        return out
    finally:
        if own:
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass


def recent_reads(limit=20, con=None):
    """The newest rows of the shadow log, newest first, for `rivia body` and the review. Read-only."""
    own = con is None
    con = con or db_open()
    try:
        rows = con.execute("select at, took_ms, ok, error, signals, extras, stored, rejected from reads "
                           "order by at desc limit ?", (int(limit),)).fetchall()
    finally:
        if own:
            con.close()
    return [{"at": a, "took_ms": ms, "ok": bool(ok), "error": err, "signals": json.loads(s),
             "extras": json.loads(x), "stored": st, "rejected": json.loads(rj)}
            for a, ms, ok, err, s, x, st, rj in rows]


async def loop(*, sleep=None, tick=None, every=None, first_delay=None, timeout=None, log=None):
    """The timer. Started by rivia_server.py as its own task, separate from the sign-in check's loop, and cancelled
    on shutdown. Each tick runs collect_once in a worker thread. If that thread is still busy when the next tick is
    due, the tick is skipped. Never raises out except CancelledError."""
    sleep = sleep or asyncio.sleep
    tick = tick or collect_once
    every = interval_s() if every is None else every
    timeout = TICK_TIMEOUT_S if timeout is None else timeout
    log = log or (lambda msg: print(msg, file=sys.stderr))
    busy = None  # the worker of the last tick, if it hasn't finished
    await sleep(FIRST_DELAY_S if first_delay is None else first_delay)
    while True:
        started = time.monotonic()
        if busy is not None and not busy.done():
            log("rivia: body collector skipped a tick (the last one is still running).")
        else:
            busy = asyncio.ensure_future(asyncio.to_thread(tick))
            try:
                res = await asyncio.wait_for(asyncio.shield(busy), timeout)
                if isinstance(res, dict) and not res.get("ok"):
                    log("rivia: body collector tick failed (%s); continuing." % res.get("error"))
            except asyncio.TimeoutError:
                log("rivia: body collector tick took over %d s; continuing." % timeout)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log("rivia: body collector tick failed (%s); continuing." % type(e).__name__)
        await sleep(max(1.0, every - (time.monotonic() - started)))


if __name__ == "__main__":  # on the tablet: python rivia_body_collect.py [once|show]
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "once":
        print(json.dumps(collect_once(), indent=2, default=str))
    else:
        print(json.dumps(recent_reads(5), indent=2))
