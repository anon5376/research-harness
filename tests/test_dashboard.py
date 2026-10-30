"""The dashboard presents the existing status view without trusting worker text."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from rh import contract, dashboard
from rh.ledger import Ledger

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_000_000.0


def full_view():
    return {
        "supervisor": {"pid": 123, "age_s": 12},
        "stops": [{"id": 1, "scope": "job:live-job", "kind": "pause", "owner": "lead",
                   "reason": "review output", "created": NOW - 20, "lifted": None, "lifted_by": None}],
        "routes": [{"route": "codex-cloud", "healthy": 0, "failures": 3,
                    "next_try": NOW + 40, "last_status": "rate-limited", "retry_after": None,
                    "updated": NOW - 5}],
        "live": [{"id": "live-job", "state": "running", "paused": False,
                  "detail": "worker output", "external_id": "worker-123"}],
        "not_started": [{"id": "queued-job", "state": "held", "paused": False,
                         "detail": "waiting for input", "external_id": None}],
        "inbox": [{"id": 7, "kind": "decision", "job": "live-job", "action": "review result",
                   "detail": "check the file", "created": NOW - 31, "resolved": None, "resolution": None}],
        "last_24h": {"done": 2, "failed": 1},
        "recent": [{"id": "finished-job", "state": "done", "paused": False,
                    "detail": "accepted output", "external_id": None}],
    }


class Page(HTMLParser):
    """Inspect rendered text and elements without relying on presentation markup."""
    def __init__(self, source):
        super().__init__()
        self.tags, self.headings, self.text = [], [], []
        self.heading = None
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "h2":
            self.heading = []

    def handle_data(self, data):
        self.text.append(data)
        if self.heading is not None:
            self.heading.append(data)

    def handle_endtag(self, tag):
        if tag == "h2" and self.heading is not None:
            self.headings.append("".join(self.heading).strip().lower())
            self.heading = None


class DashboardTest(unittest.TestCase):
    def test_detail_is_escaped(self):
        # Removing escaping must expose the worker's script and fail this test.
        view = full_view()
        view["live"][0]["detail"] = '<script>alert(1)</script> & "quoted"'
        page = dashboard.render(view, NOW)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn("&amp;", page)
        self.assertIn("&quot;quoted&quot;", page)
        self.assertNotIn("<script", page.lower())
        self.assertIn('<script>alert(1)</script> & "quoted"', "".join(Page(page).text))
        # Positive control: the parser detects an actual script in unsafe HTML.
        self.assertIn("script", [tag for tag, _ in Page("<script>alert(1)</script>").tags])

    def test_headings_and_empty_sections(self):
        # Omitting, reordering or hiding an empty section must fail.
        headings = ["supervisor", "stops", "routes", "live", "not started", "inbox", "last 24h", "recent"]
        self.assertEqual(Page(dashboard.render(full_view(), NOW)).headings, headings)
        empty = {"supervisor": None, "stops": [], "routes": [], "live": [], "not_started": [],
                 "inbox": [], "last_24h": {}, "recent": []}
        for view in ({}, empty):
            with self.subTest(view=view):
                page = Page(dashboard.render(view, NOW))
                self.assertEqual(page.headings, headings)
                self.assertEqual([t.strip() for t in page.text].count("none"), 7)
                self.assertIn("nothing finished", page.text)

    def test_paused_marker(self):
        # A missing or unconditional PAUSED marker must fail in every job section.
        for section in ("live", "not_started", "recent"):
            view = full_view()
            with self.subTest(section=section):
                view[section][0]["paused"] = True
                self.assertIn("PAUSED", "".join(Page(dashboard.render(view, NOW)).text))
                view[section][0]["paused"] = False
                self.assertNotIn("PAUSED", "".join(Page(dashboard.render(view, NOW)).text))

    def test_self_contained_document(self):
        # External resources, scripts or missing mobile/theme styling must fail.
        page = dashboard.render(full_view(), NOW)
        self.assertTrue(page.lower().startswith("<!doctype html>"))
        self.assertTrue(page.rstrip().endswith("</html>"))
        self.assertNotIn("http://", page.lower())
        self.assertNotIn("https://", page.lower())
        self.assertNotIn("<script", page.lower())
        self.assertNotIn("@import", page.lower())
        self.assertNotIn("url(", page.lower())
        self.assertIn("prefers-color-scheme: dark", page)
        self.assertIn("overflow-wrap", page)
        parsed = Page(page)
        tags = [tag for tag, _ in parsed.tags]
        for tag in ("html", "head", "title", "style", "body", "main"):
            self.assertIn(tag, tags)
        for tag in ("script", "link", "img", "iframe", "object", "embed", "video", "audio", "font"):
            self.assertNotIn(tag, tags)
        self.assertEqual(tags.count("section"), 8)
        self.assertTrue(any(tag == "meta" and attrs.get("name") == "viewport"
                            and "width=device-width" in attrs.get("content", "")
                            for tag, attrs in parsed.tags))
        for _, attrs in parsed.tags:
            self.assertFalse(any(k.startswith("on") or k in ("src", "href") for k in attrs))

    def test_all_text_fields_are_present_and_escaped(self):
        # Any unescaped or silently omitted visible string field must fail.
        fields = [(section, key) for section in ("live", "not_started", "recent")
                  for key in ("id", "state", "detail", "external_id")]
        fields += [("stops", key) for key in ("kind", "scope", "owner", "reason")]
        fields += [("routes", key) for key in ("route", "last_status")]
        fields += [("inbox", key) for key in ("kind", "job", "action", "detail")]
        value = '<b title="worker">O\'Brien & output</b>'
        escaped = '&lt;b title=&quot;worker&quot;&gt;O&#x27;Brien &amp; output&lt;/b&gt;'
        for section, key in fields:
            with self.subTest(section=section, key=key):
                view = full_view()
                view[section][0][key] = value
                page = dashboard.render(view, NOW)
                self.assertIn(escaped, page)
                self.assertIn(value, "".join(Page(page).text))
                self.assertNotIn(value, page)
        view = full_view()
        view["last_24h"] = {value: 8}
        self.assertIn(escaped, dashboard.render(view, NOW))

    def test_status_content_and_relative_times(self):
        # Dropping a row field, count or relative timestamp must fail.
        view = full_view()
        page = dashboard.render(view, NOW)
        text = " ".join(Page(page).text)
        for value in ("123", "12 s ago", "20 s ago", "31 s ago", "5 s ago", "in 40 s",
                      "lead", "review output", "job:live-job", "codex-cloud", "rate-limited",
                      "failures 3", "live-job", "running", "worker output", "worker-123", "queued-job",
                      "held", "waiting for input", "review result", "check the file", "finished-job",
                      "accepted output", "done 2", "failed 1"):
            with self.subTest(value=value):
                self.assertIn(value, text)
        later = " ".join(Page(dashboard.render(view, NOW + 10)).text)
        for value in ("30 s ago", "41 s ago", "15 s ago", "in 30 s"):
            self.assertIn(value, later)
        view["routes"][0]["next_try"] = NOW - 8
        self.assertIn("8 s ago", " ".join(Page(dashboard.render(view, NOW)).text))

    def test_render_is_deterministic_and_does_not_mutate_view(self):
        # Mutating the source ledger view or consulting wall-clock time must fail.
        view = full_view()
        before = copy.deepcopy(view)
        self.assertEqual(dashboard.render(view, NOW), dashboard.render(view, NOW))
        self.assertEqual(view, before)

    def test_status_html_end_to_end(self):
        # A broken CLI hook or a page divorced from the ledger must fail.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            led = Ledger(home / "ledger.db")
            try:
                spec = contract.normalize({"id": "dashboard-local-done", "kind": "local-shell",
                                           "brief": "Run the local step.",
                                           "command": [sys.executable, "-c", "pass"]})
                led.add_job(spec)
                for state in ("ready", "launching", "running"):
                    led.set_state(spec["id"], state)
                led.finish(spec["id"], "done", {"route": "local-shell", "checks": [],
                                               "model_requested": None, "model_served": None,
                                               "ended": NOW, "detail": "local step finished"})
            finally:
                led.close()
            dest = Path(tmp) / "s.html"
            result = subprocess.run([sys.executable, "-B", "-I", str(ROOT / "bin/rh"),
                                     "status", "--html", str(dest), "--json"],
                                    env={**os.environ, "RH_HOME": str(home)}, cwd=ROOT,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            view = json.loads(result.stdout)
            self.assertEqual(view["recent"][0]["id"], spec["id"])
            self.assertEqual(view["recent"][0]["state"], "done")
            text = " ".join(Page(dest.read_text()).text)
            self.assertIn(spec["id"], text)
            self.assertIn("done", text)
            self.assertIn("local step finished", text)
            self.assertIn("done 1", text)
            self.assertFalse(dest.with_suffix(".html.tmp").exists())


if __name__ == "__main__":
    unittest.main()
