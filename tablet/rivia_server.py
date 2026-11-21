# rivia_server.py: RIVIA's MCP server on the tablet (core).
#
# Two doors behind the bearer gate (rivia_auth.py):
#   /app/mcp    the Rivia app (app token: `rivia token`, or POST /app/login with Google):
#               get_tablet_battery (rivia_battery), the read-only hub tools hub_status /
#               list_tools / list_calls, and the tool-table rows: places tools (always) and
#               scheduling tools (when scheduling loaded).
#   /admin/mcp  the Server Hub (token from `rivia admin-token`): fixed admin tools, not rows.
#   /app/emotion  the app's emotion button (rivia_emotion_app): plain HTTP, app token, not a tool.
# The Grok bot door (/mcp, ask_bot / check_reply / post_reply) was removed in the core split;
# git tag oauth-bot-archive keeps it.
#
# Core never depends on scheduling: it is imported in a try, and if it fails (or its planner
# selftest fails) core and places keep serving.
import asyncio
import sys
import traceback
from contextlib import AsyncExitStack, asynccontextmanager, suppress

from fastmcp import FastMCP

import rivia_tools as tools

app_mcp = FastMCP(
    "rivia-app",
    instructions=("Tools for Rivia, Jordan's assistant. Speak to Jordan in first person and never "
                  "mention tools. " + tools.FAIL_RULE),
)

admin_mcp = FastMCP(
    "rivia-admin",
    instructions=("Admin tools for the Rivia Server Hub on the tablet. Fixed tools, not rows. "
                  "Standing settings applied here take effect at once. " + tools.FAIL_RULE),
)

SCHEDULING = {"loaded": False, "error": None}


def load_parts():
    """Places always (it ships with core); scheduling if it loads. Returns the scheduling module or None."""
    import rivia_places  # noqa: F401  registers app_places, app_pin_place, app_arrival, app_open, app_log_ride
    try:
        import rivia_sched
    except Exception as e:  # noqa: BLE001
        SCHEDULING.update(loaded=False, error=type(e).__name__)
        print("rivia: scheduling did not load (%s); core and places keep running." % type(e).__name__,
              file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return None
    SCHEDULING.update(loaded=True, error=None)
    try:
        # Run the planner selftest once at startup (cached; scheduling tools check it). A failure
        # only blocks scheduling's own tools, never core or places.
        if not rivia_sched.selftest_ok():
            print("rivia: planner selftest failed; scheduling tools are blocked, core and places keep running.",
                  file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print("rivia: planner selftest crashed (%s)." % type(e).__name__, file=sys.stderr)
    return rivia_sched


def build_doors():
    """Register every tool on the two FastMCP apps. Returns the scheduling module or None."""
    import rivia_admin
    import rivia_battery
    sched = load_parts()
    rivia_battery.install_on(app_mcp)
    rivia_admin.install_read_only_on(app_mcp)
    tools.install_on(app_mcp, door="app")
    rivia_admin.install_on(admin_mcp)
    return sched


def public_app():
    """Both doors behind the bearer gate. This is what the tunnel points at."""
    import rivia_auth
    sched = build_doors()
    # Mounted under /app and /admin, so served at /app/mcp and /admin/mcp. Stateless + plain JSON:
    # one POST per call (no session, no event stream), which keeps the clients tiny.
    app_inner = app_mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    admin_inner = admin_mcp.http_app(path="/mcp", stateless_http=True, json_response=True)

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(app_inner.lifespan(app))
            await stack.enter_async_context(admin_inner.lifespan(app))
            # The daily timer (05:30 ET morning tick, 21:00 evening refresh) belongs to scheduling.
            task = None
            if sched is not None:
                import rivia_gcal as gcal
                if gcal.signin_check_enabled():
                    task = asyncio.create_task(gcal.signin_check_loop(), name="rivia-signin-check")
            try:
                yield
            finally:
                if task is not None:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    return rivia_auth.build_app(lifespan=lifespan, app_mcp=app_inner, admin_mcp=admin_inner,
                                extra_routes=emotion_routes())


def emotion_routes():
    """The app's emotion button (/app/emotion, emotion spec Step 1.5): a plain route behind the app
    token, never an MCP tool, so it is never in Gemini's tool list. If it fails to load, the server
    keeps serving without it."""
    try:
        import rivia_emotion_app
        return rivia_emotion_app.routes()
    except Exception as e:  # noqa: BLE001
        print("rivia: emotion button did not load (%s); everything else keeps running." % type(e).__name__,
              file=sys.stderr)
        return []


if __name__ == "__main__":
    import uvicorn
    port = 8787
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    print("rivia server starting (doors /app/mcp and /admin/mcp, http 127.0.0.1:%d). Ctrl+C to stop." % port,
          file=sys.stderr)
    uvicorn.run(public_app(), host="127.0.0.1", port=port, proxy_headers=True)
