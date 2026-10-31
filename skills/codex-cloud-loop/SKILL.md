---
name: codex-cloud-loop
description: Run Codex cloud tasks as workers, from environment to accepted patch. Use when sending jobs to Codex cloud, steering a running cloud task, or harvesting and judging its output.
---

# codex-cloud-loop

Run jobs as Codex cloud tasks and accept only what reproduces on your machine. A cloud task is a remote worker. It starts from a cached checkout that may be stale, it hears from you only through follow-up turns, and its output stays in its container until you export it. Each step below closes one of those gaps.

You are the lead: you write the briefs, launch, steer, harvest, judge and commit what you accept. Design, module layout, interfaces and the wave plan stay with you; each brief fixes the interface (signatures, paths, schemas) and the tests, the task implements to it, and a diff that changes an interface is rejected. The account owner does the owner steps below. Workers leave their work staged and uncommitted.

## Budget

- 1 environment probe after each environment change.
- Job 1 alone as the commit gate, then the rest of the wave.
- Each job runs twice, one task per model (step 5). Keep the better attempt.
- Per task: at most 2 steering follow-ups, plus 1 export follow-up.
- One harvest per attempt. Time cap per turn: twice the job's planned time (step 10).
- One browser tab at a time; close each task tab once it is harvested.

## Owner steps

Queue each with the exact action, then keep working on everything that does not depend on it:
- publish or republish the environment, which carries every install-script and allowlist change (you fill the form and stop before Publish);
- all-internet, or any network wider than the allowlist;
- "Full access" for a wave (step 5);
- the speed and effort policy (step 5), set once as a standing rule;
- sign-in, billing and CLI upgrades.

## 1. Pick the system

As of codex-cli 0.165.0 there are two:
- **Legacy environments** are the ones the CLI sees: `codex cloud exec --env <env> --branch <branch> --attempts <1-4> "<prompt>"`, then `codex cloud diff <task> --attempt <n>`. The CLI cannot set model, effort or permission. Details: [references/harvest.md](references/harvest.md).
- **New environments** (Settings → Codex Cloud → Environments) answer "environment not found" in the CLI, by label and by id, because the CLI resolves `--env` against its own environment list and they are not in it. Their tasks answer `404 Invalid task ID` to `codex cloud status` and `diff`. Drive them from the web composer. The rest of this skill assumes this system. research-harness launches natively only into a legacy environment; while it has none, its cloud jobs wait and one inbox item asks for one.

## 2. Environment

- Install script: `set -euo pipefail`, install the stack, and end with one import line that prints `stack ok <versions>`. That line is the `<stack line>` every brief runs first.
- Repository entry: a branch, set in the UI form; Step 0 pins the commit. When a setup chat's repository tool rewrote the entry to pin a commit, the publish failed (`APPROVAL_FAILED_BEFORE_SNAPSHOT`) and the repository panel locked.
- Allowlist: the domains a named job needs, with all-internet off.
- Publishing takes about 2 minutes. A failed publish can leave the spinner up; read the publish operation's state from the page's network log (`environment-operations`).
- The environment is ready when the environment probe in [references/briefs.md](references/briefs.md) passes under the wave's permission. A setup chat's "saved" is not that proof: one environment reported saved while its tasks still lacked numpy.

## 3. Inputs

A task sees only what is committed and pushed to the branch Step 0 fetches. Missing inputs get invented: tasks handed an empty file, or an error page saved as a datasheet, made up the values.
- Copy each input into the repo path the jobs read. Scan each file for private strings (names, ids, tokens, local paths) and leave out files that hit.
- Stage only that path, because the checkout may hold other people's uncommitted work. Commit, push, and record the full 40-character SHA and its subject.
- Inputs are ready when `git ls-tree -r <sha> -- <input path>` lists every file the jobs read, each non-empty and of its expected type (a datasheet is a PDF, not an HTML page).

## 4. Brief

Prompt = Step 0 + HEADER + blank line + `Job:` and the job text. Write each prompt to a file and record its SHA-256.

Step 0. The cached checkout stays stale even after the branch moves and the environment is republished, and tasks start in `/workspace`, which is not a repository:

```
Step 0, before anything else: this task's checkout may be a stale cached copy, so fetch the wave commit first. Go to the root of the <repo> checkout (if the working directory is not a git repository, find the checkout with ls /workspace and cd into it; work from there for the rest of the task). There, run with network access:
git fetch origin <branch> && git checkout --detach <sha>
If that fails, put the exact error in SUMMARY.md line 1 and stop. This fetch is the only command in this task that may use the network; everything after it runs offline.
```

HEADER. Most lines answer a defect a harvest found; the Portability line has not yet run in a task:

```
Commit: you are on <sha> (git log -1 shows "<subject>"). If it shows another commit, stop and say so in SUMMARY.md line 1.
First command: <stack line>. Copy its output into SUMMARY.md. If it fails, say so in line 1 and continue.
Inputs: <input paths> are read-only. Code from an input that writes files is first copied to <out dir>/runtime/, which gets a .gitignore.
Write only in <out dir>. Touch nothing else.
Network: none after Step 0. A value you cannot source is a labelled assumption, swept over a range, never invented.
Size: every added file stays under 1 MB; regenerate anything bigger at test time. Pytest, TMPDIR and MPLCONFIGDIR temp folders live outside <out dir>. A test run changes no file in <out dir>, and a test fails if it does.
Portability: tests check generated output by value, floats within a tolerance, never by hash or exact string; paths are relative to <out dir>; outputs name the repository only as <origin>; units are stated wherever Linux and macOS differ.
Rulings in force: <each ruling quoted, with who made it>.
Deliver in <out dir>: VERDICT.md first, each statement marked [fact], [inference] or [open]; code; tests that would fail on a real regression; results; SUMMARY.md of at most 10 lines with the stack line, the exact test command and its real result; then an empty DONE. Never report a result you did not run.
Leave your work uncommitted: at the end, run git add -A <out dir> and nothing else, then unstage and list any file over 1 MB.
```

## 5. Model, effort, permission

- **Model:** GPT-6.1 Terra and GPT-6.1 Sol as of October 2026; use the newest of each. In the 8 jobs where both attempts could be compared, Sol won all 8. Terra lost on tests pinned to the cloud machine's hash, thinner tests and a scoring error. Run both anyway: the second attempt is the fallback when one hangs.
- **Hard jobs:** deep modelling, statistics, or a core module others build on go to GPT-6 Astra at Ultra when the owner's standing rule allows it; routine jobs stay on Sol or Terra at Extra High.
- **Effort:** Extra High. Move the slider one notch at a time with the arrow keys. End jumps to Ultra and opens a dialog: accept it only for a hard job under the owner's standing rule, otherwise decline. A page reload drops effort to Light, so set it again before each batch.
- **Speed:** the owner's standing rule. With fast on ("More usage"), a wave spends the plan faster, so read the weekly usage before each batch and stop launching at 95 % when the account holds paid credits, so a wave never draws on them.
- **Permission:** "Approve for me" leaves the task shell without network (the proxy refuses), so Step 0 fails. "Full access" routes through the environment allowlist and Step 0 works, but the shell holds a GitHub token: use it only with prompts you wrote, and only with the owner's yes for that wave.

## 6. Launch

1. **Commit gate.** Send job 1 alone and wait for its reply: `git log -1` shows the wave SHA and the stack line printed. On any mismatch, stop sending, fix Step 0, and resend job 1 alone.
2. Send the rest, about one a minute, one task per model. For a long-lived task, open the composer's Goal command ("Set a goal to keep pursuing") in an empty composer and paste the whole prompt as the goal text; the task shows "Sent as goal". Feed its later jobs as follow-up turns in the same task (step 7).
3. Paste once, with a paste event; a long paste becomes a "pasted text" chip, and a second paste sends the text twice. Send only when the SHA-256 of the composer text matches the prompt file.
4. Log each task in `TASKS.tsv` (columns in [references/briefs.md](references/briefs.md)).

## 7. Steer

A task keeps its container, uncommitted output included; the docs say saved state lasts up to 7 days after the last turn. Steer with a follow-up turn in that task; a new task starts without the old output. Follow-ups that worked:
- carry a ruling that unblocks it;
- ask for a fact without file changes, such as `git log -1`;
- run the next build in the kept container, switching the model in the composer if needed;
- after "task container became unavailable", send one `continue`.

The "Follow-up job" line of a VERDICT.md becomes the next follow-up prompt, so findings go back to the author. Follow-up header: [references/briefs.md](references/briefs.md).

## 8. Harvest

In the new system the CLI and "Copy git apply command" both fail. The route that works:
1. Send the export follow-up ([references/briefs.md](references/briefs.md)): undo any commit, stage only `<out dir>`, drop big and temp files.
2. Open Changes → Staged and save the staged diff as a patch. The capture route and its limits (no binaries, one file per view above about 5,000 lines) are in [references/harvest.md](references/harvest.md).
3. `git apply --check` against `<sha>`, then apply in a detached worktree and run the tests there at low priority: `nice -n 15 <python> -I -B -m pytest -q -p no:cacheprovider <out dir>`.
4. Accept on one check plus one independent check (step 9): the local test run in step 3, and a second model's review or a local mutant. Task-written gate files are not evidence. In wave 5, 41 GATES.md files ran no gate tool and only restated pytest, and local acceptance still found defects in 23 of 30 units whose gates read met.

The Changes view expires. In October 2026 it was gone a few hours after a task started (the page's `backend-api/tbo/by-thread/<task>` answered 404), some tasks never showed it, and `codex cloud diff <uuid>` answers 404 "Invalid task ID". Harvest from Changes within 2 hours of the task's start; after that, send the bundle-export follow-up instead. The task writes a deterministic tar.gz of its staged files of 64 KiB or less as base64 parts of 90 kB, with a sha256 for each part and for the whole, and lists bigger files by path, size and sha256 for you to regenerate locally from the code, all into a file under `/tmp`. A task cannot link or attach that file, so the follow-up prints it with `sed -n` in pieces of 700 lines, one command each: a command's output is cut at 65,536 characters, and 700 lines of 77 is 53,900. In research-harness, `rh cloud prompt <job> --bundle-export` prints that follow-up, and `rh cloud harvest <job> --bundle <reply file or dir>` refuses any part whose sha does not match, rebuilds the staged diff at the pinned sha and collects it like a patch. Format: [references/harvest.md](references/harvest.md).

## 9. Accept

- Each test must be able to fail: break the code with a one-line mutant, or revert the fix, and watch the test go red.
- Hunt each defect the Size and Portability lines target; workers shipped every one before those lines existed: hashes pinned to the cloud machine, test runs that rewrite shipped results, floats compared as strings (cloud and local numpy versions differ), temp folders inside the package (one held 367 MB), `/workspace` paths, the repository URL, and units that differ by OS (`ru_maxrss` is kilobytes on Linux, bytes on macOS).
- Pick the better attempt and write VERDICT.md per job from the template in [references/briefs.md](references/briefs.md). A job is accepted when every field of its VERDICT.md is filled from a command you ran.
- Commit accepted patches on a branch.

## 10. Stop

- **Done:** every job in the wave has a VERDICT.md naming its accepted attempt, or why none was accepted.
- **Stuck:** a turn still "Working" past the time cap, often with its answer already posted, and Stop does not end it. Judge the other attempt alone and say so in the VERDICT.
- **Wrong commit at the gate:** stop all sends (step 6).
- **Blocked:** a refusal, a denied permission or an owner step. Report it with the exact text and leave it to the owner; never work around it.

Report, verdict first: jobs run, the attempt accepted per job and why, local test results, defects found, open owner steps.

---
Structure adapted from `sol-loop` in [t3dotgg/tstack](https://github.com/t3dotgg/tstack) (MIT, © 2026 Theo Browne).
