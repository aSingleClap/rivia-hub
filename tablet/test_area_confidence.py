"""Emotion spec Step 6.4 + 6.5: how sure she is in each area, and double-check flags. Pure.
  1. only her own counted misses and your thumbs down lower an area; yours, chance and same-mistake misses don't
  2. counted once: one strike per area per day, however many misses and thumbs down
  3. it fades by half in a week, never goes under the floor, never above 1.0, and only that area moves
  4. flags: on under 0.70, off over 0.80, in plain words; logged only
    python test_area_confidence.py
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rivia_area_confidence as ac  # noqa: E402

COUNT = {"n": 0}
DAY = 86400
NOW = 1_800_000_000


def ok(cond, label):
    assert cond, label
    COUNT["n"] += 1
    print("  ok ·", label)


def miss(day, area, owner="rivia", counted=True, at=NOW):
    return {"day": day, "area": area, "result": "miss", "owner": owner, "counted": counted, "judged_at": at}


def down(day, area, at=NOW):
    return {"day": day, "area": area, "at": at, "value": -1.0, "source": "thumb"}


print("1. whose misses count")
c = ac.confidence([miss("d1", "ride:farm|home", "jordan"), miss("d1", "arrive:farm", "chance"),
                   miss("d1", "arrive:home", counted=False), {"day": "d1", "area": "pin:farm", "result": "hit",
                                                               "owner": None, "counted": True, "judged_at": NOW}],
                  [], NOW)
ok(c == {}, "your misses, chance, same-mistake and hits leave every area at 1.0")
c = ac.confidence([miss("d1", "ride:farm|home")], [], NOW)
ok(abs(c["ride:farm|home"]["value"] - 0.85) < 0.001, "one of her misses: that area to 0.85")
c = ac.confidence([], [down("d1", "arrive:gym")], NOW)
ok(abs(c["arrive:gym"]["value"] - 0.85) < 0.001, "your thumbs down counts the same as a miss")

print("2. counted once")
c = ac.confidence([miss("d1", "arrive:farm"), miss("d1", "arrive:farm")], [down("d1", "arrive:farm")], NOW)
ok(c["arrive:farm"]["strikes"] == 1 and abs(c["arrive:farm"]["value"] - 0.85) < 0.001,
   "two misses and a thumbs down on the same area and day: one strike")
c = ac.confidence([miss("d1", "arrive:farm"), miss("d2", "arrive:farm")], [], NOW)
ok(c["arrive:farm"]["strikes"] == 2, "two different days: two strikes")

print("3. fades, floor, only that area")
c = ac.confidence([miss("d1", "arrive:farm", at=NOW - 7 * DAY)], [], NOW)
ok(abs(c["arrive:farm"]["value"] - 0.925) < 0.001, "a week later the cost is half (0.925)")
ok(ac.confidence([miss("d1", "arrive:farm", at=NOW - 60 * DAY)], [], NOW) == {}, "two months later it's gone")
many = [miss("d%d" % i, "arrive:farm") for i in range(20)]
ok(ac.confidence(many, [], NOW)["arrive:farm"]["value"] == ac.FLOOR, "twenty bad days: stops at the floor 0.30")
ok(set(ac.confidence(many, [], NOW)) == {"arrive:farm"}, "no other area moves")
ok(ac.confidence([miss("d1", "arrive:farm", at=NOW - DAY)], [], NOW, since=NOW - 3600) == {},
   "the off switch's fresh start ignores what came before")

print("4. flags")
c = ac.confidence([miss("d1", "arrive:farm"), miss("d2", "arrive:farm")], [], NOW)  # 0.70
c3 = ac.confidence([miss("d%d" % i, "arrive:farm") for i in range(3)], [], NOW)    # 0.55
f = ac.flags(c3)
ok(len(f) == 1 and f[0]["say"] == "double-check getting to the farm on time",
   "under 0.70: flagged in plain words: %r" % f[0]["say"])
ok(ac.flags(c) == [], "exactly 0.70 isn't under the line")
mid = {"arrive:farm": {"value": 0.75, "strikes": 1, "words": "the farm"}}
ok(ac.flags(mid) == [] and len(ac.flags(mid, prev=["arrive:farm"])) == 1,
   "0.75: not flagged fresh, but a flag already on stays until over 0.80 (no flicker)")
ok(ac.caution_lines(f)[0].startswith("verify before answering about"), "caution line built for later, logged only")
ok(ac.flags(c3, tune={"flag_below": 0.5, "flag_clear": 0.6}) == [], "the lines are adjustable")

print("\n%d checks passed" % COUNT["n"])
