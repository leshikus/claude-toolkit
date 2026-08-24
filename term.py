#!/usr/bin/env python3
"""Talking to the terminal: OSC 8 hyperlinks (`Hyperlink`) and tabs (`TERMINAL`).

The two address the same terminal from opposite ends: `Hyperlink` writes bytes the
terminal interprets, `TERMINAL` drives the application around it.

`Hyperlink.render(text, url)` is what callers use. It emits
`ESC ] 8 ; ; <url> ST <text> ESC ] 8 ; ; ST` -- a link opened with Cmd-click
(macOS) or Ctrl-click (Linux) in iTerm2, kitty, WezTerm, Konsole, foot and
VTE >= 0.50 (GNOME Terminal) -- when the target stream is a TTY of such a
terminal, and `text (url)` when it is anything else, so a pipe or a file never
carries an escape nobody will render. `CLAUDE_TOOLKIT_HYPERLINKS=0|1` overrides
the detection.

Under tmux the sequence is stripped unless wrapped in tmux passthrough, and the
wrapper is itself wrong as soon as the output is read outside that tmux client,
so `$TMUX` takes the plain fallback rather than wrapping.

Text and URL are stripped of control characters -- an ESC or a BEL reaching a
terminal inside a hyperlink is an injection vector -- and the URL's remaining
disallowed bytes are percent-encoded, leaving existing `%xx` escapes intact.

`Hyperlink.format` is the raw sequence with no detection, for a caller that knows
its consumer even though its own stdout says nothing about it: text written to a
file that a known terminal tails.

`TERMINAL` is the terminal application of this host, `Term.detect()`-ed once at
import from the variants below -- `Iterm`, the only one so far, each of whose calls
is an `osascript -e` with an AppleScript snippet. A host with no variant available
gets the `Term` base, which opens nothing and says so, so the caller needs no
platform test of its own. iTerm2 also ships a real Python API (the `iterm2`
package, an asyncio websocket client), but it must be enabled in the application
first and it buys nothing for opening a tab; the stdlib has no terminal-application
class at all, only `shlex.quote` for the shell line such a tab is handed.
"""

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

OSC = "\x1b]"
ST = "\x1b\\"
ENV_OVERRIDE = "CLAUDE_TOOLKIT_HYPERLINKS"

# Apple_Terminal sets TERM_PROGRAM too and does not implement OSC 8, so the
# detection has to be a whitelist.
_TERM_PROGRAMS = {"iTerm.app", "WezTerm", "ghostty", "vscode", "mintty", "rio", "Tabby"}
_TERM_ENV = ("KITTY_WINDOW_ID", "KONSOLE_VERSION", "WT_SESSION", "DOMTERM")
_VTE_MIN = 5000  # VTE 0.50, the first GNOME Terminal that opens OSC 8 links

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_URL_SAFE = "-._~:/?#[]@!$&'()*+,;=%"  # RFC 3986 reserved + unreserved; % keeps escapes intact
_ID_UNSAFE = re.compile(r"[^0-9A-Za-z_.#/@+-]")  # ';' or ':' in a value would end the param


def _clean_text(text) -> str:
    return _CONTROL.sub("", str(text))


def _clean_url(url) -> str:
    return quote(_CONTROL.sub("", str(url)), safe=_URL_SAFE)


def _clean_id(key) -> str:
    return _ID_UNSAFE.sub("-", str(key))


class Hyperlink:
    """An OSC 8 hyperlink: `render` for a stream, `format` for a known consumer."""

    @staticmethod
    def format(text, url, *, id=None) -> str:
        """Return the raw OSC 8 sequence linking `text` to `url`.

        `id` marks several sequences as one logical link, so a terminal
        highlights them together when a link is split across lines. Without a `url`
        there is nothing to link, and a link to nowhere is a trap, so `text` is
        returned bare.
        """
        if not url:
            return _clean_text(text)
        params = f"id={_clean_id(id)}" if id else ""
        return f"{OSC}8;{params};{_clean_url(url)}{ST}{_clean_text(text)}{OSC}8;;{ST}"

    @staticmethod
    def plain(text, url) -> str:
        """Return the escape-free rendering: `text (url)`, or bare `url` alone."""
        text, url = _clean_text(text), _clean_url(url)
        return url if text in ("", url) else f"{text} ({url})"

    @staticmethod
    def supported(stream=None) -> bool:
        """True when `stream` (default stdout) is a TTY whose terminal opens OSC 8."""
        override = os.environ.get(ENV_OVERRIDE)
        if override in ("0", "1"):
            return override == "1"
        stream = sys.stdout if stream is None else stream
        try:
            if not stream.isatty():
                return False
        except (AttributeError, ValueError):  # not a stream, or already closed
            return False
        if os.environ.get("TMUX"):
            return False
        if os.environ.get("TERM_PROGRAM") in _TERM_PROGRAMS:
            return True
        if any(os.environ.get(v) for v in _TERM_ENV):
            return True
        term = os.environ.get("TERM", "")
        if term.startswith("foot") or "kitty" in term:
            return True
        try:
            return int(os.environ.get("VTE_VERSION", "0")) >= _VTE_MIN
        except ValueError:
            return False

    @classmethod
    def render(cls, text, url, *, stream=None, id=None) -> str:
        """Link `text` to `url` when the stream's terminal can open it, else `text (url)`."""
        if cls.supported(stream):
            return cls.format(text, url, id=id)
        return cls.plain(text, url)


class Term:
    """The terminal application the toolkit opens tabs in -- and the no-terminal case.

    A variant subclass says whether it `available()` on this host and how it opens
    a tab; everything that does not depend on the application (quoting the shell
    line, probing a tab's process) is shared. The base itself is always available
    and opens nothing, so a host with no variant degrades to a note on stderr
    instead of a crash in the caller.
    """

    @classmethod
    def detect(cls) -> "Term":
        """The first variant available on this host, else the no-terminal base."""
        for variant in (Iterm,):
            if variant.available():
                return variant()
        return cls()

    @staticmethod
    def available() -> bool:
        return True

    def open_tab(self, title: str, launch: str, *, pidfile=None) -> None:
        """Open a tab titled `title` whose shell runs `launch`; here, none.

        Given `pidfile`, a variant has the tab's shell record its own PID there
        before running `launch`, so `tab_alive` can later tell a live tab from a
        closed one -- `launch` should then `exec` its program, which keeps that PID.
        """
        print(f"term: no terminal to open {title!r} in", file=sys.stderr)

    @staticmethod
    def shquote(value) -> str:
        """Quote `value` for the shell line a tab is launched with."""
        return shlex.quote(str(value))

    @staticmethod
    def tab_alive(pidfile) -> bool:
        """True if the process a tab recorded in `pidfile` is still running.

        The terminal's own session name cannot answer this -- a running job
        overrides it -- and the recorded process is the terminal's child, not ours,
        so it outlives whoever opened the tab. Closing the tab kills it, so the
        probe fails.
        """
        try:
            pid = int(Path(pidfile).read_text().strip())
        except (FileNotFoundError, ValueError, OSError):
            return False
        try:
            os.kill(pid, 0)  # existence check; ProcessLookupError => gone
        except ProcessLookupError:
            return False
        return True


class Iterm(Term):
    """iTerm2, driven with AppleScript through `osascript`."""

    @staticmethod
    def available() -> bool:
        """True on macOS, where the toolkit requires iTerm2 (see the README)."""
        return sys.platform == "darwin"

    @staticmethod
    def _osaquote(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def open_tab(self, title: str, launch: str, *, pidfile=None) -> None:
        """Open an iTerm2 tab, reusing the current window or creating one if none is.

        The command reaches the shell via AppleScript `write text`, so it must be a
        single shell line.
        """
        if pidfile is not None:
            launch = f"echo $$ > {self.shquote(pidfile)}; {launch}"
        t, cmd = self._osaquote(title), self._osaquote(launch)
        script = (
            'tell application "iTerm2"\n'
            "  if (count of windows) = 0 then\n"
            "    create window with default profile\n"
            "    tell current session of current window\n"
            f"      set name to {t}\n"
            f"      write text {cmd}\n"
            "    end tell\n"
            "  else\n"
            "    tell current window\n"
            "      create tab with default profile\n"
            "      tell current session of current tab\n"
            f"        set name to {t}\n"
            f"        write text {cmd}\n"
            "      end tell\n"
            "    end tell\n"
            "  end if\n"
            "end tell\n"
        )
        subprocess.run(["osascript", "-e", script], check=False)


TERMINAL = Term.detect()
