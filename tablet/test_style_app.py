"""Emotion spec Step 8.11: the app's Layer 8 door (/app/style), over real HTTP through the bearer gate.
  1. GET: the card's data, even with nothing worked out yet (says why)
  2. your words copy (Step 6's /app/trust/words) feeds Step 7 then Step 8: counts only, text dropped; Step 6's
     answer is unchanged
  3. settings: set and reset; locked names refused; cursing can only be turned on from here
  4. app token only (no token 401, admin token 403); every method lands here; the MCP mount still works
  5. never a tool: no tool module imports Step 8, it registers nothing, rivia_server hands it over as routes
    python test_style_app.py
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
import rivia_style as st  # noqa: E402
import rivia_style_app as sapp  # noqa: E402
import rivia_style_settings as sset  # noqa: E402
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


# Her state stands in for emotion.db (a throwaway HOME has none): calm, at her baseline.
st.her_state = lambda: {"name": "neutral", "intensity": "neutral", "a": 0.0, "rest_a": 0.0, "irritation": 0.0}
rd.CALENDAR = lambda a, b: []
server, BASE = serve(ra.build_app(app_mcp=standin_mcp(), admin_mcp=standin_mcp(),
                                  extra_routes=tapp.routes() + rapp.routes() + sapp.routes()))
APP = ra.mint_door_token("app", 1)["token"]
ADMIN = ra.mint_door_token("admin", 1)["token"]


def stored():
    c = normal.db_open()
    try:
        st.prepare(c)
        return c.execute("select * from style_messages").fetchall(), \
            c.execute("select row from style_log where kind = 'line'").fetchall()
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
st_, r = http("/app/style", APP)
ok(st_ == 200 and r["ok"] and r["shadow"] and r["note"].startswith("nothing worked out yet"), "nothing yet: says why")
ok([p["piece"] for p in r["layers"][0]["pieces"]] == ["8.1", "8.2", "8.2b", "8.3", "8.4", "8.6", "8.7", "8.8"]
   and r["layers"][0]["layer"] == 8, "a small card of values per piece")
ok(set(r) >= {"layers", "notes", "locked", "history", "changed", "problems", "fixed_rules", "waits_on"},
   "values in /app/reading's shape, plus the fixed rules and what it waits on")

print("2. your words copy feeds Step 7 then Step 8")
ok(http("/app/trust", APP)[0] == 200, "the app has opened Layer 6 (sets up its tables)")
now_ms = int(time.time() * 1000)
st_, r = http("/app/trust/words", APP, {"message_id": "w1", "text": "UGH THIS DAMN BUS, I'm so stressed secretword",
                                        "sent_at": now_ms, "reply_at": now_ms - 90000})
ok(st_ == 200 and r["ok"] and "counted" in r, "Step 6 answers as before (nothing to count in this one)")
ok(wait_for(lambda: len(stored()[0]) == 1), "Step 8 worked it out in the background")
rows, lines = stored()
dump = str(rows) + str(lines)
ok(rows[0][4] == 1 and rows[0][5] == 4, "counts kept: 1 swear, 4 caps words")
ok("secretword" not in dump and "BUS" not in dump and "damn" not in dump, "your text isn't stored")
line = json.loads(lines[0][0])
ok(line["him"]["worked_up"] and "you said" in line["him"]["why"] and "no cursing" in line["line"],
   "you said you're stressed: worked up, she holds steady, no cursing")
st_, r = http("/app/style", APP)
ok(r["latest"]["style"] == "plain" and r["messages_14d"]["worked_up"] == 1 and r["energy"]["phase"] == "easing",
   "the card shows her would-be style and the steady-then-calmer plan")

print("3. settings")
st_, r = http("/app/style", APP, {"set": {"banter.on": 1}, "note": "trying banter"})
ok(st_ == 200 and r["edited"][0]["new"] == 1 and r["changed"], "you turn cursing on from the app")
st_, r = http("/app/style", APP, {"set": {"pushback": 0}})
ok(st_ == 400 and r["code"] == "locked", "pushback is locked")
st_, r = http("/app/style", APP, {"set": {"match_energy": 1}})
ok(st_ == 400 and r["code"] == "locked", "matching your energy is locked")
st_, r = http("/app/style", APP, {"reset": "all"})
ok(st_ == 200 and not os.path.exists(sset.path()), "reset all")
ok(http("/app/style", APP, {"set": {"banter.on": 1}, "door": "rivia"})[0] == 400, "you can't send a door")
ok(http("/app/style", APP, raw=b"x" * 9000, method="POST")[0] == 413, "too big: 413")

print("4. the gate")
ok(http("/app/style")[0] == 401 and http("/app/style", ADMIN)[0] == 403, "no token 401, admin token 403")
ok(http("/app/style", APP, method="DELETE")[0] == 405, "DELETE: 405")
ok(http("/app/mcp", APP, {"x": 1}) == (200, "mcp mount"), "/app/mcp still reaches the MCP mount")

print("5. never a tool")


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
STEP8 = {"rivia_style", "rivia_style_app", "rivia_style_settings"}
present = [m for m in sorted(TOOL_MODULES) if os.path.exists(os.path.join(HERE, m + ".py"))]
leaks = [m for m in present if imports(os.path.join(HERE, m + ".py"))[0] & STEP8]
ok(len(present) == len(TOOL_MODULES) and not leaks, "no tool module imports Step 8 (%d checked)" % len(present))
for m in sorted(STEP8):
    mods, tr_ = imports(os.path.join(HERE, m + ".py"))
    calls = {n.func.attr for n in ast.walk(tr_) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    ok("tool" not in calls and not {x for x in mods if x.startswith(("fastmcp", "mcp", "rivia_tools"))},
       "%s registers no tool and doesn't touch the tool table" % m)
src = open(os.path.join(HERE, "rivia_server.py")).read()
ok("+ style_routes()" in src and "rivia_style_app.routes()" in src, "rivia_server hands it over as routes")
ok("rivia_" + "inject" not in str(imports(os.path.join(HERE, "rivia_style.py"))[0]),   # split: test_inject scans for the name
   "Step 8 doesn't touch what Gemini is told (the injection file is unchanged)")

server.should_exit = True
print("\n%d checks passed" % COUNT["n"])
