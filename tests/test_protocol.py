import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from rh import protocol
from rh.protocol import ResultError, custody_violations, diff_paths, parse_result, task_prompt

SHA, OTHER = "a" * 40, "b" * 40
PIN = f'git fetch origin main && git checkout --detach {SHA} && test "$(git rev-parse HEAD)" = "{SHA}"'


def result(**over):
    data = {"job": "probe-1", "state": "done", "head": SHA, "summary": "s", "checks": [{"cmd": "x", "exit": 0}],
            "outputs": ["runs/probe-1/out.txt"], "refusals": []}
    data.update(over)
    return json.dumps(data)


class PromptTest(unittest.TestCase):
    def test_pin_comes_first_then_rules_then_brief(self):
        p = task_prompt("probe-1", SHA, "BRIEF TEXT\nsecond line", ["docs/x.md", "data/"], attempt=2)
        lines = p.splitlines()
        self.assertIn(PIN, lines)
        self.assertLess(p.index(PIN), p.index("Brief:"))
        self.assertLess(p.index("Brief:"), p.index("BRIEF TEXT\nsecond line"))
        self.assertIn("Write only to: runs/probe-1/, docs/x.md, data/.", p)
        self.assertIn("Never push, open pull requests, send messages or send notifications.", p)
        self.assertIn("Do not retry, reword or work around", p)
        self.assertIn('exact text in "refusals"', p)
        self.assertIn("attempt 2", lines[0])
        self.assertIn("runs/probe-1/RESULT.json", p)

    def test_find_the_checkout_comes_before_the_pin_and_only_the_fetch_uses_the_network(self):
        p = task_prompt("probe-1", SHA, "b", [])
        self.assertLess(p.index("cd \"$(git rev-parse --show-toplevel)\""), p.index(PIN))
        self.assertLess(p.index("find the checkout with `ls /workspace`, cd into it"), p.index(PIN))
        self.assertLess(p.index("It is the only command that may use the network."), p.index(PIN))

    def test_schema_in_the_prompt_is_the_result_format_and_does_not_hand_out_the_sha(self):
        p = task_prompt("probe-1", SHA, "b", [])
        schema = json.loads(next(line for line in p.splitlines() if line.startswith("{")))
        self.assertEqual(set(schema), {"job", "state", "head", "summary", "checks", "outputs", "refusals"})
        self.assertEqual(schema["job"], "probe-1")
        self.assertNotEqual(schema["head"], SHA)
        self.assertEqual(p.count(SHA), 2)

    def test_bad_inputs_raise(self):
        bad = [("Bad ID", SHA, "b", []), ("", SHA, "b", []), ("-x", SHA, "b", []), ("a" * 64, SHA, "b", []),
               ("a/b", SHA, "b", []), ("a\n", SHA, "b", []), (".x", SHA, "b", []), ("ok", "abc", "b", []),
               ("ok", SHA.upper(), "b", []), ("ok", SHA + "\n", "b", []), ("ok", SHA, "  ", []),
               ("ok", SHA, "b", ["/abs"]), ("ok", SHA, "b", ["../x"]), ("ok", SHA, "b", ["a/../b"]),
               ("ok", SHA, "b", [""]), ("ok", SHA, "b", ["~/x"]), ("ok", SHA, "b", ["a\\b"]),
               ("ok", SHA, "b", ["a\nb"]), ("ok", SHA, "b", "docs/x.md")]
        for args in bad:
            with self.subTest(args=args), self.assertRaises(ValueError):
                task_prompt(*args)
        for attempt in (0, True, "1"):
            with self.subTest(attempt=attempt), self.assertRaises(ValueError):
                task_prompt("ok", SHA, "b", [], attempt=attempt)

    def test_job_id_edges_accepted(self):
        for job in ("a", "a" * 63, "x.y_z-1", "0"):
            with self.subTest(job=job):
                self.assertIn(f"runs/{job}/RESULT.json", task_prompt(job, SHA, "b", []))

    def test_write_scope_and_result_path(self):
        self.assertEqual(protocol.write_scope("j", ["d/", "f.md", "d/", "runs/j/"]), ["runs/j/", "d/", "f.md"])
        self.assertEqual(protocol.result_path("j"), "runs/j/RESULT.json")


class ResultTest(unittest.TestCase):
    def kind(self, text, job="probe-1"):
        with self.assertRaises(ResultError) as cm:
            parse_result(text, job, SHA)
        return cm.exception.kind

    def test_good_result_round_trips(self):
        self.assertEqual(parse_result(result(), "probe-1", SHA),
                         {"state": "done", "head": SHA, "summary": "s", "checks": [{"cmd": "x", "exit": 0}],
                          "outputs": ["runs/probe-1/out.txt"], "refusals": []})

    def test_error_kinds(self):
        self.assertEqual(self.kind(None), "missing")
        self.assertEqual(self.kind(result(head=OTHER)), "stale-input")
        self.assertEqual(self.kind(result(), job="other"), "wrong-job")
        for text in ("{nope", "", "[]", b"\xff\xfe", result(state="ok"), result(state=None), result(head="abc"),
                     result(head=None), result(job=None), result(checks=[{"cmd": "x", "exit": True}]),
                     result(checks=[{"cmd": "x", "exit": "0"}]), result(checks="x"), result(outputs=[1]),
                     result(summary=3), result(refusals="denied")):
            with self.subTest(text=text):
                self.assertEqual(self.kind(text), "invalid")

    def test_refusal_forces_blocked_and_keeps_exact_text(self):
        text = "Denied by policy: network access to example.com\n  (rule 7)"
        r = parse_result(result(refusals=[text]), "probe-1", SHA)
        self.assertEqual((r["state"], r["refusals"]), ("blocked", [text]))

    def test_refusal_wins_over_a_stale_head(self):
        r = parse_result(result(state="failed", head=OTHER, refusals=["denied"]), "probe-1", SHA)
        self.assertEqual((r["state"], r["head"]), ("blocked", OTHER))

    def test_invalid_file_still_carries_its_refusals(self):
        with self.assertRaises(ResultError) as cm:
            parse_result(result(state="weird", refusals=["denied: x"]), "probe-1", SHA)
        self.assertEqual((cm.exception.kind, cm.exception.refusals), ("invalid", ["denied: x"]))

    def test_lenient_on_what_does_not_matter(self):
        data = json.loads(result(head=SHA.upper(), refusals=["  "]))
        del data["summary"], data["checks"], data["outputs"]
        r = parse_result(b"\xef\xbb\xbf" + json.dumps(data).encode(), "probe-1", SHA)
        self.assertEqual(r, {"state": "done", "head": SHA, "summary": "", "checks": [], "outputs": [],
                             "refusals": []})

    def test_blocked_without_refusal_text_stays_blocked(self):
        self.assertEqual(parse_result(result(state="blocked"), "probe-1", SHA)["state"], "blocked")


def git(cwd, *args):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, env=env,
                          check=True, capture_output=True, timeout=30).stdout


class DiffPathsTest(unittest.TestCase):
    def test_real_git_diff_with_renames_quoting_binary_and_tricky_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git(root, "init", "-q")
            (root / "runs/p").mkdir(parents=True)
            files = {"runs/p/keep.sql": "select 1;\n-- comment\n++ not a path\nselect 2;\n",
                     "runs/p/with space.txt": "a\n", "old name.txt": "".join(f"line {i}\n" for i in range(20)),
                     "gone.txt": "bye\n"}
            for name, body in files.items():
                (root / name).write_text(body)
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", "base")
            (root / "runs/p/keep.sql").write_text("select 1;\n++ added\n-- added too\nselect 2;\n")
            (root / "runs/p/with space.txt").write_text("b\n")
            (root / "old name.txt").rename(root / "runs/p/new name.txt")
            (root / "gone.txt").unlink()
            (root / "runs/p/tab\there.txt").write_text("t\n")
            (root / 'runs/p/quote"d.txt').write_text("q\n")
            (root / "runs/p/日本.txt").write_text("u\n")
            (root / "runs/p/blob.bin").write_bytes(b"\0\1\2" * 50)
            git(root, "add", "-A")
            want = {"runs/p/keep.sql", "runs/p/with space.txt", "old name.txt", "runs/p/new name.txt", "gone.txt",
                    "runs/p/tab\there.txt", 'runs/p/quote"d.txt', "runs/p/日本.txt", "runs/p/blob.bin"}
            for flags in ([], ["--binary"]):
                with self.subTest(flags=flags):
                    text = git(root, "diff", "--cached", "-M", *flags).decode("utf-8")
                    self.assertIn('"b/runs/p/tab\\there.txt"', text)
                    paths = diff_paths(text)
                    self.assertEqual(set(paths), want)
                    self.assertEqual(len(paths), len(want))
                    self.assertEqual(set(custody_violations(paths, ["runs/p/"])),
                                     {"old name.txt", "gone.txt", "runs/p/tab\there.txt"})  # control chars fail

    def test_blackbox_shape_and_dev_null(self):
        d = ("diff --git a/runs/probe-1/out.txt b/runs/probe-1/out.txt\nnew file mode 100644\n--- /dev/null\n"
             "+++ b/runs/probe-1/out.txt\n@@ -0,0 +1 @@\n+x\ndiff --git a/rh/ledger.py b/rh/ledger.py\n")
        self.assertEqual(diff_paths(d), ["runs/probe-1/out.txt", "rh/ledger.py"])

    def test_plain_unified_diff_with_timestamps_and_crlf(self):
        d = "--- a/x.txt\t2026-01-01 00:00:00\r\n+++ b/y.txt\t2026-01-01 00:00:00\r\n@@ -1 +1 @@\r\n-a\r\n+b\r\n"
        self.assertEqual(diff_paths(d), ["x.txt", "y.txt"])

    def test_short_hunk_cannot_hide_the_next_file(self):
        d = ("diff --git a/runs/p/x b/runs/p/x\n--- a/runs/p/x\n+++ b/runs/p/x\n@@ -1,9 +1,9 @@\n-a\n+b\n"
             "\\ No newline at end of file\ndiff --git a/rh/x.py b/rh/x.py\n--- a/rh/x.py\n+++ b/rh/x.py\n")
        self.assertEqual(diff_paths(d), ["runs/p/x", "rh/x.py"])

    def test_header_quoting_forms(self):
        cases = {
            'diff --git "a/runs/p/t\\tx" b/runs/p/y\n': ["runs/p/t\tx", "runs/p/y"],
            'diff --git a/runs/p/y "b/runs/p/\\303\\251"\n': ["runs/p/y", "runs/p/é"],
            "diff --git a/x b/y b/x b/y\n": ["x b/y"],
            "diff --git a/one b/two\n": ["one", "two"],
            "diff --git a/x b/runs/p/y\nsimilarity index 100%\ncopy from x\ncopy to runs/p/y\n": ["x", "runs/p/y"],
            'diff --git a/o b/n\nrename from "o\\"q"\nrename to n\n': ["o", "n", 'o"q'],
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(diff_paths(text), want)

    def test_malformed_quoting_fails_closed(self):
        paths = diff_paths('diff --git "a/runs/p/x b/runs/p/x\n')
        self.assertTrue(paths)
        self.assertEqual(custody_violations(paths, ["runs/p/"]), paths)


class CustodyTest(unittest.TestCase):
    def test_prefixes_files_and_order(self):
        paths = ["runs/probe-1/a", "runs/probe-10/x", "docs/x.md", "docs/x.md.bak", "docs", "docs/sub/y",
                 "rh/ledger.py", "rh/ledger.py", "runs/probe-1/"]
        self.assertEqual(custody_violations(paths, ["runs/probe-1/", "docs/x.md"]),
                         ["runs/probe-10/x", "docs/x.md.bak", "docs", "docs/sub/y", "rh/ledger.py", "runs/probe-1/"])

    def test_unplain_paths_always_violate(self):
        bad = ["../etc/x", "/abs", "runs/p/../../rh/x", "runs/p/./x", "runs\\p\\x", "", "~/x", "runs//p/x",
               "runs/p/a\nb", "C:runs/p/x", None]
        self.assertEqual(custody_violations(bad, ["runs/p/"]), bad)

    def test_bad_allowed_entries_raise(self):
        for allowed in ([""], ["/"], ["../x/"], ["runs/../"], ["/runs/p/"], "runs/p/"):
            with self.subTest(allowed=allowed), self.assertRaises(ValueError):
                custody_violations(["runs/p/x"], allowed)
        with self.assertRaises(ValueError):
            custody_violations("runs/p/x", ["runs/p/"])


if __name__ == "__main__":
    unittest.main()


class NetworkAndStagingTest(unittest.TestCase):
    SHA = "e" * 40

    def test_network_is_fetch_only_unless_the_job_needs_net(self):
        closed = protocol.task_prompt("j1", self.SHA, "b", [])
        opened = protocol.task_prompt("j1", self.SHA, "b", [], network=True)
        self.assertIn("only command that may use the network", closed)
        self.assertNotIn("only command that may use the network", opened)
        self.assertIn("allowlist", opened)

    def test_worker_stages_exactly_its_write_scope(self):
        p = protocol.task_prompt("j1", self.SHA, "b", ["docs/x.md"])
        self.assertIn("git add -A -- runs/j1/ docs/x.md", p)
