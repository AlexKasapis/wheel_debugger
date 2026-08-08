import os, glob, select, time, collections, sys

out = open('capture.log','w',buffering=1)
def p(*a):
    print(*a, file=out); print(*a)

raw = os.path.realpath(glob.glob('/dev/input/by-id/usb-Fanatec_*-hidraw')[0])
ev  = os.path.realpath(glob.glob('/dev/input/by-id/usb-Fanatec_*-event-joystick')[0])
p(f'hidraw={raw}  evdev={ev}')

fr = os.open(raw, os.O_RDONLY | os.O_NONBLOCK)
fe = os.open(ev,  os.O_RDONLY | os.O_NONBLOCK)   # evdev open -> hid_hw_open -> URB submission

seen  = collections.Counter()
first = {}
axis_min, axis_max = {}, {}
total_raw = total_ev = 0
end = time.time() + 90

while time.time() < end:
    r,_,_ = select.select([fr,fe],[],[],0.5)
    for fd in r:
        try:
            while True:
                d = os.read(fd, 128)
                if not d: break
                if fd == fr:
                    total_raw += 1
                    k = (d[0], len(d))
                    seen[k] += 1
                    first.setdefault(k, d)
                    if len(d) >= 34 and d[0] == 0x01:
                        if d[30] == 0xff and d[31] == 0x04:
                            seen[('AUXINFO', d[32])] += 1
                else:
                    total_ev += 1
        except BlockingIOError:
            pass

p(f'\n=== {total_raw} raw HID reports, {total_ev} evdev events in 90s ===')
if total_raw == 0:
    p('!! base sent NOTHING -- it is not transmitting at all')
for k, n in seen.most_common():
    if k[0] == 'AUXINFO':
        b = k[1]
        p(f'\n  AUX INFO byte=0x{b:02x} count={n}')
        p(f'    pedals    (low nibble) : {"CONNECTED" if b & 0xf else "not connected"}')
        p(f'    handbrake (high nibble): {"CONNECTED" if b >> 4 & 0xf else "not connected"}')
        continue
    rid, sz = k
    d = first[k]
    p(f'\n  report id=0x{rid:02x} size={sz} count={n}')
    p('    ' + ' '.join(f'{x:02x}' for x in d))
    if sz >= 34:
        p(f'    data[30]=0x{d[30]:02x} data[31]=0x{d[31]:02x} (wheel_id) '
          f'fw={d[32] | (d[33]<<8)}')
os.close(fr); os.close(fe)
p('\n=== sysfs after capture ===')
D = glob.glob('/sys/bus/hid/drivers/fanatec/*0EB7*')[0]
for f in ('wheel_id','fw_version','range'):
    try: p(f'  {f}: {open(os.path.join(D,f)).read().strip()}')
    except Exception as e: p(f'  {f}: ERR {e}')
