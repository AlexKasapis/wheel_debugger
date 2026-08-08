# Fanatec CSL Elite — session notes

## *** BRAKE HAS GONE SILENT — new fault, captured with positive control ***
User reported "the brake does not display anything in the web app". It does not,
and the channel really is producing nothing — but the first look at it was
**uninterpretable**, for a reason worth keeping.

**The base is send-on-change.** At rest it transmits NOTHING — not the ~9/s
recorded further down this file. So "I pressed X and the page did not move" has
two indistinguishable causes: X produces no data, or the base is not
transmitting at all. Every brake test must carry a positive control in the same
window.

FIRST LOOK (worthless, kept as the cautionary example): dashboard showed
BRAKE flat at 0 and the banner read `138.5 rep/s, 699 total`. But `count` was
frozen at 699 while `uptime` climbed 131 → 185 s, and a direct read of
`/dev/hidraw8` blocked for 6 s. The base had sent nothing for ~160 s. Nothing
could have moved. (`rate` freezes at its last value when the stream dies — now
fixed, see below.)

GUIDED CAPTURE WITH POSITIVE CONTROL (raw hidraw, every byte watched):

    47.7 - 54.4 s   STEER    32889 -> 21465 -> 34074      moved, stream alive
    56.4 - 59.8 s   THROTTLE 65535 -> 63847 -> 65502      moved, 3% of range
    then            BRAKE pressed fully, 3x, ~40 s        ZERO REPORTS EMITTED

Steering and throttle prove the base was awake and streaming minutes into the
same capture. The brake presses produced **not one report** — the base did not
even consider anything to have changed.

* BRAKE bytes 20-21: `lo=0 hi=0` across 699 reports in one session and across
  the whole guided capture in another. Rest value 0 is the brake's **correct**
  resting value (a load cell at zero force; the original good capture ran
  0 -> 65535), so **0 is not by itself a disconnection signature.**
* No OTHER byte in the 33-byte report moved during the brake presses either.
  The brake's data is absent from the entire report, not merely mislabelled or
  landing on a different channel.
* Clutch pedal was physically OUT during this capture and was not pressed, so
  CLUTCH (65535, flat) says nothing here.
* THROTTLE moved 63566 -> 65535 on a FULL press = **3.0% of range**, matching
  the bad 3.4% run rather than the 16.3% one. The intermittent throttle fault is
  still present and currently at its worst.

STATUS: the brake was measured HEALTHY (full 0-65535, 11 reversals in 417
samples) before the pedals were disassembled. It is now silent. The pedals have
since been apart — clutch pot removed, throttle/clutch connectors swapped — so a
disturbed brake connection is the leading hypothesis over a spontaneous failure.

### RESEATING THE BRAKE PLUG BROUGHT THE CHANNEL BACK
Bracketed capture, steer -> brake -> steer both sides of a plug reseat:

    63.4 - 67.3 s   STEER  32935 -> 54931          stream alive
    67.3 - 78.0 s   BRAKE pressed          NOTHING  <-- bracketed null
    78.0 - 81.2 s   STEER  54916 -> 32646          stream still alive
    -------- brake plug reseated --------
    96.3 - 100.1 s  BRAKE  0 -> 65535 -> 0        ** CHANNEL ALIVE **
   105.4 - 107.1 s  STEER  32692 -> 54764
   107.1 - 113.1 s  (no brake data in this window)
   113.1 - 115.8 s  STEER  55341 -> 32828

The pre-reseat null is now **bracketed on both sides** by steering that
reported — the base was demonstrably streaming before and after, so the brake
producing nothing was real and not a silent base. After the reseat the channel
delivered a full 0 -> 65535 -> 0 press. **The fault was the connection, not the
load cell and not the board.**

CAVEAT — NOT YET RELIABLE. Only ONE brake response was captured. The later
window at 107.1-113.1 s, bracketed by steering on both sides, contains no brake
data. Either the brake was simply not pressed then, or it dropped out again.
Unresolved; retest with several bracketed presses before calling this fixed.
A connector that works after reseating and then quits is the same signature the
throttle jack already has.

Signal quality on the one good press: held at 64054-65535 (97.7-100% of scale)
for ~3.5 s, released cleanly to 0 with one 1128 bounce. Sampling was thinned to
4 Hz, so the ramp shape was NOT captured — rerun at full resolution before
judging smoothness.

STILL OPEN (each needs a positive control in the same window):
 1. Several bracketed brake presses — is the reseat durable or intermittent?
 2. If it quits again: watch byte 20 while unplugging/replugging. If the value
    moves at all on unplug, the channel is alive and reading the pedal.
 3. Substitution: brake pedal -> throttle-IN jack (a known-good channel — the
    clutch pedal read cleanly through it). Registers on byte 18 => brake pedal
    and cable are fine, fault is the brake-IN channel.

### TOOLING BUGS FOUND BY THIS REPORT — all three faked a dead pedal
1. **THE LATCH DID NOT LATCH.** Axis `min`/`max`/`span`/`idle` were computed
   from `HIST`, a rolling `deque(maxlen=900)` kept for the sparkline. A pedal
   press therefore **aged out after 900 further reports** and the card went back
   to "no movement seen since reset (rests at 0)" — a channel that had
   demonstrably worked reading as dead. This is the headline bug: the dashboard's
   entire premise is that an intermittent fault stays on screen. Latched min/max
   now lives in `SEEN`, updated via `note_axis()` — one function, so a code path
   cannot fill the history and miss the latch. Only `/reset` clears it.
2. **A DEAD STREAM ADVERTISED ITS OLD RATE.** The "no reports" banner only fired
   when `count == 0`, so a stream that died *after* delivering reports kept
   showing `138.5 rep/s` while frozen for minutes. `snapshot()` now exposes
   `silent_for` / `streaming` / `frozen`; `rate` reads 0 when silent; the banner
   appends "NO REPORTS FOR Ns - THIS PAGE IS FROZEN, not idle" past `FROZEN`
   (20 s, deliberately far above `GAP` so normal rest does not cry wolf).
   Silence is a *clause*, never a branch — latched glitches stay the headline,
   since coming back later to read the page is by definition a quiet moment.
3. **DROPOUT WAS COUNTED AS A FAULT.** With a send-on-change base every rest
   longer than `GAP` fires one, so the glitch total was mostly the rig sitting
   still, burying real JUMP/RAIL catches. DROPOUT is still logged, no longer
   counted (`FAULT_KINDS`).

All three are covered by `tools/selftest-decode.py`, including a regression test
that presses the brake, pushes `HIST_LEN + 50` further reports through, and
asserts the press is still latched after it has left the sparkline.

## CURRENT MACHINE STATE (as of 2026-08-08)
The box is left in the **modified HID state for diagnostics**:
- `/etc/modprobe.d/hid-fanatec.conf` exists and contains `hidraw_pid=0`,
  so HIDRAW points at the REAL base rather than the driver's virtual PID
  device. `setup/revert-rawhid.sh` has NOT been run. Revert it when pedal
  diagnostics are finished.
- `games` group membership was granted by the installer but the **re-login
  has not happened**, so the `rumble`/`display` sysfs writes are still denied.
- As of the last check the base was enumerated (`0eb7:0e03`, device `.0109`)
  and the driver was bound, but it was sending **zero HID reports** — it had
  been streaming at ~9/s earlier the same day. Suspect base power / standby.

## *** FULL REPORT MAP — PROVEN, from the base's own HID descriptor ***
Parsed from `/sys/class/hidraw/hidrawN/device/report_descriptor` (133 bytes) by
`hid_layout.py`. This is not inferred from wiggling things; it is what the device
declares. It **supersedes all earlier guesses about byte offsets**, and it agrees
with every offset previously established by capture.

There is **no report ID**. The descriptor declares no Report ID item, so the
33-byte report is pure payload. The earlier note "33 bytes, id 0x08" was a
misreading: `0x08` is byte 0's hat nibble at rest (8 = centred).

    byte  0  bits 0-3   hat switch, 0-7 = N..NW, 8 = centred
    bits  4-111         108 buttons, LSB-first  (= bytes 0-13 exactly)
    bits  112-127       16 further declared button bits — NOT buttons on this
                        base: byte 14 = 0x00, byte 15 = constant 0x16
    byte 16  u16 LE  X       STEER
    byte 18  u16 LE  Z       THROTTLE   (throttle-IN jack)
    byte 20  u16 LE  Rz      BRAKE      (brake-IN jack, load cell)
    byte 22  u16 LE  Y       CLUTCH     (clutch-IN jack)   <-- was never decoded
    byte 24  s8      Rx      rim ministick X
    byte 25  s8      Ry      rim ministick Y
    byte 26  u8      Slider
    byte 27  s8      Dial
    bytes 28-32      vendor-defined (fw version / wheel id / pedal presence)

Button number N sits at bit N+3, i.e. byte `(N+3)//8`, bit `(N+3)%8`.
So **button 5 = GEAR UP = byte 1 bit 0** and **button 6 = GEAR DOWN = byte 1
bit 1** (functions per the `ftec_keymap` comments in `hid-ftec.c`).

Resting values from the archived capture (`data/raw-pedal-map.log`, first report
of the steering phase):
STEER 32783 (centred), THROTTLE 65535, BRAKE 65535, **CLUTCH 65535**,
ministick 0/0, Slider 255, Dial −4. The clutch channel resting at 65535 is the
same rest value as the other two pot channels — i.e. it looks like a normal
released pedal input, not a floating pin.

Verified offline by `tools/selftest-decode.py`, which replays the archived
reports through the decoders and asserts every offset, the button bit math, and
that byte 15's constant `0x16` produces no phantom button.

## *** "REMAINING PUZZLE" CLOSED — why wheel_id / fw_version / tuning are 0 ***
`ftecff_raw_event()` (hid-ftecff.c:1370) only parses wheel info when
`data[0] == 0x01 && size == FTEC_WHEEL_REPORT_SIZE (34)` — a **numbered** 34-byte
report. This base sends **33 bytes with no report ID**, so that branch never
runs. Nothing is wrong with the base and it is not refusing info requests; the
driver's parser simply does not match this device's report format. Same reason
the 64-byte tuning report never lands, so `ftec_tuning/*` (including `ACP`) all
read 0 — that is **"no data", not "mode 0"**.

Decoding the vendor block ourselves with the driver's own field offsets
(shifted by one for the missing ID byte) gives, from the archived capture:

    fw_version = LE16(byte 31, byte 32) = 0x02b5 = 693   <-- matches the base's
                                                            boot display and
                                                            bcdDevice 0693
    wheel_id   = byte 30 = 0x20   (not in hid-ftec.h's known-rim list)
    when byte 29 == 0xff and byte 30 == 0x04:
        byte 31 low nibble  = pedals connected
        byte 31 high nibble = handbrake connected

The dashboard now shows all of these live, so `wheel_id` no longer has to be
taken as "0x00 = fallback mapping".

## OPEN QUESTIONS AND THE TEST FOR EACH (not yet resolved)
1. **Does the clutch pedal reach byte 22?** The channel exists and rests at a
   sane value, but no capture has ever shown it move. Test: press the clutch
   and watch the CLUTCH card. *Unproven either way until then.*
2. **HYPOTHESIS — the rim's analog paddles may be hijacking the clutch axis.**
   Fanatec's `ACP` (Analogue Paddles) setting defaults to `1 CbP` = "clutch bite
   point, paddles work in parallel", i.e. on a rim with analog paddles the
   PADDLES drive the clutch axis, and upstream's own README warns mode `4 AnA`
   is "shared and interferes with analog ministick if present". If that is
   active, a clutch pedal plugged into a perfectly good jack would produce
   nothing — which is **exactly** the swap-bisect's null result.
   => The "clutch-IN channel is DEAD" conclusion below is **NOT safe**. Do not
   resolder the board yet.
   Test: squeeze the analog paddles and watch CLUTCH / SLIDER / DIAL in the
   dashboard's live-motion panel. If a paddle moves byte 22, the channel is
   alive and merely overridden. `ACP` cannot be read from sysfs (see above);
   read it from the base's own tuning display instead.
3. **Do the shift paddles report at all?** They are buttons 5 and 6. The one
   prior evdev capture saw buttons 1-4, 7-12, 22, 24, 26 and **not** 5 or 6 —
   but that capture was for pedals and never asked for paddle presses, so it is
   not evidence. Test: press them and watch the dashboard's button grid — cells
   5 and 6 go blue on the first press, and a `BTN-NEW` event is latched.
4. **Does the ministick really move STEER?** The descriptor says no: STEER is
   X at byte 16, the ministick is Rx/Ry at bytes 24-25 — separate fields.
   The likely explanation for what was observed is mechanical: pushing a thumb
   stick on a rim that does not self-centre and has FFB idle **physically
   rotates the wheel**. A contributing factor was the dashboard's auto-scaled
   sparkline, which drew ±30 LSB of resting ADC dither as a full-height wiggle.
   Test: **hold the rim firmly still** and move only the ministick, then read
   the live-motion panel — it names whichever field actually changed. STEER
   dithering by tens of LSB = independent, as declared. STEER swinging thousands
   of LSB with the rim held = something really is shared, and ACP mode 4 becomes
   the suspect.

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
3. `games` group added to the desktop user — needs re-login to take effect.
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

Raw report is 33 bytes. Channel -> byte offsets (LE u16):
    steering      [16:17]
    throttle-IN   [18:19]   (= driver axis Z)
    brake-IN      [20:21]   (= driver axis RZ)
  [AMENDED: there is no report id — the 0x08 read as one is byte 0's hat
   nibble at rest. The clutch-IN channel is [22:23] (axis Y); it was simply
   never decoded. See FULL REPORT MAP at the top.]

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
  ** "clutch-IN : DEAD" IS NO LONGER SAFE — see OPEN QUESTIONS #2 at the top.
     The rim's analog paddles may be driving the clutch axis (Fanatec ACP),
     which would make a good jack produce exactly this null result. Also, the
     capture tool of the time never decoded byte 22 at all. **
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

## REMAINING PUZZLE (SOLVED - see the section near the top)
wheel_id/fw_version/tuning all still 0 even though input reports flow.
=> ~~base answers normal input polling but not the driver's info-request
   reports~~ WRONG. The base sends the info fine, inside the ordinary 33-byte
   input report. The driver's parser requires a NUMBERED 34-byte report
   (`data[0]==0x01 && size==34`) and so never reads it. fw_version decodes to
   693 straight out of the raw bytes. Affects sysfs tuning + `range`, not FFB.

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
- ~~Base idles at ~9 reports/s with nothing moving => a dropout threshold of
  250 ms false-fires constantly. Use >= 2 s.~~ **SUPERSEDED — see the
  send-on-change section at the top.** The base transmits NOTHING at rest; the
  ~9/s was the faulty throttle dithering, not an idle heartbeat. No dropout
  threshold is safe, which is why DROPOUT is now logged but not counted as a
  fault.
- With pedals disassembled/unplugged: THR-IN rests at 0, BRK-IN rests at 65535,
  STEER centred 32768 with ~±30 LSB dither (stdev ~10).
  => stdev is the good health metric at rest: LSB dither ~10 vs bad throttle
  ~1380. Reversal COUNT is useless at rest (noise reverses ~50% of samples);
  normalise it per 100 samples and only trust it while the channel is moving.
- Device re-enumerates on replug: .0107 -> .0109. Always resolve via
  /dev/input/by-id/usb-Fanatec_*-hidraw, never a hardcoded hidrawN.

## Scripts
- `hid_layout.py`   — parses the base's HID report descriptor into the field map
                      every other tool uses. Falls back to hardcoded CSL Elite
                      offsets and says so loudly. Run it directly to dump the
                      layout as JSON.
- `pedal-web.py`    — LOCAL WEB DASHBOARD, http://localhost:8765 (also LAN, for
                      phone). Background thread reads hidraw at full rate and
                      LATCHES glitches (JUMP >3000 delta, RAIL entry, DROPOUT
                      >2 s) so an intermittent fault that lasts 20 ms still
                      shows. Decodes EVERYTHING the base sends: 4 analog axes
                      incl. CLUTCH, ministick/slider/dial, hat switch, all 108
                      button bits (grey = never seen / blue = seen / green =
                      down / red = stuck on, with rim function on hover), and
                      the vendor block (fw, wheel_id, pedal presence).
                      Latches first-ever button presses as BTN-NEW events, so
                      "press every button once" gives a definitive list of which
                      ones report. A LIVE MOTION panel names whatever changed in
                      the last 2 s — wiggle one control and see exactly which
                      field answers. Sparklines print their y-range so resting
                      ADC dither can no longer masquerade as movement, and every
                      16-bit channel also gets an absolute full-scale bar.
                      Stdlib only.
                      Also owns the SYSTEM CHECKS and the FFB TEST — see below.
- `sysstate.py`     — answers "is this machine in a state where the page can be
                      believed": driver bound, hidraw pointing at the REAL base
                      vs the driver's virtual PID device (decided by parsing the
                      descriptor, not by guessing), diagnostic HID mode still
                      active, udev rule, FF capability bitmask, and whether the
                      `games` re-login has happened. Pure filesystem reads, no
                      root, nothing executed — a failing check hands you the
                      command and stops there.
- `ffb.py`          — bounded FFB test (CONSTANT, 25%, 1.5 s each way) that
                      MEASURES itself by reading ABS_X off the same O_RDWR fd
                      that uploads the effect. The delta-20295 numbers below no
                      longer depend on a capture nobody kept.
- `selftest-decode.py` — replays the archived capture in `data/` through the
                      decoders and asserts the report map, the latch, the system
                      checks and the FFB state machine. Runs with the base
                      powered OFF and cannot move the wheel; use it after
                      touching any offset logic.
- `install-ffb.sh`  — root install (already run successfully)

~~`raw-live.py`, `raw-pedal-map.py`, `pedal-map.py`, `ffb-test.py`, `js.py`,
`verify-ffb.sh`, `attic/`~~ — DELETED. The dashboard is the author of all of it
now. `verify-ffb.sh` in particular mislabelled the FF bits; `sysstate.py` decodes
the sysfs `capabilities/ff` bitmask instead, which needs no device open at all.
The archived logs in `data/` are kept — they are the evidence behind everything
above, and the self-test reads its fixtures straight out of them.
