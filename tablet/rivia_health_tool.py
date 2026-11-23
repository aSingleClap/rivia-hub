# rivia_health_tool.py: the one read-only status lookup (emotion spec, Step 4.8). Draft, Nov 23, not installed.
#
# Jordan, Nov 22: "Let's keep it internal I still want to be able to check on the status of things if I ask her".
# The readers stay internal (rivia_health.py). This is the single way she can see them: when Jordan asks how things
# are, Gemini calls health_status and gets the collector's last saved reading back, as numbers and short codes.
#
# Why it's safe to hand her:
#   - Read-only, no arguments: nothing to get wrong, nothing it can change.
#   - No network call. A down tunnel or a slow Google can't make the lookup fail, so looking can't count against her.
#   - Always ok. A source it can't read comes back "unknown" or None, never an error.
#   - No log text. Strangers' scan traffic in server.log never reaches her prompt.
#
# NOT wired into the server. 4.9 adds `install_on(app_mcp)` in rivia_server.build_doors(), and the app has to let
# Gemini see the tool (the app decides which app-door tools Gemini gets). See README.
import rivia_health as health

DESCRIPTION = ("Rivia's current health: server uptime, restarts, errors, tool failures, response time, tablet "
               "temperature, memory, storage, battery, the Cloudflare tunnel and Tailscale, plus the last few "
               "failed tool calls. Read-only. Use it when Jordan asks how the server or tablet is doing. "
               "'reading' says whether the numbers are fresh (under 5 minutes old) or stale.")


def health_status() -> dict:
    """Read-only. The tablet's last saved health reading. Takes no arguments."""
    try:
        summary = health.status_summary()
    except Exception:  # noqa: BLE001  (status_summary never raises; this is belt and braces)
        summary = {"source": "code", "reading": "none", "tunnel": "unknown"}
    return {"ok": True, **summary}


health_status.__doc__ = DESCRIPTION


def install_on(mcp, door="app", log_fixed=None):
    """Put health_status on a door. log_fixed is rivia_tools.log_fixed (4.1), so the call goes in the call log like
    the other fixed tools; 4.9 keeps these rows out of 4.1's tool numbers (README)."""
    fn = log_fixed(health_status, door, health.LOOKUP_TOOL) if log_fixed else health_status
    mcp.tool(name=health.LOOKUP_TOOL, description=DESCRIPTION)(fn)
