"""Heavy-step lease: host admission (memory pressure, load) plus one exclusive flock per lock file.

Any launcher that flocks the same path shares the lease. `run` passes the locked fd to the step, so the
lease lasts exactly as long as the step and is freed by the kernel even after kill -9. The `<lock>.json`
sidecar names the holder; it is written only under the lock and never trusted for "held". Never delete the
lock file while it may be held: a new inode would admit a second holder.
"""
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from rh import oslock

TEMPFAIL = 75
ENV_HELD = "RH_LEASE_HELD"
BY = "research-harness"
PRESSURE_OID = "kern.memorystatus_vm_pressure_level"
SYSCTL_TIMEOUT = 5.0


@dataclass(frozen=True)
class Reading:
    """One host sample; level is None where the OS has no pressure reading."""
    level: int | None
    load1: float
    taken: float


class HostReadError(Exception):
    """The host could not be read; admission fails closed."""


class LeaseTimeout(Exception):
    """The lease was not acquired within max_wait."""


def read_host(sysctl="sysctl"):
    """Sample memory pressure (darwin only) and the 1-minute load average."""
    level = None
    if sys.platform == "darwin":
        try:
            proc = subprocess.run([sysctl, "-n", PRESSURE_OID], stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=SYSCTL_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            raise HostReadError(f"{PRESSURE_OID}: {e}") from e
        out = proc.stdout.decode(errors="replace").strip()
        if proc.returncode != 0:
            err = proc.stderr.decode(errors="replace").strip()
            raise HostReadError(f"{PRESSURE_OID}: exit {proc.returncode}: {err}")
        try:
            level = int(out)
        except ValueError:
            raise HostReadError(f"{PRESSURE_OID}: unreadable output {out!r}") from None
    try:
        load1 = os.getloadavg()[0]
    except OSError as e:
        raise HostReadError(f"load average: {e}") from e
    return Reading(level=level, load1=load1, taken=time.time())


def admit(reading, max_load, max_level=1):
    """Return (ok, reason); ok iff pressure is unknown or <= max_level, and load1 <= max_load."""
    refusals = []
    # `not a <= b` also refuses NaN.
    if reading.level is not None and not reading.level <= max_level:
        refusals.append(f"memory pressure level {reading.level} (limit {max_level})")
    if not reading.load1 <= max_load:
        refusals.append(f"load {reading.load1:.2f} above {max_load:.2f}")
    if refusals:
        return False, "; ".join(refusals)
    level = "n/a" if reading.level is None else reading.level
    return True, f"admitted: memory pressure level {level}, load {reading.load1:.2f}"


def acquire(lock_path, what, *, max_wait, poll=0.2, clock=time.monotonic, sleep=time.sleep):
    """Return an fd holding the lease, with `what` recorded in the sidecar; raise LeaseTimeout after max_wait.

    max_wait None waits forever. Closing the fd, or exiting, releases the lease.
    """
    lock_path = Path(lock_path)
    _sidecar(lock_path)
    for _ in _attempts(max_wait, poll, clock, sleep):
        fd = _try_lock(lock_path)
        if fd is not None:
            try:
                _write_sidecar(lock_path, what)
            except BaseException:
                os.close(fd)
                raise
            return fd
    raise LeaseTimeout(f"{what}: not acquired after {max_wait:g}s: {_held_reason(lock_path)}")


def run(cmd, what, *, lock_path, max_load, max_wait=None, reader=read_host, poll=0.5, clock=time.monotonic,
        sleep=time.sleep):
    """Run cmd under the lease once the host admits it; return its exit code, or TEMPFAIL after max_wait.

    The step inherits the locked fd and RH_LEASE_HELD=1. If RH_LEASE_HELD=1 is already set and the lease is
    held, cmd runs directly (re-entrant). A step killed by signal N returns 128+N, a missing command 127.
    """
    lock_path = Path(lock_path)
    _sidecar(lock_path)
    if os.environ.get(ENV_HELD) == "1" and status(lock_path)["held"]:
        return _spawn(cmd, (), None)
    for _ in _attempts(max_wait, poll, clock, sleep):
        fd, why = _admitted_lock(lock_path, reader, max_load)
        if fd is not None:
            break
    else:
        print(f"rh lease: {what}: not started after {max_wait:g}s: {why}", file=sys.stderr)
        return TEMPFAIL
    try:
        _write_sidecar(lock_path, what)
        try:
            return _spawn(cmd, (fd,), {**os.environ, ENV_HELD: "1"})
        finally:
            _sidecar(lock_path).unlink(missing_ok=True)
    finally:
        os.close(fd)


def status(lock_path):
    """Return {held, holder, age_s}; held comes from a LOCK_NB probe on a separate open, never the sidecar."""
    lock_path = Path(lock_path)
    try:
        lock_path.stat()
    except FileNotFoundError:
        return {"held": False, "holder": None, "age_s": None}
    try:
        fd = oslock.try_lock(lock_path)
    except oslock.Locked:
        held = True
    else:
        held = False
        oslock.unlock(fd)
    if not held:
        return {"held": False, "holder": None, "age_s": None}
    holder = _read_sidecar(lock_path)
    since = holder.get("since") if holder else None
    known = isinstance(since, (int, float)) and not isinstance(since, bool)
    return {"held": True, "holder": holder, "age_s": max(0.0, time.time() - since) if known else None}


def _attempts(max_wait, poll, clock, sleep):
    """Yield once per attempt, `poll` apart, until max_wait has passed (None: forever); always at least once."""
    deadline = None if max_wait is None else clock() + max_wait
    while True:
        yield
        left = None if deadline is None else deadline - clock()
        if left is not None and left <= 0:
            return
        sleep(poll if left is None else min(poll, left))


def _admitted_lock(lock_path, reader, max_load):
    """Return (fd, reason) when the lock is free and the host admits, else (None, refusal).

    The lock comes first and the host is read under it: no sysctl runs while a step holds the lease, and the
    reading that admits a step is taken while no other leased step can start.
    """
    fd = _try_lock(lock_path)
    if fd is None:
        return None, _held_reason(lock_path)
    try:
        ok, why = admit(reader(), max_load)
    except HostReadError as e:
        ok, why = False, f"host read failed: {e}"
    except BaseException:
        os.close(fd)
        raise
    if not ok:
        os.close(fd)
        return None, why
    return fd, why


def _try_lock(lock_path):
    """Return a new fd holding LOCK_EX on lock_path, or None if another open file holds it."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return oslock.try_lock(lock_path)
    except oslock.Locked:
        return None


def _spawn(cmd, fds, env):
    try:
        rc = subprocess.run(cmd, pass_fds=fds, env=env).returncode
    except FileNotFoundError as e:
        print(f"rh lease: {e}", file=sys.stderr)
        return 127
    except PermissionError as e:
        print(f"rh lease: {e}", file=sys.stderr)
        return 126
    return 128 - rc if rc < 0 else rc


def _sidecar(lock_path):
    side = lock_path.with_suffix(".json")
    if side == lock_path:
        raise ValueError(f"lock path must not end in .json: {lock_path}")
    return side


def _write_sidecar(lock_path, what):
    """Atomically record the holder; call only while holding the lock."""
    side = _sidecar(lock_path)
    tmp = side.with_name(f".{side.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps({"pid": os.getpid(), "what": what, "since": time.time(), "by": BY}) + "\n")
        os.replace(tmp, side)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read_sidecar(lock_path):
    try:
        data = json.loads(_sidecar(lock_path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _held_reason(lock_path):
    holder = _read_sidecar(lock_path)
    if holder and "pid" in holder:
        return f"lease held by pid {holder['pid']} ({holder.get('what', '?')})"
    return "lease held by another process"
