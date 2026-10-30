"""A small, authenticated browser front end to the existing CLI: the app page, its script and the everyday actions."""
from __future__ import annotations

import contextlib
import html
import http.server
import io
import json
import os
from pathlib import Path
import secrets
import shlex
import socket
import socketserver
import threading
import time
import tomllib
import urllib.parse
import webbrowser

from rh import cli, contract, dashboard


# Redirecting stdout/stderr and selecting RH_HOME affect the entire process, so
# even separate server instances must serialize their calls.
_CLI_LOCK = threading.Lock()
_MAX_BODY = 32 * 1024 * 1024
_LOG_TAIL = 64 * 1024  # bytes of a local job's out.log the job pane shows, at most
_LOG_LINES = 200
# The page is one document with one same-origin script and inline styles; nothing else may load or frame it.
_CSP = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; font-src data:; img-src data:; "
        "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
_POSTS = ("/inbox/resolve", "/stop", "/resume", "/attach", "/harvest", "/land", "/tick", "/job", "/job/cancel")


def _call(cfg, argv):
    output = io.StringIO()
    with _CLI_LOCK:
        previous = os.environ.get("RH_HOME")
        try:
            os.environ["RH_HOME"] = str(cfg["home"])
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
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
    return code, output.getvalue()


def _result(code, output):
    # Bound the stored escaped output without splitting an HTML entity.
    parts = []
    size = 0
    for char in output:
        escaped = html.escape(char, quote=True)
        if size + len(escaped) > 2000:
            break
        parts.append(escaped)
        size += len(escaped)
    return f'<p>Exit code: {code}</p><pre>{"".join(parts)}</pre>'


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
    forbids a raw DEL."""
    def value(v):
        if isinstance(v, bool) or not isinstance(v, (str, int, list)):
            raise ValueError(f"cannot write {v!r}")
        if isinstance(v, list):
            return "[" + ", ".join(value(x) for x in v) + "]"
        return json.dumps(v, ensure_ascii=False).replace("\x7f", "\\u007f") if isinstance(v, str) else str(v)
    return "".join(f"{key} = {value(v)}\n" for key, v in spec.items())


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
    text = _toml(spec)
    contract.normalize(tomllib.loads(text))
    if tomllib.loads(text) != spec:
        raise ValueError("the job did not survive being written as TOML")
    return spec["id"], text


def _action(cfg, path, fields):
    value = lambda name: _value(fields, name)  # noqa: E731
    if path == "/inbox/resolve":
        return ["inbox", "resolve", "--", value("id"), value("note")]
    if path in ("/stop", "/resume"):
        argv = [path[1:], "--owner=app"]
        if path == "/stop":
            argv.append("--reason=" + value("reason"))
        for name in ("job", "route"):
            if value(name):
                argv.append(f"--{name}=" + value(name))
        if "pause" in fields:
            argv.append("--pause")
        return argv
    if path == "/attach":
        return ["cloud", "attach", "--", value("job"), value("task_id")]
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
    return ["supervise", "--once"]  # /tick, checked by the request handler


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


class _Server(http.server.HTTPServer):
    def server_bind(self):
        # HTTPServer asks the resolver for the host's full name, which can hang on a machine whose reverse lookup
        # of 127.0.0.1 never answers. Nothing here uses that name.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def make_server(cfg: dict, host: str = "127.0.0.1", port: int = 0,
                token: str | None = None) -> tuple["http.server.HTTPServer", str]:
    """Bind an HTTPServer without starting it, and return its authenticated page URL."""
    token = secrets.token_urlsafe(24) if token is None else token
    query = urllib.parse.urlencode({"t": token})
    page_path = "/?" + query
    last_result = None
    draft = None  # the last New job form that the harness refused

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
            code, output = _call(cfg, ["status", "--json"])
            if code != 0:
                self.reply(500, _result(code, output))
                return
            view = {**json.loads(output), "can_add": True, "compose": draft or {}, **(extra or {})}
            self.reply(status, dashboard.render(view, time.time(), token=token, result=last_result))
            last_result = None

        def do_GET(self):
            if not self.valid_host():
                self.reply(403)
                return
            target = urllib.parse.urlsplit(self.path)
            if target.path == "/app.js":
                # Same-origin code with no data in it; the page that loads it is the part behind the token.
                self.reply(200, dashboard.SCRIPT, content_type="text/javascript; charset=utf-8")
                return
            if not self.valid_token(urllib.parse.parse_qs(target.query, keep_blank_values=True)):
                self.reply(403)
                return
            if target.path == "/actions":
                self.reply(200, _actions(token))
            elif target.path == "/":
                self.page()
            elif target.path == "/new":
                self.page({"open": None, "new": True})
            elif target.path.startswith("/job/") and contract.JOB_ID_RE.fullmatch(target.path[5:]):
                job = target.path[5:]
                code, output = _call(cfg, ["job", "show", "--", job])
                if code != 0:
                    self.page({"open": {"id": job, "job": None}}, 404)
                    return
                shown = json.loads(output)
                self.page({"open": {**shown, "log": _log(cfg, job)}})
            else:
                self.reply(404)

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
            if path not in _POSTS:
                self.reply(404)
                return
            back = _next(fields)
            try:
                argv = _action(cfg, path, fields)
                code, output = _call(cfg, argv)
            except (OSError, ValueError) as error:
                code, output = 1, f"rh: {error}"
            last_result = _result(code, output)
            if path == "/job":
                # A new job opens in its own pane; a refused one goes back to the form, filled in as it was sent.
                try:
                    draft = None if code == 0 else _draft(fields)
                except ValueError:
                    draft = None
                back = f"/job/{_value(fields, 'id').strip()}" if code == 0 else "/new"
            self.reply(303, location=back + "?" + query if back != "/" else page_path)

    server = _Server((host, port), Handler)
    return server, f"http://127.0.0.1:{server.server_port}{page_path}"


def main(cfg: dict, port: int = 0, open_browser: bool = True) -> int:
    """Print/open the page URL and serve until Ctrl-C."""
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
