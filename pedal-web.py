#!/usr/bin/env python3
"""Local web dashboard for the Fanatec base's raw HID stream.

A background thread reads the RAW HID reports at full rate and detects
dropouts, rail-pinning and value jumps, then LATCHES them - so an
intermittent fault lasting 20ms still shows on screen minutes later.
The page polls at 10Hz; the detection runs at full report rate.

Everything the base sends is decoded: 4 analog axes (steer / throttle / brake /
clutch), the rim ministick, slider and dial, the hat switch, all 108 button
bits and the vendor block (firmware version, wheel id, pedal presence). The
byte offsets are not guessed - they are derived from the device's own HID report
descriptor at startup (see hid_layout.py), and any mismatch against the
documented layout is shown as a warning instead of silently mislabelling a
channel.

Run:   python3 pedal-web.py
Then:  open the URL it prints (works from your phone on the same network).

Standard library only, no dependencies.
"""
import collections
import glob
import http.server
import json
import os
import select
import socket
import statistics
import threading
import time

import hid_layout

PORT = 8765

JUMP = 3000        # sample-to-sample delta that counts as a glitch (16-bit ch)
GAP = 2.0          # seconds without a report that counts as a dropout
                   # The base is SEND-ON-CHANGE: at rest it transmits nothing at
                   # all, so gaps of any length are normal and a DROPOUT is
                   # logged for the record but is NOT counted as a fault.
FROZEN = 20.0      # seconds of silence after which "I pressed it and nothing
                   # happened" stops being evidence about the control and starts
                   # being evidence about the stream. Deliberately far above GAP:
                   # a few seconds of rest must not cry wolf.
WINDOW = 2.0       # seconds for the rolling jitter stats and the motion panel
SD_WARN = 200      # rolling stdev above this = electrically noisy channel
                   # (LSB dither measures ~10; the bad throttle measured ~1380)
MOTION_MIN16 = 200 # peak-to-peak below this in WINDOW is dither, not input.
                   # Resting dither measures ~+-30 LSB (stdev ~10), so this
                   # clears it comfortably while any real input is thousands.
MOTION_MIN8 = 2
HIST_LEN = 900
SPARK = 180

# pedal channels are pot dividers off the 3.3V sensor supply, so a voltage
# readout is meaningful there; STEER is an encoder, so it is not
VOLT_CHANNELS = {'THROTTLE', 'BRAKE', 'CLUTCH'}

# Hardware button number -> what it is on a Fanatec rim. Taken from the
# ftec_keymap comments in hid-fanatec 0.2.3 (hid-ftec.c), which document the
# base's button numbering regardless of how the driver maps it to evdev codes.
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
# axis name -> [min, max] ever seen since reset. HIST is a ROLLING window kept
# for the sparkline and the jitter stats, so it cannot answer "did this channel
# ever move" - a pedal press ages out of it and the card goes back to claiming
# the channel has never moved. That is the whole point of a latching dashboard,
# so the latch gets its own storage.
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
    # The base is send-on-change: at rest it transmits NOTHING. A stream that
    # died therefore looks exactly like a control that produces no data, and
    # 'rate' freezes at its last value and keeps claiming the stream is live.
    # Track when the last report actually landed so the page can say so.
    'last_report_t': None,
}

# DROPOUT is logged but deliberately NOT a fault: the base is send-on-change, so
# every rest longer than GAP fires one. Counting them buried the real JUMP/RAIL
# catches under a glitch total made almost entirely of the rig sitting still.
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

    One function so the latch can never be updated in one code path and missed
    in another - the sparkline forgetting a pedal press is survivable, the latch
    forgetting one is the bug this dashboard exists to not have.
    """
    if val is None:                 # report too short for this axis - one place
        return                      # to handle it, so no caller can seed a
    HIST[name].append((now, val))   # [None, None] latch and crash the next one
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

    Mirrors ftecff_raw_event() in hid-ftecff.c, shifted by one: the driver
    expects a NUMBERED 34-byte report (data[0] == 0x01) while this base sends
    33 bytes with no report id, which is exactly why its sysfs wheel_id,
    fw_version and tuning values all stay 0.
    """
    out = {}
    if len(rep) != 33:
        # these offsets are only meaningful for this base's report shape
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
    # A bit that was already high in the very first report we saw was never
    # observed going down, which is what "stuck on" means. Distinguish that from
    # a button the user is simply holding, which we watched go down.
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
            node = glob.glob('/dev/input/by-id/usb-Fanatec_*-hidraw')[0]
            path = os.path.realpath(node)
            layout = hid_layout.layout_for(node)
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

                            # only log ENTERING a rail; a channel that simply
                            # rests at a rail must not spam the log
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
        # A frozen 'rate' from a stream that stopped minutes ago is worse than
        # no number at all - it is what made a silent base read as "the brake
        # card is broken". Report the silence explicitly and zero the rate.
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

            # Motion attribution: what actually responded in the last WINDOW.
            # Wiggle one control and only that control should appear here - this
            # is how you tell a real cross-channel link from a sparkline that
            # merely auto-scaled some resting LSB dither into a big wiggle.
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
                    # high since the first report we ever saw, i.e. never
                    # observed going down -> likely shorted
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
</style>

<h1>Fanatec wheel/pedal diagnostics</h1>
<div id="banner">waiting for data...</div>
<div id="warn"></div>
<div id="info"></div>
<button onclick="reset()">reset stats &amp; event log</button>

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

// The sparkline auto-scales, so a channel resting with +-30 LSB of ADC dither
// draws the same dramatic wiggle as a real sweep. Always print the y-range
// next to it, and show an absolute full-scale bar, so it cannot mislead.
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

async function tick() {
  let d;
  try { d = await (await fetch('/data')).json(); }
  catch (e) { return; }

  const b = document.getElementById('banner');
  // The base is send-on-change, so silence at rest is NORMAL and must not cry
  // wolf. Silence is a clause appended to whatever the headline is, never a
  // replacement for it - only a long freeze invalidates a test you just ran.
  const quiet = d.frozen
    ? '   |   NO REPORTS FOR ' + d.silent_for + 's - THIS PAGE IS FROZEN, not '
      + 'idle. The base transmits only when something changes, so a control '
      + 'that reads nothing right now proves NOTHING about that control. '
      + 'Turn the wheel to confirm the stream is alive, then retry it.'
    : (d.streaming ? '' : '   |   quiet ' + d.silent_for + 's (normal at rest)');

  if (!d.connected) {
    b.className = 'bad';
    b.textContent = 'DEVICE NOT CONNECTED - ' + d.dev;
  } else if (d.count === 0) {
    b.className = 'idle';
    b.textContent = 'device node is open (' + d.dev + ') but the base is sending '
                  + 'NO reports. Check the base is powered on and out of standby '
                  + '- it sends nothing at all when it is off. '
                  + '(' + d.uptime + 's waiting)';
  } else if (d.glitches > 0) {
    // Latched glitches stay the headline even while the base is silent - that
    // is the whole point of latching, and "come back later and read the page"
    // is by definition a moment when nothing is moving.
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

  const wide = d.axes.filter(a => a.bits === 16);
  const narrow = d.axes.filter(a => a.bits !== 16);
  fill(document.getElementById('chans'), wide, false);
  fill(document.getElementById('aux'), narrow, true);

  document.getElementById('motion').innerHTML = d.motion.length
    ? d.motion.map(m => '<div class="row"><span class="nmw">' + m.name
        + '</span><span>moved ' + m.move + '  (' + m.pct
        + '% of range, byte ' + m.byte + ')</span></div>').join('')
    : '<span class="sub">nothing moving</span>';

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

  document.getElementById('events').innerHTML = d.events.length
    ? d.events.map(e =>
        '<tr><td>' + e.t + '</td><td class="hot">' + e.kind + '</td><td>'
        + e.ch + '</td><td>' + e.detail + '</td></tr>').join('')
    : '<tr><td colspan="4" class="sub">nothing caught yet</td></tr>';

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

async function reset() { await fetch('/reset', {method: 'POST'}); tick(); }
setInterval(tick, 100);
tick();
</script>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith('/data'):
            self._send(json.dumps(snapshot()).encode(), 'application/json')
        elif self.path == '/':
            self._send(PAGE.encode(), 'text/html; charset=utf-8')
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == '/reset':
            with LOCK:
                reset_tracking()
            self._send(b'{"ok":true}', 'application/json')
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
    threading.Thread(target=reader, daemon=True).start()
    srv = http.server.ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    ip = lan_ip()
    # flush explicitly: stdout is block-buffered when this is redirected or
    # backgrounded, which would hide the URLs until the process exits
    print(f'  local:  http://localhost:{PORT}', flush=True)
    if ip:
        print(f'  phone:  http://{ip}:{PORT}', flush=True)
    print('\n  Ctrl-C to stop', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\nstopped')
