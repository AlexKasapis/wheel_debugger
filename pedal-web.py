#!/usr/bin/env python3
"""Local web dashboard for the Fanatec pedal diagnostics.

A background thread reads the RAW HID reports at full rate and detects
dropouts, rail-pinning and value jumps, then LATCHES them - so an
intermittent fault lasting 20ms still shows on screen minutes later.
The page polls at 10Hz; the detection runs at full report rate.

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

PORT = 8765
CHANNELS = [('STEER', 16), ('THR-IN', 18), ('BRK-IN', 20)]
KNOWN_BYTES = {16, 17, 18, 19, 20, 21}

JUMP = 3000        # sample-to-sample delta that counts as a glitch
GAP = 2.0          # seconds without a report that counts as a dropout
                   # (the base idles around 9 reports/s, so this must be loose)
WINDOW = 2.0       # seconds for the rolling jitter stats
SD_WARN = 200      # rolling stdev above this = electrically noisy channel
                   # (LSB dither measures ~10; the bad throttle measured ~1380)
HIST_LEN = 900

LOCK = threading.Lock()
HIST = {name: collections.deque(maxlen=HIST_LEN) for name, _ in CHANNELS}
EVENTS = collections.deque(maxlen=400)
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
}


def event(kind, ch, detail):
    STATE['glitches'] += 1
    EVENTS.appendleft({
        't': round(time.time() - STATE['started'], 2),
        'kind': kind,
        'ch': ch,
        'detail': detail,
    })


def reader():
    prev = {}
    railed = {}
    gap_open = False
    last_t = time.time()
    tick_t = time.time()
    tick_n = 0

    while True:
        try:
            path = os.path.realpath(
                glob.glob('/dev/input/by-id/usb-Fanatec_*-hidraw')[0])
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
                    tick_n += len(reports)
                    if now - tick_t >= 1.0:
                        STATE['rate'] = round(tick_n / (now - tick_t), 1)
                        tick_t, tick_n = now, 0

                    for rep in reports:
                        STATE['count'] += 1
                        STATE['report'] = rep
                        STATE['size'] = len(rep)
                        if STATE['lo'] is None or len(STATE['lo']) != len(rep):
                            STATE['lo'] = list(rep)
                            STATE['hi'] = list(rep)
                        else:
                            for i, b in enumerate(rep):
                                if b < STATE['lo'][i]:
                                    STATE['lo'][i] = b
                                if b > STATE['hi'][i]:
                                    STATE['hi'][i] = b

                        for name, idx in CHANNELS:
                            if len(rep) <= idx + 1:
                                continue
                            val = rep[idx] | (rep[idx + 1] << 8)
                            HIST[name].append((now, val))

                            old = prev.get(name)
                            if old is not None and abs(val - old) > JUMP:
                                event('JUMP', name, f'{old} -> {val}  (D{val-old:+})')
                            prev[name] = val

                            # only log ENTERING a rail; a channel that simply
                            # rests at a rail must not spam the log
                            at_rail = val in (0, 65535)
                            if name in railed and at_rail and not railed[name]:
                                event('RAIL', name,
                                      'went to ' + ('MAX 65535' if val else 'MIN 0'))
                            railed[name] = at_rail
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
        out = {
            'dev': STATE['dev'],
            'connected': STATE['connected'],
            'count': STATE['count'],
            'rate': STATE['rate'],
            'size': STATE['size'],
            'uptime': round(now - STATE['started'], 1),
            'glitches': STATE['glitches'],
            'events': list(EVENTS)[:60],
            'hex': ' '.join(f'{b:02x}' for b in rep) if rep else '',
            'channels': [],
            'bytes': [],
            'candidates': [],
        }

        for name, idx in CHANNELS:
            pts = list(HIST[name])
            recent = [v for t, v in pts if now - t <= WINDOW]
            vals = [v for _, v in pts]
            ch = {
                'name': name,
                'byte': idx,
                'value': vals[-1] if vals else None,
                'volts': round(vals[-1] / 65535 * 3.3, 3) if vals else None,
                'min': min(vals) if vals else None,
                'max': max(vals) if vals else None,
                'span': (max(vals) - min(vals)) if vals else 0,
                'jitter_sd': round(statistics.pstdev(recent), 1) if len(recent) > 1 else 0.0,
                'jitter_rev': reversals(recent) if len(recent) > 1 else 0,
                'n_recent': len(recent),
                'spark': vals[-240:],
            }
            ch['span_pct'] = round(100.0 * ch['span'] / 65535, 1)
            # normalised so it is comparable across report rates
            ch['rev_per100'] = (round(100.0 * ch['jitter_rev'] / len(recent), 1)
                                if len(recent) > 1 else 0.0)
            ch['warn'] = ch['jitter_sd'] > SD_WARN
            out['channels'].append(ch)

        if lo and hi:
            for i in range(len(lo)):
                out['bytes'].append({
                    'i': i, 'lo': lo[i], 'hi': hi[i],
                    'now': rep[i] if rep and i < len(rep) else 0,
                    'moved': hi[i] != lo[i],
                    'known': i in KNOWN_BYTES,
                })
            moved = [i for i in range(len(lo)) if hi[i] != lo[i] and i not in KNOWN_BYTES]
            seen = set()
            for i in moved:
                if i in seen or i + 1 not in moved:
                    continue
                seen.update((i, i + 1))
                out['candidates'].append({
                    'lo_byte': i,
                    'range': f'{lo[i] | (lo[i+1] << 8)} .. {hi[i] | (hi[i+1] << 8)}',
                })
    return out


PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fanatec pedal diagnostics</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; padding:14px; background:#101216; color:#dfe4ec;
         font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  h1 { font-size:15px; margin:0 0 10px; color:#9aa6b8; font-weight:600;
       letter-spacing:.06em; text-transform:uppercase; }
  h2 { font-size:12px; margin:20px 0 8px; color:#7d8798; font-weight:600;
       letter-spacing:.08em; text-transform:uppercase; }
  #banner { padding:10px 12px; border-radius:6px; margin-bottom:14px;
            background:#16351f; border:1px solid #2c6b3f; color:#7fe0a0; }
  #banner.bad { background:#3a1518; border-color:#7d2b31; color:#ff9aa2; }
  #banner.idle { background:#3a2f13; border-color:#7d6425; color:#ffd58a; }
  .grid { display:grid; gap:10px; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); }
  .card { background:#171a20; border:1px solid #262b34; border-radius:8px; padding:12px; }
  .card.warn { border-color:#7d2b31; }
  .nm { color:#8b95a6; font-size:12px; letter-spacing:.08em; }
  .big { font-size:30px; font-variant-numeric:tabular-nums; margin:2px 0; }
  .sub { color:#8b95a6; font-size:12px; }
  .hot { color:#ff9aa2; }
  .ok  { color:#7fe0a0; }
  canvas { width:100%; height:52px; display:block; margin-top:8px;
           background:#0d0f13; border-radius:4px; }
  table { border-collapse:collapse; width:100%; font-size:12px; }
  td,th { padding:3px 8px; text-align:left; border-bottom:1px solid #21252d; }
  th { color:#7d8798; font-weight:600; }
  .bytes { display:flex; flex-wrap:wrap; gap:3px; }
  .b { padding:3px 5px; border-radius:3px; background:#1b1f26; color:#5f6878;
       font-size:11px; min-width:34px; text-align:center; }
  .b.moved { background:#3a2a12; color:#ffcc7a; }
  .b.moved.known { background:#12303a; color:#7ad4ee; }
  button { background:#242a34; color:#dfe4ec; border:1px solid #39414f;
           border-radius:5px; padding:6px 14px; font:inherit; cursor:pointer; }
  button:hover { background:#2e3542; }
  .hex { word-break:break-all; color:#6f7a8c; font-size:11px; }
</style>

<h1>Fanatec pedal diagnostics</h1>
<div id="banner">waiting for data...</div>
<button onclick="reset()">reset stats &amp; event log</button>

<h2>Channels</h2>
<div class="grid" id="chans"></div>

<h2>Event log <span class="sub">(latched - survives until reset)</span></h2>
<table><thead><tr><th>t+s</th><th>kind</th><th>ch</th><th>detail</th></tr></thead>
<tbody id="events"></tbody></table>

<h2>Report bytes <span class="sub">(orange = moved, blue = known channel)</span></h2>
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
  if (hi - lo < 400) { const m = (hi + lo) / 2; lo = m - 200; hi = m + 200; }
  g.strokeStyle = '#5fb0ff'; g.lineWidth = 1.5; g.beginPath();
  vals.forEach((v, i) => {
    const x = i / (vals.length - 1) * w;
    const y = h - 3 - (v - lo) / (hi - lo) * (h - 6);
    i ? g.lineTo(x, y) : g.moveTo(x, y);
  });
  g.stroke();
}

async function tick() {
  let d;
  try { d = await (await fetch('/data')).json(); }
  catch (e) { return; }

  const b = document.getElementById('banner');
  if (!d.connected) {
    b.className = 'bad';
    b.textContent = 'DEVICE NOT CONNECTED - ' + d.dev;
  } else if (d.count === 0) {
    b.className = 'idle';
    b.textContent = 'device node is open (' + d.dev + ') but the base is sending '
                  + 'NO reports. Is the wheel base powered on? '
                  + '(' + d.uptime + 's waiting)';
  } else if (d.glitches > 0) {
    b.className = 'bad';
    b.textContent = d.glitches + ' GLITCH EVENT(S) CAUGHT - see log below   |   '
                  + d.rate + ' rep/s, ' + d.count + ' total, ' + d.uptime + 's';
  } else {
    b.className = '';
    b.textContent = 'clean - no glitches   |   ' + d.rate + ' rep/s, '
                  + d.count + ' reports, ' + d.uptime + 's up';
  }

  const host = document.getElementById('chans');
  d.channels.forEach((c, i) => {
    let el = host.children[i];
    if (!el) {
      el = document.createElement('div');
      el.className = 'card';
      el.innerHTML = '<div class="nm"></div><div class="big"></div>'
                   + '<div class="sub s1"></div><div class="sub s2"></div>'
                   + '<canvas></canvas>';
      host.appendChild(el);
    }
    const twitchy = c.warn;
    el.className = 'card' + (twitchy ? ' warn' : '');
    el.querySelector('.nm').textContent = c.name + '  [byte ' + c.byte + ']';
    el.querySelector('.big').textContent =
      (c.value === null ? '--' : c.value) + '  ~' + c.volts + 'V';
    el.querySelector('.s1').textContent =
      'seen ' + c.min + ' .. ' + c.max + '   span ' + c.span + ' (' + c.span_pct + '%)';
    el.querySelector('.s2').innerHTML =
      'noise/2s: sd <span class="' + (twitchy ? 'hot' : 'ok') + '">'
      + c.jitter_sd + '</span>, ' + c.rev_per100 + ' reversals/100'
      + ' <span style="color:#5f6878">(' + c.n_recent + ' samples)</span>';
    spark(el.querySelector('canvas'), c.spark);
  });

  document.getElementById('events').innerHTML = d.events.length
    ? d.events.map(e =>
        '<tr><td>' + e.t + '</td><td class="hot">' + e.kind + '</td><td>'
        + e.ch + '</td><td>' + e.detail + '</td></tr>').join('')
    : '<tr><td colspan="4" class="sub">nothing caught yet</td></tr>';

  document.getElementById('bytes').innerHTML = d.bytes.map(x =>
    '<div class="b' + (x.moved ? ' moved' : '') + (x.known ? ' known' : '')
    + '" title="byte ' + x.i + ': seen ' + x.lo + '-' + x.hi + '">'
    + x.i + ':' + x.now + '</div>').join('');

  document.getElementById('cands').textContent = d.candidates.length
    ? 'UNKNOWN 16-bit channels moving: '
      + d.candidates.map(c => '[' + c.lo_byte + ':' + (c.lo_byte + 1) + '] ' + c.range).join('   ')
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
                EVENTS.clear()
                STATE['glitches'] = 0
                STATE['lo'] = STATE['hi'] = None
                STATE['count'] = 0
                STATE['started'] = time.time()
                for dq in HIST.values():
                    dq.clear()
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
