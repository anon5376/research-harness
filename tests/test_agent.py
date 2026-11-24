"""rh/agent.py's Devin client against a stand-in `devin acp` that speaks the Agent Client Protocol over stdio."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from rh import agent

# The stand-in answers initialize and session/new, then, for the prompt: says something, starts a tool call, asks to
# run a command, to edit a file inside the folder and one outside it, says its answer, and ends the turn. It writes
# what the client chose for each request to choices.json beside itself.
FAKE = textwrap.dedent('''
    import json, sys
    from pathlib import Path
    here = Path(__file__).parent
    choices, pending = [], {}
    def send(**m):
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", **m}) + "\\n"); sys.stdout.flush()
    def ask(rid, kind, path):
        send(id=rid, method="session/request_permission", params={"sessionId": "s1",
             "toolCall": {"kind": kind, "title": kind + " " + path, "locations": [{"path": path}] if path else []},
             "options": [{"optionId": "plan_bypass", "kind": "allow_once"}, {"optionId": "yes", "kind": "allow_once"},
                         {"optionId": "no", "kind": "reject_once"}]})
    for line in sys.stdin:
        m = json.loads(line)
        if "method" not in m:
            choices.append(m["result"]["outcome"].get("optionId"))
            continue
        if m["method"] == "initialize":
            send(id=m["id"], result={"protocolVersion": 1})
        elif m["method"] == "session/new":
            assert m["params"]["mcpServers"] == []
            menu = here / "menu.json"  # a real session's model menu, when the test supplies one
            send(id=m["id"], result={"sessionId": "s1", "configOptions": json.loads(menu.read_text()) if menu.exists()
                 else [{"id": "model", "currentValue": "swe-2-max"}, {"id": "mode", "currentValue": "accept-edits"}]})
        elif m["method"] == "session/set_config_option":
            choices.append(m["params"]["configId"] + "=" + m["params"]["value"])
            send(id=m["id"], result={})
        elif m["method"] == "session/prompt":
            send(method="session/update", params={"update": {"sessionUpdate": "agent_message_chunk",
                                                             "content": {"type": "text", "text": "Looking."}}})
            send(method="session/update", params={"update": {"sessionUpdate": "tool_call", "title": "Read README"}})
            ask(101, "execute", "")
            ask(102, "edit", "README.md")
            ask(103, "edit", "/etc/hosts")
            for _ in range(3):
                choices.append(json.loads(sys.stdin.readline())["result"]["outcome"].get("optionId"))
            for part in ("Fixed ", "the typo."):
                send(method="session/update", params={"update": {"sessionUpdate": "agent_message_chunk",
                                                                 "content": {"type": "text", "text": part}}})
            (here / "choices.json").write_text(json.dumps(choices))
            send(id=m["id"], result={"stopReason": "end_turn"})
''')


class DevinAcpTest(unittest.TestCase):
    def test_devin_runs_over_acp_under_the_workspace_write_rule(self):
        # A client that answers a command with yes, an edit outside the copy with yes, an edit inside it with no,
        # ever picks plan_bypass, keeps text from before the last tool call in the answer, or reports a finished
        # turn as a failure, or leaves the session on a model other than the one asked for, must fail.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake = tmp / "devin"
            fake.write_text(f"#!{sys.executable}\n" + FAKE, encoding="utf-8")
            fake.chmod(0o755)
            work = tmp / "work"
            work.mkdir()
            answer = tmp / "answer.md"
            code = agent._devin(str(fake), work, "fix the typo", ["--model", "swe-2-high"], answer)
            self.assertEqual(code, 0)
            self.assertEqual(answer.read_text(encoding="utf-8"), "Fixed the typo.\n")
            self.assertEqual(json.loads((tmp / "choices.json").read_text()), ["model=swe-2-high", "no", "yes", "no"])

    def test_a_model_with_its_thinking_level_is_not_switched_by_a_name_the_menu_lacks(self):
        # `devin acp --model swe-2-max` opens on the SWE-2 family at Max thinking: the session's model menu lists
        # families (swe-2-high) and thinking is its own option. Asking the menu for "swe-2-max" by name is refused
        # ("Invalid value") and failed every SWE-2 Max and Flash Max run; the start-up flag already chose it.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake = tmp / "devin"
            fake.write_text(f"#!{sys.executable}\n" + FAKE, encoding="utf-8")
            fake.chmod(0o755)
            (tmp / "menu.json").write_text(json.dumps([
                {"id": "model", "currentValue": "swe-2-high",
                 "options": [{"value": "swe-2-high"}, {"value": "swe-2-flash-medium"}]},
                {"id": "thought_level", "currentValue": "max", "options": [{"value": "high"}, {"value": "max"}]}]))
            work = tmp / "work"
            work.mkdir()
            answer = tmp / "answer.md"
            code = agent._devin(str(fake), work, "fix the typo", ["--model", "swe-2-max"], answer)
            self.assertEqual(code, 0)
            self.assertNotIn("model=swe-2-max", json.loads((tmp / "choices.json").read_text()))


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@localhost", *args],
                          capture_output=True, text=True, encoding="utf-8", check=True).stdout


class FollowUpTest(unittest.TestCase):
    def test_a_follow_up_starts_from_the_last_runs_changes_and_reads_its_answer(self):
        # A follow-up on a clean copy, one that does not see the earlier answer, or whose changes.patch drops the
        # earlier run's changes, must fail.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            repo, prior, out = tmp / "repo", tmp / "prior", tmp / "out"
            for d in (repo, prior, out):
                d.mkdir()
            git(repo, "init", "--quiet")
            (repo / "a.txt").write_text("one\n", encoding="utf-8")
            git(repo, "add", "a.txt")
            git(repo, "commit", "--quiet", "-m", "start")
            base = git(repo, "rev-parse", "HEAD").strip()
            (prior / "base").write_text(base + "\n", encoding="utf-8")
            (prior / "answer.md").write_text("Added line two.\n", encoding="utf-8")
            (prior / "changes.patch").write_text(
                "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1,2 @@\n one\n+two\n", encoding="utf-8")
            git(repo, "commit", "--quiet", "--allow-empty", "-m", "moved on")  # the repo moved since that run
            # A stand-in claude: prints the prompt it got and the file as it found it, then adds line three.
            fake = tmp / "claude"
            fake.write_text(f"#!{sys.executable}\nimport sys, pathlib\np = pathlib.Path('a.txt')\n"
                            "print(sys.argv[2]); print('FILE', p.read_text().split())\n"
                            "p.write_text(p.read_text() + 'three\\n')\n", encoding="utf-8")
            fake.chmod(0o755)
            cwd = Path.cwd()
            try:
                os.chdir(out)
                code = agent.main(["agent.py", "claude", str(fake), str(repo), "Now add line three.",
                                   "--prior", str(prior)])
            finally:
                os.chdir(cwd)
            self.assertEqual(code, 0)
            said = (out / "answer.md").read_text(encoding="utf-8")
            self.assertIn("Added line two.", said)
            self.assertIn("Now add line three.", said)
            self.assertIn("FILE ['one', 'two']", said)
            self.assertNotIn("--prior", said)
            self.assertEqual((out / "base").read_text(encoding="utf-8").strip(), base)
            patch = (out / "changes.patch").read_text(encoding="utf-8")
            self.assertIn("+two", patch)
            self.assertIn("+three", patch)


class SwarmJudgeTest(unittest.TestCase):
    def test_a_judge_reads_every_runs_answer_and_patch_and_keeps_only_what_it_chose(self):
        # A judge that cannot see the runs' work, or whose changes.patch carries the runs' folder, must fail.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            repo, out = tmp / "repo", tmp / "out"
            runs = [tmp / "fix.w1", tmp / "fix.w2"]
            for d in (repo, out, *runs):
                d.mkdir()
            git(repo, "init", "--quiet")
            (repo / "a.txt").write_text("one\n", encoding="utf-8")
            git(repo, "add", "a.txt")
            git(repo, "commit", "--quiet", "-m", "start")
            for run, word in zip(runs, ("deux", "two")):
                (run / "answer.md").write_text(f"Added {word}.\n", encoding="utf-8")
                # w2 was made when the line sat elsewhere: its hunk header differs, its change does not
                where = "-1 +1,2" if word == "deux" else "-3 +3,2"
                (run / "changes.patch").write_text("diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
                                                   f"@@ {where} @@\n one\n+{word}\n", encoding="utf-8")
            # A stand-in claude: lists what it was shown, then applies the second run's patch.
            fake = tmp / "claude"
            fake.write_text(f"#!{sys.executable}\nimport sys, pathlib, subprocess\nprint(sys.argv[2])\n"
                            "for p in sorted(pathlib.Path('.swarm').rglob('*')): print('SEEN', p.as_posix())\n"
                            "subprocess.run(['git', 'apply', '.swarm/fix.w2/changes.patch'], check=True)\n",
                            encoding="utf-8")
            fake.chmod(0o755)
            cwd = Path.cwd()
            try:
                os.chdir(out)
                code = agent.main(["agent.py", "claude", str(fake), str(repo), "Add a second line.",
                                   "--swarm", str(runs[0]), "--swarm", str(runs[1])])
            finally:
                os.chdir(cwd)
            self.assertEqual(code, 0)
            said = (out / "answer.md").read_text(encoding="utf-8")
            self.assertIn("Add a second line.", said)
            for seen in ("fix.w1/answer.md", "fix.w1/changes.patch", "fix.w2/answer.md", "fix.w2/changes.patch"):
                self.assertIn("SEEN .swarm/" + seen, said)
            self.assertNotIn("--swarm", said)
            self.assertIn("one-line title of the change itself", said)  # the swarm is listed by that line
            patch = (out / "changes.patch").read_text(encoding="utf-8")
            self.assertIn("+two", patch)
            self.assertNotIn("deux", patch)
            self.assertNotIn(".swarm", patch)
            # the harness, not the judge's prose, records which run's work was kept whole
            self.assertEqual(json.loads((out / "swarm-result.json").read_text(encoding="utf-8")), {"kept": "fix.w2"})


class SwarmCheckTest(unittest.TestCase):
    def test_the_judge_is_told_which_runs_pass_the_check_on_a_clean_copy(self):
        # Agents may not run commands, so the harness checks each run's patch itself: a judge brief that does not
        # say which run passed, or a check run on the repository instead of the run's patched copy, must fail.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            repo, out = tmp / "repo", tmp / "out"
            runs = [tmp / "fix.w1", tmp / "fix.w2", tmp / "fix.w3"]
            for d in (repo, out, *runs):
                d.mkdir()
            git(repo, "init", "--quiet")
            (repo / "a.txt").write_text("one\n", encoding="utf-8")
            git(repo, "add", "a.txt")
            git(repo, "commit", "--quiet", "-m", "start")
            base = git(repo, "rev-parse", "HEAD").strip()
            for run, word in zip(runs[:2], ("bad", "two")):
                (run / "base").write_text(base + "\n", encoding="utf-8")
                (run / "answer.md").write_text(f"Added {word}.\n", encoding="utf-8")
                (run / "changes.patch").write_text("diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
                                                   f"@@ -1 +1,2 @@\n one\n+{word}\n", encoding="utf-8")
            (runs[2] / "answer.md").write_text("I changed nothing.\n", encoding="utf-8")  # no patch: not checked
            check = f"{sys.executable} -c \"import sys; sys.exit('bad' in open('a.txt').read())\""
            fake = tmp / "claude"
            fake.write_text(f"#!{sys.executable}\nimport sys, pathlib\nprint(sys.argv[2])\n"
                            "for p in sorted(pathlib.Path('.swarm').rglob('check.txt')): print('CHECK', p.parent.name, p.read_text().splitlines()[0])\n",
                            encoding="utf-8")
            fake.chmod(0o755)
            cwd = Path.cwd()
            try:
                os.chdir(out)
                code = agent.main(["agent.py", "claude", str(fake), str(repo), "Add a second line.",
                                   *[a for r in runs for a in ("--swarm", str(r))], "--check", check])
            finally:
                os.chdir(cwd)
            self.assertEqual(code, 0)
            said = (out / "answer.md").read_text(encoding="utf-8")
            self.assertIn("- .swarm/fix.w1/ (its check failed: exit 1)", said)
            self.assertIn("- .swarm/fix.w2/ (its check passed)", said)
            self.assertIn("- .swarm/fix.w3/ (no changes to check)", said)
            self.assertIn("CHECK fix.w1 failed: exit 1", said)
            self.assertIn("CHECK fix.w2 passed", said)
            self.assertEqual((repo / "a.txt").read_text(encoding="utf-8"), "one\n")  # the repository is never written
            self.assertNotIn("--check", said)
            self.assertEqual(json.loads((out / "swarm-checks.json").read_text(encoding="utf-8")),
                             {"fix.w1": "failed: exit 1", "fix.w2": "passed", "fix.w3": "no changes"})


@unittest.skipIf(os.name == "nt", "POSIX only: the fake judge is a #! script")
class SwarmResultCheckTest(unittest.TestCase):
    """The judge may combine runs, so the work it keeps is checked too: a swarm whose result was never checked,
    or one that checks a run kept whole a second time, must fail."""

    def judge(self, script, check=True):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        repo, out = tmp / "repo", tmp / "out"
        runs = [tmp / "fix.w1", tmp / "fix.w2"]
        for d in (repo, out, *runs):
            d.mkdir()
        git(repo, "init", "--quiet")
        (repo / "a.txt").write_text("one\n", encoding="utf-8")
        git(repo, "add", "a.txt")
        git(repo, "commit", "--quiet", "-m", "start")
        base = git(repo, "rev-parse", "HEAD").strip()
        for run, word in zip(runs, ("bad", "two")):
            (run / "base").write_text(base + "\n", encoding="utf-8")
            (run / "answer.md").write_text(f"Added {word}.\n", encoding="utf-8")
            (run / "changes.patch").write_text("diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
                                               f"@@ -1 +1,2 @@\n one\n+{word}\n", encoding="utf-8")
        log = tmp / "checks.log"
        command = (f"{sys.executable} -c \"import sys, pathlib; open('{log}', 'a').write('x'); "
                   "sys.exit('bad' in open('a.txt').read())\"")
        fake = tmp / "claude"
        fake.write_text(f"#!{sys.executable}\nimport pathlib, subprocess\n{script}\nprint('Done.')\n",
                        encoding="utf-8")
        fake.chmod(0o755)
        cwd = Path.cwd()
        try:
            os.chdir(out)
            code = agent.main(["agent.py", "claude", str(fake), str(repo), "Add a second line.",
                               *[a for r in runs for a in ("--swarm", str(r))], *(["--check", command] if check else [])])
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0)
        checks = len(log.read_text()) if log.exists() else 0
        return json.loads((out / "swarm-result.json").read_text(encoding="utf-8")), checks

    def test_a_combined_result_is_checked_and_its_failure_recorded(self):
        result, checks = self.judge("subprocess.run(['git', 'apply', '.swarm/fix.w1/changes.patch'], check=True)\n"
                                    "p = pathlib.Path('a.txt'); p.write_text(p.read_text() + 'three\\n')")
        self.assertEqual(result, {"kept": "combined", "check": "failed: exit 1"})
        self.assertEqual(checks, 3)  # each run, then the judge's own result

    def test_a_run_kept_whole_reuses_its_verdict(self):
        result, checks = self.judge("subprocess.run(['git', 'apply', '.swarm/fix.w2/changes.patch'], check=True)")
        self.assertEqual(result, {"kept": "fix.w2", "check": "passed"})
        self.assertEqual(checks, 2)

    def test_with_no_check_the_result_names_only_the_kept_run(self):
        result, checks = self.judge("subprocess.run(['git', 'apply', '.swarm/fix.w2/changes.patch'], check=True)",
                                    check=False)
        self.assertEqual(result, {"kept": "fix.w2"})
        self.assertEqual(checks, 0)


if __name__ == "__main__":
    unittest.main()
