"""rh mcp: the MCP server ChatGPT reaches through OpenAI's Secure MCP Tunnel. Driven over stdio as tunnel-client
drives it: newline-delimited JSON-RPC on stdin and stdout."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from rh import chat, config, contract
from rh.ledger import Ledger
from rh.supervisor import Supervisor

ROOT = Path(__file__).resolve().parents[1]
POLICY = contract.load_policy(ROOT / "policy.toml")
BRIEF = "Review the ledger's recovery path.\nName every state it can lose.\n"


def git(repo, *a):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "-c", "commit.gpgsign=false", *a], check=True, capture_output=True)


def tunnel_argv(raw):
    """tunnel-client's own split of --mcp.command (parseCommandArgv, pkg/runtimeconfig/config.go, v0.0.21)."""
    args, word, quote, escaped, started = [], [], None, False, False
    for ch in raw.strip():
        if escaped:
            word.append(ch)
            escaped = False
        elif quote == "'":
            if ch == "'":
                quote = None
            else:
                word.append(ch)
        elif quote == '"':
            if ch == "\\":
                escaped = True
            elif ch == '"':
                quote = None
            else:
                word.append(ch)
        elif ch == "\\":
            escaped = True
        elif ch in "'\"":
            quote = ch
        elif ch in " \t\n\r":
            if word:
                args.append("".join(word))
                word = []
        else:
            word.append(ch)
    assert not escaped and quote is None, raw
    return args + (["".join(word)] if word else [])


def flags_of(argv):
    """tunnel-client run's flags as a dict: '--name value' and '--name=value' both."""
    flags, rest = {}, list(argv)
    while rest:
        name, eq, value = rest.pop(0).partition("=")
        flags[name] = value if eq else rest.pop(0)
    return flags


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
        git(self.repo, "commit", "-q", "-m", "first")
        (self.repo / ".env").write_text("SECRET=1\n", encoding="utf-8")  # on disk, never tracked
        (self.home / "config.toml").write_text(f'[repo]\npath = "{self.repo.as_posix()}"\n', encoding="utf-8")
        cfg = config.load(self.home)
        led = Ledger(self.home / "ledger.db", clock=lambda: 1_000_000.0)
        led.add_job(contract.normalize({"id": "pro-review", "kind": "gpt-chat", "brief": BRIEF}))
        Supervisor(led, cfg, cloud=None, caps={}, policy=POLICY, clock=lambda: 1_000_000.0).tick()
        # added after the tick, so it stays a draft and nothing runs
        led.add_job(contract.normalize({"id": "a-shell-job", "kind": "local-shell", "brief": "List files.",
                                        "command": ["true"], "accept": "true"}))
        led.add_job(contract.normalize({"id": "pro-draft", "kind": "gpt-chat", "brief": "Not handed out yet."}))
        self.assertEqual(led.job("pro-review")["state"], "handoff")
        led.close()
        self.cfg = cfg

    def raw(self, data: bytes):
        """Bytes in on stdin, the replies out in order."""
        out = subprocess.run([sys.executable, "-I", str(ROOT / "bin/rh"), "mcp"], input=data,
                             env={**os.environ, "RH_HOME": str(self.home)}, capture_output=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return [json.loads(line) for line in out.stdout.decode("utf-8").splitlines() if line.strip()]

    def serve(self, *messages):
        """Send each message as one line, close stdin, and return the replies by id (notifications get none)."""
        replies = self.raw("".join(json.dumps(m) + "\n" for m in messages).encode("utf-8"))
        return {r.get("id"): r for r in replies}

    def state(self, job_id):
        led = Ledger(self.home / "ledger.db")
        try:
            return led.job(job_id)["state"]
        finally:
            led.close()

    def call(self, name, arguments=None, **kw):
        r = self.serve({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": name, "arguments": {**(arguments or {}), **kw}}})[1]["result"]
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

    def test_it_agrees_on_a_protocol_version_it_speaks(self):
        def agreed(version):
            return self.serve({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": version, "capabilities": {}}})[1]["result"]["protocolVersion"]
        self.assertEqual(agreed("2025-11-25"), "2025-11-25")
        self.assertEqual(agreed("2025-06-18"), "2025-06-18")
        # 2025-03-26 requires JSON-RPC batches, which this server does not take; offer the newest instead
        self.assertEqual(agreed("2025-03-26"), "2025-11-25")
        self.assertEqual(agreed("1999-01-01"), "2025-11-25")

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
        self.assertNotIn("pro-draft", text)
        for other in ("a-shell-job", "no-such-job", "pro-draft"):  # only a job waiting for an answer can be read
            error, text = self.call("get_job", job_id=other)
            self.assertTrue(error, other)
            self.assertNotIn("Not handed out yet", text)

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

    def test_files_are_read_as_committed_never_through_the_working_tree(self):
        (self.repo / "notes.md").write_text("edited, not committed\n", encoding="utf-8")
        (self.repo / "staged.py").write_text("x = 1\n", encoding="utf-8")
        git(self.repo, "add", "staged.py")
        self.assertEqual(self.call("read_file", path="notes.md"), (False, "é ünïcode stays\n"))
        self.assertNotIn("staged.py", self.call("list_files")[1])
        self.assertTrue(self.call("read_file", path="staged.py")[0])

    @unittest.skipIf(os.name == "nt", "a committed symlink needs core.symlinks")
    def test_a_committed_symlink_is_not_followed(self):
        outside = self.home / "secret.txt"
        outside.write_text("SECRET outside the repository\n", encoding="utf-8")
        os.symlink(outside, self.repo / "link.txt")
        os.symlink(self.home, self.repo / "linkdir")
        git(self.repo, "add", "link.txt", "linkdir")
        git(self.repo, "commit", "-q", "-m", "links")
        self.assertNotIn("link", self.call("list_files")[1])
        for path in ("link.txt", "linkdir/secret.txt", "linkdir/config.toml"):
            error, text = self.call("read_file", path=path)
            self.assertTrue(error, path)
            self.assertNotIn("SECRET", text)

    def test_a_file_that_is_not_utf8_text_is_refused(self):
        (self.repo / "blob.bin").write_bytes(b"\xff\xfe\x00binary")
        git(self.repo, "add", "blob.bin")
        git(self.repo, "commit", "-q", "-m", "binary")
        error, text = self.call("read_file", path="blob.bin")
        self.assertTrue(error)
        self.assertIn("not UTF-8", text)

    def test_a_long_file_comes_in_pages_that_join_back_to_it(self):
        body = ("é" * 99 + "\n") * 2_500  # 250,000 characters, not ASCII, so pages count characters
        (self.repo / "big.txt").write_text(body, encoding="utf-8")
        git(self.repo, "add", "big.txt")
        git(self.repo, "commit", "-q", "-m", "big")
        error, first = self.call("read_file", path="big.txt")
        self.assertFalse(error)
        page, _, trailer = first.partition("\n\n[")
        self.assertIn("offset=200000", trailer)
        error, rest = self.call("read_file", path="big.txt", offset=200_000)
        self.assertFalse(error)
        self.assertEqual(page + rest, body)
        self.assertTrue(self.call("read_file", path="big.txt", offset=250_001)[0])

    def test_arguments_must_match_the_schema(self):
        bad = [("read_file", {"path": None}), ("read_file", {"path": "notes.md", "offset": True}),
               ("read_file", {"path": "notes.md", "offset": "5"}), ("read_file", {"path": "notes.md", "offset": -1}),
               ("read_file", {"path": "notes.md", "offset": 1.5}), ("read_file", {"path": "notes.md", "x": 1}),
               ("list_files", {"prefix": 3}), ("get_job", {"job_id": ["pro-review"]}), ("get_job", {}),
               ("submit_reply", {"job_id": "pro-review", "text": 5}),
               ("submit_reply", {"job_id": "pro-review", "text": ""})]
        for name, arguments in bad:
            self.assertTrue(self.call(name, arguments)[0], (name, arguments))
        self.assertEqual(self.state("pro-review"), "handoff")
        r = self.serve({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "list_jobs", "arguments": None}})[1]["result"]
        self.assertFalse(r["isError"])

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
        self.assertTrue(self.call("get_job", job_id="pro-review")[0])  # answered, so no longer offered

    def test_an_empty_reply_is_refused_and_the_job_keeps_waiting(self):
        self.assertTrue(self.call("submit_reply", job_id="pro-review", text="  \n")[0])
        self.assertEqual(self.state("pro-review"), "handoff")
        self.assertIsNone(chat.read_reply(self.cfg, "pro-review"))

    def test_a_line_that_is_not_json_gets_a_parse_error_and_the_server_carries_on(self):
        ping = b'{"jsonrpc": "2.0", "id": 7, "method": "ping"}\n'
        reply = self.raw(b"not json\n" + ping)
        self.assertEqual(reply[0]["error"]["code"], -32700)
        self.assertEqual(reply[1], {"jsonrpc": "2.0", "id": 7, "result": {}})
        # a reply must arrive as written, so bytes that are not UTF-8 are refused, not patched over
        submit = {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                  "params": {"name": "submit_reply", "arguments": {"job_id": "pro-review", "text": "AB"}}}
        mangled = json.dumps(submit).encode("utf-8").replace(b"AB", b"A\xffB") + b"\n"
        reply = self.raw(mangled + ping)
        self.assertEqual(reply[0]["error"]["code"], -32700)
        self.assertEqual(reply[1]["id"], 7)
        self.assertEqual(self.state("pro-review"), "handoff")


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
        inherited = {"OPENAI_API_KEY": "sk-billable", "CONTROL_PLANE_API_KEY": "x",
                     "CONTROL_PLANE_URL_PATH": "/elsewhere", "MCP_SERVER_URL": "http://elsewhere",
                     "LOG_HTTP_RAW_UNSAFE": "true", "HARPOON_TARGETS": "x", "TUNNEL_CLIENT_HTTP_PROXY": "x",
                     "ALLOW_REMOTE_UI": "true", "CLOUDFLARED_TUNNEL_TOKEN": "x", "PATH": "/usr/bin", "LOGNAME": "me"}
        argv, env = mcp.tunnel_command(self.cfg(), inherited)
        self.assertEqual(argv[:2], [str(self.bin), "run"])
        flags = flags_of(argv[2:])
        self.assertEqual(flags["--control-plane.base-url"], "https://api.openai.com")
        self.assertEqual(flags["--control-plane.tunnel-id"], self.TID)
        self.assertEqual(flags["--control-plane.api-key"], f"file:{self.key}")
        command = flags["--mcp.command"].removeprefix("command=")
        self.assertEqual(tunnel_argv(command), [sys.executable, "-I", str(ROOT / "bin" / "rh"), "mcp"])
        self.assertEqual(flags["--health.listen-addr"], "127.0.0.1:0")
        self.assertEqual(flags["--log.file"], str(self.home / "tunnel.log"))
        self.assertEqual(flags["--log.http-raw-unsafe"], "false")
        self.assertEqual(flags["--pid.file"], str(self.home / "tunnel.pid"))
        self.assertNotIn("sk-not-read", " ".join(argv))
        # only this harness's own settings apply: no inherited key, endpoint or logging switch
        self.assertEqual(env, {"PATH": "/usr/bin", "LOGNAME": "me", "RH_HOME": str(self.home)})

    def test_a_path_with_spaces_or_quotes_reaches_tunnel_client_intact(self):
        from rh import mcp
        odd = self.home / "it's a \"dir\", with \\ and $HOME"
        with patch.object(mcp, "ROOT", odd), patch.object(mcp.sys, "executable", str(odd / "py thon")):
            argv, _ = mcp.tunnel_command(self.cfg(), {})
        command = flags_of(argv[2:])["--mcp.command"].removeprefix("command=")
        self.assertEqual(tunnel_argv(command), [str(odd / "py thon"), "-I", str(odd / "bin" / "rh"), "mcp"])
        with patch.object(mcp, "ROOT", Path("/x,channel=y")), self.assertRaisesRegex(ValueError, "cannot take"):
            mcp.tunnel_command(self.cfg(), {})

    def test_each_missing_owner_step_is_named(self):
        from rh import mcp
        with self.assertRaisesRegex(ValueError, "rh config set mcp.tunnel_id"):
            mcp.tunnel_command(self.cfg(tunnel_id=""), {})
        for bad in ("tunnel_short", self.TID + "/x", self.TID + "\n", "tunnel_" + "A" * 32, "x" + self.TID):
            with self.assertRaisesRegex(ValueError, "rh config set mcp.tunnel_id"):
                mcp.tunnel_command(self.cfg(tunnel_id=bad), {})
        with self.assertRaisesRegex(ValueError, "tunnel-client"):
            mcp.tunnel_command(self.cfg(tunnel_client=str(self.home / "missing")), {})
        self.key.unlink()
        with self.assertRaisesRegex(ValueError, "Tunnels: Read and Use"):
            mcp.tunnel_command(self.cfg(), {})

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_a_key_others_can_read_is_refused(self):
        from rh import mcp
        for mode in (0o644, 0o640, 0o604):
            self.key.chmod(mode)
            with self.assertRaisesRegex(ValueError, "chmod 600"):
                mcp.tunnel_command(self.cfg(), {})


if __name__ == "__main__":
    unittest.main()
