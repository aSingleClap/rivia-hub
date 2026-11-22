"""Step 2 acceptance (emotion spec, Step 2.9), run against the merged Step 2 copy (2.1 layers, 2.2 decay on read,
2.3 label contract with her baseline as neutral, 2.4 dial and lock, 2.5 fade columns and log labels, 2.7
baseline limits). Re-merged Nov 22 after Jordan's Step 2 review. The clock is faked by replacing
rivia_time.now_s, so nothing waits. Throwaway RIVIA_DIR; never touches ~/rivia. Needs starlette (for the dial)
and rivia_store / rivia_time on the path (rivia-hub tablet/):
    python test_step2_acceptance.py

  1. anger set by hand on the dial reads about half as strong 10 minutes later
  2. 4 hours on: the anger spike is gone and she rests on mood and baseline; a mood set by hand has covered
     half its way to baseline (one 4-hour half-life) and is near baseline a day later, where she reads neutral
     (2.3: her normal counts as neutral)
  3. a restart (module reloaded, database reopened) resets nothing, and time while off still counts
  4. the dial moves emotion freely; mood and baseline only behind the lock, tagged (Jordan's 2.4 decision,
     which replaced the brief's "the dial changes only emotion")
  5. the baseline drift limits hold with decay on, and only the baseline is limited
  6. fading alone writes no change_log row and changes no stored row; the next real change records the fade
  7. Step 1's "no nudge lost" race, with decay on and the clock moving: 100 rows, chain unbroken, nothing lost
  8. the real decay matches the formula 2.3's label checks were written against

This file writes the state only through rivia_emotion's own functions and the dial; it reads emotion.db with
plain selects where it needs the stored rows. (test_changelog.py scans every file here for direct writes.)
"""
import importlib, math, os, sqlite3, sys, tempfile, threading

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ["RIVIA_DIR"] = os.path.join(TMP, "rivia")
sys.path.insert(0, HERE)

import rivia_store as store  # noqa: E402  (after RIVIA_DIR is set)
import rivia_time as rt  # noqa: E402
import rivia_emotion as emo  # noqa: E402
import rivia_emotion_app as eapp  # noqa: E402  (needs starlette)

CLOCK = {"t": 1_800_000_000}
LOCK = threading.Lock()
rt.now_s = lambda: CLOCK["t"]
MIN, HOUR, DAY = 60, 3600, 86400
ANGER = emo.REFERENCE_POINTS["anger"]
passed = 0


def ok(cond, label):
    global passed
    assert cond, label
    passed += 1
    print("  ok ·", label)


def tick(seconds):
    CLOCK["t"] += int(seconds)


def vad(s):
    return (s["v"], s["a"], s["d"])


def mag(p, origin=(0.0, 0.0, 0.0)):
    return math.dist(p, origin)


def stored():
    """The rows as stored on disk, read with a plain select (no decay)."""
    con = sqlite3.connect(store.db_path("emotion"))
    try:
        return {r[0]: r[1:] for r in con.execute("select layer, v, a, d, updated from state")}
    finally:
        con.close()


def table(name):
    con = sqlite3.connect(store.db_path("emotion"))
    try:
        return con.execute("select * from %s order by rowid" % name).fetchall()
    finally:
        con.close()


def log_count():
    return len(table("change_log"))


def fresh():
    """A new, empty emotion.db, at the current faked time."""
    for f in os.listdir(store.rivia_dir()) if os.path.isdir(store.rivia_dir()) else []:
        os.remove(os.path.join(store.rivia_dir(), f))
    emo.db_open().close()


def dial(body):
    status, reply = eapp.handle(body)
    assert status == 200, (status, reply)
    return reply


def by_hand(p):
    return dial({"set": {"v": p[0], "a": p[1], "d": p[2]}})


# ── 1 ──
print("1. anger set by hand reads about half as strong 10 minutes later")
fresh()
r = by_hand(ANGER)
ok(r["new"]["label"] == "anger" and r["new"]["intensity"] == "moderate", "the dial sets her to anger: moderate anger")
tick(10 * MIN)
now = emo.read()
ratio = mag(vad(now)) / mag(ANGER)
lab = emo.label(*vad(now))
ok(abs(ratio - 0.5) < 1e-9, "10 minutes on (mood and baseline at 0) she is at %.4f of the strength she was set to" % ratio)
ok(lab["name"] == "anger" and lab["intensity"] == "mild", "still anger, now mild (%.3f from 0)" % lab["magnitude"])
g = eapp.get_reply()
ok(g["state"]["label"] == "anger" and g["state"]["intensity"] == "mild" and abs(g["state"]["v"] - now["v"]) < 1e-12,
   "the app's GET shows the same faded anger")
ok(stored()["emotion"][:3] == ANGER, "the stored row still holds what was set: reading never wrote")
# the same with a mood underneath: the gap between emotion and mood is what halves
fresh()
emo.write(*[0.3 * x for x in emo.REFERENCE_POINTS["contentment"]], cause="test mood", door="code", layer="mood")
by_hand(ANGER)
m0 = vad(emo.read(layer="mood"))
tick(10 * MIN)
gap_ratio = mag(vad(emo.read()), vad(emo.read(layer="mood"))) / mag(ANGER, m0)
ok(abs(gap_ratio - 0.5) < 0.01, "on top of a content mood, the anger's distance from mood halves in 10 minutes (%.4f)" % gap_ratio)

# ── 2 ──
print("2. 4 hours on")
fresh()
by_hand(ANGER)
tick(4 * HOUR)
e, m = vad(emo.read()), vad(emo.read(layer="mood"))
ok(mag(e) < 1e-6 and emo.label(*e)["name"] == "neutral",
   "an anger spike with mood at baseline is gone after 4 hours: %.1e from baseline, reads neutral" % mag(e))
ok(m == (0.0, 0.0, 0.0), "mood never moved, so it is at baseline")
fresh()
emo.set_anchor(*[0.4 * x for x in emo.REFERENCE_POINTS["calm"]], cause="test baseline", door="code")
b = vad(emo.read(layer="baseline"))
m0 = tuple(0.5 * x for x in emo.REFERENCE_POINTS["sadness"])
emo.write(*m0, cause="test mood", door="code", layer="mood")
tick(4 * HOUR)
m4 = vad(emo.read(layer="mood"))
half = tuple(bb + (mm - bb) / 2 for mm, bb in zip(m0, b))
ok(math.dist(m4, half) < 1e-9, "a mood set by hand has covered exactly half its way to baseline after 4 hours")
lag = math.dist(vad(emo.read()), m4)
ok(lag < 0.05 * math.dist(m4, b), "emotion has followed mood, trailing it by %.4f (2.2: emotion lags a fading mood)" % lag)
tick(20 * HOUR)
m24 = vad(emo.read(layer="mood"))
left = math.dist(m24, b) / math.dist(m0, b)
ok(left < 0.02, "a day after it was set, mood is near baseline: %.1f%% of its starting distance left" % (100 * left))
ok(emo.label(*vad(emo.read()), rest=b)["name"] == "neutral" and eapp.get_reply()["state"]["label"] == "neutral",
   "and at her normal she reads neutral, on the label and the app (2.3), though her baseline alone is %s"
   % emo.label(*b)["name"])
ok(emo.label(*b)["name"] != "neutral", "(the baseline point on its own is not neutral, so the rule was tested)")

# ── 3 ──
print("3. a restart resets nothing")
fresh()
emo.set_anchor(0.05, 0.0, 0.0, cause="test baseline", door="code")
emo.write(0.2, -0.1, 0.0, cause="test mood", door="code", layer="mood")
by_hand(ANGER)
emo.nudge(0.01, 0, 0, cause="drift test", door="code", layer="baseline")
tick(3 * MIN)
before = {"read": emo.read_all(), "stored": stored(), "log": table("change_log"), "anchor": emo.read_anchor(),
          "track": table("baseline_track"), "room": emo.baseline_room()}
emo = importlib.reload(emo)  # a new process: module state gone, constants back to their defaults
eapp = importlib.reload(eapp)
con = emo.db_open()  # reopened: db_open runs every migration and default again
con.close()
after = {"read": emo.read_all(), "stored": stored(), "log": table("change_log"), "anchor": emo.read_anchor(),
         "track": table("baseline_track"), "room": emo.baseline_room()}
for k in ("stored", "log", "anchor", "track"):
    ok(after[k] == before[k], "%s: unchanged by the restart" % k)
ok(after["read"] == before["read"], "she reads exactly the same right after the restart")
ok(after["room"] == before["room"], "the drift limits' windows are unchanged (baseline_room)")
tick(7 * MIN)  # the server is off for these 7 minutes (10 since the dial press); time off counts the same
ok(abs(mag(vad(emo.read()), vad(emo.read(layer="mood"))) / mag(ANGER, (0.2, -0.1, 0.0)) - 0.5) < 0.01,
   "10 minutes after the press, across the restart, the anger has halved toward mood")

# ── 4 ──
print("4. the dial moves emotion freely; mood and baseline only behind the lock, tagged")
fresh()
emo.set_anchor(0.1, 0.0, 0.05, cause="test baseline", door="code")
emo.write(-0.2, 0.1, 0.0, cause="test mood", door="code", layer="mood")
tick(1 * HOUR)  # let mood fade a while, so a press that wrote it back would show
eapp.handle({"lock": True})
rows0, n0 = stored(), log_count()
dial({"set": {"v": 0.4, "a": 0.4, "d": 0.4}})
tick(2 * MIN)
dial({"nudge": {"dv": -0.1}})
rows1 = stored()
ok(rows1["mood"] == rows0["mood"] and rows1["baseline"] == rows0["baseline"],
   "a set and a nudge on the emotion dial leave the mood and baseline rows exactly as stored")
new_rows = table("change_log")[n0:]
ok(len(new_rows) == 2 and all(row[2] == "emotion" for row in new_rows), "two log rows, both for emotion")
ok(all(h["tag"] is None for h in emo.history(2)), "emotion dial rows carry no tag")
for body in ({"layer": "mood", "set": {"v": 0, "a": 0, "d": 0}, "tag": "test"},
             {"layer": "baseline", "nudge": {"dv": 0.1}, "tag": "test"}):
    status, reply = eapp.handle(body)
    ok(status == 400 and reply["code"] == "locked" and stored() == rows1, "while locked, a press on %s is refused, nothing moves"
       % body["layer"])
g = eapp.get_reply()
ok(g["dial"]["moves"] == ["emotion", "mood", "baseline"] and g["dial"]["locked"] == ["mood", "baseline"]
   and g["lock"]["locked"], "GET: emotion moves freely, mood and baseline sit behind the lock, which is shut")
ok(abs(g["layers"]["mood"]["v"] - emo.read(layer="mood")["v"]) < 1e-12 and g["layers"]["baseline"]["v"] == 0.1,
   "GET shows mood (faded) and the baseline as they read now")
dial({"unlock": True})
for body, code in (({"layer": "mood", "nudge": {"dv": 0.1}}, "tag_needed"),
                   ({"layer": "mood", "nudge": {"dv": 0.1}, "tag": "calibration"}, "bad_tag")):
    status, reply = eapp.handle(body)
    ok(status == 400 and reply["code"] == code and stored() == rows1, "unlocked, a mood press %s is refused (%s)"
       % ("with no tag" if code == "tag_needed" else "tagged calibration", code))
m_before = vad(emo.read(layer="mood"))
dial({"layer": "mood", "nudge": {"dv": 0.3}, "tag": "test"})
tick(3 * MIN)
dial({"layer": "mood", "nudge": {"da": -0.2}, "tag": "test"})
mk = emo.manual_marker("mood")
ok(mk and mk["tag"] == "test" and eapp.get_reply()["layers"]["mood"]["manual"]["tag"] == "test",
   "a mood test shows as an open test, on the layer and in GET")
dial({"lock": True})
r = dial({"end_test": "mood"})
ok(r["tag"] == "test_end" and math.dist(vad(r["new"]), m_before) < 1e-12,
   "end_test (allowed while locked) puts mood back to where it read when the test began")
ok(emo.manual_marker("mood") is None, "and the test marker is gone")
dial({"unlock": True})
b0, track0 = vad(emo.read(layer="baseline")), table("baseline_track")
r = dial({"layer": "baseline", "set": {"v": 0.9, "a": 0.0, "d": 0.0}, "tag": "test"})
h = emo.history(1)[0]
ok(r["new"]["v"] == 0.9 and h["limited"][0] == "skipped" and "anchor" in h["limited"],
   "a baseline test skips the drift limits and logs which ones it would have hit: %s" % h["limited"])
ok(table("baseline_track") == track0, "a test leaves the drift windows alone")
dial({"end_test": "baseline"})
ok(vad(emo.read(layer="baseline")) == b0 and table("baseline_track") == track0,
   "end_test puts the baseline back, still with no jump in the drift windows")
n1 = log_count()
r = dial({"layer": "baseline", "set": {"v": 0.35, "a": 0.0, "d": 0.0}, "tag": "calibration"})
ok(vad(emo.read(layer="baseline")) == (0.35, 0.0, 0.0) and vad(emo.read_anchor()) == (0.35, 0.0, 0.0),
   "a baseline calibration from the app goes past the limits and moves the anchor with it (2.7)")
new_rows = emo.history(log_count() - n1)
ok(sorted(x["layer"] for x in new_rows) == ["anchor", "baseline"] and all(x["tag"] == "calibration" for x in new_rows),
   "logged as an anchor row and a baseline row, both tagged calibration")
ok(emo.manual_marker("baseline")["tag"] == "calibration", "and the baseline shows as set by hand")
dial({"lock": True})
eapp = importlib.reload(eapp)
ok(eapp.get_reply()["lock"]["locked"], "a server restart leaves the lock shut")

# ── 5 ──
print("5. the baseline drift limits hold, with decay on")
fresh()
anchor = vad(emo.read_anchor())
track = [(CLOCK["t"], vad(emo.read(layer="baseline")))]
worst = {"anchor": 0.0, "day": 0.0, "week": 0.0}
for i in range(240):  # every 6 hours for 60 days, a push of 0.01 in v, while she feels things
    tick(6 * HOUR)
    if i % 4 == 0:
        by_hand(tuple(x * (1 if i % 8 else -1) for x in ANGER))
    emo.nudge(0.01, 0, 0, cause="steady push", door="code", layer="baseline")
    t, p = CLOCK["t"], vad(emo.read(layer="baseline"))
    track.append((t, p))

    def at(t0):
        return [q for tt, q in track if tt <= t0][-1] if track[0][0] <= t0 else track[0][1]
    worst["anchor"] = max(worst["anchor"], math.dist(p, anchor))
    worst["day"] = max(worst["day"], math.dist(p, at(t - DAY)))
    worst["week"] = max(worst["week"], math.dist(p, at(t - 7 * DAY)))
eps = 1e-9
ok(worst["anchor"] <= emo.BASELINE_MAX_FROM_ANCHOR + eps, "never more than %.2f from the anchor (worst %.4f)"
   % (emo.BASELINE_MAX_FROM_ANCHOR, worst["anchor"]))
ok(worst["day"] <= emo.BASELINE_MAX_PER_DAY + eps, "never more than %.2f in a day (worst %.4f)"
   % (emo.BASELINE_MAX_PER_DAY, worst["day"]))
ok(worst["week"] <= emo.BASELINE_MAX_PER_WEEK + eps, "never more than %.2f in a week (worst %.4f)"
   % (emo.BASELINE_MAX_PER_WEEK, worst["week"]))
ok(worst["anchor"] > emo.BASELINE_MAX_FROM_ANCHOR - 0.01, "and the push did reach the edge, so the limit was tested")
r = emo.write(1, 1, 1, cause="big jump", door="code", layer="baseline")
ok(r["limited"] and r["new"]["v"] < 0.3, "a jump to the corner is cut short and names the limits it hit: %s" % r["limited"])
lim = vad(emo.read(layer="baseline"))
emo.write(0.9, 0.9, 0.9, cause="mood far off", door="code", layer="mood")
tick(5 * DAY)
ok(math.dist(vad(emo.read(layer="mood")), lim) < 0.01, "mood fades to the limited baseline, not the asked-for corner")
r = emo.write(-1, -1, -1, cause="emotion is not limited", door="code")
ok(r["limited"] == [] and vad(r["new"]) == (-1.0, -1.0, -1.0), "emotion is not limited")
r = emo.write(-1, -1, -1, cause="mood is not limited", door="code", layer="mood")
ok(r["limited"] == [] and vad(r["new"]) == (-1.0, -1.0, -1.0), "mood is not limited")
try:
    emo.set_anchor(0, 0, 0, cause="from the phone", door="app_button")
    refused = False
except emo.EmotionError as e:
    refused = e.code == "not_allowed"
ok(refused, "the phone app's door may not move the anchor without the calibration tag")
emo.set_anchor(0.3, 0, 0, cause="re-measured", door="admin")
ok(vad(emo.read(layer="baseline")) == (0.3, 0.0, 0.0) and vad(emo.read_anchor()) == (0.3, 0.0, 0.0),
   "the admin door may re-anchor, past the limits, moving the baseline with it")

# ── 6 ──
print("6. fading alone writes no change_log row")
fresh()
emo.write(0.3, 0.2, 0.0, cause="test mood", door="code", layer="mood")
by_hand(ANGER)
rows0, log0, track0 = stored(), table("change_log"), table("baseline_track")
for step in [1, 59, 10 * MIN, HOUR, 4 * HOUR, DAY, DAY]:  # about two days of reading, nothing changing her
    tick(step)
    emo.read(), emo.read_all(), emo.read(layer="mood"), emo.label(*vad(emo.read()))
    eapp.get_reply(), emo.baseline_room(), emo.history(5), emo.read_anchor()
    emo.db_open().close()
ok(table("change_log") == log0, "two days of reads, GETs, labels and opens: no new change_log row")
ok(stored() == rows0 and table("baseline_track") == track0, "and no stored row changed")
faded = emo.read()
r = emo.nudge(0.1, 0, 0, cause="first real change", door="code")
row = emo.history(1)[0]
f = emo.fade(row)
ok(len(table("change_log")) == len(log0) + 1, "the next real change writes exactly one row")
ok(vad({k: row["fade_from_" + k] for k in "vad"}) == ANGER and row["fade_toward"] == "mood",
   "it records the fade from the stored anger, toward mood")
ok(all(abs(row["old_" + k] - faded[k]) < 1e-12 for k in "vad") and abs(row["new_v"] - (faded["v"] + 0.1)) < 1e-12,
   "old is where she had faded to; the nudge moved her from there")
ok(f["seconds"] == 1 + 59 + 10 * MIN + HOUR + 4 * HOUR + 2 * DAY, "fade() gives the time since the stored value (%d s)"
   % f["seconds"])

# ── 7 ──
print("7. Step 1's 'no nudge lost' race, with decay on")
fresh()
emo.write(0, 0, 0, cause="race start", door="code")
n0 = log_count()
errs = []


def moving_clock():
    with LOCK:  # each call a second later, so every nudge in the race decays a little from the one before
        CLOCK["t"] += 1
        return CLOCK["t"]


rt.now_s = moving_clock


def worker():
    for _ in range(25):
        try:
            emo.nudge(0.004, -0.002, 0, cause="race", door="code")
        except Exception as e:  # noqa: BLE001
            errs.append(e)


ts = [threading.Thread(target=worker) for _ in range(4)]
[t.start() for t in ts]
[t.join() for t in ts]
rt.now_s = lambda: CLOCK["t"]
rows = list(reversed(emo.history(10 ** 6)))[n0 - 1:]  # the race-start row, then the race
race = rows[1:]
ok(not errs and len(race) == 100, "4 threads x 25 nudges: 100 rows, no errors")
links = all((b["fade_from_v"], b["fade_from_a"], b["fade_from_d"]) == (a["new_v"], a["new_a"], a["new_d"])
            and b["fade_from_at"] == a["at"] for a, b in zip(rows, rows[1:]))
ok(links, "chain unbroken: each row's fade_from is the previous row's new, at its time")
mood0 = (0.0, 0.0, 0.0)
replay_ok = True
for row in race:
    gap = rt.to_epoch(row["at"]) - rt.to_epoch(row["fade_from_at"])
    want_old = tuple(x * 2 ** (-gap / emo.EMOTION_HALF_LIFE_S) for x in (row["fade_from_v"], row["fade_from_a"],
                                                                          row["fade_from_d"]))
    replay_ok &= all(abs(o - w) < 1e-12 for o, w in zip((row["old_v"], row["old_a"], row["old_d"]), want_old))
    replay_ok &= abs(row["new_v"] - (row["old_v"] + 0.004)) < 1e-12 and abs(row["new_a"] - (row["old_a"] - 0.002)) < 1e-12
ok(replay_ok, "every row starts where the previous one had faded to, and adds its whole nudge: no nudge lost")
ok(0.35 < race[-1]["new_v"] < 0.4, "v ends at %.4f: under 0.4 by what faded during the race" % race[-1]["new_v"])

# ── 8 ──
print("8. the real decay matches the formula 2.3's label checks used")
J, K = math.log(2) / 10, math.log(2) / 240  # per minute: 10 min and 4 h half-lives, as in tests/test_label_layers.py
e0, m0, b0 = ANGER, tuple(0.5 * x for x in emo.REFERENCE_POINTS["contentment"]), (0.05, -0.02, 0.0)
rows_ = {"emotion": dict(zip("vad", e0), updated=rt.iso(CLOCK["t"])), "mood": dict(zip("vad", m0), updated=rt.iso(CLOCK["t"])),
         "baseline": dict(zip("vad", b0), updated=rt.iso(CLOCK["t"]))}
worst8 = 0.0
for t in (0, 2, 5, 10, 30, 60, 240, 1440):
    got = emo.decayed(rows_, CLOCK["t"] + 60 * t)
    for i, k in enumerate("vad"):
        m = b0[i] + (m0[i] - b0[i]) * math.exp(-K * t)
        e = b0[i] + (m0[i] - b0[i]) * J / (J - K) * (math.exp(-K * t) - math.exp(-J * t)) + (e0[i] - b0[i]) * math.exp(-J * t)
        worst8 = max(worst8, abs(got["emotion"][k] - e), abs(got["mood"][k] - m))
ok(worst8 < 1e-12, "the same numbers to 1e-12 at every time from 0 to a day (worst %.1e)" % worst8)
fresh()
emo.write(0.4, 0, 0, cause="test", door="code")
tick(5 * MIN)
emo.write(0.1, 0, 0, cause="mood change settles emotion", door="code", layer="mood")
tick(5 * MIN)
emo.nudge(0.1, 0, 0, cause="next emotion change", door="code")
h = emo.history(3)
ok(h[0]["fade_from_at"] == h[1]["at"] and h[0]["fade_from_at"] != h[2]["at"],
   "after a mood change settles emotion, the next emotion row's fade_from_at is the settle time (no row of its own)")

print("PASS (%d checks)" % passed)
