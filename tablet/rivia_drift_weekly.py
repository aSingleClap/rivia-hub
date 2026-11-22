# rivia_drift_weekly.py: the weekly Gemini drift check on the tablet (emotion spec Step 2.8). DRAFT.
#
# Jordan's pick, Nov 22: "Weekly tablet job check".
# Once a week (Sunday, after the 05:30 ET morning tick; catches up if the tablet was off), it:
#   1. fetches Rivia's model config (engine.json and the three prompt layers) from the app repo on
#      GitHub, or reads RIVIA_APP_CONFIG if that points at a local copy,
#   2. fetches Google's public Gemini API changelog page (free; not the Gemini API),
#   3. runs drift.watch against the reference runs in ~/rivia/drift/history (everyday Hydrogen and
#      deep Neon under Jordan's Nov 22 plan),
#   4. writes what it found to ~/rivia/drift/last_check.txt (`rivia drift` prints it), and posts ONE
#      Android notification when something new needs Jordan: a model change worth a rerun, a change
#      to her system instruction or the prompt set, or the check failing to run.
#
# It never makes a Gemini call, never runs a rerun, never changes a reference or her baseline.
# The notification never quotes the changelog page (someone else's text); details stay in the file.
# Off unless RIVIA_DRIFT_WEEKLY=1, so installing the file changes nothing until Jordan turns it on.
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import rivia_drift as D  # name at install
except ImportError:  # the drafts folder
    import drift as D

TZ = ZoneInfo("America/New_York")
RUN_DAY = 6                 # Sunday (Monday = 0)
RUN_AT = dtime(5, 30)       # after the morning tick
APP_RAW = os.environ.get(
    "RIVIA_APP_CONFIG_URL",
    "https://raw.githubusercontent.com/aSingleClap/RIVIA-Official-App-/main/app/src/main/assets/config/")
CONFIG_FILES = ("engine.json", "persona.json", "style.json", "hard_rules.json")
NOTIFY_TITLE = "Rivia drift check"
NOTIFY_KINDS = ("engine", "changelog", "system", "prompts")   # pending/unmeasured notes only go in the file


def enabled() -> bool:
    return os.environ.get("RIVIA_DRIFT_WEEKLY", "") == "1"


def base_dir() -> Path:
    try:
        import rivia_store as store
        root = store.rivia_dir()
    except Exception:  # noqa: BLE001  (drafts / tests)
        root = os.path.expanduser(os.environ.get("RIVIA_DIR", "~/rivia"))
    return Path(root) / "drift"


def _state_path() -> Path:
    return base_dir() / "weekly_state.json"


def load_state() -> dict:
    p = _state_path()
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict):
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(p)


def last_due(now: datetime) -> datetime:
    """The most recent Sunday 05:30 ET at or before now."""
    now = now.astimezone(TZ)
    d = now.date() - timedelta(days=(now.weekday() - RUN_DAY) % 7)
    slot = datetime.combine(d, RUN_AT, TZ)
    if slot > now:
        slot -= timedelta(days=7)
    return slot


def weekly_due(now: datetime, state: dict | None = None) -> bool:
    """Due once per week: when this week's Sunday 05:30 slot hasn't been run yet.
    A tablet that was off on Sunday catches up the next time the loop ticks."""
    state = load_state() if state is None else state
    last = state.get("last_run")
    if not last:
        return True
    return datetime.fromisoformat(last) < last_due(now)


def _get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "rivia-drift-check"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def fetch_config(dest: Path, get=_get) -> Path:
    """Local copy if RIVIA_APP_CONFIG is set, else the four files from the app repo."""
    local = os.environ.get("RIVIA_APP_CONFIG")
    if local:
        return Path(local)
    dest.mkdir(parents=True, exist_ok=True)
    for name in CONFIG_FILES:
        text = get(APP_RAW + name)
        json.loads(text)  # refuse a non-JSON answer (an error page) rather than compare against it
        (dest / name).write_text(text)
    return dest


def fetch_changelog(get=_get) -> str:
    return D.changelog_text_from_html(get(D.CHANGELOG_URL))


def _notify_default(content: str) -> dict:
    try:
        import rivia_gcal as gcal
        return gcal.post_termux_notification(content, title=NOTIFY_TITLE)
    except Exception as e:  # noqa: BLE001
        print("rivia: drift notification failed (%s); continuing." % type(e).__name__, file=sys.stderr)
        return {"ok": False, "reason": "error"}


def summary_text(now: datetime, findings: list[dict], targets: list[dict], plan: list[str]) -> str:
    lines = [f"Rivia drift check, {now.astimezone(TZ):%a %b %d %Y %H:%M} ET. Nothing was run or changed."]
    real = [f for f in findings if f["kind"] != "no_reference"]
    if not real:
        lines.append("No change since the reference runs." if findings == [] else findings[0]["text"])
    for f in real:
        lines.append("- " + f["text"])
    lines += plan
    return "\n".join(lines) + "\n"


def notification(findings: list[dict], targets: list[dict]) -> str | None:
    kinds = {f["kind"] for f in findings}
    if targets:
        profs = ", ".join(t["profile"] for t in targets)
        return f"Gemini changed for Rivia's {profs}. A drift rerun is suggested; it needs your yes. Details: rivia drift"
    if "system" in kinds:
        return "Rivia's prompt changed, so her drift reference is out of date. Details: rivia drift"
    if "prompts" in kinds:
        return "The drift test prompts changed, so new runs won't compare. Details: rivia drift"
    return None


def run_weekly(now: datetime | None = None, *, get=_get, notify=_notify_default,
               history: Path | None = None, prompts: Path | None = None) -> dict:
    """One weekly check. Never raises; returns what it did (for tests and logs)."""
    now = now or datetime.now(TZ)
    base = base_dir()
    base.mkdir(parents=True, exist_ok=True)
    state = load_state()
    history = history or base / "history"
    prompts = prompts or D.BASELINE_DIR / "prompts.json"
    out = {"ran": now.isoformat(timespec="seconds"), "notified": None}
    try:
        cfg = fetch_config(base / "config", get=get)
        changelog = fetch_changelog(get=get)
        h = D.History(history)
        findings = D.watch(cfg, h, changelog, prompts)
        targets = D.rerun_targets(findings, h)
        plan = D.rerun_plan(targets, h, len(D.B.load_prompts(prompts)), "<app config folder>") if targets else []
    except Exception as e:  # noqa: BLE001
        reason = f"{type(e).__name__}: {str(e)[:160]}"
        (base / "last_check.txt").write_text(
            f"Rivia drift check, {now.astimezone(TZ):%a %b %d %Y %H:%M} ET: couldn't run ({reason}). "
            "Nothing was checked this week, so no news is NOT good news.\n")
        out.update(error=reason)
        # tell Jordan once per failure streak, so a broken check never passes for a quiet week
        if not state.get("failing"):
            out["notified"] = notify("The weekly drift check couldn't run. Details: rivia drift")
        state.update(last_run=now.isoformat(timespec="seconds"), failing=True)
        save_state(state)
        return out
    (base / "last_check.txt").write_text(summary_text(now, findings, targets, plan))
    sig = D.sha(json.dumps(sorted(f["text"] for f in findings)))
    is_new = sig != state.get("sig")
    msg = notification([f for f in findings if f["kind"] in NOTIFY_KINDS], targets)
    if is_new and msg:
        out["notified"] = notify(msg)
    out.update(findings=len(findings), targets=[t["profile"] for t in targets], new=is_new)
    state.update(last_run=now.isoformat(timespec="seconds"), sig=sig, failing=False)
    save_state(state)
    return out


def maybe_run(now: datetime | None = None) -> dict | None:
    """Called from the tablet's once-a-minute timer loop. Cheap when not due."""
    if not enabled():
        return None
    now = now or datetime.now(TZ)
    if not weekly_due(now):
        return None
    return run_weekly(now)


if __name__ == "__main__":
    # `rivia drift` prints the last result; `rivia drift now` runs the check immediately.
    if sys.argv[1:] == ["now"]:
        print(json.dumps(run_weekly(), indent=2))
    p = base_dir() / "last_check.txt"
    print(p.read_text() if p.exists() else "The weekly drift check hasn't run yet.")
