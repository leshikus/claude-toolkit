#!/usr/bin/env python3
"""Unit tests for monitor.BacklogPicksEvent: `python3 -m unittest discover tests`"""

import sched
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import monitor  # noqa: E402
from term import Hyperlink  # noqa: E402

OLD = "https://github.com/ClickHouse/ClickHouse/issues/46502"
HOT = "https://github.com/ClickHouse/ClickHouse/pull/115607"
TITLE = "Make a status for failing ccache"
BOTH = f"oldest: {OLD}\nhighest: {HOT}"


class BacklogPicksEventTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(mock.patch.stopall)
        self.picks = tmp / "backlog-picks.txt"
        self.state = tmp / "backlog-picks.json"
        mock.patch.multiple(monitor, NOTIFY_LOG=tmp / "notifications.log",
                            PICKS_FILE=self.picks, PICKS_STATE_FILE=self.state,
                            PICKS_DOC=tmp / "doc.md").start()
        mock.patch.object(monitor, "_user_active", return_value=True).start()
        (tmp / "doc.md").write_text("pick the oldest")
        self.gh = mock.patch.object(monitor, "_gh_json", return_value={"title": TITLE}).start()

    def fire(self, answer, event=None) -> str:
        """Run one cycle against a model that returns `answer`; return the picks file."""
        with mock.patch.object(monitor, "_run_claude", return_value=answer) as run:
            (event or monitor.BacklogPicksEvent(sched.scheduler(time.time, time.sleep))).fire()
            self.calls = run.call_count
        return self.picks.read_text() if self.picks.exists() else ""

    def test_posts_both_picks(self):
        out = self.fire(BOTH)
        self.assertIn(f"oldest — {Hyperlink.format(TITLE, OLD)}", out)
        self.assertIn(f"highest — {Hyperlink.format(TITLE, HOT)}", out)

    def test_one_item_that_is_both_prints_once(self):
        out = self.fire(f"oldest: {OLD}\nhighest: {OLD}")
        self.assertIn("oldest + highest —", out)
        self.assertEqual(len(out.splitlines()), 1)

    def test_oldest_prints_before_highest(self):
        lines = self.fire(f"highest: {HOT}\noldest: {OLD}").splitlines()
        self.assertIn("oldest —", lines[0])
        self.assertIn("highest —", lines[1])

    def test_a_label_it_could_not_fill_is_skipped(self):
        out = self.fire(f"oldest: {OLD}")
        self.assertIn("oldest —", out)
        self.assertNotIn("highest", out)

    def test_untitled_item_still_links(self):
        self.gh.return_value = None
        self.assertIn(f"oldest — {OLD}", self.fire(f"oldest: {OLD}"))

    def test_nothing_waiting_says_nothing(self):
        """An empty answer is a real answer: quiet, and the cycle is spent on it."""
        self.assertEqual(self.fire(""), "")
        self.assertGreater(monitor._load_json(self.state, {})["last_run"], 0)

    def test_an_unlabelled_url_is_ignored(self):
        self.assertEqual(self.fire(f"I think {OLD} is the one."), "")

    def test_a_failed_run_is_announced_and_retried(self):
        """A silent failure reads exactly like an empty backlog, so it must not be silent."""
        self.assertIn("selector did not answer", self.fire(None))
        self.assertEqual(monitor._load_json(self.state, {})["last_run"], 0)
        self.fire(BOTH)  # the cycle re-opened, so the very next fire tries again
        self.assertEqual(self.calls, 1)

    def test_away_defers_instead_of_selecting(self):
        """Nobody is reading the tab, so nothing is polled, spent, or printed."""
        with mock.patch.object(monitor, "_user_active", return_value=False):
            self.assertEqual(self.fire(BOTH), "")
        self.assertEqual(self.calls, 0)
        self.assertFalse(self.state.exists())  # the cycle is held, not spent
        self.assertIn("oldest —", self.fire(BOTH))  # ... and runs the moment you are back

    def test_interval_holds_within_one_run(self):
        event = monitor.BacklogPicksEvent(sched.scheduler(time.time, time.sleep))
        self.fire(BOTH, event)
        self.fire(BOTH, event)
        self.assertEqual(self.calls, 0)
        self.assertEqual(len(self.picks.read_text().splitlines()), 2)

    def test_a_restart_does_not_re_run_the_model(self):
        """The self-supersede restart is frequent; the model call is not cheap."""
        self.fire(BOTH)
        self.fire(BOTH)  # a fresh event, reading last_run back from the state file
        self.assertEqual(self.calls, 0)


if __name__ == "__main__":
    unittest.main()
