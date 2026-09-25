#!/usr/bin/env python3
"""Unit tests for recycling idle checkouts: `python3 -m unittest discover tests`"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import claude  # noqa: E402


class RecycleTest(unittest.TestCase):
    def setUp(self):
        self.app = Path(tempfile.mkdtemp())
        self.projects = self.app / "projects"
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(claude, "APP_DIR", self.app).start()
        self.stopped = []
        mock.patch.object(claude, "supersede", side_effect=self.stopped.append).start()

    def project(self, name: str, repo: str, days_idle: float) -> Path:
        d = self.projects / name
        checkout = d / "repo"
        checkout.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin",
                        f"https://github.com/{repo}.git"], check=True)
        (d / "meta.json").write_text('{"session_id": "old"}')
        stamp = time.time() - days_idle * 86400
        for p in [d, *d.iterdir(), checkout / ".git" / "index"]:
            if p.exists():
                os.utime(p, (stamp, stamp))
        return d

    def test_under_the_cap_nothing_is_recycled(self):
        self.project("a", "ClickHouse/ClickHouse", 10)
        self.assertFalse(claude.recycle(self.projects / "new", "ClickHouse/ClickHouse"))
        self.assertTrue((self.projects / "a").is_dir())

    def test_the_oldest_idle_checkout_of_the_repo_becomes_the_new_project(self):
        for i in range(4):
            self.project(f"busy-{i}", "ClickHouse/ClickHouse", 1)
        self.project("other", "ClickHouse/clickhouse-private", 9)
        self.project("same", "ClickHouse/ClickHouse", 3)
        new = self.projects / "new"
        self.assertTrue(claude.recycle(new, "ClickHouse/ClickHouse"))
        self.assertEqual(self.stopped, ["toolkit-same"])
        self.assertFalse((self.projects / "same").exists())
        self.assertEqual(sorted(p.name for p in new.iterdir()), ["repo"])
        self.assertTrue((new / "repo" / ".git").is_dir())

    def test_another_repos_checkout_is_dropped_when_recycled(self):
        for i in range(5):
            self.project(f"busy-{i}", "ClickHouse/ClickHouse", 1)
        self.project("other", "ClickHouse/clickhouse-private", 9)
        new = self.projects / "new"
        self.assertTrue(claude.recycle(new, "ClickHouse/ClickHouse"))
        self.assertEqual(list(new.iterdir()), [])

    def test_nothing_idle_means_a_new_directory(self):
        for i in range(6):
            self.project(f"busy-{i}", "ClickHouse/ClickHouse", 1)
        self.assertFalse(claude.recycle(self.projects / "new", "ClickHouse/ClickHouse"))
        self.assertEqual(self.stopped, [])


if __name__ == "__main__":
    unittest.main()
