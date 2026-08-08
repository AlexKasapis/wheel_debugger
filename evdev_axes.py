#!/usr/bin/env python3
"""Reading the axes back through evdev, the path games actually take.

Lives here rather than in a tool so the HID->ABS table exists once. That table
is empirical, not derivable: the descriptor does not say where hid-input lands
each usage, and two of them land somewhere the name does not suggest - Slider on
ABS_THROTTLE and Dial on ABS_RUDDER - so neither evdev name means a pedal.

When a channel moves in the raw report but not here, the fault is between the
driver and the game rather than in the base.
"""
import fcntl
import os
import struct

# hid-input's own mapping, confirmed against this base.
HID_TO_ABS = {'X': 0, 'Y': 1, 'Z': 2, 'Rx': 3, 'Ry': 4, 'Rz': 5,
              'Slider': 6, 'Dial': 7}
ABS_NAMES = {0: 'ABS_X', 1: 'ABS_Y', 2: 'ABS_Z', 3: 'ABS_RX', 4: 'ABS_RY',
             5: 'ABS_RZ', 6: 'ABS_THROTTLE', 7: 'ABS_RUDDER'}

EV_ABS = 0x03
EVENT_FMT = 'llHHi'     # input_event: timeval, type, code, value
EVENT_SZ = struct.calcsize(EVENT_FMT)
ABSINFO_SZ = 24         # struct input_absinfo: 6 x s32


def axis_codes(layout):
    """{hid usage name: ABS code} for the axes this layout declares."""
    return {ax['hid']: HID_TO_ABS[ax['hid']]
            for ax in layout['axes'] if ax['hid'] in HID_TO_ABS}


def current(fd, code):
    """One axis's value now via EVIOCGABS, or None if the axis is unsupported.

    Works with hands off a send-on-change base: the driver keeps the last value,
    so this answers "where is it resting" without waiting for a report.
    """
    buf = bytearray(ABSINFO_SZ)
    req = (2 << 30) | (ABSINFO_SZ << 16) | (ord('E') << 8) | (0x40 + code)
    try:
        fcntl.ioctl(fd, req, buf)
    except OSError:
        return None
    return struct.unpack('6i', buf)[0]


def drain(fd, seen):
    """Fold every queued EV_ABS event into {code: (min, max)}."""
    while True:
        try:
            buf = os.read(fd, EVENT_SZ * 64)
        except (BlockingIOError, OSError):
            return
        if not buf:
            return
        for off in range(0, len(buf) - EVENT_SZ + 1, EVENT_SZ):
            _, _, typ, code, val = struct.unpack_from(EVENT_FMT, buf, off)
            if typ != EV_ABS:
                continue
            lo, hi = seen.get(code, (val, val))
            seen[code] = (min(lo, val), max(hi, val))
