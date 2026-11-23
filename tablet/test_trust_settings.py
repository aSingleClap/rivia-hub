"""Emotion spec Step 6.9: Step 6's adjustable values, one small card per piece. Throwaway RIVIA_DIR.
  1. the code is the source of truth: no file means the code's values; every value has words, a range and a piece
  2. only your app's button saves; every change is checked first and a bad one saves nothing
  3. safety limits are locked and refused by name
  4. values that wouldn't fit together are refused (flag clears higher than it starts, floor under the flag...)
  5. reset one setting, one piece, or all; every save and reset is logged with old and new
  6. a broken file falls back to the code's values with a plain note; Step 6 reads the values through one place
    python test_trust_settings.py
"""
import json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ.update(HOME=TMP, RIVIA_DIR=os.path.join(TMP, "rivia"))
sys.path.insert(0, HERE)
import rivia_trust_settings as ts  # noqa: E402
import rivia_trust as tr  # noqa: E402

COUNT = {"n": 0}


def ok(cond, label):
    assert cond, label
    COUNT["n"] += 1
    print("  ok ·", label)


def raises(fn, code):
    try:
        fn()
    except ts.SettingsError as e:
        return e.code == code
    return False


print("1. the code is the source of truth")
eff = ts.effective()
ok(not eff["changed"] and eff["values"] == ts.defaults(), "no file: the code's values")
ok(eff["values"]["sat.quiet_weight"] == 0.5, "a quiet day counts half (your 'Counts half')")
ok(abs(eff["values"]["trust.start"] - 0.2) < 1e-9, "trust starts at 0.20")
ok(all(s["piece"] in ts.PIECES and s["words"] and s["lo"] < s["hi"] for s in ts.SETTINGS.values()),
   "every value has words, a range and a piece")
lst = ts.listing()
ok(lst["layer"] == 6 and [p["piece"] for p in lst["pieces"]] == list(ts.PIECES), "listed by piece: a card each")

print("2. only your app's button, checked first")
ok(raises(lambda: ts.save({"hit.arrive_min": 15}, door="gemini"), "not_allowed"), "Gemini can't save")
ok(raises(lambda: ts.save({"hit.arrive_min": 999}, door="app_button"), "out_of_range"), "out of range refused")
ok(raises(lambda: ts.save({"unlock.speak_up.days": 10.5}, door="app_button"), "bad_value"), "days are whole")
ok(raises(lambda: ts.save({"hit.arrive_min": "15"}, door="app_button"), "bad_value"), "text isn't a number")
ok(raises(lambda: ts.save({"hit.arrive_min": 15, "nope": 1}, door="app_button"), "unknown"),
   "one bad name refuses the whole save")
ok(not os.path.exists(ts.path()), "nothing was saved")
r = ts.save({"hit.arrive_min": 15, "sat.quiet_weight": 0.25}, door="app_button", note="looser")
ok([c["what"] for c in r["changes"]] == ["hit.arrive_min", "sat.quiet_weight"], "two changes saved")
ok(ts.hit_lines()["arrive_hit_min"] == 15.0 and ts.sat_tune()["quiet_weight"] == 0.25, "Step 6 reads them")

print("3. locked")
for name in list(ts.LOCKED) + list(tr.HARD_LIMITS):
    ok(raises(lambda n=name: ts.save({n: 1}, door="app_button"), "locked"), "%s is locked" % name)
ok(raises(lambda: ts.save({"unlock.spend_money.trust": 0.5}, door="app_button"), "locked"),
   "a hard limit can't sneak in as an unlock line")

print("4. values that must fit together")
ok(raises(lambda: ts.save({"flag.clear": 0.6}, door="app_button"), "bad_range"), "flag must clear higher")
ok(raises(lambda: ts.save({"area.floor": 0.75}, door="app_button"), "bad_range"), "floor under the flag line")
ok(raises(lambda: ts.save({"trust.hit.%s" % k: 0 for k in tr.KIND_WEIGHTS}, door="app_button"), "all_zero"),
   "something has to count toward trust")
ok(raises(lambda: ts.save({"trust.start": 0.5, "unlock.speak_up.trust": 0.45}, door="app_button"), "bad_range"),
   "an unlock line above where trust starts")
tt = ts.trust_tune(dict(ts.defaults(), **{"trust.start": 0.4}))
ok(abs(tt["prior_good"] / (tt["prior_good"] + tt["prior_bad"]) - 0.4) < 1e-9, "trust start maps to the code's prior")

print("5. reset and log")
ts.save({"area.penalty": 0.2}, door="app_button")
r = ts.reset("piece:6.2", door="app_button")
ok([c["what"] for c in r["changes"]] == ["hit.arrive_min"], "reset one piece: only its values go back")
ok(ts.effective()["values"]["area.penalty"] == 0.2, "the others stay")
ok(raises(lambda: ts.reset("piece:9.9", door="app_button"), "unknown"), "no such piece")
ts.reset("all", door="app_button")
ok(not os.path.exists(ts.path()) and not ts.effective()["changed"], "reset all: the file is gone, code's values")
h = ts.history(10)
ok(len(h) == 4 and h[-1]["note"] == "looser" and h[-1]["changes"][0]["old"] == 10.0, "every save and reset logged")

print("6. a broken file")
with open(ts.path(), "w") as f:
    f.write("{nope")
e = ts.effective()
ok(e["values"] == ts.defaults() and e["problems"], "broken file: code's values, with a note: %s" % e["problems"][0])
with open(ts.path(), "w") as f:
    json.dump({"version": 1, "changes": {"flag.clear": 0.5}}, f)
ok(ts.effective()["values"] == ts.defaults() and ts.effective()["problems"], "a saved value that doesn't fit: code's")

print("\n%d checks passed" % COUNT["n"])
