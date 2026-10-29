# Harvest details for codex-cloud-loop

## Saving the staged diff (new environments)

- After the export follow-up, open the task's Changes panel, switch to Staged and reload. The panel runs `git diff --cached --full-index` in the container.
- Clipboard capture from a scripted browser returned nothing. What worked was reading the panel's command output from the page's websocket (`command/exec/outputDelta` frames, base64) and saving it as `<job>-<model>.patch`.
- Above about 5,000 changed lines the viewer shows one file at a time. Trim with the export follow-up first, then reload.
- The diff carries no `--binary` data: PNGs, zips and other binaries are lost. Regenerate them locally from the shipped code.
- A package that copies an existing one exports only the files that differ.

## Legacy environments (CLI)

- `codex cloud exec --env <env> --branch <branch> --attempts <n> "<prompt>"` runs best-of-n (1 to 4) on one task. Model, effort and permission cannot be set; which model the attempts ran on was not recorded.
- `codex cloud status <task>` shows state, not file contents. Harvest each attempt with `codex cloud diff <task> --attempt <n>`, or apply it with `codex cloud apply <task> --attempt <n>`.
- Attempts 3 and 4 returned HTTP 429 at first; one retry after a backoff worked.
- Inputs over 5 MB that were left out of the repo made every attempt invent stand-ins.

## Open questions (October 2026)

- The docs say the repository refresh runs automatically, yet tasks still started on the commit the environment was built on. Keep Step 0 until a probe shows otherwise.
- Whether the GitHub token in a Full access shell is real or a placeholder the proxy swaps. Treat it as real.
- The per-task permission modes are not documented.
- Whether a newer CLI sees new environments.
- Whether a cloud task should approve its own unlazy gates, and whether the environment's "Start skill" field can preload unlazy. Both untested.
