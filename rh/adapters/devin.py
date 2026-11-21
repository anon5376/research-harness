"""Devin adapter: the Devin API v3 sessions endpoints as typed calls.

Each call is one HTTPS request with a timeout, made through an injectable `send` so tests run offline. A call the
route cannot take raises RouteUnavailable with the same reasons the Codex cloud adapter uses, so the supervisor
can back off on both routes the same way. The API key goes only into the Authorization header: it is scrubbed
from every error, never in a repr, and a redirect is refused rather than followed, so the header never reaches
another host. Every session carries the tag `rh:<job>`, and launch looks for one before it creates another, so a
retry after a lost response does not start a second session. Session states come from map_status, which never
turns an unknown status into a terminal state.

Not wired into the supervisor yet: no job kind launches Devin sessions on its own."""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

from rh.adapters.codex_cloud import RouteUnavailable

API = "https://api.devin.ai/v3"
STATUSES = frozenset({"new", "claimed", "running", "exit", "error", "suspended", "resuming"})
WAITING = frozenset({"waiting_for_user", "waiting_for_approval"})
CAPACITY = frozenset({"usage_limit_exceeded", "out_of_credits", "out_of_quota", "no_quota_allocation",
                      "payment_declined", "org_usage_limit_exceeded", "user_usage_limit_exceeded",
                      "total_session_limit_exceeded", "contract_expired"})
MODES = frozenset({"normal", "fast", "fusion", "lite", "ultra", "swe-2-medium", "swe-2-high", "swe-2-max"})
TAG = "rh:"

_ORG = re.compile(r"org-[A-Za-z0-9_-]+")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def map_status(status, detail=None) -> str:
    """(status, status_detail) -> running | waiting | completed | failed | aborted | capacity | unknown.

    `waiting` means a person has to answer, approve or wake the session. `completed` means the session ended
    without an error; whether it did the work is the job's acceptance check to say."""
    s, d = _word(status), _word(detail)
    if s not in STATUSES:
        return "unknown"
    if d in CAPACITY:
        return "capacity"
    if s == "error" or d == "error":
        return "failed"
    if d == "finished":
        return "completed"
    if d in WAITING:
        return "waiting"
    if s == "exit":
        return "aborted" if d == "user_request" else "completed"
    if s == "suspended":
        return "waiting"
    return "running"


def classify(code, text="", headers=None) -> tuple[str, float | None]:
    """HTTP status of a failed call -> (reason, retry_after seconds or None)."""
    if code in (401, 403):
        return "auth", None
    if code == 429:
        return "rate-limited", _retry_after(headers)
    if code == 408 or code >= 500:
        return "timeout", _retry_after(headers)
    return "error", None


def _word(raw):
    return raw.strip().lower() if isinstance(raw, str) else None


def _retry_after(headers):
    value = next((v for k, v in (headers or {}).items() if k.lower() == "retry-after"), None)
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _check(name, value, pattern):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"bad {name}: {value!r}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib would copy the Authorization header to the new URL, whatever its host


_fetch = urllib.request.build_opener(_NoRedirect).open  # an HTTP request, not a file open


def urllib_send(method, url, headers, body, timeout):
    """One request -> (status, headers, body bytes). HTTP errors and redirects come back as their status."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _fetch(req, timeout=timeout) as r:
            return r.status, dict(r.headers.items()), r.read()
    except urllib.error.HTTPError as e:
        try:
            data = e.read()
        finally:
            e.close()
        return e.code, dict((e.headers or {}).items()), data


class Devin:
    """Sessions in one Devin organization. `send` has urllib_send's signature and is injectable for tests."""

    def __init__(self, org_id, api_key, send=urllib_send, timeout=60.0, base=API):
        _check("org_id", org_id, _ORG)
        if not isinstance(api_key, str) or not api_key.strip() or any(c in api_key for c in "\r\n\0 "):
            raise ValueError("api_key must be one non-empty token")
        self.org_id, self.timeout, self.base = org_id, float(timeout), base.rstrip("/")
        self._key, self._send = api_key, send

    def __repr__(self):
        return f"Devin(org_id={self.org_id!r}, base={self.base!r})"

    def _scrub(self, text):
        return text.replace(self._key, "[redacted]")

    def _excerpt(self, data, limit=300):
        text = data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data or "")
        try:
            problem = json.loads(text)
        except ValueError:
            problem = None
        if isinstance(problem, dict) and (problem.get("title") or problem.get("detail")):
            text = ": ".join(str(problem[k]) for k in ("title", "detail") if problem.get(k))
        text = self._scrub(" ".join(text.split()))
        return text if len(text) <= limit else text[:limit] + "..."

    def _call(self, method, path, query=None, body=None, what=""):
        url = f"{self.base}/organizations/{self.org_id}/{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            code, rh, raw = self._send(method, url, headers, data, self.timeout)
        except (TimeoutError, socket.timeout):
            raise RouteUnavailable("timeout", f"{what}: no answer in {self.timeout:g}s") from None
        except (urllib.error.URLError, OSError) as e:
            why = getattr(e, "reason", None) or getattr(e, "strerror", None) or e
            raise RouteUnavailable("timeout", self._scrub(f"{what}: no answer: {why}")) from None
        if 300 <= code < 400:
            raise RouteUnavailable("error", f"{what}: refused a {code} redirect")
        if code >= 400:
            reason, retry = classify(code, headers=rh)
            raise RouteUnavailable(reason, f"{what} answered {code}: {self._excerpt(raw)}", retry)
        try:
            out = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise RouteUnavailable("error", f"{what}: the answer is not JSON: {self._excerpt(raw, 120)}") from None
        if not isinstance(out, dict):
            raise RouteUnavailable("error", f"{what}: the answer is not a JSON object")
        return out

    def _session(self, raw, what, reused=False) -> dict:
        sid = raw.get("session_id")
        if not isinstance(sid, str) or not _ID.fullmatch(sid):
            raise RouteUnavailable("error", f"{what}: no usable session_id in the answer")
        prs = raw.get("pull_requests") if isinstance(raw.get("pull_requests"), list) else []
        acus = raw.get("acus_consumed")
        return {
            "session_id": sid,
            "url": raw.get("url") if isinstance(raw.get("url"), str) else None,
            "state": map_status(raw.get("status"), raw.get("status_detail")),
            "status": raw.get("status"),
            "detail": raw.get("status_detail"),
            "tags": [t for t in raw.get("tags") or [] if isinstance(t, str)],
            "pull_requests": [{"url": p["pr_url"], "state": p.get("pr_state")} for p in prs
                              if isinstance(p, dict) and isinstance(p.get("pr_url"), str)],
            "structured_output": raw.get("structured_output") if isinstance(raw.get("structured_output"), dict)
            else None,
            "acus": float(acus) if isinstance(acus, (int, float)) and not isinstance(acus, bool) else None,
            "reused": reused,
        }

    def find(self, job_id):
        """The newest unarchived session tagged for this job, or None. The tag is checked here too, so a server
        that ignored the filter cannot hand back another job's session."""
        _check("job id", job_id, _ID)
        tag = TAG + job_id
        out = self._call("GET", "sessions", {"tags": [tag], "is_archived": "false", "first": 20},
                         what="list sessions")
        items = out.get("items")
        if not isinstance(items, list):
            raise RouteUnavailable("error", "list sessions: no items array in the answer")
        mine = [s for s in items if isinstance(s, dict) and tag in (s.get("tags") or [])
                and not s.get("is_archived")]
        if not mine:
            return None
        newest = max(mine, key=lambda s: s.get("created_at") if isinstance(s.get("created_at"), int) else 0)
        return self._session(newest, "list sessions", reused=True)

    def launch(self, job_id, prompt, *, title=None, repos=(), max_acu_limit=None, mode=None,
               structured_output_schema=None) -> dict:
        """Start one session for the job, or return the one it already has (`reused` is then True)."""
        _check("job id", job_id, _ID)
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be non-empty")
        for r in repos:
            _check("repo", r, _REPO)
        if max_acu_limit is not None and (isinstance(max_acu_limit, bool) or not isinstance(max_acu_limit, int)
                                          or max_acu_limit < 1):
            raise ValueError(f"bad max_acu_limit: {max_acu_limit!r}")
        if mode is not None and mode not in MODES:
            raise ValueError(f"bad mode: {mode!r}")
        existing = self.find(job_id)
        if existing:
            return existing
        body = {"prompt": prompt, "title": title or f"rh {job_id}", "tags": [TAG + job_id]}
        if repos:
            body["repos"] = list(repos)
        if max_acu_limit is not None:
            body["max_acu_limit"] = max_acu_limit
        if mode is not None:
            body["devin_mode"] = mode
        if structured_output_schema is not None:
            body["structured_output_schema"] = structured_output_schema
        return self._session(self._call("POST", "sessions", body=body, what="create session"), "create session")

    def status(self, session_id) -> dict:
        _check("session id", session_id, _ID)
        return self._session(self._call("GET", f"sessions/{session_id}", what="get session"), "get session")

    def terminate(self, session_id, archive=False) -> dict:
        _check("session id", session_id, _ID)
        query = {"archive": "true"} if archive else None
        return self._session(self._call("DELETE", f"sessions/{session_id}", query, what="terminate session"),
                             "terminate session")

    def health(self) -> dict:
        """-> {ok, detail}: listing one session answers with the expected shape."""
        try:
            out = self._call("GET", "sessions", {"first": 1}, what="list sessions")
            if not isinstance(out.get("items"), list):
                raise RouteUnavailable("error", "list sessions: no items array in the answer")
        except RouteUnavailable as e:
            return {"ok": False, "detail": str(e)}
        return {"ok": True, "detail": "the sessions endpoint answered"}
