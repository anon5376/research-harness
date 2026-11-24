// A layered swarm's team on its lead's page: the server draws the agents as a nested list (each with data-lt, its id,
// and data-up, who judges it); this draws the wires between them as curves. A wire flows toward the judge while its
// agent works; when an agent finishes, a spark runs up its wire; the work each judge kept stays lit up to the lead.
(() => {
const seen = new Map();  // agent id -> the tone it had when last drawn, so a change can be played once
const still = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
const narrow = () => matchMedia('(max-width: 600px)').matches;

function wire(a, b, o) {
  // from the agent (a) to its judge (b): top-down on a wide page, an indented outline on a phone
  if (narrow()) {
    const x1 = a.left - o.x, y1 = a.top + a.height / 2 - o.y, x2 = b.left + 14 - o.x, y2 = b.bottom - o.y;
    return `M${x1},${y1} H${x2 + 8} Q${x2},${y1} ${x2},${y1 - 8} V${y2}`;
  }
  const x1 = a.left + a.width / 2 - o.x, y1 = a.top - o.y, x2 = b.left + b.width / 2 - o.x, y2 = b.bottom - o.y;
  const m = (y1 + y2) / 2;
  return `M${x1},${y1} C${x1},${m} ${x2},${m} ${x2},${y2}`;
}

function spark(svg, path, kept) {
  const dot = document.createElementNS(svg.namespaceURI, 'circle');
  dot.setAttribute('r', '3');
  dot.setAttribute('class', kept ? 'spark kept' : 'spark');
  svg.append(dot);
  const len = path.getTotalLength(), t0 = performance.now(), T = 720;
  const step = now => {
    const k = Math.min(1, (now - t0) / T), e = 1 - Math.pow(1 - k, 3);
    const p = path.getPointAtLength(len * e);
    dot.setAttribute('cx', p.x);
    dot.setAttribute('cy', p.y);
    dot.style.opacity = String(k < .8 ? 1 : (1 - k) / .2);
    if (k < 1 && dot.isConnected) requestAnimationFrame(step); else dot.remove();
  };
  requestAnimationFrame(step);
}

function draw() {
  const box = document.querySelector('[data-lt-box]');
  if (!box) { seen.clear(); return; }
  const svg = box.querySelector('svg'), tree = box.querySelector('ul');
  tree.style.zoom = '';  // a wide team shrinks to fit its card, down to 72%; past that the card scrolls
  const room = box.clientWidth - 28;
  if (!narrow() && tree.scrollWidth > room) tree.style.zoom = String(Math.max(.72, room / tree.scrollWidth));
  const r = box.getBoundingClientRect();
  const o = {x: r.left - box.scrollLeft, y: r.top - box.scrollTop};
  svg.setAttribute('width', box.scrollWidth);
  svg.setAttribute('height', box.scrollHeight);
  svg.replaceChildren();
  const nodes = [...box.querySelectorAll('[data-lt]')];
  const by = new Map(nodes.map(n => [n.dataset.lt, n]));
  const play = [], first = !seen.size;
  for (const n of nodes) {
    const up = by.get(n.dataset.up);
    if (!up) continue;
    const kept = n.classList.contains('kept'), tone = n.dataset.tone;
    const p = document.createElementNS(svg.namespaceURI, 'path');
    p.setAttribute('d', wire(n.getBoundingClientRect(), up.getBoundingClientRect(), o));
    p.setAttribute('class', `w s-${tone}${kept ? ' kept' : ''}`);
    p.dataset.from = n.dataset.lt;
    svg.append(p);
    const was = seen.get(n.dataset.lt);
    if (!first && was && was !== tone && (tone === 'done' || tone === 'fail')) play.push([p, kept]);
    if (!first && kept && was && !seen.has(n.dataset.lt + ' kept')) {  // newly kept: the lit wire draws itself in
      const len = p.getTotalLength();
      if (!still()) p.animate([{strokeDasharray: `${len}`, strokeDashoffset: len}, {strokeDasharray: `${len}`, strokeDashoffset: 0}],
                              {duration: 640, easing: 'cubic-bezier(.2,.7,.2,1)', delay: 360});
    }
  }
  seen.clear();
  for (const n of nodes) {
    seen.set(n.dataset.lt, n.dataset.tone);
    if (n.classList.contains('kept')) seen.set(n.dataset.lt + ' kept', '1');
  }
  if (!still()) for (const [p, kept] of play) spark(svg, p, kept);
}

// hovering an agent lights its line of report up to the lead
function hot(e) {
  const box = document.querySelector('[data-lt-box]');
  if (!box) return;
  const n = e.target instanceof Element ? e.target.closest('[data-lt]') : null;
  const on = new Set();
  for (let at = n; at; at = box.querySelector(`[data-lt="${CSS.escape(at.dataset.up || '')}"]`)) {
    on.add(at.dataset.lt);
    if (!at.dataset.up) break;
  }
  for (const p of box.querySelectorAll('svg path')) p.classList.toggle('hot', on.has(p.dataset.from));
}

let frame = 0;
const later = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(draw); };
document.addEventListener('rh:page', later);
window.addEventListener('resize', later);
document.addEventListener('pointerover', hot);
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', later); else later();
})();
