# M7d-2: an app people choose (design seat)

**Goal.** The founder wants to use this app instead of the Codex app or the Claude Code app, and to enjoy using it. Design and motion are part of that experience. M7d-1 made a clean status page. Now make it the place you run your workers from: live, responsive and quietly delightful. The bar is a product a person picks on purpose, not "looks fine".

**What changes from M7d-1.**
- The live page (served by `rh serve`, opened in the desktop window) may load one script, `/app.js`, from the same origin. No external URLs, no inline scripts, no `eval` or `new Function`.
- The static snapshot (`rh status --html`, no token) stays script-free and keeps every M7d-1 rule.

**Files you write:** `rh/dashboard.py` and `tests/test_dashboard.py`. Nothing else. `rh/serve.py` belongs to the app seat, which wires your output in under M7e.

**Interface (exact).**
```python
SCRIPT: str
    # the client script rh serve sends at GET /app.js

def render(view: dict, now: float, token: str | None = None, result: str | None = None) -> str:
    # token None: the static snapshot, unchanged rules, no script.
    # token set: the live page. It carries <script src="/app.js" defer></script> once, a fallback
    # <noscript><meta http-equiv="refresh" content="30"></noscript>, and controls for the actions.
    # result is the last action's output, already escaped by serve; show it once, as feedback.
```
- **Actions on the live page** are forms that POST to the existing routes, with the fields M7b fixed: `/inbox/resolve` (id, note), `/stop` (reason, job or route, pause), `/resume`, `/attach` (job, task_id), `/harvest` (job, text), `/land` (job), `/tick`. Each carries a hidden `t`.
- Put each action where its object is: resolve on its inbox item, attach on a handoff job, land on a done job, stop and resume on a live job or route.
- With the script on, a submit is a `fetch` POST and the board updates in place. With the script off, the forms still work through the server's 303.
- **Live updates:** the script re-fetches the page itself (`location.pathname + location.search`) every few seconds, parses it, and swaps only what changed.

**Experience (your judgment, measured where it can be).**
- Motion is part of it:
  - a job that changes state animates there;
  - a running job looks alive;
  - a new inbox item or stop arrives visibly;
  - an action gives feedback at once (pressed, pending, done or failed, with the CLI output).
- Animate only transform and opacity. With `prefers-reduced-motion: reduce` it settles into plain crossfades or none.
- Keyboard first: moving through jobs and items, acting on the focused one, and a visible shortcut sheet.
- Type, colour and layout with a point of view: a distinct identity rather than a template. One embedded OFL-licensed font as a `data:` URI is allowed if it earns its weight; the whole page stays under 300 KB.
- Light and dark. 375 px without horizontal scroll. Body contrast at least 4.5:1. Focus always visible.
- Use the design-taste-frontend skill (app screens). Not design-critic.

**Tests that must pass** (`tests/test_dashboard.py`):
- `render()` without a token keeps every M7d-1 test.
- `render(token=...)`:
  - has exactly one `<script`, which is `src="/app.js"`, and no `http://` or `https://`;
  - has a form for each route above with a hidden `t`;
  - escapes worker text and `result`.
- `SCRIPT` contains no `http://`, `https://`, `eval(` or `Function(`.
- `python3 -I tests/run.py` as a whole.

**Done means:**
- The suite passes.
- Screenshots at 375 px and 1280 px, light and dark, of the live page from `full_view()`.
- One frame strip, 8 to 12 frames, of a job moving from running to done, and one of an action's feedback.
- All under `$RH_HOME/outbox/design/`, with the scroll and contrast numbers as before.
- To see it live before M7e lands, run a throwaway copy of `rh serve` in your worktree that passes `token=` and serves `/app.js`. Do not hand that copy back.
- Then the founder looks and says yes.

**How:** continue from your M7d-1 files (landed as 4deb658 on `design/m7d-look`). Uncommitted is fine: send `design done` with the file paths, the numbers and the screenshot paths. One build pass plus one polish pass; no review loops.
