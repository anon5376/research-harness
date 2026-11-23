"""Exercise the local web app through real HTTP requests and the real CLI."""
import contextlib
import html
from html.parser import HTMLParser
import importlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import tempfile
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from rh import config, contract, dashboard, oslock
from rh.ledger import Ledger
from rh.supervisor import AlreadyRunning, single_instance
from tests.test_bundle import export_in_clone

SETUP = "create and publish the cloud environment (rh env spec), then rh config set cloud.env_id <ID>"


def until(ready, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready():
            return True
        time.sleep(.02)
    return ready()


class FakeSupervisor:
    """Counts what the app's check loop asks of a supervisor."""
    led = None

    def __init__(self):
        self.ticks = self.recovered = 0

    def recover(self):
        self.recovered += 1

    def tick(self):
        self.ticks += 1


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


class SayTest(unittest.TestCase):
    def test_a_started_swarm_says_its_models_not_its_judges_slug(self):
        # 'Started a swarm. fix.best keeps the best work.' names an internal id a person cannot parse: the sentence
        # names the runs' models and the judge in words.
        from rh import serve
        said = serve._say("/job", {"swarm": ["1"]}, True, "fix.best",
                          swarm={"models": ["swe-2-max", "swe-2-flash-high"], "judge": "swe-2-max"})
        self.assertEqual(said, "Swarm started · SWE-2 Max, SWE-2 Flash · SWE-2 Max judges")  # no '+': not Fusion
        self.assertNotIn("fix.best", said)

    def test_a_done_toast_carries_no_cli_details(self):
        # A success toast with a browser-default 'Details' disclosure of the CLI's output is debug text; only a
        # failure keeps its output to read.
        from rh import dashboard
        self.assertIn("if (!ok && out.trim())", dashboard.SCRIPT)
        self.assertIn(".toast summary::-webkit-details-marker { display: none; }", dashboard.STYLE)


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
        # The shared server runs no check loop, so a test sees only the states it set; the loop's own tests
        # start a second server with one.
        self.server, self.url = self.serve.make_server(self.cfg, token=self.token, checks=False)
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
                              capture_output=True, text=True, encoding="utf-8").stdout

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

    def chat_job(self):
        """A GPT Pro chat handed to the person, as the supervisor leaves it; returns its inbox item's id."""
        with self.ledger() as led:
            led.add_job(contract.normalize({"id": "chat-job", "kind": "gpt-chat", "brief": "Compare two designs."}))
            led.set_state("chat-job", "ready")
            led.set_state("chat-job", "launching")
            led.set_state("chat-job", "handoff", "Waiting for the reply from ChatGPT", route="chat")
            item, _ = led.inbox_add("chat", "Ask ChatGPT: copy the prompt, paste its reply", job="chat-job")
        return item

    def served(self, **kwargs):
        """A second app on this home with its own check loop, serving until the test ends."""
        server, _ = self.serve.make_server(self.cfg, token=self.token, **kwargs)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
        thread.start()

        def stop():
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        self.addCleanup(stop)
        return server, f"http://127.0.0.1:{server.server_port}", stop

    def request(self, path="/", fields=None, *, token=True, headers=None, raw=None, method=None, origin=None):
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
        req = Request((origin or self.origin) + path, data=raw, headers=headers, method=method)
        try:
            response = self.opener.open(req, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.code, response.headers, response.read().decode()

    def test_app_requires_token_and_renders_status(self):
        for path in ("/", "/new", "/job/web-job", "/actions", "/inbox", "/inbox/1"):
            self.assertEqual(self.request(path, token=False)[0], 403)
            self.assertEqual(self.request(path + "?t=wrong", token=False)[0], 403)
        code, _, page = self.request()
        self.assertEqual(code, 200)
        for group in ("Needs you", "Live", "Up next", "Ended today"):
            self.assertIn(f">{group}</h3>", page)
        self.assertRegex(page, r'id="compose-title" tabindex="-1" data-repo="[^"]+">[^<]+</h1>')  # the repository heads it
        self.assertIn('data-mode="coding"', page)
        self.assertIn('data-mode="research"', page)
        self.assertIn('/actions?t=serve-test-key', page)
        self.assertIn('/new?t=serve-test-key', page)
        self.assertIn('<noscript><meta http-equiv="refresh" content="30"></noscript>', page)
        self.assertEqual(page.count("<script"), 1)
        self.assertIn('<script src="/app.js" defer></script>', page)
        self.assertEqual(page.count("<nav"), 1)

    def test_every_response_carries_the_security_headers_and_app_js_needs_no_token(self):
        csp = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; font-src 'self'; img-src 'self' data:; "
               "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        replies = [self.request(), self.request(token=False), self.request("/not-found"), self.request("/tick", {}),
                   self.request("/app.js", token=False), self.request("/job/ghost")]
        self.assertEqual([code for code, _, _ in replies], [200, 403, 404, 303, 200, 404])
        for code, headers, _ in replies:
            with self.subTest(code=code):
                self.assertEqual(headers["Content-Security-Policy"], csp)
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")
                self.assertEqual(headers["Cache-Control"], "no-store")
        _, headers, script = replies[4]
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
        self.assertEqual(script, dashboard.SCRIPT)
        bad_host = {"Host": f"example.test:{self.server.server_port}"}
        self.assertEqual(self.request("/app.js", token=False, headers=bad_host)[0], 403)

    def raw(self, path, headers=None):
        """A tokenless GET answered as bytes."""
        try:
            response = self.opener.open(Request(self.origin + path, headers=headers or {}), timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.code, response.headers, response.read()

    def test_fonts_come_from_the_repo_and_nothing_else_is_served_beside_them(self):
        # A face the page names that the server does not serve, a served face that is not the shipped file, a path
        # off the fixed list answered, or a font that skips the Host check, must fail.
        page = self.request()[2]
        named = re.findall(r"url\((/fonts/[^)]+)\)", page)
        self.assertEqual(sorted(named), sorted(f"/fonts/{name}" for name in dashboard.FONTS))
        for path in named:
            with self.subTest(path=path):
                code, headers, body = self.raw(path)
                self.assertEqual((code, headers["Content-Type"]), (200, "font/woff2"))
                self.assertEqual(body, (dashboard._WEB / "fonts" / path.rsplit("/", 1)[1]).read_bytes())
                self.assertEqual(body[:4], b"wOF2")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(self.raw(path, {"Host": f"example.test:{self.server.server_port}"})[0], 403)
        for path in ("/fonts/OFL.txt", "/fonts/", "/fonts/..%2Fapp.js", "/fonts/GeistMono-VF.woff2x"):
            with self.subTest(path=path):
                self.assertIn(self.raw(path)[0], (403, 404))
        self.assertIn("SIL Open Font License", (dashboard._WEB / "fonts" / "OFL.txt").read_text())

    def test_halftone_is_a_bundled_same_origin_image(self):
        for path in ("/halftone.webp", "/clouds.webp"):
            code, headers, body = self.raw(path)
            self.assertEqual((code, headers["Content-Type"]), (200, "image/webp"))
            self.assertEqual(body[8:12], b"WEBP")
            self.assertEqual(self.raw(path, {"Host": "external.test"})[0], 403)

    def test_appearance_changes_require_auth_and_persist(self):
        self.assertIn('data-background="beam" data-tint="cobalt"', self.request()[2])
        self.assertEqual(self.request("/appearance", {"background": "off"}, token=False)[0], 403)
        self.assertEqual(self.request("/appearance", {"background": "not-a-preset"})[0], 400)
        self.assertEqual(self.request("/appearance", {"tint": "pink"})[0], 400)
        self.assertEqual(self.request("/appearance", {"tint": "paper"})[0], 204)
        self.assertIn('data-tint="paper"', self.request("/new")[2])
        for preset in ("quiet", "clouds", "off", "arc", "aurora", "dunes", "orbit", "beam"):
            self.assertEqual(self.request("/appearance", {"background": preset})[0], 204)
            self.assertIn(f'data-background="{preset}"', self.request("/new")[2])
            self.assertEqual(config.load(self.home)["appearance"]["background"], preset)

    def test_swarm_settings_offer_claude_and_devin_models_and_persist_a_valid_choice(self):
        page = self.request("/new")[2]
        self.assertIn('action="/settings/swarm"', page)
        self.assertIn('value="swe-2-max" checked', page)
        self.assertNotIn('name="models" value="gpt-', page)  # the ChatGPT plan is never a swarm worker
        self.assertIn('data-slash-cmd="/swarm "', page)
        post = lambda pairs, **kw: self.request("/settings/swarm", raw=urlencode([*pairs, ("t", self.token)]).encode(), **kw)
        self.assertEqual(self.request("/settings/swarm", {"models": "swe-2-high", "judge": "swe-2-high"}, token=False)[0], 403)
        self.assertEqual(post([("judge", "swe-2-max")])[0], 400)  # none ticked
        self.assertEqual(post([("models", "gpt-6.1-sol"), ("judge", "swe-2-max")])[0], 400)
        self.assertEqual(post([("models", "swe-2-high"), ("judge", "gpt-6.1-sol")])[0], 400)
        self.assertEqual(post([("models", m) for m in ("swe-2-high", "swe-2-max", "swe-2-flash-high", "claude-haiku-5-5",
                                                      "claude-opus-5-5")] + [("judge", "swe-2-max")])[0], 400)
        self.assertEqual(post([("models", "swe-2-high"), ("judge", "swe-2-high"), ("check", "pytest 'open")])[0], 400)
        self.assertEqual(post([("models", "swe-2-high"), ("models", "claude-haiku-5-5"), ("judge", "claude-haiku-5-5"),
                               ("check", " pytest -q ")])[0], 204)
        saved = config.load(self.home)["swarm"]
        self.assertEqual((saved["models"], saved["judge"], saved["check"]),
                         (["swe-2-high", "claude-haiku-5-5"], "claude-haiku-5-5", "pytest -q"))
        self.assertIn('name="check" class="mono"', self.request("/new")[2])
        page = self.request("/new")[2]
        self.assertIn('value="claude-haiku-5-5" checked', page)
        self.assertNotIn('value="swe-2-max" checked', page)

    def test_the_file_picker_lists_tracked_files_behind_the_token(self):
        self.assertEqual(self.request("/files", token=False)[0], 403)
        code, _, body = self.request("/files")
        self.assertEqual(code, 200)
        listed = json.loads(body)["files"]
        self.assertIsInstance(listed, list)
        self.assertTrue(all(isinstance(name, str) and not name.startswith("/") for name in listed))

    def test_composer_uses_the_configured_workspace_name_and_branch(self):
        named = self.base / "harness-visual-test"
        self.repo.rename(named)
        self.cfg["repo"]["path"] = str(named)
        page = self.request("/new")[2]
        # the configured repository heads the page; the line under it names the branch
        self.assertIn('data-repo="harness-visual-test">harness-visual-test</h1>', page)
        self.assertNotIn('title="Repository"', page)
        self.assertRegex(page, r'<span class="where-chip" title="Branch"><svg[^>]*>.*?</svg><span class="wc-t">main</span></span>')
        self.assertNotIn('compose-trail', page)
        self.cfg["repo"]["path"] = str(self.base / "missing")
        page = self.request("/new")[2]
        self.assertNotIn('where-chip"', page)
        self.assertIn('tabindex="-1">What are we building?</h1>', page)  # no repository known: the page asks

    def test_inbox_opens_on_the_newest_item_and_each_item_has_its_own_address(self):
        # An Inbox that opens on an older item, a hand launch opened from it without its prompt, or an item address
        # that serves a missing item, must fail.
        self.job()
        with self.ledger() as led:
            step, _ = led.inbox_add("operator-step", "publish the notes", "web-job")
            launch, _ = led.inbox_add("web-launch", "launch it in Codex", "web-job")
        code, _, page = self.request("/inbox")
        self.assertEqual(code, 200)
        self.assertIn('data-pane="inbox"', page)
        # One row for the job, opened on its newest item: the launch, with its prompt.
        self.assertRegex(page, r'data-key="job:web-job"[^>]*><a[^>]*aria-current="true"')
        self.assertIn('id="q-h" data-clear>Launch it in Codex, then link the task</h2>', page)
        self.assertIn("research-harness job web-job", page)
        code, _, page = self.request(f"/inbox/{step}")
        self.assertEqual(code, 200)
        self.assertRegex(page, r'data-key="job:web-job"[^>]*><a[^>]*aria-current="true"')
        self.assertIn('id="q-h" data-clear>Publish the notes</h2>', page)
        self.assertIn(f'href="/inbox/{launch}?t=', page)  # this job also asks for the launch
        self.assertNotIn("research-harness job web-job", page)
        code, _, page = self.request("/inbox/99999")
        self.assertEqual(code, 404)
        self.assertIn("No inbox item 99999", page)
        self.assertEqual(self.request("/inbox/x")[0], 404)

    def test_this_mac_and_the_seats_come_from_the_lease_and_seats_toml(self):
        # A reading the panel drops, heavy work allowed over the limit, an unreadable host that breaks the page, or a
        # seats.toml the app misreads, must fail.
        from rh.lease import Reading
        calm = self.serve._host(self.cfg, reader=lambda: Reading(level=1, load1=2.5, taken=0))
        self.assertEqual((calm["load1"], calm["level"], calm["ok"]), (2.5, 1, True))
        busy = self.serve._host(self.cfg, reader=lambda: Reading(level=1, load1=12.0, taken=0))
        self.assertFalse(busy["ok"])
        self.assertIn("load 12.00 above 10.00", busy["reason"])

        def broken():
            raise OSError("sysctl missing")
        self.assertEqual(self.serve._host(self.cfg, reader=broken), {"error": "OSError: sysctl missing"})
        self.assertIn("Load, 1 min", self.request()[2])
        self.assertEqual(self.serve._seats(self.cfg), [])
        (self.repo / "seats.toml").write_text(
            '[seat.docs]\nrole = "Docs: the guides"\nroute = "codex-cloud"\nmodel = "m-1"\neffort = "high"\n'
            'owns = ["docs/"]\ndone = "it reads well"\n')
        self.assertEqual(self.serve._seats(self.cfg),
                         [{"name": "docs", "role": "Docs: the guides", "route": "codex-cloud", "model": "m-1"}])
        self.assertIn("<li>docs, idle: The guides</li>", self.request()[2])
        (self.repo / "seats.toml").write_text("[seat.docs]\nrole = 1\n")
        self.assertEqual(self.serve._seats(self.cfg), [])
        self.assertEqual(self.request()[0], 200)

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

    def test_launch_by_hand_moves_a_ready_cloud_job_to_the_web_route_with_its_reason(self):
        self.job("ready")
        self.assertEqual(self.request("/handoff", {"job": "web-job"})[0], 303)
        with self.ledger() as led:
            j = led.job("web-job")
        self.assertEqual((j["state"], j["route"]), ("handoff", "codex-cloud-web"))
        self.assertTrue(j["detail"].startswith("launched by hand on request: rh cloud prompt web-job"))
        self.assertIn("web-job is ready to launch by hand.", self.request()[2])
        self.assertEqual(self.request("/handoff", {"job": "web-job"})[0], 303)
        self.assertIn("only a ready cloud job can be launched by hand", self.request()[2])

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
                    "/harvest": {"job", "text"}, "/land": {"job"}, "/job/cancel": {"job", "reason"},
                    "/tick": set(), "/handoff": {"job"}}
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
        for forbidden in ("<script", "http://", "https://", "<img", "<link"):
            self.assertNotIn(forbidden, page.lower())
        app = self.request()[2].lower()
        for forbidden in ("http://", "https://", "<img", "<link", "<iframe"):
            self.assertNotIn(forbidden, app)
        self.assertEqual(app.count("<script"), 1)

    def test_no_served_page_loads_anything_from_off_the_machine(self):
        # D5: a page that loads a script, style, font or image from anywhere but this server, a second script, or a
        # link off the machine other than the Codex pages and ChatGPT (opened in the browser) must fail.
        self.job()
        item = self.chat_job()
        allowed = ("https://chatgpt.com/codex", "https://chatgpt.com/")
        links = set()
        for path in ("/", "/new", "/inbox", f"/inbox/{item}", "/job/web-job", "/job/chat-job", "/actions"):
            code, _, page = self.request(path)
            self.assertEqual(code, 200, path)
            for tag in re.findall(r"<script\b[^>]*>", page):
                self.assertEqual(re.findall(r'src="([^"]*)"', tag), ["/app.js"], path)
            self.assertLessEqual(page.count("<script"), 1, path)
            for attr, url in re.findall(r'\b(src|href|action|srcset|poster|data)="([^"]*)"', page):
                if not url.startswith(("/", "#", "?")) and url:
                    self.assertEqual(attr, "href", (path, url))
                    self.assertTrue(url == allowed[1] or url.startswith(allowed[0]), (path, url))
                    links.add(url)
            self.assertNotRegex(page, r"url\(\s*['\"]?(https?:)?//", path)
            self.assertNotRegex(page, r"@import", path)
            for url in re.findall(r"https?://[^\s\"'<>)]+", page):
                self.assertTrue(url.startswith(allowed), (path, url))
        self.assertEqual({u.split("/")[2] for u in links}, {"chatgpt.com"})  # both outside links were on show
        self.assertTrue(any(u.startswith(allowed[0]) for u in links) and allowed[1] in links)
        app = self.request("/app.js", token=False)[2]
        self.assertNotRegex(app, r"https?://|fetch\(\s*['\"`]//|new WebSocket|importScripts")

    def test_a_waiting_chat_reads_waiting_for_gpt_pro_to_eyes_and_screen_readers(self):
        # A served page that names a waiting GPT Pro chat "Launch by hand" anywhere, in its text or in a ring's
        # aria-label (the list row, the head, Properties, the Inbox entry, the palette), must fail.
        item = self.chat_job()
        for path in ("/", "/job/chat-job", "/inbox", f"/inbox/{item}"):
            code, _, page = self.request(path)
            self.assertEqual(code, 200, path)
            self.assertNotIn("Launch by hand", page, path)
            self.assertIn('aria-label="Waiting for GPT Pro"', page, path)
        page = self.request("/job/chat-job")[2]
        props = re.search(r'data-region="j-props".*?</section>', page, re.S).group(0)
        state = re.search(r'<span class="pk">State</span><span class="pv">(.*?)</span></div>', props, re.S).group(1)
        self.assertIn('aria-label="Waiting for GPT Pro"', state)
        self.assertEqual(html.unescape(re.sub(r"<[^>]+>", "", state)).strip(), "Waiting for GPT Pro")

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

    def local_job(self, jid="scan-local", state="running", log=None):
        with self.ledger() as led:
            led.add_job({"id": jid, "kind": "local-shell", "write_root": f"runs/{jid}/", "write_paths": [],
                         "brief": "Scan the <listings>.", "needs": [], "command": ["python3", "scan.py"]})
            for step in {"ready": ["ready"], "running": ["ready", "launching", "running"]}[state]:
                led.set_state(jid, step)
        if log is not None:
            run = self.home / "jobs" / jid
            run.mkdir(parents=True)
            (run / "out.log").write_bytes(log)

    def test_job_page_shows_the_job_its_output_and_unknown_jobs_404(self):
        lines = b"".join(b"line %d\n" % i for i in range(400)) + b"last <line> \xff"
        self.local_job(log=lines)
        code, _, page = self.request("/job/scan-local")
        self.assertEqual(code, 200)
        self.assertIn('data-open="job:scan-local"', page)
        self.assertIn("<h1 tabindex=\"-1\" data-clear>Scan the &lt;listings&gt;</h1>", page)
        self.assertIn("python3 scan.py", page)
        self.assertIn("last &lt;line&gt; \ufffd", page)  # the tail, escaped, with undecodable bytes replaced
        self.assertIn("line 399", page)
        self.assertNotIn("line 199\n", page)  # only the last 200 lines
        self.assertIn('<span class="lines mono">last 200 lines</span>', page)
        # Under 200 lines and 64 KB the page has the whole log; past 64 KB it has the end, in fewer than 200 lines.
        self.local_job("short-local", log=b"one\ntwo\nthree\n")
        page = self.request("/job/short-local")[2]
        self.assertIn('<span class="lines mono">3 lines</span>', page)
        self.assertIn('class="job-result"', page)
        self.assertNotIn('<div class="jd-prog">', page)
        self.local_job("wide-local", log=b"".join(b"%04d " % i + b"x" * 995 + b"\n" for i in range(100)))
        page = self.request("/job/wide-local")[2]
        self.assertRegex(page, r'<span class="lines mono">last (6\d) lines</span>')
        self.assertIn(f"Added to run on {dashboard.HERE}", page)
        code, _, page = self.request("/job/ghost")
        self.assertEqual(code, 404)
        self.assertIn("No job named ghost", page)
        self.assertIn('href="/job/scan-local?t=', page)  # ⌘K still lists it
        for path in ("/job/..%2Fhome", "/job/", "/job/Bad"):
            self.assertEqual(self.request(path)[0], 404)

    def test_new_job_goes_through_job_add_and_opens_the_job(self):
        brief = 'Survey "the" papers \\ on <agents>.\r\nLine two with a tab\tand \x7f and é.'
        fields = {"id": "lit-scan-03", "kind": "codex-cloud", "brief": brief, "accept": "notes.md exists",
                  "timeout_min": "45", "command": "", "model": "gpt-6.1-sol", "effort": "max",
                  "input_path": "README.md"}
        raw = urlencode({**fields, "t": self.token}).encode() + b"&needs=net&needs=git"
        code, headers, _ = self.request("/job", raw=raw)
        self.assertEqual(code, 303)
        self.assertEqual(headers["Location"], "/job/lit-scan-03?t=" + self.token)
        with self.ledger() as led:
            job = led.job("lit-scan-03")
        self.assertEqual(job["spec"]["brief"], brief.replace("\r\n", "\n"))
        self.assertEqual(job["spec"]["needs"], ["net", "git"])
        self.assertEqual(job["spec"]["model"], {"requested": "gpt-6.1-sol", "effort": "max"})
        self.assertEqual(job["spec"]["inputs"], [{"path": "README.md"}])
        self.assertEqual((job["spec"]["timeout_min"], job["spec"]["accept"]), (45, "notes.md exists"))
        self.assertEqual(job["pinned_sha"], self.sha)
        self.assertEqual(len(list((self.home / "uploads").glob("lit-scan-03-*.toml"))), 1)
        page = self.request("/job/lit-scan-03")[2]
        self.assertIn("added lit-scan-03 (codex-cloud)", page)
        self.assertIn('data-say="Added lit-scan-03."', page)
        self.assertNotIn("added lit-scan-03 (codex-cloud)", self.request("/job/lit-scan-03")[2])
        # A local job's command is split like a shell line and reaches the contract as argv.
        local = {"id": "say-hi", "kind": "local-shell", "brief": "Say hi.", "command": "python3 -c \"print('hi there')\"",
                 "accept": "", "timeout_min": ""}
        self.assertEqual(self.request("/job", local)[0], 303)
        with self.ledger() as led:
            self.assertEqual(led.job("say-hi")["spec"]["command"], ["python3", "-c", "print('hi there')"])
        # The composer needs only the brief: the name comes from its words, and on This Mac the brief is the command.
        self.assertEqual(self.request("/job", {"id": "", "kind": "local-shell", "brief": "echo hi there"})[0], 303)
        self.assertEqual(self.request("/job", {"id": "", "kind": "local-shell", "brief": "echo hi there"})[0], 303)
        with self.ledger() as led:
            self.assertEqual(led.job("echo-hi-there")["spec"]["command"], ["echo", "hi", "there"])
            self.assertIsNotNone(led.job("echo-hi-there-2"))
        # Codex on this Mac is a local job that runs rh/agent.py on a copy of the repository, never the repo itself.
        with patch("rh.serve.shutil.which", return_value="/usr/local/bin/codex"):
            self.assertEqual(self.request("/job", {"id": "", "kind": "local-agent", "brief": "fix the typo"})[0], 303)
        with self.ledger() as led:
            spec = led.job("fix-the-typo")["spec"]
        self.assertEqual(spec["kind"], "local-shell")
        self.assertEqual(spec["command"][1:], [str(Path(dashboard.__file__).with_name("agent.py")), "codex",
                                               "/usr/local/bin/codex", str(self.cfg["repo"]["path"]), "fix the typo",
                                               "-c", 'service_tier="default"'])
        # a Claude or Devin model picks that provider's CLI, with the model passed through
        for model, provider in (("claude-sonnet-5-5", "claude"), ("swe-2-high", "devin")):
            with patch("rh.serve.shutil.which", return_value=f"/usr/local/bin/{provider}"):
                self.assertEqual(self.request("/job", {"id": f"try-{provider}", "kind": "local-agent",
                                                       "brief": "fix it", "model": model})[0], 303)
            with self.ledger() as led:
                command = led.job(f"try-{provider}")["spec"]["command"]
            self.assertEqual(command[2:4], [provider, f"/usr/local/bin/{provider}"])
            self.assertEqual(command[-2:], ["--model", model])
        # the effort reaches each CLI in its own words, and only at a level the policy allows
        for model, effort, tail, binary in (("claude-opus-5-5", "extra-high", ["--effort", "xhigh"], "claude"),
                                            ("gpt-6.1-sol", "max", ["-c", 'model_reasoning_effort="max"'], "codex")):
            with patch("rh.serve.shutil.which", return_value=f"/usr/local/bin/{binary}"):
                self.request("/job", {"id": f"effort-{binary}", "kind": "local-agent", "brief": "fix it",
                                      "model": model, "effort": effort})
            with self.ledger() as led:
                self.assertEqual(led.job(f"effort-{binary}")["spec"]["command"][-2:], tail)
        # Codex on this Mac takes any level, in its own words; a level no policy names is refused
        with patch("rh.serve.shutil.which", return_value="/usr/local/bin/codex"):
            self.request("/job", {"id": "codex-light", "kind": "local-agent", "brief": "fix it",
                                  "model": "gpt-6.1-sol", "effort": "light"})
            self.request("/job", {"id": "codex-turbo", "kind": "local-agent", "brief": "fix it",
                                  "model": "gpt-6.1-sol", "effort": "turbo"})
        with self.ledger() as led:
            self.assertEqual(led.job("codex-light")["spec"]["command"][-2:], ["-c", 'model_reasoning_effort="low"'])
            self.assertIsNone(led.job("codex-turbo"))
        # the answer goes to the chat unless the composer asks for the file; anything else is refused
        for answer, expect in (("", "chat"), ("file", "file"), ("inbox", None)):
            with patch("rh.serve.shutil.which", return_value="/usr/local/bin/claude"):
                self.request("/job", {"id": f"answer-{answer or 'default'}", "kind": "local-agent", "brief": "fix it",
                                      "model": "claude-haiku-5-5", "answer": answer})
            with self.ledger() as led:
                job = led.job(f"answer-{answer or 'default'}")
                self.assertEqual(job and job["spec"]["answer"], expect)
        # a model outside the dated policy is refused
        with patch("rh.serve.shutil.which", return_value="/usr/local/bin/codex"):
            self.request("/job", {"id": "", "kind": "local-agent", "brief": "use a banned model", "model": "gpt-9"})
        with self.ledger() as led:
            self.assertIsNone(led.job("use-a-banned-model"))

    def test_slash_swarm_runs_the_brief_on_every_swarm_model_then_a_judge_on_what_they_did(self):
        # /swarm must fan out to each model Settings names, and the judge must wait for all of them, see each run's
        # folder, and open as the swarm's page; a /swarm that ran one job, or a judge that needed every run done, fails.
        config.set_value("swarm.models", "swe-2-max,claude-haiku-5-5", self.cfg["home"])
        config.set_value("swarm.judge", "swe-2-high", self.cfg["home"])
        config.set_value("swarm.check", "python3 -I tests/run.py", self.cfg["home"])
        self.cfg.update(config.load(self.cfg["home"]))
        with patch("rh.serve.shutil.which", side_effect=lambda name: f"/usr/local/bin/{name}"):
            code, headers, _ = self.request("/job", {"id": "", "kind": "local-agent", "brief": "/swarm Fix the typo"})
        self.assertEqual(code, 303)
        self.assertEqual(headers["Location"], "/job/fix-the-typo.best?t=" + self.token)
        with self.ledger() as led:
            runs = [led.job("fix-the-typo.w1"), led.job("fix-the-typo.w2")]
            best = led.job("fix-the-typo.best")
        self.assertTrue(all(runs) and best)
        self.assertEqual([r["spec"]["command"][-1] for r in runs], ["swe-2-max", "claude-haiku-5-5"])
        self.assertEqual([r["spec"]["brief"] for r in runs], ["Fix the typo", "Fix the typo"])
        spec = best["spec"]
        self.assertEqual((spec["after"], spec["after_any"]), (["fix-the-typo.w1", "fix-the-typo.w2"], True))
        self.assertEqual(spec["command"][2:4], ["devin", "/usr/local/bin/devin"])
        home = Path(self.cfg["home"])
        self.assertEqual([spec["command"][i + 1] for i, a in enumerate(spec["command"]) if a == "--swarm"],
                         [str(home / "runs" / "fix-the-typo.w1"), str(home / "runs" / "fix-the-typo.w2")])
        self.assertTrue(spec["operator_facing"] and spec["card"]["said"] == "Fix the typo")
        self.assertEqual(spec["command"][-2:], ["--check", "python3 -I tests/run.py"])  # runs are checked first
        # an OpenAI model is never a swarm worker: the plan's usage is the owner's
        config.set_value("swarm.models", "gpt-6.1-sol", self.cfg["home"])
        self.cfg.update(config.load(self.cfg["home"]))
        with patch("rh.serve.shutil.which", side_effect=lambda name: f"/usr/local/bin/{name}"):
            self.request("/job", {"id": "", "kind": "local-agent", "brief": "/swarm Do more"})
        with self.ledger() as led:
            self.assertIsNone(led.job("do-more.best"))

    def test_refused_new_job_returns_to_the_form_with_the_draft(self):
        cases = ({"id": "Bad Name", "kind": "codex-cloud", "brief": "x <b>"},
                 {"id": "bad-quote", "kind": "local-shell", "brief": "x <b>", "command": "echo 'open"},
                 {"id": "bad-time", "kind": "codex-cloud", "brief": "x <b>", "timeout_min": "-5"},
                 {"id": "bad-input", "kind": "codex-cloud", "brief": "x <b>", "input_path": "../notes.md"},
                 {"id": "bad-need", "kind": "codex-cloud", "brief": "x <b>", "needs": "teleport"})
        for fields in cases:
            with self.subTest(fields=fields):
                code, headers, _ = self.request("/job", fields)
                self.assertEqual(code, 303)
                self.assertEqual(headers["Location"], "/new?t=" + self.token)
                code, _, page = self.request("/new")
                self.assertEqual(code, 200)
                self.assertIn('data-open="new"', page)
                self.assertIn('data-code="1"', page)
                self.assertIn("x &lt;b&gt;</textarea>", page)
                self.assertIn(f'value="{html.escape(fields["id"], quote=True)}"', page)
        with self.ledger() as led:
            self.assertEqual(led.jobs(), [])
        # The draft goes once a job is added.
        self.request("/job", {"id": "fine-job", "kind": "local-shell", "brief": "ok", "command": "true"})
        self.assertEqual(self.request()[0], 200)  # the success result is shown and gone
        page = self.request("/new")[2]
        self.assertNotIn("x &lt;b&gt;</textarea>", page)
        self.assertNotIn('value="fine-job"', page)

    def test_cancel_only_stops_a_job_that_has_not_started_or_has_stalled(self):
        self.local_job("queued-one", "ready")
        self.local_job("running-one", "running")
        self.local_job("stuck-one", "running")
        with self.ledger() as led:
            led.set_state("stuck-one", "stalled", "no finish after 180 min")
        cancel = 'action="/job/cancel"'
        self.assertIn(cancel, self.request("/job/stuck-one")[2])
        self.assertNotIn(cancel, self.request("/job/running-one")[2])
        self.assertEqual(self.request("/job/cancel", {"job": "stuck-one", "reason": "built elsewhere"})[0], 303)
        self.assertEqual(self.request("/job/cancel", {"job": "queued-one", "reason": "not needed",
                                                      "next": "/job/queued-one"})[0], 303)
        self.assertEqual(self.request("/job/cancel", {"job": "running-one", "reason": "stop it"})[0], 303)
        self.assertIn("only an unstarted or stalled job can be cancelled", self.request()[2])
        with self.ledger() as led:
            self.assertEqual(led.job("queued-one")["state"], "cancelled")
            self.assertEqual(led.receipt("queued-one")["detail"], "not needed")
            self.assertEqual(led.job("running-one")["state"], "running")
            self.assertEqual(led.job("stuck-one")["state"], "cancelled")

    def test_forms_return_to_the_page_they_came_from_and_nowhere_else(self):
        for target, expected in (("/job/web-job", "/job/web-job?t="), ("/new", "/new?t="), ("/", "/?t="),
                                 ("/inbox", "/inbox?t="), ("/inbox/12", "/inbox/12?t="), ("/inbox/x", "/?t="),
                                 ("/inbox/12/../../actions", "/?t="),
                                 ("https://example.test/", "/?t="), ("//example.test", "/?t="),
                                 ("/job/../actions", "/?t="), ("/actions", "/?t=")):
            with self.subTest(target=target):
                code, headers, _ = self.request("/tick", {"next": target})
                self.assertEqual((code, headers["Location"]), (303, expected + self.token))

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

    def finished_local(self, jid, state="done", detail="command and gate exited 0"):
        spec = contract.normalize({"id": jid, "kind": "local-shell", "brief": "Count the words.",
                                   "command": ["wc", "-w", "notes.md"]})
        with self.ledger() as led:
            led.add_job(spec)
            for step in ("ready", "launching", "running"):
                led.set_state(jid, step)
            led.finish(jid, state, {"route": "local-shell", "checks": [], "model_requested": None,
                                    "model_served": None, "ended": time.time(), "detail": detail})
        return spec

    def test_the_app_checks_by_itself_under_the_supervisor_lock_and_pause_lets_it_go(self):
        # A loop that waits for a page, ticks beside rh supervise, keeps the lock while paused or after the app
        # quits, or a status line that hides who checks, must fail.
        (self.home / "supervisor.json").write_text(json.dumps({"pid": 4242, "ts": time.time()}))  # as rh supervise writes it
        fake = FakeSupervisor()
        with patch.object(self.serve, "_RETRY", .05):
            _, origin, stop = self.served(supervisor=lambda: fake)
            self.assertTrue(until(lambda: fake.ticks >= 1))  # no page was asked for
            self.assertEqual(fake.recovered, 1)
            with self.assertRaises(AlreadyRunning):
                single_instance(self.home)
            self.assertIn("Checking automatically", self.request(origin=origin)[2])
            self.assertEqual(self.request("/checks", {"on": "0"}, origin=origin)[0], 303)
            self.assertIn("Checks paused", self.request(origin=origin)[2])
            self.assertTrue(until(lambda: self.lock_is_free()))  # Pause leaves the lock to rh supervise
            fd = single_instance(self.home)
            try:
                self.assertEqual(self.request("/checks", {"on": "1"}, origin=origin)[0], 303)
                ticks = fake.ticks
                time.sleep(.3)
                self.assertEqual(fake.ticks, ticks)  # rh supervise holds it: the app does not tick
                page = self.request(origin=origin)[2]
                self.assertIn("Checked by rh supervise (process 4242)", page)
                self.assertNotIn('action="/checks"', page)
                # Check now says who is checking instead of ticking beside it.
                self.assertEqual(self.request("/tick", {}, origin=origin)[0], 303)
                page = self.request(origin=origin)[2]
                self.assertIn('data-code="1"', page)
                self.assertIn("rh supervise (process 4242) is checking this harness.", page)
                self.assertEqual(fake.ticks, ticks)
            finally:
                oslock.unlock(fd)
            self.assertTrue(until(lambda: fake.ticks > ticks))  # and takes over once rh supervise stops
            stop()
        self.assertTrue(self.lock_is_free())  # quitting the app lets the lock go

    def lock_is_free(self):
        try:
            fd = single_instance(self.home)
        except AlreadyRunning:
            return False
        oslock.unlock(fd)
        return True

    def test_check_now_runs_through_the_apps_own_loop(self):
        # Check now taking a second lock (and failing against the app's own), or not waiting for its pass, must fail.
        fake = FakeSupervisor()
        _, origin, _ = self.served(supervisor=lambda: fake)
        self.assertTrue(until(lambda: fake.ticks >= 1))
        before = fake.ticks
        self.assertEqual(self.request("/tick", {}, origin=origin)[0], 303)
        self.assertEqual(fake.ticks, before + 1)
        self.assertIn('data-say="Checked."', self.request(origin=origin)[2])
        # Paused, Check once still checks, then leaves the lock free again.
        self.request("/checks", {"on": "0"}, origin=origin)
        self.assertEqual(self.request("/tick", {}, origin=origin)[0], 303)
        self.assertEqual(fake.ticks, before + 2)
        self.assertTrue(self.lock_is_free())
        # A server without the loop has no switch to offer.
        self.assertEqual(self.request("/checks", {"on": "0"})[0], 404)

    def test_a_job_added_in_the_terminal_runs_to_done_with_no_request(self):
        # The founder's first complaint: a job that does not move until someone presses Check now must fail.
        job = self.base / "say.toml"
        job.write_text(self.serve._toml({"id": "say-hi", "kind": "local-shell", "brief": "Write hi to hi.txt.",
                                         "command": [sys.executable, "-c", "open('hi.txt', 'w').write('hi')"],
                                         "accept": "test -s hi.txt"}))
        with patch.object(self.serve, "_BUSY", .1), patch.object(self.serve, "_FAST", .1):
            server, _, _ = self.served()
            # Added after the loop's first idle check, so without the wake-up it waits out the 60 s interval.
            self.assertTrue(until(lambda: server.checks.last is not None, 10), server.checks.state())
            code, output = self.serve._call(self.cfg, ["job", "add", "--", str(job)])
            self.assertEqual(code, 0, output)
            with self.ledger() as led:
                self.assertTrue(until(lambda: led.job("say-hi")["state"] == "done", 30),
                                (led.job("say-hi")["state"], led.job("say-hi")["detail"], server.checks.state(),
                                 server.checks.thread.is_alive()))
        self.assertEqual((self.home / "runs" / "say-hi" / "hi.txt").read_text(), "hi")
        page = self.request("/job/say-hi")[2]
        transcript = page.split('<aside class="jd-side"', 1)[0]
        self.assertIn('aria-label="Files produced"', transcript)
        self.assertIn('/job/say-hi/file?p=hi.txt', transcript)

    def test_a_local_job_started_in_the_composer_has_a_live_session_and_receipt(self):
        # Keep the command alive until the test has viewed its actual running page.
        release = self.base / "release"
        code = ("from pathlib import Path; import time\n"
                "print('Comparing the two sets.', flush=True)\n"
                f"while not Path({str(release)!r}).exists(): time.sleep(.05)\n"
                "Path('proof.txt').write_text('checked')")
        with patch.object(self.serve, "_BUSY", .1), patch.object(self.serve, "_FAST", .1):
            server, origin, _ = self.served()
            self.assertTrue(until(lambda: server.checks.last is not None))
            try:
                result = self.request("/job", {"id": "compare", "kind": "local-shell", "brief": "Compare both sets.",
                                               "command": shlex.join([sys.executable, "-c", code]),
                                               "accept": "test -s proof.txt"}, origin=origin)
                self.assertEqual(result[0], 303)
                log = self.home / "jobs" / "compare" / "out.log"
                self.assertTrue(until(lambda: log.exists() and "Comparing the two sets." in log.read_text()))
                page = self.request("/job/compare", origin=origin)[2]
                self.assertIn('<div class=\"user-message\"><span class=\"um-t\">Compare both sets.</span></div>', page)
                self.assertIn('<details class="worked"', page)
                self.assertIn("Comparing the two sets.", page)
                self.assertIn('<details class="jd-inspector"><summary>', page)
                self.assertNotIn('class="context-ring"', page)
            finally:
                release.touch()
            with self.ledger() as led:
                self.assertTrue(until(lambda: led.job("compare")["state"] == "done"))
                self.assertEqual([check["exit"] for check in led.receipt("compare")["checks"]], [0, 0])
        self.assertEqual((self.home / "runs" / "compare" / "proof.txt").read_text(), "checked")

    def test_closing_the_app_closes_its_loops_ledger(self):
        # On Windows an open ledger.db cannot be removed, so a loop that keeps it open past close must fail.
        closed, close = [], Ledger.close
        with patch.object(Ledger, "close", lambda led: closed.append(led) or close(led)):
            server, _, stop = self.served()
            self.assertTrue(until(lambda: server.checks.sup is not None, 10), server.checks.state())
            led = server.checks.sup.led
            stop()
        self.assertTrue(any(c is led for c in closed))

    def test_run_again_adds_the_next_revision_and_opens_it(self):
        # A rerun that overwrites the old job, drops what changed, or lets an unfinished job run twice must fail.
        old = self.finished_local("count-words", "failed", "command exited 2")
        code, headers, _ = self.request("/again", {"job": "count-words", "changed": "also count blank lines",
                                                   "input_path": "README.md",
                                                   "next": "/job/count-words"})
        self.assertEqual((code, headers["Location"]), (303, "/job/count-words-r2?t=" + self.token))
        with self.ledger() as led:
            new = led.job("count-words-r2")["spec"]
            self.assertEqual(led.job("count-words")["state"], "failed")
            self.assertEqual(led.job("count-words")["spec"]["inputs"], [])
        self.assertEqual((new["revision"], new["prior"], new["changed"]), (2, "count-words", "also count blank lines"))
        self.assertEqual(new["brief"], "Count the words.\n\nFollow-up: also count blank lines")
        self.assertEqual(new["inputs"], [{"path": "README.md"}])
        self.assertEqual((new["write_root"], new["command"]), ("runs/count-words-r2/", old["command"]))
        self.assertIn('data-say="Added count-words-r2, the next run of count-words."',
                      self.request("/job/count-words-r2")[2])
        self.request("/again", {"job": "count-words", "changed": ""})
        with self.ledger() as led:
            third = led.job("count-words-r3")["spec"]
        self.assertEqual((third["changed"], third["brief"]), ("run again with no changes", "Count the words."))
        self.request("/again", {"job": "count-words", "input_path": "../outside", "next": "/job/count-words"})
        self.assertIn("an input must be a path relative to the repository", self.request("/job/count-words")[2])
        with self.ledger() as led:
            self.assertIsNone(led.job("count-words-r4"))
        self.local_job("busy-one", "running")
        code, headers, _ = self.request("/again", {"job": "busy-one", "changed": "", "next": "/job/busy-one"})
        self.assertEqual(headers["Location"], "/job/busy-one?t=" + self.token)
        self.assertIn("only a finished job can run again", self.request("/job/busy-one")[2])
        with self.ledger() as led:
            self.assertIsNone(led.job("busy-one-r2"))

    def test_an_agents_follow_up_continues_its_chat_on_the_model_picked(self):
        # A follow-up the agent never sees, a model switch that is ignored or keeps the old model's effort, a run
        # that starts from scratch instead of from the last one, or a page that does not show the chat so far, must fail.
        agent = str(Path(dashboard.__file__).with_name("agent.py"))
        spec = contract.normalize({"id": "tidy-up", "kind": "local-shell", "brief": "Tidy the README.",
                                   "command": ["python3", agent, "claude", "/usr/local/bin/claude", "/repo",
                                               "Tidy the README.", "--model", "claude-sonnet-5-5", "--effort", "high"]})
        with self.ledger() as led:
            led.add_job(spec)
            for step in ("ready", "launching", "running"):
                led.set_state("tidy-up", step)
            led.finish("tidy-up", "done", {"route": "local-shell", "checks": [], "model_requested": None,
                                           "model_served": None, "ended": time.time(), "detail": "ok"})
        (self.home / "runs" / "tidy-up").mkdir(parents=True)
        (self.home / "runs" / "tidy-up" / "answer.md").write_text("**Tidied** the intro.", encoding="utf-8")
        with patch("rh.serve.shutil.which", return_value="/usr/local/bin/devin"):
            self.request("/again", {"job": "tidy-up", "changed": "now the install part", "model": "swe-2-high"})
        with patch("rh.serve.shutil.which", return_value="/usr/local/bin/claude"):
            self.request("/again", {"job": "tidy-up", "changed": "and the license"})
        with self.ledger() as led:
            devin, claude = led.job("tidy-up-r2")["spec"]["command"], led.job("tidy-up-r3")["spec"]["command"]
        self.assertEqual(devin[2:4], ["devin", "/usr/local/bin/devin"])
        self.assertEqual(devin[5], "Tidy the README.\n\nFollow-up: now the install part")
        prior = str(Path(self.cfg["home"]) / "runs" / "tidy-up")
        self.assertEqual(devin[6:], ["--model", "swe-2-high", "--prior", prior])
        self.assertEqual(claude[2], "claude")
        self.assertEqual(claude[6:], ["--model", "claude-sonnet-5-5", "--effort", "high",
                                      "--prior", prior])
        page = self.request("/job/tidy-up-r2")[2]
        self.assertIn('data-region="j-thread"', page)
        self.assertIn("<strong>Tidied</strong> the intro.", page)
        self.assertIn('<div class=\"user-message\"><span class=\"um-t\">Tidy the README.</span></div>', page)
        self.assertIn('<div class=\"user-message\"><span class=\"um-t\">now the install part</span></div>', page)
        self.assertEqual(re.findall(r'<div class="user-message"><span class="um-t">([^<]*)</span></div>', page),
                         ["Tidy the README.", "now the install part"])  # each ask once, no "Follow-up:" glue

    def test_setup_saves_the_environment_and_closes_its_step(self):
        # An environment ID that reaches the config unchecked, or a setup step left open after it is done, must fail.
        with self.ledger() as led:
            led.inbox_add("operator-step", SETUP, detail="cloud jobs wait for it")
        self.request("/setup", {"env_id": "env 1; rm -rf ~"})
        self.assertIn('data-say="Codex cloud was not set up."', self.request()[2])
        for bad in ("", "x" * 129, "../env"):
            self.request("/setup", {"env_id": bad})
        self.assertEqual(config.load(self.home)["cloud"]["env_id"], "")
        self.assertEqual(self.request("/setup", {"env_id": " env_6a1-b.c:2 "})[0], 303)
        self.assertEqual(config.load(self.home)["cloud"]["env_id"], "env_6a1-b.c:2")
        self.assertEqual(self.cfg["cloud"]["env_id"], "env_6a1-b.c:2")  # the running loop sees it without a restart
        with self.ledger() as led:
            self.assertEqual(led.inbox(), [])
            self.assertEqual(led.inbox(False)[0]["resolution"], "environment ID set in the app")

    def test_a_finished_jobs_files_are_served_read_only_from_its_own_folder(self):
        # A file route that reads outside the job's folder, follows a link out of it, or serves bytes as text
        # must fail.
        self.finished_local("made-notes")
        root = self.home / "runs" / "made-notes"
        (root / "sub").mkdir(parents=True)
        (root / "notes.md").write_bytes(b"hello <b>\n")  # bytes: Windows text mode writes CRLF
        (root / "sub" / "data.txt").write_bytes(b"1,2\n")
        (root / "blob.bin").write_bytes(b"\xff\xfe\x00")
        (self.base / "secret.txt").write_text("secret")
        (root / "leak.txt").symlink_to(self.base / "secret.txt")
        page = self.request("/job/made-notes")[2]
        for shown in ("notes.md", "sub/data.txt", "blob.bin", "hello &lt;b&gt;"):
            self.assertIn(shown, page)
        self.assertNotIn("leak.txt", page)
        code, headers, body = self.request("/job/made-notes/file?p=notes.md")
        self.assertEqual((code, headers["Content-Type"], body), (200, "text/plain; charset=utf-8", "hello <b>\n"))
        self.assertTrue(headers["Content-Security-Policy"].startswith("default-src 'none'"))
        self.assertEqual(self.request("/job/made-notes/file?p=sub/data.txt")[2], "1,2\n")
        for path in ("../../../secret.txt", "leak.txt", "missing.txt", "sub", ""):
            with self.subTest(path=path):
                self.assertEqual(self.request("/job/made-notes/file?" + urlencode({"p": path}))[0], 404)
        self.assertEqual(self.request("/job/made-notes/file?p=blob.bin")[0], 415)
        self.assertEqual(self.request("/job/made-notes/file?p=notes.md&p=sub/data.txt")[0], 404)
        self.assertEqual(self.request("/job/made-notes/file?p=notes.md", token=False)[0], 403)
        self.assertEqual(self.request("/job/ghost/file?p=notes.md")[0], 404)

    def test_a_pasted_task_link_attaches_and_short_answers_get_a_default(self):
        # A pasted link refused as a task id, or a reversible step that demands an essay, must fail.
        self.job()
        task = "task_e_" + "9" * 32
        self.request("/attach", {"job": "web-job", "task_id": f" https://chatgpt.com/codex/tasks/{task}?tab=diff "})
        with self.ledger() as led:
            self.assertEqual(led.job("web-job")["external_id"], task)
            item, _ = led.inbox_add("operator-step", "sign the form")
        self.request("/inbox/resolve", {"id": str(item), "note": "  "})
        self.request("/stop", {"job": "web-job", "pause": "on", "reason": ""})
        with self.ledger() as led:
            self.assertEqual(led.inbox(False)[-1]["resolution"], "done in the app")
            self.assertEqual([s["reason"] for s in led.active_stops()], ["paused from the app"])

    def test_a_hand_launch_page_carries_the_prompt_to_paste(self):
        # A launch page without the prompt, or with the CLI's checksum line pasted into it, must fail.
        self.job()
        page = self.request("/job/web-job")[2]
        self.assertIn("research-harness job web-job", page)
        self.assertNotIn("paste once; compare before sending", page)
        self.assertIn('href="https://chatgpt.com/codex" target="_blank" rel="noopener noreferrer"', page)
