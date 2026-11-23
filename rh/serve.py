"""A small, authenticated browser front end to the existing CLI: the app page, its script and the everyday actions.
While it serves, it also runs the harness's checks itself, holding the supervisor's lock, so a job moves on its own."""
from __future__ import annotations

import contextlib
import html
import http.server
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import shutil
import shlex
import socket
import socketserver
import threading
import time
import tomllib
import urllib.parse
import webbrowser

from rh import chat, cli, config, contract, dashboard, gitx, goals, lease, oslock, seats
from rh.ledger import TERMINAL, Ledger
from rh.supervisor import AlreadyRunning, single_instance


# Redirecting stdout/stderr and selecting RH_HOME affect the entire process, so
# even separate server instances must serialize their calls.
_CLI_LOCK = threading.Lock()
_MAX_BODY = 32 * 1024 * 1024
_LOG_TAIL = 64 * 1024  # bytes of a local job's out.log the job pane shows, at most
_LOG_LINES = 200
_FILES = 50  # files a receipt lists, at most
_PEEK = 64 * 1024  # a text file up to this size is previewed in the receipt
_PEEK_LINES = 40
_FILE_MAX = 1024 * 1024  # bytes GET /job/<id>/file serves, at most
# The page uses inline styles and bundled same-origin assets. Outside pages cannot frame it.
_CSP = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; font-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
_FONTS = {f"/fonts/{name}": dashboard._WEB / "fonts" / name for name in dashboard.FONTS}
_ART = {f"/{name}": dashboard._WEB / name for name in dashboard.BACKGROUNDS}
_HOST_TTL = 2.0  # seconds a reading of this machine's load and memory pressure is reused across page loads
_SETTLED = 86400  # how far back the Inbox looks for settled items
_POSTS = ("/inbox/resolve", "/stop", "/resume", "/attach", "/harvest", "/land", "/tick", "/job", "/job/cancel",
          "/checks", "/again", "/setup", "/handoff", "/reply", "/appearance", "/settings/swarm")
# The app's check pace: every _FAST seconds for _BURST seconds after something changes; every _BUSY seconds while a
# job is about to start or a job on this machine runs, or every _SHARED when a cloud job is also live, because each
# pass asks Codex about it; otherwise at the supervisor's own interval. While rh supervise holds the lock, the app
# tries again every _RETRY seconds.
_FAST, _BURST, _BUSY, _SHARED, _RETRY = 1.5, 20, 3, 15, 5
_TASK = re.compile(r"task_[A-Za-z0-9_]+")
_ENV_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_SETUP = "create and publish the cloud environment"  # how the supervisor's setup item begins


def _call(cfg, argv, split=False):
    output = io.StringIO()
    errors = io.StringIO() if split else output
    with _CLI_LOCK:
        previous = os.environ.get("RH_HOME")
        try:
            os.environ["RH_HOME"] = str(cfg["home"])
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                try:
                    code = cli.main(argv)
                except SystemExit as error:
                    code = error.code if isinstance(error.code, int) else 1
                except Exception as error:
                    # Filesystem and runtime errors should be visible in the app,
                    # just like the CLI's ordinary validation errors.
                    print(f"rh: {error}")
                    code = 1
        finally:
            if previous is None:
                os.environ.pop("RH_HOME", None)
            else:
                os.environ["RH_HOME"] = previous
    return (code, output.getvalue(), errors.getvalue()) if split else (code, output.getvalue())


def _result(code, output, say=""):
    """What the page reports about the last action: one sentence, and the CLI's own words, bounded to 2000 escaped
    characters without splitting an HTML entity."""
    parts = []
    size = 0
    for char in output:
        escaped = html.escape(char, quote=True)
        if size + len(escaped) > 2000:
            break
        parts.append(char)
        size += len(escaped)
    return {"code": code, "say": say, "output": "".join(parts)}


def _actions(token):
    hidden = f'<input type="hidden" name="t" value="{html.escape(token, quote=True)}">'

    def field(name, *, kind="text", required=False):
        requirement = " required" if required else ""
        return (f'<label>{name} <input type="{kind}" name="{name}"{requirement}></label>')

    scopes = field("job") + field("route") + field("pause", kind="checkbox")
    forms = (
        ("/inbox/resolve", "Resolve inbox item", field("id", required=True) + field("note", required=True)),
        ("/stop", "Stop", field("reason", required=True) + scopes),
        ("/resume", "Resume", scopes),
        ("/handoff", "Launch by hand", field("job", required=True)),
        ("/attach", "Attach", field("job", required=True) + field("task_id", required=True)),
        ("/harvest", "Harvest", field("job", required=True) +
         '<label>Saved patch or bundle reply <textarea name="text" rows="12"></textarea></label>'),
        ("/land", "Land", field("job", required=True)),
        ("/job/cancel", "Cancel job", field("job", required=True) + field("reason", required=True)),
        ("/tick", "Supervisor tick", ""),
    )
    body = "".join(f'<section><h2>{title}</h2><form method="post" action="{path}">'
                   f'{hidden}{fields}<button type="submit">{title}</button></form></section>'
                   for path, title, fields in forms)
    link = html.escape("/?" + urllib.parse.urlencode({"t": token}), quote=True)
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Research harness actions</title><style>'
            'body{font:1rem/1.5 system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:1rem}'
            'section{border-top:1px solid #999;padding:1rem 0}label{display:block;margin:.5rem 0}'
            'textarea{display:block;width:100%;box-sizing:border-box}'
            '</style></head><body><main><h1>Actions</h1>'
            f'<nav><a href="{link}">Dashboard</a></nav>{body}</main></body></html>')


def _value(fields, name):
    values = fields.get(name, [""])
    if len(values) != 1:
        raise ValueError(f"give one {name}")
    return values[0]


def _upload(cfg, job, suffix, text):
    """Write text to a new file in $RH_HOME/uploads and return its path."""
    uploads = Path(cfg["home"]) / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    milliseconds = time.time_ns() // 1_000_000
    while True:
        dest = uploads / f"{job}-{milliseconds}{suffix}"
        try:
            with dest.open("x", encoding="utf-8", newline="") as file:
                file.write(text)
            return dest
        except FileExistsError:
            milliseconds += 1


def _draft(fields):
    """The New job form as typed, kept so a refused job can be corrected rather than retyped."""
    draft = {name: _value(fields, name) for name in ("id", "kind", "brief", "command", "accept", "timeout_min",
                                                    "model", "effort", "input_path", "answer")}
    draft["needs"] = fields.get("needs", [])
    return draft


def _toml(spec):
    """One job table as TOML. JSON's string escapes are valid TOML basic-string escapes, except that TOML also
    forbids a raw DEL; tables nested in a job (its model, its card, its inputs) are written inline."""
    def key(k):
        return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else value(k)

    def value(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, str):
            return json.dumps(v, ensure_ascii=False).replace("\x7f", "\\u007f")
        if isinstance(v, int):
            return str(v)
        if isinstance(v, list):
            return "[" + ", ".join(value(x) for x in v) + "]"
        if isinstance(v, dict):
            return "{" + ", ".join(f"{key(k)} = {value(x)}" for k, x in v.items()) + "}"
        raise ValueError(f"cannot write {v!r}")
    return "".join(f"{key(k)} = {value(v)}\n" for k, v in spec.items())


def _job_text(spec):
    """The TOML for a job spec, checked: the contract must take it, and it must read back as written."""
    text = _toml(spec)
    contract.normalize(tomllib.loads(text))
    if tomllib.loads(text) != spec:
        raise ValueError("the job did not survive being written as TOML")
    return text


def _repository_input(raw, repo):
    value = raw.strip()
    if not value:
        return None
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or "\\" in value or ":" in value:
        raise ValueError("an input must be a path relative to the repository")
    root = Path(repo).resolve()
    if not (root / path).resolve().is_relative_to(root):
        raise ValueError("an input must stay inside the repository")
    return {"path": path.as_posix()}


def _auto_id(cfg, fields):
    """A job typed without a name gets one from its first words, made unique against the ledger."""
    if _value(fields, "id").strip():
        return
    words = re.findall(r"[a-z0-9]+", _value(fields, "brief").lower())[:6] or ["job"]
    base = "-".join(words)[:40].strip("-") or "job"
    led = Ledger(Path(cfg["home"]) / "ledger.db")
    try:
        taken = {j["id"] for j in led.jobs()}
    finally:
        led.close()
    name, n = base, 2
    end = ".best" if "swarm" in fields else ""  # a swarm is named by its judge, <name>.best
    while name + end in taken:
        name, n = f"{base}-{n}", n + 1
    fields["id"] = [name]


def _job_file(draft, repo):
    """The TOML file `rh job add` reads for a New job form; the contract rejects what it would reject."""
    agent = draft["kind"] == "local-agent"  # Codex on this Mac: a local job whose command is rh/agent.py
    spec = {"id": draft["id"].strip(), "kind": "local-shell" if agent else draft["kind"],
            "brief": draft["brief"].replace("\r\n", "\n")}
    if item := _repository_input(draft["input_path"], repo):
        spec["inputs"] = [item]
    if spec["kind"] != "local-shell":
        model = {key: draft[field].strip() for field, key in (("model", "requested"), ("effort", "effort"))
                 if draft[field].strip() and (field != "effort" or spec["kind"] == "codex-cloud")}
        if model:
            spec["model"] = model
    if draft["accept"].strip():
        spec["accept"] = draft["accept"].strip()
    if agent or spec["kind"] == "codex-cloud":  # agents leave an answer.md; the composer says where it is shown
        answer = draft["answer"].strip() or "chat"
        if answer not in ("chat", "file"):
            raise ValueError("answer must be chat or file")
        spec["answer"] = answer
    if draft["needs"]:
        spec["needs"] = list(draft["needs"])
    timeout = draft["timeout_min"].strip()
    if timeout:
        if not timeout.isascii() or not timeout.isdigit() or not 0 < int(timeout) <= 10080:
            raise ValueError("the time limit is a whole number of minutes, at most 10080")
        spec["timeout_min"] = int(timeout)
    if agent:
        spec["command"] = _agent_command(spec["brief"], draft["model"].strip(), draft["effort"].strip(), repo)
    elif spec["kind"] == "local-shell":
        try:  # the shell worker runs the brief as its command unless Options names another
            spec["command"] = shlex.split(draft["command"].strip() or draft["brief"])
        except ValueError as error:
            raise ValueError(f"command: {error}") from None
    return spec["id"], _job_text(spec)


SWARM_MAX = 4  # runs in one swarm; each is an agent on its own copy, so a few at once is plenty
_SWARM = re.compile(r"\A\s*/swarm(?:\s+|\Z)")


def _swarm_brief(fields):
    """The brief after /swarm, or None when the composer did not ask for a swarm."""
    brief = _value(fields, "brief")
    found = _SWARM.match(brief)
    return brief[found.end():].strip() if found else None


def swarm_choices():
    """The models a swarm may run: Claude's and Devin's in the dated policy. Codex models are left out, so a swarm
    never spends the owner's ChatGPT plan."""
    policy_path = config.REPO / "policy.toml"
    policy = contract.load_policy(policy_path) if policy_path.is_file() else {}
    return [m for table in ("devin-local", "claude-local") for m in policy.get(table, {}).get("models", [])]


def _swarm_file(cfg, draft):
    """A /swarm from the composer as a goal file, (judge id, TOML): one run of the brief on each model Settings
    names (w1, w2, ...), then a judge that waits until they have all ended and keeps the best work of those that
    finished, as its own answer and changes."""
    swarm = cfg.get("swarm", {})
    models = [m for m in swarm.get("models", []) if m][:SWARM_MAX]
    judge = swarm.get("judge") or (models[0] if models else "")
    if not models:
        raise ValueError("no swarm models are set: choose them in Settings")
    allowed = swarm_choices()
    if bad := [m for m in (*models, judge) if m not in allowed]:
        raise ValueError(f"swarm model {bad[0]} is not a Claude or Devin model in the policy: allowed {allowed}")
    gid, home = draft["id"].strip(), Path(cfg["home"])
    jobs = []
    for n, model in enumerate(models, 1):
        _, text = _job_file({**draft, "id": f"w{n}", "kind": "local-agent", "model": model, "effort": ""},
                            cfg["repo"]["path"])
        jobs.append(tomllib.loads(text))
    _, text = _job_file({**draft, "id": "best", "kind": "local-agent", "model": judge, "effort": ""},
                        cfg["repo"]["path"])
    best = tomllib.loads(text)
    for job in jobs:
        best["command"] += ["--swarm", str(home / "runs" / f"{gid}.{job['id']}")]
    best["after"], best["after_any"] = [job["id"] for job in jobs], True
    said = draft["brief"].replace("\r\n", "\n").strip()
    card = {"said": said, "form": "the chosen run's answer and its changes",
            "done_when": "the judge has kept the best work of the runs that finished"}
    text = (_toml({"id": gid}) + "\n[card]\n" + _toml(card)
            + "".join("\n[[jobs]]\n" + _toml(job) for job in (*jobs, best)))
    for raw in goals.expand(tomllib.loads(text)):
        contract.normalize(raw)
    return f"{gid}.best", text


def _agent_command(brief, model, effort, repo, prior=None):
    """The command of a local agent job: rh/agent.py with the CLI of the provider the model names, its model and effort
    in that CLI's words, and, for a follow-up, the folder of the run it continues."""
    # The model names its provider; each provider's models come from its own table in the dated policy.
    policy_path = config.REPO / "policy.toml"
    policy = contract.load_policy(policy_path) if policy_path.is_file() else {}
    tables = {"codex": "codex-cloud", "claude": "claude-local", "devin": "devin-local"}
    provider = "codex" if not model else next(
        (name for name, table in tables.items() if model in policy.get(table, {}).get("models", [])), None)
    if provider is None:
        allowed = [m for table in tables.values() for m in policy.get(table, {}).get("models", [])]
        raise ValueError(f"model {model} is not in the model policy: allowed {allowed}")
    binary = shutil.which(provider)
    if not binary:
        raise ValueError(f"{provider.capitalize()} is not installed on this Mac: the {provider} command was not found")
    extra = ["--model", model] if model else []
    if provider == "codex":
        extra += ["-c", 'service_tier="default"']  # the policy keeps fast off
    if effort and provider != "devin":  # Devin's effort is part of its model's name
        efforts = policy.get({"codex": "codex-local"}.get(provider, tables[provider]), {}).get("efforts", [])
        if effort not in efforts:
            raise ValueError(f"effort {effort} is not in the policy for {provider}: allowed {efforts}")
        cli = {"light": "low", "extra-high": "xhigh"}.get(effort, effort)
        extra += ["-c", f'model_reasoning_effort="{cli}"'] if provider == "codex" else ["--effort", cli]
    if prior:
        extra += ["--prior", str(prior)]
    return [sys.executable, str(Path(__file__).with_name("agent.py")), provider, binary, str(repo), brief, *extra]


def _again(cfg, old, changed, input_path="", model="", effort=""):
    """The next revision of a finished job, as (id, TOML): the same contract under the next free name, pointing back
    at the job it follows, with what changed (a follow-up is also added to the brief)."""
    led = Ledger(Path(cfg["home"]) / "ledger.db")
    try:
        job, taken = led.job(old), {j["id"] for j in led.jobs()}
    finally:
        led.close()
    if job is None:
        raise ValueError(f"no job {old}")
    if job["state"] not in TERMINAL:
        raise ValueError("only a finished job can run again")
    spec = dict(job["spec"])
    revision = int(spec.get("revision") or 1) + 1
    stem = re.sub(r"-r\d+$", "", old)
    new = f"{stem}-r{revision}"
    while new in taken:
        revision += 1
        new = f"{stem}-r{revision}"
    if not contract.JOB_ID_RE.fullmatch(new):
        raise ValueError("this job's name is too long to number another run")
    custom = spec.get("write_root") != f"runs/{old}/"
    spec = {k: v for k, v in spec.items() if k not in ("id", "write_root", "revision", "changed", "prior")}
    spec = {"id": new, **spec, "revision": revision, "prior": old,
            "changed": changed or "run again with no changes"}
    if custom:
        spec["write_root"] = job["spec"]["write_root"]
    if changed:
        spec["brief"] = spec["brief"].rstrip() + "\n\nFollow-up: " + changed
    if was := dashboard.agent_of(spec.get("command")):
        # A local agent's next run continues the last one: the same brief plus the follow-up, its changes applied
        # to a fresh copy and its answer read first, on the model and effort picked in the reply box (else the same).
        _, old_model, old_effort = was
        model = model or old_model
        effort = effort if model == old_model or effort else ""
        effort = effort or (old_effort if model == old_model else "")
        spec["command"] = _agent_command(spec["brief"], model, effort, spec["command"][4],
                                         Path(cfg["home"]) / job["spec"]["write_root"])
    if item := _repository_input(input_path, cfg["repo"]["path"]):
        inputs = spec.get("inputs", [])
        if not any(existing["path"] == item["path"] for existing in inputs):
            spec["inputs"] = [*inputs, item]
            if not changed:
                spec["changed"] = f"added input {item['path']}"
    return new, _job_text(spec)


def _action(cfg, path, fields):
    value = lambda name: _value(fields, name)  # noqa: E731
    if path == "/inbox/resolve":
        # Marking a step done needs no essay; the CLI still records a note.
        return ["inbox", "resolve", "--", value("id"), value("note").strip() or "done in the app"]
    if path in ("/stop", "/resume"):
        argv = [path[1:], "--owner=app"]
        if path == "/stop":
            argv.append("--reason=" + (value("reason").strip() or "paused from the app"))
        for name in ("job", "route"):
            if value(name):
                argv.append(f"--{name}=" + value(name))
        if "pause" in fields:
            argv.append("--pause")
        return argv
    if path == "/attach":
        # The Codex web view gives a link more readily than a bare id; take the id out of a pasted link.
        task = value("task_id").strip()
        found = _TASK.search(task) if task.startswith(("http://", "https://")) else None
        return ["cloud", "attach", "--", value("job"), found.group(0) if found else task]
    if path == "/reply":
        job = value("job")
        if not contract.JOB_ID_RE.fullmatch(job):
            raise ValueError("invalid job id")
        dest = _upload(cfg, job, ".txt", value("text"))
        return ["chat", "reply", "--", job, str(dest)]
    if path == "/harvest":
        job, text = value("job"), value("text")
        if not contract.JOB_ID_RE.fullmatch(job):
            raise ValueError("invalid job id")
        dest = _upload(cfg, job, ".txt", text)
        if "RH-BUNDLE" in text:
            return ["cloud", "harvest", "--bundle", str(dest), "--", job]
        return ["cloud", "harvest", "--", job, str(dest)]
    if path == "/land":
        return ["land", "--", value("job")]
    if path == "/handoff":
        return ["cloud", "handoff", "--", value("job")]
    if path == "/job/cancel":
        return ["job", "cancel", "--reason=" + value("reason"), "--", value("job")]
    if path == "/job" and "swarm" in fields:
        _, text = _swarm_file(cfg, _draft(fields))
        return ["goal", "add", "--", str(_upload(cfg, value("id").strip(), ".toml", text))]
    if path == "/job":
        job, text = _job_file(_draft(fields), cfg["repo"]["path"])
        return ["job", "add", "--", str(_upload(cfg, job, ".toml", text))]
    if path == "/again":
        job, text = _again(cfg, value("job"), value("changed").strip(), value("input_path"),
                           value("model").strip(), value("effort").strip())
        return ["job", "add", "--", str(_upload(cfg, job, ".toml", text))]
    if path == "/setup":
        env = value("env_id").strip()
        if not _ENV_ID.fullmatch(env):
            raise ValueError("an environment ID is letters, digits, dot, dash, underscore or colon")
        return ["config", "set", "cloud.env_id", env]
    return ["supervise", "--once"]  # /tick without the app's checks, checked by the request handler


def _say(path, fields, ok, made=None):
    """The app's one sentence about an action; the CLI's own output stays available as its details."""
    get = lambda name: (fields.get(name) or [""])[0].strip()  # noqa: E731
    job, route = get("job"), get("route")
    what = job or ({"codex-cloud": "Codex cloud", "local-shell": dashboard.HERE}.get(route, route)
                   if route else "every launch")
    if not ok:
        return {"/job": "The job was not added.", "/again": "The job did not start again.",
                "/setup": "Codex cloud was not set up.", "/tick": "The check did not finish.",
                "/attach": "The task was not linked.", "/reply": "The reply was not saved."}.get(path, "That did not work.")
    pause = "pause" in fields
    return {
        "/job": (f"Started a swarm. {made} keeps the best work." if "swarm" in fields else f"Added {made or get('id')}."),
        "/again": f"Added {made}, the next run of {job}.",
        "/inbox/resolve": "Done. It is off your list.",
        "/stop": f"Paused {what}." if pause else f"Stopped {what}.",
        "/resume": f"Resumed {what}.",
        "/attach": f"Linked the task to {job}. It is running.",
        "/handoff": f"{job} is ready to launch by hand.",
        "/harvest": "Patch saved. The result is being checked.",
        "/reply": f"Saved the reply. {job} is done.",
        "/land": f"Saved to the local branch cloud/{job}.",
        "/job/cancel": f"Cancelled {job}.",
        "/tick": "Checked.",
        "/setup": "Environment ID saved. Cloud jobs start on the next check.",
    }.get(path, "Done.")


def _next(fields):
    """Where a form returns to: the page it was sent from, when that is one of the app's own pages."""
    values = fields.get("next", [])
    if len(values) != 1:
        return "/"
    target = values[0]
    if target in ("/", "/new", "/inbox"):
        return target
    if target.startswith("/job/") and contract.JOB_ID_RE.fullmatch(target[5:]):
        return target
    if re.fullmatch(r"/inbox/[0-9]{1,12}", target):
        return target
    return "/"


def _log(cfg, job):
    """The end of a local job's output, decoded leniently, and whether that end is less than the whole: past
    _LOG_TAIL bytes or _LOG_LINES lines it is cut. (None, False) when there is no output."""
    path = Path(cfg["home"]) / "jobs" / job / "out.log"
    try:
        with path.open("rb") as file:
            file.seek(0, os.SEEK_END)
            size = file.tell()
            file.seek(max(0, size - _LOG_TAIL))
            data = file.read()
    except OSError:
        return None, False
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > _LOG_TAIL and lines:
        lines = lines[1:]  # the first line is cut
    return "\n".join(lines[-_LOG_LINES:]), size > _LOG_TAIL or len(lines) > _LOG_LINES


def _outputs(cfg, shown):
    """The folder a job wrote into, or None: a local job's own folder under $RH_HOME, or a collected cloud job's
    write root in the scratch worktree the result was checked in. It always lies inside $RH_HOME."""
    job = shown.get("job") or {}
    spec, receipt = job.get("spec") or {}, shown.get("receipt") or {}
    home = Path(cfg["home"]).resolve()
    if spec.get("kind") == "local-shell":
        base = home
    elif receipt.get("worktree"):
        base = Path(receipt["worktree"])
    else:
        return None
    root = (base / spec.get("write_root", "")).resolve()
    return root if root.is_relative_to(home) and root != home and root.is_dir() else None


_ANSWER = 200_000  # characters of an answer shown in the chat; the file keeps the rest


def _thread(cfg, job, most=20):
    """The earlier runs of a job, oldest first, as the chat shows them: what was asked in each (the first run's brief,
    then each follow-up), the agent and model that ran it, its state and its answer if it left one."""
    led = Ledger(Path(cfg["home"]) / "ledger.db")
    try:
        runs, seen, prior = [], {job["id"]}, job["spec"].get("prior")
        while prior and prior not in seen and len(runs) < most:
            seen.add(prior)
            row = led.job(prior)
            if row is None:
                break
            runs.append(row)
            prior = row["spec"].get("prior")
    finally:
        led.close()
    thread = []
    for row in reversed(runs):
        spec = row["spec"]
        root = _outputs(cfg, {"job": row}) if row["state"] in TERMINAL else None
        said = root / "answer.md" if root else None
        answer = (said.read_text(encoding="utf-8", errors="replace")[:_ANSWER]
                  if said and said.is_file() and not said.is_symlink() else "")
        who = dashboard.agent_of(spec.get("command"))
        thread.append({"id": row["id"], "state": row["state"], "ask": dashboard.ask_of(spec), "answer": answer,
                       "provider": who[0] if who else "", "model": who[1] if who else ""})
    return thread


def _files(root, skip=()):
    """([{path, size}], peek): what a finished job left in its folder, and the start of its first small text file."""
    found, peek = [], None
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(root).as_posix()
        if rel in skip:
            continue
        size = path.stat().st_size
        found.append({"path": rel, "size": size})
        if peek is None and size <= _PEEK:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            lines = text.splitlines()
            peek = {"path": rel, "text": "\n".join(lines[:_PEEK_LINES]), "more": len(lines) > _PEEK_LINES}
        if len(found) >= _FILES:
            break
    return found, peek


def _tail(cfg, jid, most=5):
    """The last lines a local job printed (its out.log under $RH_HOME), or "" when it printed none."""
    log = Path(cfg["home"]) / "jobs" / str(jid) / "out.log"
    try:
        if log.is_symlink() or not log.is_file():
            return ""
        with log.open("rb") as f:
            f.seek(max(0, log.stat().st_size - 4096))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.rstrip("\n").splitlines()[-most:])


_HEADS = {}  # answer.md path -> (mtime_ns, first line): the list asks for every done job's on each page


def _said_head(cfg, job):
    """The first line of a done local job's answer.md, which names the run in the lists better than its brief when
    the same brief ran many times; "" when it left none."""
    if job.get("state") != "done" or job["spec"].get("kind") != "local-shell":
        return ""
    root = _outputs(cfg, {"job": job})
    said = root / "answer.md" if root else None
    try:
        if said is None or said.is_symlink():
            return ""
        stamp = said.stat().st_mtime_ns
        if _HEADS.get(said, (None,))[0] != stamp:
            with said.open("r", encoding="utf-8", errors="replace") as f:
                _HEADS[said] = (stamp, dashboard.answer_head(f.read(600)))
        return _HEADS[said][1]
    except OSError:
        return ""


def _meta(cfg):
    """What the status view leaves out and the app needs: each job's brief, kind, seat, time limit, times and the run
    it follows; the inbox items settled in the last day; and, for the Inbox, the one item asked for, open or not."""
    led = Ledger(Path(cfg["home"]) / "ledger.db")
    try:
        meta = {j["id"]: {"brief": j["spec"].get("brief", ""), "kind": j["spec"].get("kind", ""),
                          "seat": j["spec"].get("seat", ""), "timeout_min": j["spec"].get("timeout_min"),
                          "created": j.get("created"), "started": j.get("started"), "updated": j.get("updated"),
                          "prior": j["spec"].get("prior", ""), "said": _said_head(cfg, j),
                          "model": (dashboard.agent_of(j["spec"].get("command")) or ("", ""))[1],
                          "goal": j["spec"].get("goal", ""),
                          # a swarm's judge names its runs, so the app shows them as its work
                          "after": list(j["spec"].get("after") or []) if j["spec"].get("after_any") else []}
                for j in led.jobs()}
        since = time.time() - _SETTLED
        items = led.inbox(open_only=False)
        settled = [i for i in items if i.get("resolved") and i["resolved"] >= since][-100:]
        return meta, settled, {i["id"]: i for i in items}
    finally:
        led.close()


def _seats(cfg):
    """The seat cards in the repository's seats.toml, as the app shows them; none when it is missing or unreadable."""
    path = Path(cfg["repo"]["path"]) / "seats.toml"
    try:
        cards = seats.load(path) if path.is_file() else {}
    except (OSError, ValueError):
        return []
    return [{"name": name, "role": card["role"], "route": card["route"], "model": card["model"]}
            for name, card in cards.items()]


def _host(cfg, reader=None):
    """This machine as the heavy-step lease sees it: load, memory pressure, whether a heavy step may start, and
    whether the lease is in use. A reading that fails is reported, not guessed."""
    try:
        reading = (reader or lease.read_host)()
    except Exception as error:  # noqa: BLE001  (a host that cannot be read must not break the page)
        return {"error": f"{type(error).__name__}: {error}"[:300]}
    limit = float(cfg["lease"]["max_load"])
    ok, reason = lease.admit(reading, limit)
    try:
        held = lease.status(cfg["lease"]["lock"])
    except OSError:
        held = None
    return {"load1": reading.load1, "level": reading.level, "max_load": limit, "ok": ok,
            "reason": "" if ok else reason, "lease": held and {"held": held["held"], "age_s": held["age_s"]}}


class _Checks:
    """The app's own check loop. While rh serve runs it does what rh supervise does, under the same lock
    (supervisor.lock), so one loop runs per $RH_HOME: while rh supervise holds the lock the app only watches, and
    Pause stops the loop and lets the lock go. `supervisor` makes the Supervisor; tests pass a fake."""

    def __init__(self, cfg, supervisor=None):
        self.cfg, self.home, self.make = cfg, Path(cfg["home"]), supervisor or self._supervisor
        self.on, self.fd, self.sup, self.other, self.failed, self.last = True, None, None, None, None, None
        self.recovered = False
        self.due, self.burst, self.once, self.answered = 0.0, 0.0, False, 0
        self.cv, self.done = threading.Condition(), threading.Event()
        self.thread = threading.Thread(target=self.run, name="rh-checks", daemon=True)

    def _supervisor(self):
        from rh.adapters.codex_cloud import CodexCloud
        from rh.supervisor import Supervisor
        return Supervisor(Ledger(self.home / "ledger.db"), self.cfg,
                          cloud=CodexCloud(codex=self.cfg["cloud"]["codex"], cwd=cli._codex_cwd(self.cfg)))

    def run(self):
        while not self.done.is_set():
            with self.cv:
                once, self.once = self.once, False
                active = self.on or once
            if active and self.fd is None:
                self._take()
            if active and self.fd is not None and not once and self._added():
                self.due = 0.0  # a job added from the terminal starts as soon as one from the app would
            if active and self.fd is not None and (once or time.monotonic() >= self.due):
                self._tick()
            if not self.on and self.fd is not None:
                self._release()  # a check asked for while paused lets the lock go again
            with self.cv:
                self.answered += once
                self.cv.notify_all()
                if not self.once and not self.done.is_set():
                    self.cv.wait(self._sleep())
        if self.fd is not None:
            self._release()
        led, self.sup = getattr(self.sup, "led", None), None
        if led is not None:
            led.close()  # Windows cannot remove a home whose ledger.db is still open

    def _take(self):
        try:
            self.fd = single_instance(self.home)
        except AlreadyRunning as error:
            found = re.search(r"\(pid ([^)]+)\)", str(error))
            self.other = found.group(1) if found else "unknown"
            return
        self.other, self.due, self.recovered = None, 0.0, False  # taking over from rh supervise, it checks at once

    def _release(self):
        fd, self.fd = self.fd, None
        oslock.unlock(fd)

    def _tick(self):
        try:
            if self.sup is None:
                self.sup = self.make()
            if not self.recovered:
                # What a previous supervisor left mid-launch is settled first, as rh supervise does on start.
                self.sup.recover()
                self.recovered = True
            self.sup.tick()
            self.failed = None
        except Exception as error:  # noqa: BLE001  (one failed pass must not stop the loop; the next one retries)
            self.failed = f"{type(error).__name__}: {error}"[:600]
        self.last = time.time()
        self.due = time.monotonic() + (self._pace() if self.failed is None else _RETRY)

    def _added(self):
        led = getattr(self.sup, "led", None)
        return bool(led and led.jobs(states={"draft"}))

    def _pace(self):
        if time.monotonic() < self.burst:
            return _FAST
        led = getattr(self.sup, "led", None)
        jobs = led.jobs() if led else []
        here = any(j["state"] in ("draft", "ready", "launching") or
                   (j["spec"]["kind"] == "local-shell" and j["state"] in ("running", "collecting")) for j in jobs)
        cloud = any(j["spec"]["kind"] == "codex-cloud" and j["state"] in ("running", "stalled", "collecting")
                    for j in jobs)
        if here:
            return _SHARED if cloud else _BUSY
        return float(self.cfg.get("supervisor", {}).get("interval", 60))

    def _sleep(self):
        if not self.on:
            return 3600
        if self.fd is None:
            return _RETRY
        return min(max(self.due - time.monotonic(), 0.05), 5)

    def start(self):
        if self.thread.ident is None:
            self.thread.start()

    def stop(self):
        self.done.set()
        with self.cv:
            self.cv.notify_all()
        if self.thread.ident is not None:
            self.thread.join(timeout=5)

    def nudge(self):
        """Something changed: check now, then often for a while."""
        with self.cv:
            self.burst, self.due = time.monotonic() + _BURST, 0.0
            self.cv.notify_all()

    def now(self, timeout=30):
        """Check once at once, paused or not, and wait for it; False when it could not run (the lock is taken or the
        loop is not serving)."""
        if self.thread.ident is None:
            return False
        with self.cv:
            target = self.answered + 1
            self.once = True
            self.cv.notify_all()
            self.cv.wait_for(lambda: self.answered >= target or self.done.is_set(), timeout)
            return self.answered >= target and self.other is None and self.failed is None

    def switch(self, on):
        with self.cv:
            self.on, self.due = on, 0.0
            if on:
                self.burst = time.monotonic() + _BURST
            self.cv.notify_all()

    def state(self):
        mode = "paused" if not self.on else "other" if self.other else "on"
        return {"mode": mode, "pid": self.other, "failed": self.failed, "last": self.last, "self": os.getpid()}


class _Server(http.server.HTTPServer):
    checks = None

    def server_bind(self):
        # HTTPServer asks the resolver for the host's full name, which can hang on a machine whose reverse lookup
        # of 127.0.0.1 never answers. Nothing here uses that name.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def serve_forever(self, poll_interval=0.5):
        if self.checks:
            self.checks.start()
        super().serve_forever(poll_interval)

    def server_close(self):
        if self.checks:
            self.checks.stop()
        super().server_close()


def make_server(cfg: dict, host: str = "127.0.0.1", port: int = 0, token: str | None = None,
                checks: bool = True, supervisor=None) -> tuple["http.server.HTTPServer", str]:
    """Bind an HTTPServer without starting it, and return its authenticated page URL. With `checks`, serving also
    runs the harness's checks (see _Checks); `supervisor` makes the Supervisor they tick."""
    token = secrets.token_urlsafe(24) if token is None else token
    query = urllib.parse.urlencode({"t": token})
    page_path = "/?" + query
    last_result = None
    draft = None  # the last New job form that the harness refused
    # Two first opens of a new ledger at once (the loop's and a page's) can fail with "database is locked", so the
    # ledger is created here, before either runs.
    Ledger(Path(cfg["home"]) / "ledger.db").close()
    loop = _Checks(cfg, supervisor) if checks else None
    spec_text = None  # `rh env spec`, read once, for the Codex cloud setup card
    host_seen = [0.0, None]  # when this machine was last read, and what it said

    def machine():
        if host_seen[1] is None or time.monotonic() - host_seen[0] > _HOST_TTL:
            host_seen[:] = [time.monotonic(), _host(cfg)]
        return host_seen[1]

    def env_spec():
        nonlocal spec_text
        if spec_text is None:
            code, output = _call(cfg, ["env", "spec"])
            spec_text = output if code == 0 else ""
        return spec_text

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Request targets contain the access token. Never log them.
            pass

        def reply(self, code, page="", *, location=None, content_type="text/html; charset=utf-8"):
            data = page if isinstance(page, bytes) else page.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", _CSP)
            if location is not None:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True
            if self.command == "POST" and not self.body_read:
                self.drain_body()

        def drain_body(self):
            timeout = self.connection.gettimeout()
            deadline = time.monotonic() + 1
            try:
                self.connection.shutdown(socket.SHUT_WR)
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                    return
                decimal = lengths[0].lstrip("0") or "0"
                remaining = (_MAX_BODY if len(decimal) > len(str(_MAX_BODY))
                             else min(int(decimal), _MAX_BODY))
                while remaining:
                    budget = deadline - time.monotonic()
                    if budget <= 0:
                        break
                    self.connection.settimeout(budget)
                    data = self.rfile.read1(min(remaining, 65536))
                    if not data:
                        break
                    remaining -= len(data)
            except OSError:
                pass
            finally:
                self.connection.settimeout(timeout)

        def valid_host(self):
            hosts = self.headers.get_all("Host", [])
            return len(hosts) == 1 and hosts[0] in (
                f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

        def valid_token(self, fields):
            values = fields.get("t", [])
            return len(values) == 1 and secrets.compare_digest(values[0].encode(), token.encode())

        def page(self, extra=None, status=200):
            """Render the app over the status view plus `extra`, and hand it the last action's result once."""
            nonlocal last_result
            # The check loop may print a rare warning to stderr while this runs; keep it out of the JSON.
            code, output, errors = _call(cfg, ["status", "--json"], split=True)
            if code != 0:
                self.reply(500, html.escape(output + errors))
                return
            view = json.loads(output)
            ready = bool(cfg["cloud"]["env_id"])
            meta, settled, items = _meta(cfg)
            view.update({"can_add": True, "compose": draft or {}, "meta": meta, "settled": settled,
                         "background": cfg.get("appearance", {}).get("background"),
                         "tint": cfg.get("appearance", {}).get("tint"),
                         "swarm": {**cfg.get("swarm", {}), "choices": swarm_choices()},
                         "cloud_ready": ready, "checks": loop.state() if loop else None, "seats": _seats(cfg),
                         "host": machine(), "pacing": {k: cfg["cloud"].get(k) for k in ("min_gap_s", "max_per_hour")},
                         **(extra or {})})
            policy_path = config.REPO / "policy.toml"
            view["job_policy"] = contract.load_policy(policy_path) if policy_path.is_file() else {}
            if not view.get("open") and not view.get("box"):
                repo = Path(cfg["repo"]["path"]).resolve()
                if repo.is_dir():
                    view["workspace"] = {"repo": repo.name}
                    ref = gitx.git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
                    if ref.returncode == 0:
                        view["workspace"]["branch"] = ref.stdout.strip()
                    elif gitx.git(repo, "rev-parse", "--verify", "HEAD", check=False).returncode == 0:
                        view["workspace"]["branch"] = "Detached HEAD"
            if view.get("box"):  # the Inbox shows where each failed local job stopped, as its page does
                view["tails"] = {r["id"]: _tail(cfg, r["id"]) for r in view.get("recent", [])
                                 if isinstance(r, dict) and r.get("state") in dashboard._FAILED}
            if view.get("box") and view.get("pick") is None:
                # The Inbox opens on its newest waiting item, as the page lists it.
                waiting = sorted((i for i in items.values() if not i.get("resolved")),
                                 key=lambda i: (-(i.get("created") or 0), -i["id"]))
                view["pick"] = waiting[0]["id"] if waiting else None
            if view.get("box") and view.get("pick") is not None:
                item = items.get(view["pick"])
                view["picked"] = item
                if item is None:
                    status = 404
                elif item["kind"] == "web-launch" and item.get("job") and not item.get("resolved"):
                    # The prompt a person pastes into the Codex web composer, as on the job's own page.
                    code, prompt, _ = _call(cfg, ["cloud", "prompt", "--", item["job"]], split=True)
                    view["prompt"] = prompt if code == 0 else None
            if not ready:
                view["env_spec"] = env_spec()
            self.reply(status, dashboard.render(view, time.time(), token=token, result=last_result))
            last_result = None

        def job(self, jid):
            code, output, _ = _call(cfg, ["job", "show", "--", jid], split=True)
            return json.loads(output) if code == 0 else None

        def do_GET(self):
            if not self.valid_host():
                self.reply(403)
                return
            target = urllib.parse.urlsplit(self.path)
            if target.path == "/app.js":
                # Same-origin code with no data in it; the page that loads it is the part behind the token.
                self.reply(200, dashboard.SCRIPT, content_type="text/javascript; charset=utf-8")
                return
            if target.path in _FONTS:
                # The two faces the page names, from a fixed list; like the script, they carry no data.
                self.reply(200, _FONTS[target.path].read_bytes(), content_type="font/woff2")
                return
            if target.path in _ART:
                self.reply(200, _ART[target.path].read_bytes(), content_type="image/webp")
                return
            query_fields = urllib.parse.parse_qs(target.query, keep_blank_values=True)
            if not self.valid_token(query_fields):
                self.reply(403)
                return
            parts = target.path.split("/")
            if target.path == "/files":
                # The picker behind "+": tracked files only, so nothing ignored or private to the machine is offered.
                r = subprocess.run(["git", "-C", str(cfg["repo"]["path"]), "ls-files", "-z"], capture_output=True,
                                   timeout=20, check=False)
                names = [n for n in r.stdout.decode("utf-8", "replace").split("\0") if n][:20000] if r.returncode == 0 else []
                self.reply(200, json.dumps({"files": names}), content_type="application/json; charset=utf-8")
                return
            if target.path == "/actions":
                self.reply(200, _actions(token))
            elif target.path == "/":
                self.page()
            elif target.path == "/new":
                self.page({"open": None, "new": True})
            elif target.path == "/inbox":
                self.page({"open": None, "box": True})
            elif len(parts) == 3 and parts[1] == "inbox" and re.fullmatch(r"[0-9]{1,12}", parts[2]):
                self.page({"open": None, "box": True, "pick": int(parts[2])})
            elif len(parts) == 3 and parts[1] == "job" and contract.JOB_ID_RE.fullmatch(parts[2]):
                jid = parts[2]
                shown = self.job(jid)
                if shown is None:
                    self.page({"open": {"id": jid, "job": None}}, 404)
                    return
                shown["log"], shown["log_cut"] = _log(cfg, jid)
                if shown["job"]["kind"] == "gpt-chat":
                    shown["reply"] = chat.read_reply(cfg, jid)  # the job's result, read as the next message
                elif shown["job"]["state"] == "handoff":
                    # The prompt a person pastes into the Codex web composer, so the launch needs no terminal.
                    code, prompt, _ = _call(cfg, ["cloud", "prompt", "--", jid], split=True)
                    shown["prompt"] = prompt if code == 0 else None
                root = _outputs(cfg, shown) if shown["job"]["state"] in TERMINAL else None
                if root:
                    # An answer shown in the chat is read here and left out of the files, so it appears once.
                    said = root / "answer.md"
                    chat_answer = (shown["job"].get("spec", {}).get("answer", "chat") == "chat" and said.is_file()
                                   and not said.is_symlink())
                    if chat_answer:
                        shown["answer"] = said.read_text(encoding="utf-8", errors="replace")[:_ANSWER]
                    # an agent's base (the commit its changes apply to) is bookkeeping, not a result
                    skip = (("answer.md",) if chat_answer else ()) + (
                        ("base",) if dashboard.agent_of(shown["job"].get("spec", {}).get("command")) else ())
                    shown["files"], shown["peek"] = _files(root, skip=skip)
                if shown["job"].get("spec", {}).get("prior"):
                    shown["thread"] = _thread(cfg, shown["job"])  # the chat so far, while this run works too
                self.page({"open": shown})
            elif (len(parts) == 4 and parts[1] == "job" and parts[3] == "file"
                    and contract.JOB_ID_RE.fullmatch(parts[2])):
                self.file(parts[2], query_fields)
            else:
                self.reply(404)

        def file(self, jid, fields):
            """One file a job wrote, as plain text, read only from that job's own folder."""
            shown, names = self.job(jid), fields.get("p", [])
            root = _outputs(cfg, shown) if shown else None
            if root is None or len(names) != 1:
                self.reply(404)
                return
            path = (root / names[0]).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                self.reply(404)
                return
            try:
                with path.open("rb") as handle:
                    data = handle.read(_FILE_MAX + 1)
                text = data[:_FILE_MAX].decode("utf-8")
            except UnicodeError:
                self.reply(415, "This file is not text.", content_type="text/plain; charset=utf-8")
                return
            except OSError:
                self.reply(404)
                return
            if len(data) > _FILE_MAX:
                text += "\n\n[The file goes on; this shows its first megabyte.]\n"
            self.reply(200, text, content_type="text/plain; charset=utf-8")

        def do_POST(self):
            nonlocal last_result, draft
            self.body_read = False
            if not self.valid_host():
                self.reply(403)
                return
            origins = self.headers.get_all("Origin", [])
            if origins and origins != ["http://" + self.headers["Host"]]:
                self.reply(403)
                return
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                self.reply(400)
                return
            decimal = lengths[0].lstrip("0") or "0"
            if len(decimal) > len(str(_MAX_BODY)):
                self.reply(413)
                return
            length = int(decimal)
            if length > _MAX_BODY:
                self.reply(413)
                return
            if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                self.reply(415)
                return
            if self.headers.get("Transfer-Encoding"):
                self.reply(400)
                return
            try:
                raw = self.rfile.read(length)
                self.body_read = True
                if len(raw) != length:
                    self.reply(400)
                    return
                fields = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True,
                                              encoding="utf-8", errors="strict")
            except (UnicodeError, ValueError):
                self.reply(400)
                return
            if not self.valid_token(fields):
                self.reply(403)
                return
            path = urllib.parse.urlsplit(self.path).path
            if path not in _POSTS or (path == "/checks" and not loop):
                self.reply(404)
                return
            if path == "/appearance":
                chosen = {}
                for key, allowed in (("background", dashboard.PRESETS), ("tint", dashboard.TINTS)):
                    if key in fields:
                        chosen[key] = _value(fields, key)
                        if chosen[key] not in allowed:
                            self.reply(400)
                            return
                if not chosen:
                    self.reply(400)
                    return
                with _CLI_LOCK:
                    try:
                        for key, value in chosen.items():
                            config.set_value(f"appearance.{key}", value, cfg["home"])
                    except (OSError, ValueError):
                        self.reply(500)
                        return
                    cfg.setdefault("appearance", {}).update(chosen)
                self.reply(204)
                return
            if path == "/settings/swarm":
                models, judge = fields.get("models", []), _value(fields, "judge")
                allowed = swarm_choices()
                if (not 0 < len(models) <= SWARM_MAX or len(set(models)) != len(models)
                        or any(m not in allowed for m in (*models, judge))):
                    self.reply(400)
                    return
                with _CLI_LOCK:
                    try:
                        config.set_value("swarm.models", ",".join(models), cfg["home"])
                        config.set_value("swarm.judge", judge, cfg["home"])
                    except (OSError, ValueError):
                        self.reply(500)
                        return
                    cfg.setdefault("swarm", {}).update({"models": list(models), "judge": judge})
                self.reply(204)
                return
            back = _next(fields)
            made = None
            if path == "/checks":
                on = fields.get("on") == ["1"]
                loop.switch(on)
                last_result = _result(0, "", "Checking automatically." if on else
                                      "Automatic checks are paused. The app starts nothing until you resume them or press Check once.")
            elif path == "/tick" and loop:
                ok = loop.now()
                state = loop.state()
                why = (f"rh supervise (process {state['pid']}) is checking this harness." if state["pid"] else
                       state["failed"] or "")
                last_result = _result(0 if ok else 1, why, _say(path, fields, ok))
            else:
                try:
                    if path == "/job":
                        fields.pop("swarm", None)  # only a brief that starts with /swarm asks for one
                        if (brief := _swarm_brief(fields)) is not None:
                            fields["brief"], fields["swarm"] = [brief], ["1"]
                        _auto_id(cfg, fields)
                    if path in ("/job", "/again"):
                        made = (_value(fields, "id").strip() + (".best" if "swarm" in fields else "") if path == "/job"
                                else _again(cfg, _value(fields, "job"), "")[0])
                    argv = _action(cfg, path, fields)
                    code, output = _call(cfg, argv)
                except (OSError, ValueError, contract.ContractError) as error:
                    code, output = 1, f"rh: {error}"
                if code == 0 and path == "/setup":
                    cfg["cloud"]["env_id"] = _value(fields, "env_id").strip()  # the loop's Supervisor reads this
                    for item in _open_setup_items(cfg):
                        _call(cfg, ["inbox", "resolve", "--", str(item), "environment ID set in the app"])
                last_result = _result(code, output, _say(path, fields, code == 0, made))
                if loop and code == 0 and path != "/tick":
                    loop.nudge()
                if path in ("/job", "/again"):
                    # A new job opens in its own pane; a refused one goes back to the form, filled in as it was sent.
                    if path == "/job":
                        try:
                            draft = None if code == 0 else _draft(fields)
                        except ValueError:
                            draft = None
                        back = f"/job/{made}" if code == 0 else "/new"
                    elif code == 0:
                        back = f"/job/{made}"
            self.reply(303, location=back + "?" + query if back != "/" else page_path)

    server = _Server((host, port), Handler)
    server.checks = loop
    return server, f"http://127.0.0.1:{server.server_port}{page_path}"


def _open_setup_items(cfg):
    """The open inbox items asking for the Codex cloud environment, now that the app has set it."""
    led = Ledger(Path(cfg["home"]) / "ledger.db")
    try:
        return [i["id"] for i in led.inbox() if i["kind"] == "operator-step"
                and str(i.get("action") or "").startswith(_SETUP)]
    finally:
        led.close()


def main(cfg: dict, port: int = 0, open_browser: bool = True) -> int:
    """Print/open the page URL and serve until Ctrl-C, checking the harness all the while."""
    server, url = make_server(cfg, port=port)
    with server:
        print(url, flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0
