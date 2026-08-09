# Diagnostic record

This file does not track what state the machine is in — start the dashboard and
read the system checks, which look at the machine instead of remembering it.
Superseded reasoning is not kept here either; `git log` has it.

## The rule every pedal test must follow

**The base is send-on-change: at rest it transmits nothing at all.** So "I
pressed it and the page did not move" has two indistinguishable causes — the
control produces no data, or the base is not transmitting. Every test needs a
**positive control in the same window**: turn the wheel, press the pedal, turn the
wheel again. If both steering legs report, the null result in between is real.

This is also why `DROPOUT` is logged but not counted as a fault, and why the
dashboard reports `silent_for` rather than letting `rate` freeze at whatever it
last saw.

## Health metrics

- **Rolling stdev is the metric that works at rest.** LSB dither measures ~10;
  the faulty throttle measured ~1380.
- **Reversal count is useless at rest** — clean LSB dither reverses on ~50% of
  samples. Shown normalised per 100 samples, and only meaningful while a channel
  is actually moving. It is what separated the healthy brake (11 in 417 samples)
  from the faulty throttle (248 in 401 while held motionless).

## Force feedback — done, measured

Constant force at 25% while sampling `ABS_X`:

- **left** 12776 → 33071 (Δ 20295)
- **right** 13095 → 33633 (Δ 20538)

Objective torque, not an `EV_FF` capability bit. The dashboard's FFB button
reproduces this on demand rather than relying on a capture nobody kept.

### There are two FFB paths and the button only proves one

That measurement uses `EVIOCSFF` on the event node. Proton games do not: they
write HID PID reports to the HIDRAW device, which is the driver's *injected* PID
collection and not the base's own descriptor. The paths are independent, so a
wheel that answers the dashboard's button and stays dead in a game is not a
contradiction and says nothing about the hardware.

`hidraw_pid=0` — what `setup/enable-rawhid.sh` sets, and what raw byte
inspection requires — removes the PID collection while leaving `EV_FF` intact.
That produces exactly this split: axes, buttons and pedals all correct in-game,
no force at the rim, dashboard button fine. See
[driver.md](driver.md#that-device-is-what-games-get-force-feedback-through).
The `HID mode` system check reports which mode the box is in and hands over the
revert command; ask it rather than guessing.

Two things make a revert look like it failed:

- `revert-rawhid.sh` rebinds through sysfs, which emits `bind` — the udev rule
  only matches `add|change` (above), so the new node can come up root-owned and
  a game cannot open it. **Replug the base or reboot after reverting**, then
  check the mode with `ls -l` on the hidraw node.
- The Wine prefix caches the device as it first enumerated it, PID-less. Clear
  it with `protontricks -c "wine reg delete 'HKLM\System\CurrentControlSet\Enum\HID' /f" <appid>`.

`PROTON_LOG=1 WINEDEBUG=+hid,+input,+dinput %command%` as a launch option
settles which path a game actually took: the log names `found 3 TLCs` when
Proton has picked up the PID-extended descriptor.

## Steering — healthy

Full travel (789 → 64745, 97.6%), ~±30 LSB dither at rest (stdev ~10). Wheel does
not self-centre and has firm end stops; range init landed fine.

## Brake — the base reads the channel; the fault is past the jack

Measured healthy before the pedals were disassembled: full 0 → 65535 travel, 11
reversals in 417 samples. Then went silent. A bracketed capture proved the
silence was real rather than a quiet base — and its unplug leg proved where the
fault is *not*:

    STEER   22027 -> 36808   22.6%     stream alive, 549 Hz
    BRAKE   pressed hard, 10 s         0 -> 0, nothing   <-- bracketed null
    STEER   24169 -> 39224   23.0%     stream still alive
    -------- brake plug pulled --------
    BRAKE       0 -> 65535  100.0%     ** the base IS reading this channel **
    -------- brake plug back in -------
    BRAKE   18389 -> 0        JUMP     settles at the pressed end again

`ABS_RZ` tracked the raw bytes through the unplug transition, so **the brake-IN
jack, the ADC, the report, `hid-fanatec` and evdev all work.** A game reading
this axis is being told the truth, and the dashboard showing a flat brake was
never a display bug.

Connected and untouched, the channel sits at 0. Every pedal channel rests at
65535 and falls under press ([report-map.md](report-map.md)), so 0 is the
*fully-pressed* end: something past the jack holds the signal at the bottom of
the scale. That is neither a released pedal nor an open circuit — open reads
65535, as the unplug leg shows.

This supersedes the earlier "intermittent connection" reading. Reseating fixed it
once and no longer does, and the failure is now a steady pin rather than a
dropout.

### The swap that located it

Brake and throttle pedals traded jacks. throttle-IN is known good, and the
throttle pedal's rest chatter is unmistakable wherever it lands:

    throttle pedal in brake-IN    64505 .. 64891, sd 48   its own chatter, intact
    brake pedal in throttle-IN    65535, later 32023      no response to a hard press
    brake-IN with nothing in it   65535                   the unplug leg above

**brake-IN carries a real signal** — another pedal's noise came through it whole
— so the jack, its solder and the base's input are all cleared. The brake pedal
answered in *neither* jack. The fault is the pedal or its cable.

Its idle value differs by jack: 0 in brake-IN, 65535 in throttle-IN. A pedal
shorting its own signal line would read 0 in both, so that is unexplained and is
what a meter at the pedal's plug should settle.

`brF` cannot cause this. It scales force to output; no setting makes an untouched
pedal report past full press. The tuning menu is not on the path to this fault.

## Throttle — faulty, two live hypotheses

Symptoms, consistently: rest pinned at 65535, only 3-16% of range (varies between
runs, i.e. intermittent), and while **held motionless** it swings ~15% of full
range with stdev 1378 and 248 reversals in 401 samples — ~25× noisier than the
brake.

Both explanations remain open, and they produce the same signature:

1. **Worn / oxidised pot wiper.** A connector swap put the throttle pedal in the
   known-good clutch-IN channel and it still produced garbage, so the fault
   follows the pedal. The brake swap said it again from the other side: the rest
   chatter left byte 18 with the pedal and reappeared on byte 20, sd 48 in a
   window where byte 18 sat perfectly flat. The **range coverage strip** under the THROTTLE graph is
   the direct test: sweep the pedal slowly end to end and a healthy pot fills
   every bucket, while a dead spot on the track leaves a gap at the same place
   every sweep. Coverage is latched, so the sweep does not have to be one motion.
2. **Mechanically flaky jack.** Nudging the plug while seated makes readings go
   bad or drop to zero, and the collapse is visible **at the pot's own pins** —
   the interruption is in the jack/plug contact, not downstream on the board. Not
   reproducible on demand, only avoidable by forcing a good seating. This is why
   the dashboard latches glitches instead of using timed captures.

Leading theory overall: aged solder joints / contacts on the pedal jacks.

## Clutch — channel exists, never seen to move

The clutch channel was **never dead in software**: it is `Y` at bytes 22-23 and
simply had no decoder until the descriptor was parsed. It rests at 65535, the
same as the other two pot channels, which looks like a normal released pedal
input and not a floating pin.

The clutch pot itself measures clean (stable 0 → 3.3 V end to end, in both the
clutch and throttle jacks). **That does not clear the clutch jack**: measuring at
the pot's own wiper is upstream of the return wire, so the wiper voltage reads
correct even if the signal line back to the board is fully open. Clutch signal
return remains a suspect.

The clutch *pedal* is fine — it read cleanly (28.8% span, 7 reversals in 464
samples) through the throttle-IN channel.

## Open questions, and the test for each

Each needs a positive control in the same window.

1. **What inside the brake pedal is dead?** The swap put the fault on the pedal
   side of the plug: brake-IN carries another pedal's signal intact, and the
   brake pedal answers in no jack. It is a load cell, so the suspects are the
   cell, its amplifier and the cable. Measure at the pedal's own plug — is the
   3.3 V sensor supply reaching it, and does the signal pin move under force? An
   unpowered amplifier sits at 0 V, which is what brake-IN sees.
2. **What does an unplugged *pot* channel read?** An old note claims THROTTLE
   reads 0 unplugged while BRAKE reads 65535 — an asymmetry nothing in `data/`
   shows, and it decides how much a resting 65535 is worth. Unplug the clutch and
   watch byte 22. Dropping to 0 makes the asymmetry real *and* shows the clutch
   jack reading a connected pot, which would clear repair target 2. Staying at
   65535 means that channel tells you nothing either way.
3. **Does the clutch pedal reach byte 22?** No. A bracketed capture pressed it for
   6 s with the stream alive throughout and byte 22 never left 65535. That does
   not name a cause: 65535 is also what a released pot reads, so a healthy jack
   whose axis is driven elsewhere (see 4) fits the same data as a dead one.
   Question 2 separates them.
4. **Are the rim's analog paddles hijacking the clutch axis?** Fanatec's `ACP`
   defaults to `1 CbP` — on a rim with analog paddles, the *paddles* drive the
   clutch axis — and upstream warns mode `4 AnA` "is shared and interferes with
   analog ministick if present". If active, a clutch pedal in a perfectly good
   jack would produce exactly the null result seen. **So "the clutch-IN channel
   is dead" is not a safe conclusion — do not resolder the board yet.** Squeeze
   the paddles and watch CLUTCH / SLIDER / DIAL in the live-motion panel. `ACP`
   cannot be read from sysfs (see [driver.md](driver.md)); read it off the base's
   own tuning display.
5. **Do the shift paddles report at all?** They are buttons 5 and 6. The one prior
   evdev capture saw buttons 1-4, 7-12, 22, 24, 26 and not 5 or 6 — but it never
   asked for paddle presses, so that is not evidence. Press them and watch the
   button grid; cells 5 and 6 go blue on first press and latch a `BTN-NEW`.
6. **Does the ministick really move STEER?** The descriptor says no (separate
   fields). Hold the rim firmly still, move only the ministick, and read the
   live-motion panel. STEER dithering by tens of LSB = independent, as declared;
   swinging thousands with the rim held = something really is shared, and `ACP`
   mode 4 becomes the suspect. The ministick's own XY pad answers the separate
   question of whether the stick itself is healthy: roll it round the rim and the
   trail should reach all four corners of the swept box and come back to the
   crosshair when released.

## Repair targets

1. Throttle: clean/reseat/replace the pot; check the wire and solder at its plug.
2. Clutch input channel on the controller board: check its connector solder
   joints, and whether the sensor supply is present on that header while powered
   (compare against the working throttle header).
3. Brake pedal: the load cell, its amplifier, or the cable back to its plug. The
   brake-IN jack and the base's input behind it are cleared — see Brake above.

## Gotchas that cost real time

- `EVIOCRMFF` takes the effect id **by value**, not as a pointer. Passing a packed
  struct gives `Errno 22`.
- Do not `pkill -f <script>` from a shell whose own command line contains that
  pattern — it kills the shell.
- Pre-driver "resting axis values" captured through the legacy joystick API were
  synthetic `JS_EVENT_INIT` events (initial state on open), not live hardware
  reads. That baseline is void, as is any axis map from before the driver was
  bound — it remaps axes and zeroes deadzone/fuzz.
