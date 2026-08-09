# wheel_debugger

Diagnostic tooling for a Fanatec sim rig on Linux (openSUSE Tumbleweed). Built to
answer two questions: can we get force feedback working (yes, measured — see
[docs/findings.md](docs/findings.md)), and what is wrong with the throttle and
clutch (in progress).

## Quick start

```sh
python3 pedal-web.py
```

Standard library only, no dependencies. It prints a `localhost` URL and a LAN URL
— open the LAN one on a phone and keep it beside the rig while your hands are
busy.

The dashboard reads raw HID at full rate in a background thread and **latches**
every glitch it sees, so an intermittent fault lasting 20 ms still shows on screen
minutes later. You do not have to be watching when it happens.

> The FFB routes physically move the wheel and this server has no authentication
> — anything on your LAN can POST them. Start with `--no-ffb` to leave them out.

## What the dashboard shows

Everything the base sends, decoded from the device's own HID report descriptor
rather than from guessed byte offsets:

- **Four analog axes** — steer, throttle, brake, clutch — with live value,
  approximate voltage, an absolute full-scale bar, a sparkline, and a **range
  coverage strip**: every bucket of the channel's range it has ever sat in,
  latched since reset. Travel says how far a pedal got; coverage says what it
  skipped on the way, which is what a dead spot in a pot looks like. Steering
  anchors its bar at the centre rather than at zero.
- **Rim analog** — the ministick as an **XY pad** (current position, its trail,
  the box it has swept and the centre crosshair, so corner reach and
  centre-return are one glance rather than two sparklines), plus slider and
  dial — and the **hat switch** as a 3×3 grid, latching every direction it has
  ever seen
- **All 108 button bits.** Grey = never seen, blue = seen since reset, green =
  down now, red = stuck on. Hover for the rim function and the exact report
  byte/bit. A first-ever press logs `BTN-NEW`, so pressing every button once gives
  a definitive list of which ones report.
- **Live motion panel** — names whatever actually changed in the last 2 s. Wiggle
  one control and only that control should appear. This is how you tell a genuine
  cross-channel link from an illusion.
- **Wheel info decoded from the raw report** — firmware version, `wheel_id`,
  pedal/handbrake presence. The driver's own sysfs files read 0 on this base; see
  [docs/driver.md](docs/driver.md).
- **Rolling noise (stdev)** — the health metric that works at rest. ADC dither
  measures ~10; the faulty throttle measured ~1380.
- **Event log**, latched until reset: `JUMP` (delta over 3000), `RAIL` (channel
  *entered* 0 or 65535), `DROPOUT` (no report for 2 s — logged but not counted as
  a fault, since the base is send-on-change), `BTN-NEW` / `BTN` / `HAT`.
- **Byte grid** of the whole 33-byte report with per-byte min/max, each byte
  labelled with the field the descriptor claims, plus a warning if a byte moves
  that nothing claims.
- **System checks** — a strip under the banner answering "is this machine even in
  a state where the page can be believed?" One green line when everything is
  fine; it opens itself when something is not. A failing check names the exact
  command that fixes it and never runs it — root work stays with you.
- **Force feedback test** — hold the button for a second and the wheel is pushed
  at 25% for 1.5 s each way, **measured** while it happens by reading `ABS_X` back
  off the same fd that uploads the effect. ABORT stops it and erases the effect.

**Graph scale is a toggle**, remembered per browser. `FULL` (the default) draws
every graph over the channel's own declared range, so channels are comparable and
a resting one is a flat line where it actually rests. `FIT` auto-scales to what
the channel is doing, which is the only way ±30 LSB of dither is visible at all —
and the reason a graph always prints the range it just drew, in either mode. The
coverage strip is always full-range: a zoomed one could not show a gap.

## Repository layout

```
pedal-web.py        the process - device thread + HTTP server. start here
tracker.py          the latched state: ingest() a report, snapshot() the page
decode.py           pure report decoders - no state, no I/O
hid_layout.py       report-descriptor parser + device-node resolution
sysstate.py         the system checks (detection only, never execution)
ffb.py              the force-feedback test and its measurement
evdev_axes.py       HID->ABS mapping and evdev reads, shared by the captures
web/                the page itself - index.html, app.css, app.js
setup/              root install and HID configuration
tools/              selftest-decode.py - offline test for all of the above
                    live-check.py - watch every channel, no prompts
                    bracket-capture.py - scripted capture, at the rig
data/               labelled pedal captures - evidence, and the test's fixtures
                    (raw-pedal-map.log is raw HID, pedal-map.log is evdev)
docs/               hardware, report map, driver notes, diagnostic record
```

Everything diagnostic lives in the web app. What is left outside it is either a
library it imports, a root installer it can only tell you to run, or the test that
lets it be changed without the hardware present.

Reports reach the dashboard exactly one way — `Tracker.ingest()`. The reader
thread does device I/O and nothing else, so every latch, fault and decode is
reachable without a base attached.

`tools/selftest-decode.py` replays the archived capture in `data/` through
`Tracker.ingest()` — the same call the reader thread makes — and asserts the
whole report map, the latching behaviour, the system checks and the FFB state
machine. 117 checks. It runs with the base powered off and cannot move the wheel;
run it after touching any offset logic.

`tools/live-check.py` is that pipeline in a terminal, for when the page is not
where your eyes are. It prints where every channel is resting, names each one as
it first moves, and ends on a latched summary — no phases and nothing to keep up
with, because latching makes the order you press things in irrelevant.

`tools/bracket-capture.py` is the same pipeline pointed at a scripted sequence:
it prompts through timed phases and brackets every pedal phase with a steering
one, for the times the sequence itself matters. Both print the raw HID view next
to the evdev view of the same window; when those two disagree, the fault is
between the driver and the game rather than in the base.

You do not need to remember the `setup/` scripts; the dashboard checks whether
each is needed and hands you the command with the path filled in.

| Script | Purpose |
| --- | --- |
| `install-ffb.sh` | Installs the `hid-fanatecff` driver via DKMS. Clones upstream into `vendor/` on first run. Root. |
| `enable-rawhid.sh` | Points HIDRAW at the **real** base instead of the driver's virtual PID device. Needed for raw byte inspection, and it costs games nothing — see [docs/driver.md](docs/driver.md#the-virtual-pid-device--the-expensive-trap). |
| `revert-rawhid.sh` | Undoes the above |

```sh
sudo bash setup/install-ffb.sh   # only if the system checks say to
```

## Docs

- [docs/findings.md](docs/findings.md) — the diagnostic record: what is proven,
  what is open, and the test for each open question. **Read this first.**
- [docs/hardware.md](docs/hardware.md) — the rig, its device nodes, the pedal jacks
- [docs/report-map.md](docs/report-map.md) — the 33-byte report, byte by byte
- [docs/driver.md](docs/driver.md) — `hid-fanatec`, DKMS, the virtual PID device
  trap, and why the driver's sysfs info files read 0
