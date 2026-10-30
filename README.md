# research-harness

research-harness runs research as typed jobs on agent workers and keeps one record of what each job delivered. You write jobs as contracts. One supervisor launches them, in Codex cloud or as local shell commands, and every job ends in exactly one typed state with a receipt. A job counts as done only when its acceptance check passes on your machine.

Status: [in progress] v0.2.0. Python 3.11+, standard library only, no GUI. 195 tests pass: `python3 -I tests/run.py`. Desktop builds (.dmg and .exe) are planned for v1.0.0 and do not exist yet.

## What it does

- **Contracts.** A job is a TOML file: a brief, the capabilities it needs, the model and speed, and one acceptance command. Before launch the supervisor checks `needs` against `caps.toml` and the model against `policy.toml`.
- **One supervisor.** `rh supervise` is the only launcher, one per `$RH_HOME`. It owns launch, stop and pause. A stop belongs to whoever set it, and only that owner lifts it.
- **One ledger.** Every job, event and receipt is in one SQLite file. `rh status` shows all of it in one view.
- **Typed endings.** Each job ends as `done`, `failed`, `blocked`, `capacity`, `truncated`, `aborted`, `no_result` or `cancelled`, with a receipt.
- **Local acceptance.** A cloud job's output comes back as a patch or a checksummed bundle. Its acceptance command runs on your machine, and `rh land` commits an accepted result on the local branch `cloud/<job>`.
- **Heavy-step lease.** `rh lease run` makes heavy local steps take turns and starts each one only after host admission.
- **Operator inbox.** A step only a person may take goes to `rh inbox` with the exact action.

## What it never does

It never pushes, publishes, deploys or sends anything. It never signs in, enters credentials, spends money, or changes account or system settings. Each of those becomes an inbox item for the operator.

## Use

```
bin/rh env spec                  # what the operator enters to create the cloud environment
bin/rh config set cloud.env_id <ID>
bin/rh job add jobs/probe.toml   # a TOML contract; preflight runs on the next tick
bin/rh supervise                 # the only launcher; one per $RH_HOME
bin/rh status                    # every job, stop, down route and inbox item
bin/rh inbox                     # operator steps, refusals, web launches
bin/rh land <job>                # commit a done cloud job's diff on branch cloud/<job>; never pushes
bin/rh lease run --what build -- make   # heavy local step under the shared lease
bin/rh doctor                    # health of every surface the harness depends on
```

A job contract:

```toml
id = "lit-scan-01"
kind = "codex-cloud"            # or local-shell (needs command = [...])
brief = """Collect the five most cited reviews on the topic into runs/lit-scan-01/reviews.md."""
needs = ["net"]                 # checked against caps.toml before launch
accept = "test -s runs/lit-scan-01/reviews.md"   # run locally on the collected diff
[model]
requested = "gpt-6.1-sol"       # must be allowed by policy.toml
speed = "fast"
```

A goal (`rh goal add`) expands into a set of role-based jobs with one acceptance card over all of them. For tasks launched in the web view: `rh cloud prompt <job>` prints the exact prompt, `rh cloud attach` records the task id, and `rh cloud harvest` hands the returned patch or bundle to the supervisor.

## Skills

`skills/` holds agent skills that can be used without the harness. Copy a folder into your agent's skills directory.

| Skill | Use |
|---|---|
| `codex-cloud-loop` | Codex cloud tasks as workers, from environment setup to an accepted patch |
| `claude-worker-loop` | Claude Code workers, from brief to accepted result |
| `unlazy` | Acceptance gates for long unattended builds; optional |

## Scope and limits

- macOS and Linux only. The lock and the lease use `fcntl` and `sysctl`; Windows support is planned with the desktop builds.
- Codex cloud's newer environments cannot be driven from the CLI. Their launch and harvest go through the web view, with the operator steps above.
- No licence file yet.
- Design and milestones: [DESIGN.md](DESIGN.md).
