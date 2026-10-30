"""The app. With a token it is the live page `rh serve` sends: a sidebar listing the jobs by what they need, beside a
composer that starts a job, or the open job read as a transcript: what it was asked, what happened, its live output,
what it needs from you, and the receipt it ends on, with its files. One same-origin script, SCRIPT (served at /app.js),
keeps it live, moves between jobs without reloading and turns submits into in-place updates; without it every link
and form still works. Without a token it is the static snapshot `rh status --html` writes: the overview and the job
lists, with no script and no controls."""
import re
import shlex
import zlib
from html import escape
from urllib.parse import quote


# One plane: the sidebar on a tinted ground beside a white page. Colours are tokens; tests/test_dashboard.py computes
# each text pair's contrast. Blue marks what is live; amber what needs you. Motion uses transform and opacity only.
_STYLE = """
:root {
  color-scheme: light dark;
  --ground: #f8f8f8;
  --hover: #eeeeee;
  --surface: #ffffff;
  --raised: #f4f4f4;
  --border: #e6e6e6;
  --rule: #f0f0f0;
  --control: #d4d4d4;
  --ink: #0a0a0a;
  --muted: #6b6b6b;
  --primary: #141414;
  --on-primary: #ffffff;
  --live: #1d5fbf;
  --good: #2b7a2f;
  --warn: #8a5300;
  --bad: #c4242a;
  --on-bad: #ffffff;
  --live-bg: #edf3fc;
  --good-bg: #edf6ee;
  --warn-bg: #fbf4e6;
  --bad-bg: #fdeeee;
  --shade: rgba(0, 0, 0, .12);
  --ease: cubic-bezier(.2, .8, .2, 1);
}
@media (prefers-color-scheme: dark) {
  :root {
    --ground: #0e0e0e;
    --hover: #1f1f1f;
    --surface: #161616;
    --raised: #1f1f1f;
    --border: #2b2b2b;
    --rule: #232323;
    --control: #404040;
    --ink: #ededed;
    --muted: #a3a3a3;
    --primary: #f0f0f0;
    --on-primary: #121212;
    --live: #86b4ff;
    --good: #7cc782;
    --warn: #e9b35a;
    --bad: #ff8f87;
    --on-bad: #1c0605;
    --live-bg: #16233a;
    --good-bg: #152516;
    --warn-bg: #2f2410;
    --bad-bg: #351818;
    --shade: rgba(0, 0, 0, .6);
  }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html { height: 100%; }
body {
  margin: 0;
  height: 100%;
  background: var(--surface);
  color: var(--ink);
  font: 14px/20px system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  overflow-wrap: anywhere;
  -webkit-text-size-adjust: 100%;
}
p, ul, ol, h1, h2, h3, pre, dl, dd, figure, fieldset { margin: 0; }
ul, ol { list-style: none; padding: 0; }
fieldset { padding: 0; border: 0; min-width: 0; }
a { color: inherit; }
code, kbd, pre, .mono { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
code { font-size: 12px; }
:focus-visible { outline: 2px solid var(--live); outline-offset: 2px; }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
.soft { color: var(--muted); }

/* The shell: the brand and the new-job link, the job list, and the check status stacked in the sidebar; the page beside it. */
.app { display: grid; grid-template-columns: 256px minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr) auto;
  grid-template-areas: "top pane" "side pane" "foot pane"; height: 100vh; height: 100dvh; }
.side-top, .side, .checks { background: var(--ground); border-right: 1px solid var(--border); }
.side-top { grid-area: top; display: grid; gap: 12px; padding: 14px 12px 6px; }
.side { grid-area: side; overflow-y: auto; padding: 0 8px 16px; }
.checks { grid-area: foot; display: grid; gap: 8px; padding: 12px 16px 14px; border-top: 1px solid var(--border); }
.pane { grid-area: pane; overflow-y: auto; }
.pane:focus { outline: none; }
.brand { display: flex; align-items: center; gap: 10px; padding: 0 4px; font-weight: 600; }
.mark { position: relative; flex: none; width: 20px; height: 20px; border-radius: 5px; background: var(--primary); }
.mark::before, .mark::after { content: ""; position: absolute; left: 4px; height: 2px; border-radius: 1px;
  background: var(--on-primary); }
.mark::before { top: 6px; width: 12px; }
.mark::after { top: 12px; width: 7px; }
.btn.new { justify-content: flex-start; width: 100%; }
.btn.new .plus { font-size: 18px; line-height: 1; font-weight: 400; }

/* The job list. */
.group { margin-top: 22px; }
.group .head { display: flex; align-items: baseline; gap: 8px; padding: 0 8px 4px; }
.group h2 { font-size: 12px; line-height: 16px; font-weight: 600; color: var(--muted); }
.count { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
.s-g-needs .count { color: var(--warn); font-weight: 600; }
.side-empty { padding: 16px 8px; color: var(--muted); }
.item { display: grid; grid-template-columns: 8px minmax(0, 1fr) auto; column-gap: 10px; align-items: baseline;
  padding: 6px 8px; border-radius: 6px; text-decoration: none; }
a.item:hover { background: var(--hover); }
a.item[aria-current="page"] { background: var(--hover); box-shadow: inset 0 0 0 1px var(--border); }
.item .dot { align-self: center; }
.item .name { font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; overflow-wrap: normal; }
.item .when { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
.item .sub { grid-column: 2 / 4; font-size: 12px; line-height: 16px; color: var(--muted); white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; overflow-wrap: normal; }
.s-g-needs .item .sub { color: var(--warn); }
.s-g-needs .item .sub.fail { color: var(--bad); }
.row.pending .sub { color: var(--live); }
.dot { display: inline-block; flex: none; width: 8px; height: 8px; border-radius: 50%; background: var(--control); }
.dot.live { background: var(--live); }
.dot.good { background: var(--good); }
.dot.warn { background: var(--warn); }
.dot.bad { background: var(--bad); }
.dot.pulse { animation: pulse 1.6s ease-in-out infinite; }

/* The check status at the foot of the sidebar. */
.checks .line { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.checks .line.warn { color: var(--warn); }
.checks .line.bad { color: var(--bad); }
.checks .more { font-size: 12px; line-height: 16px; color: var(--muted); }
.checks .btns { display: flex; flex-wrap: wrap; gap: 4px; margin-left: -8px; }
.checks .route { font-size: 12px; line-height: 16px; color: var(--bad); }
.checks .route.warn { color: var(--warn); }
.tools { display: flex; align-items: center; gap: 8px; margin-left: -8px; }
.conn { font-size: 12px; color: var(--warn); }
.conn[data-state="on"] { display: none; }

/* Controls. */
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 36px; padding: 0 14px;
  border: 1px solid var(--control); border-radius: 8px; background: var(--surface); color: var(--ink); font: inherit;
  font-weight: 500; text-decoration: none; cursor: pointer; white-space: nowrap; overflow-wrap: normal;
  transition: background-color .12s, border-color .12s, color .12s; }
.btn:hover { background: var(--hover); }
.btn:active { transform: translateY(1px); }
.btn.primary { background: var(--primary); border-color: var(--primary); color: var(--on-primary); }
.btn.primary:hover { background: var(--primary); opacity: .86; }
.btn.quiet { border-color: transparent; background: transparent; color: var(--muted); padding: 0 8px; }
.btn.quiet:hover { color: var(--ink); background: var(--hover); }
.btn.small { min-height: 28px; font-size: 13px; }
.btn.danger.fill { background: var(--bad); border-color: var(--bad); color: var(--on-bad); }
.btn.danger.fill:hover { background: var(--bad); opacity: .86; }
.btn[aria-busy="true"] { opacity: .6; cursor: progress; }
.btn.ok::before { content: "\\2713"; animation: pop .3s var(--ease); }
.btn.no { animation: shake .32s ease-in-out; }
summary { cursor: pointer; }
summary.btn { list-style: none; }
summary::-webkit-details-marker { display: none; }
details.act { position: relative; display: inline-block; }
.pop { position: absolute; z-index: 20; top: calc(100% + 6px); right: 0; display: grid; gap: 10px;
  width: min(320px, calc(100vw - 32px)); padding: 14px; background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; box-shadow: 0 10px 28px var(--shade); animation: drop .16s var(--ease); }
.pop.left { right: auto; left: 0; }
.pop.up { top: auto; bottom: calc(100% + 6px); }
.pop p { color: var(--muted); }
label { display: grid; gap: 6px; font-size: 13px; font-weight: 500; }
input[type="text"], textarea { width: 100%; padding: 7px 10px; border: 1px solid var(--control); border-radius: 6px;
  background: var(--surface); color: var(--ink); font: inherit; }
input[type="text"]:focus, textarea:focus { border-color: var(--ink); outline: none; }
textarea { resize: vertical; }
textarea.mono, input.mono { font-size: 13px; }
.note { font-size: 12px; line-height: 16px; color: var(--muted); }

/* The page. */
.page { display: grid; gap: 24px; max-width: 760px; margin: 0 auto; padding: 32px 40px 72px; }
.back { display: none; justify-self: start; margin: -8px 0 -8px -8px; }
h1 { font-size: 24px; line-height: 32px; font-weight: 600; letter-spacing: -.01em; }
.label { display: flex; align-items: center; gap: 8px; font-size: 12px; line-height: 16px; font-weight: 600;
  color: var(--warn); }
.result { padding: 12px 14px; border-radius: 8px; background: var(--raised); }
.result.bad { background: var(--bad-bg); color: var(--bad); }
.result pre, .toast pre { max-height: 10rem; margin-top: 6px; overflow: auto; font-size: 12px; line-height: 18px;
  white-space: pre-wrap; color: var(--ink); }

/* Home: what needs you, then the composer. */
.home-top { padding-top: 6vh; }
.s-needs .rows > .row { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
  gap: 8px 16px; padding: 14px 0; border-top: 1px solid var(--rule); }
.s-needs .rows > .row:first-child { border-top: 0; padding-top: 4px; }
.s-needs h2 { font-size: 16px; line-height: 24px; font-weight: 600; }
.s-needs .head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 4px; }
.say { display: grid; gap: 2px; flex: 1 1 320px; min-width: 0; }
.say .ask { font-weight: 500; }
.say .detail, .say .for, .say > .when { font-size: 13px; color: var(--muted); }
.say .for a { color: var(--ink); }
.acts { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.cli { margin-top: 4px; }
.compose { display: grid; gap: 12px; }
.compose h1 { margin-bottom: 4px; }
.box { border: 1px solid var(--control); border-radius: 12px; background: var(--surface);
  box-shadow: 0 1px 2px var(--shade); transition: border-color .12s; }
.box:focus-within { border-color: var(--ink); }
.box textarea[name="brief"] { display: block; min-height: 104px; padding: 14px 16px 6px; border: 0; background: transparent;
  font-size: 16px; line-height: 24px; resize: none; }
.cmd { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 2px 10px; align-items: center;
  padding: 6px 16px 8px; border-top: 1px solid var(--rule); }
.cmd label { font-size: 12px; color: var(--muted); }
.cmd input { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.cmd small { grid-column: 2; font-size: 12px; color: var(--muted); }
.compose:has(input[name="kind"][value="codex-cloud"]:checked) .cmd { display: none; }
.box .bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px 8px 8px 12px; }
.where { display: flex; flex-wrap: wrap; gap: 4px; }
.chip { position: relative; display: inline-flex; align-items: center; gap: 6px; min-height: 32px; padding: 0 12px;
  border: 1px solid transparent; border-radius: 999px; color: var(--muted); font-weight: 500; cursor: pointer; }
.chip input { position: absolute; opacity: 0; pointer-events: none; }
.chip:hover { color: var(--ink); background: var(--hover); }
.chip:has(input:checked) { border-color: var(--control); background: var(--raised); color: var(--ink); }
.chip:has(input:focus-visible) { outline: 2px solid var(--live); outline-offset: 2px; }
.chip small { font-size: 12px; font-weight: 400; color: var(--warn); }
.compose .name { display: flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 400; color: var(--muted); }
.compose .name input { width: 24ch; min-width: 0; padding: 4px 8px; font-size: 12px; }
.send { margin-left: auto; }
.under { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; }
.opts > summary { margin-left: -8px; }
.opts-body { display: grid; gap: 14px; width: min(100%, 560px); margin-top: 8px; padding: 14px;
  border: 1px solid var(--border); border-radius: 10px; }
.needs { display: flex; flex-wrap: wrap; gap: 6px 16px; }
.needs label { display: flex; align-items: center; gap: 6px; font-weight: 400; }
.opts legend { margin-bottom: 6px; font-size: 13px; font-weight: 500; }
.steps { display: grid; gap: 14px; counter-reset: step; }
.steps > li { display: grid; grid-template-columns: 24px minmax(0, 1fr); gap: 10px; }
.steps > li::before { counter-increment: step; content: counter(step); font-weight: 600; color: var(--muted);
  font-variant-numeric: tabular-nums; }
.steps > li > div { display: grid; gap: 8px; justify-items: start; }
.how { padding-top: 8px; color: var(--muted); }
.how .steps b { color: var(--ink); font-weight: 500; }

/* A job, read as a transcript. */
.job-head { display: flex; flex-wrap: wrap; align-items: flex-start; justify-content: space-between; gap: 12px 16px; }
.job-head > div:first-child { flex: 1 1 360px; min-width: 0; }
.job-head h1:focus { outline: none; }
.facts { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 14px; margin-top: 6px; font-size: 13px;
  color: var(--muted); }
.state { display: inline-flex; align-items: center; gap: 6px; color: var(--ink); font-weight: 500; }
.talk { display: grid; gap: 20px; }
.ask-bubble { justify-self: end; display: grid; gap: 8px; max-width: min(88%, 620px); }
.bubble { padding: 12px 16px; border-radius: 14px; background: var(--raised); font-size: 15px; line-height: 22px;
  white-space: pre-wrap; }
.spec-more { justify-self: end; font-size: 13px; }
.spec-more > summary { color: var(--muted); }
.spec { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 6px 16px; margin-top: 8px; font-size: 13px; }
.spec dt { color: var(--muted); }
.timeline { display: grid; gap: 2px; }
.timeline .row { display: grid; grid-template-columns: 8px minmax(0, 1fr) auto; gap: 10px; align-items: baseline;
  padding: 3px 0; font-size: 13px; color: var(--muted); }
.timeline .row .dot { width: 6px; height: 6px; justify-self: center; align-self: start; margin-top: 7px; }
.timeline .when { font-size: 12px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.s-j-output .head, .s-j-timeline .head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.s-j-output h2, .s-j-timeline h2, .receipt h2 { font-size: 12px; line-height: 16px; font-weight: 600;
  color: var(--muted); }
.livetag { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--live); font-weight: 500; }
.log.ended { max-height: 168px; }
.log { max-height: 360px; overflow: auto; padding: 12px 14px; border-radius: 10px; background: var(--raised);
  font-size: 12px; line-height: 18px; white-space: pre-wrap; overflow-wrap: anywhere; }
.ask-card { display: grid; gap: 12px; padding: 16px; border: 1px solid var(--border); border-radius: 12px;
  background: var(--surface); box-shadow: 0 1px 2px var(--shade); }
.ask-card h3 { font-size: 16px; line-height: 24px; font-weight: 600; }
.ask-card .detail { color: var(--muted); }
.ask-card form:not(.pop) { display: grid; gap: 10px; justify-items: start; width: 100%; }
.ask-card form label { width: 100%; }
.ask-card .foot { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.prompt, .envspec { width: 100%; max-height: 240px; overflow: auto; padding: 10px 12px; border: 1px solid var(--border);
  border-radius: 8px; background: var(--raised); font-size: 12px; line-height: 18px; white-space: pre-wrap; }
.hold { font-size: 13px; color: var(--warn); }
.now { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.now .what { font-weight: 500; }
.now .soft { flex-basis: 100%; padding-left: 18px; }
.receipt { border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }
.receipt > * + * { border-top: 1px solid var(--rule); }
.verdict { display: grid; gap: 4px; padding: 16px; }
.verdict .big { display: flex; align-items: baseline; gap: 10px; font-size: 18px; line-height: 26px; font-weight: 600; }
.verdict .tick { color: var(--good); }
.verdict.bad .tick { color: var(--bad); }
.verdict.warn .tick { color: var(--warn); }
.verdict .why { color: var(--muted); }
.receipt .part { display: grid; gap: 8px; padding: 12px 16px; }
.checklist li, .files li { display: flex; align-items: baseline; gap: 10px; font-size: 13px; }
.checklist code { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.checklist .ok { color: var(--good); }
.checklist .bad { color: var(--bad); }
.checklist .exit { margin-left: auto; color: var(--muted); font-size: 12px; white-space: nowrap; }
.files { display: grid; gap: 4px; }
.files a { color: var(--live); text-underline-offset: 2px; }
.files .size { margin-left: auto; color: var(--muted); font-size: 12px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.peek { max-height: 280px; overflow: auto; padding: 10px 12px; border-radius: 8px; background: var(--raised);
  font-size: 12px; line-height: 18px; white-space: pre-wrap; }
.stamp { padding: 10px 16px; font-size: 12px; line-height: 16px; color: var(--muted); background: var(--ground); }
.receipt .acts { padding: 12px 16px; }
.reply { display: flex; align-items: flex-end; gap: 8px; padding: 8px 8px 8px 14px; border: 1px solid var(--control);
  border-radius: 12px; }
.reply:focus-within { border-color: var(--ink); }
.reply textarea { min-height: 36px; padding: 7px 0; border: 0; background: transparent; resize: none; }

/* Motion and passing messages. */
.toasts { position: fixed; z-index: 30; right: 16px; bottom: 16px; display: grid; gap: 8px;
  width: min(24rem, calc(100vw - 32px)); }
.toast { position: relative; padding: 12px 40px 12px 14px; border: 1px solid var(--border); border-radius: 10px;
  background: var(--surface); box-shadow: 0 10px 28px var(--shade); animation: rise .2s var(--ease); }
.toast p { display: flex; align-items: center; gap: 8px; font-weight: 500; }
.toast.no p { color: var(--bad); }
.toast details { margin-top: 4px; font-size: 12px; color: var(--muted); }
.toast .x { position: absolute; top: 6px; right: 6px; width: 28px; height: 28px; border: 0; border-radius: 6px;
  background: transparent; color: var(--muted); font: inherit; font-size: 16px; cursor: pointer; }
.toast .x:hover { background: var(--hover); color: var(--ink); }
.toast.leaving { opacity: 0; transform: translateY(6px); transition: opacity .2s, transform .2s; }
.moving { z-index: 5; background: var(--surface); border-radius: 8px; box-shadow: 0 10px 28px var(--shade); }
.ghost { z-index: 25; background: var(--ground); pointer-events: none; }
.arrived { animation: arrive .36s var(--ease) both; }
.entering { animation: enter .26s var(--ease) both; }
.changed { animation: flash 1.4s ease-out; }
.bumped { animation: pop .34s var(--ease); }
.beating { animation: beat .6s var(--ease); }
dialog { width: min(30rem, calc(100vw - 32px)); max-height: calc(100vh - 32px); overflow: auto; padding: 20px;
  border: 1px solid var(--border); border-radius: 12px; background: var(--surface); color: var(--ink);
  box-shadow: 0 16px 40px var(--shade); }
dialog[open] { animation: rise .2s var(--ease); }
dialog::backdrop { background: rgba(0, 0, 0, .3); }
dialog .title { margin-bottom: 12px; font-size: 15px; font-weight: 600; }
dialog .title:focus { outline: none; }
dialog dl { display: grid; grid-template-columns: auto 1fr; gap: 8px 14px; margin: 0 0 16px; }
dialog dd { color: var(--muted); }
dialog .foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
dialog .foot a { font-size: 13px; color: var(--muted); }
kbd { display: inline-block; min-width: 22px; padding: 0 6px; border: 1px solid var(--control); border-bottom-width: 2px;
  border-radius: 5px; font-size: 12px; line-height: 18px; text-align: center; }
@keyframes pulse { 50% { opacity: .35; } }
@keyframes pop { from { opacity: 0; transform: scale(.8); } }
@keyframes shake { 25% { transform: translateX(-4px); } 75% { transform: translateX(4px); } }
@keyframes drop { from { opacity: 0; transform: translateY(-4px); } }
@keyframes rise { from { opacity: 0; transform: translateY(8px); } }
@keyframes arrive { from { opacity: 0; transform: translateY(-6px); } }
@keyframes enter { from { opacity: 0; transform: translateY(6px); } }
@keyframes flash { 15% { background-color: var(--live-bg); } }
@keyframes beat { 40% { transform: scale(1.8); opacity: .5; } }
@keyframes fade { from { opacity: 0; } }
@media (prefers-reduced-motion: reduce) {
  .dot.pulse, .changed, .bumped, .beating, .btn.no, .btn.ok::before { animation: none; }
  .arrived, .entering, .pop, .toast, dialog[open] { animation: fade .2s ease-out; }
  .btn:active, .toast.leaving { transform: none; }
}
@media (max-width: 1099px) {
  .app { grid-template-columns: 240px minmax(0, 1fr); }
  .page { padding: 28px 28px 64px; }
}
/* Phone: a top bar, the check line under it, then the page; the job list is the home page. */
@media (max-width: 719px) {
  .app { display: flex; flex-direction: column; height: auto; min-height: 100vh; min-height: 100dvh; }
  .side-top, .side, .checks { border-right: 0; }
  .side-top { position: sticky; top: 0; z-index: 10; display: flex; align-items: center; gap: 8px; min-height: 56px;
    padding: 6px 8px 6px 12px; border-bottom: 1px solid var(--border); }
  .btn.new { width: auto; margin-left: auto; }
  .checks { order: 2; grid-template-columns: minmax(0, 1fr) auto; align-items: center; padding: 8px 8px 8px 16px;
    border-top: 0; border-bottom: 1px solid var(--border); }
  .checks .tools { display: none; }
  .checks .more { display: none; }
  .checks .btns { margin-left: 0; }
  .pane { order: 3; overflow: visible; }
  .side { order: 4; overflow: visible; padding: 0 8px 32px; background: var(--surface); }
  body:not([data-open="home"]) .side { display: none; }
  body[data-open="home"][data-jobs="1"] .s-compose,
  body[data-open="home"][data-jobs="1"] .s-needs .row[data-job] { display: none; }
  body[data-open="home"][data-jobs="1"] .s-needs:not(:has(.row:not([data-job]))) { display: none; }
  body[data-open="home"][data-jobs="1"] .page { padding-bottom: 0; }
  .page { gap: 20px; padding: 16px 16px 48px; }
  .home-top { padding-top: 8px; }
  .back { display: inline-flex; }
  .btn, .chip { min-height: 44px; }
  .btn.small { min-height: 36px; }
  .item { padding: 10px 8px; }
  .bubble { font-size: 14px; line-height: 21px; }
  .ask-bubble { max-width: 100%; }
  .toasts { right: 8px; left: 8px; bottom: 8px; width: auto; }
  .compose .name { flex: 1 1 160px; }
  .compose .name span { white-space: nowrap; }
  .compose .name input { width: 100%; }
  .checks .status { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 4px 8px; }
  .reply { flex-wrap: wrap; }
  .reply textarea { flex: 1 1 100%; min-height: 64px; }
  .reply .btn { margin-left: auto; }
  /* A popover anchored to its button can open past the screen edge; on a phone it is a sheet at the bottom. */
  .pop, .pop.left, .pop.up { position: fixed; top: auto; right: 8px; bottom: 8px; left: 8px; width: auto;
    max-height: calc(100dvh - 72px); overflow: auto; }
}
"""

# The client for the live page, served same-origin at /app.js. It polls the page it shows, swaps only what changed
# (FLIP for rows that move between groups), follows app links without reloading, posts forms with fetch and says
# what happened in a toast, and adds the keyboard.
SCRIPT = r"""(() => {
'use strict';
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const calm = matchMedia('(prefers-reduced-motion: reduce)');
const byKey = (k, r = document) => r.querySelector('[data-key="' + CSS.escape(k) + '"]');
let skew = 0, inflight = 0, failures = 0, timer = 0, stash = null, ended = false, moving = 0;

const span = s => s < 60 ? s + ' s' : s < 3600 ? Math.floor(s / 60) + ' min'
  : s < 86400 ? Math.floor(s / 3600) + ' h' : Math.floor(s / 86400) + ' d';
const rel = s => s >= 0 ? span(s) + ' ago' : 'in ' + span(-s);
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
  const olog = $(':scope > pre.log', ob), flog = $(':scope > pre.log', fb);
  if (olog && flog && ob.children.length === 1 && fb.children.length === 1) {
    if (olog.textContent !== flog.textContent) olog.textContent = flog.textContent;
    return true;
  }
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
  for (const b of $$('[data-copy]')) b.hidden = false;
}

function apply(doc, nav) {
  const now = +doc.body.dataset.now;
  if (now) skew = now - Date.now() / 1000;
  for (const k of ['open', 'pace', 'jobs']) document.body.dataset[k] = doc.body.dataset[k] || '';
  if (nav) document.title = doc.title;
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
    if (!$('.body', el)) { if (!busy(old)) { old.replaceWith(el); if (!nav) play(el, 'arrived', 400); } continue; }
    if (sync(old, el, doc, before, added, changed)) old.dataset.sig = el.dataset.sig;
    old.hidden = el.hidden;
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
  // A new page takes focus where its work starts: the brief on home and New job, else the page, so j/k and the action
  // keys carry on from there.
  const brief = $('.pane textarea[name="brief"]');
  if (brief && brief.getClientRects().length && document.body.dataset.open !== 'home') brief.focus();
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
  // Start job answers at once: the job appears in the list as Starting while the harness adds it.
  const grp = $('.s-g-running'), brief = form.elements.namedItem('brief'), id = form.elements.namedItem('id');
  if (!grp || !brief) return null;
  const li = document.createElement('li'), a = document.createElement('div');
  li.className = 'row pending'; a.className = 'item';
  const dot = document.createElement('i'); dot.className = 'dot live pulse';
  const name = document.createElement('span'); name.className = 'name';
  name.textContent = brief.value.trim().split('\n')[0].slice(0, 80) || (id && id.value) || 'New job';
  const when = document.createElement('span'); when.className = 'when';
  const sub = document.createElement('span'); sub.className = 'sub'; sub.textContent = 'Starting…';
  a.append(dot, name, when, sub); li.append(a);
  grp.hidden = false;
  const empty = $('.side-empty'); if (empty) empty.hidden = true;
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
  if (ok && row && !landed) { const again = byKey(row.dataset.key); if (again) (again.querySelector('a.item') || again).focus({ preventScroll: true }); }
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

document.addEventListener('click', e => {
  for (const d of $$('details.act[open]')) if (!d.contains(e.target)) d.open = false;
  const c = e.target.closest && e.target.closest('[data-copy]');
  if (c) { copy(c); return; }
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
  const cmd = form.elements.namedItem('command'), kind = form.querySelector('input[name="kind"]:checked');
  if (cmd && kind) cmd.required = kind.value === 'local-shell';
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
  const card = t.closest && t.closest('.cards > .row');
  const act = k => (card && card.querySelector('[data-k="' + k + '"]')) || $('.pane [data-k="' + k + '"]');
  if (e.key === 'j' || e.key === 'ArrowDown') move(1);
  else if (e.key === 'k' || e.key === 'ArrowUp') move(-1);
  else if (e.key === '?' && keys) keys.showModal();
  else if (e.key === 'i' && $('.s-g-needs a.item')) go($('.s-g-needs a.item').href, true);
  else if (e.key === 'n' && $('[data-k="n"]')) {
    const f = $('.pane textarea[name="brief"]');
    if (f && f.getClientRects().length) f.focus(); else go($('[data-k="n"]').href, true);
  }
  else if (e.key === 't' && $('[data-k="t"]')) $('[data-k="t"]').focus();
  else if (/^[rahusclx]$/.test(e.key) && act(e.key)) activate(act(e.key));
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
setInterval(clock, 1000);
live('on');
timer = setTimeout(poll, pace());
})();
"""

# Plain words for each job state and the tone of its dot. An unknown state is shown as the harness names it.
_STATES = {
    "draft": ("New", "neutral"), "ready": ("Ready", "neutral"), "waiting": ("Waiting", "neutral"),
    "held": ("On hold", "warn"), "launching": ("Starting", "live"), "handoff": ("Launch by hand", "warn"),
    "running": ("Running", "live"), "stalled": ("Past its time limit", "warn"),
    "collecting": ("Checking the result", "live"), "done": ("Done", "good"), "failed": ("Failed", "bad"),
    "blocked": ("Blocked", "warn"), "capacity": ("No capacity", "warn"), "truncated": ("Cut short", "bad"),
    "aborted": ("Ended early", "bad"), "no_result": ("No result", "bad"), "cancelled": ("Cancelled", "neutral"),
}
_PRE = ("draft", "ready", "waiting", "held", "handoff")  # states rh job cancel accepts
_LIVE = ("launching", "handoff", "running", "stalled", "collecting")
_FAILED = ("failed", "blocked", "capacity", "truncated", "aborted", "no_result")  # a finished job that needs a person
_FAILURE_WINDOW = 86400  # a failure stays in Needs you this long, unless it was run again
# What happens next to a job the harness is moving, so a quiet page still says what it is doing.
_NEXT = {
    "draft": "Checking it now.",
    "ready": "It starts as soon as a slot {place} is free.",
    "waiting": "It starts by itself once what it waits for is ready.",
    "held": "It is rechecked on every pass. Fix what it needs, or cancel it.",
    "launching": "Starting it {place}.",
    "handoff": "It waits for you to launch it in Codex.",
    "stalled": "It has run past its time limit; it is still being checked.",
    "collecting": "Fetching and checking its result.",
}
# Platforms (routes and job kinds in the CLI) as people name them; anything else is shown as the harness names it.
_PLATFORMS = {"codex-cloud": "Codex cloud", "codex-cloud-cli": "Codex cloud", "codex-cloud-web": "Codex cloud",
              "local-shell": "This machine"}
_PLACES = {"codex-cloud": "in Codex cloud", "local-shell": "on this machine"}
_NEEDS = ("net", "python", "git", "loopback", "browser", "gpu")  # capabilities the composer offers
_STALE = 300  # seconds without a check before the status line says the checks may have stopped
_CODEX = "https://chatgpt.com/codex"  # the one page outside the app a live page links to, for a hand launch
_SETUP = "create and publish the cloud environment"  # how the supervisor's Codex cloud setup step begins
_KEYS = (("j / ↓", "Next job"), ("k / ↑", "Previous job"), ("Enter", "Open it"), ("n", "New job"),
         ("i", "Open the first job that needs you"), ("r", "Answer or mark done"), ("a", "Link a Codex task"),
         ("h", "Paste a patch"), ("u", "Resume"), ("s", "Pause"), ("c", "Cancel a job that has not started"),
         ("l", "Save a result to a branch"), ("t", "Check now"), ("⌘ / Ctrl Enter", "Send the form you are typing in"),
         ("Esc", "Close or leave a field"), ("?", "This sheet"))
_ENV = re.compile(r"in environment (\S+?),")


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


def _when(timestamp, now, key, pre="", due=None, short=False):
    """A relative time the live page keeps ticking; data-tk lets it update a row that did not otherwise change.
    `due` replaces a future time once it has passed ("retrying on the next check" rather than "8 s ago"); `short`
    gives just the span ("12 s"), for the job list."""
    marks = (f' data-pre="{_text(pre)}"' if pre else "") + (f' data-due="{_text(due)}"' if due else "")
    if short:
        marks += ' data-short="1"'
        text = _span(max(0, round(now - timestamp)))
    else:
        text = due if due and now >= timestamp else pre + _relative(timestamp, now)
    return f'<span class="when" data-t="{_text(timestamp)}" data-tk="{_text(key)}"{marks}>{_text(text)}</span>'


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _dot(tone, pulse=False):
    return f'<i class="dot {tone}{" pulse" if pulse else ""}" aria-hidden="true"></i>'


def _state(state):
    word, tone = _STATES.get(state, (state, "neutral"))
    return f'<span class="state">{_dot(tone, state == "running")}{_text(word)}</span>', tone


def _platform(route):
    name, _, env = str(route).partition(":")
    return f"Cloud environment {env}" if name == "cli-env" and env else _PLATFORMS.get(str(route), str(route))


def _sentence(text):
    text = str(text).strip()
    return text[:1].upper() + text[1:]


def _clause(text):
    return str(text).strip().rstrip(".")


def _plain(detail):
    """A state's detail as the harness wrote it, in the app's words: process ids and CLI hints left out."""
    text = str(detail or "").strip()
    rules = ((r"pid \d+", ""), (r"launched task_\w+", ""), (r"attached task_\w+", "Linked to its Codex task"),
             (r"recovered after restart: pid \d+", "Picked up again after a restart"),
             (r"harvested patch; .*", "Patch saved; checking it"),
             (r"task completed; collecting", "Finished in Codex; collecting the result"),
             (r"gate waiting for the heavy lease", "Waiting its turn to run the check"),
             (r"command and gate exited 0", "The command and its check passed"),
             (r"command exited (\d+)", r"The command exited with code \1"),
             (r"acceptance gate exited (\d+)", r"The check exited with code \1"),
             (r"launch deferred: (.*)", r"Waiting to start: \1"),
             (r"no finish after (\d+) min", r"Past its \1-minute limit"),
             (r"attached from the web composer", ""),
             (r".*rh cloud prompt .*", "Launch it in Codex by hand"))
    text = text.replace(" (operator step)", "")
    for pattern, words in rules:
        if re.fullmatch(pattern, text):
            return re.sub(pattern, words, text)
    return _sentence(re.sub(r"\s*\(?pid \d+\)?", "", text))


def _title(meta, jid):
    """A job's name in the list and on its page: the first sentence of its brief, else its id."""
    brief = str((meta or {}).get("brief") or "").strip()
    if not brief:
        return str(jid)
    line = brief.splitlines()[0].strip()
    line = re.split(r"(?<=[.!?])\s", line, maxsplit=1)[0].rstrip(".")
    if len(line) > 100:
        line = line[:98].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return line or str(jid)


def _size(n):
    if not _number(n):
        return ""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _duration(seconds):
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60} s" if seconds < 600 else f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min"


def _hidden(ctx, fields):
    back = [("next", ctx["back"])] if ctx.get("back") else []
    return "".join(f'<input type="hidden" name="{name}" value="{_text(value)}">'
                   for name, value in (("t", ctx["token"]), *fields, *back))


def _cls(kind):
    return f"btn {kind}" if kind else "btn"


def _url(ctx, path):
    return _text(f"{path}?t={ctx['token']}")


def _button(ctx, route, fields, label, key="", kind="", say=None):
    """A one-step action."""
    k = f' data-k="{key}"' if key else ""
    return (f'<form class="act" method="post" action="{route}" data-act data-label="{_text(say or label)}">'
            f'{_hidden(ctx, fields)}<button class="{_cls(kind)}"{k}>{label}</button></form>')


def _popover(ctx, route, fields, label, key, inputs, submit, kind="", note="", where=""):
    """An action that opens its form in place: the inputs, what submitting does, and the button that confirms."""
    confirm = "btn danger fill" if "danger" in kind.split() else "btn primary"
    note = f'<p class="note">{note}</p>' if note else ""
    k = f' data-k="{key}"' if key else ""
    return (f'<details class="act"><summary class="{_cls(kind)}"{k}>{label}…</summary>'
            f'<form class="pop{" " + where if where else ""}" method="post" action="{route}" data-act '
            f'data-label="{_text(submit)}">{_hidden(ctx, fields)}{inputs}{note}<button class="{confirm}">{submit}</button>'
            '</form></details>')


def _field(label, name, *, required=False, placeholder="", key="", mono=False, rows=0, value=""):
    attrs = (f' name="{name}"' + (" required" if required else "") + (f' data-k="{key}"' if key else "")
             + (f' placeholder="{_text(placeholder)}"' if placeholder else "") + (' class="mono" spellcheck="false"'
                                                                                 if mono else ""))
    box = (f'<textarea{attrs} rows="{rows}">{_text(value)}</textarea>' if rows
           else f'<input type="text"{attrs} autocomplete="off" value="{_text(value)}">')
    return f"<label>{label}{box}</label>"


def _acts(items):
    return f'<div class="acts">{"".join(items)}</div>' if items else ""


def _row(key, inner, cls="row", attrs=""):
    return f'<li class="{cls}" data-key="{_text(key)}" data-sig="{_sig(_still(inner))}"{attrs}>{inner}</li>'


def _codex_link(label="Open Codex"):
    return (f'<a class="btn" href="{_CODEX}" target="_blank" rel="noopener noreferrer">{label}'
            '<span aria-hidden="true">↗</span><span class="sr"> (opens a new tab)</span></a>')


def _copy(target, label):
    return f'<button type="button" class="btn small" data-copy="{_text(target)}" hidden>{label}</button>'


def _setup_item(item, ready):
    return (item.get("kind") == "operator-step" and not item.get("job") and not ready
            and str(item.get("action") or "").startswith(_SETUP))


def _ask_line(item):
    """(what to do, detail) for one inbox item, in plain words."""
    job, kind = item.get("job"), item.get("kind")
    action, detail = str(item.get("action") or ""), str(item.get("detail") or "")
    if kind == "web-launch" and job:
        return "Launch it in Codex, then link the task", ""
    if kind == "harvest" and job:
        return "Paste the patch from Codex", " ".join(part for part in (_sentence(action), detail) if part)
    if kind == "operator-step" and action.startswith(_SETUP):
        return "Set up Codex cloud", "Cloud jobs wait until Codex has an environment for this repository."
    if kind == "refusal":
        return f"The job stopped at a refusal: {_clause(action)}", detail
    return _sentence(action or kind), detail


class _Job:
    """Everything the app says about one job, worked out once: which group it belongs in, what it needs from a
    person, the pauses on it, and the actions it offers."""

    def __init__(self, row, where, ctx):
        self.row, self.where, self.ctx = row, where, ctx
        self.id, self.state = row["id"], row["state"]
        self.meta = ctx["meta"].get(self.id) or {}
        self.kind = self.meta.get("kind", "")
        self.word, self.tone = _STATES.get(self.state, (self.state, "neutral"))
        self.stops = ctx["scopes"].get(f"job:{self.id}", [])
        self.items = ctx["asks"].get(self.id, [])
        self.handoff = self.state == "handoff" and not any(i.get("kind") == "web-launch" for i in self.items)
        self.mine = [s for s in self.stops if s.get("owner") == "app"]
        ended = self.meta.get("updated")
        self.failure = (self.state in _FAILED and where == "recent" and self.id not in ctx["followed"]
                        and (not _number(ended) or ctx["now"] - ended < _FAILURE_WINDOW))
        self.held = self.state == "held" and not self.items
        self.setup = bool(ctx["setup"]) and self.kind == "codex-cloud" and self.state in _PRE
        self.count = len(self.items) + self.handoff + len(self.mine) + self.failure + self.held
        if self.count:
            self.group = "needs"
        elif where == "live" or self.state == "draft" or (self.state == "ready" and not row.get("detail")):
            self.group = "running"
        else:
            self.group = {"not_started": "queued", "recent": "done"}.get(where, "running")
        times = [i["created"] for i in self.items if _number(i.get("created"))]
        times += [s["created"] for s in self.mine if _number(s.get("created"))]
        self.first = min(times, default=float("inf"))
        self.title = _title(self.meta, self.id)

    def headline(self):
        """The one line the list shows under the title: what it needs when it needs you, else its state."""
        if self.items:
            return _ask_line(self.items[0])[0]
        if self.handoff:
            return "Launch it in Codex"
        if self.mine:
            return "Paused from the app"
        detail = _plain(self.row.get("detail"))
        parts = []
        held = [s for s in self.stops if s.get("owner") != "app"]
        if held:
            parts.append(f"{'Paused' if held[0].get('kind') == 'pause' else 'Stopped'} by {held[0]['owner']}")
        elif self.row.get("paused"):
            parts.append("Paused")
        if self.failure or self.held:
            return " · ".join(parts + [f"{self.word}: {detail}" if detail else self.word])
        if self.setup:
            return " · ".join(parts + ["Waits for Codex cloud to be set up"])
        if self.group == "running" and self.state in ("draft", "ready", "launching"):
            parts.append("Starting")
        elif self.state == "running" and self.kind in _PLACES:
            parts.append(f"Running {_PLACES[self.kind]}")
        else:
            parts.append(self.word)
        if detail and self.state not in ("draft", "ready", "launching"):
            parts.append(detail)
        return " · ".join(parts)

    def since(self):
        """The time the list shows: how long it has waited on you, run, or been finished."""
        if self.first != float("inf"):
            return self.first
        key = {"running": "started", "done": "updated", "needs": "updated"}.get(self.group, "created")
        stamp = self.meta.get(key) or self.meta.get("created")
        return stamp if _number(stamp) else None

    def item(self):
        """The list entry: a link to the job's page."""
        ctx = self.ctx
        since = self.since()
        when = _when(since, ctx["now"], f"since:{self.id}", short=True) if since is not None else '<span></span>'
        pulse = self.state == "running"
        dot = "bad" if self.failure else self.tone if self.group != "needs" else "warn"
        inner = (f'{_dot(dot, pulse)}<span class="name">{_text(self.title)}</span>{when}'
                 f'<span class="sub{" fail" if self.failure else ""}">{_text(self.headline())}</span>')
        link = (f'<a class="item" href="{_url(ctx, "/job/" + self.id)}" data-nav>{inner}</a>' if ctx["live"]
                else f'<div class="item">{inner}</div>')
        return _row(f"job:{self.id}", link, attrs=f' data-tone="{self.tone}"')


def _need_row(ctx, key, ask, detail, when, acts, cli, job=None, title=None, status="", listed=None):
    """One thing a person can settle on the home page, written as an instruction, beside what settles it. The
    snapshot has no buttons, so it prints the command instead. `listed` names the job whose row in the job list
    leads to the same place, so a phone, which shows that list on home, can leave this row out."""
    about = ""
    if job:
        name = _text(title or job)
        tail = f" · {_text(status)}" if status else ""
        about = (f'<p class="for">For <a href="{_url(ctx, "/job/" + job)}" data-nav>{name}</a>{tail}</p>'
                 if ctx["live"] else f'<p class="for">For {name}{tail}</p>')
    say = (f'<p class="ask">{ask}</p>' + (f'<p class="detail">{detail}</p>' if detail else "") + about
           + (when if when else "") + ("" if ctx["live"] else f'<p class="cli"><code>{_text(cli)}</code></p>'))
    inner = f'<div class="say">{say}</div>{_acts(acts) if ctx["live"] else ""}'
    return _row(key, inner, attrs=(f' data-job="{_text(job or listed)}"' if job or listed else "") + ' tabindex="-1"')


def _home_acts(ctx, item, lead, job=None):
    """The controls a home row offers for an inbox item; a launch or a patch is done on the job's page."""
    iid, kind = item["id"], item.get("kind")
    first = "primary" if lead else ""
    if kind in ("web-launch", "harvest") and job:
        label = "Launch" if kind == "web-launch" else "Paste patch"
        return [f'<a class="{_cls(first)}" href="{_url(ctx, "/job/" + job)}" data-nav data-k="'
                f'{"a" if kind == "web-launch" else "h"}">{label}…</a>']
    if kind == "decision":
        return [_popover(ctx, "/inbox/resolve", [("id", iid)], "Answer", "r",
                         _field("Your decision", "note", required=True, rows=3), "Record decision", first,
                         "Closes the question and keeps your answer with it.")]
    return [_button(ctx, "/inbox/resolve", [("id", iid), ("note", "")], "Mark done", "r", first, "Mark done")]


def _setup_card(ctx, view, lead=True):
    """Codex cloud set up from the app: the settings, a link to Codex, and the environment ID back."""
    spec = str(view.get("env_spec") or "").strip()
    first = "primary" if lead else ""
    if not ctx["live"]:
        return ('<div class="ask-card"><p class="label">' + _dot("warn") + 'Needs you</p><h3>Set up Codex cloud</h3>'
                '<p class="detail">Cloud jobs wait until Codex has an environment for this repository.</p>'
                '<p class="cli"><code>rh env spec</code> · <code>rh config set cloud.env_id &lt;ID&gt;</code></p></div>')
    settings = (f'<pre class="envspec" id="envspec" tabindex="0">{_text(spec)}</pre>{_copy("envspec", "Copy settings")}'
                if spec else '<p class="detail">The settings could not be read here; <code>rh env spec</code> prints '
                             'them.</p>')
    form = (f'<form method="post" action="/setup" data-act data-label="Save environment">{_hidden(ctx, [])}'
            '<label>Environment ID<input type="text" name="env_id" required autocomplete="off" spellcheck="false" '
            'class="mono" pattern="[A-Za-z0-9_.:\\-]{1,128}" data-k="r"></label>'
            f'<button class="{_cls(first)}">Save and start cloud jobs</button></form>')
    return ('<div class="ask-card setup"><p class="label">' + _dot("warn") + 'Needs you</p><h3>Set up Codex cloud</h3>'
            '<p class="detail">Cloud jobs wait until Codex has an environment for this repository. You do this once.'
            '</p><ol class="steps">'
            f'<li><div><p>Create an environment in Codex with these settings.</p>{settings}</div></li>'
            f'<li><div><p>Open Codex, create and publish it, then copy its ID.</p>{_codex_link()}</div></li>'
            f'<li><div>{form}</div></li></ol></div>')


def _launch_card(ctx, jid, opened, lead, foot=""):
    """A hand launch in order: copy the prompt, open Codex, paste the task link back."""
    first = "primary" if lead else ""
    if not ctx["live"]:
        return (f'<div class="ask-card"><p class="label">{_dot("warn")}Needs you</p><h3>Launch it in Codex</h3>'
                f'<p class="cli"><code>rh cloud prompt {_text(jid)}</code> · <code>rh cloud attach {_text(jid)} '
                f'&lt;task id&gt;</code></p>{foot}</div>')
    prompt = opened.get("prompt")
    env = _ENV.search(str((opened.get("job") or {}).get("detail") or ""))
    where = f" in environment <code>{_text(env.group(1))}</code>" if env else ""
    pid = f"prompt-{jid}"
    copy = (f'<textarea class="prompt mono" id="{_text(pid)}" rows="4" readonly>{_text(prompt)}</textarea>'
            f'{_copy(pid, "Copy prompt")}' if isinstance(prompt, str) and prompt.strip() else
            f'<p class="detail">The prompt could not be read here; <code>rh cloud prompt {_text(jid)}</code> prints it.'
            '</p>')
    attach = (f'<form method="post" action="/attach" data-act data-label="Link task">{_hidden(ctx, [("job", jid)])}'
              '<label>Paste the task link<input type="text" name="task_id" required autocomplete="off" '
              'spellcheck="false" class="mono" placeholder="https://chatgpt.com/codex/tasks/task_…" data-k="a">'
              f'</label><button class="{_cls(first)}">Link task</button></form>')
    return ('<div class="ask-card launch"><p class="label">' + _dot("warn") + 'Needs you</p>'
            '<h3>Launch it in Codex</h3><p class="detail">Codex cannot start this job from here. Start it in the Codex '
            'web app, then link the task so this page can follow it.</p><ol class="steps">'
            f'<li><div><p>Copy the prompt.</p>{copy}</div></li>'
            f'<li><div><p>Open Codex, start a task{where} and paste the prompt.</p>{_codex_link()}</div></li>'
            f'<li><div>{attach}</div></li></ol>{foot}</div>')


def _dismiss(ctx, iid):
    return _popover(ctx, "/inbox/resolve", [("id", iid)], "Dismiss", "x",
                    _field("Why dismiss it? (optional)", "note"), "Dismiss", "quiet",
                    "Closes this item without doing it.", "left")


def _ask_card(ctx, item, opened, lead, view):
    """One inbox item on the job's page, with the form that settles it written out."""
    iid, kind, jid = item["id"], item.get("kind"), item.get("job")
    first = "primary" if lead else ""
    if not ctx["live"] and not _setup_item(item, ctx["ready"]):
        ask, detail = _ask_line(item)
        return (f'<div class="ask-card"><p class="label">{_dot("warn")}Needs you</p><h3>{_text(ask)}</h3>'
                + (f'<p class="detail">{_text(detail)}</p>' if detail else "")
                + f'<p class="cli"><code>rh inbox resolve {_text(iid)} &lt;note&gt;</code></p></div>')
    if kind == "web-launch" and jid:
        return _launch_card(ctx, jid, opened, lead, f'<div class="foot">{_dismiss(ctx, iid)}</div>' if ctx["live"] else "")
    if _setup_item(item, ctx["ready"]):
        return _setup_card(ctx, view, lead)
    ask, detail = _ask_line(item)
    when = (_when(item["created"], ctx["now"], f"inbox:{iid}", "asked ") if _number(item.get("created")) else "")
    head = (f'<p class="label">{_dot("warn")}Needs you · {when}</p><h3>{_text(ask)}</h3>'
            + (f'<p class="detail">{_text(detail)}</p>' if detail else ""))
    if kind == "harvest" and jid:
        form = (f'<form method="post" action="/harvest" data-act data-label="Save patch">{_hidden(ctx, [("job", jid)])}'
                + _field("The patch or bundle reply, as copied", "text", required=True, key="h", mono=True, rows=8)
                + f'<button class="{_cls(first)}">Save patch</button></form>')
        return f'<div class="ask-card">{head}{form}<div class="foot">{_dismiss(ctx, iid)}</div></div>'
    if kind == "decision":
        inputs, submit = _field("Your decision", "note", required=True, key="r", rows=3), "Record decision"
    else:
        inputs = _field("What did you do? (optional)", "note", key="r")
        submit = "Mark handled" if kind == "refusal" else "Mark done"
    form = (f'<form method="post" action="/inbox/resolve" data-act data-label="{submit}">'
            f'{_hidden(ctx, [("id", iid)])}{inputs}<button class="{_cls(first)}">{submit}</button></form>')
    return f'<div class="ask-card">{head}{form}</div>'


def _pause(ctx, jid):
    return _popover(ctx, "/stop", [("job", jid), ("pause", "on")], "Pause job", "s",
                    _field("Why? (optional)", "reason", placeholder="Paused from the app"), "Pause job",
                    "quiet danger", "Nothing new starts for it until you resume it. Work already running continues.")


def _cancel(ctx, jid):
    return _popover(ctx, "/job/cancel", [("job", jid)], "Cancel job", "c", _field("Why?", "reason", required=True),
                    "Cancel job", "quiet danger", "It will not start. You can run it again later.")


def _resume_card(ctx, stop, lead, what, fields):
    pause = stop.get("kind") == "pause"
    verb = "Paused" if pause else "Stopped"
    fields = fields + ([("pause", "on")] if pause else [])
    when = (_when(stop["created"], ctx["now"], f"stop:{stop.get('id', stop['scope'])}", verb.lower() + " ")
            if _number(stop.get("created")) else "")
    return (verb, f"Nothing new starts for {what} until you resume it.", when,
            [_button(ctx, "/resume", fields, "Resume", "u", "primary" if lead else "", f"Resume {what}")])


def _hold(stop, running):
    """A pause someone else owns: shown where it applies, with who can undo it, but no button."""
    verb, owner = "Paused" if stop.get("kind") == "pause" else "Stopped", _text(stop["owner"])
    tail = "It keeps running; " if running else "Nothing new starts for it; "
    return (f'<p class="hold"><b>{verb} by {owner}:</b> {_text(_clause(stop["reason"]))}. '
            f'<span class="soft">{tail}only {owner} can undo it.</span></p>')


def _checks(view, ctx):
    """The status line at the foot of the sidebar: who checks the harness, when it last did, and Pause."""
    now, live = ctx["now"], ctx["live"]
    sup, loop = view.get("supervisor"), view.get("checks") if isinstance(view.get("checks"), dict) else None
    age = sup.get("age_s") if isinstance(sup, dict) else None
    counted = _number(age)
    quiet = counted and age > _STALE
    last = _when(now - age, now, "tick", "Last check ") if counted else ""
    mode = loop.get("mode") if loop else None
    btns, more, inline = [], last, ""
    if mode == "on":
        word, dot = ("Checking, but not lately", "warn") if quiet else ("Checking automatically", "live")
        btns = [_button(ctx, "/checks", [("on", "0")], "Pause", "", "quiet small", "Pause checks"),
                _button(ctx, "/tick", [], "Check now", "t", "quiet small", "Check now")]
    elif mode == "paused":
        word, dot, more = "Checks paused", "warn", "The app starts and collects nothing until you resume or press Check once."
        btns = [_button(ctx, "/checks", [("on", "1")], "Resume", "", "small", "Resume checks"),
                _button(ctx, "/tick", [], "Check once", "t", "quiet small", "Check once")]
    elif mode == "other":
        word, dot = f"Checked by rh supervise (process {loop.get('pid')})", "live"
        more = (last + ". " if last else "") + "This app watches while that process runs and takes over when it stops."
    elif sup:
        word, dot = "Harness", "warn" if quiet else "good"
        inline = " " + _when(now - age, now, "tick", "checked ") if counted else f" checked {_text(age)} s ago"
        more = f"Process {_text(sup.get('pid'))}." + (" It may have stopped." if quiet else "")
        if live:
            btns = [_button(ctx, "/tick", [], "Check now", "t", "quiet small", "Check now")]
    else:
        word, dot, more = "The harness has not run yet.", "warn", ""
        if live:
            btns = [_button(ctx, "/tick", [], "Check now", "t", "small", "Check now")]
    tone = dot if dot in ("warn", "bad") else ""
    out = [f'<p class="line {tone}"><i class="dot {dot} beat" aria-hidden="true"></i><span>{_text(word)}{inline}'
           '</span></p>']
    if loop and loop.get("failed"):
        out.append('<p class="line bad"><i class="dot bad" aria-hidden="true"></i><span>The last check failed</span></p>')
        more = _text(str(loop["failed"])[:300])
    if more:
        out.append(f'<p class="more">{more}</p>')
    for route in view.get("routes", []):
        name = route["route"]
        retry = _when(route["next_try"], now, f"route:{name}:next", "retrying ", "retrying on the next check")
        last = (f", the last {_when(route['updated'], now, f'route:{name}:updated')}"
                if _number(route.get("updated")) else "")
        pause = ""
        if live and not any(s.get("owner") == "app" for s in ctx["scopes"].get(f"route:{name}", [])):
            pause = _popover(ctx, "/stop", [("route", name), ("pause", "on")], "Pause it", "",
                             _field("Why? (optional)", "reason", placeholder="Paused from the app"),
                             f"Pause {_text(_platform(name))}", "quiet small danger",
                             f"No new job starts on {_text(_platform(name))} until you resume it.", "up left")
        out.append(f'<div class="route"><p>{_text(_platform(name))}: {_text(route["last_status"])} · {retry}. '
                   f'{_text(route["failures"])} failures in a row{last}; it retries by itself.</p>{pause}</div>')
    for stop in ctx["others"]:
        scope = str(stop["scope"])
        what = ("All launches" if scope == "*" else
                _text(_platform(scope[6:])) if scope.startswith("route:") else _text(scope))
        verb = "paused" if stop.get("kind") == "pause" else "stopped"
        out.append(f'<p class="route warn">{what} {verb} by {_text(stop["owner"])}: '
                   f'{_text(_clause(stop["reason"]))}</p>')
    if btns and live:
        out.append(f'<div class="btns">{"".join(btns)}</div>')
    body = "".join(out)
    return f'<div class="status" data-region="checks" data-sig="{_sig(_still(body))}">{body}</div>'


def _result(result):
    """The last action's result, as serve hands it over: {"code", "say", "output"}; a plain string is shown as is."""
    if isinstance(result, dict):
        code = result.get("code")
        say, output = str(result.get("say") or ""), str(result.get("output") or "")
    else:
        code, say, output = "", "", str(result)
    code = code if isinstance(code, int) and not isinstance(code, bool) else ""
    tone = "bad" if code not in ("", 0) else "good"
    shown = _text(say or ("Done." if code == 0 else "That did not work." if code != "" else "Last action"))
    details = f'<details><summary>Details</summary><pre>{_text(output)}</pre></details>' if output else ""
    return (f'<div id="result" class="result {tone}" role="status" data-code="{code}" data-say="{_text(say)}">'
            f'<p>{shown}</p>{details}</div>')


def _section(key, heading, body, head_extra="", cls="", hidden=False, h="h2"):
    """A keyed region the script reconciles: a head it can swap, and a body whose rows it can move."""
    head = f'<div class="head"><{h} id="{key}">{heading}</{h}>{head_extra}</div>'
    flag = " hidden" if hidden else ""
    return (f'<section class="s-{key}{" " + cls if cls else ""}" aria-labelledby="{key}" data-region="{key}" '
            f'data-key="section:{key}" data-sig="{_sig(key, hidden, _still(head + body))}"{flag}>{head}'
            f'<div class="body">{body}</div></section>')


def _rows(items, cls="rows"):
    return f'<ul class="{cls}">{"".join(items)}</ul>'


def _sidebar(jobs, loose, ctx):
    live = ctx["live"]
    groups = {"needs": [], "running": [], "queued": [], "done": []}
    for job in sorted(jobs, key=lambda j: j.first if j.group == "needs" else 0):
        groups[job.group].append(job.item())
    if loose:
        setup = ctx["setup"] is not None
        word = "question" if loose == 1 else "questions"
        title = "Set up Codex cloud" if setup else f"{loose} more {word}"
        sub = "Cloud jobs wait for it" if setup else "Not tied to a job; they are on the home page"
        inner = f'{_dot("warn")}<span class="name">{title}</span><span></span><span class="sub">{sub}</span>'
        # Setup is done on a waiting cloud job's page; other loose questions are answered on the home page.
        target = next((j for j in jobs if j.setup), None) if setup else None
        link = (f'<a class="item" href="{_url(ctx, "/job/" + target.id if target else "/")}" data-nav>{inner}</a>'
                if live
                else f'<div class="item">{inner}</div>')
        groups["needs"].insert(0, _row("loose", link))
    out = []
    for key, heading in (("needs", "Needs you"), ("running", "Running"), ("queued", "Queued"), ("done", "Done")):
        items = groups[key]
        n = ctx["count"] if key == "needs" else len(items)
        count = f'<span class="count">{n}</span>' if n else ""
        out.append(_section(f"g-{key}", heading, _rows(items), count, "group", hidden=not items))
    none = not jobs and not loose
    empty = ('<p class="side-empty" data-region="side-empty" data-sig="'
             f'{_sig(none)}"{"" if none else " hidden"}>No jobs yet.</p>')
    return f'<nav class="side" aria-label="Jobs">{empty}{"".join(out)}</nav>'


def _composer(ctx, draft, ready, first):
    """The home page's one control: what the job should do, where it runs, its name, and the rest folded away."""
    draft = draft if isinstance(draft, dict) else {}
    value = lambda name: _text(draft.get(name, ""))  # noqa: E731
    kind = draft.get("kind") or ("codex-cloud" if ready else "local-shell")
    picked = {k: " checked" if kind == k else "" for k in ("local-shell", "codex-cloud")}
    have = set(draft.get("needs", [])) if isinstance(draft.get("needs"), list) else set()
    needs = "".join(f'<label><input type="checkbox" name="needs" value="{n}"{" checked" if n in have else ""}>{n}</label>'
                    for n in _NEEDS)
    timeout = value("timeout_min") or "120"
    later = "" if ready else "<small>set up first</small>"
    form = (f'<form class="compose" method="post" action="/job" data-act data-label="Start job">{_hidden(ctx, [])}'
            '<h1 id="compose-title">What should run?</h1>'
            '<div class="box"><label class="sr" for="brief">What should the job do?</label>'
            '<textarea id="brief" name="brief" rows="3" required placeholder="Describe the job. For example: list '
            'every TODO in the notes and write them to todo.md">'
            f'{value("brief")}</textarea>'
            '<div class="cmd"><label for="command">Command</label><input type="text" id="command" name="command" '
            f'class="mono" autocomplete="off" spellcheck="false" placeholder="sh -c \'date &gt; today.txt\'" '
            f'value="{value("command")}"><small>Runs in runs/&lt;name&gt;/, so write plain file names. Pipes and &gt; need sh -c, as shown.'
            '</small></div>'
            '<div class="bar"><fieldset class="where"><legend class="sr">Runs on</legend>'
            f'<label class="chip"><input type="radio" name="kind" value="local-shell"{picked["local-shell"]}>'
            'This machine</label>'
            f'<label class="chip"><input type="radio" name="kind" value="codex-cloud"{picked["codex-cloud"]}>'
            f'Codex cloud{later}</label></fieldset>'
            '<label class="name"><span>Name</span><input type="text" name="id" required autocomplete="off" '
            'spellcheck="false" class="mono" pattern="[a-z0-9][a-z0-9._\\-]{0,62}" placeholder="todo-scan" '
            f'aria-label="Job name" value="{value("id")}"></label>'
            '<button class="btn primary send">Start job</button></div></div>'
            '<div class="under"><details class="opts"><summary class="btn quiet small">Options</summary>'
            '<div class="opts-body">'
            '<label><span>Done when <span class="soft">· a command that exits 0 when the job worked</span></span>'
            f'<input type="text" name="accept" autocomplete="off" spellcheck="false" class="mono" '
            f'placeholder="test -s todo.md" value="{value("accept")}"></label>'
            f'<fieldset><legend>It needs</legend><div class="needs">{needs}</div></fieldset>'
            '<label>Time limit, minutes<input type="text" name="timeout_min" inputmode="numeric" '
            f'pattern="[0-9]{{1,5}}" value="{timeout}"></label></div></details>'
            '<p class="note">Starts on the next check, usually within seconds. Nothing is pushed.</p></div></form>')
    if first:
        form += ('<div class="how"><ol class="steps">'
                 '<li><div><p><b>Describe a job and start it.</b> A job on this machine runs one command in its own '
                 'folder.</p></div></li>'
                 '<li><div><p><b>Watch it here.</b> Its page shows the output as it runs and the files it made when '
                 'it is done.</p></div></li>'
                 '<li><div><p><b>Answer when it asks.</b> A question or a step only you can take shows up under '
                 'Needs you.</p></div></li></ol></div>')
    body = f'<div class="body">{form}</div>'
    return (f'<section class="s-compose home-top" aria-labelledby="compose-title" data-region="compose" '
            f'data-key="section:compose" data-sig="{_sig("compose", body)}">{body}</section>')


def _event(ev, now, kind):
    """One line of what happened to the job, in plain words, or None for a line that says nothing new."""
    what, data = str(ev.get("kind", "")), ev.get("data") if isinstance(ev.get("data"), dict) else {}
    tone = "neutral"
    if what == "state":
        to, detail = data.get("to"), _plain(data.get("detail"))
        word, tone = _STATES.get(to, (to, "neutral"))
        if to == "draft" or (to == "launching" and not detail):
            return None
        if to == "running":
            text = detail or f"Started {_PLACES.get(kind, '')}".strip()
        elif to == "ready":
            if not detail:
                return None
            text = f"Ready: {detail}"
        elif to == "handoff":
            return None
        elif to == "collecting":
            text = detail or "Checking the result"
        elif to in _STATES and to not in _PRE and to not in _LIVE:
            text = str(word)
        else:
            text = f"{word}: {detail}" if detail else str(word)
        tone = "neutral" if tone == "live" and to != "running" else tone
    elif what == "job-added":
        text = f"Added to run {_PLACES.get(data.get('kind', ''), 'on ' + _platform(data.get('kind', '')))}"
    elif what == "inbox":
        text, tone = f"Asked you: {_sentence(_ask_line({**data, 'job': ev.get('job')})[0])}", "warn"
    elif what == "harvested":
        text = f"Patch saved from the {data.get('source', 'web view')}"
    elif what == "landed":
        text, tone = f"Saved to branch {data.get('branch', '')}", "good"
    elif what == "job-error":
        text, tone = f"Harness error: {data.get('error', '')}", "bad"
    elif what in ("prompt", "notify"):
        return None
    else:
        extra = ", ".join(f"{k} {v}" for k, v in sorted(data.items()) if isinstance(v, (str, int, float)))[:200]
        text = _sentence(what.replace("-", " ")) + (f": {extra}" if extra else "")
    when = _when(ev["ts"], now, f"ev:{ev.get('seq')}") if _number(ev.get("ts")) else ""
    inner = f'{_dot(tone)}<p>{_text(text)}</p>{when}'
    return text, _row(f"ev:{ev.get('seq', '')}", inner, attrs=f' data-tone="{tone}"')


def _receipt(ctx, opened, job, spec, lead):
    """Where a finished job's transcript ends: the verdict, its checks, the files it made, and its stamp."""
    jid, state = job["id"], job.get("state")
    receipt = opened.get("receipt") if isinstance(opened.get("receipt"), dict) else {}
    word, tone = _STATES.get(state, (state, "neutral"))
    started, ended = receipt.get("started") or job.get("started"), receipt.get("ended") or job.get("updated")
    took = f" in {_duration(ended - started)}" if _number(started) and _number(ended) and state == "done" else (
        f" after {_duration(ended - started)}" if _number(started) and _number(ended) else "")
    why = _plain(receipt.get("detail") or job.get("detail"))
    tick = {"good": "✓", "bad": "✕"}.get(tone, "•")
    ended_at = _when(ended, ctx["now"], "j:ended", "finished ") if _number(ended) else ""
    parts = [f'<div class="verdict {tone}"><p class="big"><span class="tick" aria-hidden="true">{tick}</span>'
             f'<span>{_text(word)}{took}.</span></p>'
             + (f'<p class="why">{_text(why)}.</p>' if why and why != word else "")
             + (f'<p class="why">{ended_at}</p>' if ended_at else "") + '</div>']
    checks = [c for c in receipt.get("checks", []) if isinstance(c, dict)] if isinstance(receipt.get("checks"),
                                                                                          list) else []
    argv = spec.get("command")
    if isinstance(argv, list) and all(isinstance(a, str) for a in argv):
        # The harness records the command space-joined, which loses its quotes; show what can be pasted and run.
        checks = [dict(c, cmd=shlex.join(argv)) if c.get("cmd") == " ".join(argv) else c for c in checks]
    if checks:
        lines = "".join(f'<li><span class="{"ok" if c.get("exit") == 0 else "bad"}" aria-hidden="true">'
                        f'{"✓" if c.get("exit") == 0 else "✕"}</span><code>{_text(c.get("cmd", ""))}</code>'
                        f'<span class="exit">exit {_text(c.get("exit"))}</span></li>' for c in checks)
        parts.append(f'<div class="part"><h2>Checks</h2><ul class="checklist">{lines}</ul></div>')
    files = opened.get("files") if isinstance(opened.get("files"), list) else []
    if files:
        rows = []
        for f in files:
            path = str(f.get("path", ""))
            name = (f'<a href="{_text("/job/" + jid + "/file?p=" + quote(path) + "&t=" + ctx["token"])}" '
                    f'target="_blank" rel="noopener">{_text(path)}</a>' if ctx["live"] else _text(path))
            rows.append(f'<li><span class="mono">{name}</span><span class="size">{_text(_size(f.get("size")))}</span>'
                        '</li>')
        where = _text(spec.get("write_root") or "")
        parts.append(f'<div class="part"><h2>Files in {where}</h2><ul class="files">{"".join(rows)}</ul>')
        peek = opened.get("peek") if isinstance(opened.get("peek"), dict) else None
        if peek:
            more = '\n…' if peek.get("more") else ""
            caption = f'<p class="note">{_text(peek.get("path", ""))}</p>' if len(files) > 1 else ""
            parts[-1] += f'{caption}<pre class="peek" tabindex="0">{_text(peek.get("text", ""))}{more}</pre>'
        parts[-1] += "</div>"
    elif state == "done" and spec.get("kind") == "local-shell":
        parts.append(f'<div class="part"><p class="soft">It left no files in {_text(spec.get("write_root", ""))}.</p>'
                     '</div>')
    tail = receipt.get("log_tail") if isinstance(receipt.get("log_tail"), list) else []
    if tail and not (isinstance(opened.get("log"), str) and opened["log"].strip()) and tone == "bad":
        parts.append(f'<div class="part"><h2>Last lines of output</h2><pre class="peek">'
                     f'{_text(chr(10).join(map(str, tail[-12:])))}</pre></div>')
    stamp = [_platform(receipt.get("route") or job.get("kind") or spec.get("kind", ""))]
    if job.get("pinned_sha"):
        stamp.append(str(job["pinned_sha"])[:7])
    if receipt.get("model_served"):
        stamp.append(f"model {receipt['model_served']}")
    if receipt.get("diff_sha256"):
        stamp.append(f"diff {str(receipt['diff_sha256'])[:12]}")
    stamp.append(jid)
    parts.append(f'<p class="stamp mono">{" · ".join(_text(s) for s in stamp)}</p>')
    if ctx["live"] and state == "done" and spec.get("kind") == "codex-cloud":
        parts.append(_acts([_button(ctx, "/land", [("job", jid)], "Save to branch", "l", "primary" if lead else "",
                                    "Save to branch")]))
    body = "".join(parts)
    return (f'<section class="receipt" aria-label="Result" data-region="j-now" data-sig="{_sig(_still(body))}">'
            f'{body}</section>')


def _job_pane(opened, jobs, ctx, feedback, view):
    """The open job, read top to bottom: what it was asked, what happened, its output, what it needs from you, and
    the receipt it ends on."""
    now = ctx["now"]
    job = opened.get("job") if isinstance(opened.get("job"), dict) else {}
    jid = str(job.get("id") or opened.get("id") or "")
    back = (f'<a class="btn quiet back" href="{_url(ctx, "/")}" data-nav>‹ Jobs</a>' if ctx["live"] else "")
    if not job:
        body = (f'{back}<section class="s-missing" data-region="j-missing" data-sig="{_sig(jid)}"><h1>No job named '
                f'{_text(jid)}</h1><p class="soft">It may have been a typo in the address. The jobs the harness knows '
                'are in the list.</p></section>')
        return "job:" + jid, body, jid
    spec = job.get("spec") if isinstance(job.get("spec"), dict) else {}
    kind = job.get("kind") or spec.get("kind", "")
    entry = next((j for j in jobs if j.id == jid), None)
    if entry is None:
        row = {"id": jid, "state": job.get("state"), "paused": False, "detail": job.get("detail", ""),
               "external_id": job.get("external_id")}
        where = ("live" if job.get("state") in _LIVE else "not_started" if job.get("state") in _PRE else "recent")
        entry = _Job(row, where, ctx)
    state = job.get("state")
    finished = entry.where == "recent" or state in ("done", "cancelled") or state in _FAILED
    title = _title(spec, jid)
    status, tone = _state(state)
    facts = [status, f'<span class="mono">{_text(jid)}</span>', f"<span>{_text(_platform(kind))}</span>"]
    if _number(job.get("started")) and not finished:
        facts.append(_when(job["started"], now, "j:started", "started "))
    elif not finished and _number(job.get("created")):
        facts.append(_when(job["created"], now, "j:added", "added "))
    ext = job.get("external_id")
    if ext is not None and not str(ext).isdigit():
        facts.append(f"<span>Codex task {_text(ext)}</span>")
    if spec.get("prior"):
        prior = _text(spec["prior"])
        facts.append(f'<span>Runs again after <a href="{_url(ctx, "/job/" + str(spec["prior"]))}" data-nav>{prior}</a>'
                     '</span>' if ctx["live"] else f"<span>Runs again after {prior}</span>")
    acts = []
    if ctx["live"] and not finished and not entry.mine:
        acts.append(_pause(ctx, jid))
    if ctx["live"] and state in _PRE and not finished:
        acts.append(_cancel(ctx, jid))
    head_body = (f'<div><h1 tabindex="-1">{_text(title)}</h1><p class="facts">{"".join(facts)}</p></div>'
                 f'{_acts(acts)}')
    parts = [back, f'<header class="job-head" data-region="j-head" data-sig="{_sig(_still(head_body))}">{head_body}'
                   '</header>', feedback]
    # What it was asked, as the person wrote it, with the rest of its contract folded away.
    rows = []
    if spec.get("command"):
        rows.append(("Command", f'<code>{_text(shlex.join(map(str, spec["command"])))}</code>'))
    if spec.get("accept"):
        rows.append(("Done when", f'<code>{_text(spec["accept"])}</code>'))
    if spec.get("needs"):
        rows.append(("Needs", _text(", ".join(map(str, spec["needs"])))))
    if spec.get("write_root"):
        rows.append(("Writes to", f'<code>{_text(spec["write_root"])}</code>'))
    if spec.get("timeout_min"):
        rows.append(("Time limit", f"{_text(spec['timeout_min'])} min"))
    if spec.get("changed") and spec.get("prior"):
        rows.append(("Changed", _text(spec["changed"])))
    details = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
    said = str(spec.get("brief") or "").strip()
    bubble = "" if said.rstrip(".") == title else f'<div class="bubble">{_text(said)}</div>'
    brief = (f'<div class="ask-bubble">{bubble}'
             + (f'<details class="spec-more"><summary>Details</summary><dl class="spec">{details}</dl></details>'
                if details else "") + '</div>')
    talk = [f'<section class="s-j-brief" aria-label="What it was asked" data-region="j-brief" '
            f'data-sig="{_sig(brief)}">{brief}</section>']
    lines, seen = [], None
    for ev in [e for e in opened.get("events", []) if isinstance(e, dict)][-60:]:
        made = _event(ev, now, kind)
        if made and made[0] != seen:
            seen = made[0]
            lines.append(made[1])
    if lines:
        talk.append(_section("j-timeline", "What happened", _rows(lines, "rows timeline")))
    log = opened.get("log")
    if isinstance(log, str) and log.strip():
        tag = (f'<span class="livetag">{_dot("live", True)}live</span>' if state == "running" else "")
        cls = "log" + (" ended" if finished else "")
        talk.append(_section("j-output", "Output", f'<pre class="{cls}" tabindex="0">{_text(log)}</pre>', tag))
    # What it needs from you, each with the form that settles it.
    cards = []
    lead = True
    for item in entry.items:
        cards.append(_ask_card(ctx, item, opened, lead, view))
        lead = False
    if entry.handoff:
        cards.append(_launch_card(ctx, jid, opened, lead))
        lead = False
    if entry.setup and not any(_setup_item(i, ctx["ready"]) for i in entry.items):
        cards.append(_setup_card(ctx, view, lead))
        lead = False
    for stop in entry.mine:
        verb, detail, when, act = _resume_card(ctx, stop, lead, "this job", [("job", jid)])
        cards.append(f'<div class="ask-card"><p class="label">{_dot("warn")}Needs you · {when}</p>'
                     f'<h3>{verb} from the app: {_text(_clause(stop["reason"]))}</h3><p class="detail">{detail}</p>'
                     f'{_acts(act) if ctx["live"] else ""}</div>')
        lead = False
    if entry.held:
        cards.append(f'<div class="ask-card"><p class="label">{_dot("warn")}Needs you</p>'
                     f'<h3>On hold: {_text(_plain(job.get("detail")))}</h3>'
                     f'<p class="detail">{_NEXT["held"]}</p></div>')
    holds = [_hold(s, entry.where == "live") for s in entry.stops if s.get("owner") != "app"]
    if entry.row.get("paused") and not any(s.get("kind") == "pause" for s in entry.stops):
        holds.append('<p class="hold"><b>Paused:</b> its platform or all launches are paused.</p>')
    if cards:
        body = "".join(cards)
        talk.append(f'<section class="s-j-asks talk" aria-label="Needs you" data-region="j-asks" '
                    f'data-sig="{_sig(_still(body))}">{body}</section>')
    if finished:
        talk.append(_receipt(ctx, opened, job, spec, lead))
    else:
        loop = view.get("checks") if isinstance(view.get("checks"), dict) else None
        doing = (f"Running {_PLACES[kind]}" if state == "running" and kind in _PLACES else entry.word)
        next_line = ("It starts once Codex cloud is set up." if entry.setup
                     else _NEXT.get(state, "").replace("{place}", _PLACES.get(kind, "on its platform")))
        if loop and loop.get("mode") == "paused":
            next_line = (next_line + " Automatic checks are paused, so nothing moves until you resume them.").strip()
        detail = _plain(job.get("detail")) if state not in ("held", "handoff") else ""
        now_body = (f'<p class="now">{_dot(tone, state == "running")}<span class="what">{_text(doing)}</span>'
                    + (f'<span class="soft">{_text(detail)}</span>' if detail and detail != doing else "")
                    + (f'<span class="soft">{_text(next_line)}</span>' if next_line else "") + "</p>" + "".join(holds))
        if state != "running" or next_line or (detail and detail != doing) or holds:
            talk.append(f'<section class="s-j-now" aria-label="Now" data-region="j-now" '
                        f'data-sig="{_sig(_still(now_body))}">{now_body}</section>')
    if finished and holds:
        talk.append("".join(holds))
    if finished and ctx["live"]:
        again = state != "done" or spec.get("kind") != "codex-cloud"
        form = (f'<form class="reply" method="post" action="/again" data-act data-label="Run again">'
                f'{_hidden(ctx, [("job", jid)])}<label class="sr" for="changed">What should change?</label>'
                '<textarea id="changed" name="changed" rows="1" placeholder="Ask for a change, or leave this empty to '
                'run it again as it was"></textarea>'
                f'<button class="{_cls("primary" if lead and again and state != "done" else "")}">Run again</button>'
                '</form>')
        talk.append(f'<section class="s-j-reply" aria-label="Run again" data-region="j-reply" '
                    f'data-sig="{_sig(form)}">{form}</section>')
    parts.append(f'<div class="talk">{"".join(talk)}</div>')
    return "job:" + jid, "".join(parts), title


def _home_pane(jobs, loose_rows, view, ctx, feedback):
    """Home: what needs you, then the composer when the server can add a job. The snapshot adds how the day went."""
    rows = []
    for j in jobs:
        if j.group != "needs":
            continue
        if j.items:
            item = j.items[0]
            ask, detail = _ask_line(item)
            when = (_when(item["created"], ctx["now"], f"inbox:{item['id']}", "asked ")
                    if _number(item.get("created")) else "")
            acts = _home_acts(ctx, item, False, j.id)
            cli = {"web-launch": f"rh cloud attach {j.id} <task id>",
                   "harvest": f"rh cloud harvest {j.id} <saved file>"}.get(item.get("kind"),
                                                                           f"rh inbox resolve {item['id']} <note>")
            more = len(j.items) - 1
            if more:
                detail = (detail + " " if detail else "") + f"And {more} more on its page."
        elif j.handoff:
            ask, detail, when, cli = "Launch it in Codex, then link the task", "", "", f"rh cloud attach {j.id} <task id>"
            acts = [f'<a class="btn" href="{_url(ctx, "/job/" + j.id)}" data-nav data-k="a">Launch…</a>']
        elif j.mine:
            verb, detail, when, acts = _resume_card(ctx, j.mine[0], False, "this job", [("job", j.id)])
            ask, cli = f"{verb} from the app: {_clause(j.mine[0]['reason'])}", f"rh resume --owner app --job {j.id}"
        elif j.failure:
            ask = f"{j.word}: {_plain(j.row.get('detail'))}" if j.row.get("detail") else j.word
            detail, when, cli = "", "", f"rh job show {j.id}"
            acts = [_button(ctx, "/again", [("job", j.id), ("changed", "")], "Run again", "r", "", "Run again"),
                    f'<a class="btn quiet" href="{_url(ctx, "/job/" + j.id)}" data-nav>Open</a>']
        else:
            ask, detail, when, cli = f"On hold: {_plain(j.row.get('detail'))}", _NEXT["held"], "", f"rh job show {j.id}"
            acts = [f'<a class="btn" href="{_url(ctx, "/job/" + j.id)}" data-nav>Open</a>']
        holds = "".join(_hold(s, j.where == "live") for s in j.stops if s.get("owner") != "app")
        status = (" · ".join(x for x in ("Paused" if j.row.get("paused") else "", j.word, _plain(j.row.get("detail")))
                             if x) if j.items else "")
        rows.append((j.first, _need_row(ctx, f"card:{j.id}", _text(ask), _text(detail) + holds, when, acts, cli,
                                        j.id, j.title, status)))
    setup = ctx["setup"]
    if setup is not None:
        # The setup itself is done on a waiting cloud job's page, where the job it unblocks is in view.
        target = next((j for j in jobs if j.setup), None)
        acts = ([f'<a class="btn" href="{_url(ctx, "/job/" + target.id)}" data-nav data-k="r">Set up…</a>']
                if target else [])
        when = (_when(setup["created"], ctx["now"], f"inbox:{setup['id']}", "asked ")
                if _number(setup.get("created")) else "")
        rows.append((setup.get("created") if _number(setup.get("created")) else float("inf"),
                     _need_row(ctx, f"inbox:{setup['id']}", "Set up Codex cloud",
                               "Cloud jobs wait until Codex has an environment for this repository.", when, acts,
                               "rh env spec, then rh config set cloud.env_id <ID>", listed=target and target.id)))
    rows = [html for _, html in sorted(rows + loose_rows, key=lambda pair: pair[0])]
    count = ctx["count"]
    parts = [feedback]
    if ctx["live"] and ctx["can_add"]:
        parts.append(_composer(ctx, view.get("compose"), ctx["ready"], not jobs))
    if rows:
        parts.append(_section("needs", "Needs you", _rows(rows, "rows cards"), f'<span class="count">{count}</span>'))
    elif not ctx["live"]:
        sup = view.get("supervisor")
        checked = (f'The harness checked {_when(ctx["now"] - sup["age_s"], ctx["now"], "tick:empty")}.'
                   if sup and _number(sup.get("age_s")) else "The harness has not run yet." if not sup else "")
        parts.append(_section("needs", "Needs you", f'<p class="soft">Nothing needs you. {checked}</p>'))
    if not ctx["live"]:
        day = ", ".join(f"{_text(n)} {_text(_STATES[s][0].lower() if s in _STATES else s)}"
                        for s, n in sorted(view.get("last_24h", {}).items()))
        groups = {g: sum(1 for j in jobs if j.group == g) for g in ("running", "queued")}
        parts.append(_section("today", "Today", f'<p>{groups["running"]} running · {groups["queued"]} queued · '
                                                f'{len(view.get("recent", []))} recently finished</p>'
                                                f'<p class="soft">Last 24 h: {day or "nothing finished"}</p>'))
    return "".join(parts)


def render(view: dict, now: float, token: str | None = None, result=None) -> str:
    """Return one self-contained HTML5 page, escaping all supplied text. Without a token it is the static snapshot:
    no script and no controls. With one it is the live app, with forms and <script src="/app.js">. The view may also
    carry "open" (the job `rh job show` returned, plus "log", its output tail, "prompt" for a hand launch, and
    "files" and "peek" for a finished job's folder; or {"id", "job": None} for a job the harness does not know),
    "can_add" (the server takes POST /job), "new" (show the composer on its own page), "compose" (a draft to refill it
    with), "meta" (each job's brief, kind and times), "checks" (the app's check loop: mode, pid, failed),
    "cloud_ready" and "env_spec" (for Codex cloud setup). `result` is serve's {"code", "say", "output"}."""
    live = token is not None
    opened = view.get("open") if isinstance(view.get("open"), dict) else None
    can_add = live and bool(view.get("can_add"))
    compose = can_add and bool(view.get("new")) and not opened
    scopes, inbox = {}, view.get("inbox", [])
    for stop in view.get("stops", []):
        scopes.setdefault(str(stop["scope"]), []).append(stop)
    rows = [(row, where) for where in ("live", "not_started", "recent") for row in view.get(where, [])]
    known = {row["id"] for row, _ in rows}
    meta = view.get("meta") if isinstance(view.get("meta"), dict) else {}
    ready = bool(view.get("cloud_ready"))
    asks, setup = {}, None
    for item in inbox:
        if _setup_item(item, ready):
            setup = setup or item
        elif item.get("job") in known:
            asks.setdefault(item["job"], []).append(item)
    # Every form on a job's page, or on the New job page, returns there; elsewhere forms return to home.
    named = opened["job"]["id"] if opened and isinstance(opened.get("job"), dict) else None
    back = f"/job/{named}" if named else "/new" if compose else None
    ctx = {"token": token, "live": live, "now": now, "scopes": scopes, "asks": asks, "can_add": can_add,
           "back": back, "meta": meta, "ready": ready, "setup": setup,
           "followed": {str(m.get("prior")) for m in meta.values() if isinstance(m, dict) and m.get("prior")},
           "others": [s for s in view.get("stops", []) if s.get("owner") != "app"
                      and not str(s["scope"]).startswith("job:")]}
    jobs = [_Job(row, where, ctx) for row, where in rows]
    # Needs with no job of their own: inbox items for other jobs or none, and the app's own pauses on platforms, on
    # every launch, or on jobs no longer listed.
    loose = []
    for item in inbox:
        if item.get("job") in known or item is setup:
            continue
        ask, detail = _ask_line(item)
        when = (_when(item["created"], now, f"inbox:{item['id']}", "asked ") if _number(item.get("created")) else "")
        about = f" For job {_text(item['job'])}." if item.get("job") else ""
        loose.append((item.get("created") if _number(item.get("created")) else float("inf"),
                      _need_row(ctx, f"inbox:{item['id']}", _text(ask), _text(detail) + about, when,
                                _home_acts(ctx, item, False), f"rh inbox resolve {item['id']} <note>")))
    for stop in view.get("stops", []):
        scope = str(stop["scope"])
        name, _, value = scope.partition(":")
        if stop.get("owner") != "app" or (name == "job" and value in known):
            continue
        if scope == "*":
            what, fields = "any job", []
        elif name == "route":
            what, fields = _platform(value), [("route", value)]
        else:
            what, fields = value or name, [(name, value)] if value else []
        verb, detail, when, acts = _resume_card(ctx, stop, False, _text(what), fields)
        loose.append((stop.get("created") if _number(stop.get("created")) else float("inf"),
                      _need_row(ctx, f"stop:{stop.get('id', scope)}", f"{verb} from the app: "
                                f"{_text(_clause(stop['reason']))}", detail, when, acts,
                                f"rh resume --owner app --{name} {value}".strip())))
    ctx["count"] = sum(j.count for j in jobs) + len(loose) + (setup is not None)
    feedback = _result(result) if live and result else ""
    if opened:
        page, pane, name = _job_pane(opened, jobs, ctx, feedback, view)
        title = f"{name} · Research harness"
    elif compose:
        page, title = "new", "New job · Research harness"
        back_link = f'<a class="btn quiet back" href="{_url(ctx, "/")}" data-nav>‹ Jobs</a>'
        pane = back_link + feedback + _composer(ctx, view.get("compose"), ready, False)
    else:
        page, title = "home", "Research harness"
        pane = _home_pane(jobs, loose, view, ctx, feedback)
    side = _sidebar(jobs, len(loose) + (setup is not None), ctx)
    fast = any(j.state in ("draft", "launching", "collecting") or (j.state == "running" and j.kind == "local-shell")
               or (j.state == "ready" and not j.row.get("detail")) for j in jobs)
    loop = view.get("checks") if isinstance(view.get("checks"), dict) else None
    pace = "fast" if fast and loop and loop.get("mode") == "on" else "calm"
    if live:
        head = ('<noscript><meta http-equiv="refresh" content="30"></noscript>\n'
                '<script src="/app.js" defer></script>\n')
        new = (f'<a class="btn new" href="{_url(ctx, "/new")}" data-nav data-k="n"><span class="plus" '
               'aria-hidden="true">+</span>New job</a>' if can_add else "")
        tools = ('<div class="tools"><button type="button" class="btn quiet small" data-keys hidden>Shortcuts '
                 '<kbd>?</kbd></button><span class="conn" data-live data-state="on" role="status"></span></div>')
        keys = "".join(f"<dt><kbd>{k}</kbd></dt><dd>{v}</dd>" for k, v in _KEYS)
        extra = ('<div class="toasts" aria-live="polite"></div>'
                 '<dialog id="keys" aria-label="Keyboard shortcuts">'
                 f'<p class="title" tabindex="-1" autofocus>Keyboard</p><dl>{keys}</dl>'
                 f'<div class="foot"><a href="{_url(ctx, "/actions")}">Every action as a plain form</a>'
                 '<form method="dialog"><button class="btn">Close</button></form></div></dialog>\n')
    else:
        head, extra, new = "", "", ""
        tools = '<div class="tools"><span class="soft">Snapshot</span></div>'
    top = (f'<div class="side-top" data-region="side-top" data-sig="{_sig(new)}"><p class="brand">'
           f'<span class="mark" aria-hidden="true"></span>Research harness</p>{new}</div>')
    foot = f'<footer class="checks">{_checks(view, ctx)}{tools}</footer>'
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>{_text(title)}</title>\n'
            f'{head}<style>{_STYLE}</style>\n</head>\n<body data-now="{now:.3f}" data-open="{_text(page)}" '
            f'data-pace="{pace}" data-jobs="{1 if jobs else 0}">\n'
            f'<div class="app">{top}{side}{foot}<main class="pane" data-pane="{_text(page)}" tabindex="-1">'
            f'<div class="page">{pane}</div></main></div>\n{extra}</body>\n</html>\n')
