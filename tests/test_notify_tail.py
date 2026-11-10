#!/usr/bin/env python3
"""Unit tests for the notify_tail hook: `python3 -m unittest discover tests`"""

import importlib.util
import io
import json
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
        self.picks = app / "backlog-picks.txt"
        self.state = app / "project" / "notify-tail.json"
        spec = importlib.util.spec_from_file_location("notify_tail", HOOK)
        self.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.hook)
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

    GQL = json.dumps({"data": {
        "viewer": {"pullRequests": {"nodes": [
            {"url": "https://x/1", "title": "A", "reviewDecision": "APPROVED",
             "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]}},
            {"url": "https://x/2", "title": "B", "reviewDecision": None,
             "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE"}}}]}},
            {"url": "https://x/3", "title": "C", "reviewDecision": "CHANGES_REQUESTED",
             "commits": {"nodes": []}}]}},
        "search": {"nodes": [{"url": "https://x/4", "title": "D"}]}}})

    def fake_todo(self, body=None):
        self.fake_gh(f"if [ \"$1\" = api ]; then cat <<'EOF'\n{body or self.GQL}\nEOF\nelse exit 1; fi")

    def test_nothing_to_do_is_silent(self):
        self.assertEqual(self.run_hook(), "")

    def test_the_oldest_to_review_fix_and_merge_are_shown_then_throttled(self):
        self.fake_todo()
        self.assertEqual(self.run_hook().splitlines(),
                         ["to do", "  review — D", "      https://x/4",
                          "  fix — B — CI failed", "      https://x/2",
                          "  merge — A — CI success", "      https://x/1"])
        self.assertEqual(self.run_hook(), "")

    def test_a_missing_kind_is_left_out(self):
        self.fake_todo(self.GQL.replace("APPROVED", "REVIEW_REQUIRED"))
        out = self.run_hook()
        self.assertNotIn("merge", out)
        self.assertIn("fix — B", out)

    def test_the_override_file_replaces_the_default_interval(self):
        self.picks.write_text("oldest — A (https://x/1)\n")
        self.run_hook()
        self.picks.write_text("oldest — B (https://x/2)\n")
        self.assertEqual(self.run_hook(), "")
        self.interval.write_text("1\n")
        self.state.write_text(self.hook.json.dumps({"at": time.time() - 2, "picks": "x"}))
        self.assertIn("oldest — B", self.run_hook())

    def test_zero_prints_on_every_prompt_with_the_gate_off(self):
        self.interval.write_text("0")
        self.picks.write_text("oldest — A (https://x/1)\n")
        self.assertIn("oldest — A", self.run_hook())
        self.assertIn("oldest — A", self.run_hook())

    def test_the_tutorial_prints_as_its_own_section_keeping_its_lines(self):
        """It is prose about this session, not a list of events to fold into rows."""
        self.hint.write_text("## /goal — keep going\nwhat it does\nTry: /goal x\nHere: y\n")
        out = self.run_hook()
        self.assertEqual(out.splitlines()[:3],
                         ["try this", "  ## /goal — keep going", "  what it does"])

    def test_a_new_tutorial_speaks_even_though_nothing_else_moved(self):
        self.hint.write_text("## /goal — keep going\n")
        self.run_hook()
        self.state.write_text(self.hook.json.dumps({"at": 1, "hint": "## /goal — keep going"}))
        self.assertEqual(self.run_hook(), "")
        self.hint.write_text("## /next\n")
        self.assertIn("## /next", self.run_hook())

    def test_the_picks_print_as_their_own_section(self):
        self.picks.write_text("oldest — A (https://x/1)\nhighest — B (https://x/2)\n")
        out = self.run_hook()
        self.assertIn("backlog\n  oldest — A\n      https://x/1\n  highest — B\n      https://x/2", out)

    def test_an_approved_pr_says_so(self):
        self.fake_gh('echo \'{"title": "C", "url": "https://x/9", "reviewDecision": "APPROVED"}\'')
        self.assertIn("backlog\n  current pr + approved — C\n      https://x/9", self.run_hook())

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

    def test_a_garbled_override_is_the_default_not_an_error(self):
        self.interval.write_text("every minute please")
        self.assertEqual(self.hook.interval(), self.hook.NOTIFY_INTERVAL)


if __name__ == "__main__":
    unittest.main()
