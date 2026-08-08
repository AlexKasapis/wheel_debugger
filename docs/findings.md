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

## Steering — healthy

Full travel (789 → 64745, 97.6%), ~±30 LSB dither at rest (stdev ~10). Wheel does
not self-centre and has firm end stops; range init landed fine.

## Brake — healthy load cell, intermittent connection

Measured healthy before the pedals were disassembled: full 0 → 65535 travel, 11
reversals in 417 samples. Then went silent — and a bracketed capture proved the
silence was real, not a quiet base:

    63.4 - 67.3 s   STEER  32935 -> 54931      stream alive
    67.3 - 78.0 s   BRAKE pressed      NOTHING <-- bracketed null
    78.0 - 81.2 s   STEER  54916 -> 32646      stream still alive
    -------- brake plug reseated --------
    96.3 - 100.1 s  BRAKE  0 -> 65535 -> 0     ** CHANNEL ALIVE **

No other byte in the report moved during the pre-reseat presses either, so the
data was absent from the whole report rather than mislabelled. **The fault was
the connection, not the load cell and not the board.**

Not yet reliable: only one good press was captured, and a later window bracketed
by steering on both sides contained no brake data. A connector that works after
reseating and then quits is the same signature the throttle jack has.

## Throttle — faulty, two live hypotheses

Symptoms, consistently: rest pinned at 65535, only 3-16% of range (varies between
runs, i.e. intermittent), and while **held motionless** it swings ~15% of full
range with stdev 1378 and 248 reversals in 401 samples — ~25× noisier than the
brake.

Both explanations remain open, and they produce the same signature:

1. **Worn / oxidised pot wiper.** A connector swap put the throttle pedal in the
   known-good clutch-IN channel and it still produced garbage, so the fault
   follows the pedal.
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

1. **Is the brake reseat durable?** Several bracketed presses. If it quits again,
   watch byte 20 while unplugging/replugging — if the value moves at all on
   unplug, the channel is alive and reading the pedal.
2. **Brake substitution.** Brake pedal → throttle-IN jack (known good). If it
   registers on byte 18, the pedal and cable are fine and the fault is the
   brake-IN channel.
3. **Does the clutch pedal reach byte 22?** Press it and watch the CLUTCH card.
   Unproven either way until then.
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
   mode 4 becomes the suspect.

## Repair targets

1. Throttle: clean/reseat/replace the pot; check the wire and solder at its plug.
2. Clutch input channel on the controller board: check its connector solder
   joints, and whether the sensor supply is present on that header while powered
   (compare against the working throttle header).

## Gotchas that cost real time

- `EVIOCRMFF` takes the effect id **by value**, not as a pointer. Passing a packed
  struct gives `Errno 22`.
- Do not `pkill -f <script>` from a shell whose own command line contains that
  pattern — it kills the shell.
- Pre-driver "resting axis values" captured through the legacy joystick API were
  synthetic `JS_EVENT_INIT` events (initial state on open), not live hardware
  reads. That baseline is void, as is any axis map from before the driver was
  bound — it remaps axes and zeroes deadzone/fuzz.
