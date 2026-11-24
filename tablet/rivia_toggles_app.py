# rivia_toggles_app.py: the phone app's door for the layer and piece switches, as plain HTTP on the app door.
#
#   GET  /app/toggles   every piece of Layers 1 to 8 in screen order: id, layer, step, title, where, switchable,
#                       value (Jordan's setting), state (what actually runs), needs, needed_by, why,
#                       would_also_turn_off. States are worked out on every read (rivia_toggles).
#   POST /app/toggles   {"id": "l7", "on": false, "note": "optional"}
#                       200 with the same body as GET after the change; 400 bad_request / unknown_id /
#                       not_switchable / phone_piece; 409 parent_off / env_off.
#
# Same pattern as /app/style, /app/reading and /app/trust: a plain route, never an MCP tool, never in /app/mcp's
# tools/list. Only app code holding the app token reaches it (the bearer gate in rivia_auth); every write uses the
# "app_button" door. Rivia can't touch any of it.
import json

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import rivia_toggles as toggles

PATH = "/app/toggles"
DOOR = "app_button"
MAX_BODY = 4096
ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def _err(status, code, msg):
    return JSONResponse({"ok": False, "code": code, "error": msg}, status_code=status)


def handle_post(body):
    """One switch change, already parsed. Returns (status, reply)."""
    if not isinstance(body, dict) or set(body) - {"id", "on", "note"} or "id" not in body or "on" not in body:
        return 400, {"ok": False, "code": "bad_request", "error": "send id and on (true or false), and optionally note"}
    try:
        toggles.set_switch(body["id"], body["on"], door=DOOR, note=body.get("note"))
    except toggles.ToggleError as e:
        return e.status, {"ok": False, "code": e.code, "error": str(e)}
    return 200, toggles.view()


async def endpoint(request: Request):
    if request.method not in ("GET", "HEAD", "POST"):
        return _err(405, "method_not_allowed", "use GET or POST")
    try:
        if request.method in ("GET", "HEAD"):
            return JSONResponse(toggles.view())
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return _err(413, "too_big", "body is over %d bytes" % MAX_BODY)
        try:
            body = json.loads(raw or b"null")
        except ValueError:
            return _err(400, "bad_json", "body must be JSON")
        status, reply = handle_post(body)
        return JSONResponse(reply, status_code=status)
    except OSError as e:  # the file couldn't be written: nothing changed, the old file is intact
        return _err(500, "write_failed", "the switch couldn't be saved (%s); nothing changed" % type(e).__name__)
    except Exception as e:  # noqa: BLE001  (a broken file must not take the server down)
        return _err(500, "internal", type(e).__name__)


def routes():
    """What rivia_server hands build_app beside the other cards. Every method lands here (405 for the rest) so none
    falls through to the /app MCP mount."""
    return [Route(PATH, endpoint, methods=ALL_METHODS)]
