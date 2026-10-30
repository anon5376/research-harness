# Codex cloud briefs

One brief per remaining milestone. Claude keeps design, interfaces and review. Codex cloud tasks implement each brief exactly, and a diff that changes an interface named in a brief is rejected.

Each brief has a job contract in `jobs/` that the harness runs:
- `rh job add jobs/<id>.toml` pins the current commit, which must be on `origin/main`.
- `rh supervise` writes the prompt to `$RH_HOME/prompts/<id>.txt` with its sha256, then hands it to the web composer.
- `rh cloud attach <id> <task>` records the task.
- `rh cloud harvest <id> <patch>` hands over the staged diff when `codex cloud diff` cannot export it.
- The supervisor collects the diff, checks custody against the job's write paths, validates RESULT.json and runs the job's `accept` command locally.
- `rh land <id>` commits it on branch `cloud/<id>`. The lead reviews and merges.

| Brief | Job | Model, speed | Needs first |
|---|---|---|---|
| [M5 environment probe](M5-probe.md) | `probe-env-01` | Sol, extra-high, fast (ran 30 Oct) | environment published, Full access |
| [M5h hardening from the v0 review](M5h-hardening.md) | `m5h-hardening` | Sol, extra-high, fast (launched 30 Oct; Astra was blocked) | probe; lands before M6c and M6d |
| [M6a static dashboard](M6a-dashboard.md) | `m6a-dashboard` | Sol, extra-high, fast (launched 30 Oct) | probe done and imported |
| [M6b research goals and roles](M6b-goals.md) | `m6b-goals` | Sol, extra-high, fast (launched 30 Oct) | probe |
| [M6c paced cloud launches](M6c-pacing.md) | `m6c-pacing` | Sol, extra-high, standard; follow-up in the m5h task | probe |
| [M6d event wake](M6d-wake.md) | `m6d-wake` | Sol, extra-high, standard; follow-up in the m5h task | probe; lands after M5h and M6c (all touch `rh/supervisor.py`) |

Next milestone: desktop builds and the v1.0.0 definition of done, planned in [M7-desktop-plan.md](M7-desktop-plan.md); its briefs follow the lead's yes.

Rules every brief inherits (the harness prompt states them too):
- Python 3.11+ standard library only.
- Run `sh cloud/setup.sh` first and copy its output into `runs/<job>/SUMMARY.md`.
- Write only the files the brief lists, plus `runs/<job>/`.
- Every new test must fail if the behaviour it names is removed.
- Run `python3 -I tests/run.py`. It prints `TESTS PASSED n=<count>` only when the whole suite passed.
- Gates: before coding, write `runs/<job>/GATES.md` from `skills/unlazy/templates/gates-leaf.md`, one gate per "Tests that must pass" line. Run `node skills/unlazy/scripts/gate-lint.mjs runs/<job>/GATES.md`. If node is missing, say so in SUMMARY.md and keep GATES.md anyway.
- Leave the work staged and uncommitted.
