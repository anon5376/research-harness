"""Seat contracts, standing-task reuse, and negation across comma-separated prohibitions."""
import hashlib
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from rh import config, contract, protocol
from rh.ledger import Ledger, TERMINAL

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
TASK = "task_e_" + "1" * 32
OTHER_TASK = "task_e_" + "2" * 32
PREFIX = ("Follow-up turn: this is a new job in the same task. Before step 1, run from the checkout root: "
          "git reset -q --hard && git clean -qfd\n\n")


def cards():
    return {
        "cli": {"role": "CLI and contracts", "route": "codex-cloud", "model": "gpt-6.1-sol",
                "effort": "max", "owns": ["rh/cli.py", "rh/seats.py", "tests/"], "done": "suite passes"},
        "design": {"role": "Design", "route": "claude", "model": "claude-opus-5-5",
                   "effort": "default", "owns": ["docs/"], "done": "reviewed"},
    }


def job(jid="next", **overrides):
    spec = {"id": jid, "kind": "codex-cloud", "brief": "Implement the CLI.", "seat": "cli",
            "write_paths": ["rh/cli.py", "tests/unit/test_cli.py"],
            "model": {"requested": "gpt-6.1-sol", "effort": "max"}}
    spec.update(overrides)
    return contract.normalize(spec)


def seat_api(test):
    test.assertIsNotNone(importlib.util.find_spec("rh.seats"), "rh.seats must implement the seat interfaces")
    return importlib.import_module("rh.seats")


class SeatFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.repo, self.home = self.base / "repo", self.base / "home"
        self.repo.mkdir()
        config.set_value("repo.path", str(self.repo), self.home)
        config.set_value("cloud.env_id", "fake", self.home)
        self.cfg = config.load(self.home)
        self.now = 100.0
        self.led = Ledger(self.home / "ledger.db", clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.write_cards()

    def write_cards(self, value=None):
        value = cards() if value is None else value
        text = "\n".join(f"[seat.{name}]\n" + "\n".join(f"{k} = {json.dumps(v)}" for k, v in card.items())
                         for name, card in value.items())
        path = self.repo / "seats.toml"
        path.write_text(text)
        return path

    def add_job(self, jid="next", **overrides):
        self.now += 1
        self.led.add_job(job(jid, **overrides), pinned_sha=SHA)
        return jid

    def attach(self, jid, task=TASK):
        self.now += 1
        self.led.set_state(jid, "ready")
        self.led.set_state(jid, "launching")
        self.led.set_state(jid, "running", external_id=task, started=self.now)

    def finish(self, jid, state="done"):
        self.now += 1
        self.led.finish(jid, state, {"route": "codex-cloud-web", "checks": [], "model_requested": "gpt-6.1-sol",
                                    "model_served": None, "ended": self.now})

    def handoff(self, jid):
        self.led.set_state(jid, "ready")
        self.led.set_state(jid, "launching")
        self.led.set_state(jid, "handoff", route="codex-cloud-web")

    def rh(self, *args):
        return subprocess.run([sys.executable, "-B", "-I", str(ROOT / "bin/rh"), *args],
                              env={**os.environ, "RH_HOME": str(self.home)}, capture_output=True,
                              text=True, timeout=20)


class LoadTest(SeatFixture):
    def setUp(self):
        super().setUp()
        self.seats = seat_api(self)

    def test_load_preserves_cards_and_accepts_both_routes(self):
        self.assertEqual(self.seats.load(self.repo / "seats.toml"), cards())

    def test_missing_and_unknown_card_keys_raise(self):
        for key in cards()["cli"]:
            with self.subTest(missing=key):
                value = cards()
                del value["cli"][key]
                with self.assertRaisesRegex(ValueError, key):
                    self.seats.load(self.write_cards(value))
        value = cards()
        value["cli"]["typo"] = "max"
        with self.assertRaisesRegex(ValueError, "typo"):
            self.seats.load(self.write_cards(value))

    def test_missing_unknown_and_malformed_top_level_tables_raise(self):
        path = self.repo / "seats.toml"
        for text in ("", "[seats.cli]\nrole = 'x'", "seat = 'cli'", "[seat\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                path.write_text(text)
                self.seats.load(path)

    def test_bad_routes_and_card_value_types_raise(self):
        for key, bad in (("route", "local-shell"), ("role", ""), ("model", 4), ("effort", ""),
                         ("done", []), ("owns", "rh/"), ("owns", [4]), ("owns", ["../rh/"])):
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                value = cards()
                value["cli"][key] = bad
                self.seats.load(self.write_cards(value))

    def test_equal_and_parent_ownership_across_seats_raise_in_either_order(self):
        for left, right in (("rh/cli.py", "rh/cli.py"), ("rh/", "rh/cli.py"),
                            ("rh/cli.py", "rh/"), ("desktop/", "desktop/icons/")):
            with self.subTest(left=left, right=right), self.assertRaisesRegex(ValueError, "cli.*design|design.*cli"):
                value = cards()
                value["cli"]["owns"], value["design"]["owns"] = [left], [right]
                self.seats.load(self.write_cards(value))

    def test_shared_text_prefix_is_not_shared_ownership(self):
        value = cards()
        value["cli"]["owns"] = ["rh/", "README.md"]
        value["design"]["owns"] = ["rhino/", "README.md.backup"]
        self.assertEqual(self.seats.load(self.write_cards(value)), value)


class HoldsTest(unittest.TestCase):
    def setUp(self):
        self.seats = seat_api(self)

    def test_inside_card_and_without_seat_have_no_holds(self):
        self.assertEqual(self.seats.holds(job(), cards()), [])
        spec = job()
        del spec["seat"]
        self.assertEqual(self.seats.holds(spec, {}), [])

    def test_each_card_mismatch_gives_a_reason(self):
        cases = [({"seat": "missing"}, "missing"), ({"kind": "local-shell", "command": ["true"]}, "route"),
                 ({"model": {"requested": "other", "effort": "max"}}, "model"),
                 ({"model": {"requested": "gpt-6.1-sol", "effort": "default"}}, "effort"),
                 ({"write_paths": ["rh/protocol.py"]}, "rh/protocol.py")]
        for overrides, word in cases:
            with self.subTest(overrides=overrides):
                reasons = self.seats.holds(job(**overrides), cards())
                self.assertEqual(len(reasons), 1, reasons)
                self.assertIn(word, reasons[0])

    def test_missing_model_and_effort_cannot_bypass_card(self):
        reasons = self.seats.holds(job(model={}), cards())
        self.assertEqual(len(reasons), 2)
        self.assertTrue(any("model" in reason for reason in reasons))
        self.assertTrue(any("effort" in reason for reason in reasons))

    def test_exact_file_and_directory_boundaries_are_enforced(self):
        for path in ("rh/cli.py.old", "rh/cli.py/child", "testing/test_cli.py", "tests/../secret"):
            with self.subTest(path=path):
                self.assertTrue(self.seats.holds(job(write_paths=[path]), cards()))
        self.assertEqual(self.seats.holds(job(write_paths=["tests/", "rh/seats.py"]), cards()), [])


class TaskTest(SeatFixture):
    def setUp(self):
        super().setUp()
        self.seats = seat_api(self)

    def test_no_attached_cloud_job_has_no_task(self):
        self.assertEqual(self.seats.task_of(self.led, "cli"), (None, None))
        self.handoff(self.add_job())
        self.attach(self.add_job("design", seat="design"))
        self.attach(self.add_job("local", kind="local-shell", command=["true"]), "123")
        self.assertEqual(self.seats.task_of(self.led, "cli"), (None, None))

    def test_running_stalled_and_collecting_jobs_are_busy(self):
        self.attach(self.add_job())
        for state in ("running", "stalled", "collecting"):
            with self.subTest(state=state):
                self.led.set_state("next", state)
                self.assertEqual(self.seats.task_of(self.led, "cli"), (TASK, "next"))

    def test_terminal_jobs_keep_the_task_but_are_not_busy(self):
        for state in sorted(TERMINAL):
            with self.subTest(state=state):
                jid = self.add_job("job-" + state)
                self.attach(jid)
                self.finish(jid, state)
                self.assertEqual(self.seats.task_of(self.led, "cli"), (TASK, None))

    def test_latest_attachment_wins_over_creation_and_later_completion(self):
        old = self.add_job("older")
        newer = self.add_job("newer")
        self.attach(newer, OTHER_TASK)
        self.attach(old)
        self.finish(newer)
        self.handoff(self.add_job("unattached"))
        self.assertEqual(self.seats.task_of(self.led, "cli"), (TASK, "older"))
        self.finish(old)
        self.assertEqual(self.seats.task_of(self.led, "cli"), (TASK, None))


class PromptTest(unittest.TestCase):
    def test_followup_is_exact_prefix_and_task_prompt_with_all_arguments(self):
        self.assertTrue(hasattr(protocol, "followup_prompt"), "the protocol needs followup_prompt")
        for kwargs in ({}, {"attempt": 3, "network": True}):
            with self.subTest(kwargs=kwargs):
                prompt = protocol.followup_prompt("next", SHA, "Implement the CLI.", ["rh/cli.py"], **kwargs)
                self.assertEqual(prompt, PREFIX + protocol.task_prompt(
                    "next", SHA, "Implement the CLI.", ["rh/cli.py"], **kwargs))


class CliTest(SeatFixture):
    def done_seat(self):
        self.attach(self.add_job("previous"))
        self.finish("previous")

    def test_prompt_for_done_seat_prints_followup_and_task_before_digest_even_with_cached_prompt(self):
        self.done_seat()
        self.add_job()
        saved = self.home / "prompts/next.txt"
        saved.parent.mkdir()
        saved.write_text("stale first-turn prompt\n")
        result = self.rh("cloud", "prompt", "next")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, PREFIX + protocol.task_prompt(
            "next", SHA, "Implement the CLI.", ["rh/cli.py", "tests/unit/test_cli.py"]))
        self.assertEqual(result.stderr.splitlines(), [f"follow-up in {TASK}",
                         f"sha256 {hashlib.sha256(result.stdout.encode()).hexdigest()}  "
                         "(paste once; compare before sending)"])

    def test_prompt_for_busy_seat_exits_one_naming_job(self):
        self.attach(self.add_job("busy"))
        self.add_job()
        result = self.rh("cloud", "prompt", "next")
        self.assertEqual(result.returncode, 1)
        self.assertIn("busy", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_first_seat_prompt_and_unseated_cached_prompt_are_unchanged(self):
        self.add_job()
        result = self.rh("cloud", "prompt", "next")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, protocol.task_prompt(
            "next", SHA, "Implement the CLI.", ["rh/cli.py", "tests/unit/test_cli.py"]))
        unseated = job("plain")
        del unseated["seat"]
        self.led.add_job(unseated, pinned_sha=SHA)
        saved = self.home / "prompts/plain.txt"
        saved.parent.mkdir(exist_ok=True)
        saved.write_text("saved prompt\n")
        result = self.rh("cloud", "prompt", "plain")
        self.assertEqual((result.returncode, result.stdout), (0, "saved prompt\n"))

    def test_attach_without_task_reuses_done_seat_and_resolves_handoff(self):
        self.done_seat()
        self.handoff(self.add_job())
        self.led.inbox_add("web-launch", "launch next", job="next")
        result = self.rh("cloud", "attach", "next")
        self.assertEqual(result.returncode, 0, result.stderr)
        attached = self.led.job("next")
        self.assertEqual((attached["state"], attached["external_id"]), ("running", TASK))
        self.assertGreater(attached["started"], 0)
        self.assertEqual(self.led.inbox(), [])

    def test_busy_seat_refuses_attach_with_or_without_explicit_task(self):
        self.attach(self.add_job("busy"))
        self.handoff(self.add_job())
        for task in ((), (OTHER_TASK,)):
            with self.subTest(task=task):
                result = self.rh("cloud", "attach", "next", *task)
                self.assertEqual(result.returncode, 1)
                self.assertIn("busy", result.stderr)
                self.assertEqual(self.led.job("next")["state"], "handoff")

    def test_new_seat_requires_task_and_accepts_explicit_task(self):
        self.handoff(self.add_job())
        missing = self.rh("cloud", "attach", "next")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("task", missing.stderr)
        self.assertEqual(self.led.job("next")["state"], "handoff")
        attached = self.rh("cloud", "attach", "next", TASK)
        self.assertEqual(attached.returncode, 0, attached.stderr)
        self.assertEqual(self.led.job("next")["external_id"], TASK)

    def test_seat_listing_uses_configured_cards_and_ledger_task_state(self):
        self.attach(self.add_job("busy"))
        result = self.rh("seats")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([line.split() for line in result.stdout.splitlines()], [
            ["cli", "gpt-6.1-sol", "max", TASK, "busy"],
            ["design", "claude-opus-5-5", "default", "-", "-"],
        ])
        self.finish("busy")
        result = self.rh("seats")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0].split(), ["cli", "gpt-6.1-sol", "max", TASK, "-"])

    def test_missing_seat_file_lists_no_seats(self):
        (self.repo / "seats.toml").unlink()
        result = self.rh("seats")
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)

    def test_bundle_export_still_works_for_the_running_job(self):
        self.attach(self.add_job())
        result = self.rh("cloud", "prompt", "next", "--bundle-export")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("git reset --soft " + SHA, result.stdout)


class PreflightTest(SeatFixture):
    def decide(self, spec):
        return contract.preflight(spec, {}, {}, contract.View(input_root=self.repo))

    def test_preflight_uses_repository_cards_and_reports_all_holds(self):
        self.assertEqual(self.decide(job()).verdict, "ready")
        held = self.decide(job(model={}, write_paths=["rh/contract.py"]))
        self.assertEqual(held.verdict, "hold")
        for word in ("model", "effort", "rh/contract.py"):
            self.assertTrue(any(word in reason for reason in held.reasons), held.reasons)

    def test_missing_cards_hold_unknown_seat_and_leave_unseated_jobs_ready(self):
        (self.repo / "seats.toml").unlink()
        held = self.decide(job())
        self.assertEqual(held.verdict, "hold")
        self.assertTrue(any("cli" in reason for reason in held.reasons))
        plain = job()
        del plain["seat"]
        self.assertEqual(self.decide(plain).verdict, "ready")

    def test_invalid_cards_hold_the_job_with_the_validation_reason(self):
        value = cards()
        value["cli"]["typo"] = "max"
        self.write_cards(value)
        held = self.decide(job())
        self.assertEqual(held.verdict, "hold")
        self.assertTrue(any("typo" in reason for reason in held.reasons), held.reasons)

    def test_supervisor_preflight_reads_cards_from_its_configured_repository(self):
        from rh.supervisor import Supervisor
        self.add_job(model={})
        sup = Supervisor(self.led, self.cfg, caps={}, policy={}, pinned=lambda sha: (True, ""))
        sup._preflight_all()
        held = self.led.job("next")
        self.assertEqual(held["state"], "held")
        self.assertIn("model", held["detail"])
        self.assertIn("effort", held["detail"])
        self.assertEqual(self.led.inbox(), [])


class NegationTest(unittest.TestCase):
    def test_leading_negation_covers_comma_separated_items(self):
        for brief in ("Never push, publish or deploy.",
                      "Do not push, open pull requests, or send messages.",
                      "Don't push, publish, deploy, or send messages."):
            with self.subTest(brief=brief):
                self.assertEqual(contract.operator_steps(brief), [])

    def test_then_and_sentence_boundaries_end_list_negation(self):
        for brief, expected in (("Do not push, then deploy.", ["deploy"]),
                                ("Never push. Publish it.", ["Publish it"]),
                                ("Do not push; deploy.", ["deploy"]),
                                ("Write notes, never push, publish or deploy.", []),
                                ("Check that it does not push, publish it.", ["publish it"]),
                                ("Do not edit tests, push to main.", ["push to main"]),
                                ("Don't touch the README, publish the release.", ["publish the release"]),
                                ("Never rename files, deploy when green.", ["deploy when green"])):
            with self.subTest(brief=brief):
                self.assertEqual(contract.operator_steps(brief), expected)


if __name__ == "__main__":
    unittest.main()
