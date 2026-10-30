"""A small, authenticated browser front end to the existing CLI."""
from __future__ import annotations

import contextlib
import html
import http.server
import io
import json
import os
from pathlib import Path
import secrets
import threading
import time
import urllib.parse
import webbrowser

from rh import cli, contract, dashboard


# Redirecting stdout/stderr and selecting RH_HOME affect the entire process, so
# even separate server instances must serialize their calls.
_CLI_LOCK = threading.Lock()
_MAX_BODY = 32 * 1024 * 1024


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


def _action(cfg, path, fields):
    def value(name):
        values = fields.get(name, [""])
        if len(values) != 1:
            raise ValueError(f"give one {name}")
        return values[0]

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
        uploads = Path(cfg["home"]) / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        milliseconds = time.time_ns() // 1_000_000
        while True:
            dest = uploads / f"{job}-{milliseconds}.txt"
            try:
                with dest.open("x", encoding="utf-8", newline="") as file:
                    file.write(text)
                break
            except FileExistsError:
                milliseconds += 1
        if "RH-BUNDLE" in text:
            return ["cloud", "harvest", "--bundle", str(dest), "--", job]
        return ["cloud", "harvest", "--", job, str(dest)]
    if path == "/land":
        return ["land", "--", value("job")]
    return ["supervise", "--once"]  # /tick, checked by the request handler


def make_server(cfg: dict, host: str = "127.0.0.1", port: int = 0,
                token: str | None = None) -> tuple["http.server.HTTPServer", str]:
    """Bind an HTTPServer without starting it, and return its authenticated page URL."""
    token = secrets.token_urlsafe(24) if token is None else token
    query = urllib.parse.urlencode({"t": token})
    page_path = "/?" + query
    actions_path = "/actions?" + query
    last_result = None

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Request targets contain the access token. Never log them.
            pass

        def reply(self, code, page="", *, location=None):
            data = page.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            if location is not None:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

        def valid_host(self):
            hosts = self.headers.get_all("Host", [])
            return len(hosts) == 1 and hosts[0] in (
                f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

        def valid_token(self, fields):
            values = fields.get("t", [])
            return len(values) == 1 and secrets.compare_digest(values[0].encode(), token.encode())

        def do_GET(self):
            nonlocal last_result
            if not self.valid_host():
                self.reply(403)
                return
            target = urllib.parse.urlsplit(self.path)
            if not self.valid_token(urllib.parse.parse_qs(target.query, keep_blank_values=True)):
                self.reply(403)
                return
            if target.path == "/actions":
                self.reply(200, _actions(token))
            elif target.path == "/":
                code, output = _call(cfg, ["status", "--json"])
                if code != 0:
                    self.reply(500, _result(code, output))
                    return
                page = dashboard.render(json.loads(output), time.time())
                page = page.replace("</head>", '<meta http-equiv="refresh" content="30">\n</head>', 1)
                nav = (f'<nav><a href="{html.escape(actions_path, quote=True)}">Actions</a>'
                       f'{last_result or ""}</nav>')
                page = page.replace("</body>", nav + "\n</body>", 1)
                self.reply(200, page)
                last_result = None
            else:
                self.reply(404)

        def do_POST(self):
            nonlocal last_result
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
            if path not in ("/inbox/resolve", "/stop", "/resume", "/attach", "/harvest", "/land", "/tick"):
                self.reply(404)
                return
            try:
                argv = _action(cfg, path, fields)
                code, output = _call(cfg, argv)
            except (OSError, ValueError) as error:
                code, output = 1, f"rh: {error}"
            last_result = _result(code, output)
            self.reply(303, location=page_path)

    server = http.server.HTTPServer((host, port), Handler)
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
