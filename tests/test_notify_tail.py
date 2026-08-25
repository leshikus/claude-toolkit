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
        self.picks = app / "backlog-picks.txt"
        self.state = app / "project" / "notify-tail.json"
        spec = importlib.util.spec_from_file_location("notify_tail", HOOK)
        self.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.hook)
        self.hook.NOTIFY_LOG = self.log
        self.hook.PICKS_FILE = self.picks
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
        printed = [r.strip() for r in self.run_hook().splitlines()[1:]]
        self.assertEqual(printed, [f"line {i}" for i in range(100 - self.hook.TAIL_LINES, 100)])

    def test_the_picks_print_as_their_own_section(self):
        self.picks.write_text("oldest — A (u1)\nhighest — B (u2)\n")
        out = self.run_hook()
        self.assertIn("backlog\n  oldest — A (u1)\n  highest — B (u2)", out)

    def test_a_changed_pick_speaks_even_though_the_log_is_quiet(self):
        self.log.write_text("first\n")
        self.picks.write_text("oldest — A (u1)\n")
        self.run_hook()
        self.state.write_text(self.hook.json.dumps(
            {"at": 1, "size": 6, "picks": "oldest — A (u1)"}))
        self.assertEqual(self.run_hook(), "")   # nothing moved
        self.picks.write_text("oldest — B (u2)\n")
        self.assertIn("oldest — B (u2)", self.run_hook())

    def test_the_picks_repeat_with_each_replay_rather_than_stacking_up(self):
        """They are state: the current pair, not one line per selection cycle."""
        self.picks.write_text("oldest — A (u1)\n")
        self.log.write_text("first\n")
        first = self.run_hook()
        self.state.write_text(self.hook.json.dumps({"at": 1, "size": 6, "picks": ""}))
        self.log.write_text("first\nsecond\n")
        second = self.run_hook()
        for out in (first, second):
            self.assertEqual(out.count("oldest — A (u1)"), 1)

    def test_a_url_gets_a_row_to_itself(self):
        self.log.write_text(
            "2026-08-25 12:00:00  ~/repos/x — A title (https://github.com/o/r/pull/7)\n")
        self.assertEqual(self.run_hook().splitlines()[1:],
                         ["  12:00  ~/repos/x — A title",
                          "      https://github.com/o/r/pull/7"])

    def test_a_line_without_a_url_stays_one_row(self):
        self.log.write_text("2026-08-25 12:00:00  hint x — Install node\n")
        self.assertEqual(self.run_hook().splitlines()[1:], ["  12:00  hint x — Install node"])

    def test_the_date_and_seconds_are_dropped_from_the_stamp(self):
        self.log.write_text("2026-08-25 09:07:42  hint x — y\n")
        self.assertEqual(self.run_hook().splitlines()[1:], ["  09:07  hint x — y"])

    def test_stamp_records_the_size_it_printed(self):
        self.log.write_text("first\n")
        self.run_hook()
        state = self.hook.json.loads(self.state.read_text())
        self.assertEqual(state["size"], 6)
        self.assertLess(time.time() - state["at"], 60)


if __name__ == "__main__":
    unittest.main()
