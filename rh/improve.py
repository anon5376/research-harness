"""The improve loop: the harness sends a coding agent to improve the harness, one reviewable change per round.

Each round files a New job exactly as the app's composer does (a local agent on a private clone of the repository,
by default Devin's Fusion of Claude Fable 5.2 leading and SWE-2 High as its sidekick), lets the supervisor run it, then
applies the job's changes.patch to a clean clone of the commit the agent saw, runs the project's tests and privacy scan
there, and keeps the patch on a local branch improve/<job> only when both pass. Nothing is pushed or merged; that is
the lead's. Each round's brief lists what the earlier rounds tried, so the agent picks something else. The rounds and
their verdicts are appended to $RH_HOME/improve.jsonl.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from rh.ledger import TERMINAL, Ledger

FUSION = "fusion-claude-fable-5-2-medium-sidekick-swe-2-high"
CHECK = "python3 -I tests/run.py"
_SCANNER = Path(__file__).with_name("privacy.py")

BRIEF = """\
Improve research-harness, the repository you are in, with ONE small, concrete change a user of it would notice.
Read README.md and AGENTS.md first, then the code you touch and its tests.

Pick a real defect or gap you can show: a bug, a missing check, an error message that misleads, a gap in the web
app's behaviour, or a test that cannot fail. Prefer the least risky change that fixes it. Rules:
- Python standard library only; match the surrounding style; no new files unless the change needs them.
- Add or change a test that fails without your change and passes with it. Run `{check}` before you finish.
- Do not touch policy.toml, AGENTS.md, hooks/, rh/privacy.py or the README's claims about what was run.
- Nothing private: no home-directory paths, emails, tokens or account ids.
End with a short answer: a one-line title, then what was wrong, what you changed, and the test that proves it.
{focus}{tried}"""


def brief(focus, history):
    """The round's brief: the standing ask, the owner's focus if any, and what earlier rounds tried."""
    tried = ""
    if history:
        lines = [f"- {h['title']} ({h['verdict']})" for h in history]
        tried = "\nEarlier rounds already tried these; pick something different:\n" + "\n".join(lines) + "\n"
    return BRIEF.format(check=CHECK, focus=f"\nFocus: {focus.strip()}\n" if focus.strip() else "", tried=tried)


def _git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=check)


def verify(repo, base, patch, branch, message, check=CHECK, timeout=1800):
    """Apply patch to a clean clone of repo at base, run check and the privacy scan; on success commit it and fetch
    the commit into repo as branch. Returns (verdict, detail): verdict is landed, no-change, no-apply, failed or
    private. The repository's own working tree is never touched."""
    if not patch.is_file() or not patch.read_bytes().strip():
        return "no-change", "the agent left no changes.patch"
    scratch = Path(tempfile.mkdtemp(prefix="rh-improve-"))
    work = scratch / "repo"
    try:
        _git(scratch, "clone", "--quiet", "--shared", "--no-checkout", str(repo), str(work))
        _git(work, "checkout", "--quiet", "--detach", base)
        applied = _git(work, "apply", "--index", "--whitespace=nowarn", str(patch.resolve()), check=False)
        if applied.returncode:
            return "no-apply", applied.stderr.strip()[-400:]
        try:
            ran = subprocess.run(["sh", "-c", check], cwd=work, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=timeout)
            code, tail = ran.returncode, (ran.stdout + ran.stderr).strip().splitlines()[-3:]
        except subprocess.TimeoutExpired:
            code, tail = 124, [f"timed out after {timeout} s"]
        if code:
            return "failed", f"`{check}` exited {code}: " + " | ".join(tail)
        scan = subprocess.run([sys.executable, "-I", str(_SCANNER), "--staged", "--repo", str(work)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if scan.returncode:
            return "private", (scan.stdout + scan.stderr).strip()[-400:]
        _git(work, "-c", "user.name=research-harness", "-c", "user.email=rh@localhost", "commit", "--quiet",
             "-m", message)
        _git(repo, "fetch", "--quiet", str(work), f"HEAD:refs/heads/{branch}")
        return "landed", f"{branch} at {_git(work, 'rev-parse', '--short=12', 'HEAD').stdout.strip()}; " + " | ".join(tail)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _title(answer):
    for line in answer.splitlines():
        line = line.strip().lstrip("#").strip().strip("*").strip()
        if line:
            return line[:120]
    return "(no answer)"


def run(cfg, rounds=3, model=FUSION, focus="", timeout_min=60, call=None, sleep=time.sleep, out=print):
    """Run the loop for up to `rounds` rounds; returns the list of round records."""
    from rh import cli, serve  # the composer's own job builder and the CLI the app calls
    call = call or (lambda argv: cli.main(argv))
    home, repo = Path(cfg["home"]), Path(cfg["repo"]["path"])
    log = home / "improve.jsonl"
    history = []
    if log.is_file():
        history = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    records = []
    for n in range(1, rounds + 1):
        base = _git(repo, "rev-parse", "HEAD").stdout.strip()
        jid = f"improve-{time.strftime('%Y%m%d-%H%M%S')}"
        draft = {"id": jid, "kind": "local-agent", "brief": brief(focus, history), "command": "", "accept": "",
                 "timeout_min": str(timeout_min), "model": model, "effort": "", "input_path": "", "answer": "chat",
                 "needs": []}
        job, text = serve._job_file(draft, str(repo))
        if call(["job", "add", "--", str(serve._upload(cfg, job, ".toml", text))]):
            raise ValueError(f"round {n}: the harness refused the job")
        out(f"round {n}: {jid} on {model}, base {base[:12]}")
        led = Ledger(home / "ledger.db")
        try:
            deadline = time.time() + timeout_min * 60 + 300
            while (state := (led.job(jid) or {}).get("state")) not in TERMINAL:
                if time.time() > deadline:
                    raise ValueError(f"round {n}: {jid} still {state} after {timeout_min} min")
                call(["supervise", "--once"])  # refused while `rh serve` or `rh supervise` holds the lock: it ticks
                sleep(5)
        finally:
            led.close()
        runs = home / "runs" / jid
        answer = (runs / "answer.md").read_text(encoding="utf-8", errors="replace") if (runs / "answer.md").is_file() else ""
        title = _title(answer)
        if state == "done":
            verdict, detail = verify(repo, base, runs / "changes.patch", f"improve/{jid}",
                                     f"{title}\n\nProposed by the improve loop (job {jid}, {model}); tests and the "
                                     f"privacy scan passed on a clean clone of {base[:12]}.")
        else:
            verdict, detail = f"job-{state}", "see the job page"
        record = {"job": jid, "model": model, "base": base, "title": title, "verdict": verdict, "detail": detail,
                  "ended": time.time()}
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        history.append(record)
        records.append(record)
        out(f"round {n}: {verdict}: {title} ({detail})")
    return records
