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


PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fanatec wheel/pedal diagnostics</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; padding:14px; background:#101216; color:#dfe4ec;
         font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  h1 { font-size:15px; margin:0 0 10px; color:#9aa6b8; font-weight:600;
       letter-spacing:.06em; text-transform:uppercase; }
  h2 { font-size:12px; margin:22px 0 8px; color:#7d8798; font-weight:600;
       letter-spacing:.08em; text-transform:uppercase; }
  #banner { padding:10px 12px; border-radius:6px; margin-bottom:10px;
            background:#16351f; border:1px solid #2c6b3f; color:#7fe0a0; }
  #banner.bad { background:#3a1518; border-color:#7d2b31; color:#ff9aa2; }
  #banner.idle { background:#3a2f13; border-color:#7d6425; color:#ffd58a; }
  #warn { padding:8px 12px; border-radius:6px; margin-bottom:10px;
          background:#3a2f13; border:1px solid #7d6425; color:#ffd58a;
          display:none; }
  #info { color:#8b95a6; font-size:12px; margin-bottom:12px; }
  .grid { display:grid; gap:10px; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); }
  .grid.sm { grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); }
  .card { background:#171a20; border:1px solid #262b34; border-radius:8px; padding:12px; }
  .card.warn { border-color:#7d2b31; }
  .card.idle { border-style:dashed; border-color:#39414f; }
  .nm { color:#8b95a6; font-size:12px; letter-spacing:.08em; }
  .big { font-size:30px; font-variant-numeric:tabular-nums; margin:2px 0; }
  .big.sm2 { font-size:22px; }
  .sub { color:#8b95a6; font-size:12px; }
  .tiny { color:#5f6878; font-size:11px; }
  .hot { color:#ff9aa2; }
  .ok  { color:#7fe0a0; }
  .bar { height:8px; background:#0d0f13; border-radius:4px; margin:7px 0 3px;
         position:relative; overflow:hidden; }
  .bar > i { position:absolute; top:0; bottom:0; background:#3d7fbf; }
  .bar > u { position:absolute; top:0; bottom:0; background:#2a3a4a; }
  canvas { width:100%; height:52px; display:block; margin-top:6px;
           background:#0d0f13; border-radius:4px; }
  table { border-collapse:collapse; width:100%; font-size:12px; }
  td,th { padding:3px 8px; text-align:left; border-bottom:1px solid #21252d; }
  th { color:#7d8798; font-weight:600; }
  .bytes { display:flex; flex-wrap:wrap; gap:3px; }
  .b { padding:3px 5px; border-radius:3px; background:#1b1f26; color:#5f6878;
       font-size:11px; min-width:34px; text-align:center; }
  .b.moved { background:#3a2a12; color:#ffcc7a; }
  .b.moved.known { background:#12303a; color:#7ad4ee; }
  #btns { display:flex; flex-wrap:wrap; gap:3px; }
  .k { padding:4px 0; border-radius:3px; background:#161a20; color:#4b5462;
       font-size:11px; width:30px; text-align:center; border:1px solid #1d222a; }
  .k.ever { background:#12303a; color:#7ad4ee; border-color:#1d4a5a; }
  .k.on { background:#7fe0a0; color:#0d1a12; border-color:#7fe0a0; font-weight:700; }
  .k.stuck { background:#3a1518; color:#ff9aa2; border-color:#7d2b31; }
  #hatgrid { display:grid; grid-template-columns:repeat(3,34px); gap:3px; }
  .h { height:26px; border-radius:3px; background:#161a20; border:1px solid #1d222a;
       color:#4b5462; font-size:10px; display:flex; align-items:center;
       justify-content:center; }
  .h.ever { background:#12303a; color:#7ad4ee; border-color:#1d4a5a; }
  .h.on { background:#7fe0a0; color:#0d1a12; font-weight:700; }
  button { background:#242a34; color:#dfe4ec; border:1px solid #39414f;
           border-radius:5px; padding:6px 14px; font:inherit; cursor:pointer; }
  button:hover { background:#2e3542; }
  .hex { word-break:break-all; color:#6f7a8c; font-size:11px; }
  #motion { font-size:13px; }
  #motion .row { display:flex; gap:10px; align-items:baseline; }
  #motion .nmw { min-width:110px; color:#7ad4ee; }

  /* ---- system checks -------------------------------------------------- */
  /* Dots are round to keep them apart from the button grid, which uses the
     same colours for a different meaning. */
  #sys { border:1px solid #262b34; border-radius:6px; background:#141821;
         margin-bottom:10px; }
  #syshead { display:flex; gap:10px; align-items:center; padding:9px 12px;
             cursor:pointer; user-select:none; }
  #syshead .caret { color:#5f6878; font-size:10px; margin-left:auto; }
  #sysbody { display:none; border-top:1px solid #21252d; }
  #sys.open #sysbody { display:block; }
  .dot { width:9px; height:9px; border-radius:50%; flex:0 0 auto;
         background:#5f6878; }
  .dot.ok { background:#4a9d68; }
  .dot.warn { background:#c79a3a; }
  .dot.bad { background:#c4505a; }
  .chk { display:flex; gap:10px; padding:8px 12px;
         border-bottom:1px solid #1b1f26; }
  .chk:last-child { border-bottom:none; }
  .chk .dot { margin-top:5px; }
  .chk .lab { flex:0 0 108px; color:#9aa6b8; font-size:12px; }
  .chk .det { flex:1 1 220px; min-width:0; }
  .chk .why { color:#5f6878; font-size:11px; margin-top:3px; }
  /* user-select:all, not navigator.clipboard, which needs a secure context and
     so does nothing over http on the LAN - i.e. on the phone beside the rig. */
  .fix { display:block; margin-top:6px; padding:5px 8px; border-radius:4px;
         background:#0d0f13; border:1px solid #2a313c; color:#7ad4ee;
         font-size:11.5px; word-break:break-all;
         user-select:all; -webkit-user-select:all; }

  /* ---- actions -------------------------------------------------------- */
  #actions { display:flex; flex-wrap:wrap; gap:10px; align-items:center;
             margin-bottom:4px; }
  #ffbwrap { display:flex; flex-wrap:wrap; gap:10px; align-items:center;
             flex:1 1 auto; }
  #hold { position:relative; overflow:hidden; border-color:#7d2b31;
          touch-action:none; }
  #hold[disabled] { opacity:.45; cursor:not-allowed; border-color:#39414f; }
  #hold .fillbar { position:absolute; inset:0 auto 0 0; width:0;
                   background:#7d2b31; }
  #hold .lbl { position:relative; }
  #abort { border-color:#7d2b31; color:#ff9aa2; display:none; }
  #ffbstat { font-size:12px; color:#8b95a6; }
  #ffbres { font-size:12px; }
  #ffbres .nmw { display:inline-block; min-width:56px; color:#7ad4ee; }
  #err { color:#ff9aa2; font-size:12px; min-height:16px; }
  #bannerfix:not(:empty) { margin:-4px 0 10px; }
</style>

<h1>Fanatec wheel/pedal diagnostics</h1>
<div id="banner">waiting for data...</div>
<div id="bannerfix"></div>

<div id="sys">
  <div id="syshead" onclick="toggleSys()">
    <span class="dot" id="sysdot"></span>
    <span id="syssum">checking this machine...</span>
    <span class="caret" id="syscaret">SHOW</span>
  </div>
  <div id="sysbody"></div>
</div>

<div id="warn"></div>
<div id="info"></div>

<div id="actions">
  <button onclick="reset()">reset stats &amp; event log</button>
  <div id="ffbwrap">
    <button id="hold" disabled><i class="fillbar"></i><span class="lbl">hold to
      run FFB test</span></button>
    <button id="abort" onclick="abortFfb()">ABORT</button>
    <span id="ffbstat"></span>
  </div>
</div>
<div id="err"></div>
<div id="ffbres"></div>

<h2>Live motion <span class="sub">(what changed in the last 2 s)</span></h2>
<div id="motion" class="sub">nothing moving</div>

<h2>Analog axes</h2>
<div class="grid" id="chans"></div>

<h2>Rim analog <span class="sub">(ministick, slider, dial)</span></h2>
<div class="grid sm" id="aux"></div>

<h2>Hat switch <span class="sub">(blue = seen since reset, green = now)</span></h2>
<div id="hatgrid"></div>
<div id="hatsub" class="sub" style="margin-top:6px"></div>

<h2>Buttons <span class="sub">(grey = never seen, blue = seen, green = down,
   red = stuck on)</span></h2>
<div id="btns"></div>
<div id="btnsub" class="sub" style="margin-top:8px"></div>

<h2>Event log <span class="sub">(latched - survives until reset)</span></h2>
<table><thead><tr><th>t+s</th><th>kind</th><th>ch</th><th>detail</th></tr></thead>
<tbody id="events"></tbody></table>

<h2>Report bytes <span class="sub">(blue = moved and decoded, orange = moved but
   undecoded)</span></h2>
<div class="bytes" id="bytes"></div>
<div id="cands" class="sub" style="margin-top:8px"></div>
<div class="hex" id="hex" style="margin-top:8px"></div>

<script>
function spark(cv, vals) {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  cv.width = w * dpr; cv.height = h * dpr;
  const g = cv.getContext('2d');
  g.scale(dpr, dpr);
  g.clearRect(0, 0, w, h);
  if (!vals || vals.length < 2) return;
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi === lo) { lo -= 1; hi += 1; }
  g.strokeStyle = '#5fb0ff'; g.lineWidth = 1.5; g.beginPath();
  vals.forEach((v, i) => {
    const x = i / (vals.length - 1) * w;
    const y = h - 3 - (v - lo) / (hi - lo) * (h - 6);
    i ? g.lineTo(x, y) : g.moveTo(x, y);
  });
  g.stroke();
}

// The sparkline auto-scales, so resting dither draws like a real sweep - hence
// the printed y-range and the absolute full-scale bar beside it.
function axisCard(el, c, small) {
  const twitchy = c.warn;
  el.className = 'card' + (twitchy ? ' warn' : '') + (c.idle ? ' idle' : '');
  el.querySelector('.nm').textContent =
    c.name + '   ' + c.hid + ' @ byte ' + c.byte + (c.bits === 16 ? '' : ' (8-bit)');
  el.querySelector('.big').textContent =
    (c.value === null ? '--' : c.value) + (c.volts !== null ? '  ~' + c.volts + 'V' : '');
  const full = c.lmax - c.lmin;
  const bar = el.querySelector('.bar');
  const pos = c.value === null ? 0 : (c.value - c.lmin) / full * 100;
  const seenLo = c.min === null ? 0 : (c.min - c.lmin) / full * 100;
  const seenW = c.min === null ? 0 : (c.max - c.min) / full * 100;
  bar.querySelector('u').style.left = seenLo + '%';
  bar.querySelector('u').style.width = seenW + '%';
  bar.querySelector('i').style.left = Math.max(0, pos - 0.6) + '%';
  bar.querySelector('i').style.width = '1.5%';
  el.querySelector('.s1').textContent = c.idle
    ? 'no movement seen since reset  (rests at ' + c.value + ')'
    : 'seen ' + c.min + ' .. ' + c.max + '   span ' + c.span + ' (' + c.span_pct + '%)';
  el.querySelector('.s2').innerHTML = small ? '' :
    'noise/2s: sd <span class="' + (twitchy ? 'hot' : 'ok') + '">'
    + c.jitter_sd + '</span>, ' + c.rev_per100 + ' reversals/100'
    + ' <span class="tiny">(' + c.n_recent + ' samples)</span>';
  spark(el.querySelector('canvas'), c.spark);
  const sp = c.spark || [];
  el.querySelector('.s3').textContent = sp.length > 1
    ? 'graph y-range ' + Math.min(...sp) + ' .. ' + Math.max(...sp)
      + '  (auto-scaled, span ' + (Math.max(...sp) - Math.min(...sp)) + ')'
    : 'graph: not enough samples';
}

function fill(host, list, small) {
  list.forEach((c, i) => {
    let el = host.children[i];
    if (!el) {
      el = document.createElement('div');
      el.className = 'card';
      el.innerHTML = '<div class="nm"></div><div class="big' + (small ? ' sm2' : '')
                   + '"></div><div class="bar"><u></u><i></i></div>'
                   + '<div class="sub s1"></div><div class="sub s2"></div>'
                   + '<canvas></canvas><div class="tiny s3"></div>';
      host.appendChild(el);
    }
    axisCard(el, c, small);
  });
  while (host.children.length > list.length) host.lastChild.remove();
}

const HAT_CELLS = [7, 0, 1, 6, null, 2, 5, 4, 3];   // NW N NE / W - E / SW S SE
const HAT_NAME = ['N','NE','E','SE','S','SW','W','NW'];

let SYS = null;          // last /system payload; polled far slower than /data
let LAST = null;         // last /data payload, for handlers that need it
let sysTouched = false;  // did the user open/close the panel themselves?

function esc(s) {
  return String(s).replace(/[&<>"]/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

function fixHtml(cmd) {
  return cmd ? '<code class="fix">' + esc(cmd) + '</code>' : '';
}

function renderBanner(d) {
  const b = document.getElementById('banner');
  // Silence at rest is normal, so it is a clause appended to the headline, never
  // a replacement for it - only a long freeze invalidates a test just run.
  const quiet = d.frozen
    ? '   |   NO REPORTS FOR ' + d.silent_for + 's - THIS PAGE IS FROZEN, not '
      + 'idle. The base transmits only when something changes, so a control '
      + 'that reads nothing right now proves NOTHING about that control. '
      + 'Turn the wheel to confirm the stream is alive, then retry it.'
    : (d.streaming ? '' : '   |   quiet ' + d.silent_for + 's (normal at rest)');
  let fix = null;

  if (!d.connected) {
    b.className = 'bad';
    // The system checks know which of the three causes this is.
    if (SYS && !SYS.driver_ok) {
      b.textContent = 'DRIVER NOT LOADED - the fanatec driver has not claimed '
                    + 'the base, so there is nothing to read.';
      fix = SYS.driver_fix;
    } else if (SYS && !SYS.hidraw_real) {
      b.textContent = 'NO RAW HID SOURCE - ' + SYS.hidraw_detail;
      fix = SYS.hidraw_fix;
    } else {
      b.textContent = 'DEVICE NOT CONNECTED - ' + d.dev;
    }
  } else if (d.count === 0) {
    if (SYS && !SYS.hidraw_real) {
      b.className = 'bad';
      b.textContent = 'READING THE WRONG DEVICE - the node opened, but it is '
                    + 'not the base: ' + SYS.hidraw_detail + '. It will never '
                    + 'send anything, which looks exactly like dead hardware '
                    + 'and is not.';
      fix = SYS.hidraw_fix;
    } else {
      b.className = 'idle';
      b.textContent = 'device node is open (' + d.dev + ') but the base is sending '
                    + 'NO reports. Check the base is powered on and out of standby '
                    + '- it sends nothing at all when it is off. '
                    + '(' + d.uptime + 's waiting)';
    }
  } else if (d.glitches > 0) {
    // Latched glitches stay the headline even while the base is silent: coming
    // back later to read the page is by definition a quiet moment.
    b.className = 'bad';
    b.textContent = d.glitches + ' GLITCH EVENT(S) CAUGHT - see log below   |   '
                  + d.rate + ' rep/s, ' + d.count + ' total, ' + d.uptime + 's'
                  + quiet;
  } else if (d.frozen) {
    b.className = 'idle';
    b.textContent = 'no glitches caught yet' + quiet;
  } else {
    b.className = '';
    b.textContent = 'clean - no glitches   |   ' + d.rate + ' rep/s, '
                  + d.count + ' reports, ' + d.uptime + 's up' + quiet;
  }
  document.getElementById('bannerfix').innerHTML = fixHtml(fix);
}

function renderInfo(d) {
  const w = document.getElementById('warn');
  w.style.display = d.warnings.length ? 'block' : 'none';
  w.innerHTML = d.warnings.map(x => 'LAYOUT WARNING: ' + x).join('<br>');

  const bits = [];
  bits.push('layout from ' + d.layout_src);
  bits.push('report ' + d.size + ' B');
  if (d.fw_version !== null) bits.push('fw ' + d.fw_version);
  if (d.wheel_id !== null)
    bits.push('wheel_id 0x' + d.wheel_id.toString(16).padStart(2, '0'));
  if (d.pedals !== null) bits.push('pedals ' + (d.pedals ? 'connected' : 'NOT connected'));
  if (d.handbrake !== null) bits.push('handbrake ' + (d.handbrake ? 'connected' : 'no'));
  if (d.spare) bits.push('spare bits ' + d.spare.map(x =>
      '0x' + x.toString(16).padStart(2, '0')).join(' '));
  document.getElementById('info').textContent = bits.join('   |   ');
}

function renderAxes(d) {
  const wide = d.axes.filter(a => a.bits === 16);
  const narrow = d.axes.filter(a => a.bits !== 16);
  fill(document.getElementById('chans'), wide, false);
  fill(document.getElementById('aux'), narrow, true);
}

function renderMotion(d) {
  document.getElementById('motion').innerHTML = d.motion.length
    ? d.motion.map(m => '<div class="row"><span class="nmw">' + m.name
        + '</span><span>moved ' + m.move + '  (' + m.pct
        + '% of range, byte ' + m.byte + ')</span></div>').join('')
    : '<span class="sub">nothing moving</span>';
}

function renderHat(d) {
  if (d.hat) {
    document.getElementById('hatgrid').innerHTML = HAT_CELLS.map(v => {
      if (v === null) return '<div class="h">--</div>';
      const on = d.hat.value === v;
      const ever = d.hat.ever.includes(v);
      return '<div class="h' + (on ? ' on' : ever ? ' ever' : '') + '">'
             + HAT_NAME[v] + '</div>';
    }).join('');
    document.getElementById('hatsub').textContent =
      'raw ' + d.hat.value + ' (' + d.hat.dir + ') at byte ' + d.hat.byte
      + ' low nibble   |   directions seen: '
      + (d.hat.ever.length ? d.hat.ever.map(v => HAT_NAME[v]).join(' ') : 'none');
  }
}

function renderButtons(d) {
  document.getElementById('btns').innerHTML = d.buttons.map(x =>
    '<div class="k' + (x.stuck ? ' stuck' : x.on ? ' on' : x.ever ? ' ever' : '')
    + '" title="button ' + x.n + (x.fn ? ' - ' + x.fn : '')
    + '  [byte ' + x.byte + ' bit ' + x.bit + ']  presses: ' + x.count + '">'
    + x.n + '</div>').join('');
  const seen = d.btn_seen;
  document.getElementById('btnsub').textContent =
    'seen ' + seen.length + ' of ' + d.buttons.length + ': '
    + (seen.length ? seen.join(', ') : 'none yet')
    + '   |   hover a cell for its rim function and report bit';
}

function renderEvents(d) {
  document.getElementById('events').innerHTML = d.events.length
    ? d.events.map(e =>
        '<tr><td>' + e.t + '</td><td class="hot">' + e.kind + '</td><td>'
        + e.ch + '</td><td>' + e.detail + '</td></tr>').join('')
    : '<tr><td colspan="4" class="sub">nothing caught yet</td></tr>';
}

function renderBytes(d) {
  document.getElementById('bytes').innerHTML = d.bytes.map(x =>
    '<div class="b' + (x.moved ? ' moved' : '') + (x.known ? ' known' : '')
    + '" title="byte ' + x.i + ' (' + x.label + '): seen ' + x.lo + '-' + x.hi + '">'
    + x.i + ':' + x.now + '</div>').join('');

  document.getElementById('cands').textContent = (d.undecoded || []).length
    ? 'MOVED BUT NOT DECODED: bytes ' + d.undecoded.join(', ')
      + ' - the descriptor does not claim these; something new is reporting'
    : '';

  document.getElementById('hex').textContent = d.hex;
}

/* ---- system checks ---------------------------------------------------- */

function toggleSys() {
  sysTouched = true;
  const el = document.getElementById('sys');
  el.classList.toggle('open');
  document.getElementById('syscaret').textContent =
    el.classList.contains('open') ? 'HIDE' : 'SHOW';
}

function renderSystem(s) {
  SYS = s;
  document.getElementById('sysdot').className = 'dot ' + s.overall;
  document.getElementById('syssum').textContent = s.summary;
  document.getElementById('sysbody').innerHTML = s.checks.map(c =>
    '<div class="chk"><span class="dot ' + c.status + '"></span>'
    + '<span class="lab">' + esc(c.label) + '</span>'
    + '<span class="det">' + esc(c.detail)
    + (c.why ? '<div class="why">' + esc(c.why) + '</div>' : '')
    + fixHtml(c.fix)
    + '</span></div>').join('');
  // Open when something is broken, but never fight a user who has chosen.
  if (s.overall === 'bad' && !sysTouched
      && !document.getElementById('sys').classList.contains('open')) {
    toggleSys();
    sysTouched = false;
  }
  if (LAST) renderFfb(LAST);
}

/* ---- force feedback --------------------------------------------------- */

const PHASE_TEXT = {arming: 'arming', left: 'pushing LEFT', pause: 'pause',
                    right: 'pushing RIGHT', erasing: 'erasing effect'};

function measured(label, m) {
  if (!m) return '';
  if (!m.samples) {
    return '<div><span class="nmw">' + label + '</span> <span class="hot">'
         + esc(m.note) + '</span></div>';
  }
  const good = m.moved;
  return '<div><span class="nmw">' + label + '</span> '
       + m.first + ' &rarr; ' + m.last
       + '  <b class="' + (good ? 'ok' : 'hot') + '">&Delta; ' + m.delta + '</b>'
       + '  <span class="tiny">(min ' + m.min + ', max ' + m.max + ', '
       + m.samples + ' samples of ABS_X)</span></div>';
}

function renderFfb(d) {
  const f = d.ffb, hold = document.getElementById('hold');
  document.getElementById('abort').style.display =
    f.running ? 'inline-block' : 'none';

  let stat;
  if (f.running) {
    stat = (PHASE_TEXT[f.phase] || f.phase) + '   (' + f.elapsed + 's)';
  } else if (f.phase === 'done') {
    stat = 'complete in ' + f.elapsed + 's';
  } else if (f.phase === 'aborted') {
    stat = 'ABORTED - effect erased';
  } else if (f.phase === 'failed') {
    stat = 'FAILED: ' + f.error;
  } else if (!d.ffb_enabled) {
    stat = 'disabled with --no-ffb';
  } else if (SYS && !SYS.ffb_ok) {
    stat = SYS.ffb_reason;
  } else {
    stat = 'constant force, ' + f.magnitude_pct + '% for '
         + (f.duration_ms / 1000) + 's each way - the wheel WILL move';
  }
  document.getElementById('ffbstat').textContent = stat;

  hold.disabled = !d.ffb_enabled || f.running || !(SYS && SYS.ffb_ok);

  const r = f.result || {};
  const rows = measured('LEFT', r.left) + measured('RIGHT', r.right);
  const both = r.left && r.right;
  document.getElementById('ffbres').innerHTML = rows + (
    both
      ? '<div class="tiny" style="margin-top:4px">'
        + (r.left.moved && r.right.moved
            ? 'torque confirmed in both directions - measured, not inferred'
            : 'the motor did not move the wheel measurably; check that the '
              + 'external PSU is on (USB power runs the logic only)')
        + '</div>'
      : '');
}

const HOLD_MS = 1200;
let holdRaf = null, holdStart = 0;

function holdStep() {
  const bar = document.querySelector('#hold .fillbar');
  const p = Math.min(1, (Date.now() - holdStart) / HOLD_MS);
  bar.style.width = (p * 100) + '%';
  if (p >= 1) { holdCancel(); startFfb(); return; }
  holdRaf = requestAnimationFrame(holdStep);
}

function holdBegin(ev) {
  if (document.getElementById('hold').disabled) return;
  ev.preventDefault();
  holdStart = Date.now();
  holdRaf = requestAnimationFrame(holdStep);
}

function holdCancel() {
  if (holdRaf) cancelAnimationFrame(holdRaf);
  holdRaf = null;
  document.querySelector('#hold .fillbar').style.width = '0';
}

/* ---- plumbing --------------------------------------------------------- */

async function post(path) {
  const e = document.getElementById('err');
  e.textContent = '';
  try {
    const r = await fetch(path, {method: 'POST'});
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) {
      e.textContent = j.msg || ('request failed: HTTP ' + r.status);
      return false;
    }
    return true;
  } catch (ex) {
    e.textContent = 'request failed: ' + ex;
    return false;
  }
}

async function reset() { await post('/reset'); tick(); }
async function startFfb() { await post('/ffb/start'); tick(); }
async function abortFfb() { await post('/ffb/abort'); tick(); }

async function tick() {
  let d;
  try { d = await (await fetch('/data')).json(); }
  catch (e) { return; }
  LAST = d;
  renderBanner(d);
  renderInfo(d);
  renderAxes(d);
  renderMotion(d);
  renderHat(d);
  renderButtons(d);
  renderEvents(d);
  renderBytes(d);
  renderFfb(d);
}

async function pollSystem() {
  try { renderSystem(await (await fetch('/system')).json()); }
  catch (e) { /* keep the last good state rather than blanking the strip */ }
}

const hold = document.getElementById('hold');
hold.addEventListener('pointerdown', holdBegin);
['pointerup', 'pointercancel', 'pointerleave'].forEach(
  n => hold.addEventListener(n, holdCancel));

setInterval(tick, 100);
// System state is filesystem reads, not the hot path - poll it far slower.
setInterval(pollSystem, 3000);
tick();
pollSystem();
</script>
"""


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

    def do_GET(self):
        if self.path.startswith('/data'):
            self._json(snapshot())
        elif self.path.startswith('/system'):
            self._json(sysstate.state())   # cached with its own TTL in sysstate
        elif self.path == '/':
            self._send(PAGE.encode(), 'text/html; charset=utf-8')
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
