# rivia_trend.py: is the gap between her plans and Jordan's real day shrinking, growing or flat? (emotion spec,
# Step 5.3, Progress Trend).
#
# DRAFT (Nov 23, not installed). Nothing imports it yet. It stores nothing, reads no database, calls nothing, and
# never touches emotion.db: it is one pure function, trend(), that 5.6's merge will feed from wherever 5.2's daily
# gaps end up stored. It only reports a direction; 5.4 turns that into a mood nudge and 5.5 owns the limits.
#
# Input: 5.2's daily gap, one number per day, 0 (the plan matched the day) to 1 (way off), or None for a day with
# too little data to score.
#
# How a read works, worked out fresh on every call:
#   1. Pick the days. Only days in the last LOOKBACK_DAYS (14) calendar days count, so a trend can't be built from
#      weeks-old data (scheduling is parked as of Nov 23, so this matters). Empty days (None, or missing) are
#      skipped, never counted as 0. Of what is left, the newest SPAN_DAYS (7) are used. Fewer than MIN_DAYS (4)
#      means "not_enough_data".
#   2. Smooth (spec: smooth before taking the slope). Each day is replaced by the median (middle value) of itself
#      and the scored day on either side. A single bad day between two ordinary ones disappears here. The first
#      and last days use Tukey's end-point rule (see _smooth), so a lone odd day at either end is dropped too.
#   3. Slope, first derivative only (spec: never the second, it amplifies noise). The Theil-Sen slope: for every
#      pair of smoothed days, how much the gap changed per day between them, then the median of all those. It uses
#      real calendar distance, so two days with a skipped week between them are a week apart, not one.
#   4. Wobble: how much the raw days bounce around. The larger of two robust guesses: how far the raw days usually
#      sit off that line (1.4826 x the median distance, the same stand-in for a standard deviation that 4.3 uses),
#      and the median jump from one scored day to the next once the trend is taken out (/ 0.954). With only 7 days either one alone often
#      guesses too low and lets noise through; the larger of the two cut false trends from about 1 in 5 to about 1 in 14
#      in simulation. Both use medians, so one odd day barely moves them.
#   5. Decide. slope_per_week = slope x 7.
#        - inside the flat band (+-FLAT_BAND, 0.03 a week, i.e. 3 points on a 0-100 scale): "flat"
#        - clarity = how many times bigger the slope is than the wobble alone would produce by chance (the slope's
#          standard error, wobble / sqrt(spread of the days)). Below MIN_CLARITY (2): "flat", it is just noise.
#        - otherwise "shrinking" (her plans getting better) or "growing" (getting worse).
#   6. Strength, 0 to 1: steepness (|slope_per_week| / STRONG_PER_WEEK, 0.15 a week, capped at 1) times
#      sureness (clarity / CLEAR_CLARITY, 4, capped at 1). Flat and not_enough_data are 0.
#
# What this means in days: one bad day never flips it. A real change of level shows on the third day in a row at
# the new level (two can be chance). A slow, steady change shows once it beats the day-to-day wobble.
#
# Every number below is a starting guess to tune by feel, like the rest of the spec.
import datetime as dt
import math

LOOKBACK_DAYS = 14        # calendar days back from today that may count at all
SPAN_DAYS = 7             # the newest days with data that are used (about a week of Jordan's days)
MIN_DAYS = 4              # fewer days with data than this: not_enough_data
FLAT_BAND = 0.03          # a week's change smaller than this (either way) reads as flat
STRONG_PER_WEEK = 0.15    # a week's change this big or bigger is full steepness
MIN_CLARITY = 2.0         # slope must be at least this many standard errors from zero, or it is noise
CLEAR_CLARITY = 4.0       # this many or more is full sureness
MAD_TO_SD = 1.4826        # median absolute distance -> standard deviation, for normally spread data
DIFF_TO_SD = 0.954        # median day-to-day jump -> standard deviation, for normally spread data
WOBBLE_FLOOR = 0.005      # days that sit almost exactly on the line still count as wobbling this much

DIRECTIONS = ("shrinking", "growing", "flat", "not_enough_data")
KEYS = ("direction", "strength", "slope_per_week", "level", "wobble", "clarity", "days_used", "days_needed",
        "today", "reason")


class TrendError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _day(x):
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, dt.date):
        return x
    if isinstance(x, str):
        try:
            return dt.date.fromisoformat(x)
        except ValueError:
            pass
    raise TrendError("bad_day", "day must be a date or 'YYYY-MM-DD', not %r" % (x,))


def _gap(x):
    if x is None:
        return None
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise TrendError("bad_gap", "gap must be a number from 0 to 1, or None, not %s" % type(x).__name__)
    x = float(x)
    if not math.isfinite(x) or x < 0.0 or x > 1.0:
        raise TrendError("bad_gap", "gap must be from 0 to 1, not %r" % (x,))
    return x


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def _smooth(values):
    """3-day running median. The first and last day have only one neighbour, so they use Tukey's end-point rule:
    the median of the day itself, its smoothed neighbour, and the straight line through the two smoothed days next
    to it carried one step out. That keeps a steady slope exact at the ends instead of flattening it, and still
    drops a single odd end day."""
    n = len(values)
    out = [_median(values[i - 1:i + 2]) if 0 < i < n - 1 else values[i] for i in range(n)]
    if n >= 3:
        out[0] = _median([values[0], out[1], 2 * out[1] - out[2]])
        out[-1] = _median([values[-1], out[-2], 2 * out[-2] - out[-3]])
    return out


def _theil_sen(xs, ys):
    """Median of the pairwise slopes, and the matching intercept (median of y - slope x)."""
    slopes = [(ys[j] - ys[i]) / (xs[j] - xs[i]) for i in range(len(xs)) for j in range(i + 1, len(xs))]
    slope = _median(slopes)
    return slope, _median([y - slope * x for x, y in zip(xs, ys)])


def _out(today, used, reason, direction="not_enough_data", strength=0.0, per_week=None, level=None, wobble=None,
         clarity=None):
    r = {"direction": direction, "strength": strength,
         "slope_per_week": None if per_week is None else round(per_week, 4),
         "level": None if level is None else round(level, 4),
         "wobble": None if wobble is None else round(wobble, 4),
         "clarity": None if clarity is None else (round(clarity, 2) if math.isfinite(clarity) else None),
         "days_used": used, "days_needed": MIN_DAYS, "today": today.isoformat(), "reason": reason}
    assert tuple(r) == KEYS
    return r


def trend(days, *, today):
    """days: (day, gap) pairs or {"day", "gap"} dicts, any order; gap 0..1 or None. today: the New York date of
    the read (5.6 passes it, so this stays a pure function). A day after today is refused; a repeated day too.
    Returns a dict (see the README):
      direction       "shrinking" (her plans getting better), "growing" (worse), "flat" or "not_enough_data"
      strength        0..1, how steep and how sure; 0 for flat and not_enough_data
      slope_per_week  gap change per week from the smoothed days (minus = shrinking), or None
      level           the average of the smoothed days: where the gap sits now, for 5.4's "steady good/bad"
      wobble          how far the raw days usually sit off the trend line
      clarity         slope in standard errors (None when the days sit exactly on the line)
      days_used       the dates used, oldest first
      reason          one plain line for the shadow log"""
    today = _day(today)
    seen = {}
    for row in days:
        if isinstance(row, dict):
            d, g = row.get("day"), row.get("gap")
        else:
            try:
                d, g = row
            except (TypeError, ValueError):
                raise TrendError("bad_row", "a row is (day, gap) or {'day': ..., 'gap': ...}")
        d, g = _day(d), _gap(g)
        if d > today:
            raise TrendError("future", "%s is after today (%s)" % (d, today))
        if d in seen:
            raise TrendError("duplicate", "%s appears twice" % d)
        seen[d] = g
    first = today - dt.timedelta(days=LOOKBACK_DAYS - 1)
    kept = sorted((d, g) for d, g in seen.items() if g is not None and d >= first)[-SPAN_DAYS:]
    used = [d.isoformat() for d, _ in kept]
    if len(kept) < MIN_DAYS:
        return _out(today, used, "only %d scored day(s) in the last %d days; need %d"
                    % (len(kept), LOOKBACK_DAYS, MIN_DAYS))
    xs = [(d - kept[0][0]).days for d, _ in kept]
    raw = [g for _, g in kept]
    smooth = _smooth(raw)
    slope, icept = _theil_sen(xs, smooth)
    per_week = slope * 7.0
    level = sum(smooth) / len(smooth)
    off_line = MAD_TO_SD * _median([abs(y - (icept + slope * x)) for x, y in zip(xs, raw)])
    res = [y - slope * x for x, y in zip(xs, raw)]  # the trend taken out, so a steady slope isn't counted as wobble
    day_to_day = _median([abs(b - a) for a, b in zip(res, res[1:])]) / DIFF_TO_SD
    wobble = max(off_line, day_to_day)
    mean_x = sum(xs) / len(xs)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    se = max(wobble, WOBBLE_FLOOR) / math.sqrt(sxx)
    clarity = abs(slope) / se if wobble > 0 else math.inf
    args = dict(per_week=per_week, level=level, wobble=wobble, clarity=clarity)
    if abs(per_week) < FLAT_BAND:
        return _out(today, used, "gap moved %+.3f a week, inside the flat band of %.2f" % (per_week, FLAT_BAND),
                    direction="flat", **args)
    if clarity < MIN_CLARITY:
        return _out(today, used, "gap moved %+.3f a week, but the days wobble too much to call it (%.1f < %.1f)"
                    % (per_week, clarity, MIN_CLARITY), direction="flat", **args)
    steep = min(1.0, abs(per_week) / STRONG_PER_WEEK)
    sure = min(1.0, clarity / CLEAR_CLARITY)
    direction = "shrinking" if per_week < 0 else "growing"
    return _out(today, used, "gap %s about %.3f a week over %d scored days" % (direction, abs(per_week), len(kept)),
                direction=direction, strength=round(steep * sure, 3), **args)
