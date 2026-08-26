#!/usr/bin/env python3
"""The shared git object store: one bare mirror per repo, borrowed by every checkout.

A ClickHouse `.git` is 7.3 GB and there are dozens of per-PR checkouts, so a checkout
is cloned with `--reference` against a mirror here and keeps almost no object database
of its own.

Two callers, which is why this is its own module: `monitor.py` fetches every mirror on
a schedule, so a launch never waits for a first clone, and `claude.py` makes one on
demand when the monitor has not got there yet.

The store has to be mounted into the container at the same absolute path it has on the
host. `objects/info/alternates` holds absolute paths and git resolves them literally --
unmounted, every command in the checkout fails with "unable to normalize alternate
object path". That is also why a borrowing checkout uses alternates rather than
`git worktree`: a worktree records the path back to itself in the parent repo, and the
container sees the checkout somewhere else.
"""

import fcntl
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

STORE = Path.home() / ".config" / "claude-toolkit" / "git-store"


def mirror(repo: str) -> Path:
    """Path of `repo`'s bare mirror, whether or not it exists yet."""
    return STORE / f"{repo.replace('/', '-')}.git"


@contextmanager
def _holding(repo: str):
    """Yield True while holding this mirror's lock, or False if someone else has it.

    git does not need protecting from itself: concurrent fetches take ref locks and
    objects are written to a temporary name and renamed, so a race cannot corrupt the
    mirror -- it fails the loser. That is the problem, because a failed refresh sends
    the caller off to clone 7.3 GB it could have borrowed. So the loser skips: the
    mirror is only ever added to, and one that a fetch is midway through is still
    perfectly good to borrow from.
    """
    STORE.mkdir(parents=True, exist_ok=True)
    with (STORE / f"{repo.replace('/', '-')}.lock").open("w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def refresh(repo: str) -> str:
    """Bring `repo`'s mirror up to date, creating it if absent.

    Returns "created", "fetched", "busy" when another process is already on it, or ""
    when there is no usable mirror. Only `reference` decides whether one can be
    borrowed, so a skipped refresh costs freshness and nothing else.
    """
    path = mirror(repo)
    with _holding(repo) as held:
        if not held:
            return "busy"
        if (path / "objects").is_dir():
            return "fetched" if _run(
                ["git", "-C", str(path), "fetch", "--prune", "--quiet"]) else ""
        if not _run(["gh", "repo", "clone", repo, str(path), "--", "--mirror", "--quiet"]):
            return ""
        # A borrower keeps no objects of its own, so anything pruned here is lost to it.
        _run(["git", "-C", str(path), "config", "gc.auto", "0"])
        return "created"


def reference(repo: str) -> list:
    """`git clone` arguments borrowing `repo`'s objects, or [] if there is no mirror."""
    path = mirror(repo)
    return ["--reference", str(path)] if (path / "objects").is_dir() else []


def _run(argv: list) -> bool:
    """Run `argv`, returning whether it succeeded and reporting what it said if not."""
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode:
        print(f"git-store: {' '.join(argv)}: {r.stderr.strip()[:200]}", file=sys.stderr)
    return r.returncode == 0
