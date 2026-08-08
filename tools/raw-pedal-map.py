#!/usr/bin/env python3
"""Labelled RAW HID capture - shows which report BYTES move for each pedal.

This bypasses the driver's axis mapping entirely, so it answers:
  is clutch data present in the report at all?

Run interactively:  python3 raw-pedal-map.py
"""
import os, glob, select, time

LOGDIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
os.makedirs(LOGDIR, exist_ok=True)
LOG = os.path.join(LOGDIR, 'raw-pedal-map.log')
out = open(LOG, 'w', buffering=1)
def p(*a):
    print(*a); print(*a, file=out)

dev = os.path.realpath(glob.glob('/dev/input/by-id/usb-Fanatec_*-hidraw')[0])
p(f'hidraw: {dev}\n')
fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)

def drain():
    try:
        while True:
            if not os.read(fd, 128): break
    except BlockingIOError:
        pass

def record(seconds):
    reps = []
    end = time.time() + seconds
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.1)
        if not r: continue
        try:
            while True:
                d = os.read(fd, 128)
                if not d: break
                reps.append(d)
        except BlockingIOError:
            pass
    return reps

def phase(title, instruction, seconds):
    p(f'\n{"="*70}\n{title}\n{"="*70}')
    input(f'  {instruction}\n  >>> ENTER to start {seconds}s window: ')
    drain()
    print(f'  ... recording {seconds}s - GO')
    reps = record(seconds)
    if not reps:
        p('  !! NO REPORTS AT ALL')
        return None
    n = len(reps); size = len(reps[0])
    p(f'  {n} reports, size {size}')
    p(f'  first: {" ".join(f"{b:02x}" for b in reps[0])}')
    lo = [255]*size; hi = [0]*size
    for r in reps:
        for i in range(min(size, len(r))):
            lo[i] = min(lo[i], r[i]); hi[i] = max(hi[i], r[i])
    moved = [i for i in range(size) if hi[i] != lo[i]]
    if not moved:
        p('  no byte changed')
        return reps
    p(f'  bytes that CHANGED: {moved}')
    for i in moved:
        p(f'    byte[{i:>2}]  {lo[i]:>3} -> {hi[i]:>3}   (span {hi[i]-lo[i]})')
    # pair adjacent changing bytes into 16-bit little-endian words
    words = [i for i in moved if i+1 in moved and i % 1 == 0]
    shown = set()
    for i in words:
        if i in shown or (i-1) in shown: continue
        vals = [r[i] | (r[i+1] << 8) for r in reps if len(r) > i+1]
        if vals:
            p(f'    -> as 16-bit LE at [{i}:{i+1}]: {min(vals)} -> {max(vals)}  '
              f'(span {max(vals)-min(vals)}, {100.0*(max(vals)-min(vals))/65535:.1f}% of u16)')
            shown.add(i); shown.add(i+1)
    return reps

p('RAW pedal byte mapping. Keep hands OFF the wheel during pedal phases.')
phase('PHASE 0 - BASELINE',  'Touch NOTHING.', 5)
phase('PHASE 1 - STEERING',  'Turn ONLY the wheel, lock to lock, twice.', 10)
phase('PHASE 2 - THROTTLE',  'Press ONLY the throttle, floor and back, 3 times.', 12)
phase('PHASE 3 - BRAKE',     'Press ONLY the brake, firmly to the stop and back, 3 times.', 12)
phase('PHASE 4 - CLUTCH',    'Press ONLY the clutch, floor and back, 3 times. Press HARD.', 12)
os.close(fd)
p(f'\nlog written to {LOG}')
