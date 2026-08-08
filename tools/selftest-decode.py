#!/usr/bin/env python3
"""Self-test for the dashboard - runs with the base powered OFF.

Replays the reports archived in data/raw-pedal-map.log through hid_layout.py,
decode.py, tracker.py and sysstate.py and asserts the result, so a decoder can
be changed without sitting at the rig to find out what broke. The layout comes
from the device's descriptor if hidraw is readable, else from the fallback.

Reports go in through Tracker.ingest(), the same call the reader thread makes,
so this exercises the production path rather than a copy of it.

Nothing here opens a device for I/O and nothing here can move the wheel.

Run:  python3 tools/selftest-decode.py
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import decode                                              # noqa: E402
import ffb                                                 # noqa: E402
import hid_layout                                          # noqa: E402
import sysstate                                            # noqa: E402
import tracker                                             # noqa: E402

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

    Read at runtime, not pasted in as hex, so the log and the fixture cannot
    drift apart: the archived capture IS the fixture.
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
    ck('STEER    centred', decode.axis_value(STEER_PHASE, by['STEER']), 32783)
    ck('THROTTLE at rest', decode.axis_value(STEER_PHASE, by['THROTTLE']), 65535)
    ck('BRAKE    at rest', decode.axis_value(STEER_PHASE, by['BRAKE']), 65535)
    ck('CLUTCH   at rest', decode.axis_value(STEER_PHASE, by['CLUTCH']), 65535)
    ck('STICK-X  centred', decode.axis_value(STEER_PHASE, by['STICK-X']), 0)
    ck('STICK-Y  centred', decode.axis_value(STEER_PHASE, by['STICK-Y']), 0)
    ck('SLIDER', decode.axis_value(STEER_PHASE, by['SLIDER']), 255)
    ck('DIAL signed', decode.axis_value(STEER_PHASE, by['DIAL']), -4)

    print('\n[3] each capture phase moves only its own channel')
    ck('throttle phase -> Z moved', decode.axis_value(THR_PHASE, by['THROTTLE']), 65337)
    ck('throttle phase -> Rz still at rest',
       decode.axis_value(THR_PHASE, by['BRAKE']), 65535)
    ck('brake phase -> Rz moved', decode.axis_value(BRK_PHASE, by['BRAKE']), 65407)
    ck('brake phase -> Z still at rest',
       decode.axis_value(BRK_PHASE, by['THROTTLE']), 65535)

    print('\n[4] vendor block - independent confirmation of the offsets')
    info = decode.decode_vendor(STEER_PHASE)
    # 693 is what the base shows on its own display at boot, and its bcdDevice.
    ck('fw_version', info.get('fw_version'), 693)
    ck('wheel_id', info.get('wheel_id'), 0x20)
    variant = bytearray(STEER_PHASE)
    variant[29], variant[30], variant[31] = 0xff, 0x04, 0x11
    ck('pedal-presence variant', decode.decode_vendor(bytes(variant)),
       {'pedals': True, 'handbrake': True})

    print('\n[5] button bit math')
    spec = layout['buttons']
    collisions = []
    for n in range(spec['first_usage'], spec['first_usage'] + spec['count']):
        bit = spec['first_bit'] + n - spec['first_usage']
        rep = bytearray(hid_layout.KNOWN_SIZE)
        rep[bit // 8] |= 1 << (bit % 8)
        if decode.button_mask(bytes(rep), spec) != 1 << (n - spec['first_usage']):
            collisions.append(n)
    ck('all buttons decode to their own bit', collisions, [])
    b5 = spec['first_bit'] + 5 - spec['first_usage']
    ck('button 5 (GEAR UP) at byte 1 bit 0', (b5 // 8, b5 % 8), (1, 0))
    ck('button 108 at byte 13 bit 7',
       ((spec['first_bit'] + 107) // 8, (spec['first_bit'] + 107) % 8), (13, 7))
    # byte 15 rests at a constant 0x16 inside the declared button block.
    ck('no phantom buttons from byte 15',
       decode.button_mask(STEER_PHASE, spec), 0)
    ck('spare bits are bytes 14-15',
       sorted({b // 8 for b in layout['spare_bits']}), [14, 15])

    print('\n[6] full pipeline: synthesised clutch sweep + gear-up + hat')
    # Reports go in through the same call the reader thread makes, so this
    # exercises the shipping path instead of a second copy of it.
    track = tracker.Tracker(layout)
    now = time.time()
    clutch = by['CLUTCH']['byte']
    sweep = list(range(65535, 20000, -4000))
    stream = [STEER_PHASE] * 4
    for val in sweep:
        rep = bytearray(STEER_PHASE)
        rep[clutch], rep[clutch + 1] = val & 0xff, val >> 8
        stream.append(bytes(rep))
    gear = bytearray(STEER_PHASE)
    gear[1] |= 0x01                                  # button 5 = GEAR UP
    stream.append(bytes(gear))
    stream.append(STEER_PHASE)
    hat = bytearray(STEER_PHASE)
    hat[0] = (STEER_PHASE[0] & 0xf0) | 2             # hat = E
    stream.append(bytes(hat))

    for rep in stream:
        track.ingest(rep, now)

    snap = track.snapshot()
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

    # The latch must outlive the rolling sparkline window, or a channel that
    # demonstrably worked reads as dead once HIST_LEN further reports arrive.
    track = tracker.Tracker(layout)
    brake = by['BRAKE']
    rest = decode.axis_value(STEER_PHASE, brake)
    pressed = bytearray(STEER_PHASE)
    pressed[brake['byte']], pressed[brake['byte'] + 1] = 0x39, 0x30   # 12345
    for rep in (bytes(pressed),) + (STEER_PHASE,) * (tracker.HIST_LEN + 50):
        track.ingest(rep, now)
    aged = {a['name']: a for a in track.snapshot()['axes']}['BRAKE']
    ck('a press that aged out of the sparkline is still latched',
       aged['min'], 12345)
    ck('and the channel is NOT called idle', aged['idle'], False)
    ck('the press is gone from the sparkline, as expected',
       min(aged['spark']), rest)

    # A report too short to carry BRAKE must skip it, not seed a [None, None]
    # latch that crashes the comparison on the next sample.
    short = tracker.Tracker(layout)
    short.ingest(STEER_PHASE[:brake['byte']], now)
    short.ingest(STEER_PHASE, now)
    ck('a short report does not poison the latch', short.seen['BRAKE'][0], rest)

    track.reset()
    ck('reset clears the latch', track.seen, {})

    # A stream that died must not keep advertising its old rate, or the silence
    # gets blamed on the pedal.
    track.rate = 138.5
    track.last_report_t = time.time() - 30.0
    stale = track.snapshot()
    ck('a dead stream is not called streaming', stale['streaming'], False)
    ck('a dead stream advertises no rate', stale['rate'], 0.0)
    ck('a dead stream reports how long it has been silent',
       stale['silent_for'] >= 29.0, True)
    ck('30s of silence is frozen', stale['frozen'], True)
    track.last_report_t = time.time()
    ck('a live stream keeps its rate', track.snapshot()['rate'], 138.5)

    # A few seconds of rest is normal here; crying wolf would get the real
    # warning ignored.
    track.last_report_t = time.time() - (tracker.GAP + 1.0)
    resting = track.snapshot()
    ck('a few seconds of rest is not frozen', resting['frozen'], False)
    ck('but it is honest that no reports are arriving', resting['rate'], 0.0)

    # DROPOUT fires whenever the rig sits still, so it must not bury JUMP/RAIL.
    before = track.glitches
    track.log('DROPOUT', '-', 'rig sat still')
    ck('a dropout is not counted as a glitch', track.glitches, before)
    track.log('RAIL', 'BRAKE', 'went to MAX')
    ck('a rail still is', track.glitches, before + 1)

    # The silence-then-return cycle: one DROPOUT per gap however long the reader
    # spins, then exactly one RESUMED when a report finally lands.
    gapped = tracker.Tracker(layout)
    gapped.ingest(STEER_PHASE, now)
    quiet = now + tracker.GAP + 1.0
    for _ in range(5):
        gapped.note_idle(quiet)
    kinds = [e['kind'] for e in gapped.snapshot()['events']]
    ck('a gap logs one DROPOUT, not one per poll', kinds.count('DROPOUT'), 1)
    gapped.note_idle(now + 0.1)          # inside GAP - nothing to report
    ck('a gap shorter than GAP logs nothing',
       [e['kind'] for e in gapped.snapshot()['events']].count('DROPOUT'), 1)
    gapped.ingest(STEER_PHASE, quiet)
    kinds = [e['kind'] for e in gapped.snapshot()['events']]
    ck('the stream returning logs RESUMED once', kinds.count('RESUMED'), 1)
    ck('and neither counts as a fault', gapped.glitches, 0)

    # a bit already high in the very first report was never seen going down
    stuck = tracker.Tracker(layout)
    held = bytearray(STEER_PHASE)
    held[1] |= 0x02                                  # button 6 high from the off
    stuck.ingest(bytes(held), now)
    ck('a bit high in the first report IS called stuck',
       next(b['stuck'] for b in stuck.snapshot()['buttons'] if b['n'] == 6), True)

    print('\n[7] byte accounting and the jitter maths')
    labels = decode.byte_labels(layout)
    ck('every report byte is claimed by some field',
       sorted(set(range(layout['size'])) - set(labels)), [])
    ck('byte 22 is CLUTCH', labels[22], 'CLUTCH')
    ck('byte 0 carries the hat and the first buttons', labels[0], 'hat + buttons')
    ck('the vendor block is claimed',
       {labels[b] for b in layout['vendor']}, {'vendor'})

    # A byte moving that the descriptor does not claim is how an undocumented
    # field would ever get noticed.
    orphan = layout['vendor'][-1]
    blind = tracker.Tracker(dict(layout, vendor=layout['vendor'][:-1]))
    moved = bytearray(STEER_PHASE)
    moved[orphan] ^= 0x01
    blind.ingest(STEER_PHASE, now)
    blind.ingest(bytes(moved), now)
    ck('a byte that moves and nothing claims is flagged',
       orphan in blind.snapshot()['undecoded'], True)

    # Reversal count separated the healthy brake from the faulty throttle.
    ck('a clean sweep never reverses', decode.reversals([0, 10, 20, 30]), 0)
    ck('one direction change is one reversal', decode.reversals([0, 10, 5]), 1)
    ck('resting dither reverses on nearly every sample',
       decode.reversals([100, 101, 100, 101, 100]), 3)
    ck('a flat channel never reverses', decode.reversals([5, 5, 5, 5]), 0)

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
    # A failing check with no fix is just an alarm.
    ck('every failing check offers a fix or a reason',
       all(c['fix'] or c['why'] for c in st['checks'] if c['status'] == 'bad'),
       True)
    ck('the summary is JSON-serialisable', bool(json.dumps(st)), True)
    # The bitmask this base actually reports, per docs/driver.md.
    ck('the FF bitmask decodes to the 12 effects docs/driver.md records',
       sysstate.decode_ff('1f7f0000 0'),
       ['RUMBLE', 'PERIODIC', 'CONSTANT', 'SPRING', 'FRICTION', 'DAMPER',
        'INERTIA', 'SQUARE', 'TRIANGLE', 'SINE', 'SAW_UP', 'SAW_DOWN'])
    ck('RAMP and CUSTOM are correctly absent',
       {'RAMP', 'CUSTOM'} & set(sysstate.decode_ff('1f7f0000 0')), set())
    ck('an empty bitmask means no force feedback', sysstate.decode_ff('0'), [])

    print('\n[9] force-feedback control (ffb.py - no device is touched)')
    # Every instance here is handed a node lookup that resolves to nothing real,
    # so start() cannot reach a device however far it gets.
    test = ffb.FfbTest(find_node=lambda: None)
    fst = test.status()
    ck('starts idle', fst['phase'], 'idle')
    ck('nothing running', fst['running'], False)
    ck('magnitude stays gentle', fst['magnitude_pct'] <= 30, True)
    ck('each push is bounded in time', fst['duration_ms'] <= 2000, True)
    ck('the effect carries its own stop', ffb.DURATION_MS > 0, True)
    ck('aborting with nothing running is a no-op', test.abort()[0], False)
    ck('status is JSON-serialisable', bool(json.dumps(fst)), True)

    # Single flight, checked before the device is looked at.
    busy = ffb.FfbTest(find_node=lambda: None)
    busy.running = True
    ok, msg = busy.start()
    ck('refuses to start a second test', ok, False)
    ck('and says one is already running', 'already running' in msg, True)

    ok, msg = test.start()
    ck('refuses to start with no event node', ok, False)
    ck('and says so', 'no Fanatec event node' in msg, True)

    unwritable = ffb.FfbTest(find_node=lambda: '/nonexistent/fanatec-event-node')
    ok, msg = unwritable.start()
    ck('refuses to start when the node is not writable', ok, False)
    ck('and names the permission problem', 'not writable' in msg, True)
    ck('a refused start leaves it idle', test.status()['running'], False)

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
