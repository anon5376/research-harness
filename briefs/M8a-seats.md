# M8a: anchor seats in the harness

**Goal.** Jobs run in standing seats (`seats.toml`): each seat has one model and one effort, owns a set of paths, and takes its jobs as follow-up turns in one long-lived Codex task. The harness checks a job against its seat's card, writes the follow-up prompt, and reuses the seat's task id.

**Files you write:** `rh/seats.py`, `rh/cli.py`, `rh/contract.py`, `rh/protocol.py`, `tests/test_seats.py`. Nothing else. No existing test may change.

**`seats.toml`** (repository root, already committed): tables `[seat.<name>]` with `role`, `route` (`codex-cloud` or `claude`), `model`, `effort`, `owns` (paths; a path ending in `/` covers everything under it) and `done`. A job names its seat with a top-level `seat = "<name>"`.

**Interface (exact).**
```python
# rh/seats.py
def load(path) -> dict[str, dict]:
    """Parse seats.toml into {name: card}. Raise ValueError for a missing or unknown key, a route other than
    codex-cloud or claude, or a path that two seats both own (equal paths, or one a prefix of the other)."""

def holds(spec: dict, seats: dict) -> list[str]:
    """Why a job breaks its seat's card, empty if it does not: an unknown seat; a route other than the job's kind;
    a model or effort other than the seat's; a write_paths entry the seat does not own. A job without a seat: []."""

def task_of(led, seat: str) -> tuple[str | None, str | None]:
    """(task_id, busy_job): the external_id of the seat's most recently attached codex-cloud job, and that job's id
    if it is not yet terminal (None if it is). (None, None) when the seat has no attached job."""
```
- `protocol.followup_prompt(job_id, sha, brief, write_paths, attempt=1, network=False) -> str` returns
  `"Follow-up turn: this is a new job in the same task. Before step 1, run from the checkout root: git reset -q --hard && git clean -qfd\n\n"` followed by exactly `task_prompt(...)`. `task_prompt` does not change.

**CLI.**
- `rh cloud prompt <job>`: if the job has a seat and `task_of` returns a task, print `followup_prompt` and, on stderr, `follow-up in <task_id>` before the sha256 line. If that seat's `busy_job` is set, exit 1 naming it. Without a seat, or for a seat with no task yet, behaviour is unchanged.
- `rh cloud attach <job> [task_id]`: `task_id` becomes optional for a job whose seat has a task; given or not, attach refuses (exit 1, naming the job) while the seat's `busy_job` is set.
- `rh seats`: one line per seat: name, model, effort, task id or `-`, busy job or `-`.
- Preflight holds a job whose `seats.holds` is non-empty, with those reasons, like a policy hold. `seats.toml` is read from the repository path in the config; a missing file means no seats.

**Item 2: negation across a list.** In `contract.operator_steps`, a negation that starts a clause also covers the comma-separated items that follow it in the same sentence: "Never push, publish or deploy." and "Do not push, open pull requests, or send messages." give no steps; "Do not push, then deploy." still gives one.

**Tests that must pass** (`tests/test_seats.py`): `load` errors for an unknown key and for overlapping owns; `holds` for each of the four reasons and `[]` for a job inside its card; `task_of` for no job, a running job (busy) and a done job (not busy); `rh cloud prompt` prints the follow-up form with the reset line for a seat with a done job, and exits 1 for a busy seat; `attach` without a task id reuses the seat's task; the three negation sentences above; and `python3 -I tests/run.py` as a whole.
