#!/usr/bin/env python3
"""Unit tests for `claude.py <pr-url>`: `python3 -m unittest discover tests`"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import claude  # noqa: E402

URL = "https://github.com/ClickHouse/ClickHouse/pull/7"
ME = "leshikus"


class StagePrTest(unittest.TestCase):
    def setUp(self):
        self.app = Path(tempfile.mkdtemp())
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(claude, "APP_DIR", self.app).start()
        self.ran, self.forced = [], []
        mock.patch.object(claude, "run_step",
                          side_effect=lambda argv, cwd=None, fatal=True:
                          (self.ran.append(argv[:3]), self.forced.extend(argv[3:]))).start()
        self.fork, self.author = False, ME
        mock.patch.object(claude, "gh_json", side_effect=self.gh).start()

    def gh(self, *args):
        if args[0] == "repo":
            return {"isFork": self.fork}
        if args[0] == "pr":
            return {"author": {"login": self.author}}
        return {"login": ME}

    def test_a_non_pr_url_is_refused(self):
        with self.assertRaises(SystemExit) as e:
            claude.stage_pr("https://github.com/ClickHouse/ClickHouse/issues/7")
        self.assertIn("not a GitHub pull request URL", str(e.exception))

    def test_my_own_pr_opens_on_finishing_it(self):
        checkout, prompt = claude.stage_pr(URL)
        self.assertEqual(checkout, self.app / "projects" / "ClickHouse-7" / "repo")
        self.assertIn("finalize", prompt)
        self.assertIn(URL, prompt)

    def test_someone_elses_pr_opens_on_reviewing_it(self):
        self.author = "alexey-milovidov"
        self.assertEqual(claude.stage_pr(URL)[1], f"Review {URL}")

    def test_the_name_carries_the_repo_because_a_number_alone_is_not_unique(self):
        """ClickHouse#7 and clickhouse-private#7 must not share a queue or a checkout."""
        other = "https://github.com/ClickHouse/clickhouse-private/pull/7"
        self.assertNotEqual(claude.stage_pr(URL)[0], claude.stage_pr(other)[0])
        self.assertEqual(claude.stage_pr(other)[0].parent.name, "clickhouse-private-7")

    def test_the_checkout_is_cloned_then_the_pr_checked_out(self):
        claude.stage_pr(URL)
        self.assertEqual(self.ran, [["gh", "repo", "clone"], ["gh", "pr", "checkout"]])

    def test_an_existing_checkout_is_fetched_rather_than_recloned(self):
        """These are ClickHouse-sized clones, but a stale one is worse than useless."""
        (self.app / "projects" / "ClickHouse-7" / "repo" / ".git").mkdir(parents=True)
        claude.stage_pr(URL)
        self.assertEqual(self.ran, [["git", "fetch", "--prune"], ["gh", "pr", "checkout"]])
        self.assertIn("--force", self.forced)

    def test_only_the_clone_is_fatal(self):
        """A failed sync or checkout still leaves a repository the session can work in."""
        self.fork = True
        seen = []
        with mock.patch.object(claude, "run_step",
                               side_effect=lambda argv, cwd=None, fatal=True:
                               seen.append((argv[0], argv[1], fatal))):
            claude.stage_pr(URL)
        self.assertEqual(seen, [("gh", "repo", False),        # sync
                                ("gh", "repo", True),         # clone
                                ("gh", "pr", False)])         # checkout

    def test_a_fork_is_synced_before_it_is_read(self):
        """A stale default branch is what makes the checkout diverge from what CI ran."""
        self.fork = True
        claude.stage_pr(URL)
        self.assertEqual(self.ran[0], ["gh", "repo", "sync"])


if __name__ == "__main__":
    unittest.main()


class SupersedeTest(unittest.TestCase):
    """One session per project: per-project state assumes it, and two do not notice
    each other -- `--resume` on a live session leaves both agents working."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        (self.home / ".claude" / "projects" / "-home-ubuntu-project").mkdir(parents=True)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(claude, "HOME", self.home).start()

    def transcript(self, session: str) -> None:
        (self.home / ".claude" / "projects" / "-home-ubuntu-project" / f"{session}.jsonl").touch()

    def test_a_session_with_a_transcript_is_resumable(self):
        self.transcript("abc-123")
        self.assertTrue(claude.resumable("abc-123"))

    def test_a_stale_id_is_not_resumable(self):
        """`claude --resume` on an unknown id aborts, which would kill the launch."""
        self.assertFalse(claude.resumable("abc-123"))

    def test_no_recorded_session_is_not_resumable(self):
        self.assertFalse(claude.resumable(None))

    def test_superseding_reports_only_when_it_removed_something(self):
        with mock.patch.object(claude.subprocess, "run",
                               return_value=mock.Mock(returncode=1)) as run:
            with mock.patch("builtins.print") as out:
                claude.supersede("toolkit-ClickHouse-7")
        run.assert_called_once_with(["docker", "rm", "-f", "toolkit-ClickHouse-7"],
                                    capture_output=True, text=True)
        out.assert_not_called()

    def test_superseding_says_so_when_a_container_was_running(self):
        with mock.patch.object(claude.subprocess, "run",
                               return_value=mock.Mock(returncode=0)):
            with mock.patch("builtins.print") as out:
                claude.supersede("toolkit-ClickHouse-7")
        self.assertIn("superseded", out.call_args.args[0])
