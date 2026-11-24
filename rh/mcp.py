"""rh mcp: an MCP server over stdio, so GPT Pro in ChatGPT can take a gpt-chat job itself.

ChatGPT reaches it through OpenAI's Secure MCP Tunnel: tunnel-client runs on this machine, polls OpenAI for the
calls ChatGPT makes, starts `rh mcp` as its stdio command and passes each call through. No public port is opened.

A person still opens ChatGPT, picks the model and asks it to take a job, as policy.toml says GPT Pro chats go. GPT Pro
then reads the brief and the repository as committed at HEAD with read-only tools, and hands its answer back with
submit_reply, the one write: ChatGPT asks before it runs, and on a plan whose custom MCP servers get read tools only
(ChatGPT Pro, at the time of writing) it is not offered, so the reply is pasted back as before (`rh chat reply`).

Everything a caller sends is untrusted: anyone who can use the plugin in the ChatGPT workspace can call these tools.
So files are read from git's object store by blob id, never through a path on disk that a symlink could point
elsewhere; only jobs waiting for an answer can be read; and every argument is checked against its schema.

Messages are newline-delimited JSON-RPC 2.0 (the MCP stdio transport); logs go to stderr only.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, chat
from .ledger import Ledger, LedgerError

# Newest first. 2025-03-26 is left out: it requires JSON-RPC batches, which this server does not take.
VERSIONS = ("2025-11-25", "2025-06-18")
PAGE = 200_000          # characters per read_file call; the tunnel carries at most 10 MiB per response
MAX_BLOB = 8 << 20      # bytes: a larger file is refused before it is read
MAX_LINE = 4 << 20      # bytes per incoming message
MAX_REPLY = 1_000_000   # characters per submitted answer
LIST_LIMIT = 2_000      # paths per list_files call

INSTRUCTIONS = (
    "research-harness hands GPT Pro jobs to you. Call list_jobs to see the jobs waiting for an answer, then get_job "
    "for one job's full brief. Read the repository with list_files and read_file when the brief needs the code. "
    "Answer the brief itself. If submit_reply is available, call it once with the job id and your complete answer; "
    "if it is not, give the complete answer in the chat and the person will paste it into the harness.\n\n"
    "Files are read as committed at HEAD, in pages of 200,000 characters; call read_file again with the offset it "
    "gives. Uncommitted edits are not visible."
)

READ = {"readOnlyHint": True, "openWorldHint": False}
TOOLS = [
    {"name": "list_jobs", "title": "List waiting GPT Pro jobs",
     "description": "Use this first: the research-harness GPT Pro jobs waiting for an answer, one per line with "
                    "its id and the first line of its brief.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": READ},
    {"name": "get_job", "title": "Read a job's brief",
     "description": "Use this to read one waiting GPT Pro job: its full brief, verbatim, and the model it asked for.",
     "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string", "description": "The job id "
                                                                 "from list_jobs."}},
                     "required": ["job_id"], "additionalProperties": False},
     "annotations": READ},
    {"name": "list_files", "title": "List repository files",
     "description": "Use this to see which files the repository has at HEAD: one path per line, optionally only "
                    "those under a prefix such as 'rh/'.",
     "inputSchema": {"type": "object", "properties": {"prefix": {"type": "string", "description": "Only paths "
                                                                 "starting with this, for example 'rh/'."}},
                     "additionalProperties": False},
     "annotations": READ},
    {"name": "read_file", "title": "Read a repository file",
     "description": "Use this to read one file by its path from list_files, as committed at HEAD. Long files come "
                    "in pages of 200,000 characters; the page says the offset to ask for next.",
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
         "text": {"type": "string", "minLength": 1, "maxLength": MAX_REPLY,
                  "description": "The complete answer, in Markdown."}},
         "required": ["job_id", "text"], "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                     "openWorldHint": False}},
]


class ToolError(Exception):
    """A tool ran and failed: ChatGPT gets the text as an error result it can read and act on."""


def _check_args(tool, args):
    """The arguments against the tool's own schema: a model may send null, a number as a string, or a field more."""
    schema = tool["inputSchema"]
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    missing = [k for k in schema.get("required", []) if k not in args]
    if missing:
        raise ToolError("missing " + ", ".join(missing))
    for key, value in args.items():
        spec = schema["properties"].get(key)
        if spec is None:
            raise ToolError(f"unknown argument {key}")
        if spec["type"] == "string":
            if not isinstance(value, str):
                raise ToolError(f"{key} must be a string")
            if not spec.get("minLength", 0) <= len(value) <= spec.get("maxLength", len(value)):
                raise ToolError(f"{key} must be {spec.get('minLength', 0)} to {spec['maxLength']:,} characters")
        elif spec["type"] == "integer":
            if isinstance(value, bool) or not isinstance(value, int) or value < spec.get("minimum", value):
                raise ToolError(f"{key} must be a whole number of at least {spec.get('minimum', 0)}")


class Server:
    def __init__(self, cfg, clock=time.time):
        self.cfg, self.clock = cfg, clock
        self.repo = Path(str(cfg["repo"]["path"])).expanduser()

    # --- the ledger, opened per call so no lock is held while ChatGPT thinks
    def _with_ledger(self, fn):
        if not (Path(self.cfg["home"]) / "ledger.db").is_file():
            raise ToolError("this harness has no jobs yet")
        led = Ledger(Path(self.cfg["home"]) / "ledger.db")
        try:
            return fn(led)
        finally:
            led.close()

    def _waiting(self, led, job_id):
        job = led.job(job_id)
        if job is None or job["spec"]["kind"] != "gpt-chat":
            raise ToolError(f"no GPT Pro job {job_id}; list_jobs names the ones waiting for an answer")
        if job["state"] != "handoff":
            raise ToolError(f"{job_id} is {job['state']}, not waiting for an answer")
        return job

    # --- tools
    def list_jobs(self):
        def run(led):
            jobs = [j for j in led.jobs(states={"handoff"}) if j["spec"]["kind"] == "gpt-chat"]
            if not jobs:
                return "No GPT Pro jobs are waiting for an answer."
            lines = [f"{j['id']}: {(j['spec']['brief'].strip().splitlines() or [''])[0][:200]}" for j in jobs[:200]]
            more = f"\n({len(jobs) - 200} more not shown)" if len(jobs) > 200 else ""
            return f"{len(jobs)} waiting:\n" + "\n".join(lines) + more
        return self._with_ledger(run)

    def get_job(self, job_id):
        def run(led):
            spec = self._waiting(led, job_id)["spec"]
            inputs = [str(i.get("path", i)) if isinstance(i, dict) else str(i) for i in spec.get("inputs", [])]
            files = ("\nInputs (read them with read_file): " + ", ".join(inputs)) if inputs else ""
            brief = chat.prompt(spec)
            if len(brief) > PAGE:
                brief = brief[:PAGE] + f"\n\n[the brief goes on for {len(brief) - PAGE:,} more characters]"
            return f"Job {job_id}, waiting for an answer, model {spec['model']['requested']}.{files}\n\nBrief:\n{brief}"
        return self._with_ledger(run)

    def _tree(self):
        """Regular files committed at HEAD under repo.path: {path: (blob id, size)}. Symlinks and submodules are
        left out, and so is a name that is not UTF-8 or holds a newline, so every listed path reads back as itself."""
        try:
            out = subprocess.run(["git", "-C", str(self.repo), "ls-tree", "-r", "-z", "-l", "HEAD"],
                                 capture_output=True, timeout=30, check=True).stdout
        except (OSError, subprocess.SubprocessError) as e:
            raise ToolError(f"cannot list the repository's files at HEAD: {e}") from None
        tree = {}
        for entry in out.split(b"\0"):
            meta, _, raw = entry.partition(b"\t")
            fields = meta.split()
            if len(fields) != 4 or fields[0] not in (b"100644", b"100755") or fields[1] != b"blob":
                continue
            try:
                path = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if path and "\n" not in path:
                tree[path] = (fields[2].decode("ascii"), int(fields[3]))
        return tree

    def list_files(self, prefix=""):
        paths = sorted(p for p in self._tree() if p.startswith(prefix))
        if not paths:
            return f"No files at HEAD start with {prefix!r}."
        more = f"\n({len(paths) - LIST_LIMIT:,} more; ask with a longer prefix)" if len(paths) > LIST_LIMIT else ""
        return "\n".join(paths[:LIST_LIMIT]) + more

    def read_file(self, path, offset=0):
        found = self._tree().get(path)
        if found is None:
            raise ToolError(f"{path!r} is not a file at HEAD; use a path exactly as list_files prints it")
        blob, size = found
        if size > MAX_BLOB:
            raise ToolError(f"{path} is {size:,} bytes, over the {MAX_BLOB:,}-byte limit")
        try:
            text = subprocess.run(["git", "-C", str(self.repo), "cat-file", "blob", blob], capture_output=True,
                                  timeout=30, check=True).stdout.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolError(f"{path} is not UTF-8 text") from None
        except (OSError, subprocess.SubprocessError) as e:
            raise ToolError(f"cannot read {path}: {e}") from None
        if offset > len(text):
            raise ToolError(f"offset {offset} is past the end of {path} ({len(text):,} characters)")
        page = text[offset:offset + PAGE]
        if offset + PAGE < len(text):
            page += (f"\n\n[{len(text) - offset - PAGE:,} more characters: call read_file with path={path} "
                     f"offset={offset + PAGE}]")
        return page

    def submit_reply(self, job_id, text):
        def run(led):
            self._waiting(led, job_id)
            try:
                chat.save_reply(self.cfg, led, job_id, text, self.clock())
            except (ValueError, LedgerError) as e:
                raise ToolError(str(e)) from None
            return f"Saved. {job_id} is done; its answer is in the harness."
        return self._with_ledger(run)

    # --- JSON-RPC
    def handle(self, msg):
        """One message in, one reply out, or None for a notification."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            mid = msg.get("id") if isinstance(msg, dict) else None
            return _error(mid if _good_id(mid) else None, -32600, "not a JSON-RPC 2.0 request")
        if "id" not in msg:
            return None  # notifications, notifications/initialized among them, need no reply
        mid, method, params = msg["id"], msg["method"], msg.get("params")
        if not _good_id(mid):
            return _error(None, -32600, "id must be a string or an integer")
        params = {} if params is None else params
        if not isinstance(params, dict):
            return _error(mid, -32602, "params must be an object")
        if method == "initialize":
            asked = params.get("protocolVersion")
            return _result(mid, {"protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
                                 "capabilities": {"tools": {"listChanged": False}},
                                 "serverInfo": {"name": "research-harness", "title": "research-harness",
                                                "version": __version__},
                                 "instructions": INSTRUCTIONS})
        if method == "ping":
            return _result(mid, {})
        if method == "tools/list":
            return _result(mid, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name")
            tool = next((t for t in TOOLS if t["name"] == name), None)
            if tool is None:
                return _error(mid, -32602, f"unknown tool {name!r}")
            args = params.get("arguments")
            args = {} if args is None else args
            try:
                _check_args(tool, args)
                text, failed = getattr(self, name)(**args), False
            except ToolError as e:
                text, failed = str(e), True
            return _result(mid, {"content": [{"type": "text", "text": text}], "isError": failed})
        return _error(mid, -32601, f"method not found: {method}")


def _good_id(mid):
    return isinstance(mid, str) or (isinstance(mid, int) and not isinstance(mid, bool))


def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def run(cfg, stdin=None, stdout=None):
    """Serve until stdin closes. Bytes in and out, UTF-8 on every OS whatever the console code page."""
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    server = Server(cfg)
    while True:
        raw = stdin.readline(MAX_LINE + 1)
        if not raw:
            return 0
        if len(raw) > MAX_LINE:
            while raw and not raw.endswith(b"\n"):  # skip the rest of the oversized message
                raw = stdin.readline(MAX_LINE)
            reply = _error(None, -32600, f"a message is over {MAX_LINE:,} bytes")
        else:
            try:
                line = raw.decode("utf-8").strip()  # strict: an answer must arrive as it was written
                if not line:
                    continue
                msg = json.loads(line)
            except (UnicodeDecodeError, ValueError, RecursionError):
                reply = _error(None, -32700, "parse error: each line must be one UTF-8 JSON-RPC message")
            else:
                try:
                    reply = server.handle(msg)
                except Exception as e:  # one bad call must not end the session ChatGPT holds open
                    print(f"rh mcp: {type(e).__name__}: {e}", file=sys.stderr)
                    mid = msg.get("id") if isinstance(msg, dict) else None
                    reply = _error(mid if _good_id(mid) else None, -32603, "internal error")
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False).encode("utf-8", "backslashreplace") + b"\n")
            stdout.flush()


ROOT = Path(__file__).resolve().parents[1]
TUNNEL_ID = re.compile(r"tunnel_[a-z0-9]{16,64}")
# Settings tunnel-client reads from its environment. Only what this harness passes on the command line may apply, so
# an inherited key cannot be used, an inherited endpoint cannot receive the key, and raw HTTP logging stays off.
_TUNNEL_ENV = ("OPENAI_API_KEY", "CONTROL_PLANE_", "MCP_", "HARPOON_", "LOG_", "TUNNEL_CLIENT_", "TRACING_",
               "HEALTH_", "ADMIN_UI_", "CLOUDFLARED_", "ALLOW_REMOTE_UI", "OPEN_WEB_UI", "PID_FILE",
               "FORWARD_REQUEST_ID", "CA_BUNDLE", "PROXY_CHECK_INTERVAL")


def key_path(cfg):
    return Path(cfg["home"]) / "secrets" / "openai-tunnel.key"


def tunnel_command(cfg, environ):
    """tunnel-client's argv and environment for this harness, or a ValueError naming the owner step still missing.

    The key is passed by reference (file:...), never read here. tunnel-client's own environment settings are dropped
    (with no key flag it would fall back to OPENAI_API_KEY, which may be a key that spends), and the control plane,
    the log file and raw-HTTP logging are set on the command line.
    """
    tid = str(cfg["mcp"].get("tunnel_id") or "")
    if not TUNNEL_ID.fullmatch(tid):
        raise ValueError("no tunnel id: create a tunnel at platform.openai.com > Settings > Tunnels, then "
                         "rh config set mcp.tunnel_id <its id, tunnel_...>")
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
    # tunnel-client splits the command like a POSIX shell (quotes, backslash escapes), so shell quoting carries any
    # path through; only the segment markers it looks for in the raw text cannot be quoted away.
    command = " ".join(shlex.quote(a) for a in (sys.executable, "-I", str(ROOT / "bin" / "rh"), "mcp"))
    if re.search(r",(channel|url|unix-socket|http-proxy|client-cert|client-key)=", command):
        raise ValueError(f"tunnel-client cannot take a command containing {command!r}")
    home = Path(cfg["home"])
    argv = [binary, "run",
            "--control-plane.base-url", "https://api.openai.com",
            "--control-plane.tunnel-id", tid,
            "--control-plane.api-key", f"file:{key}",
            "--mcp.command", f"command={command}",
            "--health.listen-addr", "127.0.0.1:0",
            "--log.file", str(home / "tunnel.log"),
            "--log.http-raw-unsafe=false",
            "--pid.file", str(home / "tunnel.pid")]
    env = {k: v for k, v in environ.items() if not k.upper().startswith(_TUNNEL_ENV)}
    env["RH_HOME"] = str(home)
    return argv, env


def run_tunnel(cfg):
    argv, env = tunnel_command(cfg, os.environ)
    print(f"tunnel {cfg['mcp']['tunnel_id']}: running tunnel-client; log in {Path(cfg['home']) / 'tunnel.log'}. "
          "Stop with Ctrl-C.", file=sys.stderr)
    if os.name != "nt":
        os.execve(argv[0], argv, env)  # tunnel-client takes this process's place, so Ctrl-C and signals reach it
    try:
        return subprocess.run(argv, env=env).returncode
    except KeyboardInterrupt:
        return 0
