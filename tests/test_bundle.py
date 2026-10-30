import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from rh import bundle, gitx
from rh.bundle import BundleError
from tests import python_path


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout


def export_in_clone(repo, job_id, files, base, deleted=(), links=()):
    """Do what a cloud task does: clone at the pinned sha, write and stage files, then run the export script.
    Returns (printed bundle text, the clone's own staged diff)."""
    clone = Path(base) / f"task-{job_id}"
    subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True)
    git(clone, "config", "core.autocrlf", "false")
    for rel, data in files.items():
        p = clone / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
        if rel.endswith(".sh"):
            p.chmod(0o755)
    for rel, target in links:
        (clone / rel).parent.mkdir(parents=True, exist_ok=True)
        (clone / rel).symlink_to(target)
    for rel in deleted:
        git(clone, "rm", "-q", rel)
    git(clone, "add", "-A")
    expected = git(clone, "diff", "--cached", "--binary", "--full-index", "--no-renames")
    for rel in files:  # an unstaged edit after staging must not reach the bundle
        p = clone / rel
        p.write_bytes(p.read_bytes() + b"unstaged\n")
    r = subprocess.run([sys.executable, "-I", "-"], input=bundle.export_script(job_id), cwd=clone,
                       capture_output=True, text=True, check=True)
    return r.stdout, expected


class BundleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "core.autocrlf", "false")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "config", "core.hooksPath", "/dev/null")
        (self.repo / "README.md").write_text("base\n")
        (self.repo / "docs").mkdir()
        (self.repo / "docs/old.md").write_text("old\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.sha = gitx.head(self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def rebuild(self, text, job_id="j1"):
        header, tgz = bundle.parse(text, job_id)
        return header, bundle.to_patch(header, bundle.members(tgz), self.repo, self.sha, self.base / "work")

    def two_part_export(self):
        files = {"runs/j1/RESULT.json": json.dumps({"job": "j1"}), "runs/j1/a.bin": os.urandom(60_000),
                 "runs/j1/b.bin": os.urandom(60_000)}
        return export_in_clone(self.repo, "j1", files, self.base)

    @unittest.skipIf(os.name == "nt", "Windows does not preserve POSIX executable bits")
    def test_export_rebuilds_the_task_s_exact_staged_diff(self):
        files = {"runs/j1/RESULT.json": json.dumps({"job": "j1", "state": "done"}), "runs/j1/run.sh": "#!/bin/sh\n",
                 "runs/j1/a.bin": os.urandom(60_000), "runs/j1/b.bin": os.urandom(60_000), "README.md": "changed\n"}
        text, expected = export_in_clone(self.repo, "j1", files, self.base, deleted=["docs/old.md"])
        header, diff = self.rebuild(text)
        self.assertEqual(header["parts"], 2, "two incompressible 60 KB files need two 90 kB parts")
        self.assertEqual(header["deleted"], ["docs/old.md"])
        self.assertEqual(diff, expected)
        self.assertIn("new file mode 100755", diff)
        self.assertNotIn("unstaged", diff)
        self.assertFalse((self.base / "work").exists(), "the scratch worktree is removed")

    def test_export_is_deterministic(self):
        files = {"runs/j1/RESULT.json": "{}", "runs/j1/notes.md": "n\n"}
        a, _ = export_in_clone(self.repo, "j1", files, self.base / "a")
        b, _ = export_in_clone(self.repo, "j1", files, self.base / "b")
        self.assertEqual(a, b)

    def test_a_part_with_a_bad_sha_is_refused(self):
        text, _ = self.two_part_export()
        lines = text.splitlines()
        i = next(n for n, line in enumerate(lines) if line.startswith("RH-PART") and '"n": 2' in line) + 1
        lines[i] = ("B" if lines[i][0] != "B" else "C") + lines[i][1:]
        with self.assertRaisesRegex(BundleError, r"part 2/2: sha256 mismatch"):
            bundle.parse("\n".join(lines), "j1")
        self.rebuild(text)  # the untouched text still passes

    def test_whole_sha_missing_part_and_wrong_job_are_refused(self):
        text, _ = self.two_part_export()
        header = json.loads(text.splitlines()[0][len("RH-BUNDLE "):])
        bad = text.replace(header["sha256"], "0" * 64)
        with self.assertRaisesRegex(BundleError, "whole-bundle"):
            bundle.parse(bad, "j1")
        with self.assertRaisesRegex(BundleError, "part 2/2 is missing"):
            bundle.parse(text[:text.index('RH-PART {"n": 2')], "j1")
        with self.assertRaisesRegex(BundleError, "not j2"):
            bundle.parse(text, "j2")

    def test_large_files_are_listed_not_carried(self):
        big = os.urandom(70_000)
        text, _ = export_in_clone(self.repo, "j1", {"runs/j1/RESULT.json": "{}", "runs/j1/big.bin": big}, self.base)
        header, tgz = bundle.parse(text, "j1")
        self.assertEqual([(f["path"], f["bytes"]) for f in header["large"]], [("runs/j1/big.bin", 70_000)])
        self.assertEqual(sorted(bundle.members(tgz)), ["runs/j1/RESULT.json"])

    @unittest.skipIf(os.name == "nt", "POSIX only: staged symlinks")
    def test_a_staged_symlink_is_refused(self):
        text, _ = export_in_clone(self.repo, "j1", {"runs/j1/RESULT.json": "{}"}, self.base,
                                  links=[("runs/j1/escape", "../../README.md")])
        with self.assertRaisesRegex(BundleError, "symlink or submodule"):
            bundle.parse(text, "j1")

    def test_unsafe_members_are_refused(self):
        for name in ("../x", "/abs", "a/../b", ".git/hooks/pre-commit", "sub/.GIT/config", "a//b"):
            with self.subTest(name=name), self.assertRaisesRegex(BundleError, "unsafe path"):
                bundle.members(tgz_of(name=name))
        with self.assertRaisesRegex(BundleError, "not a regular file"):
            bundle.members(tgz_of(name="runs/j1/link", link="../../README.md"))

    def test_a_bundle_from_another_commit_is_stale(self):
        text, _ = export_in_clone(self.repo, "j1", {"runs/j1/RESULT.json": "{}"}, self.base)
        header, tgz = bundle.parse(text, "j1")
        with self.assertRaisesRegex(BundleError, "stale input"):
            bundle.to_patch(dict(header, head="d" * 40), bundle.members(tgz), self.repo, self.sha, self.base / "w")

    @unittest.skipIf(os.name == "nt", "POSIX only: export prompt uses shell paths and sed")
    def test_follow_up_prints_pieces_under_the_output_cap_that_rejoin(self):
        files = {"runs/j1/RESULT.json": "{}", "runs/j1/a.bin": os.urandom(60_000), "runs/j1/b.bin": os.urandom(60_000)}
        _, expected = export_in_clone(self.repo, "j1", files, self.base)
        out, n = self.base / "bundle.txt", bundle.PIECE_LINES
        prompt = bundle.export_prompt("j1", self.sha, ["runs/j1/"]).replace(bundle.bundle_file("j1"), str(out))
        self.assertIn(f"sed -n '1,{n}p' {out}", prompt)
        subprocess.run(["sh", "-c", prompt[prompt.index("python3 - >"):]], cwd=self.base / "task-j1",
                       env=python_path(self.base), check=True)
        total = len(out.read_text().splitlines())
        pieces = self.base / "pieces"
        pieces.mkdir()
        for k, start in enumerate(range(1, total + 1, n), 1):
            piece = subprocess.run(["sed", "-n", f"{start},{start + n - 1}p", str(out)], capture_output=True,
                                   text=True, check=True).stdout
            self.assertLess(len(piece), 65_536, "one piece must fit a task's command output")
            (pieces / f"{k:03d}.txt").write_text(piece)
        self.assertGreater(k, 2)
        self.assertEqual(self.rebuild(bundle.read(pieces))[1], expected)

    def test_replies_saved_in_pieces_with_fences_and_prose_are_read(self):
        text, expected = self.two_part_export()
        cut = text.index('RH-PART {"n": 2')
        d = self.base / "parts"
        d.mkdir()
        (d / "1.txt").write_text("Here is the output:\n```\n" + text[:cut] + "```\n")
        (d / "2.txt").write_text("```text\n" + text[cut:] + "```\n")
        self.assertEqual(self.rebuild(bundle.read(d))[1], expected)


def tgz_of(name, data=b"x", link=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        if link:
            info.type, info.linkname = tarfile.SYMTYPE, link
            tar.addfile(info)
        else:
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


if __name__ == "__main__":
    unittest.main()
