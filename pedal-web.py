#!/usr/bin/env python3
"""Local web dashboard for the Fanatec base's raw HID stream.

A background thread reads raw HID at full report rate, detects dropouts,
rail-pinning and value jumps, and LATCHES them, so an intermittent fault lasting
20 ms still shows on screen minutes later. The page polls at 10 Hz. It also hosts
the system checks (sysstate.py) and the force-feedback test (ffb.py).

Run:  python3 pedal-web.py   (--no-ffb leaves out the routes that move the wheel)
"""
import collections
import http.server
import json
import os
import select
import socket
import statistics
import sys
import threading
import time

import ffb
import hid_layout
import sysstate

PORT = 8765
FFB_ENABLED = True   # overridden by --no-ffb in __main__

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

# Pot dividers off the 3.3V sensor supply; STEER is an encoder, so no volts.
VOLT_CHANNELS = {'THROTTLE', 'BRAKE', 'CLUTCH'}

# Hardware button number -> rim function, per the ftec_keymap comments in
# hid-fanatec 0.2.3 (hid-ftec.c).
BTN_FN = {
    1: 'Square', 2: 'Cross', 3: 'Circle', 4: 'Triangle',
    5: 'GEAR UP  (right shift paddle)', 6: 'GEAR DOWN  (left shift paddle)',
    7: 'R2', 8: 'L2', 9: 'SH / Start', 10: 'OP / Select', 11: 'R3', 12: 'L3',
    13: 'Shifter R', 21: '(unknown)', 22: 'PS / Xbox / R toggle-up',
    23: 'Funky twist left', 24: 'Funky twist right',
    25: 'Funky push', 26: 'Ministick push',
    27: 'L toggle-up', 28: '(unknown)',
    29: 'Sequential gear down', 30: 'Sequential gear up',
    31: 'R toggle-down', 32: 'L toggle-down',
    33: 'R toggle-up-normal', 34: 'L toggle-up-normal',
    35: '(unknown)', 36: '(unknown)',
    61: 'L analog paddle (as button)', 62: 'R analog paddle (as button)',
}
for _i in range(7):
    BTN_FN[14 + _i] = f'Shifter {_i + 1}'
for _i in range(12):
    _pos = (_i + 1) % 12 or 12
    BTN_FN[37 + _i] = f'L knob pos {_pos}' + (' / twist right' if _i == 0
                                              else ' / twist left' if _i == 1 else '')
    BTN_FN[49 + _i] = f'R knob pos {_pos}' + (' / twist right' if _i == 0
                                              else ' / twist left' if _i == 1 else '')

HAT_DIRS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']

LOCK = threading.Lock()
LAYOUT = hid_layout.fallback_layout('device not opened yet')
HIST = {}
EVENTS = collections.deque(maxlen=400)
BTN = {}           # button number -> {'on','ever','count','last'}
# axis name -> [min, max] ever seen since reset. Separate storage from HIST,
# which is a rolling window: a press ages out of it, but must not age out of the
# latch this dashboard exists to keep.
SEEN = {}
HAT = {'value': None, 'ever': set()}
STATE = {
    'dev': None,
    'connected': False,
    'report': None,
    'size': 0,
    'count': 0,
    'rate': 0.0,
    'lo': None,
    'hi': None,
    'started': time.time(),
    'glitches': 0,
    'fw_version': None,
    'wheel_id': None,
    'pedals': None,
    'handbrake': None,
    'spare': None,
    'size_warn': None,
    'btn_init': False,
    # A dead stream looks exactly like a control that produces no data, so track
    # when a report last landed rather than letting 'rate' freeze at its old value.
    'last_report_t': None,
}

# DROPOUT is excluded: every rest longer than GAP fires one, which would bury the
# real JUMP/RAIL catches under a total made of the rig sitting still.
FAULT_KINDS = {'JUMP', 'RAIL'}


def event(kind, ch, detail):
    if kind in FAULT_KINDS:
        STATE['glitches'] += 1
    EVENTS.appendleft({
        't': round(time.time() - STATE['started'], 2),
        'kind': kind,
        'ch': ch,
        'detail': detail,
    })


def reset_tracking():
    """Wipe the latched state. Caller holds LOCK."""
    EVENTS.clear()
    STATE['glitches'] = 0
    STATE['lo'] = STATE['hi'] = None
    SEEN.clear()
    STATE['count'] = 0
    STATE['started'] = time.time()
    for dq in HIST.values():
        dq.clear()
    BTN.clear()
    STATE['btn_init'] = False
    STATE['size_warn'] = None
    HAT['value'] = None
    HAT['ever'] = set()


def install_layout(layout):
    """Adopt a freshly parsed layout. Caller holds LOCK."""
    global LAYOUT
    LAYOUT = layout
    HIST.clear()
    for ax in layout['axes']:
        HIST[ax['name']] = collections.deque(maxlen=HIST_LEN)


def note_axis(name, val, now):
    """Record one axis sample: rolling history AND the latched min/max.

    One function, so no code path can update the history and miss the latch.
    """
    if val is None:                 # report too short for this axis; handled
        return                      # here so no caller can seed a [None, None]
    HIST[name].append((now, val))   # latch and crash on the next sample
    seen = SEEN.get(name)
    if seen is None:
        SEEN[name] = [val, val]
    else:
        if val < seen[0]:
            seen[0] = val
        if val > seen[1]:
            seen[1] = val


def axis_value(rep, ax):
    """Decode one axis out of a report, or None if the report is too short."""
    i = ax['byte']
    if ax['bits'] == 16:
        if len(rep) <= i + 1:
            return None
        return rep[i] | (rep[i + 1] << 8)
    if len(rep) <= i:
        return None
    val = rep[i]
    return val - 256 if ax['signed'] and val > 127 else val


def button_mask(rep, spec):
    """The button bits as one integer, bit 0 = lowest-numbered button."""
    need = (spec['first_bit'] + spec['count'] + 7) // 8
    if len(rep) < need:
        return 0
    whole = int.from_bytes(bytes(rep[:need]), 'little')
    return (whole >> spec['first_bit']) & ((1 << spec['count']) - 1)


def decode_vendor(rep):
    """Firmware version / wheel id / pedal presence out of the vendor block.

    Mirrors ftecff_raw_event() in hid-ftecff.c, shifted by one for the report id
    this base does not send - which is why its sysfs copies read 0. See
    docs/driver.md.
    """
    out = {}
    if len(rep) != 33:               # offsets only mean anything at this shape
        return out
    if rep[29] == 0xff:
        if rep[30] == 0x04:
            out['pedals'] = bool(rep[31] & 0x0f)
            out['handbrake'] = bool(rep[31] >> 4 & 0x0f)
    else:
        out['wheel_id'] = rep[30]
        out['fw_version'] = rep[31] | (rep[32] << 8)
    return out


def note_buttons(mask, spec, now):
    """Latch button state; log presses, and log a first-ever press loudly."""
    # A bit high in the first report was never watched going down = "stuck on",
    # as opposed to a button someone is simply holding.
    first_report = not STATE['btn_init']
    STATE['btn_init'] = True
    first = spec['first_usage']
    for i in range(spec['count']):
        num = first + i
        on = bool(mask >> i & 1)
        rec = BTN.get(num)
        if rec is None:
            rec = BTN[num] = {'on': False, 'ever': False, 'count': 0,
                              'last': None, 'from_start': False}
        if on and first_report:
            rec['from_start'] = True
        if on == rec['on']:
            continue
        rec['on'] = on
        if on:
            rec['count'] += 1
            rec['last'] = now
            label = BTN_FN.get(num, '')
            if not rec['ever']:
                rec['ever'] = True
                event('BTN-NEW', f'btn {num}',
                      f'first press seen{" - " + label if label else ""}')
            else:
                event('BTN', f'btn {num}', f'pressed{" - " + label if label else ""}')


def reader():
    prev = {}
    railed = {}
    gap_open = False
    last_t = time.time()
    tick_t = time.time()
    tick_n = 0
    prev_mask = None

    while True:
        try:
            node = hid_layout.find_nodes()['hidraw']
            if not node:
                raise FileNotFoundError('no usb-Fanatec_*-hidraw node')
            path = node['path']
            layout = hid_layout.layout_for(node['link'])
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except (IndexError, OSError) as exc:
            with LOCK:
                STATE['connected'] = False
                STATE['dev'] = f'not found ({exc.__class__.__name__})'
            time.sleep(1.0)
            continue

        with LOCK:
            STATE['dev'] = path
            STATE['connected'] = True
            install_layout(layout)
        prev.clear()
        railed.clear()
        prev_mask = None

        try:
            while True:
                ready, _, _ = select.select([fd], [], [], 0.05)
                now = time.time()

                if not ready:
                    with LOCK:
                        if not gap_open and now - last_t > GAP:
                            gap_open = True
                            event('DROPOUT', '-',
                                  f'no HID report for {int((now-last_t)*1000)}ms')
                    continue

                reports = []
                try:
                    while True:
                        data = os.read(fd, 128)
                        if not data:
                            break
                        reports.append(data)
                except BlockingIOError:
                    pass
                except OSError:
                    break
                if not reports:
                    break  # device went away -> reopen

                with LOCK:
                    if gap_open:
                        gap_open = False
                        event('RESUMED', '-',
                              f'stream returned after {int((now-last_t)*1000)}ms')
                    last_t = now
                    STATE['last_report_t'] = now
                    tick_n += len(reports)
                    if now - tick_t >= 1.0:
                        STATE['rate'] = round(tick_n / (now - tick_t), 1)
                        tick_t, tick_n = now, 0

                    for rep in reports:
                        STATE['count'] += 1
                        STATE['report'] = rep
                        STATE['size'] = len(rep)
                        if len(rep) != LAYOUT['size']:
                            STATE['size_warn'] = (
                                f'report is {len(rep)} bytes but the descriptor '
                                f'declares {LAYOUT["size"]} - offsets may be wrong')
                        if STATE['lo'] is None or len(STATE['lo']) != len(rep):
                            STATE['lo'] = list(rep)
                            STATE['hi'] = list(rep)
                        else:
                            for i, b in enumerate(rep):
                                if b < STATE['lo'][i]:
                                    STATE['lo'][i] = b
                                if b > STATE['hi'][i]:
                                    STATE['hi'][i] = b

                        for ax in LAYOUT['axes']:
                            val = axis_value(rep, ax)
                            if val is None:
                                continue
                            name = ax['name']
                            note_axis(name, val, now)

                            old = prev.get(name)
                            if (ax['bits'] == 16 and old is not None
                                    and abs(val - old) > JUMP):
                                event('JUMP', name, f'{old} -> {val}  (D{val-old:+})')
                            prev[name] = val

                            # only ENTERING a rail; resting at one must not spam
                            at_rail = ax['bits'] == 16 and val in (0, 65535)
                            if name in railed and at_rail and not railed[name]:
                                event('RAIL', name,
                                      'went to ' + ('MAX 65535' if val else 'MIN 0'))
                            railed[name] = at_rail

                        spec = LAYOUT['buttons']
                        if spec:
                            mask = button_mask(rep, spec)
                            if mask != prev_mask:
                                note_buttons(mask, spec, now)
                                prev_mask = mask

                        hat = LAYOUT['hat']
                        if hat and len(rep) > hat['byte']:
                            hv = (rep[hat['byte']] >> hat['shift']) & 0x0f
                            if hv != HAT['value']:
                                HAT['value'] = hv
                                if hv not in HAT['ever'] and hv <= hat['lmax']:
                                    HAT['ever'].add(hv)
                                    event('HAT', 'hat',
                                          f'first {HAT_DIRS[hv]} seen (raw {hv})')

                        if LAYOUT['spare_bits']:
                            lo_b = LAYOUT['spare_bits'][0] // 8
                            hi_b = LAYOUT['spare_bits'][-1] // 8
                            STATE['spare'] = list(rep[lo_b:hi_b + 1])

                        for key, val in decode_vendor(rep).items():
                            STATE[key] = val
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        with LOCK:
            STATE['connected'] = False
        time.sleep(0.5)


def reversals(vals):
    n = 0
    direction = 0
    for a, b in zip(vals, vals[1:]):
        d = (b > a) - (b < a)
        if d and direction and d != direction:
            n += 1
        if d:
            direction = d
    return n


def snapshot():
    now = time.time()
    with LOCK:
        rep = STATE['report']
        lo, hi = STATE['lo'], STATE['hi']
        warnings = list(LAYOUT['warnings'])
        if STATE['size_warn']:
            warnings.append(STATE['size_warn'])
        # Report the silence explicitly and zero the rate: a frozen 'rate' from a
        # stream that died minutes ago is worse than no number at all.
        silent = None if STATE['last_report_t'] is None else round(
            now - STATE['last_report_t'], 1)
        live = silent is not None and silent <= GAP
        out = {
            'dev': STATE['dev'],
            'connected': STATE['connected'],
            'count': STATE['count'],
            'rate': STATE['rate'] if live else 0.0,
            'silent_for': silent,
            'streaming': live,
            'frozen': silent is not None and silent > FROZEN,
            'size': STATE['size'],
            'uptime': round(now - STATE['started'], 1),
            'glitches': STATE['glitches'],
            'events': list(EVENTS)[:80],
            'hex': ' '.join(f'{b:02x}' for b in rep) if rep else '',
            'layout_src': LAYOUT['source'],
            'warnings': warnings,
            'fw_version': STATE['fw_version'],
            'wheel_id': STATE['wheel_id'],
            'pedals': STATE['pedals'],
            'handbrake': STATE['handbrake'],
            'spare': STATE['spare'],
            'axes': [],
            'motion': [],
            'buttons': [],
            'btn_seen': [],
            'hat': None,
            'bytes': [],
            'undecoded': [],
        }

        for ax in LAYOUT['axes']:
            pts = list(HIST.get(ax['name'], ()))
            recent = [v for t, v in pts if now - t <= WINDOW]
            vals = [v for _, v in pts]
            seen = SEEN.get(ax['name'])
            wide = ax['bits'] == 16
            ch = {
                'name': ax['name'],
                'hid': ax['hid'],
                'byte': ax['byte'],
                'bits': ax['bits'],
                'lmin': ax['lmin'],
                'lmax': ax['lmax'],
                'value': vals[-1] if vals else None,
                'volts': (round(vals[-1] / 65535 * 3.3, 3)
                          if vals and ax['name'] in VOLT_CHANNELS else None),
                'min': seen[0] if seen else None,
                'max': seen[1] if seen else None,
                'span': (seen[1] - seen[0]) if seen else 0,
                'jitter_sd': round(statistics.pstdev(recent), 1) if len(recent) > 1 else 0.0,
                'jitter_rev': reversals(recent) if len(recent) > 1 else 0,
                'n_recent': len(recent),
                'spark': vals[-SPARK:],
            }
            full = ax['lmax'] - ax['lmin']
            ch['span_pct'] = round(100.0 * ch['span'] / full, 1) if full else 0.0
            # normalised so it is comparable across report rates
            ch['rev_per100'] = (round(100.0 * ch['jitter_rev'] / len(recent), 1)
                                if len(recent) > 1 else 0.0)
            ch['warn'] = wide and ch['jitter_sd'] > SD_WARN
            # latched, not windowed: "never moved since you pressed reset"
            ch['idle'] = seen is None or ch['span'] == 0
            out['axes'].append(ch)

            # Motion attribution: what actually responded in the last WINDOW, so
            # a real cross-channel link can be told from an autoscaled sparkline.
            if len(recent) > 1:
                move = max(recent) - min(recent)
                floor = MOTION_MIN16 if wide else MOTION_MIN8
                if move >= floor:
                    out['motion'].append({
                        'name': ax['name'], 'byte': ax['byte'], 'move': move,
                        'pct': round(100.0 * move / full, 1) if full else 0.0,
                    })
        out['motion'].sort(key=lambda m: -m['pct'])

        spec = LAYOUT['buttons']
        if spec:
            for i in range(spec['count']):
                num = spec['first_usage'] + i
                rec = BTN.get(num)
                bit = spec['first_bit'] + i
                out['buttons'].append({
                    'n': num,
                    'byte': bit // 8,
                    'bit': bit % 8,
                    'fn': BTN_FN.get(num, ''),
                    'on': bool(rec and rec['on']),
                    'ever': bool(rec and rec['ever']),
                    'count': rec['count'] if rec else 0,
                    # never observed going down -> likely shorted
                    'stuck': bool(rec and rec['on'] and rec['from_start']),
                })
            out['btn_seen'] = [b['n'] for b in out['buttons'] if b['ever']]

        if LAYOUT['hat']:
            out['hat'] = {
                'value': HAT['value'],
                'dir': (HAT_DIRS[HAT['value']]
                        if HAT['value'] is not None and HAT['value'] < 8 else 'centre'),
                'ever': sorted(HAT['ever']),
                'byte': LAYOUT['hat']['byte'],
            }

        labels = byte_labels(LAYOUT)
        if lo and hi:
            for i in range(len(lo)):
                out['bytes'].append({
                    'i': i, 'lo': lo[i], 'hi': hi[i],
                    'now': rep[i] if rep and i < len(rep) else 0,
                    'moved': hi[i] != lo[i],
                    'label': labels.get(i, 'undecoded'),
                    'known': i in labels,
                })
            out['undecoded'] = [i for i in range(len(lo))
                                if hi[i] != lo[i] and i not in labels]

    # Outside LOCK on purpose: ffb has its own lock and does no I/O, so it can
    # ride the 10Hz poll without widening the window that blocks the reader.
    out['ffb'] = ffb.status()
    out['ffb_enabled'] = FFB_ENABLED
    return out


def byte_labels(layout):
    """byte index -> what the descriptor says lives there."""
    labels = {}
    spec = layout['buttons']
    if spec:
        first, last = spec['first_bit'], spec['first_bit'] + spec['count'] - 1
        for b in range(first // 8, last // 8 + 1):
            labels[b] = 'buttons'
    for bit in layout['spare_bits']:
        labels.setdefault(bit // 8, 'spare button bits')
    if layout['hat']:
        b = layout['hat']['byte']
        labels[b] = ('hat + ' + labels[b]) if b in labels else 'hat'
    for ax in layout['axes']:
        for k in range(ax['bits'] // 8):
            labels[ax['byte'] + k] = ax['name']
    for b in layout['vendor']:
        labels[b] = 'vendor'
    return labels


WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')

# A fixed table, not a directory walk: the request path is looked up, never
# joined onto a filename, so there is no traversal to get wrong.
STATIC = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/app.css': ('app.css', 'text/css; charset=utf-8'),
    '/app.js': ('app.js', 'application/javascript; charset=utf-8'),
}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj).encode(), 'application/json', code)

    def _static(self, name, ctype):
        """Read per request, so editing web/ only needs a refresh."""
        try:
            with open(os.path.join(WEB_DIR, name), 'rb') as fh:
                body = fh.read()
        except OSError as exc:
            self.send_error(500, f'cannot read web/{name}: {exc.strerror}')
            return
        self._send(body, ctype)

    def do_GET(self):
        # startswith, not ==: the page appends a cache-busting query string
        if self.path.startswith('/data'):
            self._json(snapshot())
        elif self.path.startswith('/system'):
            self._json(sysstate.state())   # cached with its own TTL in sysstate
        elif self.path in STATIC:
            self._static(*STATIC[self.path])
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == '/reset':
            with LOCK:
                reset_tracking()
            self._json({'ok': True})
        elif self.path == '/ffb/start':
            if not FFB_ENABLED:
                self._json({'ok': False, 'msg': 'FFB disabled (--no-ffb)'}, 403)
                return
            ok, msg = ffb.start()
            self._json({'ok': ok, 'msg': msg}, 200 if ok else 409)
        elif self.path == '/ffb/abort':
            ok, msg = ffb.abort()
            self._json({'ok': ok, 'msg': msg})
        else:
            self.send_error(404)

    def log_message(self, *_args):
        pass


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


if __name__ == '__main__':
    # No auth here, so anything on the LAN can POST the routes that move the
    # wheel. One flag takes that off the table.
    if '--no-ffb' in sys.argv:
        FFB_ENABLED = False

    threading.Thread(target=reader, daemon=True).start()
    srv = http.server.ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    ip = lan_ip()
    # flush: stdout is block-buffered when redirected, hiding the URLs until exit
    print(f'  local:  http://localhost:{PORT}', flush=True)
    if ip:
        print(f'  phone:  http://{ip}:{PORT}', flush=True)
    print('\n  Ctrl-C to stop', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\nstopped')
