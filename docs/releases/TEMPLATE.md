## What you can now do

Open the live Research Harness board in a native desktop window.

## Commands

Download the package for your platform. On macOS, mount the disk image and copy
Research Harness.app to Applications. On Windows, run the downloaded executable.

For a headless check, run the executable with `--smoke`.

## Tests

Record the exact `TESTS PASSED n=<count>` line from `python -I tests/run.py`.

These builds are not signed with a developer certificate (ad-hoc on macOS, unsigned on Windows).

- macOS: System Settings → Privacy & Security → "Open Anyway", or
  `xattr -dr com.apple.quarantine "/Applications/Research Harness.app"`.
- Windows: SmartScreen → More info → Run anyway. The window needs the Edge
  WebView2 Runtime, which Windows 11 includes.
