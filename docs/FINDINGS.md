# Fanatec CSL Elite — session notes

## Hardware
- Wheel base: Endor AG FANATEC CSL Elite Wheel Base, USB `0eb7:0e03`
- Nodes: `/dev/input/event6`, `/dev/input/js1`, `hidraw8`
  (stable: `/dev/input/by-id/usb-Fanatec_FANATEC_CSL_Elite_Wheel_Base-*`)
- Rim + pedals report *through* the base — no separate USB devices.

## BEFORE (hid_generic)
- `EV=1b` → SYN, KEY, ABS, MSC. **No FF.**
- 10 axes / 80 buttons. `capabilities/ff = 0`
- Resting axes: X=0, Y=32767, Z=32767, RX=0, RY=0, RZ=32767,
  THROTTLE=32767, RUDDER=0, HAT0X=0, HAT0Y=0
  ** STALE — driver remaps axes + zeroes deadzone/fuzz. Do not reuse. **

## AFTER (hid_fanatec 0.2.3, DKMS)
- Source: https://github.com/gotzl/hid-fanatecff  (tag 0.2.3)
- Built clean against kernel 7.1.5-1-default, no patches.
- DKMS: `hid-fanatec/0.2.3, 7.1.5-1-default, x86_64: installed`
  → auto-rebuilds on Tumbleweed kernel updates
- Module: `/usr/lib/modules/7.1.5-1-default/updates/hid-fanatec.ko.zst`
  (DKMS self-signed via MOK; Secure Boot is disabled so no enrollment needed)
- udev: `/etc/udev/rules.d/99-fanatec.rules`
- Driver name on the HID bus is **`fanatec`** (not `hid-fanatec`):
  `/sys/bus/hid/drivers/fanatec/`
- Autoload verified: `modprobe -R hid:b0003g0001v00000EB7p00000E03` → hid_fanatec
- `EV=20001b` → **EV_FF set**
- FF effects: RUMBLE, PERIODIC, CONSTANT, SPRING, FRICTION, DAMPER,
  INERTIA, SQUARE, TRIANGLE, SINE, SAW_UP, SAW_DOWN
  (not advertised: RAMP, CUSTOM, GAIN, AUTOCENTER)

## Sysfs
- Base:   `/sys/bus/hid/drivers/fanatec/0003:0EB7:0E03.0103/`
  → `range`, `display`, `rumble`, `wheel_id`, `fw_version`, `leds/`, `ftec_tuning/`
- Tuning: `.../ftec_tuning/0003:0EB7:0E03.0103/`
  → SLOT SEN FF SHO BLI DRI FOR SPR DPR brF FEI ACP advanced_mode RESET
- LEDs:   `.../leds/0003:0EB7:0E03.0103::RPM1..RPM9`

## OUTSTANDING
1. Hot-rebind left init incomplete: `wheel_id=0x00`, `fw_version=0`,
   `range=0`, all tuning values `0`. Needs a **power-cycle of the base**
   (fresh USB enumeration) for a clean probe.
2. udev rules did NOT apply — rule guards on `ACTION=="add|change"`, but a
   sysfs bind emits `bind`. LED brightness still root:root, not :games.
   Replug/reboot fixes this too.
3. `games` group added to alex — needs re-login to take effect.
4. **Throttle fault** — user reports a long-standing issue. Deferred.
   Diagnose only AFTER the power-cycle, against the NEW axis map.
   Also needs `wheel_id` resolved first: `0x00` means the button/axis
   mapping currently in effect may be a fallback, not the real rim's.
5. FFB is ADVERTISED, not yet demonstrated. `ffcfstress` test still pending.
6. Untested: whether `fanatec` wins over `hid_generic` on fresh enumeration.
   `modprobe -R` returns BOTH; hid-generic's in-kernel `hid_have_special_driver`
   table can't know about an out-of-tree module. The replug answers this.
   If hid-generic grabs it, need the module loaded before the device appears
   (`/etc/modules-load.d/hid-fanatec.conf` or initramfs).

## POST-REPLUG FINDINGS (important)
- Replug DID happen: hid ids went .0103/.0106 -> .0107/.0108
- `fanatec` claimed the device on fresh enumeration; hid-generic took nothing.
  => boot persistence is fine, no modules-load.d needed.
- udev rules DID fire this time: `range` and `leds/*/brightness` now root:games.
- BUT wheel_id/fw_version/range/all-tuning STILL 0.
- ROOT CAUSE FOUND: these are NOT queried. `hid-ftecff.c:1381-1394` populates
  them passively from incoming HID input reports
  (`data[0]==0x01 && size==FTEC_WHEEL_REPORT_SIZE(34)`; fw_version from last
  2 bytes, wheel_id from data[31]).
  Tuning values likewise come from a 64-byte report.
- **The base sends ZERO HID reports while idle** — verified by reading
  /dev/hidraw8 directly (0666 via udev rule) for 3s: 0 reports.
- USB side is healthy: enumerated, runtime_status=active, EP1 IN interrupt,
  bInterval=1 (1ms), wMaxPacketSize 64. bMaxPower=80mA (logic only; motor
  needs the external PSU).
- ALSO in the same code path (hid-ftecff.c:1371-1383): when data[30]==0xff
  and data[31]==0x04, data[32] encodes **"Pedals connected" (low nibble)** and
  **"Handbrake connected" (high nibble)**. This is the authoritative pedal
  presence check — directly useful for the deferred throttle issue.
- NOTE: the earlier pre-driver js.py "resting axis values" were synthetic
  JS_EVENT_INIT events (initial state on open), NOT live hardware reads.
  Another reason that baseline is void.

## CORRECTION (supersedes "base sends ZERO reports" above)
The base transmits FINE. Two measurement errors on my side:
 (a) /dev/hidraw8 belongs to `.0108`, the driver's VIRTUAL PID-passthrough
     device, not the real base. `hidraw_pid` defaults true and 0x0E03's quirks
     lack FTEC_PEDALS, so hid-ftec.c:882 never attaches HIDRAW to the real dev.
     The real base is `.0107` -> input279 -> event6/js1. It has NO hidraw node.
 (b) the first 90s capture window closed before the user reached the wheel.
2nd capture: 8997 evdev events, first at t+35s, last at t+67s. Device is healthy.

## FF PLUMBING VERIFIED (no motor actuation)
EVIOCGEFFECTS = 16 simultaneous effects.
Upload+erase OK for CONSTANT, SPRING, DAMPER, SINE. Kernel accepts effects.
NOTE: EVIOCRMFF takes the effect id BY VALUE, not as a pointer.
STILL UNTESTED: actual motor torque (needs user go-ahead; ffb-test.py ready,
25% magnitude, 1.5s each direction, bounded).

## *** FFB CONFIRMED WORKING (measured) ***
Ran bounded constant-force test at 25% magnitude while sampling ABS_X:
    LEFT : X moved 12776 -> 33071  (delta 20295, 1348 samples)
    RIGHT: X moved 13095 -> 33633  (delta 20538, 1365 samples)
The motor physically rotated the wheel ~20k counts in each direction.
This is objective proof of torque, not just an EV_FF capability bit.
=> USER'S FFB REQUEST IS COMPLETE.

## AXIS DATA from guided capture (user turned wheel + pressed all 3 pedals)
    X         805 ->  64714   MOVED   (steering, ~full range)
    Z       55615 ->  65535   MOVED   ** only ~15% of range **
    RX       -127 ->    127   MOVED   (rim thumbstick)
    RY       -110 ->     98   MOVED   (rim thumbstick)
    RZ          0 ->  65535   MOVED   (full range - a pedal)
    RUDDER     -6 ->     -4   noise
    Y, THROTTLE, HAT0X, HAT0Y: NO EVENTS AT ALL
13 distinct buttons: 288-291, 294-299, 662, 664, 666
=> 3 pedals pressed but only 2 axes show pedal-like travel, and one of those
   (Z) covers only 15% of its range. STRONG lead on the throttle fault.
   Needs a per-pedal labelled capture to map pedal->axis. DEFERRED per user.

## *** PEDAL DIAGNOSIS (labelled capture, definitive) ***
Hardware per user: F1 Carbon rim; CSL Elite pedals w/ load-cell brake; all 3
pedals on the pedal PCB. Base boot display: 693 (= fw, matches bcdDevice 0693),
then 22, then ---. Only power LED lit; no rim LEDs ever.
Wheel does NOT self-centre (range init landed OK) and has firm end stops.

MAPPING:
  throttle -> Z      brake -> RZ      clutch -> (nothing)

  PHASE            axis   min     max    span    n   revs   span%
  0 baseline       --  NO ACTIVITY (clean noise floor, 0 events)
  1 throttle x3    Z   54864   65535   10671  1174   701   16.3%
  2 throttle HELD  Z   55690   65535    9845   401   248   15.0%
  3 brake x3       RZ      0   65535   65535   417    11  100.0%
  4 clutch x3      --  NO ACTIVITY

VERDICT:
* BRAKE (load cell, RZ): HEALTHY. Full 0-65535 travel, only 11 reversals
  in 417 samples = smooth. Earlier worry about "Z only 15%" was misdirected;
  Z is the throttle, not the brake.
* THROTTLE (Z): FAULTY, two distinct symptoms:
    - COMPRESSED: only 16.3% of range, and confined to the TOP
      (rest=65535, full press only reaches ~54864; should reach ~0)
    - EXTREMELY NOISY: while HELD MOTIONLESS it swings 15% of full range,
      stdev 1378.6, 248 direction reversals in 401 samples.
      Brake for comparison: 11 reversals. This is ~25x noisier while static.
  Consistent with a worn/oxidised potentiometer wiper losing track contact.
* CLUTCH: NO SIGNAL AT ALL. User never used it; may never have worked.

NOTE: both bad pedals are the POT-based ones; the load-cell brake (separate
amplifier path) is fine. Could be coincidence (throttle wear + never-working
clutch) or a common pot reference/connector issue on the pedal PCB.

NEXT (hardware bisect, cheapest first):
 1. SWAP throttle and clutch connectors on the pedal PCB.
    fault follows the pedal  -> sensor/cable
    fault stays with the port -> pedal PCB
 2. Multimeter: pot end-to-end resistance (should be stable ~10k) and
    wiper-to-end while sweeping slowly (should be smooth, no dropouts/opens).
 3. Scope on wiper while sweeping - dropouts show as spikes/discontinuities.
 4. OPTIONAL software cross-check: `options hid_fanatec hidraw_pid=0` in
    /etc/modprobe.d/ gives HIDRAW on the REAL device (.0107) so raw report
    bytes can be watched -> proves whether clutch data is absent from the
    HID report (hardware) or merely unmapped by the driver (software).
    Relevant because wheel_id=0x00 means the axis map is a FALLBACK.

## *** SWAP BISECT RESULT - TWO INDEPENDENT FAULTS ***
User ran raw-pedal-map.py on ORIGINAL wiring, then swapped the throttle and
clutch connectors on the controller board and ran pedal-map.py. Prompts refer
to the PEDAL pressed, not the input it was plugged into.

Raw report is 33 bytes, id 0x08. Channel -> byte offsets (LE u16):
    steering      [16:17]
    throttle-IN   [18:19]   (= driver axis Z)
    brake-IN      [20:21]   (= driver axis RZ)

  RUN 1 (original wiring, RAW bytes):
    steering        789 -> 64745   97.6%   OK
    throttle pedal in throttle-IN : 63330 -> 65535   3.4%   BAD
    brake                             0 -> 65535  100.0%   OK
    clutch pedal in clutch-IN     : NO REPORTS AT ALL

  RUN 2 (throttle<->clutch connectors SWAPPED, evdev):
    throttle pedal in clutch-IN   : Z span 66     0.1%   nothing (noise only)
    brake                           RZ 0->65535 100.0%   OK
    clutch pedal in throttle-IN   : Z 0 -> 18883  28.8%, 464 samples,
                                    only 7 reversals = SMOOTH

CONCLUSION (substitution proves each half):
  * throttle-IN channel  : GOOD  - clutch pedal read smoothly through it
  * clutch-IN channel    : DEAD  - two different pedals both produced NOTHING;
                           raw capture shows the base sends no report at all
  * throttle PEDAL       : BAD   - garbage (3.4%, 701/248 reversals) in a
                           known-good input. Fault FOLLOWS the pedal.
  * clutch PEDAL         : OK    - clean smooth signal in a known-good input.
                           Fault does NOT follow this pedal.

Throttle pedal span varied between runs (16.3% then 3.4%) - intermittent,
consistent with a wiper making poor contact rather than a fixed offset.
The clutch pedal's 28.8% span in the throttle channel is likely pot/calibration
difference; the SMOOTHNESS (7 reversals vs 248-701) is the health signal.

REPAIR TARGETS:
  1. Throttle pedal pot: clean/reseat/replace. Check wire + solder at its plug.
  2. Clutch input channel on the controller board: check its connector solder
     joints and whether the sensor supply voltage is present on that header
     (compare against the working throttle header while powered).

## REMAINING PUZZLE
wheel_id/fw_version/tuning all still 0 even though input reports flow.
=> base answers normal input polling but not the driver's info-request reports
   (0xf8 0x09 sequence, hid-ftecff.c:1188-1195). Affects sysfs tuning + `range`,
   not necessarily FFB. Possibly firmware-version dependent.

## OLD NEXT TEST (superseded)
Capture /dev/hidraw8 while the user turns the wheel / presses pedals.
- reports appear => device is send-on-change only, all fine, values populate
- still nothing => base main power off / standby (check PSU + power switch)

## *** MULTIMETER RESULTS — REVISES THE SWAP-BISECT CONCLUSION ***
Jacks are 6-pin (small RJ-type), NOT 3-pin.
- Throttle / brake / clutch jacks: outermost pins (0 <-> 5) all read a stable
  3.3 V. => SENSOR SUPPLY REACHES THE CLUTCH JACK. Supply side is fine.
- Handbrake jack: no 3.3 V on 0<->5. Different pinout / unpopulated. IRRELEVANT
  (user has no handbrake). Do not chase.

User pulled the pot out of the CLUTCH pedal and measured at the pot's OWN pins:
- clutch pot in throttle jack: clean, stable 0 -> 3.3 V end to end
- clutch pot in clutch jack:   identical
=> CLUTCH POT IS HEALTHY.
!! CAVEAT: measuring at the pot's own wiper is UPSTREAM of the return wire.
   The wiper voltage is generated by the pot's own divider, so it reads correct
   even if the signal line back to the board is fully open. This test does NOT
   clear the clutch jack. Clutch signal return remains the prime suspect.

*** THROTTLE JACK IS MECHANICALLY FLAKY ***
Nudging the plug while seated makes readings go bad or drop to zero.
Critically: the collapse is visible AT THE POT's pins => the interruption is in
the jack/plug contact itself, not downstream on the board.
User cannot reproduce it on demand — only force a good seating. Hence the web
dashboard (latching glitch detector) instead of timed captures.

=> REVISION: "throttle pedal is bad" is NO LONGER established.
   Throttle symptoms (rest pinned at 65535, ~3% span, 248 reversals/401 samples
   held still) are equally explained by an intermittent connection. A worn pot
   and a flaky jack produce the same signature. Both hypotheses live.
   Leading theory now: aged solder joints / contacts on the pedal jacks —
   one intermittent (throttle), one open on the signal pin (clutch).

POT TRAVEL QUESTION (user removed clutch pot without marking position):
Pedal sweeping only ~1/3 of the pot's electrical arc is NORMAL by design.
Requirement is only: at rest the wiper sits just off one end stop, and at full
pedal travel it has not run past the other. Set by eye, verified live. Nothing
was ruined. ALWAYS mark pot body->shaft with a marker + photo before removing.

## OBSERVED IDLE BEHAVIOUR (matters for tooling thresholds)
- Base idles at ~9 reports/s with nothing moving => a dropout threshold of
  250 ms false-fires constantly. Use >= 2 s.
- With pedals disassembled/unplugged: THR-IN rests at 0, BRK-IN rests at 65535,
  STEER centred 32768 with ~±30 LSB dither (stdev ~10).
  => stdev is the good health metric at rest: LSB dither ~10 vs bad throttle
  ~1380. Reversal COUNT is useless at rest (noise reverses ~50% of samples);
  normalise it per 100 samples and only trust it while the channel is moving.
- Device re-enumerates on replug: .0107 -> .0109. Always resolve via
  /dev/input/by-id/usb-Fanatec_*-hidraw, never a hardcoded hidrawN.

## Scripts
- `pedal-web.py`    — LOCAL WEB DASHBOARD, http://localhost:8765 (also LAN, for
                      phone). Background thread reads hidraw at full rate and
                      LATCHES glitches (JUMP >3000 delta, RAIL entry, DROPOUT
                      >2 s) so an intermittent fault that lasts 20 ms still
                      shows. Live sparklines, rolling stdev, per-byte min/max
                      grid, and auto-detection of unknown 16-bit channels that
                      start moving (= how a revived clutch channel announces
                      itself). Stdlib only. Verified pulling real 33-byte
                      reports.
- `raw-live.py`     — terminal-only live readout, same channels
- `install-ffb.sh`  — root install (already run successfully)
- `verify-ffb.sh`   — checks ff caps + effects (note: its js-node glob and
                      driver readlink are cosmetically wrong; ff decode in it
                      mislabels bits — use the sysfs `capabilities/ff` decode)
- `js.py`           — joystick axis/button sampler
