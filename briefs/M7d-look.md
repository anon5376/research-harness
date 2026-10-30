# M7d-1: the dashboard's look (design seat)

**Goal.** `rh status --html` and the `rh serve` home page are one status board a person works in all day. Give it a deliberate, quiet app-screen look: scannable job rows, clear state, and the inbox and stops easy to see. It should not read as a landing page or a generic template.

**Files you write:** `rh/dashboard.py`, plus `tests/test_dashboard.py` only to add tests. Nothing else.

**Fixed (do not change):**
- `render(view: dict, now: float) -> str` and the view's shape.
- Each section heading is exactly `<h2 id="<key>"><Heading></h2>`.
- The page contains `</head>` and `</body>` once each, because `rh serve` inserts its refresh meta and `<nav>` there.
- All text is escaped.

**Rules:**
- No scripts, no external URLs, no web fonts, no images. Use the system font stack and inline CSS only.
- Light and dark through `prefers-color-scheme`.
- At 375 px there is no horizontal scroll (`scrollWidth <= clientWidth`, measured in a browser).
- Body text contrast is at least 4.5:1, computed, in both schemes.
- Use the design-taste-frontend skill (app screens). Do not use design-critic.

**Done means:**
- `python3 -I tests/run.py` passes as a whole.
- Screenshots of the page rendered from `full_view()` in `tests/test_dashboard.py` exist at 375 px and 1280 px, in light and dark, saved under `$RH_HOME/outbox/design/`, not in the repository.
- The measured scrollWidth and clientWidth at 375 px and the computed contrast for both schemes are reported.
- The founder looks at the screenshots and says yes. That is the manual gate.

**How:**
- Work in your own git worktree on branch `design/m7d-look`, based on `f414049`. Commit there with the noreply author. Never push.
- One pass, plus at most one fix round.
- Reply to the lead session with `design done <sha>`, the numbers, and the screenshot paths.
