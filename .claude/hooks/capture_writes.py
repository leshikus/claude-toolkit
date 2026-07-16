#!/usr/bin/env python3
"""PostToolUse hook: record every remote/GitHub write into the writes log.

The working session runs in auto mode (`--permission-mode auto`): writes are not
blocked, they execute directly and the auto-mode classifier gates the dangerous
ones. This hook does not gate anything -- it *observes*. After a Bash command
that mutates remote state runs, it appends one entry to the global writes log so
a separate review session can walk every write after the fact (asynchronous,
one-at-a-time review that never blocks the working agent -- CI starts the moment
a push lands).

The log is a single consolidated store across all projects, mounted at
~/.config/claude-toolkit/writes-log/, so the review session sees one list.

Fails open (exit 0) on any unexpected error: capture is best-effort telemetry,
never a gate, so a hook failure must never disrupt the working session.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

WRITES_LOG = Path(os.path.expanduser("~/.config/claude-toolkit/writes-log"))
META_FILE = Path("/home/ubuntu/.config/claude-toolkit/project/meta.json")


def is_remote_write(cmd: str) -> bool:
    """True for commands that mutate remote/GitHub state.

    Mirrors the classifier the old read-only queue hook used, so the writes log
    catches the same operations that model gated: git push, gh write verbs, and
    a gh api call that is certainly a write.
    """
    # git push (covers `git -C <dir> push ...`), except --dry-run (non-mutating)
    if re.search(r"\bgit\b.*\bpush\b", cmd) and not re.search(r"\B--dry-run\b", cmd):
        return True

    pr_verbs = "create|edit|merge|close|comment|review|ready|reopen|lock|unlock"
    if re.search(rf"\bgh\s+pr\s+({pr_verbs})\b", cmd):
        return True
    issue_verbs = "create|edit|comment|close|reopen|delete|lock|unlock|pin|unpin|transfer"
    if re.search(rf"\bgh\s+issue\s+({issue_verbs})\b", cmd):
        return True
    if re.search(r"\bgh\s+release\s+(create|edit|delete|upload)\b", cmd):
        return True
    if re.search(r"\bgh\s+(label|secret|variable)\s+(create|edit|set|delete)\b", cmd):
        return True
    if re.search(r"\bgh\s+workflow\s+(run|enable|disable)\b", cmd):
        return True

    # gh api: a write only when certain (explicit write method, or a body= field
    # that forces POST with a write payload). graphql query vs mutation is
    # indistinguishable here, so it is not treated as a certain write.
    if re.search(r"\bgh\s+api\b", cmd) and not re.search(r"\bgraphql\b", cmd):
        m = re.search(r"(?:-X|--method)\s+([A-Za-z]+)", cmd)
        if m:
            return m.group(1).upper() in {"POST", "PATCH", "PUT", "DELETE"}
        return bool(re.search(r"""(?:-f|-F|--field|--raw-field)\s*['"]?body=""", cmd))

    return False


def is_push(cmd: str) -> bool:
    return bool(re.search(r"\bgit\b.*\bpush\b", cmd)) and not re.search(r"\B--dry-run\b", cmd)


def git(cwd: str, *args: str):
    """Run a git command in `cwd`, returning stripped stdout or None on failure."""
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def gh_repo(cwd: str):
    """`owner/name` of the repo at `cwd` (its origin), or None.

    Recorded on a push so the review session can fetch the commit remotely without
    a local checkout. The fork's own coordinate is fine -- the pushed commit lives
    there too, so `gh api repos/<repo>/commits/<sha>` resolves it.
    """
    r = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        cwd=cwd, capture_output=True, text=True,
    )
    return r.stdout.strip() or None if r.returncode == 0 else None


def project_name() -> str:
    """Real project name: basename of host_dir in meta.json, else the cwd basename.

    The repo mounts at the fixed /home/ubuntu/project, so the cwd basename is the
    generic "project"; the recorded host checkout carries the real name.
    """
    try:
        host_dir = json.loads(META_FILE.read_text())["host_dir"]
        return os.path.basename(host_dir.rstrip("/")) or os.path.basename(os.getcwd())
    except (OSError, ValueError, KeyError):
        return os.path.basename(os.getcwd())


def slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (text or "write")[:48]


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # fail open

    if event.get("tool_name") != "Bash":
        return 0
    tool_input = event.get("tool_input") or {}
    cmd = tool_input.get("command", "")
    if not cmd or not is_remote_write(cmd):
        return 0

    cwd = event.get("cwd") or os.getcwd()
    now = datetime.now()
    entry = {
        "ts": now.isoformat(timespec="seconds"),
        "project": project_name(),
        "cwd": cwd,
        "command": cmd,
        "description": tool_input.get("description", ""),
        "kind": "push" if is_push(cmd) else "github",
        "reviewed": False,
    }
    if entry["kind"] == "push":
        # Record where the push landed so the review session can locate the exact
        # commit(s) without re-deriving them: repo + HEAD sha + branch.
        entry["sha"] = git(cwd, "rev-parse", "HEAD")
        entry["branch"] = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        entry["repo"] = gh_repo(cwd)

    try:
        WRITES_LOG.mkdir(parents=True, exist_ok=True)
        desc = entry["description"] or cmd.split("\n", 1)[0]
        stamp = now.strftime("%Y-%m-%d-%H%M%S")
        path = WRITES_LOG / f"{stamp}-{slugify(desc)}.json"
        n = 2
        while path.exists():
            path = WRITES_LOG / f"{stamp}-{slugify(desc)}-{n}.json"
            n += 1
        path.write_text(json.dumps(entry, indent=2) + "\n")
    except OSError:
        return 0  # fail open: capture is telemetry, never a gate
    return 0


if __name__ == "__main__":
    sys.exit(main())
