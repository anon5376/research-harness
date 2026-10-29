"""Local-shell worker. The supervisor starts a detached runner that executes the job's command in its write root
and writes the exit code atomically, so a supervisor restart neither kills the job nor loses its result.

    python3 -I rh/adapters/local_shell.py exec --dir RUN_DIR --cwd CWD [--lock L --max-load X --max-wait S] -- CMD...
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def start(run_dir, cwd, command, heavy=False, lease_cfg=None, python=sys.executable):
    """Start the runner detached (own session) and return its pid."""
    run_dir, cwd = Path(run_dir), Path(cwd)
    run_dir.mkdir(parents=True, exist_ok=True)
    cwd.mkdir(parents=True, exist_ok=True)
    for stale in ("exit", "exit.tmp"):
        (run_dir / stale).unlink(missing_ok=True)
    argv = [python, "-I", str(Path(__file__).resolve()), "exec", "--dir", str(run_dir), "--cwd", str(cwd)]
    if heavy:
        lc = lease_cfg or {}
        argv += ["--lock", str(lc["lock"]), "--max-load", str(lc.get("max_load", 10.0)),
                 "--max-wait", str(lc.get("max_wait", 600))]
    argv += ["--", *command]
    with open(run_dir / "out.log", "ab") as log:
        p = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True)
    (run_dir / "runner.json").write_text(json.dumps({"pid": p.pid, "argv": command}))
    return p.pid


def poll(run_dir, pid):
    """{'state': 'exited', 'exit': n} | {'state': 'running'} | {'state': 'vanished'} (dead, no exit record)."""
    f = Path(run_dir) / "exit"
    if f.is_file():
        return {"state": "exited", "exit": int(f.read_text().strip())}
    if _alive(pid):
        return {"state": "running"}
    return {"state": "exited", "exit": int(f.read_text().strip())} if f.is_file() else {"state": "vanished"}


def tail(run_dir, lines=5):
    p = Path(run_dir) / "out.log"
    return p.read_text(errors="replace").splitlines()[-lines:] if p.is_file() else []


def _alive(pid):
    if not pid:
        return False
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)  # reap our own child if it exited
        if done == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _exec(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="local_shell exec")
    ap.add_argument("--dir", required=True)
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--lock")
    ap.add_argument("--max-load", type=float, default=10.0)
    ap.add_argument("--max-wait", type=float, default=600)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if a.lock:
        sys.path.insert(0, str(_ROOT))
        from rh import lease
        code = _run_in(a.cwd, lambda: lease.run(cmd, "local-shell", lock_path=Path(a.lock), max_load=a.max_load,
                                                 max_wait=a.max_wait))
    else:
        code = _run_in(a.cwd, lambda: subprocess.run(cmd).returncode)
    tmp = Path(a.dir) / "exit.tmp"
    tmp.write_text(str(code))
    tmp.replace(Path(a.dir) / "exit")
    return 0


def _run_in(cwd, fn):
    os.chdir(cwd)
    try:
        return fn()
    except FileNotFoundError:
        return 127


if __name__ == "__main__":
    if sys.argv[1:2] == ["exec"]:
        sys.exit(_exec(sys.argv[2:]))
    sys.exit("usage: local_shell.py exec --dir D --cwd C [--lock L ...] -- CMD...")
