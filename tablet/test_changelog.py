"""The change log (Step 1.7): every state change, through write() or nudge(), clamped or not, leaves exactly
one change_log row with old and new V, A, D, the raw requested values and which axes clamped, old and new label,
cause, note, door and time; a refused change leaves none; if the log row can't be written the state change rolls
back with it; the table is append-only; rows chain (each row's fade_from, or old before 2.5, is the previous row's new), even under
concurrent nudges; nothing else in Step 1 writes the state or the log. Throwaway RIVIA_DIR; never touches ~/rivia.
    python test_changelog.py
"""
import math, os, re, sqlite3, sys, tempfile, threading

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ["RIVIA_DIR"] = os.path.join(TMP, "rivia")
sys.path.insert(0, HERE)

import rivia_emotion as emo  # noqa: E402  (after RIVIA_DIR is set)
# Step 2 (2.9 merge): decay is on, so the state moves between two reads a second apart, and a nudge starts
# from the faded value. Every check in this file is about Step 1 behaviour (storing, clamping, the log, the
# label, the doors), which assumes no time passes, so the clock is stopped for this file. Nothing else
# changes. Decay is tested in test_decay.py and test_step2_acceptance.py.
import rivia_time as _rt  # noqa: E402
_STOPPED = _rt.now_s()
_rt.now_s = lambda: _STOPPED

pts = emo.REFERENCE_POINTS
writes = 0  # successful changes so far; the log must hold exactly this many rows


def ok(cond, label):
    assert cond, label
    print("  ok ·", label)


def count():
    con = emo.db_open()
    try:
        return con.execute("select count(*) from change_log").fetchone()[0]
    finally:
        con.close()


def last():
    return emo.history(1)[0]


def refused(code, fn):
    try:
        fn()
    except emo.EmotionError as e:
        return e.code == code
    return False


print("1. a fresh emotion.db")
s0 = emo.read()
ok(count() == 0, "change_log exists and starts empty (opening is not a change)")

print("2. write() leaves one full row")
r = emo.write(0.4, -0.2, 0.1, cause="  test set  ", door="code", note="  first  ")
writes += 1
row = last()
ok(count() == writes and r["log_id"] == row["id"], "exactly one row, and write() returns its id")
ok((row["old_v"], row["old_a"], row["old_d"]) == (0, 0, 0) and (row["new_v"], row["new_a"], row["new_d"]) == (0.4, -0.2, 0.1),
   "old and new V, A, D")
ok((row["raw_v"], row["raw_a"], row["raw_d"]) == (0.4, -0.2, 0.1) and row["clamped"] == [], "in range: raw equals new, nothing clamped")
ok(row["cause"] == "test set" and row["note"] == "first" and row["door"] == "code" and row["layer"] == "emotion",
   "cause and note (trimmed), door and layer")
ok(row["at"] == emo.read()["updated"] == r["new"]["updated"], "timestamp is the state's own last-updated time")
ok(row["old_label"] == "neutral" and row["new_label"] == "%s %s" % (emo.label(0.4, -0.2, 0.1)["intensity"], emo.label(0.4, -0.2, 0.1)["name"]),
   "old and new labels (%s -> %s)" % (row["old_label"], row["new_label"]))
emo.write(*pts["anger"], cause="spec test", door="admin")
writes += 1
ok(last()["new_label"] == "moderate anger" and last()["door"] == "admin", "anger's coordinates log as moderate anger, door admin")
emo.write(0.1, 0.1, 0.1, cause="no note", door="code", note="   ")
writes += 1
ok(last()["note"] is None, "a blank note is stored as no note")

print("3. nudge() leaves one row")
before = emo.read()
r = emo.nudge(0.2, -0.05, 0, cause="nudge test", door="admin")
writes += 1
row = last()
ok(count() == writes and r["log_id"] == row["id"], "exactly one row per nudge")
ok((row["old_v"], row["old_a"], row["old_d"]) == (before["v"], before["a"], before["d"])
   and abs(row["new_v"] - (before["v"] + 0.2)) < 1e-12 and abs(row["new_a"] - (before["a"] - 0.05)) < 1e-12,
   "old is where she was, new is old plus the deltas")
ok((row["raw_v"], row["raw_a"], row["raw_d"]) == (row["new_v"], row["new_a"], row["new_d"]) and row["clamped"] == [],
   "nudge inside the cube: raw equals new, nothing clamped")

print("4. clamped changes are flagged, with the raw value kept")
r = emo.write(1.5, -3, 0.25, cause="overshoot", door="code")
writes += 1
row = last()
ok((row["new_v"], row["new_a"], row["new_d"]) == (1.0, -1.0, 0.25) and (row["raw_v"], row["raw_a"], row["raw_d"]) == (1.5, -3.0, 0.25),
   "write past the edges: stored 1, -1; raw 1.5, -3 kept")
ok(row["clamped"] == ["v", "a"] == r["clamped"], "clamped axes logged (v, a), matching what write() returned")
r = emo.nudge(0.3, 0, 0, cause="push past +1", door="code")
writes += 1
row = last()
ok(row["new_v"] == 1.0 and abs(row["raw_v"] - 1.3) < 1e-12 and row["clamped"] == ["v"],
   "nudge past +1: stored 1, raw 1.3, flagged v")
emo.write(-1, 1, -1, cause="edges exactly", door="code")
writes += 1
ok(last()["clamped"] == [], "exactly on the edges is not clamped")
ok(count() == writes, "still one row per change (%d)" % writes)

print("5. refused changes leave no row")
n0, s_before = count(), emo.read()
cases = (
    ("app door", "not_allowed", lambda: emo.write(0, 0, 0, cause="x", door="app")),
    ("no door", "not_allowed", lambda: emo.nudge(0.1, cause="x", door=None)),
    ("empty cause", "no_cause", lambda: emo.write(0, 0, 0, cause="  ", door="code")),
    ("NaN", "bad_value", lambda: emo.write(float("nan"), 0, 0, cause="x", door="code")),
    ("infinite nudge", "bad_value", lambda: emo.nudge(float("inf"), cause="x", door="code")),
    ("True as a value", "bad_value", lambda: emo.write(True, 0, 0, cause="x", door="code")),
    ("all-zero nudge", "no_change", lambda: emo.nudge(0, 0, 0, cause="x", door="code")),
    ("note not text", "bad_value", lambda: emo.write(0, 0, 0, cause="x", door="code", note=5)),
    ("bad value found inside the transaction", "bad_value",
     lambda: emo._apply(lambda old: (float("nan"), 0, 0), cause="x", door="code")),
)
for label, code, fn in cases:
    assert refused(code, fn), label
ok(count() == n0, "none of %d refusals wrote a row (%s)" % (len(cases), ", ".join(c[0] for c in cases)))
ok(emo.read() == s_before, "and the state is unchanged")

print("6. state and log land together or not at all")
con = emo.db_open()
con.execute("create temp trigger block_log before insert on main.change_log begin select raise(abort, 'test: log refused'); end")
try:
    emo.write(0.5, 0.5, 0.5, cause="should roll back", door="code", con=con)
    failed_write = False
except sqlite3.DatabaseError:
    failed_write = True
con.close()
ok(failed_write, "a log insert that fails makes the write fail")
ok(emo.read() == s_before and count() == n0, "and the state change rolls back with it (no unlogged change)")

print("7. append-only")
con = emo.db_open()
for sql in ("update change_log set cause='edited'", "delete from change_log", "delete from change_log where id=1"):
    try:
        con.execute(sql)
        blocked = False
    except sqlite3.DatabaseError as e:
        blocked = "append-only" in str(e)
    assert blocked, sql
con.close()
ok(count() == writes and emo.history(writes)[-1]["cause"] == "test set", "updates and deletes are refused; rows unchanged")

print("8. the log is a complete chain")


def _before(r):
    """Where a row's change started from, before any fade: fade_from (2.5) when recorded, else old. With
    Step 2's decay on, old is the faded value and fade_from is the previous row's new; with no decay they match."""
    p = "fade_from_" if r.get("fade_from_v") is not None else "old_"
    return (r[p + "v"], r[p + "a"], r[p + "d"])


def chain_ok():
    rows = list(reversed(emo.history(10 ** 6)))
    first = _before(rows[0]) == (s0["v"], s0["a"], s0["d"])
    links = all(_before(b) == (a["new_v"], a["new_a"], a["new_d"]) for a, b in zip(rows, rows[1:]))
    end = (rows[-1]["new_v"], rows[-1]["new_a"], rows[-1]["new_d"]) == tuple(emo.read()[k] for k in "vad")
    return first and links and end, len(rows)


good, n = chain_ok()
ok(good and n == writes, "from 0, 0, 0 to the current state, each row's old is the previous row's new (%d rows)" % n)
emo.write(0, 0, 0, cause="race start", door="code")
writes += 1
errs = []


def worker():
    for _ in range(25):
        try:
            emo.nudge(0.004, -0.002, 0, cause="race", door="code")
        except Exception as e:  # noqa: BLE001
            errs.append(e)


ts = [threading.Thread(target=worker) for _ in range(4)]
[t.start() for t in ts]
[t.join() for t in ts]
writes += 100
good, n = chain_ok()
ok(not errs and count() == writes and good,
   "4 threads x 25 nudges at once: 100 rows, chain unbroken, v ends at %.3f" % emo.read()["v"])
ok(abs(emo.read()["v"] - 0.4) < 1e-9, "and no nudge was lost")

print("9. history()")
h = emo.history(3)
ok(len(h) == 3 and h[0]["id"] > h[1]["id"] > h[2]["id"] and set(h[0]) == set(emo.LOG_KEYS), "newest first, every column")
ok(all(isinstance(r["clamped"], list) for r in emo.history(writes)), "clamped comes back as a list of axes")

print("10. nothing else writes the state or the log")
src = open(os.path.join(HERE, "rivia_emotion.py")).read()
fn = re.search(r"^def _apply\(.*?(?=^def )", src, re.M | re.S).group(0)
rest = src.replace(fn, "")
ok(re.search(r"update state", fn) and re.search(r"insert into change_log", fn), "_apply() holds the state update and the log insert")
ok(not re.search(r"(update|insert into|delete from|replace into) (state|change_log)\b", rest.replace("insert or ignore into state", "")),
   "no other function in rivia_emotion.py writes either table (db_open's first-row insert aside)")
# test_fade_log.py (2.5) is left out on purpose: it builds a Step 1 emotion.db by hand, with Step 1's own
# insert, in a throwaway folder, to test the migration. test_decay.py (2.2) likewise: it sets stored rows
# with chosen times by hand, in a throwaway folder, to test decay from known starting points (2.9 merge).
others = [f for f in os.listdir(HERE) if f.endswith((".py", ".patch"))
          and f not in ("rivia_emotion.py", "test_changelog.py", "test_fade_log.py", "test_decay.py")]
writers = [f for f in others if re.search(r"(update|insert into|delete from|replace into)\s+(state|change_log)\b",
                                          open(os.path.join(HERE, f)).read(), re.I)]
ok(not writers, "no other Step 1 file writes the state or the log directly (checked %d files)" % len(others))

print("PASS")
