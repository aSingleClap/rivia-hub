"""Step 2.5: decay goes in the change log without a row per fade. An emotion.db written by Step 1 gains the
fade columns by ALTER TABLE ADD COLUMN with every old row left exactly as it was, the append-only triggers
still in place, and Step 1's own insert still working; two processes migrating at once add each column once.
Each real change records the stored value she faded from, when it was stored and the layer she faded toward,
and starts from where she had faded to; the chain becomes "each row's fade_from is the previous row's new".
Reading or a long idle stretch adds no row. A fade that can't be worked out refuses the change and leaves no
row. Step 2's real decay (2.2) isn't built here: a stand-in plays its part. Throwaway RIVIA_DIR; never touches
~/rivia.
    python test_fade_log.py
"""
import math, os, re, sqlite3, subprocess, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ["RIVIA_DIR"] = os.path.join(TMP, "rivia")
sys.path.insert(0, HERE)

import rivia_store as store  # noqa: E402  (after RIVIA_DIR is set)
import rivia_time as rt  # noqa: E402
import rivia_emotion as emo  # noqa: E402
# 2.9 merge: decay is on (2.2), so the state moves between two reads a second apart. These checks are
# about what the log records, with stand-in fades, which assumes no time passes, so the clock is stopped for this file. test_decay.py and
# test_step2_acceptance.py test decay.
import rivia_time as _rt  # noqa: E402
_STOPPED = _rt.now_s()
_rt.now_s = lambda: _STOPPED

# Step 1's schema and log insert as installed (rivia-hub 7f23ad1), frozen here so the test can build an old
# emotion.db without the old code.
STEP1_SCHEMA = """
create table if not exists state(
    layer text primary key, v real not null, a real not null, d real not null, updated text not null);
create table if not exists change_log(
    id integer primary key, at text not null, layer text not null,
    old_v real not null, old_a real not null, old_d real not null,
    new_v real not null, new_a real not null, new_d real not null,
    raw_v real not null, raw_a real not null, raw_d real not null,
    clamped text not null,
    old_label text, new_label text,
    cause text not null, note text, door text not null);
create trigger if not exists change_log_no_update before update on change_log
    begin select raise(abort, 'change_log is append-only'); end;
create trigger if not exists change_log_no_delete before delete on change_log
    begin select raise(abort, 'change_log is append-only'); end;
"""
STEP1_INSERT = ("insert into change_log(at, layer, old_v, old_a, old_d, new_v, new_a, new_d, raw_v, raw_a, raw_d,"
                " clamped, old_label, new_label, cause, note, door) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
STEP1_COLS = ("id", "at", "layer", "old_v", "old_a", "old_d", "new_v", "new_a", "new_d", "raw_v", "raw_a", "raw_d",
              "clamped", "old_label", "new_label", "cause", "note", "door")
FADE_COLS = tuple(c for c, _ in emo.FADE_COLUMNS)
NO_DECAY = emo._faded  # Step 1 behaviour: no fade; put back after the stand-ins


def ok(cond, label):
    assert cond, label
    print("  ok ·", label)


def refused(code, fn):
    try:
        fn()
    except emo.EmotionError as e:
        return e.code == code
    return False


def make_step1_db(rivia_dir):
    """An emotion.db as Step 1 leaves it: state row plus three log rows (one clamped, one with a note)."""
    path = os.path.join(rivia_dir, "emotion.db")
    os.makedirs(rivia_dir, exist_ok=True)
    con = sqlite3.connect(path, isolation_level=None)
    con.executescript(store.META_SCHEMA + STEP1_SCHEMA)
    at = [rt.iso(rt.now_s() - 3600 * k) for k in (3, 2, 1)]
    steps = [((0, 0, 0), (0.4, -0.2, 0.1), (0.4, -0.2, 0.1), "", "neutral", "mild calm", "manual", "first", "app_button"),
             ((0.4, -0.2, 0.1), (1.0, 0.3, 0.1), (1.6, 0.3, 0.1), "v", "mild calm", "strong joy", "overshoot", None, "code"),
             ((1.0, 0.3, 0.1), (-0.51, 0.59, 0.25), (-0.51, 0.59, 0.25), "", "strong joy", "moderate anger", "spec", None, "admin")]
    for when, (old, new, raw, cl, ol, nl, cause, note, door) in zip(at, steps):
        con.execute(STEP1_INSERT, (when, "emotion", *old, *new, *raw, cl, ol, nl, cause, note, door))
    con.execute("insert into state(layer, v, a, d, updated) values('emotion', -0.51, 0.59, 0.25, ?)", (at[-1],))
    con.close()
    return path


def raw_rows(path, cols=STEP1_COLS):
    con = sqlite3.connect(path)
    try:
        return con.execute("select %s from change_log order by id" % ", ".join(cols)).fetchall()
    finally:
        con.close()


def schema_bits(path):
    con = sqlite3.connect(path)
    try:
        cols = [r[1] for r in con.execute("pragma table_info(change_log)")]
        trig = sorted(r[0] for r in con.execute("select sql from sqlite_master where type='trigger'"))
        return cols, trig
    finally:
        con.close()


def count():
    con = emo.db_open()
    try:
        return con.execute("select count(*) from change_log").fetchone()[0]
    finally:
        con.close()


def vad(row, prefix):
    return tuple(row[prefix + k] for k in "vad")


print("1. an emotion.db written by Step 1 gains the fade columns, rows untouched")
path = make_step1_db(os.environ["RIVIA_DIR"])
before_rows, (before_cols, before_trig) = raw_rows(path), schema_bits(path)
ok(not any(c in before_cols for c in FADE_COLS) and len(before_rows) == 3, "Step 1 database: 3 rows, no fade columns")
con = emo.db_open()
meta = store.get_meta(con, emo.FADE_MIGRATED_KEY)
again = emo._migrate_fade_columns(con)
con.close()
after_cols, after_trig = schema_bits(path)
# 2.9 merge: 2.7's change_log.limited is added by the same open, right after the fade columns.
ok(after_cols == before_cols + list(FADE_COLS) + ["limited", "tag"],
   "the five fade columns (then 2.7's limited and 2.4's tag) are added at the end, nothing else changes")
ok(raw_rows(path) == before_rows, "every old row reads exactly as before, column for column")
ok(all(r == (None,) * len(FADE_COLS) for r in raw_rows(path, FADE_COLS)), "old rows read None in the new columns")
ok(after_trig == before_trig and len(after_trig) == 2, "both append-only triggers are still there")
ok(meta and "fade_from_v" in meta and "3 rows" in meta, "the migration is recorded in meta (%s)" % meta)
ok(again == [] and schema_bits(path)[0] == after_cols, "opening again adds nothing")
con = emo.db_open()
for sql in ("update change_log set cause='edited'", "update change_log set fade_toward='mood'", "delete from change_log"):
    try:
        con.execute(sql)
        blocked = False
    except sqlite3.DatabaseError as e:
        blocked = "append-only" in str(e)
    assert blocked, sql
con.close()
ok(raw_rows(path) == before_rows, "after migrating, updates (the new columns included) and deletes are still refused")
ok(all(emo.fade(r) is None for r in emo.history(10)), "fade() reports nothing for rows written before 2.5")
# 2.9 merge: read() now decays (2.2), and this row was stored an hour ago, so check the stored row itself.
_c = emo.db_open()
ok(_c.execute("select v from state where layer='emotion'").fetchone()[0] == -0.51, "the state row is kept as it was")
_c.close()

print("2. Step 1's own insert still works on a migrated database (a rollback keeps logging)")
con = sqlite3.connect(path, isolation_level=None)
con.execute(STEP1_INSERT, (rt.iso(rt.now_s()), "emotion", -0.51, 0.59, 0.25, -0.5, 0.5, 0.2, -0.5, 0.5, 0.2,
                           "", "moderate anger", "moderate anger", "rollback test", None, "code"))
con.close()
ok(len(raw_rows(path)) == 4 and raw_rows(path)[:3] == before_rows, "Step 1's insert adds its row; the old three unchanged")
ok(raw_rows(path, FADE_COLS)[-1] == (None,) * len(FADE_COLS), "its fade columns are None, like any pre-2.5 row")
con = sqlite3.connect(path, isolation_level=None)
con.execute("update state set v=-0.5, a=0.5, d=0.2")  # keep state matching that row, as Step 1's _apply would
con.close()

print("3. two processes migrating an old database at once")
race_dir = os.path.join(TMP, "race")
race_path = make_step1_db(race_dir)
race_rows = raw_rows(race_path)
helpers = os.path.dirname(os.path.abspath(store.__file__))
start = time.time() + 2.0
code = ("import time, sys; time.sleep(max(0, %r - time.time())); import rivia_emotion as e; c = e.db_open(); c.close()"
        % start)
env = dict(os.environ, RIVIA_DIR=race_dir, PYTHONPATH=os.pathsep.join([HERE, helpers]), PYTHONDONTWRITEBYTECODE="1")
procs = [subprocess.Popen([sys.executable, "-c", code], env=env, stderr=subprocess.PIPE) for _ in range(6)]
errs = [(p.wait(), p.stderr.read().decode()) for p in procs]
ok(all(rc == 0 for rc, _ in errs), "6 processes opened it at the same moment; none failed %s"
   % [e[-200:] for rc, e in errs if rc])
cols, _ = schema_bits(race_path)
ok(all(cols.count(c) == 1 for c in FADE_COLS) and raw_rows(race_path) == race_rows,
   "each column added once; rows unchanged")

print("4. without decay (Step 1's behaviour), each change records a fade of nothing")
# 2.9 merge: the real decay (2.2) now fills _faded, and this database's emotion row is an hour old, so
# "without decay" here means decay turned off the way rivia_emotion.py allows (half-life infinity).
_EH, _MH = emo.EMOTION_HALF_LIFE_S, emo.MOOD_HALF_LIFE_S
emo.EMOTION_HALF_LIFE_S = emo.MOOD_HALF_LIFE_S = float("inf")
prev = emo.read()
r = emo.write(0.3, 0.2, 0.1, cause="plain set", door="code")
row = emo.history(1)[0]
ok(vad(row, "fade_from_") == (prev["v"], prev["a"], prev["d"]) and row["fade_from_at"] == prev["updated"],
   "fade_from is the stored value and when it was stored")
ok(vad(row, "old_") == vad(row, "fade_from_") and row["fade_toward"] is None, "old equals fade_from; toward is None")
f = emo.fade(row)
ok(f["dv"] == f["da"] == f["dd"] == 0 and f["seconds"] >= 0 and f["toward"] is None,
   "fade() reads zero fade over %d s" % f["seconds"])
ok(r["fade"] == {"from": {**{k: prev[k] for k in "vad"}, "updated": prev["updated"]}, "toward": None},
   "write() returns the fade too")

emo.EMOTION_HALF_LIFE_S, emo.MOOD_HALF_LIFE_S = _EH, _MH

print("5. with decay (a stand-in for 2.2), the change starts from where she faded to")
seen = []


def half_toward_zero(con, layer, row, t):
    """Stand-in decay: halfway to 0, fading toward mood. Reads nothing, writes nothing."""
    seen.append((layer, t))
    return (row["v"] / 2, row["a"] / 2, row["d"] / 2), "mood"


emo._faded = half_toward_zero
emo.write(0.8, 0.4, -0.2, cause="spike", door="code")
n0 = count()
for _ in range(50):
    emo.read()
emo.history(5)
ok(count() == n0, "50 reads and a history read add no row (a fade is never a row)")
r = emo.nudge(0.1, 0, 0, cause="after a while", door="code")
row = emo.history(1)[0]
ok(vad(row, "fade_from_") == (0.8, 0.4, -0.2) and row["fade_toward"] == "mood", "fade_from is the last real value; toward mood")
ok(vad(row, "old_") == (0.4, 0.2, -0.1), "old is the faded value (half of it, by the stand-in)")
ok(abs(row["new_v"] - 0.5) < 1e-12 and vad(row, "raw_")[1:] == (0.2, -0.1), "the nudge was added to the faded value, not the stored one")
f = emo.fade(row)
ok(abs(f["dv"] + 0.4) < 1e-12 and abs(f["da"] + 0.2) < 1e-12 and abs(f["dd"] - 0.1) < 1e-12 and f["toward"] == "mood",
   "fade() reads how much she faded: v -0.4, a -0.2, d +0.1")
ok(row["old_label"] == emo._label_text({"v": 0.4, "a": 0.2, "d": -0.1}), "old_label names the faded value (%s)" % row["old_label"])
layer, t = seen[-1]
ok(layer == "emotion" and row["at"] == rt.iso(t) and f["seconds"] == t - rt.to_epoch(row["fade_from_at"]),
   "the fade is worked out to the same second the row is stamped")
ok(r["old"]["v"] == 0.4 and r["fade"]["from"]["v"] == 0.8 and r["fade"]["toward"] == "mood", "nudge() returns old faded and the fade")
emo.write(-0.2, 0.0, 0.0, cause="absolute set", door="code")
row = emo.history(1)[0]
ok(vad(row, "old_") == (0.25, 0.1, -0.05) and vad(row, "new_") == (-0.2, 0.0, 0.0), "an absolute write logs the faded old too")

print("6. a long idle stretch is still one row")


def faded_out(con, layer, row, t):
    return (0.0, 0.0, 0.0), "mood"  # stand-in: ten hours later she has faded all the way


emo._faded = faded_out
n0 = count()
emo.nudge(0, 0.1, 0, cause="first thing after a long gap", door="code")
row = emo.history(1)[0]
ok(count() == n0 + 1 and vad(row, "old_") == (0, 0, 0) and vad(row, "fade_from_") == (-0.2, 0.0, 0.0),
   "one row, carrying the whole fade (from -0.2, 0, 0 to 0, 0, 0)")
ok(row["old_label"] == "neutral", "she reads neutral before the change, as she would have felt")

print("7. a fade that can't be worked out refuses the change and leaves no row")


def boom(con, layer, row, t):
    raise RuntimeError("decay broke")


bad = (
    ("NaN", lambda *a: ((float("nan"), 0, 0), "mood"), "bad_value"),
    ("two values", lambda *a: ((0, 0), "mood"), "bad_value"),
    ("toward not a name", lambda *a: ((0, 0, 0), 5), "bad_value"),
)
s_before, n0 = emo.read(), count()
for label, stub, code_ in bad:
    emo._faded = stub
    assert refused(code_, lambda: emo.nudge(0.1, cause="x", door="code")), label
emo._faded = boom
try:
    emo.write(0, 0, 0, cause="x", door="code")
    raised = False
except RuntimeError:
    raised = True
ok(raised, "a decay that throws makes the change fail")
ok(emo.read() == s_before and count() == n0, "none of the %d bad fades changed the state or wrote a row" % (len(bad) + 1))
emo._faded = lambda con, layer, row, t: ((row["v"], row["a"], row["d"] + 1.5), "mood")
emo.write(0.1, 0.1, 0.1, cause="fade past the edge", door="code")
row = emo.history(1)[0]
ok(row["old_d"] == 1.0 and row["clamped"] == [], "a fade past an edge is held at the edge and not logged as clamping")

print("8. the chain: each row's fade_from is the previous row's new, even under concurrent nudges")
emo._faded = half_toward_zero
errs = []


def worker():
    for _ in range(25):
        try:
            emo.nudge(0.01, 0, 0, cause="race", door="code")
        except Exception as e:  # noqa: BLE001
            errs.append(e)


n0 = count()
ts = [threading.Thread(target=worker) for _ in range(4)]
[t.start() for t in ts]
[t.join() for t in ts]
rows = [r for r in reversed(emo.history(10 ** 6)) if r["fade_from_v"] is not None]
links = all(vad(b, "fade_from_") == vad(a, "new_") and b["fade_from_at"] == a["at"] for a, b in zip(rows, rows[1:]))
end = vad(rows[-1], "new_") == tuple(emo.read()[k] for k in "vad")
ok(not errs and count() == n0 + 100, "4 threads x 25 nudges with decay on: 100 rows, no error")
ok(links and end, "every 2.5 row's fade_from (value and time) is the previous row's new, to the current state (%d rows)" % len(rows))
ok(any(vad(b, "old_") != vad(a, "new_") for a, b in zip(rows, rows[1:])), "while old differs from the previous new wherever she faded")
first = rows[0]
ok(vad(first, "fade_from_") == (-0.5, 0.5, 0.2), "the first 2.5 row links onto the last Step 1 row's new")
emo._faded = NO_DECAY

print("9. only real changes write")
src = open(os.path.join(HERE, "rivia_emotion.py")).read()
faded_src = re.search(r"^def _faded\(.*?(?=^def )", src, re.M | re.S).group(0)
ok(not re.search(r"\b(update|insert|delete|replace|alter)\b\s", faded_src.split('"""')[-1], re.I),
   "_faded() holds no write")
apply_src = re.search(r"^def _apply\(.*?(?=^def )", src, re.M | re.S).group(0)
ok("fade_from_v" in apply_src and "_faded(" in apply_src and apply_src.index("begin immediate") < apply_src.index("_faded("),
   "_apply() works out the fade inside its transaction, so the stored row can't change under it")
ok(set(emo.LOG_KEYS) >= set(FADE_COLS) and set(emo.history(1)[0]) == set(emo.LOG_KEYS), "history() returns the fade columns")

print("10. log labels read relative to her baseline (2.3: her normal counts as neutral)")
con = emo.db_open()
# 2.9 merge: 2.1's db_open always makes the baseline row (0, 0, 0 on a fresh DB, which labels exactly as Step 1
# did), so the "no baseline row yet" case can't happen here; check that _rest reads the row that is there.
ok(emo._rest(con, "emotion") == tuple(con.execute("select v, a, d from state where layer='baseline'").fetchone()),
   "an emotion change labels against the stored baseline row (2.1 always has one)")
BASE = (0.3, -0.3, 0.1)  # a mildly calm normal, 0.44 out: on its own it reads "mild calm"
# 2.9 merge: db_open already made the row, so it is updated by hand (past 2.7's limits, which aren't under test here).
con.execute("update state set v=?, a=?, d=?, updated=? where layer='baseline'", (*BASE, rt.iso(rt.now_s())))
ok(emo._rest(con, "emotion") == BASE and emo._rest(con, "baseline") is None,
   "an emotion change labels against the baseline; a change to the baseline itself does not (it would always read neutral)")
con.close()
ok(emo._label_text(dict(zip("vad", BASE))) == "mild calm", "for contrast: the baseline point alone, against 0, reads mild calm")
emo.write(*BASE, cause="at her normal", door="code")
row = emo.history(1)[0]
ok(row["new_label"] == "neutral", "resting at her baseline logs neutral, as the dial says (not mild calm)")
emo.write(BASE[0] + 0.1, BASE[1], BASE[2], cause="a touch off normal", door="code")
row = emo.history(1)[0]
ok(row["old_label"] == "neutral" and row["new_label"] == "neutral", "within 0.15 of her baseline both labels stay neutral")
emo.write(0.0, 0.0, 0.0, cause="to 0, 0, 0", door="code")
row = emo.history(1)[0]
ok(row["new_label"] == "neutral", "0, 0, 0 still reads neutral: away from her normal, the point itself is named (2.3's rule)")
emo.write(-0.51, 0.59, 0.25, cause="anger", door="code")
row = emo.history(1)[0]
ok(row["new_label"] == "moderate anger", "a state far from her baseline is named as the point it is, as in Step 1")


def toward_base(con, layer, row, t):
    return BASE, "mood"  # stand-in: she has faded all the way back to her normal


emo._faded = toward_base
emo.nudge(0, 0.1, 0, cause="after a long gap", door="code")
row = emo.history(1)[0]
ok(row["old_label"] == "neutral" and vad(row, "old_") == BASE, "faded back to her normal, the row's old_label reads neutral")
emo._faded = NO_DECAY

print("PASS")
