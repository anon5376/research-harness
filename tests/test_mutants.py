import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


TOOL = Path(__file__).resolve().parents[1] / "tools" / "mutants.py"


class MutantsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "tests").mkdir()
        (self.repo / "value.py").write_text("VALUE = 1\nIGNORED = 1\n")
        (self.repo / "tests" / "run.py").write_text(
            "import pathlib, sys, time\n"
            "scope = {}\n"
            "exec(pathlib.Path('value.py').read_text(), scope)\n"
            "if sys.argv[1:] == ['slow']: time.sleep(10)\n"
            "sys.exit(0 if scope['VALUE'] == 1 else 1)\n")
        subprocess.run(["git", "add", "value.py", "tests/run.py"], cwd=self.repo, check=True)

    def run_mutants(self, entries, timeout=5):
        listing = self.root / "list.toml"
        listing.write_text("\n".join(
            "[[mutant]]\n" + "\n".join(f"{k} = {json.dumps(v)}" for k, v in entry.items())
            for entry in entries))
        temp = self.root / "scratch"
        temp.mkdir(exist_ok=True)
        before = self.snapshot()
        result = subprocess.run([sys.executable, "-I", str(TOOL), "--repo", str(self.repo),
                                 "--timeout", str(timeout), str(listing)], capture_output=True, text=True,
                                timeout=20, env={**os.environ, "TMPDIR": str(temp),
                                                 "TEMP": str(temp), "TMP": str(temp)})
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(temp.iterdir()), [], "tool must clean up its temporary copies")
        return result

    def snapshot(self):
        return {str(p.relative_to(self.repo)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.repo.rglob("*") if p.is_file()}

    def entry(self, find="VALUE = 1", replace="VALUE = 2", name="caught", tests=None):
        return dict(name=name, file="value.py", find=find, replace=replace, tests=tests or ["check"])

    def test_killed_and_restored_between_mutants(self):
        result = self.run_mutants([self.entry(), self.entry(name="again")])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         ["KILLED  caught", "KILLED  again", "MUTANTS KILLED n=2/2"])

    def test_survived(self):
        result = self.run_mutants([self.entry("IGNORED = 1", "IGNORED = 2", "missed")])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.splitlines(), ["SURVIVED  missed", "MUTANTS KILLED n=0/1"])

    def test_missing_and_duplicate_are_errors(self):
        result = self.run_mutants([self.entry("absent", "x", "missing"), self.entry(" = 1", " = 2", "twice")])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.splitlines(),
                         ["ERROR  missing", "ERROR  twice", "MUTANTS KILLED n=0/2"])

    def test_timeout_is_killed(self):
        result = self.run_mutants([self.entry("IGNORED = 1", "IGNORED = 2", tests=["slow"])], timeout=0.1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["KILLED  caught", "MUTANTS KILLED n=1/1"])
