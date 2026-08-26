#!/usr/bin/env python3
"""UserPromptSubmit hook: show what the host monitor has to say, before the turn starts.

Two channels, both written by `monitor.py` and invisible from inside the container.
`backlog-picks.txt` is state -- the pair worth looking at right now, overwritten every
cycle -- and is reprinted whole. `notifications.log` is history -- CI results, PR
activity, setup hints -- and only its tail is replayed.

The reader sees them as the console's `systemMessage`; the session is handed the same
text as `additionalContext`.

Throttled to one print per `interval()`, and silent unless the log has grown or the
picks have changed, so a fast exchange is not padded with lines already read. Silence
and breakage look identical from the console, so the interval is readable at
`config/notify-interval` and `0` there prints on every prompt with both gates off --
then a prompt that says nothing means the hook is not running.

The stamp lives in the project dir and the interval in the config mount; the two
sources being replayed are read-only.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

CONFIG = Path(os.path.expanduser("~/.config/claude-toolkit"))
NOTIFY_LOG = CONFIG / "notifications.log"
PICKS_FILE = CONFIG / "backlog-picks.txt"
STATE_FILE = CONFIG / "project" / "notify-tail.json"
INTERVAL_FILE = CONFIG / "config" / "notify-interval"
NOTIFY_INTERVAL = 300  # seconds between prints, when config/notify-interval says nothing
TAIL_LINES = 12  # each line with a URL costs two rows below, so keep the tail short
TAIL_BYTES = 8192  # the log is unbounded; read only its end


URL_TAIL = re.compile(r"^(.*?) \((https?://\S+)\)$")
OSC8 = re.compile("\x1b]8;;([^\x1b]*)\x1b\\\\([^\x1b]*)\x1b]8;;\x1b\\\\")
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2} (\d{2}:\d{2}):\d{2}  ")
ENTRY, CONT = "  ", "      "  # an entry, and the URL row continuing it


def rows(line: str) -> list:
    """One notification as the rows it prints: the text, then its URL below.

    The full date and seconds go: a replayed tail is minutes old, so `HH:MM` is all
    the stamp anyone reads. The URL gets a row of its own because a terminal linkifies
    only a URL it can see whole, and a hard wrap would split it in two.

    An underlined title is not available here: the console drops an OSC 8 escape and
    the URL with it, and a markdown link prints as `[title](url)` verbatim. So the URL
    is shown, because it is the link -- and a line the monitor wrote while it was still
    emitting escapes is flattened to the same form, rather than replaying as a title
    with nothing to click.
    """
    line = STAMP.sub(r"\1  ", OSC8.sub(r"\2 (\1)", line.rstrip()))
    m = URL_TAIL.match(line)
    if not m:
        return [ENTRY + line]
    return [ENTRY + m.group(1), CONT + m.group(2)]


def interval() -> int:
    """Seconds to wait between prints; 0 means every prompt, with the gates off.

    Read per invocation so it can be changed mid-session from inside the container --
    `echo 0 > ~/.config/claude-toolkit/config/notify-interval` -- which is why that
    directory is the one toolkit mount the container may write. Anything missing or
    unparseable is the built-in default rather than an error: this runs on the prompt
    path, and a typo in a debugging knob must not cost a turn.
    """
    try:
        return max(0, int(INTERVAL_FILE.read_text().strip()))
    except (OSError, ValueError):
        return NOTIFY_INTERVAL


def tail() -> tuple:
    """(size, last lines) of the notification log; (0, []) if it cannot be read."""
    try:
        size = NOTIFY_LOG.stat().st_size
        with NOTIFY_LOG.open() as f:
            f.seek(max(0, size - TAIL_BYTES))
            return size, f.read().splitlines()[-TAIL_LINES:]
    except OSError:
        return 0, []


def main() -> int:
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        state = {}

    wait = interval()
    now = time.time()
    if wait and now - state.get("at", 0) < wait:
        return 0

    try:
        picks = PICKS_FILE.read_text().strip()
    except OSError:
        picks = ""
    size, lines = tail()
    # != rather than >: a truncated or rotated log is movement too, and > would stay
    # quiet until the new file grew past the byte count of the old one.
    fresh = size != state.get("size", 0)
    if wait and not fresh and picks == state.get("picks", ""):
        return 0

    def block(header: str, body: list) -> str:
        return "\n".join([header] + [r for line in body for r in rows(line)])

    sections = []
    if picks:
        sections.append(block("backlog", picks.splitlines()))
    if lines and (fresh or not wait):
        sections.append(block("monitor — most recent last", lines))
    if not sections:
        return 0

    try:
        STATE_FILE.write_text(json.dumps({"at": now, "size": size, "picks": picks}) + "\n")
    except OSError as exc:
        print(f"(could not write {STATE_FILE}: {exc})", file=sys.stderr)

    text = "\n\n".join(sections)
    # systemMessage is what the console shows; plain stdout would only reach the model
    # and the Ctrl-O transcript. additionalContext gives the session the same lines.
    print(json.dumps({
        "systemMessage": text,
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text},
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
