# Driver and machine state

`hid-fanatec` 0.2.3 from <https://github.com/gotzl/hid-fanatecff>, built clean
against kernel 7.1.5-1-default with no patches and installed via DKMS so it
survives Tumbleweed kernel updates. Installed by `setup/install-ffb.sh`.

- Driver name on the HID bus is **`fanatec`**, not `hid-fanatec`:
  `/sys/bus/hid/drivers/fanatec/`
- Module: `/usr/lib/modules/<kver>/updates/hid-fanatec.ko.zst`, DKMS self-signed
  via MOK (Secure Boot is disabled, so no enrolment needed)
- udev: `/etc/udev/rules.d/99-fanatec.rules`. The rule guards on
  `ACTION=="add|change"` but a sysfs rebind emits `bind`, so unbind/rebind (what
  `enable-rawhid.sh` does) leaves `range` and `leds/*/brightness` root-owned
  until a replug or reboot.
- It claims the device on fresh enumeration and hid-generic takes nothing, so
  boot persistence needs no `modules-load.d` entry
- `EV=20001b` — `EV_FF` set. 12 effect types (RUMBLE, PERIODIC, CONSTANT, SPRING,
  FRICTION, DAMPER, INERTIA, SQUARE, TRIANGLE, SINE, SAW_UP, SAW_DOWN); RAMP,
  CUSTOM, GAIN and AUTOCENTER are not advertised. 16 simultaneous effect slots.

`dkms status` is not a usable probe for "is this working" — dkms is not installed
on this box, yet the module is loaded and bound. `/proc/modules` plus the bound
symlink under the driver dir are the reliable root-free signals.

## The virtual PID device — the expensive trap

The driver creates a *virtual* PID passthrough device alongside the real base.
`hidraw_pid` defaults true and `0x0E03`'s quirks lack `FTEC_PEDALS`, so
`hid-ftec.c:882` never attaches HIDRAW to the real device. A hidraw node pointing
at the virtual one opens fine and delivers zero reports forever, which on screen
is indistinguishable from dead hardware.

`setup/enable-rawhid.sh` writes `options hid_fanatec hidraw_pid=0` and reloads,
pointing HIDRAW at the real base; `setup/revert-rawhid.sh` undoes it. The
dashboard's system checks detect which device it is reading by parsing the
descriptor, and say `READING THE WRONG DEVICE` rather than `not connected`.

`/sys/module/hid_fanatec/parameters/hidraw_pid` does not exist, so the live
parameter cannot be read back; the modprobe.d file is the only available proxy.

## Why sysfs wheel_id / fw_version / tuning all read 0

`ftecff_raw_event()` (`hid-ftecff.c:1370`) only parses wheel info when
`data[0] == 0x01 && size == FTEC_WHEEL_REPORT_SIZE (34)` — a *numbered* 34-byte
report. This base sends 33 bytes with no report ID, so that branch never runs.
Nothing is wrong with the base and it is not refusing info requests; the driver's
parser simply does not match this device's report format.

The 64-byte tuning report never lands for the same reason, so every
`ftec_tuning/*` value including `ACP` reads 0 — that is **"no data", not
"mode 0"**. Read `ACP` off the base's own tuning display instead.

The dashboard decodes all of these out of the raw report itself; see
[report-map.md](report-map.md#vendor-block).

## games group

`install-ffb.sh` adds the invoking user to `games` for sysfs tuning access.
Group membership is fixed at login, so it **needs a re-login to take effect** —
until then the `rumble`/`display`/tuning writes stay denied. Run the installer
with `sudo` rather than from a root shell so it can identify the desktop user;
from a root shell pass `REAL_USER=<name>`.

## Sysfs paths

    /sys/bus/hid/drivers/fanatec/0003:0EB7:0E03.<n>/
        range display rumble wheel_id fw_version leds/ ftec_tuning/
    .../ftec_tuning/0003:0EB7:0E03.<n>/
        SLOT SEN FF SHO BLI DRI FOR SPR DPR brF FEI ACP advanced_mode RESET
    .../leds/0003:0EB7:0E03.<n>::RPM1..RPM9
