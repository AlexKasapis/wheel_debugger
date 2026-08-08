import fcntl, struct, array, os, select, time
fd = os.open('/dev/input/js1', os.O_RDONLY | os.O_NONBLOCK)
buf = array.array('B', [0])
fcntl.ioctl(fd, 0x80016a11, buf); axes = buf[0]
fcntl.ioctl(fd, 0x80016a12, buf); btns = buf[0]
name = array.array('B', [0]*128)
fcntl.ioctl(fd, 0x80006a13 + (128 << 16), name)
print("name   :", bytes(name).split(b'\x00')[0].decode())
print("axes   :", axes)
print("buttons:", btns)

axmap = array.array('B', [0]*64)
fcntl.ioctl(fd, 0x80406a32, axmap)   # JSIOCGAXMAP
ABS = {0:'X (steering)',1:'Y',2:'Z',3:'RX',4:'RY',5:'RZ',6:'THROTTLE',7:'RUDDER',16:'HAT0X',17:'HAT0Y'}
print("axis map:", [ABS.get(axmap[i], axmap[i]) for i in range(axes)])

# sample 1.5s of initial state
vals = {}
end = time.time() + 1.5
while time.time() < end:
    r,_,_ = select.select([fd], [], [], 0.2)
    if not r: continue
    while True:
        try: ev = os.read(fd, 8)
        except BlockingIOError: break
        if len(ev) < 8: break
        t, v, typ, num = struct.unpack('IhBB', ev)
        if typ & 0x02: vals[num] = v
print("resting axis values:", {ABS.get(axmap[k], k): v for k, v in sorted(vals.items())})
os.close(fd)
