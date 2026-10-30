# M7 plan: desktop builds (.dmg and .exe on every release)

**Answer.** Ship one window app: a frozen Python app that opens `rh serve` in a native web view. Every `v*` tag builds a macOS .dmg (Apple silicon and Intel) and a Windows .exe on GitHub runners and attaches them to the release. The workflow copies the `build.yml` shape of a sibling project: tag or manual trigger, one job per OS, artifacts, then a release job with `gh release create --notes-file docs/releases/<tag>.md`. Tauri is not reused: it would add Rust and Node and would still need a frozen Python sidecar.

**Blocking fact.** `rh` does not run on Windows today. `fcntl` (supervisor lock, heavy-step lease), `start_new_session`, `sysctl` and `os.getloadavg` are POSIX only. `local_shell`'s liveness check `os.kill(pid, 0)` would *terminate* the process on Windows. Acceptance gates run `sh -c`, which on Windows exists only through Git for Windows. So M7 starts with a portability step.

## Steps

| Step | Owner | Builds | Needs |
|---|---|---|---|
| M7a | Codex, follow-up in task A after M5h, before M6c | `rh/oslock.py` (fcntl on POSIX, msvcrt on Windows) behind the existing `single_instance` and `lease` calls. Local-shell jobs and the lease's host reading say "POSIX only" on Windows instead of crashing. `ci.yml` runs `tests/run.py` on ubuntu, macos and windows on each push; POSIX-only tests skip with a reason. | m5h landed |
| M7b | Codex, new long-lived task D "desktop" | `rh/serve.py` plus an `rh serve` command, stdlib only. It serves 127.0.0.1 on a random port with a per-launch token. GET `/` shows the dashboard (m6a `render()`) with action forms. POST covers inbox resolve, stop/resume, cloud attach, harvest (patch or bundle text pasted into a textarea) and land. No JavaScript: plain forms and a meta refresh. It works in any browser without the desktop app. | m6a landed |
| M7c | Codex, follow-up in task D | `desktop/app.py`, which starts `rh serve` in a thread and opens it in a window. Plus `desktop/rh-desktop.spec`, the PyInstaller spec. Plus `build.yml`: a tag or manual run builds an arm64 .dmg on macos-latest and an x64 .dmg on macos-13 (both with `hdiutil`, built into macOS) and a `--onefile --windowed` .exe on windows-latest. It runs the suite first on each runner, then the release job. | M7a, M7b, the dependency yes |
| M7d | Claude | The look of the app screens, within the M7b interface, carrying over the m6a design pass. App icon as .icns and .ico, from a stdlib script. Release-note template with the unsigned note. | M7b |

Briefs fix every interface: module and function signatures, routes and form fields, file paths, workflow job names and artifact names. Each lists the tests. Each Codex task: GPT-6.1 Sol, Extra High, fast **off**, Full access, goal mode. The policy gains speed `standard`, which becomes the default.

## Dependencies to approve (founder)

The `rh` core stays standard library, and `rh serve` needs nothing. Only `desktop/` and the CI build install these, pinned with hashes in `desktop/requirements.txt`:
- **PyInstaller**: GPL-2.0-or-later with the bootloader exception, so the built app may ship under our licence. It freezes Python into the .app and .exe.
- **pywebview**: BSD-3-Clause. It draws the native window: WKWebView on macOS, Edge WebView2 on Windows. Its backends are **pyobjc** on macOS and **pythonnet** on Windows, both MIT.

Without pywebview, the app opens `rh serve` in the default browser instead (stdlib `webbrowser`). That build still needs PyInstaller.

## Release text (unsigned, until the founder buys signing)

- macOS: the first launch is blocked. Open System Settings → Privacy & Security → "Open Anyway". Or run `xattr -dr com.apple.quarantine "/Applications/Research Harness.app"`.
- Windows: SmartScreen shows "Windows protected your PC". Click More info → Run anyway. The window needs the Edge WebView2 Runtime, which Windows 11 includes.

## M7 done when

- A pushed tag `v0.N.0` produces a release with two .dmg files and one .exe.
- The ubuntu, macos and windows CI jobs are green, with the same test count except for the reported POSIX-only skips.
- On this Mac, the arm64 .dmg mounts, the app opens in a window, it shows the live ledger read-only, and one action (inbox resolve on a temp `RH_HOME`) works.
- The Windows build is accepted on CI evidence only (a smoke run that starts the frozen exe headless), because no Windows machine is here.

## v1.0.0: the first stable, working, designed build; after it, changes go through PRs

v1.0.0 is the first release that passes every check below. Until then: local commits, the lead's privacy read, push to main. After it, every change goes on a feature branch with a pull request, and the lead reviews, privacy-reads and merges. Branch protection on main is a GitHub settings change, so it is a founder step unless the founder hands it to the lead.

- **Stable**
  - CI is green on ubuntu, macos and windows at the tagged commit. The counts match except for the named POSIX-only skips.
  - The supervisor runs for 24 h on this Mac over a live ledger: at least one cloud wave and one local job, at least one `kill -9` and restart, zero `harness-error` inbox items, and no traceback in its log.
  - The M5h review items, M6c pacing and M6d wake have all landed with their tests.
  - Every one-line mutant in the mutant runs is killed.
- **Working**
  - On this Mac, the installed .dmg app takes one web-route job through its forms alone: add, prompt, attach, harvest (one by patch, one by bundle), collect, land. The job ends `done` on a `cloud/<job>` branch.
  - On Windows, the CI smoke run starts the frozen .exe headless, and `rh serve` answers `/` with 200 when given the token and 403 without it.
  - `rh --version` equals the tag; a CI step fails the release otherwise.
- **Designed**
  - Claude's design pass covers the dashboard and every app screen.
  - Screenshots exist at 375 px and 1280 px, in light and dark.
  - At 375 px there is no horizontal scroll (scrollWidth ≤ clientWidth, measured in a browser).
  - Body text contrast is at least 4.5:1, computed.
  - The pages carry no scripts and no external URLs.
  - The founder looks at the screenshots and says yes. That one is a manual gate: "designed" is the founder's call. design-critic is not used, because it runs Astra.
- **Shipped**
  - The release carries two .dmg files, one .exe and the source archive.
  - Its notes come from `docs/releases/v1.0.0.md`, including the unsigned-build note. They say what a user can do and give the test count, with no hostnames.

## Risks

- Windows git and line endings in the tests: M7a sets `core.autocrlf=false` in test repos.
- Unsigned builds face Gatekeeper and SmartScreen friction; the release text covers it.
- The macos-13 Intel runner may be retired; then ship arm64 only and say so.
- PyInstaller one-file .exe files trip antivirus heuristics more often. If CI or users see that, switch to one-dir plus an Inno Setup installer, which is preinstalled on the Windows runner. That is no new dependency, but confirm it before relying on it.
