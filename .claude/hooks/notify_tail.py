#!/usr/bin/env python3
"""UserPromptSubmit hook: show what is worth knowing before the turn starts.

`backlog-picks.txt` and this project's `hint.md` are state, written by `monitor.py` on
the host and invisible from inside the container, each overwritten every cycle and
reprinted whole. Your open issues and PRs with news -- a comment from someone else within
`ISSUE_DAYS`, or a finished CI run -- come from `gh` per print, and each is shown once.

Printed order: the issues, then the tutorial, then the picks, and last the PR the checkout
is on -- what this session *is* working on, below what it is not. That PR is resolved from
`gh` per print, not from the `meta.json` claim, which names the PR the console was
launched on and is wrong from the moment the agent moves up a stack -- so the resolved
PR is written back over that claim, which is what the monitor routes PR updates by. A
checkout on no PR's branch, such as a review of a merged PR whose branch is gone, falls
back to the claim.

The reader sees them as the console's `systemMessage`; the session is handed the same
text as `additionalContext`.

Throttled to one print per `interval()`, and silent unless something changed, so a fast
exchange is not padded with lines already read. Silence and breakage look identical from
the console, so the interval is readable at `config/notify-interval` and `0` there prints
on every prompt with the gate off -- then a prompt that says nothing means the hook is
not running.

The stamp lives in the project dir and the interval in the config mount.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONFIG = Path(os.path.expanduser("~/.config/claude-toolkit"))
PICKS_FILE = CONFIG / "backlog-picks.txt"
HINT_FILE = CONFIG / "project" / "hint.md"
META_FILE = CONFIG / "project" / "meta.json"
STATE_FILE = CONFIG / "project" / "notify-tail.json"
INTERVAL_FILE = CONFIG / "config" / "notify-interval"
NOTIFY_INTERVAL = 300  # seconds between prints, when config/notify-interval says nothing
ISSUE_DAYS = 3  # how recent the last comment on one of your issues must be
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
        proc = subprocess.run(["gh", "pr", "view", "--json", "title,url,reviewDecision"],
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


ISSUES_QUERY = """{ viewer { login
  issues(first: 20, states: OPEN, orderBy: {field: UPDATED_AT, direction: DESC}) { nodes {
    url title comments(last: 1) { nodes { author { login } createdAt } } } }
  pullRequests(first: 20, states: OPEN, orderBy: {field: UPDATED_AT, direction: DESC}) { nodes {
    url title comments(last: 1) { nodes { author { login } createdAt } }
    commits(last: 1) { nodes { commit { statusCheckRollup { state } } } } } } } }"""
CI_WORDS = {"SUCCESS": "CI passed", "FAILURE": "CI failed", "ERROR": "CI errored"}


def issues() -> list:
    """Lines `title — what happened (url)` for your open issues and PRs with news.

    News is a last comment that is not yours and is recent, or a finished CI run on a PR.
    """
    try:
        proc = subprocess.run(["gh", "api", "graphql", "-f", f"query={ISSUES_QUERY}"],
                              capture_output=True, text=True, timeout=GH_TIMEOUT)
        viewer = json.loads(proc.stdout)["data"]["viewer"] if proc.returncode == 0 else {}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=ISSUE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    for kind in ("issues", "pullRequests"):
        for node in (viewer.get(kind) or {}).get("nodes") or []:
            news = []
            for c in node["comments"]["nodes"]:
                who = (c.get("author") or {}).get("login")
                if who != viewer["login"] and c["createdAt"] >= since:
                    news.append(f"{who} commented")
            for c in (node.get("commits") or {}).get("nodes") or []:
                state = ((c["commit"] or {}).get("statusCheckRollup") or {}).get("state")
                if state in CI_WORDS:
                    news.append(CI_WORDS[state])
            if news:
                out.append(f"{node['title']} — {', '.join(news)} ({node['url']})")
    return out


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
    label = "current pr + approved" if found.get("reviewDecision") == "APPROVED" else "current pr"
    pr = f"{label} — {found.get('title') or found['url']} ({found['url']})" if found.get("url") else ""
    commented = issues()
    news = [n for n in commented if n not in state.get("issues", [])] if wait else commented
    if (wait and not news and picks == state.get("picks", "")
            and hint == state.get("hint", "") and pr == state.get("pr", "")):
        return 0

    def block(header: str, body: list) -> str:
        return "\n".join([header] + [r for line in body for r in rows(line)])

    sections = []
    if news:
        sections.append(block("your issues and PRs", news))
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
            json.dumps({"at": now, "issues": commented, "picks": picks,
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
