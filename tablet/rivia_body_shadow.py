# rivia_body_shadow.py: the body layer in shadow mode (emotion spec, Step 4.9).
#
# DRAFT (Nov 23, not installed). Shadow mode means: work out everything the body layer would do, write it to a log,
# and change nothing about her. Jordan reviews a few days of these rows before the body is allowed to move her mood.
#
# Each collector tick (4.2) calls shadow_tick() once, right after the new readings are stored. It reads:
#   4.3 her normal  -> 4.4 stress (slowness and load)  -> 4.5 confidence (things breaking)
#   -> 4.7 caution (tries allowed, pause)  -> 4.6 the capped pull on mood
# and writes one row to the `dials` table in body.db.
#
# What it never does: write emotion.db (her emotion, mood or baseline), call Gemini, open a sign-in file, or touch the
# network. 4.6's tick() is not called, because even its dry run opens emotion.db. Instead the capped pull is stepped
# here with 4.6's own pure step_toward(), from the last shadow row, so the log shows how fast the pull would grow and
# ease. That share is an estimate: the real 4.6 also fades the body's share with mood's half-life, which needs the
# live mood log. There is no switch to go live here on purpose. Going live is a separate change, on Jordan's yes.
import json
import math

import rivia_body_cap as cap
import rivia_caution as caution
import rivia_confidence as conf
import rivia_emotion as emo
import rivia_normal as normal
import rivia_store as store
import rivia_stress as stress

STATE_KEY = "shadow.state"
DIALS_KEEP_S = 7 * 86400   # a week of rows: enough for the review, about 4 MB at one row a minute

DIALS_SCHEMA = """
create table if not exists dials(
    at integer primary key, stress real, confidence real, double_check integer, pause integer,
    max_attempts integer, target text, share text, would_read text, stress_cause text, blind_spots text,
    unlearned text, error text);
"""


def _load_state(con, now):
    raw = store.get_meta(con, STATE_KEY)
    try:
        st = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        st = None
    if not isinstance(st, dict) or not isinstance(st.get("stress"), dict):
        st = {"stress": stress.new_state(now), "double_check": False, "pause": False,
              "share": [0.0, 0.0, 0.0], "at": None}
    return st


def _r(x, n=4):
    return None if x is None else round(float(x), n)


def shadow_tick(con, now):
    """One shadow reading at time `now` (epoch seconds), using the samples 4.2 just stored in body.db.
    Returns the row as a dict. Raises only on a broken body.db; the collector catches that."""
    con.executescript(DIALS_SCHEMA)
    st = _load_state(con, now)
    readings = normal.normals(con=con)

    # 4.4: slowness and load only. A signal still in warm-up (deviation None) is no news.
    devs = {n: r.get("deviation") for n, r in readings.items() if n in stress.SIGNALS}
    s_state = stress.advance(st["stress"], devs, now)
    s = stress.stress(s_state)

    # 4.5: things breaking. It skips signals that aren't its own.
    c_read = conf.reading(readings, prev_double_check=bool(st.get("double_check")))
    c = c_read["confidence"]

    # 4.7: how careful she'd be right now.
    a = caution.assess(s, c, prev_pause=bool(st.get("pause")))

    # 4.6: the capped pull, stepped from the last shadow share no faster than 4.6 allows.
    goal = cap.target(stress.pull(s), c_read["pull"])
    since = cap.BODY_MAX_STEP_S if st.get("at") is None else min(cap.BODY_MAX_STEP_S, max(0, now - st["at"]))
    share = cap.step_toward(tuple(st["share"]), goal, since)
    lab = emo.label(*share)   # on a neutral baseline; her real baseline (B1) isn't measured yet
    would = "neutral" if lab["name"] == "neutral" else "%s %s" % (lab["intensity"], lab["name"])

    row = {
        "at": int(now), "stress": _r(s), "confidence": _r(c), "double_check": c_read["double_check"],
        "pause": a["pause"], "max_attempts": a["max_attempts"],
        "target": [_r(x) for x in goal], "share": [_r(x) for x in share], "would_read": would,
        "stress_cause": stress.cause(s_state),
        "blind_spots": [b["signal"] for b in c_read["blind_spots"]],
        "unlearned": c_read["unlearned"],
    }
    con.execute("insert or replace into dials values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["at"], row["stress"], row["confidence"], int(row["double_check"]), int(row["pause"]),
                 row["max_attempts"], json.dumps(row["target"]), json.dumps(row["share"]), row["would_read"],
                 row["stress_cause"], json.dumps(row["blind_spots"]), json.dumps(row["unlearned"]), None))
    con.execute("delete from dials where at < ?", (int(now) - DIALS_KEEP_S,))
    keep = {k: v for k, v in s_state.items() if k in ("load", "wear", "raw", "updated")}
    store.set_meta(con, STATE_KEY, json.dumps({
        "stress": keep, "double_check": c_read["double_check"], "pause": a["pause"],
        "share": list(share), "at": int(now)}))
    return row


def recent(limit=20, con=None):
    """The newest shadow rows, newest first, for `rivia body dials` and the review. Read-only."""
    own = con is None
    con = con or normal.db_open()
    try:
        con.executescript(DIALS_SCHEMA)
        rows = con.execute("select at, stress, confidence, double_check, pause, max_attempts, target, share, "
                           "would_read, stress_cause, blind_spots, unlearned from dials order by at desc limit ?",
                           (int(limit),)).fetchall()
    finally:
        if own:
            con.close()
    keys = ("at", "stress", "confidence", "double_check", "pause", "max_attempts", "target", "share",
            "would_read", "stress_cause", "blind_spots", "unlearned")
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        for k in ("target", "share", "blind_spots", "unlearned"):
            d[k] = json.loads(d[k]) if d[k] else []
        d["double_check"], d["pause"] = bool(d["double_check"]), bool(d["pause"])
        out.append(d)
    return out


if __name__ == "__main__":  # on the tablet: python rivia_body_shadow.py  (shows the last 5 rows)
    print(json.dumps(recent(5), indent=2))
