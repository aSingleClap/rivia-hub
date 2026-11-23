"""Step 5.7: Jordan edits Step 5's goals, scores and weights from the app (layer 5). Checks:
  1. the code stays the source of truth: no edits means exactly the code's values and exactly 5.6's results;
     every value is listed under its layer and step (piece) with its hard range, for one small card per step
  2. edits are checked as a whole: weights add to 100, ranges, locked and unknown names refused with a plain reason,
     and a refused edit saves nothing
  3. a score switched off is left out of the day's gap, not counted as zero; the rule floor still holds
  4. putting values back (reset) brings the code's values back; a broken file falls back to the code's values
  5. every saved change is logged: who, what (old and new), when, Jordan's note
  5b. 5.4's mood sliders reach the mood pull, never past 0.25
  6. goals: switched off, fewer tries, and the limits a goal starts with come from the app
  6b. model limits: blank means not set, and 5.5's gauges read what's typed in
  7. Step 5's tick reads the edits; only the app button door can write; nothing registers a tool
  8. over real HTTP through rivia_auth's bearer gate: app token 200, admin 403, no token 401
Throwaway dirs and secrets.
    python test_progress_settings.py
"""
import ast, json, math, os, secrets, socket, sqlite3, sys, tempfile, threading, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
DOT = os.path.join(TMP, "dot")
os.makedirs(DOT, mode=0o700)
os.environ.update(RIVIA_DIR=os.path.join(TMP, "rivia"), RIVIA_HOME=os.path.join(TMP, "rivia"), RIVIA_DOTDIR=DOT,
                  RIVIA_DB=os.path.join(TMP, "auth.db"), HOME=TMP, RIVIA_JWT_KEY=secrets.token_hex(32),
                  RIVIA_SIGNIN_CHECK="0", RIVIA_GOOGLE_TOKEN_PATH=os.path.join(DOT, "google_token.json"))
os.environ.pop("RIVIA_GOAL_COST_USD", None)
sys.path.insert(0, HERE)

import rivia_caution as caution  # noqa: E402
import rivia_gap as gapmod  # noqa: E402
import rivia_goal_limits as goals  # noqa: E402
import rivia_progress as prog  # noqa: E402
import rivia_progress_settings as ps  # noqa: E402
import rivia_trend_mood as tmood  # noqa: E402

failed = 0
COUNT = {"n": 0}


def ok(cond, label):
    global failed
    COUNT["n"] += 1
    print(("  ok · " if cond else "FAIL · ") + label)
    failed += not cond


def refused(fn, code=None):
    try:
        fn()
    except ps.SettingsError as e:
        return (code is None or e.code == code), str(e)
    return False, "not refused"


def day(date="2026-11-24", **kw):
    d = {"date": date, "arrival_miss_min": None, "pin_hit_rate": None, "plan_held": None, "rule_breaks": None,
         "ride_est_miss_min": None, "ride_est_wins": None, "missing": []}
    d.update(kw)
    return d


def file_bytes():
    try:
        return open(ps.path(), "rb").read()
    except FileNotFoundError:
        return None


B = "app_button"

S = ps.SETTINGS
W = lambda n: "scores.%s.weight" % n  # noqa: E731
ON = lambda n: "scores.%s.on" % n  # noqa: E731

# ── 1. the code stays the source of truth ──
print("1. no edits: the code's values, and 5.6's results unchanged")
d0 = ps.defaults()
ok(d0[W("rule_breaks")] == 40 and d0[ON("rule_breaks")] and d0[W("plan_held")] == 25
   and d0[W("arrival_miss_min")] == 20 and d0[W("pin_hit_rate")] == 15,
   "weights read from rivia_gap: rules 40, plan 25, arrival 20, pins 15")
ok(d0[ON("ride_est_miss_min")] is False and d0[W("ride_est_miss_min")] == 0, "her ride estimate: off, weight 0 (as 5.6)")
ok((d0["metrics.arrival_grace_min"], d0["metrics.arrival_full_min"], d0["metrics.rule_breaks_full"],
    d0["metrics.rule_floor"], d0["metrics.min_scores"]) == (2.0, 15.0, 2, 0.5, 2), "5.2 numbers read from rivia_gap")
ok(d0["mood.on_track_line"] == prog.STANDING_GOOD_MAX == 0.20, "on-track line read from rivia_progress (0.20)")
ok((d0["mood.caps.hopeful"], d0["mood.caps.worried"], d0["mood.caps.content"], d0["mood.caps.focused"])
   == (tmood.TREND_CAPS["shrinking"], tmood.TREND_CAPS["growing"], tmood.TREND_CAPS["flat_good"],
       tmood.TREND_CAPS["flat_bad"]) == (0.20, 0.20, 0.15, 0.15), "5.4 mood caps read from rivia_trend_mood")
ok((d0["mood.build_per_h"], d0["mood.ease_per_h"], d0["mood.stale_h"]) == (0.05, 0.10, 48.0),
   "5.4 speeds and stale age read from rivia_trend_mood (0.05, 0.10 per hour, 48 hours)")
ok(all(d0["limits.%s.tries" % k] == 3 and d0["goals.%s.on" % k] for k in goals.DEFAULT_LIMITS),
   "every kind of goal: 3 tries (4.7), on")
ok(all(ps.goal_limits(k, d0) == {**goals.DEFAULT_LIMITS[k], "time_s": float(goals.DEFAULT_LIMITS[k]["time_s"])}
       for k in goals.DEFAULT_LIMITS), "every kind's time, token, call and money limits read from 5.5's defaults")
ok(all(v is None for m in ps.model_limits(d0).values() for v in m.values()) and len(ps.model_limits(d0)) == 3,
   "model limits: all 3 models start blank (not set); nothing is guessed")
e0 = ps.effective()
ok(e0["values"] == d0 and not e0["changed"] and e0["problems"] == [], "no file: in use = code values")
ok(not os.path.exists(ps.path()), "reading never makes a file")
sample = [day(arrival_miss_min=a, pin_hit_rate=p, plan_held=h, rule_breaks=r)
          for a in (0, 5, 40) for p in (None, 0.0, 0.7) for h in (1.0, 0.4) for r in (None, 0, 1, 3)]
same = all(gapmod.daily_gap(x)["gap"] == gapmod.daily_gap(x, ps.gap_settings(d0))["gap"] for x in sample)
ok(same, "every sample day gets the same gap with the code's values passed in as settings (%d days)" % len(sample))
RD = [{"direction": dr, "strength": st_, "standing": sd, "at": 1000} for dr, sd in
      (("shrinking", None), ("growing", None), ("flat", "good"), ("flat", "bad")) for st_ in (0.2, 0.6, 1.0)]
ok(all(tmood.target(x, 1000) == tmood.target(x, 1000, ps.mood_tune(d0)) for x in RD),
   "every 5.4 reading pulls the same with the code's values passed in")
saved = gapmod.RIDE_GRACE_MIN
gapmod.RIDE_GRACE_MIN = 3.0
ok(ps.effective()["values"]["metrics.arrival_grace_min"] == 3.0, "change the code and the app sees the new value")
gapmod.RIDE_GRACE_MIN = saved

print("1b. grouped by layer and piece for the app")
L = ps.listing()
pieces = {pc["piece"]: pc for pc in L["pieces"]}
ok(L["layer"] == 5 and list(pieces) == ["5.2", "5.4", "5.5", "models", "goals"],
   "layer 5, pieces 5.2, 5.4, 5.5, models, goals")
ok(sum(len(pc["settings"]) for pc in L["pieces"]) == len(S), "every setting is listed exactly once")
rows = {r_["path"]: r_ for pc in L["pieces"] for r_ in pc["settings"]}
ok(all(r_["words"] and r_["type"] in ("switch", "int", "num", "money", "limit") for r_ in rows.values())
   and all(r_["min"] is not None and r_["max"] is not None for r_ in rows.values() if r_["type"] != "switch"),
   "each row has words, a type, and a hard range for numbers")
ok(all(rows["limits.%s.%s" % (k, c)]["max"] == goals.HARD_CEILING[c] for k in goals.DEFAULT_LIMITS
       for c in ("tokens", "model_calls", "tool_calls", "cost_usd"))
   and all(rows["limits.%s.time_min" % k]["max"] * 60 == goals.HARD_CEILING["time_s"] for k in goals.DEFAULT_LIMITS),
   "every goal limit stops at 5.5's hard ceiling")
ok(rows["mood.caps.worried"]["max"] == 0.25 and rows["mood.on_track_line"]["max"] == 0.45
   and rows["mood.stale_h"]["min"] == 1 and rows["mood.stale_h"]["max"] == 168,
   "5.4 ceilings: caps 0.25, on-track 0.45, stale 1 to 168 hours")
ok({r_["path"].split(".")[0] for r_ in pieces["5.4"]["settings"]} == {"mood"}
   and all(S["limits.%s.tries" % k]["piece"] == "5.5" for k in goals.DEFAULT_LIMITS)
   and all(S["goals.%s.on" % k]["piece"] == "goals" for k in goals.DEFAULT_LIMITS), "each value sits under its step")
ok(not any(x in p for p in S for x in ("combined", "no_fear", "guard", "control", "ceiling", "skip", "access",
                                      "banned")), "none of the fixed limits is on any card")

# ── 2. edits are checked ──
print("2. checking edits")
r = ps.save({W("plan_held"): 30, W("pin_hit_rate"): 10}, door=B, note=" more plan ")
ok(r["values"][W("plan_held")] == 30 and r["values"][W("pin_hit_rate")] == 10, "weights 40/30/20/10 saved")
ok(json.load(open(ps.path()))["changes"] == {W("plan_held"): 30, W("pin_hit_rate"): 10},
   "only the changes are on file, not a copy of the code")
before = file_bytes()
cases = [
    ("weights to 105", {W("plan_held"): 35}, "weights_total"),
    ("weight over 100", {W("plan_held"): 101}, "out_of_range"),
    ("weight as text", {W("plan_held"): "30"}, "bad_value"),
    ("half a percent", {W("plan_held"): 29.5}, "bad_value"),
    ("on as text", {ON("plan_held"): "yes"}, "bad_value"),
    ("on with no weight", {ON("ride_est_miss_min"): True}, "bad_weight"),
    ("the plus can't be weighted", {"scores.ride_est_wins.weight": 10}, "not_weightable"),
    ("unknown score", {"scores.vibes.weight": 10}, "unknown"),
    ("grace past full", {"metrics.arrival_grace_min": 20}, "bad_range"),
    ("floor under 0.5", {"metrics.rule_floor": 0.4}, "out_of_range"),
    ("floor NaN", {"metrics.rule_floor": float("nan")}, "bad_value"),
    ("min scores 0", {"metrics.min_scores": 0}, "out_of_range"),
    ("too few scores on", {"metrics.min_scores": 5}, "too_few_on"),
    ("on-track 0.5 (a broken rule would read on track)", {"mood.on_track_line": 0.5}, "out_of_range"),
    ("worried past 0.25", {"mood.caps.worried": 0.3}, "out_of_range"),
    ("hopeful below 0", {"mood.caps.hopeful": -0.1}, "out_of_range"),
    ("concern builds too fast", {"mood.build_per_h": 0.5}, "out_of_range"),
    ("eases at 0", {"mood.ease_per_h": 0}, "out_of_range"),
    ("stale after a month", {"mood.stale_h": 720}, "out_of_range"),
    ("tries over 3", {"limits.debug.tries": 4}, "out_of_range"),
    ("tries 0", {"limits.debug.tries": 0}, "out_of_range"),
    ("tokens over the hard ceiling", {"limits.debug.tokens": 1_000_001}, "out_of_range"),
    ("tokens not whole", {"limits.debug.tokens": 1500.5}, "bad_value"),
    ("time over 4 hours", {"limits.debug.time_min": 241}, "out_of_range"),
    ("money over $5", {"limits.debug.cost_usd": 5.01}, "out_of_range"),
    ("money of $0", {"limits.debug.cost_usd": 0}, "out_of_range"),
    ("blank tokens (only money and model limits can be blank)", {"limits.debug.tokens": None}, "bad_value"),
    ("time in seconds (the app uses minutes)", {"limits.debug.time_s": 60}, "unknown"),
    ("new goal kind", {"limits.fix_things.tries": 2}, "unknown"),
    ("all goals off", {"goals.%s.on" % k: False for k in goals.DEFAULT_LIMITS}, "all_goals_off"),
    ("model RPM of 0", {"models.neon.rpm": 0}, "out_of_range"),
    ("model RPD not whole", {"models.neon.rpd": 10.5}, "bad_value"),
    ("a model Rivia doesn't use", {"models.gpt.rpm": 5}, "unknown"),
    ("a limit Google doesn't have", {"models.neon.rph": 5}, "unknown"),
    ("a list", [1, 2], "bad_value"),
]
for name, ch, code in cases:
    good, msg = refused(lambda ch=ch: ps.save(ch, door=B), code)
    ok(good, "refused: %s (%s)" % (name, msg))
ok(file_bytes() == before, "nothing refused was saved")

print("2b. safety limits can't be edited")
locked = [{"goals.plan_run.read_only": False}, {"goals.plan_run.means": ["write"]}, {"goals.plan_run.stop_when": {}},
          {"skip_confirmation": True}, {"more_access": True}, {"banned_moves": []}, {"no_fear": False},
          {"report_only": False}, {"goals.hard_ceiling.tokens": 9e9}, {"mood.combined_cap": 0.5},
          {"mood.word_guard": False}, {"mood.concern_control": True}]
for ch in locked:
    good, msg = refused(lambda ch=ch: ps.save(ch, door=B), "locked")
    ok(good and "safety limit" in msg, "locked: %s -> %s" % (json.dumps(ch)[:50], msg[:70]))
ok(file_bytes() == before, "nothing locked was saved")
names = {x["name"] for x in ps.view()["locked"]}
ok({"report_only", "skip_confirmation", "more_access", "banned_moves", "no_fear", "word_guard", "concern_control",
    "combined_cap"} <= names, "the app is told what's locked and why")
ok(goals.REPORT_ONLY is True and caution.BASE_MAX_ATTEMPTS == 3 and tmood.COMBINED_MAX == 0.25,
   "report only, 4.7's 3 tries and the 0.25 combined cap untouched")

print("2c. only the app button writes")
for door in ("app", "admin", "system", "code", "gemini", None):
    good, msg = refused(lambda door=door: ps.save({"metrics.rule_floor": 0.6}, door=door), "not_allowed")
    ok(good, "door %r refused" % (door,))
    ok(refused(lambda door=door: ps.reset("all", door=door), "not_allowed")[0], "door %r can't reset" % (door,))
ok(file_bytes() == before, "refused doors changed nothing")

# ── 3. off means left out ──
print("3. a score switched off is left out, not counted as zero")
ps.reset("all", door=B)
ps.save({ON("pin_hit_rate"): False, W("rule_breaks"): 50, W("plan_held"): 30}, door=B)
st = ps.gap_settings()
ok("pin_hit_rate" not in st["weights"] and abs(sum(st["weights"].values()) - 1) < 1e-9, "pins out, shares add to 1")
dd = day(arrival_miss_min=0, pin_hit_rate=0.0, plan_held=1.0, rule_breaks=0)
g_off = gapmod.daily_gap(dd, st)
ok(g_off["gap"] == 0.0 and "pin_hit_rate" not in g_off["used"] and "pin_hit_rate" in g_off["off"],
   "pins at 0% don't drag the day when pins are off (gap 0)")
ok(gapmod.daily_gap(dd)["gap"] > 0.1, "with the code's values the same day reads worse (pins counted)")
g_rule = gapmod.daily_gap(day(arrival_miss_min=0, plan_held=1.0, rule_breaks=1), ps.gap_settings(
    {**ps.defaults(), ON("rule_breaks"): False, W("plan_held"): 50, W("arrival_miss_min"): 30, W("pin_hit_rate"): 20}))
ok(g_rule["gap"] == 0.5 and g_rule["floored"], "rules score off: a broken rule still floors the day at 0.5")
ps.save({"metrics.rule_floor": 0.7}, door=B)
ok(gapmod.daily_gap(day(arrival_miss_min=0, plan_held=1.0, rule_breaks=1), ps.gap_settings())["gap"] == 0.7,
   "floor raised to 0.7 applies")
ps.reset("all", door=B)
ps.save({ON("ride_est_miss_min"): True, W("ride_est_miss_min"): 10, W("rule_breaks"): 30}, door=B)
g_est = gapmod.daily_gap(day(arrival_miss_min=0, pin_hit_rate=1.0, plan_held=1.0, rule_breaks=0, ride_est_miss_min=15),
                         ps.gap_settings())
ok(g_est["gap"] == 0.1 and "ride_est_miss_min" in g_est["used"], "her ride estimate weighted 10%: a full miss adds 0.1")
ps.save({"metrics.arrival_grace_min": 5, "metrics.arrival_full_min": 30}, door=B)
ok(gapmod.daily_gap(day(arrival_miss_min=5, plan_held=1.0, rule_breaks=0), ps.gap_settings())["gap"] == 0.0,
   "on-time minutes raised to 5: 5 minutes late is on time")
ok(gapmod.daily_gap(day(arrival_miss_min=5, plan_held=1.0, rule_breaks=0))["gap"] > 0, "(the code's 2 would count it)")

# ── 4. putting values back ──
print("4. reset brings the code's values back")
ok(refused(lambda: ps.reset(W("rule_breaks"), door=B), "weights_total")[0],
   "putting one weight back alone would break the total: refused with the reason")
ps.save({"mood.caps.worried": 0.10}, door=B)
r = ps.reset("piece:5.2", door=B)
v = ps.effective()["values"]
ok(all(v[p] == d0[p] for p in S if S[p]["piece"] == "5.2") and r["changes"], "reset piece 5.2: its code values back")
ok(v["mood.caps.worried"] == 0.10, "other pieces kept (5.4's worried cap still 0.10)")
ps.reset("mood.caps.worried", door=B)
ok(ps.effective()["values"]["mood.caps.worried"] == 0.20, "one setting put back")
ok(refused(lambda: ps.reset("vibes", door=B), "unknown")[0], "nothing called vibes")
ok(refused(lambda: ps.reset("piece:5.3", door=B), "unknown")[0], "5.3 has no adjustable values")
ok(refused(lambda: ps.reset("no_fear", door=B), "locked")[0], "a locked name can't be reset either")
ps.save({"metrics.rule_floor": 0.6}, door=B)
ps.reset("all", door=B)
ok(not os.path.exists(ps.path()) and ps.effective()["values"] == ps.defaults(), "reset all: file gone, code values")
r = ps.save({"metrics.rule_floor": 0.5}, door=B)
ok(r["changes"] == [] and not os.path.exists(ps.path()), "saving the code's own value stores nothing")

for bad, label in ((b"{nope", "not JSON"),
                   (b'{"version": 2, "changes": {"metrics.rule_floor": 0.1}}', "hand-edited out of range"),
                   (b'{"version": 2, "changes": {"skip_confirmation": true}}', "hand-edited locked"),
                   (b'{"version": 2, "changes": {"mood.caps.worried": 0.9}}', "hand-edited mood past 0.25"),
                   (b'{"version": 9, "changes": {}}', "wrong version")):
    os.makedirs(ps.rivia_dir(), exist_ok=True)
    open(ps.path(), "wb").write(bad)
    e = ps.effective()
    ok(e["values"] == ps.defaults() and e["problems"] and not e["changed"],
       "broken file (%s): code values, problem shown: %s" % (label, e["problems"][0][:60]))
open(ps.path(), "wb").write(b'{"version": 2, "changes": {"scores.plan_held.weight": 30}}')
e = ps.effective()
ok(e["values"] == ps.defaults() and "no longer fit" in e["problems"][0], "a saved change that doesn't add up: code values")
ps.reset("all", door=B)

# ── 5. the log ──
print("5. every change is logged")
t = 1_790_000_000
ps.save({"mood.on_track_line": 0.15}, door=B, note=" tighter ", now=t)
h = ps.history(1)[0]
ok(h["who"] == "app_button" and h["at"] == t and h["note"] == "tighter" and h["action"] == "set"
   and h["changes"] == [{"what": "mood.on_track_line", "piece": "5.4", "old": 0.2, "new": 0.15}],
   "who, when, note, and what with its step, old and new")
ps.reset("piece:5.4", door=B, now=t + 5)
h = ps.history(1)[0]
ok(h["action"] == "reset piece:5.4" and h["changes"] == [{"what": "mood.on_track_line", "piece": "5.4", "old": 0.15,
                                                          "new": 0.2}], "reset logged with old and new")
ok(refused(lambda: ps.save({"mood.on_track_line": 0.1}, door=B, note="x" * 201), "bad_value")[0],
   "note too long refused")
n_before = len(ps.history(1000))
refused(lambda: ps.save({W("plan_held"): 99}, door=B))
ok(len(ps.history(1000)) == n_before, "a refused edit isn't logged as a change")

# ── 5b. 5.4 mood sliders ──
print("5b. 5.4 mood sliders reach the mood pull")
grow = {"direction": "growing", "strength": 1.0, "standing": None, "at": t}
ok(abs(math.hypot(*tmood.target(grow, t, ps.mood_tune())) - 0.20) < 1e-9, "code: a strong growing trend pulls 0.20")
ps.save({"mood.caps.worried": 0.10}, door=B)
ok(abs(math.hypot(*tmood.target(grow, t, ps.mood_tune())) - 0.10) < 1e-9, "worried cap 0.10: it pulls 0.10")
ps.save({"mood.caps.worried": 0.0}, door=B)
ok(tmood.target(grow, t, ps.mood_tune()) == (0.0, 0.0, 0.0), "worried cap 0: the trend can't worry her at all")
ok(tmood.target(grow, t, {"caps": {"growing": 9.0}}) == tmood.target(grow, t, {"caps": {"growing": 0.25}}),
   "even a hand-made tune can't pass 0.25 (the mood module clamps it too)")
ps.reset("all", door=B)
old = {"direction": "growing", "strength": 1.0, "standing": None, "at": t - 30 * 3600}
ok(tmood.target(old, t, ps.mood_tune()) != (0.0, 0.0, 0.0), "a 30-hour-old reading counts with the code's 48 hours")
ps.save({"mood.stale_h": 24}, door=B)
ok(tmood.target(old, t, ps.mood_tune()) == (0.0, 0.0, 0.0), "stale age 24 hours: it stops counting")
ps.reset("all", door=B)
s1 = tmood.step_toward((0, 0, 0), (0.2, 0, 0), 900, **{k: ps.mood_tune()[k] for k in ("worsen_per_h", "ease_per_h")})
ps.save({"mood.build_per_h": 0.02}, door=B)
s2 = tmood.step_toward((0, 0, 0), (0.2, 0, 0), 900, **{k: ps.mood_tune()[k] for k in ("worsen_per_h", "ease_per_h")})
ok(0 < s2[0] < s1[0], "slower build: concern grows less in 15 minutes (%.4f vs %.4f)" % (s2[0], s1[0]))
ps.reset("all", door=B)

# ── 6. goals ──
print("6. goals")
SW = {"kind": "gap_at_most", "target": 0.2}
con = sqlite3.connect(os.path.join(TMP, "body.db"))
ps.save({"goals.debug.on": False}, door=B)
try:
    prog.start_goal(con, "g1", "debug", instruction="why is it slow", stop_when=SW, now=t)
    ok(False, "a switched-off kind is refused")
except goals.GoalError as e:
    ok(e.code == "goal_off", "a switched-off kind of goal is refused (%s)" % e)
g = prog.start_goal(con, "g2", "plan_run", instruction="plan tomorrow", stop_when=SW, now=t)
ok(g["caps"] == {**goals.DEFAULT_LIMITS["plan_run"]} and g["max_tries"] is None,
   "with nothing changed, a goal starts with 5.5's default limits and 4.7's tries, as before")
ps.save({"limits.plan_run.tries": 2}, door=B)
g = prog.start_goal(con, "g3", "plan_run", instruction="plan tomorrow", stop_when=SW, now=t)
ok(g["max_tries"] == 2 and g["caps"]["tokens"] == goals.DEFAULT_LIMITS["plan_run"]["tokens"],
   "your 2 tries are kept on the goal")
A = caution.assess(0.0, 1.0)
g = goals.record_try(g, ok=False, failure="maps api timed out")
g = goals.record_try(g, ok=False, failure="no ride time for farm")
d = goals.check(g, A, now=t + 1, gap=0.5)
ok(d["action"] == "stop" and d["why"] == "cap_reached", "2 tries used: she stops (4.7 alone would allow 3)")
g5 = goals.keep_going(g, now=t + 2)
d = goals.check(g5, A, now=t + 2, gap=0.5)
ok(d["action"] != "stop" and g5["max_tries"] == 3, "your \"keep going\" still gives one more try")
os.environ["RIVIA_GOAL_COST_USD"] = "2"
g6 = prog.start_goal(con, "g6", "plan_run", instruction="plan tomorrow", stop_when=SW, now=t)
ok(g6["caps"]["cost_usd"] == 2.0 and g6["max_tries"] == 2, "the tablet's $2 money cap still applies, as in 5.6")
ps.save({"limits.plan_run.cost_usd": 0.5}, door=B)
g6 = prog.start_goal(con, "g6b", "plan_run", instruction="plan tomorrow", stop_when=SW, now=t)
ok(g6["caps"]["cost_usd"] == 0.5, "your app cap of $0.50 and the tablet's $2: the tighter one wins")
os.environ.pop("RIVIA_GOAL_COST_USD")
ps.save({"limits.plan_run.tokens": 80_000, "limits.plan_run.time_min": 5, "limits.plan_run.model_calls": 20,
         "limits.plan_run.cost_usd": None}, door=B)
g7 = prog.start_goal(con, "g7", "plan_run", instruction="plan tomorrow", stop_when=SW, now=t)
ok(g7["caps"]["tokens"] == 80_000 and g7["caps"]["time_s"] == 300 and g7["caps"]["model_calls"] == 20
   and g7["caps"]["cost_usd"] is None and g7["deadline"] == t + 300,
   "your app limits (80,000 tokens, 5 minutes, 20 calls, no money cap) are the ones the goal starts with")
row7 = goals.log_row(g7, goals.check(g7, A, now=t + 1, gap=0.5), now=t + 1)
ok(row7["caps"]["tokens"] == 80_000, "the goal's log line carries your limits, so 5.5's would-have-stopped uses them")
g9 = prog.start_goal(con, "g9", "plan_run", instruction="plan tomorrow", stop_when=SW, now=t,
                     limits={"tokens": 200_000, "model_calls": 5})
ok(g9["caps"]["tokens"] == 80_000 and g9["caps"]["model_calls"] == 5,
   "code asking for more than your limit gets yours; asking for less is fine (she can't widen her own limits)")
g10 = prog.start_goal(con, "g10", "plan_run", instruction="plan tomorrow", stop_when=SW, now=t,
                      limits={"tokens": 200_000}, from_jordan=True)
ok(g10["caps"]["tokens"] == 200_000, "your own message can still set a goal looser, up to the ceiling")
try:
    prog.start_goal(con, "g11", "plan_run", instruction="x", stop_when=SW, now=t, limits={"tokens": 2_000_000},
                    from_jordan=True)
    ok(False, "past the ceiling is refused")
except goals.GoalError as e:
    ok(e.code == "over_ceiling", "nothing passes the hard ceiling, even from you (%s)" % e.code)
ok(goals.BUDGET_MODE == "measure", "measure mode is untouched: tokens and calls are logged, not enforced")
try:
    goals.new_goal("g8", "plan_run", instruction="x", stop_when=SW, now=t, read_only=False)
    ok(False, "a goal that writes is refused")
except goals.GoalError as e:
    ok(e.code == "report_only", "a goal that would write is still refused (report only)")
ps.reset("all", door=B)

# ── 6b. model limits ──
print("6b. model limits")
import rivia_model_limits as ml  # noqa: E402
tb, pr = ml.current_table()
ok(all(v is None for m in tb.values() for v in m.values()) and pr == [], "5.5's gauges read all models as not set")
ps.save({"models.hydrogen.rpm": 10, "models.hydrogen.rpd": 100, "models.flash_3_8.tpm": 250_000}, door=B)
tb, pr = ml.current_table()
ok(tb["gemini-4-hydrogen"] == {"rpm": 10, "tpm": None, "rpd": 100} and tb["gemini-3.8-flash"]["tpm"] == 250_000
   and tb["gemini-4-neon"] == {"rpm": None, "tpm": None, "rpd": None} and pr == [],
   "typed limits reach 5.5's gauges under Google's model names; the rest stay not set")
calls = [ml.call("gemini-4-hydrogen", t - i, input_tokens=100) for i in range(9)]
u = ml.usage(calls, tb, t)
ok(u["gemini-4-hydrogen"]["rpm"]["status"] == "near" and u["gemini-4-neon"]["worst"] == "not_set",
   "9 calls in a minute against your 10 RPM reads near; Neon with nothing typed reads not set")
ps.save({"models.hydrogen.rpm": None}, door=B)
ok(ml.current_table()[0]["gemini-4-hydrogen"]["rpm"] is None, "blanking a limit puts it back to not set")
ps.reset("piece:models", door=B)
ok(all(v is None for m in ps.model_limits().values() for v in m.values()), "reset the models card: all blank again")

# ── 7. Step 5's tick reads the edits, and nothing is a tool ──
print("7. wiring and doors")
ps.save({"mood.on_track_line": 0.10}, door=B)
tr = {"direction": "flat", "strength": 0.0, "level": 0.15, "slope_per_week": 0.0, "days_used": list(range(7))}
ok(prog.reading_for_mood(tr, t, ps.on_track_line())["standing"] == "bad",
   "on-track line 0.10: a steady 0.15 week reads off track")
ok(prog.reading_for_mood(tr, t)["standing"] == "good", "(the code's 0.20 would read on track)")
ps.reset("all", door=B)
src = open(os.path.join(HERE, "rivia_progress.py")).read()
ok("psettings.effective()" in src and "psettings.on_track_line(vals)" in src
   and "tune=psettings.mood_tune(vals)" in src
   and "gapmod.daily_gap(day, psettings.gap_settings(values))" in src, "the tick reads the edits once and passes them on")
ps.save({"mood.caps.worried": 0.05}, door=B)
mcon = sqlite3.connect(":memory:")
calls_seen = []
real = tmood.shadow_tick
tmood.shadow_tick = lambda *a, **k: calls_seen.append(k.get("tune")) or {"stub": True}
try:
    prog.shadow_tick(mcon, t)
finally:
    tmood.shadow_tick = real
ok(calls_seen and calls_seen[0]["caps"]["growing"] == 0.05, "a real Step 5 tick hands 5.4 Jordan's worried cap")
ps.reset("all", door=B)
tree = ast.parse(src)
calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
ok(not calls & {"save", "reset", "_write_file"}, "Step 5's own code never writes the settings")


def imports(path):
    tr_ = ast.parse(open(path).read())
    out = set()
    for n in ast.walk(tr_):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out, tr_


TOOL_MODULES = {"rivia_tools", "rivia_admin", "rivia_battery", "rivia_places", "rivia_sched", "rivia_settings",
                "rivia_approvals", "rivia_planner", "rivia_gcal", "rivia_google", "rivia_store"}
present = [m for m in sorted(TOOL_MODULES) if os.path.exists(os.path.join(HERE, m + ".py"))]
leaks = [m for m in present if imports(os.path.join(HERE, m + ".py"))[0] & {"rivia_progress_settings",
                                                                              "rivia_progress_app"}]
ok(len(present) == len(TOOL_MODULES) and not leaks, "no tool-registering module imports the editor (%d checked)"
   % len(present))
for f in ("rivia_progress_app.py", "rivia_progress_settings.py"):
    mods, tr_ = imports(os.path.join(HERE, f))
    calls = {n.func.attr for n in ast.walk(tr_) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    ok("tool" not in calls and not {m for m in mods if m.startswith(("fastmcp", "mcp", "rivia_tools"))},
       "%s registers no tool and doesn't touch the tool table" % f)
server_src = open(os.path.join(HERE, "rivia_server.py")).read()
ok("extra_routes=emotion_routes() + progress_routes()" in server_src and "app_mcp.tool" not in server_src,
   "rivia_server hands the editor to build_app as a route, not to app_mcp")

# ── 8. over real HTTP ──
print("8. over HTTP, through the bearer gate")
import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
import rivia_auth as ra  # noqa: E402
import rivia_progress_app as papp  # noqa: E402

ps.reset("all", door=B)


def standin_mcp():
    async def mcp(request):
        return PlainTextResponse("mcp mount")
    return Starlette(routes=[Route("/mcp", mcp, methods=["GET", "POST"])])


def serve(app):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    return server, "http://127.0.0.1:%d" % port


def http(base, p, token=None, body=None, method=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + p, data=data, method=method or ("POST" if data is not None else "GET"))
    if token:
        req.add_header("authorization", "Bearer " + token)
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r_:
            text, status = r_.read().decode(), r_.status
    except urllib.error.HTTPError as e:
        text, status = e.read().decode(), e.code
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


server, BASE = serve(ra.build_app(app_mcp=standin_mcp(), admin_mcp=standin_mcp(), extra_routes=papp.routes()))
APP = ra.mint_door_token("app", 1)["token"]
ADMIN = ra.mint_door_token("admin", 1)["token"]
st, r = http(BASE, "/app/progress", APP)
rows_ = {x["path"]: x for pc in r["layers"][0]["pieces"] for x in pc["settings"]} if st == 200 else {}
ok(st == 200 and r["ok"] and {p: x["value"] for p, x in rows_.items()} == ps.defaults() and not r["changed"]
   and r["locked"] and [pc["piece"] for pc in r["layers"][0]["pieces"]] == ["5.2", "5.4", "5.5", "models", "goals"],
   "GET with the app token: every value grouped by step, code values, nothing changed, locked list")
ok(http(BASE, "/app/progress")[0] == 401, "no token: 401")
ok(http(BASE, "/app/progress", ADMIN)[0] == 403, "admin token: 403 (wrong door)")
ok(http(BASE, "/app/progress", "x.y.z", {"set": {"metrics.rule_floor": 0.6}})[0] == 401, "bad token: 401")
ok(not os.path.exists(ps.path()), "refused calls saved nothing")
st, r = http(BASE, "/app/progress", APP, {"set": {W("plan_held"): 30, W("pin_hit_rate"): 10}, "note": "app"})
row = [x for x in r["layers"][0]["pieces"][0]["settings"] if x["path"] == W("plan_held")][0] if st == 200 else {}
ok(st == 200 and row["value"] == 30 and row["changed"] and row["code"] == 25 and len(r["edited"]) == 2
   and r["history"][0]["who"] == "app_button", "POST set: saved, changes listed, logged as app_button")
st, r = http(BASE, "/app/progress", APP, {"set": {W("plan_held"): 50}})
ok(st == 400 and r["code"] == "weights_total" and "100" in r["error"], "bad weights: 400 with a plain reason")
st, r = http(BASE, "/app/progress", APP, {"set": {"skip_confirmation": True}})
ok(st == 400 and r["code"] == "locked", "a locked name: 400 locked")
for name, body, raw in (("not JSON", None, b"{nope"), ("both", {"set": {}, "reset": "all"}, None),
                        ("extra key", {"set": {}, "door": "admin"}, None), ("a list", [1], None)):
    ok(http(BASE, "/app/progress", APP, body, method="POST", raw=raw)[0] == 400, "refused: %s" % name)
ok(http(BASE, "/app/progress", APP, raw=b"x" * 9000, method="POST")[0] == 413, "too big: 413")
ok(http(BASE, "/app/progress", APP, method="DELETE")[0] == 405, "DELETE: 405")
st, r = http(BASE, "/app/progress", APP, {"reset": "all", "note": "back to code"})
ok(st == 200 and not r["changed"] and not os.path.exists(ps.path()), "POST reset all: code values back")
st, r = http(BASE, "/app/progress", APP, {"set": {"mood.caps.hopeful": 0.1}})
st, r = http(BASE, "/app/progress", APP, {"reset": "piece:5.4"})
ok(st == 200 and not os.path.exists(ps.path()), "POST reset one step (5.4): its values back")
st, r = http(BASE, "/app/mcp", APP, {"x": 1})
ok(st == 200 and r == "mcp mount", "/app/mcp still reaches the MCP mount")

print("8b. section 5.5's graphs and gauges (read-only)")
import rivia_normal as normal  # noqa: E402
import rivia_usage as us  # noqa: E402
import rivia_model_limits as ml2  # noqa: E402
now_ = time.time()
st, r = http(BASE, "/app/progress/usage", APP)
ok(st == 200 and r["ok"] and len(r["days"]) == 14 and set(r["kinds"]) == set(goals.DEFAULT_LIMITS)
   and r["gauges"]["gemini-4-neon"]["worst"] == "not_set" and r["mode"] == "measure" and r["seen"] == ["tablet"],
   "empty: 14 days, every kind, every model not set, measuring, tablet-only note")
ok(r["rpd_resets_at"] == int(ml2.next_pacific_midnight(time.time())) or
   abs(r["rpd_resets_at"] - ml2.next_pacific_midnight(time.time())) < 2, "says when RPD starts over")
ok(r["goal_limits"]["debug"]["tokens"] == 150_000 and r["hard_ceiling"]["tokens"] == 1_000_000,
   "limit lines: the limits in use, and the ceiling")
c_ = normal.db_open()
try:
    for i in range(9):
        us.record_call(c_, "gemini-4-hydrogen", now_ - 5 - i, input_tokens=1000, output_tokens=200)
    g_ = prog.start_goal(c_, "u1", "plan_run", instruction="plan tomorrow", stop_when=SW, now=now_ - 30)
    g_ = goals.record_usage(g_, now=now_ - 20, tokens=60_000, model_calls=3)
    prog.log_goal(c_, g_, goals.check(g_, caution.assess(0.0, 1.0), now=now_ - 10, gap=0.5), now_ - 10)
    c_.commit()
finally:
    c_.close()
http(BASE, "/app/progress", APP, {"set": {"models.hydrogen.rpm": 10, "limits.plan_run.tokens": 70_000}})
st, r = http(BASE, "/app/progress/usage?days=7", APP)
pr_ = r["kinds"]["plan_run"][-1] if st == 200 else {}
ok(st == 200 and len(r["days"]) == 7 and pr_["goals"] == 1 and pr_["tokens_max"] == 60_000 and pr_["would_stop"] == 1,
   "a goal that used 60,000 tokens shows on today's bar and counts as would-have-stopped (its limit was 50,000)")
ok(st == 200 and r["models"]["gemini-4-hydrogen"][-1]["requests"] == 9
   and r["gauges"]["gemini-4-hydrogen"]["rpm"] == {"used": 9, "limit": 10, "share": 0.9, "status": "near"}
   and r["goal_limits"]["plan_run"]["tokens"] == 70_000,
   "9 calls a minute against your 10 RPM reads near; your new 70,000 token line is what the graph draws")
ok(http(BASE, "/app/progress/usage?days=99", APP)[0] == 400 and http(BASE, "/app/progress/usage?days=x", APP)[0] == 400,
   "days outside 1 to 31: 400")
ok(http(BASE, "/app/progress/usage", APP, {"x": 1})[0] == 405 and http(BASE, "/app/progress/usage")[0] == 401
   and http(BASE, "/app/progress/usage", ADMIN)[0] == 403, "read-only (POST 405), app token only (401, 403)")
st, r = http(BASE, "/app/progress", APP, {"set": {"models.hydrogen.rpm": None, "limits.plan_run.cost_usd": 0.25}})
ok(st == 200 and ps.model_limits()["hydrogen"]["rpm"] is None and ps.goal_limits("plan_run")["cost_usd"] == 0.25,
   "over HTTP a blank (null) clears a model limit, and a money cap can be set")

ps.reset("all", door=B)
server.should_exit = True

print("%s: %d checks, %d failed" % ("PASS" if not failed else "FAIL", COUNT["n"], failed))
sys.exit(1 if failed else 0)
