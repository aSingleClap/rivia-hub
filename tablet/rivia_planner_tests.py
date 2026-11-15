#!/usr/bin/env python3
"""
Self-checks for rivia_planner.py: the planner checks from Code.gs setup, checks on the
message payload, and checks on the ride database. Keep this file next to rivia_planner.py:
    python3 rivia_planner.py selftest
The database checks run in throwaway folders; they never touch ~/rivia.
"""

import shutil
import sqlite3
import tempfile

import rivia_planner as rp
from rivia_planner import *  # noqa: F401,F403
from rivia_planner import _UNSET  # noqa: F401


# ── SELF-TEST (the planner checks from Code.gs setup) ───────────────────

def run():
    results = []

    def check(name, fn):
        try:
            note = fn()
        except Exception as e:  # noqa: BLE001
            note = 'crashed: %r' % e
        results.append((name, note))

    shift_start = to_epoch('2026-10-06T14:30:00-04:00')
    shift = {'start': shift_start, 'end': shift_start + 32400, 'title': 'Work Shift: test'}
    fixed = {'farm|gym': 29, 'gym|hmc': 7, 'farm|home': 11, 'farm|hmc': 15, 'hmc|home': 20, 'gym|home': 17}

    def leg(a, b):
        return fixed[leg_key(a, b)]

    def at(min_before):
        return shift_start - min_before * 60

    def plan(place, t_s, s=_UNSET):
        return plan_arrival(place, t_s, '', leg, t_s, shift=shift if s is _UNSET else s)

    def one(p, aid, at_s):
        al = p['alerts']
        ok = isinstance(al, list) and len(al) == 1 and al[0]['at']['epoch'] == at_s and al[0]['id'].startswith(aid)
        return '' if ok else 'alerts were ' + json.dumps(al)

    def t_anchors():
        a = workday_anchors(shift_start, {'farmGym': 29, 'gymHmc': 7, 'homeFarm': 11})
        got = ','.join(str((shift_start - a[k]) // 60) for k in
                       ['hmcArrive', 'leaveGym', 'preworkStart', 'workoutStart', 'leaveFarm', 'farmStart', 'leaveHome'])
        return '' if got == '10,17,47,167,196,241,252' else 'minutes before the shift were ' + got
    check('anchors match your Oct 6 plan', t_anchors)

    def t_cuts():
        c = ' '.join('%d/%d/%d' % (x['preworkM'], x['workoutM'], x['overM'])
                     for x in [absorb_delay(600), absorb_delay(1800), absorb_delay(4800)])
        return '' if c == '20/120/0 15/105/0 15/60/5' else 'got ' + c
    check('cuts in priority order', t_cuts)

    def spk(p, needle, then):
        return then() if needle in p['speak'] else 'speak was ' + p['speak']

    check('farm on time: leave for the gym at 11:14',
          lambda: (lambda p: one(p, 'leave-gym', at(196) - 300) if p['speak'].startswith('Farm until')
                   else 'speak was ' + p['speak'])(plan('farm', at(241))))
    check('farm 20 min early: 20 min of gym flex',
          lambda: (lambda p: spk(p, '20 minutes of gym flex', lambda: one(p, 'leave-gym', at(216) - 300)))(plan('farm', at(261))))
    check('farm 30 min late: pre-work 15, workout 105',
          lambda: (lambda p: spk(p, 'pre-work drops to 15 minutes and the workout to 105',
                                 lambda: one(p, 'leave-gym', at(166) - 300)))(plan('farm', at(211))))
    check('farm 90 min late: flag it, leave for work by 2:05',
          lambda: (lambda p: spk(p, 'no longer fits', lambda: one(p, 'leave-work', at(25) - 300)))(plan('farm', at(151))))
    check('gym 20 min early: flex, leave for work at 2:13',
          lambda: (lambda p: one(p, 'leave-work', at(17) - 300) if p['speak'].startswith('Gym flex until')
                   else 'speak was ' + p['speak'])(plan('gym', at(187))))
    check('gym 20 min late: pre-work 15, workout 115',
          lambda: (lambda p: spk(p, 'pre-work drops to 15 minutes and the workout to 115',
                                 lambda: one(p, 'leave-work', at(17) - 300)))(plan('gym', at(147))))
    check('gym too late for 60 min: flag it',
          lambda: (lambda p: spk(p, 'under the 60-minute floor', lambda: one(p, 'leave-work', at(17) - 300)))(plan('gym', at(90))))
    check('work: says nothing, clears alerts',
          lambda: (lambda p: '' if p['speak'] == '' and p['alerts'] == [] else json.dumps(p))(plan('hmc', at(10))))
    check('home after a shift: leave for the farm at 10:18',
          lambda: (lambda p: one(p, 'leave-farm', at(252) - 300)
                   if 'leave for the farm at' in p['speak'] and 'Projected sleep' not in p['speak']
                   else 'speak was ' + p['speak'])(plan('home', at(866))))
    check('home late: projected sleep flagged',
          lambda: (lambda p: '' if 'Projected sleep is 6 hours' in p['speak'] else 'speak was ' + p['speak'])(plan('home', at(780))))
    check('home at midday: leave for work at 2:00',
          lambda: (lambda p: one(p, 'leave-work', at(30) - 300) if p['speak'] == 'Home. Leave for work at %s.' % clock(at(30))
                   else 'speak was ' + p['speak'])(plan('home', at(180))))
    check('day off at the farm: no leave-by',
          lambda: (lambda p: '' if 'No shift today' in p['speak'] and p['alerts'] == [] else json.dumps(p))(plan('farm', at(241), None)))
    check('unknown stop keeps alerts',
          lambda: (lambda p: '' if p['speak'] == 'Arrived at Dentist.' and p['alerts'] is None else json.dumps(p))(
              plan_arrival(None, at(241), 'Dentist', leg, at(241), shift=shift)))

    def t_titles():
        got = ','.join(place_from_title(t) or '' for t in [
            'Farm Chores & Horse Care', 'Gym Workout - Planet Fitness', 'Work Shift: Patient Escort Aide',
            'Work Arrival Buffer', 'Farmers Market', 'Home', 'Farm', 'Gym', 'University Fitness Center'])
        return '' if got == 'farm,pf,hmc,,,home,farm,gymday,gym' else 'got ' + got
    check('calendar titles map to places', t_titles)

    my_legs = {'homeFarm': 5, 'farmGym': 20, 'gymHmc': 5, 'hmcHome': 25}
    mon = to_epoch('2026-10-26T13:00:00-04:00')
    mon_shift = {'start': mon, 'end': mon + 30600, 'title': 'Work Shift: test'}
    tue_shift = {'start': mon + 86400, 'end': mon + 86400 + 30600, 'title': 'Work Shift: test'}
    sat = to_epoch('2026-10-31T13:00:00-04:00')
    sat_shift = {'start': sat, 'end': sat + 30600, 'title': 'Work Shift: test'}

    def start_of(p, title):
        b = [x for x in p['blocks'] if x['title'] == title]
        return (b[0]['start'] - p['anchors']['hmcArrive'] - CFG['bufferM'] * 60) // 60 if b else None

    def t_dayoff():
        p = build_workday(mon_shift, None, my_legs, 'gym')
        titles = ', '.join(b['title'] for b in p['blocks'])
        want = ('Sleep, Wake-up, Ride: home to the farm, Farm, Ride: farm to the gym, Gym, Ride: gym to work, '
                'Arrive early, Ride: work to home, Home, Sleep')
        if titles != want:
            return 'blocks were ' + titles
        got = ','.join(str(start_of(p, t)) for t in ['Wake-up', 'Farm', 'Gym', 'Arrive early'])
        return '' if got == '-295,-230,-165,-10' else 'minutes from the shift were ' + got
    check('Rivia day after a day off: blocks in order', t_dayoff)

    def t_aftershift():
        p = build_workday(tue_shift, mon_shift, my_legs, 'gym')
        flex = [b for b in p['blocks'] if b['title'] == 'Home flex']
        ok = start_of(p, 'Wake-up') == -365 and flex and flex[0]['end'] - flex[0]['start'] == 4200 and \
            p['blocks'][0]['title'] == 'Wake-up'
        return '' if ok else 'blocks were ' + json.dumps(p['blocks'][:3])
    check('Rivia day after a shift: up 6:55, 70 min spare', t_aftershift)

    def t_weekdays():
        p1 = check_day(build_workday(mon_shift, None, my_legs, 'gym')['blocks'], mon_shift)
        p2 = check_day(build_workday(tue_shift, mon_shift, my_legs, 'gym')['blocks'], tue_shift)
        return ' '.join(p1 + p2)
    check('Rivia checks pass on weekdays', t_weekdays)

    def t_gyms():
        got = ','.join(gym_for(s) for s in [mon, mon + 4 * 86400, sat, sat + 86400])
        return '' if got == 'gym,gym,pf,pf' else 'got ' + got
    check('weekdays the University Fitness Center, weekends Planet Fitness', t_gyms)

    check('Planet Fitness fits a Saturday shift',
          lambda: ' '.join(check_day(build_workday(sat_shift, None, my_legs, 'pf')['blocks'], sat_shift)))

    def t_ufc_sat():
        p = check_day(build_workday(sat_shift, None, my_legs, 'gym')['blocks'], sat_shift)
        return '' if len(p) == 1 and 'open 8 AM to 12 PM on weekends' in p[0] else 'got ' + ' '.join(p)
    check('University Fitness Center refuses a Saturday after noon', t_ufc_sat)

    def t_overlap():
        blocks = build_workday(mon_shift, None, my_legs, 'gym')['blocks']
        [b for b in blocks if b['title'] == 'Farm'][0]['end'] += 600
        p = check_day(blocks, mon_shift)
        return '' if p == ['Farm overlaps Ride: farm to the gym.'] else 'got ' + ' '.join(p)
    check('Rivia catches an overlap', t_overlap)

    def t_short_morning():
        late = {'start': mon, 'end': mon + 86400 - 25200, 'title': 'Work Shift: test'}
        p = build_workday(tue_shift, late, my_legs, 'gym')
        return '' if len(p['problems']) == 1 and p['problems'][0].startswith('Not enough morning') else 'got ' + ' '.join(p['problems'])
    check('Rivia catches a short morning', t_short_morning)

    def t_rank():
        got = ','.join(fmt_num(x) for x in [rank_min([24, 20, 22, 21, 29, 23, 25, 27, 26, 28], CFG['learnRank']),
                                            rank_min([22, 20, 21], 0.5), rank_min([20, 22, 21], CFG['learnRank'])])
        return '' if got == '27,21,22' else 'got ' + got
    check('plans with the 3rd-slowest of 10 rides', t_rank)

    def t_notes():
        n = [ride_note(21, 20), ride_note(41, 20), ride_note(4.9, 20), ride_note(9, 20), ride_note(30, None)]
        ok = n[0] == '' and n[1].startswith('Over 2x') and n[2].startswith('Under 0.25x') and n[3] == '' and n[4] == ''
        return '' if ok else 'got ' + json.dumps(n)
    check('rides with a stop or a late geofence are not used', t_notes)

    # New in the port: the parts the Bot leans on.
    def t_learned_first():
        trips = [{'from': 'farm', 'to': 'gym', 'minutes': m, 'used': True} for m in [18, 19, 17, 30]]
        trips.append({'from': 'farm', 'to': 'gym', 'minutes': 99, 'used': False})
        lg = Legs({}, trips)
        got = [lg.info('farm', 'gym'), lg.info('gym', 'farm'), lg.info('home', 'pf')]
        want = [(19, 'learned from 4 rides'), (20, 'your estimate'), (17, 'your estimate')]
        return '' if got == want else 'got ' + json.dumps(got)
    check('learned rides beat estimates; unclean rides ignored', t_learned_first)

    def t_need_maps():
        lg = Legs({}, [])
        lg('pf', 'hmc')
        try:
            lg.check()
        except RiviaError as e:
            return '' if e.code == 'need_maps' and e.extra['pairs'][0]['key'] == 'pf>hmc' else 'got ' + e.code
        return 'no need_maps error'
    check('a missing Maps time stops the answer instead of guessing', t_need_maps)

    def t_maps_used():
        lg = Legs({'pf>hmc': 22.2}, [])
        got = lg.info('pf', 'hmc')
        return '' if got == (23, 'Maps bike time') else 'got ' + json.dumps(got)
    check('Maps minutes round up', t_maps_used)

    def t_shift_filter():
        evs = [{'title': 'Work Shift: Escort', 'start': '2026-11-09T13:00:00-05:00', 'end': '2026-11-09T21:30:00-05:00'},
               {'title': 'Work Arrival Buffer', 'start': '2026-11-09T12:50:00-05:00', 'end': '2026-11-09T13:00:00-05:00',
                'location': '500 University Dr, Hershey, PA 17033'},
               {'title': 'Training', 'start': '2026-11-10T08:00:00-05:00', 'end': '2026-11-10T16:00:00-05:00',
                'location': '500 University Dr, Hershey, PA 17033'},
               {'title': 'Holiday', 'all_day': True}]
        got = ','.join(s['title'] for s in shifts_from(evs))
        return '' if got == 'Work Shift: Escort,Training' else 'got ' + got
    check('shifts found in calendar events', t_shift_filter)

    def t_times():
        a = to_epoch('2026-11-09T13:00:00-05:00')
        ok = (a == to_epoch(a * 1000) == to_epoch('2026-11-09T13:00:00') == to_epoch(str(a)) and
              time_of_day(a) == '1:00 PM' and clock(a) == '1:00' and day_key(a) == '2026-11-09')
        return '' if ok else 'time parsing is off'
    check('times: epoch, milliseconds and ISO agree', t_times)

    # The payload the voice layer reads.
    def t_situations():
        cases = [(plan('farm', at(241)), 'farm_on_time'), (plan('farm', at(261)), 'farm_early_flex'),
                 (plan('farm', at(211)), 'farm_late_cut'), (plan('farm', at(151)), 'farm_late_over_floor'),
                 (plan('gym', at(187)), 'gym_flex'), (plan('gym', at(147)), 'gym_late_cut'),
                 (plan('gym', at(90)), 'gym_late_over_floor'), (plan('gym', at(10)), 'gym_leave_now'),
                 (plan('hmc', at(10)), 'at_work'), (plan('home', at(866)), 'home_overnight'),
                 (plan('home', at(780)), 'home_overnight_short_sleep'), (plan('home', at(180)), 'home_before_shift'),
                 (plan('home', at(20)), 'home_leave_now'), (plan('home', at(180), None), 'home_no_shift'),
                 (plan('farm', at(241), None), 'no_shift_today'),
                 (plan_arrival(None, at(241), 'Dentist', leg, at(241), shift=shift), 'arrived_unknown')]
        bad = ['%s got %s' % (want, p['message']['situation']) for p, want in cases if p['message']['situation'] != want]
        return ' '.join(bad)
    check('every arrival lands in the right situation', t_situations)

    def t_payload_shape():
        probs = []
        samples = [plan('farm', at(211)), plan('gym', at(90)), plan('home', at(780))]
        for p in samples:
            msgs = [p['message']] + [a['message'] for a in (p['alerts'] or [])]
            for m in msgs:
                for k in m['must_say']:
                    if k not in m['facts']:
                        probs.append('%s: must_say %s missing from facts' % (m['situation'], k))
                for k, v in m['facts'].items():
                    if k.endswith(('_at', '_by', '_until', '_start', '_end')) and not (
                            isinstance(v, dict) and {'epoch', 'local', 'spoken'} <= set(v)):
                        probs.append('%s: %s is not a time fact' % (m['situation'], k))
                if m['situation'] not in EMOTION or set(m['delivery']) != {'say', 'urgency', 'hold_ok', 'max_spoken_sentences'}:
                    probs.append('%s: bad delivery or emotion' % m['situation'])
        return ' '.join(probs)
    check('payloads: must_say facts exist and every time is a time fact', t_payload_shape)

    def t_cut_facts():
        f = plan('gym', at(147))['message']['facts']
        ok = (f['late_minutes'], f['prework_minutes'], f['workout_minutes'], f['leave_for_work_at']['spoken']) == \
            (20, 15, 115, clock(at(17)))
        return '' if ok else 'facts were ' + json.dumps(f)
    check('late at the gym: facts carry the cut and the leave time', t_cut_facts)

    # ── The ride database (scheduler.db) and its Drive copy ─────────────
    # Each check gets its own empty folder in place of ~/rivia.

    def in_tmp(fn):
        def inner():
            old = rp.RIVIA_DIR
            d = tempfile.mkdtemp()
            rp.RIVIA_DIR = d
            try:
                return fn(d)
            finally:
                rp.RIVIA_DIR = old
                shutil.rmtree(d, ignore_errors=True)
        return inner

    def ride(rid, frm='farm', to='gym', start='2026-11-09T09:00:00-05:00', minutes=19):
        s = to_epoch(start)
        return {'ride_id': rid, 'from': frm, 'to': to, 'departed': s, 'arrived': s + minutes * 60,
                'distance_km': 8.1, 'avg_kmh': 25.6, 'polyline': 'abc'}

    def code_of(fn, *args):
        try:
            fn(*args)
        except RiviaError as e:
            return e.code
        return 'no error'

    def t_fresh_guard(d):
        probs = []
        if code_of(cmd_log_ride, ride('r1')) != 'restore_first':
            probs.append('new database accepted a ride')
        if code_of(cmd_backup, {}) != 'restore_first':
            probs.append('new database made a backup')
        if not cmd_init_db({})['initialized']:
            probs.append('init-db did not initialize')
        if not cmd_log_ride(ride('r1'))['stored']:
            probs.append('ride refused after init-db')
        if cmd_init_db({})['initialized']:
            probs.append('init-db ran twice')
        return ' '.join(probs)
    check('a new, empty database refuses rides and backups until restored or initialized', in_tmp(t_fresh_guard))

    def t_migrate(d):
        with open(os.path.join(d, 'rides.jsonl'), 'w') as f:
            f.write(json.dumps({'ride_id': 'old1', 'from': 'farm', 'to': 'gym', 'minutes': 18, 'used': True}) + '\n')
            f.write('not json\n')
            f.write(json.dumps({'ride_id': 'old2', 'from': 'gym', 'to': 'hmc', 'minutes': 6, 'used': True}) + '\n')
        got = [r['ride_id'] for r in load_rides()]
        moved = os.path.exists(os.path.join(d, 'rides.jsonl.migrated')) and not os.path.exists(os.path.join(d, 'rides.jsonl'))
        stored = cmd_log_ride(ride('new1'))['stored']  # migrated history, so not a fresh database
        return '' if got == ['old1', 'old2'] and moved and stored else 'got %s moved=%s stored=%s' % (got, moved, stored)
    check('old rides.jsonl moves into the database once', in_tmp(t_migrate))

    def t_store_learn(d):
        cmd_init_db({})
        for i, m in enumerate([18, 19, 17]):
            cmd_log_ride(ride('r%d' % i, start='2026-11-%02dT09:00:00-05:00' % (10 + i), minutes=m))
        dup = cmd_log_ride(ride('r0', minutes=18))
        got = Legs({}).info('farm', 'gym')
        ok = dup['duplicate'] and not dup['stored'] and len(load_rides()) == 3 and got == (19, 'learned from 3 rides')
        return '' if ok else 'dup=%s rides=%d legs=%s' % (dup, len(load_rides()), got)
    check('rides are stored once and planning learns from them', in_tmp(t_store_learn))

    def t_backup_when_changed(d):
        cmd_init_db({})
        cmd_log_ride(ride('r1'))
        b1 = cmd_backup({})
        if not b1['upload']:
            return 'first backup said no upload'
        con = sqlite3.connect(b1['db_path'])
        n = con.execute('select count(*) from rides').fetchone()[0]
        con.close()
        if n != 1:
            return 'backup copy holds %d rides' % n
        if not cmd_backup({})['upload']:
            return 'an unconfirmed upload was not offered again'
        cmd_backup_uploaded({'change': b1['change']})
        if cmd_backup({})['upload']:
            return 'offered an upload with nothing new'
        cmd_log_ride(ride('r2', start='2026-11-10T09:00:00-05:00'))
        return '' if cmd_backup({})['upload'] else 'no upload after a new ride'
    check('backup offers an upload only when rides changed', in_tmp(t_backup_when_changed))

    def t_restore(d):
        cmd_init_db({})
        cmd_log_ride(ride('r1'))
        cmd_log_ride(ride('r2', start='2026-11-10T09:00:00-05:00'))
        b = cmd_backup({})
        keep = tempfile.mkdtemp()
        drive_db = shutil.copy(b['db_path'], os.path.join(keep, 'drive.db'))
        drive_json = shutil.copy(b['json_path'], os.path.join(keep, 'drive.json'))
        try:
            for name in os.listdir(d):  # the Bot's computer gets reset
                p = os.path.join(d, name)
                shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
            if code_of(cmd_log_ride, ride('r3')) != 'restore_first':
                return 'reset computer accepted a ride before restoring'
            r1 = cmd_restore({'path': drive_db})
            r2 = cmd_restore({'path': drive_json})
            stored = cmd_log_ride(ride('r3', start='2026-11-11T09:00:00-05:00'))['stored']
            bad = code_of(cmd_restore, {'path': os.path.join(keep, 'missing.db')})
            ok = r1['added'] == 2 and r2['added'] == 0 and stored and len(load_rides()) == 3 and bad == 'bad_copy'
            return '' if ok else 'r1=%s r2=%s stored=%s rides=%d bad=%s' % (r1, r2, stored, len(load_rides()), bad)
        finally:
            shutil.rmtree(keep, ignore_errors=True)
    check('restore merges the Drive copy into a reset computer and never drops rides', in_tmp(t_restore))

    passed = sum(1 for _, n in results if not n)
    lines = [('PASS  ' if not n else 'FAIL  ') + name + ('' if not n else '  (' + n + ')') for name, n in results]
    return {'version': VERSION, 'passed': passed, 'total': len(results), 'lines': lines,
            'all_passed': passed == len(results)}
