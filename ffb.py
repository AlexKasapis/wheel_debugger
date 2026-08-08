#!/usr/bin/env python3
"""Bounded force-feedback test that measures its own result.

Uploads a CONSTANT effect at a gentle magnitude, plays it one way then the other,
then erases it. Nothing runs open-ended. The fd is O_RDWR, so the same descriptor
that uploads the effect reads ABS_X back while the motor pushes - torque is
measured every run, not inferred.

Measurement goes through evdev rather than the dashboard's own STEER channel
because the event node exists whenever the driver is bound, while hidraw points at
the real base only while hidraw_pid=0 is set.
"""
import fcntl
import os
import select
import struct
import threading
import time

import hid_layout

MAGNITUDE = int(0.25 * 32767)      # 25% of full scale
DURATION_MS = 1500
SETTLE_S = 0.3                     # keep reading past the effect's own end
PAUSE_S = 0.5
MOVED_MIN = 1000                   # ABS_X span that counts as the wheel moving

EV_ABS = 0x03
EV_FF = 0x15
ABS_X = 0x00
FF_CONSTANT = 0x52
DIR_LEFT = 0x4000
DIR_RIGHT = 0xC000

# struct ff_effect is 48 bytes on x86_64: the union is 8-byte aligned, so it
# starts at offset 16. struct input_event is 24: timeval(16) + type + code + value.
EFFECT_SIZE = 48
EVIOCSFF = 0x40000000 | (EFFECT_SIZE << 16) | (ord('E') << 8) | 0x80
EVIOCRMFF = 0x40000000 | (4 << 16) | (ord('E') << 8) | 0x81
EVENT_SIZE = 24

PHASES = ('idle', 'arming', 'left', 'pause', 'right', 'erasing',
          'done', 'failed', 'aborted')


def default_node():
    """The evdev node force feedback is driven through, or None."""
    return hid_layout.node_path('event')


class FfbTest:
    """One bounded push-left / push-right run, measured. Single-flight."""

    def __init__(self, find_node=default_node):
        self._find_node = find_node
        self._lock = threading.Lock()
        self._abort = threading.Event()
        self.phase = 'idle'
        self.running = False
        self.error = None
        self.started = None
        self.finished = None
        self.device = None
        self.result = {}            # 'left'/'right' -> measurement dict

    # -- read side -----------------------------------------------------------

    def status(self):
        """Snapshot for the dashboard. No I/O, so safe at the /data poll rate."""
        with self._lock:
            elapsed = 0.0
            if self.started is not None:
                elapsed = round((self.finished or time.time()) - self.started, 1)
            return {
                'phase': self.phase,
                'running': self.running,
                'error': self.error,
                'started': self.started,
                'finished': self.finished,
                'device': self.device,
                'magnitude_pct': round(100.0 * MAGNITUDE / 32767),
                'duration_ms': DURATION_MS,
                'result': {k: dict(v) for k, v in self.result.items()},
                'elapsed': elapsed,
            }

    def _set(self, **kw):
        with self._lock:
            for key, val in kw.items():
                setattr(self, key, val)

    # -- control -------------------------------------------------------------

    def start(self):
        """Kick off one test. Returns (ok, message)."""
        # Checked before the device is looked at: a double-tap must never stack
        # two uploads onto the same effect slot.
        with self._lock:
            if self.running:
                return False, 'a test is already running'

        path = self._find_node()
        if not path:
            return False, 'no Fanatec event node - is the base plugged in?'
        if not os.access(path, os.W_OK):
            return False, (f'{path} is not writable by this user - force feedback '
                           f'needs O_RDWR (see the system checks)')

        with self._lock:
            if self.running:                 # lost a race between the checks
                return False, 'a test is already running'
            self._abort.clear()
            self.phase = 'arming'
            self.running = True
            self.error = None
            self.started = time.time()
            self.finished = None
            self.device = path
            self.result = {}

        threading.Thread(target=self._run, args=(path,), daemon=True).start()
        return True, 'started'

    def abort(self):
        """Stop the current test as soon as the worker notices. Idempotent."""
        with self._lock:
            if not self.running:
                return False, 'nothing running'
        self._abort.set()
        return True, 'aborting'

    # -- the worker ----------------------------------------------------------

    def _run(self, path):
        fd = None
        eid = -1
        try:
            fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
            self._set(phase='arming')
            _drain(fd)
            eid = _upload(fd, DIR_LEFT, MAGNITUDE, DURATION_MS)

            for label, direction in (('left', DIR_LEFT), ('right', DIR_RIGHT)):
                if self._abort.is_set():
                    break
                eid = _upload(fd, direction, MAGNITUDE, DURATION_MS, eid)
                self._set(phase=label)
                measured = self._push(fd, eid, DURATION_MS / 1000 + SETTLE_S)
                with self._lock:
                    self.result[label] = measured
                if self._abort.is_set():
                    break
                self._set(phase='pause')
                time.sleep(PAUSE_S)

            self._set(phase='erasing')
            if eid >= 0:
                fcntl.ioctl(fd, EVIOCRMFF, eid)  # takes the id BY VALUE, not a pointer
            self._set(phase='aborted' if self._abort.is_set() else 'done')
        except OSError as exc:
            # Best effort - never leave an effect loaded on the device.
            try:
                if fd is not None and eid >= 0:
                    _play(fd, eid, 0)
                    fcntl.ioctl(fd, EVIOCRMFF, eid)
            except OSError:
                pass
            self._set(phase='failed',
                      error=f'{exc.strerror or exc} (errno {exc.errno})')
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._set(running=False, finished=time.time())

    def _push(self, fd, eid, seconds):
        """Play the loaded effect for `seconds`, sampling ABS_X throughout."""
        vals = []
        _play(fd, eid, 1)
        deadline = time.time() + seconds
        try:
            while time.time() < deadline and not self._abort.is_set():
                ready, _, _ = select.select([fd], [], [], 0.05)
                if not ready:
                    continue
                try:
                    blob = os.read(fd, EVENT_SIZE * 64)
                except BlockingIOError:
                    continue
                vals += _abs_x_values(blob)
        finally:
            _play(fd, eid, 0)
        return _measure(vals)


def _abs_x_values(blob):
    """The ABS_X readings in a batch of input_event structs."""
    out = []
    for off in range(0, len(blob) - EVENT_SIZE + 1, EVENT_SIZE):
        _, _, typ, code, value = struct.unpack_from('<qqHHi', blob, off)
        if typ == EV_ABS and code == ABS_X:
            out.append(value)
    return out


def _measure(vals):
    """Summarise one push. No samples means we could not measure, not zero torque."""
    if not vals:
        return {'samples': 0, 'first': None, 'last': None, 'min': None,
                'max': None, 'delta': 0, 'span': 0, 'moved': False,
                'note': 'no ABS_X samples - could not measure movement'}
    span = max(vals) - min(vals)
    return {
        'samples': len(vals),
        'first': vals[0],
        'last': vals[-1],
        'min': min(vals),
        'max': max(vals),
        'delta': vals[-1] - vals[0],
        'span': span,
        'moved': span > MOVED_MIN,
        'note': None,
    }


def _upload(fd, direction, level, length_ms, eid=-1):
    # type=FF_CONSTANT, id, direction, trigger(button,interval),
    # replay(length,delay), pad, then ff_constant_effect{level, envelope[4]}
    buf = bytearray(EFFECT_SIZE)
    struct.pack_into('<HhH', buf, 0, FF_CONSTANT, eid, direction)
    struct.pack_into('<HH', buf, 6, 0, 0)                  # trigger
    struct.pack_into('<HH', buf, 10, length_ms, 0)         # replay
    struct.pack_into('<h', buf, 16, level)                 # constant.level
    struct.pack_into('<HHHH', buf, 18, 0, 0, 0, 0)         # envelope
    fcntl.ioctl(fd, EVIOCSFF, buf, True)
    return struct.unpack_from('<h', buf, 2)[0]


def _play(fd, eid, on=1):
    os.write(fd, struct.pack('<qqHHi', 0, 0, EV_FF, eid, on))


def _drain(fd):
    while True:
        try:
            if not os.read(fd, EVENT_SIZE * 64):
                return
        except (BlockingIOError, OSError):
            return


if __name__ == '__main__':
    test = FfbTest()
    ok, msg = test.start()
    print(msg)
    if not ok:
        raise SystemExit(1)
    while test.status()['running']:
        time.sleep(0.2)
    final = test.status()
    print(f'phase={final["phase"]} error={final["error"]}')
    for label, m in final['result'].items():
        print(f'  {label:5} {m["first"]} -> {m["last"]}  delta {m["delta"]}  '
              f'({m["samples"]} samples)')
