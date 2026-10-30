"""Tests for rh/privacy.py and hooks/.

Pattern-shaped strings are assembled by concatenation so this file itself scans clean.
Git runs in temp repos with global/system config and outer GIT_* variables cut off.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rh import privacy
from tests import python_path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "rh" / "privacy.py"
HOOKS = ROOT / "hooks"

AT = "@"
MAC, LINUX = "/Us" + "ers/", "/ho" + "me/"
BAD_MAIL = "bob" + AT + "corp-mail.io"
GHP = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
TERM = "zq" + "uorblet"
SCISSORS = "# " + "-" * 24 + " >8 " + "-" * 24

_env_patch = None


def setUpModule():
    global _env_patch
    home = tempfile.mkdtemp()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t" + AT + "example.com",
        GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t" + AT + "example.com",
        RH_HOME=os.path.join(home, "rh-home"),
        PATH=os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", ""),
    )
    _env_patch = mock.patch.dict(os.environ, env, clear=True)
    _env_patch.start()


def tearDownModule():
    _env_patch.stop()


def rules(findings):
    return sorted((f.path, f.line, f.rule) for f in findings)


def scan(text, terms=()):
    return [(f.line, f.rule) for f in privacy.scan_text(text, "t.txt", terms)]


class ScanTextTest(unittest.TestCase):
    def test_home_paths_flagged_with_masked_name(self):
        for text in (
            MAC + "alice/notes", LINUX + "bob/x", "C:\\Us" + "ers\\carol\\x", "C:/Us" + "ers/dan/x",
            '"c:\\\\us' + 'ers\\\\erin"', "/mnt/c" + MAC + "fay/x", "HOME=" + MAC + "gus",
            "file://" + MAC + "hal/x", "'" + LINUX + "ivy'", MAC + "jos\u00e9/x", "-I" + MAC + "kim/include",
            "-" + LINUX + "lee/removed-diff-line",
        ):
            with self.subTest(text=text):
                found = privacy.scan_text(text, "t.txt")
                self.assertEqual([(f.line, f.rule) for f in found], [(1, "home-path")])
                self.assertTrue(found[0].excerpt.endswith("***"))
        found = privacy.scan_text("at " + MAC + "alice/notes", "t.txt")
        self.assertEqual(found[0].excerpt, MAC + "a***")

    def test_home_placeholders_tilde_and_url_paths_pass(self):
        for text in (
            "~/notes", MAC + "<you>/x", "C:\\Us" + "ers\\<name>\\x", LINUX + "$USER/x", LINUX + "${USER}/x",
            MAC + ".../x", "https://example.com" + LINUX + "about", "src" + LINUX + "page", "~" + MAC + "x",
        ):
            with self.subTest(text=text):
                self.assertEqual(scan(text), [])

    def test_personal_emails_flagged_including_near_misses(self):
        for text in (
            BAD_MAIL, "First.Last+tag" + AT + "gmail.com", "noreply" + AT + "anthropic.co",
            "x" + AT + "notexample.com", "x" + AT + "users.noreply.github.com.evil.io",
            "a" + AT + "example.com.evil.io", "git" + AT + "corp-git.io:team/repo.git",
        ):
            with self.subTest(text=text):
                self.assertEqual(scan(text), [(1, "email")])
        excerpt = privacy.scan_text("mail " + BAD_MAIL, "t.txt")[0].excerpt
        self.assertEqual(excerpt, "b***" + AT + "c***.io")

    def test_allowlisted_emails_pass(self):
        for text in (
            "anon" + AT + "users.noreply.github.com", "12345+anon" + AT + "users.noreply.github.com",
            "X" + AT + "USERS.NOREPLY.GITHUB.COM", "Co-Authored-By: Claude <noreply" + AT + "anthropic.com>",
            "a" + AT + "example.com", "a" + AT + "mail.example.org", "a" + AT + "example.net",
            "a" + AT + "corp.example", "a" + AT + "host.test", "git" + AT + "github.com:owner/repo.git",
            "icon" + AT + "2x.png", "react" + AT + "18.3.1",
        ):
            with self.subTest(text=text):
                self.assertEqual(scan(text), [])

    def test_tokens_flagged_and_never_shown(self):
        tokens = [
            GHP,
            "sk-" + "ant-api03-" + "Xy9" * 10,
            "gh" + "o_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2",
            "github" + "_pat_" + "11ABCDEFG0123456789abc_" + "x1" * 20,
            "xo" + "xb-" + "1234567890-abcdefghijkl",
            "AK" + "IA" + "Z7QWERTY2UIOP3AS",
            "ey" + "JhbGciOiJIUzI1NiJ9" + "." + "ey" + "JzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "c2lnbmF0dXJlLXNpZw",
            "AI" + "za" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
            "sk" + "_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
            "hf" + "_" + "AbCdEfGhIjKlMnOpQrStUvWxYz012345",
            "glpat" + "-" + "xY9zW8vU7tS6rQ5pO4nM",
        ]
        for tok in tokens:
            with self.subTest(tok=tok[:4]):
                found = privacy.scan_text('key = "' + tok + '"', "t.txt")
                self.assertEqual([f.rule for f in found], ["token"])
                excerpt = found[0].excerpt
                shown = excerpt.split("***")[0]
                self.assertTrue(tok.startswith(shown), excerpt)
                body = tok[len(shown):]
                self.assertGreaterEqual(len(body), 16, excerpt)
                self.assertFalse(any(body[i:i + 6] in excerpt for i in range(len(body) - 5)), excerpt)
        pem = "-----BEG" + "IN RSA PRIVATE KEY-----"
        self.assertEqual(scan(pem + "\nMIIEow"), [(1, "token")])
        self.assertEqual(scan("-----BEG" + "IN OPENSSH PRIVATE KEY-----"), [(1, "token")])

    def test_token_lookalikes_pass(self):
        for text in (
            "pip install scikit-learn  # sk-learn", "sk-" + "this-is-a-long-kebab-case-name",
            "task-" + "runner-with-a-really-long-name", "AK" + "IA" + "IOSFODNN7" + "EXAMPLE", "AK" + "IA" + "X" * 16,
            "gh" + "p_" + "short1", "-----BEG" + "IN PUBLIC KEY-----", "ey" + "Jnot.ey" + "Jshort",
        ):
            with self.subTest(text=text):
                self.assertEqual(scan(text), [])

    def test_codex_task_id(self):
        self.assertEqual(scan("launched task" + "_e_" + "0123456789abcdef"), [(1, "codex-task-id")])
        self.assertEqual(scan("task" + "_e_" + "0123456789abcde"), [])

    def test_new_system_task_id_in_a_task_url(self):
        uid = "01a2b3c4" + "-d5e6-7f80-9a1b-" + "2c3d4e5f6a7b"
        for text in ("https://chatgpt.com/codex/tasks/" + uid, "see chatgpt.com/local/" + uid.upper() + "."):
            with self.subTest(text=text):
                found = privacy.scan_text(text, "t.txt")
                self.assertEqual([(f.line, f.rule) for f in found], [(1, "codex-task-id")])
                self.assertNotIn(uid, found[0].excerpt.lower())
        # a bare UUID is ordinary data, and placeholders pass
        for text in ("run " + uid, "chatgpt.com/local/" + "0" * 8 + "-0000-0000-0000-" + "0" * 12,
                     "https://chatgpt.com/codex/tasks/<task_id>"):
            with self.subTest(text=text):
                self.assertEqual(scan(text), [])

    def test_line_numbers_with_crlf(self):
        found = privacy.scan_text("a\r\nb\x0cc\u2028d\r\nmail " + BAD_MAIL + "\r\n", "t.txt")
        self.assertEqual([(f.line, f.rule) for f in found], [(3, "email")])
        self.assertNotIn("\r", found[0].excerpt)


class DenyTermTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_file_line_numbers_comments_and_case_folding(self):
        deny = self.tmp / "deny.txt"
        deny.write_text("# private terms\n\n" + TERM + "\n  Stra\u00dfe  # street\n", encoding="utf-8")
        terms = privacy.load_deny(deny)
        self.assertEqual(terms, [(3, TERM), (4, "Stra\u00dfe")])
        text = "ok\nabout " + TERM.upper() + " here\nSTRASSE 5\nnothing\n"
        self.assertEqual(scan(text, terms), [(2, "deny-term:3"), (3, "deny-term:4")])

    def test_plain_terms_numbered_in_order(self):
        self.assertEqual(scan("x " + TERM, ["other", TERM]), [(1, "deny-term:2")])
        self.assertEqual(scan("x " + TERM, TERM), [(1, "deny-term:1")])

    def test_term_never_shown_in_path_or_excerpt(self):
        found = privacy.scan_text("say " + TERM + " at " + MAC + TERM + "/x", "notes/" + TERM.title() + ".md", [TERM])
        self.assertEqual(sorted(f.rule for f in found), ["deny-term:1", "home-path"])
        for f in found:
            self.assertEqual(f.path, "notes/" + privacy.REDACTED + ".md")
            self.assertNotIn(TERM, str(f).lower())


class GitCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q")

    def git(self, *args, check=True, env=None):
        r = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, env=env)
        if check and r.returncode:
            self.fail(f"git {args} failed: {r.stderr}")
        return r

    def put(self, rel, data):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
        return p

    def stage(self, rel, data):
        self.put(rel, data)
        self.git("add", "-A")

    def both(self, terms=()):
        """(staged findings, tree findings) as sorted (path, line, rule)."""
        return rules(privacy.scan_repo(self.repo, terms, staged=True)), rules(privacy.scan_repo(self.repo, terms))


class ScanRepoTest(GitCase):
    def test_staged_reads_index_not_working_copy(self):
        bad = "path " + MAC + "alice/x\n"
        self.stage("a.txt", bad)
        self.put("a.txt", "clean\n")
        self.assertEqual(self.both(), ([("a.txt", 1, "home-path")], []))
        self.stage("a.txt", "clean\n")
        self.put("a.txt", bad)
        self.assertEqual(self.both(), ([], [("a.txt", 1, "home-path")]))

    def test_staged_scans_added_modified_renamed_not_deleted(self):
        bad = "mail " + BAD_MAIL + "\n"
        self.stage("old.txt", bad)
        self.git("commit", "-qm", "legacy")
        self.stage("new.txt", "fine\n")
        self.assertEqual(self.both(), ([], [("old.txt", 1, "email")]))
        self.git("mv", "old.txt", "moved.txt")
        self.assertEqual(rules(privacy.scan_repo(self.repo, staged=True)), [("moved.txt", 1, "email")])
        self.git("rm", "-q", "--cached", "moved.txt")
        self.assertEqual(privacy.scan_repo(self.repo, staged=True), [])

    @unittest.skipIf(os.name == "nt", "Windows filenames cannot contain all tested characters")
    def test_odd_file_names(self):
        names = ["dir with space/file one.txt", "\u00fcn\u00ef c\u00f6d\u00e9.txt", "*.txt", ":(glob)x",
                 "-dash.txt", "line\nbreak.txt", "tab\there.txt"]
        for n in names:
            self.put(n, "mail " + BAD_MAIL + "\n")
        self.git("add", "-A")
        expected = sorted((n, 1, "email") for n in names)
        self.assertEqual(self.both(), (expected, expected))
        for f in privacy.scan_repo(self.repo, staged=True):
            self.assertNotIn("\n", str(f))

    def test_binary_skipped_but_utf16_scanned(self):
        self.stage("blob.bin", b"\x00\x01mail " + BAD_MAIL.encode())
        self.stage("wide.txt", ("mail " + BAD_MAIL + "\n").encode("utf-16"))
        self.assertEqual(self.both(), ([("wide.txt", 1, "email")],) * 2)

    def test_over_two_megabytes_fails_closed(self):
        self.stage("big.txt", b"a" * (privacy.MAX_BYTES + 1))
        self.stage("big.bin", b"\0" * (privacy.MAX_BYTES + 1))
        self.stage("edge.txt", b"a" * privacy.MAX_BYTES)
        self.assertEqual(self.both(), ([("big.bin", 0, "too-large"), ("big.txt", 0, "too-large")],) * 2)

    @unittest.skipIf(os.name == "nt", "Windows symlink creation may require developer mode or privileges")
    def test_symlink_target_scanned(self):
        os.symlink(MAC + "alice/secret", self.repo / "link")
        self.git("add", "-A")
        self.assertEqual(self.both(), ([("link", 1, "home-path")],) * 2)

    def test_file_names_scanned_and_redacted(self):
        self.stage("notes/" + TERM + ".md", "fine\n")
        redacted = [("notes/" + privacy.REDACTED + ".md", 0, "deny-term:1")]
        self.assertEqual(self.both([TERM]), (redacted, redacted))

    def test_gitlink_entries_skipped(self):
        self.stage("a.txt", "fine\n")
        self.git("update-index", "--add", "--cacheinfo", "160000," + "ab" * 20 + ",sub")
        self.assertIn("sub", self.git("ls-files").stdout.split())
        self.assertEqual(self.both(), ([], []))

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0, "needs a non-root POSIX user")
    def test_unreadable_tracked_file_fails_closed(self):
        p = self.put("a.txt", "fine\n")
        self.git("add", "-A")
        p.chmod(0)
        self.addCleanup(p.chmod, 0o644)
        with self.assertRaises(privacy.PrivacyError):
            privacy.scan_repo(self.repo)

    def test_not_a_work_tree_fails_closed(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        ceiling = os.pathsep.join([str(self.tmp), os.path.realpath(self.tmp)])
        with mock.patch.dict(os.environ, GIT_CEILING_DIRECTORIES=ceiling):
            for staged in (False, True):
                with self.subTest(staged=staged), self.assertRaises(privacy.PrivacyError):
                    privacy.scan_repo(plain, staged=staged)


class CliTest(GitCase):
    def cli(self, *args, cwd=None, env=None):
        cmd = [sys.executable, "-I", str(SCRIPT), *map(str, args)]
        return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, env=env)

    def test_report_format_and_exit_codes(self):
        ok = self.put("ok.txt", "nothing here\n")
        bad = self.put("bad.txt", "line one\nmail " + BAD_MAIL + " and " + GHP + "\n")
        r = self.cli("--paths", ok)
        self.assertEqual((r.returncode, r.stdout), (0, "PRIVACY CLEAN files=1\n"))
        r = self.cli("--paths", ok, bad)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.splitlines(), [
            f"{bad}:2: email: b***" + AT + "c***.io", f"{bad}:2: token: ghp_***(40 chars)",
            "PRIVACY FAIL findings=2 files=2",
        ])
        self.assertNotIn(GHP, r.stdout + r.stderr)

    def test_usage_errors_exit_2(self):
        ok = self.put("ok.txt", "fine\n")
        for args in (
            ["--file", ok], ["--path", "NAME"], ["--staged", "--paths", ok], ["--bogus"],
            ["--paths", self.tmp / "missing.txt"], ["--paths", self.tmp], ["--paths", ok, "--deny", self.tmp / "nope"],
        ):
            with self.subTest(args=args):
                r = self.cli(*args)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertNotIn("PRIVACY CLEAN", r.stdout)

    def test_default_deny_file_comes_from_rh_home(self):
        home = self.tmp / "rh-home"
        env = dict(os.environ, RH_HOME=str(home))
        f = self.put("t.txt", "about " + TERM + "\n")
        r = self.cli("--paths", f, env=env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("no deny file", r.stderr)
        home.mkdir()
        (home / "privacy-deny.txt").write_text(TERM + "\n")
        r = self.cli("--paths", f, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn(":1: deny-term:1: " + privacy.REDACTED, r.stdout)
        self.assertNotIn(TERM, (r.stdout + r.stderr).lower())

    def test_tree_mode_covers_whole_repo_from_a_subdirectory(self):
        self.stage("top.txt", "mail " + BAD_MAIL + "\n")
        self.stage("sub/clean.txt", "fine\n")
        self.put("sub/untracked.txt", "mail " + BAD_MAIL + "\n")
        r = self.cli(cwd=self.repo / "sub")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual([ln for ln in r.stdout.splitlines() if ": email: " in ln], ["top.txt:1: email: b***" + AT + "c***.io"])

    def test_file_mode_reports_under_given_name(self):
        msg = self.put("MSG", "fix\n\nCo-Authored-By: X <noreply" + AT + "anthropic.com>\n")
        r = self.cli("--file", msg, "--path", "COMMIT_EDITMSG")
        self.assertEqual((r.returncode, r.stdout), (0, "PRIVACY CLEAN files=1\n"))
        self.put("MSG", "fix\n\nmail " + BAD_MAIL + "\n")
        r = self.cli("--file", msg, "--path", "COMMIT_EDITMSG")
        self.assertEqual(r.returncode, 1)
        self.assertIn("COMMIT_EDITMSG:3: email:", r.stdout)


class HookTest(GitCase):
    def setUp(self):
        super().setUp()
        (self.repo / "rh").mkdir()
        shutil.copy2(SCRIPT, self.repo / "rh" / "privacy.py")
        (self.repo / ".git" / "info").mkdir(exist_ok=True)
        (self.repo / ".git" / "info" / "exclude").write_text("rh/\n")
        self.git("config", "core.hooksPath", str(HOOKS))

    def commit(self, *args, env=None):
        run_env = dict(os.environ)
        if env is not None:
            run_env.update(env)
        return self.git("commit", "-q", *args, check=False, env=python_path(self.tmp, run_env))

    def commits(self):
        return int(self.git("rev-list", "--count", "--all").stdout)

    def test_pre_commit_blocks_staged_leak(self):
        self.stage("a.txt", "see " + MAC + "alice/x\n")
        r = self.commit("-m", "add")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("a.txt:1: home-path:", r.stdout + r.stderr)
        self.assertEqual(self.commits(), 0)
        self.stage("a.txt", "see ~/x and " + MAC + "<you>/x\n")
        r = self.commit("-m", "add")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.commits(), 1)

    def test_commit_all_scans_the_temporary_index(self):
        self.stage("a.txt", "fine\n")
        self.assertEqual(self.commit("-m", "one").returncode, 0)
        self.put("a.txt", "token " + GHP + "\n")
        r = self.commit("-a", "-m", "two")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("a.txt:1: token:", r.stdout + r.stderr)
        self.assertNotIn(GHP, r.stdout + r.stderr)
        self.assertEqual(self.commits(), 1)

    def test_commit_msg_blocks_private_message(self):
        self.stage("a.txt", "fine\n")
        r = self.commit("-m", "fix\n\nreported by " + BAD_MAIL)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("COMMIT_EDITMSG:3: email:", r.stdout + r.stderr)
        self.assertEqual(self.commits(), 0)
        r = self.commit("-m", "fix\n\nCo-Authored-By: Claude <noreply" + AT + "anthropic.com>")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_pre_commit_blocks_private_identity(self):
        self.stage("a.txt", "fine\n")
        r = self.commit("-m", "x", "--author", "Bob <" + BAD_MAIL + ">")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("identity:1: email:", r.stderr)
        r = self.commit("-m", "x", env=dict(os.environ, GIT_COMMITTER_EMAIL=BAD_MAIL))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("identity:2: email:", r.stderr)
        self.assertEqual(self.commits(), 0)
        r = self.commit("-m", "x", "--author", "Bob <1+bob" + AT + "users.noreply.github.com>")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_commit_msg_scans_below_scissors_line(self):
        # without -v git commits text below a scissors line verbatim
        self.stage("a.txt", "fine\n")
        msg = self.put("../MSG", "fix\n" + SCISSORS + "\nmail " + BAD_MAIL + "\n")
        r = self.commit("-F", str(msg))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("COMMIT_EDITMSG:3: email:", r.stdout + r.stderr)
        self.assertEqual(self.commits(), 0)


if __name__ == "__main__":
    unittest.main()


class AccountIdTest(unittest.TestCase):
    """The codex CLI writes account_id=<uuid> into error.log; that must never reach the repo."""
    UUID = "3f2b9c1e" + "-4d5a-4b6c-9e8f-" + "0a1b2c3d4e5f"

    def rules(self, text):
        return [f.rule for f in privacy.scan_text(text, "x.log")]

    def test_labelled_account_ids_are_flagged_and_masked(self):
        for line in (f"auth: mode=ChatGPT account_id={self.UUID}", f"set ChatGPT-Account-Id header: {self.UUID}",
                     f'{{"workspace_id": "{self.UUID}"}}', "org-id: " + "Xk3" + "p9QzR2vLm8TbWq7Y"):
            with self.subTest(line=line):
                found = privacy.scan_text(line, "x.log")
                self.assertEqual([f.rule for f in found], ["account-id"])
                self.assertNotIn(self.UUID, found[0].excerpt)

    def test_placeholders_and_slugs_pass(self):
        for line in ("account_id = <your account id>", "user_id=00000000-0000-0000-0000-000000000000",
                     'user_id = "my-test-user-name"', "account id: see the settings page"):
            with self.subTest(line=line):
                self.assertEqual(self.rules(line), [])
