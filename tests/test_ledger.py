import tempfile
import unittest
from pathlib import Path

from rh.ledger import STATES, TERMINAL, Ledger, LedgerError

RECEIPT = {"route": "local", "checks": [{"cmd": "true", "exit": 0}], "model_requested": None,
           "model_served": None, "ended": 1.0}


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.led = Ledger(Path(self.tmp.name) / "ledger.db", clock=self.clock)
        self.led.add_job({"id": "j1", "kind": "local-shell", "write_root": "runs/j1/"})

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def walk(self, *states):
        for s in states:
            self.led.set_state("j1", s)

    def test_lifecycle_records_each_change_as_one_event(self):
        self.walk("ready", "launching", "running")
        self.led.finish("j1", "done", RECEIPT)
        self.assertEqual(self.led.job("j1")["state"], "done")
        moves = [(e["data"]["from"], e["data"]["to"]) for e in self.led.events("j1") if e["kind"] == "state"]
        self.assertEqual(moves, [("draft", "ready"), ("ready", "launching"), ("launching", "running"),
                                 ("running", "done")])
        self.assertEqual(self.led.receipt("j1")["checks"][0]["exit"], 0)

    def test_no_terminal_state_can_move_again(self):
        for end in sorted(TERMINAL):
            jid = f"t-{end}"
            self.led.add_job({"id": jid, "kind": "local-shell"})
            for s in ("ready", "launching", "running"):
                self.led.set_state(jid, s)
            self.led.finish(jid, end, RECEIPT)
            for nxt in sorted(STATES - TERMINAL):
                with self.subTest(end=end, nxt=nxt), self.assertRaises(LedgerError):
                    self.led.set_state(jid, nxt)
            with self.assertRaises(LedgerError):
                self.led.finish(jid, "done", RECEIPT)
            self.assertEqual(self.led.job(jid)["state"], end)

    def test_skipping_launch_is_refused(self):
        with self.assertRaises(LedgerError):
            self.led.set_state("j1", "running")
        self.assertEqual(self.led.job("j1")["state"], "draft")

    def test_terminal_needs_finish_and_a_full_receipt(self):
        self.walk("ready", "launching", "running")
        with self.assertRaises(LedgerError):
            self.led.set_state("j1", "done")
        partial = {k: v for k, v in RECEIPT.items() if k != "checks"}
        with self.assertRaisesRegex(LedgerError, "checks"):
            self.led.finish("j1", "done", partial)
        self.assertEqual(self.led.job("j1")["state"], "running")
        self.assertIsNone(self.led.receipt("j1"))

    def test_duplicate_job_id_is_refused(self):
        with self.assertRaises(LedgerError):
            self.led.add_job({"id": "j1", "kind": "local-shell"})

    def test_only_the_owner_lifts_a_stop(self):
        self.led.stop("lead", "maintenance window")
        self.assertEqual(self.led.lift("guard"), 0)
        self.assertEqual(self.led.blocking("stop", job="j1")["owner"], "lead")
        self.assertEqual(self.led.lift("lead"), 1)
        self.assertIsNone(self.led.blocking("stop", job="j1"))

    def test_stop_without_reason_is_refused(self):
        with self.assertRaises(LedgerError):
            self.led.stop("lead", "  ")

    def test_scoped_pause_applies_only_to_its_job_or_route(self):
        self.led.stop("guard", "timing run", scope="job:j1", kind="pause")
        self.led.stop("lead", "cli broken", scope="route:codex-cloud")
        self.assertIsNotNone(self.led.blocking("pause", job="j1"))
        self.assertIsNone(self.led.blocking("pause", job="j2"))
        self.assertIsNone(self.led.blocking("stop", job="j1", route="local"))
        self.assertIsNotNone(self.led.blocking("stop", job="j2", route="codex-cloud"))

    def test_inbox_dedupes_open_items_and_resolves_once(self):
        a, new_a = self.led.inbox_add("operator-step", "publish the environment", job="j1")
        b, new_b = self.led.inbox_add("operator-step", "publish the environment", job="j1")
        self.assertEqual((a, new_a, new_b), (b, True, False))
        self.led.inbox_resolve(a, "done by operator")
        self.assertEqual(self.led.inbox(), [])
        with self.assertRaises(LedgerError):
            self.led.inbox_resolve(a, "again")
        self.assertNotEqual(self.led.inbox_add("operator-step", "publish the environment", job="j1")[0], a)

    def test_route_backoff_grows_and_logs_one_event_per_episode(self):
        waits = []
        for _ in range(8):
            r = self.led.route_fail("codex-cloud", "error")
            waits.append(r["next_try"] - self.clock.t)
        self.assertEqual(waits, [60, 120, 240, 300, 300, 300, 300, 300])  # capped at 5 min so recovery is seen fast
        r = self.led.route_fail("codex-cloud", "rate-limited", retry_after=5000)
        self.assertEqual(r["next_try"] - self.clock.t, 5000)
        self.led.route_ok("codex-cloud")
        self.led.route_ok("codex-cloud")
        kinds = [e["kind"] for e in self.led.events() if e["kind"].startswith("route")]
        self.assertEqual(kinds, ["route-down", "route-up"])
        self.assertEqual(self.led.route("codex-cloud")["failures"], 0)

    def test_state_survives_reopen(self):
        self.walk("ready")
        self.led.stop("lead", "night")
        self.led.close()
        self.led = Ledger(Path(self.tmp.name) / "ledger.db", clock=self.clock)
        self.assertEqual(self.led.job("j1")["state"], "ready")
        self.assertEqual(self.led.active_stops()[0]["reason"], "night")


if __name__ == "__main__":
    unittest.main()
