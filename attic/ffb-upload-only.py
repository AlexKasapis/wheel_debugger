# Uploads FF effects and erases them WITHOUT ever sending a play event.
# An effect only actuates when an EV_FF input_event with value>0 is written.
import os, glob, fcntl, struct
ev = os.path.realpath(glob.glob('/dev/input/by-id/usb-Fanatec_*-event-joystick')[0])
fd = os.open(ev, os.O_RDWR)
EVIOCSFF  = 0x40000000 | (48 << 16) | (ord('E') << 8) | 0x80
EVIOCRMFF = 0x40000000 | (4  << 16) | (ord('E') << 8) | 0x81
# how many effects can be held simultaneously
n = struct.unpack('i', fcntl.ioctl(fd, 0x80044584, b'\0'*4))[0]   # EVIOCGEFFECTS
print(f'device: {ev}')
print(f'max simultaneous effects: {n}')

for name, etype, extra in (('CONSTANT', 0x52, None), ('SPRING', 0x53, None), ('DAMPER', 0x55, None)):
    buf = bytearray(48)
    struct.pack_into('<HhH', buf, 0, etype, -1, 0x4000)
    struct.pack_into('<HH', buf, 10, 1000, 0)     # replay length 1000ms
    if etype == 0x52:
        struct.pack_into('<h', buf, 16, 8000)     # level (never played)
    try:
        fcntl.ioctl(fd, EVIOCSFF, buf, True)
        eid = struct.unpack_from('<h', buf, 2)[0]
        print(f'  {name:<9} upload OK  -> effect id {eid}')
        fcntl.ioctl(fd, EVIOCRMFF, struct.pack('<i', eid))
    except OSError as e:
        print(f'  {name:<9} upload FAILED: {e}')
os.close(fd)
print('all effects erased; motor never actuated')
