"""The one launcher. Each tick: preflight, owned stops and pauses, route health with backoff probes, launch,
poll, collect, notify, heartbeat. It never writes a stop: launch failures back off; only owners stop work."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from pathlib import Path

from rh import collect, contract, gitx, lease, notify, protocol
from rh.adapters import local_shell
from rh.adapters.codex_cloud import RouteUnavailable
from rh.config import REPO
from rh.ledger import LIVE

CLOUD, LOCAL = "codex-cloud", "local-shell"
PRE = {"draft", "waiting", "held", "ready"}
_TARGET = {"ready": "ready", "wait": "waiting", "hold": "held", "operator": "held"}
ENV_RETRY = 86400  # after `env not found`, skip the CLI for this environment for a day
OUTAGES = {"timeout", "rate-limited", "cli-missing", "auth"}  # route-wide failures; others are about one task
HARVEST = "harvest needed"
NOT_CREATED = {"rate-limited", "cli-missing", "auth"}  # launch errors that certainly created no task


class AlreadyRunning(RuntimeError):
    pass


def single_instance(home):
    """Hold this fd for the supervisor's lifetime; a second supervisor gets AlreadyRunning at once."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(home / "supervisor.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        hb = home / "supervisor.json"
        who = json.loads(hb.read_text()).get("pid") if hb.is_file() else "unknown"
        raise AlreadyRunning(f"another supervisor holds {home / 'supervisor.lock'} (pid {who})") from None
    return fd


class Supervisor:
    def __init__(self, ledger, cfg, *, cloud=None, caps=None, policy=None, clock=time.time, send=notify.send,
                 gate_runner=None, pinned=None):
        self.led, self.cfg, self.cloud, self.clock, self.send = ledger, cfg, cloud, clock, send
        self.home, self.repo = Path(cfg["home"]), Path(cfg["repo"]["path"])
        self.caps = caps if caps is not None else contract.load_caps(REPO / "caps.toml", self.home / "caps.toml")
        self.policy = policy if policy is not None else contract.load_policy(REPO / "policy.toml")
        self.gate_runner = gate_runner
        self.pinned = pinned or (lambda sha: gitx.on_remote(self.repo, sha))
        self.probes = self.launch_attempts = 0
        self._faults = set()

    def run(self, once=False, ticks=None, sleep=time.sleep):
        self.recover()
        n = 0
        while True:
            self.tick()
            n += 1
            if once or (ticks and n >= ticks):
                return n
            sleep(self.cfg["supervisor"]["interval"])

    def recover(self):
        """Jobs left in `launching` by a supervisor that died mid-launch. Call only while holding single_instance."""
        for j in self.led.jobs(states={"launching"}):
            if j["spec"]["kind"] == LOCAL:
                runner = self.home / "jobs" / j["id"] / "runner.json"
                pid = json.loads(runner.read_text())["pid"] if runner.is_file() else None
                if pid:
                    self.led.set_state(j["id"], "running", f"recovered after restart: pid {pid}", route=LOCAL,
                                       external_id=str(pid), started=j.get("started") or self.clock())
                else:
                    self.led.set_state(j["id"], "ready", "recovered after restart: the runner never started")
            else:
                self._handoff(j, self.cfg["cloud"]["env_id"], "supervisor restarted mid-launch, so a task may "
                                                              "exist; check codex cloud list before launching")

    def _isolated(self, j, fn, *args):
        """Run one job's step; a harness error parks that job instead of stopping the whole supervisor."""
        try:
            return fn(j, *args)
        except Exception as e:  # noqa: BLE001  (any fault in one job must not stop the others)
            why = f"harness error: {type(e).__name__}: {str(e)[:200]}"
            cur = self.led.job(j["id"])
            if cur and cur["state"] in PRE | {"launching"} and cur["detail"] != why:
                self.led.set_state(j["id"], "held", why)
            if (j["id"], why) not in self._faults:
                self._faults.add((j["id"], why))
                self.led.event("job-error", j["id"], error=why)
                self._inbox("harness-error", why, j["id"], "fix the job or the harness; the job is parked")
            return None

    def tick(self):
        s = {"launched": 0, "finished": 0}
        self._preflight_all()
        self._launch_ready(s)
        self._poll_live(s)
        self._heartbeat(s)
        return s

    # preflight
    def _preflight_all(self):
        jobs = self.led.jobs()
        states = {j["id"]: j["state"] for j in jobs}
        live = {j["id"]: [j["spec"]["write_root"], *j["spec"]["write_paths"]] for j in jobs if j["state"] in LIVE}
        for j in jobs:
            if j["state"] in PRE:
                self._isolated(j, self._preflight_one, states, live)
        if any(j["spec"]["kind"] == CLOUD and j["state"] in PRE for j in jobs) and not self.cfg["cloud"]["env_id"]:
            self._inbox("operator-step", "create and publish the cloud environment (rh env spec), "
                                         "then rh config set cloud.env_id <ID>", None, "cloud jobs wait for it")

    def _preflight_one(self, j, states, live):
        view = contract.View(states=states, live_roots=live, input_root=self.repo, pinned=self._pin(j))
        d = contract.preflight(j["spec"], self.caps, self.policy, view)
        target, detail = _TARGET[d.verdict], "; ".join(d.reasons)
        if (j["state"], j["detail"]) != (target, detail):
            self.led.set_state(j["id"], target, detail)
        for step in d.operator_steps:
            self._inbox("operator-step", step, j["id"], "split this step out of the brief; the job stays held")

    def _pin(self, job):
        if job["spec"]["kind"] != CLOUD:
            return True, ""
        if not self.cfg["cloud"]["env_id"]:
            return False, "no cloud environment configured yet (operator step)"
        return self.pinned(job["pinned_sha"])

    # launch
    def _launch_ready(self, s):
        budget = int(self.cfg["cloud"]["launches_per_tick"])
        for j in self.led.jobs(states={"ready"}):
            route = j["spec"]["kind"]
            if self.led.blocking("stop", job=j["id"], route=route) or self.led.blocking("pause", job=j["id"],
                                                                                         route=route):
                continue
            if self._overlaps_live(j):
                continue
            if route == CLOUD:
                if budget <= 0 or not self._route_open(CLOUD):
                    continue
                budget -= 1
                if self._isolated(j, self._launch_cloud):
                    s["launched"] += 1
            elif self._isolated(j, self._launch_local) is not None:
                s["launched"] += 1

    def _overlaps_live(self, job):
        mine = [job["spec"]["write_root"], *job["spec"]["write_paths"]]
        for other in self.led.jobs(states=LIVE):
            theirs = [other["spec"]["write_root"], *other["spec"]["write_paths"]]
            if any(contract._overlap(r, t) for r in mine for t in theirs):
                return True
        return False

    def _route_open(self, name):
        r = self.led.route(name)
        if r["healthy"]:
            return True
        if self.clock() < r["next_try"]:
            return False
        self.probes += 1
        h = self.cloud.health()
        if h.get("ok"):
            self.led.route_ok(name)
            return True
        self.led.route_fail(name, str(h.get("detail", "health probe failed"))[:120])
        return False

    def _launch_cloud(self, j):
        jid, spec, env = j["id"], j["spec"], self.cfg["cloud"]["env_id"]
        prompt = protocol.task_prompt(jid, j["pinned_sha"], spec["brief"], spec["write_paths"],
                                      network="net" in spec["needs"])
        pdir = self.home / "prompts"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / f"{jid}.txt").write_text(prompt)
        self.led.event("prompt", jid, sha256=hashlib.sha256(prompt.encode()).hexdigest(), chars=len(prompt))
        self.led.set_state(jid, "launching")
        envroute = self.led.route(f"cli-env:{env}")
        if self.cfg["cloud"]["route"] == "web" or (
                envroute["last_status"] == "env-not-found" and self.clock() < envroute["next_try"]):
            self._handoff(j, env, "web route" if self.cfg["cloud"]["route"] == "web" else "CLI cannot resolve env")
            return False
        self.launch_attempts += 1
        try:
            r = self.cloud.launch(env, prompt, self.cfg["cloud"]["branch"])
        except RouteUnavailable as e:
            if e.reason == "env-not-found":
                self.led.route_fail(f"cli-env:{env}", "env-not-found", retry_after=ENV_RETRY)
                self._handoff(j, env, "CLI cannot resolve env")
                return False
            if e.reason not in NOT_CREATED:
                # timeout or an unparsed reply: the task may exist, and a relaunch could duplicate it
                self._handoff(j, env, f"launch outcome unknown ({e.reason}: {e.detail[:120]}); check codex cloud "
                                      f"list for this job's task before launching again")
                return False
            self.led.route_fail(CLOUD, e.reason, e.retry_after)
            self.led.set_state(jid, "ready", f"launch deferred: {e.reason}")
            if e.reason in ("auth", "cli-missing"):
                self._inbox("operator-step", "fix the codex CLI: " + (
                    "sign it in (codex login)" if e.reason == "auth" else "it is not installed or not on PATH"),
                    None, e.detail[:200])
            return False
        self.led.route_ok(CLOUD)
        self.led.route_ok(f"cli-env:{env}")
        self.led.set_state(jid, "running", f"launched {r['task_id']}", route="codex-cloud-cli",
                           external_id=r["task_id"], external_url=r.get("url"), started=self.clock())
        return True

    def _handoff(self, j, env, why):
        jid = j["id"]
        self.led.set_state(jid, "handoff", f"{why}: rh cloud prompt {jid}, submit it at chatgpt.com/codex in "
                                           f"environment {env}, then rh cloud attach {jid} <task_id>",
                           route="codex-cloud-web")
        self._inbox("web-launch", f"launch {jid} in the web composer, then rh cloud attach {jid} <task_id>", jid, why)

    def _launch_local(self, j):
        jid, spec = j["id"], j["spec"]
        self.led.set_state(jid, "launching")
        pid = local_shell.start(self.home / "jobs" / jid, self.home / spec["write_root"], spec["command"],
                                heavy=spec["heavy"], lease_cfg=self.cfg["lease"])
        self.led.set_state(jid, "running", f"pid {pid}", route=LOCAL, external_id=str(pid), started=self.clock())
        return True

    # poll and collect
    def _poll_live(self, s):
        for j in self.led.jobs(states={"running", "stalled", "collecting"}):
            if j["route"] == LOCAL:
                self._isolated(j, self._poll_local, s)
            elif j["route"] == "codex-cloud-web" and j["state"] != "collecting":
                self._isolated(j, self._alive)  # the CLI cannot see web tasks: wait for rh cloud harvest
            elif self._route_open(CLOUD):
                self._isolated(j, self._poll_cloud, s)

    def _poll_cloud(self, j, s):
        jid = j["id"]
        if j["state"] != "collecting":
            try:
                st = self.cloud.status(j["external_id"])
            except RouteUnavailable as e:
                self.led.route_fail(CLOUD, e.reason, e.retry_after)
                return
            if st["state"] == "running":
                return self._alive(j)
            if st["state"] in ("failed", "aborted"):
                return self._finish(j, st["state"], collect.receipt(
                    j, CLOUD, [], detail=f"cloud task {st.get('raw', '')}: {st.get('detail', '')}"[:300]), s)
            if st["state"] != "completed":
                note = f"unknown cloud status {st.get('raw')!r}"
                if j["detail"] != note:
                    self.led.event("unknown-status", jid, raw=st.get("raw"))
                    self.led.set_state(jid, j["state"], note)
                return self._alive(j)
            self.led.set_state(jid, "collecting", "task completed; collecting")
            j = self.led.job(jid)
        if j["detail"].startswith(HARVEST) and not collect.saved_patch(self.home, jid).is_file():
            return
        try:
            state, rec = collect.collect_cloud(j, self.cloud, self.repo, self.home, self._gate(j))
        except RouteUnavailable as e:
            if e.reason in OUTAGES:
                self.led.route_fail(CLOUD, e.reason, e.retry_after)
                return
            # the CLI cannot export this task's diff (new-system tasks): a browser session saves the staged diff
            self.led.set_state(jid, "collecting", f"{HARVEST}: {e.detail[:120]}")
            self._inbox("harvest", f"save the staged diff of {jid} from the web view, then "
                                   f"rh cloud harvest {jid} <patch file>", jid, e.detail[:200])
            return
        self._finish(j, state, rec, s)
        for text in rec.get("refusals", []):
            self._inbox("refusal", text, jid, "the worker stopped at a refusal; it is never retried or reworded")

    def _poll_local(self, j, s):
        run_dir = self.home / "jobs" / j["id"]
        p = local_shell.poll(run_dir, int(j["external_id"] or 0))
        if p["state"] == "running":
            return self._alive(j)
        if p["state"] == "vanished":
            return self._finish(j, "aborted", collect.receipt(
                j, LOCAL, [], detail="runner ended without an exit record", log_tail=local_shell.tail(run_dir)), s)
        state, rec = collect.collect_local(j, p["exit"], self.home / j["spec"]["write_root"],
                                           local_shell.tail(run_dir), self._gate(j))
        self._finish(j, state, rec, s)

    def _gate(self, j):
        if self.gate_runner or not j["spec"]["heavy"]:
            return self.gate_runner
        lc = self.cfg["lease"]
        return lambda cmd, cwd: lease.run(["sh", "-c", f"cd \"$1\" && {cmd}", "gate", str(cwd)], f"gate {j['id']}",
                                          lock_path=Path(lc["lock"]), max_load=float(lc["max_load"]),
                                          max_wait=float(lc["max_wait"]))

    def _alive(self, j):
        self.led.heartbeat(j["id"])
        if self.led.blocking("pause", job=j["id"], route=j["spec"]["kind"]):
            return  # a paused job is paused, not stalled
        started = j.get("started") or j["created"]
        if j["state"] == "running" and self.clock() - started > j["spec"]["timeout_min"] * 60:
            self.led.set_state(j["id"], "stalled", f"no finish after {j['spec']['timeout_min']} min")
            self._notify(f"{j['id']} stalled", f"{j['id']} has run past its {j['spec']['timeout_min']} min timeout")

    def _finish(self, j, state, rec, s):
        rec["ended"] = self.clock()
        self.led.finish(j["id"], state, rec)
        s["finished"] += 1
        self._notify(f"{j['id']} {state}", f"{j['id']} ended {state}: {rec.get('detail', '')}")

    # reporting
    def _inbox(self, kind, action, job, detail):
        _, new = self.led.inbox_add(kind, action, job=job, detail=detail)
        if new:
            self._notify(f"inbox: {kind}", f"{action}" + (f" (job {job})" if job else ""))

    def _notify(self, subject, body):
        r = self.send(self.cfg["notify"]["command"], f"research-harness: {subject}", body)
        self.led.event("notify", subject=subject, ok=r["ok"], exit=r["exit"], out=r["out"])
        if not r["ok"] and self.cfg["notify"]["command"]:
            self.led.inbox_add("notify-failed", subject, detail=r["out"])

    def _heartbeat(self, s):
        hb = {"pid": os.getpid(), "ts": self.clock(), "tick": s, "probes": self.probes,
              "launch_attempts": self.launch_attempts}
        tmp = self.home / "supervisor.json.tmp"
        tmp.write_text(json.dumps(hb))
        tmp.replace(self.home / "supervisor.json")

