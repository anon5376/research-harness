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
import shlex
import socket
import socketserver
import threading
import time
import tomllib
import urllib.parse
import webbrowser

from rh import cli, contract, dashboard, oslock
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
# The page is one document with one same-origin script and inline styles; nothing else may load or frame it.
_CSP = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; font-src data:; img-src data:; "
        "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
_POSTS = ("/inbox/resolve", "/stop", "/resume", "/attach", "/harvest", "/land", "/tick", "/job", "/job/cancel",
          "/checks", "/again", "/setup")
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
    draft = {name: _value(fields, name) for name in ("id", "kind", "brief", "command", "accept", "timeout_min")}
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


def _job_file(draft):
    """The TOML file `rh job add` reads for a New job form; the contract rejects what it would reject."""
    spec = {"id": draft["id"].strip(), "kind": draft["kind"], "brief": draft["brief"].replace("\r\n", "\n")}
    if draft["accept"].strip():
        spec["accept"] = draft["accept"].strip()
    if draft["needs"]:
        spec["needs"] = list(draft["needs"])
    timeout = draft["timeout_min"].strip()
    if timeout:
        if not timeout.isascii() or not timeout.isdigit() or not 0 < int(timeout) <= 10080:
            raise ValueError("the time limit is a whole number of minutes, at most 10080")
        spec["timeout_min"] = int(timeout)
    if spec["kind"] == "local-shell":
        try:
            spec["command"] = shlex.split(draft["command"])
        except ValueError as error:
            raise ValueError(f"command: {error}") from None
    return spec["id"], _job_text(spec)


def _again(cfg, old, changed):
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
    if path == "/job/cancel":
        return ["job", "cancel", "--reason=" + value("reason"), "--", value("job")]
    if path == "/job":
        job, text = _job_file(_draft(fields))
        return ["job", "add", "--", str(_upload(cfg, job, ".toml", text))]
    if path == "/again":
        job, text = _again(cfg, value("job"), value("changed").strip() if "changed" in fields else "")
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
    what = job or ({"codex-cloud": "Codex cloud", "local-shell": "this machine"}.get(route, route)
                   if route else "every launch")
    if not ok:
        return {"/job": "The job was not added.", "/again": "The job did not start again.",
                "/setup": "Codex cloud was not set up.", "/tick": "The check did not finish.",
                "/attach": "The task was not linked."}.get(path, "That did not work.")
    pause = "pause" in fields
    return {
        "/job": f"Added {made or get('id')}.",
        "/again": f"Added {made}, the next run of {job}.",
        "/inbox/resolve": "Done. It is off your list.",
        "/stop": f"Paused {what}." if pause else f"Stopped {what}.",
        "/resume": f"Resumed {what}.",
        "/attach": f"Linked the task to {job}. It is running.",
        "/harvest": "Patch saved. The result is being checked.",
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
    if target in ("/", "/new"):
        return target
    if target.startswith("/job/") and contract.JOB_ID_RE.fullmatch(target[5:]):
        return target
    return "/"


def _log(cfg, job):
    """The end of a local job's output, decoded leniently, or None."""
    path = Path(cfg["home"]) / "jobs" / job / "out.log"
    try:
        with path.open("rb") as file:
            file.seek(0, os.SEEK_END)
            size = file.tell()
            file.seek(max(0, size - _LOG_TAIL))
            data = file.read()
    except OSError:
        return None
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > _LOG_TAIL and lines:
        lines = lines[1:]  # the first line is cut
    return "\n".join(lines[-_LOG_LINES:])


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


def _files(root):
    """([{path, size}], peek): what a finished job left in its folder, and the start of its first small text file."""
    found, peek = [], None
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(root).as_posix()
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


def _meta(cfg):
    """What the status view leaves out and the job list needs: each job's brief, kind, times and the run it follows."""
    led = Ledger(Path(cfg["home"]) / "ledger.db")
    try:
        return {j["id"]: {"brief": j["spec"].get("brief", ""), "kind": j["spec"].get("kind", ""),
                          "created": j.get("created"), "started": j.get("started"), "updated": j.get("updated"),
                          "prior": j["spec"].get("prior", "")} for j in led.jobs()}
    finally:
        led.close()


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
            data = page.encode("utf-8")
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
            view.update({"can_add": True, "compose": draft or {}, "meta": _meta(cfg), "cloud_ready": ready,
                         "checks": loop.state() if loop else None, **(extra or {})})
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
            query_fields = urllib.parse.parse_qs(target.query, keep_blank_values=True)
            if not self.valid_token(query_fields):
                self.reply(403)
                return
            parts = target.path.split("/")
            if target.path == "/actions":
                self.reply(200, _actions(token))
            elif target.path == "/":
                self.page()
            elif target.path == "/new":
                self.page({"open": None, "new": True})
            elif len(parts) == 3 and parts[1] == "job" and contract.JOB_ID_RE.fullmatch(parts[2]):
                jid = parts[2]
                shown = self.job(jid)
                if shown is None:
                    self.page({"open": {"id": jid, "job": None}}, 404)
                    return
                shown["log"] = _log(cfg, jid)
                if shown["job"]["state"] == "handoff":
                    # The prompt a person pastes into the Codex web composer, so the launch needs no terminal.
                    code, prompt, _ = _call(cfg, ["cloud", "prompt", "--", jid], split=True)
                    shown["prompt"] = prompt if code == 0 else None
                root = _outputs(cfg, shown) if shown["job"]["state"] in TERMINAL else None
                if root:
                    shown["files"], shown["peek"] = _files(root)
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
                    if path in ("/job", "/again"):
                        made = (_value(fields, "id").strip() if path == "/job"
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
