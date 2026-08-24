#!/usr/bin/env python3
"""Unit tests for term.py: `python3 -m unittest discover tests`"""

import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import term  # noqa: E402
from term import Hyperlink, Iterm, Term  # noqa: E402

URL = "https://github.com/ClickHouse/ClickHouse/pull/7"
# A TTY of a terminal that opens OSC 8 links, i.e. supported() says yes.
ITERM_ENV = {"TERM_PROGRAM": "iTerm.app", "TERM": "xterm-256color"}


class Tty(io.StringIO):
    """A stream that claims to be (or not to be) a terminal."""

    def __init__(self, isatty: bool) -> None:
        super().__init__()
        self._isatty = isatty

    def isatty(self) -> bool:
        return self._isatty


def env(**overrides):
    """Patch the environment to exactly `overrides` -- the real one leaks TERM_PROGRAM."""
    return mock.patch.dict(os.environ, overrides, clear=True)


class FormatTest(unittest.TestCase):
    def test_exact_sequence(self):
        self.assertEqual(
            Hyperlink.format("PR #7", URL),
            f"\x1b]8;;{URL}\x1b\\PR #7\x1b]8;;\x1b\\",
        )

    def test_exact_sequence_with_id(self):
        self.assertEqual(
            Hyperlink.format("PR #7", URL, id="pr7"),
            f"\x1b]8;id=pr7;{URL}\x1b\\PR #7\x1b]8;;\x1b\\",
        )

    def test_id_keeps_a_pr_key_and_drops_the_separators(self):
        self.assertIn("id=ClickHouse/ClickHouse#7;", Hyperlink.format("t", URL, id="ClickHouse/ClickHouse#7"))
        self.assertIn("id=a-b;", Hyperlink.format("t", URL, id="a;b"))
        self.assertIn("id=a-b;", Hyperlink.format("t", URL, id="a:b"))


class SanitizeTest(unittest.TestCase):
    def test_injected_escape_and_newline_are_stripped(self):
        link = Hyperlink.format("a\x1b]0;title\x07b\nc", URL)
        self.assertIn("\x1b\\a]0;titlebc\x1b]8;;", link)
        self.assertEqual(link.count("\x1b"), 4)  # two OSC, two ST: nothing injected

    def test_control_characters_never_reach_the_url(self):
        link = Hyperlink.format("t", "https://x/\x1b]0;boom\x07\n")
        self.assertIn("\x1b]8;;https://x/]0;boom\x1b\\", link)
        self.assertEqual(link.count("\x1b"), 4)

    def test_disallowed_bytes_are_percent_encoded(self):
        self.assertIn("https://x/a%20b%22", Hyperlink.format("t", 'https://x/a b"'))

    def test_no_url_means_no_link(self):
        self.assertEqual(Hyperlink.format("PR #7", ""), "PR #7")

    def test_a_valid_url_is_left_intact(self):
        url = "https://x/a%20b?q=1&r=2#f"
        self.assertIn(f";{url}\x1b\\", Hyperlink.format("t", url))


class PlainTest(unittest.TestCase):
    def test_text_and_url(self):
        self.assertEqual(Hyperlink.plain("PR #7", URL), f"PR #7 ({URL})")

    def test_url_alone_when_the_text_adds_nothing(self):
        self.assertEqual(Hyperlink.plain(URL, URL), URL)
        self.assertEqual(Hyperlink.plain("", URL), URL)


class SupportedTest(unittest.TestCase):
    def test_not_a_tty(self):
        with env(**ITERM_ENV):
            self.assertFalse(Hyperlink.supported(Tty(False)))

    def test_tty_of_a_supporting_terminal(self):
        with env(**ITERM_ENV):
            self.assertTrue(Hyperlink.supported(Tty(True)))

    def test_apple_terminal_does_not_support_it(self):
        with env(TERM_PROGRAM="Apple_Terminal"):
            self.assertFalse(Hyperlink.supported(Tty(True)))

    def test_detected_by_terminal_env(self):
        for var, value in (("KITTY_WINDOW_ID", "1"), ("KONSOLE_VERSION", "220400"),
                           ("VTE_VERSION", "5202"), ("TERM", "foot")):
            with self.subTest(var=var), env(**{var: value}):
                self.assertTrue(Hyperlink.supported(Tty(True)))

    def test_vte_too_old(self):
        with env(VTE_VERSION="4600"):
            self.assertFalse(Hyperlink.supported(Tty(True)))

    def test_override_env_wins_both_ways(self):
        with env(CLAUDE_TOOLKIT_HYPERLINKS="1"):
            self.assertTrue(Hyperlink.supported(Tty(False)))
        with env(CLAUDE_TOOLKIT_HYPERLINKS="0", **ITERM_ENV):
            self.assertFalse(Hyperlink.supported(Tty(True)))

    def test_closed_stream(self):
        stream = open(os.devnull)
        stream.close()
        with env(**ITERM_ENV):
            self.assertFalse(Hyperlink.supported(stream))


class RenderTest(unittest.TestCase):
    def test_link_on_a_supporting_tty(self):
        with env(**ITERM_ENV):
            self.assertEqual(Hyperlink.render("PR #7", URL, stream=Tty(True)),
                             Hyperlink.format("PR #7", URL))

    def test_fallback_when_not_a_tty(self):
        with env(**ITERM_ENV):
            self.assertEqual(Hyperlink.render("PR #7", URL, stream=Tty(False)),
                             f"PR #7 ({URL})")

    def test_tmux_takes_the_fallback(self):
        with env(TMUX="/tmp/tmux-501/default,1,0", **ITERM_ENV):
            self.assertEqual(Hyperlink.render("PR #7", URL, stream=Tty(True)),
                             f"PR #7 ({URL})")

    def test_tmux_yields_to_the_override(self):
        with env(TMUX="/tmp/tmux-501/default,1,0", CLAUDE_TOOLKIT_HYPERLINKS="1"):
            self.assertEqual(Hyperlink.render("PR #7", URL, stream=Tty(True)),
                             Hyperlink.format("PR #7", URL))



class TermTest(unittest.TestCase):
    def test_the_detected_terminal_is_iterm_on_macos(self):
        with mock.patch.object(term.sys, "platform", "darwin"):
            self.assertIsInstance(Term.detect(), Iterm)

    def test_no_variant_available_leaves_the_base(self):
        with mock.patch.object(term.sys, "platform", "linux"):
            self.assertIs(type(Term.detect()), Term)

    def test_the_base_opens_nothing_and_says_so(self):
        with mock.patch("term.subprocess.run") as run, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            Term().open_tab("PR #7", "gh pr checkout 7")
        run.assert_not_called()
        self.assertIn("no terminal", err.getvalue())

    def test_the_module_exports_a_detected_instance(self):
        self.assertIsInstance(term.TERMINAL, Term)

    def test_shquote(self):
        self.assertEqual(Term.shquote(Path("/tmp/a b")), "'/tmp/a b'")

class ItermTest(unittest.TestCase):
    def script(self, *args, **kwargs) -> str:
        """Open a tab with osascript stubbed out and return the AppleScript it got."""
        with mock.patch("term.subprocess.run") as run:
            Iterm().open_tab(*args, **kwargs)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["osascript", "-e"])
        return argv[2]

    def test_open_tab_writes_the_command_and_names_the_tab(self):
        script = self.script("PR #7", "gh pr checkout 7")
        self.assertIn('tell application "iTerm2"', script)
        self.assertIn("create tab with default profile", script)
        self.assertIn('set name to "PR #7"', script)
        self.assertIn('write text "gh pr checkout 7"', script)

    def test_open_tab_escapes_the_applescript_string(self):
        self.assertIn(r'write text "echo \"hi\" \\ there"',
                      self.script("t", r'echo "hi" \ there'))


if __name__ == "__main__":
    unittest.main()
