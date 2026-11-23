# rivia_trust_shadow.py: Step 6 in one piece, in shadow (emotion spec, Step 6.1 to 6.8 tied together). DRAFT.
#
# The collector calls shadow_tick() every reading, right after Step 5's tick (so the day Step 5 just scored is
# there to judge). Each tick:
#   1. 6.1 + 6.2: every day Step 5 has scored (plan_days) and Step 6 hasn't judged yet is judged once: her plan's
#      predictions, hit or miss, and whose miss. Step 5's outage stretches are reused, so an outage is chance here
#      too, never her miss (counted once). Stored in trust_predictions; the day goes in trust_days.
#   2. 6.3: if that day was a quiet good day (you said nothing, followed the plan, times matched), one quiet signal
#      at half weight (your "Counts half"). One per day, and a later thumb for that day takes it off.
#   3. 6.4 + 6.5: confidence by area from her counted misses and your thumbs down; double-check flags. A flag going
#      on or off is logged with its reason.
#   4. 6.6 + 6.7: trust from her accuracy; satisfaction from you; each unlock's state. "Ready" is logged as a
#      would-be suggestion (at most once a week per unlock); an unlock you switched on that falls under its lines is
#      logged as paused, and as resumed when it's back.
#   5. 6.8: the off switch is read first. While it's on, every unlock reads killed, and trust and confidence only
#      count what happened after it was last switched on.
#   6. One trust_state row when anything changed (or once an hour), kept a month, for the app card (6.9).
#
# Shadow only: everything goes in body.db. Nothing reaches her replies, her mood, or her tools. She never writes
# any of it (no tool imports this file; test_trust checks). RIVIA_TRUST_SHADOW=0 switches the tick off.
import json
import os

import rivia_area_confidence as aconf
import rivia_plan_score as score
import rivia_predict as predict
import rivia_satisfaction as sat
import rivia_trust as trust
import rivia_trust_settings as tsettings

KEEP_DAYS = 120          # judged predictions kept (trust fades by half every 30 days; 120 days is ~6% left)
STATE_KEEP_S = 31 * 86400
STATE_EVERY_S = 3600

SCHEMA = """
create table if not exists trust_predictions(
    pred_id text primary key, day text not null, kind text not null, area text not null, plan_id text,
    door text, made_at integer, said text not null, result text not null, owner text, why text not null,
    got text not null, counted integer not null, judged_at integer not null);
create index if not exists trust_predictions_day on trust_predictions(day);
create table if not exists trust_days(
    day text primary key, judged_at integer not null, predictions integer not null, summary text not null,
    quiet integer not null, quiet_why text not null);
"""


def enabled():
    """On unless RIVIA_TRUST_SHADOW is 0/false/off/no. Same switch style as RIVIA_PLAN_SHADOW."""
    return os.environ.get("RIVIA_TRUST_SHADOW", "1").strip().lower() not in ("0", "false", "off", "no")


def _guard(con):
    trust._guard(con)


def _schema(con):
    con.executescript(SCHEMA)
    con.executescript(sat.SCHEMA)
    con.executescript(trust.ACTIONS_SCHEMA)


def due_days(con):
    """Days Step 5 has scored and Step 6 hasn't judged, oldest first. No plan_days table yet: none."""
    try:
        rows = con.execute("select day, scores, outage_info, outages from plan_days "
                           "where day not in (select day from trust_days) order by day").fetchall()
    except Exception:  # noqa: BLE001  (Step 5 hasn't made its table yet)
        return []
    out = []
    for day, scores, info, outs in rows:
        try:
            sc = json.loads(scores or "{}")
        except ValueError:
            sc = {}
        try:
            o = None if info == "unknown" else [tuple(x) for x in json.loads(outs or "[]")]
        except ValueError:
            o = None
        out.append({"day": day, "plan_held": sc.get("plan_held"), "outages": o})
    return out


def judge_and_store(con, d, now, folder=None, values=None):
    """Judge one day (6.1 + 6.2), store it, and record a quiet good day (6.3). Returns the day's summary."""
    values = values if values is not None else tsettings.effective()["values"]
    verdicts = predict.judge(d["day"], score.read_day(d["day"], folder), d["outages"], tsettings.hit_lines(values))
    for v in verdicts:
        con.execute("insert or replace into trust_predictions values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (v["pred_id"], v["day"], v["kind"], v["area"], v.get("plan_id"), v.get("door"), v.get("made_at"),
                     json.dumps(v.get("said") or {}, sort_keys=True), v["result"], v["owner"], v["why"],
                     json.dumps(v.get("got") or {}, sort_keys=True, default=str), int(v["counted"]), int(now)))
    quiet, why = sat.is_quiet_good(verdicts, d["plan_held"], sat.said_anything(con, d["day"]))
    if quiet:
        try:
            sat.add(con, "quiet", 0.5, door="judge", day=d["day"], now=now, area="chat", note=why)
        except sat.FeedbackError as e:
            if e.code != "already":
                raise
    summ = predict.summary(verdicts)
    con.execute("insert or replace into trust_days values(?,?,?,?,?,?)",
                (d["day"], int(now), len(verdicts), json.dumps(summ, sort_keys=True), int(quiet), why))
    return {"day": d["day"], "predictions": len(verdicts), "summary": summ, "quiet": quiet, "quiet_why": why}


def stored_verdicts(con, since=None):
    q = ("select pred_id, day, kind, area, result, owner, why, counted, judged_at from trust_predictions")
    args = ()
    if since is not None:
        q += " where judged_at >= ?"
        args = (int(since),)
    keys = ("pred_id", "day", "kind", "area", "result", "owner", "why", "counted", "judged_at")
    return [dict(zip(keys, r), counted=bool(r[7])) for r in con.execute(q + " order by day, pred_id", args)]


def last_state(con):
    try:
        r = con.execute("select at, row from trust_state order by at desc limit 1").fetchone()
    except Exception:  # noqa: BLE001
        return None
    return json.loads(r[1]) if r else None


def _same(a, b):
    if a is None or b is None:
        return False
    keep = lambda r: {k: v for k, v in r.items() if k not in ("at", "judged", "settings")}  # noqa: E731
    return keep(a) == keep(b)


def shadow_tick(con, now, folder=None):
    """One Step 6 reading at `now` (epoch seconds) in the collector's body.db connection. Returns the state row.
    Changes nothing about her."""
    _guard(con)
    now = int(now)
    _schema(con)
    eff = tsettings.effective()
    vals = eff["values"]
    kill = trust.kill_state()
    since = kill["since"]

    judged = [judge_and_store(con, d, now, folder, vals) for d in due_days(con)]
    con.execute("delete from trust_predictions where judged_at < ?", (now - KEEP_DAYS * 86400,))
    con.execute("delete from trust_days where judged_at < ?", (now - KEEP_DAYS * 86400,))

    verdicts = stored_verdicts(con)
    signals = sat.rows(con)
    s = sat.satisfaction(signals, now, since=since, **tsettings.sat_tune(vals))
    conf = aconf.confidence(verdicts, sat.corrections(signals, since), now, tsettings.area_tune(vals), since)
    prev = last_state(con) or {}
    prev_flags = [f["area"] for f in prev.get("flags", [])]
    fl = aconf.flags(conf, prev_flags, tsettings.area_tune(vals))
    for f in fl:
        if f["area"] not in prev_flags:
            trust.log_action(con, "flag_on", f["area"], "%s (sure %.2f)" % (f["say"], f["value"]), "code", now)
    for a in prev_flags:
        if a not in [f["area"] for f in fl]:
            trust.log_action(con, "flag_off", a, "back over the clear line", "code", now)

    tr = trust.trust(verdicts, now, tsettings.trust_tune(vals), since)
    on, on_problem = trust.switched_on()
    lad = trust.ladder(tr, s, tsettings.unlock_lines(vals), on, kill["killed"])
    prev_lad = prev.get("ladder") or {}
    for uid, u in lad.items():
        before = (prev_lad.get(uid) or {}).get("state")
        if u["state"] == "ready":
            last = trust.last_brought_up(con, uid)
            if last is None or now - last >= trust.SUGGEST_EVERY_DAYS * 86400:
                trust.log_action(con, "would_bring_up", uid, trust.bring_up_words(uid, tr, s), "code", now)
        elif u["state"] == "paused" and before != "paused":
            under = [k for k in ("trust", "satisfaction") if not u["met"][k]]
            trust.log_action(con, "paused", uid, "%s fell under its line; it comes back by itself once it's over "
                             "again" % " and ".join(under), "code", now)
        elif u["state"] == "active" and before == "paused":
            trust.log_action(con, "resumed", uid, "back over its lines", "code", now)

    problems = list(eff["problems"])
    if on_problem:
        problems.append(on_problem + ", so nothing is switched on")
    if kill["problem"]:
        problems.append(kill["problem"] + ", so the off switch reads ON")
    row = {
        "at": now, "judged": [j["day"] for j in judged], "trust": tr, "satisfaction": s,
        "confidence": conf, "flags": fl, "caution": aconf.caution_lines(fl),
        "ladder": {k: {"state": v["state"], "met": v["met"], "lines": v["lines"], "switched_on": v["switched_on"]}
                   for k, v in lad.items()},
        "killed": kill["killed"], "counting_since": since,
        "settings": {"edited": eff["changed"]}, "problems": problems, "shadow": True,
    }
    if not _same(row, prev) or now - (prev.get("at") or 0) >= STATE_EVERY_S:
        con.execute("insert or replace into trust_state values(?,?)", (now, json.dumps(row, sort_keys=True,
                                                                                         default=str)))
        con.execute("delete from trust_state where at < ?", (now - STATE_KEEP_S,))
    return row


def recent_days(con, limit=14):
    """The newest judged days, newest first, for `rivia trust` and the app card. Read-only."""
    _schema(con)
    out = []
    for day, n, summ, quiet, why in con.execute("select day, predictions, summary, quiet, quiet_why from trust_days "
                                                "order by day desc limit ?", (int(limit),)):
        out.append({"day": day, "predictions": n, "summary": json.loads(summ), "quiet": bool(quiet),
                    "quiet_why": why})
    return out


def day_detail(con, day_s):
    """Every judged prediction for one day, with its plain reason. Read-only."""
    _schema(con)
    return [v for v in stored_verdicts(con) if v["day"] == day_s]


def main(argv):
    """`rivia trust` (show), `rivia trust kill` / `rivia trust unkill` (the off switch, from the tablet's terminal)."""
    import sys
    import rivia_normal as normal
    c = normal.db_open()
    try:
        if argv and argv[0] in ("kill", "unkill"):
            k = trust.set_kill(argv[0] == "kill", door="termux", note=" ".join(argv[1:]) or None, con=c)
            print("off switch is %s" % ("ON: every unlock off, trust starts over" if k["killed"] else "cleared"))
            return 0
        if argv:
            print("use: rivia trust | rivia trust kill [note] | rivia trust unkill [note]", file=sys.stderr)
            return 2
        print(json.dumps({"state": last_state(c), "days": recent_days(c, 7), "actions": trust.actions(c, 10),
                          "kill": trust.kill_state()}, indent=1, default=str))
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
