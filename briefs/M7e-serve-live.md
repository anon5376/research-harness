# M7e: rh serve serves the live app (app seat)

**Goal.** `rh serve` hands the design seat's live page and script to the browser or desktop window under a strict content policy. The page's look, script and controls belong to `rh/dashboard.py` (M7d-2). This job only wires them in.

**Files you write:** `rh/serve.py` and `tests/test_serve.py`. Nothing else.

**Interface (exact).**
- `GET /` (token checked as now) returns `dashboard.render(view, now, token=token, result=last_result)`. Remove the string insertion of the `<nav>` and the refresh meta; the dashboard renders both. `last_result` is still shown once and cleared.
- `GET /app.js` returns `dashboard.SCRIPT` with `Content-Type: text/javascript; charset=utf-8` and `Cache-Control: no-store`. The Host check applies; no token is needed, because the script holds no data.
- Every response carries:
  - `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; font-src data:; img-src data:; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'`
  - `X-Content-Type-Options: nosniff`
  - `Referrer-Policy: no-referrer` (already sent)
- POST handling is unchanged: the same routes and fields, a 303 to `/?t=<token>`, the same token, Host, Origin and size checks. A same-origin `fetch` POST follows the 303 and reads the page, which carries the result.
- `/actions` stays as the full-form fallback page.

**Tests that must pass** (`tests/test_serve.py`; change only what the new page makes untrue):
- `GET /` with the token has exactly one `<script`, which is `src="/app.js"`, and no `http://` or `https://`.
- `GET /app.js` returns 200 with the JavaScript content type, and 403 with a bad Host.
- Every response carries the CSP and nosniff headers.
- A POST result shows once on the next `GET /` and not on the one after.
- All M7b security tests still hold.
- `python3 -I tests/run.py` as a whole.

**Needs:** M7d-2 landed, which brings `render(..., token, result)` and `SCRIPT`.
