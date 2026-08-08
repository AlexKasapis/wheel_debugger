#!/usr/bin/env python3
"""The latched view of the HID stream.

One Tracker owns everything the dashboard remembers. ingest() is the only way a
report enters it, so the reader thread and the self-test exercise the same code
rather than two copies that can drift apart.

Nothing here opens a device or knows about force feedback; it is fed bytes.
"""
import collections
import statistics
import threading
import time

import decode
import hid_layout

JUMP = 3000        # sample-to-sample delta that counts as a glitch (16-bit ch)
GAP = 2.0          # seconds of silence that logs a DROPOUT (never a fault: the
                   # base is send-on-change, so rest is silent by design)
FROZEN = 20.0      # seconds of silence past which the page says it is frozen,
                   # not idle. Far above GAP so brief rest cannot cry wolf.
WINDOW = 2.0       # seconds for the rolling jitter stats and the motion panel
SD_WARN = 200      # rolling stdev above this = noisy (dither ~10, bad thr ~1380)
MOTION_MIN16 = 200 # peak-to-peak below this in WINDOW is dither (~+-30 LSB)
MOTION_MIN8 = 2
HIST_LEN = 900
SPARK = 180

# DROPOUT is excluded: every rest longer than GAP fires one, which would bury the
# real JUMP/RAIL catches under a total made of the rig sitting still.
FAULT_KINDS = {'JUMP', 'RAIL'}


class Tracker:
    """Latched state for one base. Every public method is thread-safe."""

    def __init__(self, layout=None):
        self._lock = threading.Lock()
        self.dev = None
        self.connected = False
        self.layout = layout or hid_layout.fallback_layout('device not opened yet')
        self.events = collections.deque(maxlen=400)
        self._reset()
        self._install(self.layout)

    # -- state ---------------------------------------------------------------

    def _reset(self):
        """Wipe everything latched since the last reset. Caller holds the lock."""
        self.events.clear()
        self.report = None
        self.size = 0
        self.count = 0
        self.rate = 0.0
        self.lo = None
        self.hi = None
        self.started = time.time()
        self.glitches = 0
        self.fw_version = None
        self.wheel_id = None
        self.pedals = None
        self.handbrake = None
        self.spare = None
        self.size_warn = None
        self.btn_init = False
        # A dead stream looks exactly like a control that produces no data, so
        # track when a report last landed rather than letting 'rate' freeze.
        self.last_report_t = None
        # axis name -> [min, max] ever seen. Separate storage from hist, which is
        # a rolling window: a press ages out of it, but must not age out of the
        # latch this dashboard exists to keep.
        self.seen = {}
        self.btn = {}
        self.hat_value = None
        self.hat_ever = set()
        self._clear_stream_state()

    def _clear_stream_state(self):
        """Per-connection fault detection. Caller holds the lock."""
        self._prev = {}
        self._railed = {}
        self._prev_mask = None
        self._gap_open = False
        self._last_t = time.time()
        self._tick_t = time.time()
        self._tick_n = 0

    def _install(self, layout):
        """Adopt a layout and size the history to it. Caller holds the lock."""
        self.layout = layout
        self.hist = {ax['name']: collections.deque(maxlen=HIST_LEN)
                     for ax in layout['axes']}

    def reset(self):
        with self._lock:
            self._reset()
            self._install(self.layout)

    def connect(self, path, layout):
        """A device was opened: adopt its layout and drop stale stream state."""
        with self._lock:
            self.dev = path
            self.connected = True
            self._install(layout)
            self._clear_stream_state()

    def disconnect(self, why=None):
        with self._lock:
            self.connected = False
            if why:
                self.dev = why

    # -- events --------------------------------------------------------------

    def _event(self, kind, ch, detail):
        """Log one event. Caller holds the lock."""
        if kind in FAULT_KINDS:
            self.glitches += 1
        self.events.appendleft({
            't': round(time.time() - self.started, 2),
            'kind': kind,
            'ch': ch,
            'detail': detail,
        })

    def log(self, kind, ch, detail):
        with self._lock:
            self._event(kind, ch, detail)

    def note_idle(self, now=None):
        """The reader saw no report this tick. Opens a DROPOUT once per gap."""
        now = time.time() if now is None else now
        with self._lock:
            if not self._gap_open and now - self._last_t > GAP:
                self._gap_open = True
                self._event('DROPOUT', '-',
                            f'no HID report for {int((now - self._last_t) * 1000)}ms')

    # -- the stream ----------------------------------------------------------

    def ingest(self, report, now=None):
        """Take one raw HID report. The only path into the latched state."""
        now = time.time() if now is None else now
        with self._lock:
            if self._gap_open:
                self._gap_open = False
                self._event('RESUMED', '-',
                            f'stream returned after {int((now - self._last_t) * 1000)}ms')
            self._last_t = now
            self.last_report_t = now
            self._note_rate(now)

            self.count += 1
            self.report = report
            self.size = len(report)
            if len(report) != self.layout['size']:
                self.size_warn = (
                    f'report is {len(report)} bytes but the descriptor declares '
                    f'{self.layout["size"]} - offsets may be wrong')
            self._note_bytes(report)
            self._note_axes(report, now)
            self._note_buttons_changed(report, now)
            self._note_hat(report)
            self._note_vendor(report)

    def _note_rate(self, now):
        self._tick_n += 1
        span = now - self._tick_t
        if span >= 1.0:
            self.rate = round(self._tick_n / span, 1)
            self._tick_t, self._tick_n = now, 0

    def _note_bytes(self, report):
        if self.lo is None or len(self.lo) != len(report):
            self.lo = list(report)
            self.hi = list(report)
            return
        for i, b in enumerate(report):
            if b < self.lo[i]:
                self.lo[i] = b
            if b > self.hi[i]:
                self.hi[i] = b

    def _note_axes(self, report, now):
        for ax in self.layout['axes']:
            val = decode.axis_value(report, ax)
            if val is None:            # report too short for this axis
                continue
            name = ax['name']
            self._note_axis(name, val, now)

            old = self._prev.get(name)
            wide = ax['bits'] == 16
            if wide and old is not None and abs(val - old) > JUMP:
                self._event('JUMP', name, f'{old} -> {val}  (D{val - old:+})')
            self._prev[name] = val

            # only ENTERING a rail; resting at one must not spam
            at_rail = wide and val in (0, 65535)
            if name in self._railed and at_rail and not self._railed[name]:
                self._event('RAIL', name,
                            'went to ' + ('MAX 65535' if val else 'MIN 0'))
            self._railed[name] = at_rail

    def _note_axis(self, name, val, now):
        """Rolling history AND the latched min/max, so no path can miss one."""
        self.hist[name].append((now, val))
        seen = self.seen.get(name)
        if seen is None:
            self.seen[name] = [val, val]
        else:
            if val < seen[0]:
                seen[0] = val
            if val > seen[1]:
                seen[1] = val

    def _note_buttons_changed(self, report, now):
        spec = self.layout['buttons']
        if not spec:
            return
        mask = decode.button_mask(report, spec)
        if mask == self._prev_mask:
            return
        self._prev_mask = mask
        self._note_buttons(mask, spec, now)

    def _note_buttons(self, mask, spec, now):
        """Latch button state; log presses, and log a first-ever press loudly."""
        # A bit high in the first report was never watched going down = "stuck
        # on", as opposed to a button someone is simply holding.
        first_report = not self.btn_init
        self.btn_init = True
        for i in range(spec['count']):
            num = spec['first_usage'] + i
            on = bool(mask >> i & 1)
            rec = self.btn.get(num)
            if rec is None:
                rec = self.btn[num] = {'on': False, 'ever': False, 'count': 0,
                                       'last': None, 'from_start': False}
            if on and first_report:
                rec['from_start'] = True
            if on == rec['on']:
                continue
            rec['on'] = on
            if not on:
                continue
            rec['count'] += 1
            rec['last'] = now
            label = decode.BTN_FN.get(num, '')
            suffix = f' - {label}' if label else ''
            if rec['ever']:
                self._event('BTN', f'btn {num}', f'pressed{suffix}')
            else:
                rec['ever'] = True
                self._event('BTN-NEW', f'btn {num}', f'first press seen{suffix}')

    def _note_hat(self, report):
        hat = self.layout['hat']
        hv = decode.hat_value(report, hat)
        if hv is None or hv == self.hat_value:
            return
        self.hat_value = hv
        if hv not in self.hat_ever and hv <= hat['lmax']:
            self.hat_ever.add(hv)
            self._event('HAT', 'hat', f'first {decode.HAT_DIRS[hv]} seen (raw {hv})')

    VENDOR_FIELDS = ('fw_version', 'wheel_id', 'pedals', 'handbrake')

    def _note_vendor(self, report):
        spare_bits = self.layout['spare_bits']
        if spare_bits:
            lo_b, hi_b = spare_bits[0] // 8, spare_bits[-1] // 8
            self.spare = list(report[lo_b:hi_b + 1])
        info = decode.decode_vendor(report)
        for key in self.VENDOR_FIELDS:      # named, so decode cannot set anything else
            if key in info:
                setattr(self, key, info[key])

    # -- the read side -------------------------------------------------------

    def snapshot(self):
        """Everything the page draws. No force-feedback state - see server."""
        now = time.time()
        with self._lock:
            silent = (None if self.last_report_t is None
                      else round(now - self.last_report_t, 1))
            # Report the silence explicitly and zero the rate: a frozen 'rate'
            # from a stream that died minutes ago is worse than no number.
            live = silent is not None and silent <= GAP
            warnings = list(self.layout['warnings'])
            if self.size_warn:
                warnings.append(self.size_warn)
            out = {
                'dev': self.dev,
                'connected': self.connected,
                'count': self.count,
                'rate': self.rate if live else 0.0,
                'silent_for': silent,
                'streaming': live,
                'frozen': silent is not None and silent > FROZEN,
                'size': self.size,
                'uptime': round(now - self.started, 1),
                'glitches': self.glitches,
                'events': list(self.events)[:80],
                'hex': ' '.join(f'{b:02x}' for b in self.report) if self.report else '',
                'layout_src': self.layout['source'],
                'warnings': warnings,
                'fw_version': self.fw_version,
                'wheel_id': self.wheel_id,
                'pedals': self.pedals,
                'handbrake': self.handbrake,
                'spare': self.spare,
                'hat': self._hat_out(),
            }
            axes, recent = self._axes_out(now)
            out['axes'] = axes
            out['motion'] = self._motion_out(axes, recent)
            out['buttons'] = self._buttons_out()
            out['btn_seen'] = [b['n'] for b in out['buttons'] if b['ever']]
            out['bytes'], out['undecoded'] = self._bytes_out()
            return out

    def _axes_out(self, now):
        """Per-axis payload, plus the in-window samples the motion panel needs."""
        axes = []
        recent_by_name = {}
        for ax in self.layout['axes']:
            pts = list(self.hist.get(ax['name'], ()))
            recent = [v for t, v in pts if now - t <= WINDOW]
            vals = [v for _, v in pts]
            seen = self.seen.get(ax['name'])
            span = (seen[1] - seen[0]) if seen else 0
            full = ax['lmax'] - ax['lmin']
            ch = {
                'name': ax['name'],
                'hid': ax['hid'],
                'byte': ax['byte'],
                'bits': ax['bits'],
                'lmin': ax['lmin'],
                'lmax': ax['lmax'],
                'value': vals[-1] if vals else None,
                'volts': (round(vals[-1] / 65535 * 3.3, 3)
                          if vals and ax['name'] in decode.VOLT_CHANNELS else None),
                'min': seen[0] if seen else None,
                'max': seen[1] if seen else None,
                'span': span,
                'span_pct': round(100.0 * span / full, 1) if full else 0.0,
                'jitter_sd': (round(statistics.pstdev(recent), 1)
                              if len(recent) > 1 else 0.0),
                'jitter_rev': decode.reversals(recent) if len(recent) > 1 else 0,
                'n_recent': len(recent),
                'spark': vals[-SPARK:],
                # latched, not windowed: "never moved since you pressed reset"
                'idle': seen is None or span == 0,
            }
            # normalised so it is comparable across report rates
            ch['rev_per100'] = (round(100.0 * ch['jitter_rev'] / len(recent), 1)
                                if len(recent) > 1 else 0.0)
            ch['warn'] = ax['bits'] == 16 and ch['jitter_sd'] > SD_WARN
            axes.append(ch)
            recent_by_name[ax['name']] = recent
        return axes, recent_by_name

    def _motion_out(self, axes, recent_by_name):
        """What actually responded in the last WINDOW, so a real cross-channel
        link can be told from an autoscaled sparkline."""
        motion = []
        for ch in axes:
            recent = recent_by_name[ch['name']]
            if len(recent) <= 1:
                continue
            move = max(recent) - min(recent)
            full = ch['lmax'] - ch['lmin']
            if move >= (MOTION_MIN16 if ch['bits'] == 16 else MOTION_MIN8):
                motion.append({
                    'name': ch['name'], 'byte': ch['byte'], 'move': move,
                    'pct': round(100.0 * move / full, 1) if full else 0.0,
                })
        motion.sort(key=lambda m: -m['pct'])
        return motion

    def _buttons_out(self):
        spec = self.layout['buttons']
        if not spec:
            return []
        out = []
        for i in range(spec['count']):
            num = spec['first_usage'] + i
            rec = self.btn.get(num)
            bit = spec['first_bit'] + i
            out.append({
                'n': num,
                'byte': bit // 8,
                'bit': bit % 8,
                'fn': decode.BTN_FN.get(num, ''),
                'on': bool(rec and rec['on']),
                'ever': bool(rec and rec['ever']),
                'count': rec['count'] if rec else 0,
                # never observed going down -> likely shorted
                'stuck': bool(rec and rec['on'] and rec['from_start']),
            })
        return out

    def _hat_out(self):
        if not self.layout['hat']:
            return None
        centred = self.hat_value is None or self.hat_value >= 8
        return {
            'value': self.hat_value,
            'dir': 'centre' if centred else decode.HAT_DIRS[self.hat_value],
            'ever': sorted(self.hat_ever),
            'byte': self.layout['hat']['byte'],
        }

    def _bytes_out(self):
        if not self.lo or not self.hi:
            return [], []
        labels = decode.byte_labels(self.layout)
        rep = self.report
        rows = [{
            'i': i,
            'lo': self.lo[i],
            'hi': self.hi[i],
            'now': rep[i] if rep and i < len(rep) else 0,
            'moved': self.hi[i] != self.lo[i],
            'label': labels.get(i, 'undecoded'),
            'known': i in labels,
        } for i in range(len(self.lo))]
        undecoded = [r['i'] for r in rows if r['moved'] and not r['known']]
        return rows, undecoded
