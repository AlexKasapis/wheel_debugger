#!/usr/bin/env python3
"""Labelled per-pedal capture: maps each pedal to an axis and characterises it.

Run interactively:  python3 pedal-map.py
Follow the prompts. Each phase is labelled, so pedal->axis is unambiguous.
"""
import os, glob, select, struct, sys, time, statistics

LOGDIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
os.makedirs(LOGDIR, exist_ok=True)
LOG = os.path.join(LOGDIR, 'pedal-map.log')
out = open(LOG, 'w', buffering=1)
def p(*a):
    print(*a); print(*a, file=out)

ABS = {0:'X', 1:'Y', 2:'Z', 3:'RX', 4:'RY', 5:'RZ',
       6:'THROTTLE', 7:'RUDDER', 16:'HAT0X', 17:'HAT0Y'}

ev = os.path.realpath(glob.glob('/dev/input/by-id/usb-Fanatec_*-event-joystick')[0])
p(f'device: {ev}\n')
fd = os.open(ev, os.O_RDONLY | os.O_NONBLOCK)

def drain():
    try:
        while True:
            if not os.read(fd, 24): break
    except BlockingIOError:
        pass

def record(seconds):
    """Collect ABS samples for `seconds`, return {code: [values]}."""
    data = {}
    end = time.time() + seconds
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.1)
        if not r: continue
        try:
            while True:
                d = os.read(fd, 24)
                if not d: break
                _s, _u, typ, code, val = struct.unpack('qqHHi', d[:24])
                if typ == 3:
                    data.setdefault(code, []).append(val)
        except BlockingIOError:
            pass
    return data

def reversals(vals):
    """Count direction changes - a proxy for twitchiness."""
    n = 0; prev = 0
    for a, b in zip(vals, vals[1:]):
        d = (b > a) - (b < a)
        if d and prev and d != prev: n += 1
        if d: prev = d
    return n

def phase(title, instruction, seconds):
    p(f'\n{"="*62}\n{title}\n{"="*62}')
    input(f'  {instruction}\n  >>> press ENTER to start the {seconds}s window: ')
    drain()
    print(f'  ... recording {seconds}s - GO')
    data = record(seconds)
    if not data:
        p('  !! NO AXIS ACTIVITY AT ALL')
        return {}
    p(f'  {"axis":<10} {"min":>8} {"max":>8} {"span":>8} {"n":>6} {"revs":>6}  {"span%":>6}')
    for code in sorted(data):
        v = data[code]
        span = max(v) - min(v)
        pct = 100.0 * span / 65535
        p(f'  {ABS.get(code, "code"+str(code)):<10} {min(v):>8} {max(v):>8} '
          f'{span:>8} {len(v):>6} {reversals(v):>6}  {pct:>5.1f}%')
    return data

p('Fanatec pedal mapping + health check')
p('Keep hands OFF the wheel during pedal phases.\n')

results = {}
results['rest']     = phase('PHASE 0 - BASELINE',
                            'Touch NOTHING. This measures the electrical noise floor.', 6)
results['throttle'] = phase('PHASE 1 - THROTTLE',
                            'Press ONLY the throttle: slowly to the floor and back, 3 times.', 12)
results['thr_hold'] = phase('PHASE 2 - THROTTLE HELD',
                            'Hold ONLY the throttle steady at about half travel. Do not move it.', 8)
results['brake']    = phase('PHASE 3 - BRAKE',
                            'Press ONLY the brake: firmly to the stop and back, 3 times.', 12)
results['clutch']   = phase('PHASE 4 - CLUTCH',
                            'Press ONLY the clutch: slowly to the floor and back, 3 times.', 12)
os.close(fd)

p(f'\n{"="*62}\nSUMMARY - pedal to axis mapping\n{"="*62}')
noise = {c: (max(v) - min(v)) for c, v in results['rest'].items()}
for pedal in ('throttle', 'brake', 'clutch'):
    d = results.get(pedal) or {}
    moved = {c: max(v) - min(v) for c, v in d.items()
             if (max(v) - min(v)) > max(noise.get(c, 0) * 3, 500)}
    if not moved:
        p(f'  {pedal:<9} -> NO AXIS RESPONDED')
    else:
        best = max(moved, key=moved.get)
        p(f'  {pedal:<9} -> {ABS.get(best,best):<9} span {moved[best]:>6} '
          f'({100.0*moved[best]/65535:.1f}% of range)'
          + (f'   [also: {", ".join(ABS.get(c,str(c)) for c in moved if c != best)}]'
             if len(moved) > 1 else ''))

h = results.get('thr_hold') or {}
if h:
    p('\n  THROTTLE STEADY-HOLD (twitch check):')
    for c, v in sorted(h.items()):
        if len(v) < 2: continue
        p(f'    {ABS.get(c,c):<10} span {max(v)-min(v):>6}  stdev {statistics.pstdev(v):>8.1f}  '
          f'reversals {reversals(v):>5}  samples {len(v)}')
    p('    (a healthy held pedal should be near-flat; large span/reversals = twitch)')
p(f'\nlog written to {LOG}')
