# test_usage.py: checks for rivia_usage.py and the 5.7-settings part of rivia_model_limits.py (Step 5.5 draft).
# Run: python3 test_usage.py   (loads 4.7 from ../../step4/4.9/merged, like test_goal_limits.py)
import json
import os
import sqlite3
import sys
import tempfile
import types
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "step4", "4.9", "merged"))
sys.path.insert(0, HERE)

import rivia_goal_limits as gl  # noqa: E402
import rivia_model_limits as ml  # noqa: E402
import rivia_usage as us  # noqa: E402

NY = ZoneInfo("America/New_York")
passed = failed = 0


def ok(cond, name):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL", name)


def raises(fn, code, name):
    try:
        fn()
    except (us.UsageError, ml.LimitError) as e:
        ok(e.code == code, name)
        return
    ok(False, name + " (no error)")


def at(y, mo, d, h, mi=0, s=0, tz=NY):
    return datetime(y, mo, d, h, mi, s, tzinfo=tz).timestamp()


NOW = at(2026, 11, 23, 15, 0)   # 3 PM New York, noon Pacific

# Shape of a log line, as 5.6 stores it from log_row().
def row(goal_id, kind, when, tokens, caps=None, would_stop=(), action="done"):
    c = dict(gl.DEFAULT_LIMITS[kind]) if caps is None else caps
    return {"at": int(when), "goal_id": goal_id, "kind": kind, "action": action, "why": "met",
            "spent": {"seconds": 10, "tokens": tokens, "model_calls": 2, "tool_calls": 3, "cost_usd": 0.0},
            "caps": c, "would_stop": list(would_stop), "mode": "measure"}

# 1. Shape: what 5.7 asked for.
v = us.build([], [], NOW, days=14)
ok(set(v) >= {"mode", "days", "kinds", "model_days", "models", "seen"}, "1 keys")
ok(v["mode"] == gl.BUDGET_MODE == "measure", "1 mode")
ok(len(v["days"]) == 14 and v["days"][-1] == "2026-11-23" and v["days"][0] == "2026-11-10", "1 NY days oldest first")
ok(len(v["model_days"]) == 14 and v["model_days"][-1] == "2026-11-23", "1 Pacific days")
ok(set(v["kinds"]) == set(gl.DEFAULT_LIMITS), "1 every kind listed")
ok(all(len(x) == 14 for x in v["kinds"].values()), "1 one entry per day per kind")
ok(v["kinds"]["plan_run"][0] == {"goals": 0, "tokens_avg": 0, "tokens_max": 0, "would_stop": 0}, "1 empty day")
ok(list(v["models"])[:3] == list(ml.MODELS), "1 Rivia's models first")
ok(v["models"]["gemini-4-neon"][0] == {"requests": 0, "peak_rpm": 0, "peak_tpm": 0, "tokens_in": 0, "tokens_out": 0,
                                        "phone_requests": 0}, "1 empty model day")
ok(v["seen"] == ["tablet", "phone"], "1 seen: tablet and phone")
ok("phone app reports" in v["seen_note"] and "Arrival speech" in v["seen_note"], "1 seen note says what's counted")
json.dumps(v)
ok(True, "1 JSON-safe")

# 2. Day edges: New York for goals, Pacific for models.
late = at(2026, 11, 22, 23, 30)            # 11:30 PM NY on the 22nd = 8:30 PM Pacific on the 22nd
early = at(2026, 11, 23, 1, 30)            # 1:30 AM NY on the 23rd = 10:30 PM Pacific on the 22nd
v = us.build([row("a", "plan_run", late, 100), row("b", "plan_run", early, 300)],
             [{"model": "gemini-4-hydrogen", "at": early, "in": 50, "out": 5}], NOW)
ok(v["kinds"]["plan_run"][-2]["goals"] == 1 and v["kinds"]["plan_run"][-1]["goals"] == 1, "2 goal days are NY days")
ok(v["models"]["gemini-4-hydrogen"][-2]["requests"] == 1 and v["models"]["gemini-4-hydrogen"][-1]["requests"] == 0,
   "2 model days are Pacific days (RPD resets midnight Pacific)")

# 3. Each goal counts once, at its last line; averages and most.
rows = [row("g1", "question", NOW - 3600, 1000, action="try"), row("g1", "question", NOW - 600, 5000),
        row("g2", "question", NOW - 300, 3000)]
v = us.build(rows, [], NOW)
d = v["kinds"]["question"][-1]
ok(d["goals"] == 2, "3 two goals, not three lines")
ok(d["tokens_avg"] == 4000 and d["tokens_max"] == 5000, "3 average and most from last lines")
ok(us.build(list(reversed(rows)), [], NOW)["kinds"]["question"][-1] == d, "3 order of rows doesn't matter")

# 4. Would have stopped uses each goal's own caps (5.7 can change them), not the defaults.
big = dict(gl.DEFAULT_LIMITS["plan_run"], tokens=200000)
small = dict(gl.DEFAULT_LIMITS["plan_run"], tokens=10000)
v = us.build([row("x", "plan_run", NOW - 60, 60000, caps=big),          # over default, under its own: no
              row("y", "plan_run", NOW - 60, 20000, caps=small),        # under default, over its own: yes
              row("z", "plan_run", NOW - 60, 100, would_stop=["tokens"])],  # logged would_stop: yes
             [], NOW)
ok(v["kinds"]["plan_run"][-1]["would_stop"] == 2, "4 own caps decide")
old = row("o", "day_score", NOW - 60, 30000)
del old["caps"]
ok(us.build([old], [], NOW)["kinds"]["day_score"][-1]["would_stop"] == 0, "4 old line without caps uses would_stop")
ok(us.build([row("e", "plan_run", NOW - 60, 100, would_stop=["tokens"], action="try"),
             row("e", "plan_run", NOW - 30, 200)], [], NOW)["kinds"]["plan_run"][-1]["would_stop"] == 1,
   "4 a goal that would have stopped earlier still counts")

# 5. Peaks: most requests and input tokens in any 60 seconds.
base = at(2026, 11, 23, 9, 0, tz=ml.PACIFIC)
calls = [{"model": "gemini-4-neon", "at": base + s, "in": t, "out": 1} for s, t in
         ((0, 100), (10, 100), (59, 100), (60, 1000), (200, 50))]
v = us.build([], calls, NOW)
n = v["models"]["gemini-4-neon"][-1]
ok(n["requests"] == 5, "5 requests per day")
ok(n["peak_rpm"] == 3, "5 peak RPM (a call 60 s later starts a new minute)")
ok(n["peak_tpm"] == 1200, "5 peak TPM counts input tokens only")
v = us.build([], calls + [{"model": "other-model", "at": base, "in": 1, "out": 1}], NOW)
ok("other-model" in v["models"], "5 other models tracked too")
ok(us.build([], [dict(calls[0], at=NOW + 60)], NOW)["models"]["gemini-4-neon"][-1]["requests"] == 0, "5 future ignored")
ok(us.build([row("f", "debug", NOW + 60, 1)], [], NOW)["kinds"]["debug"][-1]["goals"] == 0, "5 future goal ignored")

# 6. Bad days.
for bad in (0, 32, True, 1.5, "7"):
    raises(lambda b=bad: us.build([], [], NOW, days=b), "bad_value", "6 days %r refused" % (bad,))
ok(len(us.build([], [], NOW, days=1)["days"]) == 1, "6 one day works")

# 7. Storage: body.db only, record, prune, load; missing goal_log reads as no goals.
tmp = tempfile.mkdtemp()
con = sqlite3.connect(os.path.join(tmp, "body.db"))
ok(us.load_goal_rows(con, 0) == [], "7 no goal_log yet")
con.executescript("create table goal_log(at integer not null, goal_id text not null, action text not null, "
                  "row text not null);")
r = row("s1", "plan_run", NOW - 120, 4242)
con.execute("insert into goal_log values(?,?,?,?)", (r["at"], "s1", "done", json.dumps(r)))
con.execute("insert into goal_log values(?,?,?,?)", (int(NOW - 100), "s2", "done", "not json"))
us.record_call(con, "gemini-4-hydrogen", NOW - 50, input_tokens=700, output_tokens=40)
us.record_call(con, "gemini-4-hydrogen", NOW - 40 * 86400, input_tokens=1)
us.record_call(con, "gemini-4-hydrogen", NOW - 30, input_tokens=300)   # prunes the 40-day-old one
ok(len(us.load_calls(con, 0)) == 2, "7 calls older than a month pruned")
raises(lambda: us.record_call(con, "gemini-4-hydrogen", NOW, input_tokens=-1), "bad_value", "7 bad tokens refused")
v = us.usage_view(now=NOW, con=con)
ok(v["kinds"]["plan_run"][-1]["tokens_max"] == 4242, "7 usage_view reads goal_log, skips a broken line")
ok(v["models"]["gemini-4-hydrogen"][-1] == {"requests": 2, "peak_rpm": 2, "peak_tpm": 1000,
                                              "tokens_in": 1000, "tokens_out": 40, "phone_requests": 0},
   "7 usage_view models")
before = con.total_changes
us.usage_view(now=NOW, con=con)
ok(con.total_changes == before, "7 usage_view writes nothing")
con.close()
econ = sqlite3.connect(os.path.join(tmp, "emotion.db"))
raises(lambda: us.record_call(econ, "gemini-4-neon", NOW, input_tokens=1), "wrong_db", "7 never emotion.db")
econ.close()

# 8. usage_view with no con opens body.db through 5.6's rivia_normal.db_open().
opened = []
fake = types.ModuleType("rivia_normal")
fake.db_open = lambda: opened.append(1) or sqlite3.connect(os.path.join(tmp, "body.db"))
sys.modules["rivia_normal"] = fake
v = us.usage_view(now=NOW, days=3)
ok(opened == [1] and len(v["days"]) == 3, "8 opens body.db itself")
del sys.modules["rivia_normal"]

# 9. Model limits come from 5.7's settings (model_limits()), never a copy here.
t, p = ml.from_settings({"hydrogen": {"rpm": 10, "rpd": 1000, "tpm": None}, "neon": {"rpm": 0},
                         "flash_3_8": {"rpd": True}})
ok(t["gemini-4-hydrogen"] == {"rpm": 10, "tpm": None, "rpd": 1000}, "9 short names map to model ids")
ok(t["gemini-4-neon"]["rpm"] is None and t["gemini-3.8-flash"]["rpd"] is None, "9 bad values show not set")
ok(len(p) == 2, "9 each bad value is named")
ok(ml.from_settings("nope")[0] == ml.empty_table() and ml.from_settings("nope")[1], "9 unreadable shows not set")
sys.modules.pop("rivia_progress_settings", None)
saved = sys.path[:]
sys.path[:] = [x for x in sys.path if "5.7" not in x]
t, p = ml.current_table()
# 5.7 copy: the settings file sits beside this test, so it is found; with nothing typed in, all read not set.
_here_has_settings = os.path.exists(os.path.join(HERE, "rivia_progress_settings.py"))
ok(t == ml.empty_table() and (p == [] if _here_has_settings else bool(p)),
   "9 nothing typed in yet: all not set" + ("" if _here_has_settings else ", and says why"))
fake = types.ModuleType("rivia_progress_settings")
fake.model_limits = lambda: {"neon": {"rpm": 5, "rpd": 50, "tpm": 100000}}
sys.modules["rivia_progress_settings"] = fake
t, p = ml.current_table()
ok(t["gemini-4-neon"] == {"rpm": 5, "tpm": 100000, "rpd": 50} and p == [], "9 reads 5.7's numbers")
g = ml.usage([{"model": "gemini-4-neon", "at": NOW - i, "in": 10, "out": 1} for i in range(4)], t, NOW)
ok(g["gemini-4-neon"]["rpm"]["status"] == "near" and g["gemini-4-hydrogen"]["worst"] == "not_set", "9 gauges")
del sys.modules["rivia_progress_settings"]
sys.path[:] = saved
ok(not hasattr(ml, "set_limits"), "9 no second store of limits here")

# 11. Phone calls (Nov 25): the app reports its own Gemini calls; same id twice counts once; numbers only.
con = sqlite3.connect(":memory:", isolation_level=None)
F = "gemini-3.8-flash"
def pc(i, at_, inp=500, out=120, kind="rivia_text", model=F):
    return {"id": "r-%04x-%d" % (i, int(at_)), "at": at_, "model": model, "in": inp, "out": out, "kind": kind}
r = us.record_phone_calls(con, [pc(1, NOW - 60), pc(2, NOW - 30, kind="rivia_voice")], NOW)
ok(r == {"added": 2, "already": 0, "too_old": 0}, "11 two phone calls stored")
r = us.record_phone_calls(con, [pc(1, NOW - 60), pc(3, NOW - 10)], NOW)
ok(r == {"added": 1, "already": 1, "too_old": 0}, "11 a resend counts once")
r = us.record_phone_calls(con, [pc(4, NOW - 40 * 86400)], NOW)
ok(r["too_old"] == 1 and r["added"] == 0, "11 a call older than a month is skipped")
us.record_call(con, F, NOW - 20, input_tokens=400, output_tokens=80)
calls = us.load_calls(con, NOW - 3600)
ok(len(calls) == 4 and [c["from"] for c in calls].count("phone") == 3 and calls == sorted(calls, key=lambda c: c["at"]),
   "11 load_calls gives tablet and phone calls in time order")
v = us.build([], calls, NOW, days=2)
day = v["models"][F][-1]
ok(day["requests"] == 4 and day["phone_requests"] == 3 and day["tokens_in"] == 1900 and day["tokens_out"] == 440,
   "11 the day's bar counts phone calls, with input and output tokens")
for bad, name in ((pc(5, NOW, inp=-1), "negative tokens"), (pc(5, NOW, inp=True), "bool tokens"),
                  (pc(5, NOW + 3600), "future"), (pc(5, NOW, kind="chat"), "unknown kind"),
                  (pc(5, NOW, model="gemini 3.8 flash!"), "bad model"), ({**pc(5, NOW), "text": "hi"}, "words"),
                  ({**pc(5, NOW), "id": ""}, "empty id"), (pc(5, NOW, out=3_000_000), "too many tokens")):
    raises(lambda b=bad: us.record_phone_calls(con, [pc(6, NOW - 5), b], NOW), "bad_value", "11 refused: " + name)
ok(con.execute("select count(*) from phone_calls").fetchone()[0] == 3, "11 a refused batch saves nothing")
raises(lambda: us.record_phone_calls(con, [], NOW), "bad_value", "11 empty batch refused")
raises(lambda: us.record_phone_calls(con, [pc(i, NOW - i) for i in range(201)], NOW), "bad_value", "11 over 200 refused")
cols = [r[1] for r in con.execute("pragma table_info(phone_calls)")]
ok(cols == ["call_id", "at", "model", "input_tokens", "output_tokens", "kind"], "11 phone_calls holds no words")

# 10. Source: reads only, no network, no sleep, no sign-in, no fear words.
for f in ("rivia_usage.py", "rivia_model_limits.py"):
    src = open(os.path.join(HERE, f)).read()
    for bad in ("import requests", "urllib", "socket", "sleep(", ".env", "emotion_tool", "scared", "afraid", "fear"):
        ok(bad not in src, "10 %s has no %r" % (f, bad))

print("%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
