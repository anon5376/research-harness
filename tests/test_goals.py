"""Goal contracts, dependency expansion, ledger rollups, and the offline CLI."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from rh.contract import normalize
from rh.goals import GoalError, expand, load_goal, rollup
from rh.ledger import Ledger

ROOT = Path(__file__).resolve().parents[1]
CARD = {"said": "  Review the supplied literature.\n", "form": "file", "done_when": "A reviewed synthesis exists."}


def goal():
    return {
        "id": "review-01", "card": dict(CARD),
        "roles": {"scout": {"kind": "local-shell", "command": ["true"], "needs": ["python"],
                             "model": {"requested": "gpt-6.1-sol", "speed": "fast", "effort": "extra-high"}},
                  "analyst": {"kind": "local-shell", "command": ["true"]}},
        "jobs": [{"id": "scan", "role": "scout", "brief": "Read the supplied sources."},
                 {"id": "synthesis", "role": "analyst", "after": ["scan"], "brief": "Write a synthesis."}],
    }


class GoalExpansionTest(unittest.TestCase):
    def test_role_defaults_and_overrides(self):
        raw = goal()
        raw["jobs"].append({"id": "check", "role": "scout", "after": ["synthesis"],
                            "brief": "Check the synthesis.", "needs": [], "command": ["echo", "checked"],
                            "model": {"requested": "alternate"}})
        jobs = expand(raw)
        self.assertEqual(jobs[0]["kind"], "local-shell")
        self.assertEqual(jobs[0]["command"], ["true"])
        self.assertEqual(jobs[0]["needs"], ["python"])
        self.assertEqual(jobs[0]["model"], {"requested": "gpt-6.1-sol", "speed": "fast", "effort": "extra-high"})
        self.assertEqual(jobs[2]["needs"], [])
        self.assertEqual(jobs[2]["command"], ["echo", "checked"])
        self.assertEqual(jobs[2]["model"], {"requested": "alternate"})

    def test_dependency_order_prefixes_and_final_card(self):
        raw = goal()
        raw["jobs"].reverse()
        jobs = expand(raw)
        self.assertEqual([job["id"] for job in jobs], ["review-01.scan", "review-01.synthesis"])
        self.assertEqual(jobs[0]["after"], [])
        self.assertEqual(jobs[1]["after"], ["review-01.scan"])
        self.assertEqual([job["goal"] for job in jobs], ["review-01", "review-01"])
        self.assertFalse(jobs[0].get("operator_facing", False))
        self.assertTrue(jobs[-1]["operator_facing"])
        self.assertEqual(jobs[-1]["card"], CARD)

    def test_role_dependencies_and_explicit_override(self):
        raw = goal()
        raw["roles"]["analyst"]["after"] = ["scan"]
        del raw["jobs"][1]["after"]
        self.assertEqual(expand(raw)[1]["after"], ["review-01.scan"])
        raw["jobs"][1]["after"] = []
        self.assertEqual(expand(raw)[1]["after"], [])
        raw["roles"]["analyst"]["after"] = ["missing"]
        del raw["jobs"][1]["after"]
        with self.assertRaisesRegex(GoalError, "after.*missing"):
            expand(raw)

    def test_expansion_does_not_mutate_or_alias_inputs(self):
        raw = goal()
        raw["jobs"].append({"id": "check", "role": "scout", "after": ["synthesis"], "brief": "Check the sources."})
        original = copy.deepcopy(raw)
        jobs = expand(raw)
        self.assertEqual(raw, original)
        jobs[0]["model"]["requested"] = "changed"
        jobs[0]["command"].append("changed")
        jobs[-1]["card"]["said"] = "changed"
        jobs[1]["after"].append("changed")
        self.assertEqual(raw, original)
        self.assertEqual(jobs[-1]["model"]["requested"], "gpt-6.1-sol")
        self.assertEqual(jobs[-1]["command"], ["true"])
        self.assertEqual(expand(raw)[0]["model"]["requested"], "gpt-6.1-sol")

    def test_expanded_jobs_normalize(self):
        raw = goal()
        raw["roles"]["scout"].update(kind="codex-cloud", needs=["net"], write_paths=["notes.txt"])
        jobs = [normalize(job) for job in expand(raw)]
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["id"], "review-01.scan")
        self.assertEqual(jobs[0]["write_root"], "runs/review-01.scan/")
        self.assertEqual(jobs[0]["write_paths"], ["notes.txt"])
        self.assertEqual(jobs[0]["needs"], ["net"])
        self.assertEqual(jobs[1]["command"], ["true"])
        self.assertTrue(jobs[-1]["operator_facing"])


class GoalValidationTest(unittest.TestCase):
    def test_unknown_role(self):
        raw = goal()
        raw["jobs"][0]["role"] = "missing"
        with self.assertRaisesRegex(GoalError, "unknown role.*missing"):
            expand(raw)

    def test_duplicate_job_id(self):
        raw = goal()
        raw["jobs"].append(dict(raw["jobs"][0]))
        with self.assertRaisesRegex(GoalError, "duplicate.*scan"):
            expand(raw)

    def test_unknown_dependency(self):
        raw = goal()
        raw["jobs"][1]["after"] = ["missing"]
        with self.assertRaisesRegex(GoalError, "after.*missing"):
            expand(raw)

    def test_dependency_cycle(self):
        raw = goal()
        raw["jobs"][0]["after"] = ["synthesis"]
        with self.assertRaisesRegex(GoalError, "cycle"):
            expand(raw)
        raw = goal()
        raw["jobs"][0]["after"] = ["scan"]
        with self.assertRaisesRegex(GoalError, "cycle"):
            expand(raw)

    def test_no_jobs(self):
        for jobs in (None, []):
            raw = goal()
            if jobs is None:
                del raw["jobs"]
            else:
                raw["jobs"] = jobs
            with self.subTest(jobs=jobs), self.assertRaisesRegex(GoalError, "no jobs"):
                expand(raw)

    def test_missing_card_fields(self):
        for field in ("said", "form", "done_when"):
            for value in (None, "", " \n", 12):
                raw = goal()
                if value is None:
                    del raw["card"][field]
                else:
                    raw["card"][field] = value
                with self.subTest(field=field, value=value), self.assertRaisesRegex(GoalError, field):
                    expand(raw)
        raw = goal()
        del raw["card"]
        with self.assertRaisesRegex(GoalError, "card"):
            expand(raw)

    def test_invalid_ids_and_expanded_length(self):
        for field in ("goal", "job"):
            for value in (None, "", "Upper", "bad/id", "x\n", "a" * 64):
                raw = goal()
                target = raw if field == "goal" else raw["jobs"][0]
                target["id"] = value
                with self.subTest(field=field, value=value), self.assertRaisesRegex(GoalError, "id"):
                    expand(raw)
        raw = goal()
        raw["id"] = "g" * 60
        with self.assertRaisesRegex(GoalError, "id"):
            expand(raw)
        raw = goal()
        raw["id"] = "g" * 53
        self.assertTrue(all(normalize(job) for job in expand(raw)))

    def test_invalid_table_shapes(self):
        cases = [("roles", []), ("roles", {"scout": "bad"}), ("jobs", {}),
                 ("jobs", ["bad"]), ("card", []), ("card", "bad")]
        for field, value in cases:
            raw = goal()
            raw[field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(GoalError, field):
                expand(raw)
        for value in ("scan", [1], {}, None):
            raw = goal()
            raw["jobs"][1]["after"] = value
            with self.subTest(after=value), self.assertRaisesRegex(GoalError, "after"):
                expand(raw)
        with self.assertRaisesRegex(GoalError, "goal"):
            expand([])

    def test_load_parses_and_validates_toml(self):
        text = '''id = "loaded"
[card]
said = "  Keep these words.  "
form = "file"
done_when = "Reviewed."
[roles.scout]
kind = "local-shell"
command = ["true"]
[[jobs]]
id = "scan"
role = "scout"
brief = "Read the supplied sources."
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "goal.toml"
            path.write_text(text, encoding="utf-8")
            loaded = load_goal(path)
            self.assertEqual(loaded["id"], "loaded")
            self.assertEqual(loaded["jobs"][0]["id"], "scan")
            self.assertEqual(loaded["card"]["said"], "  Keep these words.  ")
            self.assertEqual(expand(loaded)[0]["id"], "loaded.scan")
            path.write_text(text.replace('role = "scout"', 'role = "unknown"'), encoding="utf-8")
            with self.assertRaisesRegex(GoalError, "unknown role"):
                load_goal(path)
            path.write_text("id = [", encoding="utf-8")
            with self.assertRaisesRegex(GoalError, "TOML"):
                load_goal(path)


class GoalRollupTest(unittest.TestCase):
    def test_running_done_and_failed(self):
        rows = [{"id": "review-01.scan", "state": "done", "spec": {"goal": "review-01"}},
                {"id": "review-01.synthesis", "state": "running", "spec": {"goal": "review-01"}},
                {"id": "review-01.other", "state": "failed", "spec": {"goal": "other"}},
                {"id": "review-01.unrelated", "state": "failed", "spec": {}}]
        self.assertEqual(rollup("review-01", rows), {
            "goal": "review-01", "jobs": {"review-01.scan": "done", "review-01.synthesis": "running"},
            "done": False, "ended_badly": []})
        rows[1]["state"] = "done"
        self.assertTrue(rollup("review-01", rows)["done"])
        rows[1]["state"] = "failed"
        result = rollup("review-01", rows)
        self.assertFalse(result["done"])
        self.assertEqual(result["ended_badly"], ["review-01.synthesis"])

    def test_all_unsuccessful_terminal_states(self):
        for state in ("failed", "blocked", "capacity", "truncated", "aborted", "no_result", "cancelled",
                      "draft", "ready", "waiting", "held", "launching", "handoff", "running", "stalled", "collecting"):
            with self.subTest(state=state):
                row = {"id": "review-01.scan", "state": state, "spec": {"goal": "review-01"}}
                result = rollup("review-01", [row])
                self.assertFalse(result["done"])
                expected = ["review-01.scan"] if state in (
                    "failed", "blocked", "capacity", "truncated", "aborted", "no_result", "cancelled") else []
                self.assertEqual(result["ended_badly"], expected)

    def test_empty_goal_is_not_done(self):
        self.assertEqual(rollup("missing", []), {"goal": "missing", "jobs": {}, "done": False, "ended_badly": []})


class GoalCliTest(unittest.TestCase):
    def test_offline_example_add_and_show(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "RH_HOME": str(Path(tmp) / "home")}
            command = [sys.executable, "-I", "-B", str(ROOT / "bin/rh")]
            added = subprocess.run([*command, "goal", "add", "goals/example.toml"], cwd=ROOT, env=env,
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
            self.assertIn("added 3 job(s)", added.stdout)
            shown = subprocess.run([*command, "goal", "show", "lit-review-example"], cwd=ROOT, env=env,
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(shown.returncode, 0, shown.stdout + shown.stderr)
            self.assertEqual(json.loads(shown.stdout), {
                "goal": "lit-review-example", "jobs": {"lit-review-example.scan": "draft",
                "lit-review-example.synthesis": "draft", "lit-review-example.review": "draft"},
                "done": False, "ended_badly": []})
            led = Ledger(Path(env["RH_HOME"]) / "ledger.db")
            try:
                rows = led.jobs()
                self.assertEqual(len(rows), 3)
                self.assertEqual({row["spec"]["role"] for row in rows}, {"scout", "analyst", "critic"})
                for row in rows:
                    self.assertEqual(row["kind"], "local-shell")
                    self.assertEqual(row["spec"]["command"], ["true"])
                    self.assertIsNone(row["pinned_sha"])
            finally:
                led.close()


if __name__ == "__main__":
    unittest.main()
