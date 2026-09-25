#!/usr/bin/env python3
"""UserPromptSubmit hook: show what the host monitor has to say, before the turn starts.

Two channels, both written by `monitor.py` and invisible from inside the container.
`backlog-picks.txt` and this project's `hint.md` are state -- what is worth looking at
right now, and the one capability worth learning for the session in front of you, each
overwritten every cycle -- and are reprinted whole. `notifications.log` is history -- CI results, PR
activity, setup hints -- and only its tail is replayed.

Printed tail first, then the tutorial, then the picks, and last the PR the checkout is
on -- what this session *is* working on, below what it is not. That PR is resolved from
`gh` per print, not from the `meta.json` claim, which names the PR the console was
launched on and is wrong from the moment the agent moves up a stack -- so the resolved
PR is written back over that claim, which is what the monitor routes PR updates by. A
checkout on no PR's branch, such as a review of a merged PR whose branch is gone, falls
back to the claim.

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
import subprocess
import sys
import time
from pathlib import Path

CONFIG = Path(os.path.expanduser("~/.config/claude-toolkit"))
NOTIFY_LOG = CONFIG / "notifications.log"
PICKS_FILE = CONFIG / "backlog-picks.txt"
HINT_FILE = CONFIG / "project" / "hint.md"
META_FILE = CONFIG / "project" / "meta.json"
STATE_FILE = CONFIG / "project" / "notify-tail.json"
INTERVAL_FILE = CONFIG / "config" / "notify-interval"
NOTIFY_INTERVAL = 300  # seconds between prints, when config/notify-interval says nothing
TAIL_LINES = 12  # each line with a URL costs two rows below, so keep the tail short
TAIL_BYTES = 8192  # the log is unbounded; read only its end
GH_TIMEOUT = 10  # this runs on the prompt path; a hung `gh` must not hold the turn


URL_TAIL = re.compile(r"^(.*?) \((https?://\S+)\)$")
PR_URL = re.compile(r"https?://github\.com/([^/]+/[^/]+)/pull/(\d+)")
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


def read(path: Path) -> str:
    """A whole small file, stripped; "" if it is missing."""
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def branch_pr() -> dict:
    """The checked-out branch's PR, or {} when it has none.

    Asked of `gh` rather than read from the meta.json claim, which is the PR the
    console was launched on: a stacked follow-up leaves it naming the parent. The
    interval gate above means one call per print, and anything that fails or hangs
    yields {} rather than the wrong PR.
    """
    try:
        proc = subprocess.run(["gh", "pr", "view", "--json", "title,url"],
                              capture_output=True, text=True, timeout=GH_TIMEOUT)
        pr = json.loads(proc.stdout) if proc.returncode == 0 else {}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    return pr if pr.get("url") else {}


def meta() -> dict:
    """This project's meta.json; {} if it is missing or not an object."""
    try:
        data = json.loads(META_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def claim(pr: dict) -> None:
    """Record `pr` as this project's claim in meta.json, replacing the launch claim.

    The monitor routes a PR's updates to the project claiming it and claude.py reuses
    that project's console; left at the PR the console started on, work moving up a
    stack leaves both pointing at the parent.
    """
    m = PR_URL.match(pr.get("url") or "")
    if not m:
        return
    repo, number = m.group(1), int(m.group(2))
    data = meta()
    if not data or (data.get("pr") or {}).get("key") == f"{repo}#{number}":
        return
    data["pr"] = {"key": f"{repo}#{number}", "repo": repo, "number": number,
                  "url": pr["url"], "title": pr.get("title")}
    try:
        META_FILE.write_text(json.dumps(data) + "\n")
    except OSError as exc:
        print(f"(could not write {META_FILE}: {exc})", file=sys.stderr)


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

    picks, hint, found = read(PICKS_FILE), read(HINT_FILE), branch_pr()
    claim(found)
    found = found or meta().get("pr") or {}
    pr = f"current pr — {found.get('title') or found['url']} ({found['url']})" if found.get("url") else ""
    size, lines = tail()
    # != rather than >: a truncated or rotated log is movement too, and > would stay
    # quiet until the new file grew past the byte count of the old one.
    fresh = size != state.get("size", 0)
    if (wait and not fresh and picks == state.get("picks", "")
            and hint == state.get("hint", "") and pr == state.get("pr", "")):
        return 0

    def block(header: str, body: list) -> str:
        return "\n".join([header] + [r for line in body for r in rows(line)])

    sections = []
    if lines and (fresh or not wait):
        sections.append(block("monitor — most recent last", lines))
    if hint:
        # Printed whole, not through row(): it is prose about this session, not a list
        # of events, and it carries no URL to lift onto a row of its own.
        sections.append("try this\n" + "\n".join(ENTRY + l for l in hint.splitlines()))
    if pr or picks:
        sections.append(block("backlog", picks.splitlines() + ([pr] if pr else [])))
    if not sections:
        return 0

    try:
        STATE_FILE.write_text(
            json.dumps({"at": now, "size": size, "picks": picks,
                        "hint": hint, "pr": pr}) + "\n")
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
