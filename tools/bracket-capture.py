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
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import evdev_axes
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

READ_SIZE = 128


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
            evdev_axes.drain(fe, abs_seen)
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
            cur = evdev_axes.current(fe, code)
            evtxt = f'{cur} (no events)' if cur is not None else '-'
        else:
            evtxt = f'{ev[0]} .. {ev[1]}' + ('  MOVED' if ev[0] != ev[1] else '')
        flag = '  <== MOVED' if not ch['idle'] else ''
        rng = (f"{ch['min']} .. {ch['max']}" if ch['min'] is not None else '-')
        print(f"    {ch['name']:9s} byte {ch['byte']:2d}  raw {rng:>17s}"
              f"  span {ch['span_pct']:5.1f}%   "
              f"{evdev_axes.ABS_NAMES.get(code, '?'):13s} {evtxt}{flag}")

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

    abs_map = evdev_axes.axis_codes(layout)
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
