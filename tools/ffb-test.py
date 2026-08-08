#!/usr/bin/env python3
"""Controlled FFB test: bounded magnitude, bounded duration, auto-stop.

Uploads a CONSTANT force effect at a gentle magnitude, plays it for 1.5s in
one direction, pauses, then 1.5s in the other, then erases the effect.
Nothing runs open-ended -- unlike ffcfstress.
"""
import os, glob, fcntl, struct, time, sys

MAGNITUDE = int(0.25 * 32767)   # 25% of full scale
DURATION_MS = 1500

ev = os.path.realpath(glob.glob('/dev/input/by-id/usb-Fanatec_*-event-joystick')[0])
print(f'device: {ev}')
fd = os.open(ev, os.O_RDWR)

# struct ff_effect is 48 bytes on x86_64 (union is 8-byte aligned -> starts at 16)
EVIOCSFF = 0x40000000 | (48 << 16) | (ord('E') << 8) | 0x80
EVIOCRMFF = 0x40000000 | (4 << 16) | (ord('E') << 8) | 0x81

def upload(direction, level, length_ms, eid=-1):
    # type=FF_CONSTANT(0x52), id, direction, trigger(button,interval),
    # replay(length,delay), pad, then ff_constant_effect{level, envelope[4]}
    buf = bytearray(48)
    struct.pack_into('<HhH', buf, 0, 0x52, eid, direction)      # type,id,direction
    struct.pack_into('<HH', buf, 6, 0, 0)                        # trigger
    struct.pack_into('<HH', buf, 10, length_ms, 0)               # replay
    struct.pack_into('<h', buf, 16, level)                       # constant.level
    struct.pack_into('<HHHH', buf, 18, 0, 0, 0, 0)               # envelope
    fcntl.ioctl(fd, EVIOCSFF, buf, True)
    return struct.unpack_from('<h', buf, 2)[0]

def play(eid, on=1):
    # input_event: timeval(16) + type(2) + code(2) + value(4) = 24 bytes
    os.write(fd, struct.pack('<qqHHi', 0, 0, 0x15, eid, on))

try:
    print(f'uploading CONSTANT effect, magnitude {MAGNITUDE} (25%), {DURATION_MS}ms')
    eid = upload(0x4000, MAGNITUDE, DURATION_MS)
    print(f'  effect id = {eid}')

    for label, direction in (('LEFT', 0x4000), ('RIGHT', 0xC000)):
        eid = upload(direction, MAGNITUDE, DURATION_MS, eid)
        print(f'  --> pushing {label} for {DURATION_MS/1000:.1f}s ...')
        play(eid, 1)
        time.sleep(DURATION_MS / 1000 + 0.3)
        play(eid, 0)
        time.sleep(0.5)

    fcntl.ioctl(fd, EVIOCRMFF, eid)      # takes the id BY VALUE, not a pointer
    print('effect erased. done.')
except OSError as e:
    print(f'FAILED: {e}  (errno {e.errno})', file=sys.stderr)
    sys.exit(1)
finally:
    os.close(fd)
