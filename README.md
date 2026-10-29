# research-harness

A research harness built from scratch. It turns a research goal into jobs, runs agents locally and in Codex cloud, and records what each job actually delivered.

Status: v0 in progress. The design and milestones are in [DESIGN.md](DESIGN.md). Agent skills for running Codex and Claude workers are in [skills/](skills/README.md).

v0 is standard-library Python 3.11+, with no dependencies and no GUI.

```
bin/rh env spec                  # what the operator enters to create the cloud environment
bin/rh config set cloud.env_id <ID>
bin/rh job add jobs/probe.toml   # a TOML contract; preflight runs on the next tick
bin/rh supervise                 # the only launcher; one per $RH_HOME
bin/rh status                    # every job, stop, down route and inbox item
bin/rh inbox                     # operator steps, refusals, web launches
bin/rh land <job>                # commit a done cloud job's diff on branch cloud/<job>; never pushes
bin/rh lease run --what build -- make   # heavy local step under the shared lease
bin/rh doctor
```

A job contract looks like this:

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

Jobs end in exactly one typed state with a receipt: `done`, `failed`, `blocked`, `capacity`, `truncated`, `aborted`, `no_result` or `cancelled`. Stops and pauses are owned, so only their owner lifts them. The harness never pushes, publishes, signs in or changes settings. Those steps go to the inbox.

Tests: `python3 -I tests/run.py`.
