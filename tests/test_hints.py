#!/usr/bin/env python3
"""Unit tests for the hint tutorial: `python3 -m unittest discover tests`"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import monitor  # noqa: E402

TUTORIAL = """## /goal — keep working until a condition holds
Claude re-checks the condition before it stops, so a long task does not end halfway.
Try: /goal PR 115607 has green CI and every comment triaged
Here: this session stopped three times mid-rebase and you re-prompted it each time.
"""


class HintTutorialTest(unittest.TestCase):
    def setUp(self):
        self.projects = Path(tempfile.mkdtemp())
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(monitor, "PROJECTS_DIR", self.projects).start()

    def test_a_tutorial_keeps_its_shape(self):
        """It is read as a section of its own now, not folded into one glanceable line."""
        text = monitor._hint_text(TUTORIAL)
        self.assertEqual(len(text.splitlines()), 4)
        self.assertTrue(text.startswith("## /goal"))
        self.assertIn("Try: /goal", text)

    def test_none_is_not_a_tutorial(self):
        self.assertEqual(monitor._hint_text("NONE"), "")

    def test_the_topic_is_what_gets_remembered(self):
        """`recent` is replayed to the hinter so it does not teach the same thing twice."""
        self.assertEqual(monitor._hint_topic(TUTORIAL), "/goal — keep working until a condition holds")

    def test_it_lands_in_the_project_the_session_belongs_to(self):
        (self.projects / "chp-1").mkdir()
        monitor._write_hint("chp-1", monitor._hint_text(TUTORIAL))
        self.assertIn("## /goal", (self.projects / "chp-1" / "hint.md").read_text())

    def test_a_project_with_no_directory_yet_is_not_an_error(self):
        monitor._write_hint("brand-new", "## /goal\nx\n")
        self.assertIn("## /goal", (self.projects / "brand-new" / "hint.md").read_text())


if __name__ == "__main__":
    unittest.main()
