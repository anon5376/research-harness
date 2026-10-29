"""One send per episode through a locally configured command; the receipt is what the command returned."""
from __future__ import annotations

import subprocess
import time


def send(template, subject, body, run=subprocess.run, timeout=30, clock=time.time):
    """template is argv with {subject} and {body} placeholders. Returns {ok, exit, out, ts}."""
    if not template:
        return {"ok": False, "exit": None, "out": "no notify command configured", "ts": clock()}
    argv = [a.replace("{subject}", subject).replace("{body}", body) for a in template]
    try:
        r = run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        return {"ok": False, "exit": None, "out": f"notify command not found: {e.filename}", "ts": clock()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit": None, "out": f"notify command timed out after {timeout} s", "ts": clock()}
    out = (r.stdout or r.stderr or "").strip().splitlines()
    return {"ok": r.returncode == 0, "exit": r.returncode, "out": out[-1][:200] if out else "", "ts": clock()}
