"""Codex cloud adapter: `codex cloud exec | status | list | diff` as typed calls.

Each call is one argv list, run without a shell and with a timeout. A call the route cannot take raises
RouteUnavailable with a reason the supervisor can back off on. Task states come from map_status, which never
turns an unknown word into a terminal state."""
from __future__ import annotations

import json
import os
import re
import subprocess

TASK_ID_RE = re.compile(r"task_e_[0-9a-f]+")
REASONS = frozenset({"env-not-found", "cli-missing", "auth", "rate-limited", "timeout", "error"})
STATUS_MAP = {
    "pending": "running", "queued": "running", "in_progress": "running", "running": "running",
    "ready": "completed", "applied": "completed", "completed": "completed",
    "error": "failed", "failed": "failed",
    "cancelled": "aborted", "canceled": "aborted",
}

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")  # CSI colours, OSC links
_STATUS_LINE = re.compile(r"\s*\[([^\]\n]*)\]\s*(.*)")
_URL = re.compile(r"https?://[^\s\"'<>`\x00-\x1f\x7f]+")
_ENV_NOT_FOUND = re.compile(r"\benvironment\b[^\n]*\bnot found\b", re.I)
_RATE = re.compile(r"\b429\b|rate[ _-]?limit|too many requests", re.I)
_AUTH = re.compile(r"\b401\b|unauthori[sz]ed|not logged in|not authenticated|log ?in required|codex login", re.I)
_RETRY = re.compile(r"(?:retry[ _-]after|try again in|retry in)\W{0,3}(\d+(?:\.\d+)?)(?!\d|\.\d|[-:/]\d)"
                    r"\s*(ms|millisec\w*|s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?)?\b", re.I)
_TASK_ARG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class RouteUnavailable(Exception):
    """The cloud route could not take a call. `.reason` is in REASONS, `.retry_after` is seconds or None."""

    def __init__(self, reason, detail="", retry_after=None):
        if reason not in REASONS:
            raise ValueError(f"unknown route reason {reason!r}")
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason, self.detail, self.retry_after = reason, detail, retry_after


def map_status(raw) -> str:
    """CLI status word -> running | completed | failed | aborted | unknown (case-insensitive)."""
    if not isinstance(raw, str):
        return "unknown"
    return STATUS_MAP.get(re.sub(r"[\s-]+", "_", raw.strip().strip("[]").strip().lower()), "unknown")


def classify(text) -> tuple[str, float | None]:
    """Error output -> (reason, retry_after seconds or None)."""
    if _ENV_NOT_FOUND.search(text):
        return "env-not-found", None
    if _RATE.search(text):
        return "rate-limited", _retry_after(text)
    if _AUTH.search(text):
        return "auth", None
    return "error", _retry_after(text)


def _retry_after(text):
    m = _RETRY.search(text)
    if not m:
        return None
    unit = (m.group(2) or "s").lower()
    scale = 0.001 if unit.startswith(("ms", "milli")) else 60 if unit[0] == "m" else 3600 if unit[0] == "h" else 1
    return float(m.group(1)) * scale


def _decode(data):
    if isinstance(data, (bytes, bytearray)):
        return bytes(data).decode("utf-8", "replace")
    return data or ""


def _clean(text):
    return _ANSI.sub("", text or "")


def _excerpt(text, limit=600):
    text = " | ".join(line.strip() for line in _clean(text).splitlines() if line.strip())
    return text if len(text) <= limit else text[:limit] + "..."


def _check_arg(name, value):
    if not isinstance(value, str) or not value.strip() or value.startswith("-") or any(c in value for c in "\r\n\0"):
        raise ValueError(f"bad {name}: {value!r}")


def _check_task_id(task_id):
    if not isinstance(task_id, str) or not _TASK_ARG.fullmatch(task_id):
        raise ValueError(f"bad task id: {task_id!r}")


def _tasks(stdout):
    try:
        data = json.loads(stdout)
    except ValueError:
        data = None
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        raise RouteUnavailable("error", f"cloud list: no tasks array in output: {_excerpt(stdout, 200)}")
    return [t for t in tasks if isinstance(t, dict)]


class CodexCloud:
    """The `codex` CLI's cloud commands. `run` is injectable (subprocess.run's signature) for tests. Every call
    runs in `cwd`: the CLI writes an error.log holding account details into its working directory, so callers
    pass a private directory such as $RH_HOME, never a checkout."""

    def __init__(self, codex="codex", run=subprocess.run, timeout=120.0, cwd=None):
        self.codex, self._run, self.timeout, self.cwd = codex, run, float(timeout), cwd

    def _exec(self, args, timeout=None):
        """Run codex with args -> CompletedProcess with str output. Bytes are decoded here, not by text mode,
        whose newline translation would rewrite CRLF lines inside a diff."""
        t = self.timeout if timeout is None else timeout
        try:
            cp = self._run([self.codex, *args], capture_output=True, stdin=subprocess.DEVNULL, timeout=t, check=False,
                           cwd=self.cwd)
        except FileNotFoundError as e:
            if self.cwd is not None and e.filename is not None and os.fspath(e.filename) == os.fspath(self.cwd):
                raise RouteUnavailable("error", f"working directory {self.cwd} does not exist") from None
            raise RouteUnavailable("cli-missing", f"{self.codex} not found") from None
        except subprocess.TimeoutExpired:
            raise RouteUnavailable("timeout", f"{' '.join(args[:2])}: no answer in {t:g}s") from None
        except OSError as e:
            if getattr(e, "winerror", None) == 267 and self.cwd is not None:
                raise RouteUnavailable("error", f"invalid working directory {self.cwd}") from None
            raise RouteUnavailable("error", f"{self.codex}: {e.strerror or e}") from None
        return subprocess.CompletedProcess(cp.args, cp.returncode, _decode(cp.stdout), _decode(cp.stderr))

    @staticmethod
    def _failure(cp, what):
        text = cp.stderr if (cp.stderr or "").strip() else cp.stdout
        reason, retry = classify(_clean(text))
        return RouteUnavailable(reason, f"{what} exited {cp.returncode}: {_excerpt(text)}", retry)

    def launch(self, env_id, prompt, branch="main") -> dict:
        """Create one task -> {task_id, url, raw}. A task id in the output wins over the exit code, so a task
        that was created is never launched twice; ids already present in the prompt are ignored."""
        _check_arg("env_id", env_id)
        _check_arg("branch", branch)
        if not isinstance(prompt, str) or not prompt.strip() or prompt.startswith("-"):
            raise ValueError("prompt must be non-empty and must not start with '-'")
        cp = self._exec(["cloud", "exec", "--env", env_id, "--branch", branch, prompt])
        both = cp.stdout + "\n" + cp.stderr  # ids and URLs are read before escapes are cleaned: a link may hold them
        tid = next((t for t in TASK_ID_RE.findall(both) if t not in prompt), None)
        if tid is None:
            if cp.returncode:
                raise self._failure(cp, "cloud exec")
            raise RouteUnavailable("error", f"cloud exec exited 0 without a task id: {_excerpt(both)}")
        url = next((u.rstrip(".,;:!?)]}") for u in _URL.findall(both) if tid in u), None)
        return {"task_id": tid, "url": url, "raw": _clean(both).strip()}

    def status(self, task_id) -> dict:
        """-> {state, raw, title, detail} from the first `[WORD] title` line; output without one is `unknown`."""
        _check_task_id(task_id)
        cp = self._exec(["cloud", "status", task_id])
        lines = [line.strip() for line in _clean(cp.stdout).splitlines() if line.strip()]
        hit = next((i for i, line in enumerate(lines) if _STATUS_LINE.fullmatch(line)), None)
        if hit is None:
            if cp.returncode:
                raise self._failure(cp, "cloud status")
            return {"state": "unknown", "raw": "", "title": "", "detail": "\n".join(lines)}
        m = _STATUS_LINE.fullmatch(lines[hit])
        raw = m.group(1).strip()
        return {"state": map_status(raw), "raw": raw, "title": m.group(2).strip(),
                "detail": "\n".join(lines[hit + 1:])}

    def diff(self, task_id) -> str:
        """The task's unified git diff, verbatim (bytes that are not UTF-8 become U+FFFD, so applying fails)."""
        _check_task_id(task_id)
        cp = self._exec(["cloud", "diff", task_id])
        if cp.returncode:
            raise self._failure(cp, "cloud diff")
        return cp.stdout

    def health(self) -> dict:
        """-> {ok, version, detail}: `codex --version` answers and `codex cloud list` returns its JSON."""
        version = None
        try:
            cp = self._exec(["--version"], timeout=min(self.timeout, 30.0))
            if cp.returncode:
                raise self._failure(cp, "--version")
            version = next((line.strip() for line in (cp.stdout + "\n" + cp.stderr).splitlines() if line.strip()),
                           None)
            cp = self._exec(["cloud", "list", "--limit", "1", "--json"])
            if cp.returncode:
                raise self._failure(cp, "cloud list")
            _tasks(cp.stdout)
        except RouteUnavailable as e:
            return {"ok": False, "version": version, "detail": str(e)}
        return {"ok": True, "version": version, "detail": "codex cloud list answered"}

    def check_env(self, env_id) -> None:
        """Whether `codex cloud exec --env env_id` can resolve the environment, asked without creating a task:
        `codex cloud list --env` resolves it against the same client-side list. Returns when it can; raises
        RouteUnavailable otherwise, with reason env-not-found and the CLI's words when the CLI does not list it."""
        _check_arg("env_id", env_id)
        cp = self._exec(["cloud", "list", "--env", env_id, "--limit", "1", "--json"])
        if cp.returncode:
            raise self._failure(cp, "cloud list --env")
        _tasks(cp.stdout)

    def list(self, limit=20) -> list[dict]:
        """Recent tasks as the CLI reports them, each with an added `state` from map_status."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"bad limit: {limit!r}")
        cp = self._exec(["cloud", "list", "--json", "--limit", str(limit)])
        if cp.returncode:
            raise self._failure(cp, "cloud list")
        return [dict(t, state=map_status(t.get("status"))) for t in _tasks(cp.stdout)]
