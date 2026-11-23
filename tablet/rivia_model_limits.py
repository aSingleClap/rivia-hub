# rivia_model_limits.py: is Rivia near a Gemini model's real rate limits? (emotion spec, Step 5.5). DRAFT, Nov 23,
# not installed.
#
# Jordan, Nov 23, 12:27 PM: "Id like the rpm and red limits to be put in there for each model so I know if im hitting
# the models actual limits." Read as RPM and RPD.
#
# Google's terms (rate-limits page, read Nov 23, https://ai.google.dev/gemini-api/docs/rate-limits):
#   RPM  requests per minute
#   TPM  tokens per minute, input tokens
#   RPD  requests per day; "quotas reset at midnight Pacific time"
# "Rate limits are applied per project, not per API key", and they depend on the usage tier. The page lists no
# per-model numbers: it sends you to AI Studio's rate-limit page for your own project. So the numbers start empty
# ("not set") and Jordan types his own in from that page on the app card (Step 5.7). This file reads them from
# 5.7's settings store and keeps no copy. Nothing is guessed.
#
# This file is a gauge, not a brake. It never stops, slows or retries a call; it only reports how close each model is
# to its limits. The goal limits' own 10-calls-a-minute guard (rivia_goal_limits) is separate and stays.
# Pure functions, standard library only, apart from current_table(), which reads 5.7's settings.
# The call list is stored by rivia_usage.py (model_calls table in body.db).
import math
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:          # no tz database on the device: fall back to a fixed UTC-8 (off by an hour in summer)
    PACIFIC = timezone(timedelta(hours=-8))

# The models Rivia uses (Jordan's model plan, Nov 21). Other model names are tracked too; these are listed first.
MODELS = ("gemini-4-hydrogen", "gemini-4-neon", "gemini-3.8-flash")
LIMIT_KEYS = ("rpm", "tpm", "rpd")
NEAR = 0.80               # at or over 80% of a limit reads "near"; at or over 100% reads "at"
KEEP_DAYS = 31            # calls older than this aren't needed for any view


class LimitError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def empty_table():
    """A limits table with nothing set: every model's RPM, TPM and RPD are None ("not set")."""
    return {m: {k: None for k in LIMIT_KEYS} for m in MODELS}


# Where the numbers live: 5.7's settings store, which the app writes (blank by default):
#   models.{hydrogen,neon,flash_3_8}.{rpm,rpd,tpm}, read with rivia_progress_settings.model_limits().
# This file keeps no second copy. SETTINGS_KEYS maps 5.7's short names to Google's model ids.
SETTINGS_KEYS = {"hydrogen": "gemini-4-hydrogen", "neon": "gemini-4-neon", "flash_3_8": "gemini-3.8-flash"}


def from_settings(raw):
    """A limits table from 5.7's model_limits() output ({"hydrogen": {"rpm": 10, ...}, ...}). Anything missing,
    blank or not a whole number above 0 reads as not set; it's never guessed. Returns (table, problems)."""
    table, problems = empty_table(), []
    if not isinstance(raw, dict):
        return table, ["model limits from the settings weren't readable, so all show as not set"]
    for short, vals in raw.items():
        model = SETTINGS_KEYS.get(short, short)
        row = table.setdefault(model, {k: None for k in LIMIT_KEYS})
        for k in LIMIT_KEYS:
            v = (vals or {}).get(k) if isinstance(vals, dict) else None
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                problems.append("%s %s isn't a whole number above 0, so it shows as not set" % (model, k))
                continue
            row[k] = v
    return table, problems


def current_table():
    """The limits Jordan typed into the app, read from 5.7's settings. Reads only. If 5.7's settings aren't there
    (or have no model_limits yet), every model shows as not set and problems says so."""
    try:
        import rivia_progress_settings as settings
        raw = settings.model_limits()
    except Exception as e:  # noqa: BLE001  (missing module or function: show not set, say why)
        return empty_table(), ["model limits couldn't be read from the app settings (%s)" % type(e).__name__]
    return from_settings(raw)


def call(model, at, *, input_tokens, output_tokens=0):
    """One Gemini call, as the caller stores it. Input tokens count toward TPM (Google's definition)."""
    for n, v in (("at", at), ("input_tokens", input_tokens), ("output_tokens", output_tokens)):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            raise LimitError("bad_value", "%s must be a number, 0 or more" % n)
    if not isinstance(model, str) or not model.strip():
        raise LimitError("bad_value", "model must be a model name")
    return {"model": model.strip(), "at": float(at), "in": int(input_tokens), "out": int(output_tokens)}


def prune(calls, now):
    """Drop calls older than KEEP_DAYS. A new list."""
    return [c for c in calls if c["at"] > now - KEEP_DAYS * 86400]


def pacific_midnight(now):
    """The last midnight Pacific time at or before `now` (epoch seconds), when RPD resets."""
    local = datetime.fromtimestamp(now, PACIFIC)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def next_pacific_midnight(now):
    """The next midnight Pacific time after `now`, when today's RPD count starts over (right across clock changes)."""
    local = datetime.fromtimestamp(now, PACIFIC).date() + timedelta(days=1)
    return datetime(local.year, local.month, local.day, tzinfo=PACIFIC).timestamp()


def _gauge(used, limit):
    if limit is None:
        return {"used": used, "limit": None, "share": None, "status": "not_set"}
    share = used / limit
    status = "at" if share >= 1.0 else "near" if share >= NEAR else "ok"
    return {"used": used, "limit": limit, "share": round(share, 4), "status": status}


def usage(calls, table, now):
    """Per model, right now: RPM (calls in the last 60 s), TPM (input tokens in the last 60 s) and RPD (calls since
    midnight Pacific), each against its limit. Status: ok, near (80%+), at (100%+) or not_set. Reads only."""
    day0 = pacific_midnight(now)
    names = list(MODELS) + sorted({c["model"] for c in calls} - set(MODELS))
    out = {}
    for m in names:
        mine = [c for c in calls if c["model"] == m and c["at"] <= now]
        minute = [c for c in mine if c["at"] > now - 60]
        lim = table.get(m, {})
        out[m] = {
            "rpm": _gauge(len(minute), lim.get("rpm")),
            "tpm": _gauge(sum(c["in"] for c in minute), lim.get("tpm")),
            "rpd": _gauge(sum(1 for c in mine if c["at"] >= day0), lim.get("rpd")),
        }
        st = [g["status"] for g in out[m].values()]
        out[m]["worst"] = "at" if "at" in st else "near" if "near" in st else "ok" if "ok" in st else "not_set"
    return out


def daily(calls, now, days=7):
    """Per model, per Pacific day (oldest first, today last): requests and tokens in and out. For the graphs."""
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= KEEP_DAYS:
        raise LimitError("bad_value", "days must be 1 to %d" % KEEP_DAYS)
    today = datetime.fromtimestamp(now, PACIFIC).date()
    labels = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    names = list(MODELS) + sorted({c["model"] for c in calls} - set(MODELS))
    out = {m: {d: {"requests": 0, "in": 0, "out": 0} for d in labels} for m in names}
    for c in calls:
        if c["at"] > now:
            continue
        d = datetime.fromtimestamp(c["at"], PACIFIC).date().isoformat()
        if d in out[c["model"]]:
            cell = out[c["model"]][d]
            cell["requests"] += 1
            cell["in"] += c["in"]
            cell["out"] += c["out"]
    return {"days": labels, "models": {m: [out[m][d] for d in labels] for m in names}}
