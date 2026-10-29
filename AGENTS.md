# Rules for agents working in this repository

This repo is public. Codex cloud tasks launched by the harness also read this file.

## Cloud tasks started by research-harness
- Run the task prompt's first command, the pinned `git fetch … && git checkout --detach <sha>` line, before anything else. If it fails, write `runs/<job>/RESULT.json` with state `blocked` and the error, then stop.
- Write only under `runs/<job>/` and the paths the prompt lists. Anything else fails the job.
- Never push, open pull requests, send messages or notifications, sign in, or change settings.
- If a tool, permission or policy refuses an action, stop. Record the exact refusal text in `RESULT.json` `refusals`. Do not retry, reword or route around it.
- Finish by writing `runs/<job>/RESULT.json` in the schema the prompt gives. Record each check you ran with its exit code.

## Changing the harness
- Python 3.11+ standard library only. A new dependency needs the owner's yes.
- Run `python3 -I tests/run.py` before any commit. It prints `TESTS PASSED n=<count>` only when every test passed.
- Every test must be able to fail on a real regression.
- Nothing private enters the repo: no home-directory paths, personal emails, tokens, account or task ids. The pre-commit hook (`git config core.hooksPath hooks`) runs `rh/privacy.py` on staged files and commit messages.
- Runtime state lives in `$RH_HOME` (default `~/.research-harness`), never in the repo.
