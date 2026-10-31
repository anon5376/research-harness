"""The app presents the existing status view and one job's record without trusting worker text."""
import copy
import html
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
        self.tags, self.headings, self.groups, self.text = [], [], [], []
        self.heading = None
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag in ("h2", "h3"):
            self.heading = []

    def handle_data(self, data):
        self.text.append(data)
        if self.heading is not None:
            self.heading.append(data)

    def handle_endtag(self, tag):
        if tag in ("h2", "h3") and self.heading is not None:
            (self.headings if tag == "h2" else self.groups).append("".join(self.heading).strip().lower())
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


def inbox(view, pick=None):
    """The live Inbox page, showing item `pick` (or its newest waiting item)."""
    picked = next((i for i in view.get("inbox", []) if i["id"] == pick), None)
    return dashboard.render({**view, "box": True, "pick": pick, "picked": picked}, NOW, token=TOKEN)


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
        groups = ["needs you", "live", "up next", "ended today", "earlier"]
        headings = ["check loop", "jobs", "needs you", "today"]
        page = dashboard.render(full_view(), NOW)
        self.assertEqual(Page(page).headings, headings)
        self.assertEqual(Page(page).groups, groups)
        self.assertEqual(shown_groups(page), ["needs", "ended"])
        self.assertEqual(Page(dashboard.render(live_view(), NOW, token=TOKEN)).groups, groups)
        empty = {"supervisor": None, "stops": [], "routes": [], "live": [], "not_started": [],
                 "inbox": [], "last_24h": {}, "recent": []}
        for view in ({}, empty):
            with self.subTest(view=view):
                page = dashboard.render(view, NOW)
                self.assertEqual(Page(page).headings, headings)
                self.assertEqual(Page(page).groups, groups)
                self.assertEqual(shown_groups(page), [])
                shown = " ".join(Page(page).text)
                for sentence in ("No jobs yet.", "Nothing needs you.", "The harness has not run yet.",
                                 "Last 24 h: nothing finished"):
                    self.assertIn(sentence, shown)
        self.assertRegex(dashboard.render(full_view(), NOW), r'<p class="empty no-jobs"[^>]* hidden><b>No jobs yet\.')
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
        self.assertNotIn("@font-face", page)
        self.assertIn("color-scheme: dark", page)
        self.assertIn("overflow-wrap", page)
        self.assertIn("max-width: 860px", page)
        parsed = Page(page)
        tags = [tag for tag, _ in parsed.tags]
        for tag in ("html", "head", "title", "style", "body", "aside", "main"):
            self.assertIn(tag, tags)
        for tag in ("script", "link", "img", "iframe", "object", "embed", "video", "audio", "font", "canvas"):
            self.assertNotIn(tag, tags)
        self.assertEqual(tags.count("section"), 8)  # five job groups, the job list, Needs you and Today
        self.assertTrue(any(tag == "meta" and attrs.get("name") == "viewport"
                            and "width=device-width" in attrs.get("content", "")
                            for tag, attrs in parsed.tags))
        for _, attrs in parsed.tags:
            self.assertFalse(any(k.startswith("on") or k in ("src", "href") for k in attrs))

    def test_text_contrast_on_the_dark_surfaces(self):
        # A text colour under 4.5:1 on a surface it is drawn on, or a second scheme the tokens do not cover, must fail.
        style = re.search(r"<style>(.*)</style>", dashboard.render(full_view(), NOW), re.S).group(1)
        self.assertNotIn("prefers-color-scheme", style)  # dark only: no light override left half-done
        colours = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-f]{6});", style.split("}", 1)[0]))
        surfaces = ("frame", "ground", "surface", "raised", "active")
        pairs = [(fg, bg) for fg in ("text", "muted", "faint") for bg in surfaces]
        pairs += [(tone, bg) for tone in ("accent", "amber", "red", "ice") for bg in surfaces]
        pairs += [("red", "red-bg"), ("text", "red-bg"), ("accent", "accent-bg"), ("amber", "amber-bg")]
        pairs += [("frame", "text")]  # the filled button: dark words on the light fill
        for fg, bg in pairs:
            with self.subTest(pair=(fg, bg)):
                self.assertGreaterEqual(contrast(colours[fg], colours[bg]), 4.5)
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
        for pick in (7, 8, 9):
            pages[f"inbox/{pick}"] = inbox(cloudy, pick)
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
                if form["page"].startswith("inbox/"):
                    self.assertEqual(form["fields"].get("next"), "/inbox")
                elif form["page"] != "home" and form["action"] != "/job":
                    self.assertEqual(form["fields"].get("next"), f"/job/{form['page'].split('/')[0]}")
                elif form["page"] == "home":
                    self.assertNotIn("next", form["fields"])

        def one(action, page="home", **fields):
            return [f for f in forms if f["action"] == action and f["page"] == page
                    and all(f["fields"].get(k) == v for k, v in fields.items())]
        self.assertEqual(set(one("/inbox/resolve", "inbox/7", id="7")[0]["fields"]), {"t", "id", "note", "next"})
        self.assertEqual(set(one("/harvest", "inbox/8", job="web-job")[0]["fields"]), {"t", "job", "text", "next"})
        self.assertEqual(set(one("/setup", "inbox/9")[0]["fields"]), {"t", "env_id", "next"})
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
        # A waiting cloud job whose row does not lead to the setup form, a setup step home never points to, or an
        # Inbox entry for it without the form, must fail.
        view = live_view()
        setup = {"id": 9, "kind": "operator-step", "job": None, "action": SETUP, "detail": "", "created": NOW - 9}
        cloudy = {**view, "inbox": view["inbox"] + [setup], "meta": {"queued-job": {"kind": "codex-cloud"}}}
        row = item(dashboard.render(cloudy, NOW, token=TOKEN), "queued-job")
        self.assertRegex(row, r'<a class="btn gbtn" href="/job/queued-job\?t=[^"]*" data-nav data-k="r">.*Set up Codex cloud')
        self.assertIn('action="/setup"', dashboard.render(opened(cloudy, "queued-job"), NOW, token=TOKEN))
        alone = {**view, "inbox": view["inbox"] + [setup]}
        home = dashboard.render(alone, NOW, token=TOKEN)
        self.assertIn('data-key="need:9"', re.search(r'<section class="s-g-needs group".*?</section>', home, re.S).group(0))
        self.assertRegex(home, r'<li class="row" data-key="need:9"[^>]*><a class="item" href="/inbox/9\?t=')
        self.assertIn('data-key="inbox:9"', inbox(alone))
        self.assertIn('action="/setup"', inbox(alone, 9))
        # With a waiting cloud job the step rides on that job's row, and its own address still opens it.
        self.assertNotIn('data-key="need:9"', dashboard.render(cloudy, NOW, token=TOKEN))
        self.assertIn('action="/setup"', inbox(cloudy, 9))

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
        # Without the script the command field must not block a cloud job's submit: the server refuses a local job
        # that has no command, so the browser must not require one.
        command = page[page.index('id="command"'):page.index('>', page.index('id="command"'))]
        self.assertNotIn("required", command)
        # Codex cloud is the default only once it is set up; before that a new job runs on this machine.
        def kind(page):
            return Forms(page).forms[[f["action"] for f in Forms(page).forms].index("/job")]["fields"]["kind"]
        self.assertEqual(kind(dashboard.render(view, NOW, token=TOKEN)), "local-shell")
        self.assertIn("set up first", text(dashboard.render(view, NOW, token=TOKEN)))
        self.assertEqual(kind(dashboard.render({**view, "cloud_ready": True}, NOW, token=TOKEN)), "codex-cloud")

    def test_job_page_shows_the_work_left_and_its_record_right(self):
        # Dropping what the job was asked, its output tail, its checks, the files it made or a line of its timeline,
        # or moving the record into the work column, must fail.
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
        for value in ("Write the notes", "Done", "finished-job", dashboard.HERE, "finished 10 s ago", "Done in 1 min 20 s.",
                      "exit 0", "test -s notes.md", "exit 1", "Output", "<tail> & end", "Cite each source.", "notes.md exists", "runs/finished-job/",
                      "120 min", "Timeline", "Added to run in Codex cloud", "Worker output",
                      f"Asked you: {ASK.capitalize()}{dashboard._clock(NOW - 31, NOW)}",
                      f"Mystery thing: raw zz{dashboard._clock(NOW - 5, NOW)}",
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
        work, record = pane(page).split('<aside class="jd-side"', 1)
        for value in ("Cite each source.", "<tail> & end", "# Notes <draft>"):
            self.assertIn(value, text(work))
        for value in ("Properties", "Receipt", "exit 1", "2.0 KB", "Pauses and stops", "Timeline", "Worker output"):
            self.assertIn(value, text(record))
            self.assertNotIn(value, text(work))
        # A held job's page says what to do; a job the harness does not know says so, not a crash.
        held = text(pane(dashboard.render(opened(live_view(), "queued-job"), NOW, token=TOKEN)))
        self.assertIn("On hold: Waiting for input", held)
        self.assertIn("It is rechecked on every pass.", held)
        missing = dashboard.render({**live_view(), "open": {"id": "ghost", "job": None}}, NOW, token=TOKEN)
        self.assertIn("No job named ghost", text(missing))
        self.assertIn('<a class="p-row" role="option" href="/job/live-job?t=', missing)  # ⌘K still finds every job
        # A one-sentence brief is the title and is not repeated as a message under it.
        single = opened(live_view(), "live-job")
        single["open"]["job"]["spec"]["brief"] = "Count the words."
        self.assertEqual(text(pane(dashboard.render(single, NOW, token=TOKEN))).count("Count the words"), 1)
        single["open"]["job"]["spec"]["brief"] = "Read the last 90 nightly logs and list every test that timed out in timeouts.md."
        single["meta"] = {"live-job": {"brief": single["open"]["job"]["spec"]["brief"]}}
        page = dashboard.render(single, NOW, token=TOKEN)
        long = pane(page)
        self.assertIn("timed out in timeouts.md</h1>", long)
        # The list and ⌘K keep an ordinary first sentence whole too; only one past 100 characters is cut.
        self.assertIn('<span class="p-t">Read the last 90 nightly logs and list every test that timed out in timeouts.md'
                      '</span>', page)
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
                page = dashboard.render(view, NOW, token=TOKEN) + inbox(view)
                if (section, key) == ("live", "detail"):  # a job that needs you shows its ask; its page, the detail
                    page += dashboard.render(opened(view, "live-job"), NOW, token=TOKEN)
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
        self.assertNotEqual(before["job:live-job"], after["job:live-job"])
        self.assertNotEqual(before["section:g-needs"], after["section:g-needs"])
        self.assertEqual(before["job:finished-job"], after["job:finished-job"])
        self.assertEqual(before["section:g-ended"], after["section:g-ended"])
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
                + dashboard.render(view, NOW, token=TOKEN) + dashboard.render(launch, NOW, token=TOKEN)
                + inbox(live_view()) + dashboard.render({**live_view(), "live": []}, NOW, token=TOKEN))
        for in_script, in_page in (("[data-live]", " data-live "), (".toasts", 'class="toasts"'), ("#keys", 'id="keys"'),
                                   ("[data-keys]", " data-keys "), ("[data-region", " data-region="),
                                   ("dataset.sig", " data-sig="), ("[data-tk", " data-tk="), ("dataset.now", " data-now="),
                                   ("'data-act'", " data-act "), ("[data-k", " data-k="), ("#result", ' id="result"'),
                                   ("dataset.due", " data-due="), (".pane", '<main class="pane"'),
                                   ("dataset.pane", " data-pane="), ("dataset.open", " data-open="),
                                   ("a[data-nav]", " data-nav"), (".log", 'class="log"'),
                                   ("'.compose'", 'class="compose composer"'), (".pane a.item", '<a class="item"'),
                                   ("a.ib", '<a class="ib'), ("'.head'", 'class="head"'), ("'.body'", 'class="body"'),
                                   ("[data-copy]", " data-copy="), ("dataset.short", " data-short="),
                                   ("'pace'", " data-pace="), ("'jobs'", " data-jobs="),
                                   (".s-g-live", 'class="s-g-live group"'), (".no-jobs", 'class="empty no-jobs"'),
                                   ("dataset.say", " data-say="), ("#pal", 'id="pal"'), ("[data-pal]", " data-pal"),
                                   ("dataset.press", " data-press="), ("[data-tank]", " data-tank"),
                                   ("dataset.creatures", " data-creatures="), ("[data-cycle]", " data-cycle"),
                                   ("[data-clock]", " data-clock"), ("'data-still'", " data-still"),
                                   ("'.ibx'", 'class="page ibx"'), ("dataset.where", " data-where="),
                                   ("dataset.kind", " data-kind="), ("'.n'", 'class="n num"')):
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
                 + dashboard.render({**live_view(), "new": True}, NOW, token=TOKEN) + inbox(live_view()))
        rendered = set(" ".join(re.findall(r'class="([^"]*)"', pages)).split())
        self.assertEqual(sorted(selected - made - rendered), [])
        self.assertIn("g-needs", {c[2:] for c in rendered if c.startswith("s-")})  # positive control for the regex

    def test_jobs_are_listed_once_and_grouped_by_what_they_need(self):
        # A job listed twice, in the wrong group, an ask left off its row, or a row that stays after its ask is
        # settled must fail.
        view = live_view()
        page = dashboard.render(view, NOW, token=TOKEN)
        for jid, where in (("live-job", ["needs"]), ("web-job", ["needs"]), ("queued-job", ["needs"]),
                           ("finished-job", ["ended"]), ("broke-job", ["needs"])):
            with self.subTest(job=jid):
                self.assertEqual(page.count(f'data-key="job:{jid}"'), 1)
                self.assertEqual(group(page, jid), where)
        self.assertIn(ASK.capitalize(), item(page, "live-job"))
        self.assertIn("Failed: The command exited with code 2", item(page, "broke-job"))
        self.assertNotIn("4242", item(page, "broke-job"))
        self.assertIn('href="/inbox/7?t=', item(page, "live-job"))  # its Answer opens the question in the Inbox
        detail = re.search(r'<section class="ib-detail".*?</section>', inbox(view, 7), re.S).group(0)
        for part in (ASK.capitalize(), CHECK, 'action="/inbox/resolve"', 'href="/job/live-job?t='):
            self.assertIn(part, detail)
        # A finished job waiting on a person leaves Done, and an emptied Done is hidden rather than claiming nothing
        # finished.
        asked = full_view()
        asked["inbox"].append({**asked["inbox"][0], "id": 8, "job": "finished-job"})
        page = dashboard.render(asked, NOW)
        self.assertEqual(group(page, "finished-job"), ["needs"])
        self.assertNotIn("ended", shown_groups(page))
        view = full_view()
        view["inbox"] = []
        page = dashboard.render(view, NOW, token=TOKEN)
        self.assertEqual(group(page, "live-job"), ["live"])
        self.assertNotIn('data-key="card:live-job"', page)
        self.assertIn("Paused by lead", item(page, "live-job"))

    def test_a_failure_needs_you_until_it_is_run_again_or_a_day_passes(self):
        # A failure that hides under Done, or one that stays in Needs you after its next run or for ever, must fail.
        view = live_view()
        self.assertEqual(group(dashboard.render(view, NOW, token=TOKEN), "broke-job"), ["needs"])
        rerun = {**view, "meta": {"broke-job-r2": {"prior": "broke-job"}, "broke-job": {"updated": NOW - 60}}}
        self.assertEqual(group(dashboard.render(rerun, NOW, token=TOKEN), "broke-job"), ["ended"])
        old = {**view, "meta": {"broke-job": {"updated": NOW - 2 * 86400}}}
        self.assertEqual(group(dashboard.render(old, NOW, token=TOKEN), "broke-job"), ["earlier"])
        cancelled = {**view, "recent": [dict(view["recent"][1], state="cancelled")]}
        self.assertEqual(group(dashboard.render(cancelled, NOW, token=TOKEN), "broke-job"), ["ended"])
        # Its row offers Run again, posting straight to /again, and is marked as a failure, not in the amber of a
        # question waiting for an answer.
        row = item(dashboard.render(view, NOW, token=TOKEN), "broke-job")
        self.assertIn('action="/again"', row)
        self.assertIn("Run again", text(row))
        self.assertIn('class="st t-red"', row)
        self.assertIn('data-tone="bad"', row)

    def test_one_filled_button_per_page_and_destructive_actions_marked(self):
        # A page with no filled button or with two, or a pause or cancel styled like a safe action, must fail.
        home = pane(dashboard.render(live_view(), NOW, token=TOKEN))
        self.assertEqual(len(primaries(home)), 1)
        self.assertIn('<button class="btn primary send" aria-label="Start job">', home)
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
            self.assertEqual(re.findall(r"\b(Run tick|Land|Stops|Routes|Supervisor|Resolve|pid|held|"
                                        r"route|PAUSED|tick|preflight)\b", " ".join(Page(shown).text)), [])
        self.assertNotIn(" title=", "".join(pages))  # no CLI tooltips standing in for accessible names

    def test_a_ring_names_each_state_in_its_hue(self):
        # A failure drawn in teal, a hand launch in grey, two states sharing one shape, or a ring a screen reader
        # cannot name, must fail.
        states = ("ready", "held", "launching", "running", "handoff", "collecting", "done", "failed", "cancelled")
        rings = {state: dashboard._ring(state) for state in states}
        hue = {state: re.search(r'class="st t-(\w+)"', ring).group(1) for state, ring in rings.items()}
        self.assertEqual(hue, {"ready": "faint", "held": "muted", "launching": "accent", "running": "accent",
                               "handoff": "amber", "collecting": "accent", "done": "accent", "failed": "red",
                               "cancelled": "faint"})
        shapes = {re.sub(r'class="st t-\w+"|aria-label="[^"]*"', "", ring) for ring in rings.values()}
        self.assertEqual(len(shapes), len(rings))
        for state, ring in rings.items():
            self.assertIn(f'role="img" aria-label="{dashboard._STATES[state][0]}"', ring)
        page = dashboard.render(live_view(), NOW, token=TOKEN)
        self.assertIn(rings["failed"], item(page, "broke-job"))
        self.assertIn(rings["done"], item(page, "finished-job"))

    def test_each_seat_is_a_jellyfish_in_the_colour_of_what_it_is_doing(self):
        # A seat drawn teal while its job waits on you or amber while it works, an idle seat drawn lit, a seat left
        # out of the water, a tank in the snapshot (which runs no script), or a tank that ignores reduced motion, must
        # fail.
        view = live_view()
        view["seats"] = [{"name": name, "role": f"The {name} seat", "route": "codex-cloud", "model": "m-1"}
                         for name in ("asks", "works", "rests")]
        view["live"].append({"id": "busy-job", "state": "running", "paused": False, "detail": "", "external_id": None})
        view["meta"] = {"live-job": {"seat": "asks"}, "busy-job": {"seat": "works"}}
        page = dashboard.render(view, NOW, token=TOKEN)
        found = re.search(r'<canvas class="tank-c" data-tank data-creatures="([^"]*)"', page)
        tank = {c["name"]: c for c in json.loads(html.unescape(found.group(1)))}
        self.assertEqual({name: c["tone"] for name, c in tank.items()},
                         {"asks": "amber", "works": "accent", "rests": "faint"})
        self.assertLess(tank["rests"]["alpha"], tank["works"]["alpha"])
        self.assertLess(tank["rests"]["r"], tank["asks"]["r"])
        self.assertLess(tank["asks"]["r"], tank["works"]["r"])
        rail = text(re.search(r'<section class="s-r-seats".*?</section>', page, re.S).group(0))
        for words in ("waiting on you", "working", "idle", "The rests seat"):
            self.assertIn(words, rail)
        self.assertNotIn("<canvas", dashboard.render(view, NOW))
        self.assertIn("no seats.toml", text(dashboard.render(live_view(), NOW, token=TOKEN)))
        # Under reduced motion the tank draws one still frame and never starts its animation loop.
        tank_code = dashboard.SCRIPT[dashboard.SCRIPT.index("function Tank("):]
        tank_code = tank_code[:tank_code.index("\n}\n")]
        self.assertIn("if (calm.matches) { draw(", tank_code)
        self.assertLess(tank_code.index("if (calm.matches) { draw("), tank_code.index("requestAnimationFrame("))

    def test_palette_finds_every_job_every_page_and_only_this_pages_actions(self):
        # A job missing from ⌘K, a page it cannot reach, or an action offered with no control on the page behind it,
        # must fail.
        for page, offered, absent in ((dashboard.render(live_view(), NOW, token=TOKEN), "t", "s"),
                                      (dashboard.render(opened(live_view(), "live-job"), NOW, token=TOKEN), "s", "l"),
                                      (dashboard.render(opened(live_view(), "finished-job"), NOW, token=TOKEN), "l", "s")):
            pal = re.search(r'<dialog class="pal".*?</dialog>', page, re.S).group(0)
            for jid in ("live-job", "web-job", "queued-job", "finished-job", "broke-job"):
                self.assertEqual(pal.count(f'href="/job/{jid}?t='), 1)
            for path in ("/", "/inbox", "/new", "/actions"):
                self.assertIn(f'href="{path}?t=', pal)
            pressed = set(re.findall(r'data-press="([a-z])"', pal))
            self.assertIn(offered, pressed)
            self.assertNotIn(absent, pressed)
            for key in pressed:
                self.assertIn(f'data-k="{key}"', page.replace(pal, ""))
        self.assertNotIn('id="pal"', dashboard.render(live_view(), NOW))

    def test_inbox_lists_what_waits_newest_first_and_shows_one(self):
        # An ask missing from the list, the list out of order, the detail showing another item than the one picked, a
        # settled item offered its form again, or an unknown item shown as another, must fail.
        view = {**live_view(), "settled": [{"id": 5, "kind": "decision", "job": "live-job", "action": "ship it",
                                            "detail": "", "created": NOW - 100, "resolved": NOW - 60,
                                            "resolution": "yes, ship"}]}

        def shown(pick, picked=None):
            page = dashboard.render({**view, "box": True, "pick": pick, "picked": picked}, NOW, token=TOKEN)
            order = re.findall(r'<li class="ib-li[^"]*" data-key="([^"]+)"', page)
            current = re.findall(r'<li class="ib-li[^"]*" data-key="([^"]+)"[^>]*><a[^>]*aria-current', page)
            detail = re.search(r'<section class="ib-detail.*?</section>', page, re.S).group(0)
            return order, current, detail
        order, current, detail = shown(None)
        self.assertEqual(order[:2], ["job:web-job", "job:live-job"])  # newest first
        self.assertEqual(sorted(order[2:]), ["job:broke-job", "job:queued-job", "settled:5"])
        self.assertEqual(current, ["job:web-job"])
        self.assertIn("Paste the patch from Codex", text(detail))
        _, current, detail = shown(7)
        self.assertEqual(current, ["job:live-job"])
        self.assertIn(CHECK, text(detail))
        self.assertIn('action="/inbox/resolve"', detail)
        _, current, detail = shown(5, view["settled"][0])
        self.assertEqual(current, ["settled:5"])
        self.assertIn("Your note: yes, ship", text(detail))
        self.assertNotIn("<form", detail)
        _, current, detail = shown(99)
        self.assertEqual(current, [])
        self.assertIn("No inbox item 99", text(detail))
        tabs = text(inbox(view))
        for words in ("All 4", "Questions 1", "Patches 1", "Failed 1", "Other 1"):
            self.assertIn(words, tabs)
        # Two items asked in the same second: the later one is newer.
        tied = {**view, "inbox": [dict(i, created=NOW - 5) for i in view["inbox"]]}
        self.assertEqual(re.findall(r'<li class="ib-li" data-key="(job:[a-z-]+)"', inbox(tied))[:2],
                         ["job:web-job", "job:live-job"])

    def test_this_mac_says_whether_a_heavy_step_may_start(self):
        # A load or pressure the panel drops, heavy work offered over the limit, or a failed reading shown as a
        # number, must fail.
        def mac(host):
            page = dashboard.render({**live_view(), "host": host}, NOW, token=TOKEN)
            return text(re.search(r'<section class="mac".*?</section>', page, re.S).group(0))
        calm = mac({"load1": 2.17, "level": 1, "max_load": 10.0, "ok": True, "reason": "", "lease": {"held": False}})
        for words in ("Load, 1 min2.17 of 10", "Memory pressure1 normal", "Heavy jobsmay start", "Heavy leasefree"):
            self.assertIn(words, calm)
        busy = mac({"load1": 12.5, "level": 2, "max_load": 10.0, "ok": False, "reason": "load 12.50 above 10.00",
                    "lease": {"held": True, "age_s": 95}})
        for words in ("12.50 of 10", "Memory pressure2 warning", "Heavy jobswait", "Heavy leasein use",
                      "Load 12.50 above 10.00."):
            self.assertIn(words, busy)
        broken = mac({"error": "HostReadError: sysctl failed"})
        self.assertIn("Load and memorynot read", broken)
        self.assertIn("HostReadError: sysctl failed", broken)
        self.assertNotIn("This Mac", text(dashboard.render(full_view(), NOW)))

    def test_one_count_of_what_needs_you_everywhere(self):
        # The band, the Needs you header, the Inbox badge and the Inbox title disagreeing, or any of them counting
        # something other than the rows under Needs you and in the Inbox, must fail.
        view = live_view()
        setup = {"id": 9, "kind": "operator-step", "job": None, "action": SETUP, "detail": "", "created": NOW - 9}
        stop = {"id": 3, "scope": "route:codex-cloud", "kind": "pause", "owner": "app", "reason": "cooling down",
                "created": NOW - 50, "lifted": None, "lifted_by": None}
        stray = {"id": 11, "kind": "decision", "job": "gone-job", "action": "pick a source", "detail": "",
                 "created": NOW - 40, "resolved": None, "resolution": None}
        views = {4: view, 5: {**view, "inbox": view["inbox"] + [setup]},
                 7: {**view, "inbox": view["inbox"] + [setup, stray], "stops": view["stops"] + [stop]}}
        views[4.0] = {**view, "inbox": view["inbox"] + [setup], "meta": {"queued-job": {"kind": "codex-cloud"}}}
        for expected, v in views.items():
            with self.subTest(expected=expected):
                home, box = dashboard.render(v, NOW, token=TOKEN), inbox(v)
                needs = re.search(r'<section class="s-g-needs group".*?</section>', home, re.S).group(0)
                waiting = re.search(r'<section class="s-ib-wait".*?</section>', box, re.S).group(0)
                counts = {"band": re.search(r'<p class="tally".*?data-need="(\d+)"', home, re.S).group(1),
                          "header": re.search(r'<h3 id="g-needs">Needs you</h3><span class="n num">(\d+)<', needs).group(1),
                          "badge": re.search(r'<b class="count num" data-need="(\d+)"', home).group(1),
                          "inbox title": re.search(r'<span class="top-meta mono"><span data-need="(\d+)"', box).group(1),
                          "rows": str(needs.count('<li class="row"')), "inbox rows": str(waiting.count('<li class="ib-li"'))}
                self.assertEqual(set(counts.values()), {str(int(expected))}, counts)
                self.assertIn(f"{int(expected)} need you", text(home))
                self.assertIn(f"{int(expected)} need you", text(box))
        empty = {**view, "live": [], "not_started": [], "recent": [], "inbox": [], "stops": []}
        home, box = dashboard.render(empty, NOW, token=TOKEN), inbox(empty)
        self.assertNotIn("data-need=", home + box.split('<main', 1)[1].split('</main>')[0].replace(
            '<span data-need="0">', ""))
        self.assertIn("nothing needs you", text(home))
        self.assertIn("nothing needs you", text(box))

    def test_rows_say_why_under_the_title(self):
        # A Needs you row whose reason sits beside the control instead of under the title, a reason coloured like an
        # alarm, or a phone layout that hides it, must fail.
        row = item(dashboard.render(live_view(), NOW, token=TOKEN), "live-job")
        link = re.search(r'<a class="item".*?</a>', row, re.S).group(0)
        self.assertRegex(link, rf'<span class="name">live-job</span><span class="sub">{re.escape(ASK.capitalize())}</span>')
        self.assertNotIn('class="sub', row.split("</a>", 1)[1])  # the control stands alone
        rules = dict(re.findall(r"\.row \.(\w+) \{([^}]*)\}", dashboard.STYLE))
        self.assertIn("grid-row: 2", rules["sub"])
        self.assertIn("color: var(--muted)", rules["sub"])
        phone = dashboard.STYLE.split("@media (max-width: 860px)", 1)[1].split("\n}\n", 1)[0]
        self.assertNotRegex(phone, r"\.sub[^{]*\{[^}]*display: none")
        self.assertIn(".row .do { grid-column: 2 / 4; grid-row: 3;", phone)

    def test_phone_controls_are_a_thumb_tall(self):
        # A control under 44 px tall at 480 px wide or less, or one left out of the rule, must fail.
        phone = dashboard.STYLE.split("@media (max-width: 480px) {", 1)[1].split("\n}\n", 1)[0]
        tall = {}
        for selectors, body in re.findall(r"([^{}]+)\{([^}]*)\}", phone):
            height = re.search(r"min-height: (\d+)px", body)
            for selector in selectors.split(","):
                if height:
                    tall[selector.strip()] = int(height.group(1))
        for control in (".btn", ".gbtn", ".chip", ".seg", ".compose .name", ".nav a", 'input[type="text"]',
                        '.tabs [role="tab"]', ".icon-btn", ".send", ".k-pill", ".toast .x", "details.fold summary"):
            with self.subTest(control=control):
                self.assertGreaterEqual(tall.get(control, 0), 44)

    def test_finished_pages_lead_with_the_result_and_fold_the_log(self):
        # A done page that leads with its log, a log left open, a failed page whose fix box is not right under where
        # it stopped, or a box pinned below the work, must fail.
        log = "\n".join(f"line {n}" for n in range(1, 31))
        done = opened(live_view(), "finished-job", kind="local-shell", log=None)
        done["open"].update(log=log, peek={"path": "notes.md", "text": "# Notes", "more": False})
        body = pane(dashboard.render(done, NOW, token=TOKEN)).split('<div class="jd-body">', 1)[1].split("<aside", 1)[0]
        order = re.findall(r'data-region="(j-peek|j-output|j-reply|j-error)"', body)
        self.assertEqual(order, ["j-peek", "j-output", "j-reply"])
        self.assertRegex(body, r'<details class="fold"><summary>.*Output.*last 30 lines.*</summary><pre class="log ended"')
        broke = opened(live_view(), "broke-job", kind="local-shell")
        broke["open"].update(log=log, receipt={"route": "local-shell", "checks": [{"cmd": "sh run.sh", "exit": 2}],
                                               "ended": NOW - 10, "detail": "command exited 2"})
        body = pane(dashboard.render(broke, NOW, token=TOKEN)).split('<div class="jd-body">', 1)[1].split("<aside", 1)[0]
        order = re.findall(r'data-region="(j-peek|j-output|j-reply|j-error)"', body)
        self.assertEqual(order, ["j-error", "j-reply", "j-output"])
        error = re.search(r'data-region="j-error".*?</section>', body, re.S).group(0)
        self.assertIn("exit 2", error)
        self.assertIn("line 30", error)
        self.assertNotIn("line 18\n", error)  # only where it stopped: the last lines
        short = opened(live_view(), "broke-job", kind="local-shell")
        short["open"]["log"] = "ls: docs/: No such file or directory"
        body = pane(dashboard.render(short, NOW, token=TOKEN))
        self.assertNotIn('data-region="j-output"', body)  # nothing more to unfold
        self.assertNotIn(".jd-compose { flex: none", dashboard.STYLE)

    def test_machine_words_are_mono_and_the_machine_has_one_name(self):
        # A duration, size or path set in the serif inside a sentence, ticking times in the serif, or the machine
        # called anything but its one name, must fail.
        view = opened(live_view(), "finished-job", kind="local-shell")
        view["open"]["files"] = [{"path": "notes.md", "size": 2048}]
        page = pane(dashboard.render(view, NOW, token=TOKEN))
        for machine in ('Done in <span class="mono">1 min 20 s</span>', 'Files in <span class="mono">runs/finished-job/'
                        '</span>', '<span class="mono">120 min</span>', '<span class="size mono">2.0 KB</span>'):
            self.assertIn(machine, page)
        self.assertRegex(dashboard.STYLE, r"\n\.when \{ font-family: var\(--mono\)")
        pages = [dashboard.render(full_view(), NOW), dashboard.render(live_view(), NOW, token=TOKEN), page,
                 inbox(live_view()), dashboard.render({**live_view(), "new": True}, NOW, token=TOKEN)]
        for page in pages:
            self.assertNotRegex(" ".join(Page(re.sub(r"<style>.*?</style>", "", page, flags=re.S)).text),
                                r"(?i)this machine")
        self.assertIn(dashboard.HERE, text(dashboard.render(live_view(), NOW, token=TOKEN)))
        added = opened(live_view(), "live-job")
        added["open"]["events"] = [{"seq": 1, "ts": NOW, "job": "live-job", "kind": "job-added",
                                    "data": {"kind": "local-shell"}}]
        self.assertIn(f"Added to run on {dashboard.HERE}", text(dashboard.render(added, NOW, token=TOKEN)))

    def test_jellyfish_stay_inside_their_band(self):
        # A creature that rises out of the band and wraps round, or whose centre is not held a body's length from
        # every edge, must fail.
        code = dashboard.SCRIPT[dashboard.SCRIPT.index("  function creature("):]
        code = code[:code.index("\n  }\n")]
        self.assertNotIn("% span", code)
        self.assertRegex(code, r"const cy = clamp\([^;]*, y0 \+ 1 \+ up, y1 - 1 - down\);")
        self.assertRegex(code, r"const cx = clamp\([^;]*, x0 \+ 1 \+ side, x1 - 1 - side\);")
        self.assertIn("const { box: [x0, y0, x1, y1], fit, held, tall, slot } = at;", code)
        self.assertIn("const fit = Math.max(0.3, scale(c, box));", dashboard.SCRIPT)

    def test_jellyfish_swim_beside_the_composer_not_behind_it(self):
        # A composer the water does not know is solid, a creature that may swim behind it, one sized for the whole
        # band instead of its own water, or a side's seats piled on one spot or given equal room whatever their
        # heights, must fail.
        today = dashboard.render(live_view(), NOW, token=TOKEN)
        self.assertRegex(today, r'<form class="compose composer"[^>]* data-solid>')
        code = dashboard.SCRIPT[dashboard.SCRIPT.index("function Tank("):]
        code = code[:code.index("\n}\n")]
        self.assertIn("const el = $('[data-solid]', host);", code)
        # The water beside the composer and above it ends two cells short of it.
        self.assertIn("return [Math.floor((b.left - box.left) / CW) - 2, Math.ceil((b.right - box.left) / CW) + 2,\n"
                      "      Math.floor((b.top - box.top) / CH) - 2];", code)
        # Each side's water is the lane beside the composer and the strip above it, never the composer itself.
        self.assertIn("const water = s && [[[0, 0, Math.max(0, s[0]), rows], [0, 0, half, top]],\n"
                      "      [[Math.min(cols, s[1]), 0, cols, rows], [half, 0, cols, top]]];", code)
        self.assertIn("const at = spec.map(c => place(c, water && water[c.fx < 0.5 ? 0 : 1]));\n    stack(at);\n"
                      "    spec.forEach((c, k) => creature(c, k, t, at[k]));", code)
        self.assertIn("const box = held ? pick : [0, 0, cols, rows];", code)
        self.assertIn("const fit = Math.max(0.3, scale(c, box));", code)
        # Down a lane, each gets room in proportion to its height.
        self.assertIn("p.slot = p.tall ? (before + p.h / 2) / total : (i + 0.5) / here.length;", code)
        self.assertIn("const ay = held && tall ? y0 + slot * (y1 - y0) - (down - up) / 2 : c.fy * rows;", code)
        # The size it is drawn at leaves room for its bell and its arms' sway, which `side` measures.
        self.assertIn("(x1 - x0 - 6) / (c.r * 2.12), (x1 - x0 - 4) / (c.r * 1.82 + c.len * 0.64)", code)
        self.assertIn("side = Math.max(rx + 2, rx * 0.86 + L * 0.32 + 1)", code)

    def test_side_lanes_hold_a_big_jellyfish_at_1440_and_1100(self):
        # A composer so wide that the open water beside it is under 18 cells at 1440 or 1100 px, or a lane in which
        # a waiting seat's jellyfish is drawn under 0.75 of its size at 1440, must fail. The widths are worked out from
        # the stylesheet: the sidebar, the rail (shown above 1180 px), the pane's right margin and border, and the
        # composer's clamp.
        style, script = dashboard.STYLE, dashboard.SCRIPT
        root = style.split(":root", 1)[1].split("}", 1)[0]
        var = {k: int(v) for k, v in re.findall(r"--(side|rail|lane): (\d+)px;", root)}
        pane = style.split("\n.pane {", 1)[1].split("}", 1)[0]
        right = (int(re.search(r"margin: \d+px (\d+)px", pane).group(1))
                 + 2 * int(re.search(r"border: (\d+)px", pane).group(1)))
        clamp = r"\.tank-in \{[^}]*width: clamp\((\d+)px, calc\(100% - 2 \* var\(--lane\)\), (\d+)px\);"
        lo, hi = map(int, re.search(clamp, style).groups())
        narrow = style.split("@media (max-width: 1180px) {", 1)[1]
        self.assertIn(".mc, .jd, .ibx { grid-template-columns: minmax(0, 1fr); }", narrow.split("\n}", 1)[0])
        cw = int(re.search(r"const CW = (\d+),", script).group(1))
        self.assertIn("return [Math.floor((b.left - box.left) / CW) - 2,", script)
        for width, rail in ((1440, var["rail"]), (1100, 0)):
            with self.subTest(width=width):
                band = width - var["side"] - rail - right
                composer = min(max(lo, band - 2 * var["lane"]), hi, band - 48)
                lane = int((band - composer) / 2 // cw) - 2
                self.assertGreaterEqual(lane, 18)
                if width == 1440:
                    self.assertEqual(band, 810)  # as measured in the browser
                    seat = dashboard._creature("seat", "amber", 0)  # a seat waiting on you
                    r, length = seat["r"], seat["len"]
                    fit = min(1, (lane - 6) / (r * 2.12), (lane - 4) / (r * 1.82 + length * 0.64))
                    self.assertGreaterEqual(fit, 0.75)
        self.assertIn("(x1 - x0 - 6) / (c.r * 2.12), (x1 - x0 - 4) / (c.r * 1.82 + c.len * 0.64)", script)

    def test_a_running_jobs_cut_log_says_last_lines(self):
        # A running job whose page holds only the end of its log, shown as if it were all of it, or a whole log
        # called the last lines, must fail.
        view = opened(live_view(), "live-job", kind="local-shell")
        view["open"]["log"] = "\n".join(f"line {n}" for n in range(200))
        page = pane(dashboard.render(view, NOW, token=TOKEN))
        self.assertIn('<span class="pg-s">lines so far</span>', page)
        self.assertIn('<span class="lines mono">200 lines</span>', page)
        view["open"]["log_cut"] = True
        page = pane(dashboard.render(view, NOW, token=TOKEN))
        self.assertIn('<b class="num">200</b><span class="pg-s">last lines</span>', page)
        self.assertIn('<span class="lines mono">last 200 lines</span>', page)
        self.assertNotIn("lines so far", page)

    def test_a_jobs_heading_is_its_whole_first_sentence_said_once(self):
        # A heading cut short, a brief repeated under its own heading, or a list title left uncut, must fail.
        view = opened(live_view(), "live-job")
        first = ("Read every nightly log from the last ninety days, list each test that timed out with its slowest "
                 "run, and flag any test that timed out more than twice")
        view["open"]["job"]["spec"]["brief"] = first + ". Write it to timeouts.md.\nKeep it short."
        view["meta"] = {"live-job": {"brief": view["open"]["job"]["spec"]["brief"]}}
        page = dashboard.render(view, NOW, token=TOKEN)
        self.assertIn(f"{first}</h1>", page)
        self.assertEqual(text(pane(page).split('<aside class="jd-side"', 1)[0]).count(first), 1)
        bubble = re.search(r'<div class="bubble">(.*?)</div>', page, re.S).group(1)
        self.assertEqual(bubble, "Write it to timeouts.md.\nKeep it short.")
        listed = re.search(r'href="/job/live-job\?t=[^"]*" data-nav><span class="p-ic">.*?<span class="p-t">(.*?)</span>',
                           page, re.S).group(1)
        self.assertTrue(listed.endswith("…") and len(listed) <= 100 and first.startswith(listed[:-1]), listed)
        self.assertIn("text-wrap: pretty", dashboard.STYLE.split(".jd-head h1 {", 1)[1].split("}", 1)[0])

    def test_a_wrapped_brief_or_an_abbreviation_still_heads_with_the_whole_sentence(self):
        # A heading that stops at a brief's first line break or at "e.g.", or a list under it run into one line,
        # must fail.
        cases = [("Read every nightly log from the last\nninety days and list each timeout. Write timeouts.md.",
                  "Read every nightly log from the last ninety days and list each timeout", "Write timeouts.md."),
                 ("Compare e.g. two runs.", "Compare e.g. two runs", None),
                 ("Scan these logs:\n- build.log\n- test.log", "Scan these logs:", "- build.log\n- test.log")]
        for brief, heading, under in cases:
            with self.subTest(brief=brief):
                view = opened(live_view(), "live-job")
                view["open"]["job"]["spec"]["brief"] = brief
                view["meta"] = {"live-job": {"brief": brief}}
                page = dashboard.render(view, NOW, token=TOKEN)
                self.assertIn(f">{html.escape(heading, quote=False)}</h1>", page)
                bubble = re.search(r'<div class="bubble">(.*?)</div>', page, re.S)
                self.assertEqual(bubble and html.unescape(bubble.group(1)), under)

    def test_the_launch_prompt_shows_whole_lines(self):
        # A prompt box whose height is not a whole number of its lines, a box with no fade or no Show all, or a box
        # the page cannot open in full, must fail.
        view = opened(live_view(), "web-job")
        view["open"]["prompt"] = "line\n" * 12
        card = re.search(r'<div class="ask-card launch">.*?</ol>', pane(dashboard.render(view, NOW, token=TOKEN)),
                         re.S).group(0)
        self.assertRegex(card, r'<div class="clip" data-clip><textarea class="prompt mono" id="prompt-web-job"')
        self.assertIn('<button type="button" class="btn small quiet" data-more hidden>Show all</button>', card)
        # The text is cut by its own box at exactly four lines; the padding and border sit outside it, on the clip,
        # so no part of a fifth line shows in them.
        rule = dashboard.STYLE.split(".steps .clip .prompt {", 1)[1].split("}", 1)[0]
        height, line = (int(re.search(rf"(?<![\w-]){k}: (\d+)px", rule).group(1)) for k in ("height", "line-height"))
        self.assertEqual(height, 4 * line)
        for zero in ("padding: 0;", "border: 0;"):
            self.assertIn(zero, rule)
        clip = dashboard.STYLE.split(".clip {", 1)[1].split("}", 1)[0]
        for rule in ("padding: 12px", "border: 1px solid"):
            self.assertIn(rule, clip)
        self.assertIn("height: 24px", dashboard.STYLE.split(".clip::after {", 1)[1].split("}", 1)[0])
        self.assertIn(".clip.whole::after, .clip.open::after { display: none; }", dashboard.STYLE)
        self.assertIn("if (m) { more(m); return; }", dashboard.SCRIPT)
        self.assertIn("box.style.height = open ? box.scrollHeight + 'px' : '';", dashboard.SCRIPT)

    def test_the_rail_says_who_works_without_repeating_the_list(self):
        # A seat that repeats its job's title or its own name, or a Codex cloud section that lists the jobs again,
        # must fail.
        view = live_view()
        view["seats"] = [{"name": "testing", "role": "Testing: the test runner", "route": "codex-cloud", "model": "m-1"},
                         {"name": "docs", "role": "the guides", "route": "codex-cloud", "model": "m-1"},
                         {"name": "backend", "role": "Back end: the supervisor", "route": "codex-cloud", "model": "m-1"}]
        view["meta"] = {jid: {"kind": "codex-cloud"} for jid in ("live-job", "web-job", "queued-job", "finished-job",
                                                                   "broke-job")}
        view["meta"]["live-job"].update(seat="backend", brief="Tally the slow tests.")
        page = dashboard.render(view, NOW, token=TOKEN)
        seats = re.search(r'<section class="s-r-seats".*?</section>', page, re.S).group(0)
        self.assertIn('<span class="i-m mono">live-job</span>', seats)
        self.assertNotIn("Tally the slow tests", seats)
        self.assertIn('<span class="i-m">The test runner</span>', seats)
        self.assertNotIn("Testing:", seats)
        self.assertIn('<span class="i-m">The guides</span>', seats)
        self.assertNotIn("supervisor", seats)
        cloud = re.search(r'<section class="s-r-cloud".*?</section>', page, re.S).group(0)
        self.assertEqual(cloud.count('<p class="r-sum">'), 1)
        self.assertNotIn('class="card"', cloud)
        for jid in ("live-job", "web-job", "queued-job", "finished-job", "broke-job"):
            self.assertNotIn(jid, cloud)
        # The one to launch by hand comes first; live-job waits on a question, but it is still running.
        line = text(re.search(r'<p class="r-sum">.*?</p>', cloud, re.S).group(0))
        self.assertEqual(line, "1 to launch by hand · 1 running · 1 on hold · 1 failed · 1 ended today")
        self.assertIn('<span class="n num">5</span>', cloud)
        self.assertRegex(cloud, r'<a href="/inbox\?t=[^"]*" data-nav><span class="num">1</span> to launch by hand</a>')

    def test_a_finished_jobs_page_has_a_muted_meter_and_a_dim_seat(self):
        # A time meter coloured as a verdict, a finished job's page with its jellyfish drawn amber or teal, or a
        # question on a failed job drawn amber in the Inbox, must fail.
        # live-job is running with a question waiting on you, so its seat is amber; the two ended jobs' are dim.
        for jid, tone in (("broke-job", "faint"), ("finished-job", "faint"), ("live-job", "amber")):
            with self.subTest(jid=jid):
                view = opened(live_view(), jid)
                page = pane(dashboard.render(view, NOW, token=TOKEN))
                meter = re.search(r'<span class="cells( \w+)?" aria-hidden="true">', page)
                self.assertEqual(meter.group(1), " muted")
                found = re.search(r'data-creatures="([^"]*)" data-region="j-water"', page)
                self.assertEqual(json.loads(html.unescape(found.group(1)))[0]["tone"], tone)
        stalled = opened(live_view(), "live-job", state="stalled")
        self.assertIn('<span class="cells amber"', pane(dashboard.render(stalled, NOW, token=TOKEN)))
        # In the Inbox, a question on a failed job is drawn dim too; one on a running job, amber.
        view = live_view()
        view["inbox"].append({"id": 9, "kind": "decision", "job": "broke-job", "action": "run it again?", "detail": "",
                              "created": NOW - 1, "resolved": None, "resolution": None})
        for pick, tone in ((9, "faint"), (7, "amber")):
            found = re.search(r'data-creatures="([^"]*)" data-region="q-water"', inbox(view, pick))
            self.assertEqual(json.loads(html.unescape(found.group(1)))[0]["tone"], tone)

    def test_the_palettes_state_word_shows_whole_or_not_at_all(self):
        # A job's id and state run together in one ellipsised line, or a meta column that can show part of the state
        # word, must fail.
        page = dashboard.render(live_view(), NOW, token=TOKEN)
        self.assertRegex(page, r'<span class="p-s"><span class="mono">web-job</span><span>launch by hand</span></span>')
        row = dashboard.STYLE.split(".p-row {", 1)[1].split("}", 1)[0]
        self.assertIn("grid-template-columns: 20px minmax(0, 1fr) 240px auto", row)
        meta = dashboard.STYLE.split(".p-s {", 1)[1].split("}", 1)[0]
        for rule in ("flex-wrap: wrap", "justify-content: flex-end", "height: 20px", "line-height: 20px",
                     "overflow: hidden"):
            self.assertIn(rule, meta)
        self.assertNotIn("text-overflow", meta)

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
