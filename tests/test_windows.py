"""Native Windows coverage for detached local jobs, host readings, and leases."""
import ctypes
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from ctypes import wintypes
from unittest import mock

from rh import config, contract, lease
from rh.adapters import local_shell
from rh.ledger import Ledger
from rh.supervisor import Supervisor
from tests.test_lease import RUNNER, STEP, lines, wait_for

ROOT = Path(__file__).resolve().parents[1]


def taskkill(pid, tree=False):
    if not pid:
        return
    command = ["taskkill", "/PID", str(pid)]
    if tree:
        command.append("/T")
    command.append("/F")
    subprocess.run(command, capture_output=True, timeout=15,
                   creationflags=subprocess.CREATE_NO_WINDOW)


@unittest.skipUnless(os.name == "nt", "native Windows behavior")
class LocalShellTest(unittest.TestCase):
    def test_detached_runner_writes_output_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, cwd = Path(tmp) / "job", Path(tmp) / "work"
            command = [sys.executable, "-c",
                       "import time; from pathlib import Path; time.sleep(.2); "
                       "Path('result.txt').write_text('finished'); raise SystemExit(23)"]
            pid = local_shell.start(run_dir, cwd, command)
            try:
                self.assertTrue(local_shell._alive(pid))
                self.assertTrue(wait_for(lambda: (run_dir / "exit").is_file(), 10))
                self.assertEqual(local_shell.poll(run_dir, pid), {"state": "exited", "exit": 23})
                self.assertEqual((cwd / "result.txt").read_text(), "finished")
            finally:
                if local_shell._alive(pid):
                    taskkill(pid, tree=True)


@unittest.skipUnless(os.name == "nt", "native Windows behavior")
class WindowsHostTest(unittest.TestCase):
    def test_read_host_returns_finite_load_and_pressure_level(self):
        before = time.time()
        reading = lease.read_host()
        self.assertIn(reading.level, (1, 2, 4))
        self.assertGreaterEqual(reading.load1, 0.0)
        self.assertLess(reading.load1, float("inf"))
        self.assertTrue(before <= reading.taken <= time.time())

    def test_windows_host_pressure_boundaries_and_cpu_sample(self):
        for percent, expected_level in ((79, 1), (80, 2), (89, 2), (90, 4)):
            with self.subTest(memory_percent=percent):
                calls = iter(((100, 200, 300), (110, 230, 310)))
                kernel = mock.Mock()

                def system_times(*pointers):
                    for pointer, value in zip(pointers, next(calls)):
                        filetime = ctypes.cast(pointer, ctypes.POINTER(wintypes.FILETIME)).contents
                        filetime.dwLowDateTime = value & 0xFFFFFFFF
                        filetime.dwHighDateTime = value >> 32
                    return 1

                def memory_status(pointer):
                    values = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))
                    values[1] = percent
                    return 1

                kernel.GetSystemTimes.side_effect = system_times
                kernel.GlobalMemoryStatusEx.side_effect = memory_status
                with mock.patch("ctypes.WinDLL", return_value=kernel), mock.patch.object(lease.time, "sleep"):
                    reading = lease._read_windows_host()
                self.assertEqual(reading.level, expected_level)
                self.assertAlmostEqual(reading.load1, os.cpu_count() * 0.75)
                self.assertGreaterEqual(reading.load1, 0.0)

    def test_windows_host_api_failure_is_reported(self):
        kernel = mock.Mock()
        kernel.GetSystemTimes.return_value = 0
        with mock.patch("ctypes.WinDLL", return_value=kernel), \
                mock.patch("ctypes.WinError", return_value=OSError("sample failed")), \
                self.assertRaisesRegex(lease.HostReadError, "Windows host reading: sample failed"):
            lease._read_windows_host()


@unittest.skipUnless(os.name == "nt", "native Windows behavior")
class LeaseInheritanceTest(unittest.TestCase):
    def runner(self, lock, what, step, out, seconds):
        return subprocess.Popen([sys.executable, "-I", "-c", RUNNER, str(ROOT), str(lock), what,
                                 step, str(out), str(seconds)])

    def test_step_inherits_lease_after_runner_dies_until_step_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock, step, held_out, wait_out = root / "heavy.lock", root / "step.py", root / "held", root / "waiter"
            step.write_text(STEP)
            holder = waiter = None
            step_pid = None
            waiter_step_pid = None
            try:
                holder = self.runner(lock, "holder", str(step), held_out, 30)
                self.assertTrue(wait_for(lambda: lines(held_out), 10), "holder step never started")
                step_pid = int(lines(held_out)[0].split()[0])
                self.assertTrue(lease.status(lock)["held"])
                waiter = self.runner(lock, "waiter", str(step), wait_out, 0.2)
                time.sleep(0.3)
                self.assertFalse(wait_out.exists(), "waiter started while the holder ran")

                taskkill(holder.pid)
                holder.wait(10)
                time.sleep(0.3)
                self.assertTrue(wait_for(lambda: lease.status(lock)["held"], 5),
                                "lease ended with the runner rather than its inherited step handle")
                self.assertFalse(wait_out.exists(), "waiter started while the holder step ran")

                taskkill(step_pid, tree=True)
                self.assertTrue(wait_for(lambda: lines(wait_out), 10), "waiter did not start after the step died")
                waiter_step_pid = int(lines(wait_out)[0].split()[0])
                self.assertEqual(waiter.wait(15), 0)
                self.assertTrue(wait_for(lambda: not lease.status(lock)["held"], 5))
            finally:
                for pid in (step_pid, waiter_step_pid):
                    if local_shell._alive(pid):
                        taskkill(pid, tree=True)
                for process in (holder, waiter):
                    if process is not None and process.poll() is None:
                        taskkill(process.pid, tree=True)
                    if process is not None:
                        process.wait(timeout=15)


@unittest.skipUnless(os.name == "nt", "native Windows behavior")
class WindowsSupervisorTest(unittest.TestCase):
    def test_local_job_runs_to_done_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg = config.load(home)
            ledger = Ledger(Path(cfg["home"]) / "ledger.db")
            spec = contract.normalize({
                "id": "windows-local",
                "kind": "local-shell",
                "brief": "Write a local output.",
                "command": [sys.executable, "-c",
                            "from pathlib import Path; Path('result.txt').write_text('done')"],
            })
            ledger.add_job(spec)
            ledger.set_state(spec["id"], "ready")
            supervisor = Supervisor(
                ledger, cfg, cloud=None,
                caps=contract.load_caps(ROOT / "caps.toml"),
                policy=contract.load_policy(ROOT / "policy.toml"),
                send=lambda *args: {"ok": True, "exit": 0, "out": "", "ts": 0},
            )
            try:
                end = time.monotonic() + 15
                while time.monotonic() < end and ledger.job(spec["id"])["state"] not in (
                        "done", "failed", "blocked", "held"):
                    supervisor.tick()
                    time.sleep(0.05)
                self.assertEqual(ledger.job(spec["id"])["state"], "done", ledger.job(spec["id"]))
                receipt = ledger.receipt(spec["id"])
                self.assertTrue(any(check["exit"] == 0 for check in receipt["checks"]))
                self.assertEqual((Path(cfg["home"]) / "runs" / spec["id"] / "result.txt").read_text(), "done")
            finally:
                ledger.close()
