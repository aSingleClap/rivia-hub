# rivia_progress_app.py: the phone app's door for adjusting Step 5's values (goals, scores, weights, mood pull),
# (emotion spec, Step 5.7), as plain HTTP on the app door. DRAFT, Nov 23, not installed.
#
#   GET  /app/progress   every adjustable value grouped by layer and step, with words, hard range, code value and
#                        value in use; the locked list; the last edits
#   POST /app/progress   {"set": {path: value, ...}, "note": "..."}
#                        or {"reset": "all" | "piece:5.2" | "piece:5.4" | "piece:5.5" | "piece:models" |
#                        "piece:goals" | path}. Goal limits (time, tokens, calls, money, tries) and each model's
#                        RPM/TPM/RPD are settings here like any other; 5.5 reads them and keeps no copy.
#   GET  /app/progress/usage?days=14   read-only: what section 5.5's graphs and gauges draw. 5.5's usage_view()
#                        (tokens per goal per day, requests and peaks per model per day), plus each model's
#                        RPM/TPM/RPD right now against Jordan's numbers (5.5's rivia_model_limits.usage()), when
#                        RPD resets, and the limits in use for the limit lines. Never writes anything.
#
# Same pattern as the emotion button (/app/emotion, Step 1.5): a plain route, never an MCP tool, so it is never in
# /app/mcp's tools/list (what the app hands Gemini). Only app code that knows the path and holds the app token can
# reach it; rivia_auth's bearer gate covers it because it sits under /app/ (admin token 403, no token 401).
# Rivia can't edit her own goals or weights: no tool reaches this, and rivia_progress_settings only writes for the
# "app_button" door.
#
# Rules: never register these functions with mcp.tool(), never add them to the tool table (rivia_tools.ACTIONS),
# and import no tool module. test_progress_settings.py checks all three.
import json

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import rivia_progress_settings as ps

PATH = "/app/progress"
USAGE_PATH = "/app/progress/usage"
USAGE_DAYS = 14
DOOR = "app_button"
MAX_BODY = 8192  # a full set of changes is under 2 KB


def _err(status, code, msg):
    return JSONResponse({"ok": False, "code": code, "error": msg}, status_code=status)


def get_reply():
    return {"ok": True, **ps.view()}


def handle(body):
    """One save or reset from the app, already parsed. Returns (status, reply). Kept apart from Starlette so the
    test can call it directly too. All or nothing: a refused edit saves nothing."""
    if not isinstance(body, dict) or len({"set", "reset"} & set(body)) != 1:
        return 400, {"ok": False, "code": "bad_request", "error": "send exactly one of set or reset"}
    action = "set" if "set" in body else "reset"
    if set(body) - {action, "note"}:
        return 400, {"ok": False, "code": "bad_request", "error": "%s takes only an optional note beside it" % action}
    try:
        if action == "set":
            r = ps.save(body["set"], door=DOOR, note=body.get("note"))
        else:
            r = ps.reset(body["reset"], door=DOOR, note=body.get("note"))
    except ps.SettingsError as e:
        return 400, {"ok": False, "code": e.code, "error": str(e)}
    # "edited": what this press changed (old and new). "changes" (from view) is everything saved on top of the code.
    return 200, {"ok": True, **ps.view(), "edited": r["changes"]}


def usage_reply(now=None, days=USAGE_DAYS, con=None):
    """What GET /app/progress/usage returns. Reads body.db (5.5's goal_log and model_calls) and the settings; writes
    nothing. The gauges count only calls the tablet made (see seen_note)."""
    import time

    import rivia_goal_limits as gl
    import rivia_model_limits as ml
    import rivia_usage as us
    now = time.time() if now is None else now
    own = con is None
    if own:
        import rivia_normal as normal
        con = normal.db_open()
    try:
        view = us.build(us.load_goal_rows(con, now - (days + 1) * 86400),
                        us.load_calls(con, now - (days + 1) * 86400), now, days)
        today = us.load_calls(con, ml.pacific_midnight(now) - 120)
    finally:
        if own:
            con.close()
    table, problems = ml.current_table()
    vals = ps.effective()["values"]
    return {
        "ok": True, **view,
        "gauges": ml.usage(today, table, now),
        "model_limits": table, "near_share": ml.NEAR, "problems": problems,
        "rpd_resets_at": int(ml.next_pacific_midnight(now)),
        "goal_limits": {k: ps.goal_limits(k, vals) for k in gl.DEFAULT_LIMITS},
        "goal_defaults": {k: dict(v) for k, v in gl.DEFAULT_LIMITS.items()},
        "hard_ceiling": dict(gl.HARD_CEILING),
    }


def _days(request):
    raw = request.query_params.get("days")
    if raw is None:
        return USAGE_DAYS
    if not raw.isdigit():
        return None
    return int(raw)


async def usage(request: Request):
    if request.method not in ("GET", "HEAD"):
        return _err(405, "method_not_allowed", "this is read-only; use GET")
    days = _days(request)
    if days is None:
        return _err(400, "bad_value", "days must be a whole number")
    try:
        return JSONResponse(usage_reply(days=days))
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "code", None)
        if code == "bad_value":
            return _err(400, code, str(e))
        return _err(500, "internal", type(e).__name__)


async def progress(request: Request):
    if request.method not in ("GET", "HEAD", "POST"):
        return _err(405, "method_not_allowed", "use GET or POST")
    try:
        if request.method in ("GET", "HEAD"):
            return JSONResponse(get_reply())
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return _err(413, "too_big", "body is over %d bytes" % MAX_BODY)
        try:
            body = json.loads(raw or b"null")
        except ValueError:
            return _err(400, "bad_json", "body must be JSON")
        status, reply = handle(body)
        return JSONResponse(reply, status_code=status)
    except Exception as e:  # noqa: BLE001  (a broken settings file must not take the server down)
        return _err(500, "internal", type(e).__name__)


def routes():
    """The route rivia_server hands build_app beside the emotion button. Every method lands here (405 for the rest)
    so none falls through to the /app MCP mount."""
    return [Route(PATH, progress, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]),
            Route(USAGE_PATH, usage, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])]
