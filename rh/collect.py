"""Turn a finished worker into one typed end state and a receipt.

Cloud: diff -> scratch worktree at the pinned sha -> custody -> RESULT.json -> local acceptance gate.
Local: exit record -> acceptance gate."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from rh import gitx, protocol

MAX_TEXT = 300
_SCANNER = Path(__file__).resolve().parent / "privacy.py"


def receipt(job, route, checks, **extra):
    spec = job["spec"]
    out = {"route": route, "checks": checks, "model_requested": spec.get("model", {}).get("requested"),
           "model_served": None, "ended": None, "external_id": job.get("external_id"),
           "external_url": job.get("external_url"), "pinned_sha": job.get("pinned_sha"), "started": job.get("started")}
    out.update(extra)
    return out


def collect_cloud(job, cloud, repo, home, gate_runner=None):
    """Return (state, receipt). Never raises for worker faults; RouteUnavailable from the diff call propagates."""
    spec, sha, jid = job["spec"], job["pinned_sha"], job["id"]
    saved = saved_patch(home, jid)
    diff = saved.read_text() if saved.is_file() else cloud.diff(job["external_id"])
    paths = protocol.diff_paths(diff)
    base = dict(diff_sha256=hashlib.sha256(diff.encode()).hexdigest(), changed_paths=paths, model_served="unknown",
                diff_source="saved patch" if saved.is_file() else "codex cloud diff")
    bad = protocol.custody_violations(paths, [spec["write_root"], *spec["write_paths"]])  # before touching git
    if bad:
        return "failed", receipt(job, "codex-cloud", [], detail=_cut(f"custody: wrote outside its write root: {bad}"),
                                 **base)
    wt = Path(home) / "work" / jid
    try:
        gitx.worktree_at(repo, wt, sha)
        if diff.strip():
            gitx.apply(wt, diff)
    except gitx.GitError as e:
        return "failed", receipt(job, "codex-cloud", [], detail=_cut(f"diff does not apply at the pinned sha: {e}"),
                                 **base)
    base["worktree"] = str(wt)
    allowed = [spec["write_root"], *spec["write_paths"]]
    seen = gitx.staged(wt)
    late = protocol.custody_violations([p for _, p in seen], allowed)
    links = [p for mode, p in seen if mode in ("120000", "160000")]  # a symlink or submodule can point outside
    if late or links:
        return "failed", receipt(job, "codex-cloud", [], detail=_cut(
            f"custody: {'outside the write root: %s' % late if late else ''}"
            f"{'; ' if late and links else ''}{'symlink or submodule: %s' % links if links else ''}"), **base)
    rp = wt / protocol.result_path(jid)
    raw = rp.read_text() if rp.is_file() else None
    try:
        res = protocol.parse_result(raw, jid, sha)
    except protocol.ResultError as e:
        if e.refusals:
            return "blocked", receipt(job, "codex-cloud", [], detail=_cut("refused: " + "; ".join(e.refusals)),
                                      refusals=[_cut(r) for r in e.refusals], **base)
        state = "no_result" if e.kind == "missing" else "failed"
        detail = {"missing": "no RESULT.json in the task diff", "stale-input": f"stale input: {e}",
                  "wrong-job": f"RESULT.json names another job: {e}"}.get(e.kind, f"invalid RESULT.json: {e}")
        return state, receipt(job, "codex-cloud", [], detail=_cut(detail), **base)
    try:
        served = json.loads(raw).get("model")
    except (ValueError, AttributeError):
        served = None
    base["model_served"] = served or "unknown"
    checks = [dict(c, where="cloud") for c in res["checks"]]
    state, detail = res["state"], res["summary"]
    if state == "blocked":
        return "blocked", receipt(job, "codex-cloud", checks, detail=_cut("refused: " + "; ".join(res["refusals"])),
                                  refusals=[_cut(r) for r in res["refusals"]], summary=_cut(res["summary"]), **base)
    if state == "done" and spec.get("accept"):
        code = (gate_runner or run_gate)(spec["accept"], wt)
        checks.append({"cmd": spec["accept"], "exit": code, "where": "local"})
        if code != 0:
            state, detail = "failed", f"acceptance gate exited {code}"
    return state, receipt(job, "codex-cloud", checks, detail=_cut(detail), summary=_cut(res["summary"]),
                          outputs=res["outputs"], **base)


def saved_patch(home, job_id):
    """Where `rh cloud harvest` stores a patch saved from the web view, used instead of `codex cloud diff`."""
    return Path(home) / "diffs" / f"{job_id}.patch"


def collect_local(job, exit_code, cwd, log_tail=(), gate_runner=None):
    spec = job["spec"]
    checks = [{"cmd": " ".join(spec["command"]), "exit": exit_code, "where": "local"}]
    if exit_code != 0:
        return "capacity" if exit_code == 75 else "failed", receipt(
            job, "local-shell", checks, detail=_cut(f"command exited {exit_code}"), log_tail=list(log_tail))
    if spec.get("accept"):
        code = (gate_runner or run_gate)(spec["accept"], cwd)
        checks.append({"cmd": spec["accept"], "exit": code, "where": "local"})
        if code != 0:
            return "failed", receipt(job, "local-shell", checks, detail=f"acceptance gate exited {code}")
    return "done", receipt(job, "local-shell", checks, detail="command and gate exited 0", log_tail=list(log_tail))


def run_gate(command, cwd, timeout=1800):
    """The job author's acceptance command, run like CI: exit code is the verdict."""
    try:
        return subprocess.run(["sh", "-c", command], cwd=str(cwd), timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        return 124


def land(job, home):
    """Commit a done cloud job's applied diff on a local branch cloud/<job>. Never pushes."""
    wt = Path(home) / "work" / job["id"]
    if job["state"] != "done":
        raise gitx.GitError(f"{job['id']} is {job['state']}, only done jobs land")
    if not wt.is_dir():
        raise gitx.GitError(f"no worktree for {job['id']}; collect it first")
    scan = subprocess.run([sys.executable, "-I", str(_SCANNER), "--staged", "--repo", str(wt)],
                          capture_output=True, text=True)
    if scan.returncode != 0:
        raise gitx.GitError(f"privacy scan refused {job['id']}: " + (scan.stdout + scan.stderr).strip()[-300:])
    # no task id in the message: the privacy hook treats task ids as private
    return gitx.commit_branch(wt, f"cloud/{job['id']}", f"cloud job {job['id']} at {(job.get('pinned_sha') or '')[:12]}")


def _cut(s):
    s = str(s)
    return s if len(s) <= MAX_TEXT else s[:MAX_TEXT - 1] + "…"
