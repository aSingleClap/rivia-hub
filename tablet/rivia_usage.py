# rivia_usage.py: what Layer 5, section 5.5's graphs read (emotion spec, Step 5.5). DRAFT, Nov 23, not installed.
#
# 5.7 owns the route (GET /app/progress/usage, app token, read-only) and the app card. This file gives it one
# function, usage_view(), plus the small store of Gemini calls the tablet makes, so the per-model graphs have data.
#
# What the tablet can't see: the phone talks to Gemini itself. Live calls on 3.8 Flash share the same per-project
# limits (Google applies rate limits per project, not per API key) but never reach the tablet, so the model counts
# here leave them out. "seen" says so in the view.
#
# Reads body.db only (5.6's goal_log, and model_calls below). Writes only model_calls, and only in record_call().
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

SEEN = ["tablet"]
SEEN_NOTE = ("Only Gemini calls the tablet makes are counted. The phone's Live calls on 3.8 Flash go straight to "
             "Google and never reach the tablet, but they count against the same per-project limits, so real use "
             "of 3.8 Flash is higher than shown.")


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


def load_calls(con, since):
    con.executescript(CALLS_SCHEMA)
    return [{"at": a, "model": m, "in": i, "out": o} for a, m, i, o in
            con.execute("select at, model, input_tokens, output_tokens from model_calls where at >= ? order by at",
                        (since,)).fetchall()]


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
            rpm, tpm = _peaks(by[m][d])
            models[m].append({"requests": len(by[m][d]), "peak_rpm": rpm, "peak_tpm": tpm})

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
