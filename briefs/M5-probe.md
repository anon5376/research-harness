# M5: environment probe (the first real cloud spawn)

**Goal.** Prove the harness can start a Codex cloud task in the `research-harness` environment, pinned to a commit it chose, and get back a typed result. The probe also measures what the environment can do, which replaces the `unknown` entries in `caps.toml`.

**Job.** `jobs/probe.toml`. The brief inside it is the whole task; nothing in the repo changes except `runs/probe-env-01/`.

**Operator steps first.** `rh env spec` prints them:
1. Codex GitHub app access to the repo.
2. Environment created and published.
3. Full access allowed for harness tasks.
4. `rh config set cloud.env_id <ID>`.

**Run.**
```
rh job add jobs/probe.toml
rh supervise --once            # tries `codex cloud exec --env <ID>`; on "not found" the job goes to handoff
rh cloud prompt probe-env-01   # paste once into the web composer: Sol, fast, extra-high, Full access, best of 2
rh cloud attach probe-env-01 <task_id>
rh supervise --once            # polls; collects the diff, or asks for a harvest in the inbox
rh cloud harvest probe-env-01 <saved staged diff>   # only if the inbox asks
rh caps import probe-env-01
```

**Accept.**
- `rh job show probe-env-01` shows state `done` and a receipt whose `pinned_sha` equals RESULT.json `head`.
- `changed_paths` lie only under `runs/probe-env-01/`.
- `rh caps show` lists measured booleans for python, git, net, loopback and gpu under `codex-cloud`.

**It fails on** a wrong head (`failed: stale input`), a missing RESULT.json (`no_result`), any path outside the run folder (`failed: custody`), or a refusal (`blocked`, with the text in the inbox). Record a failure as it is. Do not edit the job to make it pass.
