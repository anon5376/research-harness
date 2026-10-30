"""End to end through bin/rh as a user runs it: temp RH_HOME, a temp repo whose origin is a local bare repo,
the fake codex CLI, and a notify command that appends to a file."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RH = [sys.executable, "-I", str(ROOT / "bin/rh")]
FAKE = ROOT / "tests/fakes" / ("codex.cmd" if os.name == "nt" else "codex")
TASK = "task_e_" + "7" * 32


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home, self.repo, origin = base / "home", base / "repo", base / "origin.git"
        self.env = {**os.environ, "RH_HOME": str(self.home), "FAKE_CODEX_STATE": str(base / "codex.json"),
                    "FAKE_CODEX_LOG": str(base / "codex.log"), "RH_TEST_PYTHON": sys.executable}
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "core.autocrlf", "false")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "config", "core.hooksPath", "/dev/null")
        (self.repo / "README.md").write_text("base\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        git(origin, "config", "core.autocrlf", "false")
        git(self.repo, "remote", "add", "origin", str(origin))
        git(self.repo, "push", "-q", "origin", "main")
        self.sha = git(self.repo, "rev-parse", "HEAD").strip()
        self.notify_log = base / "notify.log"
        notify = json.dumps([sys.executable, "-c", "import sys; open(sys.argv[1], 'a').write(sys.argv[2] + '\\n')",
                             str(self.notify_log), "{subject}"])
        for k, v in (("cloud.env_id", "env_test"), ("cloud.codex", str(FAKE)), ("repo.path", str(self.repo)),
                     ("notify.command", notify), ("supervisor.interval", "0.1")):
            self.rh("config", "set", k, v)

    def tearDown(self):
        self.tmp.cleanup()

    def rh(self, *args, check=True, timeout=60):
        r = subprocess.run([*RH, *args], env=self.env, capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            self.fail(f"rh {' '.join(args)} exited {r.returncode}: {r.stdout}{r.stderr}")
        return r

    def job_file(self, name, text):
        p = Path(self.tmp.name) / f"{name}.toml"
        p.write_text(text)
        return str(p)

    def task_diff(self, files):
        git(self.repo, "checkout", "-q", "-b", "scratch")
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "task")
        d = git(self.repo, "diff", self.sha, "HEAD")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "branch", "-q", "-D", "scratch")
        return d

    def fake_codex(self, diff):
        ok = lambda out: {"exit": 0, "stdout": out, "stderr": ""}  # noqa: E731
        Path(self.env["FAKE_CODEX_STATE"]).write_text(json.dumps({
            "version": "codex-cli 0.0.0-fake",
            "exec": ok(f"https://chatgpt.com/codex/tasks/{TASK}\n"),
            "status": {TASK: [ok("[PENDING] c1\nenv  •  now\n"), ok("[READY] c1\nenv  •  now\n+2/-0 • 1 file\n")]},
            "diff": {TASK: ok(diff)},
            "list": ok(json.dumps({"tasks": [], "cursor": None}))}))

    @unittest.skipIf(os.name == "nt", "POSIX only: detached local-shell jobs")
    def test_local_and_cloud_jobs_reach_typed_ends_with_receipts_and_one_inbox_view(self):
        result = {"job": "c1", "state": "done", "head": self.sha, "summary": "notes written",
                  "checks": [{"cmd": "true", "exit": 0}], "outputs": ["runs/c1/notes.md"], "refusals": []}
        self.fake_codex(self.task_diff({"runs/c1/RESULT.json": json.dumps(result), "runs/c1/notes.md": "n\n"}))
        self.rh("job", "add", self.job_file("l1", 'id = "l1"\nkind = "local-shell"\nbrief = "Make a file."\n'
                                                  'command = ["sh", "-c", "echo hi > out.txt"]\n'
                                                  'accept = "test -s out.txt"\n'))
        self.rh("job", "add", self.job_file("c1", 'id = "c1"\nkind = "codex-cloud"\nbrief = "Write notes."\n'
                                                  'accept = "test -s runs/c1/notes.md"\n'))
        self.rh("job", "add", self.job_file("p1", 'id = "p1"\nkind = "local-shell"\n'
                                                  'brief = "Build the page and push it to origin."\n'
                                                  'command = ["true"]\n'))
        for _ in range(30):
            self.rh("supervise", "--once")
            st = json.loads(self.rh("status", "--json").stdout)
            ends = {r["id"]: r["state"] for r in st["recent"]}
            if ends.get("l1") and ends.get("c1"):
                break
            time.sleep(0.2)
        self.assertEqual((ends.get("l1"), ends.get("c1")), ("done", "done"), st)
        show = json.loads(self.rh("job", "show", "c1").stdout)
        self.assertEqual(show["receipt"]["pinned_sha"], self.sha)
        self.assertEqual(show["receipt"]["external_id"], TASK)
        self.assertIn(f"git checkout --detach {self.sha}", (self.home / "prompts/c1.txt").read_text())
        held = {r["id"]: r for r in st["not_started"]}
        self.assertEqual(held["p1"]["state"], "held")
        inbox = self.rh("inbox").stdout
        self.assertIn("[operator-step]", inbox)
        self.assertIn("push it to origin", inbox)
        text = self.rh("status").stdout
        self.assertIn("last 24h: done 2", text)
        sent = self.notify_log.read_text()
        self.assertIn("research-harness: c1 done", sent)
        self.assertIn("research-harness: inbox: operator-step", sent)
        land = self.rh("land", "c1").stdout
        self.assertIn("cloud/c1", land)
        self.assertEqual(git(self.repo, "rev-parse", "main").strip(), self.sha)

    def test_probe_readings_replace_unknown_capabilities(self):
        need = 'id = "n1"\nkind = "codex-cloud"\nbrief = "Start a local server and test it."\nneeds = ["loopback"]\n'
        self.rh("job", "add", self.job_file("n1", need))
        caps = {"python": True, "git": True, "net": True, "loopback": True, "gpu": False,
                "python_version": "Python 3.12.0", "head": self.sha}
        result = {"job": "probe-env-01", "state": "done", "head": self.sha, "summary": "probed",
                  "checks": [{"cmd": "python3 --version", "exit": 0}], "outputs": [], "refusals": []}
        self.fake_codex(self.task_diff({"runs/probe-env-01/RESULT.json": json.dumps(result),
                                        "runs/probe-env-01/caps.json": json.dumps(caps)}))
        self.rh("job", "add", str(ROOT / "jobs/probe.toml"))
        self.rh("supervise", "--once")
        self.assertEqual(self.state("n1"), "held")
        for _ in range(10):
            self.rh("supervise", "--once")
            if self.state("probe-env-01") == "done":
                break
        self.assertEqual(self.state("probe-env-01"), "done")
        out = self.rh("caps", "import", "probe-env-01").stdout
        self.assertIn("loopback=True", out)
        self.rh("supervise", "--once")
        self.assertNotEqual(self.state("n1"), "held", "a probed capability must release the held job")

    def test_cli_diff_failure_asks_for_a_harvest_and_a_saved_patch_completes_the_job(self):
        result = {"job": "c1", "state": "done", "head": self.sha, "summary": "ok",
                  "checks": [], "outputs": [], "refusals": []}
        patch = self.task_diff({"runs/c1/RESULT.json": json.dumps(result)})
        self.fake_codex(patch)
        state = json.loads(Path(self.env["FAKE_CODEX_STATE"]).read_text())
        state["diff"][TASK] = {"exit": 1, "stdout": "", "stderr": "Error: failed to fetch diff for task\n"}
        Path(self.env["FAKE_CODEX_STATE"]).write_text(json.dumps(state))
        self.rh("job", "add", self.job_file("c1", 'id = "c1"\nkind = "codex-cloud"\nbrief = "Write notes."\n'))
        for _ in range(4):
            self.rh("supervise", "--once")
        self.assertEqual(self.state("c1"), "collecting")
        self.assertIn("[harvest]", self.rh("inbox").stdout)
        st = json.loads(self.rh("status", "--json").stdout)
        self.assertEqual(st["routes"], [], "one task's missing diff is not a route outage")
        diff_calls = Path(self.env["FAKE_CODEX_LOG"]).read_text().count('"diff"')
        self.assertGreater(diff_calls, 0)
        self.rh("supervise", "--once")
        self.assertEqual(Path(self.env["FAKE_CODEX_LOG"]).read_text().count('"diff"'), diff_calls,
                         "a job waiting for a harvest must not re-ask the CLI")
        saved = Path(self.tmp.name) / "c1.patch"
        saved.write_text(patch)
        self.rh("cloud", "harvest", "c1", str(saved))
        self.rh("supervise", "--once")
        self.assertEqual(self.state("c1"), "done")
        show = json.loads(self.rh("job", "show", "c1").stdout)
        self.assertEqual(show["receipt"]["diff_source"], "saved patch")
        self.assertNotIn("[harvest]", self.rh("inbox").stdout)

    def test_web_job_takes_a_new_system_id_and_waits_for_a_harvest_without_asking_the_cli(self):
        result = {"job": "c1", "state": "done", "head": self.sha, "summary": "ok",
                  "checks": [], "outputs": [], "refusals": []}
        patch = self.task_diff({"runs/c1/RESULT.json": json.dumps(result)})
        self.fake_codex(patch)
        self.rh("config", "set", "cloud.route", "web")
        self.rh("job", "add", self.job_file("c1", 'id = "c1"\nkind = "codex-cloud"\nbrief = "Write notes."\n'))
        self.rh("supervise", "--once")
        self.assertEqual(self.state("c1"), "handoff")
        self.assertNotEqual(self.rh("cloud", "attach", "c1", "not-a-task", check=False).returncode, 0)
        self.rh("cloud", "attach", "c1", "01900000-0000-7000-8000-000000000001")
        for _ in range(2):
            self.rh("supervise", "--once")
        self.assertEqual(self.state("c1"), "running")
        log = Path(self.env["FAKE_CODEX_LOG"])
        self.assertNotIn('"status"', log.read_text() if log.exists() else "", "the CLI cannot see web tasks")
        self.assertEqual(json.loads(self.rh("status", "--json").stdout)["routes"], [])
        saved = Path(self.tmp.name) / "c1.patch"
        saved.write_text(patch)
        self.rh("cloud", "harvest", "c1", str(saved))
        self.rh("supervise", "--once")
        self.assertEqual(self.state("c1"), "done")

    def test_bundle_harvest_refuses_a_bad_part_and_collects_a_good_one(self):
        from tests.test_bundle import export_in_clone
        result = {"job": "c1", "state": "done", "head": self.sha, "summary": "ok",
                  "checks": [], "outputs": [], "refusals": []}
        self.fake_codex("")
        self.rh("config", "set", "cloud.route", "web")
        self.rh("job", "add", self.job_file("c1", 'id = "c1"\nkind = "codex-cloud"\nbrief = "Write notes."\n'))
        self.rh("supervise", "--once")
        self.rh("cloud", "attach", "c1", "01900000-0000-7000-8000-000000000003")
        follow_up = self.rh("cloud", "prompt", "c1", "--bundle-export").stdout
        self.assertIn(f"git reset --soft {self.sha}", follow_up)
        self.assertIn("git add -A -- runs/c1/", follow_up)
        text, _ = export_in_clone(self.repo, "c1", {"runs/c1/RESULT.json": json.dumps(result)}, self.tmp.name)
        parts = Path(self.tmp.name) / "parts"
        parts.mkdir()
        i = text.index("\n", text.index("RH-PART")) + 1
        (parts / "reply.txt").write_text(text[:i] + ("B" if text[i] != "B" else "C") + text[i + 1:])
        bad = self.rh("cloud", "harvest", "c1", "--bundle", str(parts), check=False)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("sha256 mismatch", bad.stderr)
        self.assertEqual(self.state("c1"), "running", "a refused bundle changes nothing")
        (parts / "reply.txt").write_text(text)
        self.rh("cloud", "harvest", "c1", "--bundle", str(parts))
        self.rh("supervise", "--once")
        self.assertEqual(self.state("c1"), "done")
        show = json.loads(self.rh("job", "show", "c1").stdout)
        self.assertEqual(show["receipt"]["changed_paths"], ["runs/c1/RESULT.json"])

    def state(self, jid):
        return json.loads(self.rh("job", "show", jid).stdout)["job"]["state"]

    def test_unpushed_commit_waits_for_the_lead(self):
        (self.repo / "new.md").write_text("x\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "local only")
        self.fake_codex("")
        self.rh("job", "add", self.job_file("c2", 'id = "c2"\nkind = "codex-cloud"\nbrief = "Write notes."\n'))
        self.rh("supervise", "--once")
        st = json.loads(self.rh("status", "--json").stdout)
        row = {r["id"]: r for r in st["not_started"]}["c2"]
        self.assertEqual(row["state"], "waiting")
        self.assertIn("push pending", row["detail"])
        self.assertFalse(Path(self.env["FAKE_CODEX_LOG"]).exists() and "exec" in Path(
            self.env["FAKE_CODEX_LOG"]).read_text(), "nothing may launch before the push")

    def test_second_supervisor_exits_at_once(self):
        first = subprocess.Popen([*RH, "supervise", "--ticks", "100", "--interval", "0.1"], env=self.env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                if (self.home / "supervisor.json").exists():
                    break
                time.sleep(0.1)
            t0 = time.time()
            r = self.rh("supervise", "--once", check=False)
            self.assertEqual(r.returncode, 1)
            self.assertIn("another supervisor", r.stderr)
            self.assertLess(time.time() - t0, 2.0)
        finally:
            first.kill()
            first.wait()

    def test_service_plist_is_written_but_never_loaded(self):
        import plistlib
        out = self.rh("service", "plist").stdout
        self.assertIn("not loaded", out)
        self.assertIn("launchctl bootstrap", out)
        plist = plistlib.loads((self.home / "local.research-harness.supervise.plist").read_bytes())
        self.assertTrue(plist["KeepAlive"])
        self.assertEqual(plist["ProgramArguments"][-2:], [str(ROOT / "bin/rh"), "supervise"])
        self.assertEqual(plist["EnvironmentVariables"]["RH_HOME"], str(self.home.resolve()))
        self.assertIn(str(FAKE.parent), plist["EnvironmentVariables"]["PATH"].split(os.pathsep))

    def test_stop_is_owned(self):
        self.rh("stop", "--owner", "lead", "--reason", "night")
        r = self.rh("resume", "--owner", "guard", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("STOP * by lead: night", self.rh("status").stdout)
        self.rh("resume", "--owner", "lead")
        self.assertNotIn("STOP", self.rh("status").stdout)


if __name__ == "__main__":
    unittest.main()
