#!/usr/bin/env python3
"""
RIVIA PLANNER - the scheduling math from Code.gs (planner phase 3.2), for the Grok Bot.

The Bot owns the scheduling: it reads the calendar, gets ride times from Maps, writes the
Rivia calendar and talks to Jordan. This script owns the arithmetic. Every clock time the
Bot states or writes must come out of this script. The script never touches the network:
the Bot passes in calendar events and Maps minutes, and the script answers in JSON.

USAGE
  python3 rivia_planner.py <command> < input.json        (JSON on stdin, JSON on stdout)
  python3 rivia_planner.py selftest                      (no input; run after every change)

COMMANDS
  arrival   Jordan arrived somewhere. Returns a message payload and the new leave-by alerts.
            in:  { "place": "farm|gym|pf|hmc|home" (optional), "title": "calendar title"
                   (optional), "coords": "lat,lng" (optional), "time": arrival time,
                   "events": [main-calendar events, now-1 day to now+1 day], "maps": {...} }
            out: { place, arrived, message, alerts, fallback_text }
                 alerts: null = leave current alerts alone, [] = clear all Rivia alerts,
                 [...] = clear all, then set exactly these. Each alert has its own message.

MESSAGES  No sentences: facts plus how to say them. The app's voice layer writes the words.
  { "situation": "gym_late_cut",            what happened (a fixed list; see EMOTION)
    "facts": { ... },                       every number and time it may use; times are
                                            { epoch, local "12:45 PM", spoken "12:45", iso }
    "must_say": ["leave_for_work_at", ...], fact keys the reply has to include
    "decisions_for_jordan": [...],          choices that are his call; offer, never pick
    "delivery": { say, urgency low|normal|high, hold_ok, max_spoken_sentences },
    "emotion": { tone, intensity 0-1, note } }
  fallback_text is a plain line for when the voice layer fails or its times don't match.
  Tone and urgency per situation live in the EMOTION table; tune them there.
  plan      Build workdays for the Rivia calendar.
            in:  { "days": ["2026-11-09", ...] or "first": 1, "count": 1,
                   "events": [main-calendar events from 2 days before the first day to
                   1 day after the last], "maps": {...} }
            out: { days: [ { day, status, shift, blocks, problems, calendar_events, message } ] }
                 status: planned | no_shift | not_written (problems) | under_way | failed
  log-ride  Store one finished ride from the app and learn from it if it is clean.
            in:  { "ride_id", "from", "to", "departed", "arrived", "distance_km",
                   "avg_kmh", "polyline", "maps": {...} }
            out: { stored, duplicate, minutes, used, note, backup_due }
  legs      Ride minutes for pairs of places, with where each number came from.
            in:  { "pairs": [["farm", "pf"], ...], "maps": {...} }
  place     Which known place a title or position is.  in: { "title", "coords" }
  selftest  The planner checks (in rivia_planner_tests.py). All must pass before the Bot relies on it.

DATABASE  Rides live in ~/rivia/scheduler.db (SQLite). A copy goes to Google Drive.
  backup           Make a fresh copy for Drive, only if rides changed since the last upload.
                   in: {} (or { "force": true })
                   out: { upload, reason } or { upload: true, db_path, json_path, rides, bytes,
                        sha256, change }. Upload db_path to Drive (json_path if the Drive
                        connector can't take binary files), then run backup-uploaded.
  backup-uploaded  Record that the upload worked.  in: { "change": <change from backup> }
  restore          Merge a copy downloaded from Drive into the local database. Never
                   deletes a ride.  in: { "path": "<downloaded .db or .json file>" }
                   out: { added, rides }
  init-db          Only when Drive has no copy at all: mark a new, empty database as
                   intended.  out: { initialized }
  A new, empty database (a reset computer) answers log-ride and backup with
  restore_first until restore or init-db runs, so an empty file can never be uploaded
  over the Drive copy. Every reply from a new database carries "db_fresh": true.

TIMES  In: epoch seconds, epoch milliseconds, or ISO 8601 ("2026-11-09T13:00:00-05:00";
       no offset means New York time). Out: epoch seconds plus local text ("1:00 PM").

MAPS   Ride times come from, best first: learned (Jordan's own clean rides, once there are
       3), his own estimates (MEASURED_LEGS below), then Maps bike time passed in as
       "maps": { "farm>pf": 23 } (minutes, from>to). If a command needs a Maps time it
       doesn't have, it answers { ok: false, error: { code: "need_maps", pairs: [...] } }
       listing every missing pair with both addresses. Look those up (bicycling), then run
       the same command again with "maps" filled in. Nothing is ever guessed.

FILES  ~/rivia/scheduler.db        every ride (log-ride writes it)
       ~/rivia/backup/scheduler.db the copy for Drive (backup writes it), plus scheduler.json
       ~/rivia/rides.jsonl         old ride log; moved into scheduler.db once, then renamed
                                   rides.jsonl.migrated
       ~/rivia/places.json         optional pins: { "farm": { "lat": 40.2, "lng": -76.7 } }
       Override the folder with the RIVIA_DIR environment variable.
"""

import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo('America/New_York')
RIVIA_DIR = os.path.expanduser(os.environ.get('RIVIA_DIR', '~/rivia'))
PLACES_FILE = os.path.join(RIVIA_DIR, 'places.json')
VERSION = 'planner 4.2 (port of Code.gs 3.2, rides in SQLite)'

# Schedular v2 config. Same numbers as Code.gs.
CFG = {
    'bufferM': 10,         # arrive this long before a shift (Module 1)
    'preworkM': 30,        # pre-work default
    'preworkFloorM': 15,   # pre-work minimum
    'workoutM': 120,       # workout target (Module 4)
    'workoutFloorM': 60,   # workout hard floor
    'farmM': 45,           # farm minimum; only Jordan shortens it (Module 3)
    'leadM': 5,            # leave-by alerts fire this long before departure
    'windDownM': 60,       # Module 8
    'wakeUpM': 60,
    'sleepTargetH': 8,
    'placeRadiusM': 400,   # an arrival this close to a known place counts as that place
    'overnightH': 8,       # home with the next shift this far off: plan that morning instead
    'homeLookaheadH': 20,  # at home, look this far ahead for the next shift
    'learnKeep': 10,       # keep the last 10 clean trips per ride, each direction
    'learnMin': 3,         # and start using them once there are 3
    'learnRank': 0.75,     # plan with about the 3rd-slowest of 10
    'rideMaxH': 3,         # a ride longer than this isn't a ride
    'cleanLow': 0.25,      # under 0.25x or over 2x the usual: logged, not learned from
    'cleanHigh': 2,
    'maxKmh': 60,          # faster than this pin to pin (straight line) isn't a bike ride
    'fastRatio': 0.5,      # under 0.5x the learned usual (3+ rides): logged, not learned from
}

PLACES = {
    'home': {'say': 'home', 'addr': '600 E Main St, Middletown, PA'},
    'farm': {'say': 'the farm', 'addr': '2003 Vine Street, Middletown, PA 17057'},
    'gym': {'say': 'the gym', 'addr': '30 Conference Dr, Hershey, PA 17033'},
    'pf': {'say': 'Planet Fitness', 'addr': '1170 Mae St, Hummelstown, PA 17036'},
    'hmc': {'say': 'work', 'addr': '500 University Dr, Hershey, PA 17033'},
}
# Jordan's own places (Giant, ...) live in the server's settings store and reach the planner
# through places.json, marked "custom": true. Their ids look like this.
CUSTOM_ID = re.compile(r'^[a-z][a-z0-9_]{0,23}$')

# Jordan's own ride times, minutes, either direction. Used until a ride is learned.
MEASURED_LEGS = {'farm|home': 5, 'farm|gym': 20, 'farm|hmc': 20, 'gym|hmc': 5, 'hmc|home': 25, 'home|pf': 17}

# Posted gym hours, by hour of day.
GYM_HOURS = {
    'gym': {'weekdayOpen': 5, 'weekdayClose': 20, 'weekendOpen': 8, 'weekendClose': 12},
    'pf': {'weekdayOpen': 5, 'weekdayClose': 21, 'weekendOpen': 7, 'weekendClose': 19},
}


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


# ── PLACES ──────────────────────────────────────────────────────────────

def place_from_title(title):
    """Known place from a calendar title. A title of just Gym returns gymday."""
    t = str(title or '').lower().strip()
    if t.startswith('work shift') or 'hershey med' in t:
        return 'hmc'
    if 'planet fitness' in t:
        return 'pf'
    if 'university fitness' in t:
        return 'gym'
    if t == 'gym' or t.startswith('gym '):
        return 'gymday'
    if t == 'farm' or t.startswith('farm ') or 'horse care' in t:
        return 'farm'
    if t == 'home':
        return 'home'
    return None


def parse_coords(c):
    if c is None or c == '':
        return None
    if isinstance(c, dict):
        lat, lng = c.get('lat'), c.get('lng')
    else:
        parts = str(c).split(',')
        if len(parts) != 2:
            raise RiviaError('bad_coords', 'coords must be lat,lng.')
        lat, lng = parts
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        raise RiviaError('bad_coords', 'coords are not numbers.')
    if abs(lat) > 90 or abs(lng) > 180 or (lat == 0 and lng == 0):
        raise RiviaError('bad_coords', 'coords are not a valid lat,lng.')
    return {'lat': lat, 'lng': lng}


def meters_between(a, b):
    rad = math.pi / 180
    d_lat = (b['lat'] - a['lat']) * rad
    d_lng = (b['lng'] - a['lng']) * rad
    h = (math.sin(d_lat / 2) ** 2 +
         math.cos(a['lat'] * rad) * math.cos(b['lat'] * rad) * math.sin(d_lng / 2) ** 2)
    return 2 * 6371000 * math.asin(min(1, math.sqrt(h)))


def place_registry():
    """Every pinned place in places.json (the server rewrites it from the settings store):
    id -> {lat, lng, name, radius_m (None = no own zone size), custom, removed}.
    The five core places plus Jordan's custom ones; bad entries are skipped."""
    try:
        with open(PLACES_FILE) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for pid, p in (raw.items() if isinstance(raw, dict) else []):
        info = p if isinstance(p, dict) else {}
        custom = pid not in PLACES
        if custom and not (info.get('custom') and CUSTOM_ID.match(str(pid))):
            continue
        try:
            c = parse_coords(p)
        except RiviaError:
            continue
        if not c:
            continue
        try:
            radius = int(info['radius_m']) if info.get('radius_m') is not None else None
        except (TypeError, ValueError):
            radius = None
        name = str(info.get('name') or (pid if custom else PLACES[pid]['say']))
        out[pid] = {'lat': c['lat'], 'lng': c['lng'], 'name': name, 'radius_m': radius,
                    'custom': custom, 'removed': bool(custom and info.get('removed'))}
    return out


def place_pins():
    """Pins of the places in use (core and custom, not removed)."""
    return {pid: {'lat': p['lat'], 'lng': p['lng']} for pid, p in place_registry().items() if not p['removed']}


def place_of(coords, title):
    """Title first (the geofence was set for that event); position only as a fallback.
    A position matches the nearest place within that place's own zone size, or within
    placeRadiusM for a place without one."""
    by_title = place_from_title(title)
    if by_title:
        return by_title
    c = parse_coords(coords)
    if not c:
        return None
    best, best_m = None, float('inf')
    for pid, p in place_registry().items():
        if p['removed']:
            continue
        m = meters_between(c, p)
        if m <= (p['radius_m'] or CFG['placeRadiusM']) and m < best_m:
            best, best_m = pid, m
    return best


def gym_for(s):
    return 'pf' if is_weekend(s) else 'gym'


def gym_for_place(place, s):
    return gym_for(s) if place == 'gymday' else place


def gym_name(pid):
    return 'Planet Fitness' if pid == 'pf' else 'the University Fitness Center'


# ── CALENDAR (events come from the Bot's Calendar connector) ────────────

def norm_event(ev):
    if not isinstance(ev, dict):
        raise RiviaError('bad_events', 'Each event must be an object.')
    all_day = bool(ev.get('all_day'))
    if all_day:
        return {'title': str(ev.get('title') or ''), 'all_day': True}
    return {
        'title': str(ev.get('title') or ev.get('summary') or ''),
        'start': to_epoch(ev.get('start'), 'event start'),
        'end': to_epoch(ev.get('end'), 'event end'),
        'location': str(ev.get('location') or ''),
        'all_day': False,
    }


def is_shift(ev):
    """A timed event titled Work Shift..., or 4 h+ at 500 University Dr that isn't a buffer or transit block."""
    title = ev['title'].lower().strip()
    if title.startswith('work shift'):
        return True
    hours = (ev['end'] - ev['start']) / 3600.0
    at_hmc = '500 university dr' in ev['location'].lower()
    return at_hmc and hours >= 4 and 'buffer' not in title and 'transit' not in title


def shifts_from(events):
    if not isinstance(events, list):
        raise RiviaError('bad_events', 'events must be a list (pass [] if the calendar is empty).')
    out = []
    for raw in events:
        ev = norm_event(raw)
        if not ev['all_day'] and is_shift(ev):
            out.append({'start': ev['start'], 'end': ev['end'], 'title': ev['title']})
    return sorted(out, key=lambda x: x['start'])


def shift_today(shifts, t_s):
    day = day_key(t_s)
    for s in shifts:
        if s['start'] > t_s and day_key(s['start']) == day:
            return s
    return None


def next_shift(shifts, t_s, within_h):
    for s in shifts:
        if t_s < s['start'] <= t_s + within_h * 3600:
            return s
    return None


def shift_on(shifts, day):
    for s in shifts:
        if day_key(s['start']) == day:
            return s
    return None


# ── RIDE DATABASE (SQLite) ──────────────────────────────────────────────

SCHEMA = """
create table if not exists rides(
    ride_id text primary key, day text, from_place text, to_place text,
    departed text, arrived text, left_local text, arrived_local text,
    minutes real, used integer, note text, distance_km real, avg_kmh real,
    polyline text, logged text);
create table if not exists meta(key text primary key, value text);
"""
RIDE_FIELDS = ['ride_id', 'day', 'from', 'to', 'departed', 'arrived', 'left_local', 'arrived_local',
               'minutes', 'used', 'note', 'distance_km', 'avg_kmh', 'polyline', 'logged']


def db_file():
    return os.path.join(RIVIA_DIR, 'scheduler.db')


def backup_dir():
    return os.path.join(RIVIA_DIR, 'backup')


def get_meta(con, key, default=None):
    row = con.execute('select value from meta where key=?', (key,)).fetchone()
    return row[0] if row else default


def set_meta(con, key, value):
    con.execute('insert or replace into meta values(?,?)', (key, str(value)))


def bump_change(con):
    """Every write bumps this counter; backup compares it with the last upload."""
    set_meta(con, 'change', int(get_meta(con, 'change', '0')) + 1)


def num_or_none(v):
    try:
        return None if v is None or v == '' else float(v)
    except (TypeError, ValueError):
        return None


def insert_ride(con, r):
    """Insert one ride record; an existing ride_id is left alone. Returns 1 if added."""
    rid = str(r.get('ride_id') or '').strip()
    if not rid:
        return 0
    cur = con.execute(
        'insert or ignore into rides values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (rid, r.get('day'), r.get('from'), r.get('to'), r.get('departed'), r.get('arrived'),
         r.get('left_local'), r.get('arrived_local'), num_or_none(r.get('minutes')),
         1 if r.get('used') else 0, r.get('note') or '', num_or_none(r.get('distance_km')),
         num_or_none(r.get('avg_kmh')), r.get('polyline'), r.get('logged')))
    return cur.rowcount


def read_rides(con):
    out = []
    for row in con.execute('select * from rides order by rowid'):
        r = dict(zip(RIDE_FIELDS, row))
        r['used'] = bool(r['used'])
        out.append(r)
    return out


def read_jsonl(path):
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass  # a broken line never stops anything
    return out


def db_open():
    """Open scheduler.db, creating it if needed. A new file takes in rides.jsonl if there is one;
    otherwise it is marked fresh until restore or init-db runs."""
    path = db_file()
    new = not os.path.exists(path)
    try:
        os.makedirs(RIVIA_DIR, exist_ok=True)
        con = sqlite3.connect(path, timeout=10, isolation_level=None)
        con.executescript(SCHEMA)
        if new:
            old = os.path.join(RIVIA_DIR, 'rides.jsonl')
            set_meta(con, 'created', iso(now_s()))
            if os.path.exists(old):
                for r in read_jsonl(old):
                    insert_ride(con, r)
                bump_change(con)
                os.replace(old, old + '.migrated')
            else:
                set_meta(con, 'fresh', '1')
        return con
    except sqlite3.Error as e:
        raise RiviaError('db_error', 'scheduler.db could not be opened: %s' % e)


def is_fresh(con):
    return get_meta(con, 'fresh') == '1'


def ride_count(con):
    return con.execute('select count(*) from rides').fetchone()[0]


def need_restore(con):
    if is_fresh(con):
        raise RiviaError('restore_first', 'scheduler.db is new and empty. Restore the Drive copy first '
                         '(download it, then run restore), or run init-db if Drive has no copy.')


def load_rides():
    con = db_open()
    try:
        return read_rides(con)
    finally:
        con.close()


# ── RIDE TIMES ──────────────────────────────────────────────────────────

def leg_key(a, b):
    return '|'.join(sorted([a, b]))


def rank_min(lst, rank):
    """Nearest-rank percentile: 0.5 is the middle, 0.75 the 3rd-slowest of 10."""
    s = sorted(lst)
    return s[max(0, math.ceil(rank * len(s)) - 1)]


def learned_trips(rides):
    trips = {}
    for r in rides:
        if r.get('used') and r.get('from') and r.get('to') and isinstance(r.get('minutes'), (int, float)):
            trips.setdefault(r['from'] + '>' + r['to'], []).append(float(r['minutes']))
    return {k: v[-CFG['learnKeep']:] for k, v in trips.items()}


class Legs:
    """Ride minutes, best source first. Missing Maps times are collected, never guessed."""

    def __init__(self, maps=None, rides=None):
        self.maps = {}
        for k, v in (maps or {}).items():
            try:
                if float(v) > 0:
                    self.maps[str(k).strip()] = float(v)
            except (TypeError, ValueError):
                pass
        self.trips = learned_trips(load_rides() if rides is None else rides)
        self.missing = []

    def info(self, a, b, skip_learned=False, note_missing=True):
        if a not in PLACES or b not in PLACES:
            raise RiviaError('bad_place', 'Unknown place: %s or %s.' % (a, b))
        if a == b:
            return 0, 'same place'
        if not skip_learned:
            t = self.trips.get(a + '>' + b)
            if t and len(t) >= CFG['learnMin']:
                return math.ceil(rank_min(t, CFG['learnRank'])), 'learned from %d rides' % len(t)
        yours = MEASURED_LEGS.get(leg_key(a, b))
        if yours:
            return yours, 'your estimate'
        m = self.maps.get(a + '>' + b)
        if m:
            return math.ceil(m), 'Maps bike time'
        if note_missing and not skip_learned and (a + '>' + b) not in self.missing:
            self.missing.append(a + '>' + b)
        return None, 'missing'

    def maybe(self, a, b):
        """Minutes for a leg, or None when nothing has it. Never asks for a Maps time."""
        return self.info(a, b, note_missing=False)[0]

    def __call__(self, a, b):
        v = self.info(a, b)[0]
        return v if v is not None else 0  # placeholder; the result is thrown away

    def check(self):
        if self.missing:
            pairs = []
            for k in self.missing:
                a, b = k.split('>')
                pairs.append({'key': k, 'from': a, 'to': b, 'from_addr': PLACES[a]['addr'], 'to_addr': PLACES[b]['addr']})
            raise RiviaError('need_maps', 'Need Maps bike times (minutes) for: ' + ', '.join(self.missing) +
                             '. Look them up and run again with "maps" filled in.', pairs=pairs)


# ── ARRIVALS (Schedular v2) ─────────────────────────────────────────────
# farm 45 -> ride -> gym flex -> workout 120 -> pre-work 30 -> ride -> at work 10 min early.

def workday_anchors(shift_start, legs):
    a = {}
    a['hmcArrive'] = shift_start - CFG['bufferM'] * 60
    a['leaveGym'] = a['hmcArrive'] - legs['gymHmc'] * 60
    a['preworkStart'] = a['leaveGym'] - CFG['preworkM'] * 60
    a['workoutStart'] = a['preworkStart'] - CFG['workoutM'] * 60
    a['leaveFarm'] = a['workoutStart'] - legs['farmGym'] * 60
    a['farmStart'] = a['leaveFarm'] - CFG['farmM'] * 60
    if legs.get('homeFarm') is not None:
        a['leaveHome'] = a['farmStart'] - legs['homeFarm'] * 60
    return a


def absorb_delay(late_s):
    """Cut pre-work 30 -> 15 first, then the workout 120 -> 60. The rest is only flagged."""
    late = max(0, math.ceil(late_s / 60.0))
    prework_cut = min(late, CFG['preworkM'] - CFG['preworkFloorM'])
    workout_cut = min(late - prework_cut, CFG['workoutM'] - CFG['workoutFloorM'])
    return {
        'lateM': late,
        'preworkM': CFG['preworkM'] - prework_cut,
        'workoutM': CFG['workoutM'] - workout_cut,
        'overM': late - prework_cut - workout_cut,
    }


# What Rivia should convey, by situation: tone (the roadmap's voice regions), how strong,
# and how urgent. Tune here; the code only picks the situation.
EMOTION = {
    'plumbing_test':              ('composed',  0.2, 'low'),
    'no_shift_today':             ('quiet',     0.2, 'low'),
    'farm_on_time':               ('assured',   0.4, 'normal'),
    'farm_early_flex':            ('wry',       0.5, 'low'),
    'farm_late_cut':              ('concerned', 0.5, 'normal'),
    'farm_late_over_floor':       ('concerned', 0.8, 'high'),
    'gym_flex':                   ('wry',       0.4, 'low'),
    'gym_on_time':                ('assured',   0.4, 'normal'),
    'gym_late_cut':               ('concerned', 0.5, 'normal'),
    'gym_late_over_floor':        ('concerned', 0.8, 'high'),
    'gym_leave_now':              ('concerned', 0.9, 'high'),
    'at_work':                    ('quiet',     0.0, 'low'),
    'home_no_shift':              ('quiet',     0.2, 'low'),
    'home_before_shift':          ('assured',   0.4, 'normal'),
    'home_leave_now':             ('concerned', 0.9, 'high'),
    'home_overnight':             ('composed',  0.3, 'low'),
    'home_overnight_short_sleep': ('concerned', 0.6, 'normal'),
    'arrived_unknown':            ('curious',   0.3, 'low'),
    'arrival_ignored':            ('quiet',     0.0, 'low'),
    'morning_rundown':            ('assured',   0.3, 'normal'),
    'leave_reminder':             ('assured',   0.5, 'high'),
    'plan_ready':                 ('composed',  0.3, 'low'),
    'plan_problems':              ('concerned', 0.5, 'normal'),
    'plan_no_shift':              ('quiet',     0.1, 'low'),
}

# Why the tone fits, for the voice layer. Short on purpose.
EMOTION_NOTES = {
    'farm_late_cut': 'He is behind but it is recoverable. Matter-of-fact, no scolding.',
    'gym_late_cut': 'He is behind but it is recoverable. Matter-of-fact, no scolding.',
    'farm_late_over_floor': 'The plan no longer fits. Lay out the choice; it is his call, not yours.',
    'gym_late_over_floor': 'The workout no longer fits. Lay out the choice; it is his call, not yours.',
    'gym_leave_now': 'He should already be leaving. Urgent and brief.',
    'home_leave_now': 'He should already be leaving. Urgent and brief.',
    'farm_early_flex': 'He is ahead. Light, a little playful.',
    'gym_flex': 'He is ahead. Light, a little playful.',
    'home_overnight_short_sleep': 'Flag the short sleep once. Care, not a lecture.',
    'at_work': 'Say nothing.',
    'arrival_ignored': 'Say nothing. A queued, repeated or out-of-order arrival.',
    'morning_rundown': 'Once a day, when he first opens the app at home. Warm and brief: the greeting, the day, when to leave.',
}


def tfact(s):
    """A time fact. Say it as 'spoken' (or 'local'); never compute a different time from it."""
    return {'epoch': s, 'local': time_of_day(s), 'spoken': clock(s), 'iso': iso(s)}


def pfact(pid):
    return {'id': pid, 'name': PLACES[pid]['say']} if pid in PLACES else {'id': pid, 'name': pid}


def message(situation, facts, must_say, decisions=None, say=True, sentences=2):
    tone, intensity, urgency = EMOTION[situation]
    return {
        'situation': situation,
        'facts': facts,
        'must_say': must_say,
        'decisions_for_jordan': decisions or [],
        'delivery': {
            'say': say,
            'urgency': urgency,
            'hold_ok': say and urgency != 'high',
            'max_spoken_sentences': sentences,
        },
        'emotion': {'tone': tone, 'intensity': intensity, 'note': EMOTION_NOTES.get(situation, '')},
    }


def alert(aid, at, leave_for, leave_at, fallback):
    return {
        'id': aid,
        'at': tfact(at),
        'message': message('leave_reminder', {'leave_for': pfact(leave_for), 'leave_at': tfact(leave_at)},
                           ['leave_for', 'leave_at']),
        'fallback_text': fallback,
    }


def leave_alert(tag, leave_s, now, dest, text):
    """One alert 5 min before departure; too late for that, at departure; past it, none."""
    lead = leave_s - CFG['leadM'] * 60
    at = lead if lead > now + 30 else (leave_s if leave_s > now + 30 else 0)
    return [alert('leave-%s-%d' % (tag, leave_s), at, dest, leave_s, text)] if at else []


def cut_words(cut):
    if cut['workoutM'] == CFG['workoutM']:
        return 'pre-work drops to %d minutes' % cut['preworkM']
    return 'pre-work drops to %d minutes and the workout to %d' % (cut['preworkM'], cut['workoutM'])


def cut_facts(cut):
    return {'late_minutes': cut['lateM'], 'prework_minutes': cut['preworkM'], 'workout_minutes': cut['workoutM'],
            'prework_cut_minutes': CFG['preworkM'] - cut['preworkM'],
            'workout_cut_minutes': CFG['workoutM'] - cut['workoutM']}


def arrived_line(place, title):
    if place == 'home':
        return 'Home.'
    if place and place in PLACES:
        return 'Arrived at %s.' % PLACES[place]['say']
    t = str(title or '').strip()
    return 'Arrived at %s.' % t[:60] if t else 'Arrived.'


def result(msg, alerts, fallback):
    return {'message': msg, 'alerts': alerts, 'speak': fallback}


def farm_plan(t_s, now, shift, leg, gym_id):
    a = workday_anchors(shift['start'], {'farmGym': leg('farm', gym_id), 'gymHmc': leg(gym_id, 'hmc')})
    leave_at = t_s + CFG['farmM'] * 60
    to_gym = 'Leave for %s at %s.' % (PLACES[gym_id]['say'], clock(leave_at))
    slack = a['leaveFarm'] - leave_at
    base = {'place': pfact('farm'), 'next_stop': pfact(gym_id), 'farm_minutes': CFG['farmM'],
            'leave_at': tfact(leave_at), 'shift_start': tfact(shift['start'])}
    if slack >= 0:
        flex_m = slack // 60
        sit = 'farm_early_flex' if flex_m >= 5 else 'farm_on_time'
        facts = dict(base, gym_flex_minutes=flex_m if flex_m >= 5 else 0)
        must = ['leave_at', 'next_stop'] + (['gym_flex_minutes'] if flex_m >= 5 else [])
        fb = ('Farm until %s, then %s.' % (clock(leave_at), PLACES[gym_id]['say']) +
              (' That leaves %s of gym flex.' % min_words(flex_m) if flex_m >= 5 else ''))
        return result(message(sit, facts, must), leave_alert('gym', leave_at, now, gym_id, to_gym), fb)
    cut = absorb_delay(-slack)
    if cut['overM'] == 0:
        facts = dict(base, **cut_facts(cut))
        fb = 'Running %s behind, so %s. %s' % (min_words(cut['lateM']), cut_words(cut), to_gym)
        return result(message('farm_late_cut', facts, ['late_minutes', 'prework_minutes', 'workout_minutes', 'leave_at']),
                      leave_alert('gym', leave_at, now, gym_id, to_gym), fb)
    leave_work = a['hmcArrive'] - leg('farm', 'hmc') * 60
    facts = {'place': pfact('farm'), 'late_minutes': cut['lateM'], 'workout_floor_minutes': CFG['workoutFloorM'],
             'leave_for_work_by': tfact(leave_work), 'shift_start': tfact(shift['start'])}
    fb = ('Running %s behind, so a %d-minute workout no longer fits. ' % (min_words(cut['lateM']), CFG['workoutFloorM']) +
          'Shortening the farm or taking a rest day is your call; leave for work by %s at the latest.' % clock(leave_work))
    return result(message('farm_late_over_floor', facts, ['late_minutes', 'leave_for_work_by'],
                          ['shorten the farm', 'take a rest day']),
                  leave_alert('work', leave_work, now, 'hmc', 'Leave for work at %s.' % clock(leave_work)), fb)


def gym_plan(t_s, now, shift, leg, gym_id):
    a = workday_anchors(shift['start'], {'farmGym': 0, 'gymHmc': leg(gym_id, 'hmc')})
    to_work = 'Leave for work at %s.' % clock(a['leaveGym'])
    base = {'place': pfact(gym_id), 'leave_for_work_at': tfact(a['leaveGym']), 'due_at_work': tfact(a['hmcArrive']),
            'shift_start': tfact(shift['start'])}
    if t_s >= a['leaveGym']:
        fb = 'Leave for work now. You are due there at %s.' % clock(a['hmcArrive'])
        return result(message('gym_leave_now', base, ['due_at_work']), [], fb)
    alerts = leave_alert('work', a['leaveGym'], now, 'hmc', to_work)
    flex = a['workoutStart'] - t_s
    if flex >= 0:
        flex_m = flex // 60
        if flex_m >= 5:
            facts = dict(base, gym_flex_until=tfact(a['workoutStart']), workout_minutes=CFG['workoutM'])
            fb = 'Gym flex until %s, then a %s workout. %s' % (clock(a['workoutStart']), span_words(CFG['workoutM']), to_work)
            return result(message('gym_flex', facts, ['gym_flex_until', 'leave_for_work_at']), alerts, fb)
        facts = dict(base, workout_until=tfact(a['preworkStart']), workout_minutes=CFG['workoutM'])
        fb = 'Workout until %s, then pre-work. %s' % (clock(a['preworkStart']), to_work)
        return result(message('gym_on_time', facts, ['workout_until', 'leave_for_work_at']), alerts, fb)
    cut = absorb_delay(-flex)
    if cut['overM'] == 0:
        facts = dict(base, **cut_facts(cut))
        fb = 'Running %s behind, so %s. %s' % (min_words(cut['lateM']), cut_words(cut), to_work)
        return result(message('gym_late_cut', facts, ['late_minutes', 'prework_minutes', 'workout_minutes',
                                                      'leave_for_work_at']), alerts, fb)
    left_m = max(0, (a['leaveGym'] - CFG['preworkFloorM'] * 60 - t_s) // 60)
    facts = dict(base, workout_minutes_left=left_m, workout_floor_minutes=CFG['workoutFloorM'])
    fb = ('Only %s left for the workout, under the %d-minute floor, so short cardio, ' % (min_words(left_m), CFG['workoutFloorM']) +
          'a longer pre-work or a rest day is your call. ' + to_work)
    return result(message('gym_late_over_floor', facts, ['workout_minutes_left', 'leave_for_work_at'],
                          ['short cardio', 'a longer pre-work', 'a rest day']), alerts, fb)


def leg_or_none(leg, a, b):
    """A leg's minutes, or None if nothing knows it. Works for Legs and for a plain function."""
    if isinstance(leg, Legs):
        return leg.maybe(a, b)
    try:
        return leg(a, b)
    except (KeyError, RiviaError):
        return None


def stops_from_home(t_s, shift, leg, leave_work, farm_done=False):
    """The stops that still fit before the shift, in order, each with the latest time to leave
    home for it: the farm (unless it was done today), the gym for that shift, then work."""
    gym_id = gym_for(shift['start'])
    gym_hmc = leg_or_none(leg, gym_id, 'hmc')
    stops = []
    if not farm_done:
        farm_gym, home_farm = leg_or_none(leg, 'farm', gym_id), leg_or_none(leg, 'home', 'farm')
        if None not in (gym_hmc, farm_gym, home_farm):
            a = workday_anchors(shift['start'], {'farmGym': farm_gym, 'gymHmc': gym_hmc, 'homeFarm': home_farm})
            if t_s < a['leaveHome']:
                stops.append(('farm', a['leaveHome']))
    home_gym = leg_or_none(leg, 'home', gym_id)
    if gym_hmc is not None and (stops or home_gym is not None):
        a = workday_anchors(shift['start'], {'farmGym': 0, 'gymHmc': gym_hmc})
        leave_gym = a['workoutStart'] - (home_gym or 0) * 60
        if stops or t_s < leave_gym:
            stops.append((gym_id, leave_gym))
    stops.append(('hmc', leave_work))
    return stops


def next_stop_from_home(t_s, shift, leg, leave_work, farm_done=False):
    """The first stop that still fits before the shift, and the latest time to leave home for it."""
    return stops_from_home(t_s, shift, leg, leave_work, farm_done)[0]


def home_plan(t_s, now, shift, leg, farm_done=False):
    if not shift:
        return result(message('home_no_shift', {'place': pfact('home')}, []), [], 'Home.')
    if shift['start'] - t_s < CFG['overnightH'] * 3600:
        hmc_arrive = shift['start'] - CFG['bufferM'] * 60
        leave_work = hmc_arrive - leg('home', 'hmc') * 60
        base = {'place': pfact('home'), 'leave_for_work_at': tfact(leave_work), 'due_at_work': tfact(hmc_arrive),
                'shift_start': tfact(shift['start'])}
        if t_s >= leave_work:
            fb = 'Home. Leave for work now; you are due there at %s.' % clock(hmc_arrive)
            return result(message('home_leave_now', base, ['due_at_work']), [], fb)
        stop, leave_at = next_stop_from_home(t_s, shift, leg, leave_work, farm_done)
        facts = dict(base, next_stop=pfact(stop), leave_at=tfact(leave_at))
        if stop == 'hmc':
            text = 'Leave for work at %s.' % clock(leave_work)
            tag, fb = 'work', 'Home. ' + text
        else:
            text = 'Leave for %s at %s.' % (PLACES[stop]['say'], clock(leave_at))
            tag = 'farm' if stop == 'farm' else 'gym'
            fb = 'Home. Next stop %s; leave by %s.' % (PLACES[stop]['say'], clock(leave_at))
        return result(message('home_before_shift', facts, ['next_stop', 'leave_at']),
                      leave_alert(tag, leave_at, now, stop, text), fb)
    gym_id = gym_for(shift['start'])
    a = workday_anchors(shift['start'], {'farmGym': leg('farm', gym_id), 'gymHmc': leg(gym_id, 'hmc'),
                                         'homeFarm': leg('home', 'farm')})
    asleep = t_s + CFG['windDownM'] * 60
    wake = a['leaveHome'] - CFG['wakeUpM'] * 60
    sleep_h = (wake - asleep) // 3600
    short = sleep_h < CFG['sleepTargetH']
    to_farm = 'Leave for the farm at %s.' % clock(a['leaveHome'])
    facts = {'place': pfact('home'), 'leave_for_farm_at': tfact(a['leaveHome']), 'wake_by': tfact(wake),
             'shift_start': tfact(shift['start']), 'projected_sleep_hours': max(0, sleep_h),
             'sleep_target_hours': CFG['sleepTargetH']}
    fb = ('Home. Next up: leave for the farm at %s.' % clock(a['leaveHome']) +
          (' Projected sleep is %d hours.' % max(0, sleep_h) if short else ''))
    return result(message('home_overnight_short_sleep' if short else 'home_overnight', facts,
                          ['leave_for_farm_at'] + (['projected_sleep_hours'] if short else [])),
                  leave_alert('farm', a['leaveHome'], now, 'farm', to_farm), fb)


def todays_events(events, t_s):
    """Calendar events for the day of t_s that are not the shift: timed ones not over yet, and
    all-day ones. Each {title, start (epoch or None), all_day}. At most 3, in time order."""
    day = day_key(t_s)
    out = []
    for raw in events or []:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get('title') or raw.get('summary') or '').strip()
        if not title:
            continue
        if raw.get('all_day'):
            first, last = str(raw.get('start') or '')[:10], str(raw.get('end') or raw.get('start') or '')[:10]
            if first <= day and (day < last or first == last):
                out.append({'title': title, 'start': None, 'all_day': True})
            continue
        ev = norm_event(raw)
        if is_shift(ev) or day_key(ev['start']) != day or ev['end'] <= t_s:
            continue
        out.append({'title': title, 'start': ev['start'], 'all_day': False})
    out.sort(key=lambda e: (e['start'] is not None, e['start'] or 0))
    return out[:3]


def morning_rundown(t_s, now, events, leg, farm_done=False):
    """Good morning, once a day at home: today's shift, calendar events, the stops that still fit,
    and the next stop with the latest time to leave for it. Replaces the welcome-home for that call."""
    now = max(now, t_s)
    shift = shift_on(shifts_from(events), day_key(t_s))
    evs = todays_events(events, t_s)
    facts = {'greeting': 'Good morning, Jordan.', 'day': long_day(t_s),
             'events': [{'title': e['title'], 'start': tfact(e['start']) if e['start'] else None, 'all_day': e['all_day']}
                        for e in evs]}
    must = ['greeting']
    lines = ['Good morning, Jordan.']
    ev_words = ', '.join(e['title'] + ('' if e['all_day'] else ' at ' + time_of_day(e['start'])) for e in evs)
    alerts = None
    if not shift:
        facts['day_off'] = True
        must.append('day_off')
        if evs:
            must.append('events')
            lines.append('No shift today. On the calendar: %s.' % ev_words)
        else:
            lines.append('Day off today, nothing on the calendar.')
        return result(message('morning_rundown', facts, must, sentences=3), alerts, ' '.join(lines))
    facts.update(shift_start=tfact(shift['start']), shift_end=tfact(shift['end']), day_off=False)
    must.append('shift_start')
    lines.append('Work %s to %s.' % (time_of_day(shift['start']), time_of_day(shift['end'])))
    if evs:
        must.append('events')
        lines.append('Also on the calendar: %s.' % ev_words)
    if shift['start'] > t_s:
        hmc_arrive = shift['start'] - CFG['bufferM'] * 60
        leave_work = hmc_arrive - leg('home', 'hmc') * 60
        facts['leave_for_work_at'] = tfact(leave_work)
        if t_s >= leave_work:
            facts['due_at_work'] = tfact(hmc_arrive)
            must.append('due_at_work')
            lines.append('Leave for work now; you are due there at %s.' % clock(hmc_arrive))
            alerts = []
        else:
            stops = stops_from_home(t_s, shift, leg, leave_work, farm_done)
            stop, leave_at = stops[0]
            names = [PLACES[p]['say'] for p, _ in stops]
            facts.update(stops=[pfact(p) for p, _ in stops], next_stop=pfact(stop), leave_at=tfact(leave_at))
            must += ['next_stop', 'leave_at']
            order = names[0] if len(names) == 1 else ', '.join(names[:-1]) + ', then ' + names[-1]
            lines.append('Today: %s. Leave for %s by %s.' % (order, PLACES[stop]['say'], clock(leave_at)))
            tag = 'work' if stop == 'hmc' else ('farm' if stop == 'farm' else 'gym')
            alerts = leave_alert(tag, leave_at, now, stop, 'Leave for %s at %s.' % (PLACES[stop]['say'], clock(leave_at)))
    return result(message('morning_rundown', facts, must, sentences=3), alerts, ' '.join(lines))


_UNSET = object()


def plan_arrival(place, t_s, title, leg, now, shift=_UNSET, shifts=None, farm_done=False):
    now = max(now, t_s)
    if place in ('farm', 'gym', 'pf'):
        s = shift if shift is not _UNSET else shift_today(shifts or [], t_s)
        if not s:
            return result(message('no_shift_today', {'place': pfact(place)}, []), [],
                          arrived_line(place, title) + ' No shift today, so no leave-by time.')
        return farm_plan(t_s, now, s, leg, gym_for(s['start'])) if place == 'farm' else gym_plan(t_s, now, s, leg, place)
    if place == 'hmc':
        return result(message('at_work', {'place': pfact('hmc')}, [], say=False), [], '')  # at work: say nothing
    if place == 'home':
        s = shift if shift is not _UNSET else next_shift(shifts or [], t_s, CFG['homeLookaheadH'])
        return home_plan(t_s, now, s, leg, farm_done)
    t = str(title or '').strip()[:60]
    return result(message('arrived_unknown', {'place': {'id': None, 'name': t or None}}, []), None,
                  arrived_line(None, title))  # unknown stop: keep current alerts


# ── WORKDAYS FOR THE RIVIA CALENDAR ─────────────────────────────────────

def build_workday(shift, prev_shift, legs, gym_id):
    gym = gym_id or 'gym'
    a = workday_anchors(shift['start'], legs)
    blocks, problems = [], []

    def add(title, place, start, end, kind, desc):
        if end > start:
            blocks.append({'title': title, 'place': place, 'start': start, 'end': end, 'kind': kind, 'desc': desc})

    wake_up_s = CFG['wakeUpM'] * 60
    sleep_s = CFG['sleepTargetH'] * 3600
    if prev_shift:
        wake = prev_shift['end'] + legs['hmcHome'] * 60 + CFG['windDownM'] * 60 + sleep_s
    else:
        wake = a['leaveHome'] - wake_up_s
        add('Sleep', 'home', wake - sleep_s, wake, 'detail', '%d hours.' % CFG['sleepTargetH'])
    if wake + wake_up_s > a['leaveHome']:
        problems.append('Not enough morning: up at %s but leaving at %s leaves under %d minutes to get ready.' %
                        (clock(wake), clock(a['leaveHome']), CFG['wakeUpM']))
    add('Wake-up', 'home', wake, min(wake + wake_up_s, a['leaveHome']), 'detail', 'Wake-up buffer.')
    add('Home flex', 'home', wake + wake_up_s, a['leaveHome'], 'detail', 'Spare time at home.')
    add('Ride: home to the farm', None, a['leaveHome'], a['farmStart'], 'ride', '%d min.' % legs['homeFarm'])
    add('Farm', 'farm', a['farmStart'], a['leaveFarm'], 'stop',
        '%d minutes. Leave for %s at %s.' % (CFG['farmM'], PLACES[gym]['say'], clock(a['leaveFarm'])))
    add('Ride: farm to ' + PLACES[gym]['say'], None, a['leaveFarm'], a['workoutStart'], 'ride', '%d min.' % legs['farmGym'])
    add('Gym', gym, a['workoutStart'], a['leaveGym'], 'stop', 'At %s. Workout %s to %s, then pre-work until %s.' %
        (gym_name(gym), clock(a['workoutStart']), clock(a['preworkStart']), clock(a['leaveGym'])))
    add('Ride: ' + ('Planet Fitness' if gym == 'pf' else 'gym') + ' to work', None, a['leaveGym'], a['hmcArrive'],
        'ride', '%d min.' % legs['gymHmc'])
    add('Arrive early', 'hmc', a['hmcArrive'], shift['start'], 'detail', '%d minutes before the shift.' % CFG['bufferM'])
    home = shift['end'] + legs['hmcHome'] * 60
    add('Ride: work to home', None, shift['end'], home, 'ride', '%d min.' % legs['hmcHome'])
    add('Home', 'home', home, home + CFG['windDownM'] * 60, 'stop', 'Wind-down.')
    add('Sleep', 'home', home + CFG['windDownM'] * 60, home + CFG['windDownM'] * 60 + sleep_s, 'detail',
        '%d hours.' % CFG['sleepTargetH'])
    return {'blocks': blocks, 'problems': problems, 'anchors': a}


def gym_hours_problem(gym_id, start_s, end_s):
    hours = GYM_HOURS[gym_id]
    weekend = is_weekend(start_s)
    open_h = hours['weekendOpen'] if weekend else hours['weekdayOpen']
    close_h = hours['weekendClose'] if weekend else hours['weekdayClose']
    start_h = minutes_of_day(start_s) / 60.0
    end_h = start_h + (end_s - start_s) / 3600.0
    if start_h >= open_h and end_h <= close_h:
        return ''
    name = gym_name(gym_id)
    return '%s is open %s to %s %s, but this gym block runs %s to %s.' % (
        name[0].upper() + name[1:], hour12(open_h), hour12(close_h),
        'on weekends' if weekend else 'on weekdays', clock(start_s), clock(end_s))


def check_day(blocks, shift):
    """Run in code before anything is written."""
    problems = []
    all_b = sorted(blocks + [{'title': 'your shift', 'place': 'hmc', 'start': shift['start'], 'end': shift['end'],
                              'kind': 'shift'}], key=lambda x: x['start'])
    for i in range(1, len(all_b)):
        p, b = all_b[i - 1], all_b[i]
        if b['start'] < p['end']:
            problems.append('%s overlaps %s.' % (p['title'], b['title']))
        elif b['start'] > p['end']:
            problems.append('Gap between %s and %s.' % (p['title'], b['title']))
        if p['kind'] != 'ride' and b['kind'] != 'ride' and p['place'] and b['place'] and p['place'] != b['place']:
            problems.append('No ride between %s and %s.' % (p['title'], b['title']))
    early = [b for b in blocks if b['title'] == 'Arrive early']
    if not early or early[0]['start'] > shift['start'] - CFG['bufferM'] * 60:
        problems.append('Not at work %d minutes early.' % CFG['bufferM'])
    for b in blocks:
        if b['place'] in GYM_HOURS:
            note = gym_hours_problem(b['place'], b['start'], b['end'])
            if note:
                problems.append(note)
    return problems


def ride_table(gym_id, leg):
    rides = [('homeFarm', 'home', 'farm'), ('farmGym', 'farm', gym_id), ('gymHmc', gym_id, 'hmc'), ('hmcHome', 'hmc', 'home')]
    legs, parts = {}, []
    for key, a, b in rides:
        mins, src = leg.info(a, b)
        legs[key] = mins if mins is not None else 0
        parts.append('%s to %s %s min (%s)' % (PLACES[a]['say'], PLACES[b]['say'], mins, src))
    return legs, 'Rides via %s: %s.' % (gym_name(gym_id), ', '.join(parts))


def calendar_event(b, day):
    return {
        'summary': b['title'],
        'start': iso(b['start']),
        'end': iso(b['end']),
        'location': PLACES[b['place']]['addr'] if b['place'] else '',
        'description': b['desc'] + ' Written by Rivia; the next plan replaces it. [rivia day=%s kind=%s]' % (day, b['kind']),
    }


def block_out(b):
    return {'title': b['title'], 'place': b['place'], 'kind': b['kind'], 'start': when(b['start']),
            'end': when(b['end']), 'desc': b['desc']}


# ── RIDE LOGGING ────────────────────────────────────────────────────────

def ride_note(minutes, usual):
    if usual is None or not usual or usual <= 0:
        return ''
    if minutes > CFG['cleanHigh'] * usual:
        return 'Over %sx the usual %s min: probably a stop on the way.' % (fmt_num(CFG['cleanHigh']), fmt_num(usual))
    if minutes < CFG['cleanLow'] * usual:
        return 'Under %sx the usual %s min: probably a geofence firing late.' % (fmt_num(CFG['cleanLow']), fmt_num(usual))
    return ''


def fmt_num(x):
    return str(int(x)) if float(x) == int(x) else str(x)


def too_fast(frm, to, secs, minutes):
    """A note if the straight line between the two pins needs more than maxKmh; else ''."""
    pins = place_pins()
    if frm not in pins or to not in pins or secs <= 0:
        return ''
    km = meters_between(pins[frm], pins[to]) / 1000.0
    kmh = km / (secs / 3600.0)
    if kmh <= CFG['maxKmh']:
        return ''
    return 'Too fast: %.1f km straight-line in %s min (%d km/h), so not learned from.' % (
        km, fmt_num(minutes), round(kmh))


# ── COMMANDS ────────────────────────────────────────────────────────────

def cmd_place(inp):
    raw = place_of(inp.get('coords'), inp.get('title'))
    t = to_epoch(inp['time']) if inp.get('time') else now_s()
    return {'place': gym_for_place(raw, t) if raw else None}


def farm_visited(rides, t_s):
    """A ride to or from the farm earlier the same day (excluded rides count: he was there)."""
    day = day_key(t_s)
    for r in rides:
        if r.get('day') == day and 'farm' in (r.get('from'), r.get('to')):
            try:
                if to_epoch(r.get('arrived'), 'arrived') <= t_s:
                    return True
            except RiviaError:
                pass
    return False


def cmd_arrival(inp):
    t_s = to_epoch(inp.get('time'), 'time')
    now = to_epoch(inp['now'], 'now') if inp.get('now') else now_s()
    if t_s > now + 300:
        raise RiviaError('bad_time', 'time is more than 5 minutes in the future.')
    title = inp.get('title') or ''
    if str(title).strip().lower() == 'test':
        at = max(now, t_s) + 120
        test_alert = {'id': 'plumbing-test', 'at': tfact(at),
                      'message': message('plumbing_test', {'test_alert_at': tfact(at)}, ['test_alert_at']),
                      'fallback_text': 'Test alert from the Rivia scheduler.'}
        return {'place': None, 'arrived': when(t_s),
                'message': message('plumbing_test', {'test_alert_at': tfact(at)}, ['test_alert_at']),
                'alerts': [test_alert], 'fallback_text': 'Arrived at Test. Test alert in two minutes.'}
    place = inp.get('place') or None
    reg = place_registry() if place and place not in PLACES and place != 'gymday' else None
    if reg is not None and not (place in reg and reg[place]['custom']):
        raise RiviaError('bad_place', 'Unknown place id: %s.' % place)
    if not place:
        place = place_of(inp.get('coords'), title)
    place = gym_for_place(place, t_s) if place else None
    if place and place not in PLACES:  # one of Jordan's own places: name it, keep the alerts
        name = (reg if reg is not None else place_registry()).get(place, {}).get('name') or place
        return {'place': place, 'arrived': when(t_s),
                'message': message('arrived_unknown', {'place': {'id': place, 'name': name}}, []),
                'alerts': None, 'fallback_text': 'Arrived at %s.' % name}
    shifts = shifts_from(inp.get('events'))
    rides = load_rides()
    leg = Legs(inp.get('maps'), rides)
    farm_done = bool(inp.get('farm_done')) or farm_visited(rides, t_s)
    plan = plan_arrival(place, t_s, title, leg, now, shifts=shifts, farm_done=farm_done)
    leg.check()
    return {'place': place, 'arrived': when(t_s), 'message': plan['message'], 'alerts': plan['alerts'],
            'fallback_text': plan['speak']}


def cmd_plan(inp):
    now = to_epoch(inp['now'], 'now') if inp.get('now') else now_s()
    days = inp.get('days')
    if not days:
        first = int(inp.get('first', 1))
        count = int(inp.get('count', 1))
        if not (0 <= first <= 7 and 1 <= count <= 7):
            raise RiviaError('bad_days', 'first must be 0 to 7 and count 1 to 7.')
        today = dt(now).date()
        days = [(today + timedelta(days=first + i)).isoformat() for i in range(count)]
    if not isinstance(days, list) or not 1 <= len(days) <= 7:
        raise RiviaError('bad_days', 'days must be a list of 1 to 7 dates like 2026-11-09.')
    for d in days:
        try:
            date.fromisoformat(d)
        except (TypeError, ValueError):
            raise RiviaError('bad_days', 'Not a date: %s.' % d)
    shifts = shifts_from(inp.get('events'))
    leg = Legs(inp.get('maps'))
    out_days, ride_notes = [], []
    for d in days:
        noon = noon_of(d)
        prev_day = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
        rec = {'day': d, 'label': long_day(noon), 'status': '', 'shift': None, 'gym': None,
               'blocks': [], 'problems': [], 'calendar_events': []}
        out_days.append(rec)
        shift = shift_on(shifts, d)
        if not shift:
            rec['status'] = 'no_shift'
            rec['message'] = message('plan_no_shift', {'day': rec['label']}, ['day'])
            continue
        rec['shift'] = {'title': shift['title'], 'start': when(shift['start']), 'end': when(shift['end'])}
        gym_id = gym_for(shift['start'])
        rec['gym'] = gym_id
        legs, note = ride_table(gym_id, leg)
        if note not in ride_notes:
            ride_notes.append(note)
        plan = build_workday(shift, shift_on(shifts, prev_day), legs, gym_id)
        rec['blocks'] = [block_out(b) for b in plan['blocks']]
        rec['problems'] = plan['problems'] + check_day(plan['blocks'], shift)
        leave_home = [b for b in plan['blocks'] if b['title'] == 'Ride: home to the farm']
        day_facts = {'day': rec['label'], 'shift_start': tfact(shift['start']), 'shift_end': tfact(shift['end']),
                     'gym': pfact(gym_id)}
        if leave_home:
            day_facts['leave_home_at'] = tfact(leave_home[0]['start'])
        if rec['problems']:
            rec['message'] = message('plan_problems', dict(day_facts, problems=rec['problems']), ['day', 'problems'])
        else:
            rec['message'] = message('plan_ready', day_facts, ['day', 'shift_start', 'leave_home_at'])
        if rec['problems']:
            rec['status'] = 'not_written'
        elif plan['blocks'][0]['start'] <= now:
            rec['status'] = 'under_way'
        else:
            rec['status'] = 'planned'
            rec['calendar_events'] = [calendar_event(b, d) for b in plan['blocks']]
    leg.check()
    return {'days': out_days, 'rides': ride_notes}


def cmd_log_ride(inp):
    rid = str(inp.get('ride_id') or '').strip()
    if not rid or len(rid) > 100:
        raise RiviaError('bad_ride_id', 'ride_id is required (up to 100 characters).')
    con = db_open()
    try:
        need_restore(con)
        row = con.execute('select minutes, used from rides where ride_id=?', (rid,)).fetchone()
        if row:
            return {'stored': False, 'duplicate': True, 'minutes': row[0], 'used': bool(row[1]),
                    'note': 'Already stored; nothing changed.', 'backup_due': False}
        rides = read_rides(con)
        frm, to = inp.get('from') or None, inp.get('to') or None
        dep = to_epoch(inp.get('departed'), 'departed')
        arr = to_epoch(inp.get('arrived'), 'arrived')
        secs = arr - dep
        minutes = round(secs / 6) / 10
        reg = None
        for p in (frm, to):
            if p is not None and p not in PLACES:
                reg = place_registry() if reg is None else reg
                if not (p in reg and reg[p]['custom']):
                    raise RiviaError('bad_place', 'Unknown place id: %s.' % p)
        note, used = '', False
        if not frm or not to:
            note = 'Start or end is not a known place, so not learned from.'
        elif frm == to:
            note = 'Started and ended at the same place, so not learned from.'
        elif frm not in PLACES or to not in PLACES:
            note = 'A custom place, so not learned from.'
        elif not (60 < secs <= CFG['rideMaxH'] * 3600):
            note = 'Under a minute or over %d hours, so not learned from.' % CFG['rideMaxH']
        else:
            note = too_fast(frm, to, secs, minutes)
            if not note:
                leg = Legs(inp.get('maps'), rides)
                trips = leg.trips.get(frm + '>' + to, [])
                learned = len(trips) >= CFG['learnMin']
                usual = rank_min(trips, 0.5) if learned else leg.info(frm, to, True)[0]
                note = ride_note(minutes, usual)
                if not note and learned and minutes < CFG['fastRatio'] * usual:
                    note = 'Under %sx the usual %s min from your rides: probably a geofence firing late.' % (
                        fmt_num(CFG['fastRatio']), fmt_num(usual))
            used = note == ''
        rec = {
            'ride_id': rid, 'day': day_key(dep), 'from': frm, 'to': to,
            'departed': iso(dep), 'arrived': iso(arr), 'left_local': time_of_day(dep), 'arrived_local': time_of_day(arr),
            'minutes': minutes, 'used': used, 'note': note,
            'distance_km': inp.get('distance_km'), 'avg_kmh': inp.get('avg_kmh'), 'polyline': inp.get('polyline'),
            'logged': iso(now_s()),
        }
        insert_ride(con, rec)
        bump_change(con)
        return {'stored': True, 'duplicate': False, 'minutes': minutes, 'used': used, 'note': note, 'backup_due': True}
    finally:
        con.close()


def exclude_ride(rid, reason):
    """Mark a ride as not learned from (used=0, note 'excluded: <reason>'). Never deletes it.
    The old used/note are kept in meta so include_ride can put them back exactly."""
    rid = str(rid or '').strip()
    reason = ' '.join(str(reason or '').split())
    if not rid:
        raise RiviaError('bad_ride_id', 'ride_id is required.')
    if not reason:
        raise RiviaError('bad_reason', 'Give a reason for excluding the ride.')
    con = db_open()
    try:
        row = con.execute('select used, note from rides where ride_id=?', (rid,)).fetchone()
        if not row:
            raise RiviaError('no_ride', 'No ride with id %s.' % rid)
        used, note = bool(row[0]), row[1] or ''
        if note.startswith('excluded:'):
            return {'ride_id': rid, 'changed': False, 'used': used, 'note': note, 'backup_due': False}
        set_meta(con, 'ride_excluded.' + rid, json.dumps({'used': used, 'note': note}))
        new_note = 'excluded: ' + reason
        con.execute('update rides set used=0, note=? where ride_id=?', (new_note, rid))
        bump_change(con)
        return {'ride_id': rid, 'changed': True, 'used': False, 'note': new_note,
                'was': {'used': used, 'note': note}, 'backup_due': True}
    finally:
        con.close()


def include_ride(rid):
    """Undo exclude_ride: put back the ride's old used/note."""
    rid = str(rid or '').strip()
    if not rid:
        raise RiviaError('bad_ride_id', 'ride_id is required.')
    con = db_open()
    try:
        row = con.execute('select used, note from rides where ride_id=?', (rid,)).fetchone()
        if not row:
            raise RiviaError('no_ride', 'No ride with id %s.' % rid)
        if not (row[1] or '').startswith('excluded:'):
            raise RiviaError('not_excluded', 'Ride %s is not excluded.' % rid)
        try:
            was = json.loads(get_meta(con, 'ride_excluded.' + rid) or '{}')
        except ValueError:
            was = {}
        used, note = bool(was.get('used', True)), str(was.get('note') or '')
        con.execute('update rides set used=?, note=? where ride_id=?', (1 if used else 0, note, rid))
        con.execute('delete from meta where key=?', ('ride_excluded.' + rid,))
        bump_change(con)
        return {'ride_id': rid, 'changed': True, 'used': used, 'note': note, 'backup_due': True}
    finally:
        con.close()


def cmd_legs(inp):
    pairs = inp.get('pairs')
    if not isinstance(pairs, list) or not pairs:
        raise RiviaError('bad_pairs', 'pairs must be a list like [["farm", "pf"]].')
    leg = Legs(inp.get('maps'))
    out = []
    for p in pairs:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise RiviaError('bad_pairs', 'Each pair is [from, to].')
        mins, src = leg.info(p[0], p[1])
        out.append({'from': p[0], 'to': p[1], 'minutes': mins, 'source': src})
    leg.check()
    return {'legs': out}


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def cmd_backup(inp):
    con = db_open()
    try:
        need_restore(con)
        change = get_meta(con, 'change', '0')
        os.makedirs(backup_dir(), exist_ok=True)
        db_out = os.path.join(backup_dir(), 'scheduler.db')
        json_out = os.path.join(backup_dir(), 'scheduler.json')
        if (not inp.get('force') and change == get_meta(con, 'uploaded_change')
                and os.path.exists(db_out) and os.path.exists(json_out)):
            return {'upload': False, 'reason': 'No new rides since the last upload.', 'rides': ride_count(con)}
        tmp = db_out + '.tmp'
        if os.path.exists(tmp):
            os.remove(tmp)
        dst = sqlite3.connect(tmp)
        con.backup(dst)  # consistent copy even if a write is in progress
        dst.close()
        os.replace(tmp, db_out)
        rides = read_rides(con)
        with open(json_out + '.tmp', 'w') as f:
            json.dump({'version': VERSION, 'exported': iso(now_s()), 'change': change, 'rides': rides}, f)
        os.replace(json_out + '.tmp', json_out)
        return {'upload': True, 'db_path': db_out, 'json_path': json_out, 'rides': len(rides),
                'bytes': os.path.getsize(db_out), 'sha256': sha256_of(db_out), 'change': change}
    finally:
        con.close()


def cmd_backup_uploaded(inp):
    change = str(inp.get('change') or '').strip()
    if not change:
        raise RiviaError('bad_change', 'change is required: copy it from the backup reply.')
    con = db_open()
    try:
        set_meta(con, 'uploaded_change', change)
        set_meta(con, 'uploaded_at', iso(now_s()))
        return {'marked': True, 'change': change, 'current_change': get_meta(con, 'change', '0')}
    finally:
        con.close()


def rides_from_copy(path):
    """Rides from a Drive copy: a scheduler.db or a scheduler.json made by backup."""
    if not path or not os.path.isfile(path):
        raise RiviaError('bad_copy', 'No file at %s.' % path)
    with open(path, 'rb') as f:
        head = f.read(16)
    if head == b'SQLite format 3\x00':
        try:
            src = sqlite3.connect('file:%s?mode=ro' % path, uri=True)
            try:
                if src.execute('pragma integrity_check').fetchone()[0] != 'ok':
                    raise RiviaError('bad_copy', 'The copy failed SQLite integrity_check.')
                if not src.execute("select 1 from sqlite_master where type='table' and name='rides'").fetchone():
                    raise RiviaError('bad_copy', 'The copy has no rides table.')
                return read_rides(src)
            finally:
                src.close()
        except sqlite3.Error as e:
            raise RiviaError('bad_copy', 'The copy is not a readable database: %s' % e)
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        raise RiviaError('bad_copy', 'The copy is neither a SQLite database nor backup JSON.')
    if not isinstance(data, dict) or not isinstance(data.get('rides'), list):
        raise RiviaError('bad_copy', 'The JSON copy has no rides list.')
    return data['rides']


def cmd_restore(inp):
    rides = rides_from_copy(inp.get('path'))
    con = db_open()
    try:
        before = ride_count(con)
        added = sum(insert_ride(con, r) for r in rides if isinstance(r, dict))
        con.execute("delete from meta where key='fresh'")
        if added:
            bump_change(con)
        return {'added': added, 'in_copy': len(rides), 'rides_before': before, 'rides': ride_count(con)}
    finally:
        con.close()


def cmd_init_db(inp):
    con = db_open()
    try:
        if not is_fresh(con):
            return {'initialized': False, 'rides': ride_count(con), 'note': 'Not a new database; nothing changed.'}
        con.execute("delete from meta where key='fresh'")
        return {'initialized': True, 'rides': ride_count(con)}
    finally:
        con.close()


# ── SELF-TEST (checks live in rivia_planner_tests.py, next to this file) ─

def selftest():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import rivia_planner_tests
    except ImportError as e:
        raise RiviaError('no_tests', 'rivia_planner_tests.py must sit next to rivia_planner.py (%s).' % e)
    return rivia_planner_tests.run()


COMMANDS = {'arrival': cmd_arrival, 'plan': cmd_plan, 'log-ride': cmd_log_ride, 'legs': cmd_legs, 'place': cmd_place,
            'backup': cmd_backup, 'backup-uploaded': cmd_backup_uploaded, 'restore': cmd_restore,
            'init-db': cmd_init_db}


def db_flag(out):
    """Every reply from a new, empty database says so, so the Bot restores before relying on it."""
    try:
        if os.path.exists(db_file()):
            con = sqlite3.connect(db_file())
            try:
                if con.execute("select value from meta where key='fresh'").fetchone():
                    out['db_fresh'] = True
            finally:
                con.close()
    except sqlite3.Error:
        pass
    return out


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    try:
        if cmd == 'selftest':
            res = selftest()
            print(json.dumps(res, indent=1))
            return 0 if res['all_passed'] else 1
        if cmd not in COMMANDS:
            raise RiviaError('bad_command', 'Unknown command: %s. Use one of: %s, selftest.' % (cmd, ', '.join(COMMANDS)))
        raw = sys.stdin.read()
        try:
            inp = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise RiviaError('bad_json', 'Input is not valid JSON.')
        if not isinstance(inp, dict):
            raise RiviaError('bad_json', 'Input must be a JSON object.')
        out = {'ok': True, 'command': cmd}
        out.update(COMMANDS[cmd](inp))
        print(json.dumps(db_flag(out), indent=1))
        return 0
    except RiviaError as e:
        err = {'code': e.code, 'msg': str(e)}
        err.update(e.extra)
        print(json.dumps(db_flag({'ok': False, 'command': cmd, 'error': err}), indent=1))
        return 1
    except Exception as e:  # noqa: BLE001
        print(json.dumps({'ok': False, 'command': cmd, 'error': {'code': 'internal', 'msg': repr(e)}}, indent=1))
        return 1


if __name__ == '__main__':
    sys.exit(main())
