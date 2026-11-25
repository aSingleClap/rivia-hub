# rivia_usage.py: what Layer 5, section 5.5's graphs read (emotion spec, Step 5.5). DRAFT, Nov 23, not installed.
#
# 5.7 owns the route (GET /app/progress/usage, app token, read-only) and the app card. This file gives it one
# function, usage_view(), plus the small store of Gemini calls the tablet makes, so the per-model graphs have data.
#
# The phone talks to Gemini itself (every Rivia chat turn and Live). Since Nov 25 (Jordan: "I want the flash usage
# tracked", no limit) the app reports each of those calls' token counts, never any words, to POST
# /app/progress/usage; they land in phone_calls below, keyed by the app's own call id so a resend counts once.
# load_calls() returns both tables, so the graphs, the gauges and any limit Jordan sets count the phone's calls too
# (Google applies rate limits per project, not per API key).
#
# Reads body.db only (5.6's goal_log, model_calls and phone_calls). Writes only model_calls (record_call()) and
# phone_calls (record_phone_calls()).
# Never emotion.db, a sign-in file or the network.
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import rivia_goal_limits as gl
import rivia_model_limits as ml

NY = ZoneInfo("America/New_York")        # goal days follow Jordan's days
KEEP_S = ml.KEEP_DAYS * 86400
MAX_DAYS = ml.KEEP_DAYS

CALLS_SCHEMA = """
create table if not exists model_calls(
    at real not null, model text not null, input_tokens integer not null, output_tokens integer not null);
"""

PHONE_SCHEMA = """
create table if not exists phone_calls(
    call_id text primary key, at real not null, model text not null, input_tokens integer not null,
    output_tokens integer not null, kind text not null);
"""

SEEN = ["tablet", "phone"]
SEEN_NOTE = ("Counts the Gemini calls the tablet makes and the calls the phone app reports after each chat or Live "
             "turn. A phone call that couldn't reach the tablet shows up once the app reaches it again. Arrival "
             "speech (shelved) isn't counted.")

PHONE_MAX_BATCH = 200          # calls in one POST
PHONE_MAX_TOKENS = 2_000_000   # per number in one call; anything bigger is a bad value
PHONE_FUTURE_S = 300           # a call stamped more than 5 minutes ahead of the tablet's clock is refused
PHONE_KINDS = ("rivia_text", "rivia_voice", "lab_text", "lab_voice", "other")
_ID_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:")
_MODEL_OK = set("abcdefghijklmnopqrstuvwxyz0123456789-._")


class UsageError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _guard(con):
    for _, name, path in con.execute("pragma database_list").fetchall():
        if name == "main" and path and path.replace("\\", "/").rsplit("/", 1)[-1] == "emotion.db":
            raise UsageError("wrong_db", "usage rows never go in emotion.db")


def record_call(con, model, at, *, input_tokens, output_tokens=0):
    """Store one Gemini call the tablet made (thinking tokens count as output). Keeps a month."""
    _guard(con)
    c = ml.call(model, at, input_tokens=input_tokens, output_tokens=output_tokens)
    con.executescript(CALLS_SCHEMA)
    con.execute("insert into model_calls values(?,?,?,?)", (c["at"], c["model"], c["in"], c["out"]))
    con.execute("delete from model_calls where at < ?", (c["at"] - KEEP_S,))
    return c


def _phone_call(raw, now):
    """One call from the app's POST, checked. Returns the row or raises UsageError. Numbers only, never words."""
    if not isinstance(raw, dict) or set(raw) - {"id", "at", "model", "in", "out", "kind"}:
        raise UsageError("bad_value", "each call is {id, at, model, in, out, kind}")
    cid, model, kind = raw.get("id"), raw.get("model"), raw.get("kind", "other")
    if not isinstance(cid, str) or not 1 <= len(cid) <= 80 or set(cid) - _ID_OK:
        raise UsageError("bad_value", "id must be 1 to 80 plain characters")
    if not isinstance(model, str) or not 1 <= len(model) <= 64 or set(model) - _MODEL_OK:
        raise UsageError("bad_value", "model must be a model name")
    if kind not in PHONE_KINDS:
        raise UsageError("bad_value", "kind must be one of " + ", ".join(PHONE_KINDS))
    for n in ("in", "out"):
        v = raw.get(n, 0)
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= PHONE_MAX_TOKENS:
            raise UsageError("bad_value", "%s must be a whole number, 0 to %d" % (n, PHONE_MAX_TOKENS))
    at = raw.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or at != at:
        raise UsageError("bad_value", "at must be epoch seconds")
    if at > now + PHONE_FUTURE_S:
        raise UsageError("bad_value", "at is in the future")
    c = ml.call(model, at, input_tokens=raw.get("in", 0), output_tokens=raw.get("out", 0))
    return (cid, c["at"], c["model"], c["in"], c["out"], kind)


def record_phone_calls(con, calls, now):
    """Store the calls the phone app reports (POST /app/progress/usage). All or nothing: one bad call saves none.
    A call id already stored is skipped, so the app can resend safely. Calls older than KEEP_DAYS are skipped and
    old rows pruned. Returns {"added": n, "already": n, "too_old": n}."""
    _guard(con)
    if not isinstance(calls, list) or not 1 <= len(calls) <= PHONE_MAX_BATCH:
        raise UsageError("bad_value", "calls must be a list of 1 to %d" % PHONE_MAX_BATCH)
    rows = [_phone_call(c, now) for c in calls]
    con.executescript(PHONE_SCHEMA)
    added = already = too_old = 0
    for r in rows:
        if r[1] <= now - KEEP_S:
            too_old += 1
            continue
        cur = con.execute("insert or ignore into phone_calls values(?,?,?,?,?,?)", r)
        if cur.rowcount:
            added += 1
        else:
            already += 1
    con.execute("delete from phone_calls where at < ?", (now - KEEP_S,))
    con.commit()
    return {"added": added, "already": already, "too_old": too_old}


def load_calls(con, since):
    """Every Gemini call since `since`: the tablet's own (model_calls) and the ones the phone reported (phone_calls),
    in time order. Each is {at, model, in, out, from}."""
    con.executescript(CALLS_SCHEMA)
    con.executescript(PHONE_SCHEMA)
    rows = [{"at": a, "model": m, "in": i, "out": o, "from": "tablet"} for a, m, i, o in
            con.execute("select at, model, input_tokens, output_tokens from model_calls where at >= ?",
                        (since,)).fetchall()]
    rows += [{"at": a, "model": m, "in": i, "out": o, "from": "phone"} for a, m, i, o in
             con.execute("select at, model, input_tokens, output_tokens from phone_calls where at >= ?",
                         (since,)).fetchall()]
    rows.sort(key=lambda c: c["at"])
    return rows


def load_goal_rows(con, since):
    """5.6's goal_log lines (5.5's log_row()). No table yet means no goals yet."""
    try:
        rows = con.execute("select row from goal_log where at >= ? order by at", (int(since),)).fetchall()
    except Exception:  # noqa: BLE001  (no goal_log table yet)
        return []
    out = []
    for (raw,) in rows:
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


def _labels(now, days, tz):
    today = datetime.fromtimestamp(now, tz).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


def _peaks(calls):
    """The most requests and the most input tokens in any 60-second stretch."""
    calls = sorted(calls, key=lambda c: c["at"])
    best_n = best_t = 0
    lo = 0
    tokens = 0
    for hi, c in enumerate(calls):
        tokens += c["in"]
        while calls[lo]["at"] <= c["at"] - 60:
            tokens -= calls[lo]["in"]
            lo += 1
        best_n = max(best_n, hi - lo + 1)
        best_t = max(best_t, tokens)
    return best_n, best_t


def build(goal_rows, calls, now, days=14):
    """The view from rows and calls already loaded. Pure."""
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS:
        raise UsageError("bad_value", "days must be 1 to %d" % MAX_DAYS)

    # Goals, by New York day, each goal once at its last log line.
    days_ny = _labels(now, days, NY)
    last = {}
    for r in goal_rows:
        if r["at"] <= now and (r["goal_id"] not in last or r["at"] >= last[r["goal_id"]]["at"]):
            last[r["goal_id"]] = r
    stopped = {g for g, r in last.items()
               if gl.would_have_stopped(r) or any(x["goal_id"] == g and x.get("would_stop") for x in goal_rows)}
    bucket = {k: {d: [] for d in days_ny} for k in gl.DEFAULT_LIMITS}
    for g, r in last.items():
        d = datetime.fromtimestamp(r["at"], NY).date().isoformat()
        if r["kind"] in bucket and d in bucket[r["kind"]]:
            bucket[r["kind"]][d].append((r["spent"]["tokens"], g in stopped))
    kinds = {}
    for k, per_day in bucket.items():
        kinds[k] = []
        for d in days_ny:
            xs = per_day[d]
            toks = [x[0] for x in xs]
            kinds[k].append({"goals": len(xs), "tokens_avg": round(sum(toks) / len(toks)) if toks else 0,
                             "tokens_max": max(toks) if toks else 0, "would_stop": sum(1 for x in xs if x[1])})

    # Models, by Pacific day (Google's RPD resets at midnight Pacific).
    days_pt = _labels(now, days, ml.PACIFIC)
    names = list(ml.MODELS) + sorted({c["model"] for c in calls} - set(ml.MODELS))
    by = {m: {d: [] for d in days_pt} for m in names}
    for c in calls:
        if c["at"] > now:
            continue
        d = datetime.fromtimestamp(c["at"], ml.PACIFIC).date().isoformat()
        if d in by[c["model"]]:
            by[c["model"]][d].append(c)
    models = {}
    for m in names:
        models[m] = []
        for d in days_pt:
            xs = by[m][d]
            rpm, tpm = _peaks(xs)
            models[m].append({"requests": len(xs), "peak_rpm": rpm, "peak_tpm": tpm,
                              "tokens_in": sum(c["in"] for c in xs), "tokens_out": sum(c["out"] for c in xs),
                              "phone_requests": sum(1 for c in xs if c.get("from") == "phone")})

    return {"mode": gl.BUDGET_MODE, "days": days_ny, "kinds": kinds, "model_days": days_pt, "models": models,
            "seen": list(SEEN), "seen_note": SEEN_NOTE}


def usage_view(now=None, days=14, con=None):
    """For 5.7's GET /app/progress/usage. Reads body.db (goal_log and model_calls); never writes it. Model limits
    and gauges aren't in here: 5.7's settings hold the limits, and rivia_model_limits.usage() gives the gauges."""
    import time
    now = time.time() if now is None else now
    own = con is None
    if own:
        import rivia_normal as normal
        con = normal.db_open()
    try:
        since = now - (days + 1) * 86400
        return build(load_goal_rows(con, since), load_calls(con, since), now, days)
    finally:
        if own:
            con.close()
