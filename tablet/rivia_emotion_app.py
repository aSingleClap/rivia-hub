# rivia_emotion_app.py: the phone app's manual-control button for Rivia's emotion state (emotion
# spec, Step 1.5), as plain HTTP on the app door:
#
#   GET  /app/emotion   the state plus its current label (reads only)
#   POST /app/emotion   {"set": {"v", "a", "d"}} or {"nudge": {"dv", "da", "dd"}}, optional "note"
#
# Why a plain route and not an MCP tool: every tool on /app/mcp is in that door's tools/list, which is
# what the app hands Gemini (minus whatever the app's own filter hides). A route is never in tools/list,
# so Gemini can't see it or call it, whatever the app's filter does. Only app code that knows the path
# and holds the app token can reach it. The bearer gate (rivia_auth) already covers it: /app/emotion is
# under /app/, so it needs the app token; the admin token gets 403 and no token gets 401.
#
# Rules: never register these functions with mcp.tool(), never add them to the tool table
# (rivia_tools.ACTIONS), and keep this the only module that writes with door "app_button".
# test_emotion_app.py checks all three, and on the tablet checks the live /app/mcp tools/list too.
import json

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import rivia_emotion as emo

PATH = "/app/emotion"
DOOR = "app_button"
CAUSE = "manual"
MAX_BODY = 2048  # a set or nudge is under 200 bytes
NOTE_MAX = 200


def _view(state):
    """A state row plus the word and intensity label() gives it."""
    lab = emo.label(state["v"], state["a"], state["d"])
    return {"v": state["v"], "a": state["a"], "d": state["d"], "updated": state["updated"],
            "label": lab["name"], "intensity": lab["intensity"], "magnitude": round(lab["magnitude"], 3)}


def _err(status, code, msg):
    return JSONResponse({"ok": False, "code": code, "error": msg}, status_code=status)


def _note(body):
    note = body.get("note")
    if note is None:
        return None
    if not isinstance(note, str):
        raise emo.EmotionError("bad_value", "note must be text")
    note = note.strip()
    if len(note) > NOTE_MAX:
        raise emo.EmotionError("bad_value", "note is longer than %d characters" % NOTE_MAX)
    return note or None


def _nums(part, keys, name):
    if not isinstance(part, dict) or set(part) - set(keys):
        raise emo.EmotionError("bad_value", "%s takes only %s" % (name, ", ".join(keys)))
    return [part.get(k, 0) for k in keys]


def handle(body):
    """One button press, already parsed. Returns (status, reply dict). Kept apart from Starlette so the
    test can call it directly too."""
    if not isinstance(body, dict) or len({"set", "nudge"} & set(body)) != 1 or set(body) - {"set", "nudge", "note"}:
        return 400, {"ok": False, "code": "bad_request", "error": "send exactly one of set or nudge, plus an optional note"}
    try:
        note = _note(body)
        if "set" in body:
            v, a, d = _nums(body["set"], ("v", "a", "d"), "set")
            if not set(body["set"]) >= {"v", "a", "d"}:
                raise emo.EmotionError("bad_value", "set needs v, a and d")
            r = emo.write(v, a, d, cause=CAUSE, note=note, door=DOOR)
        else:
            dv, da, dd = _nums(body["nudge"], ("dv", "da", "dd"), "nudge")
            r = emo.nudge(dv, da, dd, cause=CAUSE, note=note, door=DOOR)
    except emo.EmotionError as e:
        return 400, {"ok": False, "code": e.code, "error": str(e)}
    return 200, {"ok": True, "old": _view(r["old"]), "new": _view(r["new"]), "clamped": r["clamped"]}


async def emotion(request: Request):
    if request.method not in ("GET", "HEAD", "POST"):
        return _err(405, "method_not_allowed", "use GET or POST")
    try:
        if request.method in ("GET", "HEAD"):
            return JSONResponse({"ok": True, "state": _view(emo.read())})
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return _err(413, "too_big", "body is over %d bytes" % MAX_BODY)
        try:
            body = json.loads(raw or b"null")
        except ValueError:
            return _err(400, "bad_json", "body must be JSON")
        status, reply = handle(body)
        return JSONResponse(reply, status_code=status)
    except Exception as e:  # noqa: BLE001  (a broken emotion.db must not take the server down)
        return _err(500, "internal", type(e).__name__)


def routes():
    """The routes rivia_auth.build_app puts ahead of the /app mount (rivia_auth.patch)."""
    # Every method lands here (405 for the rest) so none falls through to the /app MCP mount.
    return [Route(PATH, emotion, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])]
