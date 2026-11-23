"""Step 5.4: the plan-accuracy trend colors her mood, a little, in shadow.
  1. each trend points at the right word: shrinking relief, growing worried, flat good content, flat bad focused
  2. small: never past each cap; the trend alone reads "faint" at most; weak trends and no data do nothing
  3. no fear: any trend, alone or on top of any body pull 4.6 allows, never reads fear, distressed, sad or angry
  4. counted once: body and trend together, the same way, never past 0.25; opposite ways are not trimmed
  5. never pushier: concern adds no energy and lowers "in control"
  6. mostly valence: relief, concern and content lead with pleasant/unpleasant (focused is the spec's exception)
  7. slow: grows over hours, eases faster, a flip fades across, no data fades to nothing
  8. shadow: writes only trend_mood in the database it is given, refuses emotion.db, never opens it; keeps a week
  9. bad input is refused and writes nothing; stale or half-filled readings count as no data
  10. with the installed Step 4 shadow dials (4.9): a real body share goes in, the ceiling holds
Throwaway RIVIA_DIR; never touches ~/rivia.
    python test_trend_mood.py
"""
import math, os, random, sqlite3, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ["RIVIA_DIR"] = os.path.join(TMP, "rivia")
sys.path.insert(0, HERE)

import rivia_emotion as emo  # noqa: E402
import rivia_body_cap as body  # noqa: E402
import rivia_trend_mood as tm  # noqa: E402

MIN, HOUR, DAY = 60, 3600, 86400
T0 = 1_800_000_000
EPS = 1e-9
N = {"ok": 0}
BAD_WORDS = {"anger", "sadness", "distressed"}


def ok(cond, msg):
    if not cond:
        raise AssertionError(msg)
    N["ok"] += 1


def name(p):
    return emo.label(*p)["name"]


def direction_name(p):
    n = math.hypot(*p)
    return "neutral" if n == 0 else name(tuple(x / n * 0.5 for x in p))


def R(direction, strength=1.0, standing=None, at=None):
    r = {"direction": direction, "strength": strength}
    if standing is not None:
        r["standing"] = standing
    if at is not None:
        r["at"] = at
    return r


ALL = [R("shrinking"), R("growing"), R("flat", standing="good"), R("flat", standing="bad")]


def fresh():
    return sqlite3.connect(os.path.join(tempfile.mkdtemp(), "body.db"))


def body_shares(n, rng):
    """Body pulls 4.6 can produce: its capped target for random dial pulls, at a random point on the way there."""
    out = [(0.0, 0.0, 0.0), body.target((-1.0, 1.0, 0.0), (0.0, 0.0, -1.0)), body.target((0.3, -0.3, 0.3))]
    for _ in range(n):
        pulls = [tuple(rng.uniform(-1, 1) for _ in range(3)) for _ in range(rng.randint(1, 2))]
        f = rng.random()  # one factor for all three axes, so the direction stays 4.6's
        out.append(tuple(x * f for x in body.target(*pulls)))
    return out


# 1. words
for r, want in zip(ALL, ("relief", "worried", "contentment", "focused")):
    ok(direction_name(tm.target(r)) == want, "%s should point at %s, got %s" % (r, want, direction_name(tm.target(r))))
    ok(tm.READS_AS[tm.kind(r)[0]] == want, "READS_AS disagrees for %s" % r)
ok(all(w in emo.REFERENCE_POINTS for w in tm.READS_AS.values()), "every word is an existing label: no new label")

# 2. small
for r in ALL:
    k = tm.kind(r)[0]
    for s in [i / 20 for i in range(21)]:
        p = tm.target(dict(r, strength=s))
        m = math.hypot(*p)
        ok(m <= tm.TREND_CAPS[k] + EPS, "%s at %s past its cap: %s" % (k, s, m))
        ok(m == 0 or m >= tm.TREND_DEADBAND, "a pull under the deadband should be 0")
        ok(emo.label(*p)["intensity"] in ("neutral", "faint"), "trend alone read past faint: %s" % (p,))
ok(max(tm.TREND_CAPS.values()) < emo.INTENSITY_BANDS[1][0], "every cap is under the 'mild' band")
ok(tm.target(R("growing", 0.2)) == (0.0, 0.0, 0.0), "a weak trend (0.2 * 0.20 = 0.04) does nothing")
ok(tm.target(R("not_enough_data")) == (0.0, 0.0, 0.0), "no data, no pull")
ok(tm.target(None) == (0.0, 0.0, 0.0), "no reading, no pull")
ok(emo.label(*tm.target(R("flat", standing="good")))["name"] == "contentment",
   "steady and on track, at full strength, just shows as faint content")

# 3. no fear, alone or with the body
rng = random.Random(54)
shares = body_shares(600, rng)
seen = set()
for b in shares:
    for r in ALL:
        for s in (0.3, 0.6, 1.0):
            t, _ = tm.trim(tm.target(dict(r, strength=s)), b)
            for p in (t, tuple(x + y for x, y in zip(b, t))):
                n = name(p)
                seen.add(n)
                ok(n not in BAD_WORDS and not any(w in n for w in emo.NEVER_LABELS),
                   "read %s: body %s + trend %s" % (n, b, t))
ok({"relief", "worried", "focused"} <= seen, "the sweep did reach the trend's own words: %s" % sorted(seen))

# 4. counted once
full_bad = body.target((-1.0, 1.0, 0.0), (0.0, 0.0, -1.0))
ok(abs(math.hypot(*full_bad) - 0.25) < 1e-6, "test setup: body at its full 0.25")
t, k = tm.trim(tm.target(R("growing")), full_bad)
ok(k == 0.0 and t == (0.0, 0.0, 0.0), "body already full and same way: concern adds nothing")
t, k = tm.trim(tm.target(R("growing")), (0.0, 0.0, 0.0))
ok(k == 1.0, "no body pull: concern untrimmed")
half = tuple(x * 0.5 for x in full_bad)
t, k = tm.trim(tm.target(R("growing")), half)
ok(0 < k < 1 and abs(math.hypot(*(x + y for x, y in zip(half, t))) - 0.25) < 1e-9, "half body: trimmed to the ceiling")
t, k = tm.trim(tm.target(R("shrinking")), full_bad)
ok(k == 1.0, "relief against a bad body is not trimmed: opposite causes aren't counted twice")
for b in shares:
    for r in ALL:
        t, _ = tm.trim(tm.target(r), b)
        if sum(x * y for x, y in zip(b, t)) > 0:
            ok(math.hypot(*(x + y for x, y in zip(b, t))) <= tm.COMBINED_MAX + 1e-9, "ceiling broke: %s %s" % (b, t))
ok(tm.COMBINED_MAX == body.BODY_MAX_PULL, "the ceiling is the body's own cap")

# 5. never pushier
g = tm.target(R("growing"))
ok(g[1] == 0.0 and g[2] < 0, "concern: no extra energy, less in control")
for b in shares:
    t, _ = tm.trim(g, b)
    ok(t[1] <= EPS and t[2] <= EPS, "concern never raises arousal or dominance")

# 6. mostly valence
for r in ALL[:3]:
    p = tm.target(r)
    ok(abs(p[0]) == max(abs(x) for x in p), "%s leads with valence: %s" % (tm.kind(r)[0], p))

# 7. slow
share, t = (0.0, 0.0, 0.0), 0
goal = tm.target(R("growing"))
while math.dist(share, goal) > 1e-9:
    share = tm.step_toward(share, goal, 15 * MIN)
    t += 15 * MIN
ok(t >= 4 * HOUR, "full concern takes at least 4 hours, took %s h" % (t / HOUR))
up = t
t = 0
while math.hypot(*share) > 1e-9:
    share = tm.step_toward(share, (0.0, 0.0, 0.0), 15 * MIN)
    t += 15 * MIN
ok(t < up, "easing back is faster than growing")
con = fresh()
for i in range(16):  # 4 h growing
    tm.shadow_tick(con, T0 + i * 15 * MIN, R("growing"))
row = tm.shadow_tick(con, T0 + 16 * 15 * MIN, R("shrinking"))
ok(row["share"][0] < 0, "right after a flip she is still a little concerned: no jump")
for i in range(17, 40):
    row = tm.shadow_tick(con, T0 + i * 15 * MIN, R("shrinking"))
ok(row["would_read"].endswith("relief"), "after a flip she ends up hopeful: %s" % row["would_read"])
for i in range(40, 60):
    row = tm.shadow_tick(con, T0 + i * 15 * MIN, R("not_enough_data"))
ok(row["share"] == [0.0, 0.0, 0.0] and row["would_read"] == "neutral", "no data fades her back to nothing")
row = tm.shadow_tick(con, T0 + 3 * DAY, R("growing"))
ok(math.hypot(*row["share"]) <= tm.TREND_WORSEN_PER_H * 0.25 + 1e-3, "a long quiet spell earns only 15 minutes")

# 8. shadow
real_open = emo.db_open
emo.db_open = lambda *a, **k: (_ for _ in ()).throw(AssertionError("emotion.db opened"))
try:
    con = fresh()
    con.execute("create table other(x)")
    before = con.execute("select name from sqlite_master").fetchall()
    tm.shadow_tick(con, T0, R("growing"))
    after = con.execute("select name from sqlite_master where type='table'").fetchall()
    ok(sorted(set(after) - set(before)) == [("trend_mood",)], "only trend_mood is added")
    ok(con.execute("select count(*) from other").fetchone()[0] == 0, "other tables untouched")
    ok(not os.path.exists(os.path.join(os.environ["RIVIA_DIR"], "emotion.db")), "emotion.db never made")
finally:
    emo.db_open = real_open
emo_path = os.path.join(tempfile.mkdtemp(), "emotion.db")
ec = sqlite3.connect(emo_path)
try:
    tm.shadow_tick(ec, T0, R("growing"))
    ok(False, "should refuse emotion.db")
except tm.TrendMoodError as e:
    ok(e.code == "not_allowed", "refused emotion.db")
ok(ec.execute("select count(*) from sqlite_master").fetchone()[0] == 0, "nothing written to emotion.db")
con = fresh()
for d in range(9):
    tm.shadow_tick(con, T0 + d * DAY, R("growing"))
ats = [r[0] for r in con.execute("select at from trend_mood")]
ok(min(ats) >= T0 + 8 * DAY - tm.TREND_KEEP_S, "rows older than a week are dropped")
rows = tm.recent(con, 3)
ok(len(rows) == 3 and rows[0]["at"] > rows[1]["at"], "recent(): newest first")
ok(rows[0]["cause"].startswith("trend: plan misses growing"), "each row says why: %s" % rows[0]["cause"])
ok(isinstance(rows[0]["share"], list) and len(rows[0]["share"]) == 3, "recent() decodes the numbers")

# 9. bad input
for bad in ({"direction": "sideways", "strength": 1}, {"direction": "growing", "strength": 2},
            {"direction": "growing", "strength": float("nan")}, {"direction": "growing", "strength": True}, "growing"):
    con = fresh()
    try:
        tm.shadow_tick(con, T0, bad)
        ok(False, "should refuse %r" % (bad,))
    except tm.TrendMoodError as e:
        ok(e.code == "bad_value", "refused %r" % (bad,))
    ok(con.execute("select count(*) from sqlite_master where name='trend_mood'").fetchone()[0] == 0,
       "nothing written for %r" % (bad,))
ok(tm.kind(R("flat"))[0] is None, "flat with no standing: no nudge")
ok(tm.kind(R("growing", at=T0 - 3 * DAY), now=T0)[0] is None, "a 3-day-old reading: no nudge")
ok(tm.kind(R("growing", at=T0 - HOUR), now=T0)[0] == "growing", "a fresh reading counts")
ok(tm.kind({"direction": "not enough data"})[0] is None, "5.3's plain-words 'not enough data' is understood")
ok(tm.kind({"direction": "steady", "strength": 1, "standing": "bad"})[0] == "flat_bad", "'steady' means flat")
row = tm.shadow_tick(fresh(), T0, R("flat"))
ok(row["cause"] == "trend: flat but no standing" and row["share"] == [0.0, 0.0, 0.0], "logged why it did nothing")

# 10. with the installed 4.9 shadow dials
try:
    import rivia_body_shadow as bs  # noqa: F401
    import rivia_normal as normal
    bcon = normal.db_open()
    import rivia_store as store  # noqa: F401
    brow = bs.shadow_tick(bcon, T0)
    tr = tm.shadow_tick(bcon, T0, R("growing"), body_share=brow["share"])
    ok(tr["body_share"] == [round(x, 4) for x in brow["share"]], "4.9's share goes straight in")
    ok(bcon.execute("select count(*) from dials").fetchone()[0] == 1, "4.9's dials row untouched")
    ok(len(tm.recent(bcon, 5)) == 1, "trend row sits beside it in body.db")
    bcon.close()
except ImportError as e:
    print("skipped 10 (4.9 files not on path): %s" % e)

print("test_trend_mood: %d checks passed" % N["ok"])
