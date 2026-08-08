#!/usr/bin/env python3
"""Derive the base's raw HID input-report layout from its report descriptor.

Every "the dashboard does not show my clutch / my paddles / my buttons" bug in
this project came from hardcoding byte offsets that were discovered by wiggling
things. The descriptor already states the layout exactly, so parse it instead
and only fall back to hardcoded offsets when it cannot be read.

Verified against the CSL Elite base (0eb7:0e03), which yields:

      bit   0 (byte  0, bits 0-3)   Hat switch, 0-7, 8 = centre
      bit   4 (bytes 0-13)          108 buttons, LSB-first
      bits 112-127 (bytes 14-15)    16 further declared button bits; on this
                                    base they are NOT buttons - byte 15 sits at
                                    a constant 0x16 - so they are reported raw
      byte 16  u16 LE  X       STEER
      byte 18  u16 LE  Z       THROTTLE  (throttle-IN jack)
      byte 20  u16 LE  Rz      BRAKE     (brake-IN jack, load cell)
      byte 22  u16 LE  Y       CLUTCH    (clutch-IN jack)
      byte 24  s8      Rx      rim ministick X
      byte 25  s8      Ry      rim ministick Y
      byte 26  u8      Slider
      byte 27  s8      Dial
      bytes 28-32       vendor-defined (fw version, wheel id, pedal presence)

Standard library only.
"""

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

# what the CSL Elite is known to produce - used as the fallback layout and as a
# sanity check on whatever we parse, so a surprise shows up as a warning rather
# than as a silently mislabelled channel
KNOWN_OFFSETS = {
    'STEER': 16, 'THROTTLE': 18, 'BRAKE': 20, 'CLUTCH': 22,
    'STICK-X': 24, 'STICK-Y': 25, 'SLIDER': 26, 'DIAL': 27,
}
KNOWN_SIZE = 33


def parse_report_descriptor(blob):
    """Return the INPUT fields of report id 0 as a flat list, in bit order.

    Only the items Fanatec actually uses are handled; anything else is skipped
    rather than guessed at.
    """
    i = 0
    glob = {'usage_page': 0, 'lmin': 0, 'lmax': 0, 'rsize': 0, 'rcount': 0}
    usages = []
    usage_min = usage_max = None
    report_id = 0
    bitpos = {}                      # (report_id, main tag) -> next free bit
    fields = []

    while i < len(blob):
        head = blob[i]
        i += 1
        if head == 0xfe:             # long item - not used by Fanatec, skip it
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

        if itype == 1:                                   # Global
            if tag == 0x0:
                glob['usage_page'] = data
            elif tag == 0x1:
                glob['lmin'] = signed
            elif tag == 0x2:
                # logical max is signed unless min is 0 and the value would go
                # negative - that is how 0xffff means 65535 here
                glob['lmax'] = signed if signed >= 0 else data
            elif tag == 0x7:
                glob['rsize'] = data
            elif tag == 0x8:
                report_id = data
            elif tag == 0x9:
                glob['rcount'] = data
        elif itype == 2:                                 # Local
            if tag == 0x0:
                usages.append(data)
            elif tag == 0x1:
                usage_min = data
            elif tag == 0x2:
                usage_max = data
        elif itype == 0:                                 # Main
            if tag in (0x8, 0x9, 0xb):                   # Input / Output / Feature
                count, rsize = glob['rcount'], glob['rsize']
                off = bitpos.get((report_id, tag), 0)
                if tag == 0x8 and report_id == 0:
                    us = usages
                    if usage_min is not None and usage_max is not None:
                        us = list(range(usage_min, usage_max + 1))
                    for n in range(count):
                        # HID repeats the last usage when count outruns the
                        # declared usages - that is exactly what bytes 14-15 are
                        usage = us[n] if n < len(us) else (us[-1] if us else None)
                        fields.append({
                            'bit': off + n * rsize,
                            'size': rsize,
                            'page': glob['usage_page'],
                            'usage': usage,
                            'spare': n >= len(us),
                            'lmin': glob['lmin'],
                            'lmax': glob['lmax'],
                        })
                bitpos[(report_id, tag)] = off + count * rsize
            usages = []
            usage_min = usage_max = None

    size_bits = bitpos.get((0, 0x8), 0)
    return fields, size_bits


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
        elif f['page'] == 0xff00:
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

    # A dashboard that is confidently wrong is worse than one that says so.
    for name, want in KNOWN_OFFSETS.items():
        got = next((a['byte'] for a in layout['axes'] if a['name'] == name), None)
        if got is None:
            layout['warnings'].append(f'{name} not present in this descriptor')
        elif got != want:
            layout['warnings'].append(
                f'{name} is at byte {got}, not the documented {want} - '
                'docs/FINDINGS.md offsets are stale')
    if layout['size'] != KNOWN_SIZE:
        layout['warnings'].append(
            f'report is {layout["size"]} bytes, expected {KNOWN_SIZE}')

    return layout


def fallback_layout(reason):
    """Hardcoded CSL Elite layout, for when the descriptor cannot be read."""
    return {
        'size': KNOWN_SIZE,
        'axes': [
            {'name': 'STEER', 'hid': 'X', 'byte': 16, 'bits': 16,
             'signed': False, 'lmin': 0, 'lmax': 65535},
            {'name': 'THROTTLE', 'hid': 'Z', 'byte': 18, 'bits': 16,
             'signed': False, 'lmin': 0, 'lmax': 65535},
            {'name': 'BRAKE', 'hid': 'Rz', 'byte': 20, 'bits': 16,
             'signed': False, 'lmin': 0, 'lmax': 65535},
            {'name': 'CLUTCH', 'hid': 'Y', 'byte': 22, 'bits': 16,
             'signed': False, 'lmin': 0, 'lmax': 65535},
            {'name': 'STICK-X', 'hid': 'Rx', 'byte': 24, 'bits': 8,
             'signed': True, 'lmin': -128, 'lmax': 127},
            {'name': 'STICK-Y', 'hid': 'Ry', 'byte': 25, 'bits': 8,
             'signed': True, 'lmin': -128, 'lmax': 127},
            {'name': 'SLIDER', 'hid': 'Slider', 'byte': 26, 'bits': 8,
             'signed': False, 'lmin': 0, 'lmax': 255},
            {'name': 'DIAL', 'hid': 'Dial', 'byte': 27, 'bits': 8,
             'signed': True, 'lmin': -128, 'lmax': 127},
        ],
        'hat': {'byte': 0, 'shift': 0, 'size': 4, 'lmin': 0, 'lmax': 7},
        'buttons': {'first_bit': 4, 'first_usage': 1, 'count': 108},
        'spare_bits': list(range(112, 128)),
        'vendor': [28, 29, 30, 31, 32],
        'warnings': [f'using hardcoded CSL Elite layout: {reason}'],
        'source': 'hardcoded fallback',
    }


def layout_for(hidraw_path):
    """Best layout available for /dev/hidrawN, descriptor first."""
    import os
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
    import glob
    import json
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else None
    if not target:
        found = glob.glob('/dev/input/by-id/usb-Fanatec_*-hidraw')
        if not found:
            sys.exit('no Fanatec hidraw node found; pass one explicitly')
        target = found[0]
    print(json.dumps(layout_for(target), indent=2))
