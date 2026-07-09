#!/usr/bin/env python3
"""Launch Claude Code in Docker with ~/repos mounted and all permissions granted.

    ./claude-docker.py [claude args...]            # read-only session (default)
    ./claude-docker.py --write [claude args...]    # read-write: real gh token

Read-only (default): a PreToolUse hook queues GitHub writes to
~/.claude/pending-writes instead of running them, and the GitHub token is a scoped
read-only App token kept fresh by a background refresher.

--write: extract the host's real gh token (from the login keychain via
`gh auth token`) into a generated hosts.yml + token file, mount those into the
container, and leave the queue hook inactive -- so the session executes GitHub
writes directly, used to drain the pending-writes queue.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import mint_gh_token

IMAGE = "claude-docker:latest"
SCRIPT_DIR = Path(__file__).resolve().parent
HOME = Path.home()


def to_container_repo_path(host_path: Path) -> str:
    """Translate a host path under ~/repos to its container path.

    Host repos are mounted at /home/agent/repos. Mirrors the shell prefix-strip
    `${p#"$HOME"/repos/}`: if the path is not under ~/repos it is left as-is
    (so a misconfigured launch fails visibly rather than silently).
    """
    prefix = f"{HOME}/repos/"
    p = str(host_path)
    sub = p[len(prefix):] if p.startswith(prefix) else p
    return f"/home/agent/repos/{sub}"


def write_mode_gh_config() -> str:
    """Materialize a gh config dir carrying the host's real token, for --write.

    On macOS the real gh token lives in the login keychain, not in
    ~/.config/gh/hosts.yml, so mounting that dir gives the container no token.
    Extract it with `gh auth token` and write a hosts.yml (for gh) plus a raw
    token file (for git's credential helper). The dir lives OUTSIDE ~/.claude so
    read-only containers -- which mount all of ~/.claude -- never see the token.
    """
    dest = HOME / ".claude-docker-write-gh"
    try:
        token = subprocess.run(
            ["gh", "auth", "token", "-h", "github.com"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.exit("error: `gh auth token` failed; run `gh auth login` on the host first.")
    if not token:
        sys.exit("error: gh returned an empty token; run `gh auth login` on the host.")

    login = subprocess.run(
        ["gh", "api", "user", "-q", ".login"], capture_output=True, text=True,
    ).stdout.strip() or "x-access-token"

    old_umask = os.umask(0o077)
    try:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "hosts.yml").write_text(
            "github.com:\n"
            f"    oauth_token: {token}\n"
            "    git_protocol: https\n"
            f"    user: {login}\n"
        )
        (dest / "token").write_text(token)
    finally:
        os.umask(old_umask)
    return str(dest)


def read_keychain_api_key() -> bytes:
    """Read the Claude Code API key from the macOS login Keychain."""
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code", "-w"],
            capture_output=True, check=True,
        )
    except subprocess.CalledProcessError:
        sys.exit(
            "error: could not read 'Claude Code' credential from the macOS Keychain.\n"
            "       Log in on the host first (run 'claude' and authenticate), then retry."
        )
    return proc.stdout


def main() -> None:
    write_mode = "--write" in sys.argv[1:]
    claude_args = [a for a in sys.argv[1:] if a != "--write"]

    # Always build: Docker's layer cache makes this a fast no-op when nothing in
    # the build context changed, and it picks up Dockerfile edits.
    subprocess.run(["docker", "build", "-t", IMAGE, str(SCRIPT_DIR)], check=True)

    # Persist onboarding state (theme, bypass-permissions acceptance, per-project
    # trust) so the initial prompts are answered once. This lives in ~/.claude.json
    # (a file in $HOME, not inside ~/.claude); ensure it exists so the bind mount
    # attaches a file rather than Docker creating an empty directory.
    claude_json = HOME / ".claude.json"
    if not claude_json.exists():
        claude_json.write_text("{}")

    # macOS stores the Claude Code credential in the login Keychain, not on disk,
    # so mounting ~/.claude alone leaves the container unauthenticated. This account
    # uses a raw API key (not OAuth), so .credentials.json can't carry it. Instead,
    # drop the key into a 0600 file under the mounted ~/.claude and point
    # apiKeyHelper at it -- the key never enters the env or `docker inspect`.
    key_file = HOME / ".claude" / ".docker-anthropic-key"
    old_umask = os.umask(0o077)
    try:
        key_file.write_bytes(read_keychain_api_key())
    finally:
        os.umask(old_umask)
    key_file.chmod(0o600)

    # GitHub auth. Read-only mode (default): mint a short-lived, read-only App
    # installation token now (so it exists before the container starts) and keep it
    # fresh with a detached, self-deduping background refresher (only one runs across
    # all containers); git reads it from a token file, gh from a dedicated hosts.yml.
    # Write mode (--write): extract the host's real gh token (from the keychain via
    # `gh auth token`) into a generated hosts.yml + token file and mount those -- no
    # mint, no refresher.
    if write_mode:
        gh_config_src = write_mode_gh_config()
        git_helper = (
            '!f() { echo username=x-access-token; '
            'echo "password=$(cat /home/agent/.config/gh/token)"; }; f'
        )
    else:
        mint_gh_token.mint()
        subprocess.Popen(
            [sys.executable, str(SCRIPT_DIR / "token_refresher.py")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        gh_config_src = f"{HOME}/.claude/gh"
        git_helper = (
            '!f() { echo username=x-access-token; '
            'echo "password=$(cat /home/agent/.claude/.docker-gh-token)"; }; f'
        )

    # Container-side path to the queue-writes PreToolUse hook (it lives next to
    # this script) and the working dir, both translated through the ~/repos mount.
    hook_script = f"{to_container_repo_path(SCRIPT_DIR)}/queue-writes.py"
    workdir = to_container_repo_path(Path.cwd())

    settings = {
        "apiKeyHelper": "cat /home/agent/.claude/.docker-anthropic-key",
        "theme": "dark",
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": f"python3 {hook_script}"}],
                }
            ]
        },
    }

    # CLAUDE_PENDING_WRITES arms the queue hook; omit it in write mode so writes run.
    pending_env = [] if write_mode else ["-e", "CLAUDE_PENDING_WRITES=1"]
    # Only allocate a TTY when we actually have one, so the launcher also works
    # headless (e.g. `--write -p "..."` driven from another process).
    tty_flags = ["-it"] if sys.stdin.isatty() else []
    docker_args = [
        "docker", "run", "--rm", *tty_flags,
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", "HOME=/home/agent",
        *pending_env,
        "-w", workdir,
        "-v", f"{HOME}/repos:/home/agent/repos:rw",
        "-v", f"{HOME}/.claude:/home/agent/.claude:rw",
        "-v", f"{HOME}/.claude.json:/home/agent/.claude.json:rw",
        "-v", f"{HOME}/.gitconfig:/home/agent/.gitconfig:ro",
        # gh config at gh's default location. Read-only mode uses a dedicated
        # ~/.claude/gh (scoped read-only token); write mode (--write) mounts the
        # host's real ~/.config/gh so the session acts with the user's own token.
        "-v", f"{gh_config_src}:/home/agent/.config/gh:rw",
        "-e", "GIT_CONFIG_COUNT=1",
        "-e", "GIT_CONFIG_KEY_0=credential.https://github.com.helper",
        "-e", f"GIT_CONFIG_VALUE_0={git_helper}",
        IMAGE, "--dangerously-skip-permissions",
        "--settings", json.dumps(settings),
        *claude_args,
    ]

    # Replace this process with docker so the interactive TTY attaches directly and
    # signals pass through. The detached refresher (new session) survives.
    os.execvp("docker", docker_args)


if __name__ == "__main__":
    main()
