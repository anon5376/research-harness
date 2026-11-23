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


if __name__ == "__main__":
    unittest.main()
