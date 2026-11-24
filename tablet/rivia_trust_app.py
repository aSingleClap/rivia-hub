# rivia_trust_app.py: the phone app's door for Step 6 (emotion spec, Step 6.9), as plain HTTP on the app door.
# DRAFT, Nov 23, not installed.
#
#   GET  /app/trust            Layer 6's card: trust, your satisfaction, sure-by-area and flags, each unlock with its
#                              lines and state, the off switch, the hard limits (read-only), recent judged days, the
#                              action log, and every adjustable value grouped by piece (6.2 to 6.7).
#   POST /app/trust            {"set": {path: value}, "note": "..."} or {"reset": "all" | "piece:6.4" | path}
#   POST /app/trust/feedback   a thumbs up or down on one of her messages:
#                              {"value": 1 | -1, "message_id": "...", "text": "<her message>", "day": "YYYY-MM-DD"?}
#                              The text is only used to pick the area (6.3) and is kept so you can see what you rated.
#   POST /app/trust/words      a copy of a message you typed in a Rivia chat (your chats go phone -> Google and never
#                              pass the tablet, so the app sends this copy):
#                              {"message_id": "...", "text": "<your message>", "reply_text": "<her reply before it>"?}
#                              Tablet code (rivia_satisfaction.read_words), not Rivia, decides praise, correction or
#                              neither. Your text is read and dropped; only what was heard is kept. Her reply's words
#                              only pick the area for a correction.
#                              Step 7 (DRAFT, Nov 24): also optional "sent_at" and "reply_at" (when you sent it and
#                              when her reply before it arrived, milliseconds since 1970). After Step 6's reading, the
#                              same copy goes to rivia_reading (numbers only, text dropped) and the Flash read runs
#                              in the background. A Step 7 problem never changes this route's answer.
#   POST /app/trust/unlock     {"unlock": "speak_up", "on": true | false, "note": "..."}  you, by hand, only
#   POST /app/trust/kill       {"kill": true | false, "note": "..."}  the off switch
#
# Same pattern as /app/progress (Step 5.7) and the emotion button (Step 1.5): plain routes, never MCP tools, so they
# are never in /app/mcp's tools/list (what the app hands Gemini). Only app code holding the app token reaches them;
# rivia_auth's bearer gate covers everything under /app/. Rivia can't rate herself, unlock anything or touch the off
# switch: no tool reaches these, and every write checks for the "app_button" door.
#
# Rules: never register these with mcp.tool(), never add them to rivia_tools.ACTIONS, import no tool module.
# test_trust_app.py checks all three.
import json
import time
from datetime import datetime

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import rivia_satisfaction as sat
import rivia_trust as trust
import rivia_trust_settings as tsettings

PATH = "/app/trust"
FEEDBACK_PATH = "/app/trust/feedback"
UNLOCK_PATH = "/app/trust/unlock"
KILL_PATH = "/app/trust/kill"
WORDS_PATH = "/app/trust/words"
DOOR = "app_button"
WORDS_DOOR = "app_words"
REPLY_MAX = 2000
MAX_BODY = 8192
ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def _err(status, code, msg):
    return JSONResponse({"ok": False, "code": code, "error": msg}, status_code=status)


def _open(con):
    if con is not None:
        return con, False
    import rivia_normal as normal
    return normal.db_open(), True


def _today(now):
    import rivia_plan_score as score
    return datetime.fromtimestamp(now, score.TZ).date().isoformat()


def view(con=None, now=None):
    """Everything Layer 6's card shows. Reads only."""
    import rivia_trust_shadow as ts
    now = time.time() if now is None else now
    con, own = _open(con)
    try:
        state = ts.last_state(con)
        days = ts.recent_days(con, 14)
        acts = trust.actions(con, 30)
        sigs = sat.rows(con, now - 30 * 86400)
    finally:
        if own:
            con.close()
    on, on_problem = trust.switched_on()
    eff = tsettings.effective()
    sv = tsettings.view()
    return {
        "ok": True, "layer": tsettings.LAYER, "shadow": True,
        "state": state,
        "note": None if state else "nothing judged yet: Step 6 needs Step 5's scored days, and scheduling is parked",
        "unlocks": {uid: {"words": u["words"], "switched_on": uid in on,
                          "state": ((state or {}).get("ladder") or {}).get(uid, {}).get("state", "earning"),
                          "lines": tsettings.unlock_lines(eff["values"])[uid]} for uid, u in trust.UNLOCKS.items()},
        "kill": trust.kill_state(),
        "hard_limits": trust.HARD_LIMITS,
        # The adjustable values, in /app/progress's shape (layers, notes, locked, history, changed, problems).
        **sv,
        "problems": sv["problems"] + ([on_problem] if on_problem else []),
        "days": days,
        "actions": acts,
        "thumbs": [s for s in sigs if s["source"] == "thumb"][-20:],
    }


def handle_settings(body):
    if not isinstance(body, dict) or len({"set", "reset"} & set(body)) != 1:
        return 400, {"ok": False, "code": "bad_request", "error": "send exactly one of set or reset"}
    action = "set" if "set" in body else "reset"
    if set(body) - {action, "note"}:
        return 400, {"ok": False, "code": "bad_request", "error": "%s takes only an optional note beside it" % action}
    try:
        if action == "set":
            r = tsettings.save(body["set"], door=DOOR, note=body.get("note"))
        else:
            r = tsettings.reset(body["reset"], door=DOOR, note=body.get("note"))
    except tsettings.SettingsError as e:
        return 400, {"ok": False, "code": e.code, "error": str(e)}
    return 200, {"ok": True, **tsettings.view(), "edited": r["changes"]}


def handle_feedback(body, con=None, now=None):
    now = time.time() if now is None else now
    if not isinstance(body, dict) or set(body) - {"value", "message_id", "text", "day", "note"}:
        return 400, {"ok": False, "code": "bad_request",
                     "error": "send value (1 or -1), message_id, text, and optionally day and note"}
    if not isinstance(body.get("message_id"), str) or not body["message_id"]:
        return 400, {"ok": False, "code": "bad_request", "error": "message_id is needed, so one message counts once"}
    con, own = _open(con)
    try:
        # One thumb per message: a new press replaces the old one (you changed your mind), it never adds a second.
        # Saved first and checked, then any older press on the same message is taken off, so a refused press
        # never loses the one before it.
        row = sat.add(con, "thumb", body.get("value"), door=DOOR, day=body.get("day") or _today(now), now=now,
                      message_id=body["message_id"], text=body.get("text"), note=body.get("note"))
        con.execute("delete from trust_feedback where source = 'thumb' and message_id = ? and id != ?",
                    (body["message_id"], row["id"]))
    except sat.FeedbackError as e:
        return 400, {"ok": False, "code": e.code, "error": str(e)}
    finally:
        if own:
            con.close()
    return 200, {"ok": True, "saved": row}


def handle_words(body, con=None, now=None):
    now = time.time() if now is None else now
    if not isinstance(body, dict) or set(body) - {"message_id", "text", "reply_text", "day", "sent_at", "reply_at"}:
        return 400, {"ok": False, "code": "bad_request",
                     "error": "send message_id and text, and optionally reply_text, day, sent_at and reply_at"}
    mid, text, reply = body.get("message_id"), body.get("text"), body.get("reply_text")
    if not isinstance(mid, str) or not mid or len(mid) > 80:
        return 400, {"ok": False, "code": "bad_request", "error": "message_id is needed, so one message counts once"}
    if not isinstance(text, str) or len(text) > sat.WORDS_MAX:
        return 400, {"ok": False, "code": "bad_value", "error": "text must be up to %d characters" % sat.WORDS_MAX}
    if reply is not None and (not isinstance(reply, str) or len(reply) > REPLY_MAX):
        return 400, {"ok": False, "code": "bad_value", "error": "reply_text must be up to %d characters" % REPLY_MAX}
    value, heard = sat.read_words(text)
    con, own = _open(con)
    try:
        row = None
        if value is not None:
            area = sat.area_from_text(reply or text) if value < 0 else "chat"
            row = sat.add(con, "words", value, door=WORDS_DOOR, day=body.get("day") or _today(now), now=now,
                          area=area, message_id=mid, note=("heard: %s" % heard)[:sat.NOTE_MAX])
        # One per message. An edited message is sent again with the same id: its new reading replaces the old one,
        # and an edit that took the praise or correction out takes the old signal off.
        con.execute("delete from trust_feedback where source = 'words' and message_id = ? and id != ?",
                    (mid, row["id"] if row else -1))
    except sat.FeedbackError as e:
        return 400, {"ok": False, "code": e.code, "error": str(e)}
    finally:
        if own:
            con.close()
    return 200, {"ok": True, "counted": row is not None, "heard": heard, "saved": row}


def reading_after(body, status):
    """Step 7: once Step 6 has answered the phone, read the same copy for Step 7 (it may read the calendar), then the
    Flash read, all in the background so the phone never waits. Errors stay there: Step 6's answer is already sent."""
    if status != 200 or not isinstance(body, dict):
        return None
    return lambda: reading_run(body)


def reading_run(body):
    try:
        import rivia_reading as reading
        import rivia_reading_flash as flash
        row = reading.take(body)
        if row is not None and row["flash_status"] == "waiting":
            flash.read(row["message_id"], body["text"])
    except Exception as e:  # noqa: BLE001
        import sys
        print("rivia: Step 7 reading skipped a message (%s)" % type(e).__name__, file=sys.stderr)
    try:
        import rivia_style as style   # Step 8 (DRAFT Nov 24): her would-be style, after Step 7 has read the message
        style.take(body.get("message_id"), body.get("text"))
    except Exception as e:  # noqa: BLE001  (Step 8 failing never stops Steps 6 and 7)
        import sys
        print("rivia: Step 8 style skipped a message (%s)" % type(e).__name__, file=sys.stderr)
    finally:
        body.clear()   # your text is dropped once read


def handle_unlock(body, con=None, now=None):
    if not isinstance(body, dict) or set(body) - {"unlock", "on", "note"} or "unlock" not in body or "on" not in body:
        return 400, {"ok": False, "code": "bad_request", "error": "send unlock, on, and optionally note"}
    con, own = _open(con)
    try:
        on = trust.set_unlock(body["unlock"], body["on"], door=DOOR, note=body.get("note"), now=now, con=con)
    except trust.TrustError as e:
        return (403 if e.code == "hard_limit" else 400), {"ok": False, "code": e.code, "error": str(e)}
    finally:
        if own:
            con.close()
    return 200, {"ok": True, "switched_on": sorted(on),
                 "note": "in shadow nothing reads an unlock yet, so this changes nothing she does today"}


def handle_kill(body, con=None, now=None):
    if not isinstance(body, dict) or set(body) - {"kill", "note"} or "kill" not in body:
        return 400, {"ok": False, "code": "bad_request", "error": "send kill (true or false), and optionally note"}
    con, own = _open(con)
    try:
        k = trust.set_kill(body["kill"], door=DOOR, note=body.get("note"), now=now, con=con)
    except trust.TrustError as e:
        return 400, {"ok": False, "code": e.code, "error": str(e)}
    finally:
        if own:
            con.close()
    return 200, {"ok": True, "kill": k}


async def _body(request):
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return None, _err(413, "too_big", "body is over %d bytes" % MAX_BODY)
    try:
        return json.loads(raw or b"null"), None
    except ValueError:
        return None, _err(400, "bad_json", "body must be JSON")


def _post_only(fn, after=None):
    async def endpoint(request: Request):
        if request.method != "POST":
            return _err(405, "method_not_allowed", "use POST")
        try:
            body, bad = await _body(request)
            if bad is not None:
                return bad
            status, reply = fn(body)
            later = after(body, status) if after else None
            return JSONResponse(reply, status_code=status,
                                background=BackgroundTask(later) if later else None)
        except Exception as e:  # noqa: BLE001  (a broken file must not take the server down)
            return _err(500, "internal", type(e).__name__)
    return endpoint


async def card(request: Request):
    if request.method not in ("GET", "HEAD", "POST"):
        return _err(405, "method_not_allowed", "use GET or POST")
    try:
        if request.method in ("GET", "HEAD"):
            return JSONResponse(view())
        body, bad = await _body(request)
        if bad is not None:
            return bad
        status, reply = handle_settings(body)
        return JSONResponse(reply, status_code=status)
    except Exception as e:  # noqa: BLE001
        return _err(500, "internal", type(e).__name__)


def routes():
    """What rivia_server hands build_app beside the emotion button and /app/progress. Every method lands here (405
    for the rest) so none falls through to the /app MCP mount."""
    return [Route(PATH, card, methods=ALL_METHODS),
            Route(FEEDBACK_PATH, _post_only(handle_feedback), methods=ALL_METHODS),
            Route(WORDS_PATH, _post_only(handle_words, after=reading_after), methods=ALL_METHODS),
            Route(UNLOCK_PATH, _post_only(handle_unlock), methods=ALL_METHODS),
            Route(KILL_PATH, _post_only(handle_kill), methods=ALL_METHODS)]
