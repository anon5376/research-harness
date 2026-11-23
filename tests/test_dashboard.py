"""The app presents the existing status view and one job's record without trusting worker text."""
import copy
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from rh import contract, dashboard, supervisor
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
          "/checks", "/again", "/setup", "/handoff", "/appearance")
UNSEEN = {"id": 10, "kind": "operator-step", "job": None, "action": supervisor.ENV_STEP.format(env="research-harness"),
          "detail": "codex cloud answered: cloud exec exited 1: Error: environment 'research-harness' not found; run "
                    "`codex cloud` to list available environments. Cloud jobs wait and launch by themselves",
          "created": NOW - 9}


def unseen_view():
    """live_view() with a ready cloud job and the step the supervisor writes while the codex CLI cannot see the
    environment."""
    view = live_view()
    view["not_started"].append({"id": "cloud-job", "state": "ready", "paused": False, "detail": "",
                                "external_id": None})
    return {**view, "inbox": view["inbox"] + [UNSEEN], "meta": {"cloud-job": {"kind": "codex-cloud"}}}


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


FUSION = "fusion-claude-fable-5-2-medium-sidekick-swe-2-high"


class Forms(HTMLParser):
    """Each form's action, method and fields (name -> value), as a browser would submit them."""
    def __init__(self, source):
        super().__init__()
        self.forms, self.current = [], None
        self.select = None
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {"action": attrs.get("action"), "method": attrs.get("method"), "fields": {}}
            self.forms.append(self.current)
        elif tag == "select" and self.current is not None:
            self.select = attrs.get("name")
        elif tag == "option" and self.select and "selected" in attrs:
            self.current["fields"][self.select] = attrs.get("value", "")
        elif tag in ("input", "textarea") and self.current is not None and attrs.get("name"):
            if attrs.get("type") in ("radio", "checkbox") and attrs["name"] in self.current["fields"]:
                if "checked" not in attrs:
                    return
            self.current["fields"][attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None
        elif tag == "select":
            self.select = None


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


class ListsTest(unittest.TestCase):
    def test_a_done_run_is_named_by_what_its_agent_said(self):
        # One brief run many times reads as many runs: each done one is named by its answer's first line.
        self.assertEqual(dashboard.answer_head("**Reject a job whose `accept` is a list — untested: I could not**\n\nWhy"),
                         "Reject a job whose accept is a list")
        self.assertEqual(dashboard.answer_head("# Fixed the typo.\nmore"), "Fixed the typo")
        self.assertEqual(dashboard.answer_head("\n \n"), "")
        # boilerplate never names a run: a status word alone, a lead-in ending in a colon, or a leading "Done."
        self.assertEqual(dashboard.answer_head("Done\n\nThe label is renamed in the header."), "The label is renamed in the header")
        self.assertEqual(dashboard.answer_head("Done. Summary of the diff:\n- Added a copy button to code blocks"),
                         "Added a copy button to code blocks")
        self.assertEqual(dashboard.answer_head("Finished.\nOK\nHere is what changed:\n\nRounded the ETA"), "Rounded the ETA")
        self.assertEqual(dashboard.answer_head("Done. Rounded the ETA to whole hours."), "Rounded the ETA to whole hours")
        self.assertEqual(dashboard.answer_head("OK"), "")
        # a status word's dash goes with it: '— renamed the label' named a swarm run (critic r9 F13)
        # and what is left starts as a sentence does (critic r10 F3)
        self.assertEqual(dashboard.answer_head("Done — renamed the label to Run"), "Renamed the label to Run")
        self.assertEqual(dashboard.answer_head("Done. · fixed it"), "Fixed it")
        self.assertEqual(dashboard.answer_head("rh/serve.py — x"), "x")  # no status word stripped: left as written
        # a label is cut by the list's ellipsis, which would cut a line number to a false ':1…': the number goes whole
        self.assertEqual(dashboard.answer_head("The label is renamed at rh/dashboard.py:1623"),
                         "The label is renamed at rh/dashboard.py")
        self.assertEqual(dashboard.answer_head("Fixed rh/serve.py:12:5 and app.js:40-52 today"),
                         "Fixed rh/serve.py and app.js today")
        self.assertEqual(dashboard.answer_head("Ran it at 10:30 and it passed"), "Ran it at 10:30 and it passed")
        # its own cut is one '…' with no full stop before it
        self.assertEqual(dashboard.answer_head("aaa" + " b." * 40)[-3:], " b…")
        # a lone path or name before the aside is a label, not what was done: the words after it name the run
        self.assertEqual(dashboard.answer_head("**`rh/dashboard.py`** — `_left` rounds an hour or more to whole hours. More."),
                         "_left rounds an hour or more to whole hours")
        view = live_view()
        view["meta"] = {**view.get("meta", {}), "finished-job": {"kind": "local-shell", "brief": "Improve it.",
                                                                "said": "Fixed the typo"}}
        row = re.search(r'<li class="row" data-key="job:finished-job".*?</li>', dashboard.render(view, NOW, token=TOKEN),
                        re.S).group(0)
        self.assertIn('<span class="name">Fixed the typo</span>', row)
        # two listed runs of one brief are told apart by their ids
        view["meta"].update({"live-job": {"kind": "local-shell", "brief": "Same brief."},
                             "web-job": {"kind": "local-shell", "brief": "Same brief."}})
        page = dashboard.render(view, NOW, token=TOKEN)
        for jid in ("live-job", "web-job"):
            row = re.search(rf'<li class="row" data-key="job:{jid}".*?</li>', page, re.S).group(0)
            self.assertIn(f'<span class="name"><span class="nm" title="Same brief">{jid}</span></span>', row)
            # the palette too: the id leads, and the shared name moves to the grey line
            self.assertRegex(page, rf'<span class="p-t">{jid}</span><span class="p-s"><span class="p-w">[^<]+</span>'
                                   rf'(?:<span class="p-when mono num">[^<]+</span>)?<span>Same brief</span>')
            # and the job's own page names it as the sidebar does, so the two never disagree
            one = opened(view, jid, kind="local-shell")
            one["open"]["job"]["spec"]["brief"] = "Same brief."
            head = re.search(r'data-region="j-head".*?</section>', dashboard.render(one, NOW, token=TOKEN), re.S).group(0)
            self.assertIn(f'<h1 tabindex="-1" data-clear title="Same brief">{jid}</h1>', head)

    def test_a_follow_up_keeps_the_name_its_thread_first_said(self):
        # A follow-up's own answer ("Yes, the change is in this copy") must not rename the thread: the sidebar row
        # and the page header wear the first run's said, and the later answer's line shows only inside the thread.
        view = live_view()
        view["recent"] += [{"id": "finished-job-r2", "state": "done", "paused": False,
                            "detail": "accepted output", "external_id": None},
                           {"id": "finished-job-r3", "state": "done", "paused": False,
                            "detail": "accepted output", "external_id": None}]
        view["meta"] = {**view.get("meta", {}),
                        "finished-job": {"kind": "local-shell", "brief": "Improve it.", "said": "Fixed the typo"},
                        "finished-job-r2": {"kind": "local-shell", "brief": "Improve it.", "prior": "finished-job",
                                            "said": "Yes, the change is in this copy"},
                        "finished-job-r3": {"kind": "local-shell", "brief": "Improve it.",
                                            "prior": "finished-job-r2", "said": "Confirmed it once more"}}
        page = dashboard.render(view, NOW, token=TOKEN)
        for jid in ("finished-job", "finished-job-r2", "finished-job-r3"):
            row = item(page, jid)
            self.assertIn('<span class="name">Fixed the typo</span>', row)
            self.assertNotIn("this copy", row)
            self.assertNotIn("once more", row)
        # and each run's page is headed as the sidebar names it
        for jid, before in (("finished-job-r2", "finished-job"), ("finished-job-r3", "finished-job-r2")):
            one = opened(view, jid, kind="local-shell")
            one["open"]["job"]["spec"].update(brief="Improve it.", prior=before)
            head = re.search(r'data-region="j-head".*?</section>',
                             dashboard.render(one, NOW, token=TOKEN), re.S).group(0)
            self.assertIn('<h1 tabindex="-1" data-clear>Fixed the typo</h1>', head)
        # the later answer's first line shows inside the thread, not in the name
        one = opened(view, "finished-job-r2", kind="local-shell")
        one["open"]["job"]["spec"].update(brief="Improve it.", prior="finished-job")
        one["open"]["answer"] = "Yes, the change is in this copy\n\nIt sits in runs/finished-job-r2."
        body = pane(dashboard.render(one, NOW, token=TOKEN))
        self.assertIn("Yes, the change is in this copy", body)
        head = re.search(r'data-region="j-head".*?</section>', body, re.S).group(0)
        self.assertNotIn("this copy", head)

    def test_the_open_job_is_always_in_the_sidebar(self):
        # A job opened from its address after it fell off the recent list must still have a row to light, or the
        # page header names a job the sidebar does not.
        view = opened(live_view(), "finished-job", kind="local-shell")
        view["recent"] = [r for r in view["recent"] if r["id"] != "finished-job"]
        view["meta"] = {k: v for k, v in view.get("meta", {}).items() if k != "finished-job"}
        side = re.search(r'<aside class="side".*?</aside>', dashboard.render(view, NOW, token=TOKEN), re.S).group(0)
        row = re.search(r'<li class="row" data-key="job:finished-job".*?</li>', side, re.S)
        self.assertIsNotNone(row)
        self.assertIn("Write the notes", row.group(0))

    def test_a_swarm_is_one_row_and_its_judge_page_lists_each_run(self):
        # The runs of a /swarm are the judge's work, not jobs of their own: a sidebar row per run, a judge page
        # that only says "waiting for fix.w2", or a swarm that reads "Up next" while its runs work, must fail.
        view = live_view()
        view["live"] = [{"id": "fix.w2", "state": "running", "paused": False, "detail": "pid 7", "external_id": None}]
        view["not_started"] = [{"id": "fix.best", "state": "waiting", "paused": False,
                                "detail": "waiting for fix.w2 (running)", "external_id": None}]
        view["recent"] = [*view["recent"], {"id": "fix.w1", "state": "done", "paused": False, "detail": "",
                                             "external_id": None}]
        base = {"brief": "Fix the typo", "kind": "local-shell", "created": NOW - 300, "started": NOW - 290}
        view["meta"] = {**view.get("meta", {}),
                        "fix.w1": {**base, "updated": NOW - 230, "model": "swe-2-max", "goal": "fix",
                                   "said": "Renamed the label"},
                        "fix.w2": {**base, "updated": NOW - 5, "model": "swe-2-flash-high", "goal": "fix"},
                        "fix.best": {**base, "updated": NOW - 5, "model": "swe-2-max", "goal": "fix",
                                     "after": ["fix.w1", "fix.w2"]}}
        page = dashboard.render(opened(view, "fix.best", kind="local-shell", state="waiting"), NOW, token=TOKEN)
        side = re.search(r'<aside class="side".*?</aside>', page, re.S).group(0)
        self.assertNotIn('data-key="job:fix.w1"', side)
        self.assertNotIn('data-key="job:fix.w2"', side)
        live = re.search(r'Live.*?</ul>', side, re.S).group(0)
        self.assertIn('data-key="job:fix.best"', live)  # the swarm is live while a run works
        card = re.search(r'data-region="j-swarm".*?</section>', page, re.S).group(0)
        self.assertIn("1 of 2 finished", text(card))
        for word in ("SWE-2 Max", "SWE-2 Flash", "Running", "Renamed the label", "1 min"):
            self.assertIn(word, text(card))
        # a done run's ring already says it: its row shows the time alone, with no "Done" said a second time
        self.assertNotIn("Done", text(card))
        self.assertIn('href="/job/fix.w1?t=', card)
        self.assertIn("keeps the best work once every run has ended", text(page))
        self.assertNotIn("waiting for fix.w2", page)
        status = re.search(r'data-region="job-result".*?</p>', page, re.S).group(0)
        self.assertIn("Running", status)
        # the count is said once, in the Swarm card's head beside its bar, not again in the status line above it
        self.assertNotIn("of 2", text(status))
        self.assertEqual(text(page).count("1 of 2"), 1)
        self.assertNotIn("No output received yet", page)  # the judge has none until it starts
        # once the judge has checked the runs, each row says whether its changes passed
        done = opened(view, "fix.best", kind="local-shell", state="waiting")
        done["open"]["swarm_checks"] = {"fix.w1": "passed", "fix.w2": "failed: exit 1"}
        card = re.search(r'data-region="j-swarm".*?</section>', dashboard.render(done, NOW, token=TOKEN), re.S).group(0)
        self.assertIn('<span class="sw-c">Check passed</span>', card)
        self.assertIn('<span class="sw-c bad">Check failed</span>', card)
        # the run the judge kept is marked, the others step back, each row names its run, and the answer is the judge's
        done["open"].update(swarm_kept="fix.w1", answer="Kept the label fix")
        done["open"]["job"]["state"] = "done"
        done["open"]["job"]["spec"]["command"] = ["python3", "/x/rh/agent.py", "devin", "/bin/devin", "/r", "b",
                                                  "--model", "swe-2-max"]
        page = dashboard.render(done, NOW, token=TOKEN)
        card = re.search(r'data-region="j-swarm".*?</section>', page, re.S).group(0)
        self.assertRegex(card, r'<a class="sw-run kept"[^>]*>.*?<span class="sw-id mono">w1</span>.*?Kept</span>')
        self.assertRegex(card, r'<a class="sw-run lost"[^>]*>.*?<span class="sw-id mono">w2</span>')
        # marked once, in words: a coloured left rule beside the tag doubled it (critic r9 F12)
        self.assertNotIn(".sw-run.kept::before", dashboard.STYLE)
        # a passed-over run steps back in colour, not opacity: its text keeps full contrast, only the ring dims,
        # and its id stays readable: the judge's answer names runs by it (r10 F7)
        self.assertNotIn(".sw-run.lost { opacity", dashboard.STYLE)
        self.assertNotIn(".sw-run.lost > :not(.sw-id)", dashboard.STYLE)
        self.assertIn(".sw-run.lost > :is(.sw-m, .sw-s, .sw-a) { color: #9aa0ad; }", dashboard.STYLE)
        self.assertIn(".sw-run.lost > svg { opacity: .6; }", dashboard.STYLE)
        self.assertIn("font-variant-numeric: tabular-nums", dashboard.STYLE.split(".sw-id {", 1)[1].split("}", 1)[0])
        # each run row says it opens, as 'Preview ›' does: a trailing chevron and a soft hover (critic r11 F3)
        for row in re.findall(r'<a class="sw-run[^"]*".*?</a>', card, re.S):
            self.assertRegex(row, r'<span class="sw-go"[^>]*><svg[^>]*><path d="m10 7 5 5-5 5"/></svg></span></a>$')
        rule = lambda sel: dashboard.STYLE.split("\n" + sel + " {", 1)[1].split("}", 1)[0]  # noqa: E731
        self.assertIn("background: rgb(255 255 255 / .04)", rule("a.sw-run:hover"))
        self.assertIn("transition: background-color 120ms ease-out", rule("a.sw-run"))
        # the state word sits in its own span, so a phone row can leave it to the ring; a done run's ring already
        # says 'done', so its column is the time (and verdict) alone, every other state keeps word and separator
        rows = {m.group(1): m.group(0) for m in re.finditer(
            r'<a class="sw-run[^"]*"[^>]*><span class="sw-id mono">(\w+)</span>.*?</a>', card, re.S)}
        self.assertNotIn('class="sw-w"', rows["w1"])
        self.assertNotIn('class="sep"', rows["w1"])
        self.assertIn('<span class="sw-s">1 min 0 s <span class="sw-c">Check passed</span></span>', rows["w1"])
        self.assertIn('<span class="sw-w">Running</span><span class="sep">·</span>', rows["w2"])
        self.assertIn("color: #9aa0ad", rule(".sw-id"))
        self.assertIn("font-variant-numeric: tabular-nums", rule(".sw-s"))
        # on a phone each run is one 44px line: no answer line, no state word, no 'Check passed' (critic r11 F2)
        phone = dashboard.STYLE.split("/* phone swarm rows */", 1)[1].split("\n}", 1)[0]
        for spec in ("min-height: 44px", ".sw-a { display: none; }", ".sw-w, .sw-w + .sep { display: none; }",
                     ".sw-c:not(.bad) { display: none; }"):
            self.assertIn(spec, phone)
        # the Kept column is a fixed 48px so a row without the tag ends its time at the same x, and the time
        # right-aligns against it
        self.assertIn("grid-template-columns: 22px 16px minmax(0, 1fr) auto 48px 12px", phone)
        self.assertIn(".sw-run .sw-s { grid-column: 4; grid-row: 1; justify-self: end; }", phone)
        # the desktop follows the same rule: time right-aligned against a fixed Kept column, after the answer line,
        # so '51 s' and '4 min 49 s' share a tail (critic r14 F4)
        desk = dashboard.STYLE.split("/* desktop swarm rows */", 1)[1].split("\n}", 1)[0]
        # one grid for all runs (subgrid): the time column is as wide as the longest time, so an answer line is not
        # cut 98px short of it (critic r15 F4)
        self.assertIn(".sw-runs { display: grid; grid-template-columns: 32px 16px 112px minmax(0, 1fr) auto 48px 22px;",
                      desk)
        self.assertIn("grid-template-columns: subgrid", desk)
        self.assertIn(".sw-run .sw-s { grid-column: 5; grid-row: 1; justify-self: end; margin-left: 6px; }", desk)
        self.assertIn(".sw-run .sw-a { grid-column: 4; grid-row: 1; }", desk)
        answer = re.search(r'data-region="j-answer".*?</p>', page, re.S).group(0)
        self.assertIn("Judge", text(answer))

    def test_a_swarm_row_shows_each_runs_diff_size(self):
        # To compare runs the judge's page needs each run's diff size beside its time: a run whose folder held a
        # patch shows its added and removed counts inside .sw-s after the time and before the check chip, in the
        # diff preview's own colours; a run with no patch entry shows nothing there.
        view = live_view()
        base = {"brief": "Fix the typo", "kind": "local-shell", "created": NOW - 300, "started": NOW - 290,
                "goal": "fix"}
        view["recent"] = [*view["recent"], {"id": "fix.best", "state": "done", "paused": False, "detail": "",
                                            "external_id": None}]
        view["meta"] = {**view.get("meta", {}),
                        "fix.w1": {**base, "state": "done", "updated": NOW - 230, "model": "swe-2-max",
                                   "said": "Renamed the label"},
                        "fix.w2": {**base, "state": "done", "updated": NOW - 235, "model": "swe-2-flash-high"},
                        "fix.best": {**base, "state": "done", "updated": NOW - 200, "model": "swe-2-max",
                                     "after": ["fix.w1", "fix.w2"]}}
        done = opened(view, "fix.best", kind="local-shell", state="done")
        done["open"].update(swarm_stats={"fix.w1": [3, 1]}, swarm_checks={"w1": "passed"})
        card = re.search(r'data-region="j-swarm".*?</section>',
                         dashboard.render(done, NOW, token=TOKEN), re.S).group(0)
        rows = {m.group(1): m.group(0) for m in re.finditer(
            r'<a class="sw-run[^"]*"[^>]*><span class="sw-id mono">(\w+)</span>.*?</a>', card, re.S)}
        self.assertIn('<span class="sw-d"><span class="n add">+3</span><span class="n del">−1</span></span>',
                      rows["w1"])
        self.assertIn("1 min 0 s<span class=\"sw-d\"", rows["w1"])  # right after the time, no separator
        self.assertLess(rows["w1"].index('class="sw-d"'), rows["w1"].index('class="sw-c"'))  # before the chip
        self.assertNotIn("sw-d", rows["w2"])
        # r18 F2: 'Run again' beside 'SWE-2 Max' read as 'run SWE-2 Max alone', while it re-runs only the judge on
        # the runs' work. A judge's dock says what it does: 'Judge again', and a typed follow-up still says Send
        page = dashboard.render(done, NOW, token=TOKEN)
        self.assertIn('aria-label="Judge again" title="Judge again" data-followup>', page)
        self.assertNotIn('aria-label="Run again"', page)
        self.assertIn('.composer[data-label="Judge again"]:has(.c-text:placeholder-shown) .compose-tools '
                      '.send[data-followup]::before { content: "Judge again"; }', dashboard.STYLE)
        self.assertIn("t.value.trim() ? 'Send follow-up' : (form.dataset.label || 'Run again')", dashboard.SCRIPT)
        # on a phone a row with a diff size drops its time instead, so the model keeps its name (critic r17 F4)
        phone = dashboard.STYLE.split("/* phone swarm rows */", 1)[1].split("\n}", 1)[0]
        self.assertIn(".sw-run .sw-s:has(> .sw-d) { display: flex; align-items: center; font-size: 0; }", phone)
        # the counts wear the diff preview's colours, not a second palette
        self.assertIn(".sw-d { display: inline-flex; gap: 6px; margin-left: 10px;", dashboard.STYLE)
        for line in (":is(.peek-sum, .peek .dl.file, .sw-d) .n.add { color: #7ee2a0; }",
                     ":is(.peek-sum, .peek .dl.file, .sw-d) .n.del { color: #f69a9a; }",
                     ":is(.peek-sum, .peek .dl.file, .sw-d) .n.zero { color: var(--faint); }"):
            self.assertIn(line, dashboard.STYLE)

    def test_a_finished_swarm_reads_finished_and_is_named_by_the_work_it_kept(self):
        # Runs that ended long ago leave the status view: a card that then calls them 'Waiting' with no time, or a
        # swarm named by its judge's 'I chose run w1' instead of what the kept run did, must fail.
        view = live_view()
        base = {"brief": "Fix the typo", "kind": "local-shell", "created": NOW - 900, "started": NOW - 890,
                "goal": "fix"}
        view["recent"] = [*view["recent"], {"id": "fix.best", "state": "done", "paused": False, "detail": "",
                                             "external_id": None}]
        view["meta"] = {**view.get("meta", {}),
                        "fix.w1": {**base, "updated": NOW - 700, "state": "done", "model": "swe-2-max",
                                   "said": "The label is renamed in the header"},
                        "fix.w2": {**base, "updated": NOW - 650, "state": "failed", "model": "swe-2-flash-high"},
                        "fix.best": {**base, "updated": NOW - 600, "state": "done", "model": "swe-2-max",
                                     "said": "I chose run w1", "kept": "w1", "after": ["fix.w1", "fix.w2"]}}
        done = opened(view, "fix.best", kind="local-shell", state="done")
        done["open"]["swarm_kept"] = "fix.w1"
        page = dashboard.render(done, NOW, token=TOKEN)
        card = text(re.search(r'data-region="j-swarm".*?</section>', page, re.S).group(0))
        self.assertNotIn("Waiting", card)
        self.assertNotIn("Done", card)  # a done run's ring says it; the row keeps only its time
        self.assertIn("Failed", card)
        self.assertIn("3 min", card)  # w1 ran from NOW-890 to NOW-700
        side = re.search(r'<aside class="side".*?</aside>', page, re.S).group(0)
        row = re.search(r'data-key="job:fix.best".*?</a>', side, re.S).group(0)
        self.assertIn("The label is renamed in the header", text(row))
        self.assertNotIn("I chose run w1", text(row))
        self.assertIn("The label is renamed in the header", text(re.search(r"<h1.*?</h1>", pane(page), re.S).group(0)))
        # a judge that leads with the change's own title keeps it
        for said in ("The applied diff is verified by inspection", "Both runs passed",  # also the judge on itself
                     "The full diff is clean", "The applied test (lines 433-446) asserts exactly the three",
                     "The tests pass on both runs", "Everything verifies statically. The page embeds STYLE",
                     "All checks pass"):
            done["meta"]["fix.best"]["said"] = said
            row = re.search(r'data-key="job:fix.best".*?</a>', dashboard.render(done, NOW, token=TOKEN), re.S).group(0)
            self.assertIn("The label is renamed in the header", text(row))
        # a judge that combined runs has no one run to name it by: its remark on itself gives way to the brief
        done["meta"]["fix.best"].update(said="The full diff is clean", kept="combined")
        row = re.search(r'data-key="job:fix.best".*?</a>', dashboard.render(done, NOW, token=TOKEN), re.S).group(0)
        self.assertNotIn("diff is clean", text(row))
        self.assertIn("Fix the typo", text(row))
        # the kept run's own words are a remark on its process, not the work: the brief names it
        done["meta"]["fix.best"].update(said="I chose run w1", kept="w1")
        done["meta"]["fix.w1"]["said"] = "The test run was refused twice, so per the repo rules I'm not retrying"
        row = re.search(r'data-key="job:fix.best".*?</a>', dashboard.render(done, NOW, token=TOKEN), re.S).group(0)
        self.assertNotIn("refused", text(row))
        self.assertIn("Fix the typo", text(row))
        done["meta"]["fix.w1"]["said"] = "The label is renamed in the header"
        done["meta"]["fix.best"]["kept"] = "w1"
        done["meta"]["fix.best"]["said"] = "Renamed the Options label to Run"
        row = re.search(r'data-key="job:fix.best".*?</a>', dashboard.render(done, NOW, token=TOKEN), re.S).group(0)
        self.assertIn("Renamed the Options label to Run", text(row))
        # a done run with no known time leaves its column empty, not a dangling separator
        del done["meta"]["fix.w1"]["updated"]
        card = re.search(r'data-region="j-swarm".*?</section>',
                         dashboard.render(done, NOW, token=TOKEN), re.S).group(0)
        row = re.search(r'<a class="sw-run[^"]*"[^>]*><span class="sw-id mono">w1</span>.*?</a>', card, re.S).group(0)
        self.assertNotIn('class="sw-w"', row)
        self.assertNotIn('class="sep"', row)
        self.assertEqual(re.search(r'<span class="sw-s">(.*?)</span>', row).group(1), "")

    def test_a_working_swarms_head_counts_off_its_finished_runs(self):
        # "2 runs" reads the same at half-done and all-done: while a run still works the head must count the
        # finished ones and draw the count, or the owner reads rows off to tell how far the swarm is.
        view = live_view()
        view["not_started"] = [{"id": "fix.best", "state": "waiting", "paused": False,
                                "detail": "waiting for fix.w2", "external_id": None}]
        base = {"brief": "Fix the typo", "kind": "local-shell", "created": NOW - 300, "started": NOW - 290,
                "goal": "fix"}
        view["meta"] = {**view.get("meta", {}),
                        "fix.w1": {**base, "state": "done", "updated": NOW - 230, "model": "swe-2-max"},
                        "fix.w2": {**base, "state": "running", "updated": NOW - 5, "model": "swe-2-flash-high"},
                        "fix.best": {**base, "updated": NOW - 5, "model": "swe-2-max",
                                     "after": ["fix.w1", "fix.w2"]}}
        page = dashboard.render(opened(view, "fix.best", kind="local-shell", state="waiting"), NOW, token=TOKEN)
        head = re.search(r'data-region="j-swarm".*?<div class="head">(.*?)</div>', page, re.S).group(1)
        self.assertIn("1 of 2 finished", text(head))
        bar = re.search(r'<span class="sw-bar"[^>]*>', head)
        self.assertIsNotNone(bar)
        for attr in ('role="progressbar"', 'aria-valuemin="0"', 'aria-valuemax="2"', 'aria-valuenow="1"',
                     'style="--p:50%"'):
            self.assertIn(attr, bar.group(0))
        # once every run has ended the head reads as it always did, and the bar is gone
        view["meta"]["fix.w2"]["state"] = "done"
        page = dashboard.render(opened(view, "fix.best", kind="local-shell", state="waiting"), NOW, token=TOKEN)
        head = re.search(r'data-region="j-swarm".*?<div class="head">(.*?)</div>', page, re.S).group(1)
        # ended, it counts nothing the rows below already show (critic r14 F2)
        self.assertNotIn("runs", text(head))
        self.assertNotIn("finished", text(head))
        self.assertNotIn("progressbar", head)

    def test_an_answers_attachments_open_from_one_side(self):
        # Under an answer the file row opened from a trailing 'Preview ›' and the folded Output below it from a
        # leading '›', 24px apart, so they read as unrelated (critic r10 F6). Folded, Output is a row like the file's,
        # 'Output 7 lines … Show ›'; open, it keeps its card header. A file row and Output sit 8px apart.
        rule = lambda sel: dashboard.STYLE.split("\n" + sel + " {", 1)[1].split("}", 1)[0]  # noqa: E731
        trail = rule("details.worked:not([open]) > summary::after")
        for spec in ('content: "Show"', "margin-left: auto"):
            self.assertIn(spec, trail)
        # r11 F4: still two components. Folded, Output wears a 14px glyph where the file's icon sits, so its name
        # starts on the file name's edge, in the file name's ink with the size's grey count; and 'Show' ends in the
        # very chevron 'Preview' ends in (the summary's own '›', moved last), not a text '›' of another size.
        done = opened(live_view(), "finished-job", kind="local-shell", log=None)
        done["open"].update(log="one\ntwo", answer="Fixed it.")
        summary = re.search(r'<summary class="worked-summary">.*?</summary>', dashboard.render(done, NOW, token=TOKEN),
                            re.S).group(0)
        self.assertTrue(summary.startswith('<summary class="worked-summary">' + dashboard._ic("term", True)), summary)
        chev = rule("details.worked:not([open]) > summary::before")
        for spec in ("order: 1", "width: 9px", "text-align: left", "margin-left: -2px"):
            self.assertIn(spec, chev)
        self.assertNotIn("display: none", chev)
        self.assertIn("display: none", rule("details.worked[open] > summary > .ic"))
        self.assertIn("color: var(--text)", rule("details.worked:not([open]) > summary .worked-output"))
        self.assertIn("color: var(--faint)", rule("details.worked:not([open]) > summary .lines"))
        self.assertIn("width: auto", rule("details.worked:not([open]) > summary"))
        self.assertIn("margin-left: 0", rule("details.worked:not([open]) > summary .lines"))
        self.assertIn("margin-top: -20px", rule(".jd-body .session-file-list + .worked"))  # 4px apart (r16 F1)
        # r16 F1: with a pointer the answer's Copy takes no row, and its attachments sit 20px under it
        hover = dashboard.STYLE.split("@media (hover: hover) {\n  .msg-copy { opacity: 0; }", 1)[1].split("\n}", 1)[0]
        self.assertIn(".msg-acts { position: absolute;", hover)
        self.assertIn("margin-top: -7px", rule(".jd-body > .msg.ai + .session-file-list"))
        # r16 F4: open, the Output head moved its toggle 700px to a leading chevron; it now keeps the row's layout,
        # 'Hide' and the turned chevron where 'Show ›' sat, and a running log (which stays open) has no toggle
        opened_ = "details.worked[open]:not(:has(> .worked-content > pre.log[data-follow]))"
        self.assertIn('content: "Hide"', rule(opened_ + " > summary::after"))
        self.assertIn("order: 1", rule(opened_ + " > summary::before"))
        self.assertIn("display: block", rule(opened_ + " > summary > .ic"))
        following = ".worked:has(> .worked-content > pre.log[data-follow])"
        self.assertIn("content: none", rule(following + " > summary::before"))
        self.assertIn("pointer-events: none", rule(following + " > summary"))
        # r16 F3: the sidebar draws a failure as an outlined ring; the Inbox, palette and banner keep the disc
        self.assertIn("fill: none", rule('.side svg.st[data-s="failed"] circle'))
        # r16 F2: a swarm's kept run says the header's words too
        # r18 F3 reverses that: a kept run's grey answer cell is no heading, so the swarm page keeps its title
        self.assertNotIn(".sw-run.kept .sw-a", dashboard.SCRIPT.split("function echoTitle()", 1)[1].split("\n}", 1)[0])
        # r16 F6, F7: touch shows Copy as its glyph; a phone palette fades its last row over 48px
        self.assertIn(".msg-copy .pc-t { display: none; }", dashboard.STYLE.split("@media (hover: none) {\n  .msg-acts", 1)[1])
        self.assertIn("linear-gradient(#000 calc(100% - 48px), transparent)", dashboard.STYLE)

    def test_provider_marks_wear_no_brand_colour(self):
        # The Claude mark was coral in the picker and on answers but grey in Settings: drift, and a second accent
        # beside the app's one blue (critic r10 F8). Every provider mark takes its row's ink.
        self.assertNotIn("#d97757", dashboard.STYLE)

    def test_the_inbox_panes_start_on_one_line(self):
        # The list's 'Needs you' line box starts 13px below the panes' top and the detail's meta line started 24px
        # below it, so the panes began 11px apart across the divider (critic r10 F10).
        self.assertIn(".q-head-in { position: relative; padding: 13px 40px 20px; }", dashboard.STYLE)

    def test_a_lone_inbox_group_wears_no_heading(self):
        # 'Needs you' under the top bar's '4 need you', with no other group below it, says the count's fact twice
        # (critic r11 F5). Alone, the heading stays for a screen reader only; with Settled today below, both show.
        lone = pane(inbox(live_view()))
        head = re.search(r'<h2 id="ib-wait">(.*?)</h2>', lone).group(1)
        self.assertIn('class="vh"', head)
        settled = {**live_view(), "settled": [{"id": 90, "job": "live-job", "kind": "question", "body": "Which?",
                                               "created": NOW - 600, "resolved": NOW - 60, "note": "the first"}]}
        self.assertNotIn('class="vh"', re.search(r'<h2 id="ib-wait">(.*?)</h2>', pane(inbox(settled))).group(1))
        rule = lambda sel: dashboard.STYLE.split("\n" + sel + " {", 1)[1].split("}", 1)[0]  # noqa: E731
        self.assertIn("clip-path: inset(50%)", rule(".vh"))
        self.assertIn("min-height: 0", rule(".ib-list .head:has(> h2 > .vh:only-child):not(:has(.tabs))"))

    def test_the_inbox_carries_no_label_that_says_nothing(self):
        # 'newest first' over a list with no other order, or '0 settled today' beside the count of what waits, must
        # fail; a day with settled items still says how many.
        page = pane(inbox(live_view()))
        self.assertNotIn("newest first", page)
        top = re.search(r'<span class="top-meta mono">.*?</span></span>', page, re.S).group(0)
        self.assertNotIn("settled today", text(top))
        settled = {**live_view(), "settled": [{"id": 90, "job": "live-job", "kind": "question", "body": "Which?",
                                               "created": NOW - 600, "resolved": NOW - 60, "note": "the first"}]}
        self.assertIn("1 settled today", text(pane(inbox(settled))))

    def test_the_swarm_line_opens_the_swarms_models(self):
        # r19 F3: in swarm mode the model picker stepped aside for grey text, so the one row of controls had a line
        # that did nothing, and changing the swarm meant leaving the brief for Settings. The line is a control: it
        # opens the Settings ticks and judge in a popover under it, saved as they change. Its fields belong to a
        # form of their own outside the composer (a nested form would send them with the job), and the check
        # command rides along unchanged, since the settings route replaces all three.
        swarm = {"models": ["swe-2-max", "swe-2-flash-high"], "judge": "swe-2-max", "check": "pytest -q",
                 "choices": ["swe-2-max", "swe-2-flash-high", "claude-opus-5-5"]}
        page = dashboard.render({**live_view(), "swarm": swarm}, NOW, token=TOKEN)
        # a <details> would close a <p> around it, so this note is a div
        self.assertIn('<div class="swarm-note" data-swarm-note hidden>', page)
        pick = re.search(r'<details class="swarm-pick">.*?</details>', page, re.S).group(0)
        self.assertIn('<summary aria-label="Swarm models"', pick)
        self.assertIn('data-swarm-says="swarm-pick-f"', pick)
        boxes = re.findall(r'<input type="checkbox" name="models"[^>]*>', pick)
        self.assertEqual(len(boxes), 3)
        for box in boxes:
            self.assertIn(' form="swarm-pick-f"', box)
        self.assertEqual(len(re.findall(r' checked', "".join(boxes))), 2)
        self.assertIn('<select name="judge" form="swarm-pick-f">', pick)
        compose = re.search(r'<form[^>]*class="compose[^"]*".*?</form>', page, re.S).group(0)
        self.assertIn('<details class="swarm-pick">', compose)
        self.assertNotIn('id="swarm-pick-f"', compose)  # its own form, after the composer's
        own = re.search(r'<form id="swarm-pick-f"[^>]*>.*?</form>', page, re.S).group(0)
        for part in ('action="/settings/swarm"', 'method="post"', "data-swarm-form", 'name="t" value="' + TOKEN + '"',
                     '<input type="hidden" name="check" value="pytest -q">'):
            self.assertIn(part, own)
        # the script saves fields that belong to a form by its form attribute, renames the line after a save, and
        # closes the popover on a click elsewhere
        save = dashboard.SCRIPT.split("// Swarm settings save as they change", 1)[1].split("\n});", 1)[0]
        self.assertIn("t.form && t.form.matches('[data-swarm-form]')", save)
        self.assertIn("swarmSays(form)", save)
        self.assertIn("function swarmSays(form)", dashboard.SCRIPT)
        self.assertIn("details.swarm-pick[open]", dashboard.SCRIPT)
        # drawn as the model picker's trigger: a 28px ghost button with a chevron that fills on hover
        rule = lambda sel: dashboard.STYLE.split("\n" + sel + " {", 1)[1].split("}", 1)[0]  # noqa: E731
        for spec in ("height: 28px", "border-radius: 6px", "cursor: pointer"):
            self.assertIn(spec, rule(".swarm-pick > summary"))
        self.assertIn("background: rgb(255 255 255 / .06)", rule(".swarm-pick > summary:hover, .swarm-pick[open] > summary"))
        for spec in ("position: absolute", "width: min(360px, calc(100vw - 32px))"):
            self.assertIn(spec, rule(".swarm-pop"))
        # a static snapshot has nothing to save to: the line stays text
        static = dashboard.render({**live_view(), "swarm": swarm}, NOW)
        self.assertNotIn('<details class="swarm-pick">', static)

    def test_the_swarm_line_beside_the_chip_is_short(self):
        # A sentence naming the judge twice beside the chip, crowding the options button, must fail: the line lists
        # the runs and the judge once, compactly.
        page = dashboard.render({**live_view(), "swarm": {"models": ["swe-2-max", "swe-2-flash-high"],
                                                          "judge": "swe-2-max"}}, NOW, token=TOKEN)
        says = re.search(r'<span class="swarm-says"[^>]*>(.*?)</span></p>', page).group(1)
        # 'A + B' names one Fusion model everywhere else, so the runs are listed with commas and the judge is a verb
        # (critic r11 F1)
        self.assertEqual(re.sub(r"<[^>]+>", " ", says).split(), "SWE-2 Max, SWE-2 Flash · SWE-2 Max judges".split())
        # the chip's × shows on hover or focus where there is a pointer, always on touch, with a 24px hit area
        rule = lambda sel: dashboard.STYLE.split("\n" + sel + " {", 1)[1].split("}", 1)[0]  # noqa: E731
        self.assertIn("gap: 12px", rule(".swarm-note"))
        hover = dashboard.STYLE.split("@media (hover: hover) {\n  .swarm-off {", 1)[1].split("}", 1)[0]
        # at rest the x shows at 40%, so its slot reads as a control and the chip is not lopsided (critic r12 F4)
        self.assertIn("opacity: .4", hover)
        self.assertIn(".swarm-chip:is(:hover, :focus-within) .swarm-off { opacity: 1", dashboard.STYLE)
        for spec in ("inset: -3px", 'content: ""'):
            self.assertIn(spec, rule(".swarm-off::after"))

    def test_an_unticked_swarm_box_is_visible(self):
        # An unticked model box drawn at 28% white (about 2.5:1 on the settings panel, under the 3:1 a control
        # needs) must fail.
        rule = dashboard.STYLE.split("\n.swarm-x {", 1)[1].split("}", 1)[0]
        self.assertIn("border: 1.5px solid #6e7383", rule)

    def test_settings_names_the_fusion_pair_and_its_cost_as_the_picker_does(self):
        # A settings row that says only 'Fusion', hiding which pair and which paid API a swarm would run, must fail.
        fusion = "fusion-claude-fable-5-2-medium-sidekick-swe-2-high"
        self.assertEqual(dashboard._names([fusion]), "Fable 5.2 Medium + SWE-2 High")
        page = dashboard.render({**live_view(), "swarm": {"models": ["swe-2-max"], "judge": "swe-2-max",
                                                          "choices": ["swe-2-max", fusion]}}, NOW, token=TOKEN)
        row = re.search(r'<label class="swarm-m">(?:(?!</label>).)*?value="' + fusion + '".*?</label>', page, re.S).group(0)
        self.assertIn("Fable 5.2 Medium + SWE-2 High", text(row))
        self.assertIn('<span class="swarm-cost">API</span>', row)

    def test_the_swarm_options_form_one_grouped_list(self):
        # Judge and Check each run as two loose rows under the model ticks, and a hint that starts at the label's
        # edge instead of under the field it explains, must fail: they sit in a second panel like the models',
        # one 44px row each with a hairline between, and the hint drops 8px under the panel, indented to the
        # rows' text: panel 1px border + 4px padding + the rows' 10px, the edge the model rows' group labels use.
        page = dashboard.render({**live_view(), "swarm": {"models": ["swe-2-max"], "judge": "swe-2-max",
                                                          "choices": ["swe-2-max", "swe-2-high"]}}, NOW, token=TOKEN)
        groups = re.findall(r'<div class="swarm-opts">(.*?)</div>', page, re.S)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].count('class="swarm-judge"'), 2)
        for name in ('name="judge"', 'name="check"'):
            self.assertIn(name, groups[0])
        self.assertIn('</div><p class="set-hint">The harness runs it', page)
        rule = lambda sel: dashboard.STYLE.split("\n" + sel + " {", 1)[1].split("}", 1)[0]  # noqa: E731
        panel = rule(".swarm-opts")
        for spec in ("border-radius: 10px", "background: rgb(255 255 255 / .03)",
                     "border: 1px solid rgb(255 255 255 / .06)"):
            self.assertIn(spec, panel)
        for spec in ("min-height: 44px", "padding: 0 10px"):
            self.assertIn(spec, rule(".swarm-opts > .swarm-judge"))
        self.assertIn("border-top: 1px solid rgb(255 255 255 / .06)",
                      rule(".swarm-opts > .swarm-judge + .swarm-judge"))
        for spec in ("margin-top: 8px", "font-size: 12px", "color: #9aa0ad", "padding-left: 15px"):
            self.assertIn(spec, rule(".swarm-opts + .set-hint"))
        self.assertNotIn(".settings .swarm-judge + .swarm-judge", dashboard.STYLE)
        self.assertNotIn(".swarm-judge + .set-hint", dashboard.STYLE)

    def test_the_settings_grid_and_head_keep_their_measure(self):
        # Names spaced as far from the row below as from their own thumbnail read as that row's titles, and a
        # sheet that scrolled whole cut its contents mid-glyph at its top edge (critic r9 F10, r10 F5): 24px between
        # thumbnail rows (10px across), and a 48px head that never scrolls over a body that does, which fades its
        # top 16px only once scrolled. A sticky head over the translucent glass let the thumbnails show through.
        grid = dashboard.STYLE.split("\n.background-choices {", 1)[1].split("}", 1)[0]
        self.assertIn("gap: 24px 10px", grid)
        rule = lambda sel: dashboard.STYLE.split("\n" + sel + " {", 1)[1].split("}", 1)[0]  # noqa: E731
        for spec in ("display: flex", "flex-direction: column", "overflow: hidden"):
            self.assertIn(spec, rule("dialog.settings[open]"))
        self.assertIn("height: 48px", rule(".settings header"))
        self.assertNotIn("position: sticky", rule(".settings header"))
        for spec in ("overflow-y: auto", "min-height: 0"):
            self.assertIn(spec, rule(".settings-body"))
        # 16px of fade left the scrolled thumbnails as crisp 14px slivers under the head (critic r11 F8): 32px of fade,
        # and a hairline under the head once scrolled
        # r12 F3: still slivers at 32px, and a content-wide .08 hairline nobody saw: 48px, and a sheet-wide .10 line
        self.assertIn("linear-gradient(to bottom, transparent, #000 48px)", rule(".settings-body.scrolled"))
        line = rule("dialog.settings:has(.settings-body.scrolled) header::after")
        for spec in ('content: ""', "left: -24px", "right: -24px", "height: 1px", "background: rgb(255 255 255 / .10)"):
            self.assertIn(spec, line)
        self.assertIn("t.classList.contains('settings-body')", dashboard.SCRIPT)
        page = dashboard.render(live_view(), NOW, token=TOKEN)
        sheet = re.search(r'<dialog class="settings".*?</dialog>', page, re.S).group(0)
        self.assertRegex(sheet, r'</header><div class="settings-body"><form action="/appearance".*</footer></div></dialog>$')

    def test_an_agent_pages_status_reads_on_its_model_line(self):
        # An agent page whose Done/checks/time floats at the column's right edge, outside the answer's measure,
        # while shell pages lead with it on the left, must fail: it follows the model on the same line.
        rule = dashboard.STYLE.split("\n.who-status {", 1)[1].split("}", 1)[0]
        self.assertNotIn("margin-left: auto", rule)
        self.assertIn('.who-status::before { content: "·";', dashboard.STYLE)

    def test_the_headers_buttons_are_the_apps_pills(self):
        # Pause job and Cancel job drawn as rounded rectangles while every other action button is a pill must fail
        # (on a phone they are the header's glass squares instead).
        self.assertIn("@media (min-width: 601px) { .job-actions .act > summary { border-radius: 999px;", dashboard.STYLE)
        self.assertIn(".act > .pop .btn { border-radius: 999px; }", dashboard.STYLE)  # and the confirm's own button
        # a popover's destructive confirm is the outlined pill in the app's red, not a one-off salmon fill (r10 F9)
        confirm = dashboard.STYLE.split("\nform.pop .btn.danger.fill {", 1)[1].split("}", 1)[0]
        for spec in ("height: 28px", "color: var(--red)", "border-color: rgb(255 146 155 / .5)",
                     "background: rgb(255 146 155 / .08)"):
            self.assertIn(spec, confirm)

    def test_a_patch_preview_reads_as_a_diff(self):
        # A changes.patch previewed as plain text, where an added line looks like a removed one, must fail: added,
        # removed and hunk lines each carry their own mark; other files stay plain text. The four git header lines
        # (critic r9 F5: a third of the visible lines) become the card's head, the file and its +/- counts; a hunk
        # head keeps only its function; lines carry their new-file number for the gutter (a removed line has none);
        # an indented line hangs its wrap under its code.
        patch = ("diff --git a/x.py b/x.py\nindex 1..2 100644\n--- a/x.py\n+++ b/x.py\n@@ -7,3 +7,3 @@ def f():\n"
                 " keep\n-old <line>\n+new <line>\n+        deep\n")
        diff = dashboard._peek_html("changes.patch", patch)
        for header in ("diff --git", "index 1..2", "--- a/x.py", "+++ b/x.py", "@@"):
            self.assertNotIn(header, diff)
        self.assertIn('<span class="dl hunk">def f():</span>', diff)
        self.assertIn('<span class="dl" data-n="7"> keep</span>', diff)
        self.assertIn('<span class="dl del">-old &lt;line&gt;</span>', diff)
        self.assertIn('<span class="dl add" data-n="8">+new &lt;line&gt;</span>', diff)
        self.assertIn('<span class="dl add" data-n="9" style="--i:9">+        deep</span>', diff)
        self.assertEqual(dashboard._diff_head(patch),
                         '<span class="mono">x.py</span><span class="n add">+2</span><span class="n del">−1</span>')
        two = patch + "diff --git a/y.py b/y.py\n--- a/y.py\n+++ b/y.py\n@@ -1 +1 @@\n-a\n+b\n"
        # each file of a patch names its own counts (critic r10 F11); a preview cut short leaves them off its last
        # file, whose count would be partial
        self.assertIn('<span class="dl file">y.py<span class="n add">+1</span><span class="n del">−1</span></span>',
                      dashboard._peek_html("changes.patch", two))
        self.assertIn('<span class="dl file">x.py<span class="n add">+2</span>',
                      dashboard._peek_html("changes.patch", two, whole=False))
        self.assertIn('<span class="dl file">y.py</span>', dashboard._peek_html("changes.patch", two, whole=False))
        # a hunk whose head over-counts (x.py's says 3 old lines, its body has 2) ends at a line no body line starts
        # with, so the next file's header is not read as code
        self.assertNotIn("diff --git", dashboard._peek_html("changes.patch", two))
        self.assertIn(":is(.peek-sum, .peek .dl.file, .sw-d) .n.add", dashboard.STYLE)
        self.assertTrue(dashboard._diff_head(two).startswith('<span class="mono">2 files</span>'))
        self.assertIn('<span class="n del zero">−0</span>', dashboard._diff_head("+++ b/x.py\n@@ -0,0 +1 @@\n+a\n"))
        self.assertEqual(dashboard._peek_html("notes.md", "+ a list item\n"), "+ a list item\n")
        self.assertEqual(dashboard._diff_head("+ a list item\n"), "")
        for mark in ("add", "del", "hunk", "file"):
            self.assertIn(f".peek .dl.{mark} {{", dashboard.STYLE)
        # the gutter draws the number (so a copy takes only the code), 40px, right-aligned, in the quiet grey
        gutter = dashboard.STYLE.split(".peek.diff .dl::before {", 1)[1].split("}", 1)[0]
        for spec in ("content: attr(data-n)", "width: 40px", "text-align: right", "color: #6e7383"):
            self.assertIn(spec, gutter)
        # a previewed patch sits in the app's card with its head, folds with a fade and offers the rest
        done = opened(live_view(), "finished-job", kind="local-shell", log=None)
        done["open"].update(files=[{"path": "changes.patch", "size": 300}],
                            peek={"path": "changes.patch", "text": patch, "more": False})
        body = pane(dashboard.render(done, NOW, token=TOKEN))
        # one head (critic r14 F1: the file row, a card head and a file head stacked three heads before any code):
        # the file's own row names what the patch changes and its counts, and heads the card when it opens
        row = re.search(r'<span class="session-file-main">.*?</span></span>', body, re.S).group(0)
        self.assertIn('<span class="peek-sum"><span class="mono">x.py</span><span class="n add">+2</span>', row)
        self.assertNotIn("peek-head", body)
        self.assertIn('<div class="peek-card"><pre class="peek diff"', body)
        rule = lambda sel: dashboard.STYLE.split("\n" + sel + " {", 1)[1].split("}", 1)[0]  # noqa: E731
        card = rule(".session-file:has(> .file-preview[open])")
        for spec in ("border: 1px solid var(--border)", "border-radius: var(--r-card)", "overflow: hidden"):
            self.assertIn(spec, card)
        head = rule(".session-file:has(> .file-preview[open]) > .session-file-main")
        for spec in ("min-height: 40px", "padding: 0 108px 0 14px", "border-bottom: 1px solid var(--rule)"):
            self.assertIn(spec, head)
        for spec in ("margin-top: 0", "border: 0", "border-radius: 0"):
            self.assertIn(spec, rule(".session-file > .file-preview[open] .peek-card"))
        # and the page keeps its own head (a preview's head once took its place)
        self.assertRegex(body, r'<div class="jd-main"><section class="jd-head" data-region="j-head"')
        self.assertIn('<pre class="peek diff" tabindex="0">', body)
        self.assertIn("$$('.user-message, pre.peek.diff')", dashboard.SCRIPT)
        self.assertIn("pre.peek.diff.more:not(.whole)", dashboard.STYLE)

    def test_a_patch_preview_copies_the_whole_file_not_the_preview(self):
        # A preview cut short would copy a partial patch, so the card's Copy fetches the file's whole text at the
        # row link's URL; the static page has no script to run it, so it offers no button.
        patch = ("diff --git a/x.py b/x.py\nindex 1..2 100644\n--- a/x.py\n+++ b/x.py\n@@ -7,3 +7,3 @@ def f():\n"
                 " keep\n-old <line>\n+new <line>\n")
        done = opened(live_view(), "finished-job", kind="local-shell", log=None)
        done["open"].update(files=[{"path": "changes.patch", "size": 300}],
                            peek={"path": "changes.patch", "text": patch, "more": True})
        body = pane(dashboard.render(done, NOW, token=TOKEN))
        link = re.search(r'<a href="([^"]+)"[^>]*>changes\.patch</a>', body)
        self.assertIsNotNone(link)
        buttons = re.findall(r'<button[^>]*class="peek-copy"[^>]*>', body)
        self.assertEqual(len(buttons), 1)
        self.assertIn(f'data-copy-url="{link.group(1)}"', buttons[0])
        self.assertIn('aria-label="Copy the whole patch"', buttons[0])
        # a button, not a caption: its copy glyph, 28px tall, 8px corners, a fill on hover; shown once the card opens
        self.assertIn(dashboard._ic("copy", True), body.split('class="peek-copy"', 1)[1].split("</button>", 1)[0])
        rule = lambda sel: dashboard.STYLE.split("\n" + sel + " {", 1)[1].split("}", 1)[0]  # noqa: E731
        for spec in ("height: 28px", "border-radius: 8px"):
            self.assertIn(spec, rule(".peek-copy"))
        self.assertIn("background: #1c2030", rule(".peek-copy:hover, .peek-copy:focus-visible"))
        self.assertIn("display: none", rule(".session-file:not(:has(> .file-preview[open])) .peek-copy"))
        # on a phone the open head is too narrow for name, path, counts, size, Copy and Preview: the counts drew
        # over the size (layout check, r117). There the path and size step aside and Copy is its glyph alone.
        phone = dashboard.STYLE.split("/* phone patch head */", 1)[1].split("\n}", 1)[0]
        for spec in (".peek-sum .mono { display: none; }", ".peek-copy .pc-t { display: none; }",
                     ".session-file:has(> .file-preview[open]) .session-file-size { display: none; }"):
            self.assertIn(spec, phone)
        self.assertNotIn("peek-copy", pane(dashboard.render(done, NOW)))
        self.assertIn("copyUrl", dashboard.SCRIPT)
        self.assertIn("fetch(b.dataset.copyUrl", dashboard.SCRIPT)

    def test_the_inbox_counts_what_waits_once_in_its_list(self):
        # Mixed kinds get filter tabs; one kind needs none. The count lives in the top bar only: a list heading that
        # repeats it, or a lone tab, must fail.
        head = lambda v: re.search(r'<section class="s-ib-wait".*?<div class="body"', inbox(v), re.S).group(0)
        mixed = head(live_view())
        self.assertIn('role="tablist"', mixed)
        self.assertNotIn('<span class="n num">', mixed)
        one = {**live_view(), "inbox": live_view()["inbox"][:1], "live": [], "not_started": [], "recent": [], "stops": []}
        self.assertNotIn('role="tablist"', head(one))
        self.assertNotIn('<span class="n num">', head(one))  # the top bar holds the count

    def test_the_inbox_says_what_failed_as_the_job_page_does(self):
        # The Inbox is where failures are triaged, so its banner must name the command and its exit code; a bare
        # "Failed: ..." sentence must fail.
        view = {**live_view(), "inbox": [], "live": [], "not_started": []}  # a failed job leads
        view["fails"] = {r["id"]: ["make test", 2, ""] for r in view["recent"] if r["state"] in dashboard._FAILED}
        self.assertTrue(view["fails"])
        detail = re.search(r'data-region="ib-detail".*?</section>', inbox(view), re.S).group(0)
        self.assertIn('<p class="fail-c"><code>make test</code><span class="exit mono">exit 2</span></p>', detail)
        self.assertIn("The command exited with code 2", text(detail))
        # without a recorded check the one-line headline stays
        # an agent run names its CLI and model, never the agent.py line that carries the whole brief
        spec = {"command": ["python3", "/x/rh/agent.py", "devin", "/bin/devin", "/repo", "a long brief", "--model",
                            "swe-2-max"], "accept": "pytest -q"}
        self.assertEqual(dashboard.short_cmd(spec, "python3 /x/rh/agent.py devin /bin/devin /repo 'a long brief'"),
                         "devin --model swe-2-max")
        self.assertEqual(dashboard.short_cmd(spec, "pytest -q"), "pytest -q")
        self.assertEqual(dashboard.short_cmd({"command": ["make", "test"]}, "make test"), "make test")
        plain = re.search(r'data-region="ib-detail".*?</section>', inbox({**view, "fails": {}}), re.S).group(0)
        self.assertNotIn('class="fail-c"', plain)

    def test_the_inbox_opens_on_the_newest_job_that_waits_when_nothing_asks(self):
        page = inbox({**live_view(), "inbox": []})
        self.assertNotIn("No questions to answer.", page)
        detail = re.search(r'data-region="ib-detail".*?</section>', page, re.S).group(0)
        self.assertIn(">Open job</a>", detail)
        self.assertRegex(page, r'<a class="ib" href="[^"]*" data-nav aria-current="true">')
        # on a narrow screen the list and an item are two screens: the list unless an item was asked for
        self.assertIn('class="page ibx" data-view="list"', page)
        self.assertIn('class="page ibx" data-view="item"', inbox(live_view(), live_view()["inbox"][0]["id"]))

    def test_a_failed_jobs_title_folds_a_long_brief(self):
        # The detail title is the job's brief: one its name in the lists cannot say whole folds to three
        # lines with a Show all, the fold the job's own page gives the ask; one it can say keeps the bare
        # title. A title still cut with an ellipsis and no way to read the rest, the rest of the brief
        # nowhere readable, or a toggle on a short brief, must fail.
        view = {**live_view(), "inbox": [], "live": [], "not_started": [], "stops": []}  # a failed job leads
        meta = lambda b: {"broke-job": {"kind": "local-shell", "brief": b, "updated": NOW - 10}}
        brief = ("Read every nightly log from the last ninety days, list each test that timed out with "
                 "its slowest run, and flag any that timed out more than twice.\n\nWrite the list to "
                 "runs/timeouts.md with one line per test.")
        detail = re.search(r'data-region="ib-detail".*?</section>', inbox({**view, "meta": meta(brief)}),
                           re.S).group(0)
        title = re.search(r'<h2[^>]*id="q-h".*?</h2>', detail, re.S).group(0)
        self.assertIn('class="q-title user-message"', title)
        self.assertNotIn("…", title)
        self.assertIn("Write the list to runs/timeouts.md with one line per test.", title)
        # the toggle is the ask-more button the page's script offers once an ask runs past its lines
        self.assertIn('</h2><button type="button" class="ask-more" aria-expanded="false">Show all</button>',
                      detail)
        self.assertIn(".user-message:has(+ .ask-more):not(.whole)", dashboard.STYLE)
        self.assertIn("-webkit-line-clamp: 3", dashboard.STYLE)
        self.assertIn("font: 12px var(--ui)", dashboard.STYLE)
        self.assertIn("more.className = 'ask-more'; more.textContent = 'Show all';", dashboard.SCRIPT)
        # it folds like the job page's brief but stays the item's title: no chat bubble, the title's face and edge
        rule = lambda sel: dashboard.STYLE.split("\n" + sel + " {", 1)[1].split("}", 1)[0]  # noqa: E731
        face = dashboard.STYLE.split(".q-head .q-title.user-message {", 1)[1].split("}", 1)[0]
        for spec in ("background: none", "padding: 0", "margin: 0"):
            self.assertIn(spec, face)
        # and it reads below the failure and Run again, not over them (critic r9 F4): body weight, a step dimmer
        # r13 F2: at 16px white it was still the pane's loudest text, so the failure read second: 15/22 #c3c8d2,
        # 600px wide, two lines, with Show all at its right end as in the bubbles
        # r19 F1: at 15px #c3c8d2 the pane had no title at all, so the eye fell on whatever came next. The brief
        # is the pane's title again: 17px semibold, a step under the page headings, the failure banner's red still
        # carrying the second look
        self.assertIn("font: 600 17px/22px var(--ui)", face)
        self.assertIn("color: #e6e8ee", face)
        self.assertIn("max-width: 600px", face)
        more = dashboard.STYLE.split(".q-head .q-title.user-message + .ask-more {", 1)[1].split("}", 1)[0]
        for spec in ("display: block", "width: fit-content", "margin: 6px max(0px, calc(100% - 600px)) 0 auto"):
            self.assertIn(spec, more)
        # folded, its last line fades out instead of taking the clamp's "…", which after a full stop read "...."
        fold = dashboard.STYLE.split(".q-head .q-title.user-message:has(+ .ask-more):not(.whole) {", 1)[1].split("}", 1)[0]
        for spec in ("display: block", "-webkit-line-clamp: unset", "max-height: 2lh", "overflow: hidden", "mask-image:"):
            self.assertIn(spec, fold)
        # folded, its paragraphs run on, so a brief and its follow-up fill both lines instead of a line and a blank
        # (critic r18 F4: 'Show all' under one visible line read as a control with nothing to show)
        self.assertIn("white-space: normal", fold)
        # r15 F1: the pane opens on what the job is, not its run id: the brief comes first, and the id closes a
        # quiet 12px meta line under it ('This Mac · since … · broke-job'), as the Inbox rows end on it
        self.assertLess(detail.index('id="q-h"'), detail.index('class="q-who q-sub"'))
        sub = re.search(r'<p class="q-who q-sub"[^>]*>(.*?)</p>', detail, re.S).group(1)
        self.assertTrue(sub.endswith('<span class="mono">broke-job</span>'), sub)
        self.assertIn("color: #9aa0ad", rule(".q-who.q-sub"))
        self.assertIn("font-size: 12px", rule(".q-who.q-sub"))
        # r16 F5: Show all shares the meta line, and on a desktop the id is left to the selected row beside it
        self.assertIn("grid-column: 2; grid-row: 2;", rule(".q-head .q-head-in > .q-title.user-message + .ask-more"))
        self.assertIn(".q-who.q-sub > .mono:last-child", dashboard.STYLE)
        short = re.search(r'data-region="ib-detail".*?</section>',
                          inbox({**view, "meta": meta("Fix the flaky test.")}), re.S).group(0)
        self.assertIn('<h2 class="q-title" id="q-h" data-clear>Fix the flaky test</h2>', short)
        self.assertNotIn("user-message", short)
        self.assertNotIn("Show all", short)

    def test_an_inbox_follow_up_keeps_its_brief_as_the_title(self):
        # r19 F1: a follow-up run's brief carries its follow-up as a last paragraph ("Follow-up: ..."), so the pane's
        # title ended on the follow-up and that line read loudest. The title is the brief as first asked; the
        # follow-up is said in the quiet meta line under it. A title holding "Follow-up", or a follow-up said
        # nowhere, must fail.
        view = {**live_view(), "inbox": [], "live": [], "not_started": [], "stops": []}  # a failed job leads
        view["meta"] = {"broke-job": {"kind": "local-shell", "brief": "Count the words in every chapter draft and "
                                      "list the chapters over 4000 words.\n\nFollow-up: skip the appendix",
                                      "prior": "broke-old", "changed": "skip the appendix", "updated": NOW - 10}}
        detail = re.search(r'data-region="ib-detail".*?</section>', inbox(view), re.S).group(0)
        title = re.search(r'<h2[^>]*id="q-h".*?</h2>', detail, re.S).group(0)
        self.assertNotIn("Follow-up", title)
        self.assertNotIn("skip the appendix", title)
        sub = re.search(r'<p class="q-who q-sub"[^>]*>(.*?)</p>', detail, re.S).group(1)
        self.assertIn('<span class="q-fu">Follow-up: skip the appendix</span>', sub)
        self.assertTrue(sub.endswith('<span class="mono">broke-job</span>'), sub)  # the id still closes the line
        fu = dashboard.STYLE.split(".q-who.q-sub > .q-fu {", 1)[1].split("}", 1)[0]
        for spec in ("text-overflow: ellipsis", "white-space: nowrap", "min-width: 0"):
            self.assertIn(spec, fu)
        # a first run says no follow-up
        first = {**view, "meta": {"broke-job": {"kind": "local-shell", "brief": "Count the words.", "updated": NOW}}}
        self.assertNotIn('class="q-fu"', inbox(first))

    def test_an_inbox_failure_row_says_how_it_failed(self):
        # r19 F2: three failed rows each read "Failed: the command exited with code 1", a line that told them
        # apart by nothing. A failed row with a recorded check reads its exit code and the last line it printed
        # (its command when it printed nothing), on one line; the red ring already says it failed.
        view = {**live_view(), "inbox": [], "live": [], "not_started": []}
        bad = [r["id"] for r in view["recent"] if r["state"] in dashboard._FAILED]
        self.assertTrue(bad)
        view["fails"] = {jid: ["make test", 1, ""] for jid in bad}
        view["tails"] = {bad[0]: "counting\nls: drafts/: No such file or directory\n\n"}
        page = inbox(view)
        self.assertIn('<span class="ib-why ib-exit">Exit 1<span class="sep">·</span>'
                      'ls: drafts/: No such file or directory</span>', page)
        if len(bad) > 1:
            self.assertIn('<span class="ib-why ib-exit">Exit 1<span class="sep">·</span>make test</span>', page)
        rows = re.search(r'<section class="s-ib-wait".*?</section>', page, re.S).group(0)
        self.assertNotIn("Failed: the command exited", rows)
        self.assertIn("-webkit-line-clamp: 1", dashboard.STYLE.split(".ib-s:has(> .ib-why.ib-exit) {", 1)[1].split("}", 1)[0])
        # without a recorded check the headline stays
        self.assertNotIn('class="ib-why ib-exit"', inbox({**view, "fails": {}}))

    def test_a_running_job_says_how_far_along_its_log_says_it_is(self):
        self.assertEqual(dashboard._progress("start\nstep 357: window 357 of 2400\n"), (357, 2400))
        self.assertIsNone(dashboard._progress("1 of 1 done"))
        self.assertIsNone(dashboard._progress("no count here"))
        view = opened(live_view(), "live-job", kind="local-shell")
        view["open"]["log"] = "step 600: window 600 of 2400"
        view["open"]["job"]["started"] = NOW - 600
        view["open"]["job"]["state"] = "running"
        status = re.search(r'data-region="job-result".*?</p>', dashboard.render(view, NOW, token=TOKEN), re.S).group(0)
        self.assertIn("600/2400", status)
        self.assertIn("~30 min left", status)
        self.assertIn('<progress class="run-progress" max="2400" value="600"', status)

    def test_a_whole_number_of_hours_drops_the_zero_minutes(self):
        # A duration like "12 h 0 min" prints a zero minor unit; whole hours must read "12 h" while non-zero
        # minutes stay ("11 h 40 min").
        self.assertEqual(dashboard._duration(12 * 3600), "12 h")
        self.assertEqual(dashboard._duration(11 * 3600 + 40 * 60), "11 h 40 min")
        # the running status line's estimate: 1 h spent at 600/7800 leaves exactly 12 h
        view = opened(live_view(), "live-job", kind="local-shell")
        view["open"]["log"] = "step 600: window 600 of 7800"
        view["open"]["job"]["started"] = NOW - 3600
        status = re.search(r'data-region="job-result".*?</p>', dashboard.render(view, NOW, token=TOKEN), re.S).group(0)
        self.assertIn("~12 h left", status)
        self.assertNotIn("0 min", status)
        # and a done job that ran exactly two hours
        done = opened(live_view(), "finished-job", kind="local-shell")
        done["open"]["job"].update(started=NOW - 7200, updated=NOW)
        status = re.search(r'data-region="job-result".*?</p>', dashboard.render(done, NOW, token=TOKEN), re.S).group(0)
        self.assertIn("2 h", status)
        self.assertNotIn("0 min", status)

    def test_a_time_left_estimate_past_an_hour_rounds_to_whole_hours(self):
        # A guess like "~9 h 58 min left" pretends a precision it lacks, and on a narrow phone it splits a
        # number from its unit: an hour or more rounds to whole hours, while under an hour keeps its minutes.
        self.assertEqual(dashboard._left(9 * 3600 + 58 * 60), "~10 h left")
        self.assertEqual(dashboard._left(3600 + 20 * 60), "~1 h left")
        self.assertEqual(dashboard._left(25 * 60), "~25 min left")
        # the running status line's estimate: 1 h spent at 600/2400 leaves exactly 3 h
        view = opened(live_view(), "live-job", kind="local-shell")
        view["open"]["log"] = "step 600: window 600 of 2400"
        view["open"]["job"]["started"] = NOW - 3600
        view["open"]["job"]["state"] = "running"
        status = re.search(r'data-region="job-result".*?</p>', dashboard.render(view, NOW, token=TOKEN), re.S).group(0)
        self.assertIn("~3 h left", status)

    def test_the_status_line_breaks_only_at_its_separators_and_no_line_starts_or_ends_with_one(self):
        # On a 390px phone the estimate split its number from its unit, and then a wrapped line began with a bare
        # "·". Each part wears its own nowrap span with the dot that leads it inside, the parts are joined only by a
        # break chance, and the line hangs its dots 16px into a clipped band, so a wrapped line's leading dot is cut
        # off and the words line up under line 1. The mark sits beside line 1, not between two lines.
        self.assertIn(".job-result-status .nowrap { white-space: nowrap", dashboard.STYLE)
        rule = dashboard.STYLE.split("\n.job-result-status.sl {", 1)[1].split("}", 1)[0]
        for decl in ("margin-left: -16px", "text-indent: 16px", "clip-path: inset(0 0 0 16px)"):
            self.assertIn(decl, rule)
        sep = dashboard.STYLE.split("\n.job-result-status.sl .sep {", 1)[1].split("}", 1)[0]
        self.assertIn("width: 16px", sep)
        self.assertIn("margin: 0", sep)
        self.assertIn(".job-result > .st { align-self: flex-start; margin-top: 3px; }", dashboard.STYLE)
        view = opened(live_view(), "live-job", kind="local-shell", state="stalled")
        view["open"]["job"]["spec"]["timeout_min"] = 120
        view["open"]["job"]["started"] = NOW - 3600
        view["open"]["log"] = "step 600: window 600 of 2400"
        page = dashboard.render(view, NOW, token=TOKEN)
        status = re.search(r'<span class="job-result-status sl">(.*?)</span><progress', page, re.S).group(1)
        parts = status.split("<wbr>")
        self.assertEqual([re.sub(r"</?span[^>]*>", "", part) for part in parts],
                         ["Past its 2 h limit", "·600/2400", "·~3 h left"])  # no vague 'still being checked' (r12 F7)
        self.assertRegex(parts[0], r'^<span class="nowrap">[^<]*</span>$')
        for part in parts[1:]:
            self.assertRegex(part, r'^<span class="nowrap"><span class="sep">·</span>.*</span>$')


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
        # The live app with no jobs opens on the same form used after the first job.
        first = " ".join(Page(dashboard.render({**empty, "can_add": True}, NOW, token=TOKEN)).text)
        for sentence in ("What are we building?", "What should the job do?"):
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

    def test_text_contrast_in_both_schemes(self):
        # A text colour under 4.5:1 on a surface it is drawn on, in the dark scheme or the light one, or a light scheme
        # that leaves a token dark, must fail.
        style = re.search(r"<style>(.*)</style>", dashboard.render(full_view(), NOW), re.S).group(1)
        dark = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-f]{6});", style.split("}", 1)[0]))
        light_block = re.search(r"@media \(prefers-color-scheme: light\) \{\s*:root:not\(\[data-live-app\]\) \{(.*?)\}",
                                style, re.S)
        self.assertIsNotNone(light_block)
        light = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-f]{6});", light_block.group(1)))
        self.assertIn("color-scheme: light", light_block.group(1))
        self.assertEqual(set(light), set(dark))  # every colour token has a light value
        surfaces = ("frame", "ground", "surface", "raised", "active")
        pairs = [(fg, bg) for fg in ("text", "muted", "faint") for bg in surfaces]
        pairs += [(tone, bg) for tone in ("accent", "amber", "red", "ice") for bg in surfaces]
        pairs += [("red", "red-bg"), ("text", "red-bg"), ("accent", "accent-bg"), ("amber", "amber-bg")]
        pairs += [("frame", "text")]  # the filled button: the frame's colour on the text's fill
        for scheme, colours in (("dark", dark), ("light", light)):
            for fg, bg in pairs:
                with self.subTest(scheme=scheme, pair=(fg, bg)):
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
        # Dropping a row field, count or relative timestamp must fail; so must a route line that grows back its
        # failure count, or a healthy harness that names its process.
        view = full_view()
        shown = text(dashboard.render(view, NOW))
        for value in ("Harness 12 s ago", "asked 31 s ago", "Codex cloud: rate-limited·retrying in 40 s",
                      "Paused by lead:", "review output", "live-job", "Running", "Worker output", "queued-job",
                      "On hold: Waiting for input", ASK.capitalize(), CHECK, "finished-job", "Accepted output",
                      "Last 24 h: 2 done, 1 failed"):
            with self.subTest(value=value):
                self.assertIn(value, shown)
        for value in ("failures in a row", "Process 123", "it retries by itself"):
            self.assertNotIn(value, shown)
        self.assertIn("Process 123 may have stopped", text(dashboard.render(
            {**view, "supervisor": {"pid": 123, "age_s": 900}}, NOW)))
        later = text(dashboard.render(view, NOW + 10))
        for value in ("asked 41 s ago", "retrying in 30 s"):
            self.assertIn(value, later)
        view["routes"][0]["next_try"] = NOW - 8
        self.assertIn("retrying on the next check", " ".join(Page(dashboard.render(view, NOW)).text))

    def test_status_line_says_who_checks_the_harness(self):
        # A status line that claims the app checks when it is paused or when rh supervise does, or that offers
        # Pause when there is nothing of the app's to pause, must fail.
        def foot(checks, **extra):
            view = {**live_view(), "checks": checks, **extra}
            page = dashboard.render(view, NOW, token=TOKEN)
            part = page.split('<div class="coord-panel">', 1)[1].split('<h2 class="side-h">Codex cloud</h2>', 1)[0]
            return " ".join(" ".join(Page(part).text).split()), {f["fields"].get("on") for f in Forms(part).forms
                                               if f["action"] == "/checks"}
        said, switches = foot({"mode": "on", "pid": None, "failed": None})
        self.assertIn("Checking automatically 12 s ago", said)
        self.assertEqual(switches, {"0"})
        said, switches = foot({"mode": "paused", "pid": None, "failed": None})
        self.assertIn("Checks paused", said)
        self.assertIn("Check once", said)
        self.assertNotIn("Checking automatically", said)
        self.assertEqual(switches, {"1"})
        said, switches = foot({"mode": "other", "pid": "4242", "failed": None})
        self.assertIn("Checked by rh supervise (process 4242) 12 s ago", said)
        self.assertNotIn("takes over", said)
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
        pages["cloud-job"] = dashboard.render(opened(unseen_view(), "cloud-job"), NOW, token=TOKEN)
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
                         {"t", "brief", "id", "kind", "command", "accept", "needs", "timeout_min", "input_path", "answer"})
        self.assertEqual(set(one("/harvest", "web-job", job="web-job")[0]["fields"]), {"t", "job", "text", "next"})
        self.assertEqual(set(one("/attach", "web-job", job="web-job")[0]["fields"]), {"t", "job", "task_id", "next"})
        self.assertEqual(one("/resume", "web-job", job="web-job", pause="on")[0]["fields"].keys(),
                         {"t", "job", "pause", "next"})
        self.assertEqual(set(one("/stop", "live-job", job="live-job")[0]["fields"]),
                         {"t", "job", "reason", "pause", "next"})
        self.assertEqual(set(one("/land", "finished-job", job="finished-job")[0]["fields"]), {"t", "job", "next"})
        self.assertEqual(set(one("/again", "finished-job", job="finished-job")[0]["fields"]),
                         {"t", "job", "changed", "next", "input_path"})
        self.assertEqual(set(one("/job/cancel", "queued-job", job="queued-job")[0]["fields"]),
                         {"t", "job", "reason", "next"})
        self.assertEqual(set(one("/inbox/resolve", "live-job", id="7")[0]["fields"]), {"t", "id", "note", "next"})
        self.assertEqual(set(one("/setup", "queued-job/setup")[0]["fields"]), {"t", "env_id", "next"})
        # Only the app's own stops get a Resume: the lead's pause on live-job cannot be lifted from here.
        self.assertFalse([f for f in forms if f["action"] == "/resume" and f["fields"].get("job") == "live-job"])
        self.assertFalse([f for f in forms if f["action"] == "/attach" and f["fields"].get("job") == "live-job"])
        # Cancel is offered only before a job starts or once it stalls, a finished job cannot be paused, and a running
        # one cannot be run again.
        self.assertFalse(one("/job/cancel", "live-job"))
        self.assertFalse(one("/stop", "finished-job", job="finished-job"))
        self.assertFalse(one("/again", "live-job", job="live-job"))
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

    def test_unseen_environment_is_one_row_and_its_jobs_offer_a_hand_launch_that_says_why(self):
        # A row per waiting job, a waiting job shown as starting, a job page without the hand launch or without the
        # CLI's own words, an Inbox entry without the way to name another environment, or a hand-launch card that
        # does not say why, must fail; so must the dashboard's copy of the step's opening drifting from the
        # supervisor's. So must a sidebar line that repeats the step, or a receipt or bare state line on the page of a
        # job that has not started.
        self.assertTrue(UNSEEN["action"].startswith(dashboard._NATIVE))
        self.assertEqual(dashboard._NATIVE, supervisor.ENV_ASK)
        view = unseen_view()
        view["routes"] = view["routes"] + [{"route": "cli-env:research-harness", "healthy": 0, "failures": 1,
                                            "next_try": NOW + 500, "last_status": "not found", "updated": NOW - 5}]
        home = dashboard.render(view, NOW, token=TOKEN)
        self.assertEqual(re.findall(r'<div class="route"><p>([^:<]*):', home), ["Codex cloud"])
        needs = re.search(r'<section class="s-g-needs group".*?</section>', home, re.S).group(0)
        self.assertIn('data-key="need:10"', needs)
        self.assertIn("Choose an environment the codex CLI lists", needs)
        self.assertIn("It does not list research-harness, so cloud jobs wait", text(needs))
        self.assertIn("rh doctor", text(needs))
        self.assertRegex(needs, r'<a class="btn gbtn" href="/inbox/10\?t=[^"]*" data-nav data-k="r">')
        self.assertNotIn("cloud-job", needs)
        self.assertEqual(group(home, "cloud-job"), ["next"])
        # One name for a job held for its environment, wherever it shows.
        self.assertIn("Waiting for environment", item(home, "cloud-job"))
        self.assertNotIn("Ready", item(home, "cloud-job"))
        page = dashboard.render(opened(view, "cloud-job"), NOW, token=TOKEN)
        self.assertEqual([f["fields"]["job"] for f in Forms(page).forms if f["action"] == "/handoff"], ["cloud-job"])
        self.assertIn("Error: environment 'research-harness' not found", text(page))
        head = re.search(r'<p class="job-result".*?</p>', page, re.S).group(0)
        props = re.search(r'<section class="j-props".*?</section>', page, re.S).group(0)
        for part in (head, props):
            self.assertIn("Waiting for environment", text(part))
            self.assertNotIn("Ready", text(part))
        # Its page offers the fix first, the environment step in the Inbox, and the hand launch beside it.
        acts = re.search(r'<div class="acts">.*?</div>', page.split('class="ask-card"', 1)[1], re.S).group(0)
        self.assertRegex(acts, r'^<div class="acts"><a class="btn primary" href="/inbox/10\?t=[^"]*" data-nav data-k="r">'
                               r'<svg[^>]*>.*?</svg>Choose environment</a>')
        self.assertIn("Launch by hand instead", acts)
        self.assertEqual(dashboard._plain("launch deferred: the codex CLI cannot see the environment"),
                         "Waiting for environment")
        form = re.search(r'<form method="post" action="/setup".*?</form>', inbox(view, 10), re.S).group(0)
        self.assertIn('placeholder="e.g. research-harness-cli"', form)
        self.assertIn("letters, digits and", text(form))
        self.assertIn(">Use environment</button>", form)
        self.assertNotIn('data-region="j-receipt"', page)
        self.assertNotIn('data-region="j-now"', page)
        self.assertIn('action="/setup"', inbox(view, 10))
        self.assertNotIn('action="/handoff"', inbox(view, 10))
        why = ("the codex CLI cannot see environment 'research-harness': rh cloud prompt web-job, submit it at "
               "chatgpt.com/codex in environment research-harness, then rh cloud attach web-job <task_id>")
        hand = text(dashboard.render(opened(live_view(), "web-job", detail=why), NOW, token=TOKEN))
        self.assertIn("The codex CLI on this Mac cannot see environment research-harness, so the harness cannot "
                      "start it.", hand)
        web = text(dashboard.render(opened(live_view(), "web-job", detail="web route: rh cloud prompt web-job, x"),
                                    NOW, token=TOKEN))
        self.assertIn("Cloud jobs here are set to start by hand (cloud.route is web).", web)

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
        self.assertIn('value="local-shell" selected', page)
        self.assertIn('value="net" checked', page)
        self.assertIn('value="45"', page)
        self.assertIn('value="python3 x.py"', page)
        # Without the script the command field must not block a cloud job's submit: the server refuses a local job
        # that has no command, so the browser must not require one.
        command = page[page.index('id="command"'):page.index('>', page.index('id="command"'))]
        self.assertNotIn("required", command)
        # Codex cloud is the default only once it is set up; before that a new job goes to Codex on this machine.
        def kind(page):
            return Forms(page).forms[[f["action"] for f in Forms(page).forms].index("/job")]["fields"]["kind"]
        self.assertEqual(kind(dashboard.render(view, NOW, token=TOKEN)), "local-agent")
        self.assertIn(f"Codex cloud{dashboard._SEP}setup required", text(dashboard.render(view, NOW, token=TOKEN)))
        self.assertEqual(kind(dashboard.render({**view, "cloud_ready": True}, NOW, token=TOKEN)), "codex-cloud")

    def test_new_job_has_one_labeled_creation_path(self):
        view = live_view()
        page = Page(dashboard.render(view, NOW, token=TOKEN))
        links = [attrs for tag, attrs in page.tags if tag == "a" and attrs.get("href", "").startswith("/new?")
                 and attrs.get("data-k") == "n"]
        self.assertEqual(len(links), 1)
        self.assertIn("New job", links[0].get("aria-label", ""))

    def test_job_details_stay_closed_until_opened(self):
        page = pane(dashboard.render(opened(live_view(), "live-job"), NOW, token=TOKEN))
        record = page.split('<aside class="jd-side"', 1)[1].split("</aside>", 1)[0]
        self.assertRegex(record, r'<details class="jd-inspector"><summary[^>]*>.*?Job details.*?</summary>')
        folding = record.split("</summary>", 1)[1].rsplit("</details>", 1)[0]
        self.assertIn("Properties", text(folding))
        self.assertIn("Timeline", text(folding))

    def test_an_agents_answer_reads_as_its_message(self):
        # An answer that is not shown as a message from the agent that ran, or that loses its text, must fail.
        view = opened(live_view(), "finished-job", kind="local-shell")
        view["open"]["job"]["spec"].update(kind="local-shell", answer="chat",
                                           command=["python3", "/x/rh/agent.py", "claude", "/bin/claude", "/repo", "go"])
        view["open"]["answer"] = "Fixed the <typo> in README.md."
        page = dashboard.render(view, NOW, token=TOKEN)
        message = re.search(r'<section[^>]*class="msg ai"[^>]*>.*?</section>', page, re.S).group(0)
        self.assertIn('data-prov="claude"', message)
        # one line of chrome: who answered, then how the run went at its far end; no status row above it
        self.assertTrue(text(message).startswith("ClaudeDone·1 min 20 s"))
        self.assertIn('<span class="who-status">', message)
        self.assertNotIn('data-region="job-result"', page)
        said = re.search(r'<div class="assistant-message md">(.*?)</div>', message, re.S).group(1)
        self.assertEqual(text(said), "Fixed the <typo> in README.md.")

    def test_an_answer_is_rendered_from_markdown_without_letting_html_through(self):
        # Raw ** and - on the page, a link to a local path that opens, or HTML from an answer reaching the page, must fail.
        html = dashboard._markdown("**I haven't confirmed it.** Nothing yet.\n\n- **Disk:** 154 GiB free\n"
                                   "- see [guide](https://developer.apple.com/x) and [notes](/private/tmp/n.md)\n\n"
                                   "`top` was blocked <script>x</script>\n\n```\nlog <b>\n```")
        self.assertEqual(html, '<p><strong>I haven&#x27;t confirmed it.</strong> Nothing yet.</p>'
                               '<ul><li><strong>Disk:</strong> 154 GiB free</li><li>see <a href="https://developer.apple.com/x" '
                               'target="_blank" rel="noopener noreferrer">guide</a> and <span class="md-path" '
                               'title="/private/tmp/n.md">notes</span></li></ul>'
                               '<p><code class="nw">top</code> was blocked &lt;script&gt;x&lt;/script&gt;</p>'
                               '<pre><code>log &lt;b&gt;</code></pre>')
        # long code wraps at its own seams, so an identifier never splits mid-word
        self.assertEqual(dashboard._md_inline("`test_rejects_an_accept_that_is_not_a_string in tests/x.py`"),
                         "<code>test_<wbr>rejects_<wbr>an_<wbr>accept_<wbr>that_<wbr>is_<wbr>not_<wbr>a_<wbr>string "
                         "in tests/<wbr>x.<wbr>py</code>")
        # a long identifier moves whole to the next line, breaking only if wider than the column
        self.assertEqual(dashboard._md_inline("`test_rejects_an_accept_that_is_not_a_string`"),
                         '<code class="tok">test_rejects_an_accept_that_is_not_a_string</code>')

    def test_a_code_block_in_an_answer_offers_to_copy_it(self):
        # A code block with no Copy button, a button that copies its own label with the code (so the id must
        # name the code element, not the pre), or a button a touch screen can never reveal, must fail.
        self.assertIn("className = 'code-copy'", dashboard.SCRIPT)
        self.assertIn("'rh:page', codeCopies", dashboard.SCRIPT)
        self.assertIn("b.dataset.copy = code.id", dashboard.SCRIPT)
        # the button lives in a wrapper beside the scrolling pre, or it drifts over the code when a long line scrolls
        self.assertIn("wrap.append(pre, b)", dashboard.SCRIPT)
        self.assertNotIn("pre.appendChild(b)", dashboard.SCRIPT)
        self.assertIn("position: relative",
                      dashboard.STYLE.split(".assistant-message.md .code-wrap {", 1)[1].split("}", 1)[0])
        rule = dashboard.STYLE.split(".code-copy {", 1)[1].split("}", 1)[0]
        self.assertIn("position: absolute", rule)
        self.assertIn(".code-wrap:hover .code-copy", dashboard.STYLE)
        self.assertIn(".code-wrap:focus-within .code-copy", dashboard.STYLE)
        self.assertIn("@media (pointer: coarse) { .code-copy", dashboard.STYLE)

    def test_an_agents_answer_offers_to_copy_it_as_written(self):
        # No Copy under the answer, a Copy outside its message, a button naming no held source, a held source
        # that is not the Markdown as written, a Copy the y key cannot find, or a control in the script-less
        # snapshot, must fail.
        view = opened(live_view(), "finished-job", kind="local-shell")
        view["open"]["job"]["spec"].update(kind="local-shell", answer="chat",
                                           command=["python3", "/x/rh/agent.py", "claude", "/bin/claude", "/repo", "go"])
        answer = "Fixed the **typo** & <tag>.\n\n- one\n- two"
        view["open"]["answer"] = answer
        page = dashboard.render(view, NOW, token=TOKEN)
        message = re.search(r'<section[^>]*class="msg ai"[^>]*>.*?</section>', page, re.S).group(0)
        self.assertEqual(message.count('class="msg-copy"'), 1)  # the brief's own Copy sits outside the message
        acts = re.search(r'<div class="msg-acts"><button type="button" class="msg-copy" data-copy="([^"]+)"'
                         r'([^>]*)>(.*?)</button></div>', message, re.S)
        self.assertIsNotNone(acts)
        self.assertIn('aria-label="Copy the answer"', acts.group(2))
        self.assertIn('data-k="y"', acts.group(2))  # the page's y key presses it
        self.assertIn('aria-keyshortcuts="y"', acts.group(2))
        self.assertIn("<svg", acts.group(3))
        self.assertIn('<span class="pc-t">Copy</span>', acts.group(3))
        self.assertEqual(acts.group(1), "ans-" + hashlib.sha1(answer.encode()).hexdigest()[:10])
        held = re.search(rf'<textarea hidden id="{re.escape(acts.group(1))}" readonly>(.*?)</textarea>',
                         message, re.S)
        self.assertIsNotNone(held)
        self.assertEqual(html.unescape(held.group(1)), answer)  # what a copy places on the clipboard
        # the snapshot has no script to run it, so it offers no control (its <style> still carries the rules)
        still = dashboard.render(view, NOW, result="x")
        self.assertNotIn('class="msg-copy"', still)
        self.assertNotIn('id="ans-', still)
        self.assertNotIn('data-k="y"', still)

    def test_copy_marks_the_label_so_the_button_keeps_its_glyph(self):
        # copy() that rewrites the whole button drops its icon: the .pc-t label takes 'Copied' when the button
        # has one, the button's own text otherwise — copyUrl's pattern, and the same 1600 ms restore.
        body = dashboard.SCRIPT[dashboard.SCRIPT.index("async function copy(b) {"):
                                dashboard.SCRIPT.index("async function copyUrl")]
        self.assertIn("const label = b.querySelector('.pc-t') || b;", body)
        self.assertIn("const was = b.dataset.was || label.textContent;", body)
        self.assertIn("label.textContent = done ? 'Copied' : 'Select it and copy';", body)
        self.assertIn("setTimeout(() => { label.textContent = was; b.classList.remove('shown'); }, 1600);", body)

    def test_the_answer_copy_is_the_peek_copy_revealed_with_its_message(self):
        # A Copy styled apart from the patch preview's, one a touch screen or an unhovered pointer cannot use,
        # or the folded-preview hiding rule reaching it, must fail.
        self.assertIn(".msg-acts { display: flex; margin: 4px 0 -12px -10px; }", dashboard.STYLE)
        rule = dashboard.STYLE.split(".msg-copy {", 1)[1].split("}", 1)[0]
        for spec in ("display: inline-flex", "height: 28px", "padding: 0 10px", "border-radius: 8px",
                     "color: var(--muted)", "font-size: 12px", "cursor: pointer", "opacity 150ms"):
            self.assertIn(spec, rule)
        self.assertIn(".msg-copy svg { width: 14px; height: 14px; }", dashboard.STYLE)
        self.assertIn(".msg-copy:hover, .msg-copy:focus-visible { color: var(--text); background: #1c2030; }",
                      dashboard.STYLE)
        self.assertIn("opacity: 0",
                      dashboard.STYLE.split("@media (hover: hover) {\n  .msg-copy {", 1)[1].split("}", 1)[0])
        self.assertIn(".msg.ai:is(:hover, :focus-within) .msg-copy { opacity: 1", dashboard.STYLE)
        hiding = dashboard.STYLE.split(".session-file:not(:has(> .file-preview[open])) .peek-copy", 1)[1]
        self.assertNotIn("msg-copy", hiding.split("}", 1)[0])

    def test_a_brief_offers_to_copy_it_as_written(self):
        # No Copy under the brief, a Copy outside its region, a button naming no held source, a held source that is
        # the reflowed text and not the brief as written, the controls inside the bubble or ahead of it (the
        # script's Show all must still land right after the bubble), or a control in the script-less snapshot,
        # must fail.
        view = opened(live_view(), "live-job", kind="local-shell")
        brief = ("Fix the **flaky** test & <it>.\nIts second line runs on.\n\n- first\n- second\n\n```\nkeep\nas\n```")
        view["open"]["job"]["spec"]["brief"] = brief
        page = pane(dashboard.render(view, NOW, token=TOKEN))
        region = re.search(r'<section class="s-j-brief".*?</section>', page, re.S).group(0)
        self.assertEqual(page.count('aria-label="Copy the brief"'), 1)
        acts = re.search(r'<div class="msg-acts me"><button type="button" class="msg-copy" data-copy="([^"]+)"'
                         r'([^>]*)>(.*?)</button></div>', region, re.S)
        self.assertIsNotNone(acts)
        self.assertIn('aria-label="Copy the brief"', acts.group(2))
        self.assertIn("<svg", acts.group(3))
        self.assertIn('<span class="pc-t">Copy</span>', acts.group(3))
        aid = "ask-" + hashlib.sha1(brief.encode()).hexdigest()[:10]
        self.assertEqual(acts.group(1), aid)
        held = re.search(rf'<textarea hidden id="{re.escape(aid)}" readonly>(.*?)</textarea>', region, re.S)
        self.assertIsNotNone(held)
        self.assertEqual(html.unescape(held.group(1)), brief)  # as written: hard-wrapped lines keep their breaks
        # the controls are the bubble's next siblings, so the script's Show all still inserts beside the bubble
        # (.user-message + .ask-more); inside it or ahead of it, the fold's adjacency would break
        self.assertIsNotNone(re.search(r'<div class="user-message"><span class="um-t">.*?</span></div>'
                                        r'<textarea hidden', region, re.S))
        self.assertIn('</textarea><div class="msg-acts me">', region)
        bubble = re.search(r'<div class="user-message">.*?</div>', region, re.S).group(0)
        self.assertNotIn("msg-copy", bubble)
        self.assertNotIn("<textarea", bubble)
        # the snapshot has no script to run it, so it offers no control (its <style> still carries the rules)
        still = dashboard.render(view, NOW, result="x")
        self.assertNotIn('aria-label="Copy the brief"', still)
        self.assertNotIn('id="ask-', still)
        self.assertNotIn('class="msg-acts me"', still)

    def test_each_earlier_ask_offers_to_copy_it(self):
        # A follow-up's chat shows each earlier ask with its own Copy inside an .ask-turn, so the reveal wakes on
        # that ask alone; asks sharing one control, a control naming another ask's text, or the current run's
        # brief left without one, must fail.
        view = live_view()
        view["recent"] += [{"id": "finished-job-r1", "state": "done", "paused": False,
                            "detail": "accepted output", "external_id": None},
                           {"id": "finished-job-r2", "state": "done", "paused": False,
                            "detail": "accepted output", "external_id": None}]
        view = opened(view, "finished-job-r2", kind="local-shell")
        view["open"]["job"]["spec"].update(brief="Tidy the README.", prior="finished-job-r1",
                                           changed="and the license")
        asks = ["Tidy the README.\nIts second line runs on.", "now the install part"]
        view["open"]["thread"] = [
            {"id": "finished-job", "state": "done", "ask": asks[0], "answer": "Tidied it.",
             "provider": "claude", "model": "claude-sonnet-5-5"},
            {"id": "finished-job-r1", "state": "done", "ask": asks[1], "answer": "",
             "provider": "", "model": ""}]
        view["open"]["answer"] = "All tidied."
        page = pane(dashboard.render(view, NOW, token=TOKEN))
        thread = re.search(r'<section class="s-j-thread".*?</section>', page, re.S).group(0)
        # the page's y key presses the latest answer's Copy alone: an earlier turn's answer offers Copy too, but
        # none of the thread's copies may carry the key
        self.assertIn('data-copy="ans-', thread)
        self.assertNotIn('data-k="', thread)
        self.assertEqual(page.count('data-k="y"'), 1)
        turns = re.findall(r'<div class="ask-turn">(.*?)</div></div>', thread, re.S)
        self.assertEqual(len(turns), len(asks))
        self.assertEqual(page.count('aria-label="Copy the brief"'), len(asks) + 1)  # plus this run's own brief
        for block, ask in zip(turns, asks):
            self.assertEqual(block.count('aria-label="Copy the brief"'), 1)
            aid = "ask-" + hashlib.sha1(ask.encode()).hexdigest()[:10]
            held = re.search(rf'<textarea hidden id="{re.escape(aid)}" readonly>(.*?)</textarea>', block, re.S)
            self.assertIsNotNone(held)
            self.assertEqual(html.unescape(held.group(1)), ask)
            self.assertIn(f'data-copy="{aid}"', block)
            self.assertIsNotNone(re.search(r'<div class="user-message"><span class="um-t">.*?</span></div>'
                                            r'<textarea hidden', block, re.S))
        brief = re.search(r'<section class="s-j-brief".*?</section>', page, re.S).group(0)
        self.assertEqual(brief.count('aria-label="Copy the brief"'), 1)
        # the snapshot wraps nothing and offers nothing
        still = dashboard.render(view, NOW, result="x")
        self.assertNotIn('aria-label="Copy the brief"', still)
        self.assertNotIn('class="ask-turn"', still)
        self.assertNotIn('id="ask-', still)
        self.assertNotIn('data-k="y"', still)

    def test_the_brief_copy_is_revealed_with_its_own_bubble(self):
        # A brief Copy restyled apart from the answer's, hidden where a hover-less pointer or an unfocused tab
        # cannot reach it, revealed by hovering the whole thread instead of its own ask's turn, or a .msg-copy
        # rule the brief row does not share, must fail.
        self.assertIn(".msg-acts.me { justify-content: flex-end; margin: 4px -10px -12px 0; }", dashboard.STYLE)
        media = dashboard.STYLE.split("@media (hover: hover) {\n  .msg-copy {", 1)[1].split("\n}", 1)[0]
        for scope in (".s-j-brief", ".ask-turn"):
            self.assertIn(f"{scope}:is(:hover, :focus-within) .msg-copy {{ opacity: 1;", media)
        # with a pointer the answer's Copy leaves its row — a brief's keeps it, so the absolute row is the
        # answer's alone; unscoped it would pin a brief's Copy to the page, not the bubble
        self.assertIn(".msg.ai .msg-acts { position: absolute;", media)
        self.assertNotIn("\n  .msg-acts { position: absolute", media)
        # and a brief's row takes no height there either, so the brief sits as close to what follows as before
        self.assertIn(".msg-acts.me { height: 0; margin: 0 -10px 0 0;", media)

    def test_the_latest_answers_copy_alone_carries_the_y_key(self):
        # The 'y' key copies the latest answer: its Copy alone carries data-k/aria-keyshortcuts — an earlier turn's
        # Copy, the brief's, or a second y control, and pressing y finds nothing or the wrong text, must fail.
        view = live_view()
        view["recent"] += [{"id": "finished-job-r1", "state": "done", "paused": False,
                            "detail": "accepted output", "external_id": None}]
        view = opened(view, "finished-job", kind="local-shell")
        view["open"]["job"]["spec"].update(kind="local-shell", answer="chat",
                                           command=["python3", "/x/rh/agent.py", "claude", "/bin/claude", "/repo", "go"])
        view["open"]["thread"] = [{"id": "finished-job-r1", "state": "done", "ask": "Tidy the README.",
                                   "answer": "Tidied it.", "provider": "claude", "model": "claude-sonnet-5-5"}]
        view["open"]["answer"] = "Fixed the **typo**."
        page = pane(dashboard.render(view, NOW, token=TOKEN))
        self.assertEqual(page.count('data-k="y"'), 1)
        self.assertEqual(page.count('aria-keyshortcuts="y"'), 1)
        latest = re.search(r'<section class="msg ai" data-region="j-answer".*?</section>', page, re.S).group(0)
        self.assertIn('class="msg-copy" data-copy="ans-', latest)
        self.assertIn('data-k="y"', latest)
        self.assertIn('aria-keyshortcuts="y"', latest)
        for region in ("j-thread", "j-brief"):
            block = re.search(rf'<section class="[^"]*" data-region="{region}".*?</section>', page, re.S).group(0)
            self.assertIn('class="msg-copy"', block)  # each still offers its own Copy
            self.assertNotIn('data-k="', block)
            self.assertNotIn("aria-keyshortcuts", block)
        # the sheet lists it, and the palette offers it on this page because a [data-k="y"] control exists
        self.assertIn("<dt><kbd>y</kbd></dt><dd>Copy the answer</dd>", page)
        pal = re.search(r'<dialog class="pal".*?</dialog>', page, re.S).group(0)
        self.assertIn('data-press="y"', pal)
        self.assertIn("Copy the answer", pal)
        # a job page with no answer has no [data-k="y"] control, so the palette never offers the press
        pal = re.search(r'<dialog class="pal".*?</dialog>',
                        pane(dashboard.render(opened(live_view(), "finished-job", kind="local-shell"),
                                              NOW, token=TOKEN)), re.S).group(0)
        self.assertNotIn('data-press="y"', pal)

    def test_y_copies_the_answer_from_the_keyboard(self):
        # 'y' missing from the single-key regex, a press that only focuses the Copy (activate() never clicks, so
        # copy() would not run), copy() not holding the button visible while it says Copied, no .msg-copy.shown
        # rule beating the hover-hidden one, or _KEYS/_PRESS missing what the sheet and palette list, must fail.
        single = re.search(r"/\^\[([a-z]+)\]\$/\.test\(e\.key\)", dashboard.SCRIPT)
        self.assertIsNotNone(single)
        self.assertIn("y", single.group(1))
        branch = dashboard.SCRIPT.split("act(e.key)) {", 1)[1].split("else return;", 1)[0]
        self.assertIn("c.hasAttribute('data-copy')", branch)
        self.assertIn("c.click()", branch)
        copy_body = dashboard.SCRIPT[dashboard.SCRIPT.index("async function copy(b) {"):
                                 dashboard.SCRIPT.index("async function copyUrl")]
        self.assertIn("b.classList.add('shown')", copy_body)
        self.assertIn("setTimeout(() => { label.textContent = was; b.classList.remove('shown'); }, 1600);",
                      copy_body)
        hover = dashboard.STYLE.split("@media (hover: hover) {\n  .msg-copy { opacity: 0; }", 1)[1]
        self.assertIn(".msg-copy.shown { opacity: 1; }", hover.split("\n}", 1)[0])
        self.assertIn(("y", "Copy the answer"), dashboard._KEYS)
        self.assertIn(("y", "Copy the answer", "its Markdown, as written"), dashboard._PRESS)
        keys = [k for k, _ in dashboard._KEYS]
        self.assertEqual(keys[keys.index("l") + 1], "y")

    def test_a_composer_keeps_its_draft_across_pages(self):
        # A reply typed on one job and left for another came back empty, since the page swap renders a new form
        # (T3 Code keeps each thread's draft). Each composer saves its words per page and box as they are typed,
        # finds them again when the page comes back with the box empty, and drops them when sent; a draft that
        # reappeared after sending, or one shared between jobs, must fail.
        body = dashboard.SCRIPT.split("function draftKey(", 1)[1].split("\nsetTimeout(restoreDrafts, 0);", 1)[0]
        self.assertIn("'rh-draft:' + location.pathname + '#' + t.id", body)
        self.assertIn("localStorage.setItem(draftKey(t), v)", body)
        self.assertIn("localStorage.removeItem(draftKey(t))", body)
        self.assertIn("document.addEventListener('rh:page', restoreDrafts);", body)
        self.assertIn("if (!t.id || t.value) continue;", body)  # never over words already in the box
        # sending clears it before the page swaps, so the fresh empty box is not refilled with what was sent
        self.assertIn("document.addEventListener('submit'", body)
        self.assertRegex(body, r"addEventListener\('submit', e => \{[^}]*removeItem", )
        # r18 F1: a restored '/swarm …' draft stayed plain text beside the single-model pickers, because the
        # first restore ran before the slash listener existed, and a draft saved with the chip on lost the command.
        # The draft keeps '/swarm ' in front while the chip is on, taking the chip off saves again, and the first
        # restore waits until every listener is in place.
        self.assertIn("(t.form && t.form.dataset.swarm ? '/swarm ' : '') + t.value", body)
        self.assertNotIn("\nrestoreDrafts();", dashboard.SCRIPT)
        self.assertIn("setTimeout(restoreDrafts, 0);", dashboard.SCRIPT)
        off = dashboard.SCRIPT.split("function swarmOff(box) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("brief.dispatchEvent(new Event('input', { bubbles: true }))", off)
        # every composer box has a stable id to key it by
        page = dashboard.render(opened(live_view(), "finished-job"), NOW, token=TOKEN)
        new = dashboard.render({**live_view(), "new": True}, NOW, token=TOKEN)
        for box in re.findall(r'<textarea class="c-text"[^>]*>', page + new):
            self.assertRegex(box, r' id="[^"]+"')

    def test_the_reply_box_under_an_agent_picks_the_next_runs_model(self):
        # A reply box without the model picker, one that offers routes the job cannot take, or Devin models without
        # their section and cost, must fail.
        view = opened(live_view(), "finished-job", kind="local-shell")
        view["open"]["job"]["spec"].update(kind="local-shell", command=[
            "python3", "/x/rh/agent.py", "devin", "/bin/devin", "/repo", "go", "--model", "swe-2-high"])
        view["job_policy"] = {"devin-local": {"models": ["swe-2-high", FUSION]},
                              "claude-local": {"models": ["claude-opus-5-5"], "efforts": ["high"]}}
        page = dashboard.render(view, NOW, token=TOKEN)
        reply = re.search(r'<form class="compose[^>]*action="/again".*?</form>', page, re.S).group(0)
        self.assertEqual(re.findall(r'data-route="([^"]+)" name="model"', reply), ["local-agent"])
        self.assertIn('<option value="swe-2-high" data-prov="devin"', reply)
        self.assertIn('data-group="SWE-2 · Free"', reply)  # said once in the section, not as a chip on each row
        self.assertNotIn('data-cost="Free"', reply)
        self.assertIn('data-group="Fusion"', reply)
        self.assertIn('data-row="Fable 5.2 Medium + SWE-2 High" data-cost="API"', reply)
        self.assertRegex(reply, r'<option value="swe-2-high"[^>]* selected>')

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
        # r17 F3: when every check passed the line drops its count (the ring says done, the panel lists each)
        passed = copy.deepcopy(view)
        passed["open"]["receipt"]["checks"] = [{"cmd": "test -s notes.md", "exit": 0}]
        line = re.search(r'<span class="job-result-status sl">.*?</span></span>',
                         dashboard.render(passed, NOW, token=TOKEN), re.S).group(0)
        self.assertNotIn("checks", text(line))
        self.assertIn("Done", text(line))
        # r17 F1, F2: the phone toast sits inside the 16px gutters and a success leaves after 4 s; on touch the
        # answer's Copy rests on its meta line and a brief offers none
        self.assertIn("@media (max-width: 600px) { .toasts { left: 16px; right: 16px; width: auto;", dashboard.STYLE)
        self.assertIn("setTimeout(() => drop(t), 4000)", dashboard.SCRIPT)
        touch = dashboard.STYLE.split("@media (hover: none) {\n  .msg-acts", 1)[1].split("\n}", 1)[0]
        self.assertIn(".msg.ai .msg-acts { position: absolute; top: -1px; right: -7px; margin: 0; }", touch)
        self.assertIn(".msg-acts.me { display: none; }", touch)
        # r17 F5-F7: the Inbox Show all ends on the content edge; an open patch says Hide and its Show all sits
        # right; an open Options is a pressed rounded square, not the send disc's shape
        self.assertNotIn("max-width: 680px", dashboard.STYLE.split(".q-head-in:has(> .ask-more) {", 1)[1].split("}", 1)[0])
        self.assertIn('.session-file > .file-preview[open] > summary::after { content: "Hide";', dashboard.STYLE)
        self.assertIn("text-align: right", dashboard.STYLE.split("\npre.peek.diff + .ask-more {", 1)[1].split("}", 1)[0])
        self.assertIn(".opts[open] > .opts-btn { border-radius: 8px; }", dashboard.STYLE)
        for value in ("Write the notes", "Done", "2/3 checks", "1 min 20 s", "finished-job", dashboard.HERE,
                      "exit 0", "test -s notes.md", "exit 1", "Last output", "<tail> & end", "Cite each source.",
                      "notes.md exists", "runs/finished-job/", "120 min", "Timeline", "Added to run in Codex cloud", "Worker output",
                      f"Asked you: {ASK.capitalize()}{dashboard._clock(NOW - 31, NOW)}",
                      f"Mystery thing: raw zz{dashboard._clock(NOW - 5, NOW)}",
                      "2.0 KB", "sub dir/a&b.txt", "# Notes <draft>"):
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
        work = work.split('>', 1)[1]
        for value in ("Write the notes", "Cite each source.", "<tail> & end", "# Notes <draft>", "2.0 KB"):
            self.assertIn(value, text(work))
        for value in ("Properties", "Receipt", "exit 1", "Timeline", "Worker output", "finished-job"):
            self.assertIn(value, text(record))
            self.assertNotIn(value, text(work))
        # Pauses and stops show only when there are some; the Codex task id is not shown.
        self.assertNotIn("Pauses and stops", text(record))
        paused = pane(dashboard.render(opened(live_view(), "live-job"), NOW, token=TOKEN))
        self.assertIn("Paused by lead:", text(paused.split('<aside class="jd-side"', 1)[1]))
        props = re.search(r'<section class="j-props".*?</section>', paused, re.S).group(0)
        self.assertIn("Runs on", text(props))
        self.assertNotIn("worker-123", text(paused))
        self.assertNotIn("Codex task", text(props))
        # A held job's page says what to do; a job the harness does not know says so, not a crash.
        held = text(pane(dashboard.render(opened(live_view(), "queued-job"), NOW, token=TOKEN)))
        self.assertIn("On hold: Waiting for input", held)
        self.assertIn("It is rechecked on every pass.", held)
        missing = dashboard.render({**live_view(), "open": {"id": "ghost", "job": None}}, NOW, token=TOKEN)
        self.assertIn("No job named ghost", text(missing))
        self.assertIn('<a class="p-row" role="option" data-id="live-job" href="/job/live-job?t=', missing)  # ⌘K finds every job
        # A one-sentence brief remains visible as the original request.
        single = opened(live_view(), "live-job")
        single["open"]["job"]["spec"]["brief"] = "Count the words."
        self.assertIn('<div class=\"user-message\"><span class=\"um-t\">Count the words.</span></div>', pane(dashboard.render(single, NOW, token=TOKEN)))
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
                                   ("'.compose'", 'class="compose composer"'), ("'.side .jobs'", 'class="jobs lane"'),
                                   ("a.ib", '<a class="ib'), ("'.head'", 'class="head"'), ("'.body'", 'class="body"'),
                                   ("[data-copy]", " data-copy="), ("dataset.short", " data-short="),
                                   ("'pace'", " data-pace="), ("'jobs'", " data-jobs="),
                                   (".s-g-live", 'class="s-g-live group"'), (".no-jobs", 'class="empty no-jobs"'),
                                   ("dataset.say", " data-say="), ("#pal", 'id="pal"'), ("[data-pal]", " data-pal"),
                                   ("dataset.press", " data-press="), ("[data-tank]", " data-tank"),
                                   ("dataset.creatures", " data-creatures="),
                                   ("'data-still'", " data-still"),
                                   ("'.ibx'", 'class="page ibx"'), ("[data-job-filter]", " data-job-filter "),
                                   ("dataset.kind", " data-kind=")):  # '.n' bumps a count only where one shows
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
        view["open"]["answer"] = "Fixed it."  # a markdown answer, so .assistant-message.md renders
        view["open"]["files"] = [{"path": "changes.patch", "size": 90}]  # and a patch preview, so .peek.diff does
        view["open"]["peek"] = {"path": "changes.patch", "text": "+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n", "more": False}
        # and a swarm's judge page, so its run rows (.sw-run, .sw-a) render
        swarm = live_view()
        swarm["not_started"] = [{"id": "fix.best", "state": "waiting", "paused": False, "detail": "", "external_id": None}]
        swarm["recent"] = [*swarm["recent"], {"id": "fix.w1", "state": "done", "paused": False, "detail": "",
                                               "external_id": None}]
        base = {"brief": "Fix the typo", "kind": "local-shell", "created": NOW - 300, "started": NOW - 290}
        swarm["meta"] = {**swarm.get("meta", {}),
                         "fix.w1": {**base, "updated": NOW - 230, "model": "swe-2-max", "goal": "fix", "said": "Renamed"},
                         "fix.best": {**base, "updated": NOW - 5, "model": "swe-2-max", "goal": "fix", "after": ["fix.w1"]}}
        pages = (dashboard.render(live_view(), NOW, token=TOKEN) + dashboard.render(view, NOW, token=TOKEN)
                 + dashboard.render({**live_view(), "new": True, "swarm": {"models": ["swe-2-max"], "judge": "swe-2-max",
                                    "choices": ["swe-2-max"]}}, NOW, token=TOKEN)  # and the composer's swarm popover
                 + inbox(live_view())
                 + dashboard.render(opened(swarm, "fix.best", kind="local-shell", state="waiting"), NOW, token=TOKEN))
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
        self.assertIn("Failed: the command exited with code 2", item(page, "broke-job"))
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

    def test_a_failure_already_run_again_gets_a_quieter_ring(self):
        # A rerun failure still drawn in the full red of one that needs you, a failure nobody ran again drawn
        # quieter, or the quiet rule missing from the stylesheet, must fail.
        view = live_view()
        view["meta"] = {"broke-job": {"updated": NOW - 60}}
        row = item(dashboard.render(view, NOW, token=TOKEN), "broke-job")
        self.assertIn('class="st t-red"', row)
        self.assertNotIn("past", row)
        rerun = {**view, "meta": {**view["meta"], "broke-job-r2": {"prior": "broke-job"}}}
        page = dashboard.render(rerun, NOW, token=TOKEN)
        self.assertIn('class="st t-red past"', item(page, "broke-job"))
        pal = re.search(r'<dialog class="pal".*?</dialog>', page, re.S).group(0)
        ring = re.search(r'href="/job/broke-job\?t=[^"]*" data-nav><span class="p-ic">(.*?)</span>',
                         pal, re.S).group(1)
        self.assertIn('class="st t-red past"', ring)
        self.assertRegex(dashboard.STYLE, r"\.st\.past \{[^}]*opacity: \.45")

    def test_one_filled_button_per_page_and_destructive_actions_marked(self):
        # A page with no filled button or with two, or a pause or cancel styled like a safe action, must fail.
        home = pane(dashboard.render(live_view(), NOW, token=TOKEN))
        self.assertEqual(len(primaries(home)), 1)
        self.assertIn('<button class="btn primary send" aria-label="Start job" title="Start job">', home)
        for jid in ("live-job", "web-job", "finished-job", "broke-job"):
            with self.subTest(job=jid):
                right = pane(dashboard.render(opened(live_view(), jid), NOW, token=TOKEN))
                self.assertEqual(len(primaries(right)), 1)
        broke = pane(dashboard.render(opened(live_view(), "broke-job"), NOW, token=TOKEN))
        self.assertIn('aria-label="Run again" title="Run again" data-followup>', broke)
        # the quiet pill carries the Inbox Run again button's glyph before its word (critic r15 F6)
        pill = broke.split('data-followup>', 1)[1].split("</button>", 1)[0]
        self.assertIn('<svg class="ic sm again"', pill)
        self.assertIn(dashboard._ICONS["again"], pill)
        self.assertIn("order: -1; width: 12px; height: 12px;", dashboard.STYLE)
        # an added line's code keeps its ink; a 2px edge carries the green (critic r15 F8)
        diff = dashboard.STYLE.split("\n.peek .dl.add {", 1)[1].split("}", 1)[0]
        self.assertIn("color: var(--text)", diff)
        self.assertIn("box-shadow: inset 2px 0 #3fb950", diff)
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
        for page in pages:
            for tag, attrs in Page(page).tags:
                if "title" in attrs and tag in {"button", "select"}:
                    self.assertTrue(attrs.get("aria-label"), attrs)

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

    def test_a_past_its_limit_ring_is_the_running_half_in_a_dashed_ring(self):
        # The clock badge a late job once wore blurred into a blot at 14px: a badge element, a solid ring or a late
        # shape without the running half must fail.
        late = dashboard._SHAPES["late"]
        self.assertIn('d="M7 7V3.9A3.1 3.1 0 0 1 7 10.1Z" fill="currentColor"', late)
        self.assertIn('stroke-dasharray="2.1 2.3"', late)
        self.assertNotIn('cx="11"', late)
        self.assertNotIn("stroke-dasharray", dashboard._SHAPES["half"])

    def test_each_seat_is_a_jellyfish_in_the_colour_of_what_it_is_doing(self):
        # A seat drawn teal while its job waits on you or amber while it works, a seat left out of the sea or out of
        # the list a screen reader gets, a role still wearing its label ("Testing: ..."), a tank in the snapshot (which
        # runs no script), or a tank that ignores reduced motion, must fail.
        view = live_view()
        view["seats"] = [{"name": name, "role": f"Seat: the {name} seat", "route": "codex-cloud", "model": "m-1"}
                         for name in ("asks", "works", "rests")]
        view["live"].append({"id": "busy-job", "state": "running", "paused": False, "detail": "", "external_id": None})
        view["meta"] = {"live-job": {"seat": "asks", "brief": "Tally the slow tests."}, "busy-job": {"seat": "works"}}
        page = dashboard.render(view, NOW, token=TOKEN)
        found = re.search(r'<canvas class="tank-c" data-tank data-creatures="([^"]*)"', page)
        tank = {c["name"]: c for c in json.loads(html.unescape(found.group(1)))}
        self.assertEqual({name: c["tone"] for name, c in tank.items()},
                         {"asks": "amber", "works": "accent", "rests": "faint"})
        reef = re.search(r'<aside class="reef".*?</aside>', page, re.S).group(0)
        said = re.findall(r"<li>(.*?)</li>", re.search(r'<ul class="sr">.*?</ul>', reef, re.S).group(0))
        self.assertEqual(said, ["asks, waiting on you on live-job: The asks seat", "works, working on busy-job: The works "
                                "seat", "rests, idle: The rests seat"])
        self.assertNotIn("Tally the slow tests", reef)
        self.assertNotIn("<canvas", dashboard.render(view, NOW))
        self.assertIn("no seats.toml", text(dashboard.render(live_view(), NOW, token=TOKEN)))
        # Under reduced motion the tank draws one still frame and never starts its animation loop.
        tank_code = dashboard.SCRIPT[dashboard.SCRIPT.index("function Tank("):]
        tank_code = tank_code[:tank_code.index("\n}\n")]
        self.assertIn("if (calm.matches) { draw(", tank_code)
        self.assertLess(tank_code.index("if (calm.matches) { draw("), tank_code.index("requestAnimationFrame("))

    def test_the_sea_life_counts_what_the_page_shows(self):
        # A fish, school, bacteria or plankton count that drifts from the jobs it stands for, an eel that swims while
        # checks are paused or stale, an octopus that stays calm while something needs you, a harness item without
        # its octopus, or a tank that keeps drawing in a hidden tab or past about 20 frames a second, must fail.
        def life(view):
            page = dashboard.render(view, NOW, token=TOKEN)
            found = re.search(r'<canvas class="tank-c" data-tank data-creatures="[^"]*" data-fauna="([^"]*)" '
                              r'data-region="water"', page)
            return json.loads(html.unescape(found.group(1))), page
        view = live_view()
        view["recent"] += [{"id": f"ok-{n}", "state": "done", "paused": False, "detail": "", "external_id": None}
                           for n in range(3)]
        view["not_started"].append({"id": "next-job", "state": "ready", "paused": False, "detail": "waiting",
                                    "external_id": None})
        view["live"].append({"id": "swim-job", "state": "running", "paused": False, "detail": "", "external_id": None})
        got, page = life(view)
        live = re.search(r'<section class="s-g-live group".*?</section>', page, re.S).group(0)
        self.assertEqual(got["fish"], live.count('<li class="row"'))
        self.assertGreater(got["fish"], 0)
        self.assertEqual((got["school"], got["bacteria"], got["plankton"]), (4, 1, 1))  # finished-job + 3; broke-job
        self.assertTrue(got["eel"])
        self.assertEqual(got["octopus"]["tone"], "amber")
        self.assertTrue(got["floor"])
        self.assertFalse(life({**view, "checks": {"mode": "paused", "pid": None, "failed": None}})[0]["eel"])
        self.assertFalse(life({**view, "supervisor": {"pid": 1, "age_s": dashboard._STALE + 1}})[0]["eel"])
        calm = {**full_view(), "can_add": True, "inbox": [], "stops": [], "recent": [], "not_started": [], "live": []}
        quiet = life(calm)[0]
        self.assertEqual((quiet["fish"], quiet["school"], quiet["bacteria"], quiet["octopus"]["tone"]),
                         (0, 0, 0, "faint"))
        crowd = {**view, "recent": [{"id": f"d-{n}", "state": "done", "paused": False, "detail": "",
                                     "external_id": None} for n in range(40)]}
        self.assertEqual(life(crowd)[0]["school"], dashboard._SCHOOL)
        # The harness's own Inbox item is the octopus's; an item on a job keeps its jellyfish.
        alone = {**view, "inbox": view["inbox"] + [UNSEEN]}
        mine = re.search(r'<div class="q-head"><canvas[^>]*>', inbox(alone, 10)).group(0)
        self.assertIn("&quot;octopus&quot;", mine)
        self.assertNotIn("data-fauna", re.search(r'<div class="q-head"><canvas[^>]*>', inbox(alone, 7)).group(0))
        tank_code = dashboard.SCRIPT[dashboard.SCRIPT.index("function Tank("):]
        tank_code = tank_code[:tank_code.index("\n}\n")]
        self.assertGreaterEqual(int(re.search(r"const FRAME = (\d+);", dashboard.SCRIPT).group(1)), 40)
        self.assertIn("if (now - last >= FRAME)", tank_code)
        loop = tank_code[tank_code.index("const loop = now =>"):]
        self.assertLess(loop.index("if (document.hidden || !seen) return;"), loop.index("requestAnimationFrame(loop)"))
        self.assertIn("document.addEventListener('visibilitychange', go)", tank_code)
        self.assertIn("new IntersectionObserver", tank_code)
        # The water is plain: no grid behind the creatures.
        self.assertNotIn("background", dashboard.STYLE.split(".tank-c {", 1)[1].split("}", 1)[0])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_sea_draws_each_creature_whole(self):
        # A creature missing from the sea it is counted in, a dot or sprite cut by the tank's edge, two seats'
        # jellyfish on top of each other, a dot map whose rows differ in length, or a sea that draws the same dots at
        # every moment, must fail. The sea's drawing code touches no page, so node runs it as the browser would.
        block = dashboard.SCRIPT[dashboard.SCRIPT.index("// <sea>"):dashboard.SCRIPT.index("// </sea>")]
        tank = dashboard.SCRIPT[dashboard.SCRIPT.index("function Tank(canvas)"):dashboard.SCRIPT.index("let fonts = false;")]
        probe = block + tank + """
const spec = ['backend', 'cli', 'app', 'testing', 'design', 'lead'].map((name, k) =>
  ({ name, tone: ['faint', 'accent', 'amber'][k % 3], fx: 0.5, fy: 0.5, sp: 1 }));
const fauna = { fish: 3, school: 5, bacteria: 2, plankton: 2, eel: true, octopus: { tone: 'amber' }, floor: true };
const out = [];
for (const [W, H] of [[320, 760], [320, 1100], [860, 112], [390, 112]]) {
  const { cols, rows } = seaGrid(W, H, spec.length, true);
  for (const t of [0, 2.7, 9.4, 31.1, 77.7]) {
    const { dots, parts } = seaDots(spec, fauna, cols, rows, t);
    // a dot's weight reaches the cell after the one it is in, so the last cell it may stand in is cols - 1
    const outside = dots.filter(d => d[2] !== FINE && (d[0] < 0 || d[1] < 0 || d[0] > cols - 1 || d[1] > rows - 1)).length;
    const red = dots.filter(d => d[3] === 'red' && d[2] !== FINE).length;
    out.push({ W, H, cols, rows, t, parts, outside, red, first: dots.slice(0, 60) });
  }
}
// Dock rectangles are in lattice cells, including a fractional top edge from the page layout.
const docked = [[800, 550, [3, 28.4, 110, 41]], [1120, 810, [3, 47.6, 155, 61]]].map(([W, H, dock]) => {
  const { cols, rows } = seaGrid(W, H, spec.length, true);
  return { cols, rows, dock, octopus: seaDots(spec, fauna, cols, rows, 0, dock).parts.find(p => p.kind === 'octopus').box };
});
// Representative sea-page tanks at both requested viewport sizes, across animation frames.
const crowded = [];
for (const [W, H, dockTop] of [[390, 560, 417], [390, 577, 434], [806, 837, 690]]) {
  const { P, Q, cols, rows } = seaGrid(W, H, spec.length, true);
  const dock = [20 / P, dockTop / Q, (W - 20) / P, (H - 18) / Q];
  for (const t of [0, 2.7, 9.4, 31.1, 77.7]) {
    const { dots, parts, labels } = seaDots(spec, fauna, cols, rows, t, dock);
    const reference = seaDots(spec, fauna, cols, rows, t);
    const outside = dots.filter(d => d[2] !== FINE && (d[0] < 0 || d[1] < 0 || d[0] > cols - 1 || d[1] > rows - 1)).length;
    crowded.push({ W, H, t, cols, rows, dock, parts, labels, outside, reference: { parts: reference.parts, labels: reference.labels } });
  }
}
const one = seaDots([spec[0]], null, 40, 20, 3);
// a floor whose last column grows a weed: its sway must stay inside the tank
const edge = Array.from({ length: 400 }, (_, c) => c).find(c => c > 40 && hash(c - 1, 3, 17) > 0.93);
let weeds = 0;
for (const t of [0, 1.3, 2.6, 3.9]) {
  weeds += seaFloor([], edge, 40, t, [], []).dots.filter(d => d[0] < 0 || d[0] > edge - 1).length;
}
// Exercise the real tank's conversion from the dock's pixel rectangle to lattice cells.
let rendered, draws = 0;
const realSeaDots = seaDots;
seaDots = (...args) => {
  draws++;
  const frame = realSeaDots(...args), { P, Q, cols, rows } = seaGrid(600, 500, spec.length, true);
  rendered = { dock: args[5], actualDock: [20 / P, (dockTop - 100) / Q, 580 / P, 450 / Q], cols, rows,
    octopus: frame.parts.find(p => p.kind === 'octopus').box };
  return frame;
};
let dockTop = 420, onDockResize;
const dockElement = { getBoundingClientRect: () => ({ left: 120, top: dockTop, right: 680, bottom: 550 }) };
const host = { parentElement: { querySelector: selector => selector === '.dock' ? dockElement : null }, getBoundingClientRect: () => ({ left: 100, top: 100 }) };
const ctx = { setTransform() {}, clearRect() {}, fillRect() {}, fillText() {}, beginPath() {}, moveTo() {}, arc() {}, fill() {} };
const canvas = { dataset: { creatures: JSON.stringify(spec), fauna: JSON.stringify(fauna) }, parentElement: host,
  isConnected: true, getContext: () => ctx, getBoundingClientRect: () => ({ left: 100, top: 100, width: 600, height: 500 }) };
const ResizeObserver = class { constructor(callback) { onDockResize = callback; } observe() {} disconnect() {} };
const window = { devicePixelRatio: 1, ResizeObserver }, document = { documentElement: {} };
const calm = { matches: true }, light = { addEventListener() {} };
const getComputedStyle = () => ({ getPropertyValue: () => '0 0 0' });
const addEventListener = () => {}, $$ = () => [];
Tank(canvas);
const initial = rendered;
dockTop = 300;
onDockResize();
console.log(JSON.stringify({ out, docked, crowded, initial, rendered, draws, maps: { FISH, MINNOW, OCTOPUS }, alone: one.parts, weeds }));
"""
        r = subprocess.run(["node", "-e", probe], capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout)
        for name, rows in got["maps"].items():
            self.assertEqual(len({len(row) for row in rows}), 1, name)
        self.assertEqual([p["kind"] for p in got["alone"]], ["jelly"])  # a job's head: its seat, nothing else
        self.assertEqual(got["weeds"], 0)
        self.assertIsNotNone(got["initial"]["dock"])
        for frame in (got["initial"], got["rendered"]):
            for actual, expected in zip(frame["dock"], frame["actualDock"]):
                self.assertAlmostEqual(actual, expected)
        self.assertEqual(got["draws"], 2)
        self.assertLess(got["rendered"]["dock"][1], got["initial"]["dock"][1])
        for frame in [*got["docked"], got["initial"], got["rendered"]]:
            x0, y0, x1, y1 = frame["octopus"]
            dx0, dy0, dx1, dy1 = frame["dock"]
            if "cols" in frame:
                self.assertTrue(0 <= x0 < x1 <= frame["cols"] and 0 <= y0 < y1 <= frame["rows"])
            self.assertFalse(x0 < dx1 and dx0 < x1 and y0 < dy1 and dy0 < y1, frame)
        positions = {}
        for frame in got["crowded"]:
            self.assertEqual(frame["outside"], 0)
            self.assertEqual(frame["labels"], frame["reference"]["labels"])
            self.assertEqual([p for p in frame["parts"] if p["kind"] != "octopus"],
                             [p for p in frame["reference"]["parts"] if p["kind"] != "octopus"])
            octopus = next(part["box"] for part in frame["parts"] if part["kind"] == "octopus")
            size = (frame["W"], frame["H"])
            self.assertEqual(positions.setdefault(size, octopus), octopus)
            boxes = [frame["dock"], *(part["box"] for part in frame["parts"] if part["kind"] == "jelly")]
            boxes += [[x - len(name) * 1.5, y - 1, x + len(name) * 1.5, y + 1]
                      for x, y, name, _ in frame["labels"]]
            x0, y0, x1, y1 = octopus
            self.assertTrue(0 <= x0 < x1 <= frame["cols"] and 0 <= y0 < y1 <= frame["rows"], frame)
            for bx0, by0, bx1, by1 in boxes:
                self.assertFalse(x0 < bx1 and bx0 < x1 and y0 < by1 and by0 < y1, (frame["W"], frame["H"], frame["t"], octopus, [bx0, by0, bx1, by1]))
        for frame in got["out"]:
            kinds = [p["kind"] for p in frame["parts"]]
            with self.subTest(size=(frame["W"], frame["H"]), t=frame["t"]):
                self.assertEqual(frame["outside"], 0)
                self.assertEqual((kinds.count("jelly"), kinds.count("octopus"), kinds.count("plankton")), (6, 1, 2))
                if frame["rows"] > 30:  # the reef has the whole sea; a strip, the seats and the octopus
                    self.assertEqual([kinds.count(k) for k in ("fish", "minnow", "bacteria", "eel")], [3, 5, 2, 1])
                    # Big enough to read as animals: a fish 16 cells long, a colony at least 8 lit dots.
                    self.assertTrue(all(p["box"][2] - p["box"][0] >= 16 - 1e-9 for p in frame["parts"] if p["kind"] == "fish"))
                    self.assertGreaterEqual(frame["red"], 8 * 2)
                    # They swim in the open water between the lowest jellyfish and the octopus.
                    floor = min(p["box"][1] for p in frame["parts"] if p["kind"] == "octopus")
                    ceiling = max(p["box"][3] for p in frame["parts"] if p["kind"] == "jelly")
                    for part in frame["parts"]:
                        if part["kind"] in ("fish", "minnow", "bacteria"):
                            self.assertTrue(ceiling <= part["box"][1] and part["box"][3] <= floor, part)
                else:
                    self.assertEqual([kinds.count(k) for k in ("fish", "minnow", "bacteria", "eel")], [0, 0, 0, 0])
                for part in frame["parts"]:
                    if part["kind"] in ("jelly", "fish", "minnow", "octopus", "eel", "bacteria"):
                        x0, y0, x1, y1 = part["box"]
                        self.assertTrue(0 <= x0 and 0 <= y0 and x1 <= frame["cols"] and y1 <= frame["rows"], part)
                boxes = [part["box"] for part in frame["parts"] if part["kind"] == "jelly"]
                for i, a in enumerate(boxes):
                    for b in boxes[i + 1:]:
                        self.assertFalse(a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3], (a, b))
        self.assertGreater(len({json.dumps(f["first"]) for f in got["out"] if f["W"] == 320}), 1)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_tank_reuses_its_placement_until_size_or_dock_changes(self):
        # A placement search repeated on every frame, or a cached placement that ignores a new dock or tank size,
        # must fail on the real Tank path.
        block = dashboard.SCRIPT[dashboard.SCRIPT.index("// <sea>"):dashboard.SCRIPT.index("// </sea>")]
        tank = dashboard.SCRIPT[dashboard.SCRIPT.index("function Tank(canvas)"):dashboard.SCRIPT.index("let fonts = false;")]
        probe = """
const assert = require('node:assert/strict');
function exercise(reduced) {
  const events = {}, T0 = 0;
  let W = 600, H = 500, dockTop = 420, nextFrame, dockResized, layouts = 0, seen = null;
  const dock = { getBoundingClientRect: () => ({ left: 20, top: dockTop, right: 580, bottom: 482 }) };
  const host = { parentElement: { querySelector: () => dock }, getBoundingClientRect: () => ({ left: 0, top: 0 }) };
  const ctx = { setTransform() {}, clearRect() {}, fillRect() {}, fillText() {}, beginPath() {}, moveTo() {}, arc() {}, fill() {} };
  const spec = ['worker', 'waiting', 'idle'].map((name, k) => ({ name, tone: ['accent', 'amber', 'faint'][k], fx: 0.5, fy: 0.5 }));
  const fauna = { floor: true, fish: 2, bacteria: 1, plankton: 1, octopus: { tone: 'amber' } };
  const canvas = { dataset: { creatures: JSON.stringify(spec), fauna: JSON.stringify(fauna) }, parentElement: host,
    isConnected: true, getContext: () => ctx, getBoundingClientRect: () => ({ left: 0, top: 0, width: W, height: H }) };
  const ResizeObserver = class { constructor(fn) { dockResized = fn; } observe() {} disconnect() {} };
  const window = { devicePixelRatio: 1, ResizeObserver };
  const document = { documentElement: {}, hidden: false, addEventListener() {}, removeEventListener() {} };
  const calm = { matches: reduced }, light = { addEventListener() {}, removeEventListener() {} };
  const getComputedStyle = () => ({ getPropertyValue: () => '' });
  const addEventListener = (event, fn) => { events[event] = fn; }, removeEventListener = () => {};
  const requestAnimationFrame = fn => { nextFrame = fn; return 1; };
  const setTimeout = fn => { fn(); return 1; }, clearTimeout = () => {}, $$ = () => [];
""" + block + tank + """
  const realLayout = seaLayout, realPlacement = seaPlacement;
  seaLayout = (...args) => { layouts++; return realLayout(...args); };
  seaPlacement = (...args) => { seen = args[4]; return realPlacement(...args); };
  const frame = now => { if (reduced) dockResized(); else { const fn = nextFrame; nextFrame = null; fn(now); } };
  Tank(canvas);
  frame(6000);
  const settled = layouts;
  frame(6060);
  assert.equal(layouts, settled, 'unchanged geometry must reuse placement');
  dockTop -= 100;
  frame(6120);
  assert.equal(layouts, settled + 1, 'a new dock must replace placement');
  assert.equal(seen[1], dockTop / seaGrid(W, H, spec.length, true).Q);
  // The same dock in the same cells, in a tank of another size: only the tank's own change can replace placement.
  const before = seaGrid(W, H, spec.length, true);
  W = 640;
  const after = seaGrid(W, H, spec.length, true);
  assert.equal(after.P, before.P); assert.notEqual(after.cols, before.cols);
  events.resize();
  frame(6180);
  const resized = layouts;
  assert(resized > settled + 1, 'a new tank size must replace placement');
  frame(6240);
  assert.equal(layouts, resized, 'the new size keeps its placement');
}
exercise(false);
exercise(true);
console.log('TANK PLACEMENT PASSED');
"""
        r = subprocess.run(["node", "-e", probe], capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "TANK PLACEMENT PASSED")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_seas_characters_fit_their_cells(self):
        # Rows stretched to fill a tank, characters wider than their column (neighbours run together), characters too
        # small to read beside the list, or specks of plankton that touch, must fail.
        block = dashboard.SCRIPT[dashboard.SCRIPT.index("// <sea>"):dashboard.SCRIPT.index("// </sea>")]
        probe = block + """
const out = [];
for (const [W, H] of [[357, 111], [390, 112], [860, 112], [320, 760], [320, 1100], [1100, 900]]) {
  const { P, Q } = seaGrid(W, H, 6, true);
  out.push({ W, H, P, Q, size: glyphSize(P, Q), speck: speck(P), ADVANCE, LATTICE });
}
console.log(JSON.stringify(out));
"""
        r = subprocess.run(["node", "-e", probe], capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        for g in json.loads(r.stdout):
            with self.subTest(size=(g["W"], g["H"])):
                self.assertAlmostEqual(g["Q"] / g["P"], g["LATTICE"], places=9)  # one cell shape at every size
                self.assertLessEqual(g["size"] * g["ADVANCE"], g["P"] * 0.95 + 1e-9)  # a character within its column
                self.assertLessEqual(g["size"], g["Q"] * 0.9 + 1e-9)  # and within its row
                self.assertGreaterEqual(g["P"] - 2 * g["speck"], 1)  # a speck clear of the next by a pixel
                if g["H"] > 700:  # the reef: characters big enough to read
                    self.assertGreaterEqual(g["size"], 10)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_sea_moves_in_fractions_of_a_cell(self):
        # A creature that snaps to whole cells (it steps rather than glides), one that jumps a cell or more between two
        # frames, a fish whose body stays rigid, weight that a dot between cells loses or puts in only one cell, or
        # characters that do not grow heavier with weight, must fail.
        block = dashboard.SCRIPT[dashboard.SCRIPT.index("// <sea>"):dashboard.SCRIPT.index("// </sea>")]
        probe = block + """
const spec = ['a', 'b', 'c'].map((name, k) => ({ name, tone: ['faint', 'accent', 'amber'][k], fx: 0.5, fy: 0.5, sp: 1 }));
const fauna = { fish: 2, school: 4, bacteria: 1, plankton: 1, eel: true, octopus: { tone: 'amber' }, floor: true };
const { cols, rows } = seaGrid(900, 900, 3, true);
const frames = [];
for (let k = 0; k < 44; k++) frames.push(seaDots(spec, fauna, cols, rows, 10 + k * FRAME / 1000).parts);
const lone = [];
for (const t of [1, 2.5, 4]) {
  lone.push(seaDots([], { fish: 1, floor: true }, 120, 60, t).dots.filter(d => d[3] === 'accent' && d[2] !== FINE)
    .map(d => d[1]));
}
const split = seaCells([[3.25, 2, BRIGHT, 'accent', 1]], 10, 10).cells.get('accent');
const ramp = [0.06, 0.2, 0.4, 0.6, 0.8, 1, 1.2].map(w => RAMP.indexOf(glyph(w, 0.5)));
console.log(JSON.stringify({ frames, lone, split: [...split.entries()], ramp }));
"""
        r = subprocess.run(["node", "-e", probe], capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout)
        swimmers = ("jelly", "fish", "eel")
        for before, after in zip(got["frames"], got["frames"][1:]):
            self.assertEqual([p["kind"] for p in before], [p["kind"] for p in after])
            for p, q in zip(before, after):
                if p["kind"] in swimmers:
                    self.assertLess(abs(q["box"][0] - p["box"][0]), 0.5, (p, q))
                    self.assertLess(abs(q["box"][1] - p["box"][1]), 0.5, (p, q))
        for kind in (*swimmers, "minnow"):
            corners = [c for parts in got["frames"] for p in parts if p["kind"] == kind for c in p["box"][:2]]
            self.assertTrue(any(abs(c - round(c)) > 0.05 for c in corners), kind)  # between cells, not on them
            self.assertGreater(len({json.dumps(c) for c in corners}), 2, kind)  # and it moves
        for ys in got["lone"]:  # one fish's body: its columns lifted by different amounts
            self.assertGreater(len({round(y - int(y), 3) for y in ys}), 3)
        self.assertEqual(sorted((at, round(w, 9)) for at, w in got["split"]), [(23, 0.75), (24, 0.25)])
        self.assertEqual(got["ramp"], sorted(got["ramp"]))
        self.assertLess(got["ramp"][0], got["ramp"][-1])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_options_opens_inside_the_window(self):
        # Options opening under a composer docked at the window's foot (cut off by the bottom edge), past the right
        # edge of a narrow window, or taller than the side it opens on must fail.
        block = dashboard.SCRIPT[dashboard.SCRIPT.index("// <place>"):dashboard.SCRIPT.index("// </place>")]
        probe = block + """
const win = { top: 0, right: 1024, bottom: 640, left: 0 };
console.log(JSON.stringify([
  placePop({ top: 600, bottom: 630, left: 900, right: 970 }, win, 320, 250, 6),
  placePop({ top: 100, bottom: 130, left: 100, right: 170 }, win, 320, 250, 6),
  placePop({ top: 140, bottom: 170, left: 100, right: 170 }, { ...win, bottom: 300 }, 320, 250, 6),
]));
"""
        r = subprocess.run(["node", "-e", probe], capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        foot, head, short = json.loads(r.stdout)
        self.assertEqual(foot, {"up": True, "right": True, "max": 586})  # over the button, against its right edge
        self.assertEqual(head, {"up": False, "right": False, "max": 496})  # under it, from its left edge
        self.assertEqual(short, {"up": True, "right": False, "max": 126})  # the roomier side, scrolling within
        rule = lambda sel: dashboard.STYLE.split(sel + " {", 1)[1].split("}", 1)[0]  # noqa: E731
        self.assertIn("bottom: calc(100% + 6px)", rule(".opts-body.up"))
        self.assertIn("top: auto", rule(".opts-body.up"))
        self.assertIn("right: 0", rule(".opts-body.right"))
        self.assertIn("overflow: auto", rule(".opts-body"))
        self.assertIn("d.matches('.opts')) place(d);", dashboard.SCRIPT)

    def test_test_when_done_wears_a_flask_not_a_tick(self):
        # The row's value often reads "Don't test", and everywhere else a tick means done or passed. The row
        # drawn with the check icon's path, or without the flask, must fail.
        page = dashboard.render({**live_view(), "new": True}, NOW, token=TOKEN)
        row = page.split('<details class="op-row accept-option"', 1)[1].split("</details>", 1)[0]
        self.assertIn("Test when done", row)
        self.assertNotIn('d="M20 6 9 17l-5-5"', row)
        self.assertIn(dashboard._ICONS["flask"], row)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_page_script_parses(self):
        # A syntax error in app.js, which no Python test runs, must fail.
        r = subprocess.run(["node", "--check", str(ROOT / "rh" / "web" / "app.js")], capture_output=True, text=True,
                           encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_today_says_less(self):
        # The line under the composer, the list's sort note or a second "checked" in the top bar coming back must
        # fail.
        page = dashboard.render(live_view(), NOW, token=TOKEN)
        for words in ("Starts on the next check", "Nothing is pushed", "ordered by what needs you"):
            self.assertNotIn(words, text(page))
        self.assertNotIn('class="fresh', page)

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
        # a settled row reads as the rows that wait: what came of it in a sentence, then the run's id on its own line
        page = dashboard.render({**view, "box": True}, NOW, token=TOKEN)
        self.assertIn('<span class="ib-s"><span class="ib-why">Yes, ship</span></span>'
                      '<span class="ib-id mono">live-job</span>', page)
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
        for words in ("Load, 1 min2.17 of 10", "Memory pressure1 normal"):
            self.assertIn(words, calm)
        self.assertNotIn("Heavy", calm, "may start and free go unsaid")
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
                # the sidebar's group heads count nothing: the rows show how many, and the badge is the one count of
                # what needs you (critic r13 F1)
                self.assertRegex(needs, r'<h3 id="g-needs">Needs you</h3></div>')
                counts = {"badge": re.search(r'<b class="count num" data-need="(\d+)"', home).group(1),
                          "inbox title": re.search(r'<span class="top-meta mono"><span data-need="(\d+)"', box).group(1),
                          "rows": str(needs.count('<li class="row"')), "inbox rows": str(waiting.count('<li class="ib-li"'))}
                self.assertEqual(set(counts.values()), {str(int(expected))}, counts)
                self.assertIn(f'aria-label="{int(expected)} need you"', home)
                # with the sidebar folded away (a phone), that count shows as a dot on the button that opens it
                side = re.search(r'<aside class="side".*?</aside>', home, re.S).group(0)
                self.assertIn('<b class="count num" data-need=', side)
                self.assertIn("body:has(.side .count[data-need]) .app .sidebar-toggle::before", dashboard.STYLE)
                self.assertIn(f"{int(expected)} need you", text(box))
                # on the Inbox the list it opens on is the same rows: the sidebar's Needs you group steps aside so
                # the triage screen does not list each failure twice (critic r15 F3); the badge still counts them
                self.assertIn('<main class="pane" data-pane="inbox"', box)
        self.assertIn('.app:has(> .pane[data-pane^="inbox"]) .side .s-g-needs { display: none; }', dashboard.STYLE)
        empty = {**view, "live": [], "not_started": [], "recent": [], "inbox": [], "stops": []}
        home, box = dashboard.render(empty, NOW, token=TOKEN), inbox(empty)
        self.assertNotIn("data-need=", home + box.split('<main', 1)[1].split('</main>')[0].replace(
            '<span data-need="0">', ""))
        self.assertNotIn('<header class="top"', pane(home))
        self.assertIn("nothing needs you", text(box))

    def test_sidebar_links_keep_the_reason_in_their_accessible_text(self):
        row = item(dashboard.render(live_view(), NOW, token=TOKEN), "live-job")
        link = re.search(r'<a class="item".*?</a>', row, re.S).group(0)
        self.assertRegex(link, rf'<span class="name">live-job</span><span class="sub">{re.escape(ASK.capitalize())}</span>')
        self.assertNotIn('class="sub', row.split("</a>", 1)[1])  # the control stands alone

    def test_accent_colours_stay_clear_of_needs_you_and_failed(self):
        # Amber means needs you and red means failed, so no accent may sit between them; a page saved with the
        # retired orange accent opens in the default blue.
        page = dashboard.render({**live_view(), "tint": "ember"}, NOW, token=TOKEN)
        self.assertIn('data-tint="cobalt"', page)
        self.assertEqual(re.findall(r'<span class="tint-dot" data-tint="(\w+)">', page),
                         ["cobalt", "ice", "paper", "mono"])
        # violet and teal beside blue were the stock AI-tool accent set, the one template palette in the app (critic
        # r11 F11): every option is an ink tint of the same halftone, and a page saved with a retired one opens blue
        bg = open(os.path.join(os.path.dirname(dashboard.__file__), "web", "bg.js"), encoding="utf-8").read()
        for gone in ('data-tint="violet"', 'data-tint="teal"', "#6d3df2", "#0f9f8f", "#7347f0", "#13a393", "#7c4dff",
                     "#14a394"):
            self.assertNotIn(gone, dashboard.STYLE)
        for gone in ("violet", "teal", "#6d3df2", "#0f9f8f"):
            self.assertNotIn(gone, bg)
        self.assertIn('data-tint="cobalt"', dashboard.render({**live_view(), "tint": "violet"}, NOW, token=TOKEN))

    def test_the_palette_has_a_way_out_without_a_keyboard(self):
        # On a phone there is no esc key: a Cancel button closes the palette, and the script wires it.
        pal = re.search(r'<dialog class="pal".*?</dialog>', dashboard.render(live_view(), NOW, token=TOKEN), re.S).group(0)
        self.assertIn('<button type="button" class="p-x" data-pal-close>Cancel</button>', pal)
        self.assertIn("[data-pal-close]", dashboard.SCRIPT)

    def test_a_done_job_with_no_answer_shows_what_it_printed_last(self):
        # A finished shell job's result is its output: hidden behind a fold the page looks empty. Its last lines show
        # open in a card; the rest folds below only when there is more. A job that answered keeps the fold alone.
        log = "\n".join(f"line {n}" for n in range(1, 31))
        done = opened(live_view(), "finished-job", kind="local-shell", log=None)
        done["open"]["log"] = log
        body = pane(dashboard.render(done, NOW, token=TOKEN))
        self.assertEqual(re.findall(r'data-region="(j-output|j-last)"', body), ["j-last", "j-output"])
        card = re.search(r'data-region="j-last".*?</section>', body, re.S).group(0)
        self.assertIn("Last output", card)
        # r18 F8: the title says 'last'; the count says how much of the whole it shows
        self.assertIn("12 of 30 lines", card)
        self.assertNotIn("last 12", card)
        self.assertIn("line 30", card)
        self.assertNotIn("line 18\n", card)
        self.assertIn('<span class="worked-output">All output</span>', body)
        self.assertIn('<span class="lines mono">30 lines</span>', body)
        done["open"]["log_cut"] = True  # only the tail was kept: the fold says so, and never calls it all
        self.assertIn('<span class="worked-output">Output</span><span class="lines mono">last 30 lines</span>',
                      pane(dashboard.render(done, NOW, token=TOKEN)))
        done["open"]["log_cut"] = False
        done["open"]["log"] = "one\ntwo"
        body = pane(dashboard.render(done, NOW, token=TOKEN))
        self.assertIn('data-region="j-last"', body)
        self.assertNotIn('data-region="j-output"', body)
        self.assertEqual(body.count("two"), 1)
        done["open"].update(log=log, answer="Fixed it.")
        body = pane(dashboard.render(done, NOW, token=TOKEN))
        self.assertNotIn('data-region="j-last"', body)
        self.assertIn('data-region="j-output"', body)

    def test_finished_pages_keep_output_collapsible_and_the_reply_in_the_dock(self):
        # Output stays folded in Worked and the real Run again form lives after the session scroll area.
        log = "\n".join(f"line {n}" for n in range(1, 31))
        done = opened(live_view(), "finished-job", kind="local-shell", log=None)
        done["open"].update(log=log, files=[{"path": "notes.md", "size": 7}],
                            peek={"path": "notes.md", "text": "# Notes", "more": False})
        page = pane(dashboard.render(done, NOW, token=TOKEN))
        body = page.split('<div class="jd-scroll">', 1)[1].split('</div></div>', 1)[0]
        self.assertIn('data-region="j-output"', body)
        self.assertIn('<details class="file-preview"><summary>Preview</summary>', body)
        dock = page.split('<div class="jd-dock">', 1)[1]
        self.assertIn('data-region="j-reply"', dock)
        self.assertIn('action="/again"', dock)
        self.assertNotIn('data-region="j-reply"', body)
        broke = opened(live_view(), "broke-job", kind="local-shell")
        broke["open"].update(log=log, receipt={"route": "local-shell", "checks": [{"cmd": "sh run.sh", "exit": 2}],
                                               "ended": NOW - 10, "detail": "command exited 2"})
        page = pane(dashboard.render(broke, NOW, token=TOKEN))
        body = page.split('<div class="jd-scroll">', 1)[1].split('</div></div>', 1)[0]
        order = re.findall(r'data-region="(j-output|j-error)"', body)
        self.assertEqual(order, ["j-error", "j-output"])
        self.assertIn('data-region="j-reply"', page.split('<div class="jd-dock">', 1)[1])
        error = re.search(r'data-region="j-error".*?</section>', body, re.S).group(0)
        self.assertIn("12 of 30 lines", error)  # the exit code leads the page, not this card
        self.assertNotIn("exit 2", error)
        self.assertIn("line 30", error)
        self.assertNotIn("line 18\n", error)  # only where it stopped: the last lines
        # A failed job's page leads with the check that failed, its command and its exit code, before its brief; a
        # done one has no such block, and a finished head drops the Output count (the fold below says it).
        fail = re.search(r'<section class="s-j-fail".*?</section>', body, re.S).group(0)
        self.assertLess(body.index('class="s-j-fail"'), body.index('class="s-j-brief"'))
        words = lambda h: html.unescape(re.sub(r"<[^>]+>", " ", h)).split()  # noqa: E731
        # the sentence the Inbox uses too, then the command and its exit chip
        self.assertEqual(words(fail), ["The", "command", "exited", "with", "code", "2", "sh", "run.sh", "exit", "2"])
        broke["open"]["receipt"]["checks"] = [{"cmd": "sh run.sh", "exit": 0}, {"cmd": "notes.md exists", "exit": 1}]
        fail = re.search(r'<section class="s-j-fail".*?</section>', dashboard.render(broke, NOW, token=TOKEN), re.S)
        self.assertEqual(words(fail.group(0))[:4], ["The", "done-when", "check", "failed:"])
        # A file test says what it found about the file: named, then what it lacked.
        broke["open"]["job"]["spec"]["accept"] = "test -s flaky.md"
        broke["open"]["receipt"]["checks"] = [{"cmd": "sh run.sh", "exit": 0}, {"cmd": "test -s flaky.md", "exit": 1}]
        fail = re.search(r'<p class="fail-k">.*?</p>', dashboard.render(broke, NOW, token=TOKEN), re.S).group(0)
        self.assertEqual(" ".join(words(fail)), "The done-when check failed: flaky.md is missing or empty")
        self.assertEqual(dashboard._found("[ -f 'out dir/a.txt' ]", 1), "out dir/a.txt is missing or not a file")
        self.assertEqual(dashboard._found("test -s flaky.md", 2), "it exited with code 2")
        # Under the banner the status line said 'failed' again, a second red ring and word 150px below the first
        # (critic r12 F1): with a banner it keeps only its facts, the checks and the time.
        status = re.search(r'data-region="job-result".*?</p>', dashboard.render(broke, NOW, token=TOKEN), re.S).group(0)
        self.assertNotIn("<svg", status)
        self.assertNotIn("Failed", status)
        self.assertRegex(text(status), r"checks")
        page = dashboard.render(done, NOW, token=TOKEN)
        self.assertNotIn('class="s-j-fail"', page)
        # In the Inbox a failed job wears the red ring it wears on Today.
        row = re.search(r'<li class="ib-li" data-key="job:broke-job".*?</li>', inbox(live_view()), re.S).group(0)
        self.assertIn('<svg class="st t-red"', row)
        short = opened(live_view(), "broke-job", kind="local-shell")
        short["open"]["log"] = "ls: docs/: No such file or directory"
        body = pane(dashboard.render(short, NOW, token=TOKEN))
        # a failed log that fits in "Where it stopped" is shown there once, with no second fold repeating it
        self.assertIn('data-region="j-error"', body)
        self.assertNotIn('data-region="j-output"', body)
        self.assertEqual(body.count("ls: docs/: No such file or directory"), 1)
        self.assertNotIn(".jd-compose { flex: none", dashboard.STYLE)

    def test_a_stopped_log_wraps_each_line_as_its_own_hanging_block(self):
        # Each tail line is its own block so a wrapped line indents under the line's start, not the card's edge;
        # a newline-joined tail, a line rendered outside its span, or a < reaching the page raw, must fail.
        broke = opened(live_view(), "broke-job", kind="local-shell")
        broke["open"]["log"] = "first < line\n\nlast"
        card = re.search(r'<pre class="log ended err" tabindex="0">(.*?)</pre>',
                         pane(dashboard.render(broke, NOW, token=TOKEN)), re.S).group(1)
        self.assertEqual(card, '<span class="ln">first &lt; line</span><span class="ln"></span>'
                               '<span class="ln">last</span>')
        self.assertNotIn("\n", card)
        self.assertIn('pre.log.ended .ln { display:block; padding-left:2ch; text-indent:-2ch; white-space:pre-wrap; }',
                      dashboard.STYLE)

    def test_session_numbers_use_ui_type_and_the_machine_has_one_name(self):
        view = opened(live_view(), "finished-job", kind="local-shell")
        view["open"]["files"] = [{"path": "notes.md", "size": 2048}]
        page = pane(dashboard.render(view, NOW, token=TOKEN))
        self.assertIn('Done·1 min 20 s', text(page))
        for machine in ('<span class="pk">Job</span>', '<span class="mono">120 min</span>'):
            self.assertIn(machine, page)
        self.assertIn('class="session-file-size"', page)
        self.assertIn('class="session-file-main"', page)
        self.assertRegex(page, r'<span class="session-file-main"><svg[^>]*>.*?</svg><a [^>]*>notes\.md</a>'
                               r'<span class="session-file-size">2\.0 KB</span></span>')
        self.assertIn('class="prop prop-time"', page)
        self.assertNotIn('class="session-usage"', page)
        self.assertRegex(dashboard.STYLE, r"\n\.when \{ font-family: var\(--ui\).*tabular-nums")
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

    def test_today_opens_on_what_needs_you(self):
        # The queue belongs to the only sidebar, and the main workspace keeps the composer and live background.
        page = dashboard.render(live_view(), NOW, token=TOKEN)
        sidebar, detail = page.split('<main class="pane"', 1)
        self.assertIn('<aside class="side"', sidebar)
        self.assertNotIn('class="jobs lane"', detail)
        lane = sidebar.split('<section class="jobs lane"', 1)[1]
        self.assertIn('aria-label="Filter jobs by worker"', lane)
        ahead = lane[lane.index('<div class="list">'):lane.index('<section class="s-g-needs group"')]
        self.assertNotIn("<section", ahead.replace('<p class="empty no-jobs"', ""))
        self.assertNotIn("<canvas", lane)
        self.assertRegex(detail, r'<div class="page sea-page"><aside class="reef" aria-label="Who is working">'
                                 r'<canvas class="tank-c"')
        dock = detail[detail.index('<div class="dock">'):]
        self.assertIn('<textarea class="c-text" id="brief" name="brief" rows="1"', dock)

    def test_home_workspaces_skip_the_inner_header(self):
        view = live_view()
        pages = {"/": dashboard.render(view, NOW, token=TOKEN),
                 "/new": dashboard.render({**view, "new": True}, NOW, token=TOKEN),
                 "/inbox": inbox(view),
                 "/job/live-job": dashboard.render(opened(view, "live-job"), NOW, token=TOKEN)}
        for route, page in pages.items():
            with self.subTest(route=route):
                top = re.search(r'<header class="top"[^>]*>.*?</header>', page, re.S)
                if route in ("/", "/new"):
                    self.assertIsNone(top)
                    self.assertIn('id="compose-title"', pane(page))
                elif route == "/inbox":
                    self.assertIsNotNone(top)
                else:
                    self.assertIsNone(top)
                    self.assertIn('class="jd-inspector"', pane(page))

    def test_a_job_page_names_its_worker_once(self):
        # '· This Mac' after the job's name and 'This Mac' beside Run again, 820px apart, said one fact twice (critic
        # r11 F9). While the reply dock names the worker, the header leaves it out; a running job, with no reply box,
        # keeps it in the header.
        rule = dashboard.STYLE.split("\n.jd-main:has(> .jd-dock .model-note) .job-worker {", 1)[1].split("}", 1)[0]
        self.assertIn("display: none", rule)

    def test_the_failure_banner_is_a_quiet_field_under_one_red_chip(self):
        # A dark red fill, a red border, a red icon and a red-bordered exit chip stacked four red signals: the stock
        # alert-danger box (critic r13 F3). The field keeps its fill under a faint edge; the icon and chip carry the red.
        rule = dashboard.STYLE.split("\n.s-j-fail, .q-fail {", 1)[1].split("}", 1)[0]
        self.assertIn("border: 1px solid rgb(255 99 99 / .12)", rule)
        self.assertIn("background: #1d1116", rule)

    def test_every_page_header_title_wears_one_style(self):
        # 'Inbox' was 15px bold and a job's title 13px medium in the same header bar, so moving between pages felt
        # like two apps (critic r13 F7): one style, 14px 600 #e8e9ec.
        top = dashboard.STYLE.split("\n.top h1 {", 1)[1].split("}", 1)[0]
        job = dashboard.STYLE.split("\n.detail .jd-head h1 {", 1)[1].split("}", 1)[0]
        for spec in ("font-size: 14px", "font-weight: 600", "color: #e8e9ec"):
            self.assertIn(spec, top)
        self.assertIn("font: 600 14px/20px var(--ui)", job)
        self.assertIn("color: #e8e9ec", job)

    def test_a_job_header_title_steps_aside_while_its_words_show_below(self):
        # The header named a finished job by its answer's first line and then the answer said it again 220px lower,
        # so the top of every job page said one thing twice (critic r15 F2). The title keeps the sidebar's name but
        # fades out while the answer's first line or the brief with the same words is in view, and back in (160ms,
        # the app's ease-out) once they scroll under the header.
        self.assertIn("function echoTitle()", dashboard.SCRIPT)
        self.assertIn("document.addEventListener('rh:page', echoTitle);", dashboard.SCRIPT)
        self.assertIn("new IntersectionObserver(", dashboard.SCRIPT.split("function echoTitle()", 1)[1])
        self.assertIn(".detail .jd-head.echo h1 { opacity: 0; }", dashboard.STYLE)
        self.assertIn(".detail .jd-head-in > h1 { transition: opacity 160ms cubic-bezier(0.16, 1, 0.3, 1); }",
                      dashboard.STYLE)

    def test_an_inbox_rows_run_id_reads_as_an_identifier(self):
        # The run id under the reason was the same sans one pixel smaller in a near-identical grey, so the two read
        # as one level drawn twice (critic r13 F8). The live app sets .mono in the UI face with tabular figures by
        # design, so the id keeps that face and steps apart by its grey: #9aa0ad under the reason's #c1c1c6.
        rule = dashboard.STYLE.split("\n.ib-id {", 1)[1].split("}", 1)[0]
        self.assertIn("color: #9aa0ad", rule)
        self.assertIn("html[data-live-app] :is(.when, .mono:not(code, pre, input, textarea)", dashboard.STYLE)

    def test_the_reply_box_says_reply_once(self):
        # 'Reply, or send empty to run again' beside a 'Run again' pill said the empty send twice in one box (critic
        # r13 F4): the pill explains it, and the placeholder is a word.
        page = dashboard.render(opened(live_view(), "finished-job"), NOW, token=TOKEN)
        self.assertIn('placeholder="Reply…"', page)
        self.assertNotIn("send empty to run again", page)

    def test_an_inbox_rows_title_stands_apart_by_weight(self):
        # Title, reason and id step 14/13/12px but all at weight 400, so on a phone the reason read as a third line of
        # the title (critic r14 F3): the title is weight 500.
        self.assertIn("font-weight: 500", dashboard.STYLE.split("\n.ib-t {", 1)[1].split("}", 1)[0])

    def test_a_popovers_confirm_sits_under_its_trigger(self):
        # The Cancel confirm sat at the popover's bottom-left, 190px from the header button that opened it; a Mac app
        # puts the confirming action bottom-right (critic r14 F6).
        self.assertIn("justify-self: end", dashboard.STYLE.split("\nform.pop .btn.danger.fill {", 1)[1].split("}", 1)[0])

    def test_a_popover_sits_above_the_page(self):
        # The Cancel confirm, with no shadow and a fill near the brief bubble's, read as two panels colliding (critic
        # r12 F6): it casts the menus' shadow.
        self.assertIn("\nform.pop { box-shadow: 0 16px 40px rgb(0 0 0 / .55); }", dashboard.STYLE)

    def test_the_composer_offers_its_workers(self):
        # A composer without GPT Pro, Codex on this Mac, Codex cloud and the plain shell, in that order, or one whose
        # GPT Pro choice posts another kind, must fail.
        page = dashboard.render(live_view(), NOW, token=TOKEN)
        form = re.search(r'<form class="compose composer".*?</form>', page, re.S).group(0)
        chooser = re.search(r'<select[^>]+name="kind".*?</select>', form, re.S).group(0)
        self.assertEqual(re.findall(r'<option value="([a-z-]+)"', chooser),
                         ["gpt-chat", "local-agent", "codex-cloud", "local-shell"])
        self.assertRegex(chooser, r'value="gpt-chat"><svg[^>]*>.*?</svg>GPT Pro</option>')

    def test_model_and_effort_menus_carry_their_own_choices(self):
        # A model menu holding effort levels, a model without the levels its CLI knows and the policy allows, or an
        # effort menu that does not start on the highest allowed level, must fail.
        view = live_view()
        view["job_policy"] = {"codex-cloud": {"models": ["gpt-6.1-sol"], "efforts": ["max"]},
                              "codex-local": {"efforts": ["high", "max"]},
                              "claude-local": {"models": ["claude-opus-5-5"], "efforts": ["light", "high"]},
                              "devin-local": {"models": ["swe-2-high"]}}
        form = re.search(r'<form class="compose composer".*?</form>', dashboard.render(view, NOW, token=TOKEN), re.S).group(0)
        local = re.search(r'<select class="ghost model-pick" data-route="local-agent".*?</select>', form, re.S).group(0)
        self.assertEqual(re.findall(r'<option value="([^"]+)"', local), ["gpt-6.1-sol", "claude-opus-5-5", "swe-2-high"])
        self.assertIn('value="claude-opus-5-5" data-prov="claude" data-levels="light medium high extra-high max" '
                      'data-efforts="light high"', local)
        self.assertIn('data-prov="devin" data-levels="" data-efforts=""', local)
        effort = re.search(r'<select class="effort-pick" name="effort" data-route="local-agent".*?</select>', form, re.S).group(0)
        self.assertIn('<option value="max" selected>Max</option>', effort)
        self.assertIn('<span class="ef-name">Max</span>', form)

    def test_a_gpt_pro_chat_hands_off_and_reads_its_reply(self):
        # A chat job waiting on you without its prompt, a Copy, the way to ChatGPT and a /reply form for that job;
        # a Codex launch card on a chat job; a chat row that does not say to ask GPT Pro or is not filed under the
        # GPT Pro worker; or a saved reply that does not read as GPT Pro's message, must fail.
        view = live_view()
        view["live"].append({"id": "deep-dive", "state": "handoff", "paused": False,
                             "detail": "Waiting for the reply from ChatGPT", "external_id": None})
        view["inbox"].append({"id": 21, "kind": "chat", "job": "deep-dive", "created": NOW - 4, "resolved": None,
                              "action": "Ask ChatGPT (gpt-6.1-sol-pro): copy the prompt, paste its reply",
                              "detail": "Waiting for the reply from ChatGPT", "resolution": None})
        view["meta"] = {"deep-dive": {"kind": "gpt-chat", "brief": "Compare the three pacing designs.",
                                      "created": NOW - 5}}
        home = dashboard.render(view, NOW, token=TOKEN)
        row = item(home, "deep-dive")
        self.assertIn('data-where="chat"', row)
        self.assertIn("Ask GPT Pro, then paste its reply", text(row))
        self.assertIn(">Ask GPT Pro</a>", row)
        self.assertIn('<option value="chat">GPT Pro</option>', home)
        spec = {"id": "deep-dive", "kind": "gpt-chat", "brief": "Compare the three pacing designs.",
                "model": {"requested": "gpt-6.1-sol-pro"}, "write_root": "runs/deep-dive/"}
        page = dashboard.render(opened(view, "deep-dive", kind="gpt-chat", spec=spec), NOW, token=TOKEN)
        self.assertIn('data-key="job:deep-dive" aria-current="page"', page)  # its row is lit in the lane
        self.assertIn("Waiting for GPT Pro", text(pane(page)))
        own = re.sub(r'<dialog class="pal".*?</dialog>', "", pane(page), flags=re.S)  # the palette lists other jobs
        self.assertNotIn("Launch by hand", text(own))  # head and properties alike
        card = re.search(r'<div class="ask-card chat-card">.*?</div>', page, re.S).group(0)
        self.assertIn('<pre class="prompt" id="prompt-deep-dive" tabindex="0">Compare the three pacing designs.</pre>',
                      card)
        self.assertIn('data-copy="prompt-deep-dive"', card)
        self.assertIn(f'href="{dashboard._CHATGPT}" target="_blank" rel="noopener noreferrer"', card)
        self.assertIn("gpt-6.1-sol-pro", card)
        dock = page.split('<div class="jd-dock">', 1)[1]
        reply = [f for f in Forms(dock).forms if f["action"] == "/reply"]
        self.assertEqual(len(reply), 1)
        self.assertEqual(set(reply[0]["fields"]), {"t", "job", "text", "next"})
        self.assertEqual(reply[0]["fields"]["job"], "deep-dive")
        self.assertNotIn('action="/attach"', pane(page))
        boxed = dashboard.render({**view, "box": True, "pick": 21}, NOW, token=TOKEN)  # the Inbox answers it the same way
        self.assertEqual([f["fields"]["job"] for f in Forms(boxed).forms if f["action"] == "/reply"], ["deep-dive"])
        self.assertIn("Waiting for GPT Pro", text(re.search(r'<p class="q-job".*?</p>', boxed, re.S).group(0)))
        self.assertNotIn('action="/inbox/resolve"', re.search(r'data-region="ib-detail".*', boxed, re.S).group(0))
        done = opened({**view, "inbox": [i for i in view["inbox"] if i["id"] != 21]}, "deep-dive", kind="gpt-chat",
                      spec=spec, state="done")
        done["open"]["reply"] = "Design B wins.\n\nIt keeps one clock."
        done["open"]["job"]["detail"] = "Design B wins."  # the reply's first sentence, as rh.chat records it
        page = dashboard.render(done, NOW, token=TOKEN)
        self.assertNotIn("wins..", page)
        said = re.search(r'<section class="msg ai".*?</section>', page, re.S).group(0)
        self.assertIn("GPT Pro", text(said))
        self.assertIn("Design B wins.", said)
        self.assertNotIn('class="ask-card chat-card"', page)

    def test_coordination_sits_behind_one_control(self):
        # The check loop, this Mac or the seats on show outside the coordination control, a control that does not
        # say whether checks run and how many jobs are live, or a panel without Check now and Pause for the loop and
        # Pause for Codex cloud, must fail.
        view = {**live_view(), "seats": [{"name": "backend", "role": "Backend: the core"}],
                "host": {"load1": 1.5, "level": 1, "max_load": 10, "ok": True}}
        view["live"].append({"id": "running-alone", "state": "running", "paused": False, "detail": ""})
        page = dashboard.render(view, NOW, token=TOKEN)
        side = page[page.index('<aside class="side"'):page.index("</aside>", page.index('<aside class="side"'))]
        coord = side[side.index('<details class="coord">'):]
        outside = side[:side.index('<details class="coord">')]
        for region in ('data-region="checks"', 'data-region="host"', 'class="seat-list"'):
            self.assertNotIn(region, outside)
            self.assertIn(region, coord)
        summary = coord[:coord.index("</summary>")]
        self.assertIn("Checks on", text(summary))
        self.assertNotIn("1 job live", text(pane(page)))
        self.assertNotIn("live", text(summary))  # the Live group lists them; the pill repeated it (r13 F1)
        self.assertNotIn("seat", text(summary))  # the pill fits the sidebar; the seats are in the panel
        self.assertIn("backend", text(coord[coord.index("</summary>"):]))
        forms = Forms(coord).forms
        self.assertTrue(any(f["action"] == "/tick" for f in forms))
        self.assertTrue(any(f["action"] == "/checks" and f["fields"].get("on") == "0" for f in forms))
        self.assertTrue(any(f["action"] == "/stop" and f["fields"].get("route") == "codex-cloud" for f in forms))

    def test_a_failure_repeated_by_its_brief_is_one_row_until_it_is_run_again(self):
        # Runs of one brief that failed the same way listed as separate rows or Inbox entries, a row that does not
        # say how many times, a count of what needs you that counts each run, a run's page that does not lead to the
        # others, or older runs left waiting once the newest is run again, must fail. A run that failed another way
        # stays its own row; running an older run again settles only it and those before it.
        view = live_view()
        brief = "Run the test suite twenty times and list every test that failed at least once. Keep the seeds."
        runs = ["sweep", "sweep-2", "sweep-3"]
        view["recent"] += [{"id": jid, "state": "failed", "paused": False, "detail": "check exited 1",
                            "external_id": None} for jid in runs]
        view["recent"].append({"id": "sweep-odd", "state": "failed", "paused": False, "detail": "command exited 2",
                               "external_id": None})
        view["meta"] = {jid: {"brief": brief, "kind": "local-shell", "created": NOW - 900 + 100 * k,
                              "updated": NOW - 800 + 100 * k} for k, jid in enumerate(runs + ["sweep-odd"])}
        needs = lambda v: re.search(r'<section class="s-g-needs group".*?</section>',  # noqa: E731
                                    dashboard.render(v, NOW, token=TOKEN), re.S).group(0)
        listed = lambda v: re.findall(r'data-key="job:(sweep[^"]*)"', needs(v))  # noqa: E731
        page = dashboard.render(view, NOW, token=TOKEN)
        self.assertEqual(listed(view), ["sweep-odd", "sweep-3"])  # newest first, as the Inbox lists them
        # the palette keeps the older runs findable, under their own heading rather than among what needs you
        pal = re.search(r'<dialog class="pal".*?</dialog>', page, re.S).group(0)
        folded = pal.split('<p class="p-sec">Earlier runs of what needs you</p>', 1)[1].split('<p class="p-sec">', 1)[0]
        self.assertEqual(sorted(re.findall(r'href="/job/(sweep[^"?]*)', folded)), ["sweep", "sweep-2"])
        self.assertNotIn("/job/sweep-2", pal.split('<p class="p-sec">Needs you</p>', 1)[1].split('<p class="p-sec">', 1)[0])
        self.assertIn("Failed 3 times: ", text(item(page, "sweep-3")))
        self.assertNotIn("times", re.search(r'<span class="sub">([^<]*)', item(page, "sweep-odd")).group(1))
        count = lambda p: int(re.search(r'data-need="(\d+)"', p).group(1))  # noqa: E731
        alone = {**view, "recent": [r for r in view["recent"] if r["id"] not in ("sweep", "sweep-2")]}
        self.assertEqual(count(page), count(dashboard.render(alone, NOW, token=TOKEN)))
        # The Inbox: one entry, led by the job's title, with what failed and the newest run's name under it.
        box = {m.group(1): m.group(0) for m in re.finditer(r'<li class="ib-li" data-key="job:(sweep[^"]*)".*?</li>',
                                                           inbox(view), re.S)}
        self.assertEqual(sorted(box), ["sweep-3", "sweep-odd"])
        self.assertRegex(box["sweep-3"], r'<span class="ib-t">Run the test suite twenty times and list every test '
                                         r'that failed at least once\.?</span>')
        self.assertRegex(box["sweep-3"], r'<span class="ib-s"><span class="ib-why">Failed 3 times: [^<]+</span></span>'
                                         r'<span class="ib-id mono">sweep-3</span>')
        # Each run's page names every run that failed the same way, newest first, and links the others.
        for jid in ("sweep-3", "sweep"):
            body = pane(dashboard.render(opened(view, jid, kind="local-shell"), NOW, token=TOKEN))
            said = re.search(r'<p class="fail-runs">.*?</p>', body, re.S).group(0)
            self.assertTrue(text(said).startswith("Failed the same way 3 times: sweep-3·sweep-2·sweep"), said)
            self.assertEqual(re.findall(r'href="/job/([^"?]+)', said), [r for r in ("sweep-3", "sweep-2", "sweep")
                                                                       if r != jid])
        self.assertNotIn('class="fail-runs"', pane(dashboard.render(opened(view, "sweep-odd", kind="local-shell"),
                                                                    NOW, token=TOKEN)))
        # Run again on the row runs the newest; that settles every run of the brief added up to it.
        again = copy.deepcopy(view)
        again["live"].append({"id": "sweep-4", "state": "running", "paused": False, "detail": "",
                              "external_id": None})
        again["meta"]["sweep-4"] = {"brief": brief, "prior": "sweep-3", "created": NOW - 10}
        self.assertEqual(listed(again), ["sweep-odd"])
        once = copy.deepcopy(view)
        once["live"].append({"id": "sweep-r", "state": "running", "paused": False, "detail": "", "external_id": None})
        once["meta"]["sweep-r"] = {"brief": brief, "prior": "sweep", "created": NOW - 10}
        self.assertEqual(listed(once), ["sweep-odd", "sweep-3"])
        self.assertIn("Failed 2 times: ", text(item(dashboard.render(once, NOW, token=TOKEN), "sweep-3")))

    def test_the_check_loop_keeps_one_left_edge(self):
        # A status dot that pushes its words in past the sidebar's edge, a Pause whose words start elsewhere than
        # the others', or two buttons both called Pause, must fail.
        style = dashboard.STYLE
        rule = lambda sel: style.split("\n" + sel + " {", 1)[1].split("}", 1)[0]  # noqa: E731
        self.assertRegex(rule(".status .line > .dot"), r"position: absolute; left: -\d+px")
        pad = re.search(r"padding: 0 (\d+)px", rule(".btn.small")).group(1)
        self.assertIn(f"margin-left: -{pad}px", rule(".status .btns"))
        self.assertIn(f"margin-left: -{pad}px", rule(".status .route .act > summary"))
        side = dashboard.render(live_view(), NOW, token=TOKEN)
        side = side[side.index('<div class="status"'):]
        side = side[:side.index("</div>", side.index('<div class="btns">'))]
        labels = re.findall(r">(Pause[^<]*)<", side)
        self.assertIn("Pause checks", labels)
        self.assertIn("Pause Codex cloud…", labels)
        self.assertNotIn("Pause", labels)

    def test_a_stopped_logs_cause_stands_out_from_its_noise(self):
        # A WARN or ERROR line set like the INFO lines around it, INFO noise or the trailer as loud as the cause, or an
        # ordinary line (a word like 'errors' in it) dressed as either, must fail.
        tone = lambda line: re.search(r'class="ln ?(\w*)"', dashboard._ln(line)).group(1)  # noqa: E731
        for line in ("2026-11-23T22:33:01Z WARN acp: Invalid value 'swe-2-max' for config option 'model'",
                     "ERROR could not open session", "Traceback (most recent call last):",
                     "ValueError: bad model", "error: the changes did not apply", "fatal: not a git repository",
                     "ls: drafts/: No such file or directory", "sh: pytest: command not found",
                     "open: out.csv: Permission denied"):
            self.assertEqual(tone(line), "loud", line)
        for line in ("2026-11-23T22:33:00Z INFO acp: session opened", "DEBUG retry 1", "stopped: None"):
            self.assertEqual(tone(line), "quiet", line)
        for line in ("step 12: 3 errors so far", "wrote table.csv", ""):
            self.assertEqual(tone(line), "", line)
        rule = dashboard.STYLE.split("pre.log.ended .ln.loud {", 1)[1].split("}", 1)[0]
        self.assertIn("border-left: 2px solid", rule)
        self.assertIn("pre.log.ended .ln.quiet {", dashboard.STYLE)

    def test_on_a_phone_the_new_jobs_question_sits_on_its_composer(self):
        # A question centred mid-screen, a hand's width of empty navy above the composer it labels, must fail.
        rule = dashboard.STYLE.split(".sea-page > .dock > .c-strip > .compose-intro {", 1)[1].split("}", 1)[0]
        self.assertIn("align-content: end", rule)
        self.assertNotIn("center", rule)

    def test_an_empty_inbox_says_so_once(self):
        # An empty Inbox that says 'Nothing needs you.' in its list and again beside it, or keeps an empty list
        # column, must fail; an Inbox with something waiting keeps its list.
        empty = {"supervisor": None, "stops": [], "routes": [], "live": [], "not_started": [], "inbox": [],
                 "last_24h": {}, "recent": [], "can_add": True, "box": True}
        page = pane(dashboard.render(empty, NOW, token=TOKEN))
        self.assertEqual(page.count("Nothing needs you."), 1)
        self.assertNotIn('class="ib-list"', page)
        self.assertIn('data-view="empty"', page)
        # the top bar does not echo it as 'nothing needs you · 0 settled today'
        self.assertIn('.pane:has(.ibx[data-view="empty"]) .top-meta { display: none; }', dashboard.STYLE)
        busy = pane(dashboard.render({**live_view(), "box": True}, NOW, token=TOKEN))
        self.assertIn('class="ib-list"', busy)
        self.assertNotIn('data-view="empty"', busy)

    def test_every_log_reads_at_one_size_and_pitch(self):
        # A running log at one pitch and an ended one at another, a page that shrinks its logs, or a running card whose
        # line count sits inline while the others sit right, must fail.
        rule = dashboard.STYLE.split("pre.log, pre.peek {", 1)[1].split("}", 1)[0]
        self.assertIn("font-size: 13px", rule)
        self.assertIn("line-height: 20px", rule)
        self.assertNotIn(".jd-body :is(.log, .peek) { font-size", dashboard.STYLE)
        later = re.findall(r"pre\.log[^{,)]*\{([^}]*)\}", dashboard.STYLE.split("pre.log, pre.peek {", 1)[1])
        self.assertFalse([r for r in later if re.search(r"font-size|line-height", r)])
        self.assertNotIn("pre.log.ended .ln + .ln { margin-top", dashboard.STYLE)
        count = dashboard.STYLE.split(":is(.out .head, .worked-summary) .lines {", 1)[1].split("}", 1)[0]
        for spec in ("margin-left: auto", "font-size: 12px", "color: #7d8394"):
            self.assertIn(spec, count)

    def test_the_new_job_page_is_headed_by_its_repository(self):
        # A new-job page headed by a question any agent app could ask while the repository hides in a row under the
        # composer, a repository named twice, or a page with no repository left without a heading, must fail.
        view = {**live_view(), "workspace": {"repo": "research-harness", "branch": "rh3-composer-agents"}}
        page = pane(dashboard.render(view, NOW, token=TOKEN))
        self.assertIn('<h1 class="compose-title" id="compose-title" tabindex="-1" data-repo="research-harness">'
                      'research-harness</h1>', page)
        strip = page.split('<section class="c-strip', 1)[1].split("</section>", 1)[0]
        self.assertEqual(strip.count(">research-harness<"), 1)
        self.assertIn(">rh3-composer-agents<", page)
        self.assertIn(".c-strip .where-bar { top: auto; bottom: calc(100% + ", dashboard.STYLE)
        # on a phone, with no row under it, the composer sits at the reply box's 24px from the foot
        self.assertIn(".sea-page > .dock:has(> .c-strip.has-repo) { padding-bottom: calc(24px + env(safe-area-inset-bottom)); }",
                      dashboard.STYLE)
        bare = pane(dashboard.render(live_view(), NOW, token=TOKEN))
        self.assertIn('id="compose-title" tabindex="-1">What are we building?</h1>', bare)

    def test_every_floating_surface_is_cut_from_one_navy(self):
        # Menus and the model picker floated on navy glass while the Cancel popover and the toast were neutral grey:
        # two families of floating surface, the grey reading as unthemed (critic r9 F8). One rule styles them all
        # (a failure toast keeps its red), and an input inside a popover sits on the same navy, darker.
        rule = dashboard.STYLE.split("/* Floating surfaces are cut from the app's navy", 1)[1]
        selectors, decls = re.search(r"\n([^{]*\.mp-pop[^{]*)\{([^}]*)\}", rule).groups()
        for sel in (".mp-pop", ".ef-pop", ".dock .opts-body", "form.pop", ".toast:not(.no)"):
            self.assertIn(sel, selectors)
        self.assertIn("rgb(19 24 38 / .96)", decls)
        self.assertIn("backdrop-filter: blur(40px) saturate(160%)", decls)  # the menus' own glass
        self.assertIn('form.pop :is(input[type="text"], textarea) { background: #0e1220;', dashboard.STYLE)

    def test_a_phones_new_page_has_no_shade_over_empty_water(self):
        # The centre shade behind the desktop composer showed on a phone as a dark oval over empty water, its
        # composer being at the foot (critic r9 F15); the desktop keeps it.
        self.assertIn("@media (max-width: 600px) { html[data-live-app] .sea-page::before { background: none; } }",
                      dashboard.STYLE)
        self.assertIn("html[data-live-app] .sea-page::before { background: radial-gradient(", dashboard.STYLE)

    def test_all_inline_code_in_an_answer_is_one_light_chip(self):
        # A 90-character literal became three stacked heavy chips on a phone (critic r10 F4); dropping the chip past
        # 40 characters then left two code treatments in one sentence, the long one bare mono (r11 F7). Every inline
        # code is one light chip; a long one wraps at its seams and each line keeps its own padding and corners.
        rule = lambda sel: dashboard.STYLE.split("\n" + sel + " {", 1)[1].split("}", 1)[0]  # noqa: E731
        chip = rule(".assistant-message.md :not(pre) > code")
        for spec in ("font-size: .875em", "padding: 0 4px", "border-radius: 4px", "background: rgb(255 255 255 / .06)",
                     "-webkit-box-decoration-break: clone", "box-decoration-break: clone"):
            self.assertIn(spec, chip)
        self.assertNotIn("code:not(.nw)", dashboard.STYLE)
        self.assertIn("white-space: nowrap", rule(".assistant-message.md code.nw"))
        self.assertIn('<code class="nw">x</code>', dashboard._md_inline("`x`"))

    def test_a_folded_brief_fades_and_adds_no_characters(self):
        # The brief bubble's fold took the line clamp's ellipsis: after a full stop it read 'tests....', after a space
        # 'Running ...', while the Inbox faded (critic r10 F1). Its text sits in its own span, which folds to three
        # lines and fades the end of the third over 64px; the bubble keeps its fill (a mask on it would cut a hole in
        # its translucent background) and is no longer clamped itself.
        page = pane(dashboard.render(opened(live_view(), "live-job"), NOW, token=TOKEN))
        self.assertRegex(page, r'<div class="user-message"><span class="um-t">[^<]+</span></div>')
        rule = lambda sel: dashboard.STYLE.split("\n" + sel + " {", 1)[1].split("}", 1)[0]  # noqa: E731
        fold = rule(".user-message:not(.whole) > .um-t")
        for spec in ("display: block", "max-height: 3lh", "overflow: hidden"):
            self.assertIn(spec, fold)
        fade = rule(".user-message:has(+ .ask-more):not(.whole) > .um-t")
        self.assertIn("linear-gradient(to right, #000 calc(100% - 64px), transparent)", fade)
        bubble = rule(".user-message:has(> .um-t):has(+ .ask-more):not(.whole)")
        for spec in ("-webkit-line-clamp: unset", "overflow: visible", "max-height: none"):
            self.assertIn(spec, bubble)
        self.assertIn("b.querySelector(':scope > .um-t') || b", dashboard.SCRIPT)
        # the Inbox title fades the same way
        self.assertIn("linear-gradient(to right, #000 calc(100% - 64px), transparent)",
                      rule(".q-head .q-title.user-message:has(+ .ask-more):not(.whole)"))

    def test_a_files_preview_toggle_sits_on_its_row(self):
        # 'Preview' hung 18px in under its file while 'Output' sat on the column edge, so two sibling folds read as a
        # nesting (critic r9 F11). It is a trailing 'Preview ›' on the file's own row, and a long name stops short of it.
        rule = lambda sel: dashboard.STYLE.split("\n" + sel + " {", 1)[1].split("}", 1)[0]  # noqa: E731
        toggle = rule(".session-file > .file-preview > summary")
        for spec in ("position: absolute", "top: 0", "right: 0", "flex-direction: row-reverse"):
            self.assertIn(spec, toggle)
        self.assertIn("position: relative", rule(".session-file"))
        self.assertIn("margin-left: 0", rule(".session-file > .file-preview"))
        self.assertIn("padding-right:", rule(".session-file:has(> .file-preview) > .session-file-main"))

    def test_the_new_job_page_names_its_keys_under_the_composer(self):
        # A /new page without the hint, the hint above the composer or on another page, a line that stays drawn
        # while the brief holds text, or one a phone still draws, must fail.
        page = pane(dashboard.render({**live_view(), "new": True}, NOW, token=TOKEN))
        strip = re.search(r'<section class="c-strip.*?</section>', page, re.S).group(0)
        self.assertIn(dashboard._SEP_HTML.join(("<kbd>/</kbd> commands", "<kbd>⌘K</kbd> jump to anything")),
                      re.search(r'<p class="compose-hint" aria-hidden="true">(.*?)</p>', strip).group(1))
        self.assertLess(strip.index("</form>"), strip.index("compose-hint"))
        for other in (pane(dashboard.render(live_view(), NOW, token=TOKEN)),
                      pane(dashboard.render(opened(live_view(), "live-job"), NOW, token=TOKEN))):
            self.assertNotIn("compose-hint", other)
        self.assertIn(".c-strip:has(.c-text:not(:placeholder-shown)) .compose-hint { visibility: hidden; }",
                      dashboard.STYLE)
        blocks = re.findall(r"@media \(max-width: 600px\) \{(.*?)\n\}", dashboard.STYLE, re.S)
        self.assertTrue(any(re.search(r"\.compose-hint \{[^}]*display: none", b) for b in blocks))
        # it steps aside while a composer menu is open: half of it showed beside the model picker and through the
        # options menu (critic r10 F2). The picker and effort panels live on <body> while open; the rest in the strip.
        hide = re.search(r"\n([^{}]*)\{ opacity: 0; \}", dashboard.STYLE.split("/* The hint steps aside", 1)[1]).group(1)
        for sel in ("body:has(> .mp-pop, > .ef-pop) .compose-hint", ".c-strip:has(.opts[open], .slash:not([hidden]))"):
            self.assertIn(sel, hide)
        self.assertIn("transition: opacity .12s cubic-bezier(.16, 1, .3, 1)",
                      dashboard.STYLE.split(".compose-hint {", 1)[1].split("}", 1)[0])
        # its key caps are boxes (an inline kbd ignores height), 18px tall
        caps = dashboard.STYLE.split(".compose-hint kbd {", 1)[1].split("}", 1)[0]
        self.assertIn("display: inline-block", caps)
        self.assertIn("height: 18px", caps)

    def test_cancel_says_what_it_does_to_a_job_that_already_ran(self):
        # A stalled job's Cancel said "It will not start", about a job that has been running for hours. rh job cancel
        # only marks a stalled job cancelled and stops nothing (it says so), so the popover must say that, and a job
        # that has not started keeps its own line.
        stalled = opened(live_view(), "live-job", kind="local-shell", state="stalled")
        page = dashboard.render(stalled, NOW, token=TOKEN)
        pop = page.split('action="/job/cancel"', 1)[1].split("</form>", 1)[0]
        self.assertNotIn("It will not start", pop)
        self.assertIn("It ends here as cancelled. If its process still runs, end it yourself.", pop)
        queued = dashboard.render(opened(live_view(), "queued-job"), NOW, token=TOKEN)
        self.assertIn("It will not start. You can run it again later.",
                      queued.split('action="/job/cancel"', 1)[1].split("</form>", 1)[0])

    def test_on_a_phone_pause_and_stop_are_one_capsule(self):
        # Four equal glass squares made the running header four unrelated buttons around a squeezed name (critic r9
        # F3). Pause and Stop must join into one 72px capsule split by a hairline, their touch areas must not
        # overlap at the seam (a tap there would hit whichever came last), and the name gets the room back.
        spec = dashboard.STYLE.split("one button spec", 1)[1].split("\n}", 1)[0]
        rule = lambda sel: spec.split("\n  " + sel + " {", 1)[1].split("}", 1)[0]  # noqa: E731
        self.assertIn("gap: 0", rule(".job-actions:has(.act + .act)"))
        first = rule(".job-actions .act:has(+ .act) > summary")
        self.assertIn("border-radius: 10px 0 0 10px", first)
        self.assertIn("border-right: 0", first)
        second = rule(".job-actions .act + .act > summary")
        self.assertIn("border-radius: 0 10px 10px 0", second)
        self.assertIn("border-left: 1px solid rgb(255 255 255 / .10)", second)
        # each half's 44px touch area reaches outward from the seam, so the two never share a point
        self.assertIn("right: 0", rule(".job-actions .act:has(+ .act) > summary::after"))
        self.assertIn("left: 0", rule(".job-actions .act + .act > summary::after"))
        two = int(re.search(r"padding-right: (\d+)px", rule(".detail:has(.job-actions .act + .act) .jd-head-in")).group(1))
        self.assertEqual(two, 56 + 72 + 8)  # header edge to capsule, the capsule, one gap

    def test_on_a_phone_cancel_is_an_icon_with_room_for_two(self):
        # A phone header where Cancel still takes a word's width, an icon that is not the stop square, or title
        # room for only one button while Pause and Cancel both show, must fail.
        blocks = re.findall(r"@media \(max-width: 600px\) \{(.*?)\n\}", dashboard.STYLE, re.S)
        block = next(b for b in blocks if 'summary[data-k="c"]' in b)
        rule = lambda sel: block.split(sel + " {", 1)[1].split("}", 1)[0]  # noqa: E731
        for spec in ("width: 44px", "height: 44px", "padding: 0", "justify-content: center", "font-size: 0",
                     "border: 0"):
            self.assertIn(spec, rule('.job-actions .act > summary[data-k="c"]'))
        # a filled stop square, so Pause and Cancel read as one transport pair; an x would read as "close this view"
        stop = rule('.job-actions .act > summary[data-k="c"]::before')
        for spec in ("width: 10px", "height: 10px", "border-radius: 2px", "background: currentColor"):
            self.assertIn(spec, stop)
        self.assertNotIn("linear-gradient", stop)
        one = int(re.search(r"padding-right: (\d+)px",
                            rule(".detail:has(.job-actions .act) .jd-head-in")).group(1))
        two = int(re.search(r"padding-right: (\d+)px",
                            rule(".detail:has(.job-actions .act + .act) .jd-head-in")).group(1))
        self.assertGreaterEqual(two, one + 44)
        # and it wears the header's one button spec, the same glass square as Pause beside it
        spec = dashboard.STYLE.split("one button spec", 1)[1].split("\n}", 1)[0]
        shared = next(r for r in spec.split("}") if "width: 36px; height: 36px" in r).split("{")[-2]
        self.assertIn('summary[data-k="s"]', shared)
        self.assertIn('summary[data-k="c"]', shared)
        page = dashboard.render(opened(live_view(), "queued-job"), NOW, token=TOKEN)
        self.assertRegex(page, r'<summary class="[^"]*" data-k="s">Pause job…</summary>')
        self.assertRegex(page, r'<summary class="[^"]*" data-k="c">Cancel job…</summary>')

    def test_a_running_jobs_cut_log_says_last_lines(self):
        # A running job whose page holds only the end of its log, shown as if it were all of it, or a whole log
        # called the last lines, must fail.
        view = opened(live_view(), "live-job", kind="local-shell")
        view["open"]["log"] = "\n".join(f"line {n}" for n in range(200))
        page = pane(dashboard.render(view, NOW, token=TOKEN))
        self.assertIn('<span class="lines mono">200 lines</span>', page)
        view["open"]["log_cut"] = True
        page = pane(dashboard.render(view, NOW, token=TOKEN))
        self.assertIn('<span class="lines mono">last 200 lines</span>', page)
        self.assertNotIn('<div class="jd-dock">', page)

    def test_a_job_keeps_its_full_request_and_searchable_title(self):
        # A clipped request or a palette entry too long to read would make it harder to identify the job.
        view = opened(live_view(), "live-job")
        first = ("Read every nightly log from the last ninety days, list each test that timed out with its slowest "
                 "run, and flag any test that timed out more than twice")
        view["open"]["job"]["spec"]["brief"] = (first + ". Write it to\ntimeouts.md.\nKeep it short.\n\nCover:\n"
                                                "- each test\n  and its slowest run\n2) the date\n\n```\nlog --a\nb\n```")
        view["meta"] = {"live-job": {"brief": view["open"]["job"]["spec"]["brief"]}}
        page = dashboard.render(view, NOW, token=TOKEN)
        self.assertIn(f"{first}</h1>", page)
        prompt = re.search(r'<div class=\"user-message\"><span class=\"um-t\">(.*?)</span></div>', page, re.S).group(1)
        # Hard-wrapped lines run on; a blank line, a list item and a fenced block keep their own lines.
        self.assertEqual(html.unescape(prompt), first + ". Write it to timeouts.md. Keep it short.\n\nCover:\n"
                         "- each test and its slowest run\n2) the date\n\n```\nlog --a\nb\n```")
        listed = re.search(r'href="/job/live-job\?t=[^"]*" data-nav><span class="p-ic">.*?<span class="p-t">(.*?)</span>',
                           page, re.S).group(1)
        self.assertTrue(listed.endswith("…") and len(listed) <= 100 and first.startswith(listed[:-1]), listed)
        self.assertIn("text-overflow: ellipsis", dashboard.STYLE.split(".detail .jd-head h1 {", 1)[1].split("}", 1)[0])

    def test_a_wrapped_brief_or_an_abbreviation_still_heads_with_the_whole_sentence(self):
        # A heading that stops at a brief's first line break, at "e.g." or at a first list item's "1.", one that runs
        # past "etc." into the next capitalised sentence, or a list under it run into one line, must fail.
        cases = [("Read every nightly log from the last\nninety days and list each timeout. Write timeouts.md.",
                  "Read every nightly log from the last ninety days and list each timeout",
                  "Read every nightly log from the last ninety days and list each timeout. Write timeouts.md."),
                 ("Compare e.g. two runs.", "Compare e.g. two runs", "Compare e.g. two runs."),
                 ("Scan these logs:\n- build.log\n- test.log", "Scan these logs:", "Scan these logs:\n- build.log\n- test.log"),
                 ("1. Do X.\n2. Do Y.", "1. Do X", "1. Do X.\n2. Do Y."),
                 ("Collect logs, traces, etc. Then stop.", "Collect logs, traces, etc.", "Collect logs, traces, etc. Then stop.")]
        for brief, heading, prompt in cases:
            with self.subTest(brief=brief):
                view = opened(live_view(), "live-job")
                view["open"]["job"]["spec"]["brief"] = brief
                view["meta"] = {"live-job": {"brief": brief}}
                page = dashboard.render(view, NOW, token=TOKEN)
                self.assertIn(f">{html.escape(heading, quote=False)}</h1>", page)
                content = re.search(r'<div class=\"user-message\"><span class=\"um-t\">(.*?)</span></div>', page, re.S)
                self.assertEqual(content and html.unescape(content.group(1)), prompt)

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

    def test_sessions_use_inline_status_and_inbox_keeps_its_seat_tones(self):
        for jid, status in (("broke-job", "Failed"), ("finished-job", "Done"), ("live-job", "Running")):
            with self.subTest(jid=jid):
                view = opened(live_view(), jid)
                page = pane(dashboard.render(view, NOW, token=TOKEN))
                self.assertRegex(page, rf'<p class="job-result"[^>]*><svg class="st [^"]+"[^>]*>.*?</svg>'
                                       rf'<span class="job-result-status sl"><span class="nowrap">{re.escape(status)}</span>'
                                       rf'(?:</span>|<wbr>)')
                self.assertNotIn('data-region="j-water"', page)
                self.assertNotIn('<div class="jd-prog">', page)
        done_page = pane(dashboard.render(opened(live_view(), "finished-job"), NOW, token=TOKEN))
        self.assertIn('<svg class="st t-accent"', done_page.split('class="job-result"', 1)[1])
        # A job past its limit says so once, in the status line, with its limit; it is not amber (amber means it needs
        # you) and no second line repeats it. 'still being checked' named nothing and orphaned '~7 h left' on a phone.
        stalled = opened(live_view(), "live-job", state="stalled")
        stalled["open"]["job"]["spec"]["timeout_min"] = 120
        page = pane(dashboard.render(stalled, NOW, token=TOKEN))
        self.assertRegex(page, r'<p class="job-result"[^>]*><svg class="st t-accent"[^>]*data-s="late"[^>]*>.*?</svg>'
                               r'<span class="job-result-status sl"><span class="nowrap">Past its 2 h limit</span>')
        self.assertNotIn("still being checked", page.split('class="job-result"', 1)[1].split("</p>", 1)[0])
        self.assertNotIn('data-region="j-now"', page)
        work = text(page).split("Job details", 1)[0]  # the details panel keeps its own State row
        self.assertEqual(work.count("time limit") + work.count("2 h limit"), 1)
        self.assertNotIn('class="st t-amber"', page.split('class="job-result"', 1)[1].split("</p>", 1)[0])
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
        # the grey line is the state and the time only: no job id or slug unless two names clash, though a typed
        # id still finds the row
        self.assertRegex(page, r'<a class="p-row" role="option" data-id="web-job" [^>]*>.*?'
                               r'<span class="p-s"><span class="p-w">Launch by hand</span>'
                               r'(?:<span class="p-when mono num">[^<]+</span>)?</span>')
        pal_rows = re.findall(r'<a class="p-row"[^>]*data-id="[^"]+".*?</a>', page, re.S)
        self.assertTrue(pal_rows)
        for row in pal_rows:
            if '<span class="p-t">' + re.search(r'data-id="([^"]+)"', row).group(1) + "</span>" not in row:
                self.assertNotIn('<span class="mono">', row.split('class="p-s"', 1)[1])
        # its jobs are grouped as the sidebar groups them, and Needs you holds exactly what the badge counts
        pal = re.search(r'<dialog class="pal".*?</dialog>', page, re.S).group(0)
        needs = pal.split('<p class="p-sec">Needs you</p>', 1)[1].split('<p class="p-sec">', 1)[0]
        badge = int(re.search(r'<b class="count num" data-need="(\d+)"', page).group(1))
        self.assertEqual(needs.count('class="p-row"'), badge)
        self.assertNotIn('<p class="p-sec">Jobs</p>', pal)
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
