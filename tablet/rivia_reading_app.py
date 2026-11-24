# rivia_reading_app.py: the phone app's door for Step 7 (emotion spec, Step 7.11), as plain HTTP on the app door.
# DRAFT, Nov 24, not installed.
#
#   GET  /app/reading            Layer 7's card: each situation with its message count and normal, how you read
#                                right now against your normal, pattern guesses (proposed, confirmed, rejected), the
#                                questions she would have asked (shadow: she didn't), times she'd quietly adjust, how
#                                often what you said matched her read, whether the word list and Flash read are on,
#                                the wording rules, and every adjustable value grouped by piece (7.1 to 7.8).
#   POST /app/reading            {"set": {path: value}, "note": "..."} or {"reset": "all" | "piece:7.4" | path}
#   POST /app/reading/pattern    {"id": 3, "confirm": true | false, "note": "..."}  you confirm or reject a guess
#
# Messages come in through Step 6's /app/trust/words (the app's copy of each message you type). That route hands
# them to rivia_reading.take() after Step 6 has read them, and then runs the Flash read in the background.
#
# Same pattern as /app/trust and /app/progress: plain routes, never MCP tools, never in /app/mcp's tools/list. Only
# app code holding the app token reaches them; every write checks for the "app_button" door. Rivia can't touch any
# of it. test_reading_app.py checks that.
import json
import time

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import rivia_reading as reading
import rivia_reading_settings as rsettings

PATH = "/app/reading"
PATTERN_PATH = "/app/reading/pattern"
DOOR = "app_button"
MAX_BODY = 8192
ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def _err(status, code, msg):
    return JSONResponse({"ok": False, "code": code, "error": msg}, status_code=status)


def view(con=None, now=None):
    """Everything Layer 7's card shows. Reads, and refreshes pattern guesses (code's own, never Rivia's)."""
    eff = rsettings.effective()
    v = reading.view(con=con, now=now, tune=rsettings.tune(eff["values"]))
    sv = rsettings.view()
    return {**v, **sv, "problems": sv["problems"] + ([v["scores"]["word_list_problem"]]
                                                     if v["scores"]["word_list_problem"] else [])}


def handle_settings(body):
    if not isinstance(body, dict) or len({"set", "reset"} & set(body)) != 1:
        return 400, {"ok": False, "code": "bad_request", "error": "send exactly one of set or reset"}
    action = "set" if "set" in body else "reset"
    if set(body) - {action, "note"}:
        return 400, {"ok": False, "code": "bad_request", "error": "%s takes only an optional note beside it" % action}
    try:
        if action == "set":
            r = rsettings.save(body["set"], door=DOOR, note=body.get("note"))
        else:
            r = rsettings.reset(body["reset"], door=DOOR, note=body.get("note"))
    except rsettings.SettingsError as e:
        return 400, {"ok": False, "code": e.code, "error": str(e)}
    return 200, {"ok": True, **rsettings.view(), "edited": r["changes"]}


def handle_pattern(body, con=None, now=None):
    if not isinstance(body, dict) or set(body) - {"id", "confirm", "note"} or "id" not in body \
            or "confirm" not in body:
        return 400, {"ok": False, "code": "bad_request", "error": "send id, confirm (true or false), and optionally "
                                                                  "note"}
    con, own = reading._open(con)
    try:
        p = reading.decide_pattern(con, body["id"], body["confirm"], door=DOOR, note=body.get("note"), now=now)
    except reading.ReadingError as e:
        return (404 if e.code == "unknown" else 400), {"ok": False, "code": e.code, "error": str(e)}
    finally:
        if own:
            con.close()
    return 200, {"ok": True, "pattern": p,
                 "note": "in shadow a confirmed pattern is only logged; it changes nothing she says yet"}


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


async def pattern(request: Request):
    if request.method != "POST":
        return _err(405, "method_not_allowed", "use POST")
    try:
        body, bad = await _body(request)
        if bad is not None:
            return bad
        status, reply = handle_pattern(body)
        return JSONResponse(reply, status_code=status)
    except Exception as e:  # noqa: BLE001
        return _err(500, "internal", type(e).__name__)


def routes():
    """What rivia_server hands build_app beside /app/trust. Every method lands here (405 for the rest) so none falls
    through to the /app MCP mount."""
    return [Route(PATH, card, methods=ALL_METHODS), Route(PATTERN_PATH, pattern, methods=ALL_METHODS)]
