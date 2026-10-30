"""Exercise the local web app through real HTTP requests and the real CLI."""
import contextlib
import html
from html.parser import HTMLParser
import importlib
import io
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from rh import config
from rh.ledger import Ledger
from tests.test_bundle import export_in_clone


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Forms(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.forms = {}
        self.current = None
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = attrs["action"]
            self.forms[self.current] = (attrs, {})
        elif tag in ("input", "textarea") and self.current:
            self.forms[self.current][1][attrs["name"]] = attrs

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


class ServeTest(unittest.TestCase):
    def setUp(self):
        # A missing implementation is a clear assertion failure in the RED run.
        self.assertIsNotNone(importlib.util.find_spec("rh.serve"), "rh.serve must implement the brief")
        self.serve = importlib.import_module("rh.serve")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.home, self.repo = self.base / "home", self.base / "repo"
        self.repo.mkdir()
        for args in (("init", "-q", "-b", "main"), ("config", "user.name", "test"),
                     ("config", "user.email", "test@example.com"),
                     ("config", "core.hooksPath", "/dev/null")):
            self.git(*args)
        (self.repo / "README.md").write_text("base\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "base")
        self.sha = self.git("rev-parse", "HEAD").strip()
        config.set_value("repo.path", str(self.repo), self.home)
        config.set_value("notify.command", "[]", self.home)
        self.cfg = config.load(self.home)
        self.token = "serve-test-key"
        self.server, self.url = self.serve.make_server(self.cfg, token=self.token)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout

    @contextlib.contextmanager
    def ledger(self):
        led = Ledger(self.home / "ledger.db")
        try:
            yield led
        finally:
            led.close()

    def job(self, state="handoff"):
        with self.ledger() as led:
            led.add_job({"id": "web-job", "kind": "codex-cloud", "write_root": "runs/web-job/",
                         "write_paths": [], "brief": "Write notes.", "needs": []}, pinned_sha=self.sha)
            led.set_state("web-job", "ready")
            if state != "ready":
                led.set_state("web-job", "launching", route="codex-cloud")
                led.set_state("web-job", state)

    def request(self, path="/", fields=None, *, token=True, headers=None, raw=None, method=None):
        headers = dict(headers or {})
        if fields is not None or raw is not None:
            if raw is None:
                fields = dict(fields)
                if token:
                    fields["t"] = self.token
                raw = urlencode(fields).encode()
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            method = method or "POST"
        elif token:
            path += ("&" if "?" in path else "?") + urlencode({"t": self.token})
        req = Request(self.origin + path, data=raw, headers=headers, method=method)
        try:
            response = self.opener.open(req, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.code, response.headers, response.read().decode()

    def test_dashboard_requires_token_and_renders_status_and_refresh(self):
        self.assertEqual(self.request(token=False)[0], 403)
        self.assertEqual(self.request("/?t=wrong", token=False)[0], 403)
        code, _, page = self.request()
        self.assertEqual(code, 200)
        for heading in ("Supervisor", "Stops", "Routes", "Live", "Not started", "Inbox", "Last 24h", "Recent"):
            self.assertIn(f">{heading}</h2>", page)
        self.assertIn('/actions?t=serve-test-key', page)
        self.assertIn('<meta http-equiv="refresh" content="30">', page)
        self.assertEqual(page.count("<nav"), 1)

    def test_host_and_origin_checks_prevent_mutation(self):
        for path in ("/", "/actions"):
            self.assertEqual(self.request(path, headers={"Host": f"example.test:{self.server.server_port}"})[0], 403)
        for headers in ({"Origin": "https://example.test"}, {"Origin": "null"},
                        {"Origin": self.origin + "/"}, {"Host": "localhost:1"}):
            self.assertEqual(self.request("/stop", {"reason": "no"}, headers=headers)[0], 403)
        with self.ledger() as led:
            self.assertEqual(led.active_stops(), [])
        local = f"localhost:{self.server.server_port}"
        self.assertEqual(self.request(headers={"Host": local})[0], 200)
        self.assertEqual(self.request("/stop", {"reason": "yes"},
                                     headers={"Host": local, "Origin": "http://" + local})[0], 303)

    def test_rejected_post_drains_delayed_body_without_reset(self):
        body = b"x" * 65536
        with socket.create_connection(self.server.server_address, timeout=5) as client:
            client.sendall((
                f"POST /stop HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
                "Origin: https://example.test\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode("ascii"))
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = client.recv(4096)
                self.assertTrue(chunk, "EOF before response headers")
                response += chunk
            self.assertTrue(response.startswith(b"HTTP/1.0 403 Forbidden\r\n"))
            client.sendall(body)
            self.assertEqual(client.recv(4096), b"")

    def test_inbox_resolve_requires_form_token_and_keeps_note(self):
        with self.ledger() as led:
            item, _ = led.inbox_add("decision", "Review notes")
        fields = {"id": str(item), "note": "Reviewed <ok> & done"}
        self.assertEqual(self.request("/inbox/resolve", fields, token=False)[0], 403)
        self.assertEqual(self.request("/inbox/resolve?t=" + self.token, fields, token=False)[0], 403)
        with self.ledger() as led:
            self.assertEqual(len(led.inbox()), 1)
        self.assertEqual(self.request("/inbox/resolve", fields)[0], 303)
        with self.ledger() as led:
            self.assertEqual(led.inbox(), [])
            self.assertEqual(led.inbox(False)[0]["resolution"], fields["note"])

    def test_attach_uses_cli_and_shows_its_result_once(self):
        self.job()
        task = "task_e_" + "7" * 32
        code, headers, _ = self.request("/attach", {"job": "web-job", "task_id": task})
        self.assertEqual(code, 303)
        self.assertIn(headers["Location"], ("/?t=" + self.token, self.origin + "/?t=" + self.token))
        self.assertNotIn(task, headers["Location"])
        with self.ledger() as led:
            self.assertEqual(led.job("web-job")["state"], "running")
            self.assertEqual(led.job("web-job")["external_id"], task)
        # Actions and denied requests must not consume the result.
        self.request("/actions")
        self.request(token=False)
        self.assertIn("attached " + task + " to web-job", self.request()[2])
        self.assertNotIn("attached " + task + " to web-job", self.request()[2])

    def test_attach_wrong_state_preserves_job_and_escapes_cli_error(self):
        self.job("ready")
        self.assertEqual(self.request("/attach", {"job": "web-job", "task_id": "task_e_" + "8" * 32})[0], 303)
        self.assertIn("only a handoff job can be attached", self.request()[2])
        self.assertEqual(self.request("/attach", {"job": "web-job", "task_id": "<b>&bad</b>"})[0], 303)
        page = self.request()[2]
        self.assertIn("&lt;b&gt;&amp;bad&lt;/b&gt;", page)
        self.assertNotIn("<b>&bad</b>", page)
        self.assertIn("1", page)
        with self.ledger() as led:
            self.assertEqual(led.job("web-job")["state"], "ready")

    def test_harvest_saves_patch_before_real_cli_collection(self):
        self.job("running")
        text = "diff --git a/notes.txt b/notes.txt\nnew file mode 100644\n--- /dev/null\n+++ b/notes.txt\n@@ -0,0 +1 @@\n+notes\n"
        self.assertEqual(self.request("/harvest", {"job": "web-job", "text": text})[0], 303)
        files = list((self.home / "uploads").glob("web-job-*.txt"))
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^web-job-\d+\.txt$")
        self.assertEqual(files[0].read_text(), text)
        with self.ledger() as led:
            self.assertEqual(led.job("web-job")["state"], "collecting")
            event = next(e for e in led.events("web-job") if e["kind"] == "harvested")
            self.assertEqual(event["data"]["source"], "patch")

    def test_harvest_bundle_goes_through_bundle_route(self):
        self.job("running")
        text, _ = export_in_clone(self.repo, "web-job", {"runs/web-job/notes.txt": "notes\n"}, self.base)
        self.assertEqual(self.request("/harvest", {"job": "web-job", "text": text})[0], 303)
        with self.ledger() as led:
            self.assertEqual(led.job("web-job")["state"], "collecting")
            event = next(e for e in led.events("web-job") if e["kind"] == "harvested")
            self.assertEqual(event["data"]["source"], "bundle")

    def test_harvest_job_cannot_escape_uploads(self):
        self.assertEqual(self.request("/harvest", {"job": "../outside", "text": "patch"})[0], 303)
        self.assertFalse(list(self.home.glob("outside*")))
        self.assertIn("job", self.request()[2].lower())

    def test_stop_and_resume_are_app_owned_and_preserve_other_owners(self):
        with self.ledger() as led:
            led.stop("lead", "keep", scope="job:web-job", kind="pause")
        for scope in ({}, {"job": "web-job"}, {"route": "codex-cloud"}):
            for pause in ({}, {"pause": "on"}):
                fields = {**scope, **pause}
                self.assertEqual(self.request("/stop", {**fields, "reason": "operator", "owner": "lead"})[0], 303)
                with self.ledger() as led:
                    owned = [s for s in led.active_stops() if s["owner"] == "app"]
                    self.assertEqual(len(owned), 1)
                    self.assertEqual(owned[0]["reason"], "operator")
                    self.assertEqual(owned[0]["kind"], "pause" if pause else "stop")
                    expected = "job:web-job" if "job" in scope else "route:codex-cloud" if scope else "*"
                    self.assertEqual(owned[0]["scope"], expected)
                self.assertEqual(self.request("/resume", fields)[0], 303)
                with self.ledger() as led:
                    self.assertEqual([s["owner"] for s in led.active_stops()], ["lead"])

    def test_land_and_tick_show_cli_results(self):
        self.assertEqual(self.request("/land", {"job": "missing"})[0], 303)
        self.assertIn("rh: no job missing", self.request()[2])
        self.assertEqual(self.request("/tick", {})[0], 303)
        self.assertIn("supervisor ran 1 tick(s)", self.request()[2])
        self.assertTrue((self.home / "supervisor.json").is_file())

    def test_actions_have_all_forms_and_pages_have_no_active_or_external_content(self):
        expected = {"/inbox/resolve": {"id", "note"}, "/stop": {"reason", "job", "route", "pause"},
                    "/resume": {"job", "route", "pause"}, "/attach": {"job", "task_id"},
                    "/harvest": {"job", "text"}, "/land": {"job"}, "/tick": set()}
        code, _, page = self.request("/actions")
        self.assertEqual(code, 200)
        forms = Forms(page).forms
        self.assertEqual(set(forms), set(expected))
        for action, names in expected.items():
            attrs, fields = forms[action]
            self.assertEqual(attrs["method"].lower(), "post")
            self.assertTrue(names <= fields.keys())
            self.assertEqual(fields["t"]["type"], "hidden")
            self.assertEqual(fields["t"]["value"], self.token)
        self.assertEqual(forms["/stop"][1]["pause"]["type"], "checkbox")
        self.assertIn("<textarea", page)
        for page in (page, self.request()[2]):
            for forbidden in ("<script", "http://", "https://", "<img", "<link"):
                self.assertNotIn(forbidden, page.lower())

    def test_post_media_type_size_and_duplicate_tokens(self):
        self.assertEqual(self.request("/tick", raw=urlencode({"t": self.token}).encode(),
                                     headers={"Content-Type": "application/json"})[0], 415)
        self.assertEqual(self.request("/tick", raw=b"", headers={"Content-Length": str(32 * 1024 * 1024 + 1)})[0], 413)
        for raw in (b"t=wrong", b"t=%C3%A9", b"t=serve-test-key&t=wrong"):
            self.assertEqual(self.request("/tick", raw=raw)[0], 403)
        for length in ("-1", "invalid"):
            self.assertEqual(self.request("/tick", raw=b"", headers={"Content-Length": length})[0], 400)
        self.assertEqual(self.request("/tick", raw=urlencode({"t": self.token}).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})[0], 303)

    def test_very_large_content_length_returns_413_without_crashing(self):
        self.assertEqual(self.request("/tick", raw=b"", headers={"Content-Length": "9" * 5000})[0], 413)
        body = urlencode({"t": self.token}).encode()
        self.assertEqual(self.request("/tick", raw=body,
                                     headers={"Content-Length": "0" * 5000 + str(len(body))})[0], 303)
        self.assertEqual(self.request()[0], 200)

    def test_cli_usage_errors_are_captured_and_server_keeps_serving(self):
        self.assertEqual(self.request("/inbox/resolve", {"id": "bad", "note": "done"})[0], 303)
        self.assertIn("invalid int value", self.request()[2])
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.request("/not-found")[0], 404)
        self.assertEqual(self.request("/not-found", {})[0], 404)

    def test_output_is_bounded_and_home_is_restored(self):
        self.job("ready")
        previous = os.environ.get("RH_HOME")
        self.request("/attach", {"job": "web-job", "task_id": "X" * 5000 + "TAIL-MARKER"})
        page = self.request()[2]
        output = html.unescape(page.split("<pre>", 1)[1].split("</pre>", 1)[0])
        self.assertLessEqual(len(output), 2000)
        self.assertIn("rh:", output)
        self.assertNotIn("TAIL-MARKER", page)
        self.assertEqual(os.environ.get("RH_HOME"), previous)

    def test_server_starts_without_a_name_lookup(self):
        # The desktop app starts this server; a resolver that never answers for 127.0.0.1 must not stop it.
        with patch("socket.getfqdn", side_effect=AssertionError("name lookup")):
            server, url = self.serve.make_server(self.cfg)
        with server:
            self.assertEqual(urlsplit(url).port, server.server_port)

    def test_default_token_and_main_lifecycle(self):
        server, url = self.serve.make_server(self.cfg)
        with server:
            self.assertEqual(urlsplit(url).hostname, "127.0.0.1")
            self.assertEqual(urlsplit(url).port, server.server_port)
            self.assertGreaterEqual(len(parse_qs(urlsplit(url).query)["t"][0]), 32)
        for open_browser in (False, True):
            output = io.StringIO()
            with patch("http.server.HTTPServer.serve_forever", side_effect=KeyboardInterrupt), \
                    patch("webbrowser.open") as browser, contextlib.redirect_stdout(output):
                result = self.serve.main(self.cfg, open_browser=open_browser)
            self.assertEqual(result, 0)
            printed = output.getvalue().strip()
            self.assertTrue(printed.startswith("http://127.0.0.1:"))
            if open_browser:
                browser.assert_called_once_with(printed)
            else:
                browser.assert_not_called()
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", urlsplit(printed).port))
