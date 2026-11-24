"""Emotion spec Step 8: how she would sound, in shadow. Throwaway RIVIA_DIR and body.db.
  1. your energy: swears and ALL-CAPS counted (acronyms and LOL aren't shouting), rapid-fire; text never stored
  2. worked up: against your own normal (Step 7); swearing that's normal for you doesn't count; before a normal
     exists only what you say counts
  3. steady, then calmer: she never rises with you; holds, eases over 3 messages to 1 step under her usual, never
     lower; holds while you settle; back after 2 normal messages or 30 quiet minutes
  4. style: dry with light sass when mildly irritated, no sass past the switch; playful, deadpan, crisp, plain;
     your stress wins (no sass, no teasing, strength at most 1); a little warmth; cursing only when you turn it on
     and never while you're stressed or cursing
  5. learning: nothing in shadow; on styled replies a streak of 3 moves strength one step, held to the limit;
     "knock it off" mutes the style for a day; a correction never counts as a style reaction
  6. joke cooldown: a bit can't come back inside 7 days; it's her words only
  7. the guard: never emotion.db; learning writes only its own table; for_expression is "" in shadow; the fixed
     rules keep pushback; no fear words; never a tool
    python test_style.py
"""
import ast, os, re, sqlite3, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ.update(HOME=TMP, RIVIA_DIR=os.path.join(TMP, "rivia"), RIVIA_NRC_LEXICON=os.path.join(TMP, "none.txt"))
sys.path.insert(0, HERE)
import rivia_reading as rd  # noqa: E402
import rivia_style as st  # noqa: E402

COUNT = {"n": 0}
T = st.code_tune()
T7 = rd.code_tune()


def ok(cond, label):
    assert cond, label
    COUNT["n"] += 1
    print("  ok ·", label)


def db(name="body.db"):
    con = sqlite3.connect(os.path.join(tempfile.mkdtemp(), name), isolation_level=None)
    rd.prepare(con)
    return st.prepare(con)


SEQ = {"n": 0}


def put(con, at, situation="day_off", said=None, personal=0, correction=0, mid=None):
    """A Step 7 row, as rd.take would leave it (numbers only)."""
    SEQ["n"] += 1
    mid = mid or "m%d" % SEQ["n"]
    con.execute("insert into reading_messages(message_id, at, day, situation, words, gap_min, terse, correction, "
                "flash_status, personal, said, received) values(?,?,?,?,?,?,?,?,?,?,?,?)",
                (mid, at, rd._day(at), situation, 5, None, 0, correction, "off", personal, said, at))
    return mid


READ = {"value": None}
REAL_READ_NOW = rd.read_now


def fake_read_now(con, now=None, tune=None):
    return READ["value"]


def zones(sit="day_off", arousal=("normal", None), valence=("normal", None)):
    return {"situation": sit, "enough": True,
            "measures": {"arousal": {"zone": arousal[0], "direction": arousal[1]},
                         "valence": {"zone": valence[0], "direction": valence[1]}}}


HER_CALM = {"name": "neutral", "intensity": "neutral", "a": 0.1, "rest_a": 0.1, "irritation": 0.0}
HER_JOY = {"name": "joy", "intensity": "mild", "a": 0.35, "rest_a": 0.1, "irritation": 0.0}
HER_UP = {"name": "focused", "intensity": "moderate", "a": 0.6, "rest_a": 0.1, "irritation": 0.0}


def send(con, text, at, her=HER_CALM, **kw):
    mid = put(con, at, **kw)
    return st.take(mid, text, con=con, now=at, tune=T, her=her)


print("1. your energy")
ok(st.swear_count("this is fucking BS, what the hell") == 2, "swears counted")
ok(st.swear_count("hello shell assassin classic") == 0, "words that only contain a swear don't count")
ok(st.caps_count("WHY IS THIS STILL BROKEN") == 4 and st.caps_count("HMC shift, the API and MCP, LOL") == 0,
   "ALL-CAPS words of 3+ letters counted; HMC, API, MCP and LOL aren't shouting")
ok(st.caps_count("I am OK") == 0, "short caps like I and OK don't count")
con = db()
base = 1_800_000_000.0
for i in range(3):
    put(con, base + i * 30)
ok(st.rapid(con, base + 60, T) == 1, "3 messages within 2 minutes is rapid-fire")
ok(st.rapid(con, base + 400, T) == 0, "spread out isn't")

print("2. worked up, against your normal")
rd.read_now = fake_read_now
con = db()
facts0 = {"swears": 0, "caps": 0, "rapid": 0}
m = {"situation": "day_off", "said": None, "personal": 0}
READ["value"] = zones(arousal=("well_off", "up"))
ok(st.his_read(con, m, facts0, base, T, T7)["worked_up"], "words well past your usual charge: worked up")
READ["value"] = zones(arousal=("bit_off", "up"))
ok(not st.his_read(con, m, facts0, base, T, T7)["worked_up"], "a bit past your usual on its own: not worked up")
h = st.his_read(con, m, {"swears": 2, "caps": 0, "rapid": 0}, base, T, T7)
ok(h["worked_up"] and "swearing" in h["why"], "a bit past your usual with swearing: worked up")
ok(st.his_read(con, m, {"swears": 0, "caps": 0, "rapid": 1}, base, T, T7)["worked_up"], "...or with rapid-fire")
ok(not st.his_read(con, m, {"swears": 0, "caps": 2, "rapid": 0}, base, T, T7)["worked_up"],
   "2 caps words isn't shouting (needs 3)")
READ["value"] = zones(arousal=("bit_off", "down"))
ok(not st.his_read(con, m, {"swears": 2, "caps": 0, "rapid": 0}, base, T, T7)["worked_up"],
   "swearing while your charge is low isn't worked up")
for i in range(30):   # you swear in most day-off messages: that's your normal there
    con.execute("insert into style_messages values(?,?,?,?,?,?,?,?,?,?,?)",
                ("s%d" % i, base - 1000 - i, rd._day(base), "day_off", 1, 0, 0, 0, None, None, base))
READ["value"] = zones(arousal=("bit_off", "up"))
h = st.his_read(con, m, {"swears": 2, "caps": 0, "rapid": 0}, base, T, T7)
ok(not h["worked_up"] and not h["signals"], "swearing that's normal for you in this situation doesn't count")
h = st.his_read(con, {**m, "situation": "after_shift"}, {"swears": 2, "caps": 0, "rapid": 0}, base, T, T7)
ok(h["signals"] == ["swearing"] and not h["worked_up"],
   "after a shift (no normal yet for swearing there): it's a signal, but your charge isn't judged, so not worked up")
READ["value"] = None
con = db()
ok(not st.his_read(con, m, {"swears": 3, "caps": 5, "rapid": 1}, base, T, T7)["worked_up"],
   "before your normal exists, swearing and caps alone don't count")
h = st.his_read(con, {**m, "said": "stressed"}, facts0, base, T, T7)
ok(h["worked_up"] and h["stressed"] and "you said" in h["why"], "...but you saying you're stressed does")
h = st.his_read(con, {**m, "said": "tired"}, facts0, base, T, T7)
ok(not h["worked_up"] and h["stressed"], "tired: not worked up, but no teasing")
READ["value"] = zones(valence=("well_off", "down"))
ok(st.his_read(con, m, facts0, base, T, T7)["stressed"], "words reading well heavier than usual: no teasing")

print("3. steady, then calmer")
s0 = {"phase": "usual", "start_step": 0, "step_i": 0, "worked_at": None, "calm_streak": 0, "updated": 0}
s, tgt, w = st.energy_plan(s0, False, 1, base, T)
ok(tgt == 1 and s["phase"] == "usual", "he's fine: she's at her own level")
s, tgt, w = st.energy_plan(s0, True, 2, base, T)
ok(tgt == 2 and "holding steady" in w, "first worked-up message: she holds her own level")
steps = [tgt]
for i in range(1, 6):
    s, tgt, w = st.energy_plan(s, True, 3, base + i * 60, T)   # her own level rising: she still doesn't rise
    steps.append(tgt)
ok(steps[:4] == [2, 1, 0, -1], "eases down over 3 messages to 1 step under her usual (%s)" % steps)
ok(max(steps) <= 2, "never rises with him, even if her own energy climbs")
ok(min(steps) == -1 and "a bit calmer" in w, "never lower than 1 step under her usual")
s, tgt, w = st.energy_plan(s, False, 0, base + 400, T)
ok(tgt == -1 and s["phase"] == "easing" and "settle" in w, "one normal message: holds")
s, tgt, w = st.energy_plan(s, False, 0, base + 460, T)
ok(tgt == 0 and s["phase"] == "usual", "two normal messages in a row: back to her usual")
s, _, _ = st.energy_plan(s0, True, 0, base, T)
s, tgt, _ = st.energy_plan(s, False, 0, base + 31 * 60, T)
ok(s["phase"] == "usual", "30 quiet minutes: back to her usual")
s, tgt, _ = st.energy_plan(s0, True, -3, base, T)
ok(tgt == -1, "already low: held at 1 step under her usual, never flat")
T0 = {**T, "calm_steps": 0}
s, _, _ = st.energy_plan(s0, True, 1, base, T0)
for i in range(4):
    s, tgt, _ = st.energy_plan(s, True, 1, base + i, T0)
ok(tgt == 0, "0 calm steps on the card: she eases only to her usual")

print("4. style")
calm = {"stressed": False, "personal": False, "cursing_now": False}
p = st.pick_style({"name": "annoyance", "intensity": "mild", "irritation": 0.2}, calm, 0, None, T)
ok(p["style"] == "dry" and p["sass"], "mildly irritated: dry with light sass")
p2 = st.pick_style({"name": "anger", "intensity": "moderate", "irritation": 0.7}, calm, 0, None, T)
ok(p2["style"] == "dry" and not p2["sass"] and p2["strength"] > p["strength"] and "more focused" in p2["notes"][0],
   "past the switch: dry, no sass, more focused")
ok(st.pick_style({"name": "joy", "intensity": "strong", "irritation": 0}, calm, 0, None, T)["style"] == "playful",
   "joy: playful")
ok(st.pick_style({"name": "joy", "intensity": "strong", "irritation": 0}, calm, 0, None, T)["strength"] == 1,
   "playful held to its range (1)")
ok(st.pick_style({"name": "distressed", "intensity": "mild", "irritation": 0}, calm, 0, None, T)["style"]
   == "deadpan", "distressed: deadpan")
ok(st.pick_style({"name": "focused", "intensity": "mild", "irritation": 0}, calm, 0, None, T)["style"] == "crisp",
   "focused: crisp")
ok(st.pick_style({"name": "surprise", "intensity": "mild", "irritation": 0}, calm, 0, None, T)["style"] == "plain",
   "anything else: plain")
hot = {"stressed": True, "personal": False, "cursing_now": True}
p = st.pick_style({"name": "joy", "intensity": "strong", "irritation": 0}, hot, 0, None, T)
ok(p["style"] == "plain" and p["warmth"] == 1, "you're stressed: no teasing, a little warmer")
p = st.pick_style({"name": "annoyance", "intensity": "strong", "irritation": 0.1}, hot, 0, None, T)
ok(p["style"] == "dry" and not p["sass"] and p["strength"] <= 1, "you're stressed: dry with no sass, strength 1")
ok(st.pick_style({"name": "neutral", "intensity": "neutral", "irritation": 0}, {**calm, "personal": True}, 0, None,
                 T)["warmth"] == 1, "something personal: a little warmer")
ok(st.pick_style({"name": "neutral", "intensity": "neutral", "irritation": 0}, hot, 0, None, {**T, "warmth_cap": 0})
   ["warmth"] == 0, "warmth set to 0 on the card: no lift")
ok(not any(st.pick_style({"name": n, "intensity": "mild", "irritation": 0.1}, calm, 0, None, T)["curse_ok"]
           for n in st.STYLE_OF), "cursing off by default")
TB = {**T, "banter_on": 1}
ok(st.pick_style({"name": "joy", "intensity": "mild", "irritation": 0}, calm, 0, None, TB)["curse_ok"],
   "banter on, you're calm, playful: cursing ok")
ok(not st.pick_style({"name": "joy", "intensity": "mild", "irritation": 0}, {**calm, "cursing_now": True}, 0, None,
                     TB)["curse_ok"], "never while you're cursing")
ok(not st.pick_style({"name": "joy", "intensity": "mild", "irritation": 0}, hot, 0, None, TB)["curse_ok"],
   "never while you're stressed")
ok(not st.pick_style({"name": "anger", "intensity": "strong", "irritation": 0.9}, calm, 0, None, TB)["curse_ok"],
   "never in no-sass dry (she's focused)")
ok(st.pick_style({"name": "focused", "intensity": "strong", "irritation": 0}, calm, 5, None, T)["strength"] == 3,
   "learning can't push past the range")

print("5. learning")
rd.read_now = REAL_READ_NOW
con = db()
t0 = base + 10_000
send(con, "move my gym to 6", t0, her=HER_JOY)
r = send(con, "haha good one", t0 + 60, her=HER_JOY)
ok(r["style"] == "playful" and not con.execute("select * from style_learning").fetchall(),
   "shadow: her reply wasn't styled, so nothing is learned")


def styled_line(con, at, style="playful"):
    st._log(con, "line", at, {"style": style, "line": "x"}, "x%d" % at, styled=True)


def reply(con, text, at, **kw):
    mid = put(con, at, **kw)
    return st.react(con, mid, text, rd.message(con, mid), at, T)


con = db()
styled_line(con, t0)
r1 = reply(con, "lol", t0 + 10)
styled_line(con, t0 + 20)
r2 = reply(con, "haha", t0 + 30)
ok(r1["moved"] == 0 and r2["moved"] == 0, "two laughs: not yet")
styled_line(con, t0 + 40)
r3 = reply(con, "LMAO ok", t0 + 50)
ok(r3["moved"] == 1 and st.learned(con, "playful", t0 + 50)[0] == 1, "three in a row: playful up one step")
for k in range(9):
    styled_line(con, t0 + 100 + k * 20)
    reply(con, "lol", t0 + 110 + k * 20)
ok(st.learned(con, "playful", t0 + 400)[0] == T["learn_max"], "held to 2 steps either way")
styled_line(con, t0 + 500)
r = reply(con, "ok knock it off", t0 + 510)
adj, muted = st.learned(con, "playful", t0 + 520)
ok(r["reaction"] == "knock_it_off" and muted and adj == T["learn_max"] - 1, "knock it off: down a step, off now")
p = st.pick_style({"name": "joy", "intensity": "mild", "irritation": 0}, calm, adj, muted, T)
ok(p["style"] == "plain" and "knock it off" in p["notes"][0], "the muted style turns plain")
ok(st.learned(con, "playful", t0 + 510 + 25 * 3600)[1] is None, "back after 24 hours")
styled_line(con, t0 + 600, "dry")
ok(reply(con, "That's wrong, I said after work", t0 + 610, correction=1) is None,
   "a correction is about what she said, not how: never a style reaction")
ok(not con.execute("select * from style_learning where style = 'dry'").fetchall(), "dry learned nothing")
ok(st.read_reaction("thanks") == "ignores" and st.read_reaction("😂") == "plays_along"
   and st.read_reaction("not the time") == "knock_it_off", "reactions: ignores, plays along, knock it off")

print("6. joke cooldown")
con = db()
ok(st.record_bit(con, "Bold of you to assume the bus is on time.", now=t0, tune=T) is False, "first use")
ok(st.is_cooling(con, "bold of you to assume the BUS is on time", now=t0 + 86400, tune=T), "the same bit is cooling")
ok(st.record_bit(con, "Bold of you to assume the bus is on time!", now=t0 + 2 * 86400, tune=T) is True,
   "used again inside 7 days: logged as a repeat")
ok(not st.is_cooling(con, "Bold of you to assume the bus is on time.", now=t0 + 10 * 86400, tune=T),
   "after 7 days it can come back")
ok(st.cooling(con, t0 + 3 * 86400, T) and st.log_rows(con, 0, ("repeat_bit",)), "listed on the card")

print("7. the guard, end to end")
con = db()
r = send(con, "WHY THE FUCK IS THE BUS LATE AGAIN, secretword", t0, her=HER_UP, said="annoyed")
dump = str([con.execute("select * from %s" % t).fetchall() for t in
            ("style_messages", "style_state", "style_learning", "style_bits", "style_log")])
ok("secretword" not in dump and "BUS" not in dump and "fuck" not in dump.lower(), "your text isn't stored")
ok(r["him"]["worked_up"] and r["energy"].startswith("holding") and r["energy_step"] == r["her_step"],
   "worked up: she holds her own level")
ok("no cursing" in r["line"] and r["shadow"], "no cursing back; logged as shadow")
ok(st.take("nope", "hi", con=con, now=t0, tune=T, her=HER_CALM) is None, "a message Step 7 didn't read: nothing")
ok(st.take("", "hi", con=con) is None and st.take("x", None, con=con) is None, "bad input: nothing")
ok(st.for_expression(con) == "", "for_expression is \"\" in shadow")
st.SHADOW = False
ok(st.for_expression(con).count("\n") == len(st.FIXED_RULES) and "push back" in st.for_expression(con),
   "out of shadow the line always carries the fixed rules (push back when warranted)")
st.SHADOW = True
try:
    st.prepare(sqlite3.connect(os.path.join(tempfile.mkdtemp(), "emotion.db")))
    ok(False, "emotion.db refused")
except st.StyleError as e:
    ok(e.code == "wrong_db", "emotion.db refused")
tables_before = {r[0]: con.execute("select count(*) from %s" % r[0]).fetchone()[0]
                 for r in con.execute("select name from sqlite_master where type = 'table' and name not like "
                                      "'sqlite_%'").fetchall()}
st.learn(con, "dry", "plays_along", t0, T)
tables_after = {k: con.execute("select count(*) from %s" % k).fetchone()[0] for k in tables_before}
ok([k for k in tables_before if tables_before[k] != tables_after[k]] == ["style_learning"],
   "learning writes only its own table (no caution, trust or mood)")
ok(st.learn(con, "pushback", "plays_along", t0, T) is None and st.learn(con, "plain", "knock_it_off", t0, T) is None,
   "there's nothing to learn on pushback or plain")
src = open(os.path.join(HERE, "rivia_style.py")).read()
code_text = " ".join(re.findall(r'"([^"\n]*)"', src))
ok(not re.search(r"\b(afraid|scared|fear|fearful|terrified|panic)\b", code_text, re.I), "no fear words")
tree = ast.parse(src)
mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
       {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
ok(not {m for m in mods if m.startswith(("fastmcp", "mcp", "rivia_tools", "rivia_trust", "rivia_caution"))},
   "imports no tool, trust or caution code")
v = st.view(con=con, now=t0 + 5, tune=T)
ok(v["layer"] == 8 and v["latest"]["style"] and v["shadow"] and len(v["waits_on"]) == 3, "the card's data")
ok(st.view(con=db(), now=t0, tune=T)["note"].startswith("nothing worked out yet"), "empty: says why")

print("\n%d checks passed" % COUNT["n"])
