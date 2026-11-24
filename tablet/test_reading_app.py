"""Emotion spec Step 7.11: the app's Layer 7 door (/app/reading), over real HTTP through the bearer gate.
  1. GET: the card's data, even with nothing read yet (says why); the left-out pieces are named
  2. your words copy (Step 6's /app/trust/words) feeds Step 7 in the background: numbers only, text dropped;
     Step 6's answer is unchanged; sent_at and reply_at accepted; no key means no Flash call
  3. patterns: you confirm or reject from the app; bad input refused
  4. settings: set and reset; locked names refused
  5. app token only (no token 401, admin token 403); every method lands here; the MCP mount still works
  6. never a tool: no tool module imports Step 7, it registers nothing, rivia_server hands it over as routes
    python test_reading_app.py
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
os.environ.pop("GEMINI_API_KEY", None)
os.environ["RIVIA_NRC_LEXICON"] = os.path.join(TMP, "none.txt")
sys.path.insert(0, HERE)

import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
import rivia_auth as ra  # noqa: E402
import rivia_normal as normal  # noqa: E402
import rivia_reading as rd  # noqa: E402
import rivia_reading_app as rapp  # noqa: E402
import rivia_reading_settings as rset  # noqa: E402
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


rd.CALENDAR = lambda a, b: []
server, BASE = serve(ra.build_app(app_mcp=standin_mcp(), admin_mcp=standin_mcp(),
                                  extra_routes=tapp.routes() + rapp.routes()))
APP = ra.mint_door_token("app", 1)["token"]
ADMIN = ra.mint_door_token("admin", 1)["token"]


def stored():
    c = normal.db_open()
    try:
        rd.prepare(c)
        return c.execute("select * from reading_messages").fetchall()
    finally:
        c.close()


def wait_for(fn, s=5.0):
    t0 = time.time()
    while time.time() - t0 < s:
        if fn():
            return True
        time.sleep(0.05)
    return False


print("1. GET")
st, r = http("/app/reading", APP)
ok(st == 200 and r["ok"] and r["shadow"] and "none have arrived" in r["note"], "nothing read yet: says why")
ok([s["id"] for s in r["situations"]] == ["before_shift", "after_shift", "day_off", "other"], "four situations")
ok(any("isn't on the tablet" in p for p in r["problems"]), "no NRC word list: said plainly")
ok([p["piece"] for p in r["layers"][0]["pieces"]] == ["7.1", "7.2", "7.3", "7.4", "7.7", "7.8"]
   and r["layers"][0]["layer"] == 7, "a small card of values per piece")
ok(set(r) >= {"layers", "notes", "locked", "history", "changed", "problems"}, "values in /app/progress's shape")
ok(len(r["left_out"]) == 3 and r["wording_rules"], "left-out pieces and wording rules shown")

print("2. your words copy feeds Step 7")
now_ms = int(time.time() * 1000)
st, r = http("/app/trust/words", APP, {"message_id": "w1", "text": "Good plan, thanks", "sent_at": now_ms,
                                       "reply_at": now_ms - 90000})
ok(st == 200 and r["counted"] and r["saved"]["value"] == 1.0, "Step 6's answer is unchanged (praise heard)")
ok(wait_for(lambda: len(stored()) == 1), "Step 7 read it in the background")


def flash_settled():
    # The Flash step fills flash_status a moment after the row appears (same background task): wait for it, so the
    # check below isn't a race (it failed about half the time when read straight away).
    c_ = normal.db_open()
    try:
        return rd.message(c_, "w1")["flash_status"] != "waiting"
    finally:
        c_.close()


wait_for(flash_settled)
c = normal.db_open()
m = rd.message(c, "w1")
dump = str(c.execute("select * from reading_messages").fetchall())
c.close()
ok(m["words"] == 3 and m["gap_min"] == 1.5 and m["situation"] == "day_off", "numbers kept: 3 words, 1.5 min")
ok("Good plan" not in dump and "thanks" not in dump, "your text isn't stored")
ok(m["flash_status"] == "no_key", "no Gemini key on the tablet: no Flash call, said why")
st, r = http("/app/trust/words", APP, {"message_id": "w2", "text": "ok", "sent_at": "soon"})
ok(st == 200 and wait_for(lambda: len(stored()) == 2), "a bad sent_at is ignored, the tablet's clock is used")
ok(http("/app/trust/words", APP, {"message_id": "w3", "text": "ok", "mood": "bad"})[0] == 400,
   "the app can't send a mood or verdict")

print("3. patterns")
c = normal.db_open()
rd.prepare(c)
c.execute("insert into reading_patterns(situation, measure, direction, state, words, size, days_seen, found_at, "
          "updated_at) values(?,?,?,?,?,?,?,?,?)", ("after_shift", "words", "down", "proposed",
                                                    "After HMC shifts you're writing shorter messages.", -5, 12, 0, 0))
pid = c.execute("select id from reading_patterns").fetchone()[0]
c.close()
st, r = http("/app/reading/pattern", APP, {"id": pid, "confirm": True, "note": "yep"})
ok(st == 200 and r["pattern"]["state"] == "confirmed" and "changes nothing" in r["note"], "you confirm a guess")
ok(http("/app/reading", APP)[1]["patterns"][0]["state"] == "confirmed", "the card shows it confirmed")
st, r = http("/app/reading/pattern", APP, {"id": pid, "confirm": False})
ok(st == 200 and r["pattern"]["state"] == "rejected", "and can change your mind")
ok(http("/app/reading/pattern", APP, {"id": 999, "confirm": True})[0] == 404, "unknown pattern: 404")
ok(http("/app/reading/pattern", APP, {"id": pid, "confirm": "yes"})[0] == 400, "confirm must be true or false")
ok(http("/app/reading/pattern", APP, {"id": pid, "confirm": True, "door": "rivia"})[0] == 400,
   "you can't send a door")
ok(http("/app/reading/pattern", APP, method="GET")[0] == 405, "GET on a write door: 405")

print("4. settings")
st, r = http("/app/reading", APP, {"set": {"ask.per_day": 1}, "note": "fewer"})
ok(st == 200 and r["edited"][0]["new"] == 1 and r["changed"], "a value saved")
st, r = http("/app/reading", APP, {"set": {"no_fear": 1}})
ok(st == 400 and r["code"] == "locked", "a locked name refused")
st, r = http("/app/reading", APP, {"reset": "all"})
ok(st == 200 and not os.path.exists(rset.path()), "reset all")
ok(http("/app/reading", APP, raw=b"x" * 9000, method="POST")[0] == 413, "too big: 413")

print("5. the gate")
for p in ("/app/reading", "/app/reading/pattern"):
    ok(http(p)[0] == 401 and http(p, ADMIN)[0] == 403, "%s: no token 401, admin token 403" % p)
ok(http("/app/reading", APP, method="DELETE")[0] == 405, "DELETE: 405")
ok(http("/app/mcp", APP, {"x": 1}) == (200, "mcp mount"), "/app/mcp still reaches the MCP mount")

print("6. never a tool")


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
STEP7 = {"rivia_reading", "rivia_reading_app", "rivia_reading_settings", "rivia_reading_flash"}
present = [m for m in sorted(TOOL_MODULES) if os.path.exists(os.path.join(HERE, m + ".py"))]
leaks = [m for m in present if imports(os.path.join(HERE, m + ".py"))[0] & STEP7]
ok(len(present) == len(TOOL_MODULES) and not leaks, "no tool module imports Step 7 (%d checked)" % len(present))
for m in sorted(STEP7):
    mods, tr_ = imports(os.path.join(HERE, m + ".py"))
    calls = {n.func.attr for n in ast.walk(tr_) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    ok("tool" not in calls and not {x for x in mods if x.startswith(("fastmcp", "mcp", "rivia_tools"))},
       "%s registers no tool and doesn't touch the tool table" % m)
src = open(os.path.join(HERE, "rivia_server.py")).read()
ok("+ reading_routes())" in src and "rivia_reading_app.routes()" in src, "rivia_server hands it over as routes")

server.should_exit = True
print("\n%d checks passed" % COUNT["n"])
