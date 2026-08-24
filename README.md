# claude-toolkit

**Claude Code in Docker, in auto mode — writes flow, but a different-model reviewer gates every push and every write is logged for you to review.**

The agent works on your real repos with your real GitHub token and runs
autonomously (Claude Code's auto-mode classifier blocks the dangerous stuff —
force pushes, exfiltration, prod deploys). Two things keep you in control without
slowing it down: before any `git push` completes, a separate reviewer agent (a
different model) inspects the commits and blocks concrete defects so junk never
reaches the PR; and every remote write (push, PR, comment) is recorded to a global
log you walk afterward in a single review window.

## Use it

```bash
./claude.py            # working session (auto mode)
./claude.py --review   # one window over the writes log — review everything after the fact

python3 -m unittest discover tests   # unit tests
```

Run the working session and let it work. A push goes out the moment it passes the
pre-push review, so CI starts immediately — no approval queue in the hot path. When
you want oversight, open `--review`: it walks every logged write one at a time
(diff, PR, CI), reports, and lets you act. After a push lands, a host monitor
follows its CI to conclusion and drops the result back for the session to react to,
and it watches your open PRs for changes — no `/loop` babysitting. When a session
starts on a PR, the tab gets a Cmd-clickable link to it, and so does every PR the
monitor reports activity on. Anything a chat mentions — a PR, an Actions run, an
issue, a commit, a release — is posted there too, read from the session transcript
as it is said; only what the conversation says counts, never a tool call's output, so
the stream stays what you would have wanted to click. Each line is labelled with the
directory the session is working in (a container session resolves through to the host
checkout it was launched from), and the link text is the item's title read from
GitHub — a URL says only where a thing lives:

```
09:02:18  ~/repos/master-push — Push the release changelog and version bump to master
09:02:18  ~/repos/cr26.6 — CreateRelease
```

`term.py` renders these as OSC 8 terminal hyperlinks, falling back to the plain URL
wherever an escape would not be rendered, and owns every call to the terminal application itself — the tabs and
windows the monitor opens — behind one `Term` instance detected per host (iTerm2
today).

The monitor also watches how you and the agent are working together, and tells **you**
which Claude Code capability would make it go better. While a session is actively
coding — judged by its history still growing — the monitor distills the transcript
(tool-call counts, calls that repeated verbatim, what failed, turn durations, context
re-read) and asks a separate model what to change about the setup. Each cycle looks at
the single most recent session and prints at most one line into the monitoring tab, so
the stream is a slow drip you can read at a glance:

```
09:44:05  hint cr26.6 — Try `/investigate-ci` and `/patch-release-check` next time: this session manually rebuilt both workflows via dozens of ad-hoc `gh` calls.
09:44:56  hint no-manual-appr — Try the continue-pr skill to resume PR #67558 across sessions instead of manually git fetch+checkout of the branch.
```

The last few hints are replayed to the model each cycle so it never rephrases advice
you have already been given.

The waste in the transcript is only the symptom; the hint is the mechanism that
removes it — a skill you already have but aren't reaching for, a hook, a subagent, a
permission rule, a different opening prompt. It is deliberately biased against telling
you to add prompt text: a `CLAUDE.md` rule is paid in tokens on every future session,
so it has to beat a mechanism that costs nothing until it is used. The hinter is handed
an inventory of what you already have configured, and saying nothing is the expected
answer.

A backlog is read newest-first, so what nobody looks at is the oldest thing in it — and
that is rarely what matters most. Every 15 minutes the monitor posts both, as links over
their titles:

```
06:00:12  oldest — Make a status for failing ccache
06:00:12  highest — Rework AutoReleases into a praktika workflow
```

What counts as waiting on *you* — a review requested of you, a thread nobody answered, an
issue you filed that never got picked up — and which of them to do first both depend on
your accounts, repositories and priorities, so neither is hard-coded. A headless `claude`
makes the picks, run from this checkout so it loads the same always-loaded prompt an
interactive session here would; those rules live there, and `.claude/modes/backlog-picks.md`
supplies only the generic task. It answers with one labelled URL per line, and the titles
are read back from GitHub so the link text cannot drift from what it points at. One item
that is both prints once, as `oldest + highest`.

The notification stream is a host tab, so a session inside the container cannot see
it. A `UserPromptSubmit` hook replays its tail into the session before the model
starts on the turn — at most once every 5 minutes, and only when a new line has
landed since the last replay.

## Nothing prints into a tab you are not reading

Every job whose only product is a line for you to read — the PR watch, the hints, the
chat links, the backlog picks — is gated on you being at the keyboard, measured by macOS
`HIDIdleTime`. Away or asleep, those events **defer** rather than fire: nothing polls
GitHub, nothing spends a model call, and nothing scrolls past unread. The work is still
waiting when you come back, so a CI failure that landed overnight is announced when you
return instead of having been printed to an empty room. If the idle probe cannot be read
the monitor assumes you are here — a broken probe must not look like a quiet backlog. The
jobs that serve a working agent rather than a reader (the CI watch feeding results back
into a session) are never gated.

## How the safety works

- **Auto mode** (`--permission-mode auto`): no routine prompts, but a classifier
  blocks irreversible / destructive / external actions before they run.
- **Pre-push gate**: a `PreToolUse` hook runs a separate reviewer agent (a different
  model) over the exact commits about to be pushed; a concrete defect blocks the
  push and the findings go back to the working agent to fix. It fails open, so it
  never wedges you.
- **Write capture + review**: a `PostToolUse` hook logs every remote write to
  `~/.config/claude-toolkit/writes-log/`; the `--review` session is your
  after-the-fact audit over all of it.

Commits are GPG-signed with your key (via a private keyring copy); the container
never touches your host keyring.

Details: [`working-mode.md`](.claude/modes/working-mode.md),
[`pre-push-review.md`](.claude/modes/pre-push-review.md),
[`review-mode.md`](.claude/modes/review-mode.md),
[`history-hints.md`](.claude/modes/history-hints.md).

## Needs

macOS + Docker Desktop, iTerm2, `gh`/`gpg`, and a Claude account with auto mode
available (Opus/Sonnet 4.6+ on the Anthropic API).
