"""Emotion spec Step 8.11: Step 8's adjustable values, one small card per piece. Throwaway RIVIA_DIR.
  1. the code is the source of truth: no file means the code's values; every value has words, a range and a piece
  2. only your app's button saves; every change is checked first and a bad one saves nothing
  3. safety limits are locked and refused by name (pushback, energy matching, cursing back, no fear...)
  4. reset one setting, one piece, or all; logged with old and new
  5. a broken file falls back to the code's values; rivia_style reads them through tune()
    python test_style_settings.py
"""
import json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ.update(HOME=TMP, RIVIA_DIR=os.path.join(TMP, "rivia"))
sys.path.insert(0, HERE)
import rivia_style as st  # noqa: E402
import rivia_style_settings as ss  # noqa: E402

COUNT = {"n": 0}


def ok(cond, label):
    assert cond, label
    COUNT["n"] += 1
    print("  ok ·", label)


def raises(fn, code):
    try:
        fn()
    except ss.SettingsError as e:
        return e.code == code
    return False


print("1. the code is the source of truth")
eff = ss.effective()
v = eff["values"]
ok(not eff["changed"] and v == ss.defaults(), "no file: the code's values")
ok(ss.tune() == st.code_tune(), "the card's values are exactly what the code uses")
ok(v["ease.messages"] == 3 and v["ease.calm_steps"] == 1 and v["ease.back_messages"] == 2
   and v["ease.back_minutes"] == 30, "your starting numbers: ease over 3, 1 step calmer, back after 2 or 30 min")
ok(v["warmth.cap"] == 1 and v["sass.switch"] == 0.5 and v["banter.on"] == 0 and v["bits.days"] == 7,
   "warmth 1 step, sass switch at 0.5, banter off, 7-day joke cooldown")
ok((v["strength.dry"], v["strength.crisp"], v["strength.deadpan"], v["strength.playful"]) == (3, 3, 2, 1),
   "strength ranges dry 3, crisp 3, deadpan 2, playful 1")
ok(v["energy.rapid_count"] == 3 and v["energy.rapid_seconds"] == 120 and v["energy.caps_words"] == 3,
   "rapid-fire 3 in 2 minutes, shouting 3 caps words")
ok(v["learn.same"] == 3 and v["learn.mute_hours"] == 24, "learning after 3 in a row; knock it off for 24 hours")
ok(all(s["piece"] in ss.PIECES and s["words"] and s["lo"] <= ss.defaults()[p] <= s["hi"]
       for p, s in ss.SETTINGS.items()), "every value has words, a piece, and its default in range")
ok(set(ss.TUNE_KEYS) == set(ss.SETTINGS), "every value reaches the code")
ok("8.5" not in ss.PIECES and "8.9" not in ss.PIECES, "no card for your-stress-wins or the pushback guard (fixed)")
ok(ss.SETTINGS["warmth.cap"]["hi"] == 1 and ss.SETTINGS["ease.calm_steps"]["hi"] == 2,
   "warmth can't go past 1 step; calmer can't go past 2 steps (never flat)")

print("2. only your button saves; checked first")
ok(raises(lambda: ss.save({"banter.on": 1}, door="rivia"), "not_allowed"), "Rivia can't turn cursing on")
ok(raises(lambda: ss.save({"warmth.cap": 3}, door="app_button"), "out_of_range"), "out of range refused")
ok(raises(lambda: ss.save({"ease.messages": 2.5}, door="app_button"), "bad_value"), "a whole number is needed")
ok(raises(lambda: ss.save({"banter.on": True}, door="app_button"), "bad_value"), "true isn't a number")
ok(not os.path.exists(ss.path()), "nothing saved after refusals")
r = ss.save({"ease.messages": 4, "sass.switch": 0.4}, door="app_button", note="slower ease")
ok([c["what"] for c in r["changes"]] == ["ease.messages", "sass.switch"], "two changes saved")
ok(ss.tune()["ease_messages"] == 4 and st._tune()["sass_switch"] == 0.4, "rivia_style reads your edit")

print("3. locked")
for name in ss.LOCKED:
    ok(raises(lambda n=name: ss.save({n: 1}, door="app_button"), "locked"), "%s is locked" % name)
ok(raises(lambda: ss.save({"nope": 1}, door="app_button"), "unknown"), "an unknown name is refused")

print("4. reset and log")
ss.reset("piece:8.3", door="app_button")
ok(ss.tune()["sass_switch"] == 0.5 and ss.tune()["ease_messages"] == 4, "reset one piece")
ss.reset("all", door="app_button")
ok(not os.path.exists(ss.path()) and ss.tune() == st.code_tune(), "reset all removes the file")
h = ss.history()
ok(h[0]["action"] == "reset all" and h[-1]["note"] == "slower ease" and h[-1]["changes"][0]["old"] == 3,
   "every save and reset logged with old and new")
ok(raises(lambda: ss.reset("piece:9.9", door="app_button"), "unknown"), "an unknown piece is refused")

print("5. a broken file")
os.makedirs(ss.rivia_dir(), exist_ok=True)
with open(ss.path(), "w") as f:
    f.write("{not json")
eff = ss.effective()
ok(eff["values"] == ss.defaults() and "can't be read" in eff["problems"][0], "falls back with a plain note")
ok(st._tune() == st.code_tune(), "Step 8 keeps running on the code's numbers")
with open(ss.path(), "w") as f:
    json.dump({"version": 1, "changes": {"warmth.cap": 9}}, f)
ok("doesn't pass" in ss.effective()["problems"][0], "a saved value out of range is ignored, said plainly")
os.remove(ss.path())
v = ss.view()
ok(v["layers"][0]["layer"] == 8 and v["notes"] and v["locked"], "the card's data in /app/reading's shape")

print("\n%d checks passed" % COUNT["n"])
