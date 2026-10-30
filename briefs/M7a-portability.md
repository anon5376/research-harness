# M7a: rh runs on Windows, and CI proves it on three systems

**Goal.** The test suite passes on Ubuntu, macOS and Windows, so the desktop builds (M7c) ship a core that works there. On Windows, the POSIX-only features fail with a clear message, never a crash or a wrong action.

**Files you write.**
- New: `rh/oslock.py`, `tests/test_oslock.py`, `tests/fakes/codex.cmd`, `.github/workflows/ci.yml`.
- Changed:
  - `rh/supervisor.py` (`single_instance` only);
  - `rh/lease.py` (`status`, `_try_lock`);
  - `rh/adapters/local_shell.py` (`start`, `_alive`);
  - `rh/collect.py` (`run_gate` only);
  - test files, but only to add skips and the Windows fake-CLI switch.
- Nothing else.

**Interface (exact).**
```python
# rh/oslock.py
class Locked(Exception):
    """Another process holds the lock."""

def try_lock(path) -> int:
    """Open or create path, take an exclusive non-blocking lock, return the fd. Raise Locked if it is held."""

def unlock(fd: int) -> None:
    """Release the lock and close the fd."""
```
- POSIX uses `fcntl.flock(fd, LOCK_EX | LOCK_NB)`. Windows uses `msvcrt.locking(fd, LK_NBLCK, 1)` on byte 0. The platform is chosen once, at import.
- `single_instance` and the lease's two lock sites call `try_lock` and `unlock`. Their behaviour and messages on POSIX stay exactly as they are; every existing test stays green unchanged.

**Behaviour on Windows (`os.name == "nt"`).**
- `local_shell.start` raises `NotImplementedError("local-shell jobs run on macOS and Linux only")`. The supervisor's per-job isolation parks the job; no change is needed there.
- `local_shell._alive` never calls `os.kill`, because `os.kill(pid, 0)` terminates the process on Windows. It returns False there.
- `collect.run_gate` runs the gate with `shutil.which("sh")`, which Git for Windows provides. Without `sh` it returns 127 and prints `acceptance gates need sh (Git for Windows)` to stderr.
- `lease.read_host` raising `HostReadError` is the existing behaviour off macOS. Keep it.
- Tests that need POSIX-only behaviour skip on Windows with `unittest.skipIf(os.name == "nt", "POSIX only: <reason>")`: signals, `start_new_session`, sysctl, `sh -c` gates where Git's sh is absent, and symlinks. Never skip a test on macOS or Linux.
- `tests/fakes/codex.cmd` runs `tests/fakes/codex` with the current Python. Tests pick it when `os.name == "nt"`.
- Test repositories set `core.autocrlf false`.

**`.github/workflows/ci.yml`.**
- Triggers: `push` and `pull_request`.
- Matrix: `os: [ubuntu-latest, macos-latest, windows-latest]` × `python: ["3.11", "3.13"]`.
- Steps:
  1. `git config --global core.autocrlf false`
  2. checkout
  3. setup-python
  4. `python -I tests/run.py`
- No secrets, no caches, no other actions.

**Tests that must pass** (in `tests/test_oslock.py` unless stated):
1. While a child process (`python -c` using `rh.oslock`) holds `try_lock(p)`, `try_lock(p)` in the test raises `Locked`. After the child calls `unlock`, it succeeds.
2. `single_instance` still raises `AlreadyRunning` for a second holder. The existing supervisor test covers this; keep it.
3. Patch `os.name` to `"nt"` around the Windows paths: `_alive` returns False without calling `os.kill` (assert by patching `os.kill` to fail the test), and `local_shell.start` raises the NotImplementedError with the message above.
4. `python3 -I tests/run.py` passes as a whole on this Mac, with no new skips on macOS.

**Do not change** any interface, CLI output, state name or message outside the Windows paths above.
