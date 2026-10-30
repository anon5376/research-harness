"""Tests for rh/lease.py: admission, the flock lease, the status probe, and leased steps in real processes."""
import contextlib
import gc
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

if os.name != "nt":
    import fcntl

from rh import lease

ROOT = Path(__file__).resolve().parents[1]
_WINDOW_HANDLE_COUNT_WARMED = False

# A leased runner in its own process. argv: root, lock, what, then the step's argv after the interpreter.
RUNNER = (
    "import sys, time\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from rh import lease\n"
    "ok = lambda: lease.Reading(level=1, load1=0.0, taken=time.time())\n"
    "sys.exit(lease.run([sys.executable, *sys.argv[4:]], sys.argv[3], lock_path=sys.argv[2], max_load=99,\n"
    "                   max_wait=20, reader=ok, poll=0.05))\n"
)
# A step. argv: out, seconds. Appends "<pid> <start>", sleeps, appends "<end>".
STEP = (
    "import os, sys, time\n"
    "with open(sys.argv[1], 'a') as f: f.write('%d %r\\n' % (os.getpid(), time.time()))\n"
    "time.sleep(float(sys.argv[2]))\n"
    "with open(sys.argv[1], 'a') as f: f.write('%r\\n' % time.time())\n"
)
# A step that reports what it sees. argv: root, lock, out.
PROBE = (
    "import json, os, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from rh import lease\n"
    "st = lease.status(sys.argv[2])\n"
    "json.dump({'env': os.environ.get('RH_LEASE_HELD'), 'held': st['held'], 'holder': st['holder']},"
    " open(sys.argv[3], 'w'))\n"
    "sys.exit(7)\n"
)


def open_fds():
    global _WINDOW_HANDLE_COUNT_WARMED
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        if not _WINDOW_HANDLE_COUNT_WARMED:
            with tempfile.TemporaryDirectory() as tmp:
                fd = os.open(Path(tmp) / "warmup.lock", os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    if lease._spawn([sys.executable, "-c", "pass"], (fd,), dict(os.environ)) != 0:
                        raise AssertionError("Windows subprocess warmup failed")
                finally:
                    os.close(fd)
            _WINDOW_HANDLE_COUNT_WARMED = True
        gc.collect()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessHandleCount.restype = wintypes.BOOL
        count = wintypes.DWORD()
        if not kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count)):
            raise ctypes.WinError(ctypes.get_last_error())
        return count.value
    return len(os.listdir("/dev/fd"))


def wait_for(pred, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def lines(path):
    """Complete lines written so far, or [] before the file exists."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    return text.splitlines() if text.endswith("\n") else text.splitlines()[:-1]


class FakeClock:
    """Injected clock; sleep advances it and calls on_sleep(count)."""

    def __init__(self, on_sleep=None):
        self.now = 1000.0
        self.slept = []
        self.on_sleep = on_sleep

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
        if len(self.slept) > 500:
            raise AssertionError("wait loop never ended")
        if self.on_sleep:
            self.on_sleep(len(self.slept))


class Readings:
    """Injected reader that returns readings in order (the last one repeats) and counts calls."""

    def __init__(self, *items):
        self.items = list(items)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        item = self.items[min(self.calls, len(self.items)) - 1]
        if isinstance(item, Exception):
            raise item
        return item


def reading(level=1, load1=0.5):
    return lease.Reading(level=level, load1=load1, taken=time.time())


class LeaseCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.lock = self.tmp / "heavy.lock"
        self.side = self.tmp / "heavy.json"
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(lease.ENV_HELD, None)

    def foreign_hold(self):
        """Hold the lock the way another launcher would."""
        if os.name == "nt":
            f = os.fdopen(lease._try_lock(self.lock), "r+b")
        else:
            f = open(self.lock, "a")
            fcntl.flock(f, fcntl.LOCK_EX)
        self.addCleanup(f.close)
        return f

    def script(self, name, body):
        path = self.tmp / name
        path.write_text(body)
        return str(path)

    def run_quiet(self, cmd, **kw):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = lease.run(cmd, "test", lock_path=self.lock, **kw)
        return rc, err.getvalue()


class AdmitTest(unittest.TestCase):
    def test_missing_getloadavg_fails_closed(self):
        with mock.patch("sys.platform", "linux"), mock.patch.object(lease, "os") as host_os:
            del host_os.getloadavg
            with self.assertRaisesRegex(lease.HostReadError, "load average"):
                lease.read_host()

    def test_pressure_limit(self):
        self.assertTrue(lease.admit(reading(level=1), max_load=10)[0])
        self.assertTrue(lease.admit(reading(level=None), max_load=10)[0])
        self.assertTrue(lease.admit(reading(level=2), max_load=10, max_level=2)[0])
        for level in (2, 4):
            ok, why = lease.admit(reading(level=level), max_load=10)
            self.assertFalse(ok)
            self.assertEqual(why, f"memory pressure level {level} (limit 1)")

    def test_load_limit(self):
        self.assertTrue(lease.admit(reading(load1=10.0), max_load=10)[0])
        self.assertEqual(lease.admit(reading(load1=10.5), max_load=10), (False, "load 10.50 above 10.00"))
        self.assertFalse(lease.admit(reading(load1=float("nan")), max_load=10)[0])

    def test_both_refusals_named(self):
        ok, why = lease.admit(reading(level=4, load1=12.0), max_load=10)
        self.assertFalse(ok)
        self.assertIn("memory pressure level 4 (limit 1)", why)
        self.assertIn("load 12.00 above 10.00", why)


@unittest.skipIf(os.name == "nt", "POSIX only: sysctl scripts and load averages")
class ReadHostTest(LeaseCase):
    def fake_sysctl(self, body):
        path = self.tmp / "sysctl"
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
        return str(path)

    def test_darwin_reads_pressure_oid(self):
        args = self.tmp / "args"
        fake = self.fake_sysctl(f'printf "%s\\n" "$@" > "{args}"; echo 2')  # dash's echo eats -n
        before = time.time()
        with mock.patch("sys.platform", "darwin"):
            r = lease.read_host(sysctl=fake)
        self.assertEqual(r.level, 2)
        self.assertEqual(args.read_text().split(), ["-n", "kern.memorystatus_vm_pressure_level"])
        self.assertIsInstance(r.load1, float)
        self.assertGreaterEqual(r.load1, 0.0)
        self.assertTrue(before <= r.taken <= time.time())

    def test_darwin_fails_closed(self):
        cases = {
            "denied": "echo 'sysctl: Operation not permitted' >&2; exit 1",
            "garbage": "echo high",
            "empty": "true",
        }
        for name, body in cases.items():
            with self.subTest(name), mock.patch("sys.platform", "darwin"):
                with self.assertRaises(lease.HostReadError) as cm:
                    lease.read_host(sysctl=self.fake_sysctl(body))
                if name == "denied":
                    self.assertIn("Operation not permitted", str(cm.exception))
        with self.subTest("missing"), mock.patch("sys.platform", "darwin"):
            with self.assertRaises(lease.HostReadError):
                lease.read_host(sysctl=str(self.tmp / "no-such-sysctl"))

    def test_darwin_sysctl_timeout(self):
        fake = self.fake_sysctl("exec sleep 5")
        start = time.monotonic()
        with mock.patch("sys.platform", "darwin"), mock.patch.object(lease, "SYSCTL_TIMEOUT", 0.3):
            with self.assertRaises(lease.HostReadError):
                lease.read_host(sysctl=fake)
        self.assertLess(time.monotonic() - start, 2.5)

    def test_other_os_has_no_level(self):
        with mock.patch("sys.platform", "linux"):
            r = lease.read_host(sysctl=str(self.tmp / "no-such-sysctl"))
        self.assertIsNone(r.level)
        self.assertGreaterEqual(r.load1, 0.0)


class AcquireTest(LeaseCase):
    def test_creates_parent_and_records_holder(self):
        lock = self.tmp / "a" / "b" / "heavy.lock"
        before = time.time()
        fd = lease.acquire(lock, "build", max_wait=0)
        try:
            side = json.loads((lock.parent / "heavy.json").read_text())
            self.assertEqual({k: side[k] for k in ("pid", "what", "by")},
                             {"pid": os.getpid(), "what": "build", "by": "research-harness"})
            self.assertTrue(before <= side["since"] <= time.time())
            st = lease.status(lock)
            self.assertTrue(st["held"])
            self.assertEqual(st["holder"], side)
        finally:
            os.close(fd)
        self.assertEqual(lease.status(lock), {"held": False, "holder": None, "age_s": None})

    def test_times_out_and_leaves_sidecar_alone(self):
        self.foreign_hold()
        clock = FakeClock()
        fds = open_fds()
        with self.assertRaises(lease.LeaseTimeout):
            lease.acquire(self.lock, "build", max_wait=1.0, poll=0.2, clock=clock, sleep=clock.sleep)
        self.assertAlmostEqual(sum(clock.slept), 1.0)
        self.assertFalse(self.side.exists(), "sidecar written without holding the lock")
        self.assertEqual(open_fds(), fds)

    def test_waits_until_holder_releases(self):
        holder = self.foreign_hold()
        clock = FakeClock(on_sleep=lambda n: n == 2 and holder.close())
        fd = lease.acquire(self.lock, "build", max_wait=10, poll=0.2, clock=clock, sleep=clock.sleep)
        os.close(fd)
        self.assertEqual(len(clock.slept), 2)


class StatusTest(LeaseCase):
    def test_missing_lock_is_free_and_not_created(self):
        self.assertEqual(lease.status(self.lock), {"held": False, "holder": None, "age_s": None})
        self.assertFalse(self.lock.exists())

    def test_stale_sidecar_is_not_trusted(self):
        self.lock.touch()
        self.side.write_text(json.dumps({"pid": 1, "what": "old", "since": time.time(), "by": "research-harness"}))
        self.assertEqual(lease.status(self.lock), {"held": False, "holder": None, "age_s": None})

    def test_foreign_holder_without_sidecar(self):
        self.foreign_hold()
        self.assertEqual(lease.status(self.lock), {"held": True, "holder": None, "age_s": None})

    def test_age_from_sidecar(self):
        fd = lease.acquire(self.lock, "build", max_wait=0)
        self.addCleanup(os.close, fd)
        side = json.loads(self.side.read_text())
        self.side.write_text(json.dumps({**side, "since": time.time() - 100}))
        age = lease.status(self.lock)["age_s"]
        self.assertTrue(100 <= age < 105, age)

    @unittest.skipIf(os.name == "nt", "POSIX only: flock and /dev/fd")
    def test_probe_releases_at_once(self):
        self.lock.touch()
        fds = open_fds()
        lease.status(self.lock)
        self.assertEqual(open_fds(), fds)
        with open(self.lock) as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises BlockingIOError if the probe kept the lock


class RunTest(LeaseCase):
    def test_step_gets_lease_and_env_and_cleanup_follows(self):
        out = self.tmp / "probe.json"
        fds = open_fds()
        rc, _ = self.run_quiet([sys.executable, self.script("probe.py", PROBE), str(ROOT), str(self.lock), str(out)],
                               max_load=10, max_wait=5, reader=Readings(reading()))
        self.assertEqual(rc, 7)
        seen = json.loads(out.read_text())
        self.assertEqual(seen["env"], "1")
        self.assertTrue(seen["held"])
        self.assertEqual((seen["holder"]["pid"], seen["holder"]["what"]), (os.getpid(), "test"))
        self.assertFalse(self.side.exists(), "sidecar left after the step")
        self.assertFalse(lease.status(self.lock)["held"])
        self.assertEqual(open_fds(), fds)

    @unittest.skipIf(os.name == "nt", "POSIX only: Unix signal exit codes")
    def test_signal_death_is_128_plus_n(self):
        rc, _ = self.run_quiet([sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"],
                               max_load=10, max_wait=5, reader=Readings(reading()))
        self.assertEqual(rc, 128 + signal.SIGTERM)

    def test_missing_command_is_127_and_releases(self):
        fds = open_fds()
        rc, err = self.run_quiet([str(self.tmp / "no-such-step")], max_load=10, max_wait=5, reader=Readings(reading()))
        self.assertEqual(rc, 127)
        if os.name == "nt":
            self.assertIn("cannot find the file", err.lower())
        else:
            self.assertIn("no-such-step", err)
        self.assertFalse(lease.status(self.lock)["held"])
        self.assertFalse(self.side.exists())
        self.assertEqual(open_fds(), fds)

    def test_refusal_times_out_with_tempfail_and_never_runs(self):
        cases = {
            "memory pressure level 2 (limit 1)": reading(level=2),
            "load 12.00 above 10.00": reading(load1=12.0),
            "sandbox denied the read": lease.HostReadError("sandbox denied the read"),
        }
        for reason, item in cases.items():
            with self.subTest(reason):
                marker = self.tmp / "ran"
                reader, clock, fds = Readings(item), FakeClock(), open_fds()
                rc, err = self.run_quiet([sys.executable, "-c", f"open({str(marker)!r}, 'w')"], max_load=10,
                                         max_wait=2.0, poll=0.5, reader=reader, clock=clock, sleep=clock.sleep)
                self.assertEqual(rc, lease.TEMPFAIL)
                self.assertFalse(marker.exists(), "refused step ran")
                self.assertIn(reason, err)
                self.assertAlmostEqual(sum(clock.slept), 2.0)
                self.assertEqual(reader.calls, len(clock.slept) + 1, "not a fresh reading per attempt")
                self.assertFalse(lease.status(self.lock)["held"], "lock kept after refusal")
                self.assertEqual(open_fds(), fds)

    def test_admits_on_a_later_reading(self):
        marker = self.tmp / "ran"
        reader, clock = Readings(reading(level=2), reading(level=2), reading(level=1)), FakeClock()
        rc, _ = self.run_quiet([sys.executable, "-c", f"open({str(marker)!r}, 'w')"], max_load=10, max_wait=60,
                               poll=0.5, reader=reader, clock=clock, sleep=clock.sleep)
        self.assertEqual(rc, 0)
        self.assertTrue(marker.exists())
        self.assertEqual(reader.calls, 3)

    def test_foreign_holder_blocks_until_released(self):
        holder = self.foreign_hold()
        marker = self.tmp / "ran"
        clock = FakeClock(on_sleep=lambda n: n == 3 and holder.close())
        rc, _ = self.run_quiet([sys.executable, "-c", f"open({str(marker)!r}, 'w')"], max_load=10, max_wait=60,
                               poll=0.5, reader=Readings(reading()), clock=clock, sleep=clock.sleep)
        self.assertEqual(rc, 0)
        self.assertTrue(marker.exists())
        self.assertEqual(len(clock.slept), 3)

    def test_foreign_holder_times_out_without_sidecar(self):
        self.foreign_hold()
        marker = self.tmp / "ran"
        clock = FakeClock()
        rc, err = self.run_quiet([sys.executable, "-c", f"open({str(marker)!r}, 'w')"], max_load=10, max_wait=1.0,
                                 poll=0.5, reader=Readings(reading()), clock=clock, sleep=clock.sleep)
        self.assertEqual(rc, lease.TEMPFAIL)
        self.assertIn("lease held", err)
        self.assertFalse(marker.exists())
        self.assertFalse(self.side.exists(), "sidecar written without holding the lock")

    def test_reentrant_when_lease_already_held(self):
        fd = lease.acquire(self.lock, "outer", max_wait=0)
        self.addCleanup(os.close, fd)
        os.environ[lease.ENV_HELD] = "1"
        rc, _ = self.run_quiet([sys.executable, "-c", "raise SystemExit(5)"], max_load=10, max_wait=0,
                               reader=Readings(reading()))
        self.assertEqual(rc, 5)

    def test_stale_held_env_still_takes_the_lease(self):
        os.environ[lease.ENV_HELD] = "1"
        out = self.tmp / "probe.json"
        rc, _ = self.run_quiet([sys.executable, self.script("probe.py", PROBE), str(ROOT), str(self.lock), str(out)],
                               max_load=10, max_wait=5, reader=Readings(reading()))
        self.assertEqual(rc, 7)
        self.assertTrue(json.loads(out.read_text())["held"], "stale RH_LEASE_HELD skipped the lease")


class ProcessTest(LeaseCase):
    """Leased steps in real processes; the kernel, not the test, decides who holds the lock."""

    def setUp(self):
        super().setUp()
        self.step = self.script("step.py", STEP)
        self.orphans = set()
        self.addCleanup(self.kill_orphans)

    def kill_orphans(self):
        for pid in self.orphans:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)

    def runner(self, what, out, seconds):
        p = subprocess.Popen([sys.executable, "-I", "-c", RUNNER, str(ROOT), str(self.lock), what,
                              self.step, str(out), str(seconds)])
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        return p

    def test_two_runs_never_overlap(self):
        outs = [self.tmp / "a", self.tmp / "b"]
        procs = [self.runner(f"run{i}", out, 0.4) for i, out in enumerate(outs)]
        self.assertEqual([p.wait(20) for p in procs], [0, 0])
        spans = sorted((float(lines(o)[0].split()[1]), float(lines(o)[1])) for o in outs)
        self.assertGreaterEqual(spans[1][0], spans[0][1], f"leased steps overlapped: {spans}")
        self.assertFalse(lease.status(self.lock)["held"])
        self.assertFalse(self.side.exists())

    @unittest.skipIf(os.name == "nt", "POSIX only: SIGKILL process-tree behavior")
    def test_lease_follows_step_and_kill9_frees_waiter(self):
        held_out, wait_out = self.tmp / "held", self.tmp / "waiter"
        holder = self.runner("holder", held_out, 20)
        self.assertTrue(wait_for(lambda: lines(held_out), 10), "holder step never started")
        step_pid = int(lines(held_out)[0].split()[0])
        self.orphans.add(step_pid)
        st = lease.status(self.lock)
        self.assertTrue(st["held"])
        self.assertEqual((st["holder"]["pid"], st["holder"]["what"]), (holder.pid, "holder"))

        waiter = self.runner("waiter", wait_out, 0.1)
        time.sleep(0.4)
        self.assertFalse(wait_out.exists(), "waiter started while the holder ran")

        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(5)
        time.sleep(0.4)
        self.assertTrue(lease.status(self.lock)["held"], "lease ended with the runner, not the step")
        self.assertFalse(wait_out.exists(), "waiter started while the holder's step ran")

        os.kill(step_pid, signal.SIGKILL)
        killed = time.time()
        self.orphans.discard(step_pid)
        self.assertTrue(wait_for(lambda: lines(wait_out), 5), "waiter never started after kill -9")
        self.assertLess(float(lines(wait_out)[0].split()[1]) - killed, 2.0)
        self.assertEqual(waiter.wait(10), 0)
        self.assertFalse(lease.status(self.lock)["held"])


if __name__ == "__main__":
    unittest.main()
