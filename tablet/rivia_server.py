# rivia_server.py: RIVIA's MCP server on the tablet.
#
# Two doors, same OAuth gate (rivia_auth.py):
#   /mcp      for Grok bots (OAuth sign-in): get_tablet_battery, post_reply
#   /app/mcp  for the Rivia app (token from `python rivia_auth.py mint-app-token`):
#             get_tablet_battery, ask_bot, check_reply, plus scheduler tools from the
#             tool table (step 2: ride_time). The bot door gets no scheduler tools.
# The gate refuses an app token on /mcp and a bot token on /app/mcp, so the app can't
# fake a bot's answer and a bot can't start other bots.
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastmcp import FastMCP

import rivia_bots

FAIL_RULE = ("If a tool returns ok=false, report the failure as ok=false and stop that task. "
             "Never substitute data from another device, an older answer or a guess.")

mcp = FastMCP(
    "rivia",
    instructions=("RIVIA's tool server for Jordan's Grok bots. It runs on his Galaxy Tab S7+ at home. "
                  "When a request came in with a ticket, finish by calling post_reply with that ticket "
                  "and your JSON reply. " + FAIL_RULE),
)

app_mcp = FastMCP(
    "rivia-app",
    instructions=("Tools for Rivia, Jordan's assistant. The bots are parts of Rivia, not other people: "
                  "whatever a bot does, Rivia did. Speak to Jordan in first person ('I'm checking "
                  "tomorrow now') and never mention bots, Ard, tickets or tools. ask_bot starts a slow "
                  "job and returns at once; the answer comes later and the app picks it up with "
                  "check_reply. Never wait for or invent an answer. " + FAIL_RULE),
)

PS = Path("/sys/class/power_supply/battery")


def get_tablet_battery() -> dict:
    """Battery level and charging state of the Galaxy Tab S7+ that hosts RIVIA.
    This is NOT the phone the user is talking from; use it whenever they ask about the tablet."""
    try:
        level = int((PS / "capacity").read_text().strip())
    except (OSError, ValueError) as e:
        return {"ok": False, "device": "Galaxy Tab S7+",
                "error": f"Tablet battery could not be read ({type(e).__name__}). "
                         "Report this failure. Do NOT substitute phone or other data."}
    try:
        status = (PS / "status").read_text().strip()
    except OSError:
        status = "unknown"
    return {"ok": True, "device": "Galaxy Tab S7+", "battery_percent": level, "status": status}


mcp.tool()(get_tablet_battery)
app_mcp.tool()(get_tablet_battery)


@mcp.tool()
def post_reply(ticket: str, reply: dict | str) -> dict:
    """Deliver your answer to a request that came from Rivia. Call this once, as the last step,
    with the ticket from the request body exactly as given and your full JSON reply envelope
    as reply. A reply can't be changed after it's stored."""
    return rivia_bots.post_reply(ticket, reply)


@app_mcp.tool(description=(
    "Start a slow job on one of your own parts (a bot). Returns at once with a ticket; the answer "
    "comes later, usually in under a minute. Don't wait and don't guess the answer. Tell Jordan in "
    "first person that you're on it ('Checking now'), never naming the bot, and carry on. Put "
    "everything the job needs in request, in plain words. Parts: "
    + rivia_bots.bot_list_text()))
async def ask_bot(bot: str, request: str) -> dict:
    return await rivia_bots.ask_bot(bot, request)


@app_mcp.tool()
def check_reply(ticket: str) -> dict:
    """The answer for a ticket from ask_bot. status pending: not done yet, keep waiting.
    answered: reply holds the JSON result; speak from that only, in first person. ok false
    (failed or expired): tell Jordan you couldn't get it done; never make up the answer."""
    return rivia_bots.check_reply(ticket)


def public_app():
    """Both doors behind the OAuth layer. This is what the tunnel points at."""
    import rivia_auth
    import rivia_tools
    # Register row-built scheduler tools on the app door only, before http_app snapshots them.
    import asyncio
    import rivia_gcal as gcal
    from contextlib import suppress
    rivia_tools.install_on(app_mcp, door="app")
    bot_inner = mcp.http_app(path="/mcp")
    # Mounted under /app, so served at /app/mcp. Stateless + plain JSON: the phone sends one
    # POST per call (no session, no event stream), which keeps the app's client tiny.
    app_inner = app_mcp.http_app(path="/mcp", stateless_http=True, json_response=True)

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(bot_inner.lifespan(app))
            await stack.enter_async_context(app_inner.lifespan(app))
            # G1-style daily timer (05:30 ET): Google sign-in check. The later
            # 7b auto-write job can reuse run_signin_check / maybe_notify_signin_expired.
            task = None
            if gcal.signin_check_enabled():
                task = asyncio.create_task(gcal.signin_check_loop(), name="rivia-signin-check")
            try:
                yield
            finally:
                if task is not None:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    return rivia_auth.build_app(bot_inner, lifespan=lifespan, app_mcp=app_inner)


if __name__ == "__main__":
    if "--public" in sys.argv:
        import uvicorn
        print("rivia server starting (OAuth, http 127.0.0.1:8787). Ctrl+C to stop.", file=sys.stderr)
        print("bots: " + (", ".join(rivia_bots.bots()) or "none configured"), file=sys.stderr)
        uvicorn.run(public_app(), host="127.0.0.1", port=8787, proxy_headers=True)
    elif "--http" in sys.argv:
        print("rivia server starting (http :8000, NO AUTH, bot door only). Local only; never with the tunnel up.",
              file=sys.stderr)
        mcp.run(transport="http", host="127.0.0.1", port=8000)
    else:
        print("rivia server starting (stdio, bot door). Ctrl+C to stop.", file=sys.stderr)
        mcp.run(transport="stdio")
