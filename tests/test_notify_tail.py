#!/usr/bin/env python3
"""Unit tests for the notify_tail hook: `python3 -m unittest discover tests`"""

import importlib.util
import io
import os
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
        self.hint = app / "project" / "hint.md"
        self.hook.HINT_FILE = self.hint
        self.meta = app / "project" / "meta.json"
        self.hook.META_FILE = self.meta
        # The hook asks `gh` for the branch's PR; a fake one that says nothing keeps
        # every other test off the network. fake_gh() gives it an answer.
        self.bin = home / "bin"
        self.bin.mkdir()
        self.fake_gh("exit 1")
        self.path = os.environ["PATH"]
        os.environ["PATH"] = f"{self.bin}:{self.path}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", self.path))
        self.interval = app / "config" / "notify-interval"
        self.interval.parent.mkdir()
        self.hook.INTERVAL_FILE = self.interval

    def fake_gh(self, body: str) -> None:
        """Put a `gh` on PATH whose body is `body`."""
        gh = self.bin / "gh"
        gh.write_text(f"#!/bin/sh\n{body}\n")
        gh.chmod(0o755)

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

    def test_the_tutorial_prints_as_its_own_section_keeping_its_lines(self):
        """It is prose about this session, not a list of events to fold into rows."""
        self.hint.write_text("## /goal — keep going\nwhat it does\nTry: /goal x\nHere: y\n")
        out = self.run_hook()
        self.assertEqual(out.splitlines()[:3],
                         ["try this", "  ## /goal — keep going", "  what it does"])

    def test_a_new_tutorial_speaks_even_though_nothing_else_moved(self):
        self.log.write_text("first\n")
        self.run_hook()
        self.state.write_text(self.hook.json.dumps(
            {"at": 1, "size": 6, "picks": "", "hint": ""}))
        self.assertEqual(self.run_hook(), "")
        self.hint.write_text("## /goal — keep going\n")
        self.assertIn("## /goal", self.run_hook())

    def test_the_picks_print_as_their_own_section(self):
        self.picks.write_text("oldest — A (https://x/1)\nhighest — B (https://x/2)\n")
        out = self.run_hook()
        self.assertIn("backlog\n  oldest — A\n      https://x/1\n  highest — B\n      https://x/2", out)

    def test_the_tail_then_the_picks_then_the_branchs_pr_last(self):
        self.fake_gh('echo \'{"title": "C", "url": "https://x/9"}\'')
        self.log.write_text("first\n")
        self.picks.write_text("oldest — A (https://x/1)\n")
        self.assertEqual(self.run_hook().splitlines(),
                         ["monitor — most recent last", "  first", "",
                          "backlog", "  oldest — A", "      https://x/1",
                          "  current pr — C", "      https://x/9"])

    def test_a_checkout_on_no_prs_branch_shows_the_claim(self):
        """A review of a merged PR whose branch is gone stays on the default branch."""
        self.meta.write_text('{"pr": {"key": "o/r#1", "url": "https://github.com/o/r/pull/1"}}')
        self.assertIn("backlog\n  current pr — https://github.com/o/r/pull/1\n", self.run_hook())

    def test_the_pr_alone_is_enough_for_the_section(self):
        self.fake_gh('echo \'{"title": "C", "url": "https://x/9"}\'')
        self.assertIn("backlog\n  current pr — C\n      https://x/9", self.run_hook())

    def test_the_claim_follows_the_branch_up_a_stack(self):
        """The monitor routes by this claim; the PR the console started on is not it."""
        self.meta.write_text('{"host_dir": "/x", "pr": {"key": "o/r#1"}}')
        self.fake_gh('echo \'{"title": "C", "url": "https://github.com/o/r/pull/2"}\'')
        self.run_hook()
        self.assertEqual(self.hook.json.loads(self.meta.read_text())["pr"],
                         {"key": "o/r#2", "repo": "o/r", "number": 2,
                          "url": "https://github.com/o/r/pull/2", "title": "C"})

    def test_a_branch_without_a_pr_keeps_the_claim(self):
        """`gh` failing is not evidence the PR went away; only a different PR is."""
        self.meta.write_text('{"pr": {"key": "o/r#1"}}')
        self.run_hook()
        self.assertEqual(self.hook.json.loads(self.meta.read_text())["pr"], {"key": "o/r#1"})

    def test_a_branch_without_a_pr_adds_nothing(self):
        self.picks.write_text("oldest — A (https://x/1)\n")
        out = self.run_hook()
        self.assertNotIn("current pr", out)
        self.assertIn("backlog\n  oldest — A", out)

    def test_a_hung_gh_does_not_hold_the_prompt(self):
        self.hook.GH_TIMEOUT = 0.2
        self.fake_gh("sleep 5")
        self.picks.write_text("oldest — A (https://x/1)\n")
        self.assertIn("backlog\n  oldest — A", self.run_hook())

    def test_a_changed_pick_speaks_even_though_the_log_is_quiet(self):
        self.log.write_text("first\n")
        self.picks.write_text("oldest — A (https://x/1)\n")
        self.run_hook()
        self.state.write_text(self.hook.json.dumps(
            {"at": 1, "size": 6, "picks": "oldest — A (https://x/1)"}))
        self.assertEqual(self.run_hook(), "")   # nothing moved
        self.picks.write_text("oldest — B (https://x/2)\n")
        self.assertIn("oldest — B\n      https://x/2", self.run_hook())

    def test_the_picks_repeat_with_each_replay_rather_than_stacking_up(self):
        """They are state: the current pair, not one line per selection cycle."""
        self.picks.write_text("oldest — A (https://x/1)\n")
        self.log.write_text("first\n")
        first = self.run_hook()
        self.state.write_text(self.hook.json.dumps({"at": 1, "size": 6, "picks": ""}))
        self.log.write_text("first\nsecond\n")
        second = self.run_hook()
        for out in (first, second):
            self.assertEqual(out.count("oldest — A\n      https://x/1"), 1)

    def test_a_url_gets_a_row_to_itself(self):
        self.log.write_text(
            "2026-08-25 12:00:00  ~/repos/x — A title (https://github.com/o/r/pull/7)\n")
        self.assertEqual(self.run_hook().splitlines()[1:],
                         ["  12:00  ~/repos/x — A title",
                          "      https://github.com/o/r/pull/7"])

    def test_an_escape_the_monitor_already_wrote_is_flattened(self):
        """The log still holds lines from when it emitted OSC 8; the console eats those."""
        link = "\x1b]8;;https://github.com/o/r/pull/7\x1b\\A title\x1b]8;;\x1b\\"
        self.log.write_text(f"2026-08-26 12:00:00  ~/repos/x — {link}\n")
        self.assertEqual(self.run_hook().splitlines()[1:],
                         ["  12:00  ~/repos/x — A title",
                          "      https://github.com/o/r/pull/7"])

    def test_a_line_without_a_url_stays_one_row(self):
        self.log.write_text("2026-08-25 12:00:00  hint x — Install node\n")
        self.assertEqual(self.run_hook().splitlines()[1:], ["  12:00  hint x — Install node"])

    def test_the_date_and_seconds_are_dropped_from_the_stamp(self):
        self.log.write_text("2026-08-25 09:07:42  hint x — y\n")
        self.assertEqual(self.run_hook().splitlines()[1:], ["  09:07  hint x — y"])

    def test_the_override_file_replaces_the_default_interval(self):
        self.log.write_text("first\n")
        self.run_hook()
        self.log.write_text("first\nsecond\n")
        self.assertEqual(self.run_hook(), "")      # inside the default 300 s
        self.interval.write_text("1\n")
        self.state.write_text(self.hook.json.dumps({"at": time.time() - 2, "size": 6}))
        self.assertIn("second", self.run_hook())

    def test_zero_prints_on_every_prompt_with_the_gates_off(self):
        """Silence must mean the hook is dead, not that nothing moved."""
        self.interval.write_text("0")
        self.log.write_text("first\n")
        self.assertIn("first", self.run_hook())
        self.assertIn("first", self.run_hook())    # unchanged, no wait: still speaks

    def test_a_garbled_override_is_the_default_not_an_error(self):
        self.interval.write_text("every minute please")
        self.assertEqual(self.hook.interval(), self.hook.NOTIFY_INTERVAL)

    def test_a_shrinking_log_counts_as_movement(self):
        """Rotation must not mute the hook until the new file passes the old size."""
        self.log.write_text("a\nb\nc\n")
        self.run_hook()
        self.state.write_text(self.hook.json.dumps({"at": 1, "size": 6}))
        self.log.write_text("d\n")                # truncated: smaller, but new
        self.assertIn("d", self.run_hook())

    def test_stamp_records_the_size_it_printed(self):
        self.log.write_text("first\n")
        self.run_hook()
        state = self.hook.json.loads(self.state.read_text())
        self.assertEqual(state["size"], 6)
        self.assertLess(time.time() - state["at"], 60)


if __name__ == "__main__":
    unittest.main()
