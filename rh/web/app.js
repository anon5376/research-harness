// The client for the live page, served same-origin at /app.js. It polls the page it shows, swaps only what changed
// (FLIP for rows that move between groups), follows app links without reloading, posts forms with fetch and says what
// happened in a toast, and adds the keyboard and the ⌘K palette. It also draws the tank: each seat as an ASCII
// jellyfish on a faint character grid. Everything that moves stops under prefers-reduced-motion; the tank and the
// prompt cycle read it when the page loads, so a change while the page is open takes a reload.
(() => {
'use strict';
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const calm = matchMedia('(prefers-reduced-motion: reduce)');
const byKey = (k, r = document) => r.querySelector('[data-key="' + CSS.escape(k) + '"]');
let skew = 0, inflight = 0, failures = 0, timer = 0, stash = null, ended = false, moving = 0, show = '', kind = '';

const span = s => s < 60 ? s + ' s' : s < 3600 ? Math.floor(s / 60) + ' min'
  : s < 86400 ? Math.floor(s / 3600) + ' h' : Math.floor(s / 86400) + ' d';
const rel = s => s >= 0 ? span(s) + ' ago' : 'in ' + span(-s);
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const two = n => (n < 10 ? '0' : '') + n;
function clock() {
  for (const el of $$('[data-t]')) {
    const s = Math.round(Date.now() / 1000 + skew - +el.dataset.t);
    const text = el.dataset.short ? span(Math.max(0, s))
      : s >= 0 && el.dataset.due ? el.dataset.due : (el.dataset.pre || '') + rel(s);
    if (el.textContent !== text) el.textContent = text;
  }
  const d = new Date(Date.now() + skew * 1000);
  const day = DAYS[d.getDay()] + ' ' + d.getDate() + ' ' + MONTHS[d.getMonth()] + ' · ' + two(d.getHours()) + ':'
    + two(d.getMinutes());
  for (const el of $$('[data-clock]')) if (el.textContent !== day) el.textContent = day;
}
function play(el, cls, ms) {
  if (!el) return;
  el.classList.remove(cls); void el.offsetWidth; el.classList.add(cls);
  setTimeout(() => el.classList.remove(cls), ms || 1400);
}
const parse = html => new DOMParser().parseFromString(html, 'text/html');
const busy = el => !!((el.matches && el.matches('dialog[open]'))
  || el.querySelector('details[open], dialog[open], [aria-busy="true"]')
  || (el.contains(document.activeElement) && document.activeElement.matches('input, textarea')));
const regions = el => $$('[data-region]', el).map(r => r.dataset.region).join(' ');

// ───────── the tank: seats drawn as ASCII jellyfish on the character grid ─────────
const CW = 8, CH = 11, FONT = '10px "Geist Mono", ui-monospace, monospace';
const RAMP = [' ', '.', "'", ':', ';', 't', 'o', '%', 'S', 'X', '8', '@'];
const TONE = { accent: [95, 207, 185], amber: [226, 166, 84], faint: [159, 176, 212] };
const T0 = performance.now() - 6000;  // one clock for every tank, so a redrawn band carries on where it was
const hash = (a, b, c) => {
  let h = Math.imul(a | 0, 374761393) ^ Math.imul(b | 0, 668265263) ^ Math.imul(c | 0, 1274126177);
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
};
const smooth = (a, b, x) => { const t = Math.min(1, Math.max(0, (x - a) / (b - a))); return t * t * (3 - 2 * t); };
// slow value noise, so the texture of a bell shimmers instead of flickering
const noise = (x, y, z) => {
  const xi = Math.floor(x), yi = Math.floor(y), zi = Math.floor(z);
  const xf = x - xi, yf = y - yi, zf = z - zi;
  const u = xf * xf * (3 - 2 * xf), v = yf * yf * (3 - 2 * yf), w = zf * zf * (3 - 2 * zf);
  const l = (a, b, t) => a + (b - a) * t;
  const c = (dx, dy, dz) => hash(xi + dx, yi + dy, zi + dz);
  return l(l(l(c(0, 0, 0), c(1, 0, 0), u), l(c(0, 1, 0), c(1, 1, 0), u), v),
    l(l(c(0, 0, 1), c(1, 0, 1), u), l(c(0, 1, 1), c(1, 1, 1), u), v), w);
};

function Tank(canvas) {
  let spec = [];
  try { spec = JSON.parse(canvas.dataset.creatures || '[]'); } catch (err) { spec = []; }
  const ctx = canvas.getContext('2d');
  const host = canvas.parentElement;
  if (!ctx || !host) return;
  let W = 0, H = 0, cols = 0, rows = 0, dens, who, specks = [];

  function size() {
    const r = canvas.getBoundingClientRect();
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    W = r.width; H = r.height;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    cols = Math.ceil(W / CW); rows = Math.ceil(H / CH);
    dens = new Float32Array(cols * rows); who = new Int8Array(cols * rows);
    specks = [];
    for (let i = 0; i < cols * rows * 0.018; i++) specks.push([Math.floor(hash(i, 7, 3) * cols), Math.floor(hash(i, 11, 5) * rows), hash(i, 13, 9)]);
  }
  // Words sit on clear water: the creatures thin out behind every [data-clear] element. Measured on each frame, since
  // the composer grows and shrinks as you type.
  function clears() {
    const box = host.getBoundingClientRect();
    return $$('[data-clear]', host).filter(el => el.getClientRects().length).map(el => {
      const b = el.getBoundingClientRect();
      return [(b.left - box.left - 8) / CW, (b.top - box.top - 4) / CH, (b.right - box.left + 8) / CW, (b.bottom - box.top + 4) / CH];
    });
  }
  const put = (i, j, v, k) => {
    if (i < 0 || j < 0 || i >= cols || j >= rows) return;
    const n = j * cols + i;
    if (v > dens[n]) { dens[n] = Math.min(1, v); who[n] = k; }
  };

  function creature(c, k, t) {
    const sp = c.sp || 1, ph = c.ph || k * 1.7;
    // The whole body stays in the water with a cell to spare; in a band too short for it, it is drawn smaller.
    const fit = Math.max(0.3, Math.min(1, (rows - 2) / (c.r * 0.75 + c.len * 1.05 + 4), (cols - 2) / (c.r * 2.2 + 6)));
    const R = c.r * fit, L = c.len * fit;
    const rx = (R * (1 + 0.06 * Math.sin(t * 1.05 * sp + ph))), ry = R * 0.62 * (1 - 0.05 * Math.sin(t * 1.05 * sp + ph));
    const up = ry + 2, down = ry * 0.2 + L * 1.05 + 2, side = Math.max(rx + 3, L * 0.32 + 2);
    const clamp = (v, lo, hi) => (hi < lo ? (lo + hi) / 2 : Math.min(hi, Math.max(lo, v)));
    // it bobs and drifts about its place, never past the band's edges
    const cy = clamp(c.fy * rows + Math.sin(t * 0.21 * sp + ph) * 1.4, 1 + up, rows - 1 - down);
    const cx = clamp(c.fx * cols + Math.sin(t * 0.17 * sp + ph) * 1.6, 1 + side, cols - 1 - side);
    const flick = Math.floor(t * 5);
    const tz = t * 0.7 + k * 9;
    // the bell leans a little as it swims
    const tilt = (c.tilt ?? 0.16) * Math.sin(t * 0.23 * sp + ph * 1.3), ca = Math.cos(tilt), sa = Math.sin(tilt), asp = CH / CW;
    const lean = s => s * sa * 0.9;
    // bell: a dome, densest on its crown and rim, darker inside, a frilled lip
    for (let j = Math.floor(cy - ry - 2); j <= Math.ceil(cy + ry * 0.6); j++) {
      for (let i = Math.floor(cx - rx - 2); i <= Math.ceil(cx + rx + 2); i++) {
        const px = i - cx, py = (j - cy) * asp;
        const dx = (px * ca + py * sa) / rx, dy = ((-px * sa + py * ca) / asp) / ry;
        const lip = 0.22 + 0.09 * Math.sin(dx * 11 + t * 2 * sp + ph);
        if (dy > lip) continue;
        const d2 = dx * dx + dy * dy; if (d2 > 1.02) continue;
        const d = Math.sqrt(d2);
        let v = 0.3 + 0.5 * smooth(0.58, 0.98, d) + Math.max(0, -dy - 0.3) * 0.55;
        v += Math.exp(-((d - 0.4) ** 2) / 0.01) * (0.5 + 0.5 * Math.sin(Math.atan2(dy, dx) * 4 + t * 0.5)) * 0.3;
        if (dy > lip - 0.18) v += 0.18;
        v *= 0.62 + 0.62 * noise(i * 0.45, j * 0.6, tz);
        if (hash(i, j, flick + k * 31) > 0.975) v = Math.max(v, 0.96);
        put(i, j, v * c.b, k);
      }
    }
    // fringe: many short hairs under the lip
    const lipY = cy + ry * 0.2;
    const rim = u => [cx + u * rx * ca - ry * 0.2 * asp * sa, lipY + (u * rx * sa) / asp];
    for (let i = -Math.floor(rx * 0.92); i <= Math.floor(rx * 0.92); i++) {
      const [fx, fy] = rim(i / rx);
      const h = 1 + Math.floor(hash(i + 50, k, 5) * 3);
      for (let s = 1; s <= h; s++) if (hash(i + 50, s, flick + k) > 0.3) put(Math.round(fx + lean(s) + Math.sin(t + i) * 0.4), Math.round(fy + s), (0.42 - s * 0.09) * c.b, k);
    }
    // long tentacles: thin and sparse, swaying
    const n = c.n || Math.max(5, Math.round(R * 0.7));
    for (let q = 0; q < n; q++) {
      const [ax, ay] = rim(-0.86 + (1.72 * q) / (n - 1));
      const len = L * (0.75 + 0.5 * hash(q, k, 1));
      for (let s = 0; s < len; s++) {
        if (hash(q * 97 + s, k, flick >> 1) < 0.22) continue;
        const x = ax + lean(s) + Math.sin(s * 0.3 - t * 1.2 * sp + q * 1.9 + ph) * (0.4 + s * 0.17);
        put(Math.round(x), Math.round(ay + s), Math.pow(1 - s / len, 1.1) * 0.55 * c.b * (0.7 + 0.45 * hash(q, s, flick)), k);
      }
    }
    // oral arms: three or four thick, frilled, slower strands from the centre
    const arms = R > 6 ? 4 : 3;
    for (let m = 0; m < arms; m++) {
      const [ax, ay] = rim((m - (arms - 1) / 2) * 0.17);
      const len = L * (0.8 + 0.25 * hash(m, k, 2));
      const wide = Math.max(1, Math.round(R / 4.5));
      for (let s = 0; s < len; s++) {
        const x = ax + lean(s) + Math.sin(s * 0.2 - t * 0.85 * sp + m * 2.3 + ph) * (0.3 + s * 0.13);
        const taper = 1 - s / len;
        const w = Math.max(0, Math.round(wide * taper + 0.4 * Math.sin(s * 1.3 + m)));
        for (let o = -w; o <= w; o++) {
          const v = taper * (0.88 - Math.abs(o) * 0.2) * c.b * (0.62 + 0.5 * noise(s * 0.5, (m + o) * 0.7, tz));
          put(Math.round(x) + o, Math.round(ay + s), v, k);
        }
      }
    }
  }

  function draw(t) {
    if (!cols || !rows) return;
    dens.fill(0);
    spec.forEach((c, k) => creature(c, k, t));
    const clear = clears();
    ctx.clearRect(0, 0, W, H);
    // the faint character grid of the reference
    ctx.beginPath();
    for (let i = 1; i < cols; i++) { ctx.moveTo(i * CW + 0.5, 0); ctx.lineTo(i * CW + 0.5, H); }
    for (let j = 1; j < rows; j++) { ctx.moveTo(0, j * CH + 0.5); ctx.lineTo(W, j * CH + 0.5); }
    ctx.strokeStyle = 'rgba(150, 190, 182, 0.035)'; ctx.lineWidth = 1; ctx.stroke();
    ctx.font = FONT;
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    // drifting specks
    for (const [i, j, r] of specks) {
      const a = 0.08 + 0.14 * (0.5 + 0.5 * Math.sin(t * (0.6 + r) + r * 20));
      ctx.fillStyle = 'rgba(159, 176, 212, ' + a.toFixed(3) + ')';
      ctx.fillText(r > 0.5 ? '.' : "'", i * CW + CW / 2, j * CH + CH / 2);
    }
    for (let j = 0; j < rows; j++) {
      for (let i = 0; i < cols; i++) {
        const n = j * cols + i;
        let v = dens[n];
        if (v < 0.06) continue;
        // behind words the water clears, with a soft edge two cells wide
        for (const [x0, y0, x1, y1] of clear) {
          const ox = Math.max(x0 - i, 0, i - x1), oy = Math.max(y0 - j, 0, j - y1);
          const o = Math.max(ox, oy * 1.4);
          if (o < 2) v *= 0.2 + 0.4 * o;
        }
        if (v < 0.06) continue;
        const c = spec[who[n]], rgb = TONE[c.tone] || TONE.accent, al = c.alpha ?? 1;
        const lv = Math.min(RAMP.length - 1, Math.floor(v * RAMP.length));
        const ch = RAMP[lv]; if (ch === ' ') continue;
        const mix = v > 0.86 ? (v - 0.86) * 3.2 : 0;
        const r = Math.round(rgb[0] + (232 - rgb[0]) * mix), g = Math.round(rgb[1] + (240 - rgb[1]) * mix), b = Math.round(rgb[2] + (236 - rgb[2]) * mix);
        if (v > 0.5) {
          ctx.fillStyle = 'rgba(' + rgb[0] + ', ' + rgb[1] + ', ' + rgb[2] + ', ' + (0.075 * v * al).toFixed(3) + ')';
          ctx.fillRect(i * CW + 1, j * CH + 1, CW - 1, CH - 1);
        }
        ctx.fillStyle = 'rgba(' + r + ', ' + g + ', ' + b + ', ' + ((0.16 + 0.84 * v) * al).toFixed(3) + ')';
        ctx.fillText(ch, i * CW + CW / 2, j * CH + CH / 2 + 0.5);
      }
    }
  }

  size();
  let rt = 0;
  const resized = () => { clearTimeout(rt); rt = setTimeout(() => { if (!canvas.isConnected) return; size(); if (calm.matches) draw(6); }, 120); };
  addEventListener('resize', resized);
  if (calm.matches) { draw(6); return; }
  let last = 0;
  const loop = now => {
    // A band the live page replaced stops drawing and lets its listener go.
    if (!canvas.isConnected) { removeEventListener('resize', resized); return; }
    if (now - last > 40) { last = now; draw((now - T0) / 1000); }
    requestAnimationFrame(loop);
  };
  requestAnimationFrame(loop);
}

// The composer's prompt cycles through what it is for, word by word, every 4.6 s.
function cycle(el) {
  const phrases = el.dataset.cycle.split('|');
  const words = s => s.split(' ').map((w, i, a) => {
    const b = document.createElement('span'); b.className = 'w'; b.textContent = w + (i < a.length - 1 ? ' ' : '');
    return b;
  });
  let k = 0;
  el.replaceChildren(...words(phrases[0]));
  if (calm.matches || phrases.length < 2) return;
  const id = setInterval(() => {
    if (!el.isConnected) { clearInterval(id); return; }
    if (document.hidden) return;
    const old = $$('.w', el);
    old.forEach((w, i) => w.animate([{ opacity: 1, filter: 'blur(0)' }, { opacity: 0, filter: 'blur(6px)' }],
      { duration: 260, delay: i * 25, fill: 'forwards', easing: 'cubic-bezier(.2,.7,.2,1)' }));
    setTimeout(() => {
      k = (k + 1) % phrases.length;
      el.replaceChildren(...words(phrases[k]));
      $$('.w', el).forEach((w, i) => w.animate(
        [{ opacity: 0, filter: 'blur(10px)', transform: 'translateY(3px)' }, { opacity: 1, filter: 'blur(0)', transform: 'none' }],
        { duration: 620, delay: i * 85, fill: 'backwards', easing: 'cubic-bezier(.2,.7,.2,1)' }));
    }, 260 + old.length * 25);
  }, 4600);
}

let fonts = false;
function wake() {
  // Bands and prompts that arrived with a page or a poll start here; the ones already running carry on.
  if (!fonts) return;
  for (const c of $$('canvas[data-tank]')) if (!c.dataset.on) { c.dataset.on = '1'; Tank(c); }
  for (const el of $$('[data-cycle]')) if (!el.dataset.on) { el.dataset.on = '1'; cycle(el); }
}

// ───────── the live page ─────────
function gone(el, rect) {
  if (calm.matches || !rect) return;
  const g = el.cloneNode(true);
  g.removeAttribute('data-key'); g.removeAttribute('tabindex'); g.classList.add('gone');
  g.setAttribute('aria-hidden', 'true'); g.inert = true;
  Object.assign(g.style, { position: 'fixed', left: rect.left + 'px', top: rect.top + 'px', width: rect.width + 'px',
    height: rect.height + 'px', margin: '0' });
  document.body.append(g);
  g.animate([{ opacity: 1 }, { opacity: 0, transform: 'scale(.98)' }], { duration: 260, easing: 'ease-out' })
    .finished.then(() => g.remove(), () => g.remove());
}

function sync(old, fresh, doc, before, added, changed) {
  const oh = $('.head', old), fh = $('.head', fresh);
  if (oh && fh && oh.innerHTML !== fh.innerHTML && !busy(oh)) {
    const was = $('.n', oh), now = $('.n', fh);
    oh.replaceWith(fh);
    if (now && (!was || was.textContent !== now.textContent)) play(now, 'bumped', 400);
  }
  const ob = $('.body', old), fb = $('.body', fresh);
  if (!ob || !fb) return true;
  const ol = $('.rows', ob), fl = $('.rows', fb);
  const olog = $(':scope > pre.log', ob), flog = $(':scope > pre.log', fb);
  if (olog && flog && ob.children.length === 1 && fb.children.length === 1) {
    if (olog.textContent !== flog.textContent) olog.textContent = flog.textContent;
    return true;
  }
  if (!ol || !fl) {
    if (busy(ob)) return false;
    for (const el of $$('[data-key]', ob)) if (!byKey(el.dataset.key, doc)) gone(el, before.get(el.dataset.key));
    ob.replaceWith(fb);
    for (const el of $$('[data-key]', fb)) added.push(el);
    if (!fl) play(fb, 'arrived', 400);
    return true;
  }
  let whole = true;
  const keep = new Map($$(':scope > [data-key]', ol).map(el => [el.dataset.key, el]));
  const next = [];
  for (const row of $$(':scope > [data-key]', fl)) {
    const prev = keep.get(row.dataset.key);
    keep.delete(row.dataset.key);
    if (prev && prev.dataset.sig === row.dataset.sig) next.push(prev);
    else if (prev && busy(prev)) { next.push(prev); whole = false; }
    else {
      const el = document.importNode(row, true);
      next.push(el);
      (prev ? changed : added).push(el);
    }
  }
  for (const left of keep.values()) {
    if (busy(left) && byKey(left.dataset.key, doc)) { next.push(left); whole = false; }
    else if (!byKey(left.dataset.key, doc)) gone(left, before.get(left.dataset.key));
  }
  ol.replaceChildren(...next);
  return whole;
}

function motion(before, added, changed) {
  if (!calm.matches) {
    // FLIP: each keyed element travels from where it was; a row inside a group that also moved travels only its own
    // offset, because the group's animation carries it.
    const delta = new Map();
    for (const el of $$('[data-key]')) {
      const was = before.get(el.dataset.key);
      if (!was) continue;
      const now = el.getBoundingClientRect();
      delta.set(el, [was.left - now.left, was.top - now.top]);
    }
    for (const [el, [x, y]] of delta) {
      const outer = el.parentElement && el.parentElement.closest('[data-key]');
      const carried = (outer && delta.get(outer)) || [0, 0];
      const dx = x - carried[0], dy = y - carried[1];
      if (Math.abs(dx) + Math.abs(dy) < 1) continue;
      // A row crossing groups (a new element under a key that existed) lifts onto an opaque card for the trip;
      // rows that only make room for it just slide.
      el.style.zIndex = '5';
      if (added.includes(el) && el.matches('.row')) el.classList.add('moving');
      el.animate([{ transform: 'translate(' + dx + 'px,' + dy + 'px)' }, { transform: 'none' }],
        { duration: 460, easing: 'cubic-bezier(.2,.8,.2,1)' }).finished
        .then(() => { el.style.zIndex = ''; el.classList.remove('moving'); }, () => {});
    }
  }
  for (const el of added) play(el, before.has(el.dataset.key) ? 'changed' : 'arrived');
  for (const el of changed) play(el, 'changed');
}

function mark() {
  // The page in the sidebar is marked here as well as by the server, so a link followed without a reload is marked.
  for (const a of $$('.nav a')) {
    const path = new URL(a.href, location.href).pathname;
    const here = path === '/' ? location.pathname === '/' : location.pathname === path || location.pathname.startsWith(path + '/');
    if (here) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  for (const log of $$('.log')) if (!log.dataset.held) log.scrollTop = log.scrollHeight;
  for (const b of $$('[data-copy]')) b.hidden = false;
}

function filter() {
  // The list's tabs (where a job runs; what kind of item waits) filter what is already on the page.
  const jobs = $('.jobs');
  if (jobs) {
    if (show) jobs.dataset.show = show; else delete jobs.dataset.show;
    for (const t of $$('[role="tab"][data-where]')) t.setAttribute('aria-selected', String(t.dataset.where === show));
  }
  const box = $('.ibx');
  if (box) {
    if (kind) box.dataset.show = kind; else delete box.dataset.show;
    for (const t of $$('[role="tab"][data-kind]')) t.setAttribute('aria-selected', String(t.dataset.kind === kind));
  }
}

function apply(doc, nav) {
  const now = +doc.body.dataset.now;
  if (now) skew = now - Date.now() / 1000;
  for (const k of ['open', 'pace', 'jobs']) document.body.dataset[k] = doc.body.dataset[k] || '';
  if (nav) { document.title = doc.title; show = ''; kind = ''; }
  const before = new Map($$('[data-key]').map(el => [el.dataset.key, el.getBoundingClientRect()]));
  const focused = document.activeElement;
  const focusKey = focused && focused.closest && focused.closest('[data-key]') ? focused.closest('[data-key]').dataset.key : null;
  const added = [], changed = [];
  const pane = $('.pane'), fresh = $('.pane', doc);
  // A different page, or a page whose sections changed (a job finished and grew a receipt), is swapped whole, unless
  // someone is typing in it; then only the parts that still match are updated.
  let swap = !!(pane && fresh) && (nav || pane.dataset.pane !== fresh.dataset.pane || regions(pane) !== regions(fresh));
  if (swap && !nav && busy(pane)) swap = false;
  for (const el of $$('[data-region]', doc)) {
    if (swap && fresh.contains(el)) continue;
    const old = $('[data-region="' + el.dataset.region + '"]');
    if (!old || old.dataset.sig === el.dataset.sig) continue;
    if (!$('.body', el)) {
      if (!busy(old)) { old.replaceWith(el); if (!nav && !el.hasAttribute('data-still')) play(el, 'arrived', 400); }
      continue;
    }
    if (sync(old, el, doc, before, added, changed)) old.dataset.sig = el.dataset.sig;
    old.hidden = el.hidden;
  }
  if (swap) {
    const top = nav ? 0 : pane.scrollTop;
    const had = new Set(regions(pane).split(' '));
    pane.replaceWith(fresh);
    fresh.scrollTop = top;
    if (nav) { play($('.page', fresh), 'entering', 300); window.scrollTo(0, 0); }
    else for (const el of $$('[data-region]', fresh)) if (!had.has(el.dataset.region) && !el.hasAttribute('data-still')) play(el, 'arrived', 400);
  }
  for (const el of $$('[data-tk]', doc)) {
    const mine = $('[data-tk="' + CSS.escape(el.dataset.tk) + '"]');
    if (!mine) continue;
    if (el.dataset.tk === 'tick' && Math.abs(+mine.dataset.t - +el.dataset.t) > 1.5) play($('.beat'), 'beating', 700);
    mine.dataset.t = el.dataset.t;
  }
  const banner = $('#result');
  if (banner && !nav) banner.remove();
  clock();
  mark();
  filter();
  wake();
  if (!nav) {
    if (focused && focused !== document.body && !focused.isConnected && focusKey) {
      const again = byKey(focusKey);
      if (again) (again.querySelector('a.item, a.ib') || again).focus({ preventScroll: true });
    } else if (focused && focused.isConnected && document.activeElement !== focused) focused.focus({ preventScroll: true });
    motion(before, added, changed);
  }
  const res = $('#result');
  if (res) { res.remove(); return res; }
  return doc.getElementById('result');
}

function live(state) {
  const el = $('[data-live]');
  if (!el) return;
  el.dataset.state = state;
  el.textContent = state === 'on' ? '' : state === 'off' ? 'Reconnecting…' : 'This page is closed. Run rh serve again.';
  if (state === 'ended') { ended = true; clearTimeout(timer); }
}

const pace = () => document.body.dataset.pace === 'fast' ? 1500 : 4000;
async function poll() {
  clearTimeout(timer);
  if (ended) return;
  if (!document.hidden && !inflight && !moving) {
    try {
      const here = location.href;
      const r = await fetch(location.pathname + location.search, { cache: 'no-store' });
      if (r.status === 403) return live('ended');
      if (!r.ok && r.status !== 404) throw new Error('status ' + r.status);
      const doc = parse(await r.text());
      if (inflight) stash = doc.getElementById('result') || stash;
      else if (!moving && location.href === here) { const res = apply(doc, false); if (res) report(res); }
      failures = 0; live('on');
    } catch (err) { failures += 1; live('off'); }
  }
  timer = setTimeout(poll, failures ? Math.min(30000, 4000 * 2 ** failures) : pace());
}

async function go(url, push) {
  moving += 1;
  try {
    const r = await fetch(url, { cache: 'no-store' });
    if (r.status === 403) { live('ended'); return; }
    const doc = parse(await r.text());
    if (push) history.pushState(null, '', r.url || url);
    const res = apply(doc, true);
    if (res) report(res);
    settle();
  } catch (err) { location.href = url; }
  finally { moving -= 1; }
  clearTimeout(timer); timer = setTimeout(poll, pace());
}
function settle() {
  // A new page takes focus where its work starts: the brief on New job, else the page's heading, so j/k and the
  // action keys carry on from there.
  const brief = $('.pane textarea[name="brief"]');
  if (brief && brief.getClientRects().length && document.body.dataset.open === 'new') brief.focus();
  else { const h = $('.pane h1[tabindex]') || $('.pane'); if (h) h.focus({ preventScroll: true }); }
}

function report(res) {
  const code = res.dataset.code === '' || res.dataset.code === undefined ? null : +res.dataset.code;
  const pre = $('pre', res);
  toast(res.dataset.say || 'Done.', code === null || code === 0, pre ? pre.textContent : '');
}

function toast(say, ok, out) {
  const box = $('.toasts');
  if (!box) return;
  const t = document.createElement('div');
  t.className = 'toast ' + (ok ? 'ok' : 'no');
  if (!ok) t.setAttribute('role', 'alert');
  const head = document.createElement('p');
  head.textContent = say;
  const x = document.createElement('button');
  x.type = 'button'; x.className = 'x'; x.textContent = '×';
  x.setAttribute('aria-label', 'Dismiss');
  x.addEventListener('click', () => drop(t));
  t.append(head, x);
  if (out.trim()) {
    const d = document.createElement('details'), s = document.createElement('summary'), pre = document.createElement('pre');
    s.textContent = 'Details'; pre.textContent = out.trim(); d.append(s, pre); t.append(d);
  }
  box.append(t);
  while (box.children.length > 3) box.firstElementChild.remove();
  if (ok) setTimeout(() => drop(t), 5000);
}
function drop(t) {
  if (!t.isConnected || t.classList.contains('leaving')) return;
  t.classList.add('leaving');
  setTimeout(() => t.remove(), calm.matches ? 0 : 200);
}

function pending(form) {
  // Start job answers at once: the job appears under Live as Starting while the harness adds it.
  const grp = $('.s-g-live'), brief = form.elements.namedItem('brief'), id = form.elements.namedItem('id');
  if (!grp || !brief) return null;
  const li = document.createElement('li'), a = document.createElement('div');
  li.className = 'row pending'; a.className = 'item';
  const dot = document.createElement('i'); dot.className = 'dot live pulse';
  const name = document.createElement('span'); name.className = 'name';
  name.textContent = brief.value.trim().split('\n')[0].slice(0, 80) || (id && id.value) || 'New job';
  const sub = document.createElement('span'); sub.className = 'sub'; sub.textContent = 'Starting…';
  a.append(dot, name, sub); li.append(a);
  grp.hidden = false;
  const empty = $('.no-jobs'); if (empty) empty.hidden = true;
  $('.rows', grp).prepend(li);
  play(li, 'arrived', 400);
  return li;
}

document.addEventListener('submit', async e => {
  const form = e.target;
  if (!(form instanceof HTMLFormElement) || !form.hasAttribute('data-act')) return;
  e.preventDefault();
  const btn = e.submitter || $('button:not([type="button"])', form);
  if (!btn || btn.getAttribute('aria-busy') === 'true') return;
  const label = form.dataset.label || btn.textContent.trim();
  const row = form.closest('[data-key]');
  const body = new URLSearchParams(new FormData(form));
  btn.setAttribute('aria-busy', 'true');
  const early = form.matches('.compose') ? pending(form) : null;
  inflight += 1; stash = null;
  let doc = null, code = null, say = '', out = '', reached = true, landed = null;
  try {
    const r = await fetch(form.action, { method: 'POST', body });
    if (r.status === 403) live('ended');
    doc = parse(await r.text());
    if (r.redirected && new URL(r.url).pathname !== location.pathname) landed = r.url;
    const res = doc.getElementById('result') || stash;
    if (res) {
      code = res.dataset.code === '' ? null : +res.dataset.code;
      say = res.dataset.say || '';
      out = $('pre', res) ? $('pre', res).textContent : '';
    } else if (!r.ok) code = r.status;
  } catch (err) { reached = false; say = 'The app did not answer. Is rh serve still running?'; }
  btn.removeAttribute('aria-busy');
  const ok = reached && (code === null || code === 0);
  if (!ok && early) early.remove();
  if (!(ok && landed)) toast(say || (ok ? label + ': done.' : label + ' did not work.'), ok, out);
  if (!landed) play(btn, ok ? 'ok' : 'no', ok ? 900 : 400);
  if (ok) { const d = form.closest('details'); if (d) d.open = false; if (!landed) form.reset(); }
  inflight -= 1;
  if (doc && doc.body && doc.body.dataset.now) {
    if (landed) { history.pushState(null, '', landed); apply(doc, true); settle(); }
    else apply(doc, false);
  }
  if (ok && row && !landed) { const again = byKey(row.dataset.key); if (again) (again.querySelector('a.item, a.ib') || again).focus({ preventScroll: true }); }
  if (!document.activeElement || document.activeElement === document.body) { const p = $('.pane'); if (p) p.focus({ preventScroll: true }); }
  clearTimeout(timer); timer = setTimeout(poll, pace());
});

async function copy(b) {
  const src = document.getElementById(b.dataset.copy);
  if (!src) return;
  const value = 'value' in src ? src.value : src.textContent;
  let done = false;
  try { await navigator.clipboard.writeText(value); done = true; } catch (err) {
    if ('select' in src) { src.focus(); src.select(); try { done = document.execCommand('copy'); } catch (e2) { done = false; } }
  }
  const was = b.dataset.was || b.textContent;
  b.dataset.was = was;
  b.textContent = done ? 'Copied' : 'Select it and copy';
  setTimeout(() => { b.textContent = was; }, 1600);
}

// ───────── ⌘K: Raycast's window ─────────
const pal = () => $('#pal');
const rowsOf = d => $$('.p-row', d).filter(r => !r.hidden);
function pick(row) {
  const d = pal();
  for (const r of $$('.p-row', d)) r.setAttribute('aria-selected', String(r === row));
  if (row) row.scrollIntoView({ block: 'nearest' });
}
function lit(el, q) {
  // The match in a row's title is underlined in teal.
  if (el.dataset.plain === undefined) el.dataset.plain = el.textContent;
  const text = el.dataset.plain, at = q ? text.toLowerCase().indexOf(q) : -1;
  if (at < 0) { if (el.textContent !== text || el.children.length) el.textContent = text; return; }
  const b = document.createElement('b'); b.textContent = text.slice(at, at + q.length);
  el.replaceChildren(text.slice(0, at), b, text.slice(at + q.length));
}
function find(text) {
  const d = pal();
  if (!d) return;
  const q = text.trim().toLowerCase();
  let first = null;
  for (const r of $$('.p-row', d)) {
    const hit = !q || r.textContent.toLowerCase().includes(q);
    r.hidden = !hit;
    const t = $('.p-t', r); if (t) lit(t, hit ? q : '');
    if (hit && !first) first = r;
  }
  for (const s of $$('.p-sec', d)) {
    let n = s.nextElementSibling, any = false;
    while (n && !n.matches('.p-sec, .p-none')) { if (!n.hidden) any = true; n = n.nextElementSibling; }
    s.hidden = !any;
  }
  const none = $('.p-none', d); if (none) none.hidden = !!first;
  pick(first);
}
function openPal() {
  const d = pal();
  if (!d || d.open) return;
  for (const x of $$('details.act[open]')) x.open = false;
  d.showModal();
  const q = $('.p-q', d);
  q.value = '';
  find('');
  q.focus();
}
function closePal() { const d = pal(); if (d && d.open) d.close(); }
function press(k) {
  const c = $('.pane [data-k="' + k + '"]') || $('[data-k="' + k + '"]');
  if (!c) return;
  if (c.tagName === 'SUMMARY' || c.matches('input, textarea')) activate(c);
  else c.click();
}
function run(row) {
  if (!row) return;
  closePal();
  if (row.dataset.press) press(row.dataset.press);
  else if (row.matches('a[data-nav]')) go(row.href, true);
  else if (row.href) location.href = row.href;
}
document.addEventListener('input', e => { if (e.target instanceof HTMLElement && e.target.matches('.p-q')) find(e.target.value); });
document.addEventListener('keydown', e => {
  const d = pal();
  if (!d || !d.open || !d.contains(e.target)) return;
  const list = rowsOf(d), at = list.findIndex(r => r.getAttribute('aria-selected') === 'true');
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    if (list.length) pick(list[(at + (e.key === 'ArrowDown' ? 1 : -1) + list.length) % list.length]);
  } else if (e.key === 'Enter') { e.preventDefault(); run(list[at]); }
});
document.addEventListener('mousemove', e => {
  const r = e.target.closest && e.target.closest('#pal .p-row');
  if (r && r.getAttribute('aria-selected') !== 'true') pick(r);
});

document.addEventListener('click', e => {
  for (const d of $$('details.act[open], details.opts[open]')) if (!d.contains(e.target)) d.open = false;
  const c = e.target.closest && e.target.closest('[data-copy]');
  if (c) { copy(c); return; }
  if (e.target.closest && e.target.closest('[data-pal]')) { openPal(); return; }
  const d = pal();
  if (d && e.target === d) { closePal(); return; }  // a click on the backdrop
  const row = e.target.closest && e.target.closest('#pal .p-row');
  if (row) { e.preventDefault(); run(row); return; }
  const tab = e.target.closest && e.target.closest('[role="tab"][data-where], [role="tab"][data-kind]');
  if (tab) {
    if ('where' in tab.dataset) show = tab.dataset.where; else kind = tab.dataset.kind;
    filter();
    return;
  }
  const a = e.target.closest && e.target.closest('a[data-nav]');
  if (!a || e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  if (a.href !== location.href) go(a.href, true);
});
addEventListener('popstate', () => go(location.href, false));
document.addEventListener('scroll', e => {
  const log = e.target;
  if (log instanceof Element && log.matches('.log')) {
    if (log.scrollHeight - log.scrollTop - log.clientHeight > 8) log.dataset.held = '1'; else delete log.dataset.held;
  }
}, true);

// The composer suggests a name from the first words of the brief until a name is typed; the suggestion is selected
// whole when the field is focused, so typing replaces it. It asks for a command only for a job on this machine.
const NAME_SKIP = new Set(['the', 'and', 'for', 'with', 'from', 'into', 'then', 'that', 'this', 'them', 'each',
  'every', 'one', 'all', 'its', 'are', 'was']);
document.addEventListener('input', e => {
  const t = e.target;
  if (!(t instanceof HTMLElement)) return;
  const form = t.closest('form.compose');
  if (!form) return;
  const id = form.elements.namedItem('id');
  if (t === id) { id.dataset.typed = id.value ? '1' : ''; return; }
  if (t.name === 'brief' && id && !id.dataset.typed) {
    // Content words only, each once, with file extensions dropped: "numbers.txt" adds nothing to "numbers".
    const words = [...new Set(t.value.toLowerCase().replace(/\.[a-z0-9]+\b/g, ' ').replace(/[^a-z0-9\s-]/g, ' ')
      .split(/\s+/).filter(w => w.length > 2 && !NAME_SKIP.has(w)))].slice(0, 3);
    let name = words.join('-').slice(0, 48).replace(/-+$/, ''), n = 2;
    const base = name;
    while (name && byKey('job:' + name)) name = base + '-' + n++;
    id.value = name;
  }
});
let picked = null;
document.addEventListener('focusin', e => {
  const t = e.target;
  if (t instanceof HTMLInputElement && t.matches('form.compose input[name="id"]')) { t.select(); picked = t; }
});
document.addEventListener('mouseup', e => { if (picked && e.target === picked) e.preventDefault(); picked = null; });
document.addEventListener('change', e => {
  const form = e.target.closest && e.target.closest('form.compose');
  if (!form) return;
  const cmd = form.elements.namedItem('command'), k = form.querySelector('input[name="kind"]:checked');
  if (cmd && k) cmd.required = k.value === 'local-shell';
});

const items = () => $$('.pane a.item, .pane a.ib').filter(el => el.getClientRects().length || el.parentElement.getClientRects().length);
function move(step) {
  const list = items();
  if (!list.length) return;
  const cur = document.activeElement && document.activeElement.closest('a.item, a.ib');
  let i = list.indexOf(cur);
  if (i < 0) i = list.findIndex(a => a.hasAttribute('aria-current'));
  i = i < 0 ? (step >= 0 ? 0 : list.length - 1) : Math.max(0, Math.min(list.length - 1, i + step));
  list[i].focus({ preventScroll: true });
  (list[i].closest('.row, .ib-li') || list[i]).scrollIntoView({ block: 'nearest', behavior: calm.matches ? 'auto' : 'smooth' });
}
function closeOthers(keep) { for (const d of $$('details.act[open]')) if (d !== keep) d.open = false; }
const field = d => $('input:not([type="hidden"]):not([type="checkbox"]), textarea', d);
function activate(c) {
  const d = c.closest('details');
  if (d && c.tagName === 'SUMMARY') { closeOthers(d); d.open = true; (field(d) || c).focus(); }
  else c.focus();
}
document.addEventListener('toggle', e => {
  const d = e.target;
  if (!(d instanceof HTMLDetailsElement) || !d.open || !d.matches('.act')) return;
  closeOthers(d);
  // Opened by mouse or key, the popover's first field takes focus so typing lands in it.
  const f = field(d), at = document.activeElement;
  if (f && !(d.contains(at) && at.matches('input, textarea'))) f.focus();
}, true);
document.addEventListener('keydown', e => {
  const t = e.target;
  if (e.key.toLowerCase() === 'k' && (e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey) {
    e.preventDefault();
    const d = pal();
    if (d && d.open) closePal(); else openPal();
    return;
  }
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && t instanceof HTMLTextAreaElement && t.form
      && t.form.hasAttribute('data-act')) { e.preventDefault(); t.form.requestSubmit(); return; }
  if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || e.isComposing) return;
  if (e.key === 'Escape') {
    const d = t.closest && t.closest('details[open]');
    if (d) { d.open = false; $('summary', d).focus(); e.preventDefault(); return; }
    if (t.matches && t.matches('input, textarea')) { t.blur(); e.preventDefault(); return; }
    const last = $('.toasts .toast:last-child');
    if (last && !$('dialog[open]')) { drop(last); e.preventDefault(); }
    return;
  }
  if (t.closest && t.closest('input, textarea, select, [contenteditable], dialog')) return;
  const keys = $('#keys');
  const row = t.closest && t.closest('.row, .ib-li');
  const act = k => (row && row.querySelector('[data-k="' + k + '"]')) || $('.pane [data-k="' + k + '"]');
  const inbox = $('.nav a[href^="/inbox"]');
  if (e.key === 'j' || e.key === 'ArrowDown') move(1);
  else if (e.key === 'k' || e.key === 'ArrowUp') move(-1);
  else if (e.key === '?' && keys) keys.showModal();
  else if (e.key === 'i' && inbox) go(inbox.href, true);
  else if (e.key === 'n' && $('[data-k="n"]')) {
    const f = $('.pane textarea[name="brief"]');
    if (f && f.getClientRects().length) f.focus(); else go($('[data-k="n"]').href, true);
  }
  else if (e.key === 't' && $('[data-k="t"]')) $('[data-k="t"]').focus();
  else if (/^[rahusclx]$/.test(e.key) && act(e.key)) {
    const c = act(e.key);
    if (c.matches('a[data-nav]')) go(c.href, true); else activate(c);
  }
  else return;
  e.preventDefault();
});
document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });

const keysButton = $('[data-keys]');
if (keysButton) { keysButton.hidden = false; keysButton.addEventListener('click', () => $('#keys').showModal()); }
const first = $('#result');
if (first) { report(first); first.remove(); }
skew = (+document.body.dataset.now || Date.now() / 1000) - Date.now() / 1000;
clock();
mark();
filter();
(document.fonts ? document.fonts.ready : Promise.resolve()).then(() => { fonts = true; wake(); });
setInterval(clock, 1000);
live('on');
timer = setTimeout(poll, pace());
})();
