# Briefs for codex-cloud-loop

Fill every `<placeholder>`. Write each prompt to a file and record its SHA-256 before pasting.

## Environment probe

Send once after each environment change, with the permission the wave will use. It passes when the stack line prints its versions, the fetch succeeds and each allowed host answers.

```
Probe only. Change no files. Run each command once and reply with its exact output:
ls /workspace
<stack line>
git -C /workspace/<repo> log -1 --format='%H %s'
git -C /workspace/<repo> fetch origin <branch>
node --version
curl -sS -o /dev/null -w '%{http_code}\n' https://<allowed host>
env | cut -d= -f1 | sort
Never retry and never work around a failure. Print variable names only, never their values.
```

## Follow-up header

```
Follow-up for <job> in this task's container. Every HEADER rule from the first turn still binds. Your earlier output in <out dir> is here, staged or uncommitted. Do not fetch anything: the container already holds <sha>.
What changed since your last turn: <the one new input: a ruling, a harvest finding, the next build>.
Deliver as before: VERDICT.md, SUMMARY.md and DONE, only <out dir> staged, nothing committed.
Job: <text>
```

## Export follow-up

```
Export only. Change no file contents.
If you committed anything, run git reset --soft <sha>. Then run git reset -q and git add -A <out dir>.
Unstage pytest temp folders, __pycache__, runtime/, and every file over 1 MB or 1,500 lines. List each unstaged file with its size and SHA-256.
Reply with the output of git status --short and git diff --cached --stat.
```

## TASKS.tsv

One row per task, tab-separated:

```
date	job	task link	model, speed, effort	permission	prompt sha256
```

## VERDICT.md per job

```
# Verdict: <job>
Accepted: <model> attempt, because <reason>. Runner-up: <model>, which lost on <reason>.
Local result: <exact command> -> <real result>.
Mutant check: <the one-line break> -> <the test that went red>.
Defects: <file:line> <defect>, one per line, or none.
Export: <files and lines>; dropped: <files and why>.
Follow-up job: <the next follow-up prompt, or none>.
```
