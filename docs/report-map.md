# Raw report map

Parsed from the base's own report descriptor
(`/sys/class/hidraw/hidrawN/device/report_descriptor`, 133 bytes) by
`hid_layout.py`. Not inferred from wiggling things — this is what the device
declares, and it agrees with every offset previously established by capture.

There is **no report ID**, so the 33 bytes are pure payload. (An earlier note
claiming "33 bytes, id 0x08" misread byte 0's hat nibble at rest, where 8 =
centred.)

| Bytes | Field | Channel |
| --- | --- | --- |
| 0, bits 0-3 | Hat switch | 0-7 = N..NW, 8 = centred |
| bits 4-111 (bytes 0-13) | 108 buttons, LSB-first | button N at bit N+3 |
| 14-15 | declared as buttons but are not | byte 14 = `0x00`, byte 15 = constant `0x16` |
| 16-17 | `X` u16 LE | **STEER** |
| 18-19 | `Z` u16 LE | **THROTTLE** (throttle-IN jack) |
| 20-21 | `Rz` u16 LE | **BRAKE** (brake-IN jack, load cell) |
| 22-23 | `Y` u16 LE | **CLUTCH** (clutch-IN jack) |
| 24 / 25 | `Rx` / `Ry` s8 | rim ministick X / Y |
| 26 / 27 | `Slider` u8 / `Dial` s8 | rim analog |
| 28-32 | vendor | fw version, wheel id, pedal presence |

Button number N sits at bit N+3, i.e. byte `(N+3)//8` bit `(N+3)%8`. So button 5
= GEAR UP = byte 1 bit 0, and button 6 = GEAR DOWN = byte 1 bit 1 (functions per
the `ftec_keymap` comments in `hid-ftec.c`).

STEER and the ministick are **separate fields** — byte 16 versus bytes 24-25. If
moving the ministick appears to move the steering, hold the rim firmly still and
try again: the rim does not self-centre, so a thumb on the stick rotates the
wheel for real.

## Vendor block

Decoded with the driver's own field offsets shifted by one for the missing ID
byte (see [driver.md](driver.md) for why the driver itself never reads them):

    fw_version = LE16(byte 31, byte 32)
    wheel_id   = byte 30
    when byte 29 == 0xff and byte 30 == 0x04:
        byte 31 low nibble  = pedals connected
        byte 31 high nibble = handbrake connected

From the archived capture: `fw_version` 693, `wheel_id` `0x20` (not in
`hid-ftec.h`'s known-rim list).

## Resting values and polarity

From `data/raw-pedal-map.log`, first report of the steering phase: STEER 32783
(centred), THROTTLE 65535, BRAKE 65535, CLUTCH 65535, ministick 0/0, Slider 255,
Dial −4.

**All three pedal channels rest at 65535 and fall towards 0 under press** — full
scale is the *released* end. The archived capture shows it twice: bytes 18-23 are
`ff ff` at rest, and the brake phase sweeps to 0. A later bracketed capture
caught the throttle doing the same, 65535 → 63699. Games are therefore told every
pedal is inverted and need their own invert setting; that is this base's
polarity, not a fault.

So **brake = 0 is the fully-pressed end, not a resting value** — a brake sitting
at 0 untouched is pinned past full press. (An earlier note here claimed a load
cell at zero force resting at 0 was *correct*. It contradicted the resting values
directly above it, and the unplug capture in [findings.md](findings.md) settled
it.)

With the brake unplugged the channel reads 65535, observed directly. The
equivalent for an unplugged *pot* channel is not established: an old note claimed
THROTTLE reads 0 unplugged, which would make the two input circuits asymmetric,
but nothing in `data/` shows it.
