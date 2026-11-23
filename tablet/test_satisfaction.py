"""Emotion spec Step 6.3: your corrections and your satisfaction. Throwaway body.db, never touches ~/rivia.
  1. who can write: thumbs only from the app's button, quiet days only from the code's judge, never her
  2. values: thumbs +1/-1, quiet +0.5 (your "Counts half"); one quiet signal a day
  3. a later thumb for a day takes its quiet signal off (your words count instead): counted once
  4. a quiet good day needs silence, the plan followed, and every judged arrival a hit; edits never count against her
  5. satisfaction starts at 0.5, moves with your signals, fades, and the quiet weight is adjustable
  6. a thumbs down names an area from her message's words; "workout" is the gym, not work
  7. your words: tablet code hears praise or a correction; unsure, a question or both means nothing; weight adjustable
  8. refuses emotion.db; no fear words
    python test_satisfaction.py
"""
import os, sqlite3, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rivia_satisfaction as sat  # noqa: E402

COUNT = {"n": 0}
DAY = 86400
NOW = 1_800_000_000


def ok(cond, label):
    assert cond, label
    COUNT["n"] += 1
    print("  ok ·", label)


def raises(fn, code):
    try:
        fn()
    except sat.FeedbackError as e:
        return e.code == code
    return False


con = sqlite3.connect(":memory:", isolation_level=None)

print("1. who can write")
ok(raises(lambda: sat.add(con, "thumb", 1, door="gemini", day="2026-12-01", now=NOW), "not_allowed"),
   "a thumb from anywhere but the app's button is refused")
ok(raises(lambda: sat.add(con, "quiet", 0.5, door="app_button", day="2026-12-01", now=NOW), "not_allowed"),
   "you can't send a quiet day by hand either; only the code's day judge does")
ok(raises(lambda: sat.add(con, "words", 1, door="app", day="2026-12-01", now=NOW), "not_allowed"),
   "words only through the app's words door (read by tablet code), never another door")
ok(raises(lambda: sat.add(con, "edit", -1, door="app_button", day="2026-12-01", now=NOW), "bad_source"),
   "edits aren't a signal at all (your 'Edits dont always necessarily mean she is wrong')")

print("2. values")
ok(raises(lambda: sat.add(con, "thumb", 5, door="app_button", day="2026-12-01", now=NOW), "bad_value"), "thumbs are +1 or -1")
ok(raises(lambda: sat.add(con, "quiet", 1, door="judge", day="2026-12-01", now=NOW), "bad_value"), "quiet is only +0.5")
ok(raises(lambda: sat.add(con, "thumb", True, door="app_button", day="2026-12-01", now=NOW), "bad_value"), "true isn't +1")
ok(raises(lambda: sat.add(con, "thumb", 1, door="app_button", day="Dec 1", now=NOW), "bad_value"), "day must be a date")
sat.add(con, "quiet", 0.5, door="judge", day="2026-12-01", now=NOW)
ok(raises(lambda: sat.add(con, "quiet", 0.5, door="judge", day="2026-12-01", now=NOW + 60), "already"),
   "one quiet signal a day")

print("3. counted once")
ok(not sat.said_anything(con, "2026-12-01"), "nothing said yet that day")
sat.add(con, "thumb", -1, door="app_button", day="2026-12-01", now=NOW + 3600, message_id="m1",
        text="Leave the farm at 11:55 to be home by noon")
srcs = [r["source"] for r in sat.rows(con) if r["day"] == "2026-12-01"]
ok(srcs == ["thumb"], "your thumb replaced the quiet signal for that day: %r" % srcs)
ok(sat.said_anything(con, "2026-12-01"), "and the day now reads as one you spoke up on")

print("4. the quiet good day")
hit = {"kind": "arrive", "result": "hit"}
miss = {"kind": "arrive", "result": "miss"}
rule_miss = {"kind": "rule", "result": "miss"}
ok(sat.is_quiet_good([hit, hit], 1.0, False)[0], "silent, plan followed, arrivals matched: a good day")
ok(not sat.is_quiet_good([hit], 1.0, True)[0], "you spoke: your words count instead")
ok(not sat.is_quiet_good([hit], 0.8, False)[0], "you rearranged the plan: not counted either way (no penalty)")
ok(not sat.is_quiet_good([hit, miss], 1.0, False)[0], "an arrival was off: not a quiet good day")
ok(not sat.is_quiet_good([hit, rule_miss], 1.0, False)[0], "a broken rule: not a quiet good day")
ok(not sat.is_quiet_good([{"kind": "arrive", "result": "skip"}], 1.0, False)[0], "nothing checkable: not counted")
ok(not sat.is_quiet_good([hit], None, False)[0], "no plan that day: not counted")

print("5. the number")
ok(sat.satisfaction([], NOW)["value"] == 0.5, "no signals: 0.5, don't know yet")
ups = [{"at": NOW, "source": "thumb", "value": 1.0}] * 8
downs = [{"at": NOW, "source": "thumb", "value": -1.0}] * 2
s = sat.satisfaction(ups + downs, NOW)
ok(abs(s["value"] - 0.75) < 0.001, "8 up, 2 down: (8+1)/(10+2) = 0.75: %s" % s["value"])
quiet = [{"at": NOW, "source": "quiet", "value": 0.5}] * 4
ok(abs(sat.satisfaction(quiet, NOW)["value"] - 3 / 4.0) < 0.001, "4 quiet days at half = 2 thumbs up: 0.75")
ok(abs(sat.satisfaction(quiet, NOW, quiet_weight=1.0)["value"] - 5 / 6.0) < 0.001,
   "the quiet weight from the app applies to rows already stored")
old = sat.satisfaction([{"at": NOW - 30 * DAY, "source": "thumb", "value": -1.0}], NOW)
ok(abs(old["bad"] - 0.5) < 0.001, "a 30-day-old thumbs down counts half")
ok(sat.satisfaction(downs, NOW, since=NOW + 1)["value"] == 0.5, "the off switch's fresh start ignores older ones")
ok(len(sat.corrections(ups + downs + quiet)) == 2, "only the downs are corrections")

print("6. areas from her words")
for text, area in (("The ride from the farm to home took longer", "ride:farm|home"),
                   ("Your workout starts at 6", "arrive:gym"), ("Leave for work at 1:50", "arrive:hmc"),
                   ("That broke your farm rule", "rules"), ("Feed the horses at 11", "arrive:farm"),
                   ("Sounds good!", "chat")):
    ok(sat.area_from_text(text) == area, "%r -> %s" % (text, sat.area_from_text(text)))
r = sat.add(con, "thumb", 1, door="app_button", day="2026-12-02", now=NOW, text="Your workout starts at 6")
ok(r["area"] == "chat", "a thumbs up doesn't name an area (only corrections lower one)")

print("7. your words")
cases = {
    "Good plan": 1.0, "Yeah that's perfect": 1.0, "The plan looks good to me.": 1.0, "You\u2019re right": 1.0,
    "That's wrong, the farm ride is 5 minutes": -1.0, "You forgot my gym time": -1.0, "That is not a good plan": -1.0,
    "I think that won't work": -1.0, "wrong time for the farm": -1.0,
    "Is this a good plan": None, "looks good?": None, "Perfect but that's wrong": None, "not perfect": None,
    "What's wrong with my bike": None, "Thanks": None, "": None,
}
for text, want in cases.items():
    got, heard = sat.read_words(text)
    ok(got == want, "%r -> %s (%s)" % (text, {1.0: "praise", -1.0: "correction", None: "nothing"}[want], heard))
ok(sat.read_words("Good plan. Is the gym open?")[0] == 1.0, "a question after the praise doesn't cancel it")
ok(sat.read_words("x " * 3000 + "good plan")[0] is None, "only the first %d characters are read" % sat.WORDS_MAX)
w_up = {"at": NOW, "day": "d", "source": "words", "value": 1.0}
ok(sat.satisfaction([w_up], NOW)["value"] == sat.satisfaction([dict(w_up, source="thumb")], NOW)["value"],
   "your words count as much as a thumb to start")
ok(sat.satisfaction([w_up], NOW, words_weight=0)["value"] == 0.5, "words weight 0 turns them off")
c2 = sqlite3.connect(":memory:", isolation_level=None)
sat.add(c2, "quiet", 0.5, door="judge", day="2026-12-02", now=NOW)
sat.add(c2, "words", 1, door="app_words", day="2026-12-02", now=NOW, note="heard: good plan")
ok([r["source"] for r in sat.rows(c2)] == ["words"], "your words that day take the quiet half point off")

print("8. guards")
d = tempfile.mkdtemp()
emo = sqlite3.connect(os.path.join(d, "emotion.db"))
try:
    sat.add(emo, "thumb", 1, door="app_button", day="2026-12-01", now=NOW)
    ok(False, "emotion.db refused")
except ValueError as e:
    ok("emotion.db" in str(e), "emotion.db is refused")
src = open(os.path.join(HERE, "rivia_satisfaction.py")).read().lower()
ok(not any(w in src for w in (" fear", "scared", "afraid")), "no fear words")

print("\n%d checks passed" % COUNT["n"])
