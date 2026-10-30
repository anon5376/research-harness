import tempfile
import unittest
from pathlib import Path

from rh import contract
from rh.contract import ContractError, View, load, preflight

ROOT = Path(__file__).resolve().parents[1]
CAPS = contract.load_caps(ROOT / "caps.toml")
POLICY = contract.load_policy(ROOT / "policy.toml")


def spec(extra="", kind="local-shell", brief="Summarise the inputs into runs/j1/out.md."):
    cmd = 'command = ["true"]\n' if kind == "local-shell" else ""
    return load(f'id = "j1"\nkind = "{kind}"\nbrief = """{brief}"""\n{cmd}{extra}')


def decide(s, view=None):
    return preflight(s, CAPS, POLICY, view or View())


class LoadTest(unittest.TestCase):
    def test_defaults_write_root_to_runs_job(self):
        self.assertEqual(spec()["write_root"], "runs/j1/")

    def test_rejects_bad_id_kind_needs_and_hash(self):
        for text in ('id = "Bad Id"\nkind = "local-shell"\nbrief = "x"\ncommand = ["true"]',
                     'id = "j1"\nkind = "robot"\nbrief = "x"',
                     'id = "j1"\nkind = "local-shell"\nbrief = "x"\ncommand = ["true"]\nneeds = ["teleport"]',
                     'id = "j1"\nkind = "local-shell"\nbrief = "x"\ncommand = ["true"]\n'
                     'inputs = [{path = "a", sha256 = "abc"}]',
                     'id = "j1"\nkind = "local-shell"\nbrief = "x"',
                     'id = "j1"\nkind = "local-shell"\nbrief = "  "\ncommand = ["true"]',
                     'not toml ='):
            with self.subTest(text=text), self.assertRaises(ContractError):
                load(text)


class PreflightTest(unittest.TestCase):
    def test_plain_job_is_ready(self):
        self.assertEqual(decide(spec()).verdict, "ready")

    def test_needs_a_capability_the_kind_lacks(self):
        d = decide(spec('needs = ["commit"]', kind="codex-cloud"))
        self.assertEqual(d.verdict, "hold")
        self.assertIn("needs commit, but codex-cloud cannot", d.reasons)

    def test_unknown_capability_holds_unless_probe(self):
        d = decide(spec('needs = ["loopback"]', kind="codex-cloud"))
        self.assertEqual(d.verdict, "hold")
        self.assertIn("probe", d.reasons[0])
        self.assertEqual(decide(spec('needs = ["loopback"]\nprobe = true', kind="codex-cloud")).verdict, "ready")
        # a probe never unlocks a capability that is known to be missing
        self.assertEqual(decide(spec('needs = ["sysctl"]\nprobe = true', kind="codex-cloud")).verdict, "hold")

    def test_inputs_missing_waits_and_changed_holds(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            s = spec('inputs = [{path = "data/a.csv", sha256 = "%s"}]' % ("0" * 64))
            self.assertEqual(decide(s, View(input_root=root)).verdict, "wait")
            (root / "data").mkdir()
            (root / "data/a.csv").write_text("x,y\n")
            d1 = decide(s, View(input_root=root))
            self.assertEqual(d1.verdict, "hold")
            self.assertIn("input changed", d1.reasons[0])
            good = spec('inputs = [{path = "data/a.csv", sha256 = "%s"}]' % contract.file_sha256(root / "data/a.csv"))
            self.assertEqual(decide(good, View(input_root=root)).verdict, "ready")

    def test_upstream_job_gates_on_done(self):
        s = spec('after = ["up"]')
        self.assertEqual(decide(s, View(states={"up": "running"})).verdict, "wait")
        self.assertEqual(decide(s, View(states={"up": "done"})).verdict, "ready")
        self.assertEqual(decide(s, View(states={"up": "failed"})).verdict, "hold")
        self.assertEqual(decide(s, View()).verdict, "hold")

    def test_revision_two_needs_changed_and_prior(self):
        self.assertEqual(decide(spec("revision = 2")).verdict, "hold")
        self.assertEqual(decide(spec('revision = 2\nchanged = "new survey data"')).verdict, "hold")
        ok = spec('revision = 2\nchanged = "new survey data"\nprior = "runs/j0/RESULT.json"')
        self.assertEqual(decide(ok).verdict, "ready")

    def test_operator_facing_job_needs_a_card(self):
        self.assertEqual(decide(spec("operator_facing = true")).verdict, "hold")
        card = '\n[card]\nsaid = "a one-page summary I can read on my phone"\nform = "file"\ndone_when = "file opens"'
        self.assertEqual(decide(spec("operator_facing = true" + card)).verdict, "ready")

    def test_write_root_overlap_with_live_job_holds(self):
        self.assertEqual(decide(spec(), View(live_roots={"j2": "runs/"})).verdict, "hold")
        self.assertEqual(decide(spec(), View(live_roots={"j2": "runs/j1/sub/"})).verdict, "hold")
        self.assertEqual(decide(spec(), View(live_roots={"j2": "runs/j10/"})).verdict, "ready")
        self.assertEqual(decide(spec('write_root = "../elsewhere/"')).verdict, "hold")
        # a shared source file declared in write_paths counts as overlap too
        shared = spec('write_paths = ["rh/supervisor.py"]')
        self.assertEqual(decide(shared, View(live_roots={"j2": ["runs/j2/", "rh/supervisor.py"]})).verdict, "hold")
        self.assertEqual(decide(shared, View(live_roots={"j2": ["runs/j2/", "rh/ledger.py"]})).verdict, "ready")

    def test_model_outside_policy_holds(self):
        s = spec('[model]\nrequested = "some-other-model"\nspeed = "standard"', kind="codex-cloud")
        self.assertEqual(decide(s).verdict, "hold")
        ok = spec('[model]\nrequested = "gpt-6.1-sol"\nspeed = "standard"\neffort = "extra-high"', kind="codex-cloud")
        self.assertEqual(decide(ok).verdict, "ready")
        fast = spec('[model]\nrequested = "gpt-6.1-sol"\nspeed = "fast"', kind="codex-cloud")
        self.assertEqual(decide(fast).verdict, "hold")
        astra = spec('[model]\nrequested = "gpt-6-astra"\nspeed = "standard"\neffort = "extra-high"', kind="codex-cloud")
        self.assertEqual(decide(astra).verdict, "hold")
        lazy = spec('[model]\nrequested = "gpt-6.1-sol"\nspeed = "standard"\neffort = "light"', kind="codex-cloud")
        self.assertEqual(decide(lazy).verdict, "hold")

    def test_cloud_job_waits_for_pinned_commit(self):
        d = decide(spec(kind="codex-cloud"), View(pinned=(False, "commit not on origin/main: push pending")))
        self.assertEqual(d.verdict, "wait")
        self.assertIn("push pending", d.reasons[0])

    def test_operator_verbs_are_split_out_but_negations_pass(self):
        for brief in ("Write the report, then push it to origin.", "Publish the results page.",
                      "Deploy the site when tests pass.", "Send an email to the reviewers.",
                      "Open a pull request with the fix.", "Sign in to the dashboard and export data.",
                      "Change the repository settings to allow forks."):
            with self.subTest(brief=brief):
                d = decide(spec(brief=brief))
                self.assertEqual(d.verdict, "operator")
                self.assertEqual(len(d.operator_steps), 1)
        for brief in ("Write the report. Never push. Do not publish anything.",
                      "Summarise without deploying; don't send any email.",
                      "List which settings files exist.", "Read the publishing guidelines in docs/."):
            with self.subTest(brief=brief):
                self.assertEqual(decide(spec(brief=brief)).verdict, "ready")

    def test_hold_outranks_operator_and_wait(self):
        s = spec('needs = ["commit"]\nafter = ["up"]', kind="codex-cloud", brief="Publish it.")
        d = decide(s, View(states={"up": "running"}))
        self.assertEqual(d.verdict, "hold")
        self.assertTrue(any("waiting for up" in r for r in d.reasons))
        self.assertEqual(len(d.operator_steps), 1)


if __name__ == "__main__":
    unittest.main()
