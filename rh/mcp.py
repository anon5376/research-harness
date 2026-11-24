"""rh mcp: an MCP server over stdio, so GPT Pro in ChatGPT can take a gpt-chat job itself.

ChatGPT reaches it through OpenAI's Secure MCP Tunnel: tunnel-client runs on this machine, polls OpenAI for the
calls ChatGPT makes, starts `rh mcp` as its stdio command and passes each call through. Nothing listens on a port.

A person still opens ChatGPT, picks the model and asks it to take a job, as policy.toml says GPT Pro chats go. GPT Pro
then reads the brief and the repository's tracked files with read-only tools, and hands its answer back with
submit_reply, the one write: ChatGPT asks before it runs, and on a plan whose custom MCP servers get read tools only
(ChatGPT Pro, at the time of writing) it is not offered, so the reply is pasted back as before (`rh chat reply`).

Messages are newline-delimited JSON-RPC 2.0 (the MCP stdio transport); logs go to stderr only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

from . import __version__, chat
from .ledger import Ledger

VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
PAGE = 200_000  # characters per read_file call; the tunnel carries at most 10 MiB per response

INSTRUCTIONS = (
    "research-harness hands GPT Pro jobs to you. Call list_jobs to see the jobs waiting for an answer, then get_job "
    "for one job's full brief. Read the repository with list_files and read_file when the brief needs the code. "
    "Answer the brief itself; when you are done, call submit_reply with the job id and your complete answer.\n\n"
    "read_file returns a file in pages of 200,000 characters; call it again with the offset it gives. Only files "
    "tracked by git can be read. submit_reply works once per job and ends the job."
)

READ = {"readOnlyHint": True, "openWorldHint": False}
TOOLS = [
    {"name": "list_jobs", "title": "List waiting GPT Pro jobs",
     "description": "Use this first: the research-harness GPT Pro jobs waiting for an answer, one per line with "
                    "its id and the first line of its brief.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": READ},
    {"name": "get_job", "title": "Read a job's brief",
     "description": "Use this to read one GPT Pro job: its full brief, verbatim, with its state and the model it "
                    "asked for.",
     "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string", "description": "The job id "
                                                                 "from list_jobs."}},
                     "required": ["job_id"], "additionalProperties": False},
     "annotations": READ},
    {"name": "list_files", "title": "List repository files",
     "description": "Use this to see which files the repository has: the paths git tracks, one per line, "
                    "optionally only those under a prefix such as 'rh/'.",
     "inputSchema": {"type": "object", "properties": {"prefix": {"type": "string", "description": "Only paths "
                                                                 "starting with this, for example 'rh/'."}},
                     "additionalProperties": False},
     "annotations": READ},
    {"name": "read_file", "title": "Read a repository file",
     "description": "Use this to read one tracked file by its path from list_files. Long files come in pages of "
                    "200,000 characters; the page says the offset to ask for next.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "A path exactly as list_files prints it."},
         "offset": {"type": "integer", "minimum": 0, "description": "Character offset to start from; 0 by default."}},
         "required": ["path"], "additionalProperties": False},
     "annotations": READ},
    {"name": "submit_reply", "title": "Hand in the answer",
     "description": "Use this once, when your answer to a job is complete: it saves the text as the job's result "
                    "and ends the job done. Send the whole answer, not a summary.",
     "inputSchema": {"type": "object", "properties": {
         "job_id": {"type": "string", "description": "The job id from list_jobs."},
         "text": {"type": "string", "description": "The complete answer, in Markdown."}},
         "required": ["job_id", "text"], "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                     "openWorldHint": False}},
]


class ToolError(Exception):
    """A tool ran and failed: ChatGPT gets the text as an error result it can read and act on."""


class Server:
    def __init__(self, cfg, clock=time.time):
        self.cfg, self.clock = cfg, clock
        self.repo = Path(str(cfg["repo"]["path"])).expanduser()

    # --- the ledger, opened per call so no lock is held while ChatGPT thinks
    def _with_ledger(self, fn):
        led = Ledger(Path(self.cfg["home"]) / "ledger.db")
        try:
            return fn(led)
        finally:
            led.close()

    def _chat_job(self, led, job_id):
        job = led.job(str(job_id))
        if job is None or job["spec"]["kind"] != "gpt-chat":
            raise ToolError(f"no GPT Pro job {job_id}; list_jobs names the ones waiting")
        return job

    # --- tools
    def list_jobs(self):
        def run(led):
            jobs = [j for j in led.jobs(states={"handoff"}) if j["spec"]["kind"] == "gpt-chat"]
            if not jobs:
                return "No GPT Pro jobs are waiting for an answer."
            lines = [f"{j['id']}: {(j['spec']['brief'].strip().splitlines() or [''])[0][:200]}" for j in jobs]
            return f"{len(jobs)} waiting:\n" + "\n".join(lines)
        return self._with_ledger(run)

    def get_job(self, job_id):
        def run(led):
            job = self._chat_job(led, job_id)
            spec = job["spec"]
            head = f"Job {job['id']}, {job['state']}, model {spec['model']['requested']}."
            if job["state"] != "handoff":
                head += " It is not waiting for an answer, so submit_reply will refuse it."
            inputs = [str(i.get("path", i)) if isinstance(i, dict) else str(i) for i in spec.get("inputs", [])]
            files = ("\nInputs (read them with read_file): " + ", ".join(inputs)) if inputs else ""
            return f"{head}{files}\n\nBrief:\n{chat.prompt(spec)}"
        return self._with_ledger(run)

    def _tracked(self):
        try:
            out = subprocess.run(["git", "-C", str(self.repo), "ls-files", "-z"], capture_output=True, timeout=30,
                                 check=True).stdout
        except (OSError, subprocess.SubprocessError) as e:
            raise ToolError(f"cannot list the repository's files: {e}") from None
        return sorted(p for p in out.decode("utf-8", "replace").split("\0") if p)

    def list_files(self, prefix=""):
        paths = [p for p in self._tracked() if p.startswith(str(prefix))]
        return "\n".join(paths) if paths else f"No tracked files start with {prefix!r}."

    def read_file(self, path, offset=0):
        path, offset = str(path), int(offset)
        if offset < 0:
            raise ToolError("offset must be 0 or more")
        parts = PurePosixPath(path).parts
        if not path or path.startswith(("/", "\\")) or ".." in parts or "\\" in path or ":" in path:
            raise ToolError(f"{path!r} is not a repository path; use a path exactly as list_files prints it")
        if path not in set(self._tracked()):
            raise ToolError(f"{path} is not a file git tracks here; only tracked files can be read")
        try:
            text = (self.repo / path).read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            raise ToolError(f"{path} is not UTF-8 text") from None
        except OSError as e:
            raise ToolError(f"cannot read {path}: {e.strerror or e}") from None
        page = text[offset:offset + PAGE]
        if offset + PAGE < len(text):
            page += (f"\n\n[{len(text) - offset - PAGE:,} more characters: call read_file with path={path} "
                     f"offset={offset + PAGE}]")
        return page

    def submit_reply(self, job_id, text):
        def run(led):
            self._chat_job(led, job_id)
            try:
                chat.save_reply(self.cfg, led, str(job_id), str(text), self.clock())
            except ValueError as e:
                raise ToolError(str(e)) from None
            return f"Saved. {job_id} is done; its answer is in the harness."
        return self._with_ledger(run)

    # --- JSON-RPC
    def handle(self, msg):
        """One message in, one reply out, or None for a notification."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "not a JSON-RPC 2.0 request")
        if "id" not in msg:
            return None
        mid, method, params = msg["id"], msg["method"], msg.get("params") or {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            return _result(mid, {"protocolVersion": asked if asked in VERSIONS else VERSIONS[1],
                                 "capabilities": {"tools": {"listChanged": False}},
                                 "serverInfo": {"name": "research-harness", "title": "research-harness",
                                                "version": __version__},
                                 "instructions": INSTRUCTIONS})
        if method == "ping":
            return _result(mid, {})
        if method == "tools/list":
            return _result(mid, {"tools": TOOLS})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            tool = next((t for t in TOOLS if t["name"] == name), None)
            if tool is None:
                return _error(mid, -32602, f"unknown tool {name!r}")
            schema = tool["inputSchema"]
            missing = [k for k in schema.get("required", []) if k not in args]
            extra = [k for k in args if k not in schema["properties"]]
            try:
                if missing or extra:
                    raise ToolError("missing " + ", ".join(missing) if missing else "unknown " + ", ".join(extra))
                text, failed = getattr(self, name)(**args), False
            except ToolError as e:
                text, failed = str(e), True
            except (TypeError, ValueError) as e:
                text, failed = f"bad arguments: {e}", True
            return _result(mid, {"content": [{"type": "text", "text": text}], "isError": failed})
        return _error(mid, -32601, f"method not found: {method}")


def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def run(cfg, stdin=None, stdout=None):
    """Serve until stdin closes. Bytes in and out, UTF-8 on every OS whatever the console code page."""
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    server = Server(cfg)
    for raw in stdin:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply = _error(None, -32700, "parse error: each line must be one JSON-RPC message")
        else:
            try:
                reply = server.handle(msg)
            except Exception as e:  # one bad call must not end the session ChatGPT holds open
                print(f"rh mcp: {type(e).__name__}: {e}", file=sys.stderr)
                reply = _error(msg.get("id") if isinstance(msg, dict) else None, -32603, "internal error")
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()
    return 0


ROOT = Path(__file__).resolve().parents[1]
TUNNEL_ID = re.compile(r"tunnel_[0-9a-f]{32}")


def key_path(cfg):
    return Path(cfg["home"]) / "secrets" / "openai-tunnel.key"


def tunnel_command(cfg, environ):
    """tunnel-client's argv and environment for this harness, or a ValueError naming the owner step still missing.

    The key is passed by reference (file:...), never read here. OPENAI_API_KEY and CONTROL_PLANE_API_KEY are dropped
    from the environment: with no key flag tunnel-client falls back to OPENAI_API_KEY, which may be a key that spends.
    """
    tid = str(cfg["mcp"].get("tunnel_id") or "")
    if not TUNNEL_ID.fullmatch(tid):
        raise ValueError("no tunnel id: create a tunnel at platform.openai.com > Settings > Tunnels, then "
                         "rh config set mcp.tunnel_id tunnel_<32 hex>")
    key = key_path(cfg)
    if not key.is_file():
        raise ValueError(f"no tunnel key: create a Restricted API key with Tunnels: Read and Use only, and save it "
                         f"alone in {key} (chmod 600)")
    if os.name != "nt" and key.stat().st_mode & 0o077:
        raise ValueError(f"{key} is readable by others: chmod 600 {key}")
    named = str(cfg["mcp"].get("tunnel_client") or "tunnel-client")
    binary = shutil.which(named) or shutil.which(str(Path.home() / ".local" / "bin" / named))
    if not binary:
        raise ValueError(f"tunnel-client not found ({named}): install it from github.com/openai/tunnel-client "
                         "releases, or rh config set mcp.tunnel_client /path/to/tunnel-client")
    rh = ROOT / "bin" / "rh"
    if any(c in f"{sys.executable}{rh}" for c in " ,"):
        raise ValueError(f"tunnel-client splits its command on spaces and commas; {sys.executable} or {rh} has one")
    home = Path(cfg["home"])
    argv = [binary, "run",
            "--control-plane.tunnel-id", tid,
            "--control-plane.api-key", f"file:{key}",
            "--mcp.command", f"command={sys.executable} -I {rh} mcp",
            "--health.listen-addr", "127.0.0.1:0",
            "--log.file", str(home / "tunnel.log"),
            "--pid.file", str(home / "tunnel.pid")]
    env = {k: v for k, v in environ.items() if k not in ("OPENAI_API_KEY", "CONTROL_PLANE_API_KEY")}
    env["RH_HOME"] = str(home)
    return argv, env


def run_tunnel(cfg):
    argv, env = tunnel_command(cfg, os.environ)
    print(f"tunnel {cfg['mcp']['tunnel_id']}: running tunnel-client; log in {Path(cfg['home']) / 'tunnel.log'}. "
          "Stop with Ctrl-C.", file=sys.stderr)
    try:
        return subprocess.run(argv, env=env).returncode
    except KeyboardInterrupt:
        return 0
