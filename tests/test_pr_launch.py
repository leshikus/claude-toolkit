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
        # The store reaches GitHub; a unit test must never make it do so.
        self.borrow, self.refreshed = [], []
        mock.patch.object(claude.gitstore, "refresh",
                          side_effect=lambda repo: (self.refreshed.append(repo),
                                                    "fetched")[1]).start()
        mock.patch.object(claude.gitstore, "reference",
                          side_effect=lambda repo: self.borrow).start()
        mock.patch.object(claude.gitstore, "mirror",
                          side_effect=lambda repo: Path("/store") / repo).start()

    def gh(self, *args):
        if args[0] == "repo":
            return {"isFork": self.fork}
        if args[0] == "pr":
            return {"author": {"login": self.author}}
        return {"login": ME}

    def claim(self, project: str, host_dir: Path, key: str) -> None:
        """What session_start records once a session sees its branch's PR."""
        d = self.app / "projects" / project
        d.mkdir(parents=True)
        host_dir.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(
            claude.json.dumps({"host_dir": str(host_dir), "pr": {"key": key}}))

    def test_a_checkout_already_tracking_the_pr_is_used_as_is(self):
        """One PR is one project: cloning a second copy splits its queues and session."""
        chp = self.app / "repos" / "chp-1"
        self.claim("chp-1", chp, "ClickHouse/ClickHouse#7")
        checkout, prompt = claude.stage_pr(URL)
        self.assertEqual(checkout, chp)
        self.assertEqual(self.ran, [])          # nothing cloned, nothing reset
        self.assertIn("finalize", prompt)

    def test_a_claim_whose_directory_is_gone_does_not_win(self):
        self.claim("chp-1", self.app / "repos" / "chp-1", "ClickHouse/ClickHouse#7")
        (self.app / "repos" / "chp-1").rmdir()
        self.assertEqual(claude.stage_pr(URL)[0],
                         self.app / "projects" / "ClickHouse-7" / "repo")

    def test_another_prs_claim_is_not_mistaken_for_this_one(self):
        self.claim("chp-1", self.app / "repos" / "chp-1", "ClickHouse/ClickHouse#8")
        self.assertEqual(claude.stage_pr(URL)[0],
                         self.app / "projects" / "ClickHouse-7" / "repo")

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

    def test_a_clone_borrows_the_shared_objects_when_a_mirror_exists(self):
        """7.3 GB of objects per PR is the thing the store exists to avoid."""
        self.borrow = ["--reference", "/store/ClickHouse-ClickHouse.git"]
        claude.stage_pr(URL)
        self.assertEqual(self.ran[0], ["gh", "repo", "clone"])
        self.assertIn("--reference", self.forced)

    def test_a_clone_without_a_mirror_is_a_plain_one(self):
        claude.stage_pr(URL)
        self.assertNotIn("--reference", self.forced)

    def test_the_checkout_is_cloned_then_the_pr_checked_out(self):
        claude.stage_pr(URL)
        self.assertEqual(self.ran, [["gh", "repo", "clone"], ["gh", "pr", "checkout"]])

    def test_an_existing_checkout_is_fetched_rather_than_recloned(self):
        """These are ClickHouse-sized clones, but a stale one is worse than useless."""
        (self.app / "projects" / "ClickHouse-7" / "repo" / ".git").mkdir(parents=True)
        claude.stage_pr(URL)
        self.assertEqual(self.ran, [["git", "fetch", "--prune"], ["gh", "pr", "checkout"]])

    def test_the_mirror_is_refreshed_before_updating_a_checkout_too(self):
        """Objects the mirror has count as had, so the checkout's own fetch stays small."""
        (self.app / "projects" / "ClickHouse-7" / "repo" / ".git").mkdir(parents=True)
        claude.stage_pr(URL)
        self.assertEqual(self.refreshed, ["ClickHouse/ClickHouse"])
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


class StageIssueTest(unittest.TestCase):
    """An issue needs no checkout: the verdict may be "already fixed"."""

    ISSUE = "https://github.com/ClickHouse/ClickHouse/issues/92886"

    def setUp(self):
        self.app = Path(tempfile.mkdtemp())
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(claude, "APP_DIR", self.app).start()
        self.ran = []
        mock.patch.object(claude, "run_step",
                          side_effect=lambda argv, cwd=None, fatal=True:
                          self.ran.append(argv)).start()

    def test_an_unsupported_url_names_both_kinds(self):
        with self.assertRaises(SystemExit) as e:
            claude.stage_url("https://github.com/ClickHouse/ClickHouse/commit/abc1234")
        self.assertIn("pull request or issue URL", str(e.exception))

    def test_nothing_is_cloned_and_nothing_is_pushed(self):
        claude.stage_url(self.ISSUE)
        self.assertEqual(self.ran, [])

    def test_the_prompt_asks_for_the_repro_and_nothing_else(self):
        self.assertEqual(claude.stage_url(self.ISSUE)[1], f"Reproduce {self.ISSUE}")

    def test_the_issue_gets_a_project_of_its_own(self):
        """Its own queues and stamp, rather than whichever directory you typed in."""
        work, _ = claude.stage_url(self.ISSUE)
        self.assertEqual(work, self.app / "projects" / "ClickHouse-issue-92886" / "work")
        self.assertTrue(work.is_dir())
