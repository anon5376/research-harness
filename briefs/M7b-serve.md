# M7b: `rh serve`, the harness as a local web app

**Goal.** `rh serve` opens the dashboard plus the everyday actions in any browser on this machine. The desktop app (M7c) wraps it in a window. It is a view and a remote control of the CLI; it holds no logic of its own.

**Files you write:** `rh/serve.py` and `tests/test_serve.py`. Nothing else. The CLI hook already exists: `rh serve [--port N] [--no-open]` calls `serve.main(cfg, port, open_browser)`.

**Interface (exact).**
```python
def make_server(cfg: dict, host: str = "127.0.0.1", port: int = 0,
                token: str | None = None) -> tuple["http.server.HTTPServer", str]:
    """A server bound to host:port (0 = any free port), not yet serving. Returns it and the page URL
    http://127.0.0.1:<port>/?t=<token>. token defaults to secrets.token_urlsafe(24)."""

def main(cfg: dict, port: int = 0, open_browser: bool = True) -> int:
    """Print the URL, open it with webbrowser unless open_browser is False, serve until Ctrl-C, return 0."""
```
- Standard library only: `http.server.HTTPServer` (single-threaded), `urllib.parse`, `secrets`, `html`.

**Every request.**
- The token must match, from the `t` query parameter on GET and the `t` form field on POST. Compare with `secrets.compare_digest`. A mismatch gets 403.
- The `Host` header must be `127.0.0.1:<port>` or `localhost:<port>`; anything else gets 403.
- A POST with an `Origin` header must come from that same origin.
- POST bodies are `application/x-www-form-urlencoded`, at most 32 MiB; larger gets 413.

**Pages.**
- `GET /` is `rh.dashboard.render(view, now)`, where `view` is what `rh status --json` prints, produced by the call route below. Before `</body>`, insert one `<nav>` with a link to `/actions?t=<token>` and the result of the last action (see below).
- `GET /actions` is one page with these forms. Each form carries a hidden `t` and POSTs to the path given:
  - `/inbox/resolve`: `id`, `note`.
  - `/stop`: `reason`, optional `job` or `route`, `pause` checkbox; the owner is always `app`.
  - `/resume`: optional `job` or `route`, `pause` checkbox; the owner is `app`.
  - `/attach`: `job`, `task_id`.
  - `/harvest`: `job`, `text`. A textarea for a saved patch or the bundle reply text. Text containing `RH-BUNDLE` goes through `--bundle`.
  - `/land`: `job`.
  - `/tick`: one supervisor tick.
- No JavaScript, no external URLs, no fonts or images. Keep the styling minimal and inline; Claude designs the look later behind this interface. The dashboard page refreshes with `<meta http-equiv="refresh" content="30">`, carrying the token in its URL.

**The call route.**
- Every action and the status view run the real CLI in process: `rh.cli.main([...])`, with stdout and stderr captured and `RH_HOME` from `cfg`. Hold a lock around each call, because stdout redirection is process-wide.
- Harvest text is first written to `$RH_HOME/uploads/<job>-<unix ms>.txt`. Its path is then passed to `rh cloud harvest`.
- After a POST, answer 303 to `/?t=<token>`. Keep the CLI's exit code and output, HTML-escaped and cut to 2,000 characters, in server memory, and show it once on the next `GET /`. Never put that output in a URL.

**Tests that must pass** (`tests/test_serve.py`, unittest). Each uses a real server on port 0 in a thread, urllib, and a temp `RH_HOME` and repo:
1. `GET /` without the token returns 403. With it, it returns 200 and contains the dashboard's section headings and the `/actions` link.
2. A `Host: example.test:<port>` header returns 403, even with the token.
3. `POST /inbox/resolve` with the token resolves an open inbox item. The same POST without the token returns 403 and leaves the item open.
4. `POST /attach` moves a `handoff` job to `running`. The next `GET /` shows the CLI's "attached" line once, and the GET after it does not.
5. `POST /attach` for a job that is not in handoff leaves its state alone and shows the CLI's error, escaped.
6. `POST /harvest` with a patch text moves a running cloud job to `collecting`, and the uploads file exists.
7. Neither page contains `<script`, `http://` or `https://`, apart from the page's own `127.0.0.1` origin in the 303 Location header.
8. `python3 -I tests/run.py` passes as a whole.

**Do not change** `rh/cli.py`, `rh/dashboard.py`, the view's shape, or any other module.
