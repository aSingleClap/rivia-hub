# rivia_appraisal_app.py: the phone app's door for Step 3 (emotion spec, Step 3.9, Layer 3 "Reactions"), as plain HTTP
# on the app door. DRAFT, Nov 24, not installed.
#
#   GET  /app/appraisal        Layer 3's card: her would-be mood (shadow) beside her real one, the last pushes and the
#                              events behind them, how often each event came up, what was skipped and why, the latest
#                              caution flags, the shadow rewording (outcomes, pass rate, the wait it would add, the last
#                              few plain vs reworded for spot-checks), and every adjustable value by piece.
#   POST /app/appraisal        {"set": {path: value}, "note": "..."} or {"reset": "all" | "piece:3.2" | path}
#   POST /app/appraisal/reply  {"message_id", "text", "at" (ms), "tool_only"}: the app's copy of one finished typed
#                              reply of hers, sent AFTER you've seen it. Answers at once; the shadow rewording runs in
#                              the background. Kept on the tablet (Jordan: "Keep it all").
#
# Same pattern as /app/style: plain routes, never MCP tools, never in /app/mcp's tools/list. Only app code holding the
# app token reaches them; every settings write checks for the "app_button" door. Rivia can't touch any of it.
import json
import sys
import time

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import rivia_appraisal as appraisal
import rivia_appraisal_settings as asettings
import rivia_expression as expression

PATH = "/app/appraisal"
REPLY_PATH = "/app/appraisal/reply"
DOOR = "app_button"
MAX_BODY = 8192
REPLY_MAX_BODY = 16384
ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def _err(status, code, msg):
    return JSONResponse({"ok": False, "code": code, "error": msg}, status_code=status)


def view(con=None, now=None, live=None):
    """Everything Layer 3's card shows. Reads only."""
    eff = asettings.effective()
    t = asettings.tune(eff["values"])
    a = appraisal.view(con=con, now=now, tune=t, live=live)
    x = expression.view(con=con, now=now, tune=t)
    out = {"ok": True, "layer": 3, **a, "rewording": x, **asettings.view(),
            "waits_on": ["Her replies only change once rewording is switched on, later and by hand. Until then this "
                         "is what she would have done.",
                         "Events from your messages ride on Step 7's Flash read, so they need the Gemini key on the "
                         "tablet and Step 7's word list.",
                         "Typed chat only: Live voice messages aren't copied to the tablet."]}
    try:  # the layer switches: Layer 3 or 3.6 off is said plainly; the card still shows what's stored
        import rivia_toggles as toggles
        if not toggles.runs("l3"):
            out["switched_off"] = "Layer 3 is switched off: no new events or rewording; this shows what's stored."
        elif not toggles.runs("l3.6"):
            out["switched_off"] = "3.6 rewording is switched off: replies are kept, not reworded."
    except Exception:  # noqa: BLE001
        pass
    return out


def handle_settings(body):
    if not isinstance(body, dict) or len({"set", "reset"} & set(body)) != 1:
        return 400, {"ok": False, "code": "bad_request", "error": "send exactly one of set or reset"}
    action = "set" if "set" in body else "reset"
    if set(body) - {action, "note"}:
        return 400, {"ok": False, "code": "bad_request", "error": "%s takes only an optional note beside it" % action}
    try:
        if action == "set":
            r = asettings.save(body["set"], door=DOOR, note=body.get("note"))
        else:
            r = asettings.reset(body["reset"], door=DOOR, note=body.get("note"))
    except asettings.SettingsError as e:
        return 400, {"ok": False, "code": e.code, "error": str(e)}
    return 200, {"ok": True, **asettings.view(), "edited": r["changes"]}


def handle_reply(body, con=None, now=None):
    if not isinstance(body, dict) or set(body) - {"message_id", "text", "at", "tool_only"}:
        return 400, {"ok": False, "code": "bad_request", "error": "send message_id, text, at and tool_only only"}
    row = expression.take_reply(body, con=con, now=now)
    if row is None:
        return 400, {"ok": False, "code": "bad_value", "error": "message_id and text are needed; tool_only is true or "
                                                               "false"}
    return 200, {"ok": True, "kept": True, "outcome": row["outcome"]}


def reword_after(body, status):
    if status != 200 or not isinstance(body, dict):
        return None
    mid = body.get("message_id")

    def run():
        try:
            expression.reword(mid)
        except Exception as e:  # noqa: BLE001
            print("rivia: Step 3 rewording skipped a reply (%s)" % type(e).__name__, file=sys.stderr)
        finally:
            body.clear()
    return run


async def _body(request, limit=MAX_BODY):
    raw = await request.body()
    if len(raw) > limit:
        return None, _err(413, "too_big", "body is over %d bytes" % limit)
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


async def reply_copy(request: Request):
    if request.method != "POST":
        return _err(405, "method_not_allowed", "use POST")
    try:
        body, bad = await _body(request, REPLY_MAX_BODY)
        if bad is not None:
            return bad
        status, reply = handle_reply(body)
        later = reword_after(body, status)
        return JSONResponse(reply, status_code=status, background=BackgroundTask(later) if later else None)
    except Exception as e:  # noqa: BLE001
        return _err(500, "internal", type(e).__name__)


def routes():
    """What rivia_server hands build_app beside /app/style. Every method lands here (405 for the rest) so none falls
    through to the /app MCP mount."""
    return [Route(PATH, card, methods=ALL_METHODS), Route(REPLY_PATH, reply_copy, methods=ALL_METHODS)]
