# wheel_debugger

Diagnostic tooling for a Fanatec sim rig on Linux (openSUSE Tumbleweed).

Built to answer two questions:

1. **Can we get force feedback working?** — yes, solved. See [Force feedback](#force-feedback).
2. **What is wrong with the throttle and clutch?** — in progress. See
   [docs/FINDINGS.md](docs/FINDINGS.md).

## Hardware

| Part | Model |
| --- | --- |
| Base | Fanatec CSL Elite Wheel Base (USB `0eb7:0e03`, `bcdDevice 0693`) |
| Wheel | Fanatec F1 Carbon |
| Pedals | Fanatec CSL Elite, load-cell brake |
| Pedal wiring | Throttle / brake / clutch / handbrake each on a 6-pin RJ-type jack on a shared controller board |

The base's boot display reads `693` → `22` → `---`, which matches `bcdDevice 0693`.

## Quick start

The main tool is a local web dashboard. It reads raw HID at full rate in a
background thread and **latches** every glitch it sees, so an intermittent fault
lasting 20 ms still shows on screen minutes later — you do not have to be
watching when it happens.

```sh
python3 pedal-web.py
```

It prints a `localhost` URL and a LAN URL. Open the LAN one on a phone and keep
it beside the rig while your hands are busy.

Standard library only. No dependencies.

### What the dashboard shows

Everything the base sends, decoded from the device's own HID report descriptor
rather than from guessed byte offsets:

- **Four analog axes** — steer, throttle, brake and **clutch** — with live value,
  approximate voltage, an absolute full-scale bar and a sparkline
- **Rim analog** — ministick X/Y, slider, dial
- **Hat switch** as a 3×3 grid, latching every direction it has ever seen
- **All 108 button bits.** Grey = never seen, blue = seen since reset, green =
  down now, red = stuck on. Hover any cell for its rim function (from the
  driver's keymap) and its exact report byte/bit. A first-ever press is logged as
  a `BTN-NEW` event, so pressing every button once gives you a definitive list of
  which ones report and which do not.
- **Live motion panel** — names whatever actually changed in the last 2 s.
  Wiggle one control and only that control should appear. This is how you tell a
  genuine cross-channel link from an illusion.
- **Wheel info decoded from the raw report** — firmware version, `wheel_id`, and
  whether the pedals/handbrake are detected. The driver's own sysfs files read 0
  on this base; see [docs/FINDINGS.md](docs/FINDINGS.md) for why.
- **Rolling noise (stdev)** — the useful health metric. ADC dither at rest
  measures ~10; the faulty throttle measured ~1380. Unmistakable.
- **Event log**, latched until you press reset:
  - `JUMP` — sample-to-sample delta over 3000
  - `RAIL` — channel *entered* 0 or 65535 (resting at a rail does not log)
  - `DROPOUT` — no HID report for 2 s. Logged but **not counted as a fault**:
    the base is send-on-change, so sitting still fires one every time
  - `BTN-NEW` / `BTN` / `HAT` — button and hat activity
- Byte grid of the whole 33-byte report with per-byte min/max, each byte labelled
  with the field the descriptor says lives there, plus a warning if a byte moves
  that nothing claims

### System checks

A strip under the banner that answers "is this machine even in a state where the
page can be believed?" — driver bound, whether hidraw points at the **real base
or the driver's virtual PID device**, whether the box is still in diagnostic HID
mode, the udev rule, what force feedback the device advertises, and whether the
`games` group membership has actually taken effect yet. It is one green line when
everything is fine and opens itself when something is not.

A failing check names the exact command that fixes it. It never runs it: root
work stays with you. The commands are absolute paths and selectable with one tap,
because `navigator.clipboard` does nothing over plain http on the LAN — i.e. on
the phone that is actually next to the rig.

This is the check that matters most:

> The driver creates a *virtual* PID passthrough device alongside the real base.
> A hidraw node pointing at it opens fine and delivers zero reports forever,
> which on screen is indistinguishable from dead hardware. The page now says
> `READING THE WRONG DEVICE` instead of `not connected`, and tells you how to
> fix it.

### Force feedback test

Hold the button for a second and the wheel is pushed at 25% for 1.5 s each way —
and **measured** while it happens, by reading `ABS_X` back off the same file
descriptor that uploads the effect. You get `12776 → 33071  Δ 20295` per
direction, not a claim that it worked. ABORT stops it and erases the effect.

Measurement goes through evdev rather than the dashboard's own STEER channel on
purpose: the event node exists whenever the driver is bound, but the hidraw node
only points at the real base while `hidraw_pid=0` is set.

> The FFB routes physically move the wheel, and this server has no authentication
> — anything on your LAN can POST them. Start with `--no-ffb` to leave them out.

> Reversal *count* is deliberately not the headline metric: at rest a clean
> channel's LSB dither reverses on ~50% of samples, so the number looks awful
> for a healthy channel. It is shown normalised per 100 samples and is only
> meaningful while a channel is actually moving.

> Sparklines auto-scale, so they always print their y-range underneath. A channel
> resting with ±30 LSB of dither would otherwise draw the same dramatic wiggle as
> a real full sweep — which is exactly how "the steering responds to the
> ministick" got believed once already.

### The report map

No report ID; the 33 bytes are pure payload.

| Bytes | Field | Channel |
| --- | --- | --- |
| 0 (bits 0-3) | Hat switch | 0-7 = N..NW, 8 = centred |
| bits 4-111 (bytes 0-13) | 108 buttons, LSB-first | button N at bit N+3 |
| bytes 14-15 | declared as buttons but are not | byte 15 is a constant `0x16` |
| 16-17 | `X` u16 LE | **steer** |
| 18-19 | `Z` u16 LE | **throttle** (throttle-IN jack) |
| 20-21 | `Rz` u16 LE | **brake** (brake-IN jack, load cell) |
| 22-23 | `Y` u16 LE | **clutch** (clutch-IN jack) |
| 24 / 25 | `Rx` / `Ry` s8 | rim ministick X / Y |
| 26 / 27 | `Slider` u8 / `Dial` s8 | rim analog |
| 28-32 | vendor | fw version, wheel id, pedal presence |

Steering and the ministick are **separate fields** — byte 16 versus bytes 24-25.
If moving the ministick appears to move the steering, hold the rim firmly still
and try again: the rim does not self-centre, so a thumb on the stick rotates the
wheel for real.

## Repository layout

Everything diagnostic lives in the web app. What is left outside it is either a
library it imports, a root installer it can only tell you to run, or the test
that lets it be changed without the hardware present.

```
pedal-web.py        the dashboard - start here, this is the whole tool
hid_layout.py       report-descriptor parser + device-node resolution
sysstate.py         the system checks (root-free detection, never execution)
ffb.py              the force-feedback test and its measurement
setup/              root install and HID configuration
tools/              selftest-decode.py - offline test for all of the above
docs/FINDINGS.md    the running diagnostic record — read this first
data/               captured logs kept as evidence for the findings
```

### setup/

You do not need to remember these. The dashboard checks whether each one is
needed and hands you the command with the path already filled in.

| Script | Purpose |
| --- | --- |
| `install-ffb.sh` | Installs the `hid-fanatecff` driver via DKMS. Clones upstream into `vendor/` on first run. Root. |
| `enable-rawhid.sh` | Points HIDRAW at the **real** base instead of the driver's virtual PID device. Needed for raw byte inspection. |
| `revert-rawhid.sh` | Undoes the above |

### tools/

| Script | Purpose |
| --- | --- |
| `selftest-decode.py` | Replays the archived capture in `data/` through the decoders and asserts the whole report map, the latching behaviour, the system checks and the FFB state machine. **Runs with the base powered off and cannot move the wheel** — run it after touching any offset logic |

## Force feedback

Solved and measured, not inferred. With a 25% constant force applied while
sampling `ABS_X`, the wheel physically rotated:

- **left** 12776 → 33071 (Δ 20295)
- **right** 13095 → 33633 (Δ 20538)

DKMS-installed so it survives kernel updates on a rolling release; claims the
device on fresh enumeration; survives replug and reboot. 12 effect types, 16
effect slots.

Those numbers were originally captured by hand and then had to be taken on
trust. They are now reproduced on demand: hold the FFB button in the dashboard
and it measures the rotation itself.

```sh
sudo bash setup/install-ffb.sh   # only if the system checks say to
python3 pedal-web.py             # everything else is in the page
```

`install-ffb.sh` adds the invoking user to the `games` group for sysfs tuning
access — **that needs a re-login to take effect.** Run it with `sudo` rather
than from a root shell, so it can identify the desktop user; from a root shell
pass `REAL_USER=<name>` explicitly.

## Gotchas that cost real time

- **`hidraw` node identity.** The driver creates a *virtual* PID passthrough
  device alongside the real base. Reading the wrong one shows zero reports and
  looks exactly like dead hardware. Always resolve through
  `/dev/input/by-id/usb-Fanatec_*-hidraw`, never a hardcoded `hidrawN`, and run
  `setup/enable-rawhid.sh` (`hidraw_pid=0`) before raw inspection.
  **The dashboard now detects this case and says so** — it parses the
  descriptor and only calls it the real base if it declares the 33-byte report.
  Node resolution lives in one place, `hid_layout.find_nodes()`.
- **The base re-enumerates on replug** (`.0107` → `.0109`), so device paths move.
- **The base is send-on-change: at rest it transmits nothing at all.** This is
  the single most misleading thing about the rig. "I pressed it and the page did
  not move" has two indistinguishable causes — the control produces no data, or
  the base is not transmitting. **Every pedal test needs a positive control in
  the same window**: turn the wheel, press the pedal, turn the wheel again. If
  both steering legs report, the pedal window is bracketed and a null result is
  real. (An earlier note here claimed ~9 reports/s at idle; that was the faulty
  throttle dithering, not an idle heartbeat.)
- **`EVIOCRMFF` takes the effect id by value**, not a pointer. Passing a packed
  struct gives `Errno 22`.
- **Do not `pkill -f <script>`** from a shell whose own command line contains
  that pattern — it kills the shell.

## Current state

> **This file no longer tries to tell you what state the machine is in.** It used
> to carry a hand-written note saying `hidraw_pid=0` was active, which went stale
> the moment anyone ran `setup/revert-rawhid.sh`. Start the dashboard and read
> the system checks — they look at the machine instead of remembering it.

Force feedback is done. Pedal diagnosis is open, and the clutch half of it has
just been reopened:

- The **clutch channel was never dead in software** — it is `Y` at bytes 22-23
  and simply had no decoder. It rests at 65535, the same as the other two pot
  channels.
- **Before resoldering anything**, check whether the rim's analog paddles are
  driving the clutch axis. Fanatec's `ACP` setting makes them do exactly that by
  default, which would produce the same null result the connector-swap bisect
  saw from a perfectly good jack. Squeeze the analog paddles and watch whether
  the live motion panel names CLUTCH.
- The throttle remains suspect: compressed range and ~25× the noise of the brake
  while held still, with a jack that misbehaves when nudged.

Full reasoning, measurements and the revision history of the diagnosis are in
[docs/FINDINGS.md](docs/FINDINGS.md) — the open questions and the test for each
are listed near the top of that file.

> The base sends **nothing at all** while powered off or in standby, which looks
> identical to broken hardware. The dashboard says so explicitly when the device
> node opens but no reports arrive.
