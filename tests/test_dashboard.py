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
SETUP = "create and publish the cloud environment (rh env spec), then rh config set cloud.env_id <ID>"


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
ROUTES = ("/inbox/resolve", "/stop", "/resume", "/attach", "/harvest", "/land", "/tick", "/job", "/job/cancel",
          "/checks", "/again", "/setup")


def live_view():
    """full_view() plus an object for every action: a handoff job, a pause the app holds, a harvest item, a failed
    job, and the app's own check loop."""
    view = full_view()
    view["live"].append({"id": "web-job", "state": "handoff", "paused": True,
                         "detail": "launch in the web composer", "external_id": None})
    view["stops"].append({"id": 2, "scope": "job:web-job", "kind": "pause", "owner": "app", "reason": "hold",
                          "created": NOW - 3, "lifted": None, "lifted_by": None})
    view["inbox"].append({"id": 8, "kind": "harvest", "job": "web-job", "action": "save the staged diff",
                          "detail": "", "created": NOW - 2, "resolved": None, "resolution": None})
    view["recent"].append({"id": "broke-job", "state": "failed", "paused": False, "detail": "command exited 2",
                           "external_id": "4242"})
    return {**view, "can_add": True, "checks": {"mode": "on", "pid": None, "failed": None}}


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
    """A job's entry in the job list."""
    return re.search(rf'<li class="row" data-key="job:{re.escape(jid)}".*?</li>', page, re.S).group(0)


def group(page, jid):
    """The job-list groups that list a job."""
    return [m.group(1) for m in re.finditer(r'<section class="s-g-(\w+) group".*?</section>', page, re.S)
            if f'data-key="job:{jid}"' in m.group(0)]


def shown_groups(page):
    """The job-list groups a browser shows: an empty group is rendered hidden, so it can fill in without a reload."""
    return [m.group(1) for m in re.finditer(r'<section class="s-g-(\w+) group"[^>]*?data-sig="\w+"( hidden)?>', page)
            if not m.group(2)]


def pane(page):
    return page.split('<main class="pane"', 1)[1]


def primaries(html):
    """The filled buttons a person sees, leaving out the confirm button inside a popover, which confirms itself."""
    shown = re.sub(r'<form class="pop.*?</form>', "", html, flags=re.S)
    return re.findall(r'class="btn primary[ "]', shown)


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

    def test_job_list_groups_and_empty_states(self):
        # Omitting or reordering a group, showing an empty one, or an empty page that teaches nothing must fail.
        headings = ["needs you", "running", "queued", "done", "needs you", "today"]
        page = dashboard.render(full_view(), NOW)
        self.assertEqual(Page(page).headings, headings)
        self.assertEqual(shown_groups(page), ["needs", "done"])
        empty = {"supervisor": None, "stops": [], "routes": [], "live": [], "not_started": [],
                 "inbox": [], "last_24h": {}, "recent": []}
        for view in ({}, empty):
            with self.subTest(view=view):
                page = dashboard.render(view, NOW)
                self.assertEqual(Page(page).headings, headings)
                self.assertEqual(shown_groups(page), [])
                shown = " ".join(Page(page).text)
                for sentence in ("No jobs yet.", "Nothing needs you.", "The harness has not run yet.",
                                 "Last 24 h: nothing finished"):
                    self.assertIn(sentence, shown)
        self.assertIn(" hidden>No jobs yet.", dashboard.render(full_view(), NOW))
        # The live app with no jobs at all opens on the composer and walks a first-time user through three steps.
        first = " ".join(Page(dashboard.render({**empty, "can_add": True}, NOW, token=TOKEN)).text)
        for sentence in ("What should run?", "Describe a job and start it.", "Watch it here.", "Answer when it asks."):
            self.assertIn(sentence, first)
        busy = " ".join(Page(dashboard.render(live_view(), NOW, token=TOKEN)).text)
        self.assertNotIn("Describe a job and start it.", busy)
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
                if section != "live":  # live-job's line is its question
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
        self.assertIn("max-width: 719px", page)
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
        # A text colour under 4.5:1 on a surface it is drawn on, or a missing dark override, must fail.
        style = re.search(r"<style>(.*)</style>", dashboard.render(full_view(), NOW), re.S).group(1)
        light_css, dark_css = style.split("@media (prefers-color-scheme: dark)", 1)
        light = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-f]{6});", light_css))
        dark = {**light, **dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-f]{6});", dark_css))}
        surfaces = ("ground", "hover", "surface", "raised")
        pairs = [(fg, bg) for fg in ("ink", "muted") for bg in surfaces]
        pairs += [(tone, bg) for tone in ("live", "good", "warn", "bad") for bg in surfaces]
        pairs += [("bad", "bad-bg"), ("ink", "bad-bg"), ("live", "live-bg")]  # a failed action; a moved row
        pairs += [("on-primary", "primary"), ("on-bad", "bad")]  # filled buttons
        for scheme, colours in (("light", light), ("dark", dark)):
            for fg, bg in pairs:
                with self.subTest(scheme=scheme, pair=(fg, bg)):
                    self.assertGreaterEqual(contrast(colours[fg], colours[bg]), 4.5)
        self.assertNotEqual(light["surface"], dark["surface"])
        # Positive control: #777 on white is just under the floor.
        self.assertLess(contrast("#777777", "#ffffff"), 4.5)

    def test_all_text_fields_are_present_and_escaped(self):
        # Any unescaped or silently omitted visible string field must fail.
        # A known state, kind or scope is shown in plain words; an unknown one is shown as the harness gave it.
        fields = [(section, key) for section in ("live", "not_started", "recent") for key in ("id", "state", "detail")]
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
        for value in ("Process 123", "Harness checked 12 s ago", "asked 31 s ago", "the last 5 s ago",
                      "retrying in 40 s", "Paused by lead:", "review output", "Codex cloud", "rate-limited",
                      "3 failures", "live-job", "Running", "Worker output", "queued-job", "On hold: Waiting for input",
                      ASK.capitalize(), CHECK, "finished-job", "Accepted output", "Last 24 h: 2 done, 1 failed"):
            with self.subTest(value=value):
                self.assertIn(value, shown)
        later = text(dashboard.render(view, NOW + 10))
        for value in ("asked 41 s ago", "the last 15 s ago", "retrying in 30 s"):
            self.assertIn(value, later)
        view["routes"][0]["next_try"] = NOW - 8
        self.assertIn("retrying on the next check", " ".join(Page(dashboard.render(view, NOW)).text))

    def test_status_line_says_who_checks_the_harness(self):
        # A status line that claims the app checks when it is paused or when rh supervise does, or that offers
        # Pause when there is nothing of the app's to pause, must fail.
        def foot(checks, **extra):
            view = {**live_view(), "checks": checks, **extra}
            page = dashboard.render(view, NOW, token=TOKEN)
            part = page.split('<footer class="checks">', 1)[1].split('<div class="tools">', 1)[0]
            return " ".join(Page(part).text), {f["fields"].get("on") for f in Forms(part).forms
                                               if f["action"] == "/checks"}
        said, switches = foot({"mode": "on", "pid": None, "failed": None})
        self.assertIn("Checking automatically", said)
        self.assertIn("Last check 12 s ago", said)
        self.assertEqual(switches, {"0"})
        said, switches = foot({"mode": "paused", "pid": None, "failed": None})
        self.assertIn("Checks paused", said)
        self.assertIn("Check once", said)
        self.assertNotIn("Checking automatically", said)
        self.assertEqual(switches, {"1"})
        said, switches = foot({"mode": "other", "pid": "4242", "failed": None})
        self.assertIn("Checked by rh supervise (process 4242)", said)
        self.assertEqual(switches, set())
        said, _ = foot({"mode": "on", "pid": None, "failed": "OSError: disk full"})
        self.assertIn("The last check failed", said)
        self.assertIn("OSError: disk full", said)
        said, _ = foot({"mode": "on", "pid": None, "failed": None}, supervisor={"pid": 5, "age_s": 900})
        self.assertIn("Checking, but not lately", said)
        # The page polls fast only while the app's loop is moving a job here.
        fast = dashboard.render({**live_view(), "live": [{"id": "here", "state": "running", "paused": False,
                                                           "detail": "pid 9", "external_id": "9"}],
                                 "meta": {"here": {"kind": "local-shell"}}}, NOW, token=TOKEN)
        self.assertIn('data-pace="fast"', fast)
        calm = dashboard.render({**live_view(), "checks": {"mode": "paused"}, "live": [
            {"id": "here", "state": "running", "paused": False, "detail": "", "external_id": "9"}],
            "meta": {"here": {"kind": "local-shell"}}}, NOW, token=TOKEN)
        self.assertIn('data-pace="calm"', calm)

    def test_live_pages_load_only_app_js(self):
        # A second or inline script, an inline handler, an external URL other than the one Codex link a hand
        # launch needs, or controls leaking into the snapshot must fail; the live app runs under script-src 'self'.
        launch = dashboard.render(opened(live_view(), "web-job"), NOW, token=TOKEN)
        for page in (dashboard.render(live_view(), NOW, token=TOKEN),
                     dashboard.render(opened(live_view(), "live-job"), NOW, token=TOKEN),
                     dashboard.render({**live_view(), "new": True}, NOW, token=TOKEN), launch):
            parsed = Page(page)
            self.assertEqual(page.lower().count("<script"), 1)
            self.assertEqual([attrs for tag, attrs in parsed.tags if tag == "script"], [{"src": "/app.js", "defer": None}])
            self.assertIn('<noscript><meta http-equiv="refresh" content="30"></noscript>', page)
            self.assertNotIn("http://", page.lower())
            self.assertLess(len(page.encode()), 300_000)
            for _, attrs in parsed.tags:
                self.assertFalse(any(k.startswith("on") for k in attrs))
                self.assertFalse(any((v or "").strip().lower().startswith("javascript:") for v in attrs.values()))
            outside = [attrs for tag, attrs in parsed.tags
                       if any("https://" in (attrs.get(k) or "") for k in ("href", "src", "action", "formaction"))]
            if page is launch:
                self.assertEqual(outside, [{"class": "btn", "href": "https://chatgpt.com/codex", "target": "_blank",
                                            "rel": "noopener noreferrer"}])
            else:
                self.assertEqual(outside, [])
        static = {tag for tag, _ in Page(dashboard.render(opened(live_view(), "web-job"), NOW, result="x")).tags}
        self.assertFalse(static & {"script", "noscript", "form", "input", "button", "textarea", "dialog", "a"})

    def test_live_forms_cover_every_route(self):
        # A missing route, a form without the token, a form that loses its way back, or an action posting the
        # wrong fields must fail.
        view = live_view()
        pages = {"home": dashboard.render(view, NOW, token=TOKEN)}
        for jid in ("live-job", "queued-job", "finished-job", "web-job"):
            pages[jid] = dashboard.render(opened(view, jid), NOW, token=TOKEN)
        cloudy = {**view, "inbox": view["inbox"] + [{"id": 9, "kind": "operator-step", "job": None, "action": SETUP,
                                                    "detail": "", "created": NOW - 9}],
                  "meta": {"queued-job": {"kind": "codex-cloud", "brief": "Survey."}}}
        pages["queued-job/setup"] = dashboard.render(opened(cloudy, "queued-job"), NOW, token=TOKEN)
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
                    self.assertEqual(form["fields"].get("next"), f"/job/{form['page'].split('/')[0]}")
                elif form["page"] == "home":
                    self.assertNotIn("next", form["fields"])

        def one(action, page="home", **fields):
            return [f for f in forms if f["action"] == action and f["page"] == page
                    and all(f["fields"].get(k) == v for k, v in fields.items())]
        self.assertEqual(set(one("/inbox/resolve", id="7")[0]["fields"]), {"t", "id", "note"})
        self.assertEqual(set(one("/stop", route="codex-cloud")[0]["fields"]), {"t", "route", "reason", "pause"})
        self.assertEqual(one("/tick")[0]["fields"].keys(), {"t"})
        self.assertEqual(one("/checks")[0]["fields"], {"t": TOKEN, "on": "0"})
        self.assertEqual(one("/again", job="broke-job")[0]["fields"], {"t": TOKEN, "job": "broke-job", "changed": ""})
        self.assertEqual(set(one("/job")[0]["fields"]),
                         {"t", "brief", "id", "kind", "command", "accept", "needs", "timeout_min"})
        self.assertEqual(set(one("/harvest", "web-job", job="web-job")[0]["fields"]), {"t", "job", "text", "next"})
        self.assertEqual(set(one("/attach", "web-job", job="web-job")[0]["fields"]), {"t", "job", "task_id", "next"})
        self.assertEqual(one("/resume", "web-job", job="web-job", pause="on")[0]["fields"].keys(),
                         {"t", "job", "pause", "next"})
        self.assertEqual(set(one("/stop", "live-job", job="live-job")[0]["fields"]),
                         {"t", "job", "reason", "pause", "next"})
        self.assertEqual(set(one("/land", "finished-job", job="finished-job")[0]["fields"]), {"t", "job", "next"})
        self.assertEqual(set(one("/again", "finished-job", job="finished-job")[0]["fields"]),
                         {"t", "job", "changed", "next"})
        self.assertEqual(set(one("/job/cancel", "queued-job", job="queued-job")[0]["fields"]),
                         {"t", "job", "reason", "next"})
        self.assertEqual(set(one("/inbox/resolve", "live-job", id="7")[0]["fields"]), {"t", "id", "note", "next"})
        self.assertEqual(set(one("/setup", "queued-job/setup")[0]["fields"]), {"t", "env_id", "next"})
        # Only the app's own stops get a Resume: the lead's pause on live-job cannot be lifted from here.
        self.assertFalse([f for f in forms if f["action"] == "/resume" and f["fields"].get("job") == "live-job"])
        self.assertFalse([f for f in forms if f["action"] == "/attach" and f["fields"].get("job") == "live-job"])
        # Cancel is offered only before a job starts, a finished job cannot be paused, and a running one cannot be
        # run again.
        self.assertFalse(one("/job/cancel", "live-job"))
        self.assertFalse(one("/stop", "finished-job", job="finished-job"))
        self.assertFalse(one("/again", "live-job"))
        # Codex cloud is set up once it is ready; the waiting job's page then asks for nothing.
        ready = dashboard.render(opened({**cloudy, "cloud_ready": True}, "queued-job"), NOW, token=TOKEN)
        self.assertNotIn('action="/setup"', ready)

    def test_cloud_setup_is_one_tap_from_the_job_list(self):
        # The list's setup row pointing back at home (a dead tap on a phone, where home is the list), or the home
        # copy losing the mark that lets a phone drop it, must fail; with no waiting cloud job to open, home keeps it.
        view = live_view()
        setup = {"id": 9, "kind": "operator-step", "job": None, "action": SETUP, "detail": "", "created": NOW - 9}
        cloudy = {**view, "inbox": view["inbox"] + [setup], "meta": {"queued-job": {"kind": "codex-cloud"}}}
        page = dashboard.render(cloudy, NOW, token=TOKEN)
        self.assertRegex(page, r'data-key="loose"[^>]*><a class="item" href="/job/queued-job\?t=')
        self.assertRegex(page, r'data-key="inbox:9"[^>]*data-job="queued-job"')
        alone = dashboard.render({**view, "inbox": view["inbox"] + [setup]}, NOW, token=TOKEN)
        self.assertRegex(alone, r'data-key="loose"[^>]*><a class="item" href="/\?t=')
        self.assertNotRegex(alone, r'data-key="inbox:9"[^>]*data-job=')

    def test_composer_only_where_the_server_can_add_jobs(self):
        # A New job form the server cannot take, a missing one where it can, a refused draft that comes back
        # empty or unescaped, or a default platform that cannot run must fail.
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
        # Codex cloud is the default only once it is set up; before that a new job runs on this machine.
        def kind(page):
            return Forms(page).forms[[f["action"] for f in Forms(page).forms].index("/job")]["fields"]["kind"]
        self.assertEqual(kind(dashboard.render(view, NOW, token=TOKEN)), "local-shell")
        self.assertIn("set up first", text(dashboard.render(view, NOW, token=TOKEN)))
        self.assertEqual(kind(dashboard.render({**view, "cloud_ready": True}, NOW, token=TOKEN)), "codex-cloud")

    def test_job_page_is_a_transcript_that_ends_in_its_receipt(self):
        # Dropping what the job was asked, its output tail, its checks, the files it made or a line of what happened
        # must fail.
        view = opened(live_view(), "finished-job", kind="local-shell")
        spec = view["open"]["job"]["spec"]
        spec.update(kind="local-shell", command=["sh", "-c", "date > <today>.txt"])
        view["open"]["log"] = "line one\n<tail> & end"
        # The harness records the command space-joined; the page shows it as it can be pasted back into a shell.
        view["open"]["receipt"] = {"route": "local-shell", "checks": [{"cmd": "sh -c date > <today>.txt", "exit": 0},
                                                                     {"cmd": "test -s notes.md", "exit": 0},
                                                                     {"cmd": "grep -q DOI notes.md", "exit": 1}],
                                   "model_requested": None, "model_served": None, "ended": NOW - 10, "detail": "ok"}
        view["open"]["files"] = [{"path": "notes.md", "size": 2048}, {"path": "sub dir/a&b.txt", "size": 9}]
        view["open"]["peek"] = {"path": "notes.md", "text": "# Notes <draft>", "more": True}
        page = dashboard.render(view, NOW, token=TOKEN)
        right = text(pane(page))
        for value in ("Write the notes", "Done", "finished-job", "This machine", "finished 10 s ago", "Done in 1 min 20 s.",
                      "exit 0", "test -s notes.md", "exit 1", "Output", "<tail> & end", "Write the notes.",
                      "Cite each source.", "notes.md exists", "runs/finished-job/",
                      "120 min", "What happened", "Added to run in Codex cloud", "Worker output",
                      f"Asked you: {ASK.capitalize()}31 s ago", "Mystery thing: raw zz5 s ago",
                      "Files in runs/finished-job/", "2.0 KB", "sub dir/a&b.txt", "# Notes <draft>"):
            with self.subTest(value=value):
                self.assertIn(value, right)
        self.assertIn("&lt;tail&gt; &amp; end", page)
        # Once in Details and once as the first check, quoted as typed; the space-joined form is never shown.
        self.assertEqual(right.count("sh -c 'date > <today>.txt'"), 2)
        self.assertNotIn("sh -c date", right)
        self.assertIn("# Notes &lt;draft&gt;\n…</pre>", page)
        self.assertIn('href="/job/finished-job/file?p=sub%20dir/a%26b.txt&amp;t=serve-token-1"', page)
        self.assertIn('data-open="job:finished-job"', page)
        self.assertIn("<title>Write the notes · Research harness</title>", page)
        self.assertLess(right.index("What happened"), right.index("Output"))
        self.assertLess(right.index("Output"), right.index("Done in"))
        self.assertTrue(item(page, "finished-job"))
        # A held job's page says what to do; a job the harness does not know says so, not a crash.
        held = text(pane(dashboard.render(opened(live_view(), "queued-job"), NOW, token=TOKEN)))
        self.assertIn("On hold: Waiting for input", held)
        self.assertIn("It is rechecked on every pass.", held)
        missing = dashboard.render({**live_view(), "open": {"id": "ghost", "job": None}}, NOW, token=TOKEN)
        self.assertIn("No job named ghost", text(missing))
        self.assertIn('data-key="job:live-job"', missing)
        # A one-sentence brief is the title and is not repeated as a message under it.
        single = opened(live_view(), "live-job")
        single["open"]["job"]["spec"]["brief"] = "Count the words."
        self.assertEqual(text(pane(dashboard.render(single, NOW, token=TOKEN))).count("Count the words"), 1)
        single["open"]["job"]["spec"]["brief"] = "Read the last 90 nightly logs and list every test that timed out in timeouts.md."
        long = pane(dashboard.render(single, NOW, token=TOKEN))
        self.assertIn("timed out in timeouts.md</h1>", long)
        self.assertNotIn('class="bubble"', long)

    def test_hand_launch_is_one_ordered_card(self):
        # A launch card that loses the prompt, the way to Codex or the field for the task link, or puts them out of
        # order, must fail.
        view = opened(live_view(), "web-job", detail="web route: rh cloud prompt web-job, submit it at "
                                                     "chatgpt.com/codex in environment env-7, then attach")
        view["open"]["prompt"] = "research-harness job web-job <rules>"
        page = pane(dashboard.render(view, NOW, token=TOKEN))
        card = re.search(r'<div class="ask-card launch">.*?</ol>', page, re.S).group(0)
        steps = [text(m) for m in re.findall(r"<li>.*?</li>", card, re.S)]
        self.assertEqual(len(steps), 3)
        self.assertIn("Copy the prompt.", steps[0])
        self.assertIn("research-harness job web-job <rules>", steps[0])
        self.assertIn("env-7", steps[1])
        self.assertIn("Open Codex", steps[1])
        self.assertIn("Paste the task link", steps[2])
        self.assertIn('readonly>research-harness job web-job &lt;rules&gt;</textarea>', card)
        self.assertIn('data-copy="prompt-web-job"', card)
        self.assertIn('action="/attach"', card)
        missing = dict(view, open=dict(view["open"], prompt=None))
        self.assertIn("rh cloud prompt web-job", text(dashboard.render(missing, NOW, token=TOKEN)))

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
        for where in ("brief", "accept", "log", "event", "check", "file", "prompt"):
            with self.subTest(where=where):
                view = opened(live_view(), "finished-job" if where in ("check", "file") else
                              "web-job" if where == "prompt" else "live-job")
                record = view["open"]
                if where == "log":
                    record["log"] = value
                elif where == "event":
                    record["events"][1]["data"]["detail"] = value
                elif where == "check":
                    record["receipt"] = {"route": "x", "checks": [{"cmd": value, "exit": 0}]}
                elif where == "file":
                    record["files"] = [{"path": value, "size": 1}]
                elif where == "prompt":
                    record["prompt"] = value
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
        page = dashboard.render(full_view(), NOW, token=TOKEN,
                                result=serve._result(1, "rh: no job <b>x</b>", "The job <i>was</i> not added."))
        self.assertIn("rh: no job &lt;b&gt;x&lt;/b&gt;", page)
        self.assertIn('data-say="The job &lt;i&gt;was&lt;/i&gt; not added."', page)
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
        launch = opened(live_view(), "web-job")
        launch["open"]["prompt"] = "the prompt"
        page = (dashboard.render(live_view(), NOW, token=TOKEN, result="ok")
                + dashboard.render(view, NOW, token=TOKEN) + dashboard.render(launch, NOW, token=TOKEN))
        for in_script, in_page in (("[data-live]", " data-live "), (".toasts", 'class="toasts"'), ("#keys", 'id="keys"'),
                                   ("[data-keys]", " data-keys "), ("[data-region", " data-region="),
                                   ("dataset.sig", " data-sig="), ("[data-tk", " data-tk="), ("dataset.now", " data-now="),
                                   ("'data-act'", " data-act "), ("[data-k", " data-k="), ("#result", ' id="result"'),
                                   ("dataset.due", " data-due="), (".pane", '<main class="pane"'),
                                   ("dataset.pane", " data-pane="), ("dataset.open", " data-open="),
                                   ("a[data-nav]", " data-nav"), (".side a.item", '<nav class="side"'),
                                   (".s-g-needs a.item", 'class="s-g-needs group"'), (".log", 'class="log"'),
                                   ("form.compose", 'class="compose"'), (".cards > .row", 'class="rows cards"'),
                                   ("'.head'", 'class="head"'), ("'.body'", 'class="body"'),
                                   ("[data-copy]", " data-copy="), ("dataset.short", " data-short="),
                                   ("'pace'", " data-pace="), ("'jobs'", " data-jobs="),
                                   (".s-g-running", 'class="s-g-running group"'), (".side-empty", 'class="side-empty"'),
                                   ("dataset.say", " data-say=")):
            with self.subTest(hook=in_script):
                self.assertIn(in_script, dashboard.SCRIPT)
                self.assertIn(in_page, page)

    def test_every_class_the_script_selects_is_in_the_page(self):
        # A selector for a class the page never renders (v1's .g-needs broke the i key and the phone layout
        # without any test noticing) must fail, so must a guard that finds no selectors at all.
        literals = re.findall(r"'((?:[^'\\\n]|\\.)*)'", dashboard.SCRIPT)
        made = set(" ".join(re.findall(r"className = '([^']*)'", dashboard.SCRIPT)).split())
        selected = {c for lit in literals for c in re.findall(r"(?:^|[\s>(,a-z\]])\.([a-z][a-z0-9-]*)", lit)}
        self.assertGreaterEqual(len(selected), 15)
        view = opened(live_view(), "live-job")
        view["open"]["log"] = "out"
        pages = (dashboard.render(live_view(), NOW, token=TOKEN) + dashboard.render(view, NOW, token=TOKEN)
                 + dashboard.render({**live_view(), "new": True}, NOW, token=TOKEN))
        rendered = set(" ".join(re.findall(r'class="([^"]*)"', pages)).split())
        self.assertEqual(sorted(selected - made - rendered), [])
        self.assertIn("g-needs", {c[2:] for c in rendered if c.startswith("s-")})  # positive control for the regex

    def test_jobs_are_listed_once_and_grouped_by_what_they_need(self):
        # A job listed twice, in the wrong group, an ask left off its row, or a row that stays after its ask is
        # settled must fail.
        view = live_view()
        page = dashboard.render(view, NOW, token=TOKEN)
        for jid, where in (("live-job", ["needs"]), ("web-job", ["needs"]), ("queued-job", ["needs"]),
                           ("finished-job", ["done"]), ("broke-job", ["needs"])):
            with self.subTest(job=jid):
                self.assertEqual(page.count(f'data-key="job:{jid}"'), 1)
                self.assertEqual(group(page, jid), where)
        self.assertIn(ASK.capitalize(), item(page, "live-job"))
        self.assertIn("Failed: The command exited with code 2", item(page, "broke-job"))
        self.assertNotIn("4242", item(page, "broke-job"))
        card = re.search(r'<li class="row" data-key="card:live-job".*?</li>', page, re.S).group(0)
        for part in (ASK.capitalize(), CHECK, "Paused by lead:", "Worker output", "/inbox/resolve", "/job/live-job"):
            self.assertIn(part, card)
        # A finished job waiting on a person leaves Done, and an emptied Done is hidden rather than claiming nothing
        # finished.
        asked = full_view()
        asked["inbox"].append({**asked["inbox"][0], "id": 8, "job": "finished-job"})
        page = dashboard.render(asked, NOW)
        self.assertEqual(group(page, "finished-job"), ["needs"])
        self.assertNotIn("done", shown_groups(page))
        view = full_view()
        view["inbox"] = []
        page = dashboard.render(view, NOW, token=TOKEN)
        self.assertEqual(group(page, "live-job"), ["running"])
        self.assertNotIn('data-key="card:live-job"', page)
        self.assertIn("Paused by lead", item(page, "live-job"))

    def test_a_failure_needs_you_until_it_is_run_again_or_a_day_passes(self):
        # A failure that hides under Done, or one that stays in Needs you after its next run or for ever, must fail.
        view = live_view()
        self.assertEqual(group(dashboard.render(view, NOW, token=TOKEN), "broke-job"), ["needs"])
        rerun = {**view, "meta": {"broke-job-r2": {"prior": "broke-job"}, "broke-job": {"updated": NOW - 60}}}
        self.assertEqual(group(dashboard.render(rerun, NOW, token=TOKEN), "broke-job"), ["done"])
        old = {**view, "meta": {"broke-job": {"updated": NOW - 2 * 86400}}}
        self.assertEqual(group(dashboard.render(old, NOW, token=TOKEN), "broke-job"), ["done"])
        cancelled = {**view, "recent": [dict(view["recent"][1], state="cancelled")]}
        self.assertEqual(group(dashboard.render(cancelled, NOW, token=TOKEN), "broke-job"), ["done"])
        # Its row on home offers Run again, posting straight to /again.
        row = re.search(r'<li class="row" data-key="card:broke-job".*?</li>', dashboard.render(view, NOW, token=TOKEN),
                        re.S).group(0)
        self.assertIn('action="/again"', row)
        self.assertIn("Run again", text(row))
        # In the list it is marked as a failure, not in the amber of a question waiting for an answer.
        entry = re.search(r'<li class="row" data-key="job:broke-job".*?</li>', dashboard.render(view, NOW, token=TOKEN),
                          re.S).group(0)
        self.assertIn('class="dot bad', entry)
        self.assertIn('class="sub fail"', entry)

    def test_one_filled_button_per_page_and_destructive_actions_marked(self):
        # A page with no filled button or with two, or a pause or cancel styled like a safe action, must fail.
        home = pane(dashboard.render(live_view(), NOW, token=TOKEN))
        self.assertEqual(len(primaries(home)), 1)
        self.assertIn('<button class="btn primary send">Start job</button>', home)
        for jid in ("live-job", "web-job", "finished-job", "broke-job"):
            with self.subTest(job=jid):
                right = pane(dashboard.render(opened(live_view(), jid), NOW, token=TOKEN))
                self.assertEqual(len(primaries(right)), 1)
        broke = pane(dashboard.render(opened(live_view(), "broke-job"), NOW, token=TOKEN))
        self.assertIn('<button class="btn primary">Run again</button>', broke)
        pages = dashboard.render(live_view(), NOW, token=TOKEN) + "".join(
            dashboard.render(opened(live_view(), jid), NOW, token=TOKEN) for jid in ("live-job", "queued-job"))
        openers = re.findall(r'<summary class="([^"]+)" data-k="[sc]"', pages)
        self.assertTrue(openers)
        for opener in openers:
            self.assertIn("danger", opener.split())
        for form in re.findall(r'<form[^>]*action="/(?:stop|job/cancel)".*?</form>', pages, re.S):
            self.assertIn('class="btn danger fill"', form)

    def test_plain_words(self):
        # The jargon the founder could not read coming back into the app's text must fail; the snapshot's printed
        # commands may still name the CLI.
        pages = (dashboard.render(live_view(), NOW, token=TOKEN), dashboard.render(full_view(), NOW),
                 dashboard.render(opened(live_view(), "live-job"), NOW, token=TOKEN),
                 dashboard.render(opened(live_view(), "broke-job"), NOW, token=TOKEN),
                 dashboard.render({**live_view(), "checks": {"mode": "other", "pid": "77"}}, NOW, token=TOKEN))
        for page in pages:
            shown = re.sub(r"<(dialog|code|style)\b.*?</\1>", "", page, flags=re.S)
            self.assertEqual(re.findall(r"\b(Run tick|Land|Stops|Routes|Supervisor|Inbox|Resolve|pid|held|"
                                        r"route|PAUSED|tick|preflight)\b", " ".join(Page(shown).text)), [])
        self.assertNotIn(" title=", "".join(pages))  # no CLI tooltips standing in for accessible names

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
            self.assertIn("Local step finished", shown)
            self.assertIn("Last 24 h: 1 done", shown)
            self.assertFalse(dest.with_suffix(".html.tmp").exists())


if __name__ == "__main__":
    unittest.main()
