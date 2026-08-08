#!/usr/bin/env python3
"""Self-test for the dashboard - runs with the base powered OFF.

Replays the real reports archived in data/raw-pedal-map.log through
hid_layout.py, sysstate.py and pedal-web.py's decoders and asserts the result.
This exists because every "the dashboard does not show my clutch / my buttons"
bug in this project came from an unverified byte offset, and the base only
streams while someone is physically at the rig - so without this, changing a
decoder means going and sitting in the seat to find out what broke.

If /dev/hidraw for the base is readable the layout comes from the device's own
report descriptor; otherwise the hardcoded CSL Elite fallback is tested.

Nothing here opens a device for I/O and nothing here can move the wheel.

Run:  python3 tools/selftest-decode.py
"""
import importlib.util
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ffb                                                 # noqa: E402
import hid_layout                                          # noqa: E402
import sysstate                                            # noqa: E402

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pw = _load('pedal_web', os.path.join(ROOT, 'pedal-web.py'))

FAILS = []


def ck(label, got, want):
    ok = got == want
    if not ok:
        FAILS.append(f'{label}: got {got!r}, want {want!r}')
    print(('  ok   ' if ok else '  FAIL ') + f'{label} = {got!r}'
          + ('' if ok else f'   want {want!r}'))


def hx(s):
    return bytes(int(x, 16) for x in s.split())


CAPTURE = os.path.join(ROOT, 'data', 'raw-pedal-map.log')


def load_phases(path):
    """{phase name: first raw report} from an archived labelled capture.

    Read at runtime rather than pasted in as hex. These bytes used to be copied
    out of this log by hand, which meant the log and the fixture could drift
    apart silently and the test would keep asserting bytes nothing produces any
    more. If the evidence file goes missing the test now fails loudly, which is
    the correct outcome: the archived capture IS the fixture.
    """
    phases, name = {}, None
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line.startswith('PHASE ') and '-' in line:
                name = line.split('-', 1)[1].strip()
            elif line.startswith('first:') and name:
                phases[name] = hx(line.split(':', 1)[1])
    return phases


PHASES = load_phases(CAPTURE)
# Nothing but the control named in each phase was touched during the capture.
STEER_PHASE = PHASES['STEERING']
THR_PHASE = PHASES['THROTTLE']
BRK_PHASE = PHASES['BRAKE']


def get_layout():
    node = hid_layout.find_nodes()['hidraw']
    if node and os.access(node['path'], os.R_OK):
        return hid_layout.layout_for(node['link']), 'live device descriptor'
    return hid_layout.fallback_layout('no readable base'), 'hardcoded fallback'


def main():
    layout, how = get_layout()
    print(f'layout from: {how}  ({layout["source"]})')

    print('\n[1] layout matches the documented report map')
    ck('report size', layout['size'], hid_layout.KNOWN_SIZE)
    for name, byte in sorted(hid_layout.KNOWN_OFFSETS.items(),
                             key=lambda kv: kv[1]):
        got = next((a['byte'] for a in layout['axes'] if a['name'] == name), None)
        ck(f'{name:<8} byte', got, byte)
    ck('button count', layout['buttons']['count'], 108)
    ck('first button bit', layout['buttons']['first_bit'], 4)
    ck('hat at byte 0 low nibble',
       (layout['hat']['byte'], layout['hat']['shift']), (0, 0))
    if how == 'live device descriptor':
        ck('no layout warnings', layout['warnings'], [])

    by = {a['name']: a for a in layout['axes']}

    print('\n[2] axis decode of the archived steering-phase report')
    ck('STEER    centred', pw.axis_value(STEER_PHASE, by['STEER']), 32783)
    ck('THROTTLE at rest', pw.axis_value(STEER_PHASE, by['THROTTLE']), 65535)
    ck('BRAKE    at rest', pw.axis_value(STEER_PHASE, by['BRAKE']), 65535)
    ck('CLUTCH   at rest', pw.axis_value(STEER_PHASE, by['CLUTCH']), 65535)
    ck('STICK-X  centred', pw.axis_value(STEER_PHASE, by['STICK-X']), 0)
    ck('STICK-Y  centred', pw.axis_value(STEER_PHASE, by['STICK-Y']), 0)
    ck('SLIDER', pw.axis_value(STEER_PHASE, by['SLIDER']), 255)
    ck('DIAL signed', pw.axis_value(STEER_PHASE, by['DIAL']), -4)

    print('\n[3] each capture phase moves only its own channel')
    ck('throttle phase -> Z moved', pw.axis_value(THR_PHASE, by['THROTTLE']), 65337)
    ck('throttle phase -> Rz still at rest',
       pw.axis_value(THR_PHASE, by['BRAKE']), 65535)
    ck('brake phase -> Rz moved', pw.axis_value(BRK_PHASE, by['BRAKE']), 65407)
    ck('brake phase -> Z still at rest',
       pw.axis_value(BRK_PHASE, by['THROTTLE']), 65535)

    print('\n[4] vendor block - independent confirmation of the offsets')
    info = pw.decode_vendor(STEER_PHASE)
    # 693 is what the base shows on its own display at boot, and its bcdDevice
    ck('fw_version', info.get('fw_version'), 693)
    ck('wheel_id', info.get('wheel_id'), 0x20)
    variant = bytearray(STEER_PHASE)
    variant[29], variant[30], variant[31] = 0xff, 0x04, 0x11
    ck('pedal-presence variant', pw.decode_vendor(bytes(variant)),
       {'pedals': True, 'handbrake': True})

    print('\n[5] button bit math')
    spec = layout['buttons']
    collisions = []
    for n in range(spec['first_usage'], spec['first_usage'] + spec['count']):
        bit = spec['first_bit'] + n - spec['first_usage']
        rep = bytearray(hid_layout.KNOWN_SIZE)
        rep[bit // 8] |= 1 << (bit % 8)
        if pw.button_mask(bytes(rep), spec) != 1 << (n - spec['first_usage']):
            collisions.append(n)
    ck('all buttons decode to their own bit', collisions, [])
    b5 = spec['first_bit'] + 5 - spec['first_usage']
    ck('button 5 (GEAR UP) at byte 1 bit 0', (b5 // 8, b5 % 8), (1, 0))
    ck('button 108 at byte 13 bit 7',
       ((spec['first_bit'] + 107) // 8, (spec['first_bit'] + 107) % 8), (13, 7))
    # byte 15 rests at a constant 0x16 on this base; it is inside the declared
    # button block but is not a button, so it must never light a cell up
    ck('no phantom buttons from byte 15',
       pw.button_mask(STEER_PHASE, spec), 0)
    ck('spare bits are bytes 14-15',
       sorted({b // 8 for b in layout['spare_bits']}), [14, 15])

    print('\n[6] full pipeline: synthesised clutch sweep + gear-up + hat')
    pw.install_layout(layout)
    pw.reset_tracking()
    now = time.time()
    sweep = list(range(65535, 20000, -4000))
    stream = [STEER_PHASE] * 4
    for val in sweep:
        rep = bytearray(STEER_PHASE)
        rep[22], rep[23] = val & 0xff, val >> 8
        stream.append(bytes(rep))
    gear = bytearray(STEER_PHASE)
    gear[1] |= 0x01                                  # button 5 = GEAR UP
    stream.append(bytes(gear))
    stream.append(STEER_PHASE)
    hat = bytearray(STEER_PHASE)
    hat[0] = (STEER_PHASE[0] & 0xf0) | 2             # hat = E
    stream.append(bytes(hat))

    for rep in stream:
        pw.STATE['count'] += 1
        pw.STATE['report'] = rep
        pw.STATE['size'] = len(rep)
        if pw.STATE['lo'] is None:
            pw.STATE['lo'], pw.STATE['hi'] = list(rep), list(rep)
        else:
            for i, b in enumerate(rep):
                pw.STATE['lo'][i] = min(pw.STATE['lo'][i], b)
                pw.STATE['hi'][i] = max(pw.STATE['hi'][i], b)
        for ax in layout['axes']:
            val = pw.axis_value(rep, ax)
            if val is not None:
                pw.note_axis(ax['name'], val, now)
        pw.note_buttons(pw.button_mask(rep, spec), spec, now)
        hv = rep[layout['hat']['byte']] >> layout['hat']['shift'] & 0x0f
        if hv != pw.HAT['value']:
            pw.HAT['value'] = hv
            if hv <= layout['hat']['lmax']:
                pw.HAT['ever'].add(hv)
        for key, val in pw.decode_vendor(rep).items():
            pw.STATE[key] = val

    snap = pw.snapshot()
    axes = {a['name']: a for a in snap['axes']}
    ck('CLUTCH no longer flagged idle', axes['CLUTCH']['idle'], False)
    ck('CLUTCH span', axes['CLUTCH']['span'], max(sweep) - min(sweep))
    ck('THROTTLE flagged idle', axes['THROTTLE']['idle'], True)
    ck('motion panel blames CLUTCH alone',
       [m['name'] for m in snap['motion']], ['CLUTCH'])
    ck('button 5 latched as seen', 5 in snap['btn_seen'], True)
    ck('button 6 still never seen', 6 in snap['btn_seen'], False)
    ck('BTN-NEW logged for button 5',
       any(e['kind'] == 'BTN-NEW' and e['ch'] == 'btn 5' for e in snap['events']),
       True)
    ck('a button we watched go down is NOT called stuck',
       next(b['stuck'] for b in snap['buttons'] if b['n'] == 5), False)
    ck('hat reads E', snap['hat']['dir'], 'E')
    ck('hat E latched', snap['hat']['ever'], [2])
    ck('fw surfaced to the page', snap['fw_version'], 693)
    ck('no moved-but-undecoded bytes', snap['undecoded'], [])
    ck('byte 22 labelled', snap['bytes'][22]['label'], 'CLUTCH')
    ck('byte 15 labelled', snap['bytes'][15]['label'], 'spare button bits')
    ck('button cells rendered', len(snap['buttons']), spec['count'])
    json.dumps(snap)
    print('  ok   snapshot is JSON-serialisable')

    # THE LATCH MUST OUTLIVE THE SPARKLINE WINDOW. HIST is a rolling deque, so
    # min/max taken from it silently forget a pedal press once HIST_LEN further
    # reports arrive - the card goes back to "no movement seen since reset" and
    # a channel that demonstrably worked reads as dead. This is exactly the
    # false negative that got the brake reported as broken.
    pw.reset_tracking()
    pw.install_layout(layout)
    brake = next(a for a in layout['axes'] if a['name'] == 'BRAKE')
    rest = pw.axis_value(STEER_PHASE, brake)
    pressed = bytearray(STEER_PHASE)
    pressed[brake['byte']], pressed[brake['byte'] + 1] = 0x39, 0x30   # 12345
    for rep in (bytes(pressed),) + (STEER_PHASE,) * (pw.HIST_LEN + 50):
        pw.STATE['count'] += 1
        pw.STATE['report'] = rep
        for ax in layout['axes']:
            pw.note_axis(ax['name'], pw.axis_value(rep, ax), now)
    pw.STATE['lo'] = pw.STATE['hi'] = list(STEER_PHASE)
    aged = {a['name']: a for a in pw.snapshot()['axes']}['BRAKE']
    ck('a press that aged out of the sparkline is still latched',
       aged['min'], 12345)
    ck('and the channel is NOT called idle', aged['idle'], False)
    ck('the press is gone from the sparkline, as expected',
       min(aged['spark']), rest)
    # a truncated report must not seed a [None, None] latch that crashes the
    # next sample - note_axis is the single place that decides this
    pw.note_axis('BRAKE', None, now)
    pw.note_axis('BRAKE', 500, now)
    ck('a short report does not poison the latch', pw.SEEN['BRAKE'][0], 500)

    pw.reset_tracking()
    pw.install_layout(layout)
    ck('reset clears the latch', pw.SEEN, {})

    # A base that stopped transmitting must never keep advertising the rate it
    # had when it died: that reads as a live stream, so "my pedal shows
    # nothing" gets blamed on the pedal instead of on the silence.
    pw.STATE['rate'] = 138.5
    pw.STATE['last_report_t'] = time.time() - 30.0
    stale = pw.snapshot()
    ck('a dead stream is not called streaming', stale['streaming'], False)
    ck('a dead stream advertises no rate', stale['rate'], 0.0)
    ck('a dead stream reports how long it has been silent',
       stale['silent_for'] >= 29.0, True)
    ck('30s of silence is frozen', stale['frozen'], True)
    pw.STATE['last_report_t'] = time.time()
    ck('a live stream keeps its rate', pw.snapshot()['rate'], 138.5)

    # A few seconds of rest is NORMAL on a send-on-change base. If that tripped
    # the frozen warning the page would cry wolf constantly and get ignored -
    # which is exactly how the real warning would be missed.
    pw.STATE['last_report_t'] = time.time() - (pw.GAP + 1.0)
    resting = pw.snapshot()
    ck('a few seconds of rest is not frozen', resting['frozen'], False)
    ck('but it is honest that no reports are arriving', resting['rate'], 0.0)

    # DROPOUT fires every time the rig sits still, so counting it as a fault
    # buries the JUMP/RAIL catches the dashboard exists to find.
    before = pw.STATE['glitches']
    pw.event('DROPOUT', '-', 'rig sat still')
    ck('a dropout is not counted as a glitch', pw.STATE['glitches'], before)
    pw.event('RAIL', 'BRAKE', 'went to MAX')
    ck('a rail still is', pw.STATE['glitches'], before + 1)
    pw.STATE['glitches'] = before

    # a bit already high in the very first report was never seen going down
    pw.reset_tracking()
    held = bytearray(STEER_PHASE)
    held[1] |= 0x02                                  # button 6 high from the off
    pw.STATE['count'] = 1
    pw.STATE['report'] = bytes(held)
    pw.STATE['lo'] = pw.STATE['hi'] = list(held)
    pw.note_buttons(pw.button_mask(bytes(held), spec), spec, now)
    ck('a bit high in the first report IS called stuck',
       next(b['stuck'] for b in pw.snapshot()['buttons'] if b['n'] == 6), True)

    print('\n[7] byte accounting and the jitter maths')
    labels = pw.byte_labels(layout)
    ck('every report byte is claimed by some field',
       sorted(set(range(layout['size'])) - set(labels)), [])
    ck('byte 22 is CLUTCH', labels[22], 'CLUTCH')
    ck('byte 0 carries the hat and the first buttons', labels[0], 'hat + buttons')
    ck('the vendor block is claimed',
       {labels[b] for b in layout['vendor']}, {'vendor'})

    # If a byte starts moving that the descriptor does not claim, something new
    # is reporting and the page has to say so - it is how an undocumented field
    # would ever be noticed at all.
    orphan = layout['vendor'][-1]
    pw.reset_tracking()
    pw.install_layout(dict(layout, vendor=layout['vendor'][:-1]))
    pw.STATE['lo'] = list(STEER_PHASE)
    pw.STATE['hi'] = list(STEER_PHASE)
    pw.STATE['hi'][orphan] ^= 0x01
    pw.STATE['report'] = STEER_PHASE
    ck('a byte that moves and nothing claims is flagged',
       orphan in pw.snapshot()['undecoded'], True)
    pw.reset_tracking()
    pw.install_layout(layout)

    # Reversal count is what separated the healthy brake (11 in 417 samples)
    # from the faulty throttle (248 in 401 while held motionless).
    ck('a clean sweep never reverses', pw.reversals([0, 10, 20, 30]), 0)
    ck('one direction change is one reversal', pw.reversals([0, 10, 5]), 1)
    ck('resting dither reverses on nearly every sample',
       pw.reversals([100, 101, 100, 101, 100]), 3)
    ck('a flat channel never reverses', pw.reversals([5, 5, 5, 5]), 0)

    print('\n[8] system checks (sysstate.py)')
    st = sysstate.state(force=True)
    ck('every check is fully formed',
       all(set(c) == {'id', 'label', 'status', 'detail', 'fix', 'why'}
           for c in st['checks']), True)
    ck('every status is one of the four',
       sorted({c['status'] for c in st['checks']}
              - {'ok', 'warn', 'bad', 'unknown'}), [])
    ck('overall is the worst check present', sysstate.RANK[st['overall']],
       max(sysstate.RANK[c['status']] for c in st['checks']))
    # A failing check that does not tell you what to do about it is just an
    # alarm, which is the thing this whole panel exists to stop being.
    ck('every failing check offers a fix or a reason',
       all(c['fix'] or c['why'] for c in st['checks'] if c['status'] == 'bad'),
       True)
    ck('the summary is JSON-serialisable', bool(json.dumps(st)), True)
    # The bitmask this base actually reports, per docs/FINDINGS.md.
    ck('the FF bitmask decodes to the 12 effects FINDINGS recorded',
       sysstate.decode_ff('1f7f0000 0'),
       ['RUMBLE', 'PERIODIC', 'CONSTANT', 'SPRING', 'FRICTION', 'DAMPER',
        'INERTIA', 'SQUARE', 'TRIANGLE', 'SINE', 'SAW_UP', 'SAW_DOWN'])
    ck('RAMP and CUSTOM are correctly absent',
       {'RAMP', 'CUSTOM'} & set(sysstate.decode_ff('1f7f0000 0')), set())
    ck('an empty bitmask means no force feedback', sysstate.decode_ff('0'), [])

    print('\n[9] force-feedback control (ffb.py - no device is touched)')
    fst = ffb.status()
    ck('starts idle', fst['phase'], 'idle')
    ck('nothing running', fst['running'], False)
    ck('magnitude stays gentle', fst['magnitude_pct'] <= 30, True)
    ck('each push is bounded in time', fst['duration_ms'] <= 2000, True)
    ck('the effect carries its own stop', ffb.DURATION_MS > 0, True)
    ck('aborting with nothing running is a no-op', ffb.abort()[0], False)
    ck('status is JSON-serialisable', bool(json.dumps(fst)), True)

    # Single flight, checked BEFORE the device is looked at: a double-tap on a
    # phone must not stack two effect uploads onto the same slot.
    with ffb._LOCK:
        ffb._STATE['running'] = True
    try:
        ok, msg = ffb.start()
    finally:
        with ffb._LOCK:
            ffb._STATE['running'] = False
    ck('refuses to start a second test', ok, False)
    ck('and says one is already running', 'already running' in msg, True)

    class _NoDevice:
        @staticmethod
        def node_path(_kind):
            return None

    class _Unwritable:
        @staticmethod
        def node_path(_kind):
            return '/nonexistent/fanatec-event-node'

    real = ffb.hid_layout
    try:
        ffb.hid_layout = _NoDevice
        ok, msg = ffb.start()
        ck('refuses to start with no event node', ok, False)
        ck('and says so', 'no Fanatec event node' in msg, True)
        ffb.hid_layout = _Unwritable
        ok, msg = ffb.start()
        ck('refuses to start when the node is not writable', ok, False)
        ck('and names the permission problem', 'not writable' in msg, True)
    finally:
        ffb.hid_layout = real
    ck('a refused start leaves it idle', ffb.status()['running'], False)

    print()
    if FAILS:
        print(f'{len(FAILS)} FAILURE(S):')
        for f in FAILS:
            print('  ' + f)
        return 1
    print('ALL CHECKS PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
