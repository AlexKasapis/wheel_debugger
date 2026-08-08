# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Diagnostic tooling for a Fanatec CSL Elite sim rig on openSUSE Tumbleweed. Read
`README.md` for what the dashboard shows, and `docs/findings.md` for the state of
the investigation — this file covers only what those two do not.

## Commands

Standard library only, no venv, no build step, no test framework.

```sh
python3 pedal-web.py --no-ffb      # the dashboard on :8765 — use this form
python3 tools/selftest-decode.py   # 99 assertions, base can be powered off
python3 sysstate.py                # machine state as JSON
python3 hid_layout.py [node]       # parsed report layout as JSON
```

**Ask before running anything that actuates hardware or needs root:** `python3
ffb.py` (its `__main__` starts a real FFB run and physically moves the wheel),
`POST /ffb/start`, `sudo bash setup/*.sh` — and plain `python3 pedal-web.py`,
which binds `0.0.0.0` with no authentication, so anything on the LAN can POST
the routes that move the wheel. Start it with `--no-ffb` unprompted; ask before
starting it without. The rest of the list is filesystem reads and needs no
prompt.

### The self-test

`tools/selftest-decode.py` is the whole test suite. It has no argv handling and
no per-test selector — all nine sections run or none do. To test one thing,
edit `main()` temporarily; do not add a `--section` flag on the way past.

Two properties worth knowing before trusting a green run:

- `get_layout()` prefers the live descriptor and falls back to
  `hid_layout.fallback_layout()` when no readable Fanatec hidraw node exists.
  With the base off, the `no layout warnings` assertion is skipped — a green run
  is real but strictly weaker than one at the rig.
- Section [8] calls `sysstate.state(force=True)`, which reads this actual
  machine. The assertions are shape checks, so they pass either way, but the
  content differs by machine state.

Its fixtures are parsed out of `data/raw-pedal-map.log` at runtime, deliberately:
the archived capture *is* the fixture, so log and test cannot drift apart. Don't
paste hex into the test.

## Architecture

Data flows one way: device bytes → `Tracker` → JSON → the page.

```
pedal-web.py   reader thread + HTTP. Owns TRACKER and FFB. No decoding.
  tracker.py   Tracker: everything remembered. ingest() in, snapshot() out.
    decode.py  pure functions on report bytes. No state, no I/O.
  hid_layout.py   descriptor parsing + device-node resolution
  sysstate.py     the machine checks
  ffb.py          FfbTest: the bounded force-feedback run
web/           index.html + app.css + app.js, served from a fixed table
```

- **`tracker.py`** — `Tracker.ingest(report, now)` is the *only* way a report
  enters the system. The reader thread calls it and so does the self-test, which
  is the point: there is no second copy of the pipeline to drift. `note_idle()`
  is its counterpart for "the reader saw nothing this tick" and is what logs
  `DROPOUT`. `Tracker` must never import `ffb` — `pedal-web.page_data()` composes
  the two so the state machine stays ignorant of the actuator.
- **`hid_layout.py`** — parses the base's own HID report descriptor from
  `/sys/class/hidraw/*/device/report_descriptor` into the layout everything else
  consumes, and resolves device nodes. `find_nodes()` is the *only* place that
  knows node names; nodes move on replug (`.0107` → `.0109`), so never hardcode
  `hidrawN`/`eventN` anywhere else.
- **`sysstate.py`** — answers "is this machine in a state where the page can be
  believed?" Pure filesystem reads. A failing check returns the command that
  fixes it and never runs it. Keep it that way. Every check takes `nodes`,
  whether it uses them or not, so `CHECKS` needs no per-check argument list.
- **`ffb.py`** — `FfbTest` takes a `find_node` callable, so tests inject a lookup
  that resolves to nothing and `start()` cannot reach a device. Measures torque
  by reading `ABS_X` back off the same `O_RDWR` fd that uploaded the effect,
  through evdev rather than the dashboard's own STEER channel (the event node
  exists whenever the driver is bound; hidraw points at the real base only under
  `hidraw_pid=0`).
- **`pedal-web.py`** — `reader → pump → drain` is device I/O only. Routes:
  `GET /`, `/app.css`, `/app.js`, `/data`, `/system`; `POST /reset`,
  `/ffb/{start,abort}`. `/data` and `/system` match with `startswith` because the
  page appends a cache-busting query string — `==` would 404 them.

### The report map is duplicated in three places

Change the byte offsets and all three must move together, or the tool becomes
confidently wrong — the worst failure mode here:

1. `hid_layout.KNOWN_AXES` — one table that builds `fallback_layout()`, derives
   `KNOWN_OFFSETS`, and sanity-checks whatever the descriptor parse produces
2. `docs/report-map.md` — `build_layout()` literally emits *"docs/report-map.md
   offsets are stale"* when the parsed descriptor disagrees
3. Sections [1] and [2] of the self-test

`decode.decode_vendor()` still hardcodes the offsets *within* the vendor block
(29–32). Those mirror `ftecff_raw_event()` in the driver and are not derivable
from the descriptor, so they stay — but its length guard reads
`hid_layout.KNOWN_SIZE` rather than a literal 33.

### Invariants that are easy to break by accident

- **Latch, don't sample.** `Tracker.seen` (min/max ever) is separate storage from
  `Tracker.hist` (rolling window) so a press that ages out of the sparkline is
  still latched. The dashboard exists to catch faults nobody was watching for.
- **The base is send-on-change: at rest it transmits nothing.** So "no data"
  never distinguishes a dead control from a silent base, and every pedal test
  needs a positive control in the same window. This is why `DROPOUT` is logged
  but not counted as a fault, and why `snapshot()` reports `silent_for` instead
  of letting `rate` freeze at its last value.
- **Locks stay independent.** `Tracker`, `FfbTest` and `sysstate` each hold
  their own lock, so a `/data` poll never makes one wait on another. Don't cross
  them, and don't reintroduce a shared module-level lock.
- **The virtual PID trap.** The driver publishes a virtual PID device next to
  the real base. It opens fine, sends nothing forever, and looks exactly like
  dead hardware — `check_hidraw_target()` exists solely to tell them apart. See
  `docs/driver.md`.
- **Docs describe, they never remember machine state.** `docs/findings.md` says
  this outright, and two commits have already deleted "the box is currently in
  state X" notes for going stale. The code looks at the machine; the docs don't.

### Deliberate choices — fair game to change, but know why they exist

Nothing here is fenced off. Know the reason before you undo it, and say so in
the commit message if you do.

- The page is three files under `web/`, served from a fixed path table and read
  per request so editing CSS needs only a refresh. It used to be a 567-line
  Python string.
- Stdlib only. The README leads with it.
- `hid_layout.layout_for()` and `sysstate._collect()` catch bare `Exception` on
  purpose — a diagnostic that dies on a surprise is worse than one that degrades
  and says so.

## Working rules

- **Comments are short and carry the non-obvious *why*.** `EVIOCRMFF` takes the
  id by value; `struct ff_effect` is 48 bytes; `seen` is separate from `hist`.
  Narrative, retrospectives and superseded reasoning belong in `git log`, not in
  the source — a whole commit went to stripping exactly that.
- **Keep docs in sync in the same commit as the code.** All of them, including
  `docs/findings.md`.
- **Run the self-test after touching any decoder, offset or check.** It is the
  only way to change byte-level logic without sitting at the rig.
- **Clean code, actively.** Refactoring for clarity is welcome anywhere in this
  repo, not just in the file you came for.

## Commits

Committing after a task is standing authorization — don't ask. Pushing is not.

```sh
git status --short                    # see what was already dirty
git add pedal-web.py hid_layout.py    # explicit paths, only what you edited
git commit
```

Never `git add -A`, `-a`, or `.`; never `git push`; never stage a file you did
not touch. The working tree is frequently dirty with the user's own in-progress
edits, and those are not yours to commit.

Commit messages here are a subject line plus a body explaining *why*, in the
style of `git log`. End with:

```
Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
```
