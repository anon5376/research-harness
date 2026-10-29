import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from rh.adapters.codex_cloud import CodexCloud, RouteUnavailable, classify, map_status

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "fakes" / "codex"
TID = "task_e_" + "0123456789abcdef" * 2
OLD_TID = "task_e_" + "fedcba9876543210" * 2
NOT_FOUND = "Error: environment 'x' not found; run `codex cloud` to list available environments\n"


def resp(stdout="", stderr="", exit=0, **extra):
    return {"exit": exit, "stdout": stdout, "stderr": stderr, **extra}


class FakeCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.state, self.log = self.dir / "state.json", self.dir / "log.jsonl"
        env = mock.patch.dict(os.environ, {"FAKE_CODEX_STATE": str(self.state), "FAKE_CODEX_LOG": str(self.log)})
        env.start()
        self.addCleanup(env.stop)
        self.assertTrue(os.access(FAKE, os.X_OK), "tests/fakes/codex must be executable")
        self.cli = CodexCloud(codex=str(FAKE), timeout=30)

    def given(self, **state):
        self.state.write_text(json.dumps(state))

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line)["argv"][1:] for line in self.log.read_text().splitlines()]

    def reason(self, fn, *args):
        with self.assertRaises(RouteUnavailable) as cm:
            fn(*args)
        return cm.exception


class LaunchTest(FakeCliTest):
    def test_prompt_is_one_argv_element_never_seen_by_a_shell(self):
        marker = self.dir / "shell-ran"
        prompt = f'line one\nsay "hi" $(touch {marker}) `touch {marker}`; touch {marker}\n'
        url = "https://chatgpt.com/codex/tasks/" + TID
        self.given(exec=resp(stdout=f"Created task {TID}\nView it ({url}).\n"))
        r = self.cli.launch("env_x", prompt)
        self.assertEqual(self.calls(), [["cloud", "exec", "--env", "env_x", "--branch", "main", prompt]])
        self.assertEqual((r["task_id"], r["url"]), (TID, url))
        self.assertIn(TID, r["raw"])
        self.assertFalse(marker.exists())

    def test_id_found_in_stderr_and_branch_passed(self):
        self.given(exec=resp(stderr=f"submitted {TID}\n"))
        r = self.cli.launch("env_x", "p", branch="work")
        self.assertEqual((r["task_id"], r["url"]), (TID, None))
        self.assertEqual(self.calls()[0][4:6], ["--branch", "work"])

    def test_terminal_links_and_colours_do_not_hide_the_id_or_url(self):
        url = "https://chatgpt.com/codex/tasks/" + TID
        self.given(exec=resp(stdout=f"\x1b[1mCreated\x1b[0m \x1b]8;;{url}\x1b\\open task\x1b]8;;\x1b\\\n"))
        r = self.cli.launch("env_x", "p")
        self.assertEqual((r["task_id"], r["url"], r["raw"]), (TID, url, "Created open task"))

    def test_ids_quoted_in_the_prompt_are_not_the_new_task(self):
        prompt = f"compare with {OLD_TID}"
        self.given(exec=resp(stdout=f"query: {prompt}\ncreated {TID}\n"))
        self.assertEqual(self.cli.launch("env_x", prompt)["task_id"], TID)

    def test_created_task_wins_over_a_failing_exit_code(self):
        self.given(exec=resp(stdout=f"created {TID}\n", stderr="Error: lost the log stream\n", exit=1))
        self.assertEqual(self.cli.launch("env_x", "p")["task_id"], TID)

    def test_success_without_task_id_is_an_error(self):
        self.given(exec=resp(stdout="Submitted.\n"))
        self.assertEqual(self.reason(self.cli.launch, "env_x", "p").reason, "error")

    def test_unknown_environment_is_env_not_found(self):
        self.given(exec=resp(stderr=NOT_FOUND, exit=1))
        e = self.reason(self.cli.launch, "x", "p")
        self.assertEqual((e.reason, e.retry_after), ("env-not-found", None))
        self.assertIn("not found", e.detail)

    def test_failures_map_to_reasons(self):
        cases = [
            ("Error: 429 Too Many Requests; retry after 30s", "rate-limited", 30.0),
            ("Error: rate limit reached, try again in 2 minutes", "rate-limited", 120.0),
            ("Error: rate limited", "rate-limited", None),
            ("Error: request failed: 401 Unauthorized", "auth", None),
            ("Error: not logged in; run `codex login`", "auth", None),
            ("Error: backend returned 500", "error", None),
        ]
        self.given(exec=[resp(stderr=text + "\n", exit=1) for text, _, _ in cases])
        for text, reason, retry in cases:
            with self.subTest(text=text):
                e = self.reason(self.cli.launch, "env_x", "p")
                self.assertEqual((e.reason, e.retry_after), (reason, retry))

    def test_stderr_is_classified_before_stdout(self):
        self.given(exec=resp(stdout="query: check why environment foo was not found\n",
                             stderr="Error: 401 Unauthorized\n", exit=1))
        self.assertEqual(self.reason(self.cli.launch, "env_x", "p").reason, "auth")

    def test_flag_like_or_empty_arguments_are_refused_before_any_call(self):
        self.given(exec=resp(stdout=TID))
        for env, prompt, branch in (("-x", "p", "main"), ("", "p", "main"), ("env", "--help", "main"),
                                    ("env", "  ", "main"), ("env", "p", "--force"), ("env\n", "p", "main")):
            with self.subTest(env=env, prompt=prompt, branch=branch), self.assertRaises(ValueError):
                self.cli.launch(env, prompt, branch=branch)
        self.assertEqual(self.calls(), [])

    def test_missing_cli_is_cli_missing(self):
        e = self.reason(CodexCloud(codex=str(self.dir / "no-such-codex")).launch, "env_x", "p")
        self.assertEqual(e.reason, "cli-missing")

    def test_hung_cli_times_out(self):
        self.given(exec=resp(stdout=TID, sleep=30))
        t0 = time.monotonic()
        e = self.reason(CodexCloud(codex=str(FAKE), timeout=0.5).launch, "env_x", "p")
        self.assertEqual(e.reason, "timeout")
        self.assertLess(time.monotonic() - t0, 10)

    def test_injected_run_gets_argv_list_timeout_cwd_and_no_shell(self):
        seen = []

        def run(argv, **kw):
            seen.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, ("ok " + TID).encode(), b"")

        CodexCloud(codex="codex", run=run, timeout=7, cwd="state-dir").launch("env_x", "p")
        argv, kw = seen[0]
        self.assertEqual(argv[:3], ["codex", "cloud", "exec"])
        self.assertEqual((kw["timeout"], kw["cwd"]), (7.0, "state-dir"))
        self.assertFalse(kw.get("shell", False))
        self.assertIs(kw["stdin"], subprocess.DEVNULL)

    def test_every_call_runs_in_the_given_cwd(self):
        home = self.dir / "rh-home"
        home.mkdir()
        self.given(exec=resp(stdout=TID), status={TID: resp("[READY] t\n")}, diff={TID: resp("")},
                   list=resp(json.dumps({"tasks": []})))
        cli = CodexCloud(codex=str(FAKE), timeout=30, cwd=home)
        cli.launch("env_x", "p"), cli.status(TID), cli.diff(TID), cli.list(), cli.health()
        cwds = [json.loads(line)["cwd"] for line in self.log.read_text().splitlines()]
        self.assertEqual(len(cwds), 6)
        self.assertEqual({os.path.realpath(c) for c in cwds}, {os.path.realpath(home)})

    def test_missing_cwd_is_not_reported_as_missing_cli(self):
        e = self.reason(CodexCloud(codex=str(FAKE), cwd=self.dir / "gone").launch, "env_x", "p")
        self.assertEqual(e.reason, "error")
        self.assertIn("working directory", e.detail)


class StatusTest(FakeCliTest):
    def test_sequence_maps_running_then_completed_and_keeps_the_last_reply(self):
        self.given(status={TID: [resp("[PENDING] probe\nenv  •  now\n"),
                                 resp("[READY] probe\nenv  •  4h ago\n+190/-0 • 1 file\n")]})
        states = [self.cli.status(TID) for _ in range(3)]
        self.assertEqual([s["state"] for s in states], ["running", "completed", "completed"])
        self.assertEqual(states[1], {"state": "completed", "raw": "READY", "title": "probe",
                                     "detail": "env  •  4h ago\n+190/-0 • 1 file"})
        self.assertEqual(self.calls(), [["cloud", "status", TID]] * 3)
        self.assertEqual(len(json.loads(self.state.read_text())["status"][TID]), 1)

    def test_every_known_word_maps_and_unknown_words_never_end_a_task(self):
        table = {"PENDING": "running", "queued": "running", "IN_PROGRESS": "running", "in-progress": "running",
                 "In Progress": "running", "running": "running", "READY": "completed", "APPLIED": "completed",
                 "completed": "completed", "ERROR": "failed", "failed": "failed", "cancelled": "aborted",
                 "CANCELED": "aborted", "[READY]": "completed", "weird-new-state": "unknown", "archived": "unknown",
                 "": "unknown", None: "unknown"}
        for raw, state in table.items():
            with self.subTest(raw=raw):
                self.assertEqual(map_status(raw), state)

    def test_unknown_word_from_the_cli_is_unknown(self):
        self.given(status={TID: resp("[ARCHIVED] probe\n")})
        s = self.cli.status(TID)
        self.assertEqual((s["state"], s["raw"]), ("unknown", "ARCHIVED"))

    def test_status_line_wins_over_a_failing_exit(self):
        self.given(status={TID: resp("[ERROR] probe\nenv  •  1m ago\n", "Error: task failed\n", exit=1)})
        self.assertEqual(self.cli.status(TID)["state"], "failed")

    def test_output_without_status_line(self):
        self.given(status={TID: [resp("no status here\n"), resp(stderr="Error: task not found\n", exit=1)]})
        self.assertEqual(self.cli.status(TID)["state"], "unknown")
        self.assertEqual(self.reason(self.cli.status, TID).reason, "error")

    def test_banner_lines_and_colour_codes_are_skipped(self):
        self.given(status={TID: resp("note: a newer codex is available\n\x1b[32m[READY]\x1b[0m probe\n")})
        s = self.cli.status(TID)
        self.assertEqual((s["state"], s["title"]), ("completed", "probe"))

    def test_bad_task_ids_are_refused_before_any_call(self):
        for bad in ("-x", "", "a b", "a\n", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.cli.status(bad)
        self.assertEqual(self.calls(), [])


class ListDiffHealthTest(FakeCliTest):
    def test_list_adds_mapped_state(self):
        tasks = [{"id": TID, "status": "ready", "title": "t"}, {"id": OLD_TID, "status": "pending"}, "junk"]
        self.given(list=resp(json.dumps({"tasks": tasks, "cursor": None})))
        got = self.cli.list(limit=5)
        self.assertEqual(self.calls(), [["cloud", "list", "--json", "--limit", "5"]])
        self.assertEqual([(t["id"], t["state"]) for t in got], [(TID, "completed"), (OLD_TID, "running")])
        self.assertEqual(got[0]["title"], "t")

    def test_list_rejects_output_that_is_not_the_task_json(self):
        self.given(list=[resp("not json"), resp(json.dumps({"items": []}))])
        self.assertEqual(self.reason(self.cli.list).reason, "error")
        self.assertEqual(self.reason(self.cli.list).reason, "error")
        for bad in (0, -1, True, "5"):
            with self.subTest(limit=bad), self.assertRaises(ValueError):
                self.cli.list(limit=bad)

    def test_diff_is_returned_byte_for_byte(self):
        body = "diff --git a/runs/p/x.txt b/runs/p/x.txt\n@@ -1 +1 @@\n-a\r\n+b\r\n+ünï\n"
        self.given(diff={TID: resp(body)})
        self.assertEqual(self.cli.diff(TID), body)
        self.assertEqual(self.calls(), [["cloud", "diff", TID]])

    def test_diff_for_an_unknown_task_raises(self):
        self.given(diff={})
        self.assertEqual(self.reason(self.cli.diff, TID).reason, "error")

    def test_health_ok_names_the_version(self):
        self.given(version="codex-cli 9.9.9", list=resp(json.dumps({"tasks": [], "cursor": None})))
        h = self.cli.health()
        self.assertEqual((h["ok"], h["version"]), (True, "codex-cli 9.9.9"))
        self.assertEqual(self.calls(), [["--version"], ["cloud", "list", "--limit", "1", "--json"]])

    def test_health_reports_why_the_list_failed(self):
        self.given(version="codex-cli 9.9.9", list=resp(stderr="Error: not logged in\n", exit=1))
        h = self.cli.health()
        self.assertEqual((h["ok"], h["version"]), (False, "codex-cli 9.9.9"))
        self.assertIn("auth", h["detail"])

    def test_health_without_cli(self):
        h = CodexCloud(codex=str(self.dir / "no-such-codex")).health()
        self.assertEqual((h["ok"], h["version"]), (False, None))
        self.assertIn("cli-missing", h["detail"])


class ClassifyTest(unittest.TestCase):
    def test_retry_after_units_and_dates(self):
        cases = {"429; Retry-After: 1.5": 1.5, "rate limit, try again in 500ms": 0.5, "429 retry in 2h": 7200.0,
                 "429: retry after 2026-10-29T10:00:00Z": None, "rate limit; retry after 12 seconds.": 12.0}
        for text, retry in cases.items():
            with self.subTest(text=text):
                self.assertEqual(classify(text), ("rate-limited", retry))

    def test_hex_inside_ids_is_not_a_status_code(self):
        self.assertEqual(classify("Error: task " + "task_e_" + "ab429cd401ef" + " failed")[0], "error")

    def test_reason_must_be_known(self):
        with self.assertRaises(ValueError):
            RouteUnavailable("flaky")


if __name__ == "__main__":
    unittest.main()
