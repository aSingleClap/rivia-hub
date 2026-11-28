# rivia_line_wait.py: lets her mood line wait for the Flash read of the message Jordan just typed. DRAFT, Nov 28,
# not installed.
#
# Jordan (Nov 28): "Lets make this change", on the suggestion that the app fetch her mood line after the tablet's
# Flash read of his message, not before. On Nov 27 the app asked for the line while Flash was still reading, so a
# Step 3 push landed about 7 seconds after the reply had gone out and Gemini never heard it.
#
# How it works:
#   - The app sends its copy of each typed message to /app/trust/words with a message_id, then asks for the line with
#     GET /app/emotion/line?after=<that message_id>.
#   - When Step 7's read of that message (the Flash read, and Step 3's push inside it) has finished, the words route's
#     background job calls mark_done(message_id). The line route waits for that mark, at most WAIT_S, then builds
#     the line as it stands. A slow or failed Flash read never holds her reply up longer than WAIT_S.
#   - Nothing is waited for when Layer 3, Layer 7 or Step 7's Flash read (7.1) is switched off, because then no push
#     can come from his message.
#
# Memory only: message ids and times, nothing else, nothing written to disk, kept KEEP_S seconds. A tablet restart
# forgets them, which only means one reply's line doesn't wait. Never an MCP tool.
import asyncio
import threading
import time

WAIT_S = 3.5      # the longest the line waits for the read; after that it goes out as it stands
KEEP_S = 120      # how long a "read finished" mark is kept
POLL_S = 0.05     # how often the waiting line checks for the mark
ID_MAX = 80       # the words route takes message ids up to 80 characters

_lock = threading.Lock()
_done = {}        # message_id -> when its read finished


def _prune(now):
    for mid in [m for m, at in _done.items() if now - at > KEEP_S]:
        del _done[mid]


def mark_done(message_id, now=None):
    """The words route's background job: this message's read (Flash, Step 3's push) is over. Never raises."""
    try:
        if not isinstance(message_id, str) or not message_id or len(message_id) > ID_MAX:
            return
        now = time.time() if now is None else now
        with _lock:
            _prune(now)
            _done[message_id] = now
    except Exception:  # noqa: BLE001
        pass


def is_done(message_id):
    with _lock:
        return message_id in _done


def should_wait():
    """True when a push can come from reading his message: Layers 3 and 7 and Step 7's Flash read are all on."""
    try:
        import rivia_toggles as toggles
        return all(toggles.runs(p) for p in ("l3", "l7", "l7.1.flash"))
    except Exception:  # noqa: BLE001
        return True


async def wait_for(message_id, timeout=None):
    """Waits until message_id's read has finished, at most timeout (default WAIT_S). Returns (read, waited_ms):
    read is "done", "timeout", or "skipped" (bad id, or nothing can push). Never raises."""
    start = time.monotonic()
    try:
        timeout = WAIT_S if timeout is None else max(0.0, min(float(timeout), WAIT_S))
        if not isinstance(message_id, str) or not message_id or len(message_id) > ID_MAX or not should_wait():
            return "skipped", 0
        while True:
            if is_done(message_id):
                return "done", int((time.monotonic() - start) * 1000)
            if time.monotonic() - start >= timeout:
                return "timeout", int((time.monotonic() - start) * 1000)
            await asyncio.sleep(POLL_S)
    except Exception:  # noqa: BLE001
        return "skipped", int((time.monotonic() - start) * 1000)
