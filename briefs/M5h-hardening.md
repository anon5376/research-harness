# M5h: hardening from the v0 review

**Goal.** Close the eight review findings that v0 left open, without changing any interface. The driver already fixed the five that could duplicate cloud tasks, stop the supervisor or leak private strings: unknown-outcome launches, restart mid-launch, per-job isolation, relative `RH_HOME`, and the privacy scan in `land`. Lands before M6c and M6d, which also change `rh/supervisor.py`.

**Files you write:** `rh/contract.py`, `rh/protocol.py`, `rh/collect.py`, `rh/supervisor.py`, `rh/cli.py`, `tests/test_hardening.py`. Change only what each item names; the diff is reviewed line by line. No existing test may change.

**Items (each needs at least one test in `tests/test_hardening.py` that fails without the change).**
1. **Operator verbs slip past preflight.** In `contract.operator_steps`:
   - Split clauses on commas and on "then" as well as on `.;:!?` and newlines.
   - A negation cancels a verb only when it is within the three words directly before it.
   - Add verbs for: plain `push` (not "push notification"), `create|submit` a pull request, `email|message|dm` followed by a determiner or pronoun, and `log into`.
   - `preflight` also scans a local-shell job's `command` (joined with spaces) and its `accept` with the same verbs.

   Tests: these must be operator steps:
   - "Fix the test that does not pass, then push the branch to origin"
   - "Commit and push your changes"
   - "Create a pull request with the fix"
   - "Email the summary to the reviewers"
   - "Log into the dashboard"
   - `command = ["sh", "-c", "make docs && git push origin main"]`

   These stay ready: "Do not push.", "Never open a pull request.", "Read the push notification docs."
2. **A refusal ends up retryable.** In `collect.collect_cloud`, when `protocol.parse_result` raises ResultError whose `.refusals` is non-empty, the state is `blocked` with those refusals in the receipt. The supervisor files them in the inbox exactly as for a valid blocked result.
3. **Notification flood.** A blocked job sends exactly one notification: its finish. Refusals go to the inbox without their own notification. A job whose brief has N operator steps sends one notification for that job, not N.
4. **Custom cloud write_root always fails custody.** `contract.normalize` raises ContractError for a `codex-cloud` job whose `write_root` is not `runs/<id>/`, with the message "cloud jobs write to runs/<id>/; put other paths in write_paths".
5. **One transient diff error waits on a human forever.** A diff RouteUnavailable with reason `error` is retried on the next 3 ticks. After the 3rd consecutive failure the job is marked harvest-needed, as today. Count the tries in the job's `detail` ("diff error 2/3: ...") so the count survives a restart.
6. **Gates can freeze the supervisor.** In `Supervisor._gate`:
   - Run a heavy gate with `max_wait=5` for the lease. On exit 75 the job stays `collecting` with detail "gate waiting for the heavy lease" and is retried next tick, never failed.
   - Every gate, heavy or not, has a timeout of `timeout_min` minutes. A timeout gives exit 124.
7. **Races with the CLI.**
   - In `Supervisor._poll_cloud` and `_alive`, re-read the job before writing a state. If the state changed since the tick read it (for example `rh cloud harvest` moved it to `collecting`), skip the write.
   - `rh cloud harvest` writes the patch to a temp file and renames it into place.
8. **Small items.**
   - `JOB_ID_RE` in both modules also rejects ids that contain `..`, end in `.` or end in `.lock`, because git refuses them as branch names.
   - A local job whose command exited 75 (lease refused) ends `capacity`, not `failed`.
   - `rh` prints `rh: <error>` and exits 1 for OSError (a missing patch file, a deleted worktree) instead of a traceback.
   - Add `rh job cancel <id> --reason TEXT`: a job in draft, waiting, held, ready or handoff ends `cancelled` with a receipt `{route: "none", checks: [], model_requested, model_served: None, ended, detail: reason}`. Any other state exits 1 with its state named.

**Tests that must pass:** every item above, plus `python3 -I tests/run.py` as a whole, plus the driver's mutation checks (`.unlazy` is local to the lead and is not in the repo; the lead re-runs them).

**Do not change** job states or transitions, receipt keys, the prompt's pinned line, the stop and pause rules, or any test outside `tests/test_hardening.py`.
