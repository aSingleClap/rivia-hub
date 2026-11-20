# rivia_battery.py: the tablet's battery, a small part of its own (moved out of core in the
# core split). get_tablet_battery is a fixed tool on the app door; the hub reads read_battery().
from pathlib import Path

PS = Path("/sys/class/power_supply/battery")
DEVICE = "Galaxy Tab S7+"


def read_battery():
    """{battery_percent, status}, or None if the battery can't be read."""
    try:
        level = int((PS / "capacity").read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        status = (PS / "status").read_text().strip()
    except OSError:
        status = "unknown"
    return {"battery_percent": level, "status": status}


def get_tablet_battery() -> dict:
    """Battery level and charging state of the Galaxy Tab S7+ that hosts RIVIA.
    This is NOT the phone the user is talking from; use it whenever they ask about the tablet."""
    try:
        level = int((PS / "capacity").read_text().strip())
    except (OSError, ValueError) as e:
        return {"ok": False, "device": DEVICE,
                "error": f"Tablet battery could not be read ({type(e).__name__}). "
                         "Report this failure. Do NOT substitute phone or other data."}
    try:
        status = (PS / "status").read_text().strip()
    except OSError:
        status = "unknown"
    return {"ok": True, "device": DEVICE, "battery_percent": level, "status": status}


def install_on(mcp):
    mcp.tool()(get_tablet_battery)
