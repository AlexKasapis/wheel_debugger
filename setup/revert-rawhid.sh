#!/usr/bin/env bash
# Undo enable-rawhid.sh - restores the default virtual PID passthrough device.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
rm -fv /etc/modprobe.d/hid-fanatec.conf
for dev in /sys/bus/hid/drivers/fanatec/*0EB7*; do
    [ -e "$dev" ] || continue
    echo -n "$(basename "$dev")" > /sys/bus/hid/drivers/fanatec/unbind 2>/dev/null || true
done
modprobe -r hid_fanatec 2>/dev/null || true
modprobe hid_fanatec
for dev in /sys/bus/hid/drivers/hid-generic/*0EB7*; do
    [ -e "$dev" ] || continue
    id="$(basename "$dev")"
    echo -n "$id" > /sys/bus/hid/drivers/hid-generic/unbind || true
    echo -n "$id" > /sys/bus/hid/drivers/fanatec/bind || true
done
udevadm settle || true
echo "reverted to defaults (hidraw_pid=true)"
ls /sys/bus/hid/drivers/fanatec/ | grep 0EB7 || echo "none bound!"
