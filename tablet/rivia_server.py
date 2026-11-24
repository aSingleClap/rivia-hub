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
    # Step 4.8 (Jordan, Nov 22: "Let's keep it internal I still want to be able to check on the status of things if
    # I ask her"): one read-only lookup of the collector's last saved reading. No arguments, no network call.
    # Gemini sees it only once the app lets it through.
    import rivia_health_tool
    rivia_health_tool.install_on(app_mcp, log_fixed=tools.log_fixed)
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
            # Her body signals (emotion spec Step 4.2): a task of its own, started after the sign-in check and
            # never inside it, so a broken collector can't stop or delay the sign-in check.
            body_task = start_body_collector()
            try:
                yield
            finally:
                if task is not None:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                if body_task is not None:
                    body_task.cancel()
                    # A collector that already died must not break shutdown (its error was printed when it died).
                    with suppress(asyncio.CancelledError, Exception):
                        await body_task

    return rivia_auth.build_app(lifespan=lifespan, app_mcp=app_inner, admin_mcp=admin_inner,
                                extra_routes=emotion_routes() + progress_routes() + trust_routes() + style_routes()
                                + toggles_routes() + reading_routes())


def start_body_collector():
    """The minute loop (rivia_body_collect) as its own background task, or None when nothing it runs is allowed or it
    fails to load. Since the switches (Nov 24) the loop is split from Layer 4: it keeps ticking while Layer 4's reads
    (RIVIA_BODY_COLLECTOR), Step 5 (RIVIA_PLAN_SHADOW) or Step 6 (RIVIA_TRUST_SHADOW) may run, and each tick runs only
    the parts that are on. It writes only body.db and never imports the Google or sign-in code (its test checks); if
    it fails to load, the server and the sign-in check carry on without it. Jordan, Nov 22: "As long as the
    connector doesn't sign me out then that's fine"."""
    try:
        import rivia_body_collect
        if not rivia_body_collect.loop_enabled():
            return None
        t = asyncio.create_task(rivia_body_collect.loop(), name="rivia-body-collector")
        t.add_done_callback(_body_collector_ended)
        return t
    except Exception as e:  # noqa: BLE001
        print("rivia: body collector did not start (%s); everything else keeps running." % type(e).__name__,
              file=sys.stderr)
        return None


def _body_collector_ended(t):
    """If the collector's loop ever ends on an error, say so once in server.log. It isn't restarted until the next
    server start; the sign-in check and the doors are not affected."""
    if not t.cancelled() and t.exception() is not None:
        print("rivia: body collector stopped (%s); everything else keeps running." % type(t.exception()).__name__,
              file=sys.stderr)


def toggles_routes():
    """The layer and piece switches (/app/toggles, Nov 24: "Toggles first"). A plain route behind the app token, never
    an MCP tool, so Gemini can't see or call it. If it fails to load, the server keeps serving without it."""
    try:
        import rivia_toggles_app
        return [_logged_route(r, "app_toggles", "route.toggles") for r in rivia_toggles_app.routes()]
    except Exception as e:  # noqa: BLE001
        print("rivia: layer switches did not load (%s); everything else keeps running." % type(e).__name__,
              file=sys.stderr)
        return []


def emotion_routes():
    """The app's emotion button (/app/emotion, emotion spec Step 1.5): a plain route behind the app
    token, never an MCP tool, so it is never in Gemini's tool list. If it fails to load, the server
    keeps serving without it."""
    try:
        import rivia_emotion_app
        return [_logged_route(r) for r in rivia_emotion_app.routes()]
    except Exception as e:  # noqa: BLE001
        print("rivia: emotion button did not load (%s); everything else keeps running." % type(e).__name__,
              file=sys.stderr)
        return []



def progress_routes():
    """The app's layer 5 editor (/app/progress, emotion spec Step 5.7): Jordan adjusts Step 5's values (goals, scores,
    weights, mood pull), each at its own step. A plain route behind the app token like the emotion button, never an MCP tool, so Gemini can't see or
    call it. If it fails to load, the server keeps serving without it."""
    try:
        import rivia_progress_app
        return [_logged_route(r, "app_progress", "route.progress") for r in rivia_progress_app.routes()]
    except Exception as e:  # noqa: BLE001
        print("rivia: layer 5 editor did not load (%s); everything else keeps running." % type(e).__name__,
              file=sys.stderr)
        return []


def trust_routes():
    """The app's layer 6 card (/app/trust, emotion spec Step 6.9): trust, your satisfaction, thumbs on her messages,
    the unlocks you switch by hand, and the off switch. Plain routes behind the app token, never MCP tools, so Gemini
    can't see or call them. If they fail to load, the server keeps serving without them."""
    try:
        import rivia_trust_app
        return [_logged_route(r, "app_trust", "route.trust") for r in rivia_trust_app.routes()]
    except Exception as e:  # noqa: BLE001
        print("rivia: layer 6 card did not load (%s); everything else keeps running." % type(e).__name__,
              file=sys.stderr)
        return []


def reading_routes():
    """The app's layer 7 card (/app/reading, emotion spec Step 7.11, DRAFT Nov 24): how she reads you against your own
    normal, pattern guesses you confirm or reject, and the questions she would have asked (shadow). Plain routes
    behind the app token, never MCP tools, so Gemini can't see or call them. If they fail to load, the server keeps
    serving without them."""
    try:
        import rivia_reading_app
        return [_logged_route(r, "app_reading", "route.reading") for r in rivia_reading_app.routes()]
    except Exception as e:  # noqa: BLE001
        print("rivia: layer 7 card did not load (%s); everything else keeps running." % type(e).__name__,
              file=sys.stderr)
        return []


def style_routes():
    """The app's layer 8 card (/app/style, emotion spec Step 8.11, DRAFT Nov 24): how she would sound (shadow: her
    replies don't change yet), the steady-then-calmer plan when you're worked up, and the Layer 8 values. Plain routes
    behind the app token, never MCP tools, so Gemini can't see or call them. If they fail to load, the server keeps
    serving without them."""
    try:
        import rivia_style_app
        return [_logged_route(r, "app_style", "route.style") for r in rivia_style_app.routes()]
    except Exception as e:  # noqa: BLE001
        print("rivia: layer 8 card did not load (%s); everything else keeps running." % type(e).__name__,
              file=sys.stderr)
        return []


def _logged_route(route, tool="app_emotion", action="route.emotion"):
    """The emotion button's route, with each press timed in the call log (emotion spec Step 4.1, Jordan Nov 22:
    "They can be logged especially for response time and failures"): tool "app_emotion", door "app",
    action "route.emotion". Wrapped here in core, so rivia_emotion_app still imports no tool code (its Step 1
    guard). A 5xx logs "internal" (her failure); a 4xx logs "http_<status>" (a refused press: locked or bad
    input), which the body signals don't count as hers. The call log is not the tool table: nothing is added to
    rivia_tools.ACTIONS or tools/list. Logging never changes the reply."""
    import time
    from starlette.routing import Route
    inner = route.endpoint

    async def endpoint(request):
        t0 = time.perf_counter()
        resp = None
        try:
            resp = await inner(request)
            return resp
        finally:
            try:
                status = getattr(resp, "status_code", 500)
                ok = status < 400
                code = None if ok else ("internal" if status >= 500 else "http_%d" % status)
                tools.log_call("app", tool, action, ok, code,
                               int((time.perf_counter() - t0) * 1000))
            except Exception:  # noqa: BLE001
                pass

    return Route(route.path, endpoint, methods=sorted(route.methods or ()), name=route.name)

if __name__ == "__main__":
    import uvicorn
    port = 8787
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    print("rivia server starting (doors /app/mcp and /admin/mcp, http 127.0.0.1:%d). Ctrl+C to stop." % port,
          file=sys.stderr)
    uvicorn.run(public_app(), host="127.0.0.1", port=port, proxy_headers=True)
