#!/usr/bin/env python3
"""Unit tests for the notify_tail hook: `python3 -m unittest discover tests`"""

import importlib.util
import io
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "notify_tail.py"


class NotifyTailTest(unittest.TestCase):
    def setUp(self):
        home = Path(tempfile.mkdtemp())
        app = home / ".config" / "claude-toolkit"
        (app / "project").mkdir(parents=True)
        self.log = app / "notifications.log"
        self.state = app / "project" / "notify-tail.json"
        spec = importlib.util.spec_from_file_location("notify_tail", HOOK)
        self.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.hook)
        self.hook.NOTIFY_LOG = self.log
        self.hook.STATE_FILE = self.state

    def run_hook(self) -> str:
        """What the hook shows the reader; "" when it stays quiet."""
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(self.hook.main(), 0)
        raw = out.getvalue()
        if not raw:
            return ""
        payload = self.hook.json.loads(raw)
        # The reader sees systemMessage; the session is given the same lines.
        self.assertEqual(payload["systemMessage"],
                         payload["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        return payload["systemMessage"]

    def test_missing_log_is_silent(self):
        self.assertEqual(self.run_hook(), "")

    def test_prints_tail_then_throttles(self):
        self.log.write_text("first\nsecond\n")
        self.assertIn("second", self.run_hook())
        self.log.write_text("first\nsecond\nthird\n")
        self.assertEqual(self.run_hook(), "")

    def test_reprints_only_when_the_log_grew(self):
        self.log.write_text("first\n")
        self.run_hook()
        self.state.write_text('{"at": 1, "size": 6}')
        self.assertEqual(self.run_hook(), "")
        self.log.write_text("first\nsecond\n")
        self.assertIn("second", self.run_hook())

    def test_tail_is_bounded(self):
        self.log.write_text("".join(f"line {i}\n" for i in range(100)))
        printed = self.run_hook().splitlines()[1:]
        self.assertEqual(printed, [f"line {i}" for i in range(100 - self.hook.TAIL_LINES, 100)])

    def test_stamp_records_the_size_it_printed(self):
        self.log.write_text("first\n")
        self.run_hook()
        state = self.hook.json.loads(self.state.read_text())
        self.assertEqual(state["size"], 6)
        self.assertLess(time.time() - state["at"], 60)


if __name__ == "__main__":
    unittest.main()
