"""Swarm presets: a team of agents in a hierarchy, kept in $RH_HOME/swarm-presets.json (never in the repo).

A preset names its roles (each with a system prompt and the models it may use) and a tree of agents. The root is the
lead; each node is one role on one model, `count` copies of it. Two shapes run the same team:

- vertical: the lead may plan first; the leaves do the work; each node with a team judges its team's work and keeps
  the best; the lead judges what its managers kept.
- horizontal: every agent below the lead works side by side as a peer, and the lead judges them all.

The built-in "quick" preset is the flat swarm Settings ticks (swarm.models and swarm.judge in config.toml); it is
never stored here. Pure functions only: the server turns `plan()` into job files.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

QUICK = "quick"
FILE = "swarm-presets.json"
MAX_PRESETS, MAX_ROLES, MAX_SCOPE, MAX_PROMPT, MAX_NAME = 20, 8, 24, 4000, 60
MAX_JOBS, MAX_DEPTH, MAX_COUNT, MAX_WORKERS = 12, 4, 4, 8
SHAPES = ("vertical", "horizontal")
MODEL_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,79}")
ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
EFFORTS = ("", "light", "medium", "high", "extra-high", "max", "ultra")

LEAD_PROMPT = ("You are the lead agent of a research-harness swarm working on a complex task. You orchestrate a "
               "hierarchy of research agents in several layers: you set the plan the layers below follow, then review "
               "what your managers bring back and keep or combine the best into one result. Keep the whole task in "
               "view, be decisive, and trust evidence (tests, checks, sources) over claims.")
MANAGER_PROMPT = ("You are a manager in a research-harness swarm. Your team of agents works on the task under the "
                  "lead's plan. Review your team's work against that plan, keep or combine the best into one result, "
                  "and tell the lead plainly what you kept, why, and what is still open.")
WORKER_PROMPT = ("You are a worker agent in a research-harness swarm. Do the task you are given end to end and follow "
                 "the lead's plan when there is one. Keep your changes focused and tested, and say plainly what you "
                 "did and what you could not do.")
PLAN_ASK = ("Plan only: do not change any file. Write the plan your team will follow for the request below: the "
            "approach, how to split it, what each agent should check, and what done looks like. Keep it short.")


def _roles():
    return [{"id": "lead", "name": "Lead", "prompt": LEAD_PROMPT,
             "models": ["gpt-6.2-astra", "claude-opus-5-5", "claude-fable-5-2", "gpt-6.1-sol", "swe-2-max"]},
            {"id": "manager", "name": "Manager", "prompt": MANAGER_PROMPT,
             "models": ["gpt-6.2-sol", "claude-opus-5-5", "gpt-6.1-sol", "swe-2-max", "swe-2-high"]},
            {"id": "worker", "name": "Worker", "prompt": WORKER_PROMPT,
             "models": ["swe-2-max", "swe-2-high", "swe-2-flash-high", "claude-sonnet-5-5", "gpt-6.1-terra"]}]


def defaults():
    """The presets a new harness starts with: a free research team, and the example the owner described (a GPT-6.2
    Astra lead over Sol and Opus managers), which waits for policy.toml to allow its 6.2 models."""
    def team(manager, effort=""):
        return {"role": "manager", "model": manager, "effort": effort, "count": 1,
                "children": [{"role": "worker", "model": "swe-2-max", "effort": "", "count": 1, "children": []},
                             {"role": "worker", "model": "swe-2-flash-high", "effort": "", "count": 1, "children": []}]}
    free = {"id": "free-research", "name": "Free research team", "shape": "vertical", "plan": True,
            "roles": _roles(),
            "tree": {"role": "lead", "model": "swe-2-max", "effort": "", "count": 1,
                     "children": [team("swe-2-max"), team("swe-2-high")]}}
    astra = {"id": "astra-lead", "name": "Astra lead", "shape": "vertical", "plan": True, "roles": _roles(),
             "tree": {"role": "lead", "model": "gpt-6.2-astra", "effort": "max", "count": 1,
                      "children": [team("gpt-6.2-sol", "max"), team("claude-opus-5-5", "max")]}}
    return {"active": QUICK, "presets": [free, astra]}


def load(home) -> dict:
    """The stored presets, or the defaults when none are stored or the file does not parse."""
    p = Path(home) / FILE
    try:
        return validate(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return defaults()


def save(home, data) -> dict:
    """Validate and store the presets atomically; return what was stored."""
    clean = validate(data)
    p = Path(home) / FILE
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(clean, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return clean


def _text(v, limit, what):
    if not isinstance(v, str):
        raise ValueError(f"{what} must be text")
    v = v.replace("\r\n", "\n").strip()
    if len(v) > limit:
        raise ValueError(f"{what} is longer than {limit} characters")
    return v


def _node(raw, roles, depth):
    if not isinstance(raw, dict):
        raise ValueError("an agent must be an object")
    if depth > MAX_DEPTH:
        raise ValueError(f"a team is at most {MAX_DEPTH} layers deep")
    role, model = raw.get("role"), raw.get("model")
    if role not in roles:
        raise ValueError(f"no role {role!r}")
    if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
        raise ValueError(f"bad model name {model!r}")
    effort = raw.get("effort") or ""
    if effort not in EFFORTS:
        raise ValueError(f"bad effort {effort!r}")
    count = raw.get("count", 1)
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= MAX_COUNT:
        raise ValueError(f"count must be 1 to {MAX_COUNT}")
    kids = raw.get("children") or []
    if not isinstance(kids, list):
        raise ValueError("children must be a list")
    return {"role": role, "model": model, "effort": effort, "count": 1 if depth == 0 else count,
            "children": [_node(k, roles, depth + 1) for k in kids]}


def _preset(raw):
    if not isinstance(raw, dict):
        raise ValueError("a preset must be an object")
    pid = raw.get("id")
    if not isinstance(pid, str) or not ID_RE.fullmatch(pid) or pid == QUICK:
        raise ValueError(f"bad preset id {pid!r}")
    roles_raw = raw.get("roles")
    if not isinstance(roles_raw, list) or not 1 <= len(roles_raw) <= MAX_ROLES:
        raise ValueError(f"a preset has 1 to {MAX_ROLES} roles")
    roles = []
    for r in roles_raw:
        if not isinstance(r, dict) or not isinstance(r.get("id"), str) or not ID_RE.fullmatch(r["id"]):
            raise ValueError("a role needs an id")
        models = r.get("models") or []
        if not isinstance(models, list) or len(models) > MAX_SCOPE or not all(
                isinstance(m, str) and MODEL_RE.fullmatch(m) for m in models):
            raise ValueError(f"role {r['id']}: bad model list")
        roles.append({"id": r["id"], "name": _text(r.get("name") or r["id"], MAX_NAME, "a role name") or r["id"],
                      "prompt": _text(r.get("prompt") or "", MAX_PROMPT, "a system prompt"),
                      "models": list(dict.fromkeys(models))})
    if len({r["id"] for r in roles}) != len(roles):
        raise ValueError("two roles share an id")
    shape = raw.get("shape", "vertical")
    if shape not in SHAPES:
        raise ValueError(f"shape is {' or '.join(SHAPES)}")
    clean = {"id": pid, "name": _text(raw.get("name") or pid, MAX_NAME, "a preset name") or pid, "shape": shape,
             "plan": bool(raw.get("plan")), "roles": roles,
             "tree": _node(raw.get("tree"), {r["id"] for r in roles}, 0)}
    plan(clean)  # a team too big to run is refused when it is saved, not when it is started
    return clean


def validate(data) -> dict:
    if not isinstance(data, dict):
        raise ValueError("presets must be an object")
    raw = data.get("presets")
    if not isinstance(raw, list) or len(raw) > MAX_PRESETS:
        raise ValueError(f"at most {MAX_PRESETS} presets")
    presets = [_preset(p) for p in raw]
    if len({p["id"] for p in presets}) != len(presets):
        raise ValueError("two presets share an id")
    active = data.get("active") or QUICK
    if active != QUICK and active not in {p["id"] for p in presets}:
        active = QUICK
    return {"active": active, "presets": presets}


def find(data, pid):
    return next((p for p in data["presets"] if p["id"] == pid), None)


def plan(preset) -> list[dict]:
    """The jobs a preset runs, in launch order: [{id, role, model, effort, prompt, work: bool, team: [ids], plan:
    bool}]. Ids: 'plan' for the lead's plan, w1.. for agents that work, m1.. for those that judge a team, 'best' for
    the lead. `team` lists the jobs a judge compares."""
    roles = {r["id"]: r for r in preset["roles"]}
    root = preset["tree"]

    def copies(node):
        return [node] * max(1, node.get("count", 1))

    w = m = 0
    jobs = []

    def job(node, **more):
        role = roles[node["role"]]
        return {"role": role["id"], "name": role["name"], "model": node["model"], "effort": node.get("effort") or "",
                "prompt": role["prompt"], "plan": False, "team": [], **more}

    def walk(node):  # -> the id of the job that stands for this node
        nonlocal w, m
        if not node["children"]:
            w += 1
            jobs.append(job(node, id=f"w{w}", work=True))
            return f"w{w}"
        team = [walk(k) for kid in node["children"] for k in copies(kid)]
        m += 1
        jobs.append(job(node, id=f"m{m}", work=False, team=team))
        return f"m{m}"

    def everyone(node):  # every agent under a node, each copy counted, in reading order
        out = []
        for kid in node["children"]:
            for one in copies(kid):
                out += [one, *everyone(one)]
        return out

    if not root["children"]:
        raise ValueError("the lead needs at least one agent under it")
    if preset["shape"] == "horizontal":  # every agent under the lead is a peer that works
        team = []
        for node in everyone(root):
            w += 1
            jobs.append(job(node, id=f"w{w}", work=True))
            team.append(f"w{w}")
    else:
        team = [walk(k) for kid in root["children"] for k in copies(kid)]
    lead = job(root, id="best", work=False, team=team)
    out = ([job(root, id="plan", work=True, plan=True)] if preset.get("plan") else []) + jobs + [lead]
    if len(out) > MAX_JOBS:
        raise ValueError(f"this team runs {len(out)} agents; a swarm runs at most {MAX_JOBS}")
    if sum(j["work"] and not j["plan"] for j in out) > MAX_WORKERS:
        raise ValueError(f"a swarm has at most {MAX_WORKERS} agents that do the work")
    return out


def quick(models, judge) -> dict:
    """The flat swarm Settings ticks, as a preset: each ticked model works, the judge keeps the best. Its roles carry
    no system prompt, so a quick swarm runs exactly the brief it was given."""
    roles = [{**r, "prompt": ""} for r in _roles()]
    return {"id": QUICK, "name": "Quick swarm", "shape": "horizontal", "plan": False, "roles": roles,
            "tree": {"role": "lead", "model": judge or (models[0] if models else ""), "effort": "", "count": 1,
                     "children": [{"role": "worker", "model": m, "effort": "", "count": 1, "children": []}
                                  for m in models]}}
