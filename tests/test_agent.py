"""rh/agent.py's Devin client against a stand-in `devin acp` that speaks the Agent Client Protocol over stdio."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from rh import agent, collect

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


def fake_agent(directory, name, script):
    """Write a stand-in agent as a `#!` script and return it as the binary to run. Windows cannot exec a `#!`
    script, so there the same script gets a .cmd launcher that runs it with this interpreter."""
    fake = directory / name
    fake.write_text(f"#!{sys.executable}\n" + script, encoding="utf-8")
    fake.chmod(0o755)
    if os.name != "nt":
        return fake
    launcher = directory / f"{name}.cmd"
    # cd first: _devin kills the .cmd, which orphans the interpreter; a cwd outside the test's temp dir
    # keeps the orphan from blocking TemporaryDirectory cleanup on Windows.
    launcher.write_text("@echo off\n@cd /d \"%TEMP%\"\n"
                        f'@"{sys.executable}" "%~dp0{name}" %*\n@exit /b %errorlevel%\n', encoding="utf-8")
    return launcher


class DevinAcpTest(unittest.TestCase):
    def test_devin_runs_over_acp_under_the_workspace_write_rule(self):
        # A client that answers a command with yes, an edit outside the copy with yes, an edit inside it with no,
        # ever picks plan_bypass, keeps text from before the last tool call in the answer, or reports a finished
        # turn as a failure, or leaves the session on a model other than the one asked for, must fail.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake = fake_agent(tmp, "devin", FAKE)
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
            fake = fake_agent(tmp, "devin", FAKE)
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
    @unittest.skipIf(os.name == "nt", "POSIX only: the fake agent is a #! script and its prompt's newlines "
                                          "cannot pass through a .cmd")
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
            fake = fake_agent(tmp, "claude", "import sys, pathlib\np = pathlib.Path('a.txt')\n"
                              "print(sys.argv[2]); print('FILE', p.read_text().split())\n"
                              "p.write_text(p.read_text() + 'three\\n')\n")
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


class NoResultTest(unittest.TestCase):
    """A run that ends cleanly having said nothing and changed nothing produced nothing: it must end as no_result,
    not done. Ten SWE-2 Max runs at once ended 3 of 7 this way (the turn stopped after a read), and each was done."""

    def run_agent(self, script):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        repo, out = tmp / "repo", tmp / "out"
        for d in (repo, out):
            d.mkdir()
        git(repo, "init", "--quiet")
        (repo / "a.txt").write_text("one\n", encoding="utf-8")
        git(repo, "add", "a.txt")
        git(repo, "commit", "--quiet", "-m", "start")
        fake = fake_agent(tmp, "claude", script)
        cwd = Path.cwd()
        try:
            os.chdir(out)
            return agent.main(["agent.py", "claude", str(fake), str(repo), "Find one bug."]), out
        finally:
            os.chdir(cwd)

    def test_a_run_that_says_and_changes_nothing_has_no_result(self):
        code, out = self.run_agent("pass\n")
        self.assertEqual(code, agent.NO_RESULT)
        self.assertFalse((out / "changes.patch").exists())

    def test_a_run_with_an_answer_or_a_change_has_a_result(self):
        self.assertEqual(self.run_agent("print('No bug found; I checked the parser.')\n")[0], 0)
        code, out = self.run_agent("import pathlib\npathlib.Path('a.txt').write_text('two\\n')\n")
        self.assertEqual(code, 0)
        self.assertTrue((out / "changes.patch").stat().st_size)

    def test_the_harness_ends_that_run_no_result(self):
        self.assertEqual(collect.NO_RESULT, agent.NO_RESULT)
        job = {"id": "lt-x", "spec": {"command": ["python3", "rh/agent.py"], "accept": None}}
        state, receipt = collect.collect_local(job, agent.NO_RESULT, Path("."))
        self.assertEqual(state, "no_result")
        self.assertIn("no answer and no changes", receipt["detail"])
        # a person's own command that happens to exit 66 has failed, as any other exit code would say
        shell = {"id": "mine", "spec": {"command": ["sh", "-c", "exit 66"], "accept": None}}
        self.assertEqual(collect.collect_local(shell, 66, Path("."))[0], "failed")


class RoleAndPlanTest(unittest.TestCase):
    """A hierarchy swarm gives each agent its role's system prompt and, under a planning lead, the lead's plan."""

    @unittest.skipIf(os.name == "nt", "POSIX only: the fake agent is a #! script")
    def test_the_role_leads_and_the_plan_follows_the_request(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        repo, out = tmp / "repo", tmp / "out"
        for d in (repo, out):
            d.mkdir()
        git(repo, "init", "--quiet")
        (repo / "a.txt").write_text("one\n", encoding="utf-8")
        git(repo, "add", "a.txt")
        git(repo, "commit", "--quiet", "-m", "start")
        plan = tmp / "plan.md"
        plan.write_text("Split it in two; check the parser first.\n", encoding="utf-8")
        fake = fake_agent(tmp, "claude", "import sys\nprint(sys.argv[sys.argv.index('-p') + 1])\n")
        cwd = Path.cwd()
        try:
            os.chdir(out)
            code = agent.main(["agent.py", "claude", str(fake), str(repo), "Fix the parser.",
                               "--role", "You are a worker agent.", "--context", str(plan)])
            said = (out / "answer.md").read_text(encoding="utf-8")
            self.assertEqual(code, 0)
            self.assertTrue(said.startswith("You are a worker agent.\n\n---\n\nFix the parser."), said)
            self.assertIn("lead's plan", said)
            self.assertIn("Split it in two; check the parser first.", said)
            # a plan that was never written leaves the brief as it is
            plan.unlink()
            agent.main(["agent.py", "claude", str(fake), str(repo), "Fix the parser.", "--context", str(plan)])
            self.assertEqual((out / "answer.md").read_text(encoding="utf-8").strip(), "Fix the parser.")
        finally:
            os.chdir(cwd)


class SwarmJudgeTest(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX only: the fake agent is a #! script and its prompt's newlines "
                                          "cannot pass through a .cmd")
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
            fake = fake_agent(tmp, "claude", "import sys, pathlib, subprocess\nprint(sys.argv[2])\n"
                              "for p in sorted(pathlib.Path('.swarm').rglob('*')): print('SEEN', p.as_posix())\n"
                              "subprocess.run(['git', 'apply', '.swarm/fix.w2/changes.patch'], check=True)\n")
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
    @unittest.skipIf(os.name == "nt", "POSIX only: the fake agent is a #! script and its prompt's newlines "
                                          "cannot pass through a .cmd")
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
            fake = fake_agent(tmp, "claude", "import sys, pathlib\nprint(sys.argv[2])\n"
                              "for p in sorted(pathlib.Path('.swarm').rglob('check.txt')): print('CHECK', p.parent.name, p.read_text().splitlines()[0])\n")
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

    def judge(self, script, check=True, moved=False):
        """Two runs at the same commit: w1 adds 'bad' (its check fails), w2 adds 'two' (passes). With moved, the
        repository gains a commit after the runs that only the check reads, so a verdict from the runs' commit no
        longer holds for the judge's."""
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
        for run, word in zip(runs, ("bad", "two")):  # each patch made as a run makes it, by git
            (run / "base").write_text(base + "\n", encoding="utf-8")
            (run / "answer.md").write_text(f"Added {word}.\n", encoding="utf-8")
            (repo / "a.txt").write_text(f"one\n{word}\n", encoding="utf-8")
            git(repo, "add", "a.txt")
            (run / "changes.patch").write_bytes(subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--binary"],
                                                               capture_output=True, check=True).stdout)
            git(repo, "reset", "--quiet", "--hard")
        if moved:
            (repo / "b.txt").write_text("bad\n", encoding="utf-8")
            git(repo, "add", "b.txt")
            git(repo, "commit", "--quiet", "-m", "moved on")
        log = tmp / "checks.log"
        command = (f"{sys.executable} -c \"import sys, pathlib; open('{log}', 'a').write('x'); "
                   "sys.exit(any('bad' in f.read_text() for f in map(pathlib.Path, ('a.txt', 'b.txt')) if f.exists()))\"")
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
        checks = len(log.read_text()) if log.exists() else 0
        return json.loads((out / "swarm-result.json").read_text(encoding="utf-8")), checks, code

    def test_a_combined_result_is_checked_and_its_failure_recorded(self):
        # the judge starts from w2, which passed, and adds a line of its own that fails: only checking the judge's
        # own tree can say so
        result, checks, code = self.judge("subprocess.run(['git', 'apply', '.swarm/fix.w2/changes.patch'], check=True)\n"
                                          "p = pathlib.Path('a.txt'); p.write_text(p.read_text() + 'bad\\n')")
        self.assertEqual(result, {"kept": "combined", "check": "failed: exit 1"})
        self.assertEqual(checks, 3)  # each run, then the judge's own result
        self.assertEqual(code, 1)  # kept work that fails the check ends the job failed, not done

    def test_a_run_kept_whole_on_a_newer_commit_is_checked_again(self):
        result, checks, code = self.judge("subprocess.run(['git', 'apply', '.swarm/fix.w2/changes.patch'], check=True)",
                                          moved=True)
        self.assertEqual(result, {"kept": "fix.w2", "check": "failed: exit 1"})
        self.assertEqual(checks, 3)
        self.assertEqual(code, 1)

    def test_a_judge_that_failed_keeps_its_exit_code_when_the_check_fails(self):
        # the judge keeps w1, whose check fails, and itself exits 3: the failed check would make a clean exit 1,
        # but a judge that already failed keeps its own code
        result, checks, code = self.judge("subprocess.run(['git', 'apply', '.swarm/fix.w1/changes.patch'], check=True)\n"
                                          "import sys; sys.exit(3)")
        self.assertEqual(result, {"kept": "fix.w1", "check": "failed: exit 1"})
        self.assertEqual(checks, 2)  # w1's verdict holds for its own tree, so the kept work is not checked again
        self.assertEqual(code, 3)

    def test_a_run_whose_commit_is_missing_fails_its_check(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        repo, run, scratch = tmp / "repo", tmp / "fix.w1", tmp / "scratch"
        for d in (repo, run, scratch):
            d.mkdir()
        git(repo, "init", "--quiet")
        (repo / "a.txt").write_text("one\n", encoding="utf-8")
        git(repo, "add", "a.txt")
        git(repo, "commit", "--quiet", "-m", "start")
        (run / "base").write_text("0" * 40 + "\n", encoding="utf-8")
        (run / "changes.patch").write_text("diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
                                           "@@ -1 +1,2 @@\n one\n+two\n", encoding="utf-8")
        verdict, _ = agent._check(repo, run, f"{sys.executable} -c pass", scratch)
        self.assertTrue(verdict.startswith("failed: "), verdict)
        self.assertIn("commit", verdict)

    def test_a_run_kept_whole_reuses_its_verdict(self):
        result, checks, code = self.judge("subprocess.run(['git', 'apply', '.swarm/fix.w2/changes.patch'], check=True)")
        self.assertEqual(result, {"kept": "fix.w2", "check": "passed"})
        self.assertEqual(checks, 2)
        self.assertEqual(code, 0)

    def test_with_no_check_the_result_names_only_the_kept_run(self):
        result, checks, code = self.judge("subprocess.run(['git', 'apply', '.swarm/fix.w2/changes.patch'], check=True)",
                                          check=False)
        self.assertEqual(result, {"kept": "fix.w2"})
        self.assertEqual(checks, 0)
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
