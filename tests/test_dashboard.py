"""The app presents the existing status view and one job's record without trusting worker text."""
import copy
import json
import os
import re
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
# The question a person answers in the fixture: what to decide, what to check first, and the choices.
ASK = "approve the literature reviews before they go to the founder"
CHECK = ("Open runs/live-job/reviews.md: five reviews, one per paper. Check that each states the claim, the method "
         "and the evidence, and that every DOI resolves. Approve to send them on, or name what to fix.")


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
        "inbox": [{"id": 7, "kind": "decision", "job": "live-job", "action": ASK,
                   "detail": CHECK, "created": NOW - 31, "resolved": None, "resolution": None}],
        "last_24h": {"done": 2, "failed": 1},
        "recent": [{"id": "finished-job", "state": "done", "paused": False,
                    "detail": "accepted output", "external_id": None}],
    }


def contrast(a, b):
    """WCAG 2 contrast ratio of two #rrggbb colours."""
    def luminance(colour):
        rgb = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


TOKEN = "serve-token-1"
ROUTES = ("/inbox/resolve", "/stop", "/resume", "/attach", "/harvest", "/land", "/tick", "/job", "/job/cancel")


def live_view():
    """full_view() plus an object for every action: a handoff job, a pause the app holds and a harvest item."""
    view = full_view()
    view["live"].append({"id": "web-job", "state": "handoff", "paused": True,
                         "detail": "launch in the web composer", "external_id": None})
    view["stops"].append({"id": 2, "scope": "job:web-job", "kind": "pause", "owner": "app", "reason": "hold",
                          "created": NOW - 3, "lifted": None, "lifted_by": None})
    view["inbox"].append({"id": 8, "kind": "harvest", "job": "web-job", "action": "save the staged diff",
                          "detail": "", "created": NOW - 2, "resolved": None, "resolution": None})
    return {**view, "can_add": True}


def opened(view, jid, **job):
    """The view with one job open, as serve builds it from `rh job show` and the job's out.log."""
    row = next(r for section in ("live", "not_started", "recent") for r in view.get(section, []) if r["id"] == jid)
    spec = {"id": jid, "kind": "codex-cloud", "brief": "Write the notes.\nCite each source.", "needs": ["net"],
            "write_root": f"runs/{jid}/", "timeout_min": 120, "accept": "notes.md exists"}
    record = {"id": jid, "kind": "codex-cloud", "state": row["state"], "spec": spec, "detail": row["detail"],
              "external_id": row["external_id"], "created": NOW - 100, "started": NOW - 90, "updated": NOW - 10}
    record.update(job)
    events = [{"seq": 1, "ts": NOW - 100, "job": jid, "kind": "job-added", "data": {"kind": "codex-cloud"}},
              {"seq": 2, "ts": NOW - 90, "job": jid, "kind": "state",
               "data": {"from": "launching", "to": "running", "detail": "worker output"}},
              {"seq": 3, "ts": NOW - 31, "job": jid, "kind": "inbox", "data": {"kind": "decision",
                                                                                "action": ASK}},
              {"seq": 4, "ts": NOW - 5, "job": jid, "kind": "mystery-thing", "data": {"raw": "zz"}}]
    return {**view, "open": {"job": record, "receipt": None, "events": events, "log": None}}


class Forms(HTMLParser):
    """Each form's action, method and fields (name -> value), as a browser would submit them."""
    def __init__(self, source):
        super().__init__()
        self.forms, self.current = [], None
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {"action": attrs.get("action"), "method": attrs.get("method"), "fields": {}}
            self.forms.append(self.current)
        elif tag in ("input", "textarea") and self.current is not None and attrs.get("name"):
            if attrs.get("type") in ("radio", "checkbox") and attrs["name"] in self.current["fields"]:
                if "checked" not in attrs:
                    return
            self.current["fields"][attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


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


def text(page):
    return "".join(Page(page).text)


def item(page, jid):
    """A job's entry in the sidebar."""
    return re.search(rf'<li class="row" data-key="job:{re.escape(jid)}".*?</li>', page, re.S).group(0)


def group(page, jid):
    """The sidebar groups that list a job."""
    return [m.group(1) for m in re.finditer(r'<section class="s-g-(\w+) group".*?</section>', page, re.S)
            if f'data-key="job:{jid}"' in m.group(0)]


def pane(page):
    return page.split('<main class="pane"', 1)[1]


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
        self.assertIn('<script>alert(1)</script> & "quoted"', text(page))
        # Positive control: the parser detects an actual script in unsafe HTML.
        self.assertIn("script", [tag for tag, _ in Page("<script>alert(1)</script>").tags])

    def test_sidebar_groups_and_empty_states(self):
        # Omitting, reordering or hiding an empty group, or an empty state that teaches nothing, must fail.
        headings = ["needs you", "running", "queued", "done", "needs you", "today"]
        self.assertEqual(Page(dashboard.render(full_view(), NOW)).headings, headings)
        empty = {"supervisor": None, "stops": [], "routes": [], "live": [], "not_started": [],
                 "inbox": [], "last_24h": {}, "recent": []}
        for view in ({}, empty):
            with self.subTest(view=view):
                page = Page(dashboard.render(view, NOW))
                self.assertEqual(page.headings, headings)
                shown = " ".join(page.text)
                for sentence in ("Nothing needs you", "Nothing running", "Nothing queued", "Nothing finished yet",
                                 "Nothing needs you.", "The harness has not run yet.", "Harness not running",
                                 "Last 24 h: nothing finished", "No platform problems"):
                    self.assertIn(sentence, shown)
        # The live app with no jobs at all walks a first-time user through the three steps.
        first = " ".join(Page(dashboard.render(empty, NOW, token=TOKEN)).text)
        for sentence in ("Nothing here yet.", "Let the harness check", "Start a job", "rh job add",
                         "Answer what it asks"):
            self.assertIn(sentence, first)
        # A board with nothing waiting still says when the harness last checked.
        view = {**empty, "supervisor": {"pid": 9, "age_s": 7}}
        self.assertIn("The harness checked 7 s ago.", text(dashboard.render(view, NOW)))

    def test_paused_marker(self):
        # A missing or unconditional pause line must fail for a job in every section.
        for section in ("live", "not_started", "recent"):
            view = full_view()
            view["stops"] = []
            jid = view[section][0]["id"]
            with self.subTest(section=section):
                view[section][0]["paused"] = True
                page = dashboard.render(view, NOW)
                self.assertIn("Paused", text(page))
                if section != "live":  # live-job's line is its question; its card carries the pause
                    self.assertIn("Paused", item(page, jid))
                view[section][0]["paused"] = False
                self.assertNotIn("Paused", text(dashboard.render(view, NOW)))

    def test_self_contained_document(self):
        # External resources, scripts, links or missing mobile/theme styling in the snapshot must fail.
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
        for tag in ("html", "head", "title", "style", "body", "nav", "main"):
            self.assertIn(tag, tags)
        for tag in ("script", "link", "img", "iframe", "object", "embed", "video", "audio", "font"):
            self.assertNotIn(tag, tags)
        self.assertEqual(tags.count("section"), 6)
        self.assertTrue(any(tag == "meta" and attrs.get("name") == "viewport"
                            and "width=device-width" in attrs.get("content", "")
                            for tag, attrs in parsed.tags))
        for _, attrs in parsed.tags:
            self.assertFalse(any(k.startswith("on") or k in ("src", "href") for k in attrs))

    def test_text_contrast_in_both_schemes(self):
        # A text or pill colour under 4.5:1 on its surface, or a missing dark override, must fail.
        style = re.search(r"<style>(.*)</style>", dashboard.render(full_view(), NOW), re.S).group(1)
        light_css, dark_css = style.split("@media (prefers-color-scheme: dark)", 1)
        light = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-f]{6});", light_css))
        dark = {**light, **dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-f]{6});", dark_css))}
        pairs = [(fg, bg) for fg in ("ink", "muted") for bg in ("frame", "hover", "sheet", "raised", "warn-bg")]
        pairs += [(f"{tone}-ink", f"{tone}-bg") for tone in ("neutral", "active", "good", "warn", "bad")]
        pairs += [(f"{tone}-ink", bg) for tone in ("active", "good", "warn", "bad") for bg in ("sheet", "raised")]
        pairs += [("warn-ink", "frame"), ("warn-ink", "hover")]  # a question under its job in the sidebar
        pairs += [("on-accent", "accent"), ("sheet", "bad-ink"), ("sheet", "ink")]  # filled buttons
        for scheme, colours in (("light", light), ("dark", dark)):
            for fg, bg in pairs:
                with self.subTest(scheme=scheme, pair=(fg, bg)):
                    self.assertGreaterEqual(contrast(colours[fg], colours[bg]), 4.5)
        self.assertNotEqual(light["frame"], dark["frame"])
        # Positive control: #777 on white is just under the floor.
        self.assertLess(contrast("#777777", "#ffffff"), 4.5)

    def test_all_text_fields_are_present_and_escaped(self):
        # Any unescaped or silently omitted visible string field must fail.
        # A known state, kind or scope is shown in plain words; an unknown one is shown as the harness gave it.
        fields = [(section, key) for section in ("live", "not_started", "recent") for key in ("id", "state", "detail")]
        fields += [("live", "external_id")]  # a job's cloud task shows on its card; the sidebar keeps to one line
        fields += [("stops", key) for key in ("scope", "owner", "reason")]
        fields += [("routes", key) for key in ("route", "last_status")]
        fields += [("inbox", key) for key in ("job", "action", "detail")]
        value = '<b title="worker">O\'Brien & output</b>'
        escaped = '&lt;b title=&quot;worker&quot;&gt;O&#x27;Brien &amp; output&lt;/b&gt;'
        for section, key in fields:
            with self.subTest(section=section, key=key):
                view = full_view()
                view[section][0][key] = value
                page = dashboard.render(view, NOW)
                self.assertIn(escaped, page)
                self.assertIn(value, text(page))
                self.assertNotIn(value, page)
        view = full_view()
        view["last_24h"] = {value: 8}
        self.assertIn(escaped, dashboard.render(view, NOW))

    def test_status_content_and_relative_times(self):
        # Dropping a row field, count or relative timestamp must fail.
        view = full_view()
        shown = text(dashboard.render(view, NOW))
        for value in ("Process 123", "checked 12 s ago", "asked 31 s ago", "the last 5 s ago", "retrying in 40 s",
                      "Paused by lead:", "review output", "Codex cloud", "rate-limited", "3 failures",
                      "live-job", "Running", "worker output", "Cloud task worker-123", "queued-job", "On hold",
                      "waiting for input", ASK.capitalize(), CHECK, "finished-job", "accepted output",
                      "Last 24 h: 2 done, 1 failed"):
            with self.subTest(value=value):
                self.assertIn(value, shown)
        later = text(dashboard.render(view, NOW + 10))
        for value in ("asked 41 s ago", "the last 15 s ago", "retrying in 30 s"):
            self.assertIn(value, later)
        view["routes"][0]["next_try"] = NOW - 8
        self.assertIn("retrying on the next check", " ".join(Page(dashboard.render(view, NOW)).text))

    def test_live_pages_load_only_app_js(self):
        # A second or inline script, an inline handler, an external URL or controls leaking into the snapshot
        # must fail; the live app runs under script-src 'self'.
        for page in (dashboard.render(live_view(), NOW, token=TOKEN),
                     dashboard.render(opened(live_view(), "live-job"), NOW, token=TOKEN),
                     dashboard.render({**live_view(), "new": True}, NOW, token=TOKEN)):
            parsed = Page(page)
            self.assertEqual(page.lower().count("<script"), 1)
            self.assertEqual([attrs for tag, attrs in parsed.tags if tag == "script"], [{"src": "/app.js", "defer": None}])
            self.assertIn('<noscript><meta http-equiv="refresh" content="30"></noscript>', page)
            self.assertNotIn("http://", page.lower())
            self.assertNotIn("https://", page.lower())
            self.assertLess(len(page.encode()), 300_000)
            for _, attrs in parsed.tags:
                self.assertFalse(any(k.startswith("on") for k in attrs))
                self.assertFalse(any((v or "").strip().lower().startswith("javascript:") for v in attrs.values()))
        static = {tag for tag, _ in Page(dashboard.render(opened(live_view(), "live-job"), NOW, result="x")).tags}
        self.assertFalse(static & {"script", "noscript", "form", "input", "button", "textarea", "dialog", "a"})

    def test_live_forms_cover_every_route(self):
        # A missing route, a form without the token, a form that loses its way back, or an action posting the
        # wrong fields must fail.
        view = live_view()
        pages = {"home": dashboard.render(view, NOW, token=TOKEN)}
        for jid in ("live-job", "queued-job", "finished-job", "web-job"):
            pages[jid] = dashboard.render(opened(view, jid), NOW, token=TOKEN)
        forms = []
        for name, page in pages.items():
            for form in Forms(page).forms:
                if form["action"]:
                    forms.append({**form, "page": name})
        self.assertEqual({f["action"] for f in forms}, set(ROUTES))
        for form in forms:
            with self.subTest(form=form):
                self.assertEqual(form["method"], "post")
                self.assertEqual(form["fields"].get("t"), TOKEN)
                if form["page"] != "home" and form["action"] != "/job":
                    self.assertEqual(form["fields"].get("next"), f"/job/{form['page']}")
                elif form["page"] == "home":
                    self.assertNotIn("next", form["fields"])

        def one(action, page="home", **fields):
            return [f for f in forms if f["action"] == action and f["page"] == page
                    and all(f["fields"].get(k) == v for k, v in fields.items())]
        self.assertEqual(set(one("/inbox/resolve", id="7")[0]["fields"]), {"t", "id", "note"})
        self.assertEqual(set(one("/harvest", job="web-job")[0]["fields"]), {"t", "job", "text"})
        self.assertEqual(set(one("/attach", job="web-job")[0]["fields"]), {"t", "job", "task_id"})
        self.assertEqual(set(one("/stop", route="codex-cloud")[0]["fields"]), {"t", "route", "reason", "pause"})
        self.assertEqual(one("/resume", job="web-job", pause="on")[0]["fields"].keys(), {"t", "job", "pause"})
        self.assertEqual(one("/tick")[0]["fields"].keys(), {"t"})
        self.assertEqual(set(one("/job")[0]["fields"]),
                         {"t", "brief", "id", "kind", "command", "accept", "needs", "timeout_min"})
        self.assertEqual(set(one("/stop", "live-job", job="live-job")[0]["fields"]),
                         {"t", "job", "reason", "pause", "next"})
        self.assertEqual(set(one("/land", "finished-job", job="finished-job")[0]["fields"]), {"t", "job", "next"})
        self.assertEqual(set(one("/job/cancel", "queued-job", job="queued-job")[0]["fields"]),
                         {"t", "job", "reason", "next"})
        self.assertEqual(set(one("/inbox/resolve", "live-job", id="7")[0]["fields"]), {"t", "id", "note", "next"})
        # Only the app's own stops get a Resume: the lead's pause on live-job cannot be lifted from here.
        self.assertFalse([f for f in forms if f["action"] == "/resume" and f["fields"].get("job") == "live-job"])
        self.assertFalse([f for f in forms if f["action"] == "/attach" and f["fields"].get("job") == "live-job"])
        # Cancel is offered only before a job starts, and a finished job cannot be paused.
        self.assertFalse(one("/job/cancel", "live-job"))
        self.assertFalse(one("/stop", "finished-job", job="finished-job"))

    def test_composer_only_where_the_server_can_add_jobs(self):
        # A New job form the server cannot take, a missing one where it can, or a refused draft that comes back
        # empty or unescaped must fail.
        view = live_view()
        actions = lambda page: {f["action"] for f in Forms(page).forms}  # noqa: E731
        self.assertIn("/job", actions(dashboard.render(view, NOW, token=TOKEN)))
        self.assertIn('href="/new?t=serve-token-1"', dashboard.render(view, NOW, token=TOKEN))
        without = {**view, "can_add": False}
        for page in (dashboard.render(without, NOW, token=TOKEN), dashboard.render({**without, "new": True}, NOW,
                                                                                   token=TOKEN)):
            self.assertNotIn("/job", actions(page))
            self.assertNotIn("/new?", page)
        self.assertNotIn("New job", text(dashboard.render(view, NOW)))
        draft = {"id": "lit-scan-03", "kind": "local-shell", "brief": '<b>scan</b> & "sort"', "command": "python3 x.py",
                 "accept": "", "timeout_min": "45", "needs": ["net"]}
        page = dashboard.render({**view, "new": True, "compose": draft}, NOW, token=TOKEN)
        self.assertIn('data-open="new"', page)
        self.assertIn("&lt;b&gt;scan&lt;/b&gt; &amp; &quot;sort&quot;</textarea>", page)
        self.assertIn('value="local-shell" checked', page)
        self.assertIn('value="net" checked', page)
        self.assertIn('value="45"', page)
        self.assertIn('value="python3 x.py"', page)

    def test_job_pane_shows_the_ask_status_output_and_timeline(self):
        # Dropping what the job was asked, its output tail, its result checks or a timeline entry must fail.
        view = opened(live_view(), "finished-job", kind="local-shell")
        view["open"]["job"]["spec"]["command"] = ["python3", "-c", "print('<hi>')"]
        view["open"]["log"] = "line one\n<tail> & end"
        view["open"]["receipt"] = {"route": "local-shell", "checks": [{"cmd": "test -s notes.md", "exit": 0},
                                                                     {"cmd": "grep -q DOI notes.md", "exit": 1}],
                                   "model_requested": None, "model_served": None, "ended": NOW - 10, "detail": "ok"}
        page = dashboard.render(view, NOW, token=TOKEN)
        right = text(pane(page))
        for value in ("finished-job", "Done", "This machine", "finished 10 s ago", "Result", "accepted output",
                      "exit 0", "test -s notes.md", "exit 1", "Output", "<tail> & end", "Asked", "Write the notes.",
                      "Cite each source.", "python3 -c print('<hi>')", "notes.md exists", "runs/finished-job/",
                      "120 min", "Timeline", "Added, to run on Codex cloud", "Running: worker output",
                      f"Asked you: {ASK.capitalize()}31 s ago", "Mystery thing: raw zz5 s ago"):
            with self.subTest(value=value):
                self.assertIn(value, right)
        self.assertIn("&lt;tail&gt; &amp; end", page)
        self.assertIn('data-open="job:finished-job"', page)
        self.assertIn("<title>finished-job · Research harness</title>", page)
        self.assertEqual(len(item(page, "finished-job")) > 0, True)
        # A running job's pane says what happens next; a job the harness does not know says so, not a crash.
        running = text(pane(dashboard.render(opened(live_view(), "queued-job"), NOW, token=TOKEN)))
        self.assertIn("Rechecked on every pass.", running)
        missing = dashboard.render({**live_view(), "open": {"id": "ghost", "job": None}}, NOW, token=TOKEN)
        self.assertIn("No job named ghost", text(missing))
        self.assertIn('data-key="job:live-job"', missing)

    def test_live_page_escapes_worker_text_and_result(self):
        # Unescaped worker text or result, or a serve result escaped twice, must fail.
        value = '<b title="worker">O\'Brien & output</b>'
        escaped = '&lt;b title=&quot;worker&quot;&gt;O&#x27;Brien &amp; output&lt;/b&gt;'
        for section, key in [("live", "detail"), ("live", "id"), ("stops", "scope"), ("routes", "route"),
                             ("inbox", "action"), ("inbox", "job")]:
            with self.subTest(section=section, key=key):
                view = live_view()
                view[section][0][key] = value
                page = dashboard.render(view, NOW, token=TOKEN)
                self.assertIn(escaped, page)
                self.assertNotIn(value, page)
                self.assertIn(value, text(page))
        for where in ("brief", "accept", "log", "event", "check"):
            with self.subTest(where=where):
                view = opened(live_view(), "live-job")
                record = view["open"]
                if where == "log":
                    record["log"] = value
                elif where == "event":
                    record["events"][1]["data"]["detail"] = value
                elif where == "check":
                    record["receipt"] = {"route": "x", "checks": [{"cmd": value, "exit": 0}]}
                else:
                    record["job"]["spec"][where] = value
                page = dashboard.render(view, NOW, token=TOKEN)
                self.assertIn(escaped, page)
                self.assertNotIn(value, page)
                self.assertIn(value, text(page))
        page = dashboard.render(full_view(), NOW, token=TOKEN, result="<script>alert(1)</script>")
        self.assertEqual(page.lower().count("<script"), 1)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        from rh import serve
        page = dashboard.render(full_view(), NOW, token=TOKEN, result=serve._result(1, "rh: no job <b>x</b>"))
        self.assertIn("rh: no job &lt;b&gt;x&lt;/b&gt;", page)
        self.assertNotIn("&amp;lt;", page)
        self.assertIn('data-code="1"', page)
        self.assertIn("rh: no job <b>x</b>", Page(page).text)
        self.assertNotIn('id="result"', dashboard.render(full_view(), NOW, token=TOKEN))

    def test_signatures_ignore_the_clock(self):
        # The live app swaps a row or region only when its signature changes: a ticking time in it, or a state
        # change left out of it, must fail.
        def sigs(view, now):
            return dict(re.findall(r'data-key="([^"]+)"[^>]*? data-sig="(\w+)"', dashboard.render(view, now, token=TOKEN)))
        view = live_view()
        before = sigs(view, NOW)
        self.assertEqual(before, sigs(view, NOW + 30))
        view["not_started"][0]["state"] = "ready"
        view["live"][0]["state"] = "collecting"
        after = sigs(view, NOW)
        self.assertNotEqual(before["job:queued-job"], after["job:queued-job"])
        self.assertNotEqual(before["card:live-job"], after["card:live-job"])
        self.assertNotEqual(before["section:needs"], after["section:needs"])
        self.assertEqual(before["job:finished-job"], after["job:finished-job"])
        self.assertEqual(before["section:g-done"], after["section:g-done"])
        record = opened(live_view(), "live-job")
        first = sigs(record, NOW)
        self.assertIn("ev:2", first)
        self.assertEqual(first, sigs(record, NOW + 90))
        record["open"]["events"].append({"seq": 5, "ts": NOW, "job": "live-job", "kind": "state",
                                         "data": {"from": "running", "to": "collecting", "detail": ""}})
        self.assertNotEqual(first["section:j-timeline"], sigs(record, NOW)["section:j-timeline"])

    def test_script_is_csp_safe_and_matches_the_page(self):
        # A URL, eval or Function in the client, or a hook the script needs missing from the page, must fail.
        for banned in ("http://", "https://", "eval(", "Function("):
            self.assertNotIn(banned, dashboard.SCRIPT)
        view = opened(live_view(), "live-job")
        view["open"]["log"] = "out"
        page = (dashboard.render(live_view(), NOW, token=TOKEN, result="ok")
                + dashboard.render(view, NOW, token=TOKEN))
        for in_script, in_page in (("[data-live]", " data-live "), (".toasts", 'class="toasts"'), ("#keys", 'id="keys"'),
                                   ("[data-keys]", " data-keys "), ("[data-region", " data-region="),
                                   ("dataset.sig", " data-sig="), ("[data-tk", " data-tk="), ("dataset.now", " data-now="),
                                   ("'data-act'", " data-act "), ("[data-k", " data-k="), ("#result", ' id="result"'),
                                   ("dataset.due", " data-due="), (".pane", '<main class="pane"'),
                                   ("dataset.pane", " data-pane="), ("dataset.open", " data-open="),
                                   ("a[data-nav]", " data-nav"), (".side a.item", '<nav class="side"'),
                                   (".g-needs a.item", 'class="s-g-needs group"'), (".log", 'class="log"'),
                                   ("form.compose", 'class="compose"'), (".cards > .row", 'class="rows cards"'),
                                   ("'.head'", 'class="head"'), ("'.body'", 'class="body"')):
            with self.subTest(hook=in_script):
                self.assertIn(in_script, dashboard.SCRIPT)
                self.assertIn(in_page, page)

    def test_jobs_are_listed_once_and_grouped_by_what_they_need(self):
        # A job listed twice, in the wrong group, an ask left off its card, or a card that stays after its ask is
        # settled must fail.
        view = live_view()
        page = dashboard.render(view, NOW, token=TOKEN)
        for jid, where in (("live-job", ["needs"]), ("web-job", ["needs"]), ("queued-job", ["queued"]),
                           ("finished-job", ["done"])):
            with self.subTest(job=jid):
                self.assertEqual(page.count(f'data-key="job:{jid}"'), 1)
                self.assertEqual(group(page, jid), where)
        self.assertIn(ASK.capitalize(), item(page, "live-job"))
        card = re.search(r'<li class="row card" data-key="card:live-job".*?</li>', page, re.S).group(0)
        for part in (ASK.capitalize(), CHECK, "Paused by lead:", "worker output", "/inbox/resolve", "/job/live-job"):
            self.assertIn(part, card)
        # A finished job waiting on a person leaves Done; the empty Done group must not claim nothing finished.
        asked = full_view()
        asked["inbox"].append({**asked["inbox"][0], "id": 8, "job": "finished-job"})
        done = re.search(r'<section class="s-g-done group".*?</section>', dashboard.render(asked, NOW), re.S).group(0)
        self.assertIn("Finished jobs that need you are above", done)
        self.assertNotIn("finished-job", done)
        view = full_view()
        view["inbox"] = []
        page = dashboard.render(view, NOW, token=TOKEN)
        self.assertEqual(group(page, "live-job"), ["running"])
        self.assertNotIn('data-key="card:live-job"', page)
        self.assertIn("Paused by lead", item(page, "live-job"))

    def test_one_filled_button_per_place_and_destructive_actions_marked(self):
        # A card or job page with no filled button or with two, or a pause or cancel styled like a safe action,
        # must fail.
        page = dashboard.render(live_view(), NOW, token=TOKEN)
        cards = re.findall(r'<li class="row card".*?</li>', page, re.S)
        self.assertEqual(len(cards), 2)
        for card in cards:
            openers = re.findall(r'<(?:summary|button) class="([^"]+)" data-k=', card)
            self.assertEqual(sum("primary" in c.split() for c in openers), 1, card)
        for jid in ("live-job", "web-job", "finished-job"):
            with self.subTest(job=jid):
                right = pane(dashboard.render(opened(live_view(), jid), NOW, token=TOKEN))
                shown = re.sub(r'<form class="pop.*?</form>', "", right, flags=re.S)  # a popover confirms itself
                self.assertEqual(len(re.findall(r'class="btn primary"', shown)), 1)
        pages = page + "".join(dashboard.render(opened(live_view(), jid), NOW, token=TOKEN)
                               for jid in ("live-job", "queued-job"))
        for opener in re.findall(r'<summary class="([^"]+)" data-k="[sc]"', pages):
            self.assertIn("danger", opener.split())
        for form in re.findall(r'<form[^>]*action="/(?:stop|job/cancel)".*?</form>', pages, re.S):
            self.assertIn('class="btn danger fill"', form)

    def test_plain_words(self):
        # The jargon the founder could not read coming back into the app's text must fail; the glossary and
        # the snapshot's printed commands may still name the CLI.
        pages = (dashboard.render(live_view(), NOW, token=TOKEN), dashboard.render(full_view(), NOW),
                 dashboard.render(opened(live_view(), "live-job"), NOW, token=TOKEN))
        for page in pages:
            shown = re.sub(r"<(dialog|code|style)\b.*?</\1>", "", page, flags=re.S)
            self.assertEqual(re.findall(r"\b(Run tick|Land|Stops|Routes|Supervisor|Inbox|Resolve|pid|held|"
                                        r"route|PAUSED)\b", " ".join(Page(shown).text)), [])
        words = " ".join(Page(dashboard.render(full_view(), NOW, token=TOKEN)).text)
        for old in ("Run tick", "Land", "Stops", "Supervisor"):
            self.assertIn(old, words)  # the glossary keeps the old names for CLI users

    def test_render_is_deterministic_and_does_not_mutate_view(self):
        # Mutating the source ledger view or consulting wall-clock time must fail.
        for view in (full_view(), opened(live_view(), "live-job")):
            before = copy.deepcopy(view)
            self.assertEqual(dashboard.render(view, NOW, token=TOKEN), dashboard.render(view, NOW, token=TOKEN))
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
            shown = " ".join(Page(dest.read_text()).text)
            self.assertIn(spec["id"], shown)
            self.assertIn("Done", shown)
            self.assertIn("local step finished", shown)
            self.assertIn("Last 24 h: 1 done", shown)
            self.assertFalse(dest.with_suffix(".html.tmp").exists())


if __name__ == "__main__":
    unittest.main()
