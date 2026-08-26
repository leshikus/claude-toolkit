#!/usr/bin/env python3
"""Unit tests for the shared git object store: `python3 -m unittest discover tests`"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gitstore  # noqa: E402

REPO = "ClickHouse/ClickHouse"


class GitStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = Path(tempfile.mkdtemp())
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(gitstore, "STORE", self.store).start()
        self.ran = []
        mock.patch.object(gitstore, "_run",
                          side_effect=lambda argv: (self.ran.append(argv), True)[1]).start()

    def existing(self) -> Path:
        (gitstore.mirror(REPO) / "objects").mkdir(parents=True)
        return gitstore.mirror(REPO)

    def test_a_mirror_is_named_for_the_repo(self):
        self.assertEqual(gitstore.mirror(REPO), self.store / "ClickHouse-ClickHouse.git")

    def test_a_missing_mirror_is_created_with_gc_turned_off(self):
        """Pruning an object a borrowing checkout needs would corrupt it."""
        self.assertEqual(gitstore.refresh(REPO), "created")
        self.assertEqual(self.ran[0][:3], ["gh", "repo", "clone"])
        self.assertIn("--mirror", self.ran[0])
        self.assertEqual(self.ran[1][-3:], ["config", "gc.auto", "0"])

    def test_an_existing_mirror_is_fetched(self):
        self.existing()
        self.assertEqual(gitstore.refresh(REPO), "fetched")
        self.assertEqual([a[3] for a in self.ran], ["fetch"])

    def test_only_an_existing_mirror_can_be_borrowed_from(self):
        self.assertEqual(gitstore.reference(REPO), [])
        self.assertEqual(gitstore.reference(REPO) or ["--reference", str(self.existing())],
                         ["--reference", str(gitstore.mirror(REPO))])

    def test_the_loser_of_a_race_skips_rather_than_failing(self):
        """git would fail the second fetch, and a failed refresh costs a 7.3 GB clone."""
        self.existing()
        with gitstore._holding(REPO):
            code = (f"import sys; sys.path.insert(0, {str(Path.cwd())!r}); import gitstore; "
                    f"from pathlib import Path; gitstore.STORE = Path({str(self.store)!r}); "
                    f"print(gitstore.refresh({REPO!r}))")
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "busy")
        self.assertEqual(self.ran, [])           # the holder's own run did nothing

    def test_a_failed_creation_leaves_nothing_to_borrow(self):
        with mock.patch.object(gitstore, "_run", return_value=False):
            self.assertEqual(gitstore.refresh(REPO), "")
        self.assertEqual(gitstore.reference(REPO), [])


if __name__ == "__main__":
    unittest.main()
