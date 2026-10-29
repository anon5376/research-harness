# Briefs for claude-worker-loop

Fill every `<placeholder>`. The background-session brief is in the skill itself (step 3).

## Continuation (background session)

After a held message, a stall or a changed input: stop the session, save this as `PROMPT-v<n>.txt`, and relaunch on the same folder with the full flag set.

```
Continue <job> from the files already in <job dir>. Read REPORT.md, GATES.md and the newest files first.
Change from the first brief: <the one new input>. Everything else in PROMPT.txt still binds.
```

## Subagent or workflow agent

```
Load the unlazy skill and list your gates (one observable outcome each, with a check) before real work.
Read-only except <one path>. Inputs: <absolute paths>. Task: <one outcome>.
Return your report as your final message: answer first, at most <n> lines, with the met/unmet/abandoned gate counts. Do not write report files.
```

## Desktop session from a task chip

The owner clicks the chip, so the prompt must stand alone.

```
You are the <role> session. Load the unlazy skill and write GATES.md before real work.
Base checkout, read by absolute path: <abs path>. Your worktree holds tracked files only.
Build in your worktree or scratchpad. When done, say "files final" with the file list; the lead copies them in.
Report to <lead session name> with send_message at start, finish and block. Never commit, push or open a PR.
```

## Watcher for background workers

Prints only what you act on: a new end state, an open dialog, or a job folder with no DONE and no file change for 30 minutes. Run it under Monitor and re-arm on expiry.

```
prev=<tmp>/prev; : > "$prev"
while true; do
  {
    claude agents --json | python3 -c '
import json, sys
for s in json.load(sys.stdin):
    if s.get("kind") != "background":
        continue
    if s.get("state") in ("done", "blocked", "failed", "stopped") or s.get("waitingFor"):
        print(s.get("name"), s.get("state"), s.get("waitingFor") or "")
'
    for d in <job dirs>; do
      [ -e "$d/DONE" ] || [ -n "$(find "$d" -type f -mmin -30 | head -n 1)" ] || echo "$d quiet 30m"
    done
  } | sort > <tmp>/now
  comm -13 "$prev" <tmp>/now; mv <tmp>/now "$prev"; sleep 60
done
```

`failed` and `stopped` come from the docs; only `blocked` and `done` have been seen in this output so far.
