# rivia_places.py: places, pins, arrivals and rides, in places.db. Ships with core.
#
# Tools (app door): app_places, app_pin_place, app_arrival, app_open, app_log_ride. Their names
# and arguments are unchanged by the core split, and so are the stored action ids
# (sched.places, sched.pin_place, sched.arrival, sched.app_open, sched.log_ride).
#
# Places never imports scheduling (rivia_sched, rivia_planner, rivia_gcal). Scheduling reads
# from places and plugs in here through HOOKS when it loads:
#   plan_arrival     the leave-by plan and welcome line for an arrival
#   morning_rundown  the good-morning rundown
#   ride_estimate    Jordan's own ride estimates (for the "usual" in a ride's sanity check)
#   after_ride       the Drive backup after a new ride
# A scheduling failure (it did not load, its planner selftest failed, or a hook raised) never
# blocks places: arrivals are still recorded and answered plainly, rides are still stored.
import json
import math
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from typing import Union

from fastmcp.tools import tool

import rivia_settings as settings
import rivia_store as store
import rivia_time as rt
import rivia_tools as tools
from rivia_approvals import SettingsError
from rivia_time import RiviaError
from rivia_tools import fail, ok_reply

# ── the five core places (seed and fallback; names and pins live in the settings store) ──
CORE_PLACES = {
    'home': {'say': 'home', 'addr': '600 E Main St, Middletown, PA'},
    'farm': {'say': 'the farm', 'addr': '2003 Vine Street, Middletown, PA 17057'},
    'gym': {'say': 'the gym', 'addr': '30 Conference Dr, Hershey, PA 17033'},
    'pf': {'say': 'Planet Fitness', 'addr': '1170 Mae St, Hummelstown, PA 17036'},
    'hmc': {'say': 'work', 'addr': '500 University Dr, Hershey, PA 17033'},
}
RIDE_PLACES = ("farm", "gym", "hmc", "home", "pf")  # alphabetical; the core places
# Jordan's own places (Giant, ...): ids look like this. A custom place exists once it has a
# standing name; status is active or removed.
CUSTOM_ID = re.compile(r'^[a-z][a-z0-9_]{0,23}$')
PLACE_FIELDS = ("pin", "radius_m", "name", "status")
PLACE_DOORS = ("termux", "admin", "web", "app", "system")
RADIUS_MIN, RADIUS_MAX, RADIUS_STEP = 50, 400, 25
PLACE_RADIUS_M = 400       # a position this close to a place without its own size counts as it
_PLACES_SAY_DEFAULT = {pid: p["say"] for pid, p in CORE_PLACES.items()}
DUP_ARRIVAL_S = 20 * 60    # same place again within 20 min: a repeat, not a new arrival
LAST_ARRIVAL_KEY = "last_arrival"
META_LAST_DEPARTURE = "last_departure"         # {place, time}: the app's left_at for a same-place return
META_MORNING_RUNDOWN = "morning_rundown_date"  # YYYY-MM-DD the good-morning rundown was given
MORNING_FROM_H = 4                             # the rundown starts at 4:00 AM local time
RUNDOWN_CUTOFF = (14, 30)                      # and stops at 2:30 PM: later opens give none

# Ride sanity checks (the planner's learning keeps its own, configurable copy of these).
RIDE_CFG = {'rideMaxH': 3, 'cleanLow': 0.25, 'cleanHigh': 2, 'maxKmh': 60, 'fastRatio': 0.5,
            'learnMin': 3, 'learnKeep': 10}

HOOKS = {"plan_arrival": None, "morning_rundown": None, "ride_estimate": None, "after_ride": None}

PLACES_DESCRIPTION = (
    "Jordan's places for geofences: each place's id, spoken name, pin (lat, lng), "
    "and geofence radius in meters."
)
ARRIVAL_DESCRIPTION = (
    "Jordan just arrived at a place. Logs a ride if one is given (safe to repeat "
    "on the same ride_id), reads the calendar, and returns the leave-by message, "
    "alerts, and fallback_text. title 'test' returns a plumbing alert two minutes out."
)
LOG_RIDE_DESCRIPTION = (
    "Log one bike ride between two of Jordan's places. Safe to repeat on the same "
    "ride_id (returns duplicate and does not store a second copy)."
)
PIN_PLACE_DESCRIPTION = (
    "Pin one of Jordan's places at the phone's current position. coords is lat,lng from "
    "the phone. place re-pins an existing place (id or name); name adds a new place. "
    "radius_m (50 to 400) sets a new place's zone size. A pin from this door waits for "
    "approval; a pending result has not happened yet."
)
APP_OPEN_DESCRIPTION = (
    "The Rivia app was opened. The first open each day from 4:00 AM to 2:29 PM at home "
    "returns the good-morning rundown (speak it); every other open returns a quiet reply (say false)."
)

# Place-owned settings (moved from scheduling's catalog in the core split).
SCALARS = {
    "geofence_radius_meters": {"planner": None, "default": 150, "min": 100, "max": 400, "type": "int",
                               "unit": "meters", "label": "Geofence radius", "day": False, "ask": False,
                               "group": "system", "doors": ("termux", "admin")},
    "arrival_late_minutes":   {"planner": None, "default": 10, "min": 2, "max": 120, "type": "int",
                               "unit": "minutes", "label": "Late arrival cutoff", "day": False, "ask": False,
                               "group": "system", "doors": ("termux", "admin")},
}

SCHEMA = """
create table if not exists rides(
    ride_id text primary key, day text, from_place text, to_place text,
    departed text, arrived text, left_local text, arrived_local text,
    minutes real, used integer, note text, distance_km real, avg_kmh real,
    polyline text, logged text);
""" + settings.SETTINGS_SCHEMA
RIDE_FIELDS = ['ride_id', 'day', 'from', 'to', 'departed', 'arrived', 'left_local', 'arrived_local',
               'minutes', 'used', 'note', 'distance_km', 'avg_kmh', 'polyline', 'logged']
_META_KEYS = ("change", "uploaded_change", "uploaded_at", "created", "fresh", LAST_ARRIVAL_KEY,
              META_LAST_DEPARTURE, META_MORNING_RUNDOWN)
MIGRATE = [
    ("rides", "order by rowid"),
    ("settings", "where key like 'place.%' or key in ('geofence_radius_meters', 'arrival_late_minutes')"),
    ("meta", "where key in (%s) or key like 'ride_excluded.%%'" % ", ".join("'%s'" % k for k in _META_KEYS)),
]


def set_hook(name, fn):
    if name not in HOOKS:
        raise KeyError(name)
    HOOKS[name] = fn


# ── places.db ──────────────────────────────────────────────────────────

def db_file():
    return store.db_path("places")


def places_file():
    """places.json: the planner's view of the pins (rewritten after every place change)."""
    return os.path.join(store.rivia_dir(), "places.json")


def backup_dir():
    return os.path.join(store.rivia_dir(), "backup")


get_meta = store.get_meta
set_meta = store.set_meta


def bump_change(con):
    """Every ride write bumps this counter."""
    set_meta(con, 'change', int(get_meta(con, 'change', '0')) + 1)


def db_open():
    """Open places.db. The first open copies rides, place settings and arrival memory from
    scheduler.db. A brand-new install takes in rides.jsonl if there is one; otherwise it is
    marked fresh until restore or init-db runs (so an empty file never goes over Drive)."""
    try:
        new = not os.path.exists(db_file())
        con = store.connect("places", SCHEMA, migrate=MIGRATE)
        if new:
            info = store.migration_report(con) or {}
            had_rides = info.get("source") == "scheduler.db" and _source_has_rides()
            if not had_rides:
                set_meta(con, 'created', rt.iso(rt.now_s()))
                old = os.path.join(store.rivia_dir(), 'rides.jsonl')
                if os.path.exists(old):
                    for r in read_jsonl(old):
                        insert_ride(con, r)
                    bump_change(con)
                    os.replace(old, old + '.migrated')
                else:
                    set_meta(con, 'fresh', '1')
        return con
    except sqlite3.Error as e:
        raise RiviaError('db_error', 'places.db could not be opened: %s' % e)


def _source_has_rides():
    try:
        src = sqlite3.connect("file:%s?mode=ro" % store.scheduler_db_path(), uri=True)
        try:
            return bool(src.execute("select 1 from sqlite_master where type='table' and name='rides'").fetchone())
        finally:
            src.close()
    except sqlite3.Error:
        return False


@contextmanager
def _con(con=None):
    if con is not None:
        yield con
        return
    con = db_open()
    try:
        yield con
    finally:
        con.close()


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


def is_fresh(con):
    return get_meta(con, 'fresh') == '1'


def ride_count(con):
    return con.execute('select count(*) from rides').fetchone()[0]


def need_restore(con):
    if is_fresh(con):
        raise RiviaError('restore_first', 'places.db is new and empty. Restore the Drive copy first '
                         '(download it, then run restore), or run init-db if Drive has no copy.')


def load_rides():
    con = db_open()
    try:
        return read_rides(con)
    finally:
        con.close()


# ── where a place is ───────────────────────────────────────────────────

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
    """Every pinned place in places.json: id -> {lat, lng, name, radius_m (None = no own zone
    size), custom, removed}. The five core places plus Jordan's custom ones."""
    try:
        with open(places_file()) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for pid, p in (raw.items() if isinstance(raw, dict) else []):
        info = p if isinstance(p, dict) else {}
        custom = pid not in CORE_PLACES
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
        name = str(info.get('name') or (pid if custom else CORE_PLACES[pid]['say']))
        out[pid] = {'lat': c['lat'], 'lng': c['lng'], 'name': name, 'radius_m': radius,
                    'custom': custom, 'removed': bool(custom and info.get('removed'))}
    return out


def place_pins():
    """Pins of the places in use (core and custom, not removed)."""
    return {pid: {'lat': p['lat'], 'lng': p['lng']} for pid, p in place_registry().items() if not p['removed']}


def place_of(coords, title, default_radius=None):
    """Title first (the geofence was set for that event); position only as a fallback.
    A position matches the nearest place within that place's own zone size, or within
    default_radius (PLACE_RADIUS_M) for a place without one."""
    by_title = place_from_title(title)
    if by_title:
        return by_title
    c = parse_coords(coords)
    if not c:
        return None
    limit_default = default_radius or PLACE_RADIUS_M
    best, best_m = None, float('inf')
    for pid, p in place_registry().items():
        if p['removed']:
            continue
        m = meters_between(c, p)
        if m <= (p['radius_m'] or limit_default) and m < best_m:
            best, best_m = pid, m
    return best


def gym_for(s):
    return 'pf' if rt.dt(s).weekday() >= 5 else 'gym'


def gym_for_place(place, s):
    return gym_for(s) if place == 'gymday' else place


# ── rides ──────────────────────────────────────────────────────────────

def rank_min(lst, rank):
    """Nearest-rank percentile: 0.5 is the middle, 0.75 the 3rd-slowest of 10."""
    s = sorted(lst)
    return s[max(0, math.ceil(rank * len(s)) - 1)]


def _learned(rides):
    trips = {}
    for r in rides:
        if r.get('used') and r.get('from') and r.get('to') and isinstance(r.get('minutes'), (int, float)):
            trips.setdefault(r['from'] + '>' + r['to'], []).append(float(r['minutes']))
    return {k: v[-RIDE_CFG['learnKeep']:] for k, v in trips.items()}


def fmt_num(x):
    return str(int(x)) if float(x) == int(x) else str(x)


def ride_note(minutes, usual):
    if usual is None or not usual or usual <= 0:
        return ''
    if minutes > RIDE_CFG['cleanHigh'] * usual:
        return 'Over %sx the usual %s min: probably a stop on the way.' % (fmt_num(RIDE_CFG['cleanHigh']), fmt_num(usual))
    if minutes < RIDE_CFG['cleanLow'] * usual:
        return 'Under %sx the usual %s min: probably a geofence firing late.' % (fmt_num(RIDE_CFG['cleanLow']), fmt_num(usual))
    return ''


def too_fast(frm, to, secs, minutes):
    """A note if the straight line between the two pins needs more than maxKmh; else ''."""
    pins = place_pins()
    if frm not in pins or to not in pins or secs <= 0:
        return ''
    km = meters_between(pins[frm], pins[to]) / 1000.0
    kmh = km / (secs / 3600.0)
    if kmh <= RIDE_CFG['maxKmh']:
        return ''
    return 'Too fast: %.1f km straight-line in %s min (%d km/h), so not learned from.' % (
        km, fmt_num(minutes), round(kmh))


def _estimate(frm, to, maps):
    fn = HOOKS["ride_estimate"]
    if not fn:
        return None
    try:
        return fn(frm, to, maps)
    except Exception:  # noqa: BLE001
        return None


def log_ride(inp):
    """Store one finished ride and learn from it if it is clean (the planner's log-ride)."""
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
        dep = rt.to_epoch(inp.get('departed'), 'departed')
        arr = rt.to_epoch(inp.get('arrived'), 'arrived')
        secs = arr - dep
        minutes = round(secs / 6) / 10
        reg = None
        for p in (frm, to):
            if p is not None and p not in CORE_PLACES:
                reg = place_registry() if reg is None else reg
                if not (p in reg and reg[p]['custom']):
                    raise RiviaError('bad_place', 'Unknown place id: %s.' % p)
        note, used = '', False
        if not frm or not to:
            note = 'Start or end is not a known place, so not learned from.'
        elif frm == to:
            note = 'Started and ended at the same place, so not learned from.'
        elif frm not in CORE_PLACES or to not in CORE_PLACES:
            note = 'A custom place, so not learned from.'
        elif not (60 < secs <= RIDE_CFG['rideMaxH'] * 3600):
            note = 'Under a minute or over %d hours, so not learned from.' % RIDE_CFG['rideMaxH']
        else:
            note = too_fast(frm, to, secs, minutes)
            if not note:
                trips = _learned(rides).get(frm + '>' + to, [])
                learned = len(trips) >= RIDE_CFG['learnMin']
                usual = rank_min(trips, 0.5) if learned else _estimate(frm, to, inp.get('maps'))
                note = ride_note(minutes, usual)
                if not note and learned and minutes < RIDE_CFG['fastRatio'] * usual:
                    note = 'Under %sx the usual %s min from your rides: probably a geofence firing late.' % (
                        fmt_num(RIDE_CFG['fastRatio']), fmt_num(usual))
            used = note == ''
        rec = {
            'ride_id': rid, 'day': rt.day_key(dep), 'from': frm, 'to': to,
            'departed': rt.iso(dep), 'arrived': rt.iso(arr),
            'left_local': rt.time_of_day(dep), 'arrived_local': rt.time_of_day(arr),
            'minutes': minutes, 'used': used, 'note': note,
            'distance_km': inp.get('distance_km'), 'avg_kmh': inp.get('avg_kmh'), 'polyline': inp.get('polyline'),
            'logged': rt.iso(rt.now_s()),
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


def sha256_of(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def cmd_backup(inp):
    """A local copy of places.db for Drive, only if rides changed since the last upload."""
    con = db_open()
    try:
        need_restore(con)
        change = get_meta(con, 'change', '0')
        os.makedirs(backup_dir(), exist_ok=True)
        db_out = os.path.join(backup_dir(), 'places.db')
        json_out = os.path.join(backup_dir(), 'places-rides.json')
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
            json.dump({'version': 'places 1 (core split)', 'exported': rt.iso(rt.now_s()), 'change': change,
                       'rides': rides}, f)
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
        set_meta(con, 'uploaded_at', rt.iso(rt.now_s()))
        return {'marked': True, 'change': change, 'current_change': get_meta(con, 'change', '0')}
    finally:
        con.close()


def rides_from_copy(path):
    """Rides from a Drive copy: a places.db / scheduler.db, or a JSON export with a rides list."""
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


def cmd_rides():
    """Ride count in places.db. Termux only; no door or tool."""
    con = db_open()
    try:
        return {"ok": True, "rides": ride_count(con), "fresh": is_fresh(con)}
    finally:
        con.close()


def cmd_restore_file(path):
    """Restore rides from a Drive copy. Merges and never drops a ride that is already there."""
    path = os.path.abspath(os.path.expanduser(str(path or "").strip()))
    if not path or not os.path.isfile(path):
        raise RiviaError("bad_copy", "No file at %s." % (path or "(none)",))
    out = cmd_restore({"path": path})
    out["ok"] = True
    out["path"] = path
    return out


def cmd_start_fresh():
    """Clear the fresh-database guard (init-db). On a database in use it is a no-op."""
    out = cmd_init_db({})
    rides = cmd_rides()
    out["ok"] = True
    out["rides"] = rides["rides"]
    out["fresh"] = rides["fresh"]
    return out


# ── place settings (an owner in the settings engine) ───────────────────

def _cap(text):
    return text[:1].upper() + text[1:]


def _coerce_pin(spec, value):
    if isinstance(value, (list, tuple)) and len(value) == 2:
        value = {"lat": value[0], "lng": value[1]}
    try:
        parsed = parse_coords(value)
    except RiviaError as e:
        raise SettingsError("bad_value", str(e), step="input") from e
    if not parsed:
        raise SettingsError("bad_value", "A pin needs lat and lng.", step="input")
    return {"lat": parsed["lat"], "lng": parsed["lng"]}


def _place_spec(key, pid, field):
    core = pid in RIDE_PLACES
    say = _PLACES_SAY_DEFAULT[pid] if core else pid
    base = {"key": key, "family": "place", "place_id": pid, "field": field, "core": core,
            "day": False, "ask": True, "group": "places", "doors": PLACE_DOORS,
            "dup_needs_current": True, "undo_marks": True}
    if field == "pin":
        return dict(base, default=None, min=None, max=None, type="pin", unit="lat,lng",
                    label="%s pin" % _cap(say), coerce=_coerce_pin)
    if field == "radius_m":
        # Zone sizes apply at once from the app door (small, bounded, undoable).
        return dict(base, default=None, min=RADIUS_MIN, max=RADIUS_MAX, type="int", unit="meters",
                    label="%s zone size" % _cap(say), app_now=True)
    if field == "name":
        return dict(base, default=say if core else None, min=1, max=40, type="name", unit="text",
                    label="%s name" % _cap(say))
    if field == "status":
        if core:
            raise SettingsError("core_place", "%s is a core place and can't be removed." % _cap(say), step="input")
        return dict(base, default="active", min=None, max=None, type="choice", choices=("active", "removed"),
                    unit="status", label="%s status" % _cap(say))
    raise SettingsError("bad_key", "Unknown setting: %s." % key, step="input")


def parse_key(key):
    """Spec for a place-owned key, or None if the key is not ours."""
    if key in SCALARS:
        return dict(SCALARS[key], key=key, family="scalar")
    if key.startswith("place."):
        parts = key.split(".")
        if len(parts) != 3 or not (parts[1] in RIDE_PLACES or CUSTOM_ID.match(parts[1])):
            raise SettingsError("bad_key", "Unknown setting: %s." % key, step="input")
        return _place_spec(key, parts[1], parts[2])
    return None


def _custom_place_ids(con):
    """Jordan's own places (active and removed): every custom id with a standing name."""
    ids = set()
    for (key,) in con.execute("select key from settings where key like 'place.%.name'"):
        pid = key.split(".")[1]
        if pid not in RIDE_PLACES and CUSTOM_ID.match(pid):
            ids.add(pid)
    return sorted(ids)


def custom_place_ids():
    with _con() as c:
        return _custom_place_ids(c)


def setting_keys(con):
    out = list(SCALARS)
    for pid in RIDE_PLACES:
        out += ["place.%s.%s" % (pid, f) for f in ("pin", "radius_m", "name")]
    for pid in _custom_place_ids(con):
        out += ["place.%s.%s" % (pid, f) for f in PLACE_FIELDS]
    return out


def _rval(resolved, key):
    return (resolved.get(key) or (None, None))[0]


def geofence_radius_m(resolved=None, con=None):
    default = SCALARS["geofence_radius_meters"]["default"]
    if resolved is not None and "geofence_radius_meters" in resolved:
        val = resolved["geofence_radius_meters"][0]
        return int(val if val is not None else default)
    val = settings.resolved("geofence_radius_meters")[0]
    return int(val if val is not None else default)


def place_radius_m(resolved, pid):
    """A place's zone size: its own place.<id>.radius_m, else geofence_radius_meters."""
    own = _rval(resolved, "place.%s.radius_m" % pid)
    return int(own) if own is not None else geofence_radius_m(resolved)


def _place_status(resolved, pid):
    return "active" if pid in RIDE_PLACES else (_rval(resolved, "place.%s.status" % pid) or "active")


def _place_name(resolved, pid):
    return _rval(resolved, "place.%s.name" % pid) or _PLACES_SAY_DEFAULT.get(pid) or pid


def _pinned_places(resolved):
    """Active places with a pin, core first (alphabetical) then custom: id -> pin."""
    ids = []
    for key in resolved:
        parts = key.split(".")
        if len(parts) == 3 and parts[0] == "place" and parts[2] == "pin" and parts[1] not in ids:
            ids.append(parts[1])
    ids.sort(key=lambda p: (p not in RIDE_PLACES, p))
    pins = {}
    for pid in ids:
        val = _rval(resolved, "place.%s.pin" % pid)
        if val and isinstance(val, dict) and "lat" in val and "lng" in val and _place_status(resolved, pid) == "active":
            pins[pid] = val
    return pins


def pin_spacing_problems(resolved):
    """Zones must not overlap: two pins at least the sum of their zone sizes apart."""
    pins = _pinned_places(resolved)
    problems = []
    ids = list(pins)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            d = meters_between(pins[a], pins[b])
            ra, rb = place_radius_m(resolved, a), place_radius_m(resolved, b)
            if d < ra + rb:
                problems.append(
                    "The %s and %s pins are %.1f m apart; they must be at least %d m "
                    "apart (zones of %d m and %d m)." % (a, b, d, ra + rb, ra, rb)
                )
    return problems


def _own_resolved(extra=None):
    """Only the place-owned settings, so places never needs scheduling's DB."""
    return settings.owner_resolved("places", extra=extra)


def _resolve_place_id(con, text):
    """A place id from an id or a spoken name ("Giant", "the farm", "work"), or None."""
    t = " ".join(str(text or "").replace("_", " ").split()).lower()
    if not t:
        return None
    known = list(RIDE_PLACES) + _custom_place_ids(con)
    if t.replace(" ", "_") in known:
        return t.replace(" ", "_")

    def bare(x):
        return x[4:] if x.startswith("the ") else x
    for pid in known:
        name = " ".join(str(settings.resolved("place.%s.name" % pid)[0] or "").split()).lower()
        if name and (t == name or bare(t) == bare(name)):
            return pid
    return None


def _place_key(con, key):
    """place.<name or id>.<field> -> place.<id>.<field>, so place.Giant.radius_m works."""
    parts = key.split(".")
    if len(parts) == 3 and parts[0] == "place":
        pid = _resolve_place_id(con, parts[1])
        if pid:
            return "place.%s.%s" % (pid, parts[2].strip().lower())
    return key


def _place_display(con, pid):
    return _cap(str(settings.resolved("place.%s.name" % pid)[0] or pid))


def _effective_radius(con, pid):
    own = settings.resolved("place.%s.radius_m" % pid)[0]
    return int(own) if own is not None else geofence_radius_m()


def _radius_value(con, spec, value):
    """bigger / smaller (25 m) or +N / -N from the current zone size, kept within 50-400.
    Returns (value, relative)."""
    if not isinstance(value, str):
        return value, False
    text = value.strip().lower()
    m = re.fullmatch(r"(\d+)\s*(m|meters|metres)?", text)
    if m:
        return int(m.group(1)), False
    steps = {"bigger": RADIUS_STEP, "larger": RADIUS_STEP, "smaller": -RADIUS_STEP}
    if text in steps:
        step = steps[text]
    elif re.fullmatch(r"[+-]\s*\d+", text):
        step = int(text.replace(" ", ""))
    else:
        return value, False
    cur = _effective_radius(con, spec["place_id"])
    return max(RADIUS_MIN, min(RADIUS_MAX, cur + step)), True


def _prepare(con, spec, value, new_place):
    relative = False
    if spec["family"] == "place":
        if not spec["core"] and not new_place and spec["place_id"] not in _custom_place_ids(con):
            raise SettingsError("bad_place", "There is no place called %s. Pin your location to add it."
                                % spec["place_id"], step="input")
        if spec["field"] == "radius_m":
            value, relative = _radius_value(con, spec, value)
    return value, relative


def _check_value(con, spec, coerced, relative):
    if relative and coerced == _effective_radius(con, spec["place_id"]):
        edge = " (the smallest zone)" if coerced == RADIUS_MIN else " (the largest zone)" if coerced == RADIUS_MAX else ""
        raise SettingsError("no_change", "%s is already %d m%s." % (_place_display(con, spec["place_id"]), coerced, edge),
                            step="settings")


def _extra_for(spec, new_place):
    if spec.get("family") != "place" or not isinstance(new_place, dict):
        return {}
    pid = spec["place_id"]
    out = {"place.%s.name" % pid: new_place.get("name"), "place.%s.status" % pid: "active"}
    if new_place.get("radius_m") is not None:
        out["place.%s.radius_m" % pid] = new_place["radius_m"]
    return {k: v for k, v in out.items() if v is not None}


def _before_apply(con, spec):
    if spec.get("family") == "place" and spec["field"] == "radius_m":
        return {"old_radius": _effective_radius(con, spec["place_id"])}
    return None


def _after_apply(con, spec, data, ctx, coerced):
    if spec.get("family") != "place":
        return
    data["places_file"] = rewrite_places_file()
    if spec["field"] == "radius_m":
        data["say"] = "%s is now %d m (was %d). Say undo to put it back." % (
            _place_display(con, spec["place_id"]), coerced, (ctx or {}).get("old_radius"))


def _after_undo(con, out):
    key = (out or {}).get("key") or ""
    if not key.startswith("place."):
        return
    out["places_file"] = rewrite_places_file()
    parts = key.split(".")
    if len(parts) == 3 and parts[2] == "radius_m" and out.get("status") == "applied":
        out["say"] = "%s is back to %d m." % (_place_display(con, parts[1]), _effective_radius(con, parts[1]))


def _after_approve(con, spec, data):
    if spec.get("family") == "place":
        data["places_file"] = rewrite_places_file()


settings.register_owner({
    "name": "places", "open": db_open, "parse": parse_key, "keys": setting_keys,
    "normalize": lambda con, key: _place_key(con, key), "prepare": _prepare, "check_value": _check_value,
    "extra_for": _extra_for, "before_apply": _before_apply, "after_apply": _after_apply,
    "after_undo": _after_undo, "after_approve": _after_approve,
    "consistency": pin_spacing_problems,
})


# ── places.json and app_places ─────────────────────────────────────────

def rewrite_places_file(con=None):
    """Rewrite places.json from the stored pins. The planner reads that file."""
    resolved = _own_resolved()
    with _con(con) as c:
        ids = list(RIDE_PLACES) + _custom_place_ids(c)
    pins = {}
    for pid in ids:
        val = _rval(resolved, "place.%s.pin" % pid)
        if not (val and isinstance(val, dict) and "lat" in val and "lng" in val):
            continue
        entry = {"lat": val["lat"], "lng": val["lng"], "name": _place_name(resolved, pid)}
        own = _rval(resolved, "place.%s.radius_m" % pid)
        if own is not None:
            entry["radius_m"] = int(own)
        if pid not in RIDE_PLACES:
            entry["custom"] = True
            if _place_status(resolved, pid) == "removed":
                entry["removed"] = True
        pins[pid] = entry
    path = places_file()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(pins, f, indent=1)
        f.write("\n")
    os.replace(tmp, path)
    return path


def places_sync(con=None):
    """Each place for geofences: id, spoken name, lat, lng, and its own zone size (radius_m).
    The five core places always; Jordan's own places while active and pinned.
    The top-level radius_m is the default zone size (geofence_radius_meters), for old clients."""
    resolved = _own_resolved()
    radius = geofence_radius_m(resolved)
    with _con(con) as c:
        ids = list(RIDE_PLACES) + _custom_place_ids(c)
    items = []
    for pid in ids:
        core = pid in RIDE_PLACES
        pin = _rval(resolved, "place.%s.pin" % pid)
        if not core and (not pin or _place_status(resolved, pid) != "active"):
            continue
        items.append({
            "id": pid,
            "spoken_name": _place_name(resolved, pid),
            "lat": None if not pin else pin.get("lat"),
            "lng": None if not pin else pin.get("lng"),
            "radius_m": place_radius_m(resolved, pid),
            "custom": not core,
        })
    return {"places": items, "radius_m": radius}


def places_tool() -> dict:
    try:
        return ok_reply("places", places_sync())
    except SettingsError as e:
        return settings_fail(e)
    except Exception as e:  # noqa: BLE001
        return fail("internal", repr(e), "places")


def settings_fail(err):
    data = dict(err.extra) if getattr(err, "extra", None) else {}
    return fail(err.code, str(err), getattr(err, "step", "settings"), data=data)


# ── managing places (Termux and the app's Pin my location) ────────────

def _slug(name):
    s = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")
    if s.startswith("the_"):
        s = s[4:]
    s = s[:24].strip("_")
    if not s or not s[0].isalpha():
        s = ("place_" + s)[:24].strip("_")
    return s


def seed_places(con=None, now=None):
    """Store each core place's name (and its pin from places.json if the store has none), once.
    Safe to run again: a second run writes nothing. Zone sizes are not seeded."""
    file_pins = place_pins()
    seeded = []
    with _con(con) as c:
        for pid in RIDE_PLACES:
            todo = []
            if settings.standing_row(c, "place.%s.name" % pid) is None:
                todo.append(("place.%s.name" % pid, _PLACES_SAY_DEFAULT[pid]))
            if settings.standing_row(c, "place.%s.pin" % pid) is None and pid in file_pins:
                todo.append(("place.%s.pin" % pid, file_pins[pid]))
            for key, val in todo:
                d = settings.change_setting(key, val, "standing", reason="job-007 places seed", door="system", now=now)
                seeded.append({"key": key, "value": d["value"], "change_id": d["change_id"]})
                print("rivia: seeded %s=%s (standing)" % (key, json.dumps(d["value"])), file=sys.stderr)
    return {"seeded": seeded, "places_file": rewrite_places_file() if seeded else None}


def list_places(con=None):
    """Every place, removed ones too: id, name, pin, zone size, core or custom, status."""
    resolved = _own_resolved()
    with _con(con) as c:
        ids = list(RIDE_PLACES) + _custom_place_ids(c)
    out = []
    for pid in ids:
        pin = _rval(resolved, "place.%s.pin" % pid)
        out.append({
            "id": pid, "name": _place_name(resolved, pid),
            "lat": pin.get("lat") if pin else None, "lng": pin.get("lng") if pin else None,
            "radius_m": place_radius_m(resolved, pid),
            "own_radius": _rval(resolved, "place.%s.radius_m" % pid) is not None,
            "core": pid in RIDE_PLACES, "status": _place_status(resolved, pid),
        })
    return {"places": out, "default_radius_m": geofence_radius_m(resolved)}


def _known_place(con, text):
    pid = _resolve_place_id(con, text)
    if not pid:
        raise SettingsError("bad_place", "There is no place called %s. Places: %s." % (
            str(text or "(none)").strip(), ", ".join(list(RIDE_PLACES) + _custom_place_ids(con))), step="input")
    return pid


def _check_new_place(con, name, pin=None, radius=None):
    """Validate a new place's name, id, pin and size together before anything is written."""
    name = settings.coerce(_place_spec("place.x.name", "x", "name"), name)
    pid = _slug(name)
    taken = _resolve_place_id(con, name)
    if taken and (taken in RIDE_PLACES or _place_status(_own_resolved(), taken) == "active"):
        raise SettingsError("place_exists", "%s is already a place (%s). Pin it to move it." % (name, taken), step="input")
    if pid in RIDE_PLACES:
        raise SettingsError("place_exists", "%s is a core place. Pin it to move it." % _cap(pid), step="input")
    if pid in _custom_place_ids(con) and _place_status(_own_resolved(), pid) == "active":
        raise SettingsError("place_exists", "%s is already a place. Pin it to move it." % name, step="input")
    extra = {"place.%s.name" % pid: name, "place.%s.status" % pid: "active"}
    if radius is not None:
        radius = settings.coerce(_place_spec("place.x.radius_m", "x", "radius_m"), radius)
        extra["place.%s.radius_m" % pid] = radius
    if pin is not None:
        pin = settings.coerce(_place_spec("place.x.pin", "x", "pin"), pin)
        extra["place.%s.pin" % pid] = pin
    problems = pin_spacing_problems(_own_resolved(extra=extra))
    if problems:
        raise SettingsError("inconsistent", problems[0], step="settings", extra={"problems": problems})
    return pid, name, pin, radius


def add_place(name, lat, lng, radius_m=None, door="termux", now=None, con=None):
    """Add one of Jordan's own places (or bring back a removed one) with its pin, right away."""
    with _con(con) as c:
        pid, name, pin, radius = _check_new_place(c, name, {"lat": lat, "lng": lng}, radius_m)
        reason = "add place %s" % name
        settings.change_setting("place.%s.name" % pid, name, "standing", reason=reason, door=door, now=now,
                                new_place=True)
        if settings.standing_row(c, "place.%s.status" % pid) is not None:
            settings.change_setting("place.%s.status" % pid, "active", "standing", reason=reason, door=door, now=now)
        if radius is not None:
            settings.change_setting("place.%s.radius_m" % pid, radius, "standing", reason=reason, door=door, now=now)
        settings.change_setting("place.%s.pin" % pid, pin, "standing", reason=reason, door=door, now=now)
    return {"added": pid, "name": name, "places_file": rewrite_places_file(),
            "place": [p for p in list_places()["places"] if p["id"] == pid][0]}


def rename_place(place, name, door="termux", now=None, con=None):
    """Change a place's spoken name (core places keep their id and role)."""
    with _con(con) as c:
        pid = _known_place(c, place)
        name = settings.coerce(_place_spec("place.x.name", "x", "name"), name)
        other = _resolve_place_id(c, name)
        if other and other != pid:
            raise SettingsError("place_exists", "%s is already the name of %s." % (name, other), step="input")
    return settings.change_setting("place.%s.name" % pid, name, "standing", reason="rename", door=door, now=now)


def remove_place(place, door="termux", now=None, con=None):
    """Remove one of Jordan's own places (soft: its rides and settings stay). Core places can't be removed."""
    with _con(con) as c:
        pid = _known_place(c, place)
        key = "place.%s.status" % pid
        parse_key(key)  # refuses a core place in plain words
        if settings.resolved(key)[0] == "removed":
            raise SettingsError("no_change", "%s is already removed." % _place_display(c, pid), step="settings")
    return settings.change_setting(key, "removed", "standing", reason="remove place", door=door, now=now)


def set_zone(place, meters, door="termux", now=None, con=None):
    """Set a place's zone size (meters, or bigger / smaller / +N / -N)."""
    with _con(con) as c:
        pid = _known_place(c, place)
    return settings.change_setting("place.%s.radius_m" % pid, meters, "standing", reason="zone size",
                                   door=door, now=now)


def _pin_parts(coords):
    pin = settings.coerce(_place_spec("place.x.pin", "x", "pin"), coords)
    return pin["lat"], pin["lng"]


def pin_place_sync(coords, place=None, name=None, radius_m=None, door="app", now=None, con=None):
    """Pin my location: re-pin a known place, or add a new one by name. From the app door the pin
    waits for approval (G7); a new place gets one pending change that carries its name."""
    with _con(con) as c:
        if coords in (None, ""):
            raise SettingsError("bad_value", "coords (lat,lng from the phone) are required.", step="input")
        target = _resolve_place_id(c, place) if place else None
        if place and not target:
            raise SettingsError("bad_place", "There is no place called %s. Give a name to add it." % place, step="input")
        if not target and name:
            target = _resolve_place_id(c, name)
            if target and target not in RIDE_PLACES and _place_status(_own_resolved(), target) != "active":
                target = None  # a removed place: add it back
        if target:
            data = settings.change_setting("place.%s.pin" % target, coords, "standing",
                                           reason="pin my location", door=door, now=now)
            data["place_id"], data["name"] = target, str(settings.resolved("place.%s.name" % target)[0] or target)
            data["new_place"] = False
        else:
            if not name:
                raise SettingsError("bad_value", "Say which place to pin, or a name for a new one.", step="input")
            if door != "app":
                out = add_place(name, *_pin_parts(coords), radius_m=radius_m, door=door, now=now, con=c)
                return dict(out, status="applied", place_id=out["added"], new_place=True)
            pid, name, pin, radius = _check_new_place(c, name, coords, radius_m)
            data = settings.change_setting("place.%s.pin" % pid, pin, "standing", reason="new place: %s" % name,
                                           door=door, now=now, new_place={"name": name, "radius_m": radius})
            data["place_id"], data["name"], data["new_place"] = pid, name, True
    if data.get("status") == "pending":
        data["say"] = ("Saved %s here, waiting for your approval in the hub." % data["name"] if data["new_place"]
                       else "Saved the new pin for %s, waiting for your approval in the hub." % data["name"])
    else:
        data["say"] = "Pinned %s here." % data["name"]
    return data


def pin_place_tool(coords: Union[str, dict], place: str | None = None, name: str | None = None,
                   radius_m: int | None = None) -> dict:
    try:
        data = pin_place_sync(coords, place=place, name=name, radius_m=radius_m, door="app")
        pending = data.get("status") == "pending"
        return ok_reply("pending" if pending else "change", data, ref=data.get("change_id"),
                        message=settings.PENDING_MESSAGE if pending else None, fallback_text=data.get("say"))
    except SettingsError as e:
        return settings_fail(e)
    except Exception as e:  # noqa: BLE001
        return fail("internal", repr(e), "places")


def cmd_pin(place, lat, lng):
    """Store a standing place.<id>.pin through the normal setting checks. Termux only."""
    with _con() as con:
        pid = _resolve_place_id(con, place)
        if not pid:
            raise SettingsError(
                "bad_place",
                "Unknown place: %s. Use home, farm, gym, pf, hmc, or one of yours (rivia sched places); "
                "add one with rivia sched add-place." % (str(place or "").strip() or "(none)",),
                step="input",
            )
    data = settings.change_setting("place.%s.pin" % pid, {"lat": lat, "lng": lng}, "standing", door="termux")
    return {"ok": True, **data}


# ── messages (the same shape as the planner's, so the app reads them the same way) ──

_QUIET = ("quiet", 0.0, "low", "Say nothing. A queued, repeated or out-of-order arrival.")


def pfact(pid):
    return {'id': pid, 'name': _PLACES_SAY_DEFAULT[pid]} if pid in _PLACES_SAY_DEFAULT else {'id': pid, 'name': pid}


def _message(situation, facts, must_say, say, tone, intensity, urgency, note):
    return {
        'situation': situation, 'facts': facts, 'must_say': must_say, 'decisions_for_jordan': [],
        'delivery': {'say': say, 'urgency': urgency, 'hold_ok': say and urgency != 'high', 'max_spoken_sentences': 2},
        'emotion': {'tone': tone, 'intensity': intensity, 'note': note},
    }


def quiet_message(facts):
    tone, intensity, urgency, note = _QUIET
    return _message("arrival_ignored", facts, [], False, tone, intensity, urgency, note)


def _quiet(kind, data, facts):
    return ok_reply(kind, data, message=quiet_message(facts), alerts=None, fallback_text="")


def _basic_arrival(place, pid, title, t_s):
    """Scheduling is not running: record the arrival and say only where Jordan is."""
    pid = pid or place
    name = _PLACES_SAY_DEFAULT.get(pid) or (place_registry().get(pid) or {}).get("name") or (str(title).strip() or None)
    line = "Home." if pid == "home" else ("Arrived at %s." % name if name else "Arrived.")
    msg = _message("arrived", {"place": {"id": pid, "name": name}}, [], True, "quiet", 0.2, "low",
                   "Only where he is: the planner is not running, so no leave-by times.")
    return {"place": pid, "arrived": rt.when(t_s), "message": msg, "alerts": None, "fallback_text": line,
            "planned": False}


# ── arrivals ───────────────────────────────────────────────────────────

def save_last_arrival(rec, con=None):
    with _con(con) as c:
        set_meta(c, LAST_ARRIVAL_KEY, json.dumps(rec, separators=(",", ":")))


def load_last_arrival(con=None):
    try:
        with _con(con) as c:
            raw = get_meta(c, LAST_ARRIVAL_KEY)
        if not raw:
            return None
        rec = json.loads(raw) if isinstance(raw, str) else raw
        return rec if isinstance(rec, dict) else None
    except (TypeError, ValueError):
        return None


def arrival_time(rec):
    try:
        return rt.to_epoch(rec.get("time"), "time") if rec and rec.get("time") else None
    except RiviaError:
        return None


def _departed_since(con, pid, since_s, left_at=None):
    """Did Jordan leave pid after since_s? The app's left_at, a stored departure, or a ride from pid.
    (The app keeps exits on the phone; an arrival somewhere else already moves last_arrival.)"""
    times = []
    if left_at not in (None, ""):
        times.append(left_at)
    try:
        dep = json.loads(get_meta(con, META_LAST_DEPARTURE) or "null")
        if isinstance(dep, dict) and dep.get("place") == pid:
            times.append(dep.get("time"))
    except ValueError:
        pass
    times += [r[0] for r in con.execute("select departed from rides where from_place=?", (pid,))]
    for t in times:
        try:
            if rt.to_epoch(t, "departed") >= since_s:
                return True
        except RiviaError:
            pass
    return False


def _arrival_gate(place, coords, title, t_s, now_s, last, left_at=None):
    """Why an arrival should be answered quietly, or None to plan it: (reason, place id, late minutes).
    duplicate: same place within 20 min of the last arrival. repeat: same place, and he has not left
    since (the app re-sends an ENTER every time it opens). older: before the last arrival.
    late: the phone queued it longer than arrival_late_minutes."""
    try:
        pid = place or place_of(coords, title)
    except RiviaError:
        pid = place
    pid = gym_for_place(pid, t_s) if pid else None
    last_t = arrival_time(last)
    late_m = max(0, (now_s - t_s) // 60)
    con = db_open()
    try:
        if pid and left_at not in (None, ""):
            try:
                set_meta(con, META_LAST_DEPARTURE, json.dumps(
                    {"place": pid, "time": rt.iso(rt.to_epoch(left_at, "left_at"))}, separators=(",", ":")))
            except RiviaError:
                pass
        if last_t is not None and pid and pid == last.get("place"):
            if abs(t_s - last_t) <= DUP_ARRIVAL_S:
                return "duplicate", pid, late_m
            if not _departed_since(con, pid, last_t, left_at):
                return "repeat", pid, late_m
        if last_t is not None and t_s < last_t:
            return "older", pid, late_m
        cutoff = settings.resolved("arrival_late_minutes")[0]
        if now_s - t_s > int(cutoff) * 60:
            rec = {"place": pid, "title": title, "time": rt.iso(t_s), "ignored": "late"}
            if coords not in (None, ""):
                rec["coords"] = coords
            save_last_arrival(rec, con)  # still the newest arrival: later repeats match it
            return "late", pid, late_m
        return None, pid, late_m
    finally:
        con.close()


def _quiet_arrival(reason, pid, title, t_s, late_m, ride_out):
    """Answer an arrival without speaking and without touching the alerts (say=False, alerts null)."""
    if reason == "repeat":
        print("rivia: repeat_arrival_ignored: %s @ %s" % (pid or title or "?", rt.time_of_day(t_s)), file=sys.stderr)
    else:
        print("rivia: arrival ignored (%s%s): %s @ %s" % (
            reason, " %d min" % late_m if reason == "late" else "", pid or title or "?", rt.time_of_day(t_s)),
            file=sys.stderr)
    data = {"place": pid, "arrived": rt.when(t_s), "ignored": reason, "late_minutes": late_m}
    if ride_out is not None:
        data["ride"] = ride_out
    return _quiet("arrival", data, {"place": pfact(pid), "ignored": reason, "late_minutes": late_m})


def in_rundown_window(s):
    """4:00 AM up to (not including) 2:30 PM, America/New_York."""
    d = rt.dt(s)
    return d.hour >= MORNING_FROM_H and (d.hour, d.minute) < RUNDOWN_CUTOFF


def rundown_due(t_s, now_s):
    """The good-morning rundown is due: a fresh event from 4:00 AM to 2:29 PM, not given yet that day.
    Outside the window nothing is spoken and the day is NOT marked given."""
    if now_s - t_s > 10 * 60 or rt.day_key(t_s) != rt.day_key(now_s) or not in_rundown_window(t_s):
        return False
    with _con() as c:
        return get_meta(c, META_MORNING_RUNDOWN) != rt.day_key(t_s)


def _morning_reply(kind, t_s, now, now_s, ride_out=None):
    """The rundown as a spoken reply, or None if scheduling could not build it (the call then
    stays quiet and the next open tries again)."""
    fn = HOOKS["morning_rundown"]
    if not fn:
        return None
    try:
        out = fn(t_s, now=now, now_s=now_s)
    except Exception as e:  # noqa: BLE001
        print("rivia: morning_rundown failed (%s)" % (getattr(e, "code", None) or type(e).__name__), file=sys.stderr)
        return None
    if not out:
        return None
    with _con() as c:
        set_meta(c, META_MORNING_RUNDOWN, rt.day_key(t_s))
    print("rivia: morning_rundown given for %s @ %s" % (rt.day_key(t_s), rt.time_of_day(t_s)), file=sys.stderr)
    data = {"place": "home", "arrived": rt.when(t_s), "rundown": True}
    if ride_out is not None:
        data["ride"] = ride_out
    return ok_reply(kind, data, message=out["message"], alerts=out["alerts"], fallback_text=out["speak"])


def app_open_sync(now=None):
    """The app was opened: the good-morning rundown once a day (4:00 AM to 2:29 PM, at home), else quiet."""
    now_s = int(now.timestamp()) if (now is not None and hasattr(now, "timestamp")) else rt.now_s()
    last = load_last_arrival()
    if not (last and last.get("place") == "home"):
        reason = "not_home"
    elif rt.dt(now_s).hour < MORNING_FROM_H:
        reason = "before_morning"
    elif not in_rundown_window(now_s):
        reason = "after_cutoff"
    elif not rundown_due(now_s, now_s):
        reason = "already_given"
    else:
        morning = _morning_reply("open", now_s, now, now_s)
        if morning:
            return morning
        reason = "rundown_failed" if HOOKS["morning_rundown"] else "scheduling_unavailable"
    return _quiet("open", {"rundown": False, "reason": reason}, {"ignored": "open", "reason": reason})


def app_open_tool() -> dict:
    return app_open_sync()


def _after_ride(ride_out):
    """The Drive backup after a newly stored ride (scheduling's hook). Never raises."""
    fn = HOOKS["after_ride"]
    if not fn or not ride_out or not ride_out.get("stored") or not ride_out.get("backup_due"):
        return None
    try:
        return fn(ride_out)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": {"code": "internal", "msg": type(e).__name__, "step": "drive"}}


def log_ride_sync(ride):
    """Log one ride (dict of log-ride fields) and back it up if it was new."""
    if not isinstance(ride, dict):
        return fail("bad_ride", "ride must be an object with ride_id, from, to, departed and arrived.", "input")
    try:
        out = log_ride(ride)
        backup = _after_ride(out)
        if backup is not None:
            out = dict(out, backup=backup)
        return ok_reply("ride", out, ref=ride.get("ride_id"))
    except RiviaError as e:
        step = "input" if e.code in ("bad_ride_id", "bad_place", "bad_time") else "planner"
        return fail(e.code, str(e), step, data=dict(e.extra) if e.extra else {}, ref=ride.get("ride_id"))
    except Exception as e:  # noqa: BLE001
        return fail("internal", repr(e), "planner", ref=ride.get("ride_id"))


def arrival_sync(place=None, title=None, coords=None, time=None, ride=None, now=None, *, replay=False,
                 left_at=None):
    """Log an optional ride, decide whether this is a real arrival, then answer it: scheduling's
    leave-by plan when it is running, else a plain line. Stores the last arrival so a day change
    that applies today can rerun it. replay=True reruns the stored record (no ride, no guard)."""
    ref = None
    ride_out = None
    try:
        if ride and not replay:
            logged = log_ride_sync(ride)
            if not logged.get("ok"):
                return logged
            ride_out = logged.get("data")
        now_s = int(now.timestamp()) if (now is not None and hasattr(now, "timestamp")) else rt.now_s()
        t_s = now_s if time is None or time == "" else rt.to_epoch(time, "time")
        title = title or ""
        is_test = str(title).strip().lower() == "test"
        last = None if (replay or is_test) else load_last_arrival()
        pid = None
        if not (replay or is_test):
            reason, pid, late_m = _arrival_gate(place, coords, title, t_s, now_s, last, left_at)
            if reason in ("duplicate", "repeat") and pid == "home" and rundown_due(t_s, now_s):
                morning = _morning_reply("arrival", t_s, now, now_s, ride_out)
                if morning:
                    return morning  # the good-morning rundown, instead of a welcome-home
            if reason:
                return _quiet_arrival(reason, pid, title, t_s, late_m, ride_out)
        last_t = arrival_time(last)
        farm_done = bool(last and last.get("place") == "farm" and last_t is not None and last_t <= t_s
                         and rt.day_key(last_t) == rt.day_key(t_s))
        out = None
        planner = HOOKS["plan_arrival"]
        if planner:
            out = planner(place=place, title=title, coords=coords, t_s=t_s, now=now, now_s=now_s,
                          farm_done=farm_done)
            if isinstance(out, dict) and out.get("v") == 1 and out.get("ok") is False:
                return out  # scheduling's own failure reply (calendar, need_estimate, bad_place, ...)
        if out is None:
            if pid is None:
                try:
                    pid = gym_for_place(place or place_of(coords, title), t_s)
                except RiviaError:
                    pid = place
            out = _basic_arrival(place, pid, title, t_s)
        if not replay and not is_test:  # a plumbing test is not where Jordan is
            rec = {"place": out.get("place") or place,  # resolved id (gymday -> gym/pf), so the repeat guard matches
                   "title": title, "time": rt.iso(t_s)}
            if coords not in (None, ""):
                rec["coords"] = coords
            save_last_arrival(rec)
        data = {"place": out.get("place"), "arrived": out.get("arrived")}
        if out.get("planned") is False:
            data["planned"] = False
        if ride_out is not None:
            data["ride"] = ride_out
        return ok_reply("arrival", data, ref=ref, message=out.get("message"),
                        alerts=out.get("alerts"), fallback_text=out.get("fallback_text"))
    except RiviaError as e:
        step = "input" if e.code in ("bad_place", "bad_time", "bad_coords") else "planner"
        return fail(e.code, str(e), step, data=dict(e.extra) if e.extra else {}, ref=ref)
    except Exception as e:  # noqa: BLE001
        return fail("internal", repr(e), "planner", ref=ref)


def arrival_tool(place: str | None = None, title: str | None = None,
                 coords: Union[str, dict, None] = None,
                 time: Union[str, int, float, None] = None,
                 ride: dict | None = None,
                 left_at: Union[str, int, float, None] = None) -> dict:
    return arrival_sync(place=place, title=title, coords=coords, time=time, ride=ride, left_at=left_at)


def log_ride_tool(ride_id: str, from_place: str | None = None, to_place: str | None = None,
                  departed: Union[str, int, float, None] = None,
                  arrived: Union[str, int, float, None] = None,
                  distance_km: Union[int, float, None] = None,
                  avg_kmh: Union[int, float, None] = None,
                  polyline: str | None = None) -> dict:
    ride = {"ride_id": ride_id, "from": from_place, "to": to_place,
            "departed": departed, "arrived": arrived,
            "distance_km": distance_km, "avg_kmh": avg_kmh, "polyline": polyline}
    ride = {k: v for k, v in ride.items() if v is not None}
    return log_ride_sync(ride)


places = tool(places_tool)
pin_place = tool(pin_place_tool)
arrival = tool(arrival_tool)
log_ride_t = tool(log_ride_tool)
app_open = tool(app_open_tool)

ACTIONS = {
    "sched.places": {"fn": places, "lockable": (), "py_params": {}, "doors": ("app",), "kind": "places",
                     "time_bearing": False, "description": PLACES_DESCRIPTION},
    "sched.arrival": {"fn": arrival, "lockable": ("place", "title"),
                      "py_params": {"place": "place", "title": "title", "coords": "coords",
                                    "time": "time", "ride": "ride", "left_at": "left_at"},
                      "doors": ("app",), "kind": "arrival", "time_bearing": True, "description": ARRIVAL_DESCRIPTION},
    "sched.log_ride": {"fn": log_ride_t, "lockable": (),
                       "py_params": {"ride_id": "ride_id", "from_place": "from", "to_place": "to",
                                     "departed": "departed", "arrived": "arrived",
                                     "distance_km": "distance_km", "avg_kmh": "avg_kmh", "polyline": "polyline"},
                       "doors": ("app",), "kind": "ride", "time_bearing": False, "description": LOG_RIDE_DESCRIPTION},
    "sched.pin_place": {"fn": pin_place, "lockable": (),
                        "py_params": {"coords": "coords", "place": "place", "name": "name", "radius_m": "radius_m"},
                        "doors": ("app",), "kind": "change", "time_bearing": False, "description": PIN_PLACE_DESCRIPTION},
    "sched.app_open": {"fn": app_open, "lockable": (), "py_params": {}, "doors": ("app",), "kind": "open",
                       "time_bearing": True, "description": APP_OPEN_DESCRIPTION},
}
SEED = (
    {"name": "app_places", "door": "app", "action": "sched.places",
     "preset": {}, "description": PLACES_DESCRIPTION, "model": 0, "enabled": 1},
    {"name": "app_arrival", "door": "app", "action": "sched.arrival",
     "preset": {}, "description": ARRIVAL_DESCRIPTION, "model": 0, "enabled": 1},
    {"name": "app_log_ride", "door": "app", "action": "sched.log_ride",
     "preset": {}, "description": LOG_RIDE_DESCRIPTION, "model": 0, "enabled": 1},
    {"name": "app_pin_place", "door": "app", "action": "sched.pin_place",
     "preset": {}, "description": PIN_PLACE_DESCRIPTION, "model": 0, "enabled": 1},
    {"name": "app_open", "door": "app", "action": "sched.app_open",
     "preset": {}, "description": APP_OPEN_DESCRIPTION, "model": 0, "enabled": 1},
)
tools.register(ACTIONS, SEED)
