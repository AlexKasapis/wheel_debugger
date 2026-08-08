#!/usr/bin/env bash
# Verify the Fanatec FFB driver took over. Run as normal user (no sudo needed).
EV=$(readlink -f /dev/input/by-id/usb-Fanatec_*-event-joystick 2>/dev/null | head -1)
JS=$(readlink -f /dev/input/by-id/usb-Fanatec_*-joystick 2>/dev/null | head -1)

echo "event node : ${EV:-NOT FOUND}"
echo "js node    : ${JS:-NOT FOUND}"
[ -n "$EV" ] || exit 1

NAME=$(basename "$EV")
CAP="/sys/class/input/${NAME}/device/capabilities"
echo "ev caps    : $(cat "$CAP/ev" 2>/dev/null)"
echo "ff caps    : $(cat "$CAP/ff" 2>/dev/null)   <-- nonzero means force feedback is live"
echo "driver     : $(basename "$(readlink -f "/sys/class/input/${NAME}/device/../driver" 2>/dev/null)")"
echo
echo "loaded module:"; /sbin/lsmod | grep -E "hid_fanatec|hid_generic"
echo
echo "sysfs tuning dir:"
find /sys/module/hid_fanatec /sys/bus/hid/drivers/*fanatec*/*0EB7* \
     -maxdepth 1 -name 'range' -o -maxdepth 1 -name 'ftec_tuning' 2>/dev/null | head
echo
echo "FF effects supported by the device:"
python3 - "$EV" <<'PY'
import sys, fcntl, array, os
FF = {0:'RUMBLE',1:'PERIODIC',2:'CONSTANT',3:'SPRING',4:'FRICTION',5:'DAMPER',
      6:'INERTIA',7:'RAMP',8:'SQUARE',9:'TRIANGLE',10:'SINE',11:'SAW_UP',
      12:'SAW_DOWN',13:'CUSTOM',80:'GAIN',81:'AUTOCENTER'}
try:
    fd = os.open(sys.argv[1], os.O_RDONLY)
except PermissionError:
    print("  (permission denied - re-login for group access)"); sys.exit()
buf = array.array('B', [0]*16)
# EVIOCGBIT(EV_FF=0x15, len)
fcntl.ioctl(fd, 0x80004520 + 0x15 + (len(buf) << 16), buf)
bits = int.from_bytes(bytes(buf), 'little')
got = [n for b, n in FF.items() if bits >> b & 1]
print("  " + (", ".join(got) if got else "NONE - force feedback not available"))
os.close(fd)
PY
