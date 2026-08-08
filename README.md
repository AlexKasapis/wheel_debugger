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
  - `DROPOUT` — no HID report for 2 s
  - `BTN-NEW` / `BTN` / `HAT` — button and hat activity
- Byte grid of the whole 33-byte report with per-byte min/max, each byte labelled
  with the field the descriptor says lives there, plus a warning if a byte moves
  that nothing claims

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

```
pedal-web.py        main dashboard (start here)
hid_layout.py       report-descriptor parser shared by every tool
tools/              one-shot terminal diagnostics
setup/              driver install and HID configuration (root)
docs/FINDINGS.md    the running diagnostic record — read this first
data/               captured logs kept as evidence for the findings
attic/              superseded scratch scripts, kept for reference
```

### tools/

| Script | Purpose |
| --- | --- |
| `raw-live.py` | Terminal live readout of every channel, plus buttons and hat |
| `raw-pedal-map.py` | Labelled **raw HID** capture — bypasses driver axis mapping, proves whether an input's data exists in the report at all. Nine guided phases including shift paddles, analog paddles and a hold-the-rim ministick test |
| `selftest-decode.py` | Replays the archived captures in `data/` through the decoders and asserts the whole report map. **Runs with the base powered off** — use it after touching any offset logic |
| `pedal-map.py` | Labelled evdev capture per pedal, with a throttle-held twitch check |
| `ffb-test.py` | Bounded force-feedback test (25% magnitude, 1.5 s each direction) |
| `js.py` | Joystick axis/button sampler |

Logs are written to `logs/` (gitignored).

### setup/

| Script | Purpose |
| --- | --- |
| `install-ffb.sh` | Installs the `hid-fanatecff` driver via DKMS. Clones upstream into `vendor/` on first run. Root. |
| `verify-ffb.sh` | Checks FF capabilities and effect slots |
| `enable-rawhid.sh` | Points HIDRAW at the **real** base instead of the driver's virtual PID device. Needed for raw byte inspection. |
| `revert-rawhid.sh` | Undoes the above |

## Force feedback

Solved and measured, not inferred. With a 25% constant force applied while
sampling `ABS_X`, the wheel physically rotated:

- **left** 12776 → 33071 (Δ 20295)
- **right** 13095 → 33633 (Δ 20538)

DKMS-installed so it survives kernel updates on a rolling release; claims the
device on fresh enumeration; survives replug and reboot. 12 effect types, 16
effect slots.

```sh
sudo bash setup/install-ffb.sh
bash setup/verify-ffb.sh
python3 tools/ffb-test.py
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
- **The base re-enumerates on replug** (`.0107` → `.0109`), so device paths move.
- **The base idles at ~9 reports/s** with nothing moving. Any dropout threshold
  tighter than ~2 s false-fires continuously.
- **`EVIOCRMFF` takes the effect id by value**, not a pointer. Passing a packed
  struct gives `Errno 22`.
- **Do not `pkill -f <script>`** from a shell whose own command line contains
  that pattern — it kills the shell.

## Current state

> **This machine is currently left in the diagnostic HID state** —
> `hidraw_pid=0` is active via `/etc/modprobe.d/hid-fanatec.conf` and
> `setup/revert-rawhid.sh` has not been run. Revert it when pedal work is done.

Force feedback is done. Pedal diagnosis is open, and the clutch half of it has
just been reopened:

- The **clutch channel was never dead in software** — it is `Y` at bytes 22-23
  and simply had no decoder. It rests at 65535, the same as the other two pot
  channels.
- **Before resoldering anything**, check whether the rim's analog paddles are
  driving the clutch axis. Fanatec's `ACP` setting makes them do exactly that by
  default, which would produce the same null result the connector-swap bisect
  saw from a perfectly good jack. `tools/raw-pedal-map.py` phase 6 tests it.
- The throttle remains suspect: compressed range and ~25× the noise of the brake
  while held still, with a jack that misbehaves when nudged.

Full reasoning, measurements and the revision history of the diagnosis are in
[docs/FINDINGS.md](docs/FINDINGS.md) — the open questions and the test for each
are listed near the top of that file.

> The base sends **nothing at all** while powered off or in standby, which looks
> identical to broken hardware. The dashboard says so explicitly when the device
> node opens but no reports arrive.
