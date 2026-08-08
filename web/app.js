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

// The sparkline auto-scales, so resting dither draws like a real sweep - hence
// the printed y-range and the absolute full-scale bar beside it.
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

function renderAxes(d) {
  const wide = d.axes.filter(a => a.bits === 16);
  const narrow = d.axes.filter(a => a.bits !== 16);
  fill(document.getElementById('chans'), wide, false);
  fill(document.getElementById('aux'), narrow, true);
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

setInterval(tick, 100);
// System state is filesystem reads, not the hot path - poll it far slower.
setInterval(pollSystem, 3000);
tick();
pollSystem();
