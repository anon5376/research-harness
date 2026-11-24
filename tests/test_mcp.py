"""rh mcp: the MCP server ChatGPT reaches through OpenAI's Secure MCP Tunnel. Driven over stdio as tunnel-client
drives it: newline-delimited JSON-RPC on stdin and stdout."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from rh import chat, config, contract
from rh.ledger import Ledger
from rh.supervisor import Supervisor

ROOT = Path(__file__).resolve().parents[1]
POLICY = contract.load_policy(ROOT / "policy.toml")
BRIEF = "Review the ledger's recovery path.\nName every state it can lose.\n"


def git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)


class McpTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.home, self.repo = base / "home", base / "repo"
        self.home.mkdir()
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        (self.repo / "rh").mkdir()
        (self.repo / "rh" / "ledger.py").write_text("STATES = ('done', 'failed')\n", encoding="utf-8")
        (self.repo / "notes.md").write_text("é ünïcode stays\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        (self.repo / ".env").write_text("SECRET=1\n", encoding="utf-8")  # on disk, never tracked
        (self.home / "config.toml").write_text(f'[repo]\npath = "{self.repo.as_posix()}"\n', encoding="utf-8")
        cfg = config.load(self.home)
        led = Ledger(self.home / "ledger.db", clock=lambda: 1_000_000.0)
        led.add_job(contract.normalize({"id": "pro-review", "kind": "gpt-chat", "brief": BRIEF}))
        Supervisor(led, cfg, cloud=None, caps={}, policy=POLICY, clock=lambda: 1_000_000.0).tick()
        # added after the tick, so it stays a draft and nothing runs
        led.add_job(contract.normalize({"id": "a-shell-job", "kind": "local-shell", "brief": "List files.",
                                        "command": ["true"], "accept": "true"}))
        self.assertEqual(led.job("pro-review")["state"], "handoff")
        led.close()
        self.cfg = cfg

    def serve(self, *messages):
        """Send each message as one line, close stdin, and return the replies by id (notifications get none)."""
        lines = "".join(json.dumps(m) + "\n" for m in messages)
        out = subprocess.run([sys.executable, "-I", str(ROOT / "bin/rh"), "mcp"], input=lines,
                             env={**os.environ, "RH_HOME": str(self.home)}, capture_output=True, text=True,
                             encoding="utf-8", timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        replies = [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
        return {r.get("id"): r for r in replies}

    def call(self, name, **arguments):
        r = self.serve({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": name, "arguments": arguments}})[1]["result"]
        return r.get("isError", False), "".join(c["text"] for c in r["content"] if c["type"] == "text")

    def test_it_introduces_itself_and_lists_read_tools_and_one_write_tool(self):
        got = self.serve({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "tunnel-client", "version": "0"}}},
                         {"jsonrpc": "2.0", "method": "notifications/initialized"},
                         {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                         {"jsonrpc": "2.0", "id": 3, "method": "ping"},
                         {"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
        self.assertEqual(sorted(k for k in got), [1, 2, 3, 4])  # the notification got no reply
        init = got[1]["result"]
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        self.assertEqual(init["serverInfo"]["name"], "research-harness")
        self.assertIn("tools", init["capabilities"])
        self.assertLessEqual(len(init["instructions"].split("\n\n")[0]), 512)
        tools = {t["name"]: t for t in got[2]["result"]["tools"]}
        self.assertEqual(sorted(tools), ["get_job", "list_files", "list_jobs", "read_file", "submit_reply"])
        for name in ("get_job", "list_files", "list_jobs", "read_file"):
            self.assertIs(tools[name]["annotations"]["readOnlyHint"], True, name)
        # the one write: ChatGPT asks before it, and a plan without write tools leaves it out
        self.assertIs(tools["submit_reply"]["annotations"]["readOnlyHint"], False)
        for tool in tools.values():
            self.assertEqual(tool["inputSchema"]["type"], "object")
        self.assertEqual(got[3]["result"], {})
        self.assertEqual(got[4]["error"]["code"], -32601)

    def test_it_lists_the_chat_jobs_waiting_and_gives_a_brief_verbatim(self):
        error, text = self.call("list_jobs")
        self.assertFalse(error)
        self.assertIn("pro-review", text)
        self.assertIn("Review the ledger's recovery path.", text)
        self.assertNotIn("a-shell-job", text)
        error, text = self.call("get_job", job_id="pro-review")
        self.assertFalse(error)
        self.assertIn(BRIEF, text)
        self.assertIn("gpt-6.1-sol-pro", text)
        self.assertTrue(self.call("get_job", job_id="a-shell-job")[0])
        self.assertTrue(self.call("get_job", job_id="no-such-job")[0])

    def test_it_reads_only_tracked_files_inside_the_repository(self):
        error, text = self.call("list_files")
        self.assertFalse(error)
        self.assertEqual(text.split(), ["notes.md", "rh/ledger.py"])
        self.assertEqual(self.call("list_files", prefix="rh/")[1].split(), ["rh/ledger.py"])
        self.assertEqual(self.call("read_file", path="notes.md"), (False, "é ünïcode stays\n"))
        self.assertIn("STATES", self.call("read_file", path="rh/ledger.py")[1])
        for path in (".env", "../home/config.toml", "/etc/hosts", "rh/../.env", "missing.py"):
            error, text = self.call("read_file", path=path)
            self.assertTrue(error, path)
            self.assertNotIn("SECRET", text)

    def test_a_long_file_comes_in_pages(self):
        (self.repo / "big.txt").write_text("x" * 250_000, encoding="utf-8")
        git(self.repo, "add", "big.txt")
        error, first = self.call("read_file", path="big.txt")
        self.assertFalse(error)
        self.assertIn("offset=200000", first)
        error, rest = self.call("read_file", path="big.txt", offset=200_000)
        self.assertEqual(rest, "x" * 50_000)

    def test_a_submitted_reply_ends_the_job_done_once(self):
        error, text = self.call("submit_reply", job_id="pro-review", text="Two states can be lost: launching, stalled.")
        self.assertFalse(error, text)
        led = Ledger(self.home / "ledger.db")
        self.addCleanup(led.close)
        self.assertEqual(led.job("pro-review")["state"], "done")
        self.assertEqual(chat.read_reply(self.cfg, "pro-review"), "Two states can be lost: launching, stalled.")
        self.assertEqual(self.call("list_jobs"), (False, "No GPT Pro jobs are waiting for an answer."))
        error, text = self.call("submit_reply", job_id="pro-review", text="again")
        self.assertTrue(error)
        self.assertIn("done", text)
        self.assertTrue(self.call("submit_reply", job_id="pro-review", text="  ")[0])

    def test_a_line_that_is_not_json_gets_a_parse_error_and_the_server_carries_on(self):
        out = subprocess.run([sys.executable, "-I", str(ROOT / "bin/rh"), "mcp"],
                             input='not json\n{"jsonrpc": "2.0", "id": 7, "method": "ping"}\n',
                             env={**os.environ, "RH_HOME": str(self.home)}, capture_output=True, text=True,
                             encoding="utf-8", timeout=30)
        replies = [json.loads(line) for line in out.stdout.splitlines()]
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1], {"jsonrpc": "2.0", "id": 7, "result": {}})


class TunnelTest(unittest.TestCase):
    """rh mcp --tunnel builds tunnel-client's command; the key is only ever named by file, never read here."""
    TID = "tunnel_" + "0123456789abcdef" * 2

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        self.key = self.home / "secrets" / "openai-tunnel.key"
        self.key.parent.mkdir()
        self.key.write_text("sk-not-read\n", encoding="utf-8")
        self.key.chmod(0o600)
        self.bin = self.home / "tunnel-client"
        self.bin.write_text("", encoding="utf-8")
        self.bin.chmod(0o755)

    def cfg(self, **mcp):
        cfg = config.load(self.home)
        cfg["mcp"].update({"tunnel_id": self.TID, "tunnel_client": str(self.bin), **mcp})
        return cfg

    def test_the_command_runs_rh_mcp_over_stdio_with_the_key_by_reference(self):
        from rh import mcp
        argv, env = mcp.tunnel_command(self.cfg(), {"OPENAI_API_KEY": "sk-billable", "CONTROL_PLANE_API_KEY": "x",
                                                    "PATH": "/usr/bin"})
        self.assertEqual(argv[:2], [str(self.bin), "run"])
        flags = dict(zip(argv[2::2], argv[3::2]))
        self.assertEqual(flags["--control-plane.tunnel-id"], self.TID)
        self.assertEqual(flags["--control-plane.api-key"], f"file:{self.key}")
        self.assertEqual(flags["--mcp.command"], f"command={sys.executable} -I {ROOT / 'bin' / 'rh'} mcp")
        self.assertEqual(flags["--health.listen-addr"], "127.0.0.1:0")
        self.assertNotIn("sk-not-read", " ".join(argv))
        # no fallback to a key that can spend: tunnel-client reads OPENAI_API_KEY when it has no other key
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CONTROL_PLANE_API_KEY", env)
        self.assertEqual(env["RH_HOME"], str(self.home))
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_each_missing_owner_step_is_named(self):
        from rh import mcp
        with self.assertRaisesRegex(ValueError, "rh config set mcp.tunnel_id"):
            mcp.tunnel_command(self.cfg(tunnel_id=""), {})
        with self.assertRaisesRegex(ValueError, "rh config set mcp.tunnel_id"):
            mcp.tunnel_command(self.cfg(tunnel_id="tunnel_short"), {})
        with self.assertRaisesRegex(ValueError, "tunnel-client"):
            mcp.tunnel_command(self.cfg(tunnel_client=str(self.home / "missing")), {})
        self.key.unlink()
        with self.assertRaisesRegex(ValueError, "Tunnels: Read and Use"):
            mcp.tunnel_command(self.cfg(), {})

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_a_key_others_can_read_is_refused(self):
        from rh import mcp
        self.key.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "chmod 600"):
            mcp.tunnel_command(self.cfg(), {})


if __name__ == "__main__":
    unittest.main()
