"""Portable locks and Windows guards, with real child-process exclusion tests."""
import contextlib
import errno
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from rh import collect, config, contract, lease, supervisor
from rh.ledger import Ledger
from rh.adapters import local_shell

ROOT = Path(__file__).resolve().parents[1]


class NativeLockTest(unittest.TestCase):
    def test_child_excludes_another_holder_until_it_explicitly_unlocks(self):
        from rh import oslock
        with tempfile.TemporaryDirectory() as tmp:
            lock, marker = Path(tmp) / 'exclusive.lock', Path(tmp) / 'ready'
            script = (
                'import os, sys, threading\n'
                'from pathlib import Path\n'
                'sys.path.insert(0, sys.argv[1])\n'
                'from rh.oslock import try_lock, unlock\n'
                'fd = try_lock(sys.argv[2])\n'
                'watchdog = threading.Timer(15, lambda: os._exit(3))\n'
                'watchdog.daemon = True\n'
                'watchdog.start()\n'
                'Path(sys.argv[3]).write_text("held")\n'
                'try:\n'
                '    sys.stdin.readline()\n'
                'finally:\n'
                '    unlock(fd)\n'
                'Path(sys.argv[3]).write_text("released")\n'
                'sys.stdin.readline()\n'
                'watchdog.cancel()\n'
            )
            child = subprocess.Popen([sys.executable, '-I', '-B', '-c', script, str(ROOT), str(lock), str(marker)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            def wait_for(want):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if marker.is_file() and marker.read_text() == want:
                        return
                    if child.poll() is not None:
                        self.fail('lock child exited early: ' + child.communicate()[1])
                    time.sleep(0.01)
                self.fail('lock child did not report ' + want)
            try:
                wait_for('held')
                with self.assertRaises(oslock.Locked):
                    unexpected_fd = oslock.try_lock(lock)
                    oslock.unlock(unexpected_fd)
                self.assertTrue(lease.status(lock)['held'])
                child.stdin.write('unlock\n')
                child.stdin.flush()
                wait_for('released')
                self.assertIsNone(child.poll(), 'explicit unlock, rather than process exit, must release the lock')
                fd = oslock.try_lock(lock)
                oslock.unlock(fd)
                self.assertFalse(lease.status(lock)['held'])
                _, err = child.communicate('exit\n', timeout=10)
                self.assertEqual(child.returncode, 0, err)
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=10)

    def test_unlock_closes_the_fd_and_preserves_existing_contents(self):
        from rh import oslock
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'contents.lock'
            path.write_bytes(b'existing lock contents')
            fd = oslock.try_lock(path)
            os.lseek(fd, 5, os.SEEK_SET)
            oslock.unlock(fd)
            with self.assertRaises(OSError):
                os.fstat(fd)
            self.assertEqual(path.read_bytes(), b'existing lock contents')
            fd = oslock.try_lock(path)
            oslock.unlock(fd)

    def test_missing_parent_is_an_oserror_rather_than_contention(self):
        from rh import oslock
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                oslock.try_lock(Path(tmp) / 'missing' / 'lock')


class IntegrationTest(unittest.TestCase):
    def test_single_instance_rejects_second_holder_and_releases(self):
        from rh import oslock
        with tempfile.TemporaryDirectory() as tmp:
            fd = supervisor.single_instance(tmp)
            try:
                with self.assertRaisesRegex(supervisor.AlreadyRunning, 'another supervisor holds .*pid unknown'):
                    supervisor.single_instance(tmp)
            finally:
                oslock.unlock(fd)
            fd = supervisor.single_instance(tmp)
            oslock.unlock(fd)

    def test_lease_probe_does_not_create_a_missing_lock_or_keep_a_free_one(self):
        from rh import oslock
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'lease.lock'
            self.assertEqual(lease.status(path), {'held': False, 'holder': None, 'age_s': None})
            self.assertFalse(path.exists())
            fd = lease._try_lock(path)
            self.assertIsInstance(fd, int)
            self.assertIsNone(lease._try_lock(path))
            oslock.unlock(fd)
            self.assertFalse(lease.status(path)['held'])
            fd = oslock.try_lock(path)
            oslock.unlock(fd)


class WindowsPathsTest(unittest.TestCase):
    def test_alive_never_probes_or_signals_a_windows_process(self):
        with mock.patch.object(os, 'name', 'nt'), \
                mock.patch.object(os, 'kill', side_effect=AssertionError('must not signal a Windows process')):
            self.assertFalse(local_shell._alive(os.getpid()))

    def test_start_refuses_windows_before_creating_any_run_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, cwd = Path(tmp) / 'job', Path(tmp) / 'work'
            with mock.patch.object(os, 'name', 'nt'), self.assertRaisesRegex(
                    NotImplementedError, '^local-shell jobs run on macOS and Linux only$'):
                local_shell.start(str(run_dir), str(cwd), [sys.executable, '-c', 'pass'])
            self.assertFalse(run_dir.exists())
            self.assertFalse(cwd.exists())

    def test_supervisor_parks_the_unsupported_windows_job_with_its_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.load(Path(tmp) / 'home')
            led = Ledger(Path(cfg['home']) / 'ledger.db')
            try:
                spec = contract.normalize({'id': 'windows-local', 'kind': 'local-shell', 'brief': 'Run a step.',
                                           'command': [sys.executable, '-c', 'pass']})
                led.add_job(spec)
                led.set_state(spec['id'], 'ready')
                sup = supervisor.Supervisor(led, cfg, caps={}, policy={},
                    send=lambda *args: {'ok': True, 'exit': 0, 'out': ''})
                with mock.patch.object(os, 'name', 'nt'):
                    sup._isolated(led.job(spec['id']), sup._launch_local)
                self.assertEqual(led.job(spec['id'])['state'], 'held')
                self.assertIn('NotImplementedError: local-shell jobs run on macOS and Linux only',
                              led.job(spec['id'])['detail'])
                self.assertFalse((Path(cfg['home']) / 'jobs').exists())
            finally:
                led.close()

    def test_gate_without_git_sh_returns_127_and_explains_the_dependency(self):
        err = io.StringIO()
        with mock.patch.object(os, 'name', 'nt'), mock.patch('shutil.which', return_value=None), \
                mock.patch.object(subprocess, 'run', side_effect=AssertionError('no shell should run')), \
                contextlib.redirect_stderr(err):
            self.assertEqual(collect.run_gate('exit 0', '.', timeout=1), 127)
        self.assertEqual(err.getvalue(), 'acceptance gates need sh (Git for Windows)\n')

    @unittest.skipIf(os.name == 'nt' and not shutil.which('sh'), 'POSIX only: gates need Git sh')
    def test_gate_uses_the_shell_found_on_path_and_the_requested_working_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            shell = shutil.which('sh')
            self.assertIsNotNone(shell)
            with mock.patch.object(os, 'name', 'nt'), mock.patch('shutil.which', return_value=shell):
                code = collect.run_gate("printf '%s' 'gate result' > result.txt", tmp, timeout=5)
            self.assertEqual(code, 0)
            self.assertEqual((Path(tmp) / 'result.txt').read_text(), 'gate result')

    def test_windows_gate_passes_the_discovered_shell_and_timeout(self):
        calls = []
        def run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 9)
        with mock.patch.object(os, 'name', 'nt'), \
                mock.patch('shutil.which', return_value='git-tools/sh.exe'), \
                mock.patch.object(subprocess, 'run', side_effect=run):
            self.assertEqual(collect.run_gate('exit 9', 'work dir', timeout=7), 9)
        self.assertEqual(calls, [(['git-tools/sh.exe', '-c', 'exit 9'], {'cwd': 'work dir', 'timeout': 7})])

    def test_windows_gate_timeout_still_returns_124(self):
        with mock.patch.object(os, 'name', 'nt'), mock.patch('shutil.which', return_value='git-tools/sh.exe'), \
                mock.patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('sh', 0.01)):
            self.assertEqual(collect.run_gate('sleep 5', '.', timeout=0.01), 124)


class BackendTest(unittest.TestCase):
    def backend(self, windows, locking):
        """Import once with a controlled native API, then restore os.name before calling it."""
        module_spec = importlib.util.spec_from_file_location('test_oslock_backend', ROOT / 'rh/oslock.py')
        module = importlib.util.module_from_spec(module_spec)
        if windows:
            api = types.SimpleNamespace(LK_NBLCK=10, LK_UNLCK=20, locking=locking)
            modules = {'msvcrt': api}
        else:
            api = types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=locking)
            modules = {'fcntl': api}
        with mock.patch.dict(sys.modules, modules), mock.patch.object(os, 'name', 'nt' if windows else 'posix'):
            module_spec.loader.exec_module(module)
        return module

    def test_windows_locks_and_unlocks_byte_zero_after_the_import_platform_is_restored(self):
        calls = []
        def locking(fd, mode, count):
            calls.append((mode, count, os.lseek(fd, 0, os.SEEK_CUR), os.fstat(fd).st_size))
        module = self.backend(True, locking)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'byte.lock'
            fd = module.try_lock(path)
            os.lseek(fd, 9, os.SEEK_SET)
            module.unlock(fd)
            self.assertEqual(calls, [(10, 1, 0, 1), (20, 1, 0, 1)])
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_posix_uses_nonblocking_exclusive_flock_and_explicit_unlock(self):
        calls = []
        module = self.backend(False, lambda fd, mode: calls.append(mode))
        with tempfile.TemporaryDirectory() as tmp:
            fd = module.try_lock(Path(tmp) / 'posix.lock')
            module.unlock(fd)
        self.assertEqual(calls, [6, 8])

    def test_contended_native_lock_raises_locked_and_closes_the_opened_fd(self):
        for windows in (False, True):
            with self.subTest(windows=windows), tempfile.TemporaryDirectory() as tmp:
                fds = []
                def contend(fd, *args):
                    fds.append(fd)
                    raise OSError(errno.EACCES if windows else errno.EAGAIN, 'fixture contention')
                module = self.backend(windows, contend)
                with self.assertRaises(module.Locked):
                    module.try_lock(Path(tmp) / 'busy.lock')
                self.assertEqual(len(fds), 1)
                with self.assertRaises(OSError):
                    os.fstat(fds[0])

    def test_unexpected_native_failure_is_not_misreported_as_contention(self):
        for windows in (False, True):
            with self.subTest(windows=windows), tempfile.TemporaryDirectory() as tmp:
                fds = []
                def fail(fd, *args):
                    fds.append(fd)
                    raise OSError(errno.EIO, 'fixture lock I/O error')
                module = self.backend(windows, fail)
                with self.assertRaises(OSError) as error:
                    module.try_lock(Path(tmp) / 'broken.lock')
                self.assertEqual(error.exception.errno, errno.EIO)
                with self.assertRaises(OSError):
                    os.fstat(fds[0])

    def test_failed_unlock_still_closes_the_fd(self):
        for windows in (False, True):
            with self.subTest(windows=windows), tempfile.TemporaryDirectory() as tmp:
                calls = []
                def locking(*args):
                    calls.append(args)
                    if len(calls) == 2:
                        raise OSError(errno.EIO, 'fixture unlock I/O error')
                module = self.backend(windows, locking)
                fd = module.try_lock(Path(tmp) / 'release.lock')
                with self.assertRaises(OSError):
                    module.unlock(fd)
                with self.assertRaises(OSError):
                    os.fstat(fd)


if __name__ == '__main__':
    unittest.main()
