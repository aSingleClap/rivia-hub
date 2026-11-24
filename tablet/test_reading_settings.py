"""Emotion spec Step 7.11: Step 7's adjustable values, one small card per piece. Throwaway RIVIA_DIR.
  1. the code is the source of truth: no file means the code's values; every value has words, a range and a piece
  2. only your app's button saves; every change is checked first and a bad one saves nothing
  3. safety limits are locked and refused by name
  4. values that wouldn't fit together are refused
  5. reset one setting, one piece, or all; logged with old and new
  6. a broken file falls back to the code's values; rivia_reading reads them through tune()
    python test_reading_settings.py
"""
import json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ.update(HOME=TMP, RIVIA_DIR=os.path.join(TMP, "rivia"))
sys.path.insert(0, HERE)
import rivia_reading as rd  # noqa: E402
import rivia_reading_settings as rs  # noqa: E402

COUNT = {"n": 0}


def ok(cond, label):
    assert cond, label
    COUNT["n"] += 1
    print("  ok ·", label)


def raises(fn, code):
    try:
        fn()
    except rs.SettingsError as e:
        return e.code == code
    return False


print("1. the code is the source of truth")
eff = rs.effective()
ok(not eff["changed"] and eff["values"] == rs.defaults(), "no file: the code's values")
ok(rs.tune() == rd.code_tune(), "the card's values are exactly what the code uses")
ok(eff["values"]["normal.min_messages"] == 25 and eff["values"]["ask.per_day"] == 2
   and eff["values"]["pattern.days"] == 10 and eff["values"]["ask.shelve_days"] == 7, "your starting numbers")
ok(all(s["piece"] in rs.PIECES and s["words"] and s["lo"] <= rs.defaults()[p] <= s["hi"]
       for p, s in rs.SETTINGS.items()), "every value has words, a piece, and its default in range")
ok(set(rs.TUNE_KEYS) == set(rs.SETTINGS), "every value reaches the code")
ok(not {"7.5", "7.6", "7.10"} & set(rs.PIECES), "no card for the left-out pieces (mood button, voice, coloring)")

print("2. only your button saves; checked first")
ok(raises(lambda: rs.save({"ask.per_day": 3}, door="rivia"), "not_allowed"), "Rivia can't save")
ok(raises(lambda: rs.save({"ask.per_day": 99}, door="app_button"), "out_of_range"), "out of range refused")
ok(raises(lambda: rs.save({"ask.per_day": 2.5}, door="app_button"), "bad_value"), "a whole number is needed")
ok(raises(lambda: rs.save({"ask.per_day": True}, door="app_button"), "bad_value"), "true isn't a number")
ok(not os.path.exists(rs.path()), "nothing saved after refusals")
r = rs.save({"ask.per_day": 1, "situation.after_shift_hours": 8}, door="app_button", note="fewer questions")
ok([c["what"] for c in r["changes"]] == ["ask.per_day", "situation.after_shift_hours"], "two changes saved")
ok(rs.tune()["asks_per_day"] == 1 and rs.tune()["after_shift_hours"] == 8.0, "and the code reads them")
ok(rd._tune()["asks_per_day"] == 1, "rivia_reading reads your edit")

print("3. locked")
for name in rs.LOCKED:
    ok(raises(lambda n=name: rs.save({n: 1}, door="app_button"), "locked"), "%s is locked" % name)
ok(raises(lambda: rs.save({"nope": 1}, door="app_button"), "unknown"), "an unknown name is refused")

print("4. must fit together")
ok(raises(lambda: rs.save({"off.well_off": 1.0, "off.bit_off": 1.5}, door="app_button"), "bad_range"),
   "well off has to start past a bit off")
ok(raises(lambda: rs.save({"normal.keep_days": 14, "pattern.days": 20}, door="app_button"), "bad_range"),
   "a pattern can't need more days than the normal looks back")

print("5. reset and log")
rs.reset("piece:7.8", door="app_button")
ok(rs.tune()["asks_per_day"] == 2 and rs.tune()["after_shift_hours"] == 8.0, "reset one piece")
rs.reset("all", door="app_button")
ok(not os.path.exists(rs.path()) and rs.tune() == rd.code_tune(), "reset all removes the file")
h = rs.history()
ok(h[0]["action"] == "reset all" and h[-1]["note"] == "fewer questions" and h[-1]["changes"][0]["old"] == 2,
   "every save and reset logged with old and new")
ok(raises(lambda: rs.reset("piece:9.9", door="app_button"), "unknown"), "an unknown piece is refused")

print("6. a broken file")
os.makedirs(rs.rivia_dir(), exist_ok=True)
with open(rs.path(), "w") as f:
    f.write("{not json")
eff = rs.effective()
ok(eff["values"] == rs.defaults() and "can't be read" in eff["problems"][0], "falls back with a plain note")
with open(rs.path(), "w") as f:
    json.dump({"version": 1, "changes": {"ask.per_day": 50}}, f)
ok("doesn't pass" in rs.effective()["problems"][0], "a saved value out of range is ignored, said plainly")
os.remove(rs.path())
v = rs.view()
ok(v["layers"][0]["layer"] == 7 and v["notes"] and v["locked"], "the card's data in /app/progress's shape")

print("\n%d checks passed" % COUNT["n"])
