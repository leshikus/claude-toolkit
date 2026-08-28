# Developing claude-toolkit

Rules for working on this repository. They are not instructions for the agents it
launches — those get `.claude/toolkit-prompt.md` as an appended system prompt and a
pointer to `.claude/modes/<mode>.md`.

## Tests

`python3 -m unittest discover tests` before every commit.

No test may reach the network. Mocking the obvious entry point is not enough: with
`run_step` stubbed, `stage_pr` still called the real `gitstore.refresh` and started a
`gh repo clone --mirror` of ClickHouse. Stub every module a code path touches.

## Hooks and modes are live

`.claude/hooks` is mounted read-only and each script is re-read per invocation, so a
hook edit takes effect on the next prompt in every running container. Nothing needs
relaunching, and nothing protects a container from a half-saved hook.

`.claude/modes` is mounted read-write on purpose: an agent may edit its own role doc,
and the change lands in this checkout.

## Module boundaries

`claude.py` launches `monitor.py` as a subprocess and must not import it — importing
would pull in `term`, the scheduler and the event loop for the sake of one function.
Code both need goes in a module of its own; `term.py` and `gitstore.py` are that.

## Per-container state

Anything mounted read-write into more than one container is a race. `~/.claude.json`
and the OAuth credential each cost an outage that way, the second time after being
"fixed" by moving one shared copy inside, and the GPG keyring made every container's
`keyboxd` contend for one lock and one socket. Stage per project, under
`projects/<name>/`. Three instances of the same bug: assume the next shared file is one
too.

## The shared git store

Mount it at the identical absolute path it has on the host: `objects/info/alternates`
records absolute paths and git resolves them literally, so a checkout whose store is
mounted elsewhere fails every command with `unable to normalize alternate object path`.

`git worktree` cannot cross that boundary at all — it records the path back to itself in
the parent repo, which the container sees somewhere else. Alternates are one-way, which
is why they work.

`gc.auto=0` on every mirror. A borrowing checkout keeps no copy of what it reads, so
anything pruned there is lost to it.

## Committing here

Do not `git add -A`. Parallel sessions edit this checkout, and it silently swept an
unrelated 84-line `.claude/settings.json` change into a commit about session resume.
Stage the paths you touched.

Author is `Alexei Fedotov <alexei.fedotov@gmail.com>`: this is not a ClickHouse project.

This and `leshikus/claude` are leshikus's own toolbox: commit changes directly to
`main`, skipping pull requests and feature branches. The generic "new branch per task,
never commit to the default branch" rule does not apply to these repos.

## Settled, do not retry

A hook's `systemMessage` strips the OSC 8 escape and does not render markdown, so a
notification shows its URL because the URL *is* the link. Both alternatives were tested
against the running console.

Claude Code's own sandbox does not replace the container. Its egress-only mode grants
"unrestricted read/write access to the host filesystem", and its filesystem mode is a
deny-list over a live `$HOME` — weaker than an empty root with explicit mounts, and the
only mode that leaves the network open is the one that gives up on the filesystem.
