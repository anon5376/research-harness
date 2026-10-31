import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from rh import config, contract
from rh.adapters.codex_cloud import RouteUnavailable
from rh.ledger import Ledger
from rh.supervisor import Supervisor

ROOT = Path(__file__).resolve().parents[1]
CAPS = contract.load_caps(ROOT / "caps.toml")
POLICY = contract.load_policy(ROOT / "policy.toml")
SHA = "c" * 40
TASK = "task_e_" + "5" * 32


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeCloud:
    """Down until up_at; launches return a fresh task id; status follows a script per task."""

    def __init__(self, clock, up_at=0.0, launch_error=None, env_at=0.0):
        self.clock, self.up_at, self.launch_error, self.env_at = clock, up_at, launch_error, env_at
        self.launches, self.health_calls, self.statuses, self.env_checks = [], 0, {}, []

    def check_env(self, env_id):
        self.env_checks.append(self.clock())
        if self.clock() < self.env_at:
            raise RouteUnavailable("env-not-found", f"Error: environment '{env_id}' not found")

    def health(self):
        self.health_calls += 1
        return {"ok": self.clock() >= self.up_at, "version": "fake", "detail": "down"}

    def launch(self, env_id, prompt, branch="main"):
        self.launches.append(self.clock())
        if self.launch_error:
            raise self.launch_error
        if self.clock() < self.env_at:
            raise RouteUnavailable("env-not-found", f"Error: environment '{env_id}' not found")
        if self.clock() < self.up_at:
            raise RouteUnavailable("rate-limited", "429 Too Many Requests")
        return {"task_id": TASK[:-3] + f"{len(self.launches):03d}", "url": None, "raw": ""}

    def status(self, task_id):
        seq = self.statuses.get(task_id, ["running"])
        st = seq.pop(0) if len(seq) > 1 else seq[0]
        return {"state": st, "raw": st.upper(), "title": "t", "detail": ""}


def cloud_spec(jid, extra=""):
    return contract.load(f'id = "{jid}"\nkind = "codex-cloud"\nbrief = "Write runs/{jid}/notes.md."\n{extra}')


def local_spec(jid, command, extra=""):
    return contract.load(f'id = "{jid}"\nkind = "local-shell"\nbrief = "Run a step."\ncommand = {command}\n{extra}')


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.cfg = config.load(self.home)
        self.cfg["cloud"]["env_id"] = "env_test"
        self.clock = Clock()
        self.led = Ledger(self.home / "ledger.db", clock=self.clock)
        self.sent = []

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def sup(self, cloud, clock=None, **kw):
        def send(cmd, subject, body):
            self.sent.append(subject)
            return {"ok": True, "exit": 0, "out": "sent", "ts": 0}
        return Supervisor(self.led, self.cfg, cloud=cloud, caps=CAPS, policy=POLICY, clock=clock or self.clock,
                          send=send, pinned=lambda sha: (True, ""), **kw)

    def test_route_down_three_hours_backs_off_probes_and_recovers_fast(self):
        up_at = self.clock.t + 180 * 60
        cloud = FakeCloud(self.clock, up_at=up_at)
        for i in range(3):
            self.led.add_job(cloud_spec(f"c{i}"), pinned_sha=SHA)
        s = self.sup(cloud)
        s.run(ticks=200, sleep=self.clock.sleep)  # 200 min at 60 s ticks
        down = [t for t in cloud.launches if t < up_at]
        self.assertEqual(len(down), 1, "only the first launch may hit the dead route")
        self.assertLessEqual(cloud.health_calls, 40)
        first_ok = min(t for t in cloud.launches if t >= up_at)
        self.assertLessEqual(first_ok - up_at, 300)
        self.assertEqual(self.led.active_stops(), [])
        self.assertFalse([e for e in self.led.events() if e["kind"] in ("stop", "pause")])
        self.assertEqual([e["kind"] for e in self.led.events() if e["kind"].startswith("route-")],
                         ["route-down", "route-up"])
        self.assertEqual({j["state"] for j in self.led.jobs()}, {"running"})

    def test_launch_with_unknown_outcome_is_never_relaunched(self):
        # a timeout or an unparsed reply may have created the task; relaunching could duplicate it
        for reason in ("timeout", "error"):
            with self.subTest(reason=reason):
                jid = f"u-{reason}"
                cloud = FakeCloud(self.clock, launch_error=RouteUnavailable(reason, "no task id in output"))
                self.led.add_job(cloud_spec(jid), pinned_sha=SHA)
                s = self.sup(cloud)
                for _ in range(10):
                    s.tick()
                    self.clock.sleep(600)
                self.assertEqual(len(cloud.launches), 1)
                self.assertEqual(self.led.job(jid)["state"], "handoff")
                self.assertIn("check codex cloud list", self.led.job(jid)["detail"])
                self.assertTrue(self.led.route("codex-cloud")["healthy"], "one task's error is not an outage")

    def test_restart_mid_launch_hands_cloud_job_off_and_resumes_local_runner(self):
        cloud = FakeCloud(self.clock)
        self.led.add_job(cloud_spec("c1"), pinned_sha=SHA)
        self.led.add_job(local_spec("l1", '["true"]'))
        for jid in ("c1", "l1"):
            self.led.set_state(jid, "ready")
            self.led.set_state(jid, "launching")  # the previous supervisor died here
        run_dir = self.home / "jobs" / "l1"
        run_dir.mkdir(parents=True)
        (run_dir / "runner.json").write_text('{"pid": 999999, "argv": ["true"]}')
        self.sup(cloud).run(once=True)
        self.assertEqual(self.led.job("c1")["state"], "handoff")
        # recovered to running with the recorded pid, then found dead: aborted is only reachable from running
        self.assertEqual((self.led.job("l1")["state"], self.led.job("l1")["external_id"]), ("aborted", "999999"))
        self.assertEqual(cloud.launches, [], "recovery never relaunches")

    def test_one_broken_job_does_not_stop_the_others(self):
        cloud = FakeCloud(self.clock)
        self.led.add_job(cloud_spec("good"), pinned_sha=SHA)
        bad = cloud_spec("bad")
        bad["write_paths"] = 7  # corrupt spec: iterating it raises inside the tick
        self.led.add_job(bad, pinned_sha=SHA)
        s = self.sup(cloud)
        for _ in range(3):
            s.tick()
        self.assertEqual(self.led.job("good")["state"], "running")
        self.assertEqual(self.led.job("bad")["state"], "held")
        self.assertIn("harness error", self.led.job("bad")["detail"])
        self.assertEqual(len([e for e in self.led.events("bad") if e["kind"] == "job-error"]), 1)

    def test_global_stop_halts_launches_until_its_owner_lifts_it(self):
        cloud = FakeCloud(self.clock)
        self.led.add_job(cloud_spec("c1"), pinned_sha=SHA)
        self.led.stop("lead", "quiet hours")
        s = self.sup(cloud)
        s.tick()
        self.assertEqual(cloud.launches, [])
        self.led.lift("guard")
        s.tick()
        self.assertEqual(cloud.launches, [])
        self.led.lift("lead")
        s.tick()
        self.assertEqual(len(cloud.launches), 1)

    def test_paused_job_is_not_marked_stalled(self):
        cloud = FakeCloud(self.clock)
        self.led.add_job(cloud_spec("c1", "timeout_min = 10"), pinned_sha=SHA)
        self.led.add_job(cloud_spec("c2", "timeout_min = 10"), pinned_sha=SHA)
        s = self.sup(cloud)
        s.tick()
        self.clock.sleep(61)  # past the launch gap, so c2 launches too
        s.tick()
        self.led.stop("guard", "timing-sensitive run", scope="job:c1", kind="pause")
        self.clock.sleep(11 * 60)
        s.tick()
        self.assertEqual(self.led.job("c1")["state"], "running")
        self.assertEqual(self.led.job("c2")["state"], "stalled")
        self.assertIn("c2 stalled", " ".join(self.sent))

    def test_operator_verb_holds_job_and_notifies_once(self):
        self.led.add_job(contract.load('id = "l1"\nkind = "local-shell"\nbrief = "Build it, then publish the page."\n'
                                       'command = ["true"]'))
        s = self.sup(FakeCloud(self.clock))
        for _ in range(3):
            s.tick()
        self.assertEqual(self.led.job("l1")["state"], "held")
        items = self.led.inbox()
        self.assertEqual([(i["kind"], i["job"]) for i in items], [("operator-step", "l1")])
        self.assertEqual(sum("operator-step" in x for x in self.sent), 1)

    def test_env_the_cli_cannot_see_holds_jobs_ready_and_they_launch_once_it_can(self):
        visible_at = self.clock.t + 25 * 60
        cloud = FakeCloud(self.clock, env_at=visible_at)
        self.led.add_job(cloud_spec("c1"), pinned_sha=SHA)
        self.led.add_job(cloud_spec("c2"), pinned_sha=SHA)
        s = self.sup(cloud)
        for _ in range(24):  # 24 min at 60 s ticks, all before the CLI sees the environment
            s.tick()
            self.clock.sleep(60)
        self.assertEqual(len(cloud.launches), 1, "a launch into an unseen environment is tried once")
        self.assertEqual(len(cloud.env_checks), 2, "then only a read-only check, every 10 minutes")
        self.assertEqual({self.led.job(j)["state"] for j in ("c1", "c2")}, {"ready"})
        self.assertEqual([(i["kind"], i["job"]) for i in self.led.inbox()], [("operator-step", None)],
                         "one owner step, and no hand launch per job")
        item = self.led.inbox()[0]
        self.assertIn("'env_test'", item["action"])
        self.assertIn("environment 'env_test' not found", item["detail"])
        self.assertTrue(self.led.route("codex-cloud")["healthy"], "an env lookup miss is not a route outage")
        for _ in range(10):
            s.tick()
            self.clock.sleep(60)
        self.assertEqual({self.led.job(j)["state"] for j in ("c1", "c2")}, {"running"})
        self.assertEqual(self.led.inbox(), [], "the owner step closes itself once the CLI lists the environment")
        prompt = (self.home / "prompts" / "c1.txt").read_text(encoding="utf-8")
        self.assertIn(f"git checkout --detach {SHA}", prompt)

    def test_web_job_asks_for_a_bundle_once_its_changes_view_may_have_expired(self):
        self.cfg["cloud"]["route"] = "web"
        self.led.add_job(cloud_spec("c1", "timeout_min = 600"), pinned_sha=SHA)
        s = self.sup(FakeCloud(self.clock))
        s.tick()
        self.led.set_state("c1", "launching", "attached")
        self.led.set_state("c1", "running", "attached", external_id="01900000-0000-7000-8000-000000000002",
                           started=self.clock())
        asks = lambda: [i for i in self.led.inbox() if i["kind"] == "harvest" and "--bundle" in i["action"]]  # noqa
        self.clock.t += 119 * 60
        s.tick()
        self.assertEqual(asks(), [], "within 2 h the Changes view is still the harvest route")
        self.clock.t += 2 * 60
        s.tick()
        s.tick()
        self.assertEqual(len(asks()), 1)
        self.assertEqual(self.led.job("c1")["state"], "running")

    def test_cloud_job_waits_without_env_and_queues_one_operator_step(self):
        self.cfg["cloud"]["env_id"] = ""
        cloud = FakeCloud(self.clock)
        self.led.add_job(cloud_spec("c1"), pinned_sha=SHA)
        self.led.add_job(cloud_spec("c2"), pinned_sha=SHA)
        s = self.sup(cloud)
        s.tick()
        s.tick()
        self.assertEqual(cloud.launches, [])
        self.assertEqual({j["state"] for j in self.led.jobs()}, {"waiting"})
        self.assertEqual(len([i for i in self.led.inbox() if "cloud environment" in i["action"]]), 1)

    def test_failed_cloud_task_ends_failed_with_receipt(self):
        cloud = FakeCloud(self.clock)
        self.led.add_job(cloud_spec("c1"), pinned_sha=SHA)
        s = self.sup(cloud)
        s.tick()
        cloud.statuses[self.led.job("c1")["external_id"]] = ["failed"]
        s.tick()
        self.assertEqual(self.led.job("c1")["state"], "failed")
        r = self.led.receipt("c1")
        self.assertEqual((r["route"], r["pinned_sha"]), ("codex-cloud", SHA))

    def test_unknown_status_keeps_job_running_and_logs_once(self):
        cloud = FakeCloud(self.clock)
        self.led.add_job(cloud_spec("c1"), pinned_sha=SHA)
        s = self.sup(cloud)
        s.tick()
        cloud.statuses[self.led.job("c1")["external_id"]] = ["unknown"]
        for _ in range(3):
            s.tick()
        self.assertEqual(self.led.job("c1")["state"], "running")
        self.assertEqual(len([e for e in self.led.events("c1") if e["kind"] == "unknown-status"]), 1)


class LocalJobTest(unittest.TestCase):
    """Real subprocesses through the detached runner."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.cfg = config.load(self.home)
        self.led = Ledger(self.home / "ledger.db")
        self.s = Supervisor(self.led, self.cfg, cloud=None, caps=CAPS, policy=POLICY,
                            send=lambda *a: {"ok": True, "exit": 0, "out": "", "ts": 0})

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def drive(self, jid, limit=10.0):
        end = time.time() + limit
        while time.time() < end:
            self.s.tick()
            if self.led.job(jid)["state"] not in ("draft", "ready", "launching", "running"):
                return self.led.job(jid)
            time.sleep(0.1)
        self.fail(f"{jid} did not finish: {self.led.job(jid)}")

    @unittest.skipIf(os.name == "nt", "POSIX-only fixture: sys.executable is interpolated into TOML without escaping")
    def test_command_and_gate_pass_ends_done_in_write_root(self):
        py = sys.executable
        self.led.add_job(local_spec("l1", f'["{py}", "-c", "open(\'out.txt\', \'w\').write(\'x\')"]',
                                    'accept = "test -s out.txt"'))
        j = self.drive("l1")
        self.assertEqual(j["state"], "done")
        self.assertTrue((self.home / "runs/l1/out.txt").is_file())
        self.assertEqual([c["exit"] for c in self.led.receipt("l1")["checks"]], [0, 0])

    def test_failing_gate_ends_failed(self):
        self.led.add_job(local_spec("l2", '["true"]', 'accept = "test -f missing.txt"'))
        j = self.drive("l2")
        self.assertEqual(j["state"], "failed")
        self.assertIn("acceptance gate exited", j["detail"])

    def test_failing_command_ends_failed_with_exit_code(self):
        self.led.add_job(local_spec("l3", '["sh", "-c", "exit 3"]'))
        self.assertEqual(self.drive("l3")["state"], "failed")
        self.assertEqual(self.led.receipt("l3")["checks"][0]["exit"], 3)


if __name__ == "__main__":
    unittest.main()
