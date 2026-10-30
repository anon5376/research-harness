"""Desktop packaging contracts, without third-party imports."""
import ast
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


class DesktopTest(unittest.TestCase):
    def test_smoke_without_site_packages(self):
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run(
                [sys.executable, "-I", "-S", "desktop/app.py", "--smoke"],
                cwd=ROOT, env={**os.environ, "RH_HOME": home},
                capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "smoke ok 200")

    def test_window_and_shutdown(self):
        spec = importlib.util.spec_from_file_location("desktop_app", ROOT / "desktop/app.py")
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        addresses = []

        def create_window(title, url, **options):
            self.assertEqual(title, "Research Harness")
            self.assertEqual(options, {"width": 1100, "height": 760, "min_size": (375, 600)})
            with urlopen(url, timeout=5) as response:
                self.assertEqual(response.status, 200)
            parsed = urlsplit(url)
            addresses.append((parsed.hostname, parsed.port))

        fake = types.SimpleNamespace(create_window=create_window, start=lambda: None)
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, RH_HOME=home), \
                patch.dict(sys.modules, webview=fake):
            self.assertEqual(app.main([]), 0)
        self.assertEqual(len(addresses), 1)
        with socket.socket() as sock:
            self.assertNotEqual(sock.connect_ex(addresses[0]), 0)

    def test_requirements_are_hashed_and_platform_scoped(self):
        lines = (ROOT / "desktop/requirements.txt").read_text().splitlines()
        requirements = [line for line in lines if line.strip() and not line.startswith("#")]
        self.assertTrue(requirements)
        for line in requirements:
            self.assertIn("==", line)
            self.assertIn("--hash=sha256:", line)
            if line.lower().startswith("pyobjc"):
                self.assertIn('sys_platform == "darwin"', line)
            if line.lower().startswith("pythonnet"):
                self.assertIn('sys_platform == "win32"', line)

    def test_core_has_no_desktop_imports(self):
        forbidden = {"webview", "PyInstaller", "objc", "clr"}
        for path in (ROOT / "rh").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                self.assertFalse(forbidden.intersection(m.split(".")[0] for m in modules), str(path))

    def test_workflow_contract(self):
        workflow = (ROOT / ".github/workflows/build.yml").read_text()
        spec = (ROOT / "desktop/rh-desktop.spec").read_text()
        for text in ("macos-arm64:", "macos-x64:", "windows:", "release:",
                     "--require-hashes", "--smoke", "hdiutil create",
                     "bin/rh --version", '"rh ${TAG#v}"',
                     "PYINSTALLER_TARGET_ARCH: arm64",
                     "PYINSTALLER_TARGET_ARCH: x86_64"):
            self.assertIn(text, workflow)
        self.assertIn('os.environ.get("PYINSTALLER_TARGET_ARCH")', spec)
        self.assertNotIn("secrets.", workflow.replace("secrets.github.token", ""))
        self.assertNotIn("gh release create", workflow)
        for text in ("gh release upload", "--clobber", "SECONDS + 1800",
                     "shasum -a 256", "Get-FileHash -Algorithm SHA256",
                     ".sha256", "codesign -dv", "Signature=adhoc", "NotSigned"):
            self.assertIn(text, workflow)


if __name__ == "__main__":
    unittest.main()
