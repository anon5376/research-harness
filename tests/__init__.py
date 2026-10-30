import os
from pathlib import Path
import shlex
import sys


def python_path(directory, env=None):
    """Give shell subprocesses python3 from the interpreter running this suite."""
    bin_dir = Path(directory) / "bin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "python3"
    shim.write_text("#!/bin/sh\nexec " + shlex.quote(Path(sys.executable).as_posix()) + ' "$@"\n',
                    encoding="utf-8", newline="\n")
    shim.chmod(0o755)
    env = dict(os.environ if env is None else env)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", os.defpath)
    return env
