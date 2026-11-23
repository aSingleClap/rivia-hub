"""The app's emotion button (Step 1.5): /app/emotion is a plain HTTP route behind the app token, never an
MCP tool, so Gemini's tool list can't contain it. Checks:
  1. in the code: no module that registers MCP tools imports the emotion modules, the button module
     registers no tool, and only it writes as door "app_button"
  2. over real HTTP through rivia_auth's bearer gate: app token reads, sets and nudges (logged as
     app_button, cause manual), clamps, refuses bad input without touching the state; admin token 403,
     no token 401; /app/mcp still reaches the MCP mount; build_app refuses a route outside the doors
  3. on the tablet (needs fastmcp): the live /app/mcp tools/list, the list the app hands Gemini, has no
     emotion tool, and the real server answers /app/emotion
Needs the rivia-hub tablet files with rivia_server_auth.patch applied. Throwaway dirs and secrets.
    python test_emotion_app.py
"""
import ast, asyncio, json, os, secrets, socket, sys, tempfile, threading, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
DOT = os.path.join(TMP, "dot")
os.makedirs(DOT, mode=0o700)
os.environ.update(RIVIA_DIR=os.path.join(TMP, "rivia"), RIVIA_HOME=os.path.join(TMP, "rivia"), RIVIA_DOTDIR=DOT,
                  RIVIA_DB=os.path.join(TMP, "auth.db"), HOME=TMP, RIVIA_JWT_KEY=secrets.token_hex(32),
                  RIVIA_SIGNIN_CHECK="0", RIVIA_GOOGLE_TOKEN_PATH=os.path.join(DOT, "google_token.json"))
sys.path.insert(0, HERE)

import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

import rivia_auth as ra  # noqa: E402
import rivia_emotion as emo  # noqa: E402
# Step 2 (2.9 merge): decay is on, so the state moves between two reads a second apart, and a nudge starts
# from the faded value. Every check in this file is about Step 1 behaviour (storing, clamping, the log, the
# label, the doors), which assumes no time passes, so the clock is stopped for this file. Nothing else
# changes. Decay is tested in test_decay.py and test_step2_acceptance.py.
import rivia_time as _rt  # noqa: E402
_STOPPED = _rt.now_s()
_rt.now_s = lambda: _STOPPED
import rivia_emotion_app as eapp  # noqa: E402

failed = 0


def ok(cond, label):
    global failed
    print(("  ok · " if cond else "FAIL · ") + label)
    failed += not cond


# ── 1. in the code ──
print("1. in the code")
EMOTION_MODULES = {"rivia_emotion", "rivia_emotion_app"}  # the injection line has its own check in test_inject.py
TOOL_MODULES = {"rivia_tools", "rivia_admin", "rivia_battery", "rivia_places", "rivia_sched", "rivia_settings",
                "rivia_approvals", "rivia_planner", "rivia_gcal", "rivia_google", "rivia_store"}


def imports(path):
    tree = ast.parse(open(path).read())
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out, tree


present = [m for m in sorted(TOOL_MODULES) if os.path.exists(os.path.join(HERE, m + ".py"))]
ok(len(present) == len(TOOL_MODULES), "every tool-registering module is here to check (%d)" % len(present))
leaks = [m for m in present if imports(os.path.join(HERE, m + ".py"))[0] & EMOTION_MODULES]
ok(not leaks, "no tool-registering module imports the emotion code%s" % (": " + ", ".join(leaks) if leaks else ""))
mods, tree = imports(os.path.join(HERE, "rivia_emotion_app.py"))
calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
ok("tool" not in calls and not {m for m in mods if m.startswith(("fastmcp", "mcp", "rivia_tools"))},
   "the button module registers no MCP tool and doesn't touch the tool table")
users = [f for f in os.listdir(HERE) if f.endswith(".py") and not f.startswith("test_")
         and f != "rivia_emotion.py" and '"app_button"' in open(os.path.join(HERE, f)).read()
         and "rivia_emotion" in {n for n in ast.walk(ast.parse(open(os.path.join(HERE, f)).read()))
                                 for n in ([a.name for a in n.names] if isinstance(n, ast.Import) else
                                           [n.module] if isinstance(n, ast.ImportFrom) else [])}]
# 4.9 merge: Step 4.3's rivia_normal.py also names "app_button" (Jordan's reassure button), but it writes body.db
# and never imports rivia_emotion, so it can't write her emotion. The guard is about emotion writers.
ok(users == ["rivia_emotion_app.py"], "only rivia_emotion_app.py writes emotion as app_button (%s)" % users)
server_src = open(os.path.join(HERE, "rivia_server.py")).read()
ok("extra_routes=emotion_routes()" in server_src and "app_mcp.tool" not in server_src,
   "rivia_server hands the button to build_app as a route, not to app_mcp")

# ── 2. over real HTTP ──
print("2. over HTTP, through the bearer gate")


def standin_mcp():
    async def mcp(request):
        return PlainTextResponse("mcp mount")
    return Starlette(routes=[Route("/mcp", mcp, methods=["GET", "POST"])])


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(app):
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    return server, "http://127.0.0.1:%d" % port


def http(base, path, token=None, body=None, method=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, method=method or ("POST" if data is not None else "GET"))
    if token:
        req.add_header("authorization", "Bearer " + token)
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            text = r.read().decode()
            status = r.status
    except urllib.error.HTTPError as e:
        text, status = e.read().decode(), e.code
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


server, BASE = serve(ra.build_app(app_mcp=standin_mcp(), admin_mcp=standin_mcp(), extra_routes=eapp.routes()))
ok(server.started, "test server started")
APP = ra.mint_door_token("app", 1)["token"]
ADMIN = ra.mint_door_token("admin", 1)["token"]

st, r = http(BASE, "/app/emotion", APP)
ok(st == 200 and r["state"]["label"] == "neutral" and (r["state"]["v"], r["state"]["a"], r["state"]["d"]) == (0, 0, 0),
   "GET with the app token: neutral at 0, 0, 0")
ok(http(BASE, "/app/emotion")[0] == 401, "no token: 401")
ok(http(BASE, "/app/emotion", ADMIN)[0] == 403, "admin token: 403 (wrong door)")
ok(http(BASE, "/app/emotion", "x.y.z", {"nudge": {"dv": 0.5}})[0] == 401, "bad token can't write: 401")
ok(emo.read()["v"] == 0, "refused calls left the state alone")

anger = emo.REFERENCE_POINTS["anger"]
st, r = http(BASE, "/app/emotion", APP, {"set": dict(zip("vad", anger)), "note": " button test "})
ok(st == 200 and r["new"]["label"] == "anger" and r["clamped"] == [], "set to anger's coordinates reads anger")
log = emo.history(1)[0]
ok(log["door"] == "app_button" and log["cause"] == "manual" and log["note"] == "button test",
   "logged as door app_button, cause manual, note in its own field (trimmed)")

st, r = http(BASE, "/app/emotion", APP, {"nudge": {"dv": -1, "dd": 0.1}})
ok(st == 200 and r["new"]["v"] == -1 and r["clamped"] == ["v"], "a nudge past -1 stops at -1 and says so")
ok(emo.history(1)[0]["cause"] == "manual", "no note: cause is just manual")

before, n_log = emo.read(), len(emo.history(1000))
bad = [
    ("empty body", None, b""),
    ("not JSON", None, b"{nope"),
    ("both set and nudge", {"set": {"v": 0, "a": 0, "d": 0}, "nudge": {"dv": 1}}, None),
    ("neither", {"note": "hi"}, None),
    ("unknown key", {"nudge": {"dv": 0.1}, "door": "admin"}, None),
    ("set missing d", {"set": {"v": 0, "a": 0}}, None),
    ("set with an extra axis", {"set": {"v": 0, "a": 0, "d": 0, "x": 1}}, None),
    ("text as a number", {"nudge": {"dv": "0.5"}}, None),
    ("all-zero nudge", {"nudge": {}}, None),
    ("note not text", {"nudge": {"dv": 0.1}, "note": 5}, None),
    ("note too long", {"nudge": {"dv": 0.1}, "note": "x" * 201}, None),
    ("a list", [1, 2, 3], None),
]
codes = {}
for name, body, raw in bad:
    st, r = http(BASE, "/app/emotion", APP, body, method="POST", raw=raw)
    codes[name] = st
ok(all(st == 400 for st in codes.values()), "bad input is 400 (%s)" % ", ".join("%s %s" % kv for kv in codes.items()))
ok(http(BASE, "/app/emotion", APP, raw=b"x" * 3000, method="POST")[0] == 413, "oversized body: 413")
ok(emo.read() == before and len(emo.history(1000)) == n_log, "every refusal left the state and the log alone")
ok(all(http(BASE, "/app/emotion", APP, method=m, raw=b"")[0] == 405 for m in ("DELETE", "PUT", "PATCH")),
   "DELETE, PUT, PATCH: 405, none falls through to the MCP mount")

ok(http(BASE, "/app/mcp", APP, raw=b"{}", method="POST") == (200, "mcp mount"), "/app/mcp still reaches the MCP mount")
for path in ("/emotion", "/app/login", "/health/emotion"):
    try:
        ra.build_app(extra_routes=[Route(path, eapp.emotion)])
        refused = False
    except SystemExit:
        refused = True
    ok(refused, "build_app refuses an extra route at %s" % path)
server.should_exit = True

# ── 3. the live tool list (tablet) ──
print("3. Gemini's tool list (live /app/mcp)")
try:
    from fastmcp import Client
except ImportError:
    Client = None
    print("  SKIPPED: fastmcp isn't installed here; this part runs on the tablet")

if Client is not None:
    import rivia_server as rs
    import rivia_tools as tools
    tools.reset_install()
    server, BASE = serve(rs.public_app())
    ok(server.started, "real server started")
    APP = ra.mint_door_token("app", 1)["token"]
    ADMIN = ra.mint_door_token("admin", 1)["token"]

    async def listed(url, token):
        async with Client(url, auth=token) as c:
            return {t.name: (t.description or "") for t in await c.list_tools()}

    app_tools = asyncio.run(listed(BASE + "/app/mcp", APP))
    admin_tools = asyncio.run(listed(BASE + "/admin/mcp", ADMIN))
    words = ("emotion", "valence", "arousal", "dominance", "vad", "mood")
    hits = [n for n, d in app_tools.items() if any(w in (n + " " + d).lower() for w in words)]
    ok(len(app_tools) > 0, "app door lists its tools (%d)" % len(app_tools))
    ok(not hits, "no app-door tool is an emotion control%s" % (": " + ", ".join(hits) if hits else ""))
    ok(not [n for n in admin_tools if "emotion" in n], "no emotion tool on the admin door either")
    st, r = http(BASE, "/app/emotion", APP)
    ok(st == 200 and r.get("ok"), "the real server answers GET /app/emotion with the app token")
    server.should_exit = True

print("PASS" if not failed else "FAILED: %d" % failed)
sys.exit(1 if failed else 0)
