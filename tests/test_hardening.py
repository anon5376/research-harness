"""Regression tests for the eight M5h hardening items; external delivery is always faked."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rh import cli, collect, config, contract, protocol
from rh.adapters.codex_cloud import RouteUnavailable
from rh.ledger import Ledger
from rh.supervisor import Supervisor

SHA = "c" * 40


def spec(jid="j1", kind="codex-cloud", **extra):
    raw = {"id": jid, "kind": kind, "brief": "Write notes."}
    if kind == "local-shell":
        raw["command"] = ["true"]
    return contract.normalize({**raw, **extra})


class Cloud:
    def status(self, task):
        return {"state": "completed", "raw": "READY", "detail": ""}

    def diff(self, task):
        raise RouteUnavailable("error", "temporary diff error")


class HarnessCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.cfg = config.load(self.home)
        self.cfg["cloud"]["env_id"] = "env_test"
        self.cfg["lease"].update(lock=str(self.home / "heavy.lock"), max_load=1e9, max_wait=0)
        self.led = Ledger(self.home / "ledger.db", clock=lambda: 1000.0)
        self.sent = []
        self.cloud = Cloud()
        self.s = self.supervisor()

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def supervisor(self):
        def send(cmd, subject, body):
            self.sent.append((subject, body))
            return {"ok": True, "exit": 0, "out": "", "ts": 1000}
        return Supervisor(self.led, self.cfg, cloud=self.cloud, caps={}, policy={},
                          clock=lambda: 1000.0, send=send, pinned=lambda sha: (True, ""))

    def running(self, **extra):
        s = spec(**extra)
        jid = s["id"]
        self.led.add_job(s, pinned_sha=SHA)
        self.led.set_state(jid, "ready")
        self.led.set_state(jid, "launching")
        self.led.set_state(jid, "running", route=s["kind"], external_id="0", started=1.0)
        return self.led.job(jid)

    def local_exited(self, code=0, **extra):
        j = self.running(kind="local-shell", **extra)
        d = self.home / "jobs" / j["id"]
        d.mkdir(parents=True)
        (d / "exit").write_text(str(code))
        (self.home / j["spec"]["write_root"]).mkdir(parents=True)
        return j

    def result_patch(self, **extra):
        """A real scratch repo and an added RESULT file, without changing this checkout."""
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                  capture_output=True, text=True).stdout
        git("init", "-q", "-b", "main")
        git("config", "user.name", "test")
        git("config", "user.email", "test@example.com")
        git("config", "core.hooksPath", "/dev/null")
        (repo / "base").write_text("base\n")
        git("add", "base")
        git("commit", "-q", "-m", "base fixture")
        sha = git("rev-parse", "HEAD").strip()
        self.s.repo = repo
        result = {"job": "j1", "state": "done", "head": sha, "summary": "finished",
                  "checks": [], "outputs": [], "refusals": [], **extra}
        text = json.dumps(result)
        diff = ("diff --git a/runs/j1/RESULT.json b/runs/j1/RESULT.json\n"
                "new file mode 100644\n--- /dev/null\n+++ b/runs/j1/RESULT.json\n"
                "@@ -0,0 +1 @@\n+" + text + "\n")
        self.cloud.diff = lambda task: diff
        self.led.db.execute("UPDATE jobs SET pinned_sha=? WHERE id='j1'", (sha,))
        return self.led.job("j1")

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli.config, "load", return_value=self.cfg), \
                patch.object(cli, "_ledger", return_value=self.led), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = cli.main(list(args))
        return rc, stdout.getvalue(), stderr.getvalue()


class OperatorTest(unittest.TestCase):
    def test_operator_phrases_and_clause_boundaries_are_detected(self):
        # Removing a verb or clause splitter loses one of these actionable steps.
        for text in (
            "Fix the test that does not pass, then push the branch to origin",
            "Fix the test that does not pass then push the branch to origin",
            "Fix the test that does not pass, push the branch to origin",
            "Commit and push your changes", "Create a pull request with the fix",
            "Submit a pull request with the fix", "Email the summary to the reviewers",
            "Message them with the result", "DM your reviewer", "Log into the dashboard",
        ):
            with self.subTest(text=text):
                self.assertTrue(contract.operator_steps(text))

    def test_only_nearby_negations_cancel_a_verb(self):
        for text in ("Do not push.", "Never open a pull request.",
                     "Read the push notification docs.", "Do not ever push changes.",
                     "Don't push changes.", "Do not create a pull request.",
                     "Do not ever actually push changes."):
            with self.subTest(text=text):
                decision = contract.preflight(spec(brief=text), {}, {}, contract.View())
                self.assertEqual(decision.verdict, "ready")
        for text in ("Do not change these docs and push changes", "Do not push these notes and push changes",
                     "Do not ever actually really push changes"):
            with self.subTest(text=text):
                self.assertTrue(contract.operator_steps(text))

    def test_local_command_and_accept_are_preflighted(self):
        for extra in ({"command": ["sh", "-c", "make docs && git push origin main"]},
                      {"accept": "make docs && git push origin main"}):
            with self.subTest(extra=extra):
                decision = contract.preflight(spec(kind="local-shell", **extra), {}, {}, contract.View())
                self.assertEqual(decision.verdict, "operator")
                self.assertTrue(decision.operator_steps)


class RefusalTest(HarnessCase):
    def test_invalid_refusing_result_blocks_and_preserves_refusals(self):
        self.running()
        j = self.result_patch(state="invalid", refusals=["fixture refusal one", "fixture refusal two"])
        self.s._poll_cloud(j, {"finished": 0})
        self.assertEqual(self.led.job("j1")["state"], "blocked")
        self.assertEqual(self.led.receipt("j1")["refusals"], ["fixture refusal one", "fixture refusal two"])
        self.assertEqual([i["action"] for i in self.led.inbox() if i["kind"] == "refusal"],
                         ["fixture refusal one", "fixture refusal two"])


class NotificationTest(HarnessCase):
    def test_blocked_result_sends_only_the_finish_notification(self):
        self.running()
        j = self.result_patch(refusals=["fixture refusal one", "fixture refusal two"])
        self.s._poll_cloud(j, {"finished": 0})
        self.assertEqual(self.led.job("j1")["state"], "blocked")
        self.assertEqual(len(self.led.inbox()), 2)
        self.assertEqual([x[0] for x in self.sent], ["research-harness: j1 blocked"])

    def test_multiple_operator_steps_send_one_notification_and_keep_all_steps(self):
        self.led.add_job(spec(brief="Publish the page. Deploy the service. Announce the release."), pinned_sha=SHA)
        for _ in range(3):
            self.s.tick()
        self.assertEqual(self.led.job("j1")["state"], "held")
        self.assertEqual(len(self.led.inbox()), 3)
        self.assertEqual([x[0] for x in self.sent], ["research-harness: inbox: operator-step"])


class WriteRootTest(unittest.TestCase):
    def test_custom_cloud_root_is_rejected_at_normalization(self):
        for root in ("custom", "custom/", "runs/other/"):
            with self.subTest(root=root), self.assertRaisesRegex(
                    contract.ContractError, r"cloud jobs write to runs/<id>/; put other paths in write_paths"):
                spec(write_root=root)

    def test_default_cloud_root_and_custom_local_root_still_work(self):
        self.assertEqual(spec(write_root="runs/j1")["write_root"], "runs/j1/")
        self.assertEqual(spec(kind="local-shell", write_root="custom")["write_root"], "custom/")


class DiffRetryTest(HarnessCase):
    def test_diff_errors_retry_three_times_with_persisted_count_then_request_harvest(self):
        j = self.running()
        for count in range(1, 4):
            # Recreate the supervisor to prove the count lives in the ledger.
            self.s = self.supervisor()
            self.s.tick()
            j = self.led.job("j1")
            self.assertEqual(j["state"], "collecting")
            self.assertIn(f"diff error {count}/3:", j["detail"])
            self.assertEqual(len(self.led.inbox()), int(count == 3))
            self.assertTrue(self.led.route("codex-cloud")["healthy"])
        with patch.object(self.cloud, "diff", side_effect=AssertionError("must await harvest")):
            self.s.tick()
        self.assertTrue(self.led.job("j1")["detail"].startswith("harvest needed"))

    def test_transient_error_can_recover_on_the_next_tick(self):
        self.running()
        self.s.tick()
        self.result_patch()
        self.s.tick()
        self.assertEqual(self.led.job("j1")["state"], "done")
        self.assertEqual(self.led.inbox(), [])

    def test_nontransient_diff_error_requests_harvest_immediately(self):
        self.running()
        with patch.object(self.cloud, "diff", side_effect=RouteUnavailable("env-not-found", "web only")):
            self.s.tick()
        self.assertTrue(self.led.job("j1")["detail"].startswith("harvest needed"))
        self.assertEqual(len(self.led.inbox()), 1)


class GateTest(HarnessCase):
    def test_heavy_cloud_gate_lease_wait_is_bounded_and_retried(self):
        self.running(heavy=True, accept="true")
        j = self.result_patch()
        def busy(*args, **kw):
            self.assertEqual(kw["max_wait"], 5)
            return 75
        with patch("rh.supervisor.lease.run", side_effect=busy):
            self.s._poll_cloud(j, {"finished": 0})
        self.assertEqual(self.led.job("j1")["state"], "collecting")
        self.assertEqual(self.led.job("j1")["detail"], "gate waiting for the heavy lease")
        self.assertIsNone(self.led.receipt("j1"))
        self.assertEqual(self.sent, [])
        with patch("rh.supervisor.lease.run", return_value=0):
            self.s._poll_cloud(self.led.job("j1"), {"finished": 0})
        self.assertEqual(self.led.job("j1")["state"], "done")

    def test_local_heavy_gate_wait_does_not_finish_and_retries(self):
        j = self.local_exited(heavy=True, accept="true")
        with patch("rh.supervisor.lease.run", return_value=75):
            self.s._poll_local(j, {"finished": 0})
        self.assertEqual(self.led.job("j1")["state"], "collecting")
        self.assertEqual(self.led.job("j1")["detail"], "gate waiting for the heavy lease")
        self.assertIsNone(self.led.receipt("j1"))
        with patch("rh.supervisor.lease.run", return_value=0):
            self.s._poll_local(self.led.job("j1"), {"finished": 0})
        self.assertEqual(self.led.job("j1")["state"], "done")

    def test_nonheavy_gate_uses_job_timeout_and_records_exit_124(self):
        j = self.local_exited(accept="sleep 0.3", timeout_min=0.001)
        self.s._poll_local(j, {"finished": 0})
        self.assertEqual(self.led.job("j1")["state"], "failed")
        self.assertEqual(self.led.receipt("j1")["checks"][-1]["exit"], 124)

    def test_heavy_gate_uses_job_timeout_and_records_exit_124(self):
        j = self.local_exited(heavy=True, accept="sleep 0.3", timeout_min=0.001)
        self.s._poll_local(j, {"finished": 0})
        self.assertEqual(self.led.job("j1")["state"], "failed")
        self.assertEqual(self.led.receipt("j1")["checks"][-1]["exit"], 124)


class RaceTest(HarnessCase):
    def test_status_poll_does_not_overwrite_a_concurrent_harvest(self):
        j = self.running()
        for status in ("running", "completed", "unknown", "failed", "aborted"):
            with self.subTest(status=status):
                # Restore just the fixture between distinct poll races.
                self.led.db.execute("UPDATE jobs SET state='running', detail='' WHERE id='j1'")
                def status_after_harvest(task):
                    self.led.set_state("j1", "collecting", "harvested patch")
                    return {"state": status, "raw": status, "detail": ""}
                with patch.object(self.cloud, "status", side_effect=status_after_harvest):
                    self.s._poll_cloud(j, {"finished": 0})
                self.assertEqual(self.led.job("j1")["state"], "collecting")
                self.assertEqual(self.led.job("j1")["detail"], "harvested patch")
                self.assertIsNone(self.led.receipt("j1"))
        self.assertEqual(self.sent, [])

    def test_alive_rereads_before_marking_an_old_job_stalled(self):
        j = self.running(timeout_min=1)
        self.led.set_state("j1", "collecting", "harvested patch")
        self.s._alive(j)
        self.assertEqual(self.led.job("j1")["state"], "collecting")
        self.assertEqual(self.led.job("j1")["detail"], "harvested patch")
        self.assertEqual(self.sent, [])

    def test_collection_does_not_overwrite_a_concurrent_terminal_state(self):
        j = self.running()
        self.led.set_state("j1", "collecting")
        j = self.led.job("j1")
        rec = collect.receipt(j, "codex-cloud", [], detail="ended elsewhere", ended=1000)
        def ended_elsewhere(*args):
            self.led.finish("j1", "aborted", rec)
            return "done", collect.receipt(j, "codex-cloud", [])
        with patch("rh.supervisor.collect.collect_cloud", side_effect=ended_elsewhere):
            self.s._poll_cloud(j, {"finished": 0})
        self.assertEqual(self.led.job("j1")["state"], "aborted")
        self.assertEqual(self.led.receipt("j1")["detail"], "ended elsewhere")
        self.assertEqual(self.sent, [])

    def test_harvest_renames_a_complete_temporary_patch_over_the_old_patch(self):
        self.running()
        dest = collect.saved_patch(self.home, "j1")
        dest.parent.mkdir(parents=True)
        dest.write_text("old complete patch")
        incoming = Path(self.tmp.name) / "incoming.patch"
        content = "diff --git a/runs/j1/n b/runs/j1/n\nnew contents\n"
        incoming.write_text(content)
        observed = []
        replace = os.replace
        def inspect(source, target, *args, **kw):
            if Path(target).resolve() == dest.resolve():  # macOS: /var is a symlink
                source, dest_now = Path(source).resolve(), dest.resolve()
                observed.append((source != dest_now, source.parent == dest_now.parent,
                                 source.read_text(), dest.read_text()))
            return replace(source, target, *args, **kw)
        with patch("os.replace", side_effect=inspect):
            rc, _, err = self.invoke("cloud", "harvest", "j1", str(incoming))
        self.assertEqual(rc, 0, err)
        self.assertEqual(observed, [(True, True, content, "old complete patch")])
        self.assertEqual(dest.read_text(), content)
        self.assertEqual(list(dest.parent.iterdir()), [dest])
        self.assertEqual(self.led.job("j1")["state"], "collecting")


class SmallItemsTest(HarnessCase):
    def test_branch_unsafe_job_ids_are_rejected_by_both_modules(self):
        for jid in ("a..b", "a.", "a.lock", "a.lock.lock"):
            with self.subTest(jid=jid):
                self.assertIsNone(contract.JOB_ID_RE.fullmatch(jid))
                self.assertIsNone(protocol.JOB_ID_RE.fullmatch(jid))
                with self.assertRaises(contract.ContractError):
                    spec(jid=jid)
                with self.assertRaises(ValueError):
                    protocol.check_job_id(jid)
        for jid in ("a.b", "a-lock", "a.locked"):
            self.assertEqual(spec(jid=jid)["id"], jid)
            self.assertEqual(protocol.check_job_id(jid), jid)

    def test_local_command_exit_75_is_capacity_without_running_accept(self):
        j = self.local_exited(code=75, accept="exit 0")
        self.s._poll_local(j, {"finished": 0})
        self.assertEqual(self.led.job("j1")["state"], "capacity")
        self.assertEqual([c["exit"] for c in self.led.receipt("j1")["checks"]], [75])

    def test_missing_patch_is_a_cli_error_without_traceback(self):
        self.running()
        rc, _, err = self.invoke("cloud", "harvest", "j1", str(Path(self.tmp.name) / "missing.patch"))
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("rh: "))
        self.assertNotIn("Traceback", err)

    def test_configuration_oserror_is_a_cli_error(self):
        err = io.StringIO()
        with patch.object(cli.config, "load", side_effect=OSError("fixture deleted directory")), \
                contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["status"]), 1)
        self.assertEqual(err.getvalue(), "rh: fixture deleted directory\n")

    def test_cancel_prelaunch_states_with_reason_and_receipt(self):
        for state in ("draft", "waiting", "held", "ready", "handoff"):
            with self.subTest(state=state):
                jid = "cancel-" + state
                self.led.add_job(spec(jid=jid, model={"requested": "test-model"}))
                if state == "handoff":
                    for step in ("ready", "launching", "handoff"):
                        self.led.set_state(jid, step)
                elif state != "draft":
                    self.led.set_state(jid, state)
                rc, _, err = self.invoke("job", "cancel", jid, "--reason", "superseded")
                self.assertEqual(rc, 0, err)
                self.assertEqual(self.led.job(jid)["state"], "cancelled")
                rec = self.led.receipt(jid)
                self.assertEqual({k: rec[k] for k in ("route", "checks", "model_requested", "model_served", "detail")},
                                 {"route": "none", "checks": [], "model_requested": "test-model",
                                  "model_served": None, "detail": "superseded"})
                self.assertIsInstance(rec["ended"], (int, float))

    def test_cancel_rejects_every_other_state_and_names_it(self):
        for state in ("launching", "running", "stalled", "collecting", "done", "failed", "blocked",
                      "capacity", "truncated", "aborted", "no_result", "cancelled"):
            with self.subTest(state=state):
                jid = "reject-" + state
                self.led.add_job(spec(jid=jid))
                self.led.db.execute("UPDATE jobs SET state=? WHERE id=?", (state, jid))
                rc, _, err = self.invoke("job", "cancel", jid, "--reason", "superseded")
                self.assertEqual(rc, 1)
                self.assertIn(state, err)
                self.assertEqual(self.led.job(jid)["state"], state)
                self.assertIsNone(self.led.receipt(jid))


if __name__ == "__main__":
    unittest.main()
