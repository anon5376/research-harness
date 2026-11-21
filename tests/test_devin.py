import json
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rh.adapters.devin import CAPACITY, STATUSES, Devin, RouteUnavailable, classify, map_status

KEY = "test-key-not-real"
ORG = "org-test"


def session(sid="devin-1", status="running", detail="working", tags=("rh:job-1",), **extra):
    return {"session_id": sid, "url": f"https://app.devin.ai/sessions/{sid}", "status": status,
            "status_detail": detail, "tags": list(tags), "org_id": ORG, "created_at": 1, "updated_at": 1,
            "acus_consumed": 0, "pull_requests": [], **extra}


class FakeApi:
    """Answers (method, path) from a script and records every request it was sent."""

    def __init__(self, **script):
        self.script, self.calls = script, []

    def __call__(self, method, url, headers, body, timeout):
        parts = urllib.parse.urlsplit(url)
        path = parts.path.split(f"/organizations/{ORG}/", 1)[1]
        self.calls.append({"method": method, "path": path, "query": urllib.parse.parse_qs(parts.query),
                           "headers": headers, "body": json.loads(body) if body else None})
        answer = self.script.get(f"{method} {path}")
        if answer is None:
            raise AssertionError(f"unscripted call {method} {path}")
        if isinstance(answer, BaseException):
            raise answer
        code, payload = answer[:2]
        headers_out = answer[2] if len(answer) > 2 else {}
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return code, headers_out, data


def client(**script):
    api = FakeApi(**script)
    return Devin(ORG, KEY, send=api, timeout=5), api


class MapStatusTest(unittest.TestCase):
    def test_documented_pairs(self):
        table = [
            (("new", None), "running"), (("claimed", None), "running"), (("resuming", None), "running"),
            (("running", "working"), "running"), (("running", None), "running"),
            (("running", "waiting_for_user"), "waiting"), (("running", "waiting_for_approval"), "waiting"),
            (("running", "finished"), "completed"), (("exit", "finished"), "completed"), (("exit", None), "completed"),
            (("exit", "inactivity"), "completed"), (("exit", "user_request"), "aborted"),
            (("suspended", "inactivity"), "waiting"), (("suspended", "user_request"), "waiting"),
            (("suspended", "finished"), "completed"), (("error", None), "failed"), (("running", "error"), "failed"),
            (("error", "out_of_credits"), "capacity"),
        ]
        for (status, detail), want in table:
            with self.subTest(status=status, detail=detail):
                self.assertEqual(map_status(status, detail), want)

    def test_every_capacity_detail_is_capacity_whatever_the_status(self):
        for status in STATUSES:
            for detail in CAPACITY:
                self.assertEqual(map_status(status, detail), "capacity", (status, detail))

    def test_unknown_status_never_becomes_terminal(self):
        for raw in ("finished", "done", "", None, 3, "EXITED"):
            self.assertEqual(map_status(raw, "finished"), "unknown", raw)

    def test_unknown_detail_on_a_live_status_stays_running(self):
        self.assertEqual(map_status("running", "brand_new_detail"), "running")
        self.assertEqual(map_status(" Running ", "WORKING"), "running")


class ClassifyTest(unittest.TestCase):
    def test_codes(self):
        self.assertEqual(classify(401), ("auth", None))
        self.assertEqual(classify(403), ("auth", None))
        self.assertEqual(classify(429, headers={"Retry-After": "7"}), ("rate-limited", 7.0))
        self.assertEqual(classify(429, headers={"retry-after": "soon"}), ("rate-limited", None))
        self.assertEqual(classify(503, headers={"Retry-After": "30"}), ("timeout", 30.0))
        self.assertEqual(classify(404), ("error", None))
        self.assertEqual(classify(422), ("error", None))


class LaunchTest(unittest.TestCase):
    def test_creates_one_tagged_session(self):
        dev, api = client(**{"GET sessions": (200, {"items": []}),
                             "POST sessions": (200, session(tags=["rh:job-1"]))})
        got = dev.launch("job-1", "fix the flaky test", repos=["owner/repo"], max_acu_limit=5, mode="fast")
        self.assertEqual(got["session_id"], "devin-1")
        self.assertFalse(got["reused"])
        post = api.calls[-1]
        self.assertEqual(post["method"], "POST")
        self.assertEqual(post["body"], {"prompt": "fix the flaky test", "title": "rh job-1", "tags": ["rh:job-1"],
                                        "repos": ["owner/repo"], "max_acu_limit": 5, "devin_mode": "fast"})
        self.assertEqual(post["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertEqual(api.calls[0]["query"]["tags"], ["rh:job-1"])

    def test_unset_options_are_not_sent(self):
        dev, api = client(**{"GET sessions": (200, {"items": []}), "POST sessions": (200, session())})
        dev.launch("job-1", "x")
        self.assertEqual(set(api.calls[-1]["body"]), {"prompt", "title", "tags"})

    def test_a_job_that_already_has_a_session_gets_it_back_not_a_second_one(self):
        dev, api = client(**{"GET sessions": (200, {"items": [session("devin-old", created_at=1),
                                                              session("devin-new", created_at=9)]})})
        got = dev.launch("job-1", "x")
        self.assertEqual(got["session_id"], "devin-new")
        self.assertTrue(got["reused"])
        self.assertEqual([c["method"] for c in api.calls], ["GET"])

    def test_untagged_and_archived_sessions_are_not_this_jobs(self):
        items = [session("devin-other", tags=["rh:job-10"]), session("devin-arch", is_archived=True)]
        dev, api = client(**{"GET sessions": (200, {"items": items}), "POST sessions": (200, session("devin-2"))})
        self.assertEqual(dev.launch("job-1", "x")["session_id"], "devin-2")
        self.assertEqual(api.calls[-1]["method"], "POST")

    def test_bad_arguments_make_no_call(self):
        dev, api = client()
        bad = [lambda: dev.launch("../job", "x"), lambda: dev.launch("job-1", "  "),
               lambda: dev.launch("job-1", "x", repos=["not a repo"]), lambda: dev.launch("job-1", "x", mode="max"),
               lambda: dev.launch("job-1", "x", max_acu_limit=0), lambda: dev.launch("job-1", "x", max_acu_limit=True),
               lambda: dev.status("devin-1/../../x"), lambda: dev.terminate("")]
        for fn in bad:
            with self.assertRaises(ValueError):
                fn()
        self.assertEqual(api.calls, [])

    def test_constructor_refuses_a_bad_org_or_key(self):
        for org, key in (("acme", KEY), (ORG, ""), (ORG, "two words"), (ORG, "line\nbreak")):
            with self.assertRaises(ValueError):
                Devin(org, key, send=FakeApi())


class StatusTest(unittest.TestCase):
    def test_fields(self):
        raw = session(status="exit", detail="finished", acus_consumed=2.5, structured_output={"ok": True},
                      pull_requests=[{"pr_url": "https://github.com/o/r/pull/3", "pr_state": "open"}, {"bad": 1}])
        dev, api = client(**{"GET sessions/devin-1": (200, raw)})
        got = dev.status("devin-1")
        self.assertEqual(got["state"], "completed")
        self.assertEqual(got["pull_requests"], [{"url": "https://github.com/o/r/pull/3", "state": "open"}])
        self.assertEqual(got["acus"], 2.5)
        self.assertEqual(got["structured_output"], {"ok": True})

    def test_terminate_archives_only_when_asked(self):
        dev, api = client(**{"DELETE sessions/devin-1": (200, session(status="exit", detail="user_request"))})
        self.assertEqual(dev.terminate("devin-1")["state"], "aborted")
        dev.terminate("devin-1", archive=True)
        self.assertEqual([c["query"] for c in api.calls], [{}, {"archive": ["true"]}])


class ErrorTest(unittest.TestCase):
    def reason(self, fn):
        with self.assertRaises(RouteUnavailable) as cm:
            fn()
        return cm.exception

    def test_http_errors_carry_the_route_reason(self):
        problem = {"title": "Too Many Requests", "status": 429, "detail": "slow down"}
        dev, _ = client(**{"GET sessions/devin-1": (429, problem, {"Retry-After": "12"})})
        e = self.reason(lambda: dev.status("devin-1"))
        self.assertEqual((e.reason, e.retry_after), ("rate-limited", 12.0))
        self.assertIn("Too Many Requests: slow down", str(e))
        dev, _ = client(**{"GET sessions/devin-1": (401, {"title": "Unauthorized", "status": 401})})
        self.assertEqual(self.reason(lambda: dev.status("devin-1")).reason, "auth")

    def test_no_answer_is_a_timeout(self):
        dev, _ = client(**{"GET sessions/devin-1": TimeoutError("timed out")})
        self.assertEqual(self.reason(lambda: dev.status("devin-1")).reason, "timeout")
        dev, _ = client(**{"GET sessions/devin-1": ConnectionRefusedError(61, "Connection refused")})
        self.assertEqual(self.reason(lambda: dev.status("devin-1")).reason, "timeout")

    def test_answers_without_a_usable_session_are_errors(self):
        for payload in (b"<html>gateway</html>", b"[]", json.dumps({"status": "running"}).encode(),
                        json.dumps(session(sid="../x")).encode()):
            dev, _ = client(**{"GET sessions/devin-1": (200, payload)})
            self.assertEqual(self.reason(lambda: dev.status("devin-1")).reason, "error", payload)

    def test_the_key_never_reaches_an_error_or_repr(self):
        echo = {"title": "Forbidden", "status": 403, "detail": f"key {KEY} is not allowed"}
        dev, _ = client(**{"GET sessions/devin-1": (403, echo)})
        e = self.reason(lambda: dev.status("devin-1"))
        self.assertNotIn(KEY, str(e))
        self.assertIn("[redacted]", str(e))
        dev, _ = client(**{"GET sessions/devin-1": OSError(f"proxy said Bearer {KEY}")})
        self.assertNotIn(KEY, str(self.reason(lambda: dev.status("devin-1"))))
        self.assertNotIn(KEY, repr(dev))

    def test_health(self):
        dev, _ = client(**{"GET sessions": (200, {"items": [], "has_next_page": False})})
        self.assertTrue(dev.health()["ok"])
        dev, _ = client(**{"GET sessions": (401, {"title": "Unauthorized", "status": 401})})
        h = dev.health()
        self.assertFalse(h["ok"])
        self.assertIn("auth", h["detail"])
        dev, _ = client(**{"GET sessions": (200, {"sessions": []})})
        self.assertFalse(dev.health()["ok"])


class _Server:
    """A real HTTP server on 127.0.0.1 that answers every request with `answer(handler)`."""

    def __init__(self, answer):
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _any(self):
                n = int(self.headers.get("Content-Length") or 0)
                seen.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"),
                             "body": self.rfile.read(n) if n else b""})
                code, headers, body = answer(self)
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_DELETE = _any

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class RealHttpTest(unittest.TestCase):
    def serve(self, answer):
        s = _Server(answer)
        self.addCleanup(s.close)
        return s

    def test_request_on_the_wire(self):
        s = self.serve(lambda h: (200, {"Content-Type": "application/json"},
                                  json.dumps(session() if h.command == "POST" else {"items": []}).encode()))
        got = Devin(ORG, KEY, base=s.url + "/v3", timeout=5).launch("job-1", "hello")
        self.assertEqual(got["session_id"], "devin-1")
        get, post = s.seen
        self.assertTrue(get["path"].startswith(f"/v3/organizations/{ORG}/sessions?"))
        self.assertEqual(post["auth"], f"Bearer {KEY}")
        self.assertEqual(json.loads(post["body"])["prompt"], "hello")

    def test_a_redirect_is_refused_so_the_key_stays_home(self):
        other = self.serve(lambda h: (200, {}, b'{"items": []}'))
        s = self.serve(lambda h: (302, {"Location": other.url + "/steal"}, b""))
        with self.assertRaises(RouteUnavailable) as cm:
            Devin(ORG, KEY, base=s.url + "/v3", timeout=5).status("devin-1")
        self.assertIn("redirect", str(cm.exception))
        self.assertEqual(other.seen, [])

    def test_http_error_bodies_come_back_through_urllib(self):
        s = self.serve(lambda h: (429, {"Retry-After": "3"}, b'{"title": "Too Many Requests", "status": 429}'))
        with self.assertRaises(RouteUnavailable) as cm:
            Devin(ORG, KEY, base=s.url + "/v3", timeout=5).status("devin-1")
        self.assertEqual((cm.exception.reason, cm.exception.retry_after), ("rate-limited", 3.0))


if __name__ == "__main__":
    unittest.main()
