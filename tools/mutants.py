"""Run one-line mutants in disposable copies of the tracked working tree."""
import argparse
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib


def _run_tests(copy, tests, timeout):
    return subprocess.run([sys.executable, "-I", "-B", "tests/run.py", *tests], cwd=copy, timeout=timeout,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def run_mutant(repo, files, mutant, timeout, passed=None):
    """KILLED, SURVIVED, or ValueError. The named tests must pass on the unmutated copy first (once per list, cached
    in `passed`): a renamed or missing test fails on its own, and that failure is not a kill."""
    relative = Path(mutant["file"])
    if relative.is_absolute() or ".." in relative.parts or relative not in files:
        raise ValueError("mutant file must be a tracked repository path")
    find, replacement, tests = mutant["find"], mutant["replace"], mutant["tests"]
    if not isinstance(find, str) or not find or not isinstance(replacement, str):
        raise ValueError("find must be nonempty text and replace must be text")
    if not isinstance(tests, list) or not tests or not all(isinstance(t, str) and t for t in tests):
        raise ValueError("tests must be a nonempty list of test names")
    with tempfile.TemporaryDirectory(prefix="rh-mutants-") as directory:
        copy = Path(directory)
        for path in files:
            source = repo / path
            if not source.resolve().is_relative_to(repo):
                raise ValueError("tracked file points outside repository")
            destination = copy / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        if passed is None or tuple(tests) not in passed:
            try:
                clean = _run_tests(copy, tests, timeout)
            except subprocess.TimeoutExpired:
                raise ValueError("the named tests time out without the mutant") from None
            if clean:
                raise ValueError("the named tests fail without the mutant")
            if passed is not None:
                passed.add(tuple(tests))
        target = copy / relative
        original = target.read_bytes()
        needle = find.encode("utf-8")
        if original.count(needle) != 1:
            raise ValueError("find must occur exactly once")
        target.write_bytes(original.replace(needle, replacement.encode("utf-8"), 1))
        try:
            return "KILLED" if _run_tests(copy, tests, timeout) else "SURVIVED"
        except subprocess.TimeoutExpired:
            return "KILLED"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("list", type=Path)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a positive finite number")
    try:
        repo = args.repo.resolve()
        with args.list.open("rb") as stream:
            mutants = tomllib.load(stream)["mutant"]
        if not isinstance(mutants, list) or not mutants:
            raise ValueError("list must contain at least one mutant")
        tracked = subprocess.run(["git", "ls-files", "-z"], cwd=repo, check=True, capture_output=True)
        files = [Path(os.fsdecode(p)) for p in tracked.stdout.split(b"\0") if p]
        killed, passed = 0, set()
        for mutant in mutants:
            name = mutant.get("name", "<unnamed>") if isinstance(mutant, dict) else "<invalid>"
            try:
                status = run_mutant(repo, files, mutant, args.timeout, passed)
            except (OSError, ValueError, KeyError, TypeError) as error:
                status = "ERROR"
                print(f"{name}: {error}", file=sys.stderr)
            print(f"{status}  {name}", flush=True)
            killed += status == "KILLED"
        print(f"MUTANTS KILLED n={killed}/{len(mutants)}")
        return 0 if killed == len(mutants) else 1
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"ERROR  {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
