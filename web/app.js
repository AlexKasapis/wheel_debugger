/* ---- graph scale ------------------------------------------------------ */

// FULL draws every graph over the channel's own declared range, so channels are
// comparable and a resting one is a flat line where it actually rests. FIT
// auto-scales, which is the only way LSB dither is visible at all - and the
// reason a graph always prints the range it just drew.
let SCALE = 'full';

// localStorage throws outright in some privacy modes; a diagnostic must not die
// of a remembered preference.
function recall(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function remember(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* fine */ } }

function setScale(mode) {
  SCALE = mode;
  remember('scale', mode);
  document.getElementById('sc-full').classList.toggle('on', mode === 'full');
  document.getElementById('sc-fit').classList.toggle('on', mode === 'fit');
  if (LAST) renderAxes(LAST);
}

// The y-range to draw in, from one place: the caption prints what was drawn.
function yrange(c, vals) {
  if (SCALE === 'full') return [c.lmin, c.lmax];
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi === lo) { lo -= 1; hi += 1; }
  return [lo, hi];
}

function ctx(cv) {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  cv.width = w * dpr; cv.height = h * dpr;
  const g = cv.getContext('2d');
  g.scale(dpr, dpr);
  g.clearRect(0, 0, w, h);
  return [g, w, h];
}

function spark(cv, vals, lo, hi, centre) {
  const [g, w, h] = ctx(cv);
  if (!vals || vals.length < 2 || hi === lo) return;
  const y = v => h - 3 - (v - lo) / (hi - lo) * (h - 6);
  if (centre !== null && centre > lo && centre < hi) {
    g.strokeStyle = '#2f3743'; g.lineWidth = 1;
    g.beginPath(); g.moveTo(0, y(centre)); g.lineTo(w, y(centre)); g.stroke();
  }
  g.strokeStyle = '#5fb0ff'; g.lineWidth = 1.5; g.beginPath();
  vals.forEach((v, i) => {
    const x = i / (vals.length - 1) * w;
    i ? g.lineTo(x, y(v)) : g.moveTo(x, y(v));
  });
  g.stroke();
}

// Occupancy across the full declared range, latched since reset. Always full
// range whatever SCALE says: a zoomed coverage strip cannot show a gap.
function coverStrip(cv, buckets) {
  const [g, w, h] = ctx(cv);
  const top = buckets && buckets.length ? Math.max(...buckets) : 0;
  if (!top) return;
  const cw = w / buckets.length;
  buckets.forEach((n, i) => {
    if (!n) return;
    // log: a channel dwells at rest for orders of magnitude more samples than
    // it spends passing through, which on a linear scale erases the sweep
    const t = Math.log(1 + n) / Math.log(1 + top);
    g.fillStyle = 'rgba(95,176,255,' + (0.2 + 0.8 * t).toFixed(3) + ')';
    // both edges rounded the same way, so neighbours share an exact boundary:
    // a cell bleeding over the next one narrows the single-bucket gap that is
    // the whole point of this strip
    const x = Math.round(i * cw);
    g.fillRect(x, 0, Math.max(1, Math.round((i + 1) * cw) - x), h);
  });
}

function setBar(bar, c) {
  const full = (c.lmax - c.lmin) || 1;
  const pct = v => (v - c.lmin) / full * 100;
  const at = c.value === null ? c.lmin : c.value;
  const u = bar.querySelector('u'), dev = bar.querySelector('s'),
        tick = bar.querySelector('b'), now = bar.querySelector('i');
  u.style.left = (c.min === null ? 0 : pct(c.min)) + '%';
  u.style.width = (c.min === null ? 0 : pct(c.max) - pct(c.min)) + '%';
  // A centred channel grows out of its midpoint; anchoring at lmin would draw a
  // centred wheel as half pressed. Unipolar channels keep the marker alone -
  // which end of a pedal's range means "pressed" is not settled here.
  tick.style.display = dev.style.display = c.centre === null ? 'none' : 'block';
  if (c.centre !== null) {
    const a = Math.min(pct(c.centre), pct(at)), z = Math.max(pct(c.centre), pct(at));
    dev.style.left = a + '%';
    dev.style.width = (z - a) + '%';
    tick.style.left = pct(c.centre) + '%';
  }
  now.style.left = Math.max(0, pct(at) - 0.6) + '%';
  now.style.width = '1.5%';
}

function axisCard(el, c, small) {
  const twitchy = c.warn;
  el.className = 'card' + (twitchy ? ' warn' : '') + (c.idle ? ' idle' : '');
  el.querySelector('.nm').textContent =
    c.name + '   ' + c.hid + ' @ byte ' + c.byte + (c.bits === 16 ? '' : ' (8-bit)');
  el.querySelector('.big').textContent =
    (c.value === null ? '--' : c.value) + (c.volts !== null ? '  ~' + c.volts + 'V' : '');
  setBar(el.querySelector('.bar'), c);
  el.querySelector('.s1').textContent = c.idle
    ? 'no movement seen since reset  (rests at ' + c.value + ')'
    : 'seen ' + c.min + ' .. ' + c.max + '   span ' + c.span + ' (' + c.span_pct + '%)';
  el.querySelector('.s2').innerHTML = small ? '' :
    'noise/2s: sd <span class="' + (twitchy ? 'hot' : 'ok') + '">'
    + c.jitter_sd + '</span>, ' + c.rev_per100 + ' reversals/100'
    + ' <span class="tiny">(' + c.n_recent + ' samples)</span>';

  const sp = c.spark || [];
  const [lo, hi] = yrange(c, sp);
  spark(el.querySelector('.g'), sp, lo, hi, c.centre);
  el.querySelector('.s3').textContent = sp.length < 2
    ? 'graph: not enough samples'
    : SCALE === 'full'
      ? 'graph ' + c.lmin + ' .. ' + c.lmax + ' (full range)   these '
        + sp.length + ' samples: ' + Math.min(...sp) + ' .. ' + Math.max(...sp)
      : 'graph ' + lo + ' .. ' + hi + ' (auto-scaled, span ' + (hi - lo) + ')';

  const strip = el.querySelector('.cv');
  if (!strip) return;
  const cov = c.cover || [];
  coverStrip(strip, cov);
  const hit = cov.filter(n => n).length;
  el.querySelector('.s4').textContent = hit
    ? 'range coverage ' + hit + '/' + cov.length + ' - gaps never visited'
    : 'range coverage: nothing recorded yet';
}

/* ---- the ministick pad ------------------------------------------------- */

// Zoom both axes by the same amount so a circular sweep stays circular.
function padRange(x, y) {
  if (SCALE === 'full') return [x.lmin, x.lmax];
  const mid = x.centre === null ? 0 : x.centre;
  let d = 0;
  (x.spark || []).concat(y.spark || [])
    .forEach(v => { d = Math.max(d, Math.abs(v - mid)); });
  // headroom, or the dot marking the extreme sample is half outside the pad;
  // and a floor, so resting dither cannot zoom until it fills the square
  d = Math.max(4, Math.round(d * 1.12));
  return [mid - d, mid + d];
}

function pad(cv, x, y) {
  const [g, w, h] = ctx(cv);
  const side = Math.min(w, h) - 6;
  const ox = (w - side) / 2, oy = (h - side) / 2;
  const [lo, hi] = padRange(x, y);
  if (hi === lo) return;
  const px = v => ox + (v - lo) / (hi - lo) * side;
  // +Y downward, exactly as the report carries it - no guess about which way
  // the physical stick leans
  const py = v => oy + (v - lo) / (hi - lo) * side;

  g.strokeStyle = '#262b34'; g.lineWidth = 1;
  g.strokeRect(ox + .5, oy + .5, side - 1, side - 1);
  g.save();
  g.beginPath(); g.rect(ox, oy, side, side); g.clip();

  if (x.min !== null && y.min !== null) {      // the box the stick has swept
    g.fillStyle = '#1b2530';
    g.fillRect(px(x.min), py(y.min), px(x.max) - px(x.min), py(y.max) - py(y.min));
  }
  const mid = x.centre === null ? 0 : x.centre;
  g.strokeStyle = '#2f3743';
  g.beginPath();
  g.moveTo(ox, py(mid)); g.lineTo(ox + side, py(mid));
  g.moveTo(px(mid), oy); g.lineTo(px(mid), oy + side);
  g.stroke();

  // paired from the end: both axes are appended on every report, so the last n
  // samples of each are the same n reports
  const n = Math.min((x.spark || []).length, (y.spark || []).length);
  if (n > 1) {
    const sx = x.spark.slice(-n), sy = y.spark.slice(-n);
    g.strokeStyle = 'rgba(95,176,255,.35)'; g.lineWidth = 1;
    g.beginPath();
    for (let i = 0; i < n; i++) {
      const cx = px(sx[i]), cy = py(sy[i]);
      i ? g.lineTo(cx, cy) : g.moveTo(cx, cy);
    }
    g.stroke();
  }
  if (x.value !== null && y.value !== null) {
    g.fillStyle = '#7fe0a0';
    g.beginPath(); g.arc(px(x.value), py(y.value), 3.5, 0, 6.284); g.fill();
  }
  g.restore();
}

// One 2D control, so one graph: two sparklines cannot show where a stick is,
// whether it returns to centre, or whether it reaches the corners.
function xyCard(el, x, y) {
  const idle = x.idle && y.idle;
  el.className = 'card' + (x.warn || y.warn ? ' warn' : '') + (idle ? ' idle' : '');
  el.querySelector('.nm').textContent =
    'MINISTICK   ' + x.hid + '/' + y.hid + ' @ bytes ' + x.byte + '-' + y.byte;
  el.querySelector('.big').textContent =
    (x.value === null ? '--' : x.value) + ', ' + (y.value === null ? '--' : y.value);
  el.querySelector('.s1').textContent = idle
    ? 'no movement seen since reset  (rests at ' + x.value + ', ' + y.value + ')'
    : 'seen X ' + x.min + ' .. ' + x.max + '   Y ' + y.min + ' .. ' + y.max;
  pad(el.querySelector('.xy'), x, y);
  const [lo, hi] = padRange(x, y);
  el.querySelector('.s3').textContent =
    (SCALE === 'full' ? 'pad ' + lo + ' .. ' + hi + ' (full range)'
                      : 'pad ' + lo + ' .. ' + hi + ' (auto-scaled)')
    + '   X right, Y down as reported';
}

/* ---- card plumbing ----------------------------------------------------- */

function skeleton(kind, small) {
  if (kind === 'xy') {
    return '<div class="nm"></div><div class="big sm2"></div>'
         + '<canvas class="xy"></canvas>'
         + '<div class="sub s1"></div><div class="tiny s3"></div>';
  }
  return '<div class="nm"></div><div class="big' + (small ? ' sm2' : '') + '"></div>'
       + '<div class="bar"><u></u><s></s><b></b><i></i></div>'
       + '<div class="sub s1"></div><div class="sub s2"></div>'
       + '<canvas class="g"></canvas><div class="tiny s3"></div>'
       // no room for a coverage strip on the small rim cards
       + (small ? '' : '<canvas class="cv"></canvas><div class="tiny s4"></div>');
}

// Cards are reused by position, so a slot that changes what it holds has to be
// rebuilt - an axis skeleton reused as a pad has none of the pad's elements.
function slot(host, i, kind, key, small) {
  let el = host.children[i];
  if (!el) {
    el = document.createElement('div');
    host.appendChild(el);
  }
  if (el.dataset.kind !== kind || el.dataset.key !== key) {
    el.dataset.kind = kind;
    el.dataset.key = key;
    el.innerHTML = skeleton(kind, small);
  }
  return el;
}

function fill(host, views, small) {
  views.forEach((v, i) => {
    const el = slot(host, i, v.kind, v.key, small);
    if (v.kind === 'xy') xyCard(el, v.x, v.y);
    else axisCard(el, v.ch, small);
  });
  while (host.children.length > views.length) host.lastChild.remove();
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

// The ministick's two axes collapse into one pad. If the descriptor declares
// only one of them there is no pad and both fall through as plain cards, which
// is the loud-and-degraded case build_layout() already warns about.
function auxViews(narrow) {
  const by = {};
  narrow.forEach(c => { by[c.name] = c; });
  const paired = by['STICK-X'] && by['STICK-Y'];
  const views = paired
    ? [{kind: 'xy', key: 'STICK', x: by['STICK-X'], y: by['STICK-Y']}] : [];
  narrow.forEach(c => {
    if (paired && (c.name === 'STICK-X' || c.name === 'STICK-Y')) return;
    views.push({kind: 'axis', key: c.name, ch: c});
  });
  return views;
}

function renderAxes(d) {
  const wide = d.axes.filter(a => a.bits === 16);
  const narrow = d.axes.filter(a => a.bits !== 16);
  fill(document.getElementById('chans'),
       wide.map(c => ({kind: 'axis', key: c.name, ch: c})), false);
  fill(document.getElementById('aux'), auxViews(narrow), true);
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

setScale(recall('scale') === 'fit' ? 'fit' : 'full');

setInterval(tick, 100);
// System state is filesystem reads, not the hot path - poll it far slower.
setInterval(pollSystem, 3000);
tick();
pollSystem();
