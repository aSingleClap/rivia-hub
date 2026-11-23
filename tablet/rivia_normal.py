# rivia_normal.py: Rivia's own normal for each body signal (emotion spec, Step 4.3), in body.db.
#
# DRAFT (Nov 22, not installed). Step 4 "System Health as Her Body": "a rolling average per signal, compared to
# her own normal for that signal, not to fixed thresholds." This file only learns the normal and says how far
# she is from it. It does not weigh how bad anything is (4.4 stress, 4.5 confidence; it only knows which side of
# each signal is worse, so that worse never becomes her normal, below), it caps nothing (4.6),
# it collects nothing (4.2), it is not a Gemini tool (4.8), and it never touches emotion.db. Nothing imports it
# yet.
#
#   body.db   samples: raw readings as (signal, value, at), one row each. Kept RAW_KEEP_S (2 days).
#             buckets: per signal, one row per BUCKET_S (15 minutes): count, total, low, high.
#                      Kept BUCKET_KEEP_S (30 days). Updated in the same transaction as the sample that feeds it.
#
# How a read works (normal(), normals()), worked out on every read like Step 2's decay, with no timer:
#   recent    the plain average of the raw samples in the last RECENT_S (15 minutes; RECENT_S_FOR can lengthen it
#             for a signal sampled less often). This is "how she is now".
#   usual     the median of the 15-minute bucket averages over the last USUAL_S (7 days), leaving out every
#             bucket that overlaps the recent window, so "now" is never compared with itself. Median, not mean,
#             so a few bad hours (a crash, a heat spike) don't move her normal much.
#   spread    how much a 15-minute average usually wanders: 1.4826 x the median absolute deviation of the same
#             buckets (the usual robust stand-in for a standard deviation), held up by two floors so a signal that
#             is nearly always the same (errors usually 0) can't turn one blip into an infinite reading:
#             REL_SPREAD_FLOOR (2%) of the usual's size, and ABS_SPREAD_FLOOR (per signal, default 1.0 unit).
#   deviation (recent - usual) / spread, signed, clipped to +-DEVIATION_CLIP. 0 is her normal, +2 is "twice as far
#             above usual as it normally wanders". Direction only: whether up is bad is 4.4's call per signal.
#   trusted   only once the warm-up is met (at least MIN_BUCKETS 15-minute buckets with data in the last 7 days,
#             spanning at least WARMUP_S, 3 days) and the signal is not stale (a sample in the last 15 minutes).
#             Until then deviation is None and preview_deviation shows what it would be, for the shadow log only.
#             Trust is worked out on every read, so a signal that goes quiet for days loses it and earns it back.
#
# Writes: record(), record_many(), reassure() and relearn() are the only code that writes body.db. A duplicate (same signal, same whole
# second) is ignored, so a collector can resend after a restart without double counting. Old rows are pruned on
# write, never on read. Reads never write.
#
# Worse is never her normal (Jordan, Nov 22: "If it will effect how rivia runs (she run worse) then it should not
# be normall"). A plain rolling normal learns whatever keeps happening, so a slow slide (storage filling, errors
# creeping up) would end up reading as 0. So each signal with a bad side (WORSE) gets a held normal, stored in
# body.db's anchors table:
#   - It is set the first time the signal finishes its warm-up ("first normal").
#   - It moves only when her normal since it was set is better than it by at least ANCHOR_MARGIN spreads, judged
#     on at least a warm-up's worth of data since then ("got better"). So improvements take about 3 days to land.
#   - Once set, every reading is measured against the held normal and its spread. "held": True says the plain
#     rolling normal has slid worse than it. So a slide keeps reading as worse for as long as it lasts.
#   - reassure() is Jordan telling her a level is fine (Jordan, Nov 22: "I should be able to reassure her that
#     certain things are normal and ok if she brings it to my attention"). It sets the held normal to where the
#     signal is right now, from Jordan's app button, the code or the admin door. Never from Gemini's door "app":
#     she can't reassure herself.
#   - relearn() clears it on purpose (code or admin door only); the next write learns it again from her last week.
#   - Every change to a held normal is logged in anchor_log (append-only), with its cause and door, so the
#     self-model can later see what was reassured and when.
# The anchors are written only by record_many(), inside its transaction, never by a read. A signal with no bad
# side (uptime_s, or a name not in WORSE) has no held normal and learns plainly, as before.
import math
import re

import rivia_store as store
import rivia_time as rt

DB = "body"
SCHEMA = """
create table if not exists samples(
    id integer primary key, signal text not null, value real not null, at integer not null,
    unique(signal, at));
create index if not exists samples_by_time on samples(signal, at);
create table if not exists buckets(
    signal text not null, start integer not null, n integer not null, total real not null,
    lo real not null, hi real not null, primary key(signal, start));
create table if not exists anchors(
    signal text primary key, usual real not null, spread real not null, set_at integer not null,
    cause text not null);
create table if not exists anchor_log(
    id integer primary key, at integer not null, signal text not null, old_usual real, new_usual real,
    cause text not null, door text not null);
create trigger if not exists anchor_log_no_update before update on anchor_log
    begin select raise(abort, 'anchor_log is append-only'); end;
create trigger if not exists anchor_log_no_delete before delete on anchor_log
    begin select raise(abort, 'anchor_log is append-only'); end;
"""

MINUTE, HOUR, DAY = 60, 3600, 86400

# Starting guesses to tune by feel, like every other number in the spec.
BUCKET_S = 15 * MINUTE        # the unit her normal is learned in; same length as the recent window
RECENT_S = 15 * MINUTE        # "now" is the average of the last 15 minutes of samples
RECENT_S_FOR = {}             # per signal: a longer window for one sampled less often (e.g. storage, hourly)
USUAL_S = 7 * DAY             # her normal is learned from the last week
WARMUP_S = 3 * DAY            # "a few days" before it is trusted
MIN_BUCKETS = 48 * 4          # and at least 48 hours' worth of 15-minute buckets with data in that week
RAW_KEEP_S = 2 * DAY          # raw samples are only needed for the recent window and for de-duplicating resends
BUCKET_KEEP_S = 30 * DAY      # buckets outlive the 7-day window so it can be widened later without losing data
FUTURE_SLACK_S = 60           # a sample stamped more than a minute ahead of the tablet's clock is refused
MAD_TO_SD = 1.4826            # scales the median absolute deviation to a standard deviation for normal data
REL_SPREAD_FLOOR = 0.02       # spread is at least 2% of the usual's size (only means something on a scale with a true 0)
DEFAULT_ABS_SPREAD_FLOOR = 1.0  # only for a signal not in ABS_SPREAD_FLOOR
# Per signal, in 4.1's units (Jordan, Nov 22: "Yes" to a floor per signal). The smallest wander that counts as
# one spread. First guesses: each is roughly "the smallest change that would matter to how she runs".
ABS_SPREAD_FLOOR = {
    "restarts": 1.0,             # per hour: one watchdog restart an hour
    "error_rate": 2.0,           # per hour: a couple of stray log errors is ordinary
    "tool_failure_rate": 2.0,    # percent of calls
    "response_time": 50.0,       # ms (median): network jitter alone moves it this much
    "failed_connections": 1.0,   # per hour
    "tunnel_unreachable": 10.0,  # percent of checks (4.1 sends 0 or 100): one failed check in ten
    "temperature": 1.0,          # degrees C
    "memory_used": 2.0,          # percent of RAM
    "storage_free": 1.0,         # percent of the partition
    "battery": 5.0,              # percent: charging and use swing it a lot
}
# Which side of normal is worse, for the held normal above. Matches 4.4's draft (4.5 reads the same). At install
# these three copies should become one list, 4.1's. uptime_s has no bad side: it climbs all day, and 4.1 marks it
# as shown only.
WORSE = {
    "restarts": "up", "error_rate": "up", "tool_failure_rate": "up", "response_time": "up",
    "failed_connections": "up", "tunnel_unreachable": "up", "temperature": "up", "memory_used": "up",
    "storage_free": "down", "battery": "down",
}
ANCHOR_MARGIN = 0.5           # spreads: how much better the rolling normal must be before the held one moves to it,
                              # so ordinary week-to-week noise doesn't ratchet it toward her luckiest week
ANCHOR_DOORS = ("code", "admin")  # who may relearn(); never the app, never Gemini
REASSURE_DOORS = ("code", "admin", "app_button")  # who may reassure(): Jordan's button too. Never "app" (Gemini)
DEVIATION_CLIP = 10.0         # keeps a flat signal's first blip finite; 4.6 owns the real caps on influence
SIGNAL_NAME = re.compile(r"^[a-z][a-z0-9_.]{0,47}$")


class BodyError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def db_open():
    """body.db with its tables. Creating them is the only write an open makes, and only the first time."""
    return store.connect(DB, SCHEMA)


def _signal(name):
    if not isinstance(name, str) or not SIGNAL_NAME.match(name):
        raise BodyError("bad_signal", "signal name must be lower case letters, digits, _ or ., up to 48: %r"
                        % (name,))
    return name


def _value(x):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise BodyError("bad_value", "value must be a number, not %s" % type(x).__name__)
    x = float(x)
    if not math.isfinite(x):
        raise BodyError("bad_value", "value must be finite")
    return x


def _at(at, now):
    if at is None:
        return now
    try:
        t = rt.to_epoch(at, "at")
    except rt.RiviaError as e:
        raise BodyError("bad_time", str(e))
    if t > now + FUTURE_SLACK_S:
        raise BodyError("future", "sample is stamped %d s ahead of the tablet's clock" % (t - now))
    if t < now - RAW_KEEP_S:
        raise BodyError("too_old", "sample is older than %d hours and would be pruned at once" % (RAW_KEEP_S // HOUR))
    return t


def _bucket_start(t):
    return t - t % BUCKET_S


def _insert(con, signal, value, t):
    cur = con.execute("insert or ignore into samples(signal, value, at) values(?,?,?)", (signal, value, t))
    if cur.rowcount != 1:
        return False
    con.execute("""insert into buckets(signal, start, n, total, lo, hi) values(?,?,1,?,?,?)
                   on conflict(signal, start) do update set n = n + 1, total = total + excluded.total,
                   lo = min(lo, excluded.lo), hi = max(hi, excluded.hi)""",
                (signal, _bucket_start(t), value, value, value))
    return True


def _prune(con, now):
    con.execute("delete from samples where at < ?", (now - RAW_KEEP_S,))
    con.execute("delete from buckets where start < ?", (now - BUCKET_KEEP_S,))


def record_many(rows, con=None):
    """Store (signal, value, at) rows in one transaction. at is epoch seconds, epoch ms or ISO 8601 (no offset means
    New York), or None for now. A bad row is refused and the rest are stored.
    Returns {"stored": n, "duplicate": n, "rejected": [(index, code, message), ...]}."""
    own = con is None
    con = con or db_open()
    try:
        now = rt.now_s()
        out = {"stored": 0, "duplicate": 0, "rejected": []}
        touched = set()
        con.execute("begin immediate")
        try:
            for i, row in enumerate(rows):
                try:
                    signal, value, at = row
                    s, v, t = _signal(signal), _value(value), _at(at, now)
                except BodyError as e:
                    out["rejected"].append((i, e.code, str(e)))
                    continue
                except (TypeError, ValueError):
                    out["rejected"].append((i, "bad_row", "a row is (signal, value, at)"))
                    continue
                if _insert(con, s, v, t):
                    out["stored"] += 1
                    touched.add(s)
                else:
                    out["duplicate"] += 1
            _prune(con, now)
            _update_anchors(con, touched, now)
            con.execute("commit")
        except Exception:
            con.execute("rollback")
            raise
        return out
    finally:
        if own:
            con.close()


def record(signal, value, at=None, con=None):
    """Store one sample. Raises BodyError for a bad one. Returns True if stored, False if it was a duplicate."""
    out = record_many([(signal, value, at)], con=con)
    if out["rejected"]:
        _, code, msg = out["rejected"][0]
        raise BodyError(code, msg)
    return out["stored"] == 1


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return None
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def _spread(signal, means, usual):
    mad = _median([abs(x - usual) for x in means])
    floor_abs = ABS_SPREAD_FLOOR.get(signal, DEFAULT_ABS_SPREAD_FLOOR)
    return max(MAD_TO_SD * mad, REL_SPREAD_FLOOR * abs(usual), floor_abs)


def _clip(z):
    return max(-DEVIATION_CLIP, min(DEVIATION_CLIP, z))


def _rolling(con, signal, now, window, since=None):
    """Her plain rolling normal: (usual, spread, buckets used, span in seconds, warm-up done). With since, only
    buckets that start wholly after that time count, so a held normal improves only on what came after it."""
    cut = _bucket_start(now - window)  # the bucket holding the window's start overlaps it, so it is left out too
    lo = now - USUAL_S
    if since is not None:
        lo = max(lo, _bucket_start(since) + BUCKET_S)
    rows = con.execute("select start, n, total from buckets where signal=? and start >= ? and start < ? order by start",
                       (signal, lo, cut)).fetchall()
    means = [total / n for _, n, total in rows]
    usual = _median(means)
    spread = None if usual is None else _spread(signal, means, usual)
    span = (rows[-1][0] + BUCKET_S - rows[0][0]) if rows else 0
    warm = len(rows) >= MIN_BUCKETS and span >= WARMUP_S
    return usual, spread, len(rows), span, warm


def _worse(signal, x, than):
    """True if level x is worse than level `than` for this signal."""
    side = WORSE.get(signal)
    return (x > than) if side == "up" else (x < than) if side == "down" else False


def _anchor(con, signal):
    r = con.execute("select usual, spread, set_at, cause from anchors where signal=?", (signal,)).fetchone()
    return None if r is None else {"usual": r[0], "spread": r[1], "set_at": r[2], "cause": r[3]}


def _update_anchors(con, names, now):
    """Inside record_many's transaction: set or improve each touched signal's held normal."""
    for signal in sorted(names):
        if signal not in WORSE:
            continue
        window = RECENT_S_FOR.get(signal, RECENT_S)
        a = _anchor(con, signal)
        # an improvement is judged only on data since the held normal was last set, so a reassurance isn't
        # undone by the older, better-looking days still in her week, and noise can't ratchet it every write
        usual, spread, _, _, warm = _rolling(con, signal, now, window, since=None if a is None else a["set_at"])
        if not warm:
            continue
        if a is None:
            _set_anchor(con, signal, None, usual, spread, now, "first normal", "code")
        elif _worse(signal, a["usual"], usual) and abs(a["usual"] - usual) >= ANCHOR_MARGIN * a["spread"]:
            _set_anchor(con, signal, a["usual"], usual, spread, now, "got better", "code")


def _set_anchor(con, signal, old_usual, usual, spread, now, cause, door):
    """The one place a held normal is written, always with its anchor_log row, inside the caller's transaction."""
    if usual is None:
        con.execute("delete from anchors where signal=?", (signal,))
    else:
        con.execute("insert or replace into anchors(signal, usual, spread, set_at, cause) values(?,?,?,?,?)",
                    (signal, usual, spread, now, cause))
    con.execute("insert into anchor_log(at, signal, old_usual, new_usual, cause, door) values(?,?,?,?,?,?)",
                (now, signal, old_usual, usual, cause, door))


def reassure(signal, *, door, cause):
    """Jordan says this level is fine: the held normal becomes where the signal is right now (its last-15-minute
    average), keeping the wider of the old and current wobble. From the code, admin or Jordan's app button only.
    Needs a recent reading and a finished warm-up. Returns {"old", "new"} (the held normal before and after)."""
    if door not in REASSURE_DOORS:
        raise BodyError("bad_door", "reassure is for Jordan's button, the code or the admin door, not %r" % (door,))
    if not isinstance(cause, str) or not cause.strip():
        raise BodyError("bad_cause", "reassure needs a cause")
    signal = _signal(signal)
    if signal not in WORSE:
        raise BodyError("no_bad_side", "%s has no bad side, so it has no held normal to reassure" % signal)
    con = db_open()
    try:
        con.execute("begin immediate")
        try:
            now = rt.now_s()
            r = _read(con, signal, now)
            if r["stale"] or not r["warmup"]["done"]:
                raise BodyError("not_ready", "%s needs a finished warm-up and a reading in the last %d minutes"
                                % (signal, r["window_s"] // MINUTE))
            old = _anchor(con, signal)
            _, spread, _, _, _ = _rolling(con, signal, now, r["window_s"])
            if old is not None:
                spread = max(spread, old["spread"])
            _set_anchor(con, signal, None if old is None else old["usual"], r["recent"], spread, now,
                        "reassured: " + cause.strip(), door)
            con.execute("commit")
        except Exception:
            con.execute("rollback")
            raise
        return {"old": old, "new": _anchor(con, signal)}
    finally:
        con.close()


def relearn(signal, *, door, cause):
    """Forget a signal's held normal on purpose, so the next write learns it again from where she is then.
    Code or admin door only. Returns the held normal that was cleared, or None if there was none."""
    if door not in ANCHOR_DOORS:
        raise BodyError("bad_door", "relearn is for the code or admin door, not %r" % (door,))
    if not isinstance(cause, str) or not cause.strip():
        raise BodyError("bad_cause", "relearn needs a cause")
    signal = _signal(signal)
    con = db_open()
    try:
        con.execute("begin immediate")
        try:
            a = _anchor(con, signal)
            if a is not None:
                _set_anchor(con, signal, a["usual"], None, None, rt.now_s(), "relearn: " + cause.strip(), door)
            con.execute("commit")
        except Exception:
            con.execute("rollback")
            raise
        return a
    finally:
        con.close()


def _read(con, signal, now):
    window = RECENT_S_FOR.get(signal, RECENT_S)
    r = con.execute("select count(*), avg(value), max(at) from samples where signal=? and at > ? and at <= ?",
                    (signal, now - window, now)).fetchone()
    recent_n, recent = r[0], r[1]
    last_at = con.execute("select max(at) from samples where signal=? and at <= ?", (signal, now)).fetchone()[0]
    if last_at is None:  # raw samples pruned: the newest bucket still says roughly when she last heard of it
        b = con.execute("select max(start) from buckets where signal=? and start <= ?", (signal, now)).fetchone()[0]
        last_at = None if b is None else b + BUCKET_S - 1
    rolling, spread, n_buckets, span, warm = _rolling(con, signal, now, window)
    anchor = _anchor(con, signal) if signal in WORSE else None
    held = anchor is not None and rolling is not None and _worse(signal, rolling, anchor["usual"])
    usual = anchor["usual"] if anchor is not None else rolling   # a signal with a held normal is read against it
    if anchor is not None:
        spread = anchor["spread"]
    stale = recent_n == 0
    preview = None if (recent is None or usual is None) else _clip((recent - usual) / spread)
    trusted = warm and not stale
    return {
        "signal": signal, "at": now, "window_s": window,
        "recent": recent, "recent_n": recent_n, "last_at": last_at, "stale": stale,
        "usual": usual, "spread": spread, "usual_buckets": n_buckets,
        "rolling_usual": rolling,  # what a plain rolling normal would say
        "held": held,              # True: her rolling normal has slid worse, so she is measured against the held one
        "anchor": anchor,          # the held normal (None for a signal with no bad side, or before its warm-up)
        "bad_side": WORSE.get(signal),
        "floor_set": signal in ABS_SPREAD_FLOOR,  # False: the 1.0-unit default floor is in use
        "deviation": preview if trusted else None,
        "preview_deviation": preview,
        "trusted": trusted,
        "warmup": {"buckets": n_buckets, "buckets_needed": MIN_BUCKETS, "span_s": span, "span_needed_s": WARMUP_S,
                   "done": warm},
    }


def normal(signal, con=None):
    """How far one signal is from her own normal, right now. Read-only."""
    own = con is None
    con = con or db_open()
    try:
        return _read(con, _signal(signal), rt.now_s())
    finally:
        if own:
            con.close()


def signals(con=None):
    """Every signal body.db has heard of (in the last 30 days of buckets)."""
    own = con is None
    con = con or db_open()
    try:
        return [r[0] for r in con.execute("select distinct signal from buckets order by signal")]
    finally:
        if own:
            con.close()


def anchor_history(signal=None, n=20, con=None):
    """The latest changes to held normals, newest first. Read-only."""
    own = con is None
    con = con or db_open()
    try:
        q = "select id, at, signal, old_usual, new_usual, cause, door from anchor_log"
        args = ()
        if signal is not None:
            q += " where signal=?"
            args = (_signal(signal),)
        rows = con.execute(q + " order by id desc limit ?", args + (int(n),)).fetchall()
        keys = ("id", "at", "signal", "old_usual", "new_usual", "cause", "door")
        return [dict(zip(keys, r)) for r in rows]
    finally:
        if own:
            con.close()


def normals(con=None):
    """normal() for every signal, read at one instant so they line up. Read-only."""
    own = con is None
    con = con or db_open()
    try:
        now = rt.now_s()
        return {s: _read(con, s, now) for s in signals(con)}
    finally:
        if own:
            con.close()
