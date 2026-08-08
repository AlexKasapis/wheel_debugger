#!/usr/bin/env python3
"""Live raw-HID readout - for testing a jack and for setting pot position.

Shows every channel the base reports, decoded straight from its HID report
descriptor (see hid_layout.py): the four analog axes including CLUTCH, the rim
ministick / slider / dial, the hat switch and any button bits that are down.
Also flags any byte that moves without belonging to a decoded field.

Run:  python3 tools/raw-live.py      (Ctrl-C to stop)
"""
import glob
import os
import select
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hid_layout                                          # noqa: E402

HAT_DIRS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']


def byte_labels(layout):
    labels = {}
    spec = layout['buttons']
    if spec:
        first, last = spec['first_bit'], spec['first_bit'] + spec['count'] - 1
        for b in range(first // 8, last // 8 + 1):
            labels[b] = 'buttons'
    for bit in layout['spare_bits']:
        labels.setdefault(bit // 8, 'spare')
    if layout['hat']:
        labels.setdefault(layout['hat']['byte'], 'hat')
    for ax in layout['axes']:
        for k in range(ax['bits'] // 8):
            labels[ax['byte'] + k] = ax['name']
    for b in layout['vendor']:
        labels[b] = 'vendor'
    return labels


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


node = glob.glob('/dev/input/by-id/usb-Fanatec_*-hidraw')
if not node:
    sys.exit('no Fanatec hidraw node - run setup/enable-rawhid.sh, and check '
             'the base is powered on')
dev = os.path.realpath(node[0])
layout = hid_layout.layout_for(node[0])
labels = byte_labels(layout)
fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)

print(f'hidraw: {dev}    layout: {layout["source"]}', flush=True)
for warn in layout['warnings']:
    print(f'  !! LAYOUT WARNING: {warn}')
print('  (Ctrl-C to stop)\n\n\n', flush=True)

lo = hi = cur = None
last = 0.0
seen_btn = set()
try:
    while True:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            try:
                while True:
                    rep = os.read(fd, 128)
                    if not rep:
                        break
                    cur = rep
                    if lo is None:
                        lo, hi = list(rep), list(rep)
                    else:
                        for i in range(min(len(rep), len(lo))):
                            lo[i] = min(lo[i], rep[i])
                            hi[i] = max(hi[i], rep[i])
                    # collected on every report, not just on redraw, so a tap
                    # between two 150ms redraws is still recorded
                    seen_btn.update(buttons_down(rep, layout['buttons']))
            except BlockingIOError:
                pass

        now = time.time()
        if cur and now - last > 0.15:
            last = now
            vals = []
            for ax in layout['axes']:
                val = axis_value(cur, ax)
                vals.append(f'{ax["name"]}={val if val is not None else "--":>6}')

            down = buttons_down(cur, layout['buttons'])
            hat_s = '-'
            if layout['hat'] and len(cur) > layout['hat']['byte']:
                hv = (cur[layout['hat']['byte']] >> layout['hat']['shift']) & 0x0f
                hat_s = HAT_DIRS[hv] if hv < 8 else 'centre'

            moved = [i for i in range(len(lo)) if hi[i] != lo[i] and i not in labels]
            lines = [
                '  ' + '  '.join(vals[:4]),
                '  ' + '  '.join(vals[4:]) + f'   hat={hat_s}',
                f'  btn down: {str(down) if down else "-":<26} '
                f'seen: {sorted(seen_btn) if seen_btn else "-"}'
                + (f'   UNDECODED MOVING BYTES: {moved}' if moved else ''),
            ]
            sys.stdout.write('\x1b[3A\r')          # redraw the block in place
            for line in lines:
                sys.stdout.write('\x1b[2K' + line + '\n')
            sys.stdout.flush()
except KeyboardInterrupt:
    print('\nstopped')
finally:
    os.close(fd)
