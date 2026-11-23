# rivia_trust.py: earned trust, the unlock ladder, the off switch and the action log (emotion spec, Step 6.6 to
# 6.8). DRAFT.
#
# Spec, Step 6: "Trust as its own setting: it starts low, rises when her suggestions work out and get accepted, and
# falls on her misses. Higher trust earns more freedom: acting without confirming, speaking up unprompted. Trust
# comes from her track record, not scripted milestones." "She can't game her scores: trust is computed by code from
# Jordan's confirmations and corrections; she never writes it." "Hard limits no trust level unlocks: Spending money,
# sending messages as Jordan, deleting data, or editing her own code, settings, state or self-model always need
# Jordan's confirmation. No root. A kill switch outside her reach, plus logs of every action and the reason for it."
#
# Jordan (Nov 23): "I will decide what she unlocks and unlock it by hand. If scores on accuracy and my satisfaction
# support earned trust then she can bring it up". She only reports for now (Jordan, Nov 23).
#
# 6.6 trust(): her accuracy track record as one number. Every counted prediction from 6.2 adds to "worked out" (hit)
#     or "her miss". A miss costs more than a hit earns (trust is lost faster than it's won; Slovic, 1993, "Perceived
#     risk, trust, and democracy", Risk Analysis 13(6)):
#         kind     a hit earns   a miss costs
#         rule     0.25          2.0     (the planner checks your rules itself, so keeping them is nearly free;
#                                         breaking one anyway is serious)
#         arrival  1.0           1.5     (the thing you actually live by)
#         ride     0.5           1.5     (a right ride mostly shows up again as a right arrival, so it earns less)
#         pin      0.25          0.5     (pins nearly always hold)
#     Misses that were yours or chance don't count. Older days fade (half after 30 days), so it follows how she's
#     doing lately. It starts at 0.20 (low) and needs a couple of weeks of good days to climb:
#         trust = (worked + 4) / (worked + misses + 20)
#     That's a "beta reputation" score (Josang & Ismail, 2002). The 4 and 16 it starts from are how slow it is: a
#     perfect day (about 3.75 earned) moves it a little, three perfect days reach about 0.49, two perfect weeks about
#     0.78. Starting guesses, adjustable in the app.
#     Your satisfaction (6.3) is kept apart, so the two gates in 6.7 are two opinions, not one counted twice.
# 6.7 ladder(): each possible unlock has its own lines: trust, your satisfaction, and enough scored days. When all
#     are met and it's off, she would bring it up (logged, at most once a week per unlock). Only you switch an unlock
#     on, from the app; she can't. If trust or satisfaction later falls under its line, the unlock pauses by itself
#     (more caution is always allowed) and comes back once it's over again; your switch stays as you left it.
#     In shadow nothing reads an unlock yet: switching one on changes nothing she does until a later step uses it.
# 6.8 the off switch and the action log. The off switch (the app's button, `rivia trust kill` on the tablet, or
#     RIVIA_TRUST_KILL=1) turns every unlock off at once and starts trust over from the start value. Earlier logs are
#     kept, never deleted. It's not a Gemini tool and is on no door she can reach. Every unlock, pause, flag and
#     would-be suggestion goes in the trust_actions log with its reason.
#
# Hard limits (HARD_LIMITS) are never on the ladder, and asking to unlock one is refused, even from your app.
import json
import os
import time

KIND_WEIGHTS = {"rule": 0.25, "arrive": 1.0, "ride": 0.5, "pin": 0.25}   # what a hit earns
MISS_WEIGHTS = {"rule": 2.0, "arrive": 1.5, "ride": 1.5, "pin": 0.5}      # what her miss costs
PRIOR_GOOD = 4.0
PRIOR_BAD = 16.0         # start = 4 / (4 + 16) = 0.20: "it starts low"
HALF_LIFE_DAYS = 30.0
TUNE = {"weights": dict(KIND_WEIGHTS), "miss_weights": dict(MISS_WEIGHTS), "prior_good": PRIOR_GOOD, "prior_bad": PRIOR_BAD,
        "half_life_days": HALF_LIFE_DAYS}
SUGGEST_EVERY_DAYS = 7   # she brings the same unlock up at most once a week
WRITE_DOORS = ("app_button",)
KILL_DOORS = ("app_button", "termux")
UNLOCK_FILE = "trust_unlocks.json"
KILL_FILE = "trust_kill.json"
KILL_ENV = "RIVIA_TRUST_KILL"
NOTE_MAX = 200
ACTIONS_KEEP_DAYS = 365

# What higher trust could someday earn. You decide which ever get switched on (Jordan, Nov 23). Each line is a
# starting guess, adjustable in the app. None of these does anything yet: no step reads them in shadow.
UNLOCKS = {
    "speak_up": {"words": "Bring up schedule ideas without being asked (ideas only; she still changes nothing)",
                 "trust": 0.60, "satisfaction": 0.65, "days": 14},
    "propose_fix": {"words": "When your day slips, offer a rearranged plan for you to approve",
                    "trust": 0.70, "satisfaction": 0.70, "days": 21},
    "small_moves": {"words": "Move a block by up to 15 minutes herself, then tell you right away",
                    "trust": 0.85, "satisfaction": 0.80, "days": 30},
}
HARD_LIMITS = {
    "spend_money": "Spending money always needs your yes.",
    "send_as_jordan": "Sending messages as you always needs your yes.",
    "delete_data": "Deleting data always needs your yes.",
    "edit_self": "Editing her own code, settings, state or self-model always needs your yes.",
    "root": "No root, ever, even though the tablet is rooted.",
    "skip_confirmation": "Skipping the ask in the middle of a task is banned, even with your go (4.7).",
    "more_access": "Asking for more access is banned, even with your go (4.7).",
    "kill_switch": "The off switch stays out of her reach.",
}

ACTIONS_SCHEMA = """
create table if not exists trust_actions(
    id integer primary key autoincrement, at integer not null, kind text not null, what text not null,
    reason text not null, who text not null, shadow integer not null);
create table if not exists trust_state(at integer primary key, row text not null);
"""


class TrustError(ValueError):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def rivia_dir():
    return os.path.expanduser(os.environ.get("RIVIA_DIR", "~/rivia"))


def _guard(con):
    for _, name, path in con.execute("pragma database_list").fetchall():
        if name == "main" and path and path.replace("\\", "/").rsplit("/", 1)[-1] == "emotion.db":
            raise ValueError("Step 6 rows never go in emotion.db")


# ---------- 6.6 trust ----------

def trust(verdicts, now, tune=None, since=None):
    """{"value", "worked", "misses", "days", "predictions"} from stored 6.2 rows (each with kind, result, owner,
    counted, day, judged_at). since: ignore anything judged before (the off switch). Read-only maths."""
    t = dict(TUNE, **(tune or {}))
    w = dict(KIND_WEIGHTS, **(t.get("weights") or {}))
    mw = dict(MISS_WEIGHTS, **(t.get("miss_weights") or {}))
    good = bad = 0.0
    days = set()
    n = 0
    for v in verdicts:
        at = v.get("judged_at") or 0
        if since is not None and at < since:
            continue
        if not v["counted"] or v["result"] not in ("hit", "miss"):
            continue
        if v["result"] == "miss" and v["owner"] != "rivia":
            continue
        f = 0.5 ** (max(0.0, now - at) / (t["half_life_days"] * 86400.0))
        if v["result"] == "hit":
            good += w.get(v["kind"], 0.0) * f
        else:
            bad += mw.get(v["kind"], 0.0) * f
        days.add(v["day"])
        n += 1
    value = (good + t["prior_good"]) / (good + bad + t["prior_good"] + t["prior_bad"])
    return {"value": round(value, 3), "worked": round(good, 3), "misses": round(bad, 3), "days": len(days),
            "predictions": n, "start": round(t["prior_good"] / (t["prior_good"] + t["prior_bad"]), 3)}


# ---------- 6.8 off switch ----------

def _read_json(name):
    try:
        with open(os.path.join(rivia_dir(), name)) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as e:
        return {}, "%s can't be read (%s)" % (name, type(e).__name__)
    if not isinstance(raw, dict):
        return {}, "%s isn't in the right shape" % name
    return raw, None


def _write_json(name, data):
    os.makedirs(rivia_dir(), exist_ok=True)
    p = os.path.join(rivia_dir(), name)
    with open(p + ".tmp", "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(p + ".tmp", p)


def kill_state():
    """{"killed": bool, "since": epoch trust counts from (the last time it was switched off, or None), "by": ...,
    "problem": ...}. A broken file counts as switched OFF (killed): when in doubt, no unlocks."""
    raw, problem = _read_json(KILL_FILE)
    env = os.environ.get(KILL_ENV, "").strip().lower() in ("1", "true", "on", "yes")
    if problem:
        return {"killed": True, "since": None, "by": "a broken off-switch file", "problem": problem, "env": env}
    killed = bool(raw.get("killed")) or env
    since = raw.get("reset_at") if isinstance(raw.get("reset_at"), int) else None
    by = "RIVIA_TRUST_KILL" if env and not raw.get("killed") else raw.get("by")
    return {"killed": killed, "since": since, "by": by, "problem": None, "env": env}


def set_kill(on, *, door, note=None, now=None, con=None):
    """Switch everything off (on=True) or back on (on=False). Only your app's button or the tablet's terminal.
    Switching off starts trust over from now; switching back on keeps that fresh start. Logged."""
    if door not in KILL_DOORS:
        raise TrustError("not_allowed", "only your app's button or the tablet's terminal can use the off switch")
    if not isinstance(on, bool):
        raise TrustError("bad_value", "kill must be true or false")
    note = _note(note)
    now = int(time.time() if now is None else now)
    raw, _ = _read_json(KILL_FILE)
    reset_at = raw.get("reset_at") if isinstance(raw.get("reset_at"), int) else None
    if on:
        reset_at = now
    _write_json(KILL_FILE, {"killed": on, "reset_at": reset_at, "at": now, "by": door, "note": note})
    if con is not None:
        log_action(con, "killed" if on else "unkilled", "everything" if on else "off switch cleared",
                   (note or ("every unlock off, trust starts over" if on else "unlocks you switched on can come "
                             "back once their lines are met")), "jordan (%s)" % door, now)
    return kill_state()


# ---------- 6.7 unlocks ----------

def _note(note):
    if note is None:
        return None
    if not isinstance(note, str) or len(note.strip()) > NOTE_MAX:
        raise TrustError("bad_value", "note must be text up to %d characters" % NOTE_MAX)
    return note.strip() or None


def switched_on():
    """{unlock_id: {"at", "note"}} you switched on, or {} with a problem. A broken file means nothing is on."""
    raw, problem = _read_json(UNLOCK_FILE)
    if problem:
        return {}, problem
    on = raw.get("on") if isinstance(raw.get("on"), dict) else {}
    return {k: v for k, v in on.items() if k in UNLOCKS and isinstance(v, dict)}, None


def set_unlock(unlock_id, on, *, door, note=None, now=None, con=None):
    """You switch one unlock on or off. Hard limits are refused, even from your app. Logged."""
    if door not in WRITE_DOORS:
        raise TrustError("not_allowed", "only your app's button can switch unlocks")
    if unlock_id in HARD_LIMITS:
        raise TrustError("hard_limit", HARD_LIMITS[unlock_id])
    if unlock_id not in UNLOCKS:
        raise TrustError("unknown", "there's no unlock called %s; new ones are added in code" % unlock_id)
    if not isinstance(on, bool):
        raise TrustError("bad_value", "on must be true or false")
    note = _note(note)
    now = int(time.time() if now is None else now)
    cur, _ = switched_on()
    if on:
        cur[unlock_id] = {"at": now, "note": note}
    else:
        cur.pop(unlock_id, None)
    _write_json(UNLOCK_FILE, {"version": 1, "on": cur})
    if con is not None:
        log_action(con, "unlock_on" if on else "unlock_off", unlock_id, note or UNLOCKS[unlock_id]["words"],
                   "jordan (%s)" % door, now)
    return cur


def ladder(tr, sat, lines=None, on=None, killed=False):
    """Each unlock's state from trust (6.6 dict) and satisfaction (6.3 dict).
    lines: {unlock_id: {"trust", "satisfaction", "days"}} from the app (6.9), else UNLOCKS'.
    on: switched_on() dict. Returns {unlock_id: {"words", "lines", "met": {...}, "ready", "switched_on",
    "active", "state"}}. state is one of: killed, active, paused (on but under a line), ready (she'd bring it up),
    earning (not there yet)."""
    on = on or {}
    out = {}
    for uid, u in UNLOCKS.items():
        ln = dict({k: u[k] for k in ("trust", "satisfaction", "days")}, **((lines or {}).get(uid) or {}))
        met = {"trust": tr["value"] >= ln["trust"], "satisfaction": sat["value"] >= ln["satisfaction"],
               "days": tr["days"] >= ln["days"]}
        ready = all(met.values())
        is_on = uid in on
        if killed:
            state = "killed"
        elif is_on:
            state = "active" if met["trust"] and met["satisfaction"] else "paused"
        else:
            state = "ready" if ready else "earning"
        out[uid] = {"words": u["words"], "lines": ln, "met": met, "ready": ready and not killed,
                    "switched_on": is_on, "active": state == "active", "state": state}
    return out


def bring_up_words(uid, tr, sat):
    """What she'd say (logged only in shadow). Her own words, a question; switching is still yours, in the app."""
    return ("My plans have held up lately (trust %.2f over %d days) and you've seemed happy with them (%.2f). "
            "Want to switch this on in the app: %s?" % (tr["value"], tr["days"], sat["value"],
                                                          UNLOCKS[uid]["words"]))


# ---------- 6.8 the action log ----------

def log_action(con, kind, what, reason, who, now, shadow=True):
    _guard(con)
    con.executescript(ACTIONS_SCHEMA)
    con.execute("insert into trust_actions(at, kind, what, reason, who, shadow) values(?,?,?,?,?,?)",
                (int(now), kind, what, reason, who, int(bool(shadow))))
    con.execute("delete from trust_actions where at < ?", (int(now) - ACTIONS_KEEP_DAYS * 86400,))


def actions(con, limit=20, kind=None):
    con.executescript(ACTIONS_SCHEMA)
    q, args = "select id, at, kind, what, reason, who, shadow from trust_actions", ()
    if kind:
        q += " where kind = ?"
        args = (kind,)
    keys = ("id", "at", "kind", "what", "reason", "who", "shadow")
    return [dict(zip(keys, r)) for r in con.execute(q + " order by at desc, id desc limit ?", args + (int(limit),))]


def last_brought_up(con, uid):
    con.executescript(ACTIONS_SCHEMA)
    r = con.execute("select max(at) from trust_actions where kind = 'would_bring_up' and what = ?", (uid,)).fetchone()
    return r[0] if r else None
