# M7c: desktop app, packaging and the release build

**Goal.** Every `v*` tag publishes an Apple silicon .dmg, an Intel .dmg and a Windows .exe that open `rh serve` (M7b) in a native window. The builds are unsigned.

**Approved dependencies (founder, 30 Oct).**
- PyInstaller.
- pywebview, with pyobjc on macOS and pythonnet on Windows.
- Only in `desktop/` and the CI build. The `rh` package stays standard library: nothing under `rh/` may import them.
- Transitive dependencies: list each one in `runs/m7c-desktop/DEPS.md` with its licence. If any licence is not permissive (MIT, BSD, Apache-2.0, ISC, PSF, MPL-2.0, or GPL with an exception like PyInstaller's), stop and put the name and the licence in RESULT.json `refusals`.

**Files you write:**
- `desktop/app.py`
- `desktop/requirements.txt`
- `desktop/rh-desktop.spec`
- `docs/releases/TEMPLATE.md`
- `.github/workflows/build.yml`
- `tests/test_desktop.py`

Nothing else.

**Interface (exact).**
```python
# desktop/app.py
def main(argv: list[str] | None = None) -> int:
    """Start rh serve (rh.serve.make_server(cfg, port=0)) in a daemon thread, then open one window,
    "Research Harness", 1100x760, minimum 375x600, on its URL. Shut the server down when the window closes.
    Return 0. --smoke: start the server, GET the page with its token via urllib, print "smoke ok <status>"
    and return 0 if the status is 200 (else 1), without importing webview."""
```
- `import webview` happens inside the window path only, so `--smoke` and the tests run without pywebview installed.
- `RH_HOME` is the CLI's own default.

**`desktop/requirements.txt`.** Every line is pinned (`==`) with `--hash=sha256:`, using markers: pyobjc only on `sys_platform == "darwin"`, pythonnet only on `sys_platform == "win32"`. Include the hashes of the wheels the three runners install: macOS arm64, macOS x86_64 and Windows amd64, for Python 3.12. CI installs with `pip install --require-hashes -r desktop/requirements.txt`.

**`desktop/rh-desktop.spec`.**
- Entry point: `desktop/app.py`. Name: `Research Harness`. Windowed, with `rh/` bundled.
- macOS: a `BUNDLE` .app, with bundle identifier `io.github.research-harness`.
- Windows: one-file. If `desktop/icons/rh.icns` and `rh.ico` exist, use them; otherwise no icon. Claude adds the icons later.

**`.github/workflows/build.yml`** follows the build.yml shape of a sibling project:
- Triggers: `workflow_dispatch` and `push: tags: ["v*"]`.
- Jobs `macos-arm64` (runs-on `macos-latest`), `macos-x64` (`macos-13`) and `windows` (`windows-latest`). Each one:
  1. `git config --global core.autocrlf false`, checkout, and setup-python 3.12.
  2. `python -I tests/run.py`.
  3. On a tag, fail unless `python -I bin/rh --version` prints `rh <tag without v>`.
  4. `pip install --require-hashes -r desktop/requirements.txt`, then `pyinstaller desktop/rh-desktop.spec`.
  5. Run the frozen binary with `--smoke`.
  6. Package it. macOS: `hdiutil create -volname "Research Harness" -srcfolder "dist/Research Harness.app" -format UDZO -ov ResearchHarness-<ver>-macos-<arm64|x64>.dmg`. Windows: the exe renamed to `ResearchHarness-<ver>-windows-x64.exe`.
  7. `actions/upload-artifact`, with `if-no-files-found: error`.
- Job `release`:
  - Runs only for tags, after all three build jobs, with `permissions: contents: write`.
  - Downloads the three files.
  - If the release exists, `gh release upload "$TAG" <files> --clobber`. Otherwise `gh release create "$TAG" <files> --title "research-harness <ver>" --notes-file docs/releases/$TAG.md`.
- No secrets beyond `github.token`, and no signing steps.

**`docs/releases/TEMPLATE.md`** has three sections:
- What you can now do.
- Commands.
- Tests: the count, from `tests/run.py`.

Then the unsigned-build note:
- macOS: System Settings → Privacy & Security → "Open Anyway", or `xattr -dr com.apple.quarantine "/Applications/Research Harness.app"`.
- Windows: SmartScreen → More info → Run anyway. The window needs the Edge WebView2 Runtime, which Windows 11 includes.

No hostnames.

**Tests that must pass** (`tests/test_desktop.py`, unittest, no third-party imports):
1. `python -I desktop/app.py --smoke` with a temp `RH_HOME` exits 0 and prints `smoke ok 200`, with pywebview absent.
2. Every non-comment line of `desktop/requirements.txt` has `==` and at least one `--hash=sha256:`. pyobjc lines carry the darwin marker, and pythonnet lines carry the win32 marker.
3. No file under `rh/` imports `webview`, `PyInstaller`, `objc` or `clr`.
4. `build.yml` names the four jobs, `--require-hashes`, `--smoke`, `hdiutil create` and the version check, and contains no `secrets.` other than `github.token`.
5. `python3 -I tests/run.py` passes as a whole.

**Network.** This job needs the network, for `pip download` and for hashing the wheels. Use it only for PyPI.

**Do not change** `rh/` or any test outside `tests/test_desktop.py`.
