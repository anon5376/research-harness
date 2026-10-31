# research-harness: design

**Answer.** research-harness runs research work as typed jobs on agent workers. Its first worker is a Codex cloud task in an environment built from this repository. v0 is standard-library Python 3.11+, with no dependencies and no GUI. It has six parts:
- one SQLite ledger;
- a job contract checked before launch;
- one supervisor that owns launch, stop and pause;
- a heavy-step lease for local work;
- a Codex cloud adapter;
- a CLI, `rh`, that shows every job and an operator inbox.

The operator is the human owner. Operator-only steps (money, settings, publishing, sign-in) are never done by the harness. They are queued in the inbox with the exact action.

## Why it exists

A one-week audit of an agent fleet grouped its failures into 14 patterns: local and cloud Codex workers, interactive and background Claude sessions, and scheduled routines. The harness targets the patterns a control plane can change mechanically:

| Pattern | What went wrong | Harness mechanism | Milestone |
|---|---|---|---|
| P01 intent misread | Work built from a paraphrase; the mismatch was found at delivery | Operator-facing jobs need an acceptance card quoting the operator's own words. Preflight holds a job without one | M1 |
| P02 host overload | Each launcher judged load alone, so the machine nearly crashed | One heavy-step lease (flock) plus admission on memory pressure and load | M2 |
| P03 work sent where it cannot run | Briefs needed commit, sockets, sysctl or tools the worker lacked | A capability table per worker kind. Preflight holds a mismatch, and `unknown` means probe first | M1, M5 |
| P04 supervision inside chats | Restarts and sleeps killed watchers; dead work was found hours late | One supervisor process, heartbeats, and typed end states in one ledger | M2 |
| P05 controllers fight | Guards, watchers and the dispatcher acted on the same files; a launch loop retried for hours | The supervisor alone launches. Stops and pauses are owned, so only their owner lifts them. Backoff on route failure, and no stop writes | M2 |
| P06 denials not routed | Permission or classifier refusals stayed inside workers | A refusal ends the job as `blocked` with the exact text, and the inbox shows it. Never retried or reworded | M3 |
| P07 next step without proof | Steps started on "ran", not "succeeded with these inputs" | Inputs carry sha256 hashes. Cloud tasks pin a commit sha. Gates use exit codes | M1, M3 |
| P08 model policy drift | Several rule files of different ages; served model unknown | One dated policy file. Each receipt records the model requested and the model served (or `unknown`) | M1, M3 |
| P09 output custody | Unique outputs nearly deleted; workers wrote to the wrong folders | Each job has one write root. A cloud diff outside it fails the job | M1, M3 |
| P10 messages lost | Sends had no receipt; alerts flooded the lead | One notify call per episode, with a recorded receipt. A failed send goes to the inbox | M4 |
| P12 blind fan-out | Rate limits hit by unpaced creates | A limits table (route, last status, retry-after) that the supervisor reads before each launch | M2 |
| P13 versions without a changed input | New revisions with no named reason | Revision 2 or later needs `changed` and `prior`, or preflight holds it | M1 |

Not targeted in v0: P11 (tool health) gets only `rh doctor`. P14 (clock wakeups) waits for v1 event wake. P01 is only partly covered: a harness can require the card, but cannot write it or judge taste.

## What it does

1. **Contract.** A job is a TOML file. It names the worker kind, the brief, the capabilities it needs, its write root, its inputs with hashes, the jobs it runs after, a revision (with `changed` and `prior` when 2 or more), a local acceptance command, the model requested, and, for operator-facing work, an acceptance card.
2. **Preflight.** `contract.preflight(spec, caps, policy, view)` is a pure function. It returns `ready`, `wait <reason>` (inputs or upstream jobs not ready), `hold <reason>` (a conflict a person must fix), or `operator <step>`, when the brief asks for an operator-only verb such as push, publish, deploy, buy, send, sign in or change a setting. Each hold and operator step goes to the ledger, and operator steps also go to the inbox.
3. **Supervise.** `rh supervise` is the only launcher. A file lock keeps it to one instance. Each tick it:
   - reads owned stops and pauses;
   - re-runs preflight on ready jobs;
   - checks route backoff and limits;
   - takes the heavy lease for heavy local steps;
   - launches through an adapter;
   - polls running jobs;
   - writes a heartbeat.

   The clock is injectable, so tests can run three hours in milliseconds.
4. **Collect and gate.** For a finished cloud task, the supervisor:
   - fetches the diff;
   - applies it to a scratch git worktree at the pinned sha;
   - checks custody: every changed path must lie under the write root;
   - validates `runs/<job>/RESULT.json`;
   - runs the job's acceptance command, under the lease if the job is heavy.

   The job ends in one typed state with a receipt. A passing job can be landed on a local branch `cloud/<job>`. The harness never pushes.
5. **Report.** `rh status` is one view over all jobs: running, held, blocked with its text, the inbox, and the last 24 h by end state. `rh inbox` lists operator steps and decisions. Every terminal episode sends one notify through a command set in local config, and the receipt is stored.

## Architecture

```
rh (cli.py) ──reads/writes──► ledger.py  ($RH_HOME/ledger.db, SQLite WAL)
                                  ▲
supervisor.py (one process) ──────┤
   ├─ contract.py   preflight: spec + caps.toml + policy.toml + ledger view → ready | wait | hold | operator
   ├─ lease.py      host admission (pressure, load) + exclusive flock for heavy steps
   ├─ adapters/codex_cloud.py   codex cloud exec | status | diff  → typed results
   ├─ adapters/local_shell.py   run a command in the write root, exit code is the result
   ├─ collect.py    scratch worktree, custody check, RESULT.json, acceptance gate, receipt
   └─ notify.py     configured command, one per episode, receipt recorded
envspec.py   prints the cloud environment spec and builds the pinned task prompt
privacy.py   scans tracked files before every commit (public repo)
```

Runtime state lives in `$RH_HOME`, by default `~/.research-harness`, never in the repo: the ledger, local config, the notify command, the private deny list for the privacy scan, and scratch worktrees.

### Job states

`draft → ready | waiting | held → launching → (handoff →) running → collecting → done | failed | blocked | capacity | truncated | aborted | no_result | cancelled`, plus `handoff` (the web route: a browser-capable session must submit the prompt, then `rh cloud attach`) and `stalled` (no heartbeat or status change for the job's timeout; may still become `done` as a late finish). Pauses and stops are owned records that overlay any state. They never rewrite it. The ledger refuses any transition not in this table. Every terminal state needs a receipt: route, external id and URL, pinned sha, diff sha256, changed paths, checks with exit codes, model requested and served, and times.

### Codex cloud protocol

**Environment.** `rh env spec` prints what the operator enters at chatgpt.com/codex → Settings → Environments:
- the repository (from `git remote`);
- the name `research-harness`;
- the setup script `cloud/setup.sh`;
- agent internet access on, with `github.com` allowed for all methods. `git fetch` over HTTPS posts to `git-upload-pack`, so GET-only fails.

Creating and publishing the environment, and giving the Codex GitHub app access to the repo, are operator steps.

**Task prompt.** Every cloud task starts with a fixed prefix:

```
git fetch origin main && git checkout --detach <sha> && test "$(git rev-parse HEAD)" = "<sha>"
```

A published environment can serve a stale snapshot, so the pinned sha is the input proof. The prefix also tells the worker:
- write only under `runs/<job>/` and the declared paths;
- never push, open pull requests, send messages or notifications;
- on any refusal, stop and record its exact text;
- finish by writing `runs/<job>/RESULT.json`:

```json
{"job": "<id>", "state": "done|failed|blocked", "head": "<sha>", "summary": "...",
 "checks": [{"cmd": "...", "exit": 0}], "outputs": ["runs/<id>/..."], "refusals": ["<exact text>"]}
```

Preflight waits until the pinned sha is on `origin/main`, because the cloud fetches from GitHub.

**Launch route.** These were probed on 29 Oct 2026 with codex-cli 0.165.0:

| Route | Status | Use |
|---|---|---|
| `codex cloud exec --env <ENV_ID> --branch main <prompt>` | Official and authenticated. `--env` resolves on the client against the CLI's environment list (`/wham/environments` and `/wham/environments/by-repo/github/<owner>/<repo>`), by id or label; an id not in that list fails like a label (cloud-tasks `resolve_environment_id`, the same at 0.165.0 and on main on 31 Oct). A new-style environment is in neither list, so it answers `not found` by label or id, before anything is created | The native route, used whenever the CLI lists the environment |
| Web composer at chatgpt.com/codex | Proven on 29 Oct. Needs a signed-in browser session | Fallback, by choice: a ready job's page offers Launch by hand (`rh cloud handoff <job>`), and the hand-launch card says why. The harness hands a job off by itself only after a launch whose outcome is unknown or a restart mid-launch, since a task may already exist |
| `codex app-server` | No cloud-task methods in the 0.165.0 protocol schema. Its `environmentId` refers to exec-server environments | Not a route |
| Private backend HTTP API | Undocumented, and it would put the access token in our code | Rejected |

After launch, `codex cloud status <id>`, `codex cloud list --json` and `codex cloud diff <id>` worked programmatically on existing tasks. Tasks in new-style environments are invisible to the CLI: on 31 Oct `codex cloud status` and `codex cloud diff` answered `404 Invalid task ID` for one, and `codex cloud list` showed none of them. So while the CLI cannot see the environment, a cloud job waits `ready`, one operator-step item names the environment and quotes the CLI's answer, and the supervisor asks again with `codex cloud list --env <env>` (read-only) every 10 minutes; the item resolves itself once the CLI lists the environment. The CLI diff and the "copy git apply" button can also fail. So collection has a second source: a browser session saves the task's staged diff and runs `rh cloud harvest <job> <patch>`. The supervisor then collects it exactly like a CLI diff. A diff the CLI cannot export puts a `harvest` item in the inbox and is not counted as a route outage. The web Changes view expires a few hours after a task starts, so a web job still unharvested `cloud.changes_ttl_min` (120) minutes after it was attached gets an inbox item for the bundle-export follow-up (`rh/bundle.py`); `rh cloud harvest <job> --bundle <dir>` checks each part's sha256 and the whole's, rebuilds the staged diff in a scratch worktree at the pinned sha and saves it as the job's patch.

Every prompt is saved with its sha256, so the browser session can check that it pasted exactly the harness's text. Workers stage their write scope and leave it uncommitted. The working route is recorded per environment.

**Permission.** "Approve for me" leaves the task shell without network, so the pinned fetch fails. "Full access" routes through the environment allowlist but gives the shell a GitHub token. Allowing it for harness tasks is an operator step. Only harness-written prompts run there.

**Model.** `policy.toml` is dated and lists the allowed cloud models, currently GPT-6.1 Terra or Sol at standard speed and max effort. The CLI cannot be shown to set the cloud model, so receipts record `served: unknown` unless the task reports it.

## v0 scope

In v0:
- ledger and contract preflight;
- supervisor with owned stops, backoff and limits;
- heavy-step lease;
- local-shell adapter;
- Codex cloud adapter with collection, custody check and gate;
- environment spec and pinned prompt;
- `rh` with `job add|show`, `status`, `inbox`, `stop`, `resume`, `supervise`, `cloud prompt|attach|harvest`, `land`, `env spec`, `caps import`, `service plist`, `doctor` and `lease run`;
- notify with receipt;
- privacy scan.

Not in v0:
- goal planning, a role hierarchy, a dashboard, or a local Claude adapter;
- event wake;
- loading the supervisor as a launchd service. A plist is written but not loaded, because loading it is the lead's two-way step;
- self-improvement. Later it runs only with the operator's yes or an explicit setting, off by default;
- a small local model.

## Milestones (each check can fail)

**M0. Repo and privacy guard.**
- `python3 -I -m unittest discover -s tests -t .` passes.
- The scan fails on a fixture that contains a home-directory path, a non-noreply email, a token-shaped string and a term from the local deny file. It passes on the tracked tree.
- The pre-commit hook runs it.

**M1. Ledger and contract.** Unit tests show that preflight:
- holds a job that needs `commit` on a kind without it;
- holds on `unknown` capability unless the job is a probe;
- waits on a missing input or a hash mismatch, and on an upstream job that has not finished;
- holds revision 2 without `changed`;
- holds an operator-facing job with no card;
- holds a write root that overlaps a live job's;
- turns "push to origin" into an operator step but lets "never push" through;
- holds a model outside the policy.

The ledger refuses `done → running` and refuses a terminal state with no receipt.

**M2. Supervisor and lease.** On a fake clock:
- A route down for 180 min, with 3 ready jobs, gets 1 launch attempt while down, at most 40 health probes (backoff 1, 2, 4, then 5 min) and 0 stop writes. It launches within 5 min of recovery.
- A second supervisor exits non-zero within 2 s.
- A stop owned by A cannot be lifted by B.
- A paused job shows `paused`, not `stalled`.

For the lease:
- Two leased `sleep 2` steps do not overlap.
- After `kill -9` of the holder, the waiter starts within 2 s.
- A reading of pressure 2 refuses admission.

**M3. Codex cloud adapter, offline.** A fake `codex` on PATH covers these cases:
- Launch parses the task id.
- Every status value maps to a state. An unknown value stays `running` and logs an event.
- A diff that touches a path outside the write root ends `failed: custody`.
- A missing RESULT.json ends `no_result`.
- A RESULT.json whose `head` differs from the pinned sha ends `failed: stale input`.
- A refusal in RESULT.json ends `blocked` with the text in the inbox.
- `not found` for the environment keeps the job `ready` and writes one operator-step item for the environment; no task is created and no per-job launch card is written.

**M4. Run as a user.** In a temp `$RH_HOME`, use the real CLI:
- add a local-shell job and a cloud job against the fake `codex`;
- run `rh supervise --once` until both are terminal;
- `rh status` shows both with receipts;
- `rh inbox` shows the operator step from a push brief;
- the notify receipt is stored.

`rh doctor` against the real codex CLI reports its version and that `codex cloud list` answers.

**M5. Real cloud spawn.** This needs the operator steps and a pushed commit.
- One probe job runs in the research-harness environment, through the CLI route or the recorded web fallback.
- It reaches `done` with a receipt whose `head` equals the pinned sha.
- The diff touches only `runs/<probe>/`.
- The probe's capability readings (python version, network to github.com, git writable, loopback) fill `caps.toml` for the `codex-cloud` kind.

It fails on a wrong sha, a missing RESULT.json or a custody breach.

**M5h. Hardening from the v0 review (Codex).** An independent review found 13 defects. The five that could duplicate cloud tasks, stop the supervisor or leak private strings are fixed in v0. The other eight are specified in `briefs/M5h-hardening.md`, each with a test that fails without its fix.

**M6 (v1), as Codex briefs in `briefs/`.**
- A goal with a card is split into N cloud jobs, created at a measured pace under the limits.
- Results are collected and gated.
- `rh status --html` gives a static dashboard.
- A local Claude adapter is added.
- The supervisor wakes on events.

## Stop and revisit rules

After 7 days of real use, fix or revert, and build nothing on top, if any of these happens:
- more than 10 failed launches in an hour;
- the supervisor is down for more than 10 min with no recorded reason;
- more than 5 % of finished jobs end untyped or `stalled`;
- the operator finds a stall before the harness does.

Revisit the scope if more than 3 form or delivery corrections a week reach the operator. That is a card problem, not a code problem.

## Lessons carried in

- A GUI-first harness and a copy of another app were built and parked within a day. So the ledger and CLI come first, and a dashboard is a view of the ledger.
- A task board that agents routed around went almost unused. So the ledger is the only status source, and `rh status` reads nothing else.
- Guards must stay first responders: a model round trip is too slow. So the harness never edits or replaces host guards. It only reads the host and admits work.
- Never retry, reword or reroute past a refusal. The harness stops, records and routes.
- Gates use exit codes, never `;` chains or grep output.
