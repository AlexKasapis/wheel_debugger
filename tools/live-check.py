#!/usr/bin/env python3
"""Watch every channel at once and say what moved. No phases, no prompts.

bracket-capture.py exists for a protocol that has to be followed exactly, which
makes it slow to run and tedious to repeat. This is the opposite. Tracker latches
min/max, so the order things are pressed in carries no information and there is
nothing to keep up with: press what you like, in any order, and read the summary.

The positive control a send-on-change base demands is inherent here rather than
scripted - every channel is on screen at once, so a wheel that reports proves the
stream was alive at the moment a pedal did not.

Run:  python3 tools/live-check.py [seconds]      (default: until Ctrl-C)
"""
import os
import select
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import evdev_axes
import hid_layout
import tracker

READ_SIZE = 128
TICK = 0.25          # how often to look for something worth announcing

# Tracker's MOTION_MIN is tuned for a 2 s window, where 200 LSB really is more
# than dither. Latched over a whole session it is far too low: this base's
# resting chatter chews through it in seconds and every run would open with a
# channel announcing itself as moved. A pedal press is worth whole percents -
# even the faulty throttle manages 2.8% - so the two do not overlap.
TRAVEL_PCT = 1.0


def motion_min(ch):
    """Peak-to-peak below this is not even dither worth mentioning."""
    return tracker.MOTION_MIN16 if ch['bits'] == 16 else tracker.MOTION_MIN8


def verdict(ch, ev):
    """What this channel did, and whether evdev agrees the raw report moved."""
    if ch['min'] is None:
        return 'no data'
    if ch['span'] == 0:
        return 'NEVER MOVED'
    if ch['span_pct'] < TRAVEL_PCT:
        return 'noise only'
    # Raw travel that never reached evdev is a driver fault, not a base fault.
    if ev is None or ev[0] == ev[1]:
        return 'MOVED - RAW ONLY'
    return 'MOVED'


PEDALS = ('STEER', 'THROTTLE', 'BRAKE', 'CLUTCH')
LINE = 78

# The status line redraws itself with \r, which only overwrites on a terminal.
# Piped to a file it would stack up one row per tick and bury the findings.
TTY = sys.stdout.isatty()


def say(text):
    """Print above the status line, clearing whatever it left on the row."""
    print(('\r' + text.ljust(LINE)) if TTY else text)


def status_line(fe, codes, layout, elapsed, snap):
    """Redrawn in place, so a run where nothing moves still looks alive.

    Read through EVIOCGABS rather than the report stream: this base sends
    nothing at rest, so the stream has no value to show during the silence that
    makes people think the tool has hung.
    """
    vals = '  '.join(f"{ax['name'][:3]} {evdev_axes.current(fe, codes[ax['hid']])}"
                     for ax in layout['axes'] if ax['name'] in PEDALS)
    quiet = f"   base silent {snap['silent_for']}s" if snap['silent_for'] else ''
    return f'  watching {elapsed:5.1f}s   {vals}{quiet}'


def volt_range(ch):
    if ch['name'] not in ('THROTTLE', 'BRAKE', 'CLUTCH') or ch['min'] is None:
        return '-'
    return f"{ch['min'] / 65535 * 3.3:.2f}-{ch['max'] / 65535 * 3.3:.2f}"


def show_rest(fe, layout, codes):
    """Where everything is sitting right now, read with hands off."""
    print('RESTING NOW (hands off):')
    for ax in layout['axes']:
        code = codes.get(ax['hid'])
        if code is None:
            continue
        val = evdev_axes.current(fe, code)
        volts = (f"   ~{val / 65535 * 3.3:.2f} V"
                 if val is not None and ax['name'] in ('THROTTLE', 'BRAKE',
                                                       'CLUTCH') else '')
        print(f"  {ax['name']:9s} byte {ax['byte']:2d}  "
              f"{evdev_axes.ABS_NAMES.get(code, '?'):13s} {str(val):>7s}{volts}")


def summarise(snap, abs_seen, codes):
    print('\nLATCHED SUMMARY')
    print(f"  {snap['count']} reports"
          + (f", {snap['rate']} Hz" if snap['rate'] else '')
          + (f", silent for {snap['silent_for']}s" if snap['silent_for'] else ''))
    for warn in snap['warnings']:
        print(f'  !! {warn}')
    moved = [b['i'] for b in snap['bytes'] if b['moved']]
    print(f"  raw bytes that changed: {moved if moved else 'none'}\n")

    print(f"  {'channel':9s} {'byte':>4s}  {'raw min .. max':>18s} {'span':>7s}"
          f"  {'volts':11s} {'evdev':13s} {'range':>17s}  verdict")
    for ch in snap['axes']:
        code = codes.get(ch['hid'])
        ev = abs_seen.get(code)
        rng = f'{ev[0]} .. {ev[1]}' if ev else '-'
        raw = (f"{ch['min']:8d} .. {ch['max']:6d}" if ch['min'] is not None
               else '-')
        print(f"  {ch['name']:9s} {ch['byte']:>4d}  {raw:>18s} "
              f"{ch['span_pct']:6.1f}%  {volt_range(ch):11s} "
              f"{evdev_axes.ABS_NAMES.get(code, '?'):13s} {rng:>17s}  "
              f"{verdict(ch, ev)}")

    events = [e for e in snap['events'] if e['kind'] != 'DROPOUT']
    if events:
        print('\n  events:')
        for ev in events[:12]:
            print(f"    [{ev['kind']}] {ev['ch']}: {ev['detail']}")


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else None

    node = hid_layout.find_nodes()['hidraw']
    event = hid_layout.node_path('event')
    if not node or not event:
        sys.exit(f'no Fanatec nodes found: hidraw={node} event={event}')
    layout = hid_layout.layout_for(node['link'])
    for warn in layout['warnings']:
        print(f'  !! layout warning: {warn}')
    print(f"hidraw {node['path']}   evdev {event}   "
          f"{layout['size']}-byte report from {layout['source']}\n")

    codes = evdev_axes.axis_codes(layout)
    track = tracker.Tracker(layout)
    fh = os.open(node['path'], os.O_RDONLY | os.O_NONBLOCK)
    fe = os.open(event, os.O_RDONLY | os.O_NONBLOCK)
    abs_seen = {}
    moved, noisy = set(), set()      # announced once each, at most one line per tier
    start = time.time()

    try:
        show_rest(fe, layout, codes)
        print('\nPress anything, in any order — there is no prompt coming.'
              + ('  Ctrl-C when done.' if secs is None
                 else f'  Stopping after {secs:.0f}s.'))

        next_tick = start + TICK
        while secs is None or time.time() - start < secs:
            ready, _, _ = select.select([fh, fe], [], [], 0.05)
            now = time.time()
            if fh in ready:
                while True:
                    try:
                        data = os.read(fh, READ_SIZE)
                    except (BlockingIOError, OSError):
                        break
                    if not data:
                        break
                    track.ingest(data, now)
            elif not ready:
                track.note_idle(now)
            if fe in ready:
                evdev_axes.drain(fe, abs_seen)

            if now >= next_tick:
                next_tick = now + TICK
                snap = track.snapshot()
                for ch in snap['axes']:
                    if ch['min'] is None or ch['span'] < motion_min(ch):
                        continue
                    name, at = ch['name'], f"  +{now - start:5.1f}s"
                    span = f"{ch['min']} .. {ch['max']}  ({ch['span_pct']}%)"
                    if ch['span_pct'] >= TRAVEL_PCT and name not in moved:
                        moved.add(name)
                        noisy.add(name)
                        say(f'{at}  {name:9s} TRAVEL  {span}')
                    elif ch['span_pct'] < TRAVEL_PCT and name not in noisy:
                        noisy.add(name)
                        say(f'{at}  {name:9s} noise   {span}  not a press')
                if TTY:
                    print(status_line(fe, codes, layout, now - start, snap),
                          end='', flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        snap = track.snapshot()
        os.close(fh)
        os.close(fe)
    if TTY:
        print('\r'.ljust(LINE))
    summarise(snap, abs_seen, codes)


if __name__ == '__main__':
    main()
