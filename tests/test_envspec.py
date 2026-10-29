import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from rh.envspec import env_spec, repo_slug

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "cloud" / "setup.sh"
SCP = "git" + "@" + "github.com:"


class SlugTest(unittest.TestCase):
    def test_remote_forms(self):
        cases = [SCP + "owner/repo.git", SCP + "owner/repo", "https://github.com/owner/repo",
                 "https://github.com/owner/repo.git/", "http://www.github.com/owner/repo.git",
                 "ssh://git" + "@" + "github.com/owner/repo.git", "ssh://git" + "@" + "github.com:22/owner/repo.git",
                 "git://github.com/owner/repo.git", "https://user:pw" + "@" + "GitHub.com/owner/repo.git",
                 "  https://github.com/owner/repo.git\n"]
        for url in cases:
            with self.subTest(url=url):
                self.assertEqual(repo_slug(url), "owner/repo")
        self.assertEqual(repo_slug("https://github.com/my-org/re.po_x-1"), "my-org/re.po_x-1")

    def test_non_github_or_odd_paths_raise(self):
        for url in ("https://gitlab.example.com/owner/repo.git", "https://github.com/owner/repo/tree/main",
                    "https://github.com/owner", "/srv/git/repo.git", "", "https://github.com/-x/repo",
                    "https://github.com/owner/..", "https://github.example.com/owner/repo", None):
            with self.subTest(url=url), self.assertRaises(ValueError):
                repo_slug(url)

    def test_credentials_never_reach_the_error(self):
        secret = "hunter" + "2pass"
        with self.assertRaises(ValueError) as cm:
            repo_slug(f"https://user:{secret}" + "@" + "gitlab.example.com/owner/repo.git")
        self.assertNotIn(secret, str(cm.exception))


class SpecTest(unittest.TestCase):
    def test_spec_lists_every_operator_step_and_ends_with_the_config_command(self):
        spec = env_spec("owner/repo")
        for needle in ("Codex GitHub app access to owner/repo", "Repository:             owner/repo",
                       "Name:                   research-harness", "Setup script:           sh cloud/setup.sh",
                       "Agent internet access:  On", "Domain allowlist:       github.com", "All methods",
                       "git-upload-pack", "publish", "chatgpt.com/codex > Settings > Environments"):
            with self.subTest(needle=needle):
                self.assertIn(needle, spec)
        self.assertEqual(spec.rstrip("\n").splitlines()[-1], "rh config set cloud.env_id <ID>")

    def test_name_and_bad_input(self):
        self.assertIn("Name:                   rh-probe", env_spec("owner/repo", name="rh-probe"))
        for slug, name in (("owner", "n"), ("owner/repo/x", "n"), ("o/r\n", "n"), ("owner/repo", ""),
                           ("owner/repo", "a\nb")):
            with self.subTest(slug=slug, name=name), self.assertRaises(ValueError):
                env_spec(slug, name)


class SetupScriptTest(unittest.TestCase):
    def run_setup(self, path):
        return subprocess.run(["/bin/sh", str(SETUP)], env={"PATH": path, "HOME": self.tmp}, capture_output=True,
                              text=True, timeout=60, cwd=self.tmp)

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.tmp = d.name

    def stub(self, name, body):
        p = Path(self.tmp, "bin", name)
        p.parent.mkdir(exist_ok=True)
        p.write_text("#!/bin/sh\n" + body + "\n")
        p.chmod(0o755)

    def test_prints_versions_and_calls_no_installer(self):
        called = Path(self.tmp, "called")
        for tool in ("pip", "pip3", "apt", "apt-get", "brew", "curl", "wget", "npm", "uv", "conda", "sudo"):
            self.stub(tool, f'echo "$0" >> "{called}"')
        r = self.run_setup(f"{self.tmp}/bin:{os.environ.get('PATH', os.defpath)}")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"Python 3\.\d+")
        self.assertIn("git version", r.stdout)
        self.assertFalse(called.exists(), "setup.sh must install nothing")

    def test_missing_git_fails(self):
        self.stub("python3", f'exec "{sys.executable}" "$@"')
        r = self.run_setup(f"{self.tmp}/bin")
        self.assertNotEqual(r.returncode, 0)
        self.assertRegex(r.stdout, r"Python 3\.\d+")
        self.assertNotIn("git version", r.stdout)

    def test_old_python_fails_before_git(self):
        self.stub("python3", f'''if [ "$1" = "-c" ]; then
  exec "{sys.executable}" -c 'import sys; sys.version_info = (3, 9, 1); exec(sys.argv[1])' "$2"
fi
echo "Python 3.9.1"''')
        self.stub("git", 'echo "git version 2.0.0"')
        r = self.run_setup(f"{self.tmp}/bin")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("3.11", r.stderr)
        self.assertNotIn("git version", r.stdout)


if __name__ == "__main__":
    unittest.main()
