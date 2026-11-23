"""rh/improve.py: the round's brief, and the step that keeps an agent's patch only if it passes on a clean clone."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from rh import improve


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@localhost", *args],
                          capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()


class ImproveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "--quiet", "-b", "main")
        (self.repo / "greet.txt").write_text("helo\n", encoding="utf-8")
        git(self.repo, "add", "greet.txt")
        git(self.repo, "commit", "--quiet", "-m", "start")
        self.base = git(self.repo, "rev-parse", "HEAD")

    def patch(self, before, after):
        """A changes.patch as rh/agent.py writes it: the diff of an edited clone."""
        clone = self.tmp / "agent"
        git(self.tmp, "clone", "--quiet", "--shared", str(self.repo), str(clone))
        (clone / "greet.txt").write_text(after, encoding="utf-8")
        git(clone, "add", "--all")
        out = self.tmp / "changes.patch"
        out.write_text(git(clone, "diff", "--cached", "--binary") + "\n", encoding="utf-8")
        return out

    def test_a_passing_patch_lands_on_its_own_branch_and_the_checkout_is_untouched(self):
        patch = self.patch("helo\n", "hello\n")
        verdict, detail = improve.verify(self.repo, self.base, patch, "improve/j1", "Fix the greeting",
                                         check="grep -qx hello greet.txt")
        self.assertEqual(verdict, "landed", detail)
        self.assertEqual(git(self.repo, "show", "improve/j1:greet.txt"), "hello")
        self.assertEqual(git(self.repo, "rev-parse", "improve/j1^"), self.base)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)  # the loop never moves the checkout
        self.assertEqual((self.repo / "greet.txt").read_text(encoding="utf-8"), "helo\n")

    def test_a_patch_that_fails_the_check_or_does_not_apply_is_not_kept(self):
        patch = self.patch("helo\n", "hullo\n")
        verdict, detail = improve.verify(self.repo, self.base, patch, "improve/j2", "x", check="grep -qx hello greet.txt")
        self.assertEqual(verdict, "failed")
        (self.repo / "greet.txt").write_text("other\n", encoding="utf-8")
        git(self.repo, "commit", "--quiet", "-am", "moved on")
        verdict, _ = improve.verify(self.repo, git(self.repo, "rev-parse", "HEAD"), patch, "improve/j3", "x",
                                    check="true")
        self.assertEqual(verdict, "no-apply")
        empty = self.tmp / "empty.patch"
        empty.write_text("", encoding="utf-8")
        self.assertEqual(improve.verify(self.repo, self.base, empty, "improve/j4", "x", check="true")[0], "no-change")
        self.assertEqual(git(self.repo, "branch", "--list", "improve/*"), "")

    def test_the_brief_lists_what_earlier_rounds_tried(self):
        text = improve.brief("the job page", [{"title": "Fix the greeting", "verdict": "landed"}])
        self.assertIn("Focus: the job page", text)
        self.assertIn("- Fix the greeting (landed)", text)
        self.assertNotIn("Earlier rounds", improve.brief("", []))
        self.assertIn(improve.CHECK, text)

    def test_the_brief_is_not_held_as_an_operator_step(self):
        # The supervisor holds a job whose brief reads like contacting someone; the loop's own words must not.
        from rh import contract
        self.assertEqual(contract.operator_steps(improve.brief("", [])), [])
        self.assertEqual(contract.operator_steps(improve.brief("error text", [{"title": "Tidy", "verdict": "failed"}])), [])


if __name__ == "__main__":
    unittest.main()
