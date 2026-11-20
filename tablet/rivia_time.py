# rivia_time.py (core library): New York times for every part of Rivia, and RiviaError.
# Moved out of rivia_planner.py in the core split so places can use them without the planner;
# rivia_planner re-exports every name, so rp.RiviaError and rp.to_epoch are the same objects.
import time
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo('America/New_York')


class RiviaError(Exception):
    def __init__(self, code, msg, **extra):
        super().__init__(msg)
        self.code = code
        self.extra = extra


# ── TIME ────────────────────────────────────────────────────────────────

def to_epoch(v, name='time'):
    """Epoch seconds from epoch s, epoch ms or ISO 8601 (no offset = New York)."""
    if isinstance(v, bool) or v is None or v == '':
        raise RiviaError('bad_time', name + ' is missing.')
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().lstrip('-').replace('.', '', 1).isdigit()):
        n = float(v)
        if n > 1e11:
            n /= 1000.0
        return int(n)
    try:
        d = datetime.fromisoformat(str(v).strip().replace('Z', '+00:00'))
    except ValueError:
        raise RiviaError('bad_time', name + ' is not a time: ' + str(v)[:40])
    if d.tzinfo is None:
        d = d.replace(tzinfo=TZ)
    return int(d.timestamp())


def now_s():
    return int(time.time())


def dt(s):
    return datetime.fromtimestamp(s, TZ)


def clock(s):
    """Spoken clock time, like 2:13 (no AM/PM), as Code.gs says it."""
    d = dt(s)
    return '%d:%02d' % (d.hour % 12 or 12, d.minute)


def time_of_day(s):
    """Clock time with AM or PM, like 2:13 PM."""
    d = dt(s)
    return clock(s) + (' AM' if d.hour < 12 else ' PM')


def iso(s):
    return dt(s).isoformat()


def day_key(s):
    return dt(s).strftime('%Y-%m-%d')


def long_day(s):
    d = dt(s)
    return '%s %s %d' % (d.strftime('%a'), d.strftime('%b'), d.day)


def is_weekend(s):
    return dt(s).weekday() >= 5  # Saturday 5, Sunday 6


def minutes_of_day(s):
    d = dt(s)
    return d.hour * 60 + d.minute


def noon_of(day_str):
    y, m, d = (int(x) for x in day_str.split('-'))
    return int(datetime(y, m, d, 12, tzinfo=TZ).timestamp())


def hour12(h):
    hh = h % 24
    return '%d %s' % (hh % 12 or 12, 'AM' if hh < 12 else 'PM')


def min_words(m):
    if m < 90:
        return '%d %s' % (m, 'minute' if m == 1 else 'minutes')
    h, r = divmod(m, 60)
    out = '%d %s' % (h, 'hour' if h == 1 else 'hours')
    return out + (' %d %s' % (r, 'minute' if r == 1 else 'minutes') if r else '')


def span_words(m):
    return '%d-hour' % (m // 60) if m % 60 == 0 else '%d-minute' % m


def when(s):
    """A time as both epoch seconds and local text, for the Bot."""
    return {'epoch': s, 'local': time_of_day(s), 'iso': iso(s)}
