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

EV_ABS = 0x03
EV_FF = 0x15
ABS_X = 0x00
FF_CONSTANT = 0x52
DIR_LEFT = 0x4000
DIR_RIGHT = 0xC000

# struct ff_effect is 48 bytes on x86_64: the union is 8-byte aligned, so it
# starts at offset 16. struct input_event is 24: timeval(16) + type + code + value.
EVIOCSFF = 0x40000000 | (48 << 16) | (ord('E') << 8) | 0x80
EVIOCRMFF = 0x40000000 | (4 << 16) | (ord('E') << 8) | 0x81
EVENT_SIZE = 24

_LOCK = threading.Lock()
_ABORT = threading.Event()
_STATE = {
    'phase': 'idle',        # idle arming left pause right erasing done failed aborted
    'running': False,
    'error': None,
    'started': None,
    'finished': None,
    'device': None,
    'magnitude_pct': round(100.0 * MAGNITUDE / 32767),
    'duration_ms': DURATION_MS,
    'result': {},           # 'left'/'right' -> measurement dict
}


def status():
    """Snapshot for the dashboard. No I/O, so safe at the /data poll rate."""
    with _LOCK:
        out = dict(_STATE)
        out['result'] = {k: dict(v) for k, v in _STATE['result'].items()}
        if _STATE['started'] is not None:
            end = _STATE['finished'] or time.time()
            out['elapsed'] = round(end - _STATE['started'], 1)
        else:
            out['elapsed'] = 0.0
        return out


def _set(**kw):
    with _LOCK:
        _STATE.update(kw)


def _upload(fd, direction, level, length_ms, eid=-1):
    # type=FF_CONSTANT, id, direction, trigger(button,interval),
    # replay(length,delay), pad, then ff_constant_effect{level, envelope[4]}
    buf = bytearray(48)
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


def _push(fd, eid, direction, seconds):
    """Play in one direction for `seconds`, sampling ABS_X throughout."""
    vals = []
    _play(fd, eid, 1)
    deadline = time.time() + seconds
    try:
        while time.time() < deadline and not _ABORT.is_set():
            ready, _, _ = select.select([fd], [], [], 0.05)
            if not ready:
                continue
            try:
                blob = os.read(fd, EVENT_SIZE * 64)
            except BlockingIOError:
                continue
            for off in range(0, len(blob) - EVENT_SIZE + 1, EVENT_SIZE):
                _, _, typ, code, value = struct.unpack_from('<qqHHi', blob, off)
                if typ == EV_ABS and code == ABS_X:
                    vals.append(value)
    finally:
        _play(fd, eid, 0)

    if not vals:
        # No samples means we could not measure, not that torque was zero.
        return {'samples': 0, 'first': None, 'last': None, 'min': None,
                'max': None, 'delta': 0, 'span': 0, 'moved': False,
                'note': 'no ABS_X samples - could not measure movement'}
    return {
        'samples': len(vals),
        'first': vals[0],
        'last': vals[-1],
        'min': min(vals),
        'max': max(vals),
        'delta': vals[-1] - vals[0],
        'span': max(vals) - min(vals),
        'moved': (max(vals) - min(vals)) > 1000,
        'note': None,
    }


def _run(path):
    fd = None
    eid = -1
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        _set(phase='arming')
        _drain(fd)
        eid = _upload(fd, DIR_LEFT, MAGNITUDE, DURATION_MS)

        for label, direction in (('left', DIR_LEFT), ('right', DIR_RIGHT)):
            if _ABORT.is_set():
                break
            eid = _upload(fd, direction, MAGNITUDE, DURATION_MS, eid)
            _set(phase=label)
            measured = _push(fd, eid, direction, DURATION_MS / 1000 + SETTLE_S)
            with _LOCK:
                _STATE['result'][label] = measured
            if _ABORT.is_set():
                break
            _set(phase='pause')
            time.sleep(PAUSE_S)

        _set(phase='erasing')
        if eid >= 0:
            fcntl.ioctl(fd, EVIOCRMFF, eid)   # takes the id BY VALUE, not a pointer
        _set(phase='aborted' if _ABORT.is_set() else 'done')
    except OSError as exc:
        # Best effort - never leave an effect loaded on the device.
        try:
            if fd is not None and eid >= 0:
                _play(fd, eid, 0)
                fcntl.ioctl(fd, EVIOCRMFF, eid)
        except OSError:
            pass
        _set(phase='failed', error=f'{exc.strerror or exc} (errno {exc.errno})')
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        _set(running=False, finished=time.time())


def start():
    """Kick off one test. Returns (ok, message). Single-flight."""
    # Checked before the device is looked at: a double-tap must never stack two
    # uploads onto the same effect slot.
    with _LOCK:
        if _STATE['running']:
            return False, 'a test is already running'

    path = hid_layout.node_path('event')
    if not path:
        return False, 'no Fanatec event node - is the base plugged in?'
    if not os.access(path, os.W_OK):
        return False, (f'{path} is not writable by this user - force feedback '
                       f'needs O_RDWR (see the system checks)')

    with _LOCK:
        if _STATE['running']:                    # lost a race between the checks
            return False, 'a test is already running'
        _ABORT.clear()
        _STATE.update(phase='arming', running=True, error=None,
                      started=time.time(), finished=None, device=path, result={})

    threading.Thread(target=_run, args=(path,), daemon=True).start()
    return True, 'started'


def abort():
    """Stop the current test as soon as the worker notices. Idempotent."""
    with _LOCK:
        if not _STATE['running']:
            return False, 'nothing running'
    _ABORT.set()
    return True, 'aborting'


if __name__ == '__main__':
    ok, msg = start()
    print(msg)
    if not ok:
        raise SystemExit(1)
    while status()['running']:
        time.sleep(0.2)
    final = status()
    print(f'phase={final["phase"]} error={final["error"]}')
    for label, m in final['result'].items():
        print(f'  {label:5} {m["first"]} -> {m["last"]}  delta {m["delta"]}  '
              f'({m["samples"]} samples)')
