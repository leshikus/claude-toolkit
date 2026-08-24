#!/usr/bin/env python3
"""UserPromptSubmit hook: show the tail of the host monitor's notification log.

The monitor (`monitor.py`) appends every notification -- CI results, PR activity,
setup hints, backlog picks -- to ~/.config/claude-toolkit/notifications.log, which
is tailed in a separate iTerm tab. Inside the container that tab is invisible, so
this hook replays the same lines at the start of a turn: to the reader as the
console's `systemMessage`, and to the session as `additionalContext`.

Throttled to one print per NOTIFY_INTERVAL, and silent when the log has not grown
since the last print, so a fast exchange is not padded with the same lines again.
The stamp lives in the project dir (the log itself is mounted read-only).
"""

import json
import os
import sys
import time
from pathlib import Path

NOTIFY_LOG = Path(os.path.expanduser("~/.config/claude-toolkit/notifications.log"))
STATE_FILE = Path(os.path.expanduser("~/.config/claude-toolkit/project/notify-tail.json"))
NOTIFY_INTERVAL = 300  # seconds between prints
TAIL_LINES = 20
TAIL_BYTES = 8192  # the log is unbounded; read only its end


def main() -> int:
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        state = {}

    now = time.time()
    if now - state.get("at", 0) < NOTIFY_INTERVAL:
        return 0

    try:
        size = NOTIFY_LOG.stat().st_size
        if size <= state.get("size", 0):
            return 0
        with NOTIFY_LOG.open() as f:
            f.seek(max(0, size - TAIL_BYTES))
            tail = f.read().splitlines()[-TAIL_LINES:]
    except OSError:
        return 0

    if not tail:
        return 0

    try:
        STATE_FILE.write_text(json.dumps({"at": now, "size": size}) + "\n")
    except OSError as exc:
        print(f"(could not write {STATE_FILE}: {exc})", file=sys.stderr)

    text = "=== monitor notifications (most recent last) ===\n" + "\n".join(tail)
    # systemMessage is what the console shows; plain stdout would only reach the model
    # and the Ctrl-O transcript. additionalContext gives the session the same lines.
    print(json.dumps({
        "systemMessage": text,
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text},
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
