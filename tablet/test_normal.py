"""Step 4.3: her own normal per body signal. Rolling average now vs the median of her last week, a warm-up before
it is trusted, SQLite, survives restarts, reads never write. All data here is fake.
The clock is faked by replacing rivia_time.now_s, so no test waits. Throwaway RIVIA_DIR; never touches ~/rivia.
    python test_normal.py
"""
import ast, importlib, math, os, random, sqlite3, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ["RIVIA_DIR"] = os.path.join(TMP, "rivia")
sys.path.insert(0, HERE)

import rivia_store as store  # noqa: E402  (after RIVIA_DIR is set)
import rivia_time as rt  # noqa: E402
import rivia_normal as bn  # noqa: E402

START = 1_800_000_000 - 1_800_000_000 % bn.BUCKET_S  # a bucket boundary, whole seconds throughout
CLOCK = {"t": START}
rt.now_s = lambda: CLOCK["t"]
MIN, HOUR, DAY = 60, 3600, 86400


def ok(cond, label):
    assert cond, label
    print("  ok ·", label)


def near(x, y, tol=1e-9):
    return x is not None and y is not None and abs(x - y) <= tol


def fresh():
    """A new empty body.db and the clock back at START."""
    p = store.db_path(bn.DB)
    for suffix in ("", "-wal", "-shm", "-journal"):
        if os.path.exists(p + suffix):
            os.remove(p + suffix)
    CLOCK["t"] = START


def feed(signal, fn, seconds, every=MIN, batch=HOUR):
    """Fake collector: one sample every `every` seconds for `seconds`, value fn(t, i). Sent an hour at a time,
    with the clock moved to each batch's last sample, the way a real collector would arrive."""
    t0 = CLOCK["t"]
    rows, i = [], 0
    t = t0 + every
    while t <= t0 + seconds:
        rows.append((signal, fn(t, i), t))
        i += 1
        if t - rows[0][2] >= batch - every or t + every > t0 + seconds:
            CLOCK["t"] = t
            out = bn.record_many(rows)
            assert not out["rejected"], out["rejected"][:3]
            rows = []
        t += every
    CLOCK["t"] = t0 + seconds


def noisy(mu, sd, seed):
    r = random.Random(seed)
    return lambda t, i: r.gauss(mu, sd)


def counts():
    con = sqlite3.connect(store.db_path(bn.DB))
    out = (con.execute("select count(*) from samples").fetchone()[0],
           con.execute("select count(*) from buckets").fetchone()[0])
    con.close()
    return out


print("1. empty and unknown")
fresh()
ok(bn.normals() == {}, "a new body.db has no signals")
n = bn.normal("server_errors")
ok(n["recent"] is None and n["usual"] is None and n["deviation"] is None, "an unheard-of signal reads all None")
ok(n["stale"] and not n["trusted"], "and is stale and untrusted")

print("2. bad samples are refused, good ones in the same batch are kept")
fresh()
for args, code in [(("Server Errors", 1), "bad_signal"), (("", 1), "bad_signal"), ((None, 1), "bad_signal"),
                   (("x" * 49, 1), "bad_signal"), (("temp", float("nan")), "bad_value"),
                   (("temp", float("inf")), "bad_value"), (("temp", True), "bad_value"), (("temp", "35"), "bad_value"),
                   (("temp", 35, START + 61), "future"), (("temp", 35, START - 2 * DAY - 1), "too_old"),
                   (("temp", 35, "yesterday"), "bad_time")]:
    try:
        bn.record(*args)
        ok(False, "refused %r" % (args,))
    except bn.BodyError as e:
        ok(e.code == code, "%r refused as %s" % (args[:2], code))
ok(counts() == (0, 0), "nothing stored by any refused sample")
out = bn.record_many([("temp", 35, START - 60), ("temp", "hot", START - 30), ("temp", 36, None), ("bad",)])
ok(out["stored"] == 2 and [r[0] for r in out["rejected"]] == [1, 3], "a batch stores its good rows, refuses the bad")
ok(bn.record("temp", 35, START + 60), "a sample up to a minute ahead is allowed (clock skew)")
ok(bn.record("temp", 35, rt.iso(START - 120)), "ISO times are accepted")
ok(bn.record("temp", 35, (START - 180) * 1000), "epoch ms are accepted")

print("3. duplicates are ignored (a collector can resend after a restart)")
fresh()
ok(bn.record("temp", 35, START - 10) is True, "first copy stored")
ok(bn.record("temp", 99, START - 10) is False, "same signal, same second: ignored")
out = bn.record_many([("temp", 35, START - 10), ("temp", 35, START - 5)])
ok(out == {"stored": 1, "duplicate": 1, "rejected": []}, "batch counts duplicates")
con = bn.db_open()
b = con.execute("select n, total from buckets where signal='temp'").fetchall()
con.close()
ok(sum(r[0] for r in b) == 2 and near(sum(r[1] for r in b), 70.0), "the bucket counted each sample once")

print("4. warm-up: a few days before it is trusted")
fresh()
feed("temp", noisy(35, 0.5, 1), 2 * DAY)
n = bn.normal("temp")
ok(not n["trusted"] and n["deviation"] is None, "after 2 days: not trusted, no deviation")
ok(n["preview_deviation"] is not None and abs(n["preview_deviation"]) < 3, "but the preview is there for the shadow log")
ok(not n["warmup"]["done"] and n["warmup"]["span_s"] < bn.WARMUP_S, "the warm-up says why (span under 3 days)")
feed("temp", noisy(35, 0.5, 2), 1 * DAY + 30 * MIN)
n = bn.normal("temp")
ok(n["trusted"] and n["warmup"]["done"], "after 3 days of steady data: trusted")
ok(near(n["usual"], 35, 0.1), "her normal is 35 (%.3f)" % n["usual"])
ok(abs(n["deviation"]) < 2.5, "a normal reading sits near 0 (%.2f)" % n["deviation"])

fresh()  # 3 days apart, but only a day of data in between: span is met, the count is not
feed("temp", noisy(35, 0.5, 3), 12 * HOUR)
CLOCK["t"] += 2 * DAY + HOUR
feed("temp", noisy(35, 0.5, 4), 12 * HOUR)
n = bn.normal("temp")
ok(n["warmup"]["span_s"] >= bn.WARMUP_S and n["warmup"]["buckets"] < bn.MIN_BUCKETS and not n["trusted"],
   "3 days apart with gaps: still warming (%d of %d buckets)" % (n["warmup"]["buckets"], bn.MIN_BUCKETS))

print("5. compared to her own normal, not fixed thresholds")
fresh()
feed("temp", noisy(35, 0.5, 5), 4 * DAY)            # tablet temperature, about 35
CLOCK["t"] = START
feed("response_ms", noisy(200, 20, 6), 4 * DAY)     # response time, about 200 ms
CLOCK["t"] = START + 4 * DAY
base = {s: bn.normal(s) for s in ("temp", "response_ms")}
ok(all(b["trusted"] for b in base.values()), "both signals warmed up on their own data")
feed("temp", lambda t, i: 40.0, 15 * MIN)
t40 = bn.normal("temp")
feed("response_ms", lambda t, i: 40.0, 15 * MIN)
r40 = bn.normal("response_ms")
ok(t40["deviation"] >= 4, "40 on temperature (usual 35) reads well above normal (%.1f)" % t40["deviation"])
ok(r40["deviation"] < -5, "the same 40 on response time (usual 200) reads well below normal (%.1f)" % r40["deviation"])
feed("temp", lambda t, i: 30.0, 15 * MIN)
ok(bn.normal("temp")["deviation"] <= -4, "and a drop reads negative: direction only, 4.4 decides what is bad")

print("6. the recent window is a rolling 15-minute average")
fresh()
feed("temp", noisy(35, 0.5, 7), 4 * DAY)
feed("temp", lambda t, i: 38.0, 5 * MIN)   # 5 minutes high, 10 minutes of normal before it in the window
n = bn.normal("temp")
ok(5 <= n["recent_n"] <= 16 and 35.5 < n["recent"] < 37.5, "5 minutes of 38 inside 15 minutes averages part way (%.2f)"
   % n["recent"])
feed("temp", lambda t, i: 38.0, 15 * MIN)
ok(near(bn.normal("temp")["recent"], 38.0), "after a full window at 38 the recent average is 38")
ok(near(bn.normal("temp")["usual"], 35, 0.1), "and the spike now is not in her usual (%.3f)" % bn.normal("temp")["usual"])

print("7. a few bad hours don't move her normal (median, not mean)")
fresh()
feed("temp", noisy(35, 0.5, 8), 3 * DAY)
feed("temp", lambda t, i: 60.0, 6 * HOUR)       # a six-hour heat spike
feed("temp", noisy(35, 0.5, 9), 1 * DAY)
n = bn.normal("temp")
ok(near(n["usual"], 35, 0.3), "usual stays near 35 after a 6-hour spike to 60 (%.3f)" % n["usual"])
ok(n["spread"] < 1.5, "spread stays small too (%.3f)" % n["spread"])
ok(abs(n["deviation"]) < 3, "so a normal reading still reads normal (%.2f)" % n["deviation"])

print("8. a signal with no bad side learns a lasting change as her new normal (section 18 holds the rest)")
fresh()
feed("storage_pct", noisy(50, 0.3, 10), 4 * DAY)
feed("storage_pct", noisy(70, 0.3, 11), 2 * HOUR)
ok(bn.normal("storage_pct")["deviation"] == bn.DEVIATION_CLIP, "a jump to 70 reads far above normal at first")
feed("storage_pct", noisy(70, 0.3, 12), 5 * DAY)
n = bn.normal("storage_pct")
ok(near(n["usual"], 70, 0.3) and abs(n["deviation"]) < 3, "after 5 days at 70 it is her normal (%.2f)" % n["deviation"])
fresh()
feed("storage_pct", lambda t, i: 50 + 40 * (t - START) / (10 * DAY), 10 * DAY)   # slow creep, 50 to 90 over 10 days
n = bn.normal("storage_pct")
ok(n["recent"] > 89 and n["deviation"] < 5,
   "a slow creep to 90%% reads only %.1f on a plain normal: why section 18 exists" % n["deviation"])

print("9. flat signals: the spread floor keeps one blip finite")
fresh()
feed("server_errors", lambda t, i: 0.0, 4 * DAY)
n = bn.normal("server_errors")
ok(n["trusted"] and n["usual"] == 0 and n["spread"] == bn.DEFAULT_ABS_SPREAD_FLOOR, "usually 0: spread falls to the floor")
ok(n["deviation"] == 0, "zero errors reads exactly normal")
feed("server_errors", lambda t, i: 1.0 if i == 0 else 0.0, 15 * MIN)
n = bn.normal("server_errors")
ok(0 < n["deviation"] < 0.2, "one error in 15 minutes is a small reading, not infinite (%.3f)" % n["deviation"])
feed("server_errors", lambda t, i: 50.0, 15 * MIN)
ok(bn.normal("server_errors")["deviation"] == bn.DEVIATION_CLIP, "a flood is clipped to %.0f" % bn.DEVIATION_CLIP)
ok(bn.normal("server_errors")["floor_set"] is False, "the reading says the default floor is in use")
bn.ABS_SPREAD_FLOOR["server_errors"] = 10.0
ok(bn.normal("server_errors")["deviation"] == 5.0, "a per-signal floor (4.1's to set) changes the scale")
ok(bn.normal("server_errors")["floor_set"] is True, "and the reading says so")
del bn.ABS_SPREAD_FLOOR["server_errors"]

print("10. stale: no sample in the window means no reading")
fresh()
feed("temp", noisy(35, 0.5, 13), 4 * DAY)
CLOCK["t"] += 16 * MIN
n = bn.normal("temp")
ok(n["stale"] and not n["trusted"] and n["deviation"] is None and n["recent"] is None, "16 minutes quiet: stale")
ok(n["last_at"] is not None and CLOCK["t"] - n["last_at"] > 15 * MIN, "and it says when it last heard")
bn.RECENT_S_FOR["temp"] = HOUR
ok(bn.normal("temp")["trusted"], "a signal given an hour's window (sampled slowly) is not stale yet")
del bn.RECENT_S_FOR["temp"]
CLOCK["t"] += 3 * DAY
n = bn.normal("temp")
ok(n["last_at"] is not None, "after raw samples are gone, last_at still comes from the buckets")

print("11. survives a restart")
fresh()
feed("temp", noisy(35, 0.5, 14), 4 * DAY)
feed("temp", lambda t, i: 37.0, 15 * MIN)
before = bn.normals()
importlib.reload(bn)
rt.now_s = lambda: CLOCK["t"]
after = bn.normals()
ok(before == after, "a fresh module and connection read exactly the same")

print("12. reads never write")
s0 = counts()
mtime = os.path.getmtime(store.db_path(bn.DB))
con = bn.db_open()
for _ in range(5):
    bn.normals(con)
    bn.normal("temp", con)
    bn.signals(con)
ok(con.total_changes == 0, "no row changed by any read")
con.close()
ok(counts() == s0 and os.path.getmtime(store.db_path(bn.DB)) == mtime, "row counts and file unchanged")
CLOCK["t"] += 10 * DAY
bn.normals()
ok(counts() == s0, "even reads long after: pruning waits for the next write")

print("13. pruning on write: raw after 2 days, buckets after 30")
fresh()
feed("temp", noisy(35, 0.5, 15), 32 * DAY, every=5 * MIN, batch=6 * HOUR)
con = bn.db_open()
oldest_raw = con.execute("select min(at) from samples").fetchone()[0]
oldest_b = con.execute("select min(start) from buckets").fetchone()[0]
con.close()
ok(CLOCK["t"] - oldest_raw <= bn.RAW_KEEP_S, "no raw sample older than 2 days")
ok(CLOCK["t"] - oldest_b <= bn.BUCKET_KEEP_S, "no bucket older than 30 days")
n = bn.normal("temp")
ok(n["trusted"] and near(n["usual"], 35, 0.1) and n["usual_buckets"] > 600,
   "her normal still comes from a full week of buckets (%d)" % n["usual_buckets"])

print("14. buckets match the raw data, whatever order it arrives in")
fresh()
r = random.Random(16)
rows = [("x.y", r.uniform(-5, 5), START - r.randrange(0, 2 * DAY - 60)) for _ in range(3000)]
rows = list({(s, t): (s, v, t) for s, v, t in rows}.values())
half = len(rows) // 2
bn.record_many(rows[:half])
bn.record_many(list(reversed(rows[half:])))
con = bn.db_open()
got = {s: (n, tot) for s, n, tot in con.execute("select start, n, total from buckets")}
con.close()
want = {}
for _, v, t in rows:
    k = t - t % bn.BUCKET_S
    n0, tot0 = want.get(k, (0, 0.0))
    want[k] = (n0 + 1, tot0 + v)
ok(got.keys() == want.keys() and all(got[k][0] == want[k][0] and near(got[k][1], want[k][1], 1e-6) for k in want),
   "every bucket's count and total match a direct sum (%d buckets)" % len(want))

print("15. median and spread against a hand-worked case")
ok(bn._median([3, 1, 2]) == 2 and bn._median([4, 1, 3, 2]) == 2.5 and bn._median([]) is None, "median")
ok(near(bn._spread("s", [10, 12, 14, 16, 100], 14), bn.MAD_TO_SD * 2), "spread is 1.4826 x MAD (2 here)")
ok(near(bn._spread("s", [100, 100, 100], 100), 2.0), "relative floor: 2% of 100")

print("16. it stays in its lane")
src = open(os.path.join(HERE, "rivia_normal.py")).read()
imports = {a.name for node in ast.walk(ast.parse(src)) if isinstance(node, (ast.Import, ast.ImportFrom))
           for a in (node.names if isinstance(node, ast.Import) else [ast.alias(node.module)])}
ok(imports == {"math", "re", "rivia_store", "rivia_time"}, "imports only math, re, rivia_store, rivia_time")
ok(not os.path.exists(store.db_path("emotion")), "emotion.db was never created or touched")
ok("mcp" not in src.lower().replace("not a gemini tool", ""), "no MCP tool is registered here (4.8 owns tools)")

print("18. worse never becomes her normal (Jordan, Nov 22)")
def anchors():
    con = sqlite3.connect(store.db_path(bn.DB))
    rows = {r[0]: r[1:] for r in con.execute("select signal, usual, spread, set_at, cause from anchors")}
    con.close()
    return rows
fresh()
feed("error_rate", noisy(2, 1, 20), 4 * DAY)
CLOCK["t"] = START
feed("err_plain", noisy(2, 1, 20), 4 * DAY)       # same data, a name with no bad side
a = anchors()
ok("error_rate" in a and "err_plain" not in a, "a held normal is set once warm-up ends, only for signals with a bad side")
ok(a["error_rate"][3] == "first normal" and near(a["error_rate"][0], 2, 0.2), "it holds her first normal (%.2f/h)"
   % a["error_rate"][0])
day4 = CLOCK["t"]
feed("error_rate", noisy(8, 1, 21), 6 * DAY)
CLOCK["t"] = day4
feed("err_plain", noisy(8, 1, 21), 6 * DAY)
e, p_ = bn.normal("error_rate"), bn.normal("err_plain")
ok(near(p_["usual"], 8, 0.3) and abs(p_["deviation"]) < 2, "a plain normal has learned 8 errors an hour as normal (%.2f)"
   % p_["deviation"])
ok(e["held"] and near(e["rolling_usual"], 8, 0.3) and near(e["usual"], 2, 0.2),
   "error_rate's rolling normal slid to 8, but she is still measured against 2")
ok(e["deviation"] >= 2.5, "so 6 days of 8 errors an hour still read as worse (%.2f)" % e["deviation"])

fresh()
feed("storage_free", lambda t, i: 60 - 40 * (t - START) / (10 * DAY), 10 * DAY)   # slow slide, 60% free to 20%
n = bn.normal("storage_free")
ok(n["bad_side"] == "down" and n["held"] and n["recent"] < 21, "storage_free slid from 60% to 20% free, and is held")
ok(n["deviation"] <= -5, "it reads as far worse than normal (%.1f), not ordinary, though the slide began in warm-up"
   % n["deviation"])

print("19. better is learned, so the held normal can improve")
fresh()
feed("response_time", noisy(300, 30, 22), 4 * DAY)
ok(near(anchors()["response_time"][0], 300, 5), "first normal about 300 ms")
feed("response_time", noisy(200, 30, 23), 5 * DAY)
a = anchors()["response_time"]
ok(a[3] == "got better" and abs(a[0] - 200) < bn.ANCHOR_MARGIN * a[1],
   "after 5 days at 200 ms the held normal moved to %.0f (it settles within half a spread)" % a[0])
n = bn.normal("response_time")
ok(not n["held"] and abs(n["deviation"]) < 2, "and 200 now reads normal (%.2f)" % n["deviation"])
feed("response_time", noisy(300, 30, 24), 5 * DAY)
n = bn.normal("response_time")
ok(n["held"] and n["deviation"] >= 1.2, "going back to 300 for 5 days reads worse, not normal (%.2f)" % n["deviation"])

print("20. ordinary noise doesn't ratchet the held normal")
fresh()
feed("response_time", noisy(200, 30, 25), 20 * DAY, every=2 * MIN)
a = anchors()["response_time"]
ok(abs(a[0] - 200) < bn.ANCHOR_MARGIN * a[1], "20 noisy days: held normal %.1f ms, within half a spread of 200" % a[0])
ok(abs(bn.normal("response_time")["deviation"]) < 1.5, "and an ordinary reading still reads near 0")

print("21. relearn on purpose")
fresh()
feed("storage_free", noisy(60, 0.3, 26), 4 * DAY)
feed("storage_free", noisy(45, 0.3, 27), 4 * DAY)    # say a big install took 15% of the storage, on purpose
ok(bn.normal("storage_free")["held"], "the drop is held as worse")
for door in ("app", "app_button", "gemini", None):
    try:
        bn.relearn("storage_free", door=door, cause="big install")
        ok(False, "door %r refused" % (door,))
    except bn.BodyError as e:
        ok(e.code == "bad_door", "relearn refused from door %r" % (door,))
try:
    bn.relearn("storage_free", door="admin", cause=" ")
    ok(False, "empty cause refused")
except bn.BodyError as e:
    ok(e.code == "bad_cause", "relearn needs a cause")
old = bn.relearn("storage_free", door="admin", cause="big install")
ok(near(old["usual"], 60, 0.5) and "storage_free" not in anchors(), "admin cleared the held normal (it was 60%)")
feed("storage_free", noisy(45, 0.3, 28), 15 * MIN)
a = anchors()["storage_free"]
ok(near(a[0], 45, 0.5) and a[3] == "first normal", "the next write learned 45% free as her normal")
ok(not bn.normal("storage_free")["held"] and abs(bn.normal("storage_free")["deviation"]) < 2, "and it reads normal")
before = bn.normal("storage_free")
importlib.reload(bn)
rt.now_s = lambda: CLOCK["t"]
ok(bn.normal("storage_free") == before, "the held normal survives a restart")

print("22. reads never change the held normal")
fresh()
feed("response_time", noisy(300, 30, 29), 4 * DAY)
feed("response_time", noisy(300, 30, 34), 4 * DAY)   # a warm-up's worth of data since the held normal was set
a0 = anchors()
con = sqlite3.connect(store.db_path(bn.DB))
con.execute("update buckets set total = total * 2 / 3 where signal='response_time'")   # her week now looks like 200
con.commit()
con.close()
for _ in range(3):
    bn.normals()
ok(anchors() == a0, "a better week on read leaves the held normal alone")
CLOCK["t"] += 30
bn.record("response_time", 200.0)
ok(anchors()["response_time"][3] == "got better", "the next write moves it")

print("23. floors in 4.1's units")
ok(set(bn.WORSE) == set(bn.ABS_SPREAD_FLOOR), "every signal with a bad side has its own floor")
ok(all(v > 0 for v in bn.ABS_SPREAD_FLOOR.values()), "every floor is positive")
fresh()
feed("tunnel_unreachable", lambda t, i: 0.0, 4 * DAY)
feed("tunnel_unreachable", lambda t, i: 100.0 if i == 0 else 0.0, 15 * MIN)   # one failed check in 15
n = bn.normal("tunnel_unreachable")
ok(n["floor_set"] and near(n["deviation"], (100 / 15) / 10.0, 1e-6), "one failed check in 15 reads %.2f" % n["deviation"])
feed("tunnel_unreachable", lambda t, i: 100.0, 15 * MIN)
ok(bn.normal("tunnel_unreachable")["deviation"] == bn.DEVIATION_CLIP, "a full outage reads far worse, not normal")


print("24. Jordan can reassure her (Nov 22)")
fresh()
feed("storage_free", noisy(60, 0.3, 30), 4 * DAY)
out0 = bn.reassure("storage_free", door="app_button", cause="too early")
ok(near(out0["new"]["usual"], 60, 0.5), "a warmed-up signal can be reassured at any time")
feed("storage_free", noisy(45, 0.3, 31), 1 * DAY)     # a big install, on purpose
n = bn.normal("storage_free")
ok(n["deviation"] <= -5, "the drop to 45%% free reads as worse (%.1f)" % n["deviation"])
for door in ("app", "gemini", None):
    try:
        bn.reassure("storage_free", door=door, cause="it's fine")
        ok(False, "door %r refused" % (door,))
    except bn.BodyError as e:
        ok(e.code == "bad_door", "she can't reassure herself: door %r refused" % (door,))
out = bn.reassure("storage_free", door="app_button", cause="big install, that's fine")
ok(near(out["new"]["usual"], 45, 0.5) and out["new"]["cause"] == "reassured: big install, that's fine",
   "Jordan's tap made 45% free her normal")
n = bn.normal("storage_free")
ok(abs(n["deviation"]) < 2, "and 45%% now reads normal (%.2f), though most of her week was at 60" % n["deviation"])
feed("storage_free", noisy(38, 0.3, 32), 15 * MIN)
ok(bn.normal("storage_free")["deviation"] <= -4, "sliding further, to 38%, still reads worse")
feed("storage_free", noisy(45, 0.3, 35), 2 * DAY)
ok(near(anchors()["storage_free"][0], 45, 0.5), "the older, fuller days don't undo the reassurance on later writes")
feed("storage_free", noisy(38, 0.3, 32), 15 * MIN)
h = bn.anchor_history("storage_free")
ok([r["cause"] for r in reversed(h)][:3] == ["first normal", "reassured: too early", "reassured: big install, that's fine"]
   and h[0]["door"] == "app_button", "every change to the held normal is logged with who did it")
con = bn.db_open()
for sql in ("update anchor_log set cause='x'", "delete from anchor_log"):
    try:
        con.execute(sql)
        ok(False, "anchor_log refused: " + sql)
    except sqlite3.DatabaseError:
        ok(True, "anchor_log is append-only (%s refused)" % sql.split()[0])
con.close()
try:
    bn.reassure("uptime_s", door="app_button", cause="x")
    ok(False, "uptime_s refused")
except bn.BodyError as e:
    ok(e.code == "no_bad_side", "uptime_s has no held normal to reassure")
try:
    bn.reassure("storage_free", door="app_button", cause="")
    ok(False, "empty cause refused")
except bn.BodyError as e:
    ok(e.code == "bad_cause", "a reassurance needs a reason")
CLOCK["t"] += 20 * MIN
try:
    bn.reassure("storage_free", door="app_button", cause="x")
    ok(False, "stale refused")
except bn.BodyError as e:
    ok(e.code == "not_ready", "no reassuring a signal with no recent reading (she can't see it)")
fresh()
feed("battery", noisy(90, 1, 33), 1 * DAY)
try:
    bn.reassure("battery", door="app_button", cause="x")
    ok(False, "warm-up refused")
except bn.BodyError as e:
    ok(e.code == "not_ready", "no reassuring before the warm-up ends")
bn.relearn("battery", door="admin", cause="nothing held yet")
ok(bn.anchor_history("battery") == [], "a relearn with nothing held logs nothing")

print("26. size and speed (this machine, not the tablet)")
fresh()
names = sorted(bn.WORSE) + ["uptime_s"]   # 4.1's 11 names, so the held normals are updated too
t0 = time.time()
for k, s in enumerate(names):
    CLOCK["t"] = START
    feed(s, noisy(100, 5, 100 + k), 7 * DAY)
wrote = time.time() - t0
t0 = time.time()
allv = bn.normals()
read = time.time() - t0
ok(len(allv) == 11 and all(v["trusted"] for v in allv.values()), "11 signals, a week of 1-a-minute samples each")
size = os.path.getsize(store.db_path(bn.DB))
print("     wrote %d samples in %.1f s; one normals() read %.0f ms; body.db %.1f MB" % (
    11 * 7 * 24 * 60, wrote, read * 1000, size / 1e6))
ok(read < 2.0, "a full read takes under 2 s here")

print("\nall ok")
