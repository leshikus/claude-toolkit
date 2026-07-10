#!/usr/bin/env python3
"""Singleton host monitor for the claude-toolkit container.

One poll loop with two jobs:
  1. Keep the read-only GitHub token fresh -- re-mint when the token file is older
     than ~50 min (installation tokens live ~60 min).
  2. Drain the pending-writes queue -- for each project that has pending writes and
     no drain already running, open a terminal tab (interactive `claude.py --write`,
     titled by project) to process it. One tab PER PROJECT, run concurrently, so
     several projects drain in parallel instead of waiting in a single line. Each
     project's drain is guarded by `docker ps` on its own
     `claude-toolkit-drain-<project>` container, so a restart cannot spawn a
     duplicate for a project already draining.

One instance runs regardless of how many containers launch (PID-file guard).
Started detached by claude.py; runs until killed:
    kill "$(cat ~/.config/claude-toolkit/monitor.pid)"

Host-only (mints tokens, opens GUI terminal tabs); never runs inside a container.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import mint_gh_token
from claude import container_name

APP_DIR = Path(os.path.expanduser("~/.config/claude-toolkit"))
QUEUE_DIR = APP_DIR / "pending-writes"
PIDFILE = APP_DIR / "monitor.pid"
LAUNCHER = Path(__file__).resolve().parent / "claude.py"
REPOS = Path(os.path.expanduser("~/repos"))
# Where the queue folder appears inside the container (~ resolves to the container
# home, so no host path is hardcoded).
CONTAINER_QUEUE = "~/.config/claude-toolkit/pending-writes"
POLL = 2               # seconds between polls
MINT_MAX_AGE = 3000    # re-mint the token when older than this (50 min)
LAUNCH_DEBOUNCE = 30   # per-project: seconds after opening a tab before reopening
                       # (covers the gap before its container appears in `docker ps`)


def _already_running() -> bool:
    if not PIDFILE.exists():
        return False
    try:
        pid = int(PIDFILE.read_text().strip())
        os.kill(pid, 0)  # existence check
        return True
    except (ValueError, ProcessLookupError):
        return False  # stale PID file -- take over
    except PermissionError:
        return True


def _token_age() -> float:
    """Seconds since the token was last minted (inf if it does not exist yet)."""
    try:
        return time.time() - mint_gh_token.HOSTS_YML.stat().st_mtime
    except FileNotFoundError:
        return float("inf")


def _has_tasks(folder: Path) -> bool:
    return any(p.is_file() and p.name != "README.md" for p in folder.iterdir())


def _drain_running(project: str) -> bool:
    """True if a --write drain container for `project` is currently running."""
    r = subprocess.run(
        ["docker", "ps", "--filter", f"name=^{container_name(project)}$", "-q"],
        capture_output=True, text=True,
    )
    return bool(r.stdout.strip())


def _shquote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _osaquote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _build_prompt(project: str) -> str:
    """Assemble the exact queued tasks for `project` into one drain prompt."""
    host_folder = QUEUE_DIR / project
    container_folder = f"{CONTAINER_QUEUE}/{project}"
    files = sorted(p for p in host_folder.iterdir() if p.is_file() and p.name != "README.md")
    mds = [p for p in files if p.suffix == ".md"]
    others = [p.name for p in files if p.suffix != ".md"]
    parts = [
        f"You are the write-capable agent draining the pending-writes queue for "
        f"project '{project}'. The task files are in {container_folder}. Process each "
        f"task in order: summarize it, honor its guards, run its commands (approving "
        f"prompts as needed), and delete the file on error-less success.",
    ]
    if others:
        parts.append(
            "Companion payload files in that folder, read as referenced: "
            + ", ".join(others) + "."
        )
    parts.append("The exact queued tasks follow.")
    parts += [f"===== {p.name} =====\n{p.read_text().rstrip()}" for p in mds]
    return "\n\n".join(parts)


def _open_terminal_tab(project: str) -> None:
    """Open a terminal tab running an interactive --write drain for this project.

    Defaults to iTerm2 (swap the AppleScript here for Terminal.app or another
    emulator if needed). The exact queued task contents are handed to the session
    via a prompt file the tab's shell reads with $(cat ...), so no multi-line text
    goes through AppleScript `write text` (which would submit it line by line).
    """
    cwd = REPOS / project
    cwd = cwd if cwd.is_dir() else Path.home()
    prompt_file = APP_DIR / f"drain-prompt-{project}.md"
    prompt_file.write_text(_build_prompt(project))
    launch = (
        f"cd {_shquote(str(cwd))} && "
        f'python3 {_shquote(str(LAUNCHER))} --write "$(cat {_shquote(str(prompt_file))})"'
    )
    title = _osaquote(project)
    cmd = _osaquote(launch)
    script = (
        'tell application "iTerm2"\n'
        "  if (count of windows) = 0 then\n"
        "    create window with default profile\n"
        "    tell current session of current window\n"
        f"      set name to {title}\n"
        f"      write text {cmd}\n"
        "    end tell\n"
        "  else\n"
        "    tell current window\n"
        "      create tab with default profile\n"
        "      tell current session of current tab\n"
        f"        set name to {title}\n"
        f"        write text {cmd}\n"
        "      end tell\n"
        "    end tell\n"
        "  end if\n"
        "end tell\n"
    )
    subprocess.run(["osascript", "-e", script], check=False)


def main() -> int:
    if _already_running():
        return 0
    PIDFILE.write_text(str(os.getpid()))
    launched_at: dict[str, float] = {}  # project -> monotonic time its last tab opened
    try:
        while True:
            # 1) Keep the read-only token fresh.
            if _token_age() > MINT_MAX_AGE:
                try:
                    mint_gh_token.mint()
                except Exception as exc:  # keep the loop alive across transient failures
                    print(f"monitor: token mint failed, retrying next cycle: {exc}", file=sys.stderr)

            # 2) Drain the queue -- one tab per project, run concurrently. For each
            # project with pending writes, open a tab unless a drain container is
            # already running for it or we just opened one (within the debounce
            # window, before its container shows up in `docker ps`). Per-project
            # `docker ps` is the authoritative guard, so a monitor restart cannot
            # spawn a duplicate drain for a project already in flight.
            QUEUE_DIR.mkdir(parents=True, exist_ok=True)
            projects = sorted(
                f.name for f in QUEUE_DIR.iterdir() if f.is_dir() and _has_tasks(f)
            )
            now = time.monotonic()
            for project in projects:
                if now - launched_at.get(project, 0.0) <= LAUNCH_DEBOUNCE:
                    continue
                if _drain_running(project):
                    continue
                _open_terminal_tab(project)
                launched_at[project] = time.monotonic()

            time.sleep(POLL)
    finally:
        try:
            if PIDFILE.read_text().strip() == str(os.getpid()):
                PIDFILE.unlink()
        except (FileNotFoundError, ValueError):
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
