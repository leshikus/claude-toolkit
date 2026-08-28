#!/usr/bin/env python3
"""Launch Claude Code in Docker with the current directory mounted, in auto mode.

    ./claude.py [claude args...]            # working session
    ./claude.py --review [claude args...]   # review the writes log (one window)

--review opens a session pointed at the global writes log (all projects) with the
review-mode role doc: it walks the writes one at a time, fetching each diff/PR/CI
and reporting to you, after the fact. It never gates the working sessions.

The working session runs with the host's real GitHub token and
`--permission-mode auto`: it works autonomously (no routine permission prompts)
while Claude Code's auto-mode classifier gates dangerous actions (force push,
exfiltration, production deploys, routing around a review). Routine pushes and PR
creation flow directly, so CI starts as soon as a push lands.

Our runtime state (gh config, api key, per-project queues, writes log) and toolkit
code (hooks, mode docs) live under ~/.config/claude-toolkit/ and are mounted into
the container. The host's own ~/.claude is mounted as-is, so the session runs in
the user's real Claude environment (their CLAUDE.md, skills, plugins, history);
our behavior is layered on via --settings (hooks) and --append-system-prompt.

Hooks layer our behavior on: pre_push_review (PreToolUse) gates a `git push` on a
separate reviewer agent (a different model) that inspects the commits about to be
pushed and blocks concrete defects before they reach the PR; capture_writes
(PostToolUse) logs every remote write to the global writes log
(~/.config/claude-toolkit/writes-log/) for a separate --review session to analyze
after the fact; and arm_monitor (PostToolUse) arms the host monitor's CI watch
after a successful push. The host monitor also watches open PRs.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time

import gitstore
from pathlib import Path

IMAGE = "claude-toolkit:latest"
REPO_DIR = Path(__file__).resolve().parent  # claude.py is the repo-root entry point
HOME = Path.home()
# All of our runtime state lives here (mounted into the container), kept out of
# Claude Code's own ~/.claude.
APP_DIR = HOME / ".config" / "claude-toolkit"
WORKDIR = "/home/ubuntu/project"  # the fixed container path this repo is mounted at


def real_gh_config() -> str:
    """Materialize a gh config dir carrying the host's real token.

    On macOS the real gh token lives in the login keychain, not in
    ~/.config/gh/hosts.yml, so mounting that dir gives the container no token.
    Extract it with `gh auth token` and write a hosts.yml (for gh) plus a raw
    token file (for git's credential helper). Kept in its own dir (not APP_DIR/gh,
    which the monitor's token minter still writes to) so nothing clobbers it.
    """
    dest = APP_DIR / "real-gh"
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


def stage_gnupg(proj_dir: Path) -> str:
    """Refresh this project's GPG keyring copy in place and return its path.

    gpg needs a writable GNUPGHOME even to read the keyring (it writes a lockfile
    and trustdb), so a read-only mount cannot sign. Mounting a copy (rw) instead
    of ~/.gnupg lets the container sign commits and write its own agent sockets
    without being able to modify the host keyring.

    Update the files IN PLACE -- never delete this dir. A relaunch used to rmtree
    it first, but that unlinks the directory inode, which breaks the bind mount of
    any container still running against this path: its ~/.gnupg vanishes mid-session
    (the container stays pinned to the dead inode and never sees the recreated dir).
    Overwriting files under the same dir keeps the inode, so live mounts survive.
    Sockets/locks are skipped -- uncopyable, and a concurrent session's live agent
    socket must not be clobbered.

    Per project, not one copy for every container. A single keyring mounted rw into
    several of them puts their keyboxd daemons on one lock and one socket, and the
    transcripts show what that costs: `database_open ... waiting for lock (held by
    <pid>)`, `failed to start keyboxd`, `No Keybox daemon running`. gpg needs this
    directory to itself.
    """
    src = HOME / ".gnupg"
    dest = proj_dir / "gnupg"
    dest.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(
            src, dest, dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("S.*", "*.lock", ".#*"),
        )
    dest.chmod(0o700)
    return str(dest)


def oauth_blob(blob: bytes) -> bytes | None:
    """Return blob (newline-terminated) if it is the OAuth credential JSON, else None."""
    try:
        parsed = json.loads(blob)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or "claudeAiOauth" not in parsed:
        return None
    return blob + b"\n"


def expires_at(blob: bytes) -> int:
    """When the access token in `blob` dies, ms since the epoch; 0 if it does not say."""
    return (json.loads(blob).get("claudeAiOauth") or {}).get("expiresAt") or 0


def read_credentials(creds_file: Path) -> bytes | None:
    """The live Claude Code OAuth credential: the Keychain or creds_file, later expiry wins.

    Neither source is authoritative. The host CLI does not always keep the credential
    in the Keychain -- it may write ~/.claude/.credentials.json directly -- and once a
    session is running only that file is refreshed, so a Keychain item left behind
    days ago has silently outranked a live file here.
    """
    proc = subprocess.run(
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
        capture_output=True,
    )
    found = [oauth_blob(proc.stdout.strip())] if proc.returncode == 0 else []
    if creds_file.exists():
        found.append(oauth_blob(creds_file.read_bytes().strip()))
    blobs = [b for b in found if b and expires_at(b) > time.time() * 1000]
    return max(blobs, key=expires_at) if blobs else None


def read_api_key() -> str:
    """The Anthropic API key in the 'Claude Code' Keychain item, "" if there is none.

    Separate from the OAuth item: `/login` stores one or the other, and switching an
    account from a subscription to an API key zeroes 'Claude Code-credentials' and
    writes the key here instead. The host CLI then authenticates with the key while
    the OAuth JSON stays `{}` forever.
    """
    proc = subprocess.run(
        ["security", "find-generic-password", "-s", "Claude Code", "-w"], capture_output=True)
    key = proc.stdout.strip().decode(errors="replace") if proc.returncode == 0 else ""
    return key if key.startswith("sk-ant-") else ""


def stage_credentials():
    """Stage what the container authenticates with. Returns ("oauth"|"apikey", path).

    OAuth first, the API key the host CLI is using as the fallback -- `/login` stores
    one or the other, so a host switched to an API key leaves the OAuth JSON `{}`
    forever and only the key works.

    The OAuth copy is per-container on purpose: Claude Code rewrites
    .credentials.json in place as it refreshes, a rejected refresh leaves `{}`
    behind, and sharing the host's file over the rw ~/.claude mount let one container
    wipe the host login and every other container with it.
    """
    creds = read_credentials(HOME / ".claude" / ".credentials.json")
    key = "" if creds else read_api_key()
    if not creds and not key:
        sys.exit(
            "error: the host has no usable Claude Code credential. Neither an unexpired\n"
            "       OAuth login ('Claude Code-credentials' Keychain item or\n"
            "       ~/.claude/.credentials.json) nor an API key ('Claude Code' Keychain\n"
            "       item) was found. Authenticate on the host with 'claude', then retry."
        )
    kind = "oauth" if creds else "apikey"
    path = APP_DIR / ("container-credentials.json" if creds else "anthropic-key")
    old_umask = os.umask(0o077)
    try:
        path.write_bytes(creds or key.encode() + b"\n")
    finally:
        os.umask(old_umask)
    path.chmod(0o600)
    return kind, path


def stage_claude_json(proj_dir: Path) -> Path:
    """This project's own ~/.claude.json, with WORKDIR pre-trusted. Returns its path.

    Onboarding state (theme, per-project trust) lives in ~/.claude.json -- a file in
    $HOME, not inside ~/.claude. Mounting the host's copy rw shared it with whatever
    Claude Code session was running on the host, so two processes read-modify-wrote one
    40 KB document at once and it periodically came back truncated. A container then
    found no trust flag for its workdir and stopped on a prompt with nobody to answer.

    Per project, not one copy for every container: they all mount it rw and key their
    only entry on the same fixed WORKDIR, so a single shared copy just moved that race
    inside. Four containers held one open here and it came back 0 bytes.

    Anything unparseable is discarded rather than repaired: the file is a cache of
    onboarding answers, and re-seeding it from the host costs one launch. The trust
    flag is re-asserted every run, so a corrupted file on either side heals by itself.
    """
    path = proj_dir / "claude.json"
    data = read_json(path)
    if not data:  # first run, or the previous file was corrupt
        host = read_json(HOME / ".claude.json")
        data = {k: host[k] for k in ("userID", "oauthAccount") if k in host}
        data["hasCompletedOnboarding"] = True
    projects = data.get("projects")
    if not isinstance(projects, dict):
        projects = data["projects"] = {}
    entry = projects.get(WORKDIR)
    if not isinstance(entry, dict):
        entry = projects[WORKDIR] = {}
    entry["hasTrustDialogAccepted"] = True
    entry["hasCompletedProjectOnboarding"] = True
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)  # atomic, so a launch interrupted here cannot leave a partial file
    return path


def read_json(path: Path) -> dict:
    """Parsed JSON object at `path`; {} if it is missing, unreadable or not an object."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


PR_URL = re.compile(r"^https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)")
ISSUE_URL = re.compile(r"^https?://github\.com/([^/]+)/([^/]+)/issues/(\d+)")


def gh_json(*args) -> dict:
    """Parsed JSON from a `gh` command, or exit reporting its own error."""
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"error: gh {' '.join(args)} failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


def goal(condition: str) -> str:
    """A `/goal` command: the session keeps working until `condition` holds.

    A plain prompt is one instruction, and the session stops when it judges itself
    done. A goal is re-checked before stopping, which is what a launch wants from an
    agent that has a pull request to get somewhere.

    Which is also why every condition here is met by *preparing* what needs a decision
    rather than by making it. Answering a review comment requires the user's agreement
    first, so a goal demanding answered comments would push straight through that gate
    -- the goal is reached when the patch and the proposed action are ready for them.
    """
    return f"/goal {condition}"


def stage_url(url: str):
    """(checkout, opening prompt) for a pull request or issue URL.

    A pull request brings its checkout; an issue brings nothing, because establishing
    whether it still reproduces does not need one.
    """
    if PR_URL.match(url):
        return stage_pr(url)
    m = ISSUE_URL.match(url)
    if not m:
        sys.exit(f"error: not a GitHub pull request or issue URL: {url}")
    return stage_issue(m, url)


def stage_issue(m, url: str):
    """(a working directory, the opening prompt) for an issue. Nothing is cloned.

    Deciding whether an issue still reproduces needs the issue, not a checkout, and
    these are ClickHouse-sized clones to pay for a verdict that may be "already fixed".
    The pull request comes after that verdict and from the session; `claude.py <pr-url>`
    then names its directory for the PR, which is the name that outlives the issue.

    The directory is empty and its own project, so the session gets its own queues and
    notification stamp rather than borrowing whichever one you happened to type in.
    """
    project = re.sub(r"[^a-zA-Z0-9_.-]", "-", f"{m.group(2)}-issue-{m.group(3)}")
    work = APP_DIR / "projects" / project / "work"
    work.mkdir(parents=True, exist_ok=True)
    return work, goal(f"{url} is shown either to still reproduce or not to, with the "
                      f"evidence for which")


def clone_or_fetch(repo: str, checkout: Path) -> Path:
    """Clone `repo` into `checkout`, or bring an existing clone forward. Returns it.

    The clone borrows its objects from the shared mirror when there is one, so it costs
    a working tree instead of 7.3 GB. The monitor keeps those mirrors warm; this asks
    for one anyway, for the first launch on a repo the monitor has not seen.

    The mirror is refreshed before either path, not just before a clone. An object
    reachable through alternates counts as already had, so a borrowing checkout whose
    mirror is current fetches almost nothing -- while one left to fetch for itself
    transfers every new object into its own store, and the sharing stops paying after
    the first clone.
    """
    gitstore.refresh(repo)
    if (checkout / ".git").exists():
        print(f"reusing {checkout}")
        run_step(["git", "fetch", "--prune", "origin"], cwd=checkout, fatal=False)
        return checkout
    checkout.mkdir(parents=True, exist_ok=True)
    borrow = gitstore.reference(repo)
    if borrow:
        print(f"borrowing objects from {gitstore.mirror(repo)}")
    run_step(["gh", "repo", "clone", repo, ".", *(["--", *borrow] if borrow else [])],
             cwd=checkout)
    return checkout


def stage_pr(url: str):
    """Check the PR out under APP_DIR and return (its checkout, the opening prompt).

    A pull request is either yours to finish or someone else's to read, and that
    decides what the session opens on. The token's own login is what tells them
    apart -- nothing about the URL does.

    A project already tracking this PR wins: that is the checkout you have been working
    in, and cloning a second copy would split the PR across two projects. Otherwise the
    checkout lives at projects/<repo>-<N>/repo, the same place the monitor puts a
    per-PR console, so both routes to a PR land in one directory and `claude.py`
    already reads the project name from it. A fork is synced first: its default
    branch going stale is what makes a local checkout diverge from what CI ran.

    Only the clone is fatal. Without a repository there is nothing to open, while a
    failed sync, fetch or checkout leaves a usable one, and a session told what went
    wrong is better placed to sort it out than a launcher that refuses to start.
    """
    m = PR_URL.match(url)
    if not m:
        sys.exit(f"error: not a GitHub pull request URL: {url}")
    repo, number = f"{m.group(1)}/{m.group(2)}", m.group(3)
    author = gh_json("pr", "view", number, "--repo", repo, "--json", "author")["author"]["login"]
    mine = author == gh_json("api", "user")["login"]
    prompt = goal(
        f"{url} has green CI, and every open review comment on it is triaged with a "
        f"patch prepared and a proposed action waiting for my decision -- apply, reply "
        f"and push nothing for a review comment until I have agreed to it"
        if mine else
        f"{url} is reviewed and the draft findings are ready for my approval, with "
        f"nothing posted to GitHub")

    working = project_claiming(f"{repo}#{number}")
    if working:
        print(f"{repo}#{number} is already {working}")
        return working, prompt

    # Same name monitor.py's _pr_project builds, so a PR the monitor opens and one
    # opened here are one project: a number alone is unique only within a repo.
    project = re.sub(r"[^a-zA-Z0-9_.-]", "-", f"{m.group(2)}-{number}")
    if gh_json("repo", "view", repo, "--json", "isFork")["isFork"]:
        run_step(["gh", "repo", "sync", repo], fatal=False)
    # Reused rather than recloned -- these are ClickHouse-sized repositories -- but one
    # left by an earlier session is behind both the PR and its base branch.
    checkout = clone_or_fetch(repo, APP_DIR / "projects" / project / "repo")
    # --force: this checkout is ours and disposable, so matching the PR beats preserving
    # whatever a previous session left on the branch. It does not touch an unclean
    # working tree, which is why the step still only warns.
    run_step(["gh", "pr", "checkout", number, "--force"], cwd=checkout, fatal=False)
    return checkout, prompt


def project_claiming(pr_key: str):
    """The directory of the project already tracking `pr_key`, or None.

    A pull request is one project however you reach it. session_start records the
    branch's PR into meta.json, so a checkout you already work in -- ~/repos/chp-1 for
    clickhouse-private#69819 -- is found here rather than cloned a second time under a
    name of our own, which would split that PR's queues, stamp and session in two.

    Nothing in it is touched: unlike the disposable checkout below, it is yours, so the
    branch stays where you left it.
    """
    for meta in (APP_DIR / "projects").glob("*/meta.json"):
        data = read_json(meta)
        if (data.get("pr") or {}).get("key") == pr_key and data.get("host_dir"):
            if Path(data["host_dir"]).is_dir():  # a claim can outlive its directory
                return Path(data["host_dir"])
    return None


def run_step(argv: list, cwd=None, fatal: bool = True) -> None:
    """Run `argv` with its output on our own terminal; exit, or warn, if it fails."""
    if subprocess.run(argv, cwd=cwd).returncode:
        step = " ".join(str(a) for a in argv)
        if fatal:
            sys.exit(f"error: {step} failed")
        print(f"warning: {step} failed -- opening the checkout as it stands", file=sys.stderr)


def supersede(container: str) -> None:
    """Stop any container already running this project, so the newest launch owns it.

    Per-project state -- pending-reads, pending-monitoring, meta.json, the notification
    stamp -- assumes one session at a time, and two sessions on one project do not
    notice each other: `--resume` on a live session starts a second agent that
    interleaves into the same transcript while both keep working. The monitor
    self-supersedes for the same reason; this is that rule for sessions.
    """
    if subprocess.run(["docker", "rm", "-f", container],
                      capture_output=True, text=True).returncode == 0:
        print(f"superseded the session already running in {container}")


def resumable(session: str) -> bool:
    """True if `session` still has a transcript to resume; a stale id would abort the launch."""
    return bool(session) and any((HOME / ".claude" / "projects").glob(f"*/{session}.jsonl"))


def pull_toolkit() -> None:
    """Fast-forward the toolkit checkout so committed updates apply on launch.

    Best-effort: a failure (offline, diverged, or local changes in the way) warns
    but does not block the session -- launching with slightly stale tooling beats
    refusing to start.
    """
    result = subprocess.run(
        ["git", "-C", str(REPO_DIR), "pull", "--ff-only"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"warning: git pull failed; using the current checkout:\n"
              f"{result.stderr.strip()}", file=sys.stderr)


def main() -> None:
    review_mode = "--review" in sys.argv[1:]
    claude_args = [a for a in sys.argv[1:] if a != "--review"]
    pr_url = next((a for a in claude_args if a.startswith("http")), None)
    if pr_url:
        claude_args.remove(pr_url)

    pull_toolkit()
    APP_DIR.mkdir(parents=True, exist_ok=True)
    # Per-project state lives under projects/<name>/: the pending-reads /
    # pending-monitoring queues plus meta.json (the host checkout dir, read by the
    # host monitor). The whole dir is mounted at the container's
    # ~/.config/claude-toolkit/project, so the hooks need no project logic. Create the
    # queue dirs so the bind mount attaches real dirs, not new root-owned ones.
    # A PR URL brings its own checkout and opening prompt; otherwise the session works
    # in whatever directory it was launched from.
    if pr_url:
        cwd, prompt = stage_url(pr_url)
        claude_args.append(prompt)
    else:
        cwd = Path.cwd()
    projects_dir = APP_DIR / "projects"
    # A directory we made -- projects/<name>/repo for a PR, projects/<name>/work for an
    # issue -- holds its project state one level up, so the project is that parent
    # rather than the generic leaf. Any other cwd names its project by its own basename.
    if cwd.parent.parent == projects_dir:
        project = cwd.parent.name
    else:
        project = cwd.name
    proj_dir = projects_dir / project
    # No pending-writes queue anymore: writes execute directly (auto mode). We keep
    # pending-reads (the monitor's CI/PR results inbox) and pending-monitoring (the
    # CI-watch arm requests dropped by arm_monitor).
    for sub in ("pending-reads", "pending-monitoring"):
        (proj_dir / sub).mkdir(parents=True, exist_ok=True)
    # Global writes log: one consolidated store of every remote write across all
    # projects, mounted into the container so capture_writes appends here and the
    # separate --review session walks one list.
    writes_log = APP_DIR / "writes-log"
    writes_log.mkdir(parents=True, exist_ok=True)
    # notify_tail reads the monitor's log; create it so the bind mount below attaches
    # a file rather than a new root-owned directory.
    notify_log = APP_DIR / "notifications.log"
    notify_log.touch()
    picks_file = APP_DIR / "backlog-picks.txt"
    picks_file.touch()
    # The one toolkit mount the container may write: knobs the session changes as it
    # runs, e.g. notify-interval. A directory, not a file per knob, so `echo >` and
    # anything that replaces rather than rewrites both work, and the next knob needs
    # no mount.
    config_dir = APP_DIR / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    # Merge, not overwrite: preserve a `pr` claim the monitor (or a prior
    # session_start) recorded, so an open PR stays associated with this project.
    meta_file = proj_dir / "meta.json"
    try:
        meta = json.loads(meta_file.read_text())
        if not isinstance(meta, dict):
            meta = {}
    except (OSError, ValueError):
        meta = {}
    meta["host_dir"] = str(cwd)
    meta_file.write_text(json.dumps(meta) + "\n")

    # One container per project, named for it, so this launch can take the project over
    # from whatever terminal was holding it and carry on the same conversation.
    container = f"toolkit-{project}"
    supersede(container)
    if resumable(meta.get("session_id")) and "--resume" not in claude_args:
        claude_args = ["--resume", meta["session_id"], *claude_args]
        print(f"resuming session {meta['session_id']}")

    # Always build: Docker's layer cache makes this a fast no-op when nothing in
    # the build context changed, and it picks up Dockerfile edits.
    subprocess.run(["docker", "build", "-t", IMAGE, str(REPO_DIR)], check=True)

    claude_json = stage_claude_json(proj_dir)

    # macOS may keep the Claude Code credential in the login Keychain, which the Linux
    # container cannot reach; on Linux the CLI reads ~/.claude/.credentials.json
    # instead. Staged as this container's own copy and mounted over that path below, so
    # it authenticates with no key in the env or in `docker inspect`, refreshes in
    # place, and cannot take the host login down with it.
    creds_kind, creds_copy = stage_credentials()
    # The container CLI reads OAuth from ~/.claude/.credentials.json and an API key
    # through apiKeyHelper; either way the secret is a mounted file, never an env var
    # or an argument, so it stays out of `docker inspect`.
    container_creds = ("/home/ubuntu/.claude/.credentials.json" if creds_kind == "oauth"
                       else "/home/ubuntu/.config/claude-toolkit/anthropic-key")

    # GitHub auth: the working session uses the host's real token (auto mode gates
    # dangerous writes; the token lets routine writes execute). The gh config dir
    # (hosts.yml + token) mounts at gh's default location, so git and gh share one
    # token and the git credential helper is identical.
    gh_config_src = real_gh_config()
    # Launch the host monitor (open-PR + CI watch). On startup it supersedes any
    # running instance -- SIGTERMs the incumbent via the PID file, then claims it --
    # so this launch always picks up the newest code without leaving a stale monitor.
    subprocess.Popen(
        [sys.executable, str(REPO_DIR / "monitor.py")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )

    git_helper = (
        '!f() { echo username=x-access-token; '
        'echo "password=$(cat /home/ubuntu/.config/gh/token)"; }; f'
    )

    # Toolkit code (capture_writes, session-start orientation, arm_monitor) is mounted
    # under ~/.config/claude-toolkit/ below -- NOT into ~/.claude, so we no longer
    # overwrite the user's ~/.claude dir. Fixed paths, independent of where the
    # checkout lives.
    pre_push_script = "/home/ubuntu/.config/claude-toolkit/hooks/pre_push_review.py"
    capture_script = "/home/ubuntu/.config/claude-toolkit/hooks/capture_writes.py"
    session_start_script = "/home/ubuntu/.config/claude-toolkit/hooks/session_start.py"
    arm_monitor_script = "/home/ubuntu/.config/claude-toolkit/hooks/arm_monitor.py"
    notify_tail_script = "/home/ubuntu/.config/claude-toolkit/hooks/notify_tail.py"
    # Mount the current directory at the fixed /home/ubuntu/project and work there.
    # The container is project-agnostic: no repo name appears in any container path
    # (the name lives only host-side, under projects/<name>/). No ~/repos assumption;
    # the session sees only the checkout it was launched from. (cwd and proj_dir were
    # computed above, with meta.json already recorded.)
    workdir = WORKDIR

    # Point this session at its role doc (mounted rw under
    # ~/.config/claude-toolkit/modes below, so the agent can refine it). A pointer
    # (vs injecting a snapshot) keeps one live, editable source of truth. --review
    # runs the review agent over the writes log; the default is the working agent.
    mode_doc = "review-mode" if review_mode else "working-mode"
    mode_prompt = (
        f"Follow the {mode_doc} workflow in ~/.config/claude-toolkit/modes/{mode_doc}.md. "
        "That file is the source of truth; if its guidance is wrong or incomplete (e.g. it "
        "did not prevent a mistake you just made), edit it to improve it."
    )

    # The generic toolkit prompt is read from the checkout on the host and injected
    # as an appended system prompt. Nothing from .claude/ is mounted into ~/.claude,
    # so this is the only channel for the prompt. Combine it with the mode pointer so
    # a single --append-system-prompt carries both.
    generic_prompt = (REPO_DIR / ".claude" / "toolkit-prompt.md").read_text().strip()
    append_prompt = f"{generic_prompt}\n\n{mode_prompt}"

    settings = {
        "theme": "dark",
        "outputStyle": "Concise",
        **({} if creds_kind == "oauth" else {"apiKeyHelper": f"cat {container_creds}"}),
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [{"type": "command", "command": f"python3 {session_start_script}"}],
                }
            ],
            # At the start of a turn: replay the tail of the host monitor's
            # notification log, which the container cannot see (it is tailed in an
            # iTerm tab on the host). The hook throttles itself to one print per
            # 5 minutes.
            "UserPromptSubmit": [
                {
                    "hooks": [{"type": "command", "command": f"python3 {notify_tail_script}"}],
                }
            ],
            # Before a Bash command runs: pre_push_review gates a `git push` on a
            # separate reviewer agent (a different model). It denies the push only on
            # a concrete defect and fails open otherwise, so it never wedges the
            # session; non-push commands pass straight through.
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": f"python3 {pre_push_script}"}],
                }
            ],
            # After a Bash command runs: capture_writes logs any remote write to the
            # global writes log (observe-only, never a gate), and arm_monitor arms the
            # host monitor's CI watch on a successful `git push`.
            "PostToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {"type": "command", "command": f"python3 {capture_script}"},
                        {"type": "command", "command": f"python3 {arm_monitor_script}"},
                    ],
                }
            ],
        },
    }

    # Only allocate a TTY when we actually have one, so the session also works
    # headless (e.g. `-p "..."` driven from another process).
    tty_flags = ["-it"] if sys.stdin.isatty() else []
    # --review needs every project's dir (to read each meta.json for repo/PR, and any
    # per-PR checkout under projects/<name>/repo). A working session sees only its own
    # project, so this all-projects mount is review-only. The container target
    # (.../projects) is a sibling of the per-project .../project mount, not nested.
    review_mount = (
        ["-v", f"{projects_dir}:/home/ubuntu/.config/claude-toolkit/projects:rw"]
        if review_mode else []
    )
    gnupg_copy = stage_gnupg(proj_dir)
    docker_args = [
        "docker", "run", "--rm", "--name", container, *tty_flags,
        "-e", "HOME=/home/ubuntu",
        "-w", workdir,
        "-v", f"{cwd}:{workdir}:rw",
        # Mount the host's real ~/.claude as-is: the session runs in the user's own
        # Claude environment -- their CLAUDE.md (memory), skills, commands, plugins,
        # and history. We impose nothing here; our behavior arrives via --settings
        # (hooks) and --append-system-prompt (the generic prompt) instead.
        # rw because Claude writes its runtime state (history, projects/, todos/).
        "-v", f"{HOME}/.claude:/home/ubuntu/.claude:rw",
        # ...except the credential, which is this container's alone (see stage_credentials).
        # Mounted after ~/.claude: Docker orders bind mounts by destination depth.
        # rw for OAuth, which the CLI refreshes in place; an API key is only ever read.
        "-v", f"{creds_copy}:{container_creds}:" + ("rw" if creds_kind == "oauth" else "ro"),
        # Toolkit code lives under ~/.config/claude-toolkit/, referenced by the hook
        # commands and the mode pointer -- kept out of ~/.claude so it shadows nothing.
        # Hooks are read-only; modes are rw so the agent can refine the role docs and
        # the edits land back in the checkout.
        "-v", f"{REPO_DIR}/.claude/hooks:/home/ubuntu/.config/claude-toolkit/hooks:ro",
        "-v", f"{REPO_DIR}/.claude/modes:/home/ubuntu/.config/claude-toolkit/modes:rw",
        # settings.json (permissions allowlist + enabledPlugins) at ~/.claude's default
        # location. Read-only so the container cannot clobber the committed repo source
        # (see the write-eacces-mounted-settings note). In auto mode narrow allow rules
        # speed up routine reads; the classifier gates everything else.
        "-v", f"{REPO_DIR}/.claude/settings.json:/home/ubuntu/.claude/settings.json:ro",
        "-v", f"{claude_json}:/home/ubuntu/.claude.json:rw",
        "-v", f"{HOME}/.gitconfig:/home/ubuntu/.gitconfig:ro",
        # Mount THIS project's own dir (projects/<name>/) at a fixed container path,
        # ~/.config/claude-toolkit/project/, so the hooks see project/pending-reads/...
        # with no project name. It is a fresh subtree -- nothing else mounts under it --
        # so it sidesteps the Docker Desktop virtiofs failure you get from mounting
        # proj_dir AS ~/.config/claude-toolkit and nesting hooks/modes on top.
        "-v", f"{proj_dir}:/home/ubuntu/.config/claude-toolkit/project:rw",
        # Global writes log (all projects) so capture_writes records here and the
        # --review session reads one consolidated list.
        "-v", f"{writes_log}:/home/ubuntu/.config/claude-toolkit/writes-log:rw",
        # The monitor's notification log, replayed into the session by notify_tail.
        "-v", f"{notify_log}:/home/ubuntu/.config/claude-toolkit/notifications.log:ro",
        "-v", f"{picks_file}:/home/ubuntu/.config/claude-toolkit/backlog-picks.txt:ro",
        "-v", f"{config_dir}:/home/ubuntu/.config/claude-toolkit/config:rw",
        # The shared git store, at the identical absolute path: a borrowing checkout
        # records it in objects/info/alternates, and git resolves that literally.
        *(["-v", f"{gitstore.STORE}:{gitstore.STORE}:ro"] if gitstore.STORE.is_dir() else []),
        # --review only: every project's dir (meta.json + per-PR checkouts).
        *review_mount,
        # Private copy of the GPG keyring so the container can sign commits without
        # touching the host keyring.
        "-v", f"{gnupg_copy}:/home/ubuntu/.gnupg:rw",
        # gh config (hosts.yml + token) at gh's default location: the host's real token.
        "-v", f"{gh_config_src}:/home/ubuntu/.config/gh:rw",
        "-e", "GIT_CONFIG_COUNT=1",
        "-e", "GIT_CONFIG_KEY_0=credential.https://github.com.helper",
        "-e", f"GIT_CONFIG_VALUE_0={git_helper}",
        IMAGE, "--permission-mode", "auto",
        "--settings", json.dumps(settings),
        "--append-system-prompt", append_prompt,
        *claude_args,
    ]

    # Replace this process with docker so the interactive TTY attaches directly and
    # signals pass through. The detached monitor (new session) survives.
    os.execvp("docker", docker_args)


if __name__ == "__main__":
    main()
