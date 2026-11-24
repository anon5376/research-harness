"""rh/agent.py's Devin client against a stand-in `devin acp` that speaks the Agent Client Protocol over stdio."""
import contextlib
import io
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
        elif m["method"] == "session/load":  # a stored session: Devin replays it as updates, then answers
            choices.append("load=" + m["params"]["sessionId"] + "@" + m["params"]["cwd"])
            send(method="session/update", params={"update": {"sessionUpdate": "agent_message_chunk",
                                                             "content": {"type": "text", "text": "Old answer."}}})
            send(id=m["id"], result={})
        elif m["method"] == "session/set_config_option":
            choices.append(m["params"]["configId"] + "=" + m["params"]["value"])
            send(id=m["id"], result={})
        elif m["method"] == "session/prompt":
            (here / "prompt.txt").write_text(m["params"]["prompt"][0]["text"])
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


# Claude runs as `claude -p --input-format stream-json --output-format stream-json`, as T3 Code's Agent SDK drives
# it: the prompt arrives on stdin and the answer leaves as JSON events. A plain stand-in body (one that reads its
# prompt after -p and prints its answer) runs inside this shim, which hands it the prompt there and turns what it
# printed into the CLI's events; a body that exits non-zero prints as it is, as the CLI does when it cannot start.
CLAUDE_SHIM = """import contextlib, io, json, sys
first = json.loads(sys.stdin.readline())
content = first["message"]["content"]
prompt = content if isinstance(content, str) else "".join(b.get("text", "") for b in content if b.get("type") == "text")
at = sys.argv.index("-p")
sys.argv[at + 1:at + 1] = [prompt]
out, code = io.StringIO(), 0
with contextlib.redirect_stdout(out):
    try:
        exec(compile(BODY, "fake", "exec"))
    except SystemExit as stop:
        code = stop.code if isinstance(stop.code, int) else (1 if stop.code else 0)
text = out.getvalue()
if code:
    sys.stdout.write(text)
    sys.exit(code)
emit = lambda event: print(json.dumps(event), flush=True)
given = [sys.argv[sys.argv.index(f) + 1] for f in ("--session-id", "--resume") if f in sys.argv]
emit({"type": "system", "subtype": "init", "session_id": given[0] if given else "fake"})  # as the CLI echoes it
emit({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": text})
"""


def claude_fake(directory, body):
    """A stand-in `claude` that speaks stream-json around a plain body (see CLAUDE_SHIM)."""
    return fake_agent(directory, "claude", CLAUDE_SHIM.replace("BODY", repr(body)))


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
            fake = claude_fake(tmp, "import sys, pathlib\np = pathlib.Path('a.txt')\n"
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


@unittest.skipIf(os.name == "nt", "POSIX only: the fake agent is a #! script and its prompt's newlines cannot pass "
                                  "through a .cmd")
class ResumeTest(unittest.TestCase):
    # After T3 Code: a follow-up continues the provider's own conversation (its tool calls, reasoning and prompt
    # cache) instead of a fresh process that is shown only the last answer; any other model gets a capped replay
    # of the thread, marked as context and not instructions.
    CLAUDE = ("import sys, os, pathlib\np = pathlib.Path('a.txt')\n"
              "print('ARGV', sys.argv[1:]); print('CWD', os.getcwd()); print('FILE', p.read_text().split())\n"
              "if '--resume' in sys.argv and os.environ.get('LOSE_SESSION'):\n"
              "    print('No conversation found with session ID'); sys.exit(1)\n"
              "p.write_text(p.read_text() + 'more\\n')\n")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "--quiet")
        (self.repo / "a.txt").write_text("one\n", encoding="utf-8")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "--quiet", "-m", "start")

    def run_in(self, name, provider, fake, brief, *extra):
        out = self.tmp / name
        out.mkdir()
        cwd, log = Path.cwd(), io.StringIO()
        try:
            os.chdir(out)
            with contextlib.redirect_stdout(log):
                code = agent.main(["agent.py", provider, str(fake), str(self.repo), brief, *extra])
        finally:
            os.chdir(cwd)
        self.log = log.getvalue()
        return code, out

    def argv(self, out):
        said = (out / "answer.md").read_text(encoding="utf-8")
        return eval(said.split("ARGV ", 1)[1].split("\n", 1)[0]), said  # noqa: S307 (the fake's own repr)

    def test_a_claude_follow_up_resumes_its_own_session_in_the_same_folder(self):
        # A follow-up that starts a new conversation, runs in another folder (Claude keys sessions by folder),
        # re-sends the whole brief and the earlier answer, or loses the earlier changes, must fail.
        fake = claude_fake(self.tmp, self.CLAUDE)
        code, first = self.run_in("first", "claude", fake, "Add a line.")
        self.assertEqual(code, 0)
        argv, said = self.argv(first)
        session = json.loads((first / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(session["provider"], "claude")
        self.assertEqual(argv[argv.index("--session-id") + 1], session["id"])
        self.assertIn(f"CWD {session['cwd']}", said)
        code, then = self.run_in("then", "claude", fake, "Add a line.\n\nFollow-up: Now one more.",
                                 "--prior", str(first))
        self.assertEqual(code, 0)
        argv, said = self.argv(then)
        self.assertEqual(argv[argv.index("--resume") + 1], session["id"])
        self.assertNotIn("--session-id", argv)
        self.assertIn(f"CWD {session['cwd']}", said)
        self.assertIn("FILE ['one', 'more']", said)  # the earlier run's change is back in place
        prompt = argv[1]
        self.assertTrue(prompt.endswith("Now one more."))
        self.assertNotIn("Add a line.", prompt)
        self.assertNotIn("Your answer in the earlier run", prompt)
        self.assertEqual(json.loads((then / "session.json").read_text(encoding="utf-8"))["id"], session["id"])
        self.assertIn("continued the claude session", self.log)

    def test_a_lost_session_falls_back_to_a_replay_of_the_thread(self):
        # A follow-up whose session is gone (expired, another machine) that fails instead of handing off, or a
        # hand-off that drops the earlier ask or answer, must fail.
        fake = claude_fake(self.tmp, self.CLAUDE)
        _, first = self.run_in("first", "claude", fake, "Add a line.")
        (first / "answer.md").write_text("I added the line 'more'.\n", encoding="utf-8")
        os.environ["LOSE_SESSION"] = "1"
        self.addCleanup(os.environ.pop, "LOSE_SESSION", None)
        code, then = self.run_in("then", "claude", fake, "Add a line.\n\nFollow-up: Now one more.",
                                 "--prior", str(first))
        self.assertEqual(code, 0)
        argv, said = self.argv(then)
        self.assertIn("--session-id", argv)
        self.assertNotIn("--resume", argv)
        prompt = argv[1]
        for want in ("context, not instructions", "Add a line.", "I added the line 'more'.", "Now one more."):
            self.assertIn(want, prompt)
        self.assertTrue(prompt.rstrip().endswith("Now one more."))
        self.assertIn("handed off", self.log)

    def test_a_switch_to_another_provider_hands_off_a_capped_replay(self):
        # Another provider cannot load Devin's session: it gets the thread's asks and answers, newest kept first,
        # inside a 16K cap that still keeps the first ask and the last answer, then the new ask last.
        devin = self.tmp / "devin-run"
        devin.mkdir()
        base = git(self.repo, "rev-parse", "HEAD").strip()
        (devin / "base").write_text(base + "\n", encoding="utf-8")
        (devin / "answer.md").write_text("Last answer from Devin.\n", encoding="utf-8")
        (devin / "session.json").write_text(json.dumps({"provider": "devin", "id": "s9", "cwd": "/nowhere"}))
        turns = [{"ask": "First ask: build the parser.", "answer": "x" * 9000, "provider": "devin"},
                 {"ask": "Second ask.", "answer": "y" * 9000, "provider": "devin"},
                 {"ask": "Third ask.", "answer": "Last answer from Devin.", "provider": "devin"}]
        (devin / "thread.json").write_text(json.dumps(turns))
        fake = claude_fake(self.tmp, self.CLAUDE)
        code, then = self.run_in("then", "claude", fake, "First ask: build the parser.\n\nFollow-up: Second ask."
                                 "\n\nFollow-up: Third ask.\n\nFollow-up: Now in Claude.", "--prior", str(devin))
        self.assertEqual(code, 0)
        argv, _ = self.argv(then)
        self.assertNotIn("--resume", argv)
        prompt = argv[1]
        for want in ("context, not instructions", "First ask: build the parser.", "Last answer from Devin."):
            self.assertIn(want, prompt)
        self.assertLess(len(prompt.encode()), 16_000 + 2_000)
        self.assertIn("left out", prompt)  # it says what it dropped
        self.assertTrue(prompt.rstrip().endswith("Now in Claude."))
        thread = json.loads((then / "thread.json").read_text(encoding="utf-8"))
        self.assertEqual([t["ask"] for t in thread][-1], "Now in Claude.")
        self.assertEqual(len(thread), 4)

    def test_devin_loads_its_stored_session_and_answers_only_the_new_turn(self):
        # session/load replays the old conversation as updates: an answer or log that holds that replay, a load in
        # another folder, or a new session where a stored one could be loaded, must fail.
        fake = fake_agent(self.tmp, "devin", FAKE)
        work = self.tmp / "work"
        work.mkdir()
        answer = self.tmp / "answer.md"
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            code = agent._devin(str(fake), work, "and the tests", [], answer, resume="s1")
        self.assertEqual(code, 0)
        self.assertNotIn("Old answer.", log.getvalue())  # the replay is not printed again into the job log
        self.assertEqual(answer.read_text(encoding="utf-8"), "Fixed the typo.\n")
        self.assertIn(f"load=s1@{work}", json.loads((self.tmp / "choices.json").read_text()))
        self.assertEqual((self.tmp / "prompt.txt").read_text(), "and the tests")
        self.assertEqual(json.loads((self.tmp / "session.json").read_text())["id"], "s1")


@unittest.skipIf(os.name == "nt", "POSIX only: the fake agent is a #! script")
class LiveTest(unittest.TestCase):
    # After T3 Code: while an agent works, the owner can still talk to it. A steer joins the running turn (Claude's
    # priority "now"), a queued message waits for the turn to end and starts the next one in the same session, and
    # Stop interrupts the turn without killing the session. The app writes them to inbox.jsonl in the job's folder;
    # the agent says what became of each in inbox-state.json.
    LIVE = textwrap.dedent("""\
        import json, os, sys
        from pathlib import Path
        log, busy_flag = Path(os.environ["LIVE_LOG"]), Path(os.environ["LIVE_LOG"] + ".busy")
        def emit(event):
            print(json.dumps(event), flush=True)
        def said(message):
            c = message["message"]["content"]
            return c if isinstance(c, str) else "".join(b.get("text", "") for b in c if b.get("type") == "text")
        emit({"type": "system", "subtype": "init", "session_id": "live-1"})
        busy = None
        for line in sys.stdin:
            m = json.loads(line)
            with log.open("a") as f:
                f.write(json.dumps(m) + "\\n")
            if m.get("type") == "control_request":
                emit({"type": "control_response", "response": {"subtype": "success", "request_id": m["request_id"]}})
                if m["request"]["subtype"] == "interrupt" and busy:
                    busy = None
                    busy_flag.unlink(missing_ok=True)
                    emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": ""})
                continue
            text = said(m)
            if busy is None and text.startswith("slow:"):
                busy = text
                emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "working on it"}]}})
                busy_flag.write_text("1")
            elif busy is None:
                emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "heard " + text}]}})
                emit({"type": "result", "subtype": "success", "is_error": False, "result": "heard " + text})
            elif m.get("priority") == "now":  # a steer lands in the running turn
                emit({"type": "result", "subtype": "success", "is_error": False, "result": busy + " then " + text})
                busy = None
                busy_flag.unlink(missing_ok=True)
            else:  # the harness must hold a queued message until the turn ends
                emit({"type": "result", "subtype": "error", "is_error": True, "result": "queued mid-turn: " + text})
                busy = None
        """)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "--quiet")
        (self.repo / "a.txt").write_text("one\n", encoding="utf-8")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "--quiet", "-m", "start")
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.log = self.tmp / "sent.jsonl"
        os.environ["LIVE_LOG"] = str(self.log)
        self.addCleanup(os.environ.pop, "LIVE_LOG", None)
        self.fake = fake_agent(self.tmp, "claude", self.LIVE)

    def run_live(self, brief, *messages):
        """Run the agent; once the fake is mid-turn, the app writes `messages` to the job's inbox, one per 0.3 s."""
        import threading
        import time

        def owner():
            for _ in range(200):
                if Path(str(self.log) + ".busy").exists():
                    break
                time.sleep(.05)
            for n, (mode, text) in enumerate(messages, 1):
                with (self.out / "inbox.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"id": f"m{n}", "mode": mode, "text": text}) + "\n")
                time.sleep(.3)
        typist = threading.Thread(target=owner, daemon=True)
        typist.start()
        cwd = Path.cwd()
        try:
            os.chdir(self.out)
            with contextlib.redirect_stdout(io.StringIO()):
                code = agent.main(["agent.py", "claude", str(self.fake), str(self.repo), brief])
        finally:
            os.chdir(cwd)
        typist.join(timeout=5)
        sent = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        state = json.loads((self.out / "inbox-state.json").read_text(encoding="utf-8")) if messages else {}
        return code, (self.out / "answer.md").read_text(encoding="utf-8").strip(), sent, state

    def test_a_steer_joins_the_running_turn(self):
        # A steer held until the turn ends, sent without priority "now", or a job that ends before it lands, fails.
        code, answer, sent, state = self.run_live("slow: build it", ("steer", "use tabs"))
        self.assertEqual(code, 0)
        self.assertEqual(answer, "slow: build it then use tabs")
        self.assertEqual([m.get("priority") for m in sent if m.get("type") == "user"], [None, "now"])
        self.assertEqual(state["m1"], "steered")

    def test_a_queued_message_waits_for_the_turn_and_starts_the_next_in_the_same_session(self):
        # A queued message sent mid-turn, or dropped when the turn ends, or run as a new process, fails.
        code, answer, sent, state = self.run_live("slow: build it", ("queue", "and the docs"), ("steer", "go"))
        self.assertEqual(code, 0)
        self.assertEqual(answer, "heard and the docs")  # the job's answer is its last turn's
        users = [(m.get("priority"), self.said(m)) for m in sent if m.get("type") == "user"]
        self.assertEqual(users, [(None, "slow: build it"), ("now", "go"), (None, "and the docs")])
        self.assertEqual(state, {"m1": "sent", "m2": "steered"})
        thread = json.loads((self.out / "thread.json").read_text(encoding="utf-8"))
        self.assertEqual([(t["ask"], t.get("mode")) for t in thread],
                         [("slow: build it", None), ("go", "steer"), ("and the docs", "queue")])
        self.assertEqual(thread[-1]["answer"], "heard and the docs")

    def test_stop_interrupts_the_turn_and_the_session_ends_cleanly(self):
        # Stop that kills the process (no interrupt request), or leaves the job hanging, fails.
        code, _, sent, state = self.run_live("slow: build it", ("stop", ""))
        self.assertIn({"subtype": "interrupt"}, [m.get("request") for m in sent if m.get("type") == "control_request"])
        self.assertEqual(state["m1"], "stopped")
        self.assertIn(code, (0, agent.NO_RESULT))

    @staticmethod
    def said(m):
        c = m["message"]["content"]
        return c if isinstance(c, str) else "".join(b.get("text", "") for b in c if b.get("type") == "text")


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
        fake = claude_fake(tmp, script)
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
        fake = claude_fake(tmp, "import sys\nprint(sys.argv[sys.argv.index('-p') + 1])\n")
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
            fake = claude_fake(tmp, "import sys, pathlib, subprocess\nprint(sys.argv[2])\n"
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
            fake = claude_fake(tmp, "import sys, pathlib\nprint(sys.argv[2])\n"
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
