"""rh/agent.py's Devin client against a stand-in `devin acp` that speaks the Agent Client Protocol over stdio."""
import json
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
            send(id=m["id"], result={"sessionId": "s1", "configOptions": [
                 {"id": "model", "currentValue": "swe-2-max"}, {"id": "mode", "currentValue": "accept-edits"}]})
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


if __name__ == "__main__":
    unittest.main()
