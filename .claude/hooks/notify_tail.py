#!/usr/bin/env python3
"""UserPromptSubmit hook: show what the host monitor has to say, before the turn starts.

Two channels, both written by `monitor.py` and invisible from inside the container.
`backlog-picks.txt` is state -- the pair worth looking at right now, overwritten every
cycle -- and is reprinted whole. `notifications.log` is history -- CI results, PR
activity, setup hints -- and only its tail is replayed.

The reader sees them as the console's `systemMessage`; the session is handed the same
text as `additionalContext`.

Throttled to one print per NOTIFY_INTERVAL, and silent unless the log has grown or the
picks have changed, so a fast exchange is not padded with lines already read. The stamp
lives in the project dir: both sources are mounted read-only.
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
NOTIFY_INTERVAL = 300  # seconds between prints
TAIL_LINES = 12  # each line with a URL costs two rows below, so keep the tail short
TAIL_BYTES = 8192  # the log is unbounded; read only its end


URL_TAIL = re.compile(r"^(.*?) \((https?://\S+)\)$")


def rows(line: str) -> list:
    """A line with a trailing `(url)` split so the URL gets a row to itself.

    A terminal linkifies a URL it can see whole, and this console strips an OSC 8
    escape, so the bare text is the only link there is. Long lines are hard-wrapped
    to the pane, and a URL broken across rows stops being one clickable token; alone
    on its row it fits any sane width.
    """
    m = URL_TAIL.match(line)
    return [m.group(1), "    " + m.group(2)] if m else [line]


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

    now = time.time()
    if now - state.get("at", 0) < NOTIFY_INTERVAL:
        return 0

    try:
        picks = PICKS_FILE.read_text().strip()
    except OSError:
        picks = ""
    size, lines = tail()
    grew = size > state.get("size", 0)
    if not grew and picks == state.get("picks", ""):
        return 0

    def block(header: str, body: list) -> str:
        return "\n".join([header] + [r for line in body for r in rows(line)])

    sections = []
    if picks:
        sections.append(block("=== backlog ===", picks.splitlines()))
    if grew and lines:
        sections.append(block("=== monitor notifications (most recent last) ===", lines))
    if not sections:
        return 0

    try:
        STATE_FILE.write_text(json.dumps({"at": now, "size": size, "picks": picks}) + "\n")
    except OSError as exc:
        print(f"(could not write {STATE_FILE}: {exc})", file=sys.stderr)

    text = "\n".join(sections)
    # systemMessage is what the console shows; plain stdout would only reach the model
    # and the Ctrl-O transcript. additionalContext gives the session the same lines.
    print(json.dumps({
        "systemMessage": text,
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text},
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
