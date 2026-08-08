import os, glob, time, select, subprocess
ev = os.path.realpath(glob.glob('/dev/input/by-id/usb-Fanatec_*-event-joystick')[0])
D  = glob.glob('/sys/bus/hid/drivers/fanatec/*0EB7*')[0]

def snap(tag):
    vals = {}
    for f in ('wheel_id','fw_version','range'):
        try: vals[f] = open(os.path.join(D,f)).read().strip()
        except Exception as e: vals[f] = f'ERR'
    T = glob.glob(os.path.join(D,'ftec_tuning','*0EB7*'))
    if T:
        for f in ('SEN','FF','DRI','SPR','DPR'):
            try: vals[f] = open(os.path.join(T[0],f)).read().strip()
            except Exception: pass
    print(f'{tag:<22}', ' '.join(f'{k}={v}' for k,v in vals.items()))

print('event node:', ev)
snap('before open:')
fd = os.open(ev, os.O_RDONLY | os.O_NONBLOCK)
print('-- device opened, letting reports flow 3s --')
n = 0
end = time.time() + 3
while time.time() < end:
    r,_,_ = select.select([fd],[],[],0.3)
    if r:
        try:
            while True:
                d = os.read(fd, 24)
                if not d: break
                n += 1
        except BlockingIOError: pass
print(f'-- {n} evdev events seen --')
snap('while open:')
os.close(fd)
time.sleep(0.3)
snap('after close:')
