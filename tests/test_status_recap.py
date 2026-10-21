#!/usr/bin/env python3
"""Unit tests for the status_recap hook: `python3 -m unittest discover tests`"""

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "status_recap.py"


def prompt(text: str, at: str) -> dict:
    return {"type": "user", "timestamp": at, "message": {"role": "user", "content": text}}


def tool_use(at: str) -> dict:
    return {"type": "assistant", "timestamp": at,
            "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}}


def tool_result(at: str) -> dict:
    return {"type": "user", "timestamp": at,
            "message": {"content": [{"type": "tool_result", "content": "ok"}]}}


def say(text: str, at: str) -> dict:
    return {"type": "assistant", "timestamp": at,
            "message": {"content": [{"type": "text", "text": text}]}}


class StatusRecapTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        spec = importlib.util.spec_from_file_location("status_recap", HOOK)
        self.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.hook)
        self.hook.READS_DIR = self.dir / "pending-reads"
        self.hook.READS_DIR.mkdir()
        self.transcript = self.dir / "t.jsonl"

    def run_hook(self, records: list, active: bool = False, last: str = "") -> dict:
        """The hook's decision; {} when it lets the turn stop."""
        self.transcript.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        event = {"transcript_path": str(self.transcript), "stop_hook_active": active,
                 "last_assistant_message": last}
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(event))), redirect_stdout(out):
            self.assertEqual(self.hook.main(), 0)
        return json.loads(out.getvalue()) if out.getvalue() else {}

    def test_plain_answer_passes(self):
        self.assertEqual(self.run_hook([prompt("why?", "2026-01-01T00:00:00Z"),
                                        say("Because.", "2026-01-01T00:00:01Z")]), {})

    def test_tool_turn_without_status_blocks(self):
        decision = self.run_hook([prompt("fix it", "2026-01-01T00:00:00Z"),
                                  tool_use("2026-01-01T00:00:01Z"),
                                  tool_result("2026-01-01T00:00:02Z"),
                                  say("Fixed.", "2026-01-01T00:00:03Z")])
        self.assertEqual(decision["decision"], "block")
        self.assertIn("**Next:**", decision["reason"])

    def test_status_present_passes(self):
        # The transcript does not yet hold the final message when the hook runs.
        self.assertEqual(self.run_hook([prompt("fix it", "2026-01-01T00:00:00Z"),
                                        tool_use("2026-01-01T00:00:01Z"),
                                        tool_result("2026-01-01T00:00:02Z")],
                                       last="Fixed.\n\n**Status** — waiting on: nothing"), {})

    def test_tools_of_an_earlier_turn_do_not_count(self):
        self.assertEqual(self.run_hook([prompt("fix it", "2026-01-01T00:00:00Z"),
                                        tool_use("2026-01-01T00:00:01Z"),
                                        prompt("why?", "2026-01-01T00:01:00Z"),
                                        say("Because.", "2026-01-01T00:01:01Z")]), {})

    def test_answering_the_hook_passes(self):
        self.assertEqual(self.run_hook([prompt("fix it", "2026-01-01T00:00:00Z"),
                                        tool_use("2026-01-01T00:00:01Z")], active=True), {})

    def test_monitor_state_is_listed(self):
        old = self.hook.READS_DIR / "ci-status-old.md"
        old.write_text("x")
        os.utime(old, (0, 0))
        (self.hook.READS_DIR / "ci-status-new.md").write_text("x")
        reason = self.run_hook([prompt("push", "2026-01-01T00:00:00Z"),
                                tool_use("2026-01-01T00:00:01Z")])["reason"]
        self.assertIn("ci-status-new.md", reason)
        self.assertNotIn("ci-status-old.md", reason)


if __name__ == "__main__":
    unittest.main()
