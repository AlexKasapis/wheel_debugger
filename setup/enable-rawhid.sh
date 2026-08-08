#!/usr/bin/env bash
# Expose HIDRAW on the REAL wheel base (.0107) instead of the virtual PID device,
# so raw report bytes can be inspected. Fully reversible: see revert-rawhid.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

echo "options hid_fanatec hidraw_pid=0" > /etc/modprobe.d/hid-fanatec.conf
echo "==> wrote /etc/modprobe.d/hid-fanatec.conf"

# unbind cleanly, then reload with the new parameter
for dev in /sys/bus/hid/drivers/fanatec/*0EB7*; do
    [ -e "$dev" ] || continue
    echo -n "$(basename "$dev")" > /sys/bus/hid/drivers/fanatec/unbind 2>/dev/null || true
done
modprobe -r hid_fanatec 2>/dev/null || true
modprobe hid_fanatec
echo "==> reloaded module; hidraw_pid is now:"
cat /sys/module/hid_fanatec/parameters/hidraw_pid 2>/dev/null || echo "   (param not readable, expected)"

# rebind anything hid-generic may have grabbed in the gap
for dev in /sys/bus/hid/drivers/hid-generic/*0EB7*; do
    [ -e "$dev" ] || continue
    id="$(basename "$dev")"
    echo -n "$id" > /sys/bus/hid/drivers/hid-generic/unbind || true
    echo -n "$id" > /sys/bus/hid/drivers/fanatec/bind || true
done
udevadm settle || true

echo
echo "==> fanatec-bound devices:"
ls /sys/bus/hid/drivers/fanatec/ | grep 0EB7 || echo "   none!"
echo "==> hidraw ownership:"
for h in /sys/class/hidraw/hidraw*; do
    d=$(readlink -f "$h/device" 2>/dev/null) || continue
    case "$d" in *0EB7*) echo "   $(basename "$h") -> $(basename "$d")";; esac
done
echo
echo "If a device now has BOTH an input dir and a hidraw dir, we have raw access."
