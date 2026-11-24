"""The layer and piece switches (rivia_toggles, /app/toggles), from server-proposal.md, server-proposal-layer8.md and
BUILD.md (Nov 24, "Toggles first"). Throwaway RIVIA_DIR, HOME and auth.db; never touches ~/rivia.
  1. route: app token only (401 without, 403 admin); the exact GET shape, every piece with step; POST codes
  2. defaults: no file = today's behavior; a broken or half-written file = defaults, reported in problems
  3. the file holds only Jordan's choices; states are worked out on read; atomic writes; the change log
  4. cascades: l1 off forces l2 off and says why; turning l1 back on restores l2 to Jordan's last setting
  5. each switch stops its work (l4.collect, l4.dials, l5, l6.score, l6.words, l7, l7.2, l7.7, l7.8, l8, l1, l2, l2.8)
  6. the minute loop is split from Layer 4: RIVIA_BODY_COLLECTOR=0 with l5 on still ticks and scores
  7. 7.1 Flash writes through to Step 7's facts.flash_on (one value)
  8. env flags: the switches read them the same way the modules do; never a tool on any door
    python test_toggles.py
"""
import asyncio, json, os, secrets, socket, sqlite3, sys, tempfile, threading, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
DOT = os.path.join(TMP, "dot")
RIVIA = os.path.join(TMP, "rivia")
os.makedirs(DOT, mode=0o700)
os.makedirs(RIVIA)
os.environ.update(RIVIA_DIR=RIVIA, RIVIA_HOME=RIVIA, RIVIA_DOTDIR=DOT, RIVIA_DB=os.path.join(TMP, "auth.db"),
                  HOME=TMP, RIVIA_JWT_KEY=secrets.token_hex(32), RIVIA_SIGNIN_CHECK="0",
                  RIVIA_GOOGLE_TOKEN_PATH=os.path.join(DOT, "google_token.json"),
                  RIVIA_NRC_LEXICON=os.path.join(TMP, "none.txt"))
for k in ("RIVIA_TRUST_KILL", "GEMINI_API_KEY", "RIVIA_BODY_COLLECTOR", "RIVIA_BODY_SHADOW", "RIVIA_PLAN_SHADOW",
          "RIVIA_TRUST_SHADOW", "RIVIA_DRIFT_WEEKLY"):
    os.environ.pop(k, None)
sys.path.insert(0, HERE)

import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
import rivia_auth as ra  # noqa: E402
import rivia_body_collect as collect  # noqa: E402
import rivia_emotion_app as eapp  # noqa: E402
import rivia_normal as normal  # noqa: E402
import rivia_reading as rd  # noqa: E402
import rivia_reading_app as rapp  # noqa: E402
import rivia_reading_flash as flash  # noqa: E402
import rivia_reading_settings as rset  # noqa: E402
import rivia_style as st  # noqa: E402
import rivia_style_app as sapp  # noqa: E402
import rivia_toggles as tg  # noqa: E402
import rivia_toggles_app as tgapp  # noqa: E402
import rivia_trust_app as tapp  # noqa: E402

COUNT = {"n": 0}


def ok(cond, label):
    assert cond, label
    COUNT["n"] += 1
    print("  ok ·", label)


def standin_mcp():
    async def mcp(request):
        return PlainTextResponse("mcp mount")
    return Starlette(routes=[Route("/mcp", mcp, methods=["GET", "POST"])])


def serve(app):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    return server, "http://127.0.0.1:%d" % port


def http(p, token=None, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + p, data=data, method=method or ("POST" if data is not None else "GET"))
    if token:
        req.add_header("authorization", "Bearer " + token)
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r_:
            text, status = r_.read().decode(), r_.status
    except urllib.error.HTTPError as e:
        text, status = e.read().decode(), e.code
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def wait_for(fn, s=5.0):
    t0 = time.time()
    while time.time() - t0 < s:
        if fn():
            return True
        time.sleep(0.05)
    return False


def clear():
    for f in (tg.path(), tg.log_path()):
        if os.path.exists(f):
            os.remove(f)


def piece(r, i):
    return [p for p in r["pieces"] if p["id"] == i][0]


def count(table):
    c = normal.db_open()
    try:
        try:
            return c.execute("select count(*) from %s" % table).fetchone()[0]
        except sqlite3.OperationalError:
            return 0
    finally:
        c.close()


st.her_state = lambda: {"name": "neutral", "intensity": "neutral", "a": 0.0, "rest_a": 0.0, "irritation": 0.0}
CAL = {"n": 0}


def fake_calendar(a, b):
    CAL["n"] += 1
    return []


rd.CALENDAR = fake_calendar
FLASH_CALLS = {"n": 0}
_real_flash_read = flash.read


def counting_flash(*a, **k):
    FLASH_CALLS["n"] += 1
    return _real_flash_read(*a, **k)


flash.read = counting_flash
server, BASE = serve(ra.build_app(app_mcp=standin_mcp(), admin_mcp=standin_mcp(),
                                  extra_routes=tapp.routes() + rapp.routes() + sapp.routes() + tgapp.routes()
                                  + eapp.routes()))
APP = ra.mint_door_token("app", 1)["token"]
ADMIN = ra.mint_door_token("admin", 1)["token"]

print("1. route")
ok(http("/app/toggles")[0] == 401, "no token: 401")
ok(http("/app/toggles", body={"id": "l7", "on": False})[0] == 401, "POST without a token: 401")
ok(http("/app/toggles", ADMIN)[0] == 403, "the admin token can't open the app's door")
ok(not os.path.exists(tg.path()), "nothing saved by a refused POST")
st_, r = http("/app/toggles", APP)
ok(st_ == 200 and r["ok"] is True and r["version"] == 1 and r["note"] is None and r["problems"] == [],
   "GET: ok, version 1, no note, no problems")
KEYS = {"id", "layer", "step", "title", "where", "switchable", "value", "state", "needs", "needed_by", "why",
        "would_also_turn_off"}
ok(all(set(p) == KEYS for p in r["pieces"]), "every piece has exactly the contract's fields, step included")
ok(all(p["step"] is None or isinstance(p["step"], str) for p in r["pieces"]), "step is a string or null")
ok(all(isinstance(p["layer"], int) and 1 <= p["layer"] <= 8 for p in r["pieces"]), "layer is a whole number 1 to 8")
layers = [p["layer"] for p in r["pieces"]]
ok(layers == sorted(layers) and set(layers) == set(range(1, 9)), "screen order, Layers 1 to 8")
for lay in (1, 2, 5, 7, 8):
    first = [p for p in r["pieces"] if p["layer"] == lay][0]
    ok(first["id"] == "l%d" % lay and first["step"] is None, "Layer %d's whole-layer switch is first, step null" % lay)
ok(all(p["state"] in ("on", "off", "off_by_parent", "off_by_env", "waiting", "nothing_to_switch", "always_on",
                      "phone") for p in r["pieces"]), "states are from the contract's list")
ok(all(k["kind"] in ("hard", "soft") for p in r["pieces"] for k in p["needs"] + p["needed_by"]), "needs kinds")
l7 = piece(r, "l7")
ok(l7["where"] == "tablet" and l7["switchable"] and l7["value"] is True and l7["state"] == "on" and l7["why"] is None,
   "l7: a tablet switch, on")
ok(set(l7["would_also_turn_off"]) == {"l7.1.flash", "l7.2", "l7.7", "l7.8", "l8"},
   "l7 would also turn off 7.1, 7.2, 7.7, 7.8 and Layer 8: %s" % l7["would_also_turn_off"])
ok({"id": "l8", "kind": "hard"} in l7["needed_by"] and {"id": "l7.2", "kind": "hard"} in l7["needed_by"],
   "l7's needed_by names its hard dependents")
for pid, step, need in (("p1.5", "1.5", "l1"), ("p2.4", "2.4", "l2"), ("p6.3.thumbs", "6.3", None)):
    p = piece(r, pid)
    ok(p["where"] == "phone" and p["switchable"] and p["value"] is None and p["state"] == "phone"
       and p["step"] == step and ([n["id"] for n in p["needs"]] == ([need] if need else [])),
       "phone piece %s listed with where phone, value null, step %s" % (pid, step))
ok({"l2", "p1.5", "p2.4"} <= set(piece(r, "l1")["would_also_turn_off"]), "l1 would also turn off l2 and the phone dial")
ok("p2.4" in piece(r, "l2")["would_also_turn_off"], "l2 would also turn off the phone's 2.4 controls")
ok(piece(r, "l1.2")["state"] == "always_on" and not piece(r, "l1.2")["switchable"], "guardrail 1.2: always on")
ok(piece(r, "l7.9")["state"] == "nothing_to_switch" and piece(r, "l7.9")["why"] == "Part of the Flash read"
   and piece(r, "l7.9")["needs"] == [{"id": "l7.1.flash", "kind": "hard"}], "7.9: part of the Flash read")
ok(piece(r, "l4.2")["needs"] == [{"id": "l4.collect", "kind": "hard"}], "rows without a switch name the one covering them")
l8 = piece(r, "l8")
ok(l8["state"] == "on" and l8["why"] == "Shadow: logs the style she would use. Her replies don't change until Step 3.",
   "l8 on, with the shadow line")
ok(l8["needs"][0] == {"id": "l7", "kind": "hard"} and {n["kind"] for n in l8["needs"][1:]} == {"soft"},
   "l8 needs Layer 7 (hard), the rest soft")
ok(piece(r, "l8.9")["state"] == "always_on" and piece(r, "l8.6")["why"].startswith("Cursing is a value"),
   "Layer 8 rows as in the addendum")
ok(piece(r, "l4.8")["where"] == "tablet" and piece(r, "l4.8")["state"] == "on", "4.8 health tool: tablet, on")
for body, code in (({"id": "l7"}, "bad_request"), ({"id": "l7", "on": "no"}, "bad_request"),
                   ({"id": "l7", "on": False, "extra": 1}, "bad_request"), ({"id": "nope", "on": False}, "unknown_id"),
                   ({"id": "l1.2", "on": False}, "not_switchable"), ({"id": "l2.2", "on": False}, "not_switchable"),
                   ({"id": "l6.8", "on": False}, "not_switchable"), ({"id": "p1.5", "on": False}, "phone_piece")):
    s_, rr = http("/app/toggles", APP, body)
    ok(s_ == 400 and rr["code"] == code and not os.path.exists(tg.path()), "%s refused: %s, nothing saved" % (body, code))
try:
    tg.set_switch("l7", False, door="app")
    refused = False
except tg.ToggleError as e:
    refused = e.code == "not_allowed"
ok(refused and not os.path.exists(tg.path()), "any door but app_button is refused, nothing saved")
ok(http("/app/toggles", APP, method="DELETE")[0] == 405, "other methods: 405, never the MCP mount")

print("2. defaults equal today")
vals = tg.values()
ok(all(vals[i] == tg.DEFAULTS[i] for i in tg.SWITCHES if i != tg.FLASH) and vals[tg.FLASH] is True,
   "no file: every switch at the code's default")
ok(all(tg.runs(i) for i in ("l1", "l2", "l4.collect", "l4.dials", "l4.8", "l5", "l6.score", "l6.words", "l7",
                            "l7.1.flash", "l7.2", "l7.7", "l7.8", "l8")), "no file: everything that runs today runs")
ok(not tg.runs("l2.8") and piece(r, "l2.8")["state"] == "off_by_env", "2.8 weekly check: off, as today (env unset)")
with open(tg.path(), "w") as f:
    f.write('{"version": 1, "choices": {"l7": fal')
s_, r = http("/app/toggles", APP)
ok(s_ == 200 and r["problems"] and "can't be read" in r["problems"][0] and piece(r, "l7")["state"] == "on",
   "half-written file: code defaults, reported in problems")
ok(tg.runs("l7"), "a broken file never switches anything off")
with open(tg.path(), "w") as f:
    json.dump({"version": 1, "choices": {"l7": "off"}}, f)
ok(tg.view()["problems"] and tg.runs("l7"), "a value that isn't on/off: defaults, reported")
with open(tg.path(), "w") as f:
    json.dump({"version": 1, "choices": {"l1.2": False}}, f)
ok(tg.view()["problems"] and tg.runs("l1"), "a guardrail in the file: ignored, reported")
clear()

print("3. only Jordan's choices are saved; atomic writes; the log")
s_, r = http("/app/toggles", APP, {"id": "l7", "on": False, "note": "quiet week"})
ok(s_ == 200 and piece(r, "l7")["value"] is False and piece(r, "l7")["state"] == "off",
   "POST answers with the full GET body after the change")
saved = json.load(open(tg.path()))
ok(saved == {"version": 1, "choices": {"l7": False}}, "toggles.json holds only the choice: %s" % saved)
ok(piece(r, "l8")["state"] == "off_by_parent" and piece(r, "l8")["why"] == "Off because Layer 7: Reading you is off",
   "Layer 8 is off because Layer 7 is (worked out, not saved)")
log = [json.loads(x) for x in open(tg.log_path())]
ok(len(log) == 1 and log[0]["who"] == "app_button" and log[0]["action"] == "set" and log[0]["note"] == "quiet week"
   and log[0]["changes"] == [{"what": "l7", "old": True, "new": False}] and isinstance(log[0]["at"], int),
   "one log line: who, what, old, new, when, note")
s_, r = http("/app/toggles", APP, {"id": "l7", "on": True})
ok(s_ == 200 and json.load(open(tg.path())) == {"version": 1, "choices": {}} and len(open(tg.log_path()).readlines()) == 2,
   "back at the default: the choice is dropped from the file, and the change logged")
# A crash between the temp write and the replace leaves the old file whole.
tg.set_switch("l7.7", False, door="app_button")
before = open(tg.path()).read()
lines_before = len(open(tg.log_path()).readlines())
_real_replace = os.replace


def crash(*a, **k):
    raise OSError("power cut")


os.replace = crash
try:
    s_, rr = http("/app/toggles", APP, {"id": "l7.8", "on": False})
finally:
    os.replace = _real_replace
ok(s_ == 500 and rr["code"] == "write_failed", "the failed write answers 500 write_failed")
ok(open(tg.path()).read() == before and json.load(open(tg.path()))["choices"] == {"l7.7": False},
   "the old toggles.json is intact")
ok(len(open(tg.log_path()).readlines()) == lines_before, "nothing logged for a change that wasn't saved")
ok(tg.runs("l7.8") and not tg.runs("l7.7"), "the switches read as before the crash")
clear()

print("4. cascades")
s_, r = http("/app/toggles", APP, {"id": "l1", "on": False})
ok(piece(r, "l2")["state"] == "off_by_parent" and piece(r, "l2")["why"] == "Off because Layer 1: Her emotion is off"
   and piece(r, "l2")["value"] is True, "l1 off forces l2 off, says why; l2's own setting is kept")
s_, rr = http("/app/toggles", APP, {"id": "l2", "on": True})
ok(s_ == 409 and rr["code"] == "parent_off" and rr["error"] == "Turn Layer 1: Her emotion on first",
   "turning l2 on while l1 is off: 409 parent_off, parent named")
s_, r = http("/app/toggles", APP, {"id": "l1", "on": True})
ok(piece(r, "l2")["state"] == "on", "l1 back on restores l2 to on (Jordan's last setting)")
http("/app/toggles", APP, {"id": "l2", "on": False})
http("/app/toggles", APP, {"id": "l1", "on": False})
s_, r = http("/app/toggles", APP, {"id": "l1", "on": True})
ok(piece(r, "l2")["state"] == "off" and piece(r, "l2")["value"] is False,
   "l1 back on restores l2 to off when that's what Jordan last set")
ok(piece(r, "l1")["would_also_turn_off"] == ["p1.5"], "with l2 off, turning l1 off would only take the phone dial")
clear()
s_, r = http("/app/toggles", APP, {"id": "l4.collect", "on": False})
ok(piece(r, "l4.dials")["state"] == "off_by_parent" and piece(r, "l4.8")["state"] == "waiting"
   and piece(r, "l4.8")["why"].startswith("Waiting:"), "l4.collect off forces the dials off; the 4.8 tool waits (soft)")
s_, r = http("/app/toggles", APP, {"id": "l5", "on": False})
ok(piece(r, "l6.score")["state"] == "waiting" and tg.runs("l6.score"),
   "l5 off leaves Layer 6 scoring on but waiting (soft): it still runs")
clear()

print("5. each switch stops its work")


def fake_read(cursor=None, window_s=None):
    return {"battery": 80.0, "memory_used": 40.0}, {"local_ok": True}, None


CALLS = {"dials": 0, "plan": 0, "trust": 0}


class FakeShadow:
    @staticmethod
    def shadow_tick(con, now):
        CALLS["dials"] += 1
        return {"share": (0.0, 0.0, 0.0)}


class FakeProgress:
    @staticmethod
    def enabled():
        return tg.env_allows("RIVIA_PLAN_SHADOW")

    @staticmethod
    def shadow_tick(con, now, body_share=(0.0, 0.0, 0.0)):
        CALLS["plan"] += 1
        con.execute("create table if not exists plan_days(day text primary key, scored_at integer, scores text, "
                    "gap real, gap_detail text, outage_info text, outages text, outage_minutes real)")
        con.execute("insert or replace into plan_days(day, scored_at, scores, gap, gap_detail, outage_info, outages, "
                    "outage_minutes) values(?,?,?,?,?,?,?,?)", ("2026-11-%02d" % (CALLS["plan"] % 28 + 1), now, "{}",
                                                                 0.1, "{}", "{}", "[]", 0))
        return {"scored": 1, "trend": {"direction": "flat"}, "mood": {"would_read": "neutral"}}


class FakeTrust:
    @staticmethod
    def enabled():
        return tg.env_allows("RIVIA_TRUST_SHADOW")

    @staticmethod
    def shadow_tick(con, now):
        CALLS["trust"] += 1
        return {"judged": 0, "trust": {"value": 0.2}, "satisfaction": {"value": None}, "killed": False}


collect.shadow, collect.progress, collect.trust_shadow = FakeShadow, FakeProgress, FakeTrust


def tick():
    for k in CALLS:
        CALLS[k] = 0
    return collect.collect_once(read=fake_read, now=int(time.time()))


out = tick()
ok(out["ok"] and out["stored"] == 2 and CALLS == {"dials": 1, "plan": 1, "trust": 1},
   "defaults: reads, dials, Step 5 and Step 6 all run, as today")
for sid, key in (("l4.dials", "dials"), ("l5", "plan"), ("l6.score", "trust")):
    tg.set_switch(sid, False, door="app_button")
    tick()
    ok(CALLS[key] == 0 and sum(CALLS.values()) == 2, "%s off: its tick work is skipped, the rest runs" % sid)
    clear()
samples0, reads0 = count("samples"), count("reads")
tg.set_switch("l4.collect", False, door="app_button")
out = tick()
ok(out["ok"] and out.get("body") == "off" and CALLS == {"dials": 0, "plan": 1, "trust": 1},
   "l4.collect off and l5 on: no body read, no dials, Step 5 still scores, Step 6 still runs")
ok(count("samples") == samples0 and count("reads") == reads0, "no body signal rows and no reads row while Layer 4 is off")
clear()

# Words copy, Step 7 and Step 8, over real HTTP.
ok(http("/app/trust", APP)[0] == 200, "the app has opened Layer 6 (sets up its tables)")
http("/app/reading", APP)
http("/app/style", APP)
n_ms = int(time.time() * 1000)


def words(mid, text="I'm so tired today, long shift", **extra):
    return http("/app/trust/words", APP, dict({"message_id": mid, "text": text, "sent_at": n_ms}, **extra))


def reading_rows():
    return count("reading_messages")


def style_rows():
    return count("style_messages")


r0, s0, f0 = reading_rows(), style_rows(), FLASH_CALLS["n"]
s_, r = words("base1")
ok(s_ == 200 and wait_for(lambda: reading_rows() == r0 + 1 and style_rows() == s0 + 1),
   "defaults: the words copy feeds Step 7 and Step 8")
ok(wait_for(lambda: FLASH_CALLS["n"] == f0 + 1), "defaults: the Flash step runs (no key here, so no Gemini call)")

tg.set_switch("l7", False, door="app_button")
r0, s0, f0 = reading_rows(), style_rows(), FLASH_CALLS["n"]
s_, r = words("off7")
time.sleep(0.6)
ok(s_ == 200 and reading_rows() == r0 and style_rows() == s0 and FLASH_CALLS["n"] == f0,
   "l7 off: Step 6 still answers 200; no reading_* row, no style_* row, no Flash call")
ok(piece(http("/app/toggles", APP)[1], "l8")["state"] == "off_by_parent", "and GET shows Layer 8 off_by_parent")
clear()

tg.set_switch("l6.words", False, door="app_button")
fb0, r0 = count("trust_feedback"), reading_rows()
s_, r = words("off6w", "thanks, perfect, that was great")
ok(s_ == 200 and r["ok"] and r["counted"] is False and "switched off" in r["note"], "l6.words off: 200, nothing counted")
ok(wait_for(lambda: reading_rows() == r0 + 1) and count("trust_feedback") == fb0,
   "l6.words off and l7 on: Step 7 still reads the copy; no Step 6 signal saved")
clear()

tg.set_switch("l8", False, door="app_button")
r0, s0 = reading_rows(), style_rows()
words("off8")
ok(wait_for(lambda: reading_rows() == r0 + 1) and (time.sleep(0.4) or style_rows() == s0),
   "l8 off: reading_* rows written, no style_* rows")
s_, r = http("/app/style", APP)
ok(s_ == 200 and r["note"].startswith("Layer 8 is switched off"), "/app/style keeps answering, with a note")
clear()

tg.set_switch("l7.2", False, door="app_button")
CAL["n"] = 0
c = normal.db_open()
try:
    ok(rd.situation(c, time.time() - 50 * 86400, time.time()) == "other" and CAL["n"] == 0,
       "l7.2 off: no calendar read, the message counts as 'other'")
finally:
    c.close()
clear()
CAL["n"] = 0
c = normal.db_open()
try:
    rd.situation(c, time.time() - 40 * 86400, time.time())   # a day Step 7 hasn't read (it keeps each day's shifts)
    ok(CAL["n"] >= 1, "l7.2 on: the calendar is read (faked here)")
finally:
    c.close()

tg.set_switch("l7.7", False, door="app_button")
c = normal.db_open()
try:
    p0 = c.execute("select count(*) from reading_patterns").fetchone()[0]
    ok(rd.find_patterns(c) == [] and c.execute("select count(*) from reading_patterns").fetchone()[0] == p0,
       "l7.7 off: no pattern guesses worked out or saved")
finally:
    c.close()
clear()

tg.set_switch("l7.8", False, door="app_button")
l0, r0 = count("reading_log"), reading_rows()
words("off78", "UGH whatever")
ok(wait_for(lambda: reading_rows() == r0 + 1) and (time.sleep(0.3) or count("reading_log") == l0),
   "l7.8 off: the message is read, nothing judged, no would-be question logged")
clear()

# Layer 1 and Layer 2 switches on the emotion button.
s_, r = http("/app/emotion", APP)
ok(s_ == 200 and "note" not in r, "defaults: /app/emotion answers exactly as before (no note)")
tg.set_switch("l1", False, door="app_button")
s_, r = http("/app/emotion", APP, {"nudge": {"dv": 0.1}})
ok(s_ == 409 and r["code"] == "layer_off" and r["error"] == "Layer 1 is off", "l1 off: the dial's write refused")
s_, r = http("/app/emotion", APP)
ok(s_ == 200 and r["ok"] and r["note"].startswith("Layer 1 is off"), "l1 off: GET still answers and says off")
clear()
tg.set_switch("l2", False, door="app_button")
ok(http("/app/emotion", APP, {"unlock": True})[0] == 200, "l2 off: unlock still answers (not a write)")
s_, r = http("/app/emotion", APP, {"nudge": {"dv": 0.1}, "layer": "mood", "tag": "test"})
ok(s_ == 409 and r["error"] == "Layer 2 is off", "l2 off: a mood write refused")
s_, r = http("/app/emotion", APP, {"nudge": {"dv": 0.05}})
ok(s_ == 200 and r["ok"], "l2 off: Layer 1's own emotion dial still works")
clear()

# 2.8: the switch and RIVIA_DRIFT_WEEKLY must both allow it.
os.environ["RIVIA_DRIFT_WEEKLY"] = "1"
ok(not tg.runs("l2.8") and piece(tg.view(), "l2.8")["state"] == "off", "env 1, switch at its default (off): no run")
tg.set_switch("l2.8", True, door="app_button")
ok(tg.runs("l2.8"), "env 1 and the switch on: runs")
os.environ.pop("RIVIA_DRIFT_WEEKLY")
ok(not tg.runs("l2.8") and piece(tg.view(), "l2.8")["state"] == "off_by_env", "env unset: off by env, whatever the switch")
s_, r = http("/app/toggles", APP, {"id": "l2.8", "on": True})
ok(s_ == 409 and r["code"] == "env_off" and "RIVIA_DRIFT_WEEKLY" in r["error"], "turning it on: 409 env_off")
clear()
src = open(os.path.join(HERE, "rivia_gcal.py")).read()
ok('toggles.runs("l2.8")' in src and "drift_weekly.enabled() and toggles.runs" in src, "the drift block checks both")

print("6. the minute loop is split from Layer 4")
os.environ["RIVIA_BODY_COLLECTOR"] = "0"
ok(collect.loop_enabled(), "RIVIA_BODY_COLLECTOR=0 with Steps 5 and 6 allowed: the loop still starts")
s_, r = http("/app/toggles", APP)
ok(piece(r, "l4.collect")["state"] == "off_by_env" and piece(r, "l4.collect")["why"].startswith("Off by env flag "
   "RIVIA_BODY_COLLECTOR") and piece(r, "l4.dials")["state"] == "off_by_parent" and piece(r, "l5")["state"] == "on",
   "GET: Layer 4 off by env, the dials off by parent, Layer 5 on")
s_, r = http("/app/toggles", APP, {"id": "l4.collect", "on": True})
ok(s_ == 409 and r["code"] == "env_off", "turning Layer 4 on while its env flag is off: 409 env_off")
ticks = {"n": 0}
days0, samples0 = count("plan_days"), count("samples")


async def one_tick_loop():
    calls = {"n": 0}

    async def sleep(_s):
        calls["n"] += 1
        if calls["n"] > 1:
            raise asyncio.CancelledError

    def t():
        ticks["n"] += 1
        return collect.collect_once(read=fake_read, now=int(time.time()))
    try:
        await collect.loop(sleep=sleep, tick=t, first_delay=0, log=lambda m: None)
    except asyncio.CancelledError:
        pass


asyncio.run(one_tick_loop())
ok(ticks["n"] == 1 and count("plan_days") == days0 + 1, "a tick still writes a plan_days row")
ok(count("samples") == samples0, "and no body signal rows")


async def started():
    import rivia_server
    task = rivia_server.start_body_collector()
    ok(task is not None, "the server starts the loop with RIVIA_BODY_COLLECTOR=0")
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


asyncio.run(started())
os.environ.update(RIVIA_PLAN_SHADOW="0", RIVIA_TRUST_SHADOW="0")
ok(not collect.loop_enabled(), "with Layer 4, Step 5 and Step 6 all off by env, the loop doesn't start")
for k in ("RIVIA_BODY_COLLECTOR", "RIVIA_PLAN_SHADOW", "RIVIA_TRUST_SHADOW"):
    os.environ.pop(k)
ok(collect.loop_enabled(), "env flags back to unset: the loop starts as today")

print("7. the Flash switch writes through to Step 7's setting")
s_, r = http("/app/toggles", APP, {"id": "l7.1.flash", "on": False})
ok(s_ == 200 and piece(r, "l7.1.flash")["value"] is False and piece(r, "l7.1.flash")["state"] == "off",
   "7.1 off from the switches")
ok(rset.effective()["values"]["facts.flash_on"] == 0, "Step 7's facts.flash_on is 0: one value, not two")
ok(not os.path.exists(tg.path()) or "l7.1.flash" not in json.load(open(tg.path()))["choices"],
   "the Flash value isn't copied into toggles.json")
ok(piece(r, "l8")["state"] == "waiting", "Layer 8 waits (soft) while the Flash read is off")
rset.save({"facts.flash_on": 1}, door="app_button")
ok(piece(http("/app/toggles", APP)[1], "l7.1.flash")["value"] is True, "set back from Step 7's card: the switch follows")
clear()

print("8. env flags and never a tool")
import rivia_body_shadow as real_shadow  # noqa: E402
import rivia_drift_weekly as real_drift  # noqa: E402
import rivia_progress as real_progress  # noqa: E402
import rivia_trust_shadow as real_trust  # noqa: E402
CHECKS = (("RIVIA_BODY_COLLECTOR", collect.enabled), ("RIVIA_BODY_SHADOW", collect.shadow_enabled),
          ("RIVIA_PLAN_SHADOW", real_progress.enabled), ("RIVIA_TRUST_SHADOW", real_trust.enabled),
          ("RIVIA_DRIFT_WEEKLY", real_drift.enabled))
for name, fn in CHECKS:
    agree = True
    for v in (None, "1", "0", "false", "off", "no", "yes", " 0 ", "OFF"):
        if v is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = v
        agree = agree and fn() == tg.env_allows(name)
    os.environ.pop(name, None)
    ok(agree, "%s reads the same in the switches as in its own module" % name)
ok(real_shadow is not None, "the dials module still loads")
import ast  # noqa: E402
for f in ("rivia_toggles.py", "rivia_toggles_app.py"):
    tree = ast.parse(open(os.path.join(HERE, f)).read())
    mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
        {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    ok("tool" not in calls and not {m for m in mods if m.startswith(("fastmcp", "mcp", "rivia_tools"))}
       and "rivia_emotion" not in mods and ("rivia_" + "inject") not in str(mods),   # split: test_inject scans for the name
       "%s registers no tool" % f)
top = {a.name for n in ast.parse(open(os.path.join(HERE, "rivia_toggles.py")).read()).body
       if isinstance(n, ast.Import) for a in n.names}
ok(top <= {"json", "os", "time"}, "rivia_toggles imports only the standard library at load time: %s" % sorted(top))
src = open(os.path.join(HERE, "rivia_server.py")).read()
ok("+ toggles_routes()" in src and "rivia_toggles_app.routes()" in src and "app_mcp.tool" not in src,
   "rivia_server hands the switches to build_app as a route, not to app_mcp")
ok(http("/app/mcp", APP)[1] == "mcp mount", "the /app MCP mount still answers beside it")

server.should_exit = True
print("\n%d checks passed" % COUNT["n"])
