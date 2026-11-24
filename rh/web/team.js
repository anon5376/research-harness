// Swarm teams: presets edited as a tree you drag (Settings › Swarm › Edit teams, or the composer's swarm popover).
// The page carries the presets as JSON (#team-data); every change is sent whole to /settings/swarm-presets, which
// validates it (rh/hierarchy.py) and says why when it refuses. Motion is the app's: a short ease-out, no overshoot.
(function () {
'use strict';
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const calm = matchMedia('(prefers-reduced-motion: reduce)');
const EASE = 'cubic-bezier(.2, .7, .2, 1)';
const MODEL_RE = /^[a-z0-9][a-z0-9.-]{0,79}$/;
const EFFORTS = {claude: ['light', 'medium', 'high', 'extra-high', 'max'],
                 codex: ['light', 'medium', 'high', 'extra-high', 'max', 'ultra'], devin: []};
const WORDS = {light: 'Light', medium: 'Medium', high: 'High', 'extra-high': 'Extra high', max: 'Max', ultra: 'Ultra'};
const LIMITS = {jobs: 12, workers: 8, depth: 4, count: 4};
const T = {data: null, cur: null, sel: null, uid: 0, timer: 0, drag: null, dirty: false};
// app.js's one swarm line (it is served ahead of this file in the page's one script): with no judge it names the
// ticked models, the Quick swarm's subtitle in every list
const swarmLine = window.RHswarmLine;

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === 'class') el.className = v;
    else if (k === 'html') el.innerHTML = v;  // only the app's own icon markup comes this way
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'value') el.value = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid);
  return el;
}

function data() {
  if (T.data) return T.data;
  const el = $('#team-data');
  if (!el) return null;
  try { T.data = JSON.parse(el.dataset.json); } catch (e) { return null; }
  for (const p of T.data.presets.presets) key(p.tree);
  return T.data;
}
function key(n) { n._k = n._k || 'n' + (++T.uid); (n.children || []).forEach(key); return n; }
const presets = () => T.data.presets.presets;
const preset = () => presets().find(p => p.id === T.cur) || null;
const roleOf = (p, id) => p.roles.find(r => r.id === id) || {id, name: id, prompt: '', models: []};

function model(id) {
  const d = T.data, m = d.models.find(x => x.id === id);
  if (m) return m;
  const prov = id.startsWith('claude-') ? 'claude' : id.startsWith('gpt-') ? 'codex' : 'devin';
  return {id, name: d.names[id] || id, prov, plan: prov === 'codex', off: true};
}
const icon = prov => T.data.icons[prov === 'codex' ? 'openai' : prov] || '';

// what the server will run (rh/hierarchy.plan), counted here so the editor can say it and refuse a team too big
function census(p) {
  let workers = 0, judges = 0, depth = 0;
  const walk = (n, d) => {
    depth = Math.max(depth, d);
    if (!n.children.length) { workers++; return; }
    judges++;
    for (const c of n.children) for (let i = 0; i < (c.count || 1); i++) walk(c, d + 1);
  };
  if (p.shape === 'horizontal') {
    const all = n => n.children.reduce((s, c) => s + (c.count || 1) * (1 + all(c)), 0);
    workers = all(p.tree); judges = 1; depth = workers ? 1 : 0;
  } else walk(p.tree, 0);
  if (!p.tree.children.length) workers = 0;
  // jobs counts the lead's plan as its own run (the swarm's limit); agents counts who is drawn, as the owner reads it
  return {workers, judges, depth, agents: workers + judges, jobs: workers + judges + (p.plan ? 1 : 0)};
}
function fits(p) {
  const c = census(p);
  if (!p.tree.children.length) return 'The lead needs at least one agent under it.';
  if (c.jobs > LIMITS.jobs) return `This team would run ${c.jobs} agents; a swarm runs at most ${LIMITS.jobs}.`;
  if (c.workers > LIMITS.workers) return `At most ${LIMITS.workers} agents can do the work.`;
  if (c.depth > LIMITS.depth) return `A team is at most ${LIMITS.depth} layers deep.`;
  return '';
}

function find(n, k, parent = null) {
  if (n._k === k) return {node: n, parent};
  for (const c of n.children) { const f = find(c, k, n); if (f) return f; }
  return null;
}
const within = (n, k) => n._k === k || n.children.some(c => within(c, k));

// ---- saving ----
function say(text, bad) {
  const el = $('[data-team-saved]');
  if (!el) return;
  el.textContent = text;
  el.classList.toggle('bad', !!bad);
  el.classList.remove('shown'); void el.offsetWidth; el.classList.add('shown');
}
function save() {
  T.dirty = true;
  clearTimeout(T.timer);
  say('Saving…');
  T.timer = setTimeout(async () => {
    const form = $('[data-team-form]');
    const body = new URLSearchParams(new FormData(form));
    body.set('data', JSON.stringify(T.data.presets, (k, v) => k === '_k' ? undefined : v));
    try {
      const r = await fetch(form.action, {method: 'POST', body});
      if (r.ok) say('Saved');
      else say((await r.text()) || 'Not saved', true);
    } catch (e) { say('Not saved: the app did not answer', true); }
  }, 420);
}
async function use(pid) {
  const form = $('[data-team-active]');
  const body = new URLSearchParams(new FormData(form));
  body.set('active', pid);
  try {
    const r = await fetch(form.action, {method: 'POST', body});
    if (!r.ok) { say((await r.text()) || 'Not switched', true); return; }
  } catch (e) { say('Not switched: the app did not answer', true); return; }
  T.data.presets.active = pid;
  say('In use for /swarm');
  sync();
  render(false);
}

// ---- the composer's team list and Settings' team name follow the editor ----
// a radio's line is plain text, 'A · B': it is drawn with the dot as the page draws it
function line(el, text) {
  const at = text.lastIndexOf(' · ');
  if (at < 0) { el.textContent = text; return; }
  el.replaceChildren(text.slice(0, at), h('span', {class: 'sep'}, '·'), text.slice(at + 3));
}
function size(p) { return census(p).agents; }
// a team's subtitle, the same in the list, the composer and the page: its lead's model, then its layers or 'side by side'
function sub(p) {
  const c = census(p);
  return `${model(p.tree.model).name} · ${p.shape === 'vertical' ? c.depth + 1 + ' layers' : 'side by side'}`;
}
function sync() {
  const d = T.data, active = d.presets.active;
  const named = active === 'quick' ? 'Quick swarm' : (presets().find(p => p.id === active) || {}).name || 'Quick swarm';
  for (const b of $$('[data-team-name]')) b.textContent = named;
  const pick = $('[data-team-pick]');
  if (pick) {
    const form = pick.querySelector('input')?.getAttribute('form') || 'team-pick-f';
    const quickSays = pick.querySelector('input[value="quick"]')?.dataset.says || '';
    const cut = quickSays.lastIndexOf(' · ');  // the subtitle is the line's models part, as the chip names them
    const rows = [['quick', 'Quick swarm', cut < 0 ? quickSays : quickSays.slice(0, cut), quickSays]].concat(presets().map(p =>
      [p.id, p.name, sub(p),
       `${p.name} · ${size(p)} agents`]));
    const edit = pick.querySelector('[data-team-open]');
    pick.replaceChildren(...rows.map(([id, name, sub, says]) => h('label', {class: 'swarm-team'},
      h('input', {type: 'radio', name: 'active', value: id, form, checked: id === active, 'data-says': says}),
      h('span', {class: 'swarm-team-n'}, name), h('span', {class: 'swarm-team-d'}, sub))), edit || '');
    const pop = pick.closest('.swarm-pop');
    if (pop) { pop.dataset.team = active === 'quick' ? 'quick' : 'preset';
               pop.classList.toggle('more', pop.scrollTop + pop.clientHeight < pop.scrollHeight - 2); }
    const says = $('[data-swarm-says]');
    const chosen = rows.find(r => r[0] === active);
    if (says && chosen && active !== 'quick') line(says, chosen[3]);
  }
}
document.addEventListener('change', async e => {
  const t = e.target;
  if (!(t instanceof HTMLInputElement) || !t.closest('[data-team-pick]')) return;
  const form = t.form;
  if (!form) return;
  const body = new URLSearchParams(new FormData(form));
  body.set('active', t.value);
  const r = await fetch(form.action, {method: 'POST', body}).catch(() => null);
  if (!r || !r.ok) { t.checked = false; return; }
  const pop = t.closest('.swarm-pop');
  if (pop) { pop.dataset.team = t.value === 'quick' ? 'quick' : 'preset';
             pop.classList.toggle('more', pop.scrollTop + pop.clientHeight < pop.scrollHeight - 2); }
  const says = $('[data-swarm-says]');
  if (says) line(says, t.dataset.says || '');
  if (data()) { T.data.presets.active = t.value; for (const b of $$('[data-team-name]'))
    b.textContent = t.value === 'quick' ? 'Quick swarm' : (presets().find(p => p.id === t.value) || {}).name; }
});

// ---- drawing ----
function card(p, n, depth) {
  const m = model(n.model), role = roleOf(p, n.role);
  const scope = role.models.includes(n.model);
  // two lines at one height (r28 F1/F3): role and effort, then the short model name as Settings writes it, which
  // wraps before it is ever cut; a model the team cannot run wears a lock right after its name (amber means 'needs
  // you' in this app), and the words for every mark are in the card's label and title and in the inspector
  const short = m.name.replace(/^Claude /, '');
  const effort = n.effort ? WORDS[n.effort] || n.effort : '', count = (n.count || 1) > 1 ? '×' + n.count : '';
  const notes = [m.off ? 'Not in policy: the swarm will not start' : m.plan ? 'ChatGPT plan: runs on Codex' : '',
                 !scope && !m.off ? `Outside role: ${role.name} does not list this model` : ''].filter(Boolean);
  const lead = depth === 0;
  const what = lead ? (p.plan ? 'Plans, then judges' : 'Judges') : n.children.length && p.shape === 'vertical' ? 'Judges its team'
    : 'Works';
  return h('div', {class: 'tn' + (T.sel === n._k ? ' sel' : '') + (lead ? ' root' : '') + (m.off ? ' off' : ''), 'data-k': n._k, tabindex: 0,
                   role: 'button', 'aria-pressed': T.sel === n._k ? 'true' : 'false', title: notes.join('\n') || null,
                   'aria-label': `${role.name}, ${m.name}${effort ? ', ' + effort + ' effort' : ''}${count ? ', ' + n.count + ' copies' : ''}`
                     + `${lead ? ', the lead' : ''}. ${what}.${notes.length ? ' ' + notes.join('. ') + '.' : ''}`},
    h('span', {class: 'tn-ic', html: icon(m.prov)}),
    h('span', {class: 'tn-t'}, h('span', {class: 'tn-role'}, [role.name, effort, count].filter(Boolean).join(' · '),
                                     h('span', {class: 'tn-does'}, ' · ' + what)),
      h('span', {class: 'tn-m'}, short.slice(0, short.lastIndexOf(' ') + 1),
        // the mark stays with the name's last word, so it never wraps onto a line of its own
        h('span', {class: 'tn-end'}, short.slice(short.lastIndexOf(' ') + 1),
          m.off ? h('span', {class: 'tn-lock', 'aria-hidden': 'true'}) : notes.length ? h('span', {class: 'tn-mark', 'aria-hidden': 'true'}) : null))),
    h('button', {type: 'button', class: 'tn-add', title: 'Add an agent under this one', 'aria-label': 'Add an agent under ' + m.name,
                 'data-add': n._k}, '+'));
}
function branch(p, n, depth) {
  const li = h('li', {'data-k': n._k}, card(p, n, depth));
  if (n.children.length) li.append(h('ul', {}, n.children.map(c => branch(p, c, depth + 1))));
  return li;
}
function canvas(p, animate) {
  const box = $('.tm-canvas');
  if (!box) return;
  const before = new Map();
  if (animate && !calm.matches) for (const c of $$('.tn', box)) before.set(c.dataset.k, c.getBoundingClientRect());
  box.dataset.shape = p.shape;
  box.replaceChildren(h('ul', {class: 'tt'}, branch(p, p.tree, 0)));
  fit(box);
  if (!animate || calm.matches) return;
  for (const c of $$('.tn', box)) {
    const b = before.get(c.dataset.k), a = c.getBoundingClientRect();
    if (!b) {
      c.animate([{opacity: 0, transform: 'translateY(8px) scale(.96)'}, {opacity: 1, transform: 'none'}],
                {duration: 300, easing: EASE});
    } else if (Math.abs(b.left - a.left) + Math.abs(b.top - a.top) > 1) {
      c.animate([{transform: `translate(${b.left - a.left}px, ${b.top - a.top}px)`}, {transform: 'none'}],
                {duration: 300, easing: EASE});
    }
  }
  box.classList.remove('lines-in'); void box.offsetWidth; box.classList.add('lines-in');
}

// a team wider than the canvas shrinks to fit (never under 62%), so the whole hierarchy reads at once
const OUTLINE = matchMedia('(max-width: 760px)');  // rh.css draws the team as an indented outline there
function fit(box) {
  const tt = $('.tt', box);
  if (!tt) return;
  tt.style.zoom = '';
  if (OUTLINE.matches) return;
  const room = box.clientWidth - 24, need = tt.scrollWidth;  // the canvas's 12px sides
  tt.style.zoom = need > room ? String(Math.max(.62, room / need)) : '';
}
addEventListener('resize', () => { const box = $('#team[open] .tm-canvas'); if (box) fit(box); });

function list() {
  const d = T.data, active = d.presets.active;
  const row = (id, name, sub) => h('button', {type: 'button', class: 'tm-p' + (T.cur === id ? ' on' : ''), 'data-pick': id,
                                              'aria-current': T.cur === id ? 'true' : false},
    h('span', {class: 'tm-p-n'}, name, active === id ? h('span', {class: 'tm-use', title: 'Used by /swarm'}, 'In use') : null),
    h('span', {class: 'tm-p-d'}, sub));
  const q = d.quickNames;
  return h('nav', {class: 'tm-list', 'aria-label': 'Teams'},
    row('quick', 'Quick swarm', q.models.length ? swarmLine(q.models) : 'the ticks in Settings'),
    presets().map(p => row(p.id, p.name, sub(p))),
    h('button', {type: 'button', class: 'tm-new', 'data-new': ''}, '+ New team'));
}

function quickPane() {
  const d = T.data, active = d.presets.active === 'quick';
  return h('section', {class: 'tm-main tm-quick'},
    h('h3', {}, 'Quick swarm'),
    h('p', {class: 'soft'}, 'Runs the brief on each model ticked in Settings › Swarm at once; the judge keeps the best work. ' +
      'No system prompts, no layers.'),
    h('p', {}, d.quickNames.models.join(', ') || 'No models ticked', h('span', {class: 'soft'}, ' · judge ' + (d.quickNames.judge || '—'))),
    h('button', {type: 'button', class: 'btn' + (active ? ' quiet' : ' primary'), 'data-use': 'quick', disabled: active}, active ? 'In use for /swarm' : 'Use for /swarm'));
}

function bar(p) {
  const c = census(p), active = T.data.presets.active === p.id;
  return h('div', {class: 'tm-bar'},
    h('input', {class: 'tm-name', type: 'text', maxlength: 60, value: p.name, 'aria-label': 'Team name', 'data-name': ''}),
    h('div', {class: 'tm-seg', role: 'radiogroup', 'aria-label': 'Shape'},
      ['vertical', 'horizontal'].map(s => h('button', {type: 'button', role: 'radio', 'aria-checked': p.shape === s ? 'true' : 'false',
        'data-shape': s, title: s === 'vertical' ? 'Layers: each manager judges its team, the lead judges the managers'
          : 'Side by side: every agent works as a peer, the lead judges them all'},
        s === 'vertical' ? 'Vertical' : 'Horizontal'))),
    h('label', {class: 'tm-tog'}, h('input', {type: 'checkbox', 'data-plan': '', checked: !!p.plan}),
      h('span', {class: 'tm-sw', 'aria-hidden': 'true'}), 'Lead plans first'),
    h('span', {class: 'tm-census mono', 'aria-live': 'polite'},
      `${c.agents} agents · ${c.workers} ${c.workers === 1 ? 'worker' : 'workers'} · ${p.shape === 'vertical' ? c.depth + 1 + ' layers' : 'peers'}`),
    h('span', {class: 'tm-bar-r'},
      h('button', {type: 'button', class: 'btn small' + (active ? ' quiet' : ' primary'), 'data-use': p.id, disabled: active},
        active ? 'In use for /swarm' : 'Use for /swarm'),
      // the rarer actions wait in one menu, so the head is one row: name, shape, plan, count, use, more (r28 F4)
      h('details', {class: 'tm-more'},
        h('summary', {class: 'tm-more-b', 'aria-label': 'More team actions', title: 'More'}, '⋯'),
        h('div', {class: 'tm-menu', role: 'menu'},
          h('button', {type: 'button', role: 'menuitem', 'data-dup': ''}, 'Duplicate'),
          h('button', {type: 'button', role: 'menuitem', class: 'danger-q', 'data-del': ''}, 'Delete')))));
}
// a click anywhere else closes the menu
document.addEventListener('click', e => {
  for (const m of $$('details.tm-more[open]')) if (!m.contains(e.target)) m.open = false;
});

function palette(p) {
  return h('div', {class: 'tm-pal', 'aria-label': 'Roles'},
    h('span', {class: 'tm-pal-h'}, 'Drag a role onto an agent'),
    p.roles.map(r => h('span', {class: 'tm-chip', 'data-role': r.id, title: `Drag onto an agent to add a ${r.name} under it`},
      h('span', {class: 'tm-grip', 'aria-hidden': 'true'}), r.name)));
}

function optionList(p, n) {
  const role = roleOf(p, n.role), all = T.data.models.map(m => m.id);
  const extra = [...new Set([...role.models.filter(m => !all.includes(m)), ...(all.includes(n.model) || role.models.includes(n.model) ? [] : [n.model])])];
  const opt = id => { const m = model(id); return h('option', {value: id, selected: id === n.model},
    m.name + (m.off ? ' · not in policy' : m.plan ? ' · ChatGPT plan' : '')); };
  return [h('optgroup', {label: `Allowed for ${role.name}`}, role.models.map(opt)),
          h('optgroup', {label: 'Other models'}, [...all.filter(m => !role.models.includes(m)), ...extra.filter(m => !role.models.includes(m))].map(opt))];
}

function inspector(p) {
  const f = T.sel && find(p.tree, T.sel);
  if (!f) return h('aside', {class: 'tm-ins empty'}, h('p', {class: 'soft'}, 'Pick an agent to set its role, model and effort. ' +
    'Drag agents to change who reports to whom; drop on an agent to put it under that agent, near its edge to put it beside.'));
  const n = f.node, root = !f.parent, m = model(n.model), efforts = EFFORTS[m.prov] || [];
  const parents = [];
  const walk = (x, d) => {  // any agent but this one and those under it can be its manager
    if (x._k === n._k) return;
    parents.push([x, d]);
    x.children.forEach(c => walk(c, d + 1));
  };
  walk(p.tree, 0);
  return h('aside', {class: 'tm-ins'},
    h('h3', {}, root ? 'The lead' : 'Agent'),
    h('label', {class: 'tm-f'}, h('span', {}, 'Role'), h('select', {'data-f': 'role'},
      p.roles.map(r => h('option', {value: r.id, selected: r.id === n.role}, r.name)))),
    h('label', {class: 'tm-f'}, h('span', {}, 'Model'), h('select', {'data-f': 'model'}, optionList(p, n))),
    efforts.length ? h('label', {class: 'tm-f'}, h('span', {}, 'Effort'), h('select', {'data-f': 'effort'},
      h('option', {value: '', selected: !n.effort}, 'Default'),
      efforts.map(e => h('option', {value: e, selected: e === n.effort}, WORDS[e])))) : null,
    root ? h('p', {class: 'soft tm-note'}, p.plan ? 'Writes the plan every agent reads first, then judges the layer under it.'
      : 'Judges the layer under it and keeps the best work.') :
      h('div', {class: 'tm-f'}, h('span', {}, 'Copies'), h('span', {class: 'tm-step'},
        h('button', {type: 'button', 'data-count': -1, 'aria-label': 'One fewer', disabled: (n.count || 1) <= 1}, '−'),
        h('b', {class: 'mono'}, String(n.count || 1)),
        h('button', {type: 'button', 'data-count': 1, 'aria-label': 'One more', disabled: (n.count || 1) >= LIMITS.count}, '+'))),
    root ? null : h('label', {class: 'tm-f'}, h('span', {}, 'Reports to'), h('select', {'data-f': 'parent'},
      parents.map(([x, d]) => h('option', {value: x._k, selected: x === f.parent},
        ' '.repeat(d) + roleOf(p, x.role).name + ' · ' + model(x.model).name)))),
    root ? null : h('div', {class: 'tm-row'},
      h('button', {type: 'button', class: 'btn quiet small', 'data-move': -1, 'aria-label': 'Move earlier'}, '↑'),
      h('button', {type: 'button', class: 'btn quiet small', 'data-move': 1, 'aria-label': 'Move later'}, '↓'),
      h('button', {type: 'button', class: 'tm-add-under', 'data-add': n._k}, 'Add under'),
      h('button', {type: 'button', class: 'btn quiet small danger-q', 'data-remove': ''}, 'Remove')),
    root ? h('div', {class: 'tm-row'}, h('button', {type: 'button', class: 'tm-add-under', 'data-add': n._k}, 'Add under')) : null);
}

function roles(p) {
  const used = new Set();
  const walk = n => { used.add(n.role); n.children.forEach(walk); };
  walk(p.tree);
  const all = T.data.models.map(m => m.id);
  return h('details', {class: 'tm-roles', open: T.rolesOpen || false},
    h('summary', {}, 'Roles, system prompts and the models each may use'),
    h('div', {class: 'tm-roles-in'}, p.roles.map(r => h('div', {class: 'tm-role', 'data-role-id': r.id},
      h('div', {class: 'tm-role-h'},
        h('input', {type: 'text', class: 'tm-role-n', maxlength: 60, value: r.name, 'aria-label': 'Role name', 'data-rf': 'name'}),
        used.has(r.id) ? h('span', {class: 'soft'}, 'in use') :
          h('button', {type: 'button', class: 'btn quiet small danger-q', 'data-role-del': ''}, 'Remove role')),
      h('label', {class: 'tm-f tm-prompt'}, h('span', {}, 'System prompt'),
        h('textarea', {rows: 4, maxlength: 4000, 'data-rf': 'prompt', placeholder: 'Who this agent is in the team, and how it works'}, r.prompt)),
      h('div', {class: 'tm-scope'}, h('span', {class: 'tm-scope-h'}, 'Models it may use'),
        r.models.map(id => { const m = model(id); return h('span', {class: 'tm-mchip' + (m.off ? ' off' : m.plan ? ' plan' : '')},
          h('span', {class: 'tm-mchip-ic', html: icon(m.prov)}), m.name,
          h('button', {type: 'button', 'aria-label': 'Remove ' + m.name, 'data-scope-del': id}, '×')); }),
        h('select', {'data-scope-add': '', 'aria-label': 'Add a model'}, h('option', {value: ''}, 'Add a model…'),
          all.filter(id => !r.models.includes(id)).map(id => h('option', {value: id}, model(id).name + (model(id).plan ? ' · ChatGPT plan' : '')))),
        h('input', {type: 'text', class: 'tm-custom mono', placeholder: 'or type a model id: gpt-6.2-astra', maxlength: 80,
                    'data-scope-custom': '', 'aria-label': 'Add a model by id'}))))),
    h('button', {type: 'button', class: 'btn quiet small tm-addrole', 'data-role-add': ''}, '+ Add role'));
}

function render(animate) {
  const body = $('[data-team-body]');
  if (!body || !data()) return;
  const p = preset();
  // a team opens with its lead picked, so the inspector shows an agent to edit rather than a help paragraph
  if (p && !(T.sel && find(p.tree, T.sel))) T.sel = p.tree._k;
  const scroll = $('.tm-work', body)?.scrollTop || 0;
  const main = p ? h('section', {class: 'tm-main'}, bar(p),
    h('div', {class: 'tm-work'}, h('div', {class: 'tm-stage'}, palette(p), h('div', {class: 'tm-canvas lines-in'})), roles(p)),
    inspector(p)) : quickPane();
  if (!animate || !$('.tm', body)) body.replaceChildren(h('div', {class: 'tm'}, list(), main));
  else {
    $('.tm-list', body).replaceWith(list());
    $('.tm-main', body).replaceWith(main);
  }
  if (p) {
    const old = $('.tm-canvas', body);
    if (animate && T.lastCanvas) { old.replaceChildren(...T.lastCanvas.childNodes); }
    canvas(p, animate);
    T.lastCanvas = null;
    const work = $('.tm-work', body);
    if (work) work.scrollTop = scroll;
  }
}
// keep the old drawing for one frame, so the move animates from where the cards were
function change(fn, animate = true) {
  const p = preset();
  if (!p) return;
  const box = $('.tm-canvas');
  const before = JSON.stringify(p);
  fn(p);
  const why = fits(p);
  if (why) {
    Object.assign(p, JSON.parse(before));
    key(p.tree);
    say(why, true);
    render(false);
    return;
  }
  if (box) { T.lastCanvas = box.cloneNode(true); }
  render(animate);
  save();
}

// ---- dragging: pointer events, so a mouse and a finger work the same ----
function zone(target, x, y, p) {
  const k = target.dataset.k, rect = target.getBoundingClientRect();
  if (k === p.tree._k) return 'in';
  // siblings sit side by side only in a wide vertical tree; a phone's outline stacks them, as horizontal does
  const along = p.shape === 'vertical' && !OUTLINE.matches ? (x - rect.left) / rect.width : (y - rect.top) / rect.height;
  return along < .26 ? 'before' : along > .74 ? 'after' : 'in';
}
function clearMarks() { for (const el of $$('.tn.drop-in, .tn.drop-before, .tn.drop-after')) el.classList.remove('drop-in', 'drop-before', 'drop-after'); }
document.addEventListener('pointerdown', e => {
  if (e.button !== 0) return;
  const chip = e.target.closest && e.target.closest('.tm-chip');
  const tn = e.target.closest && e.target.closest('.tm-canvas .tn');
  if (!chip && !tn) return;
  if (e.target.closest('button')) return;
  const p = preset();
  if (!p) return;
  T.drag = {chip: chip && chip.dataset.role, k: tn && tn.dataset.k, el: chip || tn, x: e.clientX, y: e.clientY, on: false,
            root: tn && tn.dataset.k === p.tree._k};
});
document.addEventListener('pointermove', e => {
  const d = T.drag;
  if (!d) return;
  if (!d.on) {
    if (Math.hypot(e.clientX - d.x, e.clientY - d.y) < 5 || d.root) return;
    d.on = true;
    const r = d.el.getBoundingClientRect();
    d.dx = e.clientX - r.left; d.dy = e.clientY - r.top;
    d.ghost = d.el.cloneNode(true);
    d.ghost.classList.add('tm-ghost');
    d.ghost.style.width = r.width + 'px';
    document.body.append(d.ghost);
    d.el.classList.add('dragging');
    document.documentElement.classList.add('tm-dragging');
  }
  e.preventDefault();
  d.ghost.style.transform = `translate3d(${e.clientX - d.dx}px, ${e.clientY - d.dy}px, 0) rotate(-1.2deg) scale(1.03)`;
  clearMarks();
  d.target = null;
  const under = document.elementFromPoint(e.clientX, e.clientY);
  const tn = under && under.closest('.tm-canvas .tn');
  const p = preset();
  if (!tn || !p) return;
  if (d.k && (tn.dataset.k === d.k || within(find(p.tree, d.k).node, tn.dataset.k))) return;
  d.target = tn.dataset.k;
  d.zone = zone(tn, e.clientX, e.clientY, p);
  tn.classList.add('drop-' + d.zone);
});
function drop(e) {
  const d = T.drag;
  T.drag = null;
  if (!d) return;
  clearMarks();
  document.documentElement.classList.remove('tm-dragging');
  if (e.type === 'pointercancel') {  // the page took the gesture (a phone's scroll): not a pick, and no drop
    if (!d.on) return;
    d.target = null;
  }
  if (!d.on) {  // a click: pick the agent
    if (d.k) { T.sel = d.k; render(false); $(`.tn[data-k="${d.k}"]`)?.focus(); }
    return;
  }
  d.el.classList.remove('dragging');
  const ghost = d.ghost;
  const settle = () => ghost.remove();
  if (!d.target) {
    if (calm.matches) settle();
    else {  // nowhere to land: the card goes home
      const home = d.el.getBoundingClientRect();
      ghost.animate([{transform: ghost.style.transform}, {transform: `translate3d(${home.left}px, ${home.top}px, 0)`, opacity: .4}],
                    {duration: 220, easing: EASE}).onfinish = settle;
    }
    return;
  }
  settle();
  change(p => {
    const node = d.k ? (() => { const f = find(p.tree, d.k); f.parent.children.splice(f.parent.children.indexOf(f.node), 1); return f.node; })()
      : key({role: d.chip, model: (roleOf(p, d.chip).models[0] || T.data.models[0].id), effort: '', count: 1, children: []});
    const t = find(p.tree, d.target);
    if (d.zone === 'in' || !t.parent) t.node.children.push(node);
    else t.parent.children.splice(t.parent.children.indexOf(t.node) + (d.zone === 'after' ? 1 : 0), 0, node);
    T.sel = node._k;
  });
}
document.addEventListener('pointerup', drop);
document.addEventListener('pointercancel', drop);

// ---- clicks and fields ----
document.addEventListener('click', e => {
  const t = e.target instanceof Element ? e.target : null;
  if (!t) return;
  if (t.closest('[data-team-open]')) {
    if (!data()) return;
    e.preventDefault();
    const active = T.data.presets.active;
    T.cur = active === 'quick' ? (presets()[0] || {id: 'quick'}).id : active;
    T.sel = null;
    $('#settings')?.open && $('#settings').close();
    t.closest('details')?.removeAttribute('open');
    render(false);
    $('#team').showModal();
    return;
  }
  if (!t.closest('#team')) return;
  const p = preset();
  const pick = t.closest('[data-pick]');
  if (pick) { T.cur = pick.dataset.pick; T.sel = null; render(false); return; }
  if (t.closest('[data-new]')) {
    const base = presets()[0];
    let n = 1; while (presets().some(x => x.id === 'team-' + n)) n++;
    const roles = JSON.parse(JSON.stringify(base ? base.roles : T.data.template));
    const worker = roles.find(r => r.id === 'worker') || roles[roles.length - 1];
    const lead = roles.find(r => r.id === 'lead') || roles[0];
    const fresh = {id: 'team-' + n, name: 'New team ' + n, shape: 'vertical', plan: false, roles,
                   tree: key({role: lead.id, model: lead.models[0] || 'swe-2-max', effort: '', count: 1, children: [
                     {role: worker.id, model: worker.models[0] || 'swe-2-max', effort: '', count: 1, children: []},
                     {role: worker.id, model: worker.models[1] || worker.models[0] || 'swe-2-max', effort: '', count: 1, children: []}]})};
    presets().push(fresh);
    T.cur = fresh.id; T.sel = null;
    render(false); save();
    $('.tm-name')?.select();
    return;
  }
  const useIt = t.closest('[data-use]');
  if (useIt) { use(useIt.dataset.use); return; }
  if (!p) return;
  const shape = t.closest('[data-shape]');
  if (shape) { change(q => { q.shape = shape.dataset.shape; }); return; }
  if (t.closest('[data-dup]')) {
    let n = 2; while (presets().some(x => x.id === `${p.id}-${n}`.slice(0, 40))) n++;
    const copy = JSON.parse(JSON.stringify(p, (k, v) => k === '_k' ? undefined : v));
    copy.id = `${p.id}-${n}`.slice(0, 40); copy.name = `${p.name} ${n}`.slice(0, 60);
    key(copy.tree); presets().push(copy); T.cur = copy.id; T.sel = null;
    render(false); save(); return;
  }
  const del = t.closest('[data-del]');
  if (del) {
    if (!del.classList.contains('arm')) {  // a second press deletes: the first only asks
      del.classList.add('arm'); del.textContent = 'Delete this team?';
      setTimeout(() => { if (del.isConnected) { del.classList.remove('arm'); del.textContent = 'Delete'; } }, 3500);
      return;
    }
    T.data.presets.presets = presets().filter(x => x !== p);
    if (T.data.presets.active === p.id) T.data.presets.active = 'quick';
    T.cur = (presets()[0] || {id: 'quick'}).id; T.sel = null;
    render(false); save(); sync(); return;
  }
  const add = t.closest('[data-add]');
  if (add) {
    change(q => {
      const f = find(q.tree, add.dataset.add);
      const below = q.roles.find(r => r.id === 'worker') || q.roles[q.roles.length - 1];
      const node = key({role: below.id, model: below.models[0] || T.data.models[0].id, effort: '', count: 1, children: []});
      f.node.children.push(node); T.sel = node._k;
    });
    return;
  }
  const count = t.closest('[data-count]');
  if (count && T.sel) { change(q => { const n = find(q.tree, T.sel).node; n.count = Math.min(LIMITS.count, Math.max(1, (n.count || 1) + +count.dataset.count)); }, false); return; }
  const move = t.closest('[data-move]');
  if (move && T.sel) {
    change(q => { const f = find(q.tree, T.sel), sib = f.parent.children, i = sib.indexOf(f.node), j = i + +move.dataset.move;
                  if (j >= 0 && j < sib.length) { sib.splice(i, 1); sib.splice(j, 0, f.node); } });
    return;
  }
  if (t.closest('[data-remove]') && T.sel) {
    change(q => { const f = find(q.tree, T.sel); f.parent.children.splice(f.parent.children.indexOf(f.node), 1); T.sel = f.parent._k; });
    return;
  }
  const scopeDel = t.closest('[data-scope-del]');
  if (scopeDel) {
    const r = p.roles.find(x => x.id === scopeDel.closest('[data-role-id]').dataset.roleId);
    r.models = r.models.filter(m => m !== scopeDel.dataset.scopeDel);
    T.rolesOpen = true; render(false); save(); return;
  }
  if (t.closest('[data-role-add]')) {
    let n = 1; while (p.roles.some(r => r.id === 'role-' + n)) n++;
    if (p.roles.length >= 8) { say('A team has at most 8 roles.', true); return; }
    p.roles.push({id: 'role-' + n, name: 'New role', prompt: '', models: ['swe-2-max']});
    T.rolesOpen = true; render(false); save(); return;
  }
  if (t.closest('[data-role-del]')) {
    const id = t.closest('[data-role-id]').dataset.roleId;
    p.roles = p.roles.filter(r => r.id !== id);
    T.rolesOpen = true; render(false); save(); return;
  }
  const tn = t.closest('.tm-canvas .tn');
  if (tn && e.detail === 0) { T.sel = tn.dataset.k; render(false); $(`.tn[data-k="${tn.dataset.k}"]`)?.focus(); }  // keyboard
});
document.addEventListener('keydown', e => {
  const tn = e.target instanceof Element && e.target.closest('#team .tm-canvas .tn');
  if (tn && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); tn.click(); }
});
document.addEventListener('change', e => {
  const t = e.target;
  if (!(t instanceof Element) || !t.closest('#team')) return;
  const p = preset();
  if (!p) return;
  if (t.matches('[data-name]')) { p.name = t.value.trim().slice(0, 60) || p.name; render(false); save(); sync(); return; }
  if (t.matches('[data-plan]')) { change(q => { q.plan = t.checked; }, false); return; }
  const f = t.dataset.f;
  if (f && T.sel) {
    if (f === 'parent') {
      change(q => { const me = find(q.tree, T.sel), to = find(q.tree, t.value);
                    me.parent.children.splice(me.parent.children.indexOf(me.node), 1); to.node.children.push(me.node); });
    } else change(q => { const n = find(q.tree, T.sel).node; n[f] = t.value;
                         if (f === 'model' && !(EFFORTS[model(t.value).prov] || []).includes(n.effort)) n.effort = ''; }, false);
    return;
  }
  const role = t.closest('[data-role-id]');
  if (role) {
    const r = p.roles.find(x => x.id === role.dataset.roleId);
    if (t.matches('[data-rf]')) { r[t.dataset.rf] = t.dataset.rf === 'name' ? (t.value.trim() || r.name) : t.value; }
    else if (t.matches('[data-scope-add]') && t.value) { r.models.push(t.value); }
    else if (t.matches('[data-scope-custom]')) {
      const id = t.value.trim().toLowerCase();
      if (!MODEL_RE.test(id)) { say('A model id is lowercase letters, digits, dots and dashes.', true); return; }
      if (!r.models.includes(id)) r.models.push(id);
      if (!T.data.names[id] && !T.data.models.some(m => m.id === id)) T.data.names[id] = id;
    }
    T.rolesOpen = true;
    render(false); save();
  }
});
document.addEventListener('toggle', e => { if (e.target instanceof Element && e.target.matches('.tm-roles')) T.rolesOpen = e.target.open; }, true);
})();
