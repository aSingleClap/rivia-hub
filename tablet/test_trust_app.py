"""Emotion spec Step 6.9: the app's Layer 6 door (/app/trust), over real HTTP through the bearer gate.
  1. GET: the card's data, even with nothing judged yet (says why)
  2. thumbs: one per message (a new press replaces the old), a thumbs down names an area, refused input saves nothing
  2b. your words: tablet code reads them; your text isn't kept; an edit replaces; a correction's area from her reply
  3. unlocks: you switch them; hard limits refused with 403; switching changes nothing she does in shadow
  4. the off switch from the app
  5. settings: set and reset by piece; locked names refused
  6. app token only (no token 401, admin token 403); every method lands here; the MCP mount still works
  7. never a tool: no tool module imports it, it registers nothing, rivia_server hands it over as routes
    python test_trust_app.py
"""
import ast, json, os, secrets, socket, sys, tempfile, threading, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
DOT = os.path.join(TMP, "dot")
os.makedirs(DOT, mode=0o700)
os.environ.update(RIVIA_DIR=os.path.join(TMP, "rivia"), RIVIA_HOME=os.path.join(TMP, "rivia"), RIVIA_DOTDIR=DOT,
                  RIVIA_DB=os.path.join(TMP, "auth.db"), HOME=TMP, RIVIA_JWT_KEY=secrets.token_hex(32),
                  RIVIA_SIGNIN_CHECK="0", RIVIA_GOOGLE_TOKEN_PATH=os.path.join(DOT, "google_token.json"))
os.environ.pop("RIVIA_TRUST_KILL", None)
sys.path.insert(0, HERE)

import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
import rivia_auth as ra  # noqa: E402
import rivia_normal as normal  # noqa: E402
import rivia_satisfaction as sat  # noqa: E402
import rivia_trust as trust  # noqa: E402
import rivia_trust_app as tapp  # noqa: E402
import rivia_trust_settings as tset  # noqa: E402

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


def http(p, token=None, body=None, method=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
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


server, BASE = serve(ra.build_app(app_mcp=standin_mcp(), admin_mcp=standin_mcp(), extra_routes=tapp.routes()))
APP = ra.mint_door_token("app", 1)["token"]
ADMIN = ra.mint_door_token("admin", 1)["token"]


def thumbs():
    c = normal.db_open()
    try:
        return [r for r in sat.rows(c) if r["source"] == "thumb"]
    finally:
        c.close()


print("1. GET")
st, r = http("/app/trust", APP)
ok(st == 200 and r["ok"] and r["shadow"] and r["state"] is None and "parked" in r["note"],
   "nothing judged yet: an empty card that says why")
ok(set(r["unlocks"]) == set(trust.UNLOCKS) and all(u["state"] == "earning" for u in r["unlocks"].values()),
   "each unlock listed, earning")
ok(set(r["hard_limits"]) == set(trust.HARD_LIMITS) and r["locked"], "hard limits shown read-only")
ok([p["piece"] for p in r["layers"][0]["pieces"]] == ["6.2", "6.3", "6.4", "6.5", "6.6", "6.7"]
   and r["layers"][0]["layer"] == 6, "a small card of values per piece")
ok(set(r) >= {"layers", "notes", "locked", "history", "changed", "problems"} and isinstance(r["locked"], list),
   "values in /app/progress's shape, so the app's small card reads them unchanged")

print("2. thumbs")
st, r = http("/app/trust/feedback", APP, {"value": -1, "message_id": "m1",
                                          "text": "The ride from the farm to home is 5 minutes"})
ok(st == 200 and r["saved"]["area"] == "ride:farm|home", "a thumbs down names the area from her words")
st, r = http("/app/trust/feedback", APP, {"value": 1, "message_id": "m1", "text": "same message"})
ok(st == 200 and len(thumbs()) == 1 and thumbs()[0]["value"] == 1.0, "changed your mind: replaced, not added")
st, r = http("/app/trust/feedback", APP, {"value": 3, "message_id": "m1"})
ok(st == 400 and r["code"] == "bad_value" and thumbs()[0]["value"] == 1.0, "a bad press saves nothing, keeps yours")
ok(http("/app/trust/feedback", APP, {"value": 1})[0] == 400, "a thumb needs the message id")
ok(http("/app/trust/feedback", APP, {"value": 1, "message_id": "m2", "door": "judge"})[0] == 400,
   "you can't send a door (or a quiet day) from the app")
ok(http("/app/trust/feedback", APP, method="GET")[0] == 405, "GET on a write door: 405")

print("2b. your words")


def words():
    c = normal.db_open()
    try:
        return [r for r in sat.rows(c) if r["source"] == "words"]
    finally:
        c.close()


st, r = http("/app/trust/words", APP, {"message_id": "u1", "text": "Thanks, what's for dinner"})
ok(st == 200 and not r["counted"] and words() == [], "nothing heard: nothing saved")
st, r = http("/app/trust/words", APP, {"message_id": "u2", "text": "That's wrong, I left on time",
                                       "reply_text": "The ride from the farm to home is 5 minutes"})
ok(st == 200 and r["counted"] and r["saved"]["value"] == -1.0 and r["saved"]["area"] == "ride:farm|home",
   "a correction, with the area taken from her reply")
_c = normal.db_open()
_kept = _c.execute("select text from trust_feedback where source = 'words'").fetchall()
_c.close()
ok(_kept == [(None,)] and words()[0]["note"] == "heard: that's wrong", "your message isn't kept, only what was heard")
st, r = http("/app/trust/words", APP, {"message_id": "u2", "text": "Good plan actually"})
ok(st == 200 and len(words()) == 1 and words()[0]["value"] == 1.0, "you edited it: the new reading replaces the old")
st, r = http("/app/trust/words", APP, {"message_id": "u2", "text": "never mind"})
ok(st == 200 and words() == [], "an edit that took it out takes the old signal off")
ok(http("/app/trust/words", APP, {"message_id": "u3", "text": "good plan", "value": 1})[0] == 400,
   "the app can't send a verdict, only your text: the tablet decides")
ok(http("/app/trust/words", APP, {"text": "good plan"})[0] == 400, "needs the message id")
ok(http("/app/trust/words", APP, {"message_id": "u4", "text": "x" * 5000})[0] in (400, 413), "too long refused")
ok(http("/app/trust/words")[0] == 401 and http("/app/trust/words", ADMIN, {"message_id": "u5", "text": "good plan"})[0]
   == 403, "app token only")

print("3. unlocks")
st, r = http("/app/trust/unlock", APP, {"unlock": "spend_money", "on": True})
ok(st == 403 and r["code"] == "hard_limit", "a hard limit: 403, even from your app")
st, r = http("/app/trust/unlock", APP, {"unlock": "speak_up", "on": True, "note": "trying it"})
ok(st == 200 and r["switched_on"] == ["speak_up"] and "changes nothing" in r["note"],
   "speak_up on, and it says so plainly: nothing reads it in shadow")
ok(http("/app/trust", APP)[1]["unlocks"]["speak_up"]["switched_on"], "the card shows it on")

print("4. off switch")
st, r = http("/app/trust/kill", APP, {"kill": True, "note": "testing"})
ok(st == 200 and r["kill"]["killed"], "switched off from the app")
st, r = http("/app/trust/kill", APP, {"kill": False})
ok(st == 200 and not r["kill"]["killed"], "and back")
ok(http("/app/trust/kill", APP, {"kill": "yes"})[0] == 400, "kill must be true or false")
acts = http("/app/trust", APP)[1]["actions"]
ok({"killed", "unkilled", "unlock_on"} <= {a["kind"] for a in acts}, "each is in the action log on the card")

print("5. settings")
st, r = http("/app/trust", APP, {"set": {"sat.quiet_weight": 0.3}, "note": "less"})
ok(st == 200 and r["edited"][0]["new"] == 0.3 and r["changed"] and r["history"][0]["note"] == "less",
   "a value saved; the reply carries the new values and the log")
st, r = http("/app/trust", APP, {"set": {"report_only": 1}})
ok(st == 400 and r["code"] == "locked", "a locked name refused")
st, r = http("/app/trust", APP, {"reset": "piece:6.3"})
ok(st == 200 and not os.path.exists(tset.path()), "reset by piece")
ok(http("/app/trust", APP, raw=b"x" * 9000, method="POST")[0] == 413, "too big: 413")
ok(http("/app/trust", APP, {"set": {}, "reset": "all"})[0] == 400, "both at once: 400")

print("6. the gate")
for p in ("/app/trust", "/app/trust/feedback", "/app/trust/words", "/app/trust/unlock", "/app/trust/kill"):
    ok(http(p)[0] == 401 and http(p, ADMIN)[0] == 403, "%s: no token 401, admin token 403" % p)
ok(http("/app/trust", APP, method="DELETE")[0] == 405, "DELETE: 405")
ok(http("/app/mcp", APP, {"x": 1}) == (200, "mcp mount"), "/app/mcp still reaches the MCP mount")


print("7. never a tool")


def imports(path):
    tr_ = ast.parse(open(path).read())
    out = set()
    for n in ast.walk(tr_):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out, tr_


TOOL_MODULES = {"rivia_tools", "rivia_admin", "rivia_battery", "rivia_places", "rivia_sched", "rivia_settings",
                "rivia_approvals", "rivia_planner", "rivia_gcal", "rivia_google", "rivia_store"}
STEP6 = {"rivia_trust", "rivia_trust_app", "rivia_trust_settings", "rivia_trust_shadow", "rivia_predict",
         "rivia_satisfaction", "rivia_area_confidence"}
present = [m for m in sorted(TOOL_MODULES) if os.path.exists(os.path.join(HERE, m + ".py"))]
leaks = [m for m in present if imports(os.path.join(HERE, m + ".py"))[0] & STEP6]
ok(len(present) == len(TOOL_MODULES) and not leaks, "no tool module imports Step 6 (%d checked)" % len(present))
for m in sorted(STEP6):
    mods, tr_ = imports(os.path.join(HERE, m + ".py"))
    calls = {n.func.attr for n in ast.walk(tr_) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    ok("tool" not in calls and not {x for x in mods if x.startswith(("fastmcp", "mcp", "rivia_tools"))},
       "%s registers no tool and doesn't touch the tool table" % m)
src = open(os.path.join(HERE, "rivia_server.py")).read()
ok("+ trust_routes()" in src and "rivia_trust_app.routes()" in src, "rivia_server hands it over as routes")

server.should_exit = True
print("\n%d checks passed" % COUNT["n"])
