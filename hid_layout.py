#!/usr/bin/env python3
"""Derive the base's raw HID input-report layout from its report descriptor.

The layout it yields is documented in docs/report-map.md.
"""
import glob
import os

# The base re-enumerates on replug, so hardcoded node names go stale silently.
BY_ID = '/dev/input/by-id'
NODE_GLOBS = {
    'hidraw': 'usb-Fanatec_*-hidraw',
    'event': 'usb-Fanatec_*-event-joystick',
    'js': 'usb-Fanatec_*-joystick',
}

# usage (page, id) -> (dashboard name, HID axis name)
AXIS_USAGES = {
    (0x01, 0x30): ('STEER', 'X'),
    (0x01, 0x31): ('CLUTCH', 'Y'),
    (0x01, 0x32): ('THROTTLE', 'Z'),
    (0x01, 0x33): ('STICK-X', 'Rx'),
    (0x01, 0x34): ('STICK-Y', 'Ry'),
    (0x01, 0x35): ('BRAKE', 'Rz'),
    (0x01, 0x36): ('SLIDER', 'Slider'),
    (0x01, 0x37): ('DIAL', 'Dial'),
}
HAT_USAGE = (0x01, 0x39)
BUTTON_PAGE = 0x09
VENDOR_PAGE = 0xff00

# HID item prefix is [tag:4][type:2][size:2]; these are the items Fanatec uses.
LONG_ITEM = 0xfe
TYPE_MAIN, TYPE_GLOBAL, TYPE_LOCAL = 0, 1, 2
MAIN_INPUT, MAIN_OUTPUT, MAIN_FEATURE = 0x8, 0x9, 0xb
G_USAGE_PAGE, G_LOGICAL_MIN, G_LOGICAL_MAX = 0x0, 0x1, 0x2
G_REPORT_SIZE, G_REPORT_ID, G_REPORT_COUNT = 0x7, 0x8, 0x9
L_USAGE, L_USAGE_MIN, L_USAGE_MAX = 0x0, 0x1, 0x2

# The documented layout, in one place: it builds the fallback AND checks whatever
# we parse. docs/report-map.md is the prose copy of this table.
KNOWN_AXES = [
    {'name': 'STEER',    'hid': 'X',      'byte': 16, 'bits': 16, 'signed': False},
    {'name': 'THROTTLE', 'hid': 'Z',      'byte': 18, 'bits': 16, 'signed': False},
    {'name': 'BRAKE',    'hid': 'Rz',     'byte': 20, 'bits': 16, 'signed': False},
    {'name': 'CLUTCH',   'hid': 'Y',      'byte': 22, 'bits': 16, 'signed': False},
    {'name': 'STICK-X',  'hid': 'Rx',     'byte': 24, 'bits': 8,  'signed': True},
    {'name': 'STICK-Y',  'hid': 'Ry',     'byte': 25, 'bits': 8,  'signed': True},
    {'name': 'SLIDER',   'hid': 'Slider', 'byte': 26, 'bits': 8,  'signed': False},
    {'name': 'DIAL',     'hid': 'Dial',   'byte': 27, 'bits': 8,  'signed': True},
]
KNOWN_OFFSETS = {ax['name']: ax['byte'] for ax in KNOWN_AXES}
KNOWN_SIZE = 33


def limits(bits, signed):
    """Logical min/max a field of this width covers."""
    if signed:
        return -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    return 0, (1 << bits) - 1


def _input_fields(gstate, usages, first_bit):
    """One INPUT main item expanded into per-field dicts, in bit order."""
    fields = []
    for n in range(gstate['rcount']):
        # HID repeats the last usage when the count outruns the declared
        # usages - that is what bytes 14-15 are.
        usage = usages[n] if n < len(usages) else (usages[-1] if usages else None)
        fields.append({
            'bit': first_bit + n * gstate['rsize'],
            'size': gstate['rsize'],
            'page': gstate['usage_page'],
            'usage': usage,
            'spare': n >= len(usages),
            'lmin': gstate['lmin'],
            'lmax': gstate['lmax'],
        })
    return fields


def parse_report_descriptor(blob):
    """INPUT fields of report id 0, flat and in bit order.

    Only the items Fanatec actually uses are handled; the rest are skipped
    rather than guessed at.
    """
    i = 0
    gstate = {'usage_page': 0, 'lmin': 0, 'lmax': 0, 'rsize': 0, 'rcount': 0}
    usages = []
    usage_min = usage_max = None
    report_id = 0
    bitpos = {}                      # (report_id, main tag) -> next free bit
    fields = []

    while i < len(blob):
        head = blob[i]
        i += 1
        if head == LONG_ITEM:        # unused here, skip it
            if i + 1 >= len(blob):
                break
            i += 2 + blob[i]
            continue

        size = head & 0x03
        size = 4 if size == 3 else size
        itype = (head >> 2) & 0x03
        tag = head >> 4
        raw = blob[i:i + size]
        i += size
        data = int.from_bytes(raw, 'little') if size else 0
        signed = int.from_bytes(raw, 'little', signed=True) if size else 0

        if itype == TYPE_GLOBAL:
            if tag == G_USAGE_PAGE:
                gstate['usage_page'] = data
            elif tag == G_LOGICAL_MIN:
                gstate['lmin'] = signed
            elif tag == G_LOGICAL_MAX:
                # signed unless that goes negative - how 0xffff means 65535 here
                gstate['lmax'] = signed if signed >= 0 else data
            elif tag == G_REPORT_SIZE:
                gstate['rsize'] = data
            elif tag == G_REPORT_ID:
                report_id = data
            elif tag == G_REPORT_COUNT:
                gstate['rcount'] = data
        elif itype == TYPE_LOCAL:
            if tag == L_USAGE:
                usages.append(data)
            elif tag == L_USAGE_MIN:
                usage_min = data
            elif tag == L_USAGE_MAX:
                usage_max = data
        elif itype == TYPE_MAIN:
            if tag in (MAIN_INPUT, MAIN_OUTPUT, MAIN_FEATURE):
                off = bitpos.get((report_id, tag), 0)
                if tag == MAIN_INPUT and report_id == 0:
                    declared = usages
                    if usage_min is not None and usage_max is not None:
                        declared = list(range(usage_min, usage_max + 1))
                    fields += _input_fields(gstate, declared, off)
                bitpos[(report_id, tag)] = off + gstate['rcount'] * gstate['rsize']
            usages = []
            usage_min = usage_max = None

    return fields, bitpos.get((0, MAIN_INPUT), 0)


def build_layout(fields, size_bits):
    """Turn parsed fields into the structure the dashboard consumes."""
    layout = {
        'size': (size_bits + 7) // 8,
        'axes': [],
        'hat': None,
        'buttons': None,
        'spare_bits': [],
        'vendor': [],
        'warnings': [],
        'source': 'report descriptor',
    }

    btn_bits = []
    for f in fields:
        key = (f['page'], f['usage'])
        if f['page'] == BUTTON_PAGE:
            btn_bits.append(f)
        elif key == HAT_USAGE:
            layout['hat'] = {
                'byte': f['bit'] // 8, 'shift': f['bit'] % 8,
                'size': f['size'], 'lmin': f['lmin'], 'lmax': f['lmax'],
            }
        elif key in AXIS_USAGES:
            name, hid_name = AXIS_USAGES[key]
            if f['bit'] % 8:
                layout['warnings'].append(
                    f'{name} is not byte-aligned (bit {f["bit"]}) - not decoded')
                continue
            layout['axes'].append({
                'name': name, 'hid': hid_name,
                'byte': f['bit'] // 8, 'bits': f['size'],
                'signed': f['lmin'] < 0,
                'lmin': f['lmin'], 'lmax': f['lmax'],
            })
        elif f['page'] == VENDOR_PAGE:
            layout['vendor'].append(f['bit'] // 8)

    if btn_bits:
        real = [f for f in btn_bits if not f['spare']]
        spare = [f for f in btn_bits if f['spare']]
        layout['buttons'] = {
            'first_bit': real[0]['bit'],
            'first_usage': real[0]['usage'],
            'count': len(real),
        }
        layout['spare_bits'] = [f['bit'] for f in spare]

    # Warn rather than mislabel a channel: confidently wrong is the worst case.
    for name, want in KNOWN_OFFSETS.items():
        got = next((a['byte'] for a in layout['axes'] if a['name'] == name), None)
        if got is None:
            layout['warnings'].append(f'{name} not present in this descriptor')
        elif got != want:
            layout['warnings'].append(
                f'{name} is at byte {got}, not the documented {want} - '
                'docs/report-map.md offsets are stale')
    if layout['size'] != KNOWN_SIZE:
        layout['warnings'].append(
            f'report is {layout["size"]} bytes, expected {KNOWN_SIZE}')

    return layout


def fallback_layout(reason):
    """Hardcoded CSL Elite layout, for when the descriptor cannot be read."""
    axes = []
    for spec in KNOWN_AXES:
        lmin, lmax = limits(spec['bits'], spec['signed'])
        axes.append(dict(spec, lmin=lmin, lmax=lmax))
    return {
        'size': KNOWN_SIZE,
        'axes': axes,
        'hat': {'byte': 0, 'shift': 0, 'size': 4, 'lmin': 0, 'lmax': 7},
        'buttons': {'first_bit': 4, 'first_usage': 1, 'count': 108},
        'spare_bits': list(range(112, 128)),
        'vendor': [28, 29, 30, 31, 32],
        'warnings': [f'using hardcoded CSL Elite layout: {reason}'],
        'source': 'hardcoded fallback',
    }


def find_nodes():
    """Locate the base's device nodes. {kind: {'link','path'} or None}.

    kind is 'hidraw' (raw reports), 'event' (evdev, and the only node force
    feedback can be driven through) or 'js' (legacy joystick API).
    """
    found = {}
    for kind, pattern in NODE_GLOBS.items():
        links = sorted(glob.glob(os.path.join(BY_ID, pattern)))
        if kind == 'js':
            # '*-joystick' also matches '*-event-joystick'
            links = [x for x in links if not x.endswith('-event-joystick')]
        found[kind] = ({'link': links[0], 'path': os.path.realpath(links[0])}
                       if links else None)
    return found


def node_path(kind):
    """Realpath of one node, or None. Convenience over find_nodes()."""
    node = find_nodes()[kind]
    return node['path'] if node else None


def layout_for(hidraw_path):
    """Best layout available for /dev/hidrawN, descriptor first."""
    node = os.path.basename(os.path.realpath(hidraw_path))
    sysfs = f'/sys/class/hidraw/{node}/device/report_descriptor'
    try:
        with open(sysfs, 'rb') as fh:
            blob = fh.read()
    except OSError as exc:
        return fallback_layout(f'{sysfs}: {exc.__class__.__name__}')
    try:
        fields, size_bits = parse_report_descriptor(blob)
        layout = build_layout(fields, size_bits)
    except Exception as exc:                              # never break the tool
        return fallback_layout(f'descriptor parse failed: {exc!r}')
    if not layout['axes'] or layout['buttons'] is None:
        return fallback_layout('descriptor yielded no axes/buttons')
    return layout


if __name__ == '__main__':
    import json
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else None
    if not target:
        node = find_nodes()['hidraw']
        if not node:
            sys.exit('no Fanatec hidraw node found; pass one explicitly')
        target = node['link']
    print(json.dumps(layout_for(target), indent=2))
