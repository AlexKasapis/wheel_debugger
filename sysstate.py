#!/usr/bin/env python3
"""Detect whether this machine is in a state where the dashboard can be trusted.

Every check is a filesystem read: no root, no device opened for I/O, nothing ever
executed. A failing check reports the command that fixes it and stops there.

Run directly to dump the state as JSON.
"""
import glob
import grp
import json
import os
import pwd
import threading
import time

import hid_layout

REPO = os.path.dirname(os.path.abspath(__file__))

MODPROBE_CONF = '/etc/modprobe.d/hid-fanatec.conf'
UDEV_RULE = '/etc/udev/rules.d/99-fanatec.rules'
DRIVER_DIR = '/sys/bus/hid/drivers/fanatec'
MODULE = 'hid_fanatec'
VENDOR_MATCH = '0EB7'

# Own lock and cache, so many pollers cost one set of reads and the reader
# thread never waits on pedal-web's LOCK.
TTL = 2.0
_LOCK = threading.Lock()
_CACHE = {'t': 0.0, 'data': None}

# linux/input-event-codes.h: bit N of sysfs capabilities/ff means effect type N.
# Read from sysfs rather than via EVIOCGBIT, which needs the device open and so
# fails on exactly the machine states worth describing.
FF_EFFECTS = {
    0x50: 'RUMBLE', 0x51: 'PERIODIC', 0x52: 'CONSTANT', 0x53: 'SPRING',
    0x54: 'FRICTION', 0x55: 'DAMPER', 0x56: 'INERTIA', 0x57: 'RAMP',
    0x58: 'SQUARE', 0x59: 'TRIANGLE', 0x5a: 'SINE', 0x5b: 'SAW_UP',
    0x5c: 'SAW_DOWN', 0x5d: 'CUSTOM', 0x60: 'GAIN', 0x61: 'AUTOCENTER',
}

RANK = {'bad': 3, 'warn': 2, 'unknown': 1, 'ok': 0}


def fix_cmd(script):
    """Absolute so it can be copied straight off a phone screen."""
    return f'sudo bash {os.path.join(REPO, "setup", script)}'


def chk(cid, label, status, detail, fix=None, why=None):
    return {'id': cid, 'label': label, 'status': status, 'detail': detail,
            'fix': fix, 'why': why}


def read_text(path):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return None


def hid_id_of(hidraw_path):
    """The HID device a hidraw node belongs to, e.g. 0003:0EB7:0E03.0109."""
    node = os.path.basename(hidraw_path)
    link = f'/sys/class/hidraw/{node}/device'
    try:
        return os.path.basename(os.path.realpath(link))
    except OSError:
        return None


def decode_ff(raw):
    """Effect names from a sysfs capabilities/ff bitmask.

    The kernel prints it as space-separated longs, most significant first, so the
    words are reversed before shifting. x86_64 only: 64-bit longs assumed.
    """
    words = raw.split()
    value = 0
    for i, word in enumerate(reversed(words)):
        value |= int(word, 16) << (64 * i)
    return [name for bit, name in sorted(FF_EFFECTS.items()) if value >> bit & 1]


def check_driver():
    mods = read_text('/proc/modules') or ''
    loaded = any(line.split(' ')[0] == MODULE for line in mods.splitlines())
    bound = [os.path.basename(p) for p in glob.glob(f'{DRIVER_DIR}/*{VENDOR_MATCH}*')]
    # dkms status is not a usable probe here; see docs/driver.md.
    if loaded and bound:
        return chk('driver', 'FFB driver', 'ok',
                   f'{MODULE} loaded, claiming {", ".join(bound)}')
    if loaded:
        return chk('driver', 'FFB driver', 'bad',
                   f'{MODULE} is loaded but has claimed no Fanatec device',
                   fix_cmd('install-ffb.sh'),
                   'hid-generic may have grabbed the base first; replug or rebind')
    return chk('driver', 'FFB driver', 'bad', f'{MODULE} is not loaded',
               fix_cmd('install-ffb.sh'),
               'without it there is no force feedback and no fanatec axis map')


def check_nodes(nodes):
    have = [k for k, v in nodes.items() if v]
    if not have:
        return chk('nodes', 'Device nodes', 'bad',
                   'no usb-Fanatec_* nodes under /dev/input/by-id',
                   None,
                   'the base is unplugged, powered off, or not enumerating')
    detail = '   '.join(f'{k} -> {nodes[k]["path"]}' for k in ('hidraw', 'event', 'js')
                        if nodes[k])
    missing = [k for k, v in nodes.items() if not v]
    if missing:
        return chk('nodes', 'Device nodes', 'warn',
                   f'{detail}   (missing: {", ".join(sorted(missing))})')
    return chk('nodes', 'Device nodes', 'ok', detail)


def check_hidraw_target(nodes):
    """Is hidraw the REAL base, or the driver's virtual PID device?

    The check this module exists for; see docs/driver.md. Only the real base
    declares the 33-byte report with four analog axes and 108 buttons.
    """
    node = nodes.get('hidraw')
    if not node:
        return chk('hidraw_target', 'Raw HID source', 'bad',
                   'no hidraw node for the base',
                   fix_cmd('enable-rawhid.sh'),
                   'raw byte inspection and the whole dashboard need this node')

    layout = hid_layout.layout_for(node['link'])
    hid_id = hid_id_of(node['path'])
    where = f'{node["path"]}' + (f' ({hid_id})' if hid_id else '')
    real = (layout['source'] != 'hardcoded fallback'
            and layout['size'] == hid_layout.KNOWN_SIZE
            and len(layout['axes']) >= 4
            and layout['buttons'] and layout['buttons']['count'] >= 100)
    if real:
        return chk('hidraw_target', 'Raw HID source', 'ok',
                   f'REAL BASE at {where} - {layout["size"]}-byte report, '
                   f'{len(layout["axes"])} axes, {layout["buttons"]["count"]} buttons')
    return chk('hidraw_target', 'Raw HID source', 'bad',
               f'{where} does not look like the base: {layout["source"]}, '
               f'{layout["size"]}-byte report, {len(layout["axes"])} axes',
               fix_cmd('enable-rawhid.sh'),
               'this is almost certainly the driver\'s virtual PID device - it '
               'opens fine and sends nothing, which looks exactly like dead '
               'hardware')


def check_diagnostic_state():
    """Is the box left in the modified HID state?

    The live hidraw_pid parameter cannot be read back, so the modprobe.d file is
    the only available proxy for 'somebody deliberately turned this on'.
    """
    text = read_text(MODPROBE_CONF)
    if text is None:
        return chk('diagnostic_state', 'HID mode', 'ok',
                   'stock (no hidraw_pid override)')
    if 'hidraw_pid=0' in text.replace(' ', ''):
        return chk('diagnostic_state', 'HID mode', 'warn',
                   f'DIAGNOSTIC - {MODPROBE_CONF} sets hidraw_pid=0',
                   fix_cmd('revert-rawhid.sh'),
                   'correct for pedal work; revert it when diagnostics are done')
    return chk('diagnostic_state', 'HID mode', 'warn',
               f'{MODPROBE_CONF} exists but does not set hidraw_pid=0: '
               f'{text.strip()[:80]}')


def check_udev():
    if os.path.exists(UDEV_RULE):
        return chk('udev', 'udev rule', 'ok', UDEV_RULE)
    return chk('udev', 'udev rule', 'warn', f'{UDEV_RULE} missing',
               fix_cmd('install-ffb.sh'),
               'without it the sysfs tuning and LED files stay root-owned')


def check_ff(nodes):
    node = nodes.get('event')
    if not node:
        return chk('ff', 'Force feedback', 'unknown', 'no event node to ask')
    name = os.path.basename(node['path'])
    path = f'/sys/class/input/{name}/device/capabilities/ff'
    raw = read_text(path)
    if raw is None:
        return chk('ff', 'Force feedback', 'unknown', f'cannot read {path}')
    effects = decode_ff(raw.strip())
    if not effects:
        return chk('ff', 'Force feedback', 'bad', 'device advertises no FF effects',
                   fix_cmd('install-ffb.sh'),
                   'hid-generic exposes no force feedback; the fanatec driver does')
    return chk('ff', 'Force feedback', 'ok',
               f'{len(effects)} effect types: {", ".join(effects)}')


def check_games_group():
    """The re-login trap: added to the group, but not in this process's set."""
    try:
        entry = grp.getgrnam('games')
    except KeyError:
        return chk('games_group', 'games group', 'unknown', 'no games group on this system')
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        user = str(os.getuid())
    listed = user in entry.gr_mem
    active = entry.gr_gid in os.getgroups() or entry.gr_gid == os.getgid()
    if active:
        return chk('games_group', 'games group', 'ok', f'{user} is in games (gid {entry.gr_gid})')
    if listed:
        return chk('games_group', 'games group', 'warn',
                   f'{user} was added to games but this session predates it',
                   'log out and back in',
                   'group membership is fixed at login; the sysfs tuning and '
                   'rumble writes stay denied until then')
    return chk('games_group', 'games group', 'warn', f'{user} is not in the games group',
               fix_cmd('install-ffb.sh'),
               'needed for the sysfs tuning writes, not for reading axes')


def check_ffb_writable(nodes):
    """Can the FFB test run? Decided here so the card can say why not."""
    node = nodes.get('event')
    if not node:
        return chk('ffb_writable', 'FFB test', 'bad', 'no event node',
                   None, 'force feedback is driven through the evdev node')
    if os.access(node['path'], os.W_OK):
        return chk('ffb_writable', 'FFB test', 'ok', f'{node["path"]} is writable')
    return chk('ffb_writable', 'FFB test', 'bad',
               f'{node["path"]} is not writable by this user',
               None,
               'uploading an effect needs O_RDWR; usually the pending games '
               're-login, sometimes a missing udev rule')


def _collect():
    nodes = hid_layout.find_nodes()
    checks = []
    for fn, args in (
        (check_driver, ()),
        (check_nodes, (nodes,)),
        (check_hidraw_target, (nodes,)),
        (check_diagnostic_state, ()),
        (check_udev, ()),
        (check_ff, (nodes,)),
        (check_games_group, ()),
        (check_ffb_writable, (nodes,)),
    ):
        try:
            checks.append(fn(*args))
        except Exception as exc:                          # never break the page
            checks.append(chk(fn.__name__, fn.__name__, 'unknown',
                              f'check raised {exc!r}'))

    by_id = {c['id']: c for c in checks}
    worst = max((RANK[c['status']] for c in checks), default=0)
    overall = next(k for k, v in RANK.items() if v == worst)

    bad = [c['label'] for c in checks if c['status'] == 'bad']
    if bad:
        summary = 'PROBLEM: ' + ', '.join(bad)
    else:
        parts = ['driver ' + by_id['driver']['status']]
        if by_id['hidraw_target']['status'] == 'ok':
            parts.append('raw HID -> real base')
        if by_id['ffb_writable']['status'] == 'ok':
            parts.append('FFB ready')
        if by_id['diagnostic_state']['status'] == 'warn':
            parts.append('diagnostic HID mode')
        summary = ' · '.join(parts)

    return {
        'checks': checks,
        'overall': overall,
        'summary': summary,
        'nodes': nodes,
        'ffb_ok': by_id['ffb_writable']['status'] == 'ok',
        'ffb_reason': by_id['ffb_writable']['detail'],
        'driver_ok': by_id['driver']['status'] == 'ok',
        'hidraw_real': by_id['hidraw_target']['status'] == 'ok',
        'hidraw_fix': by_id['hidraw_target']['fix'],
        'hidraw_detail': by_id['hidraw_target']['detail'],
        'driver_fix': by_id['driver']['fix'],
        't': time.time(),
    }


def state(force=False):
    """Cached machine state. Safe to call from a request handler."""
    with _LOCK:
        now = time.time()
        if not force and _CACHE['data'] and now - _CACHE['t'] < TTL:
            return _CACHE['data']
        data = _collect()
        _CACHE['t'] = now
        _CACHE['data'] = data
        return data


if __name__ == '__main__':
    print(json.dumps(state(force=True), indent=2))
