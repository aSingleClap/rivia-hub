# rivia_style_app.py: the phone app's door for Step 8 (emotion spec, Step 8.11), as plain HTTP on the app door.
# DRAFT, Nov 24, not installed.
#
#   GET  /app/style    Layer 8's card: the latest would-be style (shadow: she didn't use it), the last few, how
#                      often each style came up, where the steady-then-calmer plan stands, how often you were worked
#                      up, what she has learned per style (empty in shadow), how many bits are cooling down, the fixed
#                      rules, and every adjustable value grouped by piece (8.1 to 8.8).
#   POST /app/style    {"set": {path: value}, "note": "..."} or {"reset": "all" | "piece:8.2" | path}
#
# Messages come in through Step 6's /app/trust/words (the app's copy of each message you type). That route hands
# them to Step 7, then to rivia_style.take(), then drops the text.
#
# Same pattern as /app/reading: plain routes, never MCP tools, never in /app/mcp's tools/list. Only app code holding
# the app token reaches them; every write checks for the "app_button" door. Rivia can't touch any of it.
# test_style_app.py checks that.
import json
import time

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import rivia_style as style
import rivia_style_settings as ssettings

PATH = "/app/style"
DOOR = "app_button"
MAX_BODY = 8192
ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def _err(status, code, msg):
    return JSONResponse({"ok": False, "code": code, "error": msg}, status_code=status)


def view(con=None, now=None):
    """Everything Layer 8's card shows. Reads only."""
    eff = ssettings.effective()
    v = style.view(con=con, now=now, tune=ssettings.tune(eff["values"]))
    sv = ssettings.view()
    return {**v, **sv}


def handle_settings(body):
    if not isinstance(body, dict) or len({"set", "reset"} & set(body)) != 1:
        return 400, {"ok": False, "code": "bad_request", "error": "send exactly one of set or reset"}
    action = "set" if "set" in body else "reset"
    if set(body) - {action, "note"}:
        return 400, {"ok": False, "code": "bad_request", "error": "%s takes only an optional note beside it" % action}
    try:
        if action == "set":
            r = ssettings.save(body["set"], door=DOOR, note=body.get("note"))
        else:
            r = ssettings.reset(body["reset"], door=DOOR, note=body.get("note"))
    except ssettings.SettingsError as e:
        return 400, {"ok": False, "code": e.code, "error": str(e)}
    return 200, {"ok": True, **ssettings.view(), "edited": r["changes"]}


async def _body(request):
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return None, _err(413, "too_big", "body is over %d bytes" % MAX_BODY)
    try:
        return json.loads(raw or b"null"), None
    except ValueError:
        return None, _err(400, "bad_json", "body must be JSON")


async def card(request: Request):
    if request.method not in ("GET", "HEAD", "POST"):
        return _err(405, "method_not_allowed", "use GET or POST")
    try:
        if request.method in ("GET", "HEAD"):
            return JSONResponse(view(now=time.time()))
        body, bad = await _body(request)
        if bad is not None:
            return bad
        status, reply = handle_settings(body)
        return JSONResponse(reply, status_code=status)
    except Exception as e:  # noqa: BLE001  (a broken file must not take the server down)
        return _err(500, "internal", type(e).__name__)


def routes():
    """What rivia_server hands build_app beside /app/reading. Every method lands here (405 for the rest) so none
    falls through to the /app MCP mount."""
    return [Route(PATH, card, methods=ALL_METHODS)]
