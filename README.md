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

- Live value and approximate voltage per channel, with sparklines
- **Rolling noise (stdev)** — the useful health metric. ADC dither at rest
  measures ~10; the faulty throttle measured ~1380. Unmistakable.
- **Event log**, latched until you press reset:
  - `JUMP` — sample-to-sample delta over 3000
  - `RAIL` — channel *entered* 0 or 65535 (resting at a rail does not log)
  - `DROPOUT` — no HID report for 2 s
- Byte grid of the whole 33-byte report with per-byte min/max
- **Auto-detection of unknown 16-bit channels that start moving** — this is how
  a revived clutch channel will announce itself

> Reversal *count* is deliberately not the headline metric: at rest a clean
> channel's LSB dither reverses on ~50% of samples, so the number looks awful
> for a healthy channel. It is shown normalised per 100 samples and is only
> meaningful while a channel is actually moving.

## Repository layout

```
pedal-web.py        main dashboard (start here)
tools/              one-shot terminal diagnostics
setup/              driver install and HID configuration (root)
docs/FINDINGS.md    the running diagnostic record — read this first
data/               captured logs kept as evidence for the findings
attic/              superseded scratch scripts, kept for reference
```

### tools/

| Script | Purpose |
| --- | --- |
| `raw-live.py` | Terminal live readout of the known channels |
| `raw-pedal-map.py` | Labelled **raw HID** capture — bypasses driver axis mapping, proves whether a pedal's data exists in the report at all |
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

`install-ffb.sh` adds the user to the `games` group for sysfs tuning access —
**that needs a re-login to take effect.**

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

Force feedback is done. Pedal diagnosis is open; the leading theory is aged
solder joints or contacts on the pedal jacks — one intermittent (throttle), one
open on the signal pin (clutch). Full reasoning, measurements and the revision
history of the diagnosis are in [docs/FINDINGS.md](docs/FINDINGS.md).
