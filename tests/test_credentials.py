#!/usr/bin/env python3
"""Unit tests for credential selection in claude.py: `python3 -m unittest discover tests`"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import claude  # noqa: E402

KEY = "sk-ant-api03-" + "x" * 80


def creds(hours: float) -> bytes:
    """An OAuth blob whose access token dies `hours` from now."""
    return json.dumps({"claudeAiOauth": {
        "accessToken": f"tok{hours}", "refreshToken": "r",
        "expiresAt": int((time.time() + hours * 3600) * 1000)}}).encode()


class CredentialsTest(unittest.TestCase):
    """`/login` stores either an OAuth credential or an API key; the container needs whichever."""

    def setUp(self):
        home = Path(tempfile.mkdtemp())
        (home / ".claude").mkdir()
        self.file = home / ".claude" / ".credentials.json"
        self.keychain = {}  # service -> stored blob
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(claude, "HOME", home).start()
        mock.patch.object(claude, "APP_DIR", home / ".config").start()
        (home / ".config").mkdir()
        mock.patch.object(claude.subprocess, "run", side_effect=self.security).start()

    def security(self, argv, **kw):
        blob = self.keychain.get(argv[argv.index("-s") + 1])
        return mock.Mock(returncode=0 if blob else 1, stdout=blob or b"")

    def staged(self):
        kind, path = claude.stage_credentials()
        return kind, path.read_bytes()

    def test_the_later_expiry_wins_over_the_keychain(self):
        self.keychain["Claude Code-credentials"] = creds(1)
        self.file.write_bytes(creds(8))
        self.assertIn(b"tok8", self.staged()[1])

    def test_the_keychain_wins_when_it_is_the_later_one(self):
        self.keychain["Claude Code-credentials"] = creds(8)
        self.file.write_bytes(creds(1))
        self.assertIn(b"tok8", self.staged()[1])

    def test_a_wiped_store_never_outranks_a_live_one(self):
        self.keychain["Claude Code-credentials"] = b"{}"
        self.file.write_bytes(creds(8))
        self.assertIn(b"tok8", self.staged()[1])

    def test_an_expired_oauth_login_is_not_a_credential(self):
        self.file.write_bytes(creds(-1))
        self.keychain["Claude Code"] = KEY.encode()
        self.assertEqual(self.staged(), ("apikey", KEY.encode() + b"\n"))

    def test_an_api_key_host_falls_back_to_the_key(self):
        self.keychain["Claude Code-credentials"] = b"{}"
        self.file.write_bytes(b"{}")
        self.keychain["Claude Code"] = KEY.encode()
        self.assertEqual(self.staged(), ("apikey", KEY.encode() + b"\n"))

    def test_oauth_is_preferred_when_both_exist(self):
        self.file.write_bytes(creds(8))
        self.keychain["Claude Code"] = KEY.encode()
        self.assertEqual(self.staged()[0], "oauth")

    def test_a_keychain_item_that_is_not_a_key_is_ignored(self):
        self.keychain["Claude Code"] = b"not-a-key"
        with self.assertRaises(SystemExit) as e:
            claude.stage_credentials()
        self.assertIn("no usable Claude Code credential", str(e.exception))

    def test_nothing_anywhere_is_a_clear_exit(self):
        self.file.write_bytes(b"{}")
        with self.assertRaises(SystemExit) as e:
            claude.stage_credentials()
        self.assertIn("Authenticate on the host", str(e.exception))


if __name__ == "__main__":
    unittest.main()
