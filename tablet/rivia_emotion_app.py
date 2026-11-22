# rivia_emotion_app.py: the phone app's manual-control button for Rivia's emotion state (emotion
# spec, Step 1.5), as plain HTTP on the app door:
#
#   GET  /app/emotion   the state plus its current label (reads only)
#   POST /app/emotion   {"set": {"v", "a", "d"}} or {"nudge": {"dv", "da", "dd"}}, optional "note"
#
# 2.4, what the dial moves: emotion freely; mood and baseline only while the lock is open (10 minutes
# from an unlock press), and every mood or baseline change carries a tag, logged in change_log.tag and
# in the cause: "test" (end_test puts the layer back) or, for baseline only, "calibration" (a lasting
# change). GET still returns emotion as "state" (what app v55.0 reads) and adds "layers" (all three,
# each with its label and the hand change standing on it), "dial" and "lock".
#
# 2.3 (Jordan, Nov 21): her normal counts as neutral. Emotion and mood are labelled against her baseline
# (label(rest=)), so within NEUTRAL_BELOW of it they read neutral. 2.9 merge: the baseline row itself is
# labelled as its own point (against her baseline it would always read neutral), so the app can show what
# her normal is; a baseline change's old/new are labelled the same way.
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
import time

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import rivia_emotion as emo

PATH = "/app/emotion"
DOOR = "app_button"
CAUSE = "manual"
MAX_BODY = 2048  # a set or nudge is under 200 bytes
NOTE_MAX = 200
# 2.4: the layers the dial may move. Emotion first, so a press with no "layer" field means emotion.
# Jordan (Nov 21): mood and baseline as buttons for testing, behind a lock, every change marked manual and
# tagged test or permanent. Taking a name out of HAND_LAYERS makes that layer read-only again.
HAND_LAYERS = ("emotion", "mood", "baseline")
LOCKED = ("mood", "baseline")               # move only while the lock is open
TAGS_BY_LAYER = {"mood": ("test",), "baseline": ("test", "calibration")}  # emotion takes no tag
UNLOCK_SECONDS = 600                        # the lock closes itself 10 minutes after it was opened
_lock = {"until": 0.0}                      # time.monotonic(); in memory, so a server restart locks it


def _hand_layers():
    """HAND_LAYERS checked: known layers only, emotion first, every locked layer has tags. A bad edit fails
    loudly (500 over HTTP), never widens the dial."""
    hand = tuple(HAND_LAYERS)
    if (not hand or hand[0] != emo.LAYER or len(set(hand)) != len(hand) or set(hand) - set(emo.LAYERS)
            or set(LOCKED) - set(emo.LAYERS) or emo.LAYER in LOCKED
            or any(not TAGS_BY_LAYER.get(n) or set(TAGS_BY_LAYER[n]) - {"test", "calibration"} for n in LOCKED)):
        raise RuntimeError("HAND_LAYERS must start with %r and name only %s; locked layers need tags"
                           % (emo.LAYER, ", ".join(emo.LAYERS)))
    return hand


def _seconds_left():
    return max(0, int(round(_lock["until"] - time.monotonic())))


def lock_view():
    left = _seconds_left()
    return {"locked": left == 0, "seconds_left": left, "layers": list(LOCKED)}


def _rest():
    """Her baseline as (v, a, d), for label(rest=) (2.3: her normal reads neutral)."""
    b = emo.read(layer="baseline")
    return b["v"], b["a"], b["d"]


def _view(state, rest=None):
    """A state row plus the word and intensity label() gives it, measured against her baseline (rest)."""
    lab = emo.label(state["v"], state["a"], state["d"], rest=rest)
    return {"v": state["v"], "a": state["a"], "d": state["d"], "updated": state["updated"],
            "label": lab["name"], "intensity": lab["intensity"], "magnitude": round(lab["magnitude"], 3)}


def get_reply():
    """What GET returns. "state" is emotion, as in Step 1 (app v55.0 reads it). "layers" has all three rows,
    read together, each with its label and "manual": the hand change standing on it (None, an open test or
    the latest calibration). "dial" says what moves, what needs the lock and which tags each layer takes;
    "lock" says whether it is open and for how long."""
    hand = _hand_layers()
    con = emo.db_open()
    try:
        rows = emo.read_all(con)
        b = rows["baseline"]
        rest = (b["v"], b["a"], b["d"])
        layers = {}
        for name in emo.LAYERS:
            layers[name] = _view(rows[name], None if name == "baseline" else rest)
            layers[name]["manual"] = emo.manual_marker(name, DOOR, con) if name in LOCKED else None
    finally:
        con.close()
    return {"ok": True, "state": _view(rows[emo.LAYER], rest), "layers": layers,
            "dial": {"moves": list(hand), "locked": [n for n in hand if n in LOCKED],
                     "read_only": [n for n in emo.LAYERS if n not in hand],
                     "tags": {n: list(TAGS_BY_LAYER[n]) for n in hand if n in LOCKED}},
            "lock": lock_view()}


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


def _bad(code, msg):
    return 400, {"ok": False, "code": code, "error": msg}


def _moved(r, layer, tag):
    rest = None if layer == "baseline" else _rest()
    return 200, {"ok": True, "old": _view(r["old"], rest), "new": _view(r["new"], rest), "clamped": r["clamped"],
                 "layer": layer, "tag": tag}


ACTIONS = ("set", "nudge", "end_test", "unlock", "lock")


def handle(body):
    """One button press, already parsed. Returns (status, reply dict). Kept apart from Starlette so the
    test can call it directly too.
      {"set": {v, a, d}} / {"nudge": {dv, da, dd}}, optional "layer" (emotion), "tag", "note"
      {"unlock": true} opens the lock for UNLOCK_SECONDS; {"lock": true} closes it
      {"end_test": "mood" | "baseline"}, optional "note": back to where the layer was before its open test
        (allowed while locked: it only undoes a test)"""
    hand = _hand_layers()
    if isinstance(body, dict) and len(hand) == 1 and ({"layer", "tag", "end_test", "unlock", "lock"} & set(body)):
        # every hand layer switched off: the dial is emotion only, refused exactly as in 2.1
        return _bad("bad_request", "the dial moves emotion only; mood and baseline are read-only")
    if not isinstance(body, dict) or len(set(ACTIONS) & set(body)) != 1:
        return _bad("bad_request", "send exactly one of set, nudge, end_test, unlock or lock")
    action = (set(ACTIONS) & set(body)).pop()

    if action in ("unlock", "lock"):
        if set(body) != {action} or body[action] is not True:
            return _bad("bad_request", "send {\"%s\": true} on its own" % action)
        _lock["until"] = time.monotonic() + UNLOCK_SECONDS if action == "unlock" else 0.0
        return 200, {"ok": True, "lock": lock_view()}

    if action == "end_test":
        layer = body["end_test"]
        if set(body) - {"end_test", "note"}:
            return _bad("bad_request", "end_test takes only a layer and an optional note")
        if not isinstance(layer, str) or layer not in hand or layer not in LOCKED:
            return _bad("bad_layer", "end_test takes %s" % " or ".join(n for n in hand if n in LOCKED))
        try:
            r = emo.end_test(layer, door=DOOR, note=_note(body))
        except emo.EmotionError as e:
            return _bad(e.code, str(e))
        return _moved(r, layer, "test_end")

    if set(body) - {action, "note", "layer", "tag"}:
        return _bad("bad_request", "%s takes only layer, tag and note beside it" % action)
    layer = body.get("layer", emo.LAYER)
    if not isinstance(layer, str) or layer not in emo.LAYERS:
        return _bad("bad_layer", "layer must be one of %s" % ", ".join(hand))
    if layer not in hand:
        return _bad("read_only", "the dial can't move %s" % layer)
    tag = body.get("tag")
    if layer in LOCKED:
        if _seconds_left() == 0:
            return _bad("locked", "%s is locked; unlock it first" % layer)
        if tag is None:
            return _bad("tag_needed", "a %s change needs a tag: %s" % (layer, " or ".join(TAGS_BY_LAYER[layer])))
        if tag not in TAGS_BY_LAYER[layer]:
            return _bad("bad_tag", "%s takes the tag %s" % (layer, " or ".join(TAGS_BY_LAYER[layer])))
    elif tag is not None:
        return _bad("bad_tag", "%s takes no tag" % layer)
    cause = CAUSE if tag is None else "%s %s" % (CAUSE, tag)
    try:
        note = _note(body)
        if action == "set":
            v, a, d = _nums(body["set"], ("v", "a", "d"), "set")
            if not set(body["set"]) >= {"v", "a", "d"}:
                raise emo.EmotionError("bad_value", "set needs v, a and d")
            r = emo.write(v, a, d, cause=cause, note=note, door=DOOR, layer=layer, tag=tag)
        else:
            dv, da, dd = _nums(body["nudge"], ("dv", "da", "dd"), "nudge")
            r = emo.nudge(dv, da, dd, cause=cause, note=note, door=DOOR, layer=layer, tag=tag)
    except emo.EmotionError as e:
        return _bad(e.code, str(e))
    return _moved(r, layer, tag)


async def emotion(request: Request):
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
    except Exception as e:  # noqa: BLE001  (a broken emotion.db must not take the server down)
        return _err(500, "internal", type(e).__name__)


def routes():
    """The routes rivia_auth.build_app puts ahead of the /app mount (rivia_auth.patch)."""
    # Every method lands here (405 for the rest) so none falls through to the /app MCP mount.
    return [Route(PATH, emotion, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])]
