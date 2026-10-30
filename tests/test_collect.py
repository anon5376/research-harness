import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from rh import collect, contract, gitx


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout


class DiffCloud:
    def __init__(self, diff):
        self.d = diff

    def diff(self, task_id):
        return self.d


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo, self.home = base / "repo", base / "home"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "config", "core.hooksPath", "/dev/null")
        (self.repo / "README.md").write_text("base\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.sha = gitx.head(self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def task_diff(self, files):
        """The diff a cloud task would return: files written on top of the pinned sha."""
        git(self.repo, "checkout", "-q", "-b", "scratch")
        for rel, text in files.items():
            p = self.repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "task")
        d = git(self.repo, "diff", self.sha, "HEAD")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "branch", "-q", "-D", "scratch")
        return d

    def result(self, **over):
        r = {"job": "j1", "state": "done", "head": self.sha, "summary": "wrote notes",
             "checks": [{"cmd": "python3 -c pass", "exit": 0}], "outputs": ["runs/j1/notes.md"], "refusals": []}
        r.update(over)
        return json.dumps(r)

    def job(self, extra=""):
        spec = contract.load(f'id = "j1"\nkind = "codex-cloud"\nbrief = "Write notes."\n{extra}')
        return {"id": "j1", "spec": spec, "pinned_sha": self.sha, "external_id": "t1", "external_url": None,
                "started": 1.0, "state": "collecting"}

    def collect(self, files, extra=""):
        return collect.collect_cloud(self.job(extra), DiffCloud(self.task_diff(files)), self.repo, self.home)

    def test_done_result_passes_local_gate_and_lands_on_a_branch(self):
        state, rec = self.collect({"runs/j1/RESULT.json": self.result(), "runs/j1/notes.md": "n\n"},
                                  'accept = "test -s runs/j1/notes.md"')
        self.assertEqual(state, "done", rec)
        self.assertEqual(rec["changed_paths"], ["runs/j1/RESULT.json", "runs/j1/notes.md"])
        self.assertEqual(rec["checks"][-1], {"cmd": "test -s runs/j1/notes.md", "exit": 0, "where": "local"})
        self.assertEqual(len(rec["diff_sha256"]), 64)
        sha = collect.land({**self.job(), "state": "done"}, self.home)
        self.assertEqual(git(self.repo, "rev-parse", "cloud/j1").strip(), sha)
        self.assertEqual(gitx.head(self.repo), self.sha, "landing must not move main")

    def test_failing_local_gate_fails_a_done_result(self):
        state, rec = self.collect({"runs/j1/RESULT.json": self.result()}, 'accept = "test -f runs/j1/absent.md"')
        self.assertEqual(state, "failed")
        self.assertIn("acceptance gate exited", rec["detail"])

    def test_write_outside_root_fails_custody_before_reading_result(self):
        state, rec = self.collect({"runs/j1/RESULT.json": self.result(), "README.md": "changed\n"})
        self.assertEqual(state, "failed")
        self.assertIn("custody", rec["detail"])
        self.assertIn("README.md", rec["detail"])

    @unittest.skipIf(os.name == "nt", "Windows symlink creation may require developer mode or privileges")
    def test_symlink_inside_root_fails_custody(self):
        git(self.repo, "checkout", "-q", "-b", "scratch")
        (self.repo / "runs/j1").mkdir(parents=True)
        (self.repo / "runs/j1/RESULT.json").write_text(self.result())
        (self.repo / "runs/j1/escape").symlink_to("../../README.md")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "task")
        d = git(self.repo, "diff", self.sha, "HEAD")
        git(self.repo, "checkout", "-q", "main")
        state, rec = collect.collect_cloud(self.job(), DiffCloud(d), self.repo, self.home)
        self.assertEqual(state, "failed")
        self.assertIn("symlink", rec["detail"])

    def test_declared_write_path_is_allowed(self):
        state, _ = self.collect({"runs/j1/RESULT.json": self.result(), "docs/notes.md": "x\n"},
                                'write_paths = ["docs/notes.md"]')
        self.assertEqual(state, "done")

    def test_missing_result_is_no_result(self):
        state, rec = self.collect({"runs/j1/notes.md": "n\n"})
        self.assertEqual(state, "no_result")

    def test_result_from_another_commit_is_stale_input(self):
        state, rec = self.collect({"runs/j1/RESULT.json": self.result(head="d" * 40)})
        self.assertEqual(state, "failed")
        self.assertIn("stale input", rec["detail"])

    def test_refusal_blocks_with_its_text(self):
        state, rec = self.collect({"runs/j1/RESULT.json": self.result(refusals=["sandbox denied: network access"])})
        self.assertEqual(state, "blocked")
        self.assertEqual(rec["refusals"], ["sandbox denied: network access"])

    def test_land_refuses_a_diff_with_a_private_string(self):
        notes = "see /Us" + "ers/someone/private/notes\n"
        state, _ = self.collect({"runs/j1/RESULT.json": self.result(), "runs/j1/notes.md": notes})
        self.assertEqual(state, "done")
        with self.assertRaisesRegex(gitx.GitError, "privacy scan refused"):
            collect.land({**self.job(), "state": "done"}, self.home)
        r = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "--verify", "--quiet", "cloud/j1"])
        self.assertNotEqual(r.returncode, 0, "no branch may be created for a refused land")

    def test_only_done_jobs_land(self):
        with self.assertRaises(gitx.GitError):
            collect.land({**self.job(), "state": "failed"}, self.home)


if __name__ == "__main__":
    unittest.main()
