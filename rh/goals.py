"""Research goals: validate role-based jobs, expand their contracts, and roll up ledger states."""
from copy import deepcopy
from graphlib import CycleError, TopologicalSorter
import pathlib
import tomllib

from rh.contract import JOB_ID_RE
from rh.ledger import TERMINAL


class GoalError(ValueError):
    pass


def load_goal(path: pathlib.Path) -> dict:
    """Parse and validate a goal TOML file, preserving the requester's card verbatim."""
    try:
        goal = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise GoalError(f"not valid goal TOML: {exc}") from None
    except (OSError, UnicodeError) as exc:
        raise GoalError(f"cannot read goal file: {exc}") from None
    expand(goal)
    return goal


def expand(goal: dict) -> list[dict]:
    """Merge role defaults into independent raw job tables in dependency order."""
    if not isinstance(goal, dict):
        raise GoalError("goal must be a table")
    goal_id = goal.get("id")
    if not isinstance(goal_id, str) or not JOB_ID_RE.fullmatch(goal_id):
        raise GoalError("goal id must match " + JOB_ID_RE.pattern)
    card = goal.get("card")
    if not isinstance(card, dict):
        raise GoalError("goal needs a card table with said, form, done_when")
    missing = [key for key in ("said", "form", "done_when")
               if not isinstance(card.get(key), str) or not card[key].strip()]
    if missing:
        raise GoalError("card needs non-empty fields: " + ", ".join(missing))
    roles = goal.get("roles", {})
    if not isinstance(roles, dict):
        raise GoalError("roles must be a table of defaults")
    for role, defaults in roles.items():
        if not isinstance(defaults, dict):
            raise GoalError(f"roles.{role} must be a table of defaults")
    jobs = goal.get("jobs", [])
    if not isinstance(jobs, list):
        raise GoalError("jobs must be an array of tables")
    if not jobs:
        raise GoalError("goal has no jobs")

    by_id = {}
    for job in jobs:
        if not isinstance(job, dict):
            raise GoalError("jobs must contain only tables")
        defaults = {}
        if "role" in job:
            role = job["role"]
            if not isinstance(role, str) or role not in roles:
                raise GoalError(f"unknown role {role!r} for job {job.get('id')!r}")
            defaults = roles[role]
        table = deepcopy({**defaults, **job})
        job_id = table.get("id")
        if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
            raise GoalError("job id must match " + JOB_ID_RE.pattern)
        if job_id in by_id:
            raise GoalError(f"duplicate job id {job_id!r}")
        if not JOB_ID_RE.fullmatch(f"{goal_id}.{job_id}"):
            raise GoalError(f"expanded job id {goal_id}.{job_id} exceeds the job id limit")
        after = table.setdefault("after", [])
        if not isinstance(after, list) or not all(isinstance(dep, str) for dep in after):
            raise GoalError(f"job {job_id!r}: after must be a list of job ids")
        by_id[job_id] = table

    for job_id, table in by_id.items():
        for dep in table["after"]:
            if dep not in by_id:
                raise GoalError(f"job {job_id!r}: after {dep!r} names no job in the goal")
    try:
        order = list(TopologicalSorter({job_id: table["after"] for job_id, table in by_id.items()}).static_order())
    except CycleError as exc:
        raise GoalError("dependency cycle: " + " -> ".join(exc.args[1])) from None

    expanded = []
    for job_id in order:
        table = by_id[job_id]
        table["id"] = f"{goal_id}.{job_id}"
        table["goal"] = goal_id
        table["after"] = [f"{goal_id}.{dep}" for dep in table["after"]]
        expanded.append(table)
    expanded[-1]["operator_facing"] = True
    expanded[-1]["card"] = deepcopy(card)
    return expanded


def rollup(goal_id: str, jobs: list[dict]) -> dict:
    """Summarize only rows whose stored contract belongs to this goal."""
    states = {job["id"]: job["state"] for job in jobs if job["spec"].get("goal") == goal_id}
    return {"goal": goal_id, "jobs": states,
            "done": bool(states) and all(state == "done" for state in states.values()),
            "ended_badly": [job_id for job_id, state in states.items() if state in TERMINAL and state != "done"]}
