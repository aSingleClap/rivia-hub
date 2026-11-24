"""Emotion spec Step 3.9: Step 3's adjustable values (Layer 3 "Reactions"), one small card per piece. Throwaway
RIVIA_DIR.
  1. the code is the source of truth: no file means the code's values; Jordan's starting numbers are there
  2. only your app's button saves; every change is checked first and a bad one saves nothing
  3. safety limits are locked and refused by name (no fear, event directions, plans never reworded, fact check...)
  4. reset one setting, one piece, or all; logged with old and new
  5. a broken file falls back to the code's values; both Step 3 files read them through tune()
    python test_appraisal_settings.py
"""
import json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ.update(HOME=TMP, RIVIA_DIR=os.path.join(TMP, "rivia"))
sys.path.insert(0, HERE)
import rivia_appraisal as ap  # noqa: E402
import rivia_appraisal_settings as s  # noqa: E402
import rivia_expression as ex  # noqa: E402

COUNT = {"n": 0}
CODE = {**ap.code_tune(), **ex.code_tune()}


def ok(cond, label):
    assert cond, label
    COUNT["n"] += 1
    print("  ok ·", label)


def raises(fn, code):
    try:
        fn()
    except s.SettingsError as e:
        return e.code == code
    return False


print("1. the code is the source of truth")
eff = s.effective()
v = eff["values"]
ok(not eff["changed"] and v == s.defaults(), "no file: the code's values")
ok(s.tune() == CODE, "the card's values are exactly what the code uses")
ok(v["limit.per_message"] == 0.3 and v["limit.mood_soak"] == 0.1, "your starting numbers: limit 0.3, mood soaks 10%")
ok(abs(v["strength.share_1"] - 1 / 3) < 1e-3 and abs(v["strength.share_2"] - 2 / 3) < 1e-3,
   "strength 1 a third, strength 2 two thirds")
ok(v["size.good_news"] == 1.0 and abs(v["size.cursed_at_her"] - 1 / 3) < 1e-3 and abs(v["size.thumbs_down"] - 1 / 3)
   < 1e-3, "biggest event = the limit; cursing and thumbs down a third of it")
ok(v["reword.short_words"] == 15 and v["tone.cap"] == 3 and v["events.unanswered_hours"] == 6
   and v["events.message_on"] == 1, "under 15 words left alone, tone capped at moderate, unanswered after 6 h")
ok(all(x["piece"] in s.PIECES and x["words"] and x["lo"] <= s.defaults()[p] <= x["hi"]
       for p, x in s.SETTINGS.items()), "every value has words, a piece, and its default in range")
ok(set(s.TUNE_KEYS) == set(s.SETTINGS) and len([p for p in s.SETTINGS if p.startswith("size.")]) == len(ap.EVENTS),
   "every value reaches the code, one size per event")
ok("3.8" not in s.PIECES and "3.4" not in s.PIECES, "no card to turn off the fact check or let Flash pick numbers")

print("2. only your button saves; checked first")
ok(raises(lambda: s.save({"limit.per_message": 0.2}, door="rivia"), "not_allowed"), "Rivia can't change these")
ok(raises(lambda: s.save({"limit.per_message": 0.9}, door="app_button"), "out_of_range"), "out of range refused")
ok(raises(lambda: s.save({"reword.short_words": 12.5}, door="app_button"), "bad_value"), "a whole number is needed")
ok(raises(lambda: s.save({"strength.share_1": 0.9}, door="app_button"), "doesnt_fit"),
   "strength 1 can't give more than strength 2")
ok(not os.path.exists(s.path()), "nothing saved after refusals")
r = s.save({"limit.per_message": 0.25, "size.bad_news": 0.5}, door="app_button", note="softer")
ok([c["what"] for c in r["changes"]] == ["limit.per_message", "size.bad_news"], "two changes saved")
ok(ap._tune()["limit"] == 0.25 and ex._tune()["size_bad_news"] == 0.5, "both Step 3 files read your edit")
ok(max(abs(x) for x in ap.push_for("good_news", 3, ap._tune())) == 0.25, "every push scales with the limit")

print("3. locked")
for name in s.LOCKED:
    ok(raises(lambda n=name: s.save({n: 1}, door="app_button"), "locked"), "%s is locked" % name)
ok(raises(lambda: s.save({"nope": 1}, door="app_button"), "unknown"), "an unknown name is refused")

print("4. reset and log")
s.reset("piece:3.3", door="app_button")
ok(s.tune()["limit"] == 0.3 and s.tune()["size_bad_news"] == 0.5, "reset one piece")
s.reset("all", door="app_button")
ok(not os.path.exists(s.path()) and s.tune() == CODE, "reset all removes the file")
h = s.history()
ok(h[0]["action"] == "reset all" and h[-1]["note"] == "softer" and h[-1]["changes"][0]["old"] == 0.3,
   "every save and reset logged with old and new")

print("5. a broken file")
os.makedirs(s.rivia_dir(), exist_ok=True)
with open(s.path(), "w") as f:
    f.write("{not json")
eff = s.effective()
ok(eff["values"] == s.defaults() and "can't be read" in eff["problems"][0], "falls back with a plain note")
ok(ap._tune() == CODE, "Step 3 keeps running on the code's numbers")
with open(s.path(), "w") as f:
    json.dump({"version": 1, "changes": {"strength.share_1": 0.8, "strength.share_2": 0.5}}, f)
ok("no longer fit" in s.effective()["problems"][0], "saved values that don't fit together are ignored, said plainly")
os.remove(s.path())
v = s.view()
ok(v["layers"][0]["layer"] == 3 and v["notes"] and v["locked"], "the card's data in /app/style's shape")

print("\n%d checks passed" % COUNT["n"])
