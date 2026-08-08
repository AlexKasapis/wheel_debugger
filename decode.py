#!/usr/bin/env python3
"""Pure decoders: raw report bytes in, meaning out.

Nothing here holds state, opens a device or knows the dashboard exists, so it
can all be exercised against an archived capture with the base powered off.
"""
import hid_layout

# Pot dividers off the 3.3V sensor supply; STEER is an encoder, so no volts.
VOLT_CHANNELS = {'THROTTLE', 'BRAKE', 'CLUTCH'}

# Unsigned channels that still rest mid-range. Nothing in the descriptor says
# so - STEER declares a plain 0..65535 - but a bar anchored at 0 draws a centred
# wheel as half pressed.
CENTRED_CHANNELS = {'STEER'}

HAT_DIRS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']


def _rim_keymap():
    """Hardware button number -> rim function.

    Per the ftec_keymap comments in hid-fanatec 0.2.3 (hid-ftec.c).
    """
    fn = {
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
    for i in range(7):
        fn[14 + i] = f'Shifter {i + 1}'
    for i in range(12):
        pos = (i + 1) % 12 or 12
        twist = ' / twist right' if i == 0 else ' / twist left' if i == 1 else ''
        fn[37 + i] = f'L knob pos {pos}{twist}'
        fn[49 + i] = f'R knob pos {pos}{twist}'
    return fn


BTN_FN = _rim_keymap()


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


def axis_centre(ax):
    """The value this channel rests at mid-range, or None if it runs end to end.

    A signed field is centred at 0 by definition; the rest is by name.
    """
    if ax['lmin'] < 0:
        return 0
    if ax['name'] in CENTRED_CHANNELS:
        return (ax['lmin'] + ax['lmax'] + 1) // 2
    return None


def button_mask(rep, spec):
    """The button bits as one integer, bit 0 = lowest-numbered button."""
    need = (spec['first_bit'] + spec['count'] + 7) // 8
    if len(rep) < need:
        return 0
    whole = int.from_bytes(bytes(rep[:need]), 'little')
    return (whole >> spec['first_bit']) & ((1 << spec['count']) - 1)


def hat_value(rep, hat):
    """The hat nibble, or None if the report is too short."""
    if not hat or len(rep) <= hat['byte']:
        return None
    return (rep[hat['byte']] >> hat['shift']) & 0x0f


def decode_vendor(rep):
    """Firmware version / wheel id / pedal presence out of the vendor block.

    Mirrors ftecff_raw_event() in hid-ftecff.c, shifted by one for the report id
    this base does not send - which is why its sysfs copies read 0. See
    docs/driver.md.
    """
    out = {}
    if len(rep) != hid_layout.KNOWN_SIZE:   # offsets only mean anything here
        return out
    if rep[29] == 0xff:
        if rep[30] == 0x04:
            out['pedals'] = bool(rep[31] & 0x0f)
            out['handbrake'] = bool(rep[31] >> 4 & 0x0f)
    else:
        out['wheel_id'] = rep[30]
        out['fw_version'] = rep[31] | (rep[32] << 8)
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


def reversals(vals):
    """Direction changes in a sample run. Useless at rest - see docs/findings.md."""
    n = 0
    direction = 0
    for a, b in zip(vals, vals[1:]):
        d = (b > a) - (b < a)
        if d and direction and d != direction:
            n += 1
        if d:
            direction = d
    return n
