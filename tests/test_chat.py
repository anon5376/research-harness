"""Chat jobs through the contract, supervisor, real CLI and authenticated loopback server."""
import contextlib
import http.client
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from rh import chat, cli, config, contract, serve
from rh.ledger import Ledger
from rh.supervisor import Supervisor

ROOT = Path(__file__).resolve().parents[1]
POLICY = contract.load_policy(ROOT / "policy.toml")
BRIEF = "  Compare the supplied sources.\nKeep uncertainty explicit.\n"


def spec(job_id="chat-job", **extra):
    return contract.normalize({"id": job_id, "kind": "gpt-chat", "brief": BRIEF, **extra})


class RecordingCloud:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def unexpected(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"a chat job called Codex: {name}")
        return unexpected


class ChatContractTest(unittest.TestCase):
    def test_chat_defaults_and_verbatim_prompt(self):
        job = spec()
        self.assertEqual(job["model"]["requested"], "gpt-6.1-sol-pro")
        self.assertEqual(job["write_root"], "runs/chat-job/")
        self.assertNotIn("command", job)
        self.assertNotIn("accept", job)
        self.assertEqual(chat.prompt(job), BRIEF)
        self.assertEqual(contract.preflight(job, {}, POLICY, contract.View()).verdict, "ready")

    def test_command_and_accept_are_refused_even_when_empty(self):
        cases = (("command", [], "a chat job has no command"),
                 ("accept", "", "a chat job has no check: its reply is the result"))
        for key, value, message in cases:
            with self.subTest(key=key), self.assertRaises(contract.ContractError) as error:
                spec(**{key: value})
            self.assertEqual(str(error.exception), message)
        with self.assertRaisesRegex(contract.ContractError, "chat jobs write to runs/<id>/"):
            spec(write_root="elsewhere/")

    def test_chat_models_speed_and_effort_use_policy(self):
        for model in ("gpt-6.1-sol-pro", "gpt-6-pro"):
            with self.subTest(model=model):
                decision = contract.preflight(spec(model={"requested": model}), {}, POLICY, contract.View())
                self.assertEqual(decision.verdict, "ready")
        for model, reason in (({"requested": "outside-policy"}, "model outside-policy"),
                              ({"speed": "fast"}, "speed fast"),
                              ({"effort": "max"}, "effort max")):
            with self.subTest(model=model):
                decision = contract.preflight(spec(model=model), {}, POLICY, contract.View())
                self.assertEqual(decision.verdict, "hold")
                self.assertTrue(any(reason + " is not in the policy" in r for r in decision.reasons))


class ChatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.cfg = config.load(self.home)
        self.now = 1_000_000.0
        self.led = Ledger(self.home / "ledger.db", clock=lambda: self.now)
        self.addCleanup(self.led.close)
        self.cloud = RecordingCloud()
        self.sup = Supervisor(self.led, self.cfg, cloud=self.cloud, caps={}, policy=POLICY,
                              clock=lambda: self.now)

    def handoff(self, job_id="chat-job"):
        self.led.add_job(spec(job_id))
        self.sup.tick()
        self.assertEqual(self.led.job(job_id)["state"], "handoff")

    def rh(self, *args, text=None):
        return subprocess.run([sys.executable, "-I", str(ROOT / "bin/rh"), *args],
                              env={**os.environ, "RH_HOME": str(self.home)}, input=text,
                              capture_output=True, text=True, encoding="utf-8", timeout=15)

    def snapshot(self, job_id):
        return (self.led.job(job_id), self.led.receipt(job_id), self.led.events(),
                self.led.inbox(open_only=False), chat.read_reply(self.cfg, job_id))

    def test_tick_hands_off_once_without_codex_pacing_or_stalling(self):
        self.led.add_job(spec())
        self.led.stop("test", "cloud stopped", scope="route:codex-cloud")
        self.led.stop("test", "cloud paused", scope="route:codex-cloud", kind="pause")
        self.cfg["cloud"].update(launches_per_tick=0, max_per_hour=0)
        self.led.route_fail("codex-cloud", "unavailable", retry_after=99999)
        self.assertEqual(self.sup.tick(), {"launched": 0, "finished": 0})
        job = self.led.job("chat-job")
        self.assertEqual((job["state"], job["detail"]),
                         ("handoff", "Waiting for the reply from ChatGPT"))
        moves = [(e["data"]["from"], e["data"]["to"]) for e in self.led.events("chat-job")
                 if e["kind"] == "state"]
        self.assertEqual(moves, [("draft", "ready"), ("ready", "launching"), ("launching", "handoff")])
        self.now += 86400
        self.sup.tick()
        items = self.led.inbox()
        self.assertEqual([(i["kind"], i["job"], i["action"]) for i in items],
                         [("chat", "chat-job", "Ask ChatGPT (gpt-6.1-sol-pro): copy the prompt, paste its reply")])
        self.assertEqual(self.led.job("chat-job")["state"], "handoff")
        self.assertEqual(self.led.launches_since("codex-cloud", 0), [])
        self.assertEqual(self.led.launches_since("chat", 0), [])
        self.assertEqual(self.cloud.calls, [])

    def test_restart_in_ledger_launching_step_recovers_chat_handoff(self):
        self.led.add_job(spec())
        self.led.set_state("chat-job", "ready")
        self.led.set_state("chat-job", "launching")
        self.sup.recover()
        self.sup.tick()
        self.assertEqual(self.led.job("chat-job")["state"], "handoff")
        self.assertEqual([i["kind"] for i in self.led.inbox()], ["chat"])
        self.assertEqual(self.cloud.calls, [])

    def test_cli_prompt_and_file_reply_complete_with_receipt_and_inbox_resolution(self):
        job_file = self.home / "job.toml"
        job_file.write_text('id = "chat-job"\nkind = "gpt-chat"\nbrief = """' + BRIEF + '"""\n',
                            encoding="utf-8")
        added = self.rh("job", "add", str(job_file))
        self.assertEqual(added.returncode, 0, added.stderr)
        self.sup.tick()
        printed = self.rh("chat", "prompt", "chat-job")
        self.assertEqual((printed.returncode, printed.stdout), (0, BRIEF))
        reply = "The evidence is mixed — uncertain.\nA second sentence adds detail.\n"
        path = self.home / "incoming.md"
        path.write_bytes(reply.replace("\n", "\r\n").encode("utf-8"))  # a reply saved by a Windows editor
        result = self.rh("chat", "reply", "chat-job", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.led.job("chat-job")["state"], "done")
        self.assertEqual(chat.reply_path(self.cfg, "chat-job"), self.home / "runs/chat-job/reply.md")
        self.assertEqual(chat.reply_path(self.cfg, "chat-job").read_bytes(), reply.encode("utf-8"))  # stored with \n
        receipt = self.led.receipt("chat-job")
        self.assertIsInstance(receipt["ended"], (float, int))
        self.assertEqual({**receipt, "ended": 123.0},
                         {"route": "chat", "checks": [], "model_requested": "gpt-6.1-sol-pro",
                          "model_served": None, "ended": 123.0, "detail": "The evidence is mixed — uncertain."})
        events = [e["data"] for e in self.led.events("chat-job") if e["kind"] == "reply"]
        self.assertEqual(events, [{"chars": len(reply)}])
        self.assertEqual(self.led.inbox(), [])
        self.assertEqual(self.led.inbox(open_only=False)[0]["resolution"], "reply saved")
        before = self.snapshot("chat-job")
        second = self.rh("chat", "reply", "chat-job", "-", text="A replacement.")
        self.assertEqual(second.returncode, 1)
        self.assertTrue(second.stderr.startswith("rh: "))
        self.assertEqual(self.snapshot("chat-job"), before)

    def test_cli_refuses_empty_wrong_kind_and_wrong_state_without_changes(self):
        self.handoff()
        self.led.add_job(spec("draft-chat"))
        self.led.add_job(contract.normalize({"id": "local-job", "kind": "local-shell",
                                            "brief": "Read inputs.", "command": ["true"]}))
        self.led.add_job(contract.normalize({"id": "cloud-job", "kind": "codex-cloud", "brief": "Read inputs."}))
        for state in ("ready", "launching", "handoff"):
            self.led.set_state("cloud-job", state)
        cases = (("chat-job", " \n\t", "empty"), ("local-job", "A reply.", "gpt-chat"),
                 ("cloud-job", "A reply.", "gpt-chat"), ("draft-chat", "A reply.", "handoff"))
        for job_id, text, message in cases:
            with self.subTest(job=job_id):
                before = self.snapshot(job_id)
                result = self.rh("chat", "reply", job_id, "-", text=text)
                self.assertEqual(result.returncode, 1)
                self.assertTrue(result.stderr.startswith("rh: "))
                self.assertIn(message, result.stderr)
                self.assertEqual(self.snapshot(job_id), before)
                self.assertFalse(chat.reply_path(self.cfg, job_id).parent.exists())

    def test_stdin_reply_and_bounded_read_preserve_unicode(self):
        self.handoff()
        self.assertIsNone(chat.read_reply(self.cfg, "chat-job"))
        text = "é" * 220 + ". Next sentence.\n"
        result = self.rh("chat", "reply", "chat-job", "-", text=text)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(chat.read_reply(self.cfg, "chat-job"), text)
        self.assertEqual(chat.read_reply(self.cfg, "chat-job", limit=5), "é" * 5)
        self.assertEqual(self.led.receipt("chat-job")["detail"], "é" * 200)

    def test_stdin_reply_is_utf8_whatever_the_locale(self):
        # A reply on stdin decoded in the locale's encoding (cp1252 on Windows), or a lone CR or CRLF kept in the
        # saved reply, must fail.
        self.handoff()
        text = "Design B wins — it keeps one clock.\r\nSecond line.\rThird.\n"
        stdin = io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="latin-1", newline="")
        with patch.object(cli.config, "load", return_value=self.cfg), patch.object(cli, "_ledger", return_value=self.led), \
                patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["chat", "reply", "chat-job", "-"]), 0)
        saved = "Design B wins — it keeps one clock.\nSecond line.\nThird.\n"
        self.assertEqual(chat.reply_path(self.cfg, "chat-job").read_bytes(), saved.encode("utf-8"))
        self.assertEqual([e["data"] for e in self.led.events("chat-job") if e["kind"] == "reply"],
                         [{"chars": len(saved)}])

    def test_rename_failure_leaves_job_waiting_and_no_partial_reply(self):
        self.handoff()
        before = self.snapshot("chat-job")
        with patch("rh.chat.Path.replace", side_effect=OSError("rename failed")):
            with self.assertRaisesRegex(OSError, "rename failed"):
                chat.save_reply(self.cfg, self.led, "chat-job", "Complete reply.", now=123.0)
        self.assertEqual(self.snapshot("chat-job"), before)
        self.assertEqual(list(chat.reply_path(self.cfg, "chat-job").parent.iterdir()), [])

    def test_saved_reply_records_the_supplied_end_time(self):
        self.handoff()
        chat.save_reply(self.cfg, self.led, "chat-job", "Answer! Further detail.", now=123.0)
        self.assertEqual(self.led.receipt("chat-job")["ended"], 123.0)
        self.assertEqual(self.led.receipt("chat-job")["detail"], "Answer!")

    def test_real_server_reply_requires_form_token_then_saves_through_cli(self):
        self.handoff()
        server, _ = serve.make_server(self.cfg, checks=False, token="chat-test-key")
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
        thread.start()
        try:
            def post(fields):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                try:
                    connection.request("POST", "/reply", urlencode(fields),
                                       {"Content-Type": "application/x-www-form-urlencoded"})
                    response = connection.getresponse()
                    response.read()
                    return response.status
                finally:
                    connection.close()

            fields = {"job": "chat-job", "text": "A web reply. More detail."}
            before = self.snapshot("chat-job")
            self.assertEqual(post(fields), 403)
            self.assertEqual(self.snapshot("chat-job"), before)
            self.assertFalse((self.home / "uploads").exists())
            self.assertEqual(post({**fields, "t": "chat-test-key"}), 303)
            self.assertEqual(self.led.job("chat-job")["state"], "done")
            self.assertEqual(chat.read_reply(self.cfg, "chat-job"), fields["text"])
            self.assertEqual(self.led.receipt("chat-job")["detail"], "A web reply.")
            self.assertEqual(self.led.inbox(), [])
            # The job's page reads the saved reply as GPT Pro's message.
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("GET", "/job/chat-job?t=chat-test-key")
                page = connection.getresponse().read().decode("utf-8")
            finally:
                connection.close()
            self.assertIn('<section class="msg ai"', page)
            self.assertIn("A web reply. More detail.", page)
            self.assertEqual(serve._say("/reply", {"job": ["chat-job"]}, True),
                             "Saved the reply. chat-job is done.")
            self.assertEqual(serve._say("/reply", {"job": ["chat-job"]}, False),
                             "The reply was not saved.")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
