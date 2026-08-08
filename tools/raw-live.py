#!/usr/bin/env python3
"""Live raw-HID readout - for testing a jack and for setting pot position.

Shows the three known channels updating in real time, and flags any OTHER
byte in the report that moves (which is how a revived clutch channel would
announce itself).

Run:  python3 raw-live.py      (Ctrl-C to stop)
"""
import os, glob, select, sys, time

dev = os.path.realpath(glob.glob('/dev/input/by-id/usb-Fanatec_*-hidraw')[0])
fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
print(f'hidraw: {dev}    (Ctrl-C to stop)\n')

CH = {'STEER': 16, 'THR-IN': 18, 'BRK-IN': 20}
KNOWN = {16, 17, 18, 19, 20, 21}

lo = hi = cur = None
last = 0.0
try:
    while True:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                while True:
                    d = os.read(fd, 128)
                    if not d:
                        break
                    cur = d
                    if lo is None:
                        lo, hi = list(d), list(d)
                    else:
                        for i in range(min(len(d), len(lo))):
                            if d[i] < lo[i]: lo[i] = d[i]
                            if d[i] > hi[i]: hi[i] = d[i]
            except BlockingIOError:
                pass
        now = time.time()
        if cur and now - last > 0.15:
            last = now
            word = lambda i: (cur[i] | (cur[i + 1] << 8)) if len(cur) > i + 1 else -1
            parts = [f'{n}={word(i):>5}' for n, i in CH.items()]
            moved = [i for i in range(len(lo)) if hi[i] != lo[i] and i not in KNOWN]
            extra = f'   OTHER MOVING BYTES: {moved}' if moved else ''
            sys.stdout.write('\r  ' + '   '.join(parts) + extra + '          ')
            sys.stdout.flush()
except KeyboardInterrupt:
    print('\nstopped')
finally:
    os.close(fd)
