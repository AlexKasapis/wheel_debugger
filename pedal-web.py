#!/usr/bin/env python3
"""Local web dashboard for the Fanatec base's raw HID stream.

This module is the process: a thread that owns the hidraw node and feeds every
report into a Tracker, and the HTTP server that serves web/ and the Tracker's
snapshot. The latching, the fault detection and the decoding live in tracker.py
and decode.py; the system checks and the force-feedback test live in sysstate.py
and ffb.py.

Run:  python3 pedal-web.py   (--no-ffb leaves out the routes that move the wheel)
"""
import http.server
import json
import os
import select
import socket
import sys
import threading
import time

import ffb
import hid_layout
import sysstate
import tracker

PORT = 8765
FFB_ENABLED = True   # overridden by --no-ffb in __main__
READ_SIZE = 128      # one report is 33 bytes; this batches without truncating

TRACKER = tracker.Tracker()
FFB = ffb.FfbTest()


# -- the device thread -------------------------------------------------------

def drain(fd):
    """Every report already queued on fd, or None if the device errored."""
    reports = []
    while True:
        try:
            data = os.read(fd, READ_SIZE)
        except BlockingIOError:
            return reports
        except OSError:
            return None
        if not data:
            return reports
        reports.append(data)


def pump(fd, track):
    """Feed reports in until the device stops answering."""
    while True:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            track.note_idle()
            continue
        reports = drain(fd)
        if not reports:                  # errored or went away -> reopen
            return
        now = time.time()
        for rep in reports:
            track.ingest(rep, now)


def reader(track):
    """Own the hidraw node forever, reopening it across replugs."""
    while True:
        try:
            node = hid_layout.find_nodes()['hidraw']
            if not node:
                raise FileNotFoundError('no usb-Fanatec_*-hidraw node')
            layout = hid_layout.layout_for(node['link'])
            fd = os.open(node['path'], os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            track.disconnect(f'not found ({exc.__class__.__name__})')
            time.sleep(1.0)
            continue

        track.connect(node['path'], layout)
        try:
            pump(fd, track)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        track.disconnect()
        time.sleep(0.5)


# -- HTTP --------------------------------------------------------------------

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')

# A fixed table, not a directory walk: the request path is looked up, never
# joined onto a filename, so there is no traversal to get wrong.
STATIC = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/app.css': ('app.css', 'text/css; charset=utf-8'),
    '/app.js': ('app.js', 'application/javascript; charset=utf-8'),
}


def page_data():
    """The tracked stream plus the FFB test's own status.

    Composed here rather than inside Tracker: ffb keeps its own lock and does no
    I/O, so it rides the 10Hz poll without widening the window that blocks the
    reader, and the state machine stays ignorant of the actuator.
    """
    data = TRACKER.snapshot()
    data['ffb'] = FFB.status()
    data['ffb_enabled'] = FFB_ENABLED
    return data


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
            self._json(page_data())
        elif self.path.startswith('/system'):
            self._json(sysstate.state())   # cached with its own TTL in sysstate
        elif self.path in STATIC:
            self._static(*STATIC[self.path])
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == '/reset':
            TRACKER.reset()
            self._json({'ok': True})
        elif self.path == '/ffb/start':
            if not FFB_ENABLED:
                self._json({'ok': False, 'msg': 'FFB disabled (--no-ffb)'}, 403)
                return
            ok, msg = FFB.start()
            self._json({'ok': ok, 'msg': msg}, 200 if ok else 409)
        elif self.path == '/ffb/abort':
            ok, msg = FFB.abort()
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

    threading.Thread(target=reader, args=(TRACKER,), daemon=True).start()
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
