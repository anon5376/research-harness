# M6a: static dashboard page

**Goal.** `rh status --html <path>` writes one self-contained HTML page with the same content as `rh status`. The dashboard is a view of the ledger, never a second status source.

**Files you write:** `rh/dashboard.py`, `tests/test_dashboard.py`. Nothing else. The CLI hook already exists in `rh/cli.py` (`cmd_status` calls `dashboard.render(view, now)`).

**Interface (exact).**
```python
def render(view: dict, now: float) -> str
```
- `view` is the dict `rh status --json` prints. Keys: `supervisor` (None or {pid, age_s}), `stops`, `routes`, `live`, `not_started`, `inbox`, `last_24h`, `recent`. Read `cmd_status` in `rh/cli.py` for the row shapes.
- It returns a complete HTML5 document as a string.

**Behaviour.**
- One section per key, in the order above, each with a visible heading.
- Empty sections say so ("nothing finished" for an empty `last_24h`, "none" elsewhere).
- Every string from the view is HTML-escaped. Job details carry worker text.
- Job rows show id, state, a PAUSED marker when `paused` is true, and the detail.
- Inline CSS only. No scripts, no external URLs, no fonts or images. Light and dark through `prefers-color-scheme`. Readable at 375 px width.
- Times are shown relative to `now` ("12 s ago").

**Tests that must pass** (`tests/test_dashboard.py`, unittest):
1. A detail containing `<script>alert(1)</script>` appears escaped, and the raw tag does not appear.
2. All eight section headings appear for a full view. For an empty view, the page still renders with "nothing finished".
3. A paused live job shows PAUSED; the same row with `paused` false does not.
4. The page contains no `http://` or `https://` and no `<script`.
5. End to end: a temp `RH_HOME` with one finished local-shell job; `bin/rh status --html <tmp>/s.html` exits 0, and the file contains that job id and `done`.
6. `python3 -I tests/run.py` passes as a whole.

**Do not change** `rh/cli.py`, the view's shape, or any other module.
