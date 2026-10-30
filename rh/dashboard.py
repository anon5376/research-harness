"""The app. With a token it is the live page `rh serve` sends: a sidebar of jobs grouped by what they need, and a
pane for the open job (what it was asked, its questions, result, output and timeline, with its actions), for the
overview, or for the New job composer when the server can add jobs. One same-origin script, SCRIPT (served at
/app.js), keeps it live, moves between jobs without reloading and turns submits into in-place updates; without it
every link and form still works. Without a token it is the static snapshot `rh status --html` writes: the overview
and the job lists, with no script and no controls."""
import re
import zlib
from html import escape, unescape


# The sidebar sits on a tinted frame; the open pane is one sheet beside it. Colours are tokens; tests/test_dashboard.py
# computes each text pair's contrast. Motion uses transform and opacity only.
_STYLE = """
:root {
  color-scheme: light dark;
  --frame: #eaeef0;
  --hover: #dfe4e7;
  --sheet: #ffffff;
  --raised: #f4f6f7;
  --border: #e0e5e8;
  --rule: #edf0f2;
  --control: #c3cad0;
  --ink: #131416;
  --muted: #5a5f65;
  --accent: #1b6c80;
  --on-accent: #ffffff;
  --neutral-bg: #eceef0;
  --neutral-ink: #363a3e;
  --active-bg: #e0f0f3;
  --active-ink: #145566;
  --good-bg: #dcf4e4;
  --good-ink: #155a33;
  --warn-bg: #fcf1d4;
  --warn-ink: #774500;
  --bad-bg: #fde8e6;
  --bad-ink: #8e1e1e;
  --shade: rgba(16, 24, 32, .16);
  --ease: cubic-bezier(.2, .8, .2, 1);
}
@media (prefers-color-scheme: dark) {
  :root {
    --frame: #0e1011;
    --hover: #1b1e20;
    --sheet: #17191b;
    --raised: #1f2224;
    --border: #2a2e31;
    --rule: #222628;
    --control: #464c51;
    --ink: #eceeef;
    --muted: #a2a8ad;
    --accent: #5cb3c7;
    --on-accent: #0b1a1e;
    --neutral-bg: #272a2d;
    --neutral-ink: #c9cdd1;
    --active-bg: #113039;
    --active-ink: #8fd1e1;
    --good-bg: #13301f;
    --good-ink: #8ed6a9;
    --warn-bg: #382a0b;
    --warn-ink: #efc66a;
    --bad-bg: #3c1b1b;
    --bad-ink: #f3a4a4;
    --shade: rgba(0, 0, 0, .55);
  }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html { height: 100%; }
body {
  margin: 0;
  min-height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--frame);
  color: var(--ink);
  font: 14px/20px system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  overflow-wrap: anywhere;
}
p, ul, ol, h1, h2, h3, pre, dl, dd, figure, fieldset { margin: 0; }
ul, ol { list-style: none; padding: 0; }
fieldset { padding: 0; border: 0; min-width: 0; }
a { color: inherit; }
code, kbd, pre, .mono { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
code { font-size: 12px; }
.bar { flex: none; display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; min-height: 52px; padding: 8px 16px; }
.mark { position: relative; flex: none; width: 22px; height: 22px; border-radius: 6px; background: var(--accent); }
.mark::before, .mark::after { content: ""; position: absolute; left: 5px; height: 2px; border-radius: 1px; background: var(--on-accent); }
.mark::before { top: 7px; width: 12px; }
.mark::after { top: 13px; width: 7px; }
h1 { margin-right: 4px; font-size: 15px; line-height: 20px; font-weight: 600; }
.health { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; min-width: 0; }
.chip { display: inline-flex; align-items: center; gap: 7px; max-width: 100%; min-height: 28px; padding: 4px 11px;
  border-radius: 999px; background: var(--neutral-bg); color: var(--neutral-ink); font-size: 13px; line-height: 20px; font-weight: 500; }
.chip > i { flex: none; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.chip > i.t-good { background: var(--good-ink); }
.chip > i.beat { background: var(--accent); }
.chip.warn > i.beat { background: currentColor; animation: none; }
summary.chip { cursor: pointer; list-style: none; user-select: none; }
summary.chip::-webkit-details-marker { display: none; }
summary.chip:hover { box-shadow: inset 0 0 0 1px var(--control); }
.chip .when { color: inherit; font-size: inherit; }
.tools { display: flex; align-items: center; gap: 12px; margin-left: auto; }
.tools > * { flex: none; white-space: nowrap; }
.live { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: 13px; }
.live i { width: 7px; height: 7px; border-radius: 50%; background: var(--accent); }
.live[data-state="off"] i, .live[data-state="ended"] i { background: var(--bad-ink); }
.app { flex: 1; display: grid; grid-template-columns: 18.5rem minmax(0, 1fr); min-height: 0; padding: 0 8px 8px 0; }
.side { display: flex; flex-direction: column; gap: 20px; min-width: 0; padding: 4px 10px 20px 12px; }
.pane { position: relative; min-width: 0; background: var(--sheet); border: 1px solid var(--border); border-radius: 14px; }
.pane:focus { outline: none; }
.page { display: grid; align-content: start; gap: 28px; max-width: 50rem; margin: 0 auto; padding: 28px 32px 56px; }
.side-top { display: grid; gap: 2px; }
.btn.new { justify-content: space-between; width: 100%; height: 36px; margin-bottom: 8px; padding: 0 12px; font-size: 14px; }
.btn.new kbd { border-color: transparent; background: rgba(255, 255, 255, .18); color: inherit; }
.side .head { display: flex; align-items: baseline; gap: 8px; margin: 0 0 4px; padding: 0 8px; }
.side h2 { font-size: 12px; line-height: 18px; font-weight: 600; color: var(--muted); }
.side .count { font-size: 12px; }
.side .rows { display: grid; gap: 1px; border: 0; border-radius: 0; }
.side .row { display: block; padding: 0; border: 0; border-radius: 8px; }
.side .empty { padding: 2px 8px; border: 0; color: var(--muted); font-size: 13px; }
.item { display: grid; grid-template-columns: 8px minmax(0, 1fr); align-items: center; column-gap: 10px; padding: 7px 8px;
  border-radius: 8px; text-decoration: none; color: inherit; }
a.item:hover { background: var(--hover); }
a.item:focus { outline: none; }
a.item:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
a.item[aria-current="page"] { background: var(--sheet); box-shadow: 0 0 0 1px var(--border); }
.item > i { width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
.item .name, .item .sub { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.item .name { color: var(--ink); font-weight: 500; }
.item .sub { grid-column: 2; color: var(--muted); font-size: 13px; line-height: 18px; }
.g-needs .item .sub { color: var(--warn-ink); }
.item .tally { margin-left: 6px; font-size: 12px; font-weight: 600; color: var(--warn-ink); }
.home-item { grid-template-columns: minmax(0, 1fr) auto; }
.side-foot { margin-top: auto; padding: 0 8px; }
.side-foot a { color: var(--muted); font-size: 13px; }
.side-foot a:hover { color: var(--ink); }
section { min-width: 0; }
.page > section > .head, .s-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; margin-bottom: 12px; }
.page h2 { font-size: 15px; line-height: 20px; font-weight: 600; }
.page .s-needs h2, .page .job-name, .s-compose h2 { font-size: 22px; line-height: 28px; letter-spacing: -0.015em; }
.count { color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }
.s-needs .count.warn { align-self: center; min-width: 22px; padding: 0 7px; border-radius: 999px; text-align: center; font-weight: 600; line-height: 22px; }
.rows { border: 1px solid var(--border); border-radius: 12px; }
.row { position: relative; isolation: isolate; display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: start;
  column-gap: 16px; padding: 12px 16px; border-top: 1px solid var(--rule); }
.row:first-child { border-top: 0; border-radius: 12px 12px 0 0; }
.row:last-child { border-radius: 0 0 12px 12px; }
.row:only-child { border-radius: 12px; }
.row::before { content: ""; position: absolute; inset: 0; z-index: -1; border-radius: inherit; background: var(--flash, var(--active-bg)); opacity: 0; pointer-events: none; }
.row:focus-within, .row:has(details[open]) { z-index: 3; }
.cards { display: grid; gap: 10px; border: 0; border-radius: 0; }
.cards > .row { display: block; padding: 16px 18px; border: 1px solid var(--border); border-radius: 12px; background: var(--sheet); }
.moving { z-index: 5; background: var(--sheet); border-radius: 12px; box-shadow: 0 10px 28px var(--shade); }
.row:focus { outline: none; }
.cell { min-width: 0; }
.row > .acts { justify-content: flex-end; }
.need { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: start; gap: 10px 20px; }
.need + .need { margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--rule); }
.need > .acts { justify-content: flex-end; }
.ask { font-size: 16px; line-height: 22px; font-weight: 600; }
.say .detail { margin-top: 3px; white-space: pre-wrap; }
.say .when { display: block; margin-top: 4px; }
.say .cli { margin-top: 6px; color: var(--muted); }
.about { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; margin-top: 14px; padding-top: 12px;
  border-top: 1px solid var(--rule); color: var(--muted); font-size: 13px; }
.about > .acts { margin-left: auto; }
.about > .what::before, .about > .task::before { content: "·"; margin-right: 8px; }
.name { color: var(--ink); font-weight: 600; }
.state { display: inline-flex; align-items: center; gap: 6px; font-weight: 600; white-space: nowrap; }
.state i { flex: none; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.t-active { color: var(--active-ink); } .t-good { color: var(--good-ink); } .t-warn { color: var(--warn-ink); }
.t-bad { color: var(--bad-ink); } .t-neutral { color: var(--muted); }
.what { white-space: pre-wrap; }
.hold { margin-top: 8px; color: var(--warn-ink); }
.hold b { font-weight: 600; }
.soft, .when, .meta { color: var(--muted); font-size: 13px; }
.when, .count { font-variant-numeric: tabular-nums; }
.row[data-tone="good"] { --flash: var(--good-bg); } .row[data-tone="warn"] { --flash: var(--warn-bg); }
.row[data-tone="bad"] { --flash: var(--bad-bg); } .row[data-tone="neutral"] { --flash: var(--neutral-bg); }
.active { background: var(--active-bg); color: var(--active-ink); }
.good { background: var(--good-bg); color: var(--good-ink); }
.warn { background: var(--warn-bg); color: var(--warn-ink); }
.bad { background: var(--bad-bg); color: var(--bad-ink); }
.state.t-active i, .item > i.t-active, .beat { animation: pulse 1.8s ease-in-out infinite; }
.empty { display: grid; gap: 4px; padding: 14px 16px; border: 1px dashed var(--control); border-radius: 12px; }
.empty .soft { font-size: 14px; }
.s-needs .empty { padding: 18px 20px; border: 1px solid var(--border); background: var(--raised); }
.steps { display: grid; gap: 12px; margin-top: 10px; counter-reset: step; }
.steps li { display: grid; grid-template-columns: 24px minmax(0, 1fr); gap: 2px 10px; counter-increment: step; }
.steps li::before { content: counter(step); grid-row: span 2; width: 24px; height: 24px; border-radius: 50%; background: var(--sheet);
  border: 1px solid var(--control); text-align: center; font: 600 12px/22px system-ui, sans-serif; }
.steps b { font-weight: 600; }
.tallies { display: flex; flex-wrap: wrap; gap: 8px 28px; color: var(--muted); }
.tallies b { color: var(--ink); font-size: 20px; line-height: 28px; font-weight: 500; font-variant-numeric: tabular-nums; margin-right: 6px; }
.back { justify-self: start; color: var(--muted); font-size: 13px; text-decoration: none; }
.job-head { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: start; gap: 8px 20px; }
.facts { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; margin-top: 4px; color: var(--muted); font-size: 13px; }
.facts > * + *::before { content: "·"; margin-right: 8px; color: var(--muted); }
.ask-card { display: grid; gap: 12px; padding: 16px 18px; border-radius: 12px; background: var(--warn-bg); color: var(--ink); }
.ask-card .label { color: var(--warn-ink); font-size: 12px; font-weight: 600; }
.ask-card .soft, .ask-card .when { color: var(--muted); }
.ask-card .need { grid-template-columns: minmax(0, 1fr); }
.ask-card .need > .acts { display: grid; justify-content: stretch; gap: 10px; }
.ask-card .need > .acts > .act { justify-self: start; }
.answer { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: end; gap: 8px 10px; }
.answer label { display: grid; gap: 6px; font-size: 13px; font-weight: 500; }
.answer .note { grid-column: 1 / -1; color: var(--muted); font-size: 13px; }
.answer textarea { min-height: 5rem; }
.answer + .answer { margin-top: 4px; }
input[type="text"], textarea { width: 100%; padding: 7px 10px; border: 1px solid var(--control); border-radius: 8px;
  background: var(--sheet); color: var(--ink); font: 14px/20px system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
textarea { resize: vertical; }
textarea.mono, input.mono { font: 12px/18px ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
input[type="text"]:focus-visible, textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; border-color: var(--accent); }
.brief { max-height: 15rem; overflow: auto; padding: 14px 16px; border-radius: 12px; background: var(--raised); white-space: pre-wrap; }
.spec { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 6px 16px; margin-top: 12px; font-size: 13px; }
.spec dt { color: var(--muted); }
.timeline { position: relative; display: grid; gap: 0; border: 0; border-radius: 0; }
.timeline .row { display: grid; grid-template-columns: 12px minmax(0, 1fr) auto; gap: 12px; padding: 8px 0; border: 0; border-radius: 6px; }
.timeline .row > i { width: 9px; height: 9px; margin-top: 6px; border-radius: 50%; background: currentColor; box-shadow: 0 0 0 3px var(--sheet); }
.timeline::before { content: ""; position: absolute; left: 4px; top: 14px; bottom: 14px; width: 1px; background: var(--border); }
.timeline .when { white-space: nowrap; }
.log { max-height: 24rem; overflow: auto; padding: 12px 14px; border-radius: 10px; background: var(--raised); color: var(--ink);
  font-size: 12px; line-height: 18px; white-space: pre-wrap; }
.checks { display: grid; gap: 6px; margin-top: 10px; }
.checks li { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 10px; align-items: baseline; }
.result-line { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 10px; font-size: 15px; line-height: 22px; }
.compose { display: grid; gap: 16px; }
.compose label, .compose legend { display: grid; gap: 6px; font-size: 13px; font-weight: 600; }
.compose .opt { color: var(--muted); font-weight: 400; }
.compose small { color: var(--muted); font-weight: 400; }
.compose .split { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.4fr); gap: 16px; align-items: start; }
.where { display: grid; gap: 6px; }
.choice { display: grid !important; grid-template-columns: auto minmax(0, 1fr); gap: 2px 10px; align-items: start; padding: 10px 12px;
  border: 1px solid var(--control); border-radius: 10px; cursor: pointer; font-weight: 400 !important; }
.choice input { margin: 3px 0 0; accent-color: var(--accent); }
.choice b { font-weight: 600; }
.choice small { grid-column: 2; }
.choice:has(input:checked) { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
.compose .cmd { display: none; }
.compose:has(input[value="local-shell"]:checked) .cmd { display: grid; }
.compose details summary { width: max-content; color: var(--muted); font-size: 13px; cursor: pointer; }
.compose .opts { display: grid; gap: 12px; margin-top: 12px; }
.compose .needs { display: flex; flex-wrap: wrap; gap: 6px 14px; font-weight: 400; }
.compose .needs label { display: inline-flex; align-items: center; gap: 6px; font-weight: 400; }
.compose .send { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px 16px; }
.compose .send .note { color: var(--muted); font-size: 13px; }
.compose .btn.primary { height: 36px; padding: 0 16px; font-size: 14px; }
.acts { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.act { position: relative; margin: 0; }
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: 30px; padding: 0 11px;
  border: 1px solid var(--control); border-radius: 7px; background: var(--sheet); color: var(--ink); text-decoration: none;
  font: 500 13px/1 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; cursor: pointer; user-select: none;
  white-space: nowrap; transition: transform .08s ease-out; }
.need .btn, .answer .btn { height: 34px; padding: 0 13px; }
.btn:hover { background: var(--raised); }
.btn:active { transform: scale(.96); }
.btn:focus-visible, .x:focus-visible, .bar a:focus-visible, .side-foot a:focus-visible, .back:focus-visible,
summary.chip:focus-visible, .compose summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.btn.primary { background: var(--accent); border-color: var(--accent); color: var(--on-accent); font-weight: 600; }
.btn.primary:hover { box-shadow: inset 0 0 0 99px rgba(0, 0, 0, .12); }
.btn.danger { color: var(--bad-ink); }
.btn.danger:hover { background: var(--bad-bg); }
.btn.fill { background: var(--bad-ink); border-color: var(--bad-ink); color: var(--sheet); font-weight: 600; }
.btn[aria-busy="true"] { pointer-events: none; }
.btn[aria-busy="true"]::before { content: ""; width: 10px; height: 10px; border: 2px solid currentColor; border-right-color: transparent;
  border-radius: 50%; animation: spin .7s linear infinite; }
.btn.ok::before { content: "\\2713"; animation: pop .3s var(--ease); }
.btn.no { animation: shake .32s ease-in-out; }
summary.btn { list-style: none; }
summary.btn::-webkit-details-marker { display: none; }
.act[open] > summary.btn:not(.primary) { background: var(--raised); border-color: var(--ink); }
.act[open] > summary.btn.danger { border-color: var(--bad-ink); }
.pane[data-pane^="job:"] .btn[data-k]::after, .cards > .row:focus-within .btn[data-k]::after { content: attr(data-k); min-width: 16px; padding: 0 4px; border-radius: 4px;
  background: var(--neutral-bg); color: var(--neutral-ink); font: 600 11px/16px ui-monospace, "SF Mono", Menlo, monospace; }
.btn.primary[data-k]::after { background: rgba(255, 255, 255, .2); color: inherit; }
.pop { position: absolute; z-index: 20; top: calc(100% + 6px); right: 0; display: grid; gap: 10px; width: min(22rem, calc(100vw - 32px));
  padding: 12px; background: var(--sheet); border: 1px solid var(--border); border-radius: 10px; box-shadow: 0 10px 28px var(--shade);
  color: var(--ink); font-size: 14px; font-weight: 400; animation: drop .16s var(--ease); }
.pop.left { left: 0; right: auto; }
.pop form { display: grid; gap: 10px; }
.pop label { display: grid; gap: 6px; font-size: 13px; font-weight: 500; }
.pop .note { color: var(--muted); font-size: 13px; }
.pop textarea { font: 12px/18px ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
.pop .btn.primary, .pop .btn.fill { justify-self: end; }
.result { padding: 12px 14px; border: 1px solid var(--border); border-radius: 10px; background: var(--raised); }
.result p { font-weight: 600; }
.result.good p { color: var(--good-ink); } .result.bad p { color: var(--bad-ink); }
.result pre, .toast pre { margin-top: 6px; max-height: 10rem; overflow: auto; white-space: pre-wrap;
  font-size: 12px; line-height: 18px; color: var(--muted); }
.toasts { position: fixed; z-index: 30; right: 16px; bottom: 16px; display: grid; gap: 8px; width: min(26rem, calc(100vw - 32px)); }
.toast { position: relative; padding: 12px 40px 12px 14px; background: var(--sheet); border: 1px solid var(--border);
  border-radius: 12px; box-shadow: 0 12px 32px var(--shade); animation: rise .26s var(--ease); }
.toast p { display: flex; align-items: center; gap: 8px; }
.toast p i { width: 8px; height: 8px; border-radius: 50%; background: var(--good-ink); }
.toast.no p i { background: var(--bad-ink); }
.toast.leaving { opacity: 0; transform: translateY(6px); transition: opacity .2s, transform .2s; }
.x { position: absolute; top: 8px; right: 8px; width: 28px; height: 28px; border: 0; border-radius: 6px; background: none;
  color: var(--muted); font: 18px/1 system-ui, sans-serif; cursor: pointer; }
.x:hover { background: var(--raised); }
.ghost { z-index: 25; background: var(--sheet); pointer-events: none; }
.arrived { animation: arrive .36s var(--ease) both; }
.entering { animation: enter .26s var(--ease) both; }
.changed::before { animation: flash 1.4s ease-out; }
.changed .state, .bumped { animation: pop .34s var(--ease); }
.beating { animation: beat .6s var(--ease); }
.bar a { color: var(--muted); font-size: 13px; text-decoration: none; border-radius: 4px; }
.bar a:hover { color: var(--ink); }
dialog { width: min(30rem, calc(100vw - 32px)); max-height: calc(100vh - 32px); overflow: auto; padding: 20px;
  background: var(--sheet); color: var(--ink); border: 1px solid var(--border); border-radius: 14px; box-shadow: 0 20px 48px var(--shade); }
dialog[open] { animation: rise .2s var(--ease); }
dialog::backdrop { background: rgba(0, 0, 0, .3); }
dialog .title { margin-bottom: 12px; font-size: 15px; font-weight: 600; }
dialog .title:focus { outline: none; }
dialog dl { display: grid; grid-template-columns: auto 1fr; gap: 8px 14px; margin: 0 0 20px; }
dialog dd { color: var(--muted); }
dialog .words { grid-template-columns: 1fr; gap: 2px; }
dialog .words dd { margin-bottom: 8px; }
dialog .words dt { font-weight: 600; }
dialog form { display: flex; justify-content: flex-end; }
kbd { display: inline-block; min-width: 22px; padding: 0 6px; border: 1px solid var(--control); border-bottom-width: 2px;
  border-radius: 5px; background: var(--raised); color: var(--ink); text-align: center; font: 600 12px/18px ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
@keyframes pulse { 50% { opacity: .35; } }
@keyframes spin { to { transform: rotate(360deg); } }
@keyframes pop { from { opacity: 0; transform: scale(.8); } }
@keyframes shake { 25% { transform: translateX(-4px); } 75% { transform: translateX(4px); } }
@keyframes drop { from { opacity: 0; transform: translateY(-4px); } }
@keyframes rise { from { opacity: 0; transform: translateY(8px); } }
@keyframes arrive { from { opacity: 0; transform: translateY(-8px); } }
@keyframes enter { from { opacity: 0; transform: translateX(10px); } }
@keyframes flash { 15% { opacity: 1; } }
@keyframes beat { 40% { transform: scale(1.8); opacity: .5; } }
@keyframes fade { from { opacity: 0; } }
@media (prefers-reduced-motion: reduce) {
  .state.t-active i, .item > i.t-active, .beat, .changed .state, .bumped, .beating, .btn.no { animation: none; }
  .arrived, .entering, .pop, .toast, dialog[open] { animation: fade .2s ease-out; }
  .btn.ok::before { animation: none; }
  .btn[aria-busy="true"]::before { animation: none; border-right-color: currentColor; opacity: .5; }
  .btn:active, .toast.leaving { transform: none; }
}
@media (min-width: 45rem) {
  body { height: 100vh; overflow: hidden; }
  .side, .pane { overflow: auto; overscroll-behavior: contain; }
  .back { display: none; }
}
@media (max-width: 44.99rem) {
  .app { display: flex; flex-direction: column; padding: 0; }
  .pane { order: -1; border-width: 1px 0; border-radius: 0; }
  .page { gap: 24px; padding: 20px 16px 28px; }
  .side { padding: 24px 8px 32px; }
  body[data-open^="job:"] .side, body[data-open="new"] .side, body[data-open="home"] .side .g-needs { display: none; }
  .tools a, [data-keys] { display: none; }
  .health { order: 3; flex: 1 1 100%; }
  .need, .job-head, .answer, .compose .split { grid-template-columns: minmax(0, 1fr); }
  .need > .acts { justify-content: flex-start; }
  .need .act[open], .health .act[open], .job-head .act[open] { flex: 1 1 100%; }
  .need .pop, .health .pop, .job-head .pop { position: static; width: auto; margin-top: 8px; box-shadow: none; }
  .cards > .row { padding: 14px; }
}
@media (min-width: 50rem) {
  dialog { width: min(54rem, calc(100vw - 32px)); }
  dialog .pair { display: grid; grid-template-columns: 1fr 1.4fr; gap: 32px; }
}
"""

# The client for the live page, served same-origin at /app.js. It polls the page it shows, swaps only what changed
# (FLIP for rows that move between groups), follows app links without reloading, posts forms with fetch and reports
# the CLI's answer, and adds the keyboard.
SCRIPT = r"""(() => {
'use strict';
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const calm = matchMedia('(prefers-reduced-motion: reduce)');
const wait = ms => new Promise(done => setTimeout(done, ms));
const byKey = (k, r = document) => r.querySelector('[data-key="' + CSS.escape(k) + '"]');
let skew = 0, inflight = 0, failures = 0, timer = 0, stash = null, ended = false, moving = 0;

const span = s => s < 60 ? s + ' s' : s < 3600 ? Math.floor(s / 60) + ' min'
  : s < 86400 ? Math.floor(s / 3600) + ' h' : Math.floor(s / 86400) + ' d';
const rel = s => s >= 0 ? span(s) + ' ago' : 'in ' + span(-s);
function clock() {
  for (const el of $$('[data-t]')) {
    const s = Math.round(Date.now() / 1000 + skew - +el.dataset.t);
    const text = s >= 0 && el.dataset.due ? el.dataset.due : (el.dataset.pre || '') + rel(s);
    if (el.textContent !== text) el.textContent = text;
  }
}
function play(el, cls, ms) {
  if (!el) return;
  el.classList.remove(cls); void el.offsetWidth; el.classList.add(cls);
  setTimeout(() => el.classList.remove(cls), ms || 1400);
}
const parse = html => new DOMParser().parseFromString(html, 'text/html');
const busy = el => !!(el.querySelector('details[open], [aria-busy="true"]')
  || (el.contains(document.activeElement) && document.activeElement.matches('input, textarea')));
const regions = el => $$('[data-region]', el).map(r => r.dataset.region).join(' ');

function ghost(el, rect) {
  if (calm.matches || !rect) return;
  const g = el.cloneNode(true);
  g.removeAttribute('data-key'); g.removeAttribute('tabindex'); g.classList.add('ghost');
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
    const was = $('.count', oh), now = $('.count', fh);
    oh.replaceWith(fh);
    if (now && (!was || was.textContent !== now.textContent)) play(now, 'bumped', 400);
  }
  const ob = $('.body', old), fb = $('.body', fresh);
  if (!ob || !fb) return true;
  const ol = $('.rows', ob), fl = $('.rows', fb);
  if (!ol || !fl) {
    if (busy(ob)) return false;
    for (const el of $$('[data-key]', ob)) if (!byKey(el.dataset.key, doc)) ghost(el, before.get(el.dataset.key));
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
  for (const gone of keep.values()) {
    if (busy(gone) && byKey(gone.dataset.key, doc)) { next.push(gone); whole = false; }
    else if (!byKey(gone.dataset.key, doc)) ghost(gone, before.get(gone.dataset.key));
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
  // The open job's sidebar entry is marked here, not by the server, so a row's signature never depends on the page.
  for (const a of $$('.side a.item')) {
    const here = new URL(a.href, location.href).pathname === location.pathname;
    if (here) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  for (const log of $$('.log')) if (!log.dataset.held) log.scrollTop = log.scrollHeight;
}

function apply(doc, nav) {
  const now = +doc.body.dataset.now;
  if (now) skew = now - Date.now() / 1000;
  document.body.dataset.open = doc.body.dataset.open || '';
  const before = new Map($$('[data-key]').map(el => [el.dataset.key, el.getBoundingClientRect()]));
  const focused = document.activeElement;
  const focusKey = focused && focused.closest && focused.closest('[data-key]') ? focused.closest('[data-key]').dataset.key : null;
  const added = [], changed = [];
  const pane = $('.pane'), fresh = $('.pane', doc);
  // A different page, or a page whose sections changed (a job finished and grew a result), is swapped whole, unless
  // someone is typing in it; then only the parts that still match are updated.
  let swap = !!(pane && fresh) && (nav || pane.dataset.pane !== fresh.dataset.pane || regions(pane) !== regions(fresh));
  if (swap && !nav && busy(pane)) swap = false;
  for (const el of $$('[data-region]', doc)) {
    if (swap && fresh.contains(el)) continue;
    const old = $('[data-region="' + el.dataset.region + '"]');
    if (!old || old.dataset.sig === el.dataset.sig) continue;
    if (!$('.body', el)) { if (!busy(old)) { old.replaceWith(el); if (!nav) play(el, 'arrived', 400); } continue; }
    if (sync(old, el, doc, before, added, changed)) old.dataset.sig = el.dataset.sig;
  }
  if (swap) {
    const top = nav ? 0 : pane.scrollTop;
    const had = new Set(regions(pane).split(' '));
    pane.replaceWith(fresh);
    fresh.scrollTop = top;
    if (nav) { play($('.page', fresh), 'entering', 300); window.scrollTo(0, 0); }
    else for (const el of $$('[data-region]', fresh)) if (!had.has(el.dataset.region)) play(el, 'arrived', 400);
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
  if (!nav) {
    if (focused && focused !== document.body && !focused.isConnected && focusKey) {
      const again = byKey(focusKey);
      if (again) (again.querySelector('a.item') || again).focus({ preventScroll: true });
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
  $('span', el).textContent = state === 'on' ? 'Live' : state === 'off' ? 'Reconnecting' : 'Session ended';
  if (state === 'ended') { ended = true; clearTimeout(timer); }
}

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
      else if (!moving && location.href === here) { const res = apply(doc, false); if (res) report('Last action', res, null); }
      failures = 0; live('on');
    } catch (err) { failures += 1; live('off'); }
  }
  timer = setTimeout(poll, failures ? Math.min(30000, 4000 * 2 ** failures) : 4000);
}

async function go(url, push) {
  moving += 1;
  try {
    const r = await fetch(url, { cache: 'no-store' });
    if (r.status === 403) { live('ended'); return; }
    const doc = parse(await r.text());
    if (push) history.pushState(null, '', r.url || url);
    const res = apply(doc, true);
    if (res) report('Last action', res, null);
    // A new page takes focus where its work starts: the brief on New job, else the pane, so j/k and the action
    // keys carry on from there.
    const brief = $('.pane textarea[name="brief"]');
    if (document.body.dataset.open === 'new' && brief) brief.focus();
    else { const pane = $('.pane'); if (pane) pane.focus({ preventScroll: true }); }
  } catch (err) { location.href = url; }
  finally { moving -= 1; }
}

function report(label, res, fallback) {
  const raw = res ? res.dataset.code : fallback;
  const code = raw === '' || raw === null || raw === undefined ? null : +raw;
  const pre = res && $('pre', res);
  toast(label, code === null || code === 0, code, pre ? pre.textContent : '');
  return code === null || code === 0;
}

function toast(label, ok, code, out) {
  const box = $('.toasts');
  if (!box) return;
  const t = document.createElement('div');
  t.className = 'toast ' + (ok ? 'ok' : 'no');
  if (!ok) t.setAttribute('role', 'alert');
  const head = document.createElement('p');
  const b = document.createElement('b');
  b.textContent = label;
  head.append(document.createElement('i'), b, (ok ? 'done' : 'failed') + (code === null ? '' : ' · exit ' + code));
  const x = document.createElement('button');
  x.type = 'button'; x.className = 'x'; x.textContent = '×';
  x.setAttribute('aria-label', 'Dismiss');
  x.addEventListener('click', () => drop(t));
  t.append(head, x);
  if (out.trim()) { const pre = document.createElement('pre'); pre.textContent = out.trim(); t.append(pre); }
  box.append(t);
  while (box.children.length > 3) box.firstElementChild.remove();
  if (ok) setTimeout(() => drop(t), 6000);
}
function drop(t) {
  if (!t.isConnected || t.classList.contains('leaving')) return;
  t.classList.add('leaving');
  setTimeout(() => t.remove(), calm.matches ? 0 : 200);
}

document.addEventListener('submit', async e => {
  const form = e.target;
  if (!(form instanceof HTMLFormElement) || !form.hasAttribute('data-act')) return;
  e.preventDefault();
  const btn = e.submitter || $('button', form);
  if (!btn || btn.getAttribute('aria-busy') === 'true') return;
  const label = form.dataset.label || btn.textContent.trim();
  const row = form.closest('[data-key]');
  btn.setAttribute('aria-busy', 'true');
  inflight += 1; stash = null;
  let doc = null, code = null, out = '', reached = true, landed = null;
  try {
    const r = await fetch(form.action, { method: 'POST', body: new URLSearchParams(new FormData(form)) });
    if (r.status === 403) live('ended');
    doc = parse(await r.text());
    if (r.redirected && new URL(r.url).pathname !== location.pathname) landed = r.url;
    const res = doc.getElementById('result') || stash;
    if (res) {
      code = res.dataset.code === '' ? null : +res.dataset.code;
      out = $('pre', res) ? $('pre', res).textContent : '';
    } else if (!r.ok) code = r.status;
  } catch (err) { reached = false; out = 'The harness did not answer. Is rh serve still running?'; }
  btn.removeAttribute('aria-busy');
  const ok = reached && (code === null || code === 0);
  toast(label, ok, code, out);
  play(btn, ok ? 'ok' : 'no', ok ? 900 : 400);
  if (ok) { const d = form.closest('details'); if (d) d.open = false; if (!landed) form.reset(); }
  await wait(ok ? 650 : 250);
  inflight -= 1;
  if (doc && doc.body && doc.body.dataset.now) {
    if (landed) { history.pushState(null, '', landed); apply(doc, true); }
    else apply(doc, false);
  }
  if (ok && row && !landed) { const again = byKey(row.dataset.key); if (again) (again.querySelector('a.item') || again).focus({ preventScroll: true }); }
  if (!document.activeElement || document.activeElement === document.body) { const p = $('.pane'); if (p) p.focus({ preventScroll: true }); }
});

document.addEventListener('click', e => {
  for (const d of $$('details.act[open]')) if (!d.contains(e.target)) d.open = false;
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

// The composer suggests a name from the first words of the brief until the name is typed, and asks for a command
// only for a job that runs on this machine.
document.addEventListener('input', e => {
  const t = e.target;
  if (!(t instanceof HTMLElement)) return;
  const form = t.closest('form.compose');
  if (!form) return;
  const id = form.elements.namedItem('id');
  if (t === id) { id.dataset.typed = id.value ? '1' : ''; return; }
  if (t.name === 'brief' && id && !id.dataset.typed) {
    const words = t.value.toLowerCase().replace(/[^a-z0-9\s-]/g, ' ').split(/\s+/).filter(w => w.length > 2).slice(0, 4);
    id.value = words.join('-').slice(0, 48);
  }
});
document.addEventListener('change', e => {
  const form = e.target.closest && e.target.closest('form.compose');
  if (!form) return;
  const cmd = form.elements.namedItem('command');
  if (cmd) cmd.required = form.elements.namedItem('kind').value === 'local-shell';
});

const items = () => $$('.side a.item').filter(el => el.getClientRects().length);
function move(step) {
  const list = items();
  if (!list.length) return;
  const cur = document.activeElement && document.activeElement.closest('a.item');
  let i = list.indexOf(cur);
  if (i < 0) i = list.findIndex(a => a.getAttribute('aria-current') === 'page');
  i = i < 0 ? (step >= 0 ? 0 : list.length - 1) : Math.max(0, Math.min(list.length - 1, i + step));
  list[i].focus({ preventScroll: true });
  list[i].scrollIntoView({ block: 'nearest', behavior: calm.matches ? 'auto' : 'smooth' });
}
function closeOthers(keep) { for (const d of $$('details.act[open]')) if (d !== keep) d.open = false; }
function activate(c) {
  const d = c.closest('details');
  if (d && c.tagName === 'SUMMARY') {
    closeOthers(d); d.open = true;
    const f = $('input:not([type="hidden"]), textarea', d);
    (f || c).focus();
  } else c.focus();
}
document.addEventListener('toggle', e => {
  const d = e.target;
  if (!(d instanceof HTMLDetailsElement) || !d.open || !d.matches('.act')) return;
  closeOthers(d);
  const f = $('input:not([type="hidden"]), textarea', d);
  if (f && !d.contains(document.activeElement)) f.focus();
}, true);
document.addEventListener('keydown', e => {
  if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || e.isComposing) return;
  const t = e.target;
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
  const card = t.closest && t.closest('.cards > .row');
  const act = k => (card && card.querySelector('[data-k="' + k + '"]')) || $('.pane [data-k="' + k + '"]');
  if (e.key === 'j' || e.key === 'ArrowDown') move(1);
  else if (e.key === 'k' || e.key === 'ArrowUp') move(-1);
  else if (e.key === '?' && keys) keys.showModal();
  else if (e.key === 'i' && $('.g-needs a.item')) go($('.g-needs a.item').href, true);
  else if (e.key === 'n' && $('[data-k="n"]')) { const f = $('.pane textarea[name="brief"]'); if (f) f.focus(); else go($('[data-k="n"]').href, true); }
  else if (e.key === 't' && $('[data-k="t"]')) $('[data-k="t"]').focus();
  else if (/^[rahusclx]$/.test(e.key) && act(e.key)) activate(act(e.key));
  else return;
  e.preventDefault();
});
document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });

const keysButton = $('[data-keys]');
if (keysButton) { keysButton.hidden = false; keysButton.addEventListener('click', () => $('#keys').showModal()); }
const first = $('#result');
if (first) { report('Last action', first, null); first.remove(); }
skew = (+document.body.dataset.now || Date.now() / 1000) - Date.now() / 1000;
clock();
mark();
setInterval(clock, 1000);
live('on');
timer = setTimeout(poll, 4000);
})();
"""

# Plain words for each job state and the tone of its dot. An unknown state is shown as the harness names it.
_STATES = {
    "draft": ("Draft", "neutral"), "ready": ("Ready to launch", "neutral"), "waiting": ("Waiting", "neutral"),
    "held": ("On hold", "warn"), "launching": ("Launching", "active"), "handoff": ("Needs a hand launch", "warn"),
    "running": ("Running", "active"), "stalled": ("No progress", "warn"), "collecting": ("Collecting the result", "active"),
    "done": ("Done", "good"), "failed": ("Failed", "bad"), "blocked": ("Blocked", "warn"),
    "capacity": ("Out of capacity", "warn"), "truncated": ("Cut short", "bad"), "aborted": ("Aborted", "bad"),
    "no_result": ("No result", "bad"), "cancelled": ("Cancelled", "neutral"),
}
_PRE = ("draft", "ready", "waiting", "held", "handoff")  # states rh job cancel accepts
# What happens next to a job in a state that is waiting on the harness, so a quiet card still says what it is doing.
_NEXT = {
    "draft": "The next check reviews it.",
    "ready": "Starts on the next check unless its platform is busy or down.",
    "waiting": "Starts by itself once what it waits for is done.",
    "held": "Rechecked on every pass. Fix what it needs, or cancel it.",
    "launching": "Starting on its platform.",
    "stalled": "It has run past its time limit; the harness keeps checking it.",
    "collecting": "Fetching and checking its result.",
}
# Platforms (routes and job kinds in the CLI) as people name them; anything else is shown as the harness names it.
_PLATFORMS = {"codex-cloud": "Codex cloud", "local-shell": "This machine"}
_NEEDS = ("net", "python", "git", "loopback", "browser", "gpu")  # capabilities the composer offers
_STALE = 300  # seconds without a check before the header says the harness may have stopped
# serve's last result is "<p>Exit code: N</p><pre>escaped output</pre>"; anything else is shown as plain text.
_RESULT = re.compile(r"<p>Exit code: (-?\d+)</p><pre>(.*)</pre>", re.S)
_REASON = '<label>Why?<input type="text" name="reason" required autocomplete="off"></label>'
_TASK = '<label>Task id<input type="text" name="task_id" required autocomplete="off" spellcheck="false" class="mono"></label>'
_PATCH = ('<label>Saved patch or bundle reply<textarea name="text" rows="8" required spellcheck="false" class="mono">'
          '</textarea></label>')
_KEYS = (("j / ↓", "Next job"), ("k / ↑", "Previous job"), ("Enter", "Open it"), ("n", "New job"),
         ("i", "Open the first job that needs you"), ("r", "Answer, mark done or dismiss"), ("a", "Attach a cloud task"),
         ("h", "Paste a patch"), ("u", "Resume"), ("s", "Pause"), ("c", "Cancel a job that has not started"),
         ("l", "Save a result to a branch"), ("t", "Check now"), ("Esc", "Close or leave a field"), ("?", "This sheet"))
# The app's words, each with the name the CLI and the older pages use for it.
_WORDS = (("Harness", "The supervisor process: it launches ready jobs, watches running ones and collects their "
                      "results (Supervisor on older pages)."),
          ("Check now", "Runs one harness pass at once (rh supervise --once, once called Run tick)."),
          ("Platform", "Where jobs run: Codex cloud, or this machine (a route in the CLI). A red chip means the "
                       "harness is backing off and will retry by itself."),
          ("Pause", "Nothing new starts for the job or platform, and a running job is not timed out. Work already "
                    "running continues (rh stop --pause; the old Stops list)."),
          ("On hold", "The harness parked the job until something is fixed and rechecks it on every pass (held)."),
          ("Paste patch", "Saves the diff copied from the Codex web view into the job (rh cloud harvest)."),
          ("Save to branch", "Commits a done cloud job's result to the local branch cloud/<job>. Nothing is pushed "
                             "(rh land, once called Land)."))


def _text(value):
    return escape(str(value), quote=True)


def _sig(*fields):
    return format(zlib.crc32(repr(fields).encode()), "08x")


def _still(html):
    """The markup without its ticking times, so a signature changes only when what the markup says changes."""
    return re.sub(r'<span class="when"[^>]*>[^<]*</span>', "", html)


def _span(seconds):
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


def _relative(timestamp, now):
    seconds = round(now - timestamp)
    return f"{_span(seconds)} ago" if seconds >= 0 else f"in {_span(-seconds)}"


def _when(timestamp, now, key, pre="", due=None):
    """A relative time the live page keeps ticking; data-tk lets it update a row that did not otherwise change.
    `due` replaces a future time once it has passed ("retrying on the next check" rather than "8 s ago")."""
    marks = (f' data-pre="{_text(pre)}"' if pre else "") + (f' data-due="{_text(due)}"' if due else "")
    text = due if due and now >= timestamp else pre + _relative(timestamp, now)
    return f'<span class="when" data-t="{_text(timestamp)}" data-tk="{_text(key)}"{marks}>{_text(text)}</span>'


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _state(state):
    word, tone = _STATES.get(state, (state, "neutral"))
    return f'<span class="state t-{tone}"><i></i>{_text(word)}</span>', tone


def _platform(route):
    name, _, env = str(route).partition(":")
    return f"Cloud environment {env}" if name == "cli-env" and env else _PLATFORMS.get(str(route), str(route))


def _sentence(text):
    text = str(text).strip()
    return text[:1].upper() + text[1:]


def _clause(text):
    return str(text).strip().rstrip(".")


def _hidden(ctx, fields):
    back = [("next", ctx["back"])] if ctx.get("back") else []
    return "".join(f'<input type="hidden" name="{name}" value="{_text(value)}">'
                   for name, value in (("t", ctx["token"]), *fields, *back))


def _cls(kind):
    return f"btn {kind}" if kind else "btn"


def _tip(cli):
    return f' title="{_text(cli)}"' if cli else ""


def _url(ctx, path):
    return _text(f"{path}?t={ctx['token']}")


def _button(ctx, route, fields, label, key, kind="", cli=""):
    """A one-step action; the title names the CLI command it runs."""
    return (f'<form class="act" method="post" action="{route}" data-act data-label="{label}">'
            f'{_hidden(ctx, fields)}<button class="{_cls(kind)}" data-k="{key}"{_tip(cli)}>{label}</button></form>')


def _popover(ctx, route, fields, label, key, inputs, submit, kind="", cli="", note="", left=False):
    """An action that opens its form in place: the inputs, what submitting does, and the button that confirms."""
    confirm = "btn danger fill" if kind == "danger" else "btn primary"
    note = f'<p class="note">{note}</p>' if note else ""
    return (f'<details class="act"><summary class="{_cls(kind)}" data-k="{key}"{_tip(cli)}>{label}…</summary>'
            f'<form class="pop{" left" if left else ""}" method="post" action="{route}" data-act data-label="{submit}">'
            f'{_hidden(ctx, fields)}{inputs}{note}<button class="{confirm}">{submit}</button></form></details>')


def _inline(ctx, route, fields, inputs, submit, kind="", cli="", note=""):
    """An answer form written out in the open job's pane, where the space is there to type in."""
    note = f'<p class="note">{note}</p>' if note else ""
    return (f'<form class="answer" method="post" action="{route}" data-act data-label="{submit}">'
            f'{_hidden(ctx, fields)}{inputs}<button class="{_cls(kind)}"{_tip(cli)}>{submit}</button>{note}</form>')


def _note(label, key="r", rows=0):
    field = (f'<textarea name="note" rows="{rows}" required></textarea>' if rows
             else '<input type="text" name="note" required autocomplete="off">')
    return f'<label>{label}{field.replace(" name=", f" data-k={chr(34)}{key}{chr(34)} name=", 1) if key else field}</label>'


def _acts(items):
    return f'<span class="acts">{"".join(items)}</span>' if items else ""


def _row(key, live, inner, state=None, tone=None, acts=(), card=False):
    marks = f' data-state="{_text(state)}" data-tone="{tone}"' if state is not None else ""
    focus = ' tabindex="-1"' if live else ""
    body = f'<div class="cell">{inner}</div>{_acts(acts)}'
    return (f'<li class="row{" card" if card else ""}" data-key="{_text(key)}" data-sig="{_sig(_still(body))}"'
            f'{marks}{focus}>{body}</li>')


def _need(ctx, ask, detail, when, acts, cli):
    """One thing a person can settle, written as an instruction, beside the buttons that settle it. The snapshot
    has no buttons, so it prints the command instead."""
    say = (f'<p class="ask">{ask}</p>' + (f'<p class="detail">{detail}</p>' if detail else "") + when
           + ("" if ctx["live"] else f'<p class="cli"><code>{_text(cli)}</code></p>'))
    return f'<div class="need"><div class="say">{say}</div>{_acts(acts if ctx["live"] else ())}</div>'


def _ask_text(item):
    """(instruction html, detail text, cli) for one inbox item."""
    job, kind = item.get("job"), item.get("kind")
    action, detail = str(item.get("action") or ""), str(item.get("detail") or "")
    if kind == "web-launch" and job:
        return (f"Launch {_text(job)} in the Codex web composer, then attach its task id", detail,
                f"rh cloud attach {job} <task id>")
    if kind == "harvest" and job:
        return (f"Paste the patch for {_text(job)} from the Codex web view",
                " ".join(part for part in (_sentence(action), detail) if part), f"rh cloud harvest {job} <saved file>")
    return _text(_sentence(action or kind)), detail, f"rh inbox resolve {item['id']} <note>"


def _ask_acts(item, ctx, lead):
    """The controls that settle an inbox item: popovers on a card, open forms in the job's pane."""
    iid, job, kind = item["id"], item.get("job"), item.get("kind")
    first = "primary" if lead else ""
    resolve = f"rh inbox resolve {iid} <note>"
    label, field = {"decision": ("Record decision", "Your decision"),
                    "operator-step": ("Mark done", "What did you do?")}.get(kind, ("Mark handled", "Note"))
    if kind == "web-launch" and job:
        main = ("/attach", [("job", job)], "Attach task", "a", _TASK, "Links the cloud task to the job and closes this "
                "item.", f"rh cloud attach {job} <task id>")
    elif kind == "harvest" and job:
        main = ("/harvest", [("job", job)], "Paste patch", "h", _PATCH, "Saves the patch into the job and closes this "
                "item.", f"rh cloud harvest {job} <saved file>")
    else:
        main = ("/inbox/resolve", [("id", iid)], label, "r", None, "Closes this item and keeps your note with it.", resolve)
    route, fields, name, key, inputs, note, cli = main
    extra = route != "/inbox/resolve"
    if ctx["mode"] == "inline":
        inputs = inputs.replace(' name=', f' data-k="{key}" name=', 1) if inputs else _note(field, key, 3)
        out = [_inline(ctx, route, fields, inputs, name, first, cli, note)]
        if extra:
            out.append(_popover(ctx, "/inbox/resolve", [("id", iid)], "Dismiss", "x", _note("Why dismiss it?", ""),
                                "Dismiss", cli=resolve, note="Closes this item without doing it."))
        return out
    out = [_popover(ctx, route, fields, name, key, inputs or _note(field, ""), name, first, cli, note)]
    if extra:
        out.append(_popover(ctx, "/inbox/resolve", [("id", iid)], "Dismiss", "x", _note("Why dismiss it?", ""),
                            "Dismiss", cli=resolve, note="Closes this item without doing it."))
    return out


def _ask(item, ctx, lead):
    """An inbox item as one plain instruction; the first need on a card gets the filled button."""
    ask, detail, cli = _ask_text(item)
    when = (_when(item["created"], ctx["now"], f"inbox:{item['id']}", "asked ")
            if _number(item.get("created")) else "")
    return _need(ctx, ask, _text(detail), when, _ask_acts(item, ctx, lead), cli)


def _resume(stop, ctx, lead, noun, what, fields, scope):
    """A pause this app placed: only the app's own stops can be lifted from here (rh resume --owner app)."""
    pause = stop.get("kind") == "pause"
    verb = "Paused" if pause else "Stopped"
    fields = fields + ([("pause", "on")] if pause else [])
    cli = f"rh resume --owner app{' --pause' if pause else ''}{scope}"
    when = (_when(stop["created"], ctx["now"], f"stop:{stop.get('id', stop['scope'])}", verb.lower() + " ")
            if _number(stop.get("created")) else "")
    act = _button(ctx, "/resume", fields, f"Resume {noun}", "u", "primary" if lead else "", cli)
    return _need(ctx, f"{verb} from the app: {_text(_clause(stop['reason']))}",
                 f"Nothing new starts for {what} until you resume it.", when, [act], cli)


def _hold(stop, running):
    """A pause someone else owns: shown where it applies, with who can undo it, but no button."""
    verb, owner = "Paused" if stop.get("kind") == "pause" else "Stopped", _text(stop["owner"])
    tail = "It keeps running; " if running else "Nothing new starts for it; "
    return (f'<p class="hold"><b>{verb} by {owner}:</b> {_text(_clause(stop["reason"]))}. '
            f'<span class="soft">{tail}only {owner} can undo it.</span></p>')


def _pause(ctx, jid, left=False):
    return _popover(ctx, "/stop", [("job", jid), ("pause", "on")], "Pause", "s", _REASON, "Pause job", "danger",
                    f"rh stop --owner app --pause --job {jid} --reason <why>",
                    "Nothing new starts for it until you resume it here. Work already running continues.", left)


def _cancel(ctx, jid):
    return _popover(ctx, "/job/cancel", [("job", jid)], "Cancel job", "c", _REASON, "Cancel job", "danger",
                    f"rh job cancel {jid} --reason <why>", "The job will not start. It moves to Done as cancelled.")


def _land(ctx, jid, kind=""):
    return _button(ctx, "/land", [("job", jid)], "Save to branch", "l", kind, f"rh land {jid}")


def _local(row):
    return str(row.get("external_id") or "-").isdigit()  # a bare pid is a local job


class _Job:
    """Everything the app says about one job, worked out once: which group it belongs in, what it needs from a
    person, the pauses on it, and the actions it offers."""

    def __init__(self, row, where, ctx):
        self.row, self.where, self.ctx = row, where, ctx
        self.id, self.state = row["id"], row["state"]
        self.status, self.tone = _state(self.state)
        self.stops = ctx["scopes"].get(f"job:{self.id}", [])
        self.items = ctx["asks"].get(self.id, [])
        self.handoff = self.state == "handoff" and not any(i.get("kind") == "web-launch" for i in self.items)
        self.mine = [s for s in self.stops if s.get("owner") == "app"]
        self.count = len(self.items) + self.handoff + len(self.mine)
        self.group = "needs" if self.count else {"live": "running", "not_started": "queued", "recent": "done"}[where]
        times = [i["created"] for i in self.items if _number(i.get("created"))]
        times += [s["created"] for s in self.mine if _number(s.get("created"))]
        self.first = min(times, default=float("inf"))

    def headline(self):
        """The one line the sidebar shows under the name: the question when there is one, else the state."""
        if self.items:
            return re.sub(r"<[^>]+>", "", _ask_text(self.items[0])[0])
        if self.handoff:
            return "Launch it in the Codex web composer"
        if self.mine:
            return "Paused from the app"
        parts = [_STATES.get(self.state, (self.state,))[0]]
        held = [s for s in self.stops if s.get("owner") != "app"]
        if held:
            parts.insert(0, f"{'Paused' if held[0].get('kind') == 'pause' else 'Stopped'} by {held[0]['owner']}")
        elif self.row.get("paused"):
            parts.insert(0, "Paused")
        if self.row.get("detail"):
            parts.append(self.row["detail"])
        return " · ".join(map(str, parts))

    def needs(self, ctx):
        out = [_ask(item, ctx, i == 0) for i, item in enumerate(self.items)]
        if self.handoff:
            cli = f"rh cloud attach {self.id} <task id>"
            first = "" if out else "primary"
            act = (_inline(ctx, "/attach", [("job", self.id)], _TASK.replace(' name=', ' data-k="a" name=', 1),
                           "Attach task", first, cli, "Links the cloud task to the job.") if ctx["mode"] == "inline"
                   else _popover(ctx, "/attach", [("job", self.id)], "Attach task", "a", _TASK, "Attach task", first,
                                 cli, "Links the cloud task to the job."))
            out.append(_need(ctx, f"Launch {_text(self.id)} in the Codex web composer, then attach its task id",
                             "", "", [act], cli))
        for stop in self.mine:
            out.append(_resume(stop, ctx, not out, "job", "this job", [("job", self.id)], f" --job {self.id}"))
        return out

    def holds(self):
        out = [_hold(s, self.where == "live") for s in self.stops if s.get("owner") != "app"]
        if self.row.get("paused") and not any(s.get("kind") == "pause" for s in self.stops):
            out.append('<p class="hold"><b>Paused:</b> its platform or all launches are paused.</p>')
        return out

    def actions(self, ctx, left=False):
        """The job's own actions, outlined: the filled button always belongs to what it needs from a person."""
        if not ctx["live"]:
            return []
        acts = []
        if self.where != "recent" and not self.mine:
            acts.append(_pause(ctx, self.id, left))
        if self.state in _PRE and self.where != "recent":
            acts.append(_cancel(ctx, self.id))
        if self.state == "done" and not _local(self.row):
            acts.append(_land(ctx, self.id, "" if self.count or ctx["mode"] != "inline" else "primary"))
        return acts

    def item(self):
        """The sidebar entry: a link to the job's pane."""
        ctx = self.ctx
        inner = (f'<i class="t-{self.tone}"></i><span class="name">{_text(self.id)}</span>'
                 f'<span class="sub">{_text(self.headline())}</span>')
        link = (f'<a class="item" href="{_url(ctx, "/job/" + self.id)}" data-nav>{inner}</a>' if ctx["live"]
                else f'<div class="item">{inner}</div>')
        return (f'<li class="row" data-key="job:{_text(self.id)}" data-sig="{_sig(_still(link))}" '
                f'data-tone="{self.tone}">{link}</li>')

    def card(self):
        """The overview's card: what the job needs, then which job it is."""
        ctx = self.ctx
        task = _task(self.row)
        what = f' <span class="what">{_text(self.row["detail"])}</span>' if self.row.get("detail") else ""
        tag = f'<span class="task">{task}</span>' if task else ""
        link = f'<a class="btn" href="{_url(ctx, "/job/" + self.id)}" data-nav>Open job</a>' if ctx["live"] else ""
        about = (f'<div class="about"><span class="name">{_text(self.id)}</span>{self.status}{what}{tag}'
                 f'{_acts([link] if link else [])}</div>')
        return _row(f"card:{self.id}", ctx["live"], "".join(self.needs(ctx)) + about + "".join(self.holds()),
                    self.state, self.tone, card=True)


def _task(row):
    ext = row.get("external_id")
    return f"Cloud task {_text(ext)}" if ext is not None and not str(ext).isdigit() else ""


def _health(view, ctx):
    """The header's one line: is the harness checking, when did it last check, and is any platform down."""
    now, live = ctx["now"], ctx["live"]
    sup, chips = view.get("supervisor"), []
    if sup:
        age = sup["age_s"]
        counted = isinstance(age, int) and not isinstance(age, bool)
        ago = _when(now - age, now, "tick", "checked ") if counted else f"checked {_text(age)} s ago"
        quiet = counted and age > _STALE
        head, tone = ("Harness quiet", "warn") if quiet else ("Harness running", "")
        more = (f"Process {_text(sup['pid'])}. It checks on a timer: it launches ready jobs, watches "
                "running ones and collects results." + (" It has not checked for a while and may have stopped; "
                                                        "rh supervise starts it." if quiet else ""))
        line = f"{head} · {ago}"
    else:
        line, tone = "Harness not running", "warn"
        more = ("Nothing launches or gets checked until the supervisor runs. Check now runs one pass; "
                "rh supervise keeps it running.")
    chips.append(f'<details class="act"><summary class="chip {tone}"><i class="beat"></i><span>{line}</span></summary>'
                 f'<div class="pop left"><p>{more}</p></div></details>')
    for route in view.get("routes", []):
        name = route["route"]
        label = _platform(name)
        retry = _when(route["next_try"], now, f"route:{name}:next", "retrying ", "retrying on the next check")
        last = (f", the last {_when(route['updated'], now, f'route:{name}:updated')}"
                if _number(route.get("updated")) else "")
        panel = (f'<p>{_text(route["failures"])} failures in a row{last}. The harness waits and retries by itself; '
                 'nothing needs doing unless it stays down.</p>')
        if live and not any(s.get("owner") == "app" for s in ctx["scopes"].get(f"route:{name}", [])):
            panel += (f'<form method="post" action="/stop" data-act data-label="Pause platform">'
                      f'{_hidden(ctx, [("route", name), ("pause", "on")])}{_REASON}'
                      f'<p class="note">No new job starts on {_text(label)} until you resume it from Needs you.</p>'
                      f'<button class="btn danger fill"{_tip(f"rh stop --owner app --pause --route {name} --reason <why>")}>'
                      'Pause platform</button></form>')
        chips.append(f'<details class="act"><summary class="chip bad"><i></i><span>{_text(label)}: '
                     f'{_text(route["last_status"])} · {retry}</span></summary><div class="pop left">{panel}</div></details>')
    for stop in ctx["others"]:
        scope = str(stop["scope"])
        what = ("All launches" if scope == "*" else
                _text(_platform(scope[6:])) if scope.startswith("route:") else _text(scope))
        verb = "paused" if stop.get("kind") == "pause" else "stopped"
        chips.append(f'<span class="chip warn"><i></i><span>{what} {verb} by '
                     f'{_text(stop["owner"])}: {_text(_clause(stop["reason"]))}</span></span>')
    if not view.get("routes") and not ctx["others"]:
        chips.append('<span class="chip"><i class="t-good"></i>No platform problems</span>')
    if live:
        chips.append(_button(ctx, "/tick", [], "Check now", "t", "" if sup else "primary", "rh supervise --once"))
    body = "".join(chips)
    return f'<div class="health" data-region="status" data-sig="{_sig(_still(body))}">{body}</div>'


def _result(result):
    match = _RESULT.fullmatch(result.strip())
    if match:
        code, output = int(match.group(1)), escape(unescape(match.group(2)), quote=True)
        tone, head = ("good", "Last action finished") if code == 0 else ("bad", "Last action failed")
        head = f"{head} · exit {code}"
    else:
        code, output, tone, head = "", _text(result), "neutral", "Last action"
    return (f'<div id="result" class="result {tone}" role="status" data-code="{code}">'
            f'<p>{head}</p><pre>{output}</pre></div>')


def _section(key, heading, body, head_extra="", cls=""):
    """A keyed region the script reconciles: a head it can swap, and a body whose rows it can move."""
    head = f'<div class="head"><h2 id="{key}">{heading}</h2>{head_extra}</div>'
    return (f'<section class="s-{key}{" " + cls if cls else ""}" aria-labelledby="{key}" data-region="{key}" '
            f'data-key="section:{key}" data-sig="{_sig(key, _still(head + body))}">{head}<div class="body">{body}</div>'
            '</section>')


def _rows(items, cls="rows"):
    return f'<ul class="{cls}">{"".join(items)}</ul>'


def _sidebar(jobs, loose, view, ctx):
    live = ctx["live"]
    groups = {"needs": [], "running": [], "queued": [], "done": []}
    for job in sorted(jobs, key=lambda j: j.first if j.group == "needs" else 0):
        groups[job.group].append(job.item())
    needs = ctx["count"]
    top = ""
    if live:
        new = (f'<a class="btn primary new" href="{_url(ctx, "/new")}" data-nav data-k="n"><span>New job</span>'
               '<kbd>n</kbd></a>' if ctx["can_add"] else "")
        home = f'<a class="item home-item" href="{_url(ctx, "/")}" data-nav><span class="name">Overview</span></a>'
        top = f'<div class="side-top" data-region="side-top" data-sig="{_sig(new, home)}">{new}{home}</div>'
    if loose:
        word = "question" if loose == 1 else "questions"
        other = (f'<a class="item" href="{_url(ctx, "/")}" data-nav>' if live else '<div class="item">')
        other += (f'<i class="t-warn"></i><span class="name">{loose} more {word}</span>'
                  f'<span class="sub">Not tied to a listed job; answer them in the overview</span>'
                  + ("</a>" if live else "</div>"))
        groups["needs"].append(f'<li class="row" data-key="loose" data-sig="{_sig(_still(other))}">{other}</li>')
    empty = {"needs": "Nothing needs you", "running": "Nothing running", "queued": "Nothing queued",
             "done": "Nothing finished yet"}
    if any(job.where == "recent" for job in jobs):
        empty["done"] = "Finished jobs that need you are above"
    out = []
    for key, heading in (("needs", "Needs you"), ("running", "Running"), ("queued", "Queued"), ("done", "Done")):
        items = groups[key]
        n = needs if key == "needs" else len(items)
        count = f'<span class="count">{n}</span>' if n else ""
        body = _rows(items) if items else f'<p class="empty">{empty[key]}</p>'
        out.append(_section(f"g-{key}", heading, body, count, "group"))
    foot = (f'<p class="side-foot"><a href="{_url(ctx, "/actions")}">Every action as a plain form</a></p>' if live else "")
    return f'<nav class="side" aria-label="Jobs">{top}{"".join(out)}{foot}</nav>'


def _composer(ctx, draft):
    """New job: the brief first, then where it runs, the name, and the rest folded away."""
    draft = draft if isinstance(draft, dict) else {}
    value = lambda name: _text(draft.get(name, ""))  # noqa: E731
    kind = draft.get("kind", "codex-cloud")
    picked = {k: " checked" if kind == k else "" for k in ("codex-cloud", "local-shell")}
    have = set(draft.get("needs", [])) if isinstance(draft.get("needs"), list) else set()
    needs = "".join(f'<label><input type="checkbox" name="needs" value="{n}"{" checked" if n in have else ""}>{n}</label>'
                    for n in _NEEDS)
    timeout = value("timeout_min") or "120"
    form = (f'<form class="compose" method="post" action="/job" data-act data-label="Start job">{_hidden(ctx, [])}'
            '<label>What should the job do?<textarea name="brief" rows="6" required placeholder="Survey the papers '
            'on … and write runs/&lt;name&gt;/summary.md with one paragraph per paper.">'
            f'{value("brief")}</textarea></label>'
            '<div class="split"><label>Name<input type="text" name="id" required autocomplete="off" spellcheck="false" '
            f'class="mono" pattern="[a-z0-9][a-z0-9._\\-]{{0,62}}" placeholder="lit-scan-03" value="{value("id")}">'
            '<small>Lowercase letters, digits, dot, dash. Its files go to runs/&lt;name&gt;/.</small></label>'
            '<fieldset class="where"><legend>Runs on</legend>'
            f'<label class="choice"><input type="radio" name="kind" value="codex-cloud"{picked["codex-cloud"]}>'
            '<b>Codex cloud</b><small>Works on a copy of the repo at its current commit and returns a patch.</small></label>'
            f'<label class="choice"><input type="radio" name="kind" value="local-shell"{picked["local-shell"]}>'
            '<b>This machine</b><small>Runs one command here, in runs/&lt;name&gt;/.</small></label></fieldset></div>'
            '<label class="cmd">Command<input type="text" name="command" autocomplete="off" spellcheck="false" '
            f'class="mono" placeholder="python3 tools/scan.py --out runs/lit-scan-03/" value="{value("command")}">'
            '<small>Split like a shell line; no shell runs it.</small></label>'
            '<label><span>Done when <span class="opt">· optional</span></span><input type="text" name="accept" autocomplete="off" '
            f'placeholder="summary.md lists every paper with its DOI" value="{value("accept")}"></label>'
            f'<details><summary>More options</summary><div class="opts"><fieldset><legend>It needs</legend>'
            f'<div class="needs">{needs}</div></fieldset><label>Time limit, minutes<input type="text" name="timeout_min" '
            f'inputmode="numeric" pattern="[0-9]{{1,5}}" value="{timeout}"></label></div></details>'
            '<div class="send"><p class="note">The next check picks it up. Nothing is pushed.</p>'
            '<button class="btn primary">Start job</button></div></form>')
    return _section("compose", "New job", form)


def _event(ev, now):
    """One timeline entry in plain words."""
    kind, data = str(ev.get("kind", "")), ev.get("data") if isinstance(ev.get("data"), dict) else {}
    tone = "neutral"
    if kind == "state":
        word, tone = _STATES.get(data.get("to"), (data.get("to"), "neutral"))
        text = f"{word}: {data['detail']}" if data.get("detail") else str(word)
    elif kind == "job-added":
        text = f"Added, to run on {_platform(data.get('kind', ''))}"
    elif kind == "inbox":
        text, tone = f"Asked you: {_sentence(data.get('action', ''))}", "warn"
    elif kind == "harvested":
        text = f"Patch saved from the {data.get('source', 'web view')}"
    elif kind == "landed":
        text, tone = f"Saved to branch {data.get('branch', '')}", "good"
    elif kind == "job-error":
        text, tone = f"Harness error: {data.get('error', '')}", "bad"
    elif kind == "prompt":
        text = "Prompt written for the web composer"
    else:
        extra = ", ".join(f"{k} {v}" for k, v in sorted(data.items()) if isinstance(v, (str, int, float)))[:200]
        text = _sentence(kind.replace("-", " ")) + (f": {extra}" if extra else "")
    when = _when(ev["ts"], now, f"ev:{ev.get('seq')}") if _number(ev.get("ts")) else ""
    inner = f'<i class="t-{tone}"></i><p>{_text(text)}</p>{when}'
    return (f'<li class="row" data-key="ev:{_text(ev.get("seq", ""))}" data-sig="{_sig(_still(inner))}" '
            f'data-tone="{tone}">{inner}</li>')


def _job_pane(opened, jobs, ctx, feedback):
    """The open job, read top to bottom: what it needs from you, what it was asked, its result and output, then
    everything that happened to it."""
    now = ctx["now"]
    job = opened.get("job") if isinstance(opened.get("job"), dict) else {}
    jid = str(job.get("id") or opened.get("id") or "")
    back = (f'<a class="back" href="{_url(ctx, "/")}" data-nav>‹ All jobs</a>' if ctx["live"] else "")
    if not job:
        body = (f'{back}<section class="s-missing" data-region="j-missing" data-sig="{_sig(jid)}"><h2 class="job-name">'
                f'No job named {_text(jid)}</h2><p class="soft">It may have been a typo in the address. The jobs the '
                'harness knows are in the list.</p></section>')
        return "job:" + jid, body
    entry = next((j for j in jobs if j.id == jid), None)
    row = {"id": jid, "state": job.get("state"), "paused": False, "detail": job.get("detail", ""),
           "external_id": job.get("external_id")}
    if entry is None:
        where = ("live" if job.get("state") in ("launching", "handoff", "running", "stalled", "collecting")
                 else "not_started" if job.get("state") in _PRE else "recent")
        entry = _Job(row, where, ctx)
    spec = job.get("spec") if isinstance(job.get("spec"), dict) else {}
    status, tone = _state(job.get("state"))
    facts = [status, _text(_platform(job.get("kind") or spec.get("kind", "")))]
    if _number(job.get("started")) and entry.where == "live":
        facts.append(_when(job["started"], now, "j:started", "started "))
    elif entry.where == "recent" and _number(job.get("updated")):
        facts.append(_when(job["updated"], now, "j:ended", "finished "))
    elif _number(job.get("created")):
        facts.append(_when(job["created"], now, "j:added", "added "))
    task = _task(job)
    if task:
        facts.append(f"<span>{task}</span>")
    head_body = (f'<div><h2 class="job-name" id="j-name" tabindex="-1">{_text(jid)}</h2><p class="facts">'
                 f'{"".join(f if f.startswith("<") else f"<span>{f}</span>" for f in facts)}</p></div>'
                 f'{_acts(entry.actions(ctx))}')
    parts = [back, f'<header class="job-head" data-region="j-head" data-sig="{_sig(_still(head_body))}">'
                   f'{head_body}</header>', feedback]
    needs = entry.needs(ctx)
    if needs:
        cards = "".join(f'<div class="ask-card"><p class="label">Needs you</p>{n}</div>' for n in needs)
        parts.append(f'<section class="asks" data-region="j-asks" data-sig="{_sig(_still(cards))}">{cards}</section>')
    holds = "".join(entry.holds())
    detail = _text(job.get("detail") or "")
    next_line = _NEXT.get(job.get("state"), "")
    now_body = (f'<p class="result-line">{status}{f" <span class=\"what\">{detail}</span>" if detail else ""}</p>'
                + (f'<p class="soft">{next_line}</p>' if next_line else "") + holds)
    receipt = opened.get("receipt") if isinstance(opened.get("receipt"), dict) else None
    if receipt:
        checks = receipt.get("checks") if isinstance(receipt.get("checks"), list) else []
        lines = "".join(
            f'<li><span class="state t-{"good" if c.get("exit") == 0 else "bad"}"><i></i>exit {_text(c.get("exit"))}'
            f'</span><code>{_text(c.get("cmd", c))}</code></li>' if isinstance(c, dict) else f"<li>{_text(c)}</li>"
            for c in checks)
        model = receipt.get("model_served") or receipt.get("model_requested")
        rest = "".join(f"<dt>{label}</dt><dd>{_text(receipt[key])}</dd>" for key, label in
                       (("route", "Ran on"), ("model_served", "Model")) if receipt.get(key))
        now_body += ((f'<ul class="checks">{lines}</ul>' if lines else "")
                     + (f'<dl class="spec">{rest}</dl>' if rest or model else ""))
    heading = "Result" if entry.where == "recent" else "Now"
    parts.append(_section("j-now", heading, now_body))
    log = opened.get("log")
    if isinstance(log, str) and log.strip():
        parts.append(_section("j-output", "Output", f'<pre class="log" tabindex="0">{_text(log)}</pre>',
                              '<span class="count">the end of out.log</span>'))
    brief = _text(spec.get("brief") or "")
    rows = []
    if spec.get("command"):
        rows.append(("Command", f'<code>{_text(" ".join(map(str, spec["command"])))}</code>'))
    if spec.get("accept"):
        rows.append(("Done when", _text(spec["accept"])))
    if spec.get("needs"):
        rows.append(("Needs", _text(", ".join(map(str, spec["needs"])))))
    if spec.get("write_root"):
        rows.append(("Writes to", f'<code>{_text(spec["write_root"])}</code>'))
    if spec.get("timeout_min"):
        rows.append(("Time limit", f"{_text(spec['timeout_min'])} min"))
    spec_list = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
    parts.append(_section("j-brief", "Asked", f'<div class="brief">{brief}</div>'
                          + (f'<dl class="spec">{spec_list}</dl>' if spec_list else "")))
    events = [e for e in opened.get("events", []) if isinstance(e, dict)][-60:]
    timeline = _rows([_event(e, now) for e in events], "rows timeline") if events else (
        '<p class="empty">Nothing has happened yet. The next check reviews it.</p>')
    parts.append(_section("j-timeline", "Timeline", timeline))
    return "job:" + jid, "".join(parts)


def _home_pane(jobs, loose_cards, view, ctx, feedback):
    """The overview: what needs you, a New job composer when the server can add one, and how the day went."""
    cards = sorted([(j.first, j.card()) for j in jobs if j.group == "needs"] + loose_cards, key=lambda pair: pair[0])
    sup = view.get("supervisor")
    checked = (f'The harness checked {_when(ctx["now"] - sup["age_s"], ctx["now"], "tick:empty")}.'
               if sup and isinstance(sup["age_s"], int) and not isinstance(sup["age_s"], bool)
               else "The harness has not run yet." if not sup else "")
    anything = bool(jobs or loose_cards)
    if cards:
        body = _rows([html for _, html in cards], "rows cards")
    elif anything or not ctx["live"]:
        body = (f'<div class="empty"><p class="ask">Nothing needs you.</p><p>{checked}</p><p class="soft">Questions to '
                'answer, patches to paste and jobs paused from this app show up here, each with the button that '
                'settles it.</p></div>')
    else:
        start = ("<b>Start a job</b><span class=\"soft\">Describe it in New job below, or add a job file with "
                 "<code>rh job add &lt;file&gt;</code>.</span>")
        body = ('<div class="empty"><p class="ask">Nothing here yet.</p><ol class="steps">'
                '<li><b>Let the harness check</b><span class="soft">The header says when it last checked. Check now '
                'runs one pass; <code>rh supervise</code> keeps it running.</span></li>'
                f'<li>{start}</li><li><b>Answer what it asks</b><span class="soft">Questions, patches to paste and '
                'paused jobs appear here with the button that settles each.</span></li></ol></div>')
    count = ctx["count"]
    parts = [feedback, _section("needs", "Needs you", body, f'<span class="count warn">{count}</span>' if count else "")]
    if ctx["live"] and ctx["can_add"]:
        parts.append(_composer(ctx, view.get("compose")))
    day = ", ".join(f"{_text(n)} {_text(_STATES[s][0].lower() if s in _STATES else s)}"
                    for s, n in sorted(view.get("last_24h", {}).items()))
    groups = {g: sum(1 for j in jobs if j.group == g) for g in ("running", "queued")}
    tallies = (f'<p class="tallies"><span><b>{groups["running"]}</b>running</span><span><b>{groups["queued"]}</b>queued'
               f'</span><span><b>{len(view.get("recent", []))}</b>recently finished</span></p>'
               f'<p class="soft">Last 24 h: {day or "nothing finished"}</p>')
    parts.append(_section("today", "Today", tallies))
    return "home", "".join(parts)


def render(view: dict, now: float, token: str | None = None, result: str | None = None) -> str:
    """Return one self-contained HTML5 page, escaping all supplied text. Without a token it is the static snapshot:
    no script and no controls. With one it is the live app, with forms and <script src="/app.js">. The view may also
    carry "open" (the job `rh job show` returned, plus "log", its output tail; or {"id", "job": None} for a job the
    harness does not know), "can_add" (the server takes POST /job), "new" (show the New job form on its own) and
    "compose" (a draft to refill that form with)."""
    live = token is not None
    opened = view.get("open") if isinstance(view.get("open"), dict) else None
    can_add = live and bool(view.get("can_add"))
    compose = can_add and bool(view.get("new")) and not opened
    scopes, inbox = {}, view.get("inbox", [])
    for stop in view.get("stops", []):
        scopes.setdefault(str(stop["scope"]), []).append(stop)
    rows = [(row, where) for where in ("live", "not_started", "recent") for row in view.get(where, [])]
    known = {row["id"] for row, _ in rows}
    asks = {}
    for item in inbox:
        if item.get("job") in known:
            asks.setdefault(item["job"], []).append(item)
    # Every form on a job's page, or on the New job page, returns there; elsewhere forms return to the overview.
    named = opened["job"]["id"] if opened and isinstance(opened.get("job"), dict) else None
    back = f"/job/{named}" if named else "/new" if compose else None
    ctx = {"token": token, "live": live, "now": now, "scopes": scopes, "asks": asks, "mode": "card",
           "can_add": can_add, "back": back,
           "others": [s for s in view.get("stops", []) if s.get("owner") != "app"
                      and not str(s["scope"]).startswith("job:")]}
    jobs = [_Job(row, where, ctx) for row, where in rows]
    # Needs with no job of their own: inbox items for other jobs or none, and the app's own pauses on platforms, on
    # every launch, or on jobs no longer listed.
    loose = []
    for item in inbox:
        if item.get("job") not in known:
            about = (f'<div class="about">For job <span class="name">{_text(item["job"])}</span></div>'
                     if item.get("job") else "")
            loose.append((item.get("created") if _number(item.get("created")) else float("inf"),
                          _row(f"inbox:{item['id']}", live, _ask(item, ctx, True) + about, card=True)))
    for stop in view.get("stops", []):
        scope = str(stop["scope"])
        name, _, value = scope.partition(":")
        if stop.get("owner") != "app" or (name == "job" and value in known):
            continue
        if scope == "*":
            need = _resume(stop, ctx, True, "launches", "any job", [], "")
        elif name == "route":
            need = _resume(stop, ctx, True, "platform", _text(_platform(value)), [("route", value)], f" --route {value}")
        else:
            need = _resume(stop, ctx, True, name, _text(value), [(name, value)] if value else [],
                           f" --{name} {value}" if value else "")
        loose.append((stop.get("created") if _number(stop.get("created")) else float("inf"),
                      _row(f"stop:{stop.get('id', scope)}", live, need, card=True)))
    ctx["count"] = sum(j.count for j in jobs) + len(loose)
    feedback = _result(result) if live and result else ""
    if opened:
        ctx["mode"] = "inline"
        page, pane = _job_pane(opened, jobs, ctx, feedback)
        ctx["mode"] = "card"
    elif compose:
        page, pane = "new", feedback + _composer(ctx, view.get("compose"))
    else:
        page, pane = _home_pane(jobs, loose, view, ctx, feedback)
    side = _sidebar(jobs, len(loose), view, ctx)
    if live:
        head = ('<noscript><meta http-equiv="refresh" content="30"></noscript>\n'
                '<script src="/app.js" defer></script>\n')
        tools = ('<button type="button" class="btn" data-keys hidden>Shortcuts <kbd>?</kbd></button>'
                 '<span class="live" data-live data-state="on"><i></i><span>Live</span></span>')
        keys = "".join(f"<dt><kbd>{k}</kbd></dt><dd>{v}</dd>" for k, v in _KEYS)
        words = "".join(f"<dt>{_text(k)}</dt><dd>{_text(v)}</dd>" for k, v in _WORDS)
        extra = ('<div class="toasts" aria-live="polite"></div>'
                 '<dialog id="keys" aria-label="Keyboard shortcuts and words"><div class="pair">'
                 f'<div><p class="title" tabindex="-1" autofocus>Keyboard</p><dl>{keys}</dl></div>'
                 f'<div><p class="title">Words in this app</p><dl class="words">{words}</dl></div></div>'
                 '<form method="dialog"><button class="btn">Close</button></form></dialog>\n')
    else:
        head, extra = "", ""
        tools = '<span class="live"><span>Snapshot</span></span>'
    title = f"{named} · Research harness" if named else "New job · Research harness" if compose else "Research harness"
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>{_text(title)}</title>\n'
            f'{head}<style>{_STYLE}</style>\n</head>\n<body data-now="{now:.3f}" data-open="{_text(page)}">\n'
            f'<header class="bar"><span class="mark" aria-hidden="true"></span><h1>Research harness</h1>'
            f'{_health(view, ctx)}<div class="tools">{tools}</div></header>\n'
            f'<div class="app">{side}<main class="pane" data-pane="{_text(page)}" tabindex="-1"><div class="page">'
            f'{pane}</div></main></div>\n{extra}</body>\n</html>\n')
