# rivia_reading_flash.py: the Flash read of each message Jordan types (emotion spec, Step 7.1 and 7.9). DRAFT, Nov 24,
# not installed.
#
# Jordan (Nov 24): "Why not a word list and flash read", "We could just use flash for now", "Every message",
# "lets do the tablet". So the tablet asks Gemini 3.8 Flash to score how each typed message reads (pleasant, charged,
# in control: V/A/D), beside the word list. For a personal message (7.9) it also says whether there's another way to
# read it, which is a reason she'd ask rather than assume.
#
# How it runs:
#   - After /app/trust/words has answered the phone, in the background. The phone never waits on Gemini.
#   - His text is held in memory for this one call and then dropped. It is sent to Google (the same Google project
#     his Rivia chats already go to) with store=false. Only the three scores and the yes/no come back into body.db.
#   - Through Step 5.5's tracking: each call is logged in model_calls, so it shows on Layer 5's graphs and counts
#     toward the limits Jordan set there. If 3.8 Flash is at a limit, this message gets the word list only.
#   - The key: GEMINI_API_KEY in ~/.rivia/.env (rivia.sh loads it; the Drive backup's secrets archive covers that
#     folder). No key = no Flash read, said plainly as "no_key". This file never stores, makes or prints a key. The key
#     goes on the tablet at install, on Jordan's yes.
#
# Step 3.4 (DRAFT Nov 24, Jordan: "Split"): the same call also lists which events the message is (thanked her,
# corrected her, frustrated at something, cursed at her, personal, good news, bad news) with a strength 1 to 3. Only
# the names and strengths go to rivia_appraisal, which moves a would-be copy of her mood (shadow). No extra call.
#
# Never an MCP tool. Never emotion.db. Rivia never sees these scores in shadow.
import os
import sys
import time

import baseline
import rivia_reading as reading

MODEL = "gemini-3.8-flash"
TEXT_MAX = 4000

RATER = (
    "You read the tone of one short chat message a man named Jordan typed to his AI assistant. "
    "Rate how the message reads, not the topic: a calm message about bad news is calm. "
    "Jordan writes bluntly and briefly as his normal style; short or blunt alone is not upset. "
    "Give three integers from -3 to 3.\n"
    "valence: -3 very unpleasant (upset, hostile, gloomy) to 3 very pleasant (warm, cheerful). 0 neutral.\n"
    "arousal: -3 very calm, flat or tired to 3 very excited, urgent or agitated. 0 ordinary.\n"
    "dominance: -3 very unsure or overwhelmed to 3 very confident and in control. 0 balanced.\n"
    "other_reading: true only if a careful listener could reasonably read this message's feeling a clearly "
    "different way; otherwise false.\n"
)
RATER_PLAIN = RATER + "Answer with JSON only."   # Step 7's prompt without Step 3's event menu (Layer 3 switched off)
try:  # Step 3.4: the event menu rides in the same call. If Step 3's file is missing, Step 7 reads as before.
    import rivia_appraisal as _appraisal
    RATER += _appraisal.FLASH_EVENTS
except Exception:  # noqa: BLE001
    _appraisal = None
RATER += "Answer with JSON only."
SCHEMA = {
    "type": "object",
    "properties": {**{ax: {"type": "integer", "minimum": -3, "maximum": 3}
                      for ax in ("valence", "arousal", "dominance")},
                   "other_reading": {"type": "boolean"}},
    "required": ["valence", "arousal", "dominance", "other_reading"],
}
SCHEMA_PLAIN = {**SCHEMA, "properties": dict(SCHEMA["properties"])}
if _appraisal is not None:  # not required: an answer without events still gives Step 7 its scores
    SCHEMA["properties"]["events"] = _appraisal.FLASH_EVENTS_SCHEMA


def events_on():
    """Step 3's event menu rides in this call only while Layer 3 (l3) is switched on. Never raises."""
    if _appraisal is None:
        return False
    try:
        import rivia_toggles as toggles
        return toggles.runs("l3")
    except Exception:  # noqa: BLE001
        return True

STATUSES = ("ok", "no_key", "at_limit", "off", "failed", "unusable", "gone")


def key():
    """The Gemini key from the environment, or None. Never logged."""
    k = os.environ.get("GEMINI_API_KEY", "").strip()
    return k or None


def body(text, model=MODEL, events=None):
    """The Flash request. events (default: Layer 3's switch) decides whether Step 3's event menu rides along."""
    events = events_on() if events is None else events
    return {"model": model, "system_instruction": RATER if events else RATER_PLAIN,
            "input": [{"type": "user_input", "content": "Message to rate:\n<<<\n" + text[:TEXT_MAX] + "\n>>>"}],
            "store": False,
            "response_format": {"type": "text", "mime_type": "application/json",
                                "schema": SCHEMA if events else SCHEMA_PLAIN},
            "generation_config": {}}   # model default thinking, as the baseline rater


def parse(text):
    """Scores (-1..+1) and the other-reading yes/no, or None if the answer can't be used (counted, never guessed)."""
    vad = baseline.parse_rating(text or "")
    if vad is None:
        return None, None
    import json
    import re
    try:
        m = re.search(r"\{.*\}", text, re.S)
        other = json.loads(m.group(0) if m else text).get("other_reading")
    except Exception:  # noqa: BLE001
        other = None
    return vad, other if isinstance(other, bool) else None


def at_limit(con, now):
    """True when 3.8 Flash is at one of the limits Jordan set on Layer 5 (5.5). Unset limits never block."""
    try:
        import rivia_model_limits as ml
        import rivia_usage as usage
        calls = usage.load_calls(con, now - 86400 * 2)
        table, _ = ml.current_table()
        return ml.usage(calls, table, now).get(MODEL, {}).get("worst") == "at"
    except Exception as e:  # noqa: BLE001  (no limits table: nothing blocks)
        print("rivia: reading couldn't check model limits (%s)" % type(e).__name__, file=sys.stderr)
        return False


def _tokens(res):
    u = res.get("usage") or {}
    return (int(u.get("total_input_tokens") or 0),
            int(u.get("total_output_tokens") or 0) + int(u.get("total_thought_tokens") or 0))


def read(message_id, text, con=None, now=None, tune=None):
    """Score one message with Flash and store the result. Returns the status. Never raises; never keeps the text."""
    now = time.time() if now is None else now
    con, own = reading._open(con)
    try:
        t = reading._tune(tune)
        if reading.message(con, message_id) is None:
            return "gone"
        raw = None
        if not t.get("flash_on", 1):
            status, vad, other = "off", None, None
        elif key() is None:
            status, vad, other = "no_key", None, None
        elif at_limit(con, now):
            status, vad, other = "at_limit", None, None
        else:
            try:
                res = baseline.interaction(body(text), key())
                tin, tout = _tokens(res)
                import rivia_usage as usage
                usage.record_call(con, MODEL, time.time(), input_tokens=tin, output_tokens=tout)
                raw = baseline.output_text(res)
                vad, other = parse(raw)
                status = "ok" if vad else "unusable"
            except Exception as e:  # noqa: BLE001  (network down, Google error: word list only for this one)
                print("rivia: Flash read failed (%s)" % type(e).__name__, file=sys.stderr)
                status, vad, other = "failed", None, None
        reading.store_flash(con, message_id, vad, status, other, now=now, tune=t)
        if _appraisal is not None and events_on():   # Step 3.4: never raises; Layer 3 off: no message events
            _appraisal.from_flash(con, message_id, status, raw, now=now)
        raw = None  # noqa: F841  (dropped)
        return status
    except Exception as e:  # noqa: BLE001  (never take the words copy down)
        print("rivia: reading's Flash step failed (%s)" % type(e).__name__, file=sys.stderr)
        return "failed"
    finally:
        text = None  # noqa: F841  (dropped)
        if own:
            con.close()
