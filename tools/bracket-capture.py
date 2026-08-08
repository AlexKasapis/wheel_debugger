#!/usr/bin/env python3
"""Bracketed capture: raw HID through the Tracker, evdev sampled alongside.

docs/findings.md requires every pedal test to carry a positive control in the
same window - the base is send-on-change, so a channel that reports nothing is
indistinguishable from a base that is transmitting nothing. Each pedal phase
here is bracketed by a steering phase for exactly that reason.

Reports go through Tracker.ingest() rather than a private decoder, so this tool
and the dashboard cannot drift apart. evdev is read next to it because that is
the path games take: when the two disagree the fault is between the driver and
the game, not in the base.

Run:  python3 tools/bracket-capture.py
"""
import os
import select
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hid_layout
import tracker

# (prompt, seconds). Every pedal phase sits between two steering phases.
PHASES = [
    ('BASELINE  - hands OFF everything',                     4),
    ('STEER     - turn the wheel left and right',            6),
    ('BRAKE     - press the brake HARD, several times',     10),
    ('STEER     - turn the wheel again (positive control)',  6),
    # If the brake never moves under press, does the base notice the pedal
    # leaving at all? No movement on unplug = the channel is not being read.
    ('UNPLUG    - unplug the BRAKE pedal from the base',    12),
    ('REPLUG    - plug the BRAKE back in, firmly',          12),
    ('THROTTLE  - press the gas pedal, several times',       6),
    ('CLUTCH    - press the clutch pedal, several times',    6),
]

# hid-input's own mapping, confirmed against this base: HID Slider lands on
# ABS_THROTTLE and Dial on ABS_RUDDER, which is why neither is named for a pedal.
HID_TO_ABS = {'X': 0, 'Y': 1, 'Z': 2, 'Rx': 3, 'Ry': 4, 'Rz': 5,
              'Slider': 6, 'Dial': 7}
ABS_NAMES = {0: 'ABS_X', 1: 'ABS_Y', 2: 'ABS_Z', 3: 'ABS_RX', 4: 'ABS_RY',
             5: 'ABS_RZ', 6: 'ABS_THROTTLE', 7: 'ABS_RUDDER'}

EV_ABS = 0x03
EVENT_FMT = 'llHHi'                 # input_event: timeval, type, code, value
EVENT_SZ = struct.calcsize(EVENT_FMT)
ABSINFO_SZ = 24                     # struct input_absinfo: 6 x s32
READ_SIZE = 128


def abs_now(fd, code):
    """One axis's current evdev value via EVIOCGABS, or None if unsupported."""
    import fcntl
    buf = bytearray(ABSINFO_SZ)
    req = (2 << 30) | (ABSINFO_SZ << 16) | (ord('E') << 8) | (0x40 + code)
    try:
        fcntl.ioctl(fd, req, buf)
    except OSError:
        return None
    return struct.unpack('6i', buf)[0]


def drain_hid(fd, track, now):
    """Feed every queued report into the Tracker. False if the device went away."""
    while True:
        try:
            data = os.read(fd, READ_SIZE)
        except BlockingIOError:
            return True
        except OSError:
            return False
        if not data:
            return True
        track.ingest(data, now)


def drain_evdev(fd, seen):
    """Fold queued EV_ABS events into {code: [min, max]}."""
    while True:
        try:
            buf = os.read(fd, EVENT_SZ * 64)
        except BlockingIOError:
            return
        except OSError:
            return
        if not buf:
            return
        for off in range(0, len(buf) - EVENT_SZ + 1, EVENT_SZ):
            _, _, typ, code, val = struct.unpack_from(EVENT_FMT, buf, off)
            if typ != EV_ABS:
                continue
            lo, hi = seen.get(code, (val, val))
            seen[code] = (min(lo, val), max(hi, val))


def run_phase(fh, fe, track, secs):
    """Collect for secs. Returns (snapshot, {abs code: (min, max)})."""
    track.reset()
    abs_seen = {}
    deadline = time.time() + secs
    while True:
        left = deadline - time.time()
        if left <= 0:
            break
        ready, _, _ = select.select([fh, fe], [], [], min(left, 0.05))
        now = time.time()
        if not ready:
            track.note_idle(now)
            continue
        if fh in ready and not drain_hid(fh, track, now):
            break
        if fe in ready:
            drain_evdev(fe, abs_seen)
    return track.snapshot(), abs_seen


def report_phase(snap, abs_seen, fe, abs_map):
    """Print what moved, raw side and game side."""
    print(f"\n  {snap['count']} reports"
          + (f", {snap['rate']} Hz" if snap['rate'] else '')
          + (f", silent for {snap['silent_for']}s" if snap['silent_for'] else ''))
    if not snap['count']:
        print('  !! NO REPORTS AT ALL - the base sent nothing this whole phase')
    for warn in snap['warnings']:
        print(f'  !! {warn}')

    moved = [b['i'] for b in snap['bytes'] if b['moved']]
    print(f"  raw bytes that CHANGED: {moved if moved else 'none'}")

    print('  axes (raw HID -> evdev, the path games read):')
    for ch in snap['axes']:
        code = abs_map.get(ch['hid'])
        ev = abs_seen.get(code)
        if ev is None and code is not None:
            cur = abs_now(fe, code)
            evtxt = f'{cur} (no events)' if cur is not None else '-'
        else:
            evtxt = f'{ev[0]} .. {ev[1]}' + ('  MOVED' if ev[0] != ev[1] else '')
        flag = '  <== MOVED' if not ch['idle'] else ''
        rng = (f"{ch['min']} .. {ch['max']}" if ch['min'] is not None else '-')
        print(f"    {ch['name']:9s} byte {ch['byte']:2d}  raw {rng:>17s}"
              f"  span {ch['span_pct']:5.1f}%   {ABS_NAMES.get(code, '?'):13s}"
              f" {evtxt}{flag}")

    if snap['motion']:
        top = ', '.join(f"{m['name']} {m['pct']}%" for m in snap['motion'])
        print(f'  in-window motion: {top}')
    interesting = [e for e in snap['events'] if e['kind'] != 'DROPOUT']
    for ev in interesting[:8]:
        print(f"    [{ev['kind']}] {ev['ch']}: {ev['detail']}")


def main():
    node = hid_layout.find_nodes()['hidraw']
    event = hid_layout.node_path('event')
    if not node or not event:
        sys.exit(f'no Fanatec nodes found: hidraw={node} event={event}')
    layout = hid_layout.layout_for(node['link'])
    for warn in layout['warnings']:
        print(f'  !! layout warning: {warn}')
    print(f"hidraw {node['path']}   evdev {event}   "
          f"{layout['size']}-byte report from {layout['source']}\n")

    abs_map = {ax['hid']: HID_TO_ABS[ax['hid']]
               for ax in layout['axes'] if ax['hid'] in HID_TO_ABS}
    track = tracker.Tracker(layout)

    fh = os.open(node['path'], os.O_RDONLY | os.O_NONBLOCK)
    fe = os.open(event, os.O_RDONLY | os.O_NONBLOCK)
    try:
        for title, secs in PHASES:
            print('=' * 72)
            print(f'PHASE  {title}')
            print('=' * 72)
            for n in (3, 2, 1):
                print(f'   starting in {n}...', end='\r', flush=True)
                time.sleep(1)
            print(f'   GO - {secs}s' + ' ' * 24, flush=True)
            snap, abs_seen = run_phase(fh, fe, track, secs)
            report_phase(snap, abs_seen, fe, abs_map)
            print()
    finally:
        os.close(fh)
        os.close(fe)
    print('done.')


if __name__ == '__main__':
    main()
