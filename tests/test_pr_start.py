#!/usr/bin/env python3
"""Unit tests for monitor.PrStartEvent: `python3 -m unittest discover tests`"""

import json
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

URL = "https://github.com/ClickHouse/ClickHouse/pull/7"


class PrStartEventTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(mock.patch.stopall)
        self.projects = tmp / "projects"
        self.log = tmp / "notifications.log"
        mock.patch.multiple(monitor, PROJECTS_DIR=self.projects,
                            PR_START_FILE=tmp / "pr-start.json",
                            NOTIFY_LOG=self.log).start()
        mock.patch.object(monitor, "_user_active", return_value=True).start()

    def claim(self, project: str, number: int, **fields) -> None:
        d = self.projects / project
        d.mkdir(parents=True, exist_ok=True)
        pr = {"key": f"ClickHouse/ClickHouse#{number}", "number": number,
              "url": f"https://github.com/ClickHouse/ClickHouse/pull/{number}"}
        pr.update(fields)
        (d / "meta.json").write_text(json.dumps({"host_dir": str(d), "pr": pr}))

    def fire(self) -> str:
        """Run one scan with a fresh event and return what it notified."""
        before = self.log.read_text() if self.log.exists() else ""
        monitor.PrStartEvent(sched.scheduler(time.time, time.sleep)).fire()
        return (self.log.read_text() if self.log.exists() else "")[len(before):]

    def test_first_scan_baselines_silently(self):
        self.claim("cr26.6", 7, title="Fix the thing")
        self.assertEqual(self.fire(), "")

    def test_a_new_claim_prints_a_clickable_link(self):
        self.claim("cr26.6", 7, title="Fix the thing")
        self.fire()  # baseline
        self.claim("cr26.7", 8, title="Fix the other thing")
        out = self.fire()
        self.assertIn("pr start cr26.7", out)
        self.assertIn(Hyperlink.format("PR #8: Fix the other thing",
                                       "https://github.com/ClickHouse/ClickHouse/pull/8"), out)

    def test_an_unchanged_claim_is_not_repeated(self):
        self.claim("cr26.6", 7, title="Fix the thing")
        self.fire()
        self.claim("cr26.7", 8, title="Fix the other thing")
        self.fire()
        self.assertEqual(self.fire(), "")

    def test_a_titleless_claim_links_the_number_alone(self):
        self.fire()  # baseline an empty projects dir
        self.claim("cr26.6", 7)
        self.assertIn(Hyperlink.format("PR #7", URL), self.fire())

    def test_an_incomplete_claim_is_ignored(self):
        self.fire()
        d = self.projects / "cr26.6"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"pr": {"key": "x#1", "number": 1}}))
        self.assertEqual(self.fire(), "")


if __name__ == "__main__":
    unittest.main()
