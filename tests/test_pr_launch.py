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
        self.assertIn("green CI", prompt)

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
        self.assertTrue(prompt.startswith("/goal "))
        self.assertIn("green CI", prompt)
        self.assertIn(URL, prompt)

    def test_my_own_goal_stops_at_preparing_a_review_reply(self):
        """A goal is re-checked before stopping: demanding answered comments would
        drive the session straight through the gate that waits for the user."""
        prompt = claude.stage_pr(URL)[1]
        self.assertIn("waiting for my decision", prompt)
        self.assertIn("until I have agreed to it", prompt)
        self.assertNotIn("answered", prompt)

    def test_a_review_goal_is_met_by_a_draft_not_a_posted_review(self):
        self.author = "alexey-milovidov"
        prompt = claude.stage_pr(URL)[1]
        self.assertIn("ready for my approval", prompt)
        self.assertIn("nothing posted to GitHub", prompt)

    def test_someone_elses_pr_opens_on_reviewing_it(self):
        self.author = "alexey-milovidov"
        prompt = claude.stage_pr(URL)[1]
        self.assertTrue(prompt.startswith("/goal "))
        self.assertIn("reviewed", prompt)
        self.assertIn("nothing posted", prompt)

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

    def test_the_prompt_is_a_goal_about_reproducing_it(self):
        prompt = claude.stage_url(self.ISSUE)[1]
        self.assertTrue(prompt.startswith("/goal "))
        self.assertIn("reproduce", prompt)
        self.assertIn(self.ISSUE, prompt)

    def test_the_issue_gets_a_project_of_its_own(self):
        """Its own queues and stamp, rather than whichever directory you typed in."""
        work, _ = claude.stage_url(self.ISSUE)
        self.assertEqual(work, self.app / "projects" / "ClickHouse-issue-92886" / "work")
        self.assertTrue(work.is_dir())


class ClaudeJsonTest(unittest.TestCase):
    """~/.claude.json is mounted rw into every container, so one copy is a race."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(claude, "HOME", self.home).start()
        (self.home / ".claude.json").write_text(
            claude.json.dumps({"userID": "u", "oauthAccount": {"a": 1}, "projects": {"/x": {}}}))

    def stage_gnupg(self, project: str) -> str:
        d = self.home / "projects" / project
        d.mkdir(parents=True, exist_ok=True)
        return claude.stage_gnupg(d)

    def stage(self, project: str) -> Path:
        d = self.home / "projects" / project
        d.mkdir(parents=True)
        return claude.stage_claude_json(d)

    def test_each_project_gets_its_own_copy(self):
        a, b = self.stage("one"), self.stage("two")
        self.assertNotEqual(a, b)
        self.assertEqual(a.name, "claude.json")

    def test_the_workdir_is_pre_trusted_so_nothing_prompts(self):
        data = claude.read_json(self.stage("one"))
        self.assertTrue(data["projects"][claude.WORKDIR]["hasTrustDialogAccepted"])

    def test_each_project_gets_its_own_keyring(self):
        """One keyring mounted rw into four containers makes their keyboxd fight
        over one lock and one socket: `No Keybox daemon running`, then EIO."""
        (self.home / ".gnupg").mkdir()
        (self.home / ".gnupg" / "pubring.kbx").write_text("keys")
        a = self.stage_gnupg("one")
        b = self.stage_gnupg("two")
        self.assertNotEqual(a, b)
        self.assertEqual(Path(a).name, "gnupg")
        self.assertTrue((Path(a) / "pubring.kbx").exists())

    def test_a_corrupt_copy_is_reseeded_from_the_host(self):
        """It is a cache of onboarding answers; re-seeding costs one launch."""
        d = self.home / "projects" / "one"
        d.mkdir(parents=True)
        (d / "claude.json").write_text("")
        self.assertEqual(claude.read_json(claude.stage_claude_json(d))["userID"], "u")
