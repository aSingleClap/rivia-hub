# rivia_progress.py: Step 5 (Progress Trend) in one piece, in shadow mode (emotion spec, Step 5.6 merge).
#
# DRAFT (Nov 23, not installed). Shadow mode means: work it all out, write it to a log in body.db, and change nothing
# about her. Jordan reads the log, then decides. There is no switch to go live here on purpose.
#
# Jordan (Nov 23): her goal right now is "scheduling my day as accurately as possible". She only reports (Jordan,
# Nov 23: "Shouldn't she only be able to report issues not fix then yet?"). Nothing here moves a plan, sends
# anything, calls Gemini or calls a tool.
#
# What one tick does (the collector calls shadow_tick() right after 4.9's shadow dials):
#   1. Score finished days (5.1), once each, about half a day after the day ends (night-shift rides home come in
#      the next morning). Plan misses while the server was down are left out first (counted once, see below).
#   2. Turn each day into one gap number (5.2) and keep it in plan_days (a month).
#   3. Read the trend over the last two weeks of gaps (5.3): shrinking, growing or flat.
#   4. Turn the trend into a small mood pull and log it beside Step 4's pull (5.4), in the trend_mood table.
#
# Counted once (Jordan, Nov 22: "The problems shouldnt hit her twice"). When the server is down or the phone can't
# reach it, arrivals don't log, so her plan looks worse. Step 4 already counts that outage in her body. So:
#   - outage_windows() finds those stretches from Step 4's own once-a-minute readings (body.db reads): a gap of more
#     than OUTAGE_GAP_S with no reading (the server, which runs the collector, was down), or a reading where the
#     public health check failed (the phone couldn't get through the tunnel).
#   - 5.1 leaves out a pin miss whose arrival window overlaps one. A hit still counts: a ride that did log is real.
#   - 5.4's 0.25 ceiling on body + trend stays as the backstop.
# If the readings don't cover the whole day (body.db keeps 2 days, or the collector was off), outages are "unknown"
# and nothing is left out. That is written on the day's row so it shows in the review.
#
# What it never does: open emotion.db (5.4 refuses it too), open a sign-in file, ~/.rivia or auth.db, call Gemini
# or the network, or write anywhere but the body.db connection it is handed. The scheduler's files (approvals,
# places, core, scheduler) are opened read-only by 5.1 and never created.
#
# Goals (5.5): no code here runs a goal yet. Scheduling is parked, so there are no plan runs to limit, and the tool
# loop doesn't report its use yet. start_goal(), save_goal() and log_goal() are the storage 5.5 asked 5.6 for,
# ready for when it does. A goal's limits come from the app (5.7); RIVIA_GOAL_COST_USD can add a money cap on top.
import json
import os
from datetime import date, datetime, timedelta

import rivia_gap as gapmod
import rivia_goal_limits as goals
import rivia_plan_score as score
import rivia_progress_settings as psettings
import rivia_trend as trendmod
import rivia_trend_mood as mood

TZ = score.TZ
DAYS_KEEP = 31                 # plan_days rows kept (5.3 looks back 14 days)
SCORE_AFTER_S = 12 * 3600      # score a day this long after it ends: the night shift ride home lands next morning
SCORE_BACK_DAYS = 3            # look this many finished days back for any not yet scored (after a restart)
OUTAGE_GAP_S = 5 * 60          # no body reading for this long = the server was down (the collector reads each minute)
OUTAGE_TUNNEL_MIN = 50.0       # a reading's tunnel_unreachable at or over this = the phone couldn't get through
COVER_PAD_S = 3 * 3600         # outages must be known from this long before the day to this long after
                               # (5.1 matches rides up to 2 hours from the plan)
STANDING_GOOD_MAX = 0.20       # a flat trend sitting at or under this gap is "on track" (5.4 content); over is "off"
                               # (5.4 focused). Guess: Nov 16 to 18's real days read 0.07 to 0.20.
GOALS_KEEP_S = 30 * 86400
COST_ENV = "RIVIA_GOAL_COST_USD"

SCHEMA = """
create table if not exists plan_days(
    day text primary key, scored_at integer not null, scores text not null, gap real, gap_detail text not null,
    outage_info text not null, outages text not null, outage_minutes real);
create table if not exists goals(
    goal_id text primary key, updated integer not null, record text not null);
create table if not exists goal_log(
    at integer not null, goal_id text not null, action text not null, row text not null);
"""


def enabled():
    """On unless RIVIA_PLAN_SHADOW is 0/false/off/no. Same switch style as RIVIA_BODY_SHADOW."""
    return os.environ.get("RIVIA_PLAN_SHADOW", "1").strip().lower() not in ("0", "false", "off", "no")


def _guard(con):
    for _, name, path in con.execute("pragma database_list").fetchall():
        if name == "main" and path and path.replace("\\", "/").rsplit("/", 1)[-1] == "emotion.db":
            raise ValueError("Step 5 shadow rows never go in emotion.db")


def _day_bounds(day_s):
    d = date.fromisoformat(day_s)
    start = datetime(d.year, d.month, d.day, tzinfo=TZ)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def _today(now):
    return datetime.fromtimestamp(now, TZ).date()


# ---------- counted once: when was the server down? ----------

def outage_windows(con, lo, hi, now):
    """(start, end) stretches between lo and hi when the server was down or unreachable, from 4.2's reads table.
    Returns None when the readings don't cover lo..hi (nothing is known, so nothing should be left out)."""
    try:
        rows = con.execute("select at, signals from reads where at between ? and ? order by at",
                           (lo - OUTAGE_GAP_S, hi + OUTAGE_GAP_S)).fetchall()
        first = con.execute("select min(at) from reads").fetchone()[0]
    except Exception:  # noqa: BLE001  (no reads table yet)
        return None
    end = min(hi, now)
    if first is None or first > lo or not rows:
        return None
    out = []
    prev = None
    for at, sig in rows:
        if prev is not None and at - prev > OUTAGE_GAP_S:
            out.append((prev, at))
        try:
            tun = json.loads(sig or "{}").get("tunnel_unreachable")
        except (TypeError, ValueError):
            tun = None
        if isinstance(tun, (int, float)) and not isinstance(tun, bool) and tun >= OUTAGE_TUNNEL_MIN:
            out.append((at - 60, at))
        prev = at
    if prev is not None and end - prev > OUTAGE_GAP_S:
        out.append((prev, end))  # down since the last reading
    out = sorted((max(lo, s), min(hi, e)) for s, e in out if e > lo and s < hi)
    merged = []
    for s, e in out:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


# ---------- 5.1 + 5.2: score finished days, once ----------

def score_and_store(con, day_s, now, folder=None, values=None):
    """Score one finished day (5.1), leaving out outage misses, then its gap (5.2). Stores and returns the row.
    values: 5.7's settings in use (rivia_progress_settings.effective()["values"]); None reads them now."""
    _guard(con)
    if values is None:
        values = psettings.effective()["values"]
    con.executescript(SCHEMA)
    lo, hi = _day_bounds(day_s)
    outs = outage_windows(con, lo - COVER_PAD_S, hi + COVER_PAD_S, now)
    day = score.score_day(day_s, folder, outages=outs)
    g = gapmod.daily_gap(day, psettings.gap_settings(values))
    info = "unknown" if outs is None else ("none" if not outs else "left out")
    mins = None if outs is None else round(sum(e - s for s, e in outs) / 60.0, 1)
    con.execute("insert or replace into plan_days values(?,?,?,?,?,?,?,?)",
                (day_s, int(now), json.dumps(day, sort_keys=True, default=str), g["gap"],
                 json.dumps(g, sort_keys=True), info, json.dumps(outs or []), mins))
    return {"day": day_s, "gap": g["gap"], "outage_info": info, "outage_minutes": mins, "scores": day, "gap_detail": g}


def due_days(con, now):
    """Finished days (New York) that are old enough to score and not scored yet, oldest first."""
    con.executescript(SCHEMA)
    today = _today(now)
    out = []
    for back in range(SCORE_BACK_DAYS, 0, -1):
        d = (today - timedelta(days=back)).isoformat()
        if now < _day_bounds(d)[1] + SCORE_AFTER_S:
            continue
        if con.execute("select 1 from plan_days where day = ?", (d,)).fetchone() is None:
            out.append(d)
    return out


def gaps(con, now):
    """(day, gap) for the stored days, oldest first, for 5.3. Days with no gap stay holes (None)."""
    today = _today(now).isoformat()
    return [(d, g) for d, g in con.execute("select day, gap from plan_days where day < ? order by day", (today,))]


# ---------- 5.3 -> 5.4: names lined up ----------

def steadiness(tr):
    """How steady a flat week is, 0..1, for 5.4. 5.3 gives flat a strength of 0, but 5.4 reads a flat reading's
    strength as "how steady", so a flat week would never show as content or focused. Merge rule: level (slope inside
    5.3's flat band) counts fully, sliding to 0 at the band's edge, so a week that is flat only because the days zigzag
    too much to call is not "steady". Fewer than a full week of scored days counts for less."""
    spw = tr.get("slope_per_week")
    if spw is None:
        return 0.0
    level = max(0.0, 1.0 - abs(spw) / trendmod.FLAT_BAND)
    days = min(1.0, len(tr.get("days_used") or ()) / float(trendmod.SPAN_DAYS))
    return round(level * days, 3)


def reading_for_mood(tr, now, on_track=STANDING_GOOD_MAX):
    """5.3's trend() result in the shape 5.4 reads. 5.3 says `level` (where the gap sits); 5.4 wants `standing`.
    For flat, 5.4's strength is how steady the week was (see steadiness)."""
    level = tr.get("level")
    standing, strength = None, tr["strength"]
    if tr["direction"] == "flat" and level is not None:
        standing = "good" if level <= on_track else "bad"
        strength = steadiness(tr)
    return {"direction": tr["direction"], "strength": strength, "standing": standing, "at": int(now)}


def shadow_tick(con, now, body_share=(0.0, 0.0, 0.0), folder=None):
    """One Step 5 shadow reading at `now` (epoch seconds), in the body.db connection the collector holds.
    body_share is 4.9's dials row "share" from the same tick. Returns a summary dict. Changes nothing about her."""
    _guard(con)
    now = int(now)
    con.executescript(SCHEMA)
    # 5.7: Jordan's edits from the app, read once a tick. A broken edit file means the code's values (and a note).
    eff = psettings.effective()
    vals = eff["values"]
    scored = [score_and_store(con, d, now, folder, vals) for d in due_days(con, now)]
    con.execute("delete from plan_days where day < ?", ((_today(now) - timedelta(days=DAYS_KEEP)).isoformat(),))
    tr = trendmod.trend(gaps(con, now), today=_today(now))
    rd = reading_for_mood(tr, now, psettings.on_track_line(vals))
    row = mood.shadow_tick(con, now, rd, body_share=body_share, tune=psettings.mood_tune(vals))
    return {"at": now, "scored": [s["day"] for s in scored], "trend": tr, "mood": row,
            "settings": {"edited": eff["changed"], "problems": eff["problems"]}}


def recent_days(con, limit=14):
    """The newest stored days, newest first, for `rivia body plan` and the review. Read-only."""
    con.executescript(SCHEMA)
    keys = ("day", "gap", "outage_info", "outage_minutes", "gap_detail")
    out = []
    for r in con.execute("select day, gap, outage_info, outage_minutes, gap_detail from plan_days "
                         "order by day desc limit ?", (int(limit),)):
        d = dict(zip(keys, r))
        gd = json.loads(d.pop("gap_detail"))
        d.update(misses=gd.get("misses"), missing=gd.get("missing"), logged=gd.get("logged"), floored=gd.get("floored"))
        out.append(d)
    return out


# ---------- 5.5: where goal records live (not called by anything yet) ----------

def cost_cap():
    """Jordan's money cap per goal from RIVIA_GOAL_COST_USD, or None (the default: no money cap). Kept to 5.5's
    hard ceiling. A bad value means no cap and is reported, never guessed."""
    raw = os.environ.get(COST_ENV, "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    if not v > 0 or v != v:
        return None
    return min(v, goals.HARD_CEILING["cost_usd"])


def _tighter(a, b):
    """The tighter of two limits; None means no limit."""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def start_goal(con, goal_id, kind, *, instruction, stop_when, now, limits=None, from_jordan=False):
    """5.5's new_goal() with the limits Jordan set in the app (5.7), saved in body.db.
    - A kind Jordan switched off in the app is refused, and his lower try count is kept on the goal.
    - The goal's limits start from the app's values for its kind (the code's, where he changed nothing). Those come
      only from his app button, so they may be looser than the code's defaults; nothing passes 5.5's hard ceiling.
    - limits= from the caller can only make them tighter, unless from_jordan is True (his own message).
    - RIVIA_GOAL_COST_USD, if set, is a money cap on every goal; the tighter of it and the app's cap wins."""
    _guard(con)
    tries = None
    base = None
    if kind in goals.DEFAULT_LIMITS:  # unknown kinds go on to 5.5's own refusal
        on, tries = psettings.goal_switch_and_tries(kind)
        if not on:
            raise goals.GoalError("goal_off", "%s goals are switched off in the app" % kind)
        if tries >= goals.caution.BASE_MAX_ATTEMPTS:
            tries = None  # Jordan's count equals 4.7's: leave 4.7's rule to decide alone, exactly as before 5.7
        base = psettings.goal_limits(kind)
        cap = cost_cap()
        if cap is not None:
            base["cost_usd"] = _tighter(base["cost_usd"], cap)
        if limits is not None:
            if not isinstance(limits, dict) or not set(limits) <= set(goals.CAP_KEYS):
                raise goals.GoalError("bad_value", "limits may only name %s" % ", ".join(goals.CAP_KEYS))
            for k, v in limits.items():
                base[k] = v if from_jordan else _tighter(base[k], v)
        limits, from_jordan = base, True  # every looser value here came from Jordan (app button or his message)
    g = goals.new_goal(goal_id, kind, instruction=instruction, stop_when=stop_when, now=now,
                       limits=limits, from_jordan=from_jordan, max_tries=tries)
    save_goal(con, g, now)
    return g


def save_goal(con, goal, now):
    _guard(con)
    con.executescript(SCHEMA)
    con.execute("insert or replace into goals values(?,?,?)", (goal["goal_id"], int(now), json.dumps(goal)))
    con.execute("delete from goals where updated < ?", (int(now) - GOALS_KEEP_S,))


def load_goal(con, goal_id):
    con.executescript(SCHEMA)
    r = con.execute("select record from goals where goal_id = ?", (goal_id,)).fetchone()
    return json.loads(r[0]) if r else None


def log_goal(con, goal, decision, now):
    """5.5's log_row(), stored in goal_log (a month)."""
    _guard(con)
    con.executescript(SCHEMA)
    row = goals.log_row(goal, decision, now=now)
    con.execute("insert into goal_log values(?,?,?,?)", (int(now), goal["goal_id"], decision["action"],
                                                         json.dumps(row)))
    con.execute("delete from goal_log where at < ?", (int(now) - GOALS_KEEP_S,))
    return row


if __name__ == "__main__":  # on the tablet: python rivia_progress.py  (shows the last 7 scored days, read-only)
    import rivia_normal as normal
    c = normal.db_open()
    try:
        print(json.dumps(recent_days(c, 7), indent=2))
    finally:
        c.close()
