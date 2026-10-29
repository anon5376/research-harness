import sys
import tempfile
import unittest
from pathlib import Path

from rh import notify


class NotifyTest(unittest.TestCase):
    def test_placeholders_reach_the_command_and_receipt_is_ok(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "sent"
            cmd = [sys.executable, "-c", "import sys; open(sys.argv[1], 'w').write(sys.argv[2] + '|' + sys.argv[3])",
                   str(out), "{subject}", "body: {body}"]
            r = notify.send(cmd, "job done", "j1 ended done")
            self.assertEqual((r["ok"], r["exit"]), (True, 0))
            self.assertEqual(out.read_text(), "job done|body: j1 ended done")

    def test_failures_are_receipts_not_exceptions(self):
        self.assertFalse(notify.send([], "s", "b")["ok"])
        r = notify.send([sys.executable, "-c", "import sys; print('bus refused'); sys.exit(3)"], "s", "b")
        self.assertEqual((r["ok"], r["exit"], r["out"]), (False, 3, "bus refused"))
        self.assertIn("not found", notify.send(["/nonexistent/notifier"], "s", "b")["out"])
        r = notify.send([sys.executable, "-c", "import time; time.sleep(5)"], "s", "b", timeout=0.3)
        self.assertEqual((r["ok"], r["exit"]), (False, None))
        self.assertIn("timed out", r["out"])


if __name__ == "__main__":
    unittest.main()
