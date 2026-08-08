import os, glob, select, time, collections
p = os.path.realpath(glob.glob('/dev/input/by-id/usb-Fanatec_*-hidraw')[0])
print('hidraw:', p)
fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
seen = collections.Counter()
first = {}
end = time.time() + 3.0
total = 0
while time.time() < end:
    r,_,_ = select.select([fd],[],[],0.3)
    if not r: continue
    try:
        while True:
            d = os.read(fd, 128)
            if not d: break
            total += 1
            key = (d[0], len(d))
            seen[key] += 1
            first.setdefault(key, d)
    except BlockingIOError:
        pass
os.close(fd)
print(f'total reports in 3s: {total}')
for (rid, sz), n in seen.most_common():
    d = first[(rid,sz)]
    print(f'\n  report id=0x{rid:02x} size={sz} count={n}')
    print('   ', ' '.join(f'{b:02x}' for b in d))
    if sz >= 34:
        print(f'    data[30]=0x{d[30]:02x} data[31]=0x{d[31]:02x} data[32]=0x{d[32]:02x} data[33]=0x{d[33]:02x}')
