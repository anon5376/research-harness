# Cloud runs for claude-worker-loop

A cloud run is a one-off scheduled task on Anthropic's cloud. It runs on a clone of the repo, in auto mode, with nobody to answer it.

## Rules

- **Probe once before a wave:** one tiny run that reports CPU, memory, disk, network reach, repo access and the language stack. One probe found only one of two repos reachable.
- **Network:** the default environment allows a trusted allowlist only. Other hosts return `403` with `x-deny-reason: host_not_allowed`, and research runs quietly fall back to search snippets. Widening access is the owner's click.
- **Containment:** the brief forbids editing the clone and confines work to `/tmp/work`. Without that line, a run can push to `claude/*` branches; one run also sent the owner a phone notification.
- **Pre-register:** put the decision rule in the prompt, and have the run write it to a file and print its SHA-256 before it touches data.
- **Result in the final message:** at most 20 lines, with a TSV block. Long logs get cut, and the run pages need the owner's sign-in.
- **Consent:** a fired prompt is not live user input and approves nothing. Blocked requests show up in the transcript while the run still reads "Succeeded", so read the result, not the status.
- **Model and effort:** pick the model per run. Effort could not be set through the scheduling API, though the web dialog offers it.
- **Limits:** at most 100 scheduled runs per hour per account, one-offs included, and 30 "Run now" or API fires per hour per task. Runs draw on plan usage.
- **Harvest:** keep a TSV of run ids, then read the run list and each run's log.

## Brief

```
OWNER RULES: read <repo path>/skills/unlazy/SKILL.md and write /tmp/work/GATES.md before real work. Do not edit the cloned repo; work only in /tmp/work. Never commit, push, branch, open a PR, or send any notification or message. At most <n> processes, memory under <m> GB. Delete downloads at the end.
QUESTION: <one question>.
PRE-REGISTERED RULE: <rule>. Write it to /tmp/work/PREREG.txt and print its sha256 before touching data.
DATA: <url, size>.
FINAL MESSAGE, at most 20 lines: <TSV block spec>; gate counts met/unmet/abandoned; deviations from this brief; machine time.
```
