"""Emotion spec Step 3.9: the app's Layer 3 door (/app/appraisal) and her reply copy (/app/appraisal/reply), over real
HTTP through the bearer gate.
  1. GET: the card's data, even with nothing yet (says why); rewording numbers; what it waits on
  2. her reply copy: answered at once, kept, the shadow rewording runs in the background; bad copies refused
  3. your words copy (Step 6) still feeds Steps 7 and 8, and Step 3 notes why it had no message events (no key)
  4. settings: set and reset; locked names refused
  5. app token only (no token 401, admin token 403); every method lands here; the MCP mount still works
  6. never a tool: no tool module imports Step 3, it registers nothing, rivia_server hands it over as routes
    python test_appraisal_app.py
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
import rivia_appraisal as ap  # noqa: E402
import rivia_appraisal_app as aapp  # noqa: E402
import rivia_appraisal_settings as aset  # noqa: E402
import rivia_expression as ex  # noqa: E402
import rivia_reading as rd  # noqa: E402
import rivia_reading_app as rapp  # noqa: E402
import rivia_style as st  # noqa: E402
import rivia_style_app as sapp  # noqa: E402
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


st.her_state = lambda: {"name": "neutral", "intensity": "neutral", "a": 0.0, "rest_a": 0.0, "irritation": 0.0}
rd.CALENDAR = lambda a, b: []
server, BASE = serve(ra.build_app(app_mcp=standin_mcp(), admin_mcp=standin_mcp(),
                                  extra_routes=tapp.routes() + rapp.routes() + sapp.routes() + aapp.routes()))
APP = ra.mint_door_token("app", 1)["token"]
ADMIN = ra.mint_door_token("admin", 1)["token"]


def db():
    c = normal.db_open()
    ex.prepare(c)
    return c


def wait_for(fn, s=5.0):
    t0 = time.time()
    while time.time() - t0 < s:
        if fn():
            return True
        time.sleep(0.05)
    return False


LONG = ("I checked the gym list again and the closer one is open later on weekends, so that should be easier "
        "for you after the barn.")

print("1. GET")
st_, r = http("/app/appraisal", APP)
ok(st_ == 200 and r["ok"] and r["shadow"] and r["layer"] == 3 and r["note"].startswith("no events yet"),
   "nothing yet: says why")
ok([p["piece"] for p in r["layers"][0]["pieces"]] == ["3.1", "3.2", "3.2s", "3.3", "3.6", "3.7"],
   "a small card of values per piece")
ok(r["rewording"]["shadow"] and r["rewording"]["note"].startswith("no replies yet"), "the rewording part, empty")
ok(set(r) >= {"layers", "notes", "locked", "history", "waits_on", "events", "would_be", "real_mood", "caution"},
   "values in /app/style's shape, plus the mood, events, caution and what it waits on")

print("2. her reply copy")
now_ms = int(time.time() * 1000)
st_, r = http("/app/appraisal/reply", APP, {"message_id": "h1", "text": LONG, "at": now_ms, "tool_only": False})
ok(st_ == 200 and r["ok"] and r["kept"] and r["outcome"] == "waiting", "answered at once, kept")


def outcome(mid):
    c = db()
    try:
        r_ = ex.row(c, mid)
        return r_ and r_["outcome"]
    finally:
        c.close()


ok(wait_for(lambda: outcome("h1") == "no_key"), "the shadow rewording ran in the background (no key here: said so)")
st_, r = http("/app/appraisal/reply", APP, {"message_id": "h2", "text": "Leave by 6:15 for the gym.", "at": now_ms,
                                             "tool_only": False})
ok(st_ == 200 and r["outcome"] == "skipped_short", "a short one is kept and left alone")
ok(http("/app/appraisal/reply", APP, {"message_id": "h3", "text": LONG, "extra": 1})[0] == 400, "extra fields: 400")
ok(http("/app/appraisal/reply", APP, {"message_id": "h3", "text": ""})[0] == 400, "no text: 400")
ok(http("/app/appraisal/reply", APP, raw=b"x" * 17000, method="POST")[0] == 413, "too big: 413")
ok(http("/app/appraisal/reply", APP)[0] == 405, "GET on the copy route: 405")

print("3. your words copy still feeds Steps 7 and 8, and Step 3")
ok(http("/app/trust", APP)[0] == 200, "the app has opened Layer 6 (sets up its tables)")
st_, r = http("/app/trust/words", APP, {"message_id": "w1", "text": "thanks, that worked secretword",
                                        "sent_at": now_ms, "reply_at": now_ms - 90000})
ok(st_ == 200 and r["ok"], "Step 6 answers as before")


def skipped():
    c = db()
    try:
        return [x for x in ap.log_rows(c, 0, ("skip",)) if x["message_id"] == "w1"]
    finally:
        c.close()


ok(wait_for(lambda: skipped()), "Step 3 heard about it after Step 7's read")
ok("no Gemini key" in skipped()[0]["why"], "no key on the tablet: message events skipped, said so")
c = db()
dump = str(c.execute("select * from appraisal_log").fetchall()) + str(c.execute("select * from appraisal_events")
                                                                      .fetchall())
c.close()
ok("secretword" not in dump, "your text isn't stored")

print("4. settings")
st_, r = http("/app/appraisal", APP, {"set": {"limit.per_message": 0.2}, "note": "gentler"})
ok(st_ == 200 and r["edited"][0]["new"] == 0.2 and r["changed"], "you change the limit from the app")
st_, r = http("/app/appraisal", APP, {"set": {"no_fear": 0}})
ok(st_ == 400 and r["code"] == "locked", "no fear is locked")
st_, r = http("/app/appraisal", APP, {"set": {"fact_check": 0}})
ok(st_ == 400 and r["code"] == "locked", "the fact check is locked on")
st_, r = http("/app/appraisal", APP, {"reset": "all"})
ok(st_ == 200 and not os.path.exists(aset.path()), "reset all")
ok(http("/app/appraisal", APP, {"set": {"limit.per_message": 0.2}, "door": "rivia"})[0] == 400,
   "you can't send a door")

print("5. the gate")
ok(http("/app/appraisal")[0] == 401 and http("/app/appraisal", ADMIN)[0] == 403, "no token 401, admin token 403")
ok(http("/app/appraisal/reply", None, {"message_id": "x", "text": LONG})[0] == 401, "the copy route needs the token")
ok(http("/app/appraisal", APP, method="DELETE")[0] == 405, "DELETE: 405")
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
STEP3 = {"rivia_appraisal", "rivia_appraisal_app", "rivia_appraisal_settings", "rivia_expression"}
present = [m for m in sorted(TOOL_MODULES) if os.path.exists(os.path.join(HERE, m + ".py"))]
leaks = [m for m in present if imports(os.path.join(HERE, m + ".py"))[0] & STEP3]
ok(len(present) == len(TOOL_MODULES) and not leaks, "no tool module imports Step 3 (%d checked)" % len(present))
for m in sorted(STEP3):
    mods, tr_ = imports(os.path.join(HERE, m + ".py"))
    calls = {n.func.attr for n in ast.walk(tr_) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    ok("tool" not in calls and not {x for x in mods if x.startswith(("fastmcp", "mcp", "rivia_tools"))},
       "%s registers no tool and doesn't touch the tool table" % m)
src = open(os.path.join(HERE, "rivia_server.py")).read()
ok("+ appraisal_routes()" in src and "rivia_appraisal_app.routes()" in src, "rivia_server hands it over as routes")
ok(not any("rivia_" + "inject" in str(imports(os.path.join(HERE, m + ".py"))[0]) for m in STEP3),
   "Step 3 doesn't touch what Gemini is told (the injection file is unchanged)")

server.should_exit = True
print("\n%d checks passed" % COUNT["n"])
