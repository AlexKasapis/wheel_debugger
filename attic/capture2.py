import os, glob, select, time, struct, collections
LOG='/tmp/claude-1000/-home-alex/0244953b-932d-4027-95ec-08cb1c49be8d/scratchpad/capture2.log'
out=open(LOG,'w',buffering=1)
def p(*a): print(*a,file=out)

ev=os.path.realpath(glob.glob('/dev/input/by-id/usb-Fanatec_*-event-joystick')[0])
js=os.path.realpath(glob.glob('/dev/input/by-id/usb-Fanatec_*-joystick')[0])
p(f'evdev={ev} js={js}  started, 240s window')

fe=os.open(ev,os.O_RDONLY|os.O_NONBLOCK)
ABS={0:'X',1:'Y',2:'Z',3:'RX',4:'RY',5:'RZ',6:'THROTTLE',7:'RUDDER',16:'HAT0X',17:'HAT0Y'}
rng=collections.defaultdict(lambda:[None,None]); btns=collections.Counter()
n=0; t0=None; last=None; start=time.time(); nexthb=start+15
while time.time()-start < 240:
    r,_,_=select.select([fe],[],[],0.5)
    now=time.time()
    if now>=nexthb:
        p(f'  [t+{int(now-start):3d}s] events so far: {n}'); nexthb=now+15
    if not r: continue
    try:
        while True:
            d=os.read(fe,24)
            if not d: break
            for off in range(0,len(d)-23,24):
                _s,_us,typ,code,val=struct.unpack('qqHHi',d[off:off+24])
                if typ==0: continue
                n+=1
                if t0 is None: t0=now; p(f'  *** FIRST EVENT at t+{int(now-start)}s ***')
                last=now
                if typ==3:
                    a=rng[code]
                    a[0]=val if a[0] is None else min(a[0],val)
                    a[1]=val if a[1] is None else max(a[1],val)
                elif typ==1 and val==1: btns[code]+=1
    except BlockingIOError: pass
os.close(fe)
p(f'\n=== {n} evdev events in 240s ===')
if n==0: p('!! NOTHING received on the real base input node')
else:
    p(f'first at t+{int(t0-start)}s, last at t+{int(last-start)}s')
    p('\n  AXIS RANGES SEEN (min -> max):')
    for c in sorted(rng): 
        lo,hi=rng[c]; nm=ABS.get(c,f'code{c}')
        p(f'    {nm:<10} {lo:>7} -> {hi:>7}   {"MOVED" if lo!=hi else "static"}')
    p(f'\n  buttons pressed: {len(btns)} distinct -> {sorted(btns)}')
D=glob.glob('/sys/bus/hid/drivers/fanatec/*0EB7*')[0]
p('\n=== sysfs after ===')
for f in ('wheel_id','fw_version','range'):
    try: p(f'  {f}: {open(os.path.join(D,f)).read().strip()}')
    except Exception as e: p(f'  {f}: ERR')
T=glob.glob(os.path.join(D,'ftec_tuning','*0EB7*'))
if T:
    for f in ('SEN','FF','DRI'):
        try: p(f'  {f}: {open(os.path.join(T[0],f)).read().strip()}')
        except Exception: pass
