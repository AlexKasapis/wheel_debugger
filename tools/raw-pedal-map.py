#!/usr/bin/env python3
"""Labelled RAW HID capture - which channel and which buttons respond to what.

This bypasses the driver's axis/button mapping entirely, so it answers directly:
is this input present in the report at all?

Channels and button bits are decoded from the base's own HID report descriptor
(see hid_layout.py), so each phase reports by NAME - "CLUTCH moved 0 -> 65535",
"button 5 (GEAR UP) appeared" - not just "bytes 22,23 changed".

Phase order is deliberate. The later phases exist to settle open questions:
  * PADDLES   - Fanatec rims with analog paddles can drive the CLUTCH axis
                themselves (the base's ACP setting). If the paddles move CLUTCH,
                the clutch-IN channel is alive and merely overridden, and the
                "dead clutch channel" reading is wrong.
  * MINISTICK - hold the rim STILL. If STEER only dithers (tens of LSB), the
                ministick and steering are independent, as the descriptor says.
                If STEER swings thousands of LSB with the rim held, something
                really is shared.

Run interactively:  python3 tools/raw-pedal-map.py
"""
import glob
import os
import select
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hid_layout                                          # noqa: E402

HAT_DIRS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
# hardware button number -> rim function, from the ftec_keymap comments in
# hid-fanatec's hid-ftec.c
BTN_FN = {1: 'Square', 2: 'Cross', 3: 'Circle', 4: 'Triangle',
          5: 'GEAR UP (right paddle)', 6: 'GEAR DOWN (left paddle)',
          7: 'R2', 8: 'L2', 9: 'SH/Start', 10: 'OP/Select', 11: 'R3', 12: 'L3',
          22: 'PS/Xbox', 23: 'Funky twist L', 24: 'Funky twist R',
          25: 'Funky push', 26: 'Ministick push',
          29: 'Seq gear down', 30: 'Seq gear up',
          61: 'L analog paddle (as button)', 62: 'R analog paddle (as button)'}

PHASES = [
    ('PHASE 0 - BASELINE', 'Touch NOTHING at all.', 5),
    ('PHASE 1 - STEERING', 'Turn ONLY the wheel, lock to lock, twice.', 10),
    ('PHASE 2 - THROTTLE PEDAL',
     'Press ONLY the throttle, floor and back, 3 times.', 12),
    ('PHASE 3 - BRAKE PEDAL',
     'Press ONLY the brake, firmly to the stop and back, 3 times.', 12),
    ('PHASE 4 - CLUTCH PEDAL',
     'Press ONLY the clutch, floor and back, 3 times. Press HARD.', 12),
    ('PHASE 5 - SHIFT PADDLES',
     'Pull the RIGHT shift paddle 3x, then the LEFT shift paddle 3x. '
     'Nothing else.', 12),
    ('PHASE 6 - ANALOG PADDLES (rim)',
     'Squeeze the two ANALOG paddles behind the rim, each slowly through full '
     'travel. If the rim has none, just press ENTER and wait it out.', 12),
    ('PHASE 7 - MINISTICK  (hold the rim STILL)',
     'HOLD THE RIM FIRMLY so it cannot rotate, then move ONLY the ministick '
     'through its full travel. Do not let the wheel turn.', 12),
    ('PHASE 8 - EVERY BUTTON',
     'Press every button, encoder and switch on the rim, ONE AT A TIME, '
     'slowly. Nothing else.', 30),
]


def axis_value(rep, ax):
    i = ax['byte']
    if ax['bits'] == 16:
        return rep[i] | (rep[i + 1] << 8) if len(rep) > i + 1 else None
    if len(rep) <= i:
        return None
    val = rep[i]
    return val - 256 if ax['signed'] and val > 127 else val


def buttons_down(rep, spec):
    if not spec:
        return []
    need = (spec['first_bit'] + spec['count'] + 7) // 8
    if len(rep) < need:
        return []
    mask = int.from_bytes(bytes(rep[:need]), 'little') >> spec['first_bit']
    return [spec['first_usage'] + i for i in range(spec['count']) if mask >> i & 1]


def reversals(vals):
    n = 0
    direction = 0
    for a, b in zip(vals, vals[1:]):
        d = (b > a) - (b < a)
        if d and direction and d != direction:
            n += 1
        if d:
            direction = d
    return n


def decoded_bytes(layout):
    """Byte indices the descriptor accounts for."""
    used = set()
    for ax in layout['axes']:
        used.update(range(ax['byte'], ax['byte'] + ax['bits'] // 8))
    spec = layout['buttons']
    if spec:
        used.update(range(spec['first_bit'] // 8,
                          (spec['first_bit'] + spec['count'] - 1) // 8 + 1))
    used.update(b // 8 for b in layout['spare_bits'])
    used.update(layout['vendor'])
    if layout['hat']:
        used.add(layout['hat']['byte'])
    return used


def analyse(reps, layout):
    """Turn one phase's reports into report lines plus a one-line summary."""
    spec = layout['buttons']
    if not reps:
        # the base only transmits on change, so silence is a real result
        return (['  !! NO REPORTS AT ALL - nothing in this input reached the base'],
                'NO REPORTS - no response at all')

    lines = [f'  {len(reps)} reports, size {len(reps[0])}',
             '  first: ' + ' '.join(f'{b:02x}' for b in reps[0])]

    moved = []
    for ax in layout['axes']:
        vals = [v for v in (axis_value(r, ax) for r in reps) if v is not None]
        if not vals:
            continue
        span = max(vals) - min(vals)
        if span == 0:
            continue
        full = ax['lmax'] - ax['lmin']
        pct = 100.0 * span / full if full else 0.0
        moved.append((pct, f'{ax["name"]:<9} {ax["hid"]:<7} byte {ax["byte"]:>2}   '
                           f'{min(vals):>6} -> {max(vals):>6}   span {span:>6} '
                           f'({pct:5.1f}%)   {reversals(vals)} reversals in '
                           f'{len(vals)} samples'))
    if moved:
        lines.append('  CHANNELS THAT MOVED:')
        lines += ['    ' + text for _, text in sorted(moved, reverse=True)]
    else:
        lines.append('  no decoded channel moved')

    btns = sorted({b for r in reps for b in buttons_down(r, spec)})
    if btns:
        lines.append('  BUTTON BITS SEEN DOWN:')
        for b in btns:
            fn = BTN_FN.get(b, '')
            bit = spec['first_bit'] + b - spec['first_usage']
            lines.append(f'    button {b:<4} byte {bit // 8:>2} bit {bit % 8}'
                         + (f'   {fn}' if fn else ''))
    else:
        lines.append('  no button bit went down')

    if layout['hat']:
        hb, hs = layout['hat']['byte'], layout['hat']['shift']
        hats = sorted({(r[hb] >> hs) & 0x0f for r in reps if len(r) > hb})
        lines.append('  hat values seen: ' + ' '.join(
            HAT_DIRS[h] if h < 8 else f'centre({h})' for h in hats))

    used = decoded_bytes(layout)
    size = len(reps[0])
    lo, hi = [255] * size, [0] * size
    for r in reps:
        for i in range(min(size, len(r))):
            lo[i] = min(lo[i], r[i])
            hi[i] = max(hi[i], r[i])
    stray = [i for i in range(size) if hi[i] != lo[i] and i not in used]
    if stray:
        lines.append(f'  !! MOVED BUT UNDECODED: bytes {stray}')

    if moved:
        summary = max(moved)[1]
    elif btns:
        summary = f'buttons {btns}'
    else:
        summary = 'reports arrived, but nothing decoded moved'
    return lines, summary


def main():
    logdir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
    os.makedirs(logdir, exist_ok=True)
    log = os.path.join(logdir, 'raw-pedal-map.log')
    out = open(log, 'w', buffering=1)

    def p(*a):
        print(*a, flush=True)
        print(*a, file=out)

    node = glob.glob('/dev/input/by-id/usb-Fanatec_*-hidraw')
    if not node:
        sys.exit('no Fanatec hidraw node - run setup/enable-rawhid.sh, and check '
                 'the base is powered on')
    dev = os.path.realpath(node[0])
    layout = hid_layout.layout_for(node[0])
    p(f'hidraw: {dev}')
    p(f'layout: {layout["source"]}, {layout["size"]}-byte report')
    for warn in layout['warnings']:
        p(f'  !! LAYOUT WARNING: {warn}')
    p('')
    fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)

    def drain():
        try:
            while True:
                if not os.read(fd, 128):
                    break
        except BlockingIOError:
            pass

    def record(seconds):
        reps = []
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            try:
                while True:
                    data = os.read(fd, 128)
                    if not data:
                        break
                    reps.append(data)
            except BlockingIOError:
                pass
        return reps

    p('RAW input mapping. Do ONE thing per phase; the base only sends a report')
    p('when something actually changes, so silence is a real result.')

    summary = []
    try:
        for title, instruction, seconds in PHASES:
            p(f'\n{"=" * 70}\n{title}\n{"=" * 70}')
            input(f'  {instruction}\n  >>> ENTER to start the {seconds}s window: ')
            drain()
            print(f'  ... recording {seconds}s - GO', flush=True)
            lines, one = analyse(record(seconds), layout)
            for line in lines:
                p(line)
            summary.append((title, one))
    finally:
        os.close(fd)

    p(f'\n{"=" * 70}\nSUMMARY\n{"=" * 70}')
    for title, one in summary:
        p(f'  {title}')
        p(f'      {one}')
    p(f'\nlog written to {log}')
    out.close()


if __name__ == '__main__':
    main()
