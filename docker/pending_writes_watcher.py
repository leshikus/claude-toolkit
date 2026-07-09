#!/usr/bin/env python3
"""Singleton host watcher: open one terminal tab at a time to drain pending writes.

Watches ~/.config/claude-toolkit/pending-writes/<project>/ folders. When a project
has pending writes and no drain is currently running, it opens ONE terminal tab
running an interactive `claude_docker.py --write` session scoped to that project --
interactive (not `-p`) so the human answers the write-permission prompts. The tab
is titled with the project name.

Drains are serial: the --write container runs as `claude-toolkit-drain`, and the
watcher opens a new tab only when no such container is running (checked via
`docker ps`, so a watcher restart cannot spawn a duplicate). Terminate a tab
manually once its project is done; the next poll opens a tab for the next project
that still has pending writes.

One instance runs no matter how many containers launch (PID-file guard). Started
detached by claude_docker.py; runs until killed:
    kill "$(cat ~/.config/claude-toolkit/pending-writes-watcher.pid)"

Host-only (opens GUI terminal tabs), so it never runs inside a container.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

QUEUE_DIR = Path(os.path.expanduser("~/.config/claude-toolkit/pending-writes"))
PIDFILE = Path(os.path.expanduser("~/.config/claude-toolkit/pending-writes-watcher.pid"))
LAUNCHER = Path(__file__).resolve().parent / "claude_docker.py"
REPOS = Path(os.path.expanduser("~/repos"))
# Where the queue folder appears inside the container (~/.config/claude-toolkit is
# mounted there; ~ resolves to the container home, so no path is hardcoded).
CONTAINER_QUEUE = "~/.config/claude-toolkit/pending-writes"
DRAIN_CONTAINER = "claude-toolkit-drain"  # name of the --write drain container
INTERVAL = 2  # seconds between polls
LAUNCH_DEBOUNCE = 30  # seconds after a launch before another, for the container to register


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


def _has_tasks(folder: Path) -> bool:
    return any(p.is_file() and p.name != "README.md" for p in folder.iterdir())


def _drain_running() -> bool:
    """True if a --write drain container is currently running (serial guard)."""
    r = subprocess.run(
        ["docker", "ps", "--filter", f"name=^{DRAIN_CONTAINER}$", "-q"],
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
    prompt_file = Path(os.path.expanduser(f"~/.config/claude-toolkit/drain-prompt-{project}.md"))
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
    launched_at = 0.0
    try:
        while True:
            QUEUE_DIR.mkdir(parents=True, exist_ok=True)
            projects = sorted(
                f.name for f in QUEUE_DIR.iterdir() if f.is_dir() and _has_tasks(f)
            )
            # Serial: at most one drain tab. Open the next project's tab only when no
            # drain container is running and we are past the launch/build window of the
            # last one (it may not appear in `docker ps` yet). `docker ps` is the
            # authoritative guard, so a watcher restart cannot spawn a duplicate.
            if (
                projects
                and time.monotonic() - launched_at > LAUNCH_DEBOUNCE
                and not _drain_running()
            ):
                _open_terminal_tab(projects[0])
                launched_at = time.monotonic()
            time.sleep(INTERVAL)
    finally:
        try:
            if PIDFILE.read_text().strip() == str(os.getpid()):
                PIDFILE.unlink()
        except (FileNotFoundError, ValueError):
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
