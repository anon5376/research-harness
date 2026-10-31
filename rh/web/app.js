// The client for the live page, served same-origin at /app.js. It polls the page it shows, swaps only what changed
// (FLIP for rows that move between groups), follows app links without reloading, posts forms with fetch and says what
// happened in a toast, and adds the keyboard and the ⌘K palette. It also draws the sea: each seat as a jellyfish of
// dots, with fish, bacteria, plankton, an eel and an octopus that count what the page shows. Everything that moves
// stops under prefers-reduced-motion; the sea reads it when the page loads, so a change while the page is open takes a
// reload.
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

// ───────── the sea: dot creatures on one lattice of tall cells ─────────
// Drawn the way 0xSero's ascii-art skill draws dot art: every creature is dots on one fixed grid of cells about 1.9
// times as tall as wide, in two tones (a big bright dot, and a small dim one nudged down), lit from the upper left, and
// red only on what moves. Plankton are a finer grain, a different substance from the creatures.
// Each creature stands for something the page shows: a jellyfish per seat (teal while it works, amber while it waits
// on you, dim while idle), a fish per job running, a school of small fish for the jobs done today, a colony of
// bacteria per job that failed today, plankton for the jobs up next, the eel while the harness is being checked, and
// the octopus on the sea floor, amber while anything needs you. The fish, the school and the bacteria swim in the open
// water between the jellyfish and the octopus.
// <sea> From here to </sea> the code touches no page, so a test runs it in node.
const FRAME = 46;  // ms between frames: about 20 a second on a 60 or 120 Hz screen
const LATTICE = 1.887;  // a cell's height over its width, the skill's 19 x 36 lattice
const BRIGHT = 2, DIM = 1, RING = 3, FINE = 0;  // a big dot, a small one, an eye (a big dot with a hollow), a speck
const TONES = { accent: [95, 207, 185], amber: [226, 166, 84], faint: [159, 176, 212], red: [239, 107, 115],
  sand: [150, 160, 150], weed: [95, 160, 140] };
// Dot maps: '#' a bright dot, '.' a dim one, 'o' an eye, anything else water. Fish face right; a map is mirrored to
// swim left.
const FISH = ['##.   .######.  ', ' .##.#########o#', '##.   .######.  '];  // a forked tail and a whole body
const MINNOW = ['#. .###. ', '.#.####o#', '#. .###. '];
const OCTOPUS = ['  .#####.  ', ' ##.....## ', ' #.o...o.# ', ' .#######. '];
const JW = 15, JH = 12;  // a jellyfish's box in cells: its bell, its tentacles and its seat's name under them
const OW = 14, OH = 8;  // the octopus's box: head and arms
const EEL = 14;  // the eel's length in cells
// the school: [dx, dy] of each small fish from the leader; fish on one row are a whole fish apart
const SCHOOL = [[0, 0], [-6, -3], [-6, 3], [-12, -6], [-12, 0], [-12, 6], [-18, -3], [-18, 3], [-24, 0]];
const hash = (a, b, c) => {
  let h = Math.imul(a | 0, 374761393) ^ Math.imul(b | 0, 668265263) ^ Math.imul(c | 0, 1274126177);
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
};
const flip = map => map.map(r => r.split('').reverse().join(''));

// The lattice's pitch in px for a tank: big enough to read, small enough that every seat's jellyfish fits whole.
function seaPitch(W, H, n, sea) {
  if (!sea) return Math.max(3, Math.min(6, H / ((JH + 2) * LATTICE)));
  if (H > W * 1.2) return Math.max(3, Math.min(7, W / (3 * JW + 2)));  // the reef: a column beside the list
  return Math.max(2.5, Math.min(7, H / ((JH + 1) * LATTICE), W / (Math.max(1, n) * JW + OW + 3)));  // a strip
}

// Where each seat's jellyfish lives: [x, y, slack], the top-left cell of its box and how far it may drift sideways
// without touching its neighbour. In the reef the seats take up to three lanes, the middle one set half a jellyfish
// lower; in a strip they stand in one row beside the octopus; in a small tank (a job's head) a creature sits where the
// page put it.
function seaLayout(spec, fauna, cols, rows) {
  const n = spec.length, sea = !!(fauna && fauna.floor);
  if (!sea) {
    return spec.map(c => [Math.max(0, Math.min(cols - JW, Math.round(c.fx * cols - JW / 2))),
      Math.max(0, Math.min(rows - JH, Math.round(c.fy * rows - JH / 2))), 1]);
  }
  const tall = rows > 30;
  if (!tall) {
    const room = Math.max(JW, cols - OW - 1), step = n ? room / n : 0;
    return spec.map((c, k) => [OW + 1 + Math.max(0, Math.min(room - JW, Math.round(k * step + (step - JW) / 2))),
      Math.max(0, Math.floor((rows - JH) / 2)), Math.max(0, Math.floor((step - JW) / 2))]);
  }
  const lanes = Math.max(1, Math.min(n, 3, Math.floor(cols / JW))), tiers = Math.ceil(n / lanes);
  const room = rows - OH - 4;  // the octopus and the floor keep the bottom rows
  const th = Math.max(4, Math.min(JH + 2, Math.floor((room - 1) / (tiers + (lanes > 1 ? 0.5 : 0)))));
  return spec.map((c, k) => {
    const lane = k % lanes, tier = Math.floor(k / lanes);
    const x = Math.round((lane + 0.5) * cols / lanes - JW / 2);
    const y = 1 + tier * th + (lane % 2 ? Math.round(th / 2) : 0);
    return [Math.max(0, Math.min(cols - JW, x)), Math.max(0, Math.min(rows - JH, y)),
      Math.max(0, Math.floor((cols / lanes - JW) / 2))];
  });
}

// One swimmer's way back and forth across [0, cols - width): where it is at time t and which way it faces. It turns
// round at each wall, so it is always drawn whole.
function lap(n, t, cols, width, speed) {
  const span = Math.max(1, cols - width), f = (hash(n, 8, 4) + (t * speed) / (2 * span)) % 1;
  const out = f < 0.5, p = out ? f * 2 : 2 - f * 2, dir = hash(n, 2, 6) > 0.5 ? 1 : -1;
  return { x: Math.round((dir > 0 ? p : 1 - p) * span), dir: out ? dir : -dir };
}

// Every dot of the sea at time t, in cells: [x, y, kind, tone, alpha]; the seats' names to write under their
// jellyfish; and each creature's box, for the test that checks every creature is drawn whole.
function seaDots(spec, fauna, cols, rows, t) {
  const dots = [], labels = [], parts = [];
  const put = (x, y, kind, tone, a) => { dots.push([x, y, kind, tone, a]); };
  const map = (rowsOf, x0, y0, tone, a, part) => {
    rowsOf.forEach((row, j) => {
      for (let i = 0; i < row.length; i++) {
        const ch = row[i];
        if (ch === '#') put(x0 + i, y0 + j, BRIGHT, tone, a);
        else if (ch === '.') put(x0 + i, y0 + j, DIM, tone, a);
        else if (ch === 'o') put(x0 + i, y0 + j, RING, tone, a);
      }
    });
    parts.push({ ...part, box: [x0, y0, x0 + rowsOf[0].length, y0 + rowsOf.length] });
  };
  const slots = seaLayout(spec, fauna, cols, rows);
  let below = 0;
  spec.forEach((c, k) => {
    const sp = c.sp || 1, ph = k * 1.7, tone = TONES[c.tone] ? c.tone : 'faint', a = tone === 'faint' ? 0.78 : 0.95;
    // it bobs about its slot, never past its own box's edge
    let [x0, y0, slack] = slots[k];
    x0 = Math.max(0, Math.min(cols - JW, x0 + Math.round(Math.sin(t * 0.22 * sp + ph) * slack)));
    y0 = Math.max(0, Math.min(rows - JH, y0 + Math.round(Math.sin(t * 0.35 * sp + ph))));
    below = Math.max(below, y0 + JH);
    // the bell: a solid dome, lit on its upper left and dim on its shaded side, over a frilled lip; it opens and
    // closes as it swims
    const pulse = Math.sin(t * (tone === 'accent' ? 1.7 : 0.9) * sp + ph);
    const cx = x0 + 7, cy = y0 + 3.7, rx = 6.6 * (1 + 0.04 * pulse), ry = 3.8 * (1 - 0.05 * pulse);
    for (let j = 0; j <= 4; j++) {
      for (let i = 0; i < JW; i++) {
        const dx = (x0 + i - cx) / rx, dy = (y0 + j - cy) / ry;
        if (dy > 0.1) continue;
        const d = Math.hypot(dx, dy);
        if (d > 1.04) continue;
        const kind = j === 4 ? ((i + Math.floor(t * 0.8 * sp)) % 2 ? BRIGHT : DIM)
          : dx * 0.55 + dy * 0.85 < -0.12 || (d > 0.8 && dy < -0.5) ? BRIGHT : DIM;
        put(x0 + i, y0 + j, kind, tone, a);
      }
    }
    // tentacles: a wave runs from root to tip, and the tips swing most
    for (let q = 0; q < 5; q++) {
      const len = 4 + Math.round(2 * hash(q, k, 1));
      for (let s = 0; s < len; s++) {
        const sway = Math.sin(t * 1.1 * sp - s * 0.8 + q * 1.3 + ph) * (0.3 + s * 0.28);
        put(Math.round(cx - 5 + q * 2.5 + sway), y0 + 5 + s, s === len - 1 && q % 2 ? BRIGHT : DIM, tone,
          a * (1 - s / (len * 2)));
      }
    }
    // two heavier oral arms in the middle, the lit one bright
    for (let m = 0; m < 2; m++) {
      for (let s = 0; s < 5; s++) {
        put(Math.round(cx - 1 + m * 2 + Math.sin(t * 0.7 * sp - s * 0.6 + m * 2 + ph) * (0.2 + s * 0.22)), y0 + 5 + s,
          m ? DIM : BRIGHT, tone, a * (1 - s / 7));
      }
    }
    if (c.name && fauna && fauna.floor) labels.push([x0 + JW / 2, y0 + JH - 0.5, String(c.name), tone]);
    parts.push({ kind: 'jelly', name: c.name, box: [x0, y0, x0 + JW, y0 + JH] });
  });
  if (!fauna) return { dots, labels, parts };
  const f = fauna, floor = f.floor ? 1 : 0;
  // plankton: a fine drift rising everywhere, and a brighter cloud for each job up next
  const specks = Math.round(cols * rows * 0.012);
  for (let i = 0; i < specks; i++) {
    const r = hash(i, 13, 9), y = ((hash(i, 11, 5) * rows - t * (0.15 + 0.25 * r)) % rows + rows) % rows;
    put(hash(i, 7, 3) * cols + Math.sin(t * 0.3 + i) * 0.8, y, FINE, 'faint', 0.14 + 0.2 * (0.5 + 0.5 * Math.sin(t + r * 20)));
  }
  for (let q = 0; q < Math.min(12, f.plankton || 0); q++) {
    const cx = (0.1 + 0.8 * hash(q, 21, 4)) * cols + Math.sin(t * 0.12 + q) * 3;
    const cy = ((hash(q, 22, 5) * rows - t * 0.2) % rows + rows) % rows;
    for (let s = 0; s < 7; s++) {
      put(cx + Math.cos(s * 0.9 + t * 0.4 + q) * (0.6 + s * 0.25), cy + Math.sin(s * 1.3 + t * 0.3) * 0.7, FINE, 'faint', 0.6);
    }
    parts.push({ kind: 'plankton', box: [cx, cy, cx, cy] });
  }
  // the octopus on the sea floor, or where the page puts it in a small tank; it blinks and curls its arms
  if (f.octopus) {
    const o = f.octopus, tone = TONES[o.tone] ? o.tone : 'faint', a = tone === 'faint' ? 0.55 : 0.95;
    const ox = o.at ? Math.round(o.at[0] * cols - OW / 2) : 0;
    const oy = o.at ? Math.round(o.at[1] * rows - OH / 2) : rows - OH - floor;
    const x = Math.max(0, Math.min(cols - OW, ox)), y = Math.max(0, Math.min(rows - OH, oy));
    const head = t % 5.3 < 0.18 ? OCTOPUS.map(r => r.replace(/o/g, '.')) : OCTOPUS;
    map(head, x + 1, y, tone, a, { kind: 'octopus' });
    parts[parts.length - 1].box = [x, y, x + OW, y + OH];
    for (let m = 0; m < 5; m++) {
      const out = m < 2 ? -1 : m > 2 ? 1 : 0;
      for (let s = 0; s < 4; s++) {
        put(Math.round(x + 2 + m * 2.2 + out * s * 0.5 + Math.sin(t * 0.9 - s * 0.9 + m * 1.7) * s * 0.3), y + 4 + s,
          s ? DIM : BRIGHT, tone, a * (1 - s * 0.15));
      }
    }
  }
  if (!f.floor) return { dots, labels, parts };
  // A strip (a phone's sea) has room for the seats and the octopus only; the rest swims in the reef.
  if (rows <= 30) return seaFloor(dots, cols, rows, t, labels, parts);
  // The open water under the jellyfish and above the octopus, when there is room, else the whole water: the fish, the
  // school and the bacteria all swim there, so the middle of the reef is never empty while there is anything to show.
  const top = below + 4 < rows - OH - 6 ? below + 1 : 1, bottom = rows - OH - 3;
  const depth = (n, h) => Math.max(1, Math.min(rows - h - 2, Math.round(top + hash(n, 9, 5) * Math.max(0, bottom - top - h))));
  for (let n = 0; n < Math.min(9, f.fish || 0); n++) {  // a fish for each job running
    const w = FISH[0].length, { x, dir } = lap(n, t, cols, w, 2.2 + 1.4 * hash(n, 7, 3));
    const y = depth(n, 3) + Math.round(Math.sin(t * 0.4 + n * 2) * 0.6);
    map(dir > 0 ? FISH : flip(FISH), x, Math.max(0, Math.min(rows - 3, y)), 'accent', 0.95, { kind: 'fish' });
  }
  const school = Math.min(SCHOOL.length, f.school || 0);
  if (school) {  // the jobs done today swim together, the first one leading
    const crew = SCHOOL.slice(0, school), lo = Math.min(...crew.map(([, dy]) => dy));
    const high = Math.max(...crew.map(([, dy]) => dy)) - lo + 3;
    const back = Math.max(...crew.map(([dx]) => -dx)), w = back + MINNOW[0].length;
    const { x, dir } = lap(31, t, cols, w, 1.1), y = Math.min(depth(31, high), Math.max(1, bottom - high)) - lo;
    crew.forEach(([dx, dy]) => {
      const mx = dir > 0 ? x + back + dx : x - dx;
      map(dir > 0 ? MINNOW : flip(MINNOW), mx, Math.max(0, Math.min(rows - 3, y + dy)), 'accent', 0.75, { kind: 'minnow' });
    });
  }
  // a colony for each job that failed today: red, the one thing that moves on its own here. Rods (a lit capsule of
  // dots, with a flagellum) and cocci (pairs of round cells) on half a row's pitch; every 8 s each one divides.
  for (let n = 0; n < Math.min(7, f.bacteria || 0); n++) {
    const cx = Math.max(5, Math.min(cols - 5, (0.12 + 0.76 * hash(n, 12, 7)) * cols + Math.sin(t * 0.13 + n) * 3));
    const band = Math.max(0, bottom - top - 4);
    const cy = Math.max(2, Math.min(rows - 3, top + 2 + (0.1 + 0.8 * hash(n, 13, 8)) * band + Math.sin(t * 0.11 + n * 2) * 1.2));
    const phase = (t / 8 + hash(n, 3, 3)) % 1, split = phase > 0.72 ? (phase - 0.72) / 0.28 : 0;  // it divides
    const ang = Math.sin(t * 0.2 + n) * 0.5, ux = Math.cos(ang), uy = Math.sin(ang) * 0.5, gap = split * 1.6;
    const at = (along, v, kind, a) => put(cx + ux * along, cy + uy * along + v, kind, 'red', a);
    if (n % 2 === 0) {
      for (let s = 0; s < 6; s++) {
        const along = (s - 2.5) * 0.85 + (s < 3 ? -gap / 2 : gap / 2);
        at(along, -0.25, s === 0 ? DIM : BRIGHT, 0.9);
        at(along, 0.25, DIM, 0.8);
      }
      for (let s = 1; s <= 3; s++) at(-2.6 - gap / 2 - s * 0.6, Math.sin(t * 3 + s + n) * 0.3, FINE, 0.7);
    } else {
      [-1, 1].forEach(side => {
        const c = side * (0.75 + gap / 2);
        at(c - 0.35, -0.25, side < 0 ? BRIGHT : DIM, 0.9); at(c + 0.35, -0.25, BRIGHT, 0.9);
        at(c - 0.35, 0.25, DIM, 0.8); at(c + 0.35, 0.25, DIM, 0.8);
      });
    }
    parts.push({ kind: 'bacteria', box: [cx - 5, cy - 2, cx + 5, cy + 2] });
  }
  if (f.eel) {  // the eel weaves back and forth just above the sand while the harness is being checked
    const lane = lap(47, t, cols - OW, EEL, 2.6), x = lane.x + OW, dir = lane.dir, base = rows - 3;
    for (let s = 0; s < EEL; s++) {
      const ex = dir > 0 ? x + EEL - 1 - s : x + s, ey = base + Math.round(0.9 * Math.sin(s * 0.5 - t * 2.2));
      put(ex, ey, s === 1 ? RING : s < 6 ? BRIGHT : DIM, 'accent', s < 6 ? 0.85 : 0.85 - s * 0.03);
      if (s > 3 && s < EEL - 2 && s % 2 === 0) put(ex, ey - 1, DIM, 'accent', 0.45);
    }
    parts.push({ kind: 'eel', box: [x, base - 2, x + EEL, base + 2] });
  }
  return seaFloor(dots, cols, rows, t, labels, parts);
}

// The sea floor: sand along the bottom row, and a few weeds swaying on it.
function seaFloor(dots, cols, rows, t, labels, parts) {
  const put = (x, y, kind, tone, a) => { dots.push([x, y, kind, tone, a]); };
  for (let i = 0; i < cols; i++) {
    const h = hash(i, 3, 17);
    if (h > 0.45) put(i, rows - 1, DIM, 'sand', 0.35);
    if (h > 0.93 && i > OW + 1) {
      const tall = 2 + Math.floor(hash(i, 4, 9) * 3);
      for (let s = 1; s <= tall; s++) {
        put(i + Math.round(Math.sin(t * 0.7 + i + s * 0.8) * 0.6 * s / tall), rows - 1 - s, s === tall ? BRIGHT : DIM, 'weed', 0.5);
      }
    }
  }
  return { dots, labels, parts };
}
// </sea>

const T0 = performance.now() - 6000;  // one clock for every tank, so a redrawn tank carries on where it was
function Tank(canvas) {
  let spec = [], fauna = null;
  try { spec = JSON.parse(canvas.dataset.creatures || '[]'); } catch (err) { spec = []; }
  try { fauna = JSON.parse(canvas.dataset.fauna || 'null'); } catch (err) { fauna = null; }
  const ctx = canvas.getContext('2d');
  const host = canvas.parentElement;
  if (!ctx || !host) return;
  let W = 0, H = 0, P = 6, Q = 11, cols = 0, rows = 0;
  const ground = getComputedStyle(document.documentElement).getPropertyValue('--ground').trim() || '#0b0e0e';
  function size() {
    const r = canvas.getBoundingClientRect();
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    W = r.width; H = r.height;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    P = seaPitch(W, H, spec.length, !!(fauna && fauna.floor)); Q = P * LATTICE;
    cols = Math.floor(W / P); rows = Math.floor(H / Q);
  }
  // Words sit on clear water: dots fade behind every [data-clear] element in the tank's box.
  function clears() {
    const box = host.getBoundingClientRect();
    return $$('[data-clear]', host).filter(el => el.getClientRects().length).map(el => {
      const b = el.getBoundingClientRect();
      return [(b.left - box.left) / P - 1, (b.top - box.top) / Q - 0.5, (b.right - box.left) / P + 1, (b.bottom - box.top) / Q + 0.5];
    });
  }
  function draw(t) {
    if (!cols || !rows) return;
    const { dots, labels } = seaDots(spec, fauna, cols, rows, t);
    const clear = clears();
    ctx.clearRect(0, 0, W, H);
    // one path per tone, size and alpha, so a frame is a few dozen fills however many dots it has
    const groups = new Map(), rings = [];
    for (const [x, y, kind, tone, alpha] of dots) {
      let a = alpha;
      for (const [x0, y0, x1, y1] of clear) {
        const o = Math.max(Math.max(x0 - x, 0, x - x1), Math.max(y0 - y, 0, y - y1) * 1.6);
        if (o < 2) a *= 0.15 + 0.4 * o;
      }
      if (a < 0.04) continue;
      const key = tone + '|' + kind + '|' + Math.round(a * 20);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push([x, y]);
      if (kind === RING) rings.push([x, y]);
    }
    for (const [key, list] of groups) {
      const [tone, k, a] = key.split('|'), kind = +k, rgb = TONES[tone];
      ctx.fillStyle = 'rgba(' + rgb[0] + ', ' + rgb[1] + ', ' + rgb[2] + ', ' + (a / 20).toFixed(2) + ')';
      const r = kind === DIM ? P * 0.19 : kind === FINE ? Math.max(0.8, P * 0.16) : P * 0.4;
      ctx.beginPath();
      for (const [x, y] of list) {
        const px = kind === FINE ? x * P : (x + 0.5) * P, py = kind === FINE ? y * Q : (y + 0.5) * Q + (kind === DIM ? Q * 0.19 : 0);
        ctx.moveTo(px + r, py); ctx.arc(px, py, r, 0, Math.PI * 2);
      }
      ctx.fill();
    }
    if (rings.length) {  // an eye is a bright dot with the water showing through its middle
      ctx.fillStyle = ground; ctx.beginPath();
      for (const [x, y] of rings) { const px = (x + 0.5) * P, py = (y + 0.5) * Q; ctx.moveTo(px + P * 0.15, py); ctx.arc(px, py, P * 0.15, 0, Math.PI * 2); }
      ctx.fill();
    }
    if (labels.length) {
      ctx.font = Math.max(9, Math.min(11, Q * 0.8)) + 'px "Geist Mono", ui-monospace, monospace';
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      for (const [x, y, name, tone] of labels) {
        ctx.fillStyle = tone === 'faint' ? 'rgba(128, 139, 136, .9)' : 'rgba(' + TONES[tone].join(', ') + ', .9)';
        ctx.fillText(name, x * P, y * Q);
      }
    }
  }

  size();
  let rt = 0;
  const resized = () => { clearTimeout(rt); rt = setTimeout(() => { if (!canvas.isConnected) return; size(); if (calm.matches) draw(6); }, 120); };
  addEventListener('resize', resized);
  if (calm.matches) { draw(6); return; }
  // About 20 frames a second, and none at all while the tab is hidden or the tank is scrolled out of view.
  let last = 0, raf = 0, seen = true;
  const io = 'IntersectionObserver' in window
    ? new IntersectionObserver(es => { seen = es[es.length - 1].isIntersecting; go(); }) : null;
  const go = () => { if (!raf && !document.hidden && seen && canvas.isConnected) raf = requestAnimationFrame(loop); };
  const loop = now => {
    raf = 0;
    // A tank the live page replaced stops drawing and lets its listeners go.
    if (!canvas.isConnected) {
      removeEventListener('resize', resized); document.removeEventListener('visibilitychange', go);
      if (io) io.disconnect();
      return;
    }
    if (document.hidden || !seen) return;
    if (now - last >= FRAME) { last = now; draw((now - T0) / 1000); }
    raf = requestAnimationFrame(loop);
  };
  document.addEventListener('visibilitychange', go);
  if (io) io.observe(canvas);
  go();
}

let fonts = false;
function wake() {
  // Tanks that arrived with a page or a poll start here; the ones already running carry on.
  if (!fonts) return;
  for (const c of $$('canvas[data-tank]')) if (!c.dataset.on) { c.dataset.on = '1'; Tank(c); }
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
  // A clipped box offers Show all only when it holds more than it shows.
  for (const c of $$('[data-clip]')) {
    const box = c.firstElementChild, more = c.nextElementSibling && $('[data-more]', c.nextElementSibling);
    const whole = box.scrollHeight <= box.clientHeight + 1;
    c.classList.toggle('whole', whole && !c.classList.contains('open'));
    if (more) more.hidden = whole && !c.classList.contains('open');
  }
}
function more(b) {
  const c = b.parentElement.previousElementSibling, box = c && c.firstElementChild;
  if (!box) return;
  const open = !c.classList.contains('open');
  c.classList.toggle('open', open);
  box.style.height = open ? box.scrollHeight + 'px' : '';
  b.textContent = open ? 'Show less' : 'Show all';
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
  const m = e.target.closest && e.target.closest('[data-more]');
  if (m) { more(m); return; }
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
// On a phone, Today's composer is its one line until it is used. It stays open while focus is in it, and for a moment
// after focus leaves it empty, so a tap on Codex cloud (which takes no focus) lands before the controls fold away.
document.addEventListener('focusin', e => {
  const form = e.target.closest && e.target.closest('.c-strip form.compose');
  if (form) { clearTimeout(form.shut); form.dataset.open = ''; }
});
document.addEventListener('focusout', e => {
  const form = e.target.closest && e.target.closest('.c-strip form.compose');
  if (form) form.shut = setTimeout(() => { if (!form.contains(document.activeElement)) delete form.dataset.open; }, 400);
});
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
