"""The This Mac coding agent: run Codex, Claude or Devin on a private copy of the repository and keep what it
said and changed.

A job's command runs in its own folder (runs/<id>/). This clones the repository at HEAD into a temporary folder,
runs the agent there (`codex exec`, `claude -p`, or `devin acp` driven over the Agent Client Protocol as T3 Code
drives it), and leaves two files in the job's folder: answer.md
(the agent's last message) and changes.patch (everything it changed, binary-safe). The repository itself is never
written; uncommitted work in it is not in the copy.

Usage: python3 agent.py PROVIDER BINARY REPO BRIEF [OPTIONS...]   (PROVIDER: codex, claude or devin; options
such as --model go to the agent). Codex writes inside its own sandbox; Claude may edit files without asking
(accept-edits) but is refused anything else that needs permission. Devin runs in its Code mode, never Bypass: what it
asks about is answered by T3 Code's workspace-write rule (reads and searches pass, edits, deletes and moves pass inside
the copy, the rest is refused), but Devin itself decides which shell commands are safe enough to run without asking,
and its process sandbox is off unless set up with `devin sandbox`. Either way it works in the throwaway copy.
"""
import collections
import json
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


def _within(path, root):
    try:
        return (root / path).resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def _devin(binary, work, brief, extra, answer):
    """Run one prompt through `devin acp` (JSON-RPC over stdio) and write its last message to answer.md."""
    model = extra[extra.index("--model") + 1] if "--model" in extra else None
    proc = subprocess.Popen([binary, "acp", *(["--model", model] if model else [])], cwd=work, text=True,
                            encoding="utf-8", bufsize=1, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    log = collections.deque(maxlen=40)  # Devin logs to stderr at length; its tail is shown only if the run fails
    threading.Thread(target=lambda: log.extend(proc.stderr), daemon=True).start()
    ids = iter(range(1, 1_000_000))

    def send(**msg):
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        proc.stdin.flush()

    def permission(params):
        call = params.get("toolCall") or {}
        kind = call.get("kind") or "other"
        places = [loc.get("path", "") for loc in call.get("locations") or [] if isinstance(loc, dict)]
        allow = kind in ("read", "search", "think") or (
            kind in ("edit", "delete", "move") and places and all(_within(p, work) for p in places))
        wanted = ("allow_once",) if allow else ("reject_once", "reject_always")
        option = next((o for o in params.get("options", []) if o.get("kind") in wanted
                       and o.get("optionId", "").strip() != "plan_bypass"), None)  # never bypass, as T3
        print(f"{'allowed' if allow and option else 'refused'}: {call.get('title') or kind}", flush=True)
        return {"outcome": {"outcome": "selected", "optionId": option["optionId"]} if option
                else {"outcome": "cancelled"}}

    said, stop = [], None

    def call(method, params):
        rid = next(ids)
        send(id=rid, method=method, params=params)
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == rid and "method" not in msg:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message', msg['error'])}")
                return msg.get("result") or {}
            if msg.get("method") == "session/request_permission" and "id" in msg:
                send(id=msg["id"], result=permission(msg.get("params") or {}))
            elif "id" in msg and "method" in msg:  # a client feature this harness does not offer
                send(id=msg["id"], error={"code": -32601, "message": f"{msg['method']} is not supported"})
            elif msg.get("method") == "session/update":
                update = (msg.get("params") or {}).get("update") or {}
                kind = update.get("sessionUpdate")
                if kind == "agent_message_chunk" and (update.get("content") or {}).get("type") == "text":
                    said.append(update["content"]["text"])
                    print(update["content"]["text"], end="", flush=True)
                elif kind == "tool_call":
                    said.clear()  # the answer is the message after the last tool call
                    print(f"\n→ {update.get('title') or update.get('kind') or 'tool'}", flush=True)
        raise RuntimeError(f"{method}: devin acp exited ({proc.poll()})")

    try:
        call("initialize", {"protocolVersion": 1, "clientInfo": {"name": "research-harness", "version": "3"},
                            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                                   "terminal": False}})
        session = call("session/new", {"cwd": str(work), "mcpServers": []})["sessionId"]
        stop = call("session/prompt", {"sessionId": session, "prompt": [{"type": "text", "text": brief}]}).get("stopReason")
    except (RuntimeError, KeyError, BrokenPipeError) as error:
        print(f"\ndevin: {error}", flush=True)
        print("".join(log), end="", file=sys.stderr)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                pipe.close()
            except (OSError, ValueError):
                pass
    print(f"\nstopped: {stop}", flush=True)
    answer.write_text("".join(said).strip() + "\n", encoding="utf-8")
    return 0 if stop == "end_turn" else 1


def main(argv):
    provider, binary, repo, brief, extra = argv[1], argv[2], argv[3], argv[4], argv[5:]
    out = Path.cwd()
    scratch = Path(tempfile.mkdtemp(prefix="rh-agent-"))
    work = scratch / "repo"
    try:
        subprocess.run(["git", "clone", "--quiet", "--shared", repo, str(work)], check=True)
        answer = out / "answer.md"
        if provider == "codex":
            code = subprocess.run([binary, "exec", "--sandbox", "workspace-write", "--cd", str(work),
                                   "--output-last-message", str(answer), *extra, brief]).returncode
        elif provider == "devin":
            code = _devin(binary, work, brief, extra, answer)
        else:
            argv = [binary, "-p", brief, *extra, "--permission-mode", "acceptEdits", "--output-format", "text"]
            with answer.open("w", encoding="utf-8") as said:
                code = subprocess.run(argv, cwd=work, stdin=subprocess.DEVNULL, stdout=said).returncode
        if answer.exists():  # the copy is deleted below, so its paths in the answer become repo-relative
            text = answer.read_text(encoding="utf-8", errors="replace")
            answer.write_text(text.replace(str(work.resolve()) + "/", "").replace(str(work) + "/", ""),
                              encoding="utf-8")
        subprocess.run(["git", "-C", str(work), "add", "--all"], check=True)
        diff = subprocess.run(["git", "-C", str(work), "diff", "--cached", "--binary"],
                              capture_output=True, check=True).stdout
        if diff:
            (out / "changes.patch").write_bytes(diff)
        print(f"changes.patch: {len(diff.splitlines())} lines" if diff else "no files changed")
        return code
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
