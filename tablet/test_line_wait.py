"""Her mood line waits for the Flash read of the message Jordan just typed (Nov 28, Jordan: "Lets make this change").
  1. rivia_line_wait on its own: a mark is seen, old marks are dropped, the wait never runs past WAIT_S, a bad id
     or Layer 3's message reading switched off means no wait
  2. over real HTTP through the bearer gate, with the words route and the line route on one server and the Flash read
     stood in for (it sleeps, then pushes her mood as Step 3 would):
     - the line asked for before the words copy even arrives still waits, and carries the push
     - the line asked for with no ?after= is exactly as before (same shape, no wait)
     - a message the tablet never gets: the line waits WAIT_S, then goes out as it stands
     - a Flash read that fails still lets the line go out at once
     - Layer 7 switched off: the words copy says "done" straight away, so the line doesn't wait
  3. never a tool: no tool module imports rivia_line_wait
Throwaway dirs and secrets.
    python test_line_wait.py
"""
import ast, asyncio, json, os, secrets, socket, sys, tempfile, threading, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
DOT = os.path.join(TMP, "dot")
os.makedirs(DOT, mode=0o700)
os.environ.update(RIVIA_DIR=os.path.join(TMP, "rivia"), RIVIA_HOME=os.path.join(TMP, "rivia"), RIVIA_DOTDIR=DOT,
                  RIVIA_DB=os.path.join(TMP, "auth.db"), HOME=TMP, RIVIA_JWT_KEY=secrets.token_hex(32),
                  RIVIA_SIGNIN_CHECK="0", RIVIA_GOOGLE_TOKEN_PATH=os.path.join(DOT, "google_token.json"))
os.environ.pop("RIVIA_TRUST_KILL", None)
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, HERE)

import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

import rivia_auth as ra  # noqa: E402
import rivia_emotion as emo  # noqa: E402
import rivia_emotion_app as eapp  # noqa: E402
import rivia_line_wait as lw  # noqa: E402
import rivia_reading_flash as flash  # noqa: E402
import rivia_toggles as toggles  # noqa: E402
import rivia_trust_app as tapp  # noqa: E402

failed = 0


def ok(cond, label):
    global failed
    print(("  ok · " if cond else "FAIL · ") + label)
    failed += not cond


# ── 1. on its own ──
print("1. rivia_line_wait on its own")
lw.mark_done("a1", now=1000.0)
ok(lw.is_done("a1"), "a mark is seen")
lw.mark_done("a2", now=1000.0 + lw.KEEP_S + 1)
ok(not lw.is_done("a1") and lw.is_done("a2"), "marks older than KEEP_S are dropped")
for bad in (None, "", "x" * 81, 5):
    lw.mark_done(bad)
ok(None not in lw._done and "" not in lw._done and 5 not in lw._done, "bad ids are never marked (and never raise)")

read, ms = asyncio.run(lw.wait_for("a2"))
ok(read == "done" and ms < 200, "already finished: no wait (%s, %d ms)" % (read, ms))
ok(asyncio.run(lw.wait_for(""))[0] == "skipped" and asyncio.run(lw.wait_for("x" * 81))[0] == "skipped",
   "a bad id: no wait")


async def _late_mark():
    asyncio.get_running_loop().call_later(0.3, lw.mark_done, "b1")
    return await lw.wait_for("b1")

read, ms = asyncio.run(_late_mark())
ok(read == "done" and 250 <= ms < 1000, "a read that finishes 0.3 s in: the line goes out right then (%d ms)" % ms)
t0 = time.monotonic()
read, ms = asyncio.run(lw.wait_for("never", timeout=99))
took = time.monotonic() - t0
ok(read == "timeout" and lw.WAIT_S - 0.1 <= took < lw.WAIT_S + 0.5,
   "never finishes: gives up at WAIT_S (%.1f s), even when asked to wait longer (%.2f s)" % (lw.WAIT_S, took))
_runs = toggles.runs
for off in ("l3", "l7", "l7.1.flash"):
    toggles.runs = lambda piece, off=off: False if piece == off else _runs(piece)
    ok(asyncio.run(lw.wait_for("c1"))[0] == "skipped", "%s switched off: no push can come, no wait" % off)
toggles.runs = _runs
ok(lw.WAIT_S <= 4, "the wait is at most 4 s (it's %.1f s)" % lw.WAIT_S)


# ── 2. over real HTTP ──
print("2. over real HTTP")


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


server, BASE = serve(ra.build_app(app_mcp=standin_mcp(), admin_mcp=standin_mcp(),
                                  extra_routes=eapp.routes() + tapp.routes()))
APP = ra.mint_door_token("app", 1)["token"]


def http(p, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + p, data=data, method="POST" if data is not None else "GET")
    req.add_header("authorization", "Bearer " + APP)
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r_:
            return r_.status, json.loads(r_.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


# The Flash read stood in for: no key or network here. It sleeps like a real read, then pushes her mood the way
# Step 3's from_flash does (door "code"). PUSH_AFTER and FAIL are set per check.
STAND = {"sleep": 0.8, "fail": False, "seen": []}


def fake_read(message_id, text, con=None, now=None, tune=None):
    STAND["seen"].append(message_id)
    time.sleep(STAND["sleep"])
    if STAND["fail"]:
        raise RuntimeError("Flash down")
    emo.write(*(0.5 * x for x in emo.REFERENCE_POINTS["annoyance"]), cause="test push", door="code")
    return "ok"


flash.read = fake_read
import rivia_normal as normal  # noqa: E402
import rivia_satisfaction as sat  # noqa: E402
_c = normal.db_open()
_c.executescript(sat.SCHEMA)   # the tablet has this table already; a fresh test folder doesn't
_c.close()
emo.write(0, 0, 0, cause="start neutral", door="code")
ok(http("/app/emotion/line")[1].get("line") == "", "she starts neutral: no line")


def get_line_in_thread(path, out):
    t0 = time.monotonic()
    out["reply"] = http(path)
    out["took"] = time.monotonic() - t0

# The app asks for the line first and the words copy lands 0.2 s later (the race seen on Nov 27).
out = {}
th = threading.Thread(target=get_line_in_thread, args=("/app/emotion/line?after=m-1", out))
th.start()
time.sleep(0.2)
st_w, _ = http("/app/trust/words", {"message_id": "m-1", "text": "reply shorter"})
th.join(10)
st, r = out["reply"]
ok(st_w == 200 and STAND["seen"] == ["m-1"], "the words copy arrived and its Flash read ran")
ok(st == 200 and r["read"] == "done" and r["line"] != "",
   "the line waited for the read and carries the push: %r" % r.get("line"))
ok(0.8 <= out["took"] < lw.WAIT_S, "it waited about as long as the read took (%.2f s)" % out["took"])
ok(set(r) == {"ok", "line", "shadow", "read", "waited_ms"} and isinstance(r["waited_ms"], int),
   "shape: the usual three plus read and waited_ms")

t0 = time.monotonic()
st, r = http("/app/emotion/line")
ok(st == 200 and set(r) == {"ok", "line", "shadow"} and time.monotonic() - t0 < 0.5,
   "no ?after=: exactly as before, no wait")

emo.write(0, 0, 0, cause="back to neutral", door="code")
t0 = time.monotonic()
st, r = http("/app/emotion/line?after=m-lost")
took = time.monotonic() - t0
ok(st == 200 and r["read"] == "timeout" and r["line"] == "" and lw.WAIT_S - 0.1 <= took < lw.WAIT_S + 0.7,
   "a message the tablet never got: waits %.1f s, then the line as it stands (%.2f s)" % (lw.WAIT_S, took))

STAND.update(fail=True, sleep=0.1)
out = {}
th = threading.Thread(target=get_line_in_thread, args=("/app/emotion/line?after=m-2", out))
th.start()
time.sleep(0.1)
http("/app/trust/words", {"message_id": "m-2", "text": "ok"})
th.join(10)
ok(out["reply"][1]["read"] == "done" and out["took"] < 1.5,
   "a Flash read that fails: the line still goes out at once (%.2f s)" % out["took"])

_runs = toggles.runs
toggles.runs = lambda piece: False if piece.startswith("l7") else _runs(piece)
STAND.update(fail=False, sleep=0.1, seen=[])
st_w, _ = http("/app/trust/words", {"message_id": "m-3", "text": "thanks"})
time.sleep(0.3)
ok(st_w == 200 and STAND["seen"] == [] and lw.is_done("m-3"),
   "Layer 7 off: nothing reads the copy, and it's marked done so no line waits on it")
toggles.runs = _runs
st_w, _ = http("/app/trust/words", {"message_id": "m-4"})
time.sleep(0.2)
ok(st_w == 400 and lw.is_done("m-4"), "a refused words copy is marked done too")

# ── 3. never a tool ──
print("3. never a tool")
tool_modules = [f for f in os.listdir(HERE) if f.endswith(".py") and f in ("rivia_tools.py", "rivia_server.py")]
imports = set()
for f in tool_modules:
    for node in ast.walk(ast.parse(open(os.path.join(HERE, f)).read())):
        if isinstance(node, ast.Import):
            imports |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
ok("rivia_line_wait" not in imports, "rivia_tools and rivia_server don't import it")
src = open(os.path.join(HERE, "rivia_line_wait.py")).read()
ok("mcp" not in src.lower().replace("never an mcp tool", ""), "it registers nothing with MCP")

server.should_exit = True
print("PASS" if not failed else "%d FAILED" % failed)
sys.exit(1 if failed else 0)
