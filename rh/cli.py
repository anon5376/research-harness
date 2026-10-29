"""rh: the research-harness command line. Every view reads the ledger; nothing else is a status source."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import tomllib
from pathlib import Path

from rh import __version__, collect, config, contract, envspec, gitx, lease, protocol
from rh.ledger import LIVE, TERMINAL, Ledger, LedgerError

TASK_RE = re.compile(r"^task_[a-z]_[0-9a-f]{8,}$")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rh", description="research-harness: typed jobs, one ledger, one supervisor")
    ap.add_argument("--version", action="version", version=f"rh {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("job", help="add or show jobs")
    js = p.add_subparsers(dest="job_cmd", required=True)
    a = js.add_parser("add", help="add a job from a TOML contract")
    a.add_argument("file", type=Path)
    a.add_argument("--sha", help="cloud jobs: commit to pin (default: HEAD of the repo)")
    s = js.add_parser("show", help="one job with its events and receipt")
    s.add_argument("id")

    p = sub.add_parser("status", help="one view over every job, stop, route and the inbox")
    p.add_argument("--json", action="store_true")
    p.add_argument("--html", type=Path, help="also write a static dashboard page to this path")

    p = sub.add_parser("goal", help="a research goal: one acceptance card over a set of role-based jobs")
    gs = p.add_subparsers(dest="goal_cmd", required=True)
    g = gs.add_parser("add", help="add every job a goal file expands to")
    g.add_argument("file", type=Path)
    g = gs.add_parser("show", help="the goal's jobs and their rolled-up state")
    g.add_argument("id")

    p = sub.add_parser("inbox", help="operator steps, refusals and decisions")
    p.add_argument("--all", action="store_true")
    ins = p.add_subparsers(dest="inbox_cmd")
    r = ins.add_parser("resolve")
    r.add_argument("id", type=int)
    r.add_argument("note")

    for name in ("stop", "resume"):
        p = sub.add_parser(name, help=f"{name} launches (owned: only the owner resumes its own stop)")
        p.add_argument("--owner", required=True)
        if name == "stop":
            p.add_argument("--reason", required=True)
        g = p.add_mutually_exclusive_group()
        g.add_argument("--job")
        g.add_argument("--route")
        p.add_argument("--pause", action="store_true", help="pause (running work continues; never counted stalled)")

    p = sub.add_parser("supervise", help="run the supervisor (one instance per RH_HOME)")
    p.add_argument("--once", action="store_true")
    p.add_argument("--ticks", type=int)
    p.add_argument("--interval", type=float)

    p = sub.add_parser("cloud", help="web-route helpers")
    cs = p.add_subparsers(dest="cloud_cmd", required=True)
    c = cs.add_parser("prompt", help="print the exact prompt to submit for a handoff job")
    c.add_argument("id")
    c = cs.add_parser("harvest", help="hand the supervisor a patch saved from the web view")
    c.add_argument("id")
    c.add_argument("patch", type=Path)
    c = cs.add_parser("attach", help="record the task id of a job launched in the web composer")
    c.add_argument("id")
    c.add_argument("task_id")

    p = sub.add_parser("land", help="commit a done cloud job's diff on local branch cloud/<job> (never pushes)")
    p.add_argument("id")

    p = sub.add_parser("env", help="cloud environment")
    es = p.add_subparsers(dest="env_cmd", required=True)
    es.add_parser("spec", help="print what the operator enters to create the environment")

    sub.add_parser("doctor", help="health of every surface the harness depends on")

    p = sub.add_parser("lease", help="heavy-step lease")
    ls = p.add_subparsers(dest="lease_cmd", required=True)
    lr = ls.add_parser("run", help="run CMD holding the heavy lease, after host admission")
    lr.add_argument("--what", default="step")
    lr.add_argument("--max-wait", type=float)
    lr.add_argument("cmd", nargs=argparse.REMAINDER)
    ls.add_parser("status")

    p = sub.add_parser("service", help="launchd service file for the supervisor (written, never loaded)")
    sv = p.add_subparsers(dest="service_cmd", required=True)
    sv.add_parser("plist", help="write the LaunchAgent plist to $RH_HOME and print the line that loads it")

    p = sub.add_parser("caps", help="capability table")
    cps = p.add_subparsers(dest="caps_cmd", required=True)
    c = cps.add_parser("import", help="write a done probe job's readings to $RH_HOME/caps.toml")
    c.add_argument("id")
    cps.add_parser("show")

    p = sub.add_parser("config", help="local config in $RH_HOME/config.toml")
    cfs = p.add_subparsers(dest="config_cmd", required=True)
    c = cfs.add_parser("set")
    c.add_argument("key")
    c.add_argument("value")
    cfs.add_parser("show")

    args = ap.parse_args(argv)
    cfg = config.load()
    try:
        return COMMANDS[args.cmd](args, cfg)
    except (LedgerError, contract.ContractError, gitx.GitError, ValueError) as e:
        print(f"rh: {e}", file=sys.stderr)
        return 1


def _codex_cwd(cfg):
    """codex writes error.log (with account ids) into its working directory, so run it in $RH_HOME."""
    d = Path(cfg["home"]) / "codex-cwd"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _ledger(cfg):
    return Ledger(Path(cfg["home"]) / "ledger.db")


def cmd_job(args, cfg):
    led = _ledger(cfg)
    if args.job_cmd == "show":
        j = led.job(args.id)
        if not j:
            print(f"rh: no job {args.id}", file=sys.stderr)
            return 1
        print(json.dumps({"job": j, "receipt": led.receipt(args.id), "events": led.events(args.id)}, indent=2,
                         default=str))
        return 0
    spec = contract.load(args.file)
    sha = None
    if spec["kind"] == "codex-cloud":
        sha = args.sha or gitx.head(cfg["repo"]["path"])
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("--sha must be a full 40-hex commit")
    led.add_job(spec, pinned_sha=sha)
    print(f"added {spec['id']} ({spec['kind']}); the supervisor preflights it on its next tick")
    return 0


def cmd_status(args, cfg):
    led = _ledger(cfg)
    now = time.time()
    jobs = led.jobs()
    hb_path = Path(cfg["home"]) / "supervisor.json"
    hb = json.loads(hb_path.read_text()) if hb_path.is_file() else None
    view = {
        "supervisor": {"pid": hb["pid"], "age_s": round(now - hb["ts"])} if hb else None,
        "stops": led.active_stops(),
        "routes": [r for r in led.routes() if not r["healthy"]],
        "live": [_row(led, j) for j in jobs if j["state"] in LIVE],
        "not_started": [_row(led, j) for j in jobs if j["state"] in ("draft", "ready", "waiting", "held")],
        "inbox": led.inbox(),
        "last_24h": _counts(j for j in jobs if j["state"] in TERMINAL and now - j["updated"] < 86400),
        "recent": [_row(led, j) for j in sorted(jobs, key=lambda j: -j["updated"]) if j["state"] in TERMINAL][:10],
    }
    if args.html:
        page = _optional("dashboard", "M6a").render(view, now)
        tmp = args.html.with_suffix(args.html.suffix + ".tmp")
        tmp.write_text(page)
        tmp.replace(args.html)
    if args.json:
        print(json.dumps(view, indent=2, default=str))
        return 0
    sup = view["supervisor"]
    print(f"supervisor: {'pid %s, last tick %ss ago' % (sup['pid'], sup['age_s']) if sup else 'never ran'}")
    for s in view["stops"]:
        print(f"{s['kind'].upper()} {s['scope']} by {s['owner']}: {s['reason']}")
    for r in view["routes"]:
        print(f"ROUTE DOWN {r['route']}: {r['last_status']} (failures {r['failures']}, "
              f"next try in {max(0, round(r['next_try'] - now))}s)")
    for title, rows in (("live", view["live"]), ("not started", view["not_started"]), ("recent", view["recent"])):
        if rows:
            print(f"{title}:")
            for r in rows:
                print(f"  {r['id']:<28} {r['state']:<10}{' PAUSED' if r['paused'] else ''} {r['detail']}")
    if view["inbox"]:
        print(f"inbox ({len(view['inbox'])} open):")
        for i in view["inbox"]:
            print(f"  #{i['id']} [{i['kind']}] {i['action']}" + (f" (job {i['job']})" if i["job"] else ""))
    print("last 24h: " + (", ".join(f"{k} {v}" for k, v in sorted(view["last_24h"].items())) or "nothing finished"))
    return 0


def _optional(module, milestone):
    import importlib
    try:
        return importlib.import_module(f"rh.{module}")
    except ModuleNotFoundError as e:
        if e.name != f"rh.{module}":
            raise
        raise ValueError(f"rh/{module}.py is not built yet (milestone {milestone}, briefs/)") from None


def cmd_goal(args, cfg):
    goals = _optional("goals", "M6b")
    led = _ledger(cfg)
    if args.goal_cmd == "show":
        print(json.dumps(goals.rollup(args.id, led.jobs()), indent=2))
        return 0
    goal = goals.load_goal(args.file)
    specs = [contract.normalize(raw) for raw in goals.expand(goal)]
    sha = gitx.head(cfg["repo"]["path"]) if any(s["kind"] == "codex-cloud" for s in specs) else None
    for s in specs:
        led.add_job(s, pinned_sha=sha if s["kind"] == "codex-cloud" else None)
    print(f"goal {goal['id']}: added {len(specs)} job(s): {', '.join(s['id'] for s in specs)}")
    return 0


def _row(led, j):
    paused = bool(led.blocking("pause", job=j["id"], route=j["spec"]["kind"]))
    return {"id": j["id"], "state": j["state"], "paused": paused, "detail": j["detail"][:160],
            "external_id": j["external_id"]}


def _counts(jobs):
    out = {}
    for j in jobs:
        out[j["state"]] = out.get(j["state"], 0) + 1
    return out


def cmd_inbox(args, cfg):
    led = _ledger(cfg)
    if args.inbox_cmd == "resolve":
        led.inbox_resolve(args.id, args.note)
        print(f"resolved #{args.id}")
        return 0
    items = led.inbox(open_only=not args.all)
    for i in items:
        state = f"resolved: {i['resolution']}" if i["resolved"] else "open"
        print(f"#{i['id']} [{i['kind']}] {i['action']}" + (f" (job {i['job']})" if i["job"] else "") + f" - {state}")
        if i["detail"]:
            print(f"    {i['detail']}")
    if not items:
        print("inbox empty")
    return 0


def _scope(args):
    return f"job:{args.job}" if args.job else f"route:{args.route}" if args.route else "*"


def cmd_stop(args, cfg):
    _ledger(cfg).stop(args.owner, args.reason, scope=_scope(args), kind="pause" if args.pause else "stop")
    print(f"{'paused' if args.pause else 'stopped'} {_scope(args)} (owner {args.owner})")
    return 0


def cmd_resume(args, cfg):
    n = _ledger(cfg).lift(args.owner, scope=_scope(args), kind="pause" if args.pause else "stop")
    if not n:
        print(f"rh: {args.owner} holds no {'pause' if args.pause else 'stop'} on {_scope(args)}", file=sys.stderr)
        return 1
    print(f"lifted {n} on {_scope(args)}")
    return 0


def cmd_supervise(args, cfg):
    from rh.adapters.codex_cloud import CodexCloud
    from rh.supervisor import AlreadyRunning, Supervisor, single_instance
    try:
        fd = single_instance(cfg["home"])
    except AlreadyRunning as e:
        print(f"rh: {e}", file=sys.stderr)
        return 1
    if args.interval:
        cfg["supervisor"]["interval"] = args.interval
    try:
        sup = Supervisor(_ledger(cfg), cfg, cloud=CodexCloud(codex=cfg["cloud"]["codex"], cwd=_codex_cwd(cfg)))
        n = sup.run(once=args.once, ticks=args.ticks)
        print(f"supervisor ran {n} tick(s)")
        return 0
    finally:
        os.close(fd)


def cmd_cloud(args, cfg):
    led = _ledger(cfg)
    j = led.job(args.id)
    if not j:
        print(f"rh: no job {args.id}", file=sys.stderr)
        return 1
    if args.cloud_cmd == "prompt":
        p = Path(cfg["home"]) / "prompts" / f"{args.id}.txt"
        text = p.read_text() if p.is_file() else protocol.task_prompt(
            args.id, j["pinned_sha"], j["spec"]["brief"], j["spec"]["write_paths"],
            network="net" in j["spec"]["needs"])
        print(text, end="")
        print(f"sha256 {hashlib.sha256(text.encode()).hexdigest()}  (paste once; compare before sending)",
              file=sys.stderr)
        return 0
    if args.cloud_cmd == "harvest":
        if j["state"] not in ("running", "stalled", "collecting"):
            raise ValueError(f"{args.id} is {j['state']}; only a launched cloud job can be harvested")
        text = args.patch.read_text()
        if text.strip() and "diff --git" not in text:
            raise ValueError(f"{args.patch} is not a git diff")
        dest = collect.saved_patch(cfg["home"], args.id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        led.set_state(args.id, "collecting", "harvested patch; the supervisor collects it next tick")
        led.event("harvested", args.id, sha256=hashlib.sha256(text.encode()).hexdigest(), lines=text.count("\n"))
        for i in led.inbox():
            if i["kind"] == "harvest" and i["job"] == args.id:
                led.inbox_resolve(i["id"], "patch saved")
        print(f"saved {dest}; the supervisor collects {args.id} on its next tick")
        return 0
    if not TASK_RE.match(args.task_id):
        raise ValueError(f"{args.task_id!r} does not look like a cloud task id")
    if j["state"] != "handoff":
        raise ValueError(f"{args.id} is {j['state']}; only a handoff job can be attached")
    led.set_state(args.id, "launching", "attached from the web composer")
    led.set_state(args.id, "running", f"attached {args.task_id}", external_id=args.task_id, started=time.time())
    for i in led.inbox():
        if i["kind"] == "web-launch" and i["job"] == args.id:
            led.inbox_resolve(i["id"], f"attached {args.task_id}")
    print(f"attached {args.task_id} to {args.id}; the supervisor polls it from now on")
    return 0


def cmd_land(args, cfg):
    led = _ledger(cfg)
    j = led.job(args.id)
    if not j:
        print(f"rh: no job {args.id}", file=sys.stderr)
        return 1
    sha = collect.land(j, cfg["home"])
    led.event("landed", args.id, branch=f"cloud/{args.id}", commit=sha)
    print(f"landed {args.id} on cloud/{args.id} at {sha[:12]} (local only; pushing is the lead's)")
    return 0


def cmd_env(args, cfg):
    url = gitx.remote_url(cfg["repo"]["path"])
    if not url:
        raise ValueError("the repo has no origin remote")
    print(envspec.env_spec(envspec.repo_slug(url)))
    return 0


def cmd_doctor(args, cfg):
    from rh.adapters.codex_cloud import CodexCloud
    ok = True
    h = CodexCloud(codex=cfg["cloud"]["codex"], cwd=_codex_cwd(cfg)).health()
    print(f"codex cli: {'ok' if h.get('ok') else 'FAIL'} {h.get('version', '')} {h.get('detail', '')}".rstrip())
    ok &= bool(h.get("ok"))
    url = gitx.remote_url(cfg["repo"]["path"])
    print(f"git origin: {url or 'MISSING'}")
    print(f"cloud env: {cfg['cloud']['env_id'] or 'not configured (operator step: rh env spec)'}")
    try:
        r = lease.read_host()
        allowed, why = lease.admit(r, float(cfg["lease"]["max_load"]))
        print(f"host: {why}")
    except lease.HostReadError as e:
        print(f"host: FAIL {e}")
        ok = False
    led = _ledger(cfg)
    print(f"ledger: ok, {len(led.jobs())} jobs, {len(led.inbox())} open inbox items")
    hb = Path(cfg["home"]) / "supervisor.json"
    print(f"supervisor: {'last tick %ds ago' % (time.time() - json.loads(hb.read_text())['ts']) if hb.is_file() else 'never ran'}")
    print(f"notify: {'configured' if cfg['notify']['command'] else 'not configured'}")
    return 0 if ok else 1


def cmd_lease(args, cfg):
    lock = Path(cfg["lease"]["lock"])
    if args.lease_cmd == "status":
        print(json.dumps(lease.status(lock), default=str))
        return 0
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        print("rh: lease run needs -- CMD", file=sys.stderr)
        return 2
    mw = args.max_wait if args.max_wait is not None else float(cfg["lease"]["max_wait"])
    return lease.run(cmd, args.what, lock_path=lock, max_load=float(cfg["lease"]["max_load"]), max_wait=mw)


LABEL = "local.research-harness.supervise"


def cmd_service(args, cfg):
    import plistlib
    import shutil
    home = Path(cfg["home"])
    codex = shutil.which(cfg["cloud"]["codex"]) or cfg["cloud"]["codex"]
    path = os.pathsep.join(dict.fromkeys([str(Path(codex).parent), str(Path(sys.executable).parent),
                                          "/usr/bin", "/bin", "/usr/sbin", "/sbin"]))
    plist = {"Label": LABEL, "ProgramArguments": [sys.executable, "-I", str(config.REPO / "bin" / "rh"), "supervise"],
             "EnvironmentVariables": {"RH_HOME": str(home), "PATH": path}, "WorkingDirectory": str(home),
             "KeepAlive": True, "RunAtLoad": True, "ThrottleInterval": 60, "ProcessType": "Background",
             "StandardOutPath": str(home / "supervisor.out.log"), "StandardErrorPath": str(home / "supervisor.err.log")}
    home.mkdir(parents=True, exist_ok=True)
    dest = home / f"{LABEL}.plist"
    dest.write_bytes(plistlib.dumps(plist))
    print(f"wrote {dest} (not loaded)")
    print(f"load (lead's step): launchctl bootstrap gui/$(id -u) {dest}")
    print(f"undo:               launchctl bootout gui/$(id -u)/{LABEL}")
    return 0


PROBED = ("python", "git", "net", "loopback", "gpu")


def cmd_caps(args, cfg):
    home = Path(cfg["home"])
    if args.caps_cmd == "show":
        print(json.dumps(contract.load_caps(config.REPO / "caps.toml", home / "caps.toml"), indent=2))
        return 0
    j = _ledger(cfg).job(args.id)
    if not j or not j["spec"]["probe"] or j["state"] != "done":
        raise ValueError(f"{args.id} must be a done probe job")
    readings = json.loads((home / "work" / args.id / "runs" / args.id / "caps.json").read_text())
    bad = [k for k in PROBED if not isinstance(readings.get(k), bool)]
    if bad:
        raise ValueError(f"caps.json lacks boolean readings for {bad}")
    kind = j["spec"]["kind"]
    p = home / "caps.toml"
    data = tomllib.loads(p.read_text()) if p.is_file() else {}
    data[kind] = {**data.get(kind, {}), **{k: readings[k] for k in PROBED},
                  "updated": time.strftime("%Y-%m-%d"), "source": f"probe {args.id} at {(j['pinned_sha'] or '')[:12]}"}
    p.write_text(config.dumps(data))
    print(f"{kind}: " + ", ".join(f"{k}={readings[k]}" for k in PROBED) + f" -> {p}")
    return 0


def cmd_config(args, cfg):
    if args.config_cmd == "set":
        v = config.set_value(args.key, args.value)
        print(f"{args.key} = {v!r}")
        return 0
    print(json.dumps(cfg, indent=2))
    return 0


COMMANDS = {"job": cmd_job, "goal": cmd_goal, "status": cmd_status, "inbox": cmd_inbox, "stop": cmd_stop, "resume": cmd_resume,
            "supervise": cmd_supervise, "cloud": cmd_cloud, "land": cmd_land, "env": cmd_env, "doctor": cmd_doctor,
            "lease": cmd_lease, "service": cmd_service, "caps": cmd_caps, "config": cmd_config}
