import os
import tempfile
import time
import unittest
from pathlib import Path

from rh import config
from rh.ledger import Ledger
from rh.supervisor import Supervisor
from tests.test_supervisor import CAPS, POLICY, Clock, local_spec


class WakeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.led = Ledger(self.home / "ledger.db")
        self.addCleanup(self.led.close)
        self.cfg = config.load(self.home)
        self.cfg["supervisor"]["interval"] = 60
        self.sup = Supervisor(self.led, self.cfg, caps=CAPS, policy=POLICY)

    def test_idle_and_own_writes_wait_full_interval(self):
        clock, times = Clock(), []

        def tick():
            times.append(clock())
            self.led.event("own-write")

        self.sup.tick = tick
        self.assertEqual(self.sup.run(ticks=3, sleep=clock.sleep, wake_poll=7), 3)
        self.assertEqual(times, [1000000, 1000060, 1000120])

    def test_wake_file_triggers_once_and_is_removed(self):
        clock, times = Clock(), []
        self.sup.tick = lambda: times.append(clock())

        def sleep(seconds):
            clock.sleep(seconds)
            if len(times) == 1:
                (self.home / "wake").touch()

        self.sup.run(ticks=3, sleep=sleep, wake_poll=1)
        self.assertEqual(times, [1000000, 1000001, 1000061])
        self.assertFalse((self.home / "wake").exists())

    @unittest.skipIf(os.name == "nt", "POSIX only: local-shell true command")
    def test_second_connection_job_launches_within_two_seconds(self):
        other = Ledger(self.home / "ledger.db")
        self.addCleanup(other.close)
        added = []

        def sleep(seconds):
            if not added:
                added.append(time.monotonic())
                other.add_job(local_spec("wake-job", '["true"]'))
            time.sleep(seconds)
            if time.monotonic() - added[0] > 2:
                self.fail("supervisor did not wake within two seconds")

        self.sup.run(ticks=2, sleep=sleep, wake_poll=0.05)
        self.assertLess(time.monotonic() - added[0], 2)
        self.assertTrue(any(e["kind"] == "state" and e["data"]["to"] == "running"
                            for e in self.led.events("wake-job")))
        end = time.monotonic() + 2
        while self.led.job("wake-job")["state"] == "running" and time.monotonic() < end:
            time.sleep(0.05)
            self.sup.tick()
        self.assertEqual(self.led.job("wake-job")["state"], "done")
