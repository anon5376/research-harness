"""Job contracts: parse a TOML job file, then decide before launch whether it may run.

preflight() is pure. It returns ready, wait (inputs or upstream not ready yet), hold (a person must change the job),
or operator (the brief asks for an operator-only step, which must be split out of the brief)."""
from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from rh import seats

KINDS = ("codex-cloud", "local-shell")
CAPABILITIES = ("commit", "push", "git", "python", "net", "loopback", "sysctl", "gpu", "browser")
JOB_ID_RE = re.compile(r"^(?!.*\.\.)(?!.*(?:\.|\.lock)$)[a-z0-9][a-z0-9._-]{0,62}\Z")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Operator-only verbs. Leading negations also cover the rest of a comma-separated list.
_VERBS = [re.compile(p, re.I) for p in (
    r"\bgit\s+push\b",
    r"\bpush(?:es|ed|ing)?\b(?!\s+notifications?\b)",
    r"\b(?:open(?:s|ing)?|create|submit)\s+(?:(?:a|the|your)\s+)?(?:pr|pull\s+request)\b",
    r"\bpublish(?:es|ed)?\b",  # not "publishing", which is usually a noun
    r"\bdeploy(?:s|ed)?\b",
    r"\b(?:buy|purchase|pay\s+for|subscribe\s+to)\b",
    r"\bsend(?:s|ing)?\b.{0,40}\b(?:e-?mail|message|dm|sms|invite|newsletter)s?\b",
    r"\b(?:e-?mail|message|dm)\s+(?:a|an|the|this|that|these|those|some|any|each|every|all|both|"
    r"my|your|his|her|its|our|their|me|you|him|it|us|them|someone|somebody|everyone|everybody)\b",
    r"\bpost(?:s|ed|ing)?\b.{0,40}\b(?:on|to)\b.{0,30}\b(?:x|twitter|slack|discord|linkedin|reddit|forum|blog)\b",
    r"\bannounce(?:s|d|ing)?\b",
    r"\b(?:sign|log)\s*(?:in|on|into)\b",
    r"\b(?:change|toggle|enable|disable)\b.{0,40}\bsettings?\b",
)]
_NEGATION = re.compile(r"\b(?:never|not|no|without|forbid(?:den)?|avoid)\b|n['’]t\b", re.I)
_CLAUSE = re.compile(r"[.;:!?\n]+|\bthen\b", re.I)
_LEADING_NEGATION = re.compile(r"^\s*(?:do\s+not|don['’]t|never|not|no|without|forbid(?:den)?|avoid)\b", re.I)


class ContractError(ValueError):
    pass


@dataclass(frozen=True)
class Decision:
    verdict: str  # ready | wait | hold | operator
    reasons: tuple = ()
    operator_steps: tuple = ()


@dataclass
class View:
    """Facts preflight needs about the world. The supervisor builds one per tick; tests build them by hand."""
    states: dict = field(default_factory=dict)  # job id -> state
    live_roots: dict = field(default_factory=dict)  # job id -> write root, or [root, *write_paths], for live jobs
    input_root: Path = Path(".")
    pinned: tuple = (True, "")  # (ready, reason) for a cloud job's pinned commit


def load(source):
    """Parse a job TOML file (path) or string and return a normalized spec dict, or raise ContractError."""
    text = Path(source).read_text() if isinstance(source, Path) else source
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ContractError(f"not valid TOML: {e}") from None
    return normalize(raw)


def normalize(raw):
    """Validate one job table (as parsed from TOML) and fill its defaults; raise ContractError."""
    spec = dict(raw)
    jid = spec.get("id")
    if not isinstance(jid, str) or not JOB_ID_RE.match(jid):
        raise ContractError("id must match " + JOB_ID_RE.pattern)
    if spec.get("kind") not in KINDS:
        raise ContractError(f"kind must be one of {', '.join(KINDS)}")
    if not isinstance(spec.get("brief"), str) or not spec["brief"].strip():
        raise ContractError("brief must be a non-empty string")
    spec.setdefault("needs", [])
    unknown = [n for n in spec["needs"] if n not in CAPABILITIES]
    if unknown:
        raise ContractError(f"unknown needs {unknown}; known: {', '.join(CAPABILITIES)}")
    spec.setdefault("write_root", f"runs/{jid}/")
    if not spec["write_root"].endswith("/"):
        spec["write_root"] += "/"
    if spec["kind"] == "codex-cloud" and spec["write_root"] != f"runs/{jid}/":
        raise ContractError("cloud jobs write to runs/<id>/; put other paths in write_paths")
    spec.setdefault("write_paths", [])
    spec.setdefault("after", [])
    spec.setdefault("inputs", [])
    for i in spec["inputs"]:
        if not isinstance(i, dict) or not isinstance(i.get("path"), str):
            raise ContractError("each input needs a path")
        if "sha256" in i and not SHA256_RE.match(i["sha256"]):
            raise ContractError(f"input {i['path']}: sha256 must be 64 lowercase hex")
    spec.setdefault("revision", 1)
    if not isinstance(spec["revision"], int) or spec["revision"] < 1:
        raise ContractError("revision must be an integer >= 1")
    spec.setdefault("changed", "")
    spec.setdefault("prior", "")
    spec.setdefault("heavy", False)
    spec.setdefault("probe", False)
    spec.setdefault("operator_facing", False)
    spec.setdefault("card", {})
    spec.setdefault("model", {})
    spec.setdefault("accept", "")
    spec.setdefault("timeout_min", 120)
    if spec["kind"] == "local-shell":
        cmd = spec.get("command")
        if not cmd or not isinstance(cmd, list) or not all(isinstance(a, str) for a in cmd):
            raise ContractError("a local-shell job needs a command: a list of argv strings")
    return spec


def preflight(spec, caps, policy, view):
    holds, waits = [], []
    kind = spec["kind"]
    kcaps = caps.get(kind, {})
    for need in spec["needs"]:
        have = kcaps.get(need, "unknown")
        if have is False:
            holds.append(f"needs {need}, but {kind} cannot")
        elif have is not True and not spec["probe"]:  # "unknown" or anything not literally true
            holds.append(f"needs {need}, but {kind} capability is unknown: run a probe job first")
    if spec["revision"] >= 2 and not (spec["changed"].strip() and spec["prior"].strip()):
        holds.append(f"revision {spec['revision']} needs both changed (what input changed) and prior (the last result)")
    if spec["operator_facing"]:
        card = spec["card"]
        lacking = [k for k in ("said", "form", "done_when") if not str(card.get(k, "")).strip()]
        if lacking:
            holds.append(f"operator-facing job needs an acceptance card; missing {', '.join(lacking)}")
    holds += _policy_holds(spec, policy)
    if "seat" in spec:
        path = Path(view.input_root) / "seats.toml"
        try:
            holds += seats.holds(spec, seats.load(path) if path.is_file() else {})
        except ValueError as e:
            holds.append(f"seats.toml: {e}")
    holds += _root_holds(spec, view)
    steps = operator_steps(spec["brief"]) + operator_steps(spec["accept"])  # accept runs here for every kind
    if kind == "local-shell":
        steps += operator_steps(" ".join(spec["command"]))
    for up in spec["after"]:
        st = view.states.get(up)
        if st is None:
            holds.append(f"after {up}: no such job")
        elif st == "done":
            continue
        elif st in ("failed", "blocked", "capacity", "truncated", "aborted", "no_result", "cancelled"):
            holds.append(f"after {up}: it ended {st}")
        else:
            waits.append(f"waiting for {up} ({st})")
    for i in spec["inputs"]:
        p = view.input_root / i["path"]
        if not p.is_file():
            waits.append(f"input missing: {i['path']}")
        elif "sha256" in i and file_sha256(p) != i["sha256"]:
            holds.append(f"input changed since the job was written: {i['path']}")
    if kind == "codex-cloud" and not view.pinned[0]:
        waits.append(view.pinned[1] or "pinned commit not ready")
    if holds:
        return Decision("hold", tuple(holds + waits), tuple(steps))
    if steps:
        return Decision("operator", tuple(f"operator step in brief: {s}" for s in steps), tuple(steps))
    if waits:
        return Decision("wait", tuple(waits))
    return Decision("ready")


def operator_steps(brief):
    out = []
    for sentence in _CLAUSE.split(brief):
        negated = False
        for clause in sentence.split(","):
            for m in (m for v in _VERBS for m in v.finditer(clause)):
                words = re.findall(r"\b\w+(?:['’]\w+)*\b", clause[:m.start()])
                if not negated and not _NEGATION.search(" ".join(words[-3:])):
                    out.append(clause.strip())
                    break
            negated = negated or bool(_LEADING_NEGATION.match(clause))
    return out


def _policy_holds(spec, policy):
    rules = policy.get(spec["kind"], {})
    model = spec["model"]
    out = []
    req = model.get("requested")
    if req and rules.get("models") is not None and req not in rules["models"]:
        out.append(f"model {req} is not in the policy dated {policy.get('dated', '?')}: allowed {rules['models']}")
    for key, rule in (("speed", "speeds"), ("effort", "efforts")):
        val = model.get(key)
        if val and rules.get(rule) is not None and val not in rules[rule]:
            out.append(f"{key} {val} is not in the policy dated {policy.get('dated', '?')}: allowed {rules[rule]}")
    return out


def _root_holds(spec, view):
    out = []
    roots = [spec["write_root"], *spec["write_paths"]]
    for r in roots:
        parts = r.rstrip("/").split("/")
        if PurePosixPath(r).is_absolute() or not r.strip("/") or any(p in ("", ".", "..") for p in parts):
            out.append(f"write path {r!r} must be relative and normal: no '.', '..', or empty segments")
    for other, theirs in view.live_roots.items():
        theirs = [theirs] if isinstance(theirs, str) else list(theirs)
        if other == spec["id"] or not theirs:
            continue
        hit = [t for t in theirs if any(_overlap(r, t) for r in roots)]
        if hit:
            out.append(f"write paths overlap live job {other} ({', '.join(hit)})")
    return out


def _overlap(a, b):
    a, b = a.rstrip("/") + "/", b.rstrip("/") + "/"
    return a.startswith(b) or b.startswith(a)


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def load_caps(*paths):
    """Merge capability tables; later files override earlier ones key by key. Missing files are skipped."""
    out = {}
    for p in paths:
        p = Path(p)
        if p.is_file():
            for kind, table in tomllib.loads(p.read_text()).items():
                out.setdefault(kind, {}).update(table)
    return out


def load_policy(path):
    return tomllib.loads(Path(path).read_text())
