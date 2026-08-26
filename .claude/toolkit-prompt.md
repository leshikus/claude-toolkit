# You are running in the claude-toolkit container

Facts about this environment that nothing else will tell you. Everything generic —
communication, commits, code style — comes from the user's own instructions.

## Where you are

The checkout is mounted at `/home/ubuntu/project`, whatever it is called on the host.
Its real path is `host_dir` in `~/.config/claude-toolkit/project/meta.json`; use that
when you name a directory to the user, since the container path means nothing to them.

You are the only session in this project. A relaunch elsewhere supersedes you rather
than running alongside.

## What happens when you push

Auto mode is on: routine writes execute without asking, and a classifier blocks
irreversible or external actions before they run.

A `git push` is gated. A `PreToolUse` hook runs a reviewer agent on a different model
over the exact commits being pushed, and a concrete defect blocks the push and comes
back to you to fix. It fails open, so it never wedges you.

Every remote write — push, PR, comment — is appended to
`~/.config/claude-toolkit/writes-log/` for the user to audit afterwards. Assume what
you do to GitHub is read later.

## Results arrive in a queue, not in chat

A successful push arms a host monitor that follows the run to conclusion and writes the
verdict into `~/.config/claude-toolkit/project/pending-reads/`. Read that directory:
CI outcomes and PR changes land there, and nobody will paste them to you.

Notifications the monitor produces — CI, PR activity, backlog picks — are replayed into
the start of a turn by a hook. They are already in your context; do not go looking for
`notifications.log`.

## What you may write

`~/.config/claude-toolkit/modes/*.md` is your own role doc, mounted read-write. If its
guidance is wrong or did not prevent a mistake you just made, edit it.

`~/.config/claude-toolkit/config/` is the only other toolkit directory you may write —
runtime knobs, e.g. `notify-interval`.

Everything else under `~/.config/claude-toolkit/` is read-only by design. `hooks/`
included: edit those in the host checkout, not here.

## Do not break the object store

This checkout may hold no objects of its own, borrowing them through
`objects/info/alternates` from a read-only mirror mounted at a host path. So: never run
`git gc` or `git prune`, and if git reports `unable to normalize alternate object path`,
the mirror is missing from the mount — say so rather than recloning.
