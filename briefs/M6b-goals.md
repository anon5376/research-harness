# M6b: research goals and roles

**Goal.** A research goal is one acceptance card over several jobs. Roles give jobs their defaults: the configurable hierarchy that allocates specialised tasks. `rh goal add <file>` expands a goal file into ordinary job contracts. `rh goal show <id>` rolls their states up.

**Files you write:** `rh/goals.py`, `tests/test_goals.py`, `goals/example.toml`. Nothing else. The CLI hooks already exist in `rh/cli.py` (`cmd_goal`).

**Interface (exact).**
```python
class GoalError(ValueError): ...
def load_goal(path: pathlib.Path) -> dict      # parse + validate a goal TOML; raise GoalError
def expand(goal: dict) -> list[dict]           # raw job tables, each later passed to contract.normalize()
def rollup(goal_id: str, jobs: list[dict]) -> dict   # jobs = Ledger.jobs() rows
```

**Goal file format.**
```toml
id = "lit-review-01"            # same rule as job ids: ^[a-z0-9][a-z0-9._-]{0,62}$
[card]                          # required; all three non-empty
said = "..."                    # the requester's words, verbatim
form = "file"
done_when = "..."
[roles.scout]                   # defaults for every job of this role; any job field is allowed
kind = "codex-cloud"
needs = ["net"]
model = {requested = "gpt-6.1-sol", speed = "fast", effort = "extra-high"}
[[jobs]]
id = "scan"                     # expanded id: "<goal id>.<job id>"
role = "scout"
brief = "..."
[[jobs]]
id = "synthesis"
role = "analyst"
after = ["scan"]                # ids inside the goal; expanded to "<goal id>.scan"
brief = "..."
```

**Behaviour.**
- `expand` merges role defaults under each job's own fields (the job wins), sets `goal = <goal id>`, prefixes `id` and `after` with `<goal id>.`, and sets `operator_facing = true` with the goal's card on the last job in dependency order.
- It raises GoalError for:
  - an unknown role;
  - a duplicate job id;
  - an `after` that names no job in the goal;
  - a dependency cycle;
  - a goal with no jobs;
  - a missing card field.
- `rollup` returns `{"goal": id, "jobs": {id: state}, "done": bool, "ended_badly": [ids]}`. `done` is true only when every job of the goal is `done`. `ended_badly` lists jobs in a terminal state other than done.

**Tests that must pass** (`tests/test_goals.py`, unittest):
1. Role defaults reach the job, and a job field overrides its role's.
2. Ids and `after` are prefixed. The last job carries the card and `operator_facing`.
3. Each GoalError case above raises with a message naming the problem: six cases.
4. Every expanded table passes `rh.contract.normalize`.
5. `rollup` gives done=false with one job running, done=true with all done, and lists a failed job in `ended_badly`.
6. End to end: a temp `RH_HOME` with no cloud jobs. `bin/rh goal add goals/example.toml` adds N jobs. `bin/rh goal show <id>` prints their states.
7. `python3 -I tests/run.py` passes as a whole.

**`goals/example.toml`:** a generic three-job literature-review goal (scout, analyst, critic) whose jobs are `local-shell` with `command = ["true"]`, so test 6 runs offline. No real names, topics or data.

**Do not change** `rh/cli.py`, `rh/contract.py`, `rh/ledger.py` or the job contract format.
