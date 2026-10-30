"""Cloud task protocol: the pinned task prompt, RESULT.json, and the custody check on a task's diff."""
from __future__ import annotations

import json
import re

JOB_ID_RE = re.compile(r"^(?!.*\.\.)(?!.*(?:\.|\.lock)$)[a-z0-9][a-z0-9._-]{0,62}\Z")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RESULT_STATES = ("done", "failed", "blocked")
RESULT_KINDS = frozenset({"missing", "invalid", "stale-input", "wrong-job"})

_HUNK = re.compile(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


class ResultError(Exception):
    """RESULT.json is unusable. `.kind` is in RESULT_KINDS; `.refusals` keeps refusal text an invalid file carried."""

    def __init__(self, kind, detail="", refusals=()):
        if kind not in RESULT_KINDS:
            raise ValueError(f"unknown result error kind {kind!r}")
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.detail, self.refusals = kind, detail, list(refusals)


def check_job_id(job_id):
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise ValueError(f"bad job id {job_id!r}: want {JOB_ID_RE.pattern}")
    return job_id


def check_sha(sha):
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise ValueError(f"bad sha {sha!r}: want 40 lowercase hex")
    return sha


def _rel_ok(path):
    """A plain repo-relative path: no absolute, `~`, drive, backslash, control char, empty, `.` or `..` part.
    One trailing `/` marks a directory."""
    if not isinstance(path, str) or not path or path.startswith(("/", "~")) or "\\" in path:
        return False
    if re.match(r"[A-Za-z]:", path) or any(ord(c) < 32 or c == "\x7f" for c in path):
        return False
    return all(part not in ("", ".", "..") for part in (path[:-1] if path.endswith("/") else path).split("/"))


def result_path(job_id) -> str:
    return f"runs/{check_job_id(job_id)}/RESULT.json"


def write_scope(job_id, write_paths) -> list[str]:
    """`runs/<job>/` plus the declared paths (directories end with `/`): what the prompt allows and custody checks."""
    if isinstance(write_paths, str):
        raise ValueError("write_paths must be a list of paths, not one string")
    scope = [f"runs/{check_job_id(job_id)}/"]
    for p in write_paths:
        if not _rel_ok(p):
            raise ValueError(f"bad write path {p!r}: want a plain repo-relative path")
        if p not in scope:
            scope.append(p)
    return scope


def task_prompt(job_id, sha, brief, write_paths, attempt=1, network=False) -> str:
    """The full cloud task prompt: the pinned checkout line, the rules, the RESULT.json schema, then the brief.
    network=True (the job declared needs = ["net"]) lets later commands use the environment's allowlist."""
    check_sha(sha)
    scope = write_scope(job_id, write_paths)
    if not isinstance(brief, str) or not brief.strip():
        raise ValueError("empty brief")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError(f"bad attempt {attempt!r}")
    result, allowed, staged = result_path(job_id), ", ".join(scope), " ".join(scope)
    net = ("Later commands may use the network only where the brief needs it, within the environment's allowlist."
           if network else "It is the only command that may use the network.")
    schema = json.dumps({"job": job_id, "state": "done|failed|blocked", "head": "<sha>", "summary": "...",
                         "checks": [{"cmd": "...", "exit": 0}], "outputs": [f"runs/{job_id}/..."],
                         "refusals": ["<exact text>"]})
    return f"""research-harness job {job_id}, attempt {attempt}. Follow these rules exactly; they override the brief.

1. Go to the root of the repository checkout (`cd "$(git rev-parse --show-toplevel)"`). If the working directory \
is not a git repository, find the checkout with `ls /workspace`, cd into it, and work from there for the rest of the \
task. Every path below is relative to that root.
2. Run this next. {net} If it fails, do nothing else except step 6 with state "failed":
git fetch origin main && git checkout --detach {sha} && test "$(git rev-parse HEAD)" = "{sha}"
3. Write only to: {allowed}. Change no other path. Do not commit. When you finish, stage what you wrote with \
`git add -A -- {staged}` and nothing else.
4. Never push, open pull requests, send messages or send notifications.
5. If a permission, policy or classifier check refuses anything, stop at once. Do not retry, reword or work around \
it. Put its exact text in "refusals" and set "state" to "blocked".
6. Finish by writing {result}:
{schema}
"head" is what `git rev-parse HEAD` printed right after step 2. "checks" lists each command you ran to verify the \
work, with its exit code. "outputs" lists the files you wrote.

Brief:
{brief.strip()}
"""


def followup_prompt(job_id, sha, brief, write_paths, attempt=1, network=False) -> str:
    """A new job in a standing task starts by dropping the previous job's staged work."""
    return ("Follow-up turn: this is a new job in the same task. Before step 1, run from the checkout root: "
            "git reset -q --hard && git clean -qfd\n\n" +
            task_prompt(job_id, sha, brief, write_paths, attempt=attempt, network=network))


def _str_list(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def parse_result(text, job_id, sha) -> dict:
    """RESULT.json text (None when the file is absent) -> {state, head, summary, checks, outputs, refusals}.
    Any refusal forces `blocked`, and wins over a stale head: a refused job must never be retried as stale."""
    check_job_id(job_id)
    check_sha(sha)
    if text is None:
        raise ResultError("missing", f"no {result_path(job_id)}")
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8")
        except UnicodeDecodeError:
            raise ResultError("invalid", "not UTF-8") from None
    try:
        data = json.loads(text.lstrip("\ufeff"))
    except ValueError as e:
        raise ResultError("invalid", f"not JSON: {e}") from None
    if not isinstance(data, dict):
        raise ResultError("invalid", "not a JSON object")
    job = data.get("job")
    if not isinstance(job, str):
        raise ResultError("invalid", "job is missing")
    if job != job_id:
        raise ResultError("wrong-job", f"RESULT.json is for {job!r}, not {job_id!r}")
    refusals = data.get("refusals", [])
    if not _str_list(refusals):
        raise ResultError("invalid", "refusals is not a list of strings")
    refusals = [r for r in refusals if r.strip()]

    def invalid(why):
        return ResultError("invalid", why, refusals)

    state, head = data.get("state"), data.get("head")
    if state not in RESULT_STATES:
        raise invalid(f"state {state!r} is not one of {'|'.join(RESULT_STATES)}")
    if not isinstance(head, str) or not SHA_RE.fullmatch(head.strip().lower()):
        raise invalid(f"head {head!r} is not a 40-hex sha")
    head = head.strip().lower()
    summary, checks, outputs = data.get("summary", ""), data.get("checks", []), data.get("outputs", [])
    if not isinstance(summary, str):
        raise invalid("summary is not a string")
    if not isinstance(checks, list) or not all(
            isinstance(c, dict) and isinstance(c.get("cmd"), str) and _is_int(c.get("exit")) for c in checks):
        raise invalid("checks is not a list of {cmd: str, exit: int}")
    if not _str_list(outputs):
        raise invalid("outputs is not a list of strings")
    if refusals:
        state = "blocked"
    elif head != sha:
        raise ResultError("stale-input", f"head {head} is not the pinned {sha}")
    return {"state": state, "head": head, "summary": summary,
            "checks": [{"cmd": c["cmd"], "exit": c["exit"]} for c in checks], "outputs": outputs,
            "refusals": refusals}


def _unquote(s):
    """Decode the git C-quoted string at the start of s -> (path, rest), or None when it is malformed."""
    out, i = bytearray(), 1
    while i < len(s):
        c = s[i]
        if c == '"':
            return out.decode("utf-8", "replace"), s[i + 1:]
        if c != "\\":
            out += c.encode("utf-8")
            i += 1
            continue
        esc, octal = s[i + 1:i + 2], s[i + 1:i + 4]
        if esc in _C_ESCAPES:
            out.append(_C_ESCAPES[esc])
            i += 2
        elif re.fullmatch(r"[0-3][0-7]{2}", octal):
            out.append(int(octal, 8))
            i += 4
        else:
            return None
    return None


def _token(s, prefix):
    """One path from a `---`, `+++`, rename or copy line; None for /dev/null. Malformed quoting keeps the raw
    text, so custody fails closed."""
    if s.startswith('"'):
        q = _unquote(s)
        path = q[0] if q else s
    else:
        path = s.split("\t", 1)[0]
    if path == "/dev/null":
        return None
    return path[len(prefix):] if prefix and path.startswith(prefix) else path


def _header(rest):
    """Paths named by `diff --git A B`. Unquoted names may hold spaces, so try the symmetric split git itself
    prefers, then the first ` b/`; rename and copy lines add the exact names."""
    if rest.startswith('"'):
        q = _unquote(rest)
        if q is None:
            return [rest]
        a, tail = q
        return [a[2:] if a.startswith("a/") else a, _token(tail[1:] if tail.startswith(" ") else tail, "b/")]
    if '"' in rest:
        i = rest.index('"')
        return [_token(rest[:i - 1] if rest[i - 1:i] == " " else rest[:i], "a/"), _token(rest[i:], "b/")]
    n = len(rest)
    if n % 2 and rest[n // 2] == " ":
        a, b = rest[:n // 2], rest[n // 2 + 1:]
        if a.startswith("a/") and b.startswith("b/") and a[2:] == b[2:]:
            return [a[2:]]
        if a == b:
            return [a]
    i = rest.find(" b/")
    return [_token(rest[:i], "a/"), _token(rest[i + 1:], "b/")] if i > 0 else [rest]


def diff_paths(diff_text) -> list[str]:
    """Every path a unified git diff touches, both sides of renames and copies, in first-seen order; /dev/null
    skipped. Hunk bodies are skipped by their line counts, so a content line such as `--- x` is never a path."""
    seen = {}
    old = new = 0
    for line in diff_text.split("\n"):
        if old > 0 or new > 0:
            tag = line[:1]
            if tag in (" ", "", "\r"):
                old, new = old - 1, new - 1
                continue
            if tag == "-":
                old -= 1
                continue
            if tag == "+":
                new -= 1
                continue
            if tag == "\\":
                continue
            old = new = 0
        line = line.rstrip("\r")
        found = []
        if line.startswith("diff --git "):
            found = _header(line[len("diff --git "):])
        elif line.startswith("@@ "):
            m = _HUNK.match(line)
            if m:
                old, new = int(m.group(1) or "1"), int(m.group(2) or "1")
        elif line.startswith("--- "):
            found = [_token(line[4:], "a/")]
        elif line.startswith("+++ "):
            found = [_token(line[4:], "b/")]
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            found = [_token(line.split(" ", 2)[2], "")]
        for p in found:
            if p is not None and p != "/dev/null":
                seen.setdefault(p, None)
    return list(seen)


def custody_violations(paths, allowed) -> list[str]:
    """Paths outside `allowed` (directory prefixes ending `/`, or exact files), in input order. An absolute,
    `..`, backslash or otherwise unplain path always violates; a bad `allowed` entry raises ValueError."""
    if isinstance(paths, str) or isinstance(allowed, str):
        raise ValueError("paths and allowed must be lists, not one string")
    allow = list(allowed)
    for a in allow:
        if not _rel_ok(a):
            raise ValueError(f"bad allowed path {a!r}")
    out = {}
    for p in paths:
        ok = _rel_ok(p) and not p.endswith("/") and any(
            p.startswith(a) if a.endswith("/") else p == a for a in allow)
        if not ok:
            out.setdefault(p, None)
    return list(out)
