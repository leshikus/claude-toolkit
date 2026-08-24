#!/usr/bin/env python3
"""Unit tests for the clickable links the monitor posts to the notification stream.

`python3 -m unittest discover tests`
"""

import json
import os
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

PR_URL = "https://github.com/ClickHouse/ClickHouse/pull/7"
RUN_URL = "https://github.com/ClickHouse/ClickHouse/actions/runs/12345"
PR_TITLE = "Fix the flaky DistributedCache test"
RUN_TITLE = "Rerun after the rebase"
PR_LINK = Hyperlink.plain(PR_TITLE, PR_URL)
TITLES = {"/repos/ClickHouse/ClickHouse/issues/7": {"title": PR_TITLE},
          "/repos/ClickHouse/ClickHouse/actions/runs/12345": {"display_title": RUN_TITLE}}


def human(text: str) -> dict:
    return {"type": "user", "origin": {"kind": "human"},
            "cwd": "/Users/x/repos/cr26.6", "message": {"content": text}}


def assistant(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def tool_call(command: str) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": command}}]}}


def tool_result(text: str) -> dict:
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "content": text}]}}


class ChatLinksTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(mock.patch.stopall)
        self.path = tmp / "projects" / "-Users-x-repos-cr26.6" / "session.jsonl"
        self.path.parent.mkdir(parents=True)
        self.log = tmp / "notifications.log"
        mock.patch.multiple(monitor, CLAUDE_PROJECTS_DIR=tmp / "projects",
                            LINK_STATE_FILE=tmp / "link-state.json",
                            NOTIFY_LOG=self.log).start()
        mock.patch.object(monitor, "_user_active", return_value=True).start()
        # No network in a unit test: titles come from a fixed table, and an unknown
        # item answers as GitHub does when it cannot be read.
        mock.patch.object(monitor, "_gh_json",
                          side_effect=lambda argv: TITLES.get(argv[1])).start()
        self.event = None

    def append(self, *entries) -> None:
        with self.path.open("a") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def scan(self) -> str:
        """One scan of the same event the monitor keeps; returns what it posted."""
        before = self.log.read_text() if self.log.exists() else ""
        if self.event is None:
            self.event = monitor.GithubLinksEvent(sched.scheduler(time.time, time.sleep))
        self.event._scan()
        self.event.baseline = False  # what fire() does around _scan
        return (self.log.read_text() if self.log.exists() else "")[len(before):]

    def started(self) -> None:
        """A live chat whose backlog is already baselined."""
        self.append(human("start"))
        self.scan()

    def test_a_first_scan_baselines_the_backlog(self):
        self.append(human(f"look at {PR_URL}"))
        self.assertEqual(self.scan(), "")

    def test_a_mentioned_pr_and_run_are_posted_as_links(self):
        self.started()
        self.append(assistant(f"CI failed: {RUN_URL} — the PR is {PR_URL}"))
        out = self.scan()
        self.assertIn(Hyperlink.plain(RUN_TITLE, RUN_URL), out)
        self.assertIn(PR_LINK, out)
        self.assertIn("/Users/x/repos/cr26.6 —", out)  # the directory the chat runs in

    def test_an_unreadable_item_falls_back_to_what_it_refers_to(self):
        self.started()
        self.append(assistant("see https://github.com/o/r/pull/9"))
        self.assertIn(Hyperlink.plain("PR o/r#9", "https://github.com/o/r/pull/9"), self.scan())

    def test_a_pasted_link_counts_as_mentioned(self):
        self.started()
        self.append(human(f"have a look at {PR_URL}"))
        self.assertIn(PR_LINK, self.scan())

    def test_tool_calls_and_their_output_are_not_the_chat(self):
        self.started()
        self.append(tool_call(f"gh pr view {PR_URL}"), tool_result(f"url: {PR_URL}"))
        self.assertEqual(self.scan(), "")

    def test_one_pr_posts_once_however_it_is_written(self):
        self.started()
        self.append(assistant(f"{PR_URL} then {PR_URL}/files then {PR_URL}#discussion_r1"))
        self.assertEqual(len(self.scan().splitlines()), 1)

    def test_trailing_punctuation_is_not_part_of_the_url(self):
        self.started()
        self.append(assistant(f"see {PR_URL}."))
        self.assertIn(PR_LINK, self.scan())

    def test_a_re_mention_is_not_re_posted(self):
        self.started()
        self.append(assistant(f"see {PR_URL}"))
        self.scan()
        self.append(assistant(f"still waiting on {PR_URL}"))
        self.assertEqual(self.scan(), "")

    def test_beyond_the_cap_only_the_count_prints(self):
        self.started()
        self.append(assistant(" ".join(
            f"https://github.com/o/r/pull/{n}" for n in range(1, monitor.LINK_MAX_PER_SCAN + 3))))
        out = self.scan()
        self.assertEqual(len(out.splitlines()), monitor.LINK_MAX_PER_SCAN + 1)
        self.assertIn("2 more GitHub links mentioned, not shown", out)

    def test_a_half_written_entry_waits_for_its_newline(self):
        self.started()
        with self.path.open("a") as f:
            f.write(json.dumps(assistant(f"see {PR_URL}")))
        self.assertEqual(self.scan(), "")
        with self.path.open("a") as f:
            f.write("\n")
        self.assertIn(PR_LINK, self.scan())

    def test_a_headless_agent_run_is_not_a_chat(self):
        self.append({"type": "user", "origin": {"kind": "sdk-cli"},
                     "cwd": "/Users/x/repos/cr26.6", "message": {"content": "review this"}})
        self.scan()
        self.append(assistant(f"see {PR_URL}"))
        self.assertEqual(self.scan(), "")

    def test_a_transcript_nobody_has_touched_for_an_hour_is_left_alone(self):
        self.started()
        self.append(assistant(f"see {PR_URL}"))
        stale = time.time() - monitor.LINK_ACTIVE_WINDOW - 60
        os.utime(self.path, (stale, stale))
        self.assertEqual(self.scan(), "")


class PrActivityLinkTest(unittest.TestCase):
    KEY = "ClickHouse/ClickHouse#7"

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(mock.patch.stopall)
        self.log = tmp / "notifications.log"
        mock.patch.multiple(monitor, NOTIFY_LOG=self.log,
                            PROJECTS_DIR=tmp / "projects").start()
        mock.patch.object(monitor, "_current_login", return_value="leshikus").start()
        mock.patch.object(monitor, "_user_active", return_value=True).start()
        self.event = monitor.PullRequestsEvent(sched.scheduler(time.time, time.sleep))

    def pr(self) -> dict:
        return {"number": 7, "title": "Fix the changelog check", "url": PR_URL,
                "repository": {"nameWithOwner": "ClickHouse/ClickHouse", "name": "ClickHouse"}}

    def test_activity_prints_the_pr_as_a_link(self):
        self.event.state[self.KEY] = {"action": None}
        self.event._dispatch(self.KEY, self.pr(), ["CI failure", "new comment/review"], {})
        out = self.log.read_text()
        self.assertIn(Hyperlink.plain("PR #7: Fix the changelog check", PR_URL), out)
        self.assertIn("CI failure; new comment/review", out)

    def test_an_action_required_change_is_still_marked(self):
        self.event.state[self.KEY] = {"action": "review requested"}
        self.event._dispatch(self.KEY, self.pr(), ["added as reviewer"], {})
        self.assertIn("⚠ PR #7:", self.log.read_text())


if __name__ == "__main__":
    unittest.main()


class HistoryProjectTest(unittest.TestCase):
    """The label the link and hint lines are posted under."""

    PATH = Path("/x/projects/-Users-dataved-repos-cr26.6/session.jsonl")

    def project(self, cwd: str, session_start: str = "") -> str:
        return monitor._history_project(session_start, cwd, self.PATH)

    def test_a_checkout_is_named_by_its_basename(self):
        self.assertEqual(self.project("/Users/dataved/repos/cr26.6"), "cr26.6")

    def test_the_home_directory_is_not_a_project(self):
        self.assertEqual(self.project(str(Path.home())), "home")
        self.assertEqual(self.project(str(Path.home()) + "/"), "home")

    def test_the_hook_name_wins_over_the_home_directory(self):
        self.assertEqual(
            self.project(str(Path.home()), "Project name: claude-toolkit\nbranch: main"),
            "claude-toolkit")


class HistoryDirTest(unittest.TestCase):
    """The label the link lines are posted under: where the session is working."""

    PATH = Path("/x/projects/-Users-dataved-repos-cr26.6/session.jsonl")
    HOST_DIR = "/Users/dataved/repos/cr26.6"

    def setUp(self):
        self.projects = Path(tempfile.mkdtemp())
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(monitor, "PROJECTS_DIR", self.projects).start()

    def claim(self, project: str, host_dir: str) -> None:
        """What claude.py records for a project at launch."""
        (self.projects / project).mkdir()
        (self.projects / project / "meta.json").write_text(json.dumps({"host_dir": host_dir}))

    def where(self, cwd: str, session_start: str = "") -> str:
        return monitor._history_dir(session_start, cwd, self.PATH)

    def test_a_host_session_is_its_own_cwd(self):
        self.assertEqual(self.where(str(Path.home()) + "/repos/cr26.6"), "~/repos/cr26.6")

    def test_a_path_outside_home_is_left_absolute(self):
        self.assertEqual(self.where("/srv/build/cr26.6"), "/srv/build/cr26.6")

    def test_the_home_directory_is_one_character(self):
        self.assertEqual(self.where(str(Path.home())), "~")

    def test_a_container_session_resolves_to_the_host_checkout(self):
        self.claim("cr26.6", self.HOST_DIR)
        self.assertEqual(
            self.where(monitor.CONTAINER_WORKDIR, "Project name: cr26.6\nbranch: main"),
            "~/repos/cr26.6")

    def test_an_unclaimed_container_session_keeps_the_project_name(self):
        self.assertEqual(
            self.where(monitor.CONTAINER_WORKDIR, "Project name: cr26.6\nbranch: main"),
            "cr26.6")
