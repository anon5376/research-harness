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
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

# A run that ended cleanly having said nothing and changed nothing exits with this, so the harness ends it as
# no_result rather than done (rh/collect.py maps it, as it maps 75 to capacity).
NO_RESULT = 66
SILENT = "_Devin ended its turn without a final message. Its work is in the output above._"


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
        opened = call("session/new", {"cwd": str(work), "mcpServers": []})
        session = opened["sessionId"]
        # `--model` is a start-up default; if the session came up on another model, ask for this one by name, but
        # only by a name its menu lists: the menu names families (swe-2-high) and keeps thinking (max) as its own
        # option, so "swe-2-max" is refused there while the start-up flag has already chosen it.
        menu = next((o for o in opened.get("configOptions") or [] if isinstance(o, dict) and o.get("id") == "model"), {})
        current = menu.get("currentValue")
        names = {x.get("value") for x in menu.get("options") or [] if isinstance(x, dict)}
        if model and current and current != model and (not names or model in names):
            call("session/set_config_option", {"sessionId": session, "configId": "model", "value": model})
            current = model
        print(f"model: {model or current or 'default'}", flush=True)
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
    text = "".join(said).strip()
    if not text and stop == "end_turn":  # it happens: the turn ends on a tool call with nothing said after it
        text = SILENT
    answer.write_text(text + "\n", encoding="utf-8")
    return 0 if stop == "end_turn" else 1


def _continue(work, prior, brief):
    """Set the fresh copy up as the earlier run left it (its commit, with its changes applied) and return the brief
    with that run's answer in front, so the agent picks up where it stopped. The changes.patch of a follow-up holds
    every run's changes since that commit, so a chain carries them all."""
    base, patch, said = prior / "base", prior / "changes.patch", prior / "answer.md"
    if base.is_file():
        subprocess.run(["git", "-C", str(work), "checkout", "--quiet", "--detach",
                        base.read_text(encoding="utf-8").strip()], check=True)
    note = ""
    if patch.is_file() and patch.stat().st_size:
        applied = subprocess.run(["git", "-C", str(work), "apply", "--index", "--whitespace=nowarn", str(patch)],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace")
        note = ("The earlier run's changes are already applied in this copy.\n" if applied.returncode == 0 else
                "The earlier run's changes could not be applied here; start from the files as they are.\n")
        print(note.strip(), flush=True)
    answer = said.read_text(encoding="utf-8", errors="replace").strip() if said.is_file() else ""
    earlier = f"Your answer in the earlier run was:\n\n{answer}\n\n" if answer else ""
    return f"This continues an earlier run on this repository. {note}{earlier}The request, with the follow-up last:\n\n{brief}"


def _check(repo, run, check, scratch):
    """Run the swarm's check on one run's changes in a clean copy at that run's commit: (verdict, output tail)."""
    patch = run / "changes.patch"
    if not patch.is_file() or not patch.stat().st_size:
        return "no changes", ""
    copy = scratch / f"check-{run.name}"
    subprocess.run(["git", "clone", "--quiet", "--shared", repo, str(copy)], check=True)
    if (run / "base").is_file():
        commit = (run / "base").read_text(encoding="utf-8").strip()
        moved = subprocess.run(["git", "-C", str(copy), "checkout", "--quiet", "--detach", commit],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
        if moved.returncode:  # checked anywhere else, a pass would be for a tree the run never made
            return f"failed: its commit {commit[:12]} is not in the repository", moved.stderr[-2000:]
    applied = subprocess.run(["git", "-C", str(copy), "apply", "--whitespace=nowarn", str(patch)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
    if applied.returncode:
        return "failed: the changes did not apply", applied.stderr[-2000:]
    try:
        done = subprocess.run(shlex.split(check), cwd=copy, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=1800)
    except subprocess.TimeoutExpired:
        return "failed: timed out after 30 min", ""
    except OSError as error:
        return f"failed: {error}", ""
    tail = "\n".join((done.stdout + done.stderr).rstrip().splitlines()[-30:])
    return ("passed" if done.returncode == 0 else f"failed: exit {done.returncode}"), tail


def _judge(work, runs, brief, repo=None, check="", out=None, scratch=None):
    """Lay each swarm run's answer and changes in .swarm/<run>/ of the fresh copy and return the judge's brief. With a
    check, the harness first runs it on each run's changes in a clean copy (agents may not run commands), so the
    judge decides on evidence; the verdicts are also kept in swarm-checks.json beside the judge's answer."""
    shown, verdicts = [], {}
    for run in runs:
        place = work / ".swarm" / run.name
        place.mkdir(parents=True, exist_ok=True)
        for name in ("answer.md", "changes.patch"):
            if (run / name).is_file():
                shutil.copyfile(run / name, place / name)
        notes = [] if (run / "answer.md").is_file() else ["it left no answer"]
        if check:
            verdict, tail = _check(repo, run, check, scratch)
            verdicts[run.name] = verdict
            print(f"check {run.name}: {verdict}", flush=True)
            if verdict == "no changes":
                notes.append("no changes to check")
            else:
                notes.append(f"its check {verdict}")
                (place / "check.txt").write_text(f"{verdict}\n\n{tail}\n", encoding="utf-8")
        shown.append(f"- .swarm/{run.name}/" + (f" ({'; '.join(notes)})" if notes else ""))
    if check and out is not None:
        (out / "swarm-checks.json").write_text(json.dumps(verdicts, indent=1) + "\n", encoding="utf-8")
    checked = (f"\n\nThe harness ran `{check}` on each run's changes in a clean copy; its verdict and last lines "
               "are in check.txt. Prefer work that passed." if check else "")
    return (f"{len(runs)} agents each worked on the request below in their own copy of this repository. Each one's "
            "final answer (answer.md) and its changes (changes.patch, a git diff) are in this folder:\n\n"
            + "\n".join(shown) + checked + "\n\nRead them all. Choose the best work, or combine the best parts of several, "
            "and make this copy hold that result (git apply a patch, then edit as needed); check it the way the "
            "request needs. Do not edit .swarm, which is removed afterwards. Start your answer with a one-line "
            "title of the change itself (what it does, not which run you chose), then say which run you chose and "
            "why in two or three sentences.\n\nThe request:\n\n" + brief)


def _with_plan(brief, plan):
    """A swarm agent's brief with its lead's plan after it, read when the agent starts (the plan is written by then)."""
    try:
        text = plan.read_text(encoding="utf-8", errors="replace").strip()[:20000]
    except OSError:
        return brief
    return brief + ("\n\nYour lead's plan for this request (follow it; where it is wrong, say so):\n\n" + text
                    if text else "")


def main(argv):
    provider, binary, repo, brief, extra = argv[1], argv[2], argv[3], argv[4], argv[5:]
    prior, runs = None, []
    if "--prior" in extra[:-1]:  # a follow-up: continue the run in this folder
        at = extra.index("--prior")
        prior, extra = Path(extra[at + 1]), extra[:at] + extra[at + 2:]
    while "--swarm" in extra[:-1]:  # a swarm's judge: compare these runs' work
        at = extra.index("--swarm")
        runs.append(Path(extra[at + 1]))
        extra = extra[:at] + extra[at + 2:]
    role, plan = "", None
    if "--role" in extra[:-1]:  # a swarm agent's system prompt: who it is in the team
        at = extra.index("--role")
        role, extra = extra[at + 1], extra[:at] + extra[at + 2:]
    if "--context" in extra[:-1]:  # a swarm agent's lead's plan, by the path its answer is written to
        at = extra.index("--context")
        plan, extra = Path(extra[at + 1]), extra[:at] + extra[at + 2:]
    check = ""
    if "--check" in extra[:-1]:  # a swarm's judge: the command that checks each run's changes first
        at = extra.index("--check")
        check, extra = extra[at + 1], extra[:at] + extra[at + 2:]
    out = Path.cwd()
    scratch = Path(tempfile.mkdtemp(prefix="rh-agent-"))
    work = scratch / "repo"
    try:
        subprocess.run(["git", "clone", "--quiet", "--shared", repo, str(work)], check=True)
        if plan is not None:
            brief = _with_plan(brief, plan)
        if prior:
            brief = _continue(work, prior, brief)
        if runs:
            brief = _judge(work, runs, brief, repo, check, out, scratch)
        if role:  # the role leads, apart from the request, so the request reads as the person wrote it
            brief = f"{role.strip()}\n\n---\n\n{brief}"
        base = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True,
                              encoding="utf-8", check=True).stdout.strip()
        (out / "base").write_text(base + "\n", encoding="utf-8")  # the commit the changes apply to
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
        shutil.rmtree(work / ".swarm", ignore_errors=True)  # the runs' work is theirs, never the judge's change
        subprocess.run(["git", "-C", str(work), "add", "--all"], check=True)
        diff = subprocess.run(["git", "-C", str(work), "diff", "--cached", "--binary"],
                              capture_output=True, check=True).stdout
        if diff:
            (out / "changes.patch").write_bytes(diff)
        if runs:  # which run's work the judge kept whole, by its patch, so the app need not read the judge's prose
            def same(patch):  # one change, whatever blob ids or line positions (the repo may have moved) it carries
                return [b"@@" if line.startswith(b"@@") else line for line in patch.splitlines()
                        if not line.startswith(b"index ")]
            kept = next((run.name for run in runs if (run / "changes.patch").is_file()
                         and same((run / "changes.patch").read_bytes()) == same(diff)), "combined" if diff else "none")
            result = {"kept": kept}
            if check and diff:  # the judge may have combined runs, so what it keeps is checked like any run
                try:
                    earlier = json.loads((out / "swarm-checks.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    earlier = {}
                def same_tree(run):  # a verdict holds only for the exact tree it was given: same commit, same patch
                    try:
                        return ((run / "base").read_text(encoding="utf-8").strip() == base
                                and (run / "changes.patch").read_bytes() == diff)
                    except OSError:
                        return False
                whole = next((run for run in runs if run.name == kept), None)
                verdict = earlier.get(kept) if isinstance(earlier, dict) and whole and same_tree(whole) else None
                if not verdict:  # anything else, a combined tree or one on a newer commit, is checked now
                    verdict, _ = _check(repo, out, check, scratch)
                result["check"] = verdict
                print(f"check result: {verdict}", flush=True)
            (out / "swarm-result.json").write_text(json.dumps(result) + "\n", encoding="utf-8")
        print(f"changes.patch: {len(diff.splitlines())} lines" if diff else "no files changed")
        said = answer.read_text(encoding="utf-8", errors="replace").strip() if answer.exists() else ""
        if code == 0 and not diff and said in ("", SILENT):
            print("no result: the agent ended with no answer and no changes", flush=True)
            code = NO_RESULT
        # a swarm judge ends failed, not done, when the work it kept fails the check, whatever the agent returned
        if runs and result.get("check", "passed") != "passed":
            print(f"the kept work failed its check: {result['check']}", flush=True)
            code = code or 1
        return code
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
