#!/usr/bin/env python3
"""Singleton host monitor for the claude-toolkit container.

A std-lib `sched.scheduler` drives a time-ordered queue of `Event` objects. Each
event's `fire` does its work and re-arms itself (or schedules other events) on the
scheduler, so the queue never empties and the loop runs forever. Six recurring
events cover the jobs below; the monitoring event schedules a fresh CiWatchEvent
per request it discovers -- events adding events at runtime:
  1. Service the pending-monitoring queue -- each request (dispatched by `kind`;
     `ci` today) is a job to watch, e.g. a CI run armed by the arm_monitor push
     hook. Poll it to a terminal state, then hand the result back as a
     `ci-status-*` file in pending-reads for the working agent to react to.
     Requests are claimed into memory on first sight, so deleting the request file
     mid-watch cannot abort it; pending-monitoring also doubles as durable state,
     so a restart re-scans it and resumes. GitHub is polled with the host's own gh
     credentials, independent of the container, so Actions/checks are readable.
  2. Watch every open pull request (authored by you + review-requested) for a
     change that needs your attention -- CI reaching a terminal state, a new
     comment/review from someone else, or a fresh review request. Each change is
     appended to notifications.log as a clickable link to the PR, and is handed to an
     agent: if a project already tracks the
     PR (its dir exists under projects/, or its meta.json claims it) the change
     lands in that project's pending-reads/; otherwise a per-PR iTerm console is
     opened that clones the PR into projects/pr<N>/repo and starts a session on it.
     A periodic digest summarizes the open set. The monitor only ever touches
     projects/ -- it never learns a repo's local layout, and per-PR checkouts live
     inside projects/. Per-PR state persists to pr-state.json, so the frequent
     self-supersede restarts do not re-notify; a PR seen for the first time is
     baselined silently.
  3. Read the history of a session that is coding *right now* and tell YOU -- the
     human running it -- which Claude Code capability would make it cheaper. Claude
     Code records each session as a JSONL transcript under ~/.claude/projects/ (the
     host's ~/.claude is mounted into the container, so container sessions land there
     too), appending an entry per step, so a transcript still growing is the signal
     that an agent is mid-work; only those are considered. The transcript is distilled
     into aggregate stats -- calls per tool, identical calls repeated, failures, turn
     durations, token volume -- plus a tail of the actual steps, and a separate model
     is asked what to change about the setup: adopt a skill, add a hook, send a
     subagent, allow a permission, open with a different prompt. The waste is only the
     symptom; advice the agent would have to act on is useless to the reader, and a
     new CLAUDE.md rule is charged against every future session, so the hinter is
     handed an inventory of the existing setup and told that silence is usually right.
     At most two one-line hints go into the notification log -- that is the
     whole delivery. Our own headless agents (the pre-push reviewer, this hinter)
     write transcripts into the same dirs and are skipped, so it never feeds itself.
  4. Print a clickable link to a pull request the moment a session starts working
     on it. The container's session_start hook records the checked-out branch's PR
     in the project's meta.json, so a claim that is new for a project is a session
     starting on that PR; the link is an OSC 8 hyperlink (see term.py) over
     `PR #<n>: <title>`.
  5. Post every GitHub link *mentioned in a chat* -- a pull request, an Actions run,
     an issue, a commit -- to the same log as a clickable link, so a URL the agent
     names mid-answer is one Cmd-click away instead of a selection out of a wall of
     prose. Transcripts are tailed from the offset last read, and only conversation
     text counts: a tool call or its output would put every `gh` result in the
     stream.
  6. Post two picks out of your backlog -- what has waited longest, and what is
     worth doing first. A backlog is read newest-first, so its oldest item is the one
     nobody looks at, and that is rarely the one that matters most; printing only one
     of the two is misleading either way. Both judgments depend on your repositories,
     your role and your priorities, so they are made by a headless `claude` run from
     this checkout -- it loads the same always-loaded prompt an interactive session
     here would, and that is where those rules live. It answers with one labelled URL
     per line; the titles are read back from GitHub and printed as clickable links.

Every job whose only product is a line for you to read is gated on you being at the
keyboard (`Event.requires_user`, macOS `HIDIdleTime`): away or asleep, those events
defer rather than fire, so nothing polls GitHub, spends a model call, or scrolls past
unread -- and the work is still waiting when you come back. The jobs
that serve a working agent rather than a reader are never gated.

One instance runs at a time: on startup a new monitor supersedes any running
one (SIGTERMs the incumbent via the PID file, then claims it), so a relaunch
always picks up the newest code. Started detached by claude.py; runs until
killed:
    kill "$(cat ~/.config/claude-toolkit/monitor.pid)"

Host-only (opens GUI terminal tabs, polls GitHub with the host's own gh
credentials); never runs inside a container.
"""

import json
import os
import re
import sched
import signal
import subprocess
import sys
import time
from collections import Counter, deque
from pathlib import Path

from term import TERMINAL, Hyperlink

APP_DIR = Path(os.path.expanduser("~/.config/claude-toolkit"))
PIDFILE = APP_DIR / "monitor.pid"
REPO_DIR = Path(__file__).resolve().parent
LAUNCHER = REPO_DIR / "claude.py"
# All per-project state lives under projects/<name>/: the pending-reads /
# pending-monitoring queues plus meta.json (host_dir + any PR claim). claude.py
# mounts projects/<name>/ at the container's ~/.config/claude-toolkit/project, so the
# container queue paths are project-scoped.
PROJECTS_DIR = APP_DIR / "projects"
CONTAINER_WORKDIR = "/home/ubuntu/project"  # claude.py mounts every checkout here
POLL = 2               # seconds between polls
CI_POLL_INTERVAL = 150     # seconds between polls of a single monitoring request
WATCH_EXPIRY = 6 * 3600    # give up on a watch with no terminal result after this
PR_SCAN_INTERVAL = 300     # seconds between full open-PR scans
PR_DIGEST_INTERVAL = 3600  # seconds between summary ("regular") notifications
PR_STATE_FILE = APP_DIR / "pr-state.json"  # per-PR state, so restarts don't re-notify
PR_START_FILE = APP_DIR / "pr-start.json"  # PR claimed per project, so a restart does not re-link
PR_START_INTERVAL = 10     # seconds between scans of the projects' PR claims
PR_TITLE_CHARS = 80        # a PR title is unbounded; the link text stays one line
LINK_STATE_FILE = APP_DIR / "link-state.json"  # per-transcript read offset + links posted
LINK_SCAN_INTERVAL = 15    # seconds between transcript tails: a link lands while you still care
LINK_ACTIVE_WINDOW = 3600  # only tail transcripts written this recently
LINK_STATE_TTL = 6 * 3600  # forget a transcript's offset once it is this stale
LINK_SEEN_MEMORY = 200     # links remembered per transcript, so a re-mention is not re-posted
LINK_MAX_PER_SCAN = 5      # links printed per transcript per scan; the rest are counted, not shown
LINK_HEAD_LINES = 200      # transcript head read once, for the project name and whose chat it is
NOTIFY_LOG = APP_DIR / "notifications.log"  # every notification; replayed by notify_tail

# Nothing is worth reporting to somebody who is not there, so every job that exists
# to be *read* is gated on you being at the keyboard (see `Event.requires_user`).
ACTIVE_WINDOW = 900  # seconds since the last keypress/mouse move that still counts as here
ACTIVE_POLL = 60     # how often a deferred event looks to see whether you came back

# Two picks out of the backlog (job 8 below): what has waited longest, and what is
# worth doing first. Both are judgments about your own repositories, role and
# priorities, so a headless `claude` makes them.
PICKS_DOC = REPO_DIR / ".claude" / "modes" / "backlog-picks.md"  # the generic task
PICKS_STATE_FILE = APP_DIR / "backlog-picks.json"  # last run, so a restart does not re-run
PICKS_FILE = APP_DIR / "backlog-picks.txt"  # the current pair, rewritten every cycle
PICKS = ("oldest", "highest")  # the labels it answers with, and the order they print in
PICKS_INTERVAL = 900          # seconds between selections
PICKS_RETRY = 900             # ... but a selection that failed must not cost a whole cycle
PICKS_MODEL = os.environ.get("CLAUDE_PICKS_MODEL", "claude-sonnet-5")
PICKS_TIMEOUT = 600           # seconds to wait; the model makes several gh calls
# A headless run cannot answer a permission prompt, so anything missing here is
# refused and the selection comes back empty -- read-only triage, plus the user's
# own skill scripts, which is where a backlog procedure is meant to be packaged.
PICKS_TOOLS = ["Bash(gh:*)", "Bash(~/.claude/skills/**)", "Read", "Glob", "Grep", "Skill"]

# Agent-history hints (job 3 below). Claude Code writes one JSONL transcript per
# session under ~/.claude/projects/<mangled-cwd>/<session-id>.jsonl; the host's
# ~/.claude is mounted into the container, so container sessions land here too.
CLAUDE_PROJECTS_DIR = Path(os.path.expanduser("~/.claude/projects"))
HINT_STATE_FILE = APP_DIR / "hint-state.json"  # per-transcript progress, survives restarts
HINT_DOC = REPO_DIR / ".claude" / "modes" / "history-hints.md"  # the hinter's instructions
HISTORY_SCAN_INTERVAL = 300   # seconds between transcript scans
HISTORY_ACTIVE_WINDOW = 600   # a session is "actively coding" if written within this
HINT_MIN_NEW_BYTES = 20_000   # new transcript bytes needed before hinting a session again
HINT_MIN_TOOL_CALLS = 10      # skip a window with too little work to say anything about
HINT_MAX_LINES = 1            # one hint per cycle: the stream stays glanceable
HINT_MAX_LINE_CHARS = 140     # ... and it must fit one terminal line of that stream
HINT_STATE_TTL = 6 * 3600     # forget a transcript's mark once it is this stale
HINT_RECENT_MEMORY = 5        # past hints replayed to the hinter so it does not repeat one
HISTORY_READS_PER_SCAN = 20   # transcripts read per scan (a skipped one costs only I/O)
HINT_MODEL = os.environ.get("CLAUDE_HINT_MODEL", "claude-sonnet-5")  # a second opinion
HINT_TIMEOUT = 240            # seconds to wait for the hinter
HINT_TAIL_ENTRIES = 600       # transcript entries (newest) distilled into the trace
HINT_MAX_TRACE_CHARS = 60_000  # cap the distilled trace fed to the hinter


def _supersede_incumbent() -> None:
    """Take over from any monitor already running, so a relaunch always wins.

    The monitor owns its PID file: read the incumbent's PID, SIGTERM it, and wait
    for it to actually exit before returning, so the caller can claim the PID file
    with no overlapping poll cycle. A missing/stale PID, or a process we cannot
    signal, is left behind -- we take over regardless. The default SIGTERM
    disposition skips the incumbent's `finally` cleanup, so it leaves its (now
    stale) PID behind; the caller overwrites the file unconditionally, so that is
    harmless.
    """
    try:
        pid = int(PIDFILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return  # no incumbent (or unreadable) -- nothing to supersede
    if pid == os.getpid():
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return  # already gone, or not ours to signal -- take over anyway
    for _ in range(50):  # wait up to ~5s for the incumbent to exit
        try:
            os.kill(pid, 0)  # existence check
        except ProcessLookupError:
            return
        time.sleep(0.1)


# ---- Job 3: servicing pending-monitoring -> pending-reads -------------------

# GitHub check/status verdicts grouped for terminal-state detection. A verdict
# that is not yet final counts as PENDING (the run is still going).
_PENDING_VERDICTS = {"", "PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "EXPECTED"}
_FAILED_VERDICTS = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}


def _user_active() -> bool:
    """True when this host saw keyboard or mouse input within ACTIVE_WINDOW.

    macOS exposes it as `HIDIdleTime`, nanoseconds since the last HID event; asleep or
    away, it just keeps growing. Unreadable means active: a probe that breaks must not
    silence the monitor, which would look exactly like a quiet backlog.
    """
    r = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4", "-r"],
                       capture_output=True, text=True)
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', r.stdout)
    return int(m.group(1)) / 1e9 < ACTIVE_WINDOW if m else True


def _gh_json(args):
    """Run a gh command with the host's credentials; return parsed JSON or None."""
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def _check_verdict(c: dict) -> str:
    """Normalize a check-run / status-context entry to an uppercase verdict.

    Handles both shapes we consume: a PR's statusCheckRollup (check runs carry
    `conclusion`/`status`, status contexts carry `state`) and the REST check-runs
    endpoint (`status`/`conclusion`). A not-yet-completed check reads as PENDING.
    """
    concl = c.get("conclusion")
    if concl:
        return concl.upper()
    state = c.get("state")
    if state:
        return state.upper()
    return "PENDING"


def _fetch_checks(repo, sha, pr):
    """Fetch a commit's check list, or None if it can't be fetched.

    Prefers the PR's statusCheckRollup (merges check runs + status contexts, the
    same source session_start uses); falls back to the commit check-runs endpoint
    when no PR is known.
    """
    if pr and repo:
        data = _gh_json(["pr", "view", str(pr), "--repo", repo, "--json", "statusCheckRollup"])
        if data is not None:
            return data.get("statusCheckRollup") or []
    if repo and sha:
        data = _gh_json(["api", f"/repos/{repo}/commits/{sha}/check-runs"])
        if data is not None:
            return data.get("check_runs") or []
    return None


def _monitor_ci(req: dict):
    """CI watch handler. Returns a result dict once terminal, else None.

    Result: {"conclusion": "success"|"failure", "total": int, "failed": [names]}.
    """
    checks = _fetch_checks(req.get("repo"), req.get("sha"), req.get("pr"))
    if not checks:  # None (fetch failed) or [] (CI not started) -> keep waiting
        return None
    verdicts = [_check_verdict(c) for c in checks]
    if any(v in _PENDING_VERDICTS for v in verdicts):
        return None  # still running
    failed = [
        (c.get("name") or c.get("context") or "?")
        for c, v in zip(checks, verdicts) if v in _FAILED_VERDICTS
    ]
    return {"conclusion": "failure" if failed else "success", "total": len(checks), "failed": failed}


_HANDLERS = {"ci": _monitor_ci}


def _ci_status_text(req: dict, result: dict) -> str:
    """Render a terminal CI result as a pending-reads message for the read agent."""
    pr = req.get("pr")
    pr_line = f"PR #{pr}: {req.get('pr_url')}" if pr else "PR: (none found)"
    label = req.get("branch") or (req.get("sha") or "")[:12]
    lines = [
        f"### CI result — {label} ({result['conclusion']})",
        f"Repo: {req.get('repo')}",
        f"Commit: {req.get('sha')}",
        pr_line,
        f"Checks: {result['total']} total, {len(result['failed'])} failing.",
        "",
    ]
    if result["conclusion"] == "failure":
        lines.append("Failing checks: " + ", ".join(result["failed"]))
        lines.append(
            "CI failed. Fetch the failed logs (`gh run view --log-failed` / "
            "`.claude/tools/fetch_ci_report.js`), identify the failing step, state a "
            "concrete root-cause hypothesis, then decide the fix and queue any writes."
        )
    else:
        lines.append("All checks passed. Note completion; no further action needed.")
    return "\n".join(lines) + "\n"


# ---- Event framework ---------------------------------------------------------


class Event:
    """One unit of scheduled monitoring work on a shared `sched.scheduler`.

    Subclasses implement `fire`, which does the work and re-arms itself (or
    schedules other events) via `arm`, so a recurring event keeps the scheduler's
    queue non-empty and the loop alive. `priority` breaks ties between events due
    at the same instant.
    """

    priority = 1
    requires_user = False  # True for a job whose only product is a line you read

    def __init__(self, scheduler: sched.scheduler) -> None:
        self.scheduler = scheduler

    def arm(self, delay: float) -> None:
        """Queue this event's `fire` to run `delay` seconds from now."""
        self.scheduler.enter(delay, self.priority, self.fire)

    def deferred(self) -> bool:
        """True when this job is only worth doing for a reader who is not here.

        Gated events *defer*, they do not fire and drop the line: a transition noticed
        while you were away would otherwise update the stored state, print into a tab
        nobody is tailing, and never be mentioned again. Held work is still waiting
        when you come back -- and nothing polls GitHub or spends a model call
        meanwhile. Jobs that serve a working agent rather than a reader
        (the pending-monitoring queue and its watches) are never gated.
        """
        if not self.requires_user or _user_active():
            return False
        self.arm(ACTIVE_POLL)
        return True

    def fire(self) -> None:
        raise NotImplementedError


class ScanMonitoringEvent(Event):
    """Discover new pending-monitoring requests and add a CiWatchEvent for each.

    A request (projects/<project>/pending-monitoring/<slug>.json) is claimed on
    first sight -- its path recorded in `active` and turned into a watch event --
    so a later deletion of the request file (e.g. by an over-eager
    agent) cannot abort or re-add a watch in flight. On a monitor restart `active`
    is empty and this re-scans the dir to resume: a terminal watch already deleted
    its file, so only unfinished requests reappear (run resolution from the sha is
    stateless). Re-arms every POLL.
    """

    priority = 2

    def __init__(self, scheduler: sched.scheduler) -> None:
        super().__init__(scheduler)
        self.active: set[str] = set()

    def fire(self) -> None:
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        for path in sorted(PROJECTS_DIR.glob("*/pending-monitoring/*.json")):
            key = str(path)
            if key in self.active:
                continue
            try:
                req = json.loads(path.read_text())
                first_seen = path.stat().st_mtime
            except (json.JSONDecodeError, OSError):
                continue
            self.active.add(key)
            CiWatchEvent(
                self.scheduler, path, req, path.parent.parent.name,
                path.stem, first_seen, self.active,
            ).arm(0)
        self.arm(POLL)


class CiWatchEvent(Event):
    """Poll one armed monitoring request to a terminal state, then hand the result
    back as a pending-reads file. Re-arms every CI_POLL_INTERVAL until the run is
    terminal or the watch expires; on finishing it deletes the request file and
    drops its key from the scan event's `active` set (stopping the loop and letting
    a post-restart re-scan stay clean)."""

    priority = 3

    def __init__(self, scheduler: sched.scheduler, path: Path, req: dict,
                 project: str, slug: str, first_seen: float, active: set) -> None:
        super().__init__(scheduler)
        self.path = path
        self.req = req
        self.project = project
        self.slug = slug
        self.first_seen = first_seen
        self.active = active

    def _finish(self, text: str) -> None:
        """Write the result into pending-reads/, delete the request, drop the watch."""
        dest = PROJECTS_DIR / self.project / "pending-reads"
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / f"ci-status-{self.slug}.md"
        n = 2
        while out.exists():
            out = dest / f"ci-status-{self.slug}-{n}.md"
            n += 1
        out.write_text(text)
        self.path.unlink(missing_ok=True)
        self.active.discard(str(self.path))

    def fire(self) -> None:
        handler = _HANDLERS.get(self.req.get("kind"))
        if handler is None:  # unknown kind: report once and drop, don't spin
            self._finish(
                f"### Monitoring skipped — unknown kind {self.req.get('kind')!r}\n"
                f"Request: {self.req}\n"
            )
            return
        try:
            result = handler(self.req)
        except Exception as exc:  # keep the loop alive across transient failures
            print(f"monitor: {self.req.get('kind')} handler error for {self.path}: {exc}",
                  file=sys.stderr)
            result = None
        if result is not None:
            self._finish(_ci_status_text(self.req, result))
        elif time.time() - self.first_seen > WATCH_EXPIRY:
            self._finish(
                f"### CI monitoring expired — {self.req.get('branch') or ''}\n"
                f"Repo: {self.req.get('repo')}\nCommit: {self.req.get('sha')}\n"
                f"No terminal CI result after {WATCH_EXPIRY // 3600}h; check the run manually.\n"
            )
        else:
            self.arm(CI_POLL_INTERVAL)


# ---- Job 4: watching every open PR for changes that need attention ----------


def _notify(title: str, message: str) -> None:
    """Append a notification line to the log the notify_tail hook replays into a session.

    Replaces the old macOS `display notification`: a banner sent via osascript
    opens Script Editor when clicked and scrolls away, so it never told you
    *which* PR changed. Best-effort; never raises.
    """
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {title} — {message}\n"
    try:
        with NOTIFY_LOG.open("a") as f:
            f.write(line)
    except OSError:
        pass


def _write_picks(lines: list) -> None:
    """Replace the picks file with `lines`.

    The pair is state, not history: re-posting it every cycle filled the stream with
    the same two items, so what is current overwrites what was. Best-effort; the
    caller is a scheduler job that must not die on a full disk.
    """
    try:
        PICKS_FILE.write_text("".join(f"{line}\n" for line in lines))
    except OSError:
        pass


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path: Path, data) -> None:
    """Write `data` as JSON atomically (tmp file + rename)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def _current_login() -> str:
    """The gh account login this monitor runs as (empty string if unavailable)."""
    r = subprocess.run(["gh", "api", "user", "-q", ".login"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _pr_key(pr: dict) -> str:
    """Stable identity for a PR across scans: ``owner/name#number``."""
    return f"{pr['repository']['nameWithOwner']}#{pr['number']}"


def _search_prs(filters: list) -> list:
    """Open PRs matching `filters`, via `gh search prs` (host credentials)."""
    data = _gh_json([
        "search", "prs", "--state", "open", "--limit", "100",
        "--json", "number,repository,title,url", *filters,
    ])
    return data or []


def _ci_bucket(checks) -> str:
    """Collapse a check list to one of none/pending/failure/success."""
    if not checks:
        return "none"
    verdicts = [_check_verdict(c) for c in checks]
    if any(v in _PENDING_VERDICTS for v in verdicts):
        return "pending"
    if any(v in _FAILED_VERDICTS for v in verdicts):
        return "failure"
    return "success"


def _latest_foreign_activity(detail: dict, login: str) -> str:
    """Newest ISO timestamp of a comment/review authored by someone other than `login`.

    ISO-8601 UTC strings sort lexicographically, so ``max`` gives the latest. Empty
    string when there is none (compares less than any real timestamp).
    """
    stamps = []
    for c in detail.get("comments") or []:
        if (c.get("author") or {}).get("login") != login and c.get("createdAt"):
            stamps.append(c["createdAt"])
    for rv in detail.get("reviews") or []:
        if (rv.get("author") or {}).get("login") != login and rv.get("submittedAt"):
            stamps.append(rv["submittedAt"])
    return max(stamps) if stamps else ""


def _pr_project(pr: dict) -> str:
    """Project name for a PR: ``<repo>-<number>``.

    Per-PR state and its checkout live under projects/<name>/, so the name has to be
    unique: a PR number is unique only within its repo, and ClickHouse/ClickHouse and
    ClickHouse/clickhouse-private overlap constantly. Naming the repo makes a clash
    impossible instead of something to detect after the fact -- and says which repo
    without a lookup. `claude.py` builds the same name for a PR opened by URL.
    """
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", f"{pr['repository']['name']}-{pr['number']}")


def _meta_pr_claims() -> dict:
    """Map pr_key -> project for every project whose meta.json claims a PR.

    Lets a manually-opened session (session_start records its branch's PR into
    meta.json) claim a PR, so a change routes to that existing agent instead of
    opening a duplicate console.
    """
    claims = {}
    if not PROJECTS_DIR.is_dir():
        return claims
    for meta in PROJECTS_DIR.glob("*/meta.json"):
        pr = (_load_json(meta, {}) or {}).get("pr")
        if isinstance(pr, dict) and pr.get("key"):
            claims[pr["key"]] = meta.parent.name
    return claims


def _pr_link(pr: dict) -> str:
    """A PR as an OSC 8 hyperlink over `PR #<n>: <title>`, for the notification log.

    `format` rather than `render`: the monitor's own stdout is detached and says
    nothing about the terminal that will show the line, which is whichever one the
    notify_tail hook replays it into.
    """
    title = _clip(pr.get("title") or "", PR_TITLE_CHARS)
    return Hyperlink.format(f"PR #{pr['number']}" + (f": {title}" if title else ""),
                            pr.get("url"))


_ITEM_RE = re.compile(r"github\.com/([^/]+/[^/]+)/(?:pull|issues)/(\d+)")


def _api_title(api_path: str) -> str:
    """The one-line name GitHub gives an item: a PR/issue title, a run's, a commit subject.

    Empty when the item is unreadable -- a private repo, a deleted item, no `gh`.
    """
    item = _gh_json(["api", api_path]) or {}
    text = (item.get("title") or item.get("display_title") or item.get("name")
            or ((item.get("commit") or {}).get("message") or "").split("\n")[0])
    return _clip(text.strip(), PR_TITLE_CHARS)


def _item_link(url: str) -> str:
    """A GitHub item as an OSC 8 hyperlink over its title, resolved here via `gh`.

    Only the URL is taken from the model; the title is read from GitHub, so the link
    text cannot drift from the item it points at. The bare URL when the title is
    unreadable -- a link with no text is not clickable.
    """
    m = _ITEM_RE.search(url)
    title = _api_title(f"/repos/{m.group(1)}/issues/{m.group(2)}") if m else ""
    return Hyperlink.format(title, url) if title else url


def _pr_change_text(pr: dict, notes: list) -> str:
    """Render a PR change as a pending-reads item for a agent to act on."""
    return (
        f"### PR update — {_pr_key(pr)}\n"
        f"{pr.get('title') or ''}\n"
        f"URL: {pr.get('url')}\n\n"
        f"What changed: {'; '.join(notes)}.\n\n"
        "Act on this PR: inspect it (`gh pr view` / `gh pr diff` and its review "
        "threads), decide what is needed (reply to a reviewer, root-cause and fix "
        "failing CI, take up a review request), and queue any GitHub writes as "
        "pending writes. This is a result to act on, not a command to run.\n"
    )


def _deliver_pr_read(project: str, number: int, text: str) -> None:
    """Drop a PR-update item into a project's pending-reads inbox."""
    dest = PROJECTS_DIR / project / "pending-reads"
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"pr-{number}.md"
    n = 2
    while out.exists():
        out = dest / f"pr-{number}-{n}.md"
        n += 1
    out.write_text(text)


def _write_pr_meta(project: str, pr: dict) -> None:
    """Record the PR claim + checkout dir in the project's meta.json (merging)."""
    d = PROJECTS_DIR / project
    d.mkdir(parents=True, exist_ok=True)
    meta = _load_json(d / "meta.json", {})
    if not isinstance(meta, dict):
        meta = {}
    meta["host_dir"] = str(d / "repo")
    meta["pr"] = {
        "key": _pr_key(pr),
        "repo": pr["repository"]["nameWithOwner"],
        "number": pr["number"],
        "url": pr.get("url"),
        "title": pr.get("title"),
    }
    _save_json(d / "meta.json", meta)


def _open_pr_console(pr: dict, project: str) -> None:
    """Open an iTerm tab that clones the PR into projects/<project>/repo and starts
    a session on it. The checkout stays inside the monitor's own projects/
    subtree; the repo is fetched by its GitHub coordinate, so no local repo-layout
    knowledge is needed."""
    repo = pr["repository"]["nameWithOwner"]
    checkout = PROJECTS_DIR / project / "repo"
    q = TERMINAL.shquote
    prep = (
        f"mkdir -p {q(checkout)} && cd {q(checkout)} && "
        f"{{ [ -e .git ] || gh repo clone {q(repo)} . ; }} && "
        f"gh pr checkout {pr['number']}"
    )
    launch = f"{prep} && python3 {q(LAUNCHER)}"
    TERMINAL.open_tab(f"PR #{pr['number']}", launch)


class PullRequestsEvent(Event):
    """Watch every open PR for a change that needs the user's attention.

    Sources: `gh search prs --author @me` and `--review-requested @me`. For each PR
    it compares CI state, latest foreign comment/review timestamp, and the review-
    requested flag against `self.state` (persisted to PR_STATE_FILE, so the monitor's
    frequent self-supersede restarts do not re-notify). A PR seen for the first time
    is baselined silently. On a transition it prints a notification line --
    the PR itself, as a link to Cmd-click, marked ⚠ when it needs your action -- and routes the change to a agent -- an
    existing project's pending-reads, or a fresh per-PR console. A periodic digest
    lists the standing "action required" set (review requests, red CI), so an item
    you have not acted on keeps showing even without a fresh change. Re-arms every
    PR_SCAN_INTERVAL.
    """

    priority = 4
    requires_user = True

    def __init__(self, scheduler: sched.scheduler) -> None:
        super().__init__(scheduler)
        self.state = _load_json(PR_STATE_FILE, {})
        if not isinstance(self.state, dict):
            self.state = {}
        self.login = _current_login()
        self.launched: set[str] = set()  # PRs we opened a console for this run
        self.last_digest = 0.0

    def fire(self) -> None:
        if self.deferred():
            return
        try:
            self._scan()
        except Exception as exc:  # keep the loop alive across transient failures
            print(f"monitor: PR scan failed: {exc}", file=sys.stderr)
        self.arm(PR_SCAN_INTERVAL)

    def _scan(self) -> None:
        review_prs = _search_prs(["--review-requested", "@me"])
        review_keys = {_pr_key(p) for p in review_prs}
        prs = {_pr_key(p): p for p in _search_prs(["--author", "@me"]) + review_prs}

        claims = _meta_pr_claims()
        for key, pr in prs.items():
            notes = self._evaluate(key, pr, key in review_keys)
            if notes:
                self._dispatch(key, pr, notes, claims)

        # Forget PRs that merged/closed so their state and launch guard don't linger.
        self.state = {k: v for k, v in self.state.items() if k in prs}
        self.launched &= set(prs)
        _save_json(PR_STATE_FILE, self.state)

        # Standing "action required" set: PRs whose present state needs you (a review
        # requested of you, red CI on your own PR), recomputed every scan -- so an
        # item stays marked until you act, not just on the cycle it first changed.
        action = [
            (pr, self.state[key]["action"])
            for key, pr in prs.items() if (self.state.get(key) or {}).get("action")
        ]
        now = time.time()
        if now - self.last_digest >= PR_DIGEST_INTERVAL:
            self.last_digest = now
            lines = [f"{len(prs)} open, {len(action)} action required"]
            for pr, reason in sorted(action, key=lambda pa: _pr_key(pa[0])):
                link = Hyperlink.format(_pr_key(pr), pr.get("url"))
                lines.append(f"  ⚠ {link} — {reason}")
            _notify("Open pull requests", "\n".join(lines))

    def _pr_action(self, cur: dict, is_review_req: bool) -> str | None:
        """Current 'action required' reason for a PR, or None.

        Unlike the transition notes, this reflects the PR's *present* state on every
        scan, so a standing item -- a review requested of you, red CI on your own PR
        -- keeps showing in the digest until you act, regardless of whether anything
        changed this cycle. Add reasons here as new signals are wanted.
        """
        if is_review_req:
            return "review requested"
        if cur.get("ci") == "failure":
            return "CI failing"
        return None

    def _evaluate(self, key: str, pr: dict, is_review_req: bool) -> list:
        """Update stored state for a PR; return the human-readable changes, if any.

        First sight baselines silently (returns []), so pre-existing comments/CI on
        a PR the monitor has never seen do not fire a *transition* notification --
        but the PR's `action` reason is still recorded, so a just-discovered review
        request is marked in the very next digest.
        """
        repo = pr["repository"]["nameWithOwner"]
        detail = _gh_json([
            "pr", "view", str(pr["number"]), "--repo", repo,
            "--json", "statusCheckRollup,comments,reviews",
        ]) or {}
        cur = {
            "ci": _ci_bucket(detail.get("statusCheckRollup")),
            "activity": _latest_foreign_activity(detail, self.login),
            "review_requested": is_review_req,
        }
        cur["action"] = self._pr_action(cur, is_review_req)
        prev = self.state.get(key)
        self.state[key] = cur
        if prev is None:
            return []
        notes = []
        if prev.get("ci") == "pending" and cur["ci"] in ("success", "failure"):
            notes.append(f"CI {cur['ci']}")
        if cur["activity"] and cur["activity"] > (prev.get("activity") or ""):
            notes.append("new comment/review")
        if is_review_req and not prev.get("review_requested"):
            notes.append("added as reviewer")
        return notes

    def _dispatch(self, key: str, pr: dict, notes: list, claims: dict) -> None:
        """Notify -- the PR itself, clickable -- and hand the change to a agent."""
        mark = "⚠ " if (self.state.get(key) or {}).get("action") else ""
        _notify(f"{mark}{_pr_link(pr)}", "; ".join(notes))
        text = _pr_change_text(pr, notes)

        # An agent already tracks this PR -> its pending-reads inbox. Match either an
        # explicit meta.json claim (a manual session) or the deterministic project dir
        # a prior console created.
        project = claims.get(key)
        if project is None:
            deterministic = _pr_project(pr)
            if (PROJECTS_DIR / deterministic).is_dir():
                project = deterministic
        if project is not None:
            _deliver_pr_read(project, pr["number"], text)
            return

        # No agent yet: this used to check out the PR and launch a fresh agent
        # console for it. Disabled pending a rethink of when/whether the monitor
        # should auto-spawn agents on an event -- for now an untracked PR only
        # notifies (see the ⚠ digest); nothing is checked out or launched. Restore
        # by uncommenting; _open_pr_console / _write_pr_meta remain defined.
        #
        # if key in self.launched:
        #     return
        # self.launched.add(key)
        # project = _pr_project(pr)
        # _deliver_pr_read(project, pr["number"], text)  # pre-seed the new inbox
        # _write_pr_meta(project, pr)
        # _open_pr_console(pr, project)


# ---- Job 5: hints from recent agent history ---------------------------------


def _clip(s: str, n: int) -> str:
    """Collapse whitespace and cut to `n` chars -- transcript fields are unbounded."""
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n] + "..."


def _clip_words(s: str, n: int) -> str:
    """Clip to `n` chars on a word boundary; a hint cut mid-word reads as garbage.

    The model is told to write within the limit, so this is the backstop for the
    occasional overshoot -- worth making the result still readable.
    """
    s = " ".join((s or "").split())
    if len(s) <= n:
        return s
    head = s[:n].rsplit(" ", 1)[0] or s[:n]
    return head.rstrip(" ,;:-") + "..."


def _tool_brief(inp) -> str:
    """One-line gist of a tool call: the input field that says what it acted on.

    Keyed off the field names the built-in tools use, so a Bash call reads as its
    command and a Read as its path. Two calls with the same (name, brief) are the
    same call -- that is what makes repetition detectable.
    """
    if not isinstance(inp, dict):
        return ""
    for key in ("command", "file_path", "pattern", "path", "url", "query", "prompt"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            return " ".join(val.split())
    return " ".join(json.dumps(inp, default=str).split())


def _result_text(block: dict) -> str:
    """Text of a tool_result, whose content is either a string or a block list."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text") or "" for b in content if isinstance(b, dict))
    return ""


def _read_transcript(path: Path):
    """Read one session transcript: (tail entries, session-start text, cwd, is_interactive).

    Streams the whole file but keeps only the newest HINT_TAIL_ENTRIES entries -- a
    transcript runs to megabytes and the recent history is what we hint on. Two
    things are collected on the way, because both live outside the tail:

    - the SessionStart hook's output, the only place the real project name appears
      (container sessions all run at the fixed /home/ubuntu/project, so neither the
      cwd nor the transcript's dir name distinguishes them), plus the session's
      first cwd as a fallback for a host session;
    - whether the session is interactive at all. Our own headless agents (the
      pre-push reviewer, and this hinter) write transcripts into the same dirs, and
      they are marked `sdk`/`sdk-cli`; hinting them would be noise, and hinting the
      hinter would feed itself. A session counts as interactive only once a
      human-origin typed prompt is seen.
    """
    entries = deque(maxlen=HINT_TAIL_ENTRIES)
    start = ""
    cwd = ""
    interactive = False
    with path.open(errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue  # a partially-written trailing line from a live session
            if e.get("type") == "user" and (e.get("origin") or {}).get("kind") == "human":
                interactive = True
            if not cwd and e.get("cwd"):
                cwd = e["cwd"]  # first cwd: a session can cd away mid-run
            if not start and e.get("type") == "attachment":
                att = e.get("attachment") or {}
                if att.get("hookEvent") == "SessionStart":
                    start = att.get("content") or ""
            entries.append(e)
    return list(entries), start, cwd, interactive


def _history_project(session_start: str, cwd: str, path: Path) -> str:
    """Project a transcript belongs to: the name the session-start hook reported.

    Falls back to the basename of the session's cwd, which is right for a host
    session (and merely generic -- "project" -- for a container one, where the hook
    output is the real source). A cwd of $HOME is the exception: its basename is the
    account name, which reads as a project and is not one, so it is named "home".
    Last resort is the transcript's dir name, which is the cwd path-mangled and so
    cannot be split back into components. Sanitized, since the result becomes a path
    component under projects/.
    """
    m = re.search(r"^Project name: (.+)$", session_start, re.M)
    if m and m.group(1).strip():
        name = m.group(1).strip()
    elif cwd and Path(cwd) == Path.home():
        name = "home"  # a session run outside any checkout: the username is not a project
    else:
        name = os.path.basename((cwd or "").rstrip("/")) or path.parent.name
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", name) or "unknown"


def _tilde(path: str) -> str:
    """A path with $HOME written as `~`, so the label stays one short field."""
    home = str(Path.home())
    path = path.rstrip("/")
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def _history_dir(session_start: str, cwd: str, path: Path) -> str:
    """The local directory the session is working in -- where its links come from.

    The transcript's own cwd, except for a container session: there it is the fixed
    CONTAINER_WORKDIR and names nothing, so the host checkout is read from the
    project's meta.json, which claude.py wrote at launch. A checkout under projects/
    is one we made for a PR rather than a directory you work in, and its path is far
    too long to label a line with, so those answer with the project name.
    """
    if cwd and cwd.rstrip("/") != CONTAINER_WORKDIR:
        return _tilde(cwd)
    project = _history_project(session_start, cwd, path)
    host_dir = (_load_json(PROJECTS_DIR / project / "meta.json", {}) or {}).get("host_dir")
    if host_dir and PROJECTS_DIR not in Path(host_dir).parents:
        return _tilde(host_dir)
    return project  # a checkout of ours, not somewhere you work: its path names nothing


def _distill_history(entries: list):
    """Turn transcript entries into (stats, trace, tool_calls) for the hinter.

    Raw JSONL is far too big, and mostly noise for this purpose (thinking
    signatures, whole-file reads, base64). So we hand the model two things:

    - `stats`: the aggregates that expose waste without reading every step -- calls
      per tool, which *identical* calls repeated, what failed and with what error,
      turn durations, and token volume (context re-read is the cost of bloat).
    - `trace`: the tail of the real steps, so a hint can name the calls involved.
    """
    tools = Counter()      # tool name -> number of calls
    sigs = Counter()       # (name, brief) -> calls; >1 means a literally repeated call
    calls = {}             # tool_use id -> (name, brief), to attribute a failed result
    failures = []
    durations = []
    prompts = 0
    out_tokens = cache_reads = 0
    lines = []

    for e in entries:
        etype = e.get("type")
        if etype == "attachment":
            att = e.get("attachment") or {}
            if att.get("hookEvent") == "SessionStart":
                lines.append(f"[session start] {_clip(att.get('content') or '', 400)}")
            continue
        if etype == "system":
            if e.get("subtype") == "turn_duration":
                secs = (e.get("durationMs") or 0) / 1000
                durations.append(secs)
                lines.append(f"[turn ended: {secs:.0f}s over {e.get('messageCount')} messages]")
            continue
        msg = e.get("message") or {}
        if etype == "assistant":
            usage = msg.get("usage") or {}
            out_tokens += usage.get("output_tokens") or 0
            cache_reads += usage.get("cache_read_input_tokens") or 0
        content = msg.get("content")
        if isinstance(content, str):  # a typed human prompt
            if etype == "user":
                prompts += 1
                lines.append(f"USER: {_clip(content, 600)}")
            continue
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            kind = b.get("type")
            if kind == "text":
                if etype == "user":
                    prompts += 1
                    lines.append(f"USER: {_clip(b.get('text') or '', 600)}")
                else:
                    lines.append(f"ASSISTANT: {_clip(b.get('text') or '', 300)}")
            elif kind == "tool_use":
                name = b.get("name") or "?"
                brief = _tool_brief(b.get("input"))
                tools[name] += 1
                sigs[(name, brief)] += 1
                calls[b.get("id")] = (name, brief)
                lines.append(f"TOOL {name}: {_clip(brief, 300)}")
            elif kind == "tool_result":
                text = _result_text(b)
                if b.get("is_error"):
                    failures.append((calls.get(b.get("tool_use_id")), _clip(text, 200)))
                    lines.append(f"  -> ERROR: {_clip(text, 200)}")
                else:
                    lines.append(f"  -> {_clip(text, 200)}")

    stats = [f"Tool calls: {sum(tools.values())} total"]
    if tools:
        stats.append("  by tool: " + ", ".join(f"{n} x{c}" for n, c in tools.most_common()))
    repeats = [(sig, c) for sig, c in sigs.most_common(12) if c > 1]
    if repeats:
        stats.append(f"Identical calls made more than once ({len(repeats)} distinct):")
        for (name, brief), c in repeats:
            stats.append(f"  x{c} {name}: {_clip(brief, 200)}")
    if failures:
        stats.append(f"Failed tool calls: {len(failures)}")
        for call, err in failures[-8:]:
            name, brief = call or ("?", "")
            stats.append(f"  {name}: {_clip(brief, 120)} -> {err}")
    if durations:
        stats.append(
            f"Turns: {len(durations)}, total {sum(durations) / 60:.1f} min, "
            f"slowest {max(durations):.0f}s"
        )
    stats.append(f"Typed user prompts: {prompts}")
    stats.append(
        f"Assistant output tokens: {out_tokens}; context re-read from cache: {cache_reads}"
    )

    # Keep the newest lines that fit -- the recent steps are the ones worth hinting on.
    trace, size = [], 0
    for ln in reversed(lines):
        size += len(ln) + 1
        if size > HINT_MAX_TRACE_CHARS:
            trace.append("[... earlier steps omitted ...]")
            break
        trace.append(ln)
    trace.reverse()
    return "\n".join(stats), "\n".join(trace), sum(tools.values())


def _always_loaded() -> list:
    """Prompt files loaded into every session's context, host paths.

    The project's own CLAUDE.md is found through meta.json's recorded host_dir, since
    the monitor never learns a repo's layout otherwise.
    """
    files = [
        Path(os.path.expanduser("~/.claude/CLAUDE.md")),
        REPO_DIR / ".claude" / "toolkit-prompt.md",
        REPO_DIR / ".claude" / "modes" / "working-mode.md",
    ]
    return [f for f in files if f.is_file()]


def _skill_names(project: str) -> list:
    """Skills reachable from a session: the user's own plus the project's."""
    roots = [Path(os.path.expanduser("~/.claude/skills"))]
    host_dir = (_load_json(PROJECTS_DIR / project / "meta.json", {}) or {}).get("host_dir")
    if host_dir:
        roots.append(Path(host_dir) / ".claude" / "skills")
    names = set()
    for root in roots:
        for skill in root.glob("*/SKILL.md"):
            names.add(skill.parent.name)
    return sorted(names)


def _setup_inventory(project: str) -> str:
    """What is already configured, so the hinter proposes only genuinely new setup.

    Also states the size of the always-loaded prompt text: the cost of another
    CLAUDE.md rule is the whole judgment call, and it should be a number the model can
    see rather than an abstraction.
    """
    loaded = _always_loaded()
    chars = sum(len(f.read_text(errors="replace")) for f in loaded)
    hooks = sorted(p.stem for p in (REPO_DIR / ".claude" / "hooks").glob("*.py"))
    skills = _skill_names(project)
    return "\n".join([
        "Already configured -- do not propose any of these again:",
        f"  hooks: {', '.join(hooks) or '(none)'}",
        "  monitor jobs: CI watch armed on push (result lands in pending-reads),",
        "    open-PR watch (comments/reviews/CI/review requests), these hints",
        "  session settings: auto permission mode, pre-push review by a second model,",
        "    every remote write logged for a separate --review session",
        f"  skills available: {', '.join(skills) or '(none)'}",
        "",
        "Context already spent before a session starts: "
        f"~{chars:,} characters of always-loaded prompt text across {len(loaded)} files "
        f"({', '.join(f.name for f in loaded)}). Anything added there is paid again on "
        "every future session.",
    ])


def _run_claude(prompt: str, model: str, timeout: int, tools: list = ()):
    """Run a headless `claude`; None on any failure, which the caller skips.

    Authenticates like any host CLI invocation, from the login Keychain. Run from the
    checkout so the model sees a fixed environment regardless of where the monitor was
    launched -- and so it loads the same always-loaded prompt a session here would.
    Without `tools` the run is text-only: a headless run cannot answer a permission
    prompt, so anything not allow-listed here is refused rather than asked about.
    Blocking -- see the calling class's docstring on scheduler occupancy.
    """
    tool_args = ["--allowedTools", *tools] if tools else []
    try:
        r = subprocess.run(
            ["claude", "-p", "--model", model, *tool_args],
            input=prompt, capture_output=True, text=True,
            timeout=timeout, cwd=str(REPO_DIR),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _hint_lines(out: str) -> list:
    """Split hinter output into individual one-line hints, capped for the stream.

    The hints are read inline in the notification stream, so each has to survive as a
    single glanceable line: bullet markers are stripped, a wrapped bullet is folded
    back into one line, and both the count and the length are enforced here rather
    than trusted to the model.
    """
    hints = []
    for raw in out.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith(("- ", "* ")) or re.match(r"^\d+[.)]\s", s):
            hints.append(re.sub(r"^([-*]|\d+[.)])\s*", "", s))
        elif hints:
            hints[-1] += " " + s  # a bullet the model wrapped across lines
        else:
            hints.append(s)       # output with no bullet markers at all
    out_lines = []
    for h in hints:
        h = _clip_words(h, HINT_MAX_LINE_CHARS)
        if h:
            out_lines.append(h)
        if len(out_lines) >= HINT_MAX_LINES:
            break
    return out_lines


class HistoryHintsEvent(Event):
    """Tell the operator which Claude Code capability would make the live session cheaper.

    Claude Code records every session as a JSONL transcript under
    ~/.claude/projects/; the host's ~/.claude is mounted into the container, so
    container sessions show up here too. Each cycle takes the single most recently
    written transcript (see `_candidates` -- a live file means an agent is mid-work),
    distills it into aggregate stats plus a trace tail (see `_distill_history` -- the
    raw file is far too large to hand to a model), and asks a separate model what the
    human should change about the setup. One session, one hint, then the cycle ends:
    the scan interval is the cadence, and the stream stays a slow drip of single
    lines.

    The audience is the human, not the agent: the waste in the trace is the symptom,
    and the hint is the mechanism that removes it -- a skill, a hook, a subagent, a
    permission rule, a different opening prompt. Advice the agent would have to act on
    is worthless here, and a new CLAUDE.md rule is charged against every future
    session, so the hinter is given an inventory of the existing setup and told that
    saying nothing is the common right answer. The hint prints inline into the
    notification log; there is no other delivery.

    A session is looked at again once HINT_MIN_NEW_BYTES of fresh transcript exist, so
    consecutive cycles see genuinely new work; the last HINT_RECENT_MEMORY hints are
    replayed to the hinter so it cannot rephrase advice already sent. State is
    persisted to HINT_STATE_FILE, so the monitor's frequent self-supersede restarts
    neither re-hint covered history nor forget what was already said.

    The analysis blocks the (single-threaded) scheduler for up to HINT_TIMEOUT, which
    one-per-cycle also bounds. Re-arms every HISTORY_SCAN_INTERVAL.
    """

    priority = 5
    requires_user = True

    def __init__(self, scheduler: sched.scheduler) -> None:
        super().__init__(scheduler)
        self.state = _load_json(HINT_STATE_FILE, {})
        if not isinstance(self.state, dict):
            self.state = {}

    def fire(self) -> None:
        if self.deferred():
            return
        try:
            self._scan()
        except Exception as exc:  # keep the loop alive across transient failures
            print(f"monitor: history hint scan failed: {exc}", file=sys.stderr)
        self.arm(HISTORY_SCAN_INTERVAL)

    def _candidates(self) -> list:
        """Transcripts of sessions coding *right now*, most recently active first.

        A transcript's mtime is the session's pulse: the harness appends an entry per
        step, so a file written in the last HISTORY_ACTIVE_WINDOW means an agent is
        mid-work, and one that stopped growing means the session ended or is parked
        waiting on the user. Only the live ones qualify -- a hint is worth sending
        when it can still change how the work goes; hinting a session that has
        stopped is advice about something already over.

        Newest first because the cycle hints the single most recent session; the rest
        of the list is only a fallback for when that one turns out to be unreadable.

        Cheap filters only (mtime, size against the recorded mark) -- reading and
        distilling happens per analysis, so a scan over hundreds of transcripts
        stays a stat() walk.
        """
        now = time.time()
        out = []
        for path in CLAUDE_PROJECTS_DIR.glob("*/*.jsonl"):
            try:
                st = path.stat()
            except OSError:
                continue
            if now - st.st_mtime > HISTORY_ACTIVE_WINDOW:
                continue  # not being written to -- nobody is coding in this session
            prev = self.state.get(str(path)) or {}
            if st.st_size - (prev.get("size") or 0) < HINT_MIN_NEW_BYTES:
                continue  # no new history, so nothing new to say about it
            out.append((st.st_mtime, st.st_size, path))
        out.sort(reverse=True)
        return out

    def _scan(self) -> None:
        """Hint the most recently active session, once, and stop.

        One session per cycle: the scan interval is the cadence, so the stream stays a
        slow drip of single lines rather than a wall of advice. `NONE` from the hinter
        ends the cycle too -- we do not go shopping through older sessions for
        something to say. The loop past the first candidate only covers transcripts
        that cannot be analyzed at all (a headless agent's, or too little work in the
        window), which should not silently consume the cycle.
        """
        now = time.time()
        # Bound the state file: drop transcripts we will never look at again.
        self.state = {
            k: v for k, v in self.state.items()
            if now - (v.get("hinted_at") or 0) < HINT_STATE_TTL
        }
        for _, size, path in self._candidates()[:HISTORY_READS_PER_SCAN]:
            key = str(path)
            recent = list((self.state.get(key) or {}).get("recent") or [])
            # Mark examined before the slow analysis, so a crash cannot wedge the queue.
            self.state[key] = {"size": size, "hinted_at": now, "recent": recent}
            _save_json(HINT_STATE_FILE, self.state)
            hints = self._analyze(path, recent)
            if hints is None:
                continue  # unreadable session: fall through to the next-most-recent
            if hints:
                self.state[key]["recent"] = (recent + hints)[-HINT_RECENT_MEMORY:]
                _save_json(HINT_STATE_FILE, self.state)
            return

    def _analyze(self, path: Path, recent: list):
        """Hint on one transcript: the hints sent (possibly none), or None if unreadable.

        `recent` is what this session was already told, replayed to the hinter so a
        cycle five minutes later does not rephrase the same advice over an overlapping
        window.
        """
        entries, session_start, cwd, interactive = _read_transcript(path)
        if not interactive or not entries:
            return None  # a headless reviewer/hinter run, or nothing to read
        stats, trace, tool_calls = _distill_history(entries)
        # Growth alone can be one huge tool result or a stretch of conversation.
        if tool_calls < HINT_MIN_TOOL_CALLS or not trace.strip():
            return None
        try:
            instructions = HINT_DOC.read_text()
        except OSError:
            instructions = (
                "You are reading a distilled trace of a Claude Code session. Your "
                "reader is the human running it, not the agent: say which Claude Code "
                "capability -- a skill, hook, subagent, slash command, permission rule, "
                "background task, or a different opening prompt -- would remove the "
                "waste you see. Never address the agent, and never propose CLAUDE.md "
                "prompt text unless the agent hits it often and gets it wrong often, "
                "since prompt text is paid on every future session. First line exactly "
                "NONE if nothing is worth the reader's attention (the common answer); "
                "otherwise exactly one hint, a single `- ` bullet fitting one "
                "140-character terminal line, recommendation first: `do X: because Y`."
            )
        project = _history_project(session_start, cwd, path)
        already = ""
        if recent:
            already = (
                "===== already sent to this reader =====\n"
                "Do not repeat or rephrase any of these; find something else or say NONE.\n"
                + "\n".join(f"- {h}" for h in recent) + "\n\n"
            )
        out = _run_claude(
            f"{instructions}\n\n"
            f"===== session =====\nproject: {project}\ntranscript: {path.name}\n\n"
            f"===== existing setup =====\n{_setup_inventory(project)}\n\n"
            f"{already}"
            f"===== statistics =====\n{stats}\n\n"
            f"===== recent steps =====\n{trace}\n",
            HINT_MODEL, HINT_TIMEOUT,
        )
        if out is None:
            print(f"monitor: hinter unavailable for {path.name}", file=sys.stderr)
            return None
        first = next((ln.strip() for ln in out.splitlines() if ln.strip()), "")
        if first.upper().startswith("NONE"):
            return []  # ran, found nothing worth saying: the cycle is spent
        hints = _hint_lines(out)
        for hint in hints:
            _notify(f"hint {project}", hint)
        return hints


# ---- Job 6: a clickable link to the PR a session just started on ------------


class PrStartEvent(Event):
    """Print a clickable link to the PR a session has just started working on.

    The container's session_start hook resolves the checked-out branch's PR and
    records it in that project's meta.json, so a claim new for a project *is* a
    session starting on that PR -- nothing extra to trigger, and a PR reached by
    any route (manual checkout, `gh pr checkout`, a per-PR console) announces
    itself. Claims are persisted so the frequent self-supersede restarts stay
    quiet, and a first run with no state at all baselines every existing claim
    silently. Re-arms every PR_START_INTERVAL.
    """

    priority = 6
    requires_user = True

    def __init__(self, scheduler: sched.scheduler) -> None:
        super().__init__(scheduler)
        self.seen = _load_json(PR_START_FILE, None)
        self.baseline = not isinstance(self.seen, dict)
        if self.baseline:
            self.seen = {}

    def fire(self) -> None:
        if self.deferred():
            return
        changed = False
        for meta in sorted(PROJECTS_DIR.glob("*/meta.json")):
            pr = (_load_json(meta, {}) or {}).get("pr")
            if not isinstance(pr, dict) or not pr.get("url") or not pr.get("number"):
                continue
            project = meta.parent.name
            if self.seen.get(project) == pr.get("key"):
                continue
            self.seen[project] = pr.get("key")
            changed = True
            if self.baseline:
                continue
            _notify(f"pr start {project}", _pr_link(pr))
        # A baseline pass persists even an empty map, so the next start is not one too.
        if changed or self.baseline:
            _save_json(PR_START_FILE, self.seen)
        self.baseline = False
        self.arm(PR_START_INTERVAL)


# ---- Job 7: GitHub links mentioned in a chat --------------------------------

_URL_RE = re.compile(r"https?://(?:www\.)?github\.com/[^\s<>\"'`)\]]+")
_LINK_KINDS = (  # path pattern -> (what it refers to, API path naming it), tried in order
    (re.compile(r"([^/]+/[^/]+)/pull/(\d+)"), "PR {0}#{1}", "/repos/{0}/issues/{1}"),
    (re.compile(r"([^/]+/[^/]+)/issues/(\d+)"), "issue {0}#{1}", "/repos/{0}/issues/{1}"),
    (re.compile(r"([^/]+/[^/]+)/actions/runs/(\d+)"), "run {1} in {0}",
     "/repos/{0}/actions/runs/{1}"),
    (re.compile(r"([^/]+/[^/]+)/commit/([0-9a-f]{7,40})"), "commit {0}@{1}",
     "/repos/{0}/commits/{1}"),
    (re.compile(r"([^/]+/[^/]+)/releases/tag/([^/]+)"), "release {0}@{1}",
     "/repos/{0}/releases/tags/{1}"),
)


def _github_ref(url: str):
    """(what a GitHub URL refers to, the API path naming it), the path itself if neither."""
    path = url.split("github.com/", 1)[-1]
    for pattern, label, api in _LINK_KINDS:
        m = pattern.match(path)
        if m:
            return label.format(*m.groups()), api.format(*m.groups())
    return path.rstrip("/") or url, None


def _github_key(url: str) -> str:
    """Identity of a GitHub URL, so /pull/7, /pull/7/files and a comment on it are one PR."""
    return _github_ref(url)[0]


def _github_text(url: str) -> str:
    """Link text for a GitHub URL: the item's title, which is what the reader recognises.

    A URL says only where a thing lives. Falls back to naming the kind and number
    when GitHub cannot be asked for the title.
    """
    ref, api = _github_ref(url)
    return (_api_title(api) if api else "") or ref


def _chat_urls(chunk: str) -> list:
    """Every GitHub URL said in the chat text of these transcript lines, in order.

    Conversation only -- a typed prompt or a message's text block. A tool call and
    its result are excluded on purpose: one `gh api` result would fill the stream
    with links nobody mentioned.
    """
    urls = []
    for line in chunk.splitlines():
        try:
            e = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if e.get("type") not in ("user", "assistant"):
            continue
        content = (e.get("message") or {}).get("content")
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            texts = [b.get("text") or "" for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
        else:
            continue
        for text in texts:
            urls += [u.rstrip(".,;:!?*_") for u in _URL_RE.findall(text)]
    return urls


def _transcript_head(path: Path):
    """(is_a_chat, dir) read once from a transcript's first LINK_HEAD_LINES entries.

    Both answers live in the opening entries: the session-start hook's output and the
    first cwd locate the checkout, and a human-origin prompt is what separates a chat
    from one of our own headless agents (marked `sdk`), whose text is not addressed
    to the reader.
    """
    chat = False
    start = cwd = ""
    with path.open(errors="replace") as f:
        for n, line in enumerate(f):
            if n >= LINK_HEAD_LINES:
                break
            try:
                e = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if e.get("type") == "user" and (e.get("origin") or {}).get("kind") == "human":
                chat = True
            if not cwd and e.get("cwd"):
                cwd = e["cwd"]
            if not start and e.get("type") == "attachment":
                att = e.get("attachment") or {}
                if att.get("hookEvent") == "SessionStart":
                    start = att.get("content") or ""
    return chat, _history_dir(start, cwd, path)


class GithubLinksEvent(Event):
    """Print every GitHub link a chat mentions, so it is one Cmd-click away.

    A pull request, Actions run, issue or commit named mid-answer is something the
    reader wants to open, and the notification log is replayed where they are working;
    an OSC 8 hyperlink there beats hunting the URL out of agent prose. Each
    transcript is tailed from the offset last read, stopping at the last complete
    line so a half-written entry is picked up whole on the next scan, and only chat
    text is searched (`_chat_urls`). A link is keyed by what it refers to, and the
    keys posted are remembered per transcript, so the same PR named ten times prints
    once. Beyond LINK_MAX_PER_SCAN per scan only the count prints -- a digest of
    thirty PRs is not thirty lines. Offsets persist, so the frequent self-supersede
    restarts neither re-post nor re-read; a first run with no state at all starts
    every transcript at its current size, keeping the backlog out of the stream.
    Re-arms every LINK_SCAN_INTERVAL.
    """

    priority = 7
    requires_user = True

    def __init__(self, scheduler: sched.scheduler) -> None:
        super().__init__(scheduler)
        self.state = _load_json(LINK_STATE_FILE, None)
        self.baseline = not isinstance(self.state, dict)
        if self.baseline:
            self.state = {}

    def fire(self) -> None:
        if self.deferred():
            return
        try:
            self._scan()
        except Exception as exc:  # keep the loop alive across transient failures
            print(f"monitor: github link scan failed: {exc}", file=sys.stderr)
        self.baseline = False
        self.arm(LINK_SCAN_INTERVAL)

    def _scan(self) -> None:
        now = time.time()
        self.state = {k: v for k, v in self.state.items()
                      if now - (v.get("seen_at") or 0) < LINK_STATE_TTL}
        changed = self.baseline
        for path in sorted(CLAUDE_PROJECTS_DIR.glob("*/*.jsonl")):
            try:
                st = path.stat()
            except OSError:
                continue
            if now - st.st_mtime > LINK_ACTIVE_WINDOW:
                continue  # nothing said here for an hour
            mark = self.state.get(str(path))
            if mark is None or "dir" not in mark:  # new, or written before dir replaced project
                try:
                    chat, where = _transcript_head(path)
                except OSError:
                    continue
                mark = {"offset": mark["offset"] if mark else (st.st_size if self.baseline else 0),
                        "chat": chat, "dir": where, "seen": (mark or {}).get("seen") or []}
                self.state[str(path)] = mark
                changed = True
            mark["seen_at"] = now
            if not mark.get("chat") or st.st_size <= mark["offset"]:
                continue
            changed |= self._tail(path, mark)
        if changed:
            _save_json(LINK_STATE_FILE, self.state)

    def _tail(self, path: Path, mark: dict) -> bool:
        """Read what was appended to one transcript and print the links it mentions."""
        try:
            with path.open("rb") as f:
                f.seek(mark["offset"])
                raw = f.read()
        except OSError:
            return False
        end = raw.rfind(b"\n") + 1
        if not end:
            return False  # only a half-written entry so far: leave it for the next scan
        mark["offset"] += end
        posted = set(mark.get("seen") or [])
        fresh = []
        for url in _chat_urls(raw[:end].decode(errors="replace")):
            key = _github_key(url)
            if key in posted:
                continue
            posted.add(key)
            fresh.append((key, url))
        for _, url in fresh[:LINK_MAX_PER_SCAN]:
            _notify(mark["dir"], Hyperlink.format(_github_text(url), url))
        if len(fresh) > LINK_MAX_PER_SCAN:
            _notify(mark["dir"],
                    f"{len(fresh) - LINK_MAX_PER_SCAN} more GitHub links mentioned, not shown")
        mark["seen"] = (list(mark.get("seen") or []) + [k for k, _ in fresh])[-LINK_SEEN_MEMORY:]
        return True


# ---- Job 8: what has waited longest, and what to do first -------------------


def _picks(out: str) -> dict:
    """The selector's `<label>: <url>` lines, as url -> the labels that chose it.

    Keyed by URL rather than by label so one item picked twice prints one line saying
    it is both, instead of the same title twice. A label whose line names no item is
    simply absent -- the selector is told to omit what it cannot fill.
    """
    found = {}
    for line in out.splitlines():
        label, _, rest = line.partition(":")
        if label.strip().lower() not in PICKS:
            continue
        url = next(iter(_URL_RE.findall(rest)), "")
        if _ITEM_RE.search(url):
            found.setdefault(url, []).append(label.strip().lower())
    return found


class BacklogPicksEvent(Event):
    """Post what has waited on you longest, and what is worth doing first.

    A backlog is read newest-first, so its oldest item is the one nobody looks at --
    but oldest is not most important, and printing only one of the two is misleading
    either way. Both are judgments about your repositories, your role and what
    "actionable" means for you: not something to hard-code here, and not something the
    mechanical action-required set in PullRequestsEvent can express. So the selection
    is delegated to a headless `claude`. Run from the checkout, it loads the same
    always-loaded prompt an interactive session here would, which is where those
    account- and repository-specific rules already live; PICKS_DOC supplies only the
    generic task, and the answer is one labelled URL per line.

    Titles are resolved from GitHub rather than taken from the answer, so the link text
    cannot drift from the item it points at.

    The selection blocks the (single-threaded) scheduler for up to PICKS_TIMEOUT. The
    last run is persisted, so the monitor's frequent self-supersede restarts do not
    re-run the model; re-arms every PICKS_INTERVAL.
    """

    priority = 8
    requires_user = True

    def __init__(self, scheduler: sched.scheduler) -> None:
        super().__init__(scheduler)
        self.last_run = (_load_json(PICKS_STATE_FILE, {}) or {}).get("last_run") or 0

    def fire(self) -> None:
        if self.deferred():
            return
        due = self.last_run + PICKS_INTERVAL - time.time()
        if due > 0:  # a restart mid-cycle waits out the remainder rather than re-running
            self.arm(due)
            return
        try:
            self._select()
        except Exception as exc:  # keep the loop alive across transient failures
            self._mark(0)
            print(f"monitor: backlog picks failed: {exc}", file=sys.stderr)
        self.arm(PICKS_INTERVAL if self.last_run else PICKS_RETRY)

    def _mark(self, when: float) -> None:
        """Record when the cycle was spent; 0 re-opens it for a retry."""
        self.last_run = when
        _save_json(PICKS_STATE_FILE, {"last_run": when})

    def _select(self) -> None:
        """Ask for the two picks and record them.

        An answer naming no item means nothing is waiting -- a real answer, and the
        cycle is spent on it. A run that fails outright is not: it re-opens the cycle
        for PICKS_RETRY, and says so in the file, because a silent failure here is
        indistinguishable from an empty backlog and claude.py gives the monitor no
        stderr.
        """
        # Marked before the slow call, so a crash mid-run cannot re-run it every restart.
        self._mark(time.time())
        out = _run_claude(PICKS_DOC.read_text(), PICKS_MODEL, PICKS_TIMEOUT, PICKS_TOOLS)
        if out is None:
            self._mark(0)
            _write_picks([f"{PICKS[0]} — selector did not answer; "
                          f"retrying in {PICKS_RETRY // 60} min"])
            return
        picks = _picks(out)
        _write_picks([f"{' + '.join(picks[url])} — {_item_link(url)}"
                      for url in sorted(picks, key=lambda u: PICKS.index(picks[u][0]))])


def main() -> int:
    _supersede_incumbent()
    PIDFILE.write_text(str(os.getpid()))
    try:
        scheduler = sched.scheduler(time.time, time.sleep)
        ScanMonitoringEvent(scheduler).arm(0)  # pending-monitoring -> pending-reads
        PullRequestsEvent(scheduler).arm(0)    # open PRs -> notify + per-PR console
        HistoryHintsEvent(scheduler).arm(0)    # agent history -> optimization hints
        PrStartEvent(scheduler).arm(0)         # a session starting on a PR -> clickable link
        GithubLinksEvent(scheduler).arm(0)     # links mentioned in a chat -> clickable links
        BacklogPicksEvent(scheduler).arm(0)    # longest-waiting + do-this-first -> clickable links
        # Recurring events re-arm themselves, so the queue never empties and run()
        # blocks forever -- until the process is killed.
        scheduler.run()
    finally:
        try:
            if PIDFILE.read_text().strip() == str(os.getpid()):
                PIDFILE.unlink()
        except (FileNotFoundError, ValueError):
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
