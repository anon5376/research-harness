import tempfile
import unittest
from pathlib import Path

from rh import config
from rh.ledger import Ledger
from rh.supervisor import Supervisor
from tests.test_supervisor import CAPS, POLICY, SHA, Clock, FakeCloud, cloud_spec, local_spec


class PacingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.clock = Clock()
        self.led = Ledger(self.home / "ledger.db", clock=self.clock)
        self.addCleanup(self.led.close)
        self.cfg = config.load(self.home)
        self.cfg["cloud"].update(env_id="env_test", launches_per_tick=10)
        self.cloud = FakeCloud(self.clock)
        self.sent = []
        self.sup = Supervisor(self.led, self.cfg, cloud=self.cloud, clock=self.clock,
                              caps=CAPS, policy=POLICY, pinned=lambda sha: (True, ""),
                              send=lambda *args: (self.sent.append(args) or
                                                 {"ok": True, "exit": 0, "out": ""}))

    def jobs(self, n):
        for i in range(n):
            self.led.add_job(cloud_spec(f"c{i}"), pinned_sha=SHA)

    def test_gap_and_quiet_ready_wait(self):
        self.assertEqual(self.cfg["cloud"]["min_gap_s"], 60)
        self.assertEqual(self.cfg["cloud"]["max_per_hour"], 20)
        self.cfg["cloud"]["max_per_hour"] = 99
        self.jobs(10)
        start = self.clock()
        self.sup.tick()
        events, sent = self.led.events("c1"), list(self.sent)
        self.clock.sleep(30)
        self.sup.tick()
        self.assertEqual(self.led.job("c1")["state"], "ready")
        self.assertEqual(self.led.events("c1"), events)
        self.assertEqual(self.sent, sent)
        for _ in range(21):
            self.clock.sleep(30)
            self.sup.tick()
        self.assertEqual(len(self.cloud.launches), 10)
        self.assertTrue(all(b - a >= 60 for a, b in zip(self.cloud.launches, self.cloud.launches[1:])))
        self.assertLessEqual(self.cloud.launches[-1] - start, 660)

    def test_hourly_cap_expires_at_boundary(self):
        self.cfg["cloud"].update(min_gap_s=0, max_per_hour=5)
        self.jobs(6)
        self.sup.tick()
        self.assertEqual(len(self.cloud.launches), 5)
        self.clock.sleep(3599)
        self.sup.tick()
        self.assertEqual(len(self.cloud.launches), 5)
        self.assertEqual(self.led.job("c5")["state"], "ready")
        self.clock.sleep(1)
        self.sup.tick()
        self.assertEqual(len(self.cloud.launches), 6)

    def test_launch_history_filters_and_attach_counts(self):
        start = self.clock()
        for jid, spec, route in (("old", cloud_spec("old"), "codex-cloud-cli"),
                                 ("local", local_spec("local", '["true"]'), "local-shell"),
                                 ("web", cloud_spec("web"), "codex-cloud-web")):
            self.led.add_job(spec, pinned_sha=SHA)
            self.led.set_state(jid, "ready")
            self.led.set_state(jid, "launching")
            self.led.set_state(jid, "running", route=route, started=self.clock())
            self.clock.sleep(10)
        self.assertEqual(self.led.launches_since("codex-cloud", start + 1), [start + 20])
        self.assertEqual(self.led.launches_since("codex-cloud", start), [start, start + 20])
        self.jobs(1)
        self.sup.tick()
        self.assertEqual(self.led.job("c0")["state"], "ready")
        self.assertEqual(self.cloud.launches, [])
