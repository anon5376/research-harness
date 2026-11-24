import copy
import json
import tempfile
import unittest
from pathlib import Path

from rh import hierarchy


def agent(role, model, *children, count=1, effort=""):
    return {"role": role, "model": model, "effort": effort, "count": count, "children": list(children)}


def preset(tree, shape="vertical", plan=False, pid="team"):
    return {"id": pid, "name": "Team", "shape": shape, "plan": plan, "roles": hierarchy._roles(), "tree": tree}


class PlanTest(unittest.TestCase):
    def test_vertical_managers_judge_their_teams_and_the_lead_judges_the_managers(self):
        tree = agent("lead", "claude-opus-5-5",
                     agent("manager", "gpt-6.1-sol", agent("worker", "swe-2-max"), agent("worker", "swe-2-high")),
                     agent("manager", "claude-opus-5-5", agent("worker", "swe-2-flash-high", count=2)))
        jobs = hierarchy.plan(hierarchy.validate({"presets": [preset(tree, plan=True)]})["presets"][0])
        ids = [j["id"] for j in jobs]
        self.assertEqual(ids, ["plan", "w1", "w2", "m1", "w3", "w4", "m2", "best"])
        by = {j["id"]: j for j in jobs}
        self.assertEqual(by["m1"]["team"], ["w1", "w2"])
        self.assertEqual(by["m2"]["team"], ["w3", "w4"])  # count 2 is two agents on that model
        self.assertEqual(by["best"]["team"], ["m1", "m2"])
        self.assertEqual((by["plan"]["model"], by["plan"]["plan"], by["plan"]["work"]), ("claude-opus-5-5", True, True))
        self.assertEqual(by["m2"]["model"], "claude-opus-5-5")
        self.assertIn("manager", by["m1"]["prompt"])
        self.assertIn("lead agent", by["best"]["prompt"])
        self.assertTrue(by["w1"]["work"] and not by["m1"]["work"])

    def test_horizontal_runs_the_same_team_side_by_side_under_the_lead(self):
        tree = agent("lead", "swe-2-max",
                     agent("manager", "gpt-6.1-sol", agent("worker", "swe-2-max"), agent("worker", "swe-2-high")))
        jobs = hierarchy.plan(preset(tree, shape="horizontal"))
        self.assertEqual([j["id"] for j in jobs], ["w1", "w2", "w3", "best"])
        self.assertEqual(jobs[-1]["team"], ["w1", "w2", "w3"])
        self.assertEqual([j["model"] for j in jobs[:3]], ["gpt-6.1-sol", "swe-2-max", "swe-2-high"])

    def test_a_team_too_big_to_run_is_refused(self):
        many = agent("lead", "swe-2-max", *[agent("worker", "swe-2-max", count=3) for _ in range(3)])
        with self.assertRaisesRegex(ValueError, "at most 8 agents that do the work"):
            hierarchy.validate({"presets": [preset(many)]})
        with self.assertRaisesRegex(ValueError, "needs at least one agent"):
            hierarchy.plan(preset(agent("lead", "swe-2-max")))


class ValidateTest(unittest.TestCase):
    def test_defaults_are_valid_and_start_on_the_quick_swarm(self):
        d = hierarchy.defaults()
        self.assertEqual(hierarchy.validate(copy.deepcopy(d)), d)
        self.assertEqual(d["active"], hierarchy.QUICK)
        astra = hierarchy.find(d, "astra-lead")
        self.assertEqual((astra["tree"]["model"], astra["tree"]["effort"]), ("gpt-6.2-astra", "max"))
        self.assertEqual([k["model"] for k in astra["tree"]["children"]], ["gpt-6.2-sol", "claude-opus-5-5"])

    def test_bad_input_is_refused(self):
        good = preset(agent("lead", "swe-2-max", agent("worker", "swe-2-max")))
        for bad in ({**good, "id": "Has Space"}, {**good, "id": hierarchy.QUICK}, {**good, "shape": "diagonal"},
                    {**good, "tree": agent("ghost", "swe-2-max", agent("worker", "swe-2-max"))},
                    {**good, "tree": agent("lead", "Bad Model", agent("worker", "swe-2-max"))},
                    {**good, "tree": agent("lead", "swe-2-max", agent("worker", "swe-2-max", count=9))},
                    {**good, "roles": [{**hierarchy._roles()[0], "prompt": "x" * 5000}]}):
            with self.subTest(bad=str(bad)[:80]), self.assertRaises(ValueError):
                hierarchy.validate({"presets": [bad]})
        with self.assertRaises(ValueError):
            hierarchy.validate({"presets": [good, good]})  # two presets share an id
        # an active preset that is gone falls back to the quick swarm
        self.assertEqual(hierarchy.validate({"active": "nope", "presets": [good]})["active"], hierarchy.QUICK)

    def test_save_and_load_round_trip_and_a_broken_file_loads_the_defaults(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(hierarchy.load(home), hierarchy.defaults())
            data = {"active": "team", "presets": [preset(agent("lead", "swe-2-max", agent("worker", "swe-2-max")))]}
            stored = hierarchy.save(home, data)
            self.assertEqual(hierarchy.load(home), stored)
            self.assertEqual(json.loads((Path(home) / hierarchy.FILE).read_text())["active"], "team")
            (Path(home) / hierarchy.FILE).write_text("{not json")
            self.assertEqual(hierarchy.load(home), hierarchy.defaults())

    def test_the_quick_swarm_is_the_flat_ticks_with_no_prompts(self):
        q = hierarchy.quick(["swe-2-max", "swe-2-flash-high"], "swe-2-max")
        jobs = hierarchy.plan(q)
        self.assertEqual([(j["id"], j["model"]) for j in jobs],
                         [("w1", "swe-2-max"), ("w2", "swe-2-flash-high"), ("best", "swe-2-max")])
        self.assertTrue(all(j["prompt"] == "" for j in jobs))


if __name__ == "__main__":
    unittest.main()
