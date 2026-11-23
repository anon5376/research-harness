// The client for the live page, served same-origin at /app.js. It polls the page it shows, swaps only what changed
// (FLIP for rows that move between groups), follows app links without reloading, posts forms with fetch and says what
// happened in a toast, and adds the keyboard and the ⌘K palette. It also draws the sea in characters: each seat as
// a jellyfish, with fish, bacteria, plankton, an eel and an octopus that count what the page shows. Everything that
// moves stops under prefers-reduced-motion; the sea reads it when the page loads, so a change while the page is open
// takes a reload.
(() => {
// The composer's two pickers (model, effort): declared first, since the composer syncs them while the page starts.
let modelPanel = null;
const EFFORT_WORDS = {light: 'Light', medium: 'Medium', high: 'High', 'extra-high': 'Extra High', max: 'Max', ultra: 'Ultra'};
let effortPanel = null;
'use strict';
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const calm = matchMedia('(prefers-reduced-motion: reduce)');
const light = matchMedia('(prefers-color-scheme: light)');
const compact = matchMedia('(max-width: 900px)');
const sidebarToggle = $('.sidebar-toggle');
const byKey = (k, r = document) => r.querySelector('[data-key="' + CSS.escape(k) + '"]');
let skew = 0, inflight = 0, failures = 0, timer = 0, stash = null, ended = false, moving = 0, show = '', kind = '';

const span = s => s < 60 ? s + ' s' : s < 3600 ? Math.floor(s / 60) + ' min'
  : s < 86400 ? Math.floor(s / 3600) + ' h' : Math.floor(s / 86400) + ' d';
const rel = s => s >= 0 ? span(s) + ' ago' : 'in ' + span(-s);
function setSidebar(open, refocus = false) {
  if (!sidebarToggle) return;
  open = open && compact.matches;
  $('.app').classList.toggle('side-open', open);
  $('.pane').inert = open;
  sidebarToggle.setAttribute('aria-expanded', String(open));
  if (refocus) sidebarToggle.focus();
}
function clock() {
  for (const el of $$('[data-t]')) {
    const s = Math.round(Date.now() / 1000 + skew - +el.dataset.t);
    const text = el.dataset.short ? span(Math.max(0, s))
      : s >= 0 && el.dataset.due ? el.dataset.due : (el.dataset.pre || '') + rel(s);
    if (el.textContent !== text) el.textContent = text;
  }
}
function play(el, cls, ms) {
  if (!el) return;
  el.classList.remove(cls); void el.offsetWidth; el.classList.add(cls);
  setTimeout(() => el.classList.remove(cls), ms || 1400);
}
const parse = html => new DOMParser().parseFromString(html, 'text/html');
const busy = el => !!((el.matches && el.matches('dialog[open]'))
  || el.querySelector('details[open]:not(.jd-inspector, .worked), dialog[open], [aria-busy="true"]')
  || (el.contains(document.activeElement) && document.activeElement.matches('input, textarea, select')));
const regions = el => $$('[data-region]', el).map(r => r.dataset.region).join(' ');

// ───────── the sea: creatures drawn in characters on one lattice of tall cells ─────────
// Every creature is weight laid on one fixed grid of cells about 1.9 times as tall as wide, and each cell shows a
// character as heavy as the weight it holds, so the sea reads as ASCII. It moves the way water does: a creature keeps
// the fractions of its position and spreads its weight over the cells around it, so it glides across them rather than
// stepping; a bell swells and contracts as it swims; tentacles and fish bodies bend in waves that run to their tips.
// Parts lit from the upper left weigh more than shaded ones, and red is only on what moves. Plankton are a finer
// grain, dots rather than characters.
// Each creature stands for something the page shows: a jellyfish per seat (teal while it works, amber while it waits
// on you, dim while idle), a fish per job running (at most 9), a school of up to 9 small fish for the jobs done today,
// a colony of bacteria per job that failed today (at most 7), a cloud of plankton per job up next (at most 12), the
// eel while the harness is being checked, and the octopus near the floor, amber while anything needs you. The fish,
// the school and the bacteria swim in the open water between the jellyfish and the octopus; once the seats reach
// down near the octopus (8 seats in a reef 760 px tall, 14 in one 1100 px tall), they use the whole water.
// <sea> From here to </sea> the code touches no page, so a test runs it in node.
const FRAME = 46;  // ms between frames: about 20 a second on a 60 or 120 Hz screen
const LATTICE = 1.887;  // a cell's height over its width, the skill's 19 x 36 lattice
const BRIGHT = 2, DIM = 1, RING = 3, FINE = 0;  // a lit part, a shaded one, an eye, a speck of plankton
const WEIGHT = [0, 0.55, 1, 1];  // what a dot of each kind lays on the cells under it
const RAMP = ".':;tox%SX8#@";  // the characters, from the lightest weight a cell can hold to the heaviest
const TONES = { accent: [95, 207, 185], amber: [226, 166, 84], faint: [159, 176, 212], red: [239, 107, 115],
  sand: [150, 160, 150], weed: [95, 160, 140] };
// Dot maps: '#' a lit part, '.' a shaded one, 'o' an eye, anything else water. Fish face right; a map is mirrored to
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

// The tank's lattice: column pitch P and row pitch Q = P * LATTICE at every size, so a creature keeps its shape and
// only its size changes; cols and rows are the whole cells that fit.
function seaGrid(W, H, n, sea) {
  const P = seaPitch(W, H, n, sea), Q = P * LATTICE;
  return { P, Q, cols: Math.floor(W / P), rows: Math.floor(H / Q) };
}

// A character's size in px: nearly the cell's height, but no wider than its column (a mono character is ADVANCE of
// its size wide), so neighbours never touch. A speck of plankton stays at least a pixel clear of the next column.
const ADVANCE = 0.6;
const glyphSize = (P, Q) => Math.min(Q * 0.9, P * 0.95 / ADVANCE);
const speck = P => Math.max(0.8, P * 0.16);

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

// One swimmer's way back and forth across [0, cols - width): where it is at time t and which way it faces. It slows
// into each wall and turns round there, so it is always drawn whole.
function lap(n, t, cols, width, speed) {
  const span = Math.max(1, cols - width), f = (hash(n, 8, 4) + (t * speed) / (2 * span)) % 1;
  const out = f < 0.5, p = out ? f * 2 : 2 - f * 2, dir = hash(n, 2, 6) > 0.5 ? 1 : -1;
  const e = p + Math.sin(2 * Math.PI * p) * -0.08;  // eased at both walls, still 0 at one and 1 at the other
  return { x: (dir > 0 ? e : 1 - e) * span, dir: out ? dir : -dir };
}

// A swimmer's body bends in a wave that runs from its head to its tail, and the tail swings most: the lift of column
// i of a body `len` long with its head at column `head`.
const ripple = (len, head, beat, amp) => i => {
  const back = Math.abs(i - head) / (len - 1);
  return Math.sin(beat - back * 3.2) * amp * back * back;
};

// Reserve the same slots and octopus box until the tank or dock changes; neither depends on animation time.
function seaPlacement(spec, fauna, cols, rows, dock) {
  const slots = seaLayout(spec, fauna, cols, rows), o = fauna && fauna.octopus;
  let octopus = null;
  if (o) {
    const floor = fauna.floor ? 1 : 0;
    let ox = o.at ? Math.round(o.at[0] * cols - OW / 2) : 0;
    let oy = o.at ? Math.round(o.at[1] * rows - OH / 2) : rows - OH - floor, scale = 1;
    if (dock && !o.at && ox < dock[2] && ox + OW > dock[0] && oy < dock[3] && oy + OH > dock[1]) {
      if (dock[1] >= OH + 1) oy = Math.floor(dock[1] - OH - 1);
      else if (dock[2] + OW <= cols) ox = Math.ceil(dock[2]);
      else if (dock[0] >= OW) ox = Math.floor(dock[0] - OW);
      // Reserve each jellyfish's full drift and its label, so the octopus does not jump as they swim.
      const occupied = slots.map(([sx, sy, slack], k) => {
        const half = Math.max(JW / 2, String(spec[k].name || '').length * 1.5);
        return [sx + JW / 2 - half - slack - 1, sy - 1, sx + JW / 2 + half + slack + 1, sy + JH + 1.5];
      });
      occupied.push([dock[0] - 1, dock[1] - 1, dock[2] + 1, dock[3] + 1]);
      search: for (const size of [1, 0.75, 0.5, 0.4]) {
        for (let y = Math.floor(rows - OH * size - floor); y >= 0; y--) {
          for (let x = 0; x <= cols - OW * size; x++) {
            const free = occupied.every(([l, top, r, bottom]) => x + OW * size <= l || x >= r || y + OH * size <= top || y >= bottom);
            if (free) { ox = x; oy = y; scale = size; break search; }
          }
        }
      }
    }
    octopus = [Math.max(0, Math.min(cols - OW * scale, ox)), Math.max(0, Math.min(rows - OH * scale, oy)), scale];
  }
  return { slots, octopus };
}

// Every dot of the sea at time t, in cells: [x, y, kind, tone, alpha]; the seats' names to write under their
// jellyfish; and each creature's box, for the test that checks every creature is drawn whole.
function seaDots(spec, fauna, cols, rows, t, dock = null, placement = null) {
  const dots = [], labels = [], parts = [];
  const put = (x, y, kind, tone, a) => { dots.push([x, y, kind, tone, a]); };
  const map = (rowsOf, x0, y0, tone, a, part, bend) => {
    rowsOf.forEach((row, j) => {
      for (let i = 0; i < row.length; i++) {
        const ch = row[i], y = y0 + j + (bend ? bend(i) : 0);
        if (ch === '#') put(x0 + i, y, BRIGHT, tone, a);
        else if (ch === '.') put(x0 + i, y, DIM, tone, a);
        else if (ch === 'o') put(x0 + i, y, RING, tone, a);
      }
    });
    parts.push({ ...part, box: [x0, y0, x0 + rowsOf[0].length, y0 + rowsOf.length] });
  };
  const { slots, octopus } = placement || seaPlacement(spec, fauna, cols, rows, dock);
  let below = 0;
  spec.forEach((c, k) => {
    const sp = c.sp || 1, ph = k * 1.7, tone = TONES[c.tone] ? c.tone : 'faint', a = tone === 'faint' ? 0.78 : 0.95;
    // it swims in pulses: the bell contracts and lifts it a little, then opens while it sinks back; it drifts about
    // its slot, never past its own box's edge
    const beat = t * (tone === 'accent' ? 1.7 : 0.9) * sp + ph, pulse = Math.sin(beat);
    let [x0, y0, slack] = slots[k];
    x0 = Math.max(0, Math.min(cols - JW, x0 + Math.sin(t * 0.22 * sp + ph) * slack));
    y0 = Math.max(0, Math.min(rows - JH, y0 + Math.sin(t * 0.35 * sp + ph) * 0.7 - 0.3 * pulse));
    below = Math.max(below, y0 + JH);
    // the bell: a dome lit on its upper left and shaded on the other side, over a frilled lip, with a soft edge that
    // follows it as it swells and contracts
    const cx = x0 + 7, cy = y0 + 3.7, rx = 6.6 * (1 + 0.07 * pulse), ry = 3.8 * (1 - 0.07 * pulse);
    for (let j = 0; j <= 4; j++) {
      for (let i = 0; i < JW; i++) {
        const dx = (x0 + i - cx) / rx, dy = (y0 + j - cy) / ry;
        if (dy > 0.1) continue;
        const d = Math.hypot(dx, dy), soft = Math.min(1, (1.08 - d) / 0.2);
        if (soft <= 0) continue;
        if (j === 4) put(x0 + i, y0 + j, BRIGHT, tone, a * soft * (0.55 + 0.45 * Math.sin(i * 1.6 - beat * 1.3)));
        else put(x0 + i, y0 + j, dx * 0.55 + dy * 0.85 < -0.12 || (d > 0.8 && dy < -0.5) ? BRIGHT : DIM, tone, a * soft);
      }
    }
    // tentacles: a wave runs from root to tip, the tips swing most, and they trail longer as the bell opens
    for (let q = 0; q < 5; q++) {
      const len = 4 + 1.6 * hash(q, k, 1) - 0.4 * pulse;
      for (let s = 0; s < len; s++) {
        const sway = Math.sin(beat * 0.65 - s * 0.8 + q * 1.3) * (0.3 + s * 0.3);
        put(cx - 5 + q * 2.5 + sway, y0 + 5 + s, s >= len - 1 && q % 2 ? BRIGHT : DIM, tone, a * (1 - s / (len * 2)));
      }
    }
    // two heavier oral arms in the middle, the lit one bright
    for (let m = 0; m < 2; m++) {
      for (let s = 0; s < 5; s++) {
        put(cx - 1 + m * 2 + Math.sin(beat * 0.4 - s * 0.6 + m * 2) * (0.2 + s * 0.22), y0 + 5 + s,
          m ? DIM : BRIGHT, tone, a * (1 - s / 7));
      }
    }
    if (c.name && fauna && fauna.floor) labels.push([x0 + JW / 2, y0 + JH - 0.5, String(c.name), tone]);
    parts.push({ kind: 'jelly', name: c.name, box: [x0, y0, x0 + JW, y0 + JH] });
  });
  if (!fauna) return { dots, labels, parts };
  const f = fauna;
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
  // the octopus near the floor, clear of the dock, or where the page puts it in a small tank; it breathes and blinks
  if (f.octopus) {
    const o = f.octopus, tone = TONES[o.tone] ? o.tone : 'faint', a = tone === 'faint' ? 0.55 : 0.95;
    const [x, y, scale] = octopus;
    const start = dots.length;
    const lift = 0.3 * (0.5 + 0.5 * Math.sin(t * 0.8));
    const head = t % 5.3 < 0.18 ? OCTOPUS.map(r => r.replace(/o/g, '.')) : OCTOPUS;
    map(head, x + 1, y + lift, tone, a, { kind: 'octopus' }, i => 0.06 + Math.sin(t * 0.8 + i * 0.5) * 0.06);
    parts[parts.length - 1].box = [x, y, x + OW, y + OH];
    for (let m = 0; m < 5; m++) {
      const out = m < 2 ? -1 : m > 2 ? 1 : 0;
      for (let s = 0; s < 4; s++) {
        put(x + 2.4 + m * 2.1 + out * s * 0.5 + Math.sin(t * 0.9 - s * 0.9 + m * 1.7) * s * 0.2, y + 4 + s,
          s ? DIM : BRIGHT, tone, a * (1 - s * 0.15));
      }
    }
    if (scale !== 1) {
      for (let i = start; i < dots.length; i++) { dots[i][0] = x + (dots[i][0] - x) * scale; dots[i][1] = y + (dots[i][1] - y) * scale; }
      parts[parts.length - 1].box = [x, y, x + OW * scale, y + OH * scale];
    }
  }
  if (!f.floor) return { dots, labels, parts };
  // A strip (a phone's sea) has room for the seats and the octopus only; the rest swims in the reef.
  if (rows <= 30) return seaFloor(dots, cols, rows, t, labels, parts);
  // The open water under the jellyfish and above the octopus, when there is room, else the whole water: the fish, the
  // school and the bacteria all swim there, so the middle of the reef is never empty while there is anything to show.
  const top = below + 4 < rows - OH - 6 ? below + 1 : 1, bottom = rows - OH - 3;
  const depth = (n, h) => Math.max(1, Math.min(rows - h - 2, top + hash(n, 9, 5) * Math.max(0, bottom - top - h)));
  for (let n = 0; n < Math.min(9, f.fish || 0); n++) {  // a fish for each job running
    const w = FISH[0].length, { x, dir } = lap(n, t, cols, w, 2.2 + 1.4 * hash(n, 7, 3));
    const y = depth(n, 3) + Math.sin(t * 0.4 + n * 2) * 0.6;
    map(dir > 0 ? FISH : flip(FISH), x, Math.max(0.5, Math.min(rows - 3.5, y)), 'accent', 0.95, { kind: 'fish' },
      ripple(w, dir > 0 ? w - 1 : 0, t * 6 + n, 0.45));
  }
  const school = Math.min(SCHOOL.length, f.school || 0);
  if (school) {  // the jobs done today swim together, the first one leading, each holding its place loosely
    const crew = SCHOOL.slice(0, school), lo = Math.min(...crew.map(([, dy]) => dy));
    const high = Math.max(...crew.map(([, dy]) => dy)) - lo + 3;
    const back = Math.max(...crew.map(([dx]) => -dx)), w = back + MINNOW[0].length, mw = MINNOW[0].length;
    const { x, dir } = lap(31, t, cols, w, 1.1), y = Math.min(depth(31, high), Math.max(1, bottom - high)) - lo;
    crew.forEach(([dx, dy], m) => {
      const mx = (dir > 0 ? x + back + dx : x - dx) + Math.sin(t * 0.9 + m * 2.1) * 0.8;
      const my = y + dy + Math.sin(t * 0.7 + m * 1.3) * 0.5;
      map(dir > 0 ? MINNOW : flip(MINNOW), Math.max(0, Math.min(cols - mw, mx)), Math.max(0.5, Math.min(rows - 3.5, my)),
        'accent', 0.75, { kind: 'minnow' }, ripple(mw, dir > 0 ? mw - 1 : 0, t * 7 + m * 1.9, 0.35));
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
      const ex = dir > 0 ? x + EEL - 1 - s : x + s, ey = base + 0.9 * Math.sin(s * 0.5 - t * 2.2);
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
    if (h > 0.93 && i > OW + 1 && i < cols - 2) {
      const tall = 2 + Math.floor(hash(i, 4, 9) * 3);
      for (let s = 1; s <= tall; s++) {
        put(i + Math.sin(t * 0.7 + i + s * 0.8) * 0.6 * s / tall, rows - 1 - s, s === tall ? BRIGHT : DIM, 'weed', 0.5);
      }
    }
  }
  return { dots, labels, parts };
}

// The weight each cell holds, per tone: a dot spreads its weight over the four cells around where it is, each share as
// big as the dot is near that cell, so a creature half a cell along lights both cells by half and glides rather than
// steps. Also the cells that hold an eye, and the specks of plankton, which are drawn as dots.
function seaCells(dots, cols, rows) {
  const cells = new Map(), eyes = new Map(), specks = [];
  for (const [x, y, kind, tone, a] of dots) {
    if (kind === FINE) { specks.push([x, y, tone, a]); continue; }
    let m = cells.get(tone);
    if (!m) cells.set(tone, m = new Map());
    const i = Math.floor(x), j = Math.floor(y), fx = x - i, fy = y - j, w = WEIGHT[kind] * a;
    const add = (ci, cj, share) => {
      if (share <= 0 || ci < 0 || cj < 0 || ci >= cols || cj >= rows) return;
      const at = cj * cols + ci;
      m.set(at, (m.get(at) || 0) + w * share);
    };
    add(i, j, (1 - fx) * (1 - fy)); add(i + 1, j, fx * (1 - fy)); add(i, j + 1, (1 - fx) * fy); add(i + 1, j + 1, fx * fy);
    if (kind === RING) eyes.set(Math.round(y) * cols + Math.round(x), tone);
  }
  return { cells, eyes, specks };
}

// The character for a cell's weight. `grain`, from 0 to 1 and steady for a while in each cell, moves it a step up or
// down now and then, so a body of even weight keeps the texture of hand-set ASCII.
function glyph(w, grain) {
  const n = RAMP.length, k = Math.floor(Math.min(0.999, w / 1.2) * n) + (grain > 0.82 ? 1 : grain < 0.18 ? -1 : 0);
  return RAMP[Math.max(0, Math.min(n - 1, k))];
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
  let placement = null, placedDock = '';
  // The creatures' colours come from the page's theme (rh.css --sea-*), and change with it.
  let tones = TONES, label = [128, 139, 136];
  function paint() {
    const css = getComputedStyle(document.documentElement);
    const rgb = (name, or) => {
      const v = css.getPropertyValue(name).trim().split(/\s+/).map(Number);
      return v.length === 3 && v.every(n => n >= 0 && n <= 255) ? v : or;
    };
    tones = Object.fromEntries(Object.entries(TONES).map(([k, v]) => [k, rgb('--sea-' + k, v)]));
    label = rgb('--sea-label', [128, 139, 136]);
  }
  paint();
  function size() {
    const r = canvas.getBoundingClientRect();
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    W = r.width; H = r.height;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ({ P, Q, cols, rows } = seaGrid(W, H, spec.length, !!(fauna && fauna.floor)));
    placement = null;
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
    const dock = host.parentElement && host.parentElement.querySelector('.dock');
    const tankBox = canvas.getBoundingClientRect(), dockBox = dock && dock.getBoundingClientRect();
    const dockCells = dockBox && dockBox.right > dockBox.left && dockBox.bottom > dockBox.top &&
      [(dockBox.left - tankBox.left) / P, (dockBox.top - tankBox.top) / Q,
      (dockBox.right - tankBox.left) / P, (dockBox.bottom - tankBox.top) / Q];
    const dockKey = dockCells ? dockCells.join('|') : '';
    if (!placement || dockKey !== placedDock) {
      placement = seaPlacement(spec, fauna, cols, rows, dockCells); placedDock = dockKey;
    }
    const { dots, labels } = seaDots(spec, fauna, cols, rows, t, dockCells, placement);
    const { cells, eyes, specks } = seaCells(dots, cols, rows);
    const clear = clears();
    const fade = (x, y) => {
      let k = 1;
      for (const [x0, y0, x1, y1] of clear) {
        const o = Math.max(Math.max(x0 - x, 0, x - x1), Math.max(y0 - y, 0, y - y1) * 1.6);
        if (o < 2) k *= 0.15 + 0.4 * o;
      }
      return k;
    };
    const rgba = (rgb, a) => 'rgba(' + rgb[0] + ', ' + rgb[1] + ', ' + rgb[2] + ', ' + a.toFixed(2) + ')';
    ctx.clearRect(0, 0, W, H);
    // One fill colour per tone and alpha, so a frame sets a few dozen colours however many cells it lights. A few of
    // the heaviest cells sit in a faint lit square, as hand-set ASCII art does.
    const groups = new Map(), lit = [];
    for (const [tone, m] of cells) {
      for (const [at, w] of m) {
        if (w < 0.06) continue;
        const x = at % cols, y = (at - x) / cols, eye = eyes.get(at) === tone;
        const grain = hash(x, y, Math.floor(t * 0.7 + hash(x, y, 1) * 4));
        const a = (eye ? 1 : Math.min(1, 0.3 + 0.7 * w)) * fade(x, y);
        if (a < 0.04) continue;
        const key = tone + '|' + Math.round(a * 20);
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push([x, y, eye ? 'o' : glyph(w, grain)]);
        if (w > 0.95 && grain > 0.88) lit.push([x, y, tone, a]);
      }
    }
    for (const [x, y, tone, a] of lit) {
      ctx.fillStyle = rgba(tones[tone], a * 0.14);
      ctx.fillRect(x * P + 0.5, y * Q + 0.5, P - 1, Q - 1);
    }
    ctx.font = glyphSize(P, Q).toFixed(1) + 'px "Geist Mono", ui-monospace, monospace';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    for (const [key, list] of groups) {
      const [tone, a] = key.split('|');
      ctx.fillStyle = rgba(tones[tone], a / 20);
      for (const [x, y, ch] of list) ctx.fillText(ch, (x + 0.5) * P, (y + 0.5) * Q);
    }
    // plankton: specks, one path per alpha
    const fine = new Map(), r = speck(P);
    for (const [x, y, tone, alpha] of specks) {
      const a = alpha * fade(x, y);
      if (a < 0.04) continue;
      const key = tone + '|' + Math.round(a * 20);
      if (!fine.has(key)) fine.set(key, []);
      fine.get(key).push([x, y]);
    }
    for (const [key, list] of fine) {
      const [tone, a] = key.split('|');
      ctx.fillStyle = rgba(tones[tone], a / 20);
      ctx.beginPath();
      for (const [x, y] of list) { ctx.moveTo(x * P + r, y * Q); ctx.arc(x * P, y * Q, r, 0, Math.PI * 2); }
      ctx.fill();
    }
    if (labels.length) {
      ctx.font = Math.max(9, Math.min(11, Q * 0.8)) + 'px "Geist Mono", ui-monospace, monospace';
      for (const [x, y, name, tone] of labels) {
        ctx.fillStyle = 'rgba(' + (tone === 'faint' ? label : tones[tone]).join(', ') + ', .9)';
        ctx.fillText(name, x * P, y * Q);
      }
    }
  }

  size();
  let rt = 0;
  const resized = () => { clearTimeout(rt); rt = setTimeout(() => { if (!canvas.isConnected) return; size(); if (calm.matches) draw(6); }, 120); };
  addEventListener('resize', resized);
  const themed = () => { if (!canvas.isConnected) return; paint(); if (calm.matches) draw(6); };
  light.addEventListener('change', themed);
  if (calm.matches) { draw(6);
    const dock = host.parentElement && host.parentElement.querySelector('.dock');
    if (dock && 'ResizeObserver' in window) {
      const observer = new ResizeObserver(() => { if (canvas.isConnected) draw(6); else observer.disconnect(); });
      observer.observe(dock);
    }
    return;
  }
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
      light.removeEventListener('change', themed);
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
  for (const c of $$('canvas[data-tank]')) if (!c.dataset.on && c.getClientRects().length) { c.dataset.on = '1'; Tank(c); }
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
  for (const worker of $$('form.compose select[name="kind"]')) requireCommand(worker);
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
  // Filter the existing lists without fetching another page.
  const jobs = $('.side .jobs');
  if (jobs) {
    if (show) jobs.dataset.show = show; else delete jobs.dataset.show;
    $('[data-job-filter]', jobs).value = show;
  }
  // The open job's row in the lane is lit; the server marks it, and this keeps it right as pages swap.
  const here = document.body.dataset.here || '';
  for (const r of $$('.lane .row[data-key]')) {
    if (here && r.dataset.key === here) r.setAttribute('aria-current', 'page'); else r.removeAttribute('aria-current');
  }
  const box = $('.ibx');
  if (box) {
    if (kind) box.dataset.show = kind; else delete box.dataset.show;
    for (const t of $$('[role="tab"][data-kind]')) t.setAttribute('aria-selected', String(t.dataset.kind === kind));
  }
}

function apply(doc, nav) {
  // the server was restarted with a new look or client: load the whole page again rather than mix the two
  const build = doc.body.dataset.build;
  if (build && document.body.dataset.build && build !== document.body.dataset.build) { location.reload(); return null; }
  const now = +doc.body.dataset.now;
  if (now) skew = now - Date.now() / 1000;
  for (const k of ['open', 'pace', 'jobs', 'here']) document.body.dataset[k] = doc.body.dataset[k] || '';
  if (nav) { document.title = doc.title; kind = ''; }  // the worker filter stays as you move between jobs
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
    if (old.matches('.worked') && el.matches('.worked')) {
      $('summary', old).innerHTML = $('summary', el).innerHTML;
      const log = $('.log', old), freshLog = $('.log', el);
      if (log && freshLog) {
        const pinned = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
        log.textContent = freshLog.textContent;
        if (pinned) log.scrollTop = log.scrollHeight;
      }
      else $('.worked-content', old).replaceWith($('.worked-content', el));
      old.dataset.sig = el.dataset.sig;
      continue;
    }
    if (!$('.body', el)) {
      if (!busy(old)) { old.replaceWith(el); if (!nav && !el.hasAttribute('data-still')) play(el, 'arrived', 400); }
      continue;
    }
    if (sync(old, el, doc, before, added, changed)) old.dataset.sig = el.dataset.sig;
    old.hidden = el.hidden;
  }
  if (swap) {
    const top = nav ? 0 : ($('.jd-scroll', pane) || pane).scrollTop;
    const had = new Set(regions(pane).split(' '));
    let refocus = null;
    if (!nav) for (const selector of ['.jd-inspector', '.worked']) {
      const before = $(selector, pane), after = $(selector, fresh);
      if (!before || !after) continue;
      after.open = before.open;
      if (focused === $('summary', before)) refocus = $('summary', after);
    }
    pane.replaceWith(fresh);
    fresh.inert = pane.inert;
    if (refocus) refocus.focus({ preventScroll: true });
    const scroller = $('.jd-scroll', fresh) || fresh;
    scroller.scrollTop = top;
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
  applyMode();
  document.dispatchEvent(new Event('rh:page'));
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
  setSidebar(false);
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
  if (!ok && out.trim()) {  // a failure keeps the CLI's words to read; a success says only what happened
    const d = document.createElement('details'), s = document.createElement('summary'), pre = document.createElement('pre');
    s.textContent = 'Details'; pre.textContent = out.trim(); d.append(s, pre); t.append(d);
  }
  box.append(t);
  while (box.children.length > 3) box.firstElementChild.remove();
  if (ok) setTimeout(() => drop(t), 4000);
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
  const empty = $('.no-jobs'), groupWasHidden = grp.hidden, emptyWasHidden = empty && empty.hidden;
  grp.hidden = false;
  if (empty) empty.hidden = true;
  $('.rows', grp).prepend(li);
  play(li, 'arrived', 400);
  return () => {
    if (!li.isConnected) return;
    li.remove();
    grp.hidden = groupWasHidden;
    if (empty && empty.isConnected) empty.hidden = emptyWasHidden;
  };
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
  if (early) early();
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
  const label = b.querySelector('.pc-t') || b;  // the label alone changes, so the button keeps its glyph
  const was = b.dataset.was || label.textContent;
  b.dataset.was = was;
  label.textContent = done ? 'Copied' : 'Select it and copy';
  b.classList.add('shown');  // hover-hidden, it still has to be seen saying Copied
  setTimeout(() => { label.textContent = was; b.classList.remove('shown'); }, 1600);
}

// A patch preview's Copy fetches the file's whole text at the row link's URL: the preview itself may be cut short.
async function copyUrl(b) {
  let done = false;
  try {
    const r = await fetch(b.dataset.copyUrl, { credentials: 'same-origin' });
    if (r.ok) { await navigator.clipboard.writeText(await r.text()); done = true; }
  } catch (err) { done = false; }
  const label = b.querySelector('.pc-t') || b;  // the label alone changes, so the button keeps its glyph
  const was = b.dataset.was || label.textContent;
  b.dataset.was = was;
  label.textContent = done ? 'Copied' : 'Open it to copy';
  setTimeout(() => { label.textContent = was; }, 1200);
}

// A fenced block in an answer offers Copy at its corner. The button sits in a wrapper beside the pre, not in it,
// so it stays at the corner while long lines scroll; it names the code's id, so what is copied is the code alone.
let codeN = 0;
function codeCopies() {
  for (const pre of $$('.assistant-message.md pre')) {
    const code = $('code', pre);
    if (!code || pre.parentElement.classList.contains('code-wrap')) continue;
    if (!code.id) code.id = 'md-code-' + (codeN += 1);
    const wrap = document.createElement('div');
    wrap.className = 'code-wrap';
    pre.replaceWith(wrap);
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'code-copy'; b.textContent = 'Copy';
    b.dataset.copy = code.id;
    b.setAttribute('aria-label', 'Copy code');
    wrap.append(pre, b);
  }
}
document.addEventListener('rh:page', codeCopies);
codeCopies();

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
    const hit = !q || r.textContent.toLowerCase().includes(q) || (r.dataset.id || '').includes(q);
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
  else if (e.key === 'Escape') { e.preventDefault(); closePal(); }
});
document.addEventListener('mousemove', e => {
  const r = e.target.closest && e.target.closest('#pal .p-row');
  if (r && r.getAttribute('aria-selected') !== 'true') pick(r);
});

document.addEventListener('click', e => {
  const target = e.target.closest?.('.sidebar-toggle, .side-scrim');
  if (target) {
    const open = sidebarToggle.getAttribute('aria-expanded') === 'true';
    setSidebar(!open, open);
    return;
  }
  for (const d of $$('details.act[open], details.opts[open]')) if (!d.contains(e.target)) d.open = false;
  const u = e.target.closest && e.target.closest('[data-copy-url]');
  if (u) { copyUrl(u); return; }
  const c = e.target.closest && e.target.closest('[data-copy]');
  if (c) { copy(c); return; }
  const m = e.target.closest && e.target.closest('[data-more]');
  if (m) { more(m); return; }
  if (e.target.closest && e.target.closest('[data-pal]')) { openPal(); return; }
  if (e.target.closest && e.target.closest('[data-settings]')) { $('#settings').showModal(); return; }
  const addInput = e.target.closest && e.target.closest('[data-add-input]');
  if (addInput) { openFiles(addInput); return; }
  const clearInput = e.target.closest && e.target.closest('[data-clear-input]');
  if (clearInput) { attach(clearInput.form, ''); return; }
  const pick = $('.file-pick');
  if (pick && !pick.hidden && !pick.contains(e.target)) closeFiles();
  const d = pal();
  if (d && (e.target === d || (e.target.closest && e.target.closest('[data-pal-close]')))) { closePal(); return; }  // the backdrop or Cancel
  const row = e.target.closest && e.target.closest('#pal .p-row');
  if (row) { e.preventDefault(); run(row); return; }
  const tab = e.target.closest && e.target.closest('[role="tab"][data-kind]');
  if (tab) {
    kind = tab.dataset.kind;
    filter();
    return;
  }
  const a = e.target.closest && e.target.closest('a[data-nav]');
  if (!a || e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  if (a.href !== location.href) go(a.href, true);
  else if (a.closest('.side') && $('.app').classList.contains('side-open')) setSidebar(false, true);
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
  if (t.matches('textarea[name="changed"]')) {
    const send = $('[data-followup]', form);
    const label = t.value.trim() ? 'Send follow-up' : (form.dataset.label || 'Run again');
    send.setAttribute('aria-label', label);
    send.title = label;
  }
  const id = form.elements.namedItem('id');
  if (t === id) { id.dataset.typed = id.value ? '1' : ''; return; }
  if (t.name === 'brief' && id && !id.dataset.typed) {
    // Content words only, each once, with file extensions dropped: "numbers.txt" adds nothing to "numbers".
    const words = [...new Set(t.value.replace(/^\s*\/[a-z]+\b/, ' ').toLowerCase().replace(/\.[a-z0-9]+\b/g, ' ').replace(/[^a-z0-9\s-]/g, ' ')
      .split(/\s+/).filter(w => w.length > 2 && !NAME_SKIP.has(w)))].slice(0, 3);
    let name = words.join('-').slice(0, 48).replace(/^-+|-+$/g, '') || 'job', n = 2;
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
// Two modes share one app: Coding runs on Codex cloud or this Mac, Research asks GPT Pro. The choice is per viewer.
let mode = 'coding';
try { if (localStorage.getItem('rh-mode') === 'research') mode = 'research'; } catch { /* storage may be blocked */ }
const PROMPTS = {'gpt-chat': 'Ask a research question…', 'codex-cloud': 'Describe what to build or fix…',
                 'local-agent': 'Describe what to build or fix…',
                 'local-shell': '$ a shell command, like pytest -q'};
function applyMode() {
  document.documentElement.dataset.mode = mode;
  for (const t of $$('[role="tab"][data-mode]')) t.setAttribute('aria-selected', String(t.dataset.mode === mode));
  const title = $('#compose-title');
  if (title) title.textContent = mode === 'research' ? 'What should we find out?' : (title.dataset.repo || 'What are we building?');
  for (const kind of $$('form.compose select[name="kind"]')) {
    const research = kind.value === 'gpt-chat';
    if (research !== (mode === 'research')) {
      const cloud = kind.querySelector('option[value="codex-cloud"]');
      kind.value = mode === 'research' ? 'gpt-chat'
        : (cloud && !/setup required/.test(cloud.textContent) ? 'codex-cloud' : 'local-agent');
    }
    requireCommand(kind);
  }
  if (show && (mode === 'research') !== (show === 'chat')) { show = ''; filter(); }
}
document.addEventListener('click', e => {
  const tab = e.target.closest('[role="tab"][data-mode]');
  if (!tab || tab.dataset.mode === mode) return;
  mode = tab.dataset.mode;
  try { localStorage.setItem('rh-mode', mode); } catch { /* the choice lasts this page only */ }
  applyMode();
});
function requireCommand(worker) {
  const form = worker.form;
  const brief = form.elements.namedItem('brief');
  if (brief) brief.placeholder = PROMPTS[worker.value] || 'Describe a coding or research job…';
  form.elements.namedItem('accept').disabled = worker.value === 'gpt-chat';
  for (const choice of $$('[data-route]', form)) {
    choice.hidden = choice.dataset.route !== worker.value;
    if (choice instanceof HTMLSelectElement) choice.disabled = choice.hidden;
  }
  const label = $('[data-worker-name]', form.parentElement);
  if (label) label.textContent = worker.selectedOptions[0].text.split(/\s*·\s*/)[0];
  const models = $('.model-picker', form);
  if (models) syncModel(models);
  syncEffort(form);
}
// The field behind everything is drawn, not loaded: a halftone scene in the chosen colour, redrawn on resize.
function drawField() {
  const bg = window.RHBackground;
  const field = $('.bg-field');
  if (!bg || !field) return;
  const {background, tint} = document.body.dataset;
  field.hidden = background === 'off';
  if (!field.hidden) bg.render(field, {preset: background, tint, width: innerWidth, height: innerHeight});
}
function drawPreviews() {
  const bg = window.RHBackground;
  if (!bg) return;
  // A scene drawn at thumbnail size is a handful of dots, and one drawn at window size shrinks to a grey haze; at
  // twice the thumbnail each dot keeps about a pixel, so the scene's shape reads.
  const big = document.createElement('canvas');
  for (const c of $$('canvas.background-preview')) {
    bg.render(big, {preset: c.dataset.preview, tint: document.body.dataset.tint, width: 528, height: 320, dpr: 1});
    c.width = 264; c.height = 160;
    const g = c.getContext('2d');
    g.imageSmoothingQuality = 'high';
    g.drawImage(big, 0, 0, c.width, c.height);
  }
}
let fieldTimer = 0;
addEventListener('resize', () => { clearTimeout(fieldTimer); fieldTimer = setTimeout(drawField, 120); });
drawField();
setTimeout(drawPreviews, 300);
let savedBackground = document.body.dataset.background;
let savedTint = document.body.dataset.tint;
let appearanceSave = Promise.resolve();
function chooseBackground(choice) {
  const key = choice.name;
  const chosen = choice.value;
  const form = choice.form;
  const body = new URLSearchParams(new FormData(form));
  document.body.dataset[key] = chosen;
  drawField();
  if (key === 'tint') drawPreviews();
  appearanceSave = appearanceSave.then(async () => {
    const response = await fetch(form.action, {method: 'POST', body});
    if (!response.ok) throw new Error('Appearance could not be saved');
    if (key === 'tint') savedTint = chosen; else savedBackground = chosen;
  }).catch(() => {
    if (document.body.dataset[key] === chosen) {
      const saved = key === 'tint' ? savedTint : savedBackground;
      document.body.dataset[key] = saved;
      form.elements.namedItem(key).value = saved;
      drawField();
      if (key === 'tint') drawPreviews();
    }
    toast('Appearance could not be saved.', false, '');
  });
}
document.addEventListener('change', e => {
  if (e.target.matches('[data-job-filter]')) {
    show = e.target.value;
    filter();
  } else if (e.target.matches('form.compose select[name="kind"]')) {
    requireCommand(e.target);
  } else if (e.target.matches('[data-appearance] input:is([name="background"], [name="tint"])')) {
    chooseBackground(e.target);
  }
});
document.addEventListener('invalid', e => {
  const options = e.target.closest('form.compose details.opts');
  if (options) options.open = true;
}, true);

const items = () => $($('.ibx') && !$('.app').classList.contains('side-open') ? '.ibx' : '.side .jobs')
  ?.querySelectorAll('a.item, a.ib');
function move(step) {
  if (compact.matches && sidebarToggle && !$('.ibx') && !$('.app').classList.contains('side-open')) setSidebar(true);
  const list = Array.from(items() || []).filter(el => el.getClientRects().length || el.parentElement.getClientRects().length);
  if (!list.length) return;
  const cur = document.activeElement && document.activeElement.closest('a.item, a.ib');
  let i = list.indexOf(cur);
  if (i < 0) i = list.findIndex(a => a.hasAttribute('aria-current'));
  i = i < 0 ? (step >= 0 ? 0 : list.length - 1) : Math.max(0, Math.min(list.length - 1, i + step));
  list[i].focus({ preventScroll: true });
  (list[i].closest('.row, .ib-li') || list[i]).scrollIntoView({ block: 'nearest', behavior: calm.matches ? 'auto' : 'smooth' });
}
// <place> From here to </place> the code touches no page, so a test runs it in node.
// Where a popover opens, given its button's box `at`, the box `room` it must stay inside (both {top, right, bottom,
// left} in px), its size and its gap from the button: under the button when it fits there, else over it when there is
// more room above; lined up with the button's left edge unless that runs past the room's right, then with its right
// edge; and never taller than the side it opens on.
function placePop(at, room, w, h, gap) {
  const below = room.bottom - at.bottom - gap, above = at.top - room.top - gap;
  const up = h > below && above > below;
  return { up, right: at.left + w > room.right && at.right - w >= room.left, max: Math.max(80, Math.floor(up ? above : below) - 8) };
}
// A composer's popover floats 8px clear of the composer's edge, below it when it fits, else above its trigger.
function dropTop(trigger, h) {
  const r = trigger.getBoundingClientRect(), box = (trigger.closest('.composer') || trigger).getBoundingClientRect();
  return innerHeight - box.bottom - 16 >= h || r.top < h + 14 ? box.bottom + 8 : r.top - h - 8;
}
// </place>
// Options opens inside what shows of the page: over its button when the composer sits low, as it does docked at the
// foot of the sea, and scrolling within itself when even that side is short.
function place(d) {
  const body = $('.opts-body', d), s = $('summary', d);
  if (!body || !s) return;
  body.classList.remove('up', 'right'); body.style.maxHeight = '';
  let room = { top: 0, right: innerWidth, bottom: innerHeight, left: 0 };
  for (let el = d.parentElement; el && el !== document.body; el = el.parentElement) {
    if (getComputedStyle(el).overflow === 'visible') continue;
    const b = el.getBoundingClientRect();
    room = { top: Math.max(room.top, b.top), right: Math.min(room.right, b.right), bottom: Math.min(room.bottom, b.bottom),
      left: Math.max(room.left, b.left) };
  }
  const p = placePop(s.getBoundingClientRect(), room, body.offsetWidth, body.scrollHeight, 6);
  body.classList.toggle('up', p.up); body.classList.toggle('right', p.right); body.style.maxHeight = p.max + 'px';
  // below a composer it floats the same 8px clear of the glass as the model and effort popovers
  const box = d.closest('.composer'), cb = body.offsetParent;
  body.style.top = !p.up && box && cb ? (box.getBoundingClientRect().bottom + 8 - cb.getBoundingClientRect().top) + 'px' : '';
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
  if (d instanceof HTMLDetailsElement && d.open && d.matches('.opts')) place(d);
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
    if ($('.app').classList.contains('side-open') && !$('dialog[open]')) {
      setSidebar(false, true); e.preventDefault(); return;
    }
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
  else if (/^[rahusclxy]$/.test(e.key) && act(e.key)) {
    const c = act(e.key);
    if (c.matches('a[data-nav]')) go(c.href, true);
    else if (c.hasAttribute('data-copy')) c.click();  // a Copy runs on its key; the rest only point at their control
    else activate(c);
  }
  else return;
  e.preventDefault();
});
document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });

const keysButton = $('[data-keys]');
if (sidebarToggle) {
  document.documentElement.classList.add('has-sidebar-toggle');
  compact.addEventListener('change', () => setSidebar(false));
}
if (keysButton) {
  keysButton.hidden = false;
  keysButton.addEventListener('click', () => { $('#settings').close(); $('#keys').showModal(); });
}
const first = $('#result');
if (first) { report(first); first.remove(); }
skew = (+document.body.dataset.now || Date.now() / 1000) - Date.now() / 1000;
clock();
mark();
filter();
applyMode();
(document.fonts ? document.fonts.ready : Promise.resolve()).then(() => { fonts = true; wake(); });
setInterval(clock, 1000);
live('on');
timer = setTimeout(poll, pace());


// "+" opens a file picker over the composer: the repository's tracked files, filtered as you type.
let repoFiles = null, filesFor = null, fileRows = [], fileAt = 0;
async function loadFiles() {
  if (repoFiles) return repoFiles;
  const t = new URLSearchParams(location.search).get('t') || '';
  try {
    const r = await fetch('/files?t=' + encodeURIComponent(t));
    repoFiles = r.ok ? (await r.json()).files : [];
  } catch { repoFiles = []; }
  return repoFiles;
}
function attach(form, path) {
  form.elements.namedItem('input_path').value = path;
  const chip = $('.file-chip', form);
  chip.hidden = !path;
  chip.title = path;
  $('.file-name', chip).textContent = path.split('/').pop();
}
function filePanel() {
  let pick = $('.file-pick');
  if (pick) return pick;
  pick = document.createElement('div');
  pick.className = 'file-pick';
  pick.setAttribute('role', 'dialog');
  pick.setAttribute('aria-label', 'Attach a repository file');
  pick.hidden = true;
  const search = document.createElement('input');
  search.className = 'file-search';
  search.type = 'search';
  search.placeholder = 'Search files…';
  search.setAttribute('aria-label', 'Search the repository');
  search.autocomplete = 'off';
  search.spellcheck = false;
  const list = document.createElement('ul');
  list.className = 'file-list';
  list.setAttribute('role', 'listbox');
  pick.append(search, list);
  document.body.append(pick);
  document.dispatchEvent(new Event('rh:page'));
  search.addEventListener('input', () => listFiles(search.value));
  search.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (fileRows.length) { fileAt = (fileAt + (e.key === 'ArrowDown' ? 1 : -1) + fileRows.length) % fileRows.length; lightFile(); }
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (fileRows[fileAt]) chooseFile(fileRows[fileAt].dataset.path);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      closeFiles();
    }
  });
  list.addEventListener('mousedown', e => {
    const row = e.target.closest('.file-row');
    if (row) { e.preventDefault(); chooseFile(row.dataset.path); }
  });
  return pick;
}
function listFiles(query) {
  const list = $('.file-pick .file-list');
  const q = query.trim().toLowerCase();
  const all = repoFiles || [];
  const scored = [];
  for (const path of all) {
    const low = path.toLowerCase(), base = low.slice(low.lastIndexOf('/') + 1);
    const at = q ? low.indexOf(q) : 0;
    if (at < 0) continue;
    scored.push([q ? (base.startsWith(q) ? 0 : base.includes(q) ? 1 : 2) : 0, path.length, path]);
  }
  scored.sort((a, b) => a[0] - b[0] || a[1] - b[1] || (a[2] < b[2] ? -1 : 1));
  list.replaceChildren();
  fileRows = [];
  for (const [, , path] of scored.slice(0, 80)) {
    const row = document.createElement('li');
    row.className = 'file-row';
    row.setAttribute('role', 'option');
    row.dataset.path = path;
    const cut = path.lastIndexOf('/');
    const name = document.createElement('span');
    name.className = 'file-row-name';
    name.textContent = path.slice(cut + 1);
    const dir = document.createElement('span');
    dir.className = 'file-row-dir';
    dir.textContent = cut > 0 ? path.slice(0, cut) : '';
    const icon = $('.file-chip .ic', filesFor || document);
    if (icon) row.append(icon.cloneNode(true));
    row.append(name, dir);
    list.append(row);
    fileRows.push(row);
  }
  if (!fileRows.length) {
    const none = document.createElement('li');
    none.className = 'file-none';
    none.textContent = repoFiles === null ? 'Loading files…' : all.length ? 'No file matches' : 'No tracked files in this repository';
    list.append(none);
  }
  fileAt = 0;
  lightFile();
}
function lightFile() {
  fileRows.forEach((row, i) => row.setAttribute('aria-selected', String(i === fileAt)));
  if (fileRows[fileAt]) fileRows[fileAt].scrollIntoView({block: 'nearest'});
}
async function openFiles(button) {
  const pick = filePanel();
  filesFor = button.form;
  const r = button.getBoundingClientRect();
  const width = Math.min(420, innerWidth - 24);
  pick.style.width = width + 'px';
  pick.style.left = Math.max(12, Math.min(r.left - 6, innerWidth - width - 12)) + 'px';
  const above = r.top > 340;
  pick.style.top = above ? '' : (r.bottom + 8) + 'px';
  pick.style.bottom = above ? (innerHeight - r.top + 8) + 'px' : '';
  pick.hidden = false;
  const search = $('.file-search', pick);
  search.value = '';
  listFiles('');
  search.focus();
  await loadFiles();
  if (!pick.hidden) listFiles(search.value);
}
function chooseFile(path) {
  if (filesFor) attach(filesFor, path);
  const brief = filesFor && $('textarea.c-text', filesFor);
  closeFiles();
  if (brief) brief.focus();
}
function closeFiles() {
  const pick = $('.file-pick');
  if (pick) pick.hidden = true;
}

// Liquid glass needs an SVG filter as a backdrop, which only Chromium draws; elsewhere surfaces stay frosted.
if (/Chrome\//.test(navigator.userAgent)) document.documentElement.classList.add('lg-on');

// The sidebar is docked to the left edge: drag its edge to resize it, hide it to give the work the whole window.
const root = document.documentElement;
try {
  const w = +localStorage.getItem('rh-side');
  if (w >= 200 && w <= 440) root.style.setProperty('--side', w + 'px');
  if (localStorage.getItem('rh-side-hidden') === '1') root.classList.add('side-hidden');
} catch { /* defaults */ }
function sideWidth(w) {
  w = Math.round(Math.max(200, Math.min(440, w)));
  root.style.setProperty('--side', w + 'px');
  try { localStorage.setItem('rh-side', String(w)); } catch { /* this page only */ }
}
function hideSide(hidden) {
  root.classList.toggle('side-hidden', hidden);
  try { localStorage.setItem('rh-side-hidden', hidden ? '1' : '0'); } catch { /* this page only */ }
}
document.addEventListener('pointerdown', e => {
  const grip = e.target.closest && e.target.closest('.side-grip');
  if (!grip || compact.matches) return;
  e.preventDefault();
  grip.setPointerCapture(e.pointerId);
  root.classList.add('side-dragging');
  const move = ev => sideWidth(ev.clientX);
  const stop = () => {
    root.classList.remove('side-dragging');
    grip.removeEventListener('pointermove', move);
    grip.removeEventListener('pointerup', stop);
    grip.removeEventListener('pointercancel', stop);
  };
  grip.addEventListener('pointermove', move);
  grip.addEventListener('pointerup', stop);
  grip.addEventListener('pointercancel', stop);
});
document.addEventListener('keydown', e => {
  if (e.target.matches && e.target.matches('.side-grip') && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
    e.preventDefault();
    sideWidth(parseFloat(getComputedStyle(root).getPropertyValue('--side')) + (e.key === 'ArrowRight' ? 16 : -16));
  }
});
document.addEventListener('click', e => {
  if (e.target.closest && e.target.closest('[data-collapse-side]')) { hideSide(true); return; }
  const toggle = e.target.closest && e.target.closest('.sidebar-toggle');
  if (toggle && !compact.matches && root.classList.contains('side-hidden')) {
    e.stopImmediatePropagation();
    hideSide(false);
  }
}, true);

// Job settings rows show their current value; editing a row updates it.
const NEED_WORDS = {net: 'Internet', python: 'Python', git: 'Git', loopback: 'Local ports', browser: 'Browser', gpu: 'GPU'};
function minutesText(m) {
  const h = Math.floor(m / 60), r = m % 60;
  return h && r ? `${h} h ${r} min` : h ? `${h} h` : `${r} min`;
}
function rowValue(row) {
  const out = $('[data-op-val]', row);
  if (!out) return;
  const time = $('input[name="timeout_min"]:checked', row);
  const place = $('input[name="answer"]:checked', row);
  if (place) { out.textContent = place.nextElementSibling.textContent; return; }
  const needs = $$('input[name="needs"]', row);
  const text = $('input[type="text"]', row);
  if (time) out.textContent = minutesText(+time.value);
  else if (needs.length) out.textContent = needs.filter(n => n.checked).map(n => NEED_WORDS[n.value] || n.value).join(', ') || 'Nothing special';
  else if (text) {
    const empty = {accept: "Don't test", command: 'Run the brief', id: 'From the brief'}[text.name] || '';
    out.textContent = text.value.trim() || empty;
  }
}
document.addEventListener('input', e => { const row = e.target.closest && e.target.closest('.op-row'); if (row) rowValue(row); });
document.addEventListener('change', e => { const row = e.target.closest && e.target.closest('.op-row'); if (row) rowValue(row); });
document.addEventListener('toggle', e => {
  const row = e.target;
  if (!(row instanceof HTMLDetailsElement) || !row.matches('.op-row') || !row.open) return;
  for (const other of $$('.op-row[open]', row.parentElement)) if (other !== row) other.open = false;
  const field = $('input[type="text"]', row);
  if (field) field.focus();
}, true);

// Model picker, after T3 Code's: a provider rail, a search across every model, rows with the provider's mark and a
// check on the chosen one. The hidden select keeps the value the form sends.
const CHECK_SVG = '<svg class="ic sm" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>';
function liveModelSelect(picker) { return $$('select[name="model"]', picker).find(s => !s.disabled) || null; }
function syncModel(picker) {
  const select = liveModelSelect(picker), name = $('.mp-name', picker), trigger = $('.mp-trigger', picker);
  if (trigger) trigger.hidden = !select;
  if (select && name) name.textContent = (select.selectedOptions[0] || select.options[0] || {textContent: ''}).textContent.trim();
}
function closeModels(refocus) {
  if (!modelPanel) return;
  const {panel, trigger} = modelPanel;
  modelPanel = null;
  panel.remove();
  trigger.setAttribute('aria-expanded', 'false');
  if (refocus && trigger.isConnected) trigger.focus({preventScroll: true});
}
function markFor(picker, name) {
  const tpl = $('.mp-marks', picker);
  const mark = tpl && tpl.content.querySelector(`[data-mark="${name}"] svg`);
  return mark ? mark.cloneNode(true) : document.createElement('span');
}
function openModels(trigger) {
  const picker = trigger.closest('.model-picker');
  const select = liveModelSelect(picker);
  if (!select) return;
  closeModels(false);
  closeEffort(false);
  const options = [...select.options];
  const groups = [];
  for (const o of options) {
    const prov = o.dataset.prov || 'openai';
    let g = groups.find(x => x.prov === prov);
    if (!g) groups.push(g = {prov, label: o.parentElement.label || 'OpenAI', options: []});
    g.options.push(o);
  }
  let prov = (select.selectedOptions[0] || options[0]).dataset.prov || groups[0].prov;
  let highlight = 0;

  const panel = document.createElement('div');
  panel.className = 'mp-pop';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-label', 'Choose a model');
  const rail = document.createElement('div');
  rail.className = 'mp-rail';
  rail.setAttribute('role', 'toolbar');
  rail.setAttribute('aria-orientation', 'vertical');
  rail.setAttribute('aria-label', 'Providers');
  rail.hidden = groups.length < 2;
  const bar = document.createElement('span');
  bar.className = 'mp-bar';
  rail.append(bar);
  for (const g of groups) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'mp-prov';
    b.dataset.prov = g.prov;
    b.title = g.label;
    b.setAttribute('aria-label', g.label);
    b.append(markFor(picker, g.prov === 'devin' ? 'cognition' : g.prov));
    b.addEventListener('click', () => { prov = g.prov; query.value = ''; highlight = 0; render(); query.focus(); });
    rail.append(b);
  }
  const main = document.createElement('div');
  main.className = 'mp-main';
  const query = document.createElement('input');
  query.type = 'text';
  query.className = 'mp-q';
  query.placeholder = 'Search models…';
  query.autocomplete = 'off';
  query.spellcheck = false;
  query.setAttribute('aria-label', 'Search models');
  const list = document.createElement('div');
  list.className = 'mp-list';
  list.setAttribute('role', 'listbox');
  list.setAttribute('aria-label', 'Models');
  main.append(query, list);
  panel.append(rail, main);

  function shown() {
    const term = query.value.trim().toLowerCase();
    return term ? options.filter(o => o.textContent.toLowerCase().includes(term))
                : options.filter(o => (o.dataset.prov || 'openai') === prov);
  }
  function choose(o) {
    select.value = o.value;
    select.dispatchEvent(new Event('change', {bubbles: true}));
    syncModel(picker);
    closeModels(true);
  }
  function render() {
    const rows = shown();
    highlight = Math.max(0, Math.min(highlight, rows.length - 1));
    // Devin's own chooser: its models in sections (Fusion first, with what Fusion is), each tagged with its cost,
    // and the one in use marked Current.
    query.placeholder = prov === 'devin' ? 'Search all models' : 'Search models…';
    let section = null;
    const items = [];
    rows.forEach((o, i) => {
      if (o.dataset.group && o.dataset.group !== section) {
        section = o.dataset.group;
        const head = document.createElement('p');
        head.className = 'mp-sec';
        head.textContent = section;
        if (o.dataset.groupTip) head.title = o.dataset.groupTip;
        items.push(head);
      }
      items.push(rowFor(o, i));
    });
    list.replaceChildren(...items);
    if (!rows.length) {
      const empty = document.createElement('p');
      empty.className = 'mp-empty';
      empty.textContent = 'No models match';
      list.append(empty);
    }
    const searching = query.value.trim() !== '';
    for (const b of $$('button', rail)) b.setAttribute('aria-pressed', String(!searching && b.dataset.prov === prov));
    const on = !searching && $(`button[data-prov="${prov}"]`, rail);
    bar.hidden = !on;
    if (on) bar.style.top = (on.offsetTop + on.offsetHeight / 2 - 10) + 'px';
  }
  function rowFor(o, i) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'mp-row' + (i === highlight ? ' hi' : '');
      row.dataset.prov = o.dataset.prov || 'openai';
      row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', String(o.selected));
      const name = document.createElement('span');
      name.className = 'mp-n';
      name.textContent = o.dataset.row || o.textContent.trim();
      row.title = o.textContent.trim();  // the full name, when the row cuts it short
      row.append(markFor(picker, row.dataset.prov), name);
      if (o.dataset.cost) {
        if (o.selected) {
          const now = document.createElement('span');
          now.className = 'mp-cur';
          now.textContent = 'Current';
          row.append(now);
        }
        const cost = document.createElement('span');
        cost.className = 'mp-cost';
        cost.textContent = o.dataset.cost;
        if (o.dataset.costTip) cost.title = o.dataset.costTip;
        row.append(cost);
      } else if (o.selected) row.insertAdjacentHTML('beforeend', CHECK_SVG);
      row.addEventListener('click', () => choose(o));
      row.addEventListener('pointermove', () => {
        if (highlight === i) return;
        highlight = i;
        for (const [n, r] of $$('.mp-row', list).entries()) r.classList.toggle('hi', n === i);
      });
      return row;
  }
  query.addEventListener('input', () => { highlight = 0; render(); });
  panel.addEventListener('keydown', e => {
    const rows = shown();
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeModels(true); }
    else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      highlight = (highlight + (e.key === 'ArrowDown' ? 1 : -1) + rows.length) % Math.max(rows.length, 1);
      render();
      const hi = $('.mp-row.hi', list);
      if (hi) hi.scrollIntoView({block: 'nearest'});
    } else if (e.key === 'Enter' && e.target === query && rows[highlight]) { e.preventDefault(); choose(rows[highlight]); }
    else if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && e.target === query && !query.value && groups.length > 1) {
      e.preventDefault();
      const at = groups.findIndex(g => g.prov === prov);
      prov = groups[(at + (e.key === 'ArrowRight' ? 1 : -1) + groups.length) % groups.length].prov;
      highlight = 0;
      render();
    }
  });

  // One height for every provider, so switching tabs never resizes the panel: the tallest provider's list, measured.
  panel.style.visibility = 'hidden';
  document.body.append(panel);
  const own = prov;
  let tallest = 0;
  for (const g of groups) { prov = g.prov; render(); tallest = Math.max(tallest, list.scrollHeight); }
  prov = own;
  panel.style.height = Math.min(346, query.offsetHeight + tallest + 2) + 'px';
  panel.style.visibility = '';
  const r = trigger.getBoundingClientRect(), h = panel.offsetHeight, w = panel.offsetWidth;
  panel.style.left = Math.max(8, Math.min(r.left - 4, innerWidth - w - 8)) + 'px';
  panel.style.top = dropTop(trigger, h) + 'px';
  trigger.setAttribute('aria-expanded', 'true');
  modelPanel = {panel, trigger};
  render();
  query.focus({preventScroll: true});
}
document.addEventListener('click', e => {
  const trigger = e.target.closest && e.target.closest('.mp-trigger');
  if (trigger) {
    const same = modelPanel && modelPanel.trigger === trigger;
    closeModels(false);
    if (!same) openModels(trigger);
    return;
  }
  if (modelPanel && !modelPanel.panel.contains(e.target)) closeModels(false);
});
addEventListener('resize', () => closeModels(false));
document.addEventListener('scroll', e => { if (modelPanel && !modelPanel.panel.contains(e.target)) closeModels(false); }, true);
document.addEventListener('rh:page', () => {
  if (modelPanel && !modelPanel.trigger.isConnected) closeModels(false);
  for (const picker of $$('.model-picker')) syncModel(picker);
});
for (const picker of $$('.model-picker')) syncModel(picker);

// Reasoning effort, after Codex's power slider: one stop per level the chosen model's CLI knows; the stops the dated
// policy does not allow stay visible but cannot be chosen. The hidden select keeps the value the form sends.
function effortState(form) {
  const select = $$('select[name="effort"]', form).find(s => !s.hidden) || null;
  const model = $$('select[name="model"]', form).find(s => !s.disabled);
  const option = model && model.selectedOptions[0];
  const words = key => (option && option.dataset[key] ? option.dataset[key].split(' ') : []);
  return {select, levels: words('levels'), allowed: words('efforts'), option};
}
function syncEffort(form) {
  const picker = $('.effort-picker', form);
  if (!picker) return;
  const {select, allowed} = effortState(form);
  const trigger = $('.ef-trigger', picker);
  const off = !select || !allowed.length;
  if (select) select.disabled = off;
  if (trigger) trigger.hidden = off;
  if (off) { if (effortPanel && effortPanel.form === form) closeEffort(false); return; }
  if (!allowed.includes(select.value)) select.value = allowed[allowed.length - 1];
  $('.ef-name', picker).textContent = EFFORT_WORDS[select.value] || select.value;
}
function closeEffort(refocus) {
  if (!effortPanel) return;
  const {panel, trigger} = effortPanel;
  effortPanel = null;
  panel.remove();
  trigger.setAttribute('aria-expanded', 'false');
  if (refocus && trigger.isConnected) trigger.focus({preventScroll: true});
}
function openEffort(trigger) {
  const form = trigger.form || trigger.closest('form');
  const {select, levels, allowed, option} = effortState(form);
  if (!select || !allowed.length) return;
  closeModels(false);
  closeEffort(false);
  const panel = document.createElement('div');
  panel.className = 'ef-pop';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-label', 'Reasoning effort');
  const head = document.createElement('div');
  head.className = 'ef-head';
  const title = document.createElement('span');
  title.className = 'ef-title';
  title.textContent = 'Reasoning effort';
  const value = document.createElement('span');
  value.className = 'ef-val';
  head.append(title, value);
  const slider = document.createElement('div');
  slider.className = 'ef-slider';
  slider.tabIndex = 0;
  slider.setAttribute('role', 'slider');
  slider.setAttribute('aria-label', 'Reasoning effort');
  slider.setAttribute('aria-valuemin', '0');
  slider.setAttribute('aria-valuemax', String(levels.length - 1));
  const track = document.createElement('div');
  track.className = 'ef-track';
  track.dataset.locked = String(allowed.length === 1);
  const range = document.createElement('div');
  range.className = 'ef-range';
  track.append(range);
  const ticks = levels.map(key => {
    const t = document.createElement('span');
    t.className = 'ef-tick' + (allowed.includes(key) ? '' : ' locked');
    t.title = EFFORT_WORDS[key] + (allowed.includes(key) ? '' : ' (not in the policy)');
    track.append(t);
    return t;
  });
  const thumb = document.createElement('span');
  thumb.className = 'ef-thumb';
  slider.append(track, thumb);
  const ends = document.createElement('div');
  ends.className = 'ef-ends';
  const low = document.createElement('span'), high = document.createElement('span');
  low.textContent = EFFORT_WORDS[levels[0]];
  high.textContent = EFFORT_WORDS[levels[levels.length - 1]];
  ends.append(low, high);
  const note = document.createElement('p');
  note.className = 'ef-note';
  panel.append(head, slider, ends, note);

  const at = i => 12 + i * (slider.clientWidth - 24) / Math.max(levels.length - 1, 1);
  function paint() {
    const i = Math.max(0, levels.indexOf(select.value));
    value.textContent = EFFORT_WORDS[select.value];
    slider.dataset.level = select.value;
    slider.setAttribute('aria-valuenow', String(i));
    slider.setAttribute('aria-valuetext', EFFORT_WORDS[select.value]);
    thumb.style.left = at(i) + 'px';
    range.style.width = (at(i) + 12) + 'px';
    ticks.forEach((t, n) => { t.style.left = at(n) + 'px'; t.classList.toggle('on', n <= i); });
    const provider = option.closest('optgroup') ? option.closest('optgroup').label : 'this model';
    note.textContent = allowed.length < levels.length
      ? `The policy allows ${allowed.map(k => EFFORT_WORDS[k]).join(', ')} for ${provider}.`
      : (select.value === 'max' || select.value === 'ultra') ? 'Uses your limits faster.' : '';
  }
  function choose(key) {
    if (!allowed.includes(key) || key === select.value) return;
    select.value = key;
    select.dispatchEvent(new Event('change', {bubbles: true}));
    $('.ef-name', form).textContent = EFFORT_WORDS[key];
    paint();
  }
  function nearest(x) {
    const r = slider.getBoundingClientRect();
    let best = null, gap = Infinity;
    levels.forEach((key, n) => {
      const d = Math.abs(r.left + at(n) - x);
      if (allowed.includes(key) && d < gap) { gap = d; best = key; }
    });
    return best;
  }
  slider.addEventListener('pointerdown', e => {
    e.preventDefault();
    slider.focus({preventScroll: true});
    slider.setPointerCapture(e.pointerId);
    slider.classList.add('dragging');
    choose(nearest(e.clientX));
    const move = ev => choose(nearest(ev.clientX));
    const stop = () => {
      slider.classList.remove('dragging');
      slider.removeEventListener('pointermove', move);
      slider.removeEventListener('pointerup', stop);
      slider.removeEventListener('pointercancel', stop);
    };
    slider.addEventListener('pointermove', move);
    slider.addEventListener('pointerup', stop);
    slider.addEventListener('pointercancel', stop);
  });
  panel.addEventListener('keydown', e => {
    const i = allowed.indexOf(select.value);
    if (e.key === 'Escape' || e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); closeEffort(true); }
    else if (e.key === 'ArrowRight' || e.key === 'ArrowUp') { e.preventDefault(); choose(allowed[Math.min(i + 1, allowed.length - 1)]); }
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') { e.preventDefault(); choose(allowed[Math.max(i - 1, 0)]); }
    else if (e.key === 'Home') { e.preventDefault(); choose(allowed[0]); }
    else if (e.key === 'End') { e.preventDefault(); choose(allowed[allowed.length - 1]); }
  });

  document.body.append(panel);
  const r = trigger.getBoundingClientRect(), h = panel.offsetHeight, w = panel.offsetWidth;
  panel.style.left = Math.max(8, Math.min(r.left - 4, innerWidth - w - 8)) + 'px';
  panel.style.top = dropTop(trigger, h) + 'px';
  trigger.setAttribute('aria-expanded', 'true');
  effortPanel = {panel, trigger, form};
  paint();
  slider.focus({preventScroll: true});
}
document.addEventListener('click', e => {
  const trigger = e.target.closest && e.target.closest('.ef-trigger');
  if (trigger) {
    const same = effortPanel && effortPanel.trigger === trigger;
    closeEffort(false);
    if (!same) openEffort(trigger);
    return;
  }
  if (effortPanel && !effortPanel.panel.contains(e.target)) closeEffort(false);
});
document.addEventListener('change', e => {
  if (!(e.target.matches && e.target.matches('select[name="model"]') && e.target.form)) return;
  const models = e.target.closest('.model-picker');
  if (models) syncModel(models);
  syncEffort(e.target.form);
});
addEventListener('resize', () => closeEffort(false));
document.addEventListener('scroll', e => { if (effortPanel && !effortPanel.panel.contains(e.target)) closeEffort(false); }, true);
document.addEventListener('rh:page', () => {
  if (effortPanel && !effortPanel.trigger.isConnected) closeEffort(false);
  for (const picker of $$('.effort-picker')) if (picker.closest('form')) syncEffort(picker.closest('form'));
});
for (const picker of $$('.effort-picker')) if (picker.closest('form')) syncEffort(picker.closest('form'));

// A long ask scrolls inside its bubble; while more of it lies below, its bottom edge fades instead of cutting a line.
function fadeLong(bubble) { bubble.classList.toggle('more', bubble.scrollTop + bubble.clientHeight < bubble.scrollHeight - 2); }
function fadeAll() {
  for (const b of $$('.user-message, pre.peek.diff')) {
    fadeLong(b);
    // an ask cut to its first lines offers the rest once
    // a bubble's text folds in its own span, so that is what runs past its lines
    const t = b.querySelector(':scope > .um-t') || b;
    const long = b.classList.contains('whole') || t.scrollHeight > t.clientHeight + 2;
    const next = b.nextElementSibling, has = next && next.matches('.ask-more');
    if (long && !has) {
      const more = document.createElement('button');
      more.type = 'button'; more.className = 'ask-more'; more.textContent = 'Show all';
      more.setAttribute('aria-expanded', 'false');
      b.after(more);
    } else if (!long && has) next.remove();
  }
  // a running job's output opens at its newest lines
  for (const log of $$('pre.log[data-follow]')) { log.scrollTop = log.scrollHeight; fadeTop(log); }
}
// older lines scrolled out of a log dissolve at its top edge instead of being cut through
function fadeTop(log) { log.classList.toggle('above', log.scrollTop > 2); }
document.addEventListener('scroll', e => {
  const t = e.target;
  if (!t.classList) return;
  if (t.classList.contains('user-message')) fadeLong(t);
  else if (t.matches('pre.log[data-follow]')) fadeTop(t);
  else if (t.classList.contains('jd-scroll')) t.classList.toggle('scrolled', t.scrollTop > 2);
  else if (t.classList.contains('settings-body')) t.classList.toggle('scrolled', t.scrollTop > 2);
}, true);
document.addEventListener('click', e => {
  const more = e.target.closest && e.target.closest('.ask-more');
  if (!more) return;
  const b = more.previousElementSibling, whole = b.classList.toggle('whole');
  more.textContent = whole ? 'Show less' : 'Show all';
  more.setAttribute('aria-expanded', String(whole));
  fadeLong(b);
});
document.addEventListener('rh:page', fadeAll);
addEventListener('resize', fadeAll);
// a preview folded away has no height to measure until it opens
document.addEventListener('toggle', e => { if (e.target.matches && e.target.matches('.file-preview')) fadeAll(); }, true);
fadeAll();

// A follow-up opens on its own turn, under the earlier ones, once per visit; live refreshes leave the scroll alone.
let turnFor = null;
function toTurn() {
  const key = document.body.dataset.open;
  if (key === turnFor) return;
  turnFor = key;
  const brief = $('.s-j-brief[aria-label="Follow-up"]'), scroller = brief && brief.closest('.jd-scroll');
  if (scroller) scroller.scrollTop += brief.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 24;
}
document.addEventListener('rh:page', toTurn);
toTurn();
// The header names a job as the sidebar does, often by the answer's first line or the brief: while those same words
// are in view just below, the title steps aside, and it fades back in once they scroll under the header.
let echoIo = null;
function echoTitle() {
  if (echoIo) { echoIo.disconnect(); echoIo = null; }
  const h = $('.detail .jd-head h1'), scroller = $('.jd-scroll');
  if (!h) return;
  const head = h.closest('.jd-head');
  const norm = s => s.replace(/\s+/g, ' ').trim().replace(/[.…]+$/, '').toLowerCase();
  const said = norm(h.textContent);
  const twin = said && scroller && 'IntersectionObserver' in window
    ? $$('.assistant-message > :first-child, .user-message', scroller).find(el => norm(el.textContent).startsWith(said))
    : null;
  head.classList.toggle('echo', !!twin);
  if (!twin) return;
  echoIo = new IntersectionObserver(es => head.classList.toggle('echo', es[es.length - 1].isIntersecting),
    { root: scroller });
  echoIo.observe(twin);
}
document.addEventListener('rh:page', echoTitle);
echoTitle();

// A brief or reply typed and left keeps its words, as T3 Code keeps each thread's draft: every composer box saves its
// text per page as it is typed, finds it again when the page comes back with the box empty, and drops it when sent.
function draftKey(t) { return 'rh-draft:' + location.pathname + '#' + t.id; }
function restoreDrafts() {
  for (const t of $$('.pane textarea.c-text')) {
    if (!t.id || t.value) continue;
    let saved = null;
    try { saved = localStorage.getItem(draftKey(t)); } catch { return; }
    if (saved) { t.value = saved; t.dispatchEvent(new Event('input', { bubbles: true })); }
  }
}
document.addEventListener('input', e => {
  const t = e.target;
  if (!(t instanceof HTMLTextAreaElement) || !t.id || !t.matches('.pane textarea.c-text')) return;
  try {
    // with the Swarm chip on the command is out of the text, so the draft puts it back in front
    const v = (t.form && t.form.dataset.swarm ? '/swarm ' : '') + t.value;
    if (t.value.trim()) localStorage.setItem(draftKey(t), v); else localStorage.removeItem(draftKey(t));
  } catch { /* the draft lasts this page only */ }
});
// sent, it is no longer a draft: cleared before the page swaps, so the fresh empty box is not refilled with it
document.addEventListener('submit', e => { for (const t of $$('textarea.c-text', e.target)) { try { localStorage.removeItem(draftKey(t)); } catch { /* none kept */ } } }, true);
document.addEventListener('rh:page', restoreDrafts);
setTimeout(restoreDrafts, 0);  // once every listener below is in place, so a '/swarm' draft becomes the chip again
// Commands in the composer: typing / lists them (Tab or Enter takes the highlighted one), and a brief that starts with
// /swarm says under it which models will run it.
function slashState(brief) {
  const box = brief && brief.closest('form');
  const menu = box && $('[data-slash]', box), note = box && $('[data-swarm-note]', box);
  if (!menu) return null;
  // a typed "/swarm " becomes the Swarm chip: the command leaves the text, and the chip says it was recognised
  const typed = /^\s*\/swarm\s+/.exec(brief.value);
  if (typed) {
    const at = Math.max(0, brief.selectionStart - typed[0].length);
    brief.value = brief.value.slice(typed[0].length);
    brief.setSelectionRange(at, at);
    box.dataset.swarm = '1';
  }
  const v = brief.value;
  menu.hidden = !!box.dataset.swarm || !(/^\/[a-z]*$/.test(v) && '/swarm'.startsWith(v));
  note.hidden = !box.dataset.swarm;
  return menu;
}
function swarmOff(box) {
  delete box.dataset.swarm;
  const brief = box.elements.namedItem('brief');
  slashState(brief);
  brief.dispatchEvent(new Event('input', { bubbles: true }));  // the saved draft drops the command too
  brief.focus();
}
function takeSlash(brief, cmd) {
  brief.value = cmd + brief.value.replace(/^\s*\/[a-z]*\s*/, '');
  brief.focus();
  brief.setSelectionRange(brief.value.length, brief.value.length);
  brief.dispatchEvent(new Event('input', {bubbles: true}));
}
document.addEventListener('input', e => {
  if (e.target instanceof HTMLTextAreaElement && e.target.name === 'brief') slashState(e.target);
});
document.addEventListener('keydown', e => {
  const t = e.target;
  if (!(t instanceof HTMLTextAreaElement) || t.name !== 'brief') return;
  // Backspace at the very start of the brief takes the Swarm chip away, as it would delete a typed token
  if (e.key === 'Backspace' && t.form && t.form.dataset.swarm && t.selectionStart === 0 && t.selectionEnd === 0) {
    e.preventDefault(); swarmOff(t.form); return;
  }
  const menu = slashState(t);
  if (!menu || menu.hidden) return;
  if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey && !e.metaKey && !e.ctrlKey)) {
    e.preventDefault(); e.stopPropagation();
    takeSlash(t, $('[data-slash-cmd]', menu).dataset.slashCmd);
  } else if (e.key === 'Escape') {
    e.preventDefault(); e.stopPropagation();
    menu.hidden = true;
  }
}, true);
document.addEventListener('click', e => {
  const o = e.target instanceof Element && e.target.closest('[data-slash-cmd]');
  if (!o) return;
  const brief = o.closest('form').elements.namedItem('brief');
  takeSlash(brief, o.dataset.slashCmd);
});
document.addEventListener('focusout', e => {
  const t = e.target;
  if (!(t instanceof HTMLTextAreaElement) || t.name !== 'brief') return;
  setTimeout(() => {  // after a click on the menu itself has been taken
    const menu = t.form && $('[data-slash]', t.form);
    if (menu && !menu.contains(document.activeElement)) menu.hidden = true;
  }, 150);
});
document.addEventListener('click', e => {
  const x = e.target instanceof Element && e.target.closest('[data-swarm-off]');
  if (x) swarmOff(x.closest('form'));
});
// the server reads a swarm from the brief, so the command goes back in front of it as the form is sent
document.addEventListener('submit', e => {
  const form = e.target;
  if (!(form instanceof HTMLFormElement) || !form.dataset.swarm) return;
  const brief = form.elements.namedItem('brief');
  if (brief && !/^\s*\/swarm\b/.test(brief.value)) brief.value = '/swarm ' + brief.value;
}, true);
// Swarm settings save as they change; a swarm needs at least one model, so the last one cannot be unticked.
document.addEventListener('change', e => {
  const form = e.target instanceof Element && e.target.closest('[data-swarm-form]');
  if (!form) return;
  if (e.target.name === 'models' && !form.querySelector('input[name="models"]:checked')) {
    e.target.checked = true;
    toast('A swarm needs at least one model.', false, '');
    return;
  }
  fetch(form.action, {method: 'POST', body: new URLSearchParams(new FormData(form))}).then(r => {
    if (!r.ok) throw new Error('not saved');
  }).catch(() => toast('The swarm settings could not be saved.', false, ''));
});
// An identifier moves whole to the next line; only one wider than its column breaks, and then at its own seams
// (after _ and .), never mid-word as "strin / g".
function seamTokens() {
  for (const el of document.querySelectorAll('code.tok:not(.seams)')) {
    if (el.getClientRects().length < 2) continue;
    el.className = 'tok seams';
    el.innerHTML = el.textContent.replace(/[&<>]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;'})[c])
      .replace(/([_.])/g, '$1<wbr>');
  }
}
let seamTimer = 0;
addEventListener('resize', () => { clearTimeout(seamTimer); seamTimer = setTimeout(seamTokens, 150); });
document.addEventListener('rh:page', seamTokens);
seamTokens();
})();
