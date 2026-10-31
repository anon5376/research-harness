# Harvest details for codex-cloud-loop

## Saving the staged diff (new environments)

- After the export follow-up, open the task's Changes panel, switch to Staged and reload. The panel runs `git diff --cached --full-index` in the container.
- Clipboard capture from a scripted browser returned nothing. What worked was reading the panel's command output from the page's websocket (`command/exec/outputDelta` frames, base64) and saving it as `<job>-<model>.patch`.
- Above about 5,000 changed lines the viewer shows one file at a time. Trim with the export follow-up first, then reload.
- The diff carries no `--binary` data: PNGs, zips and other binaries are lost. Regenerate them locally from the shipped code.
- A package that copies an existing one exports only the files that differ.

## Bundle export (when Changes has expired)

The follow-up's script reads the index (`git diff --cached --raw`, then `git cat-file blob` for each staged blob), so unstaged edits stay out. It prints one `RH-BUNDLE {json}` header (job, HEAD, size and sha256 of the tar.gz, part count, the `large` files over 64 KiB, `special` entries such as symlinks and submodules, and `deleted` paths), then each `RH-PART {"n", "of", "sha256"}` followed by its base64 in 76-character lines, then `RH-END`. A part's sha256 is taken over its base64 text without line breaks, and the whole's over the tar.gz bytes. Tar entries are sorted, with mtime 0 and owner 0, and gzip carries mtime 0, so the same staged work prints the same text. Binaries up to 64 KiB survive, unlike in the Changes capture. The script writes all of this to `/tmp/rh-bundle-<job>.txt`, and the follow-up prints that file with `sed -n '1,700p'`, `sed -n '701,1400p'` and so on, one command per piece. One command's output is cut at 65,536 characters, and 700 lines of 77 is 53,900. Tasks asked to link or attach a `/tmp` file said they could not, and the page showed no file. Save each piece as its own file, numbered in order (`001.txt`, `002.txt`, ...), in one directory: `rh cloud harvest --bundle` reads its files in name order and ignores prose and code fences outside the parts. It refuses a missing or mismatched part, a header for another job or commit, special entries, and any member that is not a regular file at a plain relative path outside `.git`.

## Legacy environments (CLI)

- `codex cloud exec --env <env> --branch <branch> --attempts <n> "<prompt>"` runs best-of-n (1 to 4) on one task. Model, effort and permission cannot be set; which model the attempts ran on was not recorded.
- `codex cloud status <task>` shows state, not file contents. Harvest each attempt with `codex cloud diff <task> --attempt <n>`, or apply it with `codex cloud apply <task> --attempt <n>`.
- Attempts 3 and 4 returned HTTP 429 at first; one retry after a backoff worked.
- Inputs over 5 MB that were left out of the repo made every attempt invent stand-ins.

## Open questions (October 2026)

- The docs say the repository refresh runs automatically, yet tasks still started on the commit the environment was built on. Keep Step 0 until a probe shows otherwise.
- Whether the GitHub token in a Full access shell is real or a placeholder the proxy swaps. Treat it as real.
- The per-task permission modes are not documented.
- Whether a newer CLI sees new environments. On 31 Oct the CLI's environment lookup (`codex-rs/cloud-tasks/src/env_detect.rs`) was the same on main as at 0.165.0, and the 0.166.0 notes name no change to it.
