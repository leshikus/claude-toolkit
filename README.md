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
./claude.py            # working session (auto mode) in the current directory
./claude.py <pr-url>   # session on that pull request, checked out for you
./claude.py --review   # one window over the writes log — review everything after the fact

python3 -m unittest discover tests   # unit tests
```

Given a pull request URL, the launcher does the setup you would otherwise type. If a
project is already tracking that PR — `~/repos/chp-1` for `clickhouse-private#69819`,
recorded by the session-start hook — that checkout is used untouched: a PR is one
project however you reach it, and a second clone would split its queues, its
notification stamp and its session in two. Otherwise it syncs the repo if it is a fork,
clones it under `projects/<repo>-<N>/repo` — the same place the monitor puts a per-PR
console — checks the PR out, and opens the session there. A checkout that is already present is fetched
rather than recloned, and a fresh one borrows its objects from a shared bare mirror
under `git-store/` rather than transferring them: a ClickHouse `.git` is 7.3 GB and
there are dozens of these checkouts. The monitor keeps those mirrors fetched on a
schedule, so a launch never waits for a first clone, and both callers take a lock —
git will not corrupt a mirror under concurrent fetches, but it fails the loser, and a
failed refresh costs the caller the 7.3 GB it could have borrowed. The store is mounted
into the container at the identical absolute path, because `objects/info/alternates`
records it absolutely and git resolves it literally. Only the clone is
fatal: a failed sync, fetch or checkout still leaves something to work in, so the
session opens and is told what went wrong. The checkout is forced to match the PR:
it belongs to the toolkit, so tracking the remote beats preserving whatever the last
session left on the branch.

An issue URL clones nothing. Establishing whether it still reproduces needs the issue,
not a checkout, and an issue old enough to be the oldest thing in a backlog is often
fixed already — a ClickHouse-sized clone is a lot to pay for that verdict. The session
opens in an empty directory of its own, on a goal met by showing whether it still
reproduces, either way, with the evidence. The pull request comes
after the verdict, and `./claude.py <pr-url>` then names its directory for the PR.

Relaunching a project takes it over. A container is named for its project, so the
launch stops the one already running and resumes its session here, in the terminal you
typed in — `--resume` alone would not have: it starts a second agent on the same
transcript and leaves both working. Per-project state assumes one session, the same way
the monitor allows one instance. The directory is named `<repo>-<number>`, because a
PR number is unique only inside its repo and `ClickHouse` and `clickhouse-private`
overlap constantly. What it opens *on* is a `/goal`, not an
instruction — a goal is re-checked before the session stops, which is what a launch
wants from an agent that has a pull request to get somewhere. Whose PR it is decides
the condition, from the token's own login: your own is finished when CI is green and
every review comment is triaged; anyone else's when the review is drafted.

Each condition is met by *preparing* what needs you, never by doing it. Answering a
review comment needs your agreement first, so a goal demanding answered comments would
drive the session straight through that gate; it is reached when the patch and the
proposed action are waiting for your decision.

Run the working session and let it work. A push goes out the moment it passes the
pre-push review, so CI starts immediately — no approval queue in the hot path. When
you want oversight, open `--review`: it walks every logged write one at a time
(diff, PR, CI), reports, and lets you act. After a push lands, a host monitor
follows its CI to conclusion and drops the result back for the session to react to,
and it watches your open PRs for changes — no `/loop` babysitting. When a session
starts on a PR, the notification stream gets a Cmd-clickable link to it, and so does
every PR the monitor reports activity on. Anything a chat mentions — a PR, an Actions run, an
issue, a commit, a release — is posted there too, read from the session transcript
as it is said; only what the conversation says counts, never a tool call's output, so
the stream stays what you would have wanted to click. Each line is labelled with the
directory the session is working in (a container session resolves through to the host
checkout it was launched from), and the link text is the item's title read from
GitHub — a URL says only where a thing lives:

```
monitor — most recent last
  09:02  ~/repos/master-push — Push the release changelog and version bump to master
      https://github.com/ClickHouse/ClickHouse/pull/113528
  09:02  ~/repos/cr26.6 — CreateRelease
      https://github.com/ClickHouse/ClickHouse/actions/runs/32462474552
```

The URL is written out rather than hidden behind an OSC 8 escape, and it keeps a row
of its own. The console destroys the escape, so bare text is the only link there is,
and a terminal linkifies only a URL it can see whole — a hard wrap splits it into two
things that are neither. `term.py` owns both renderings, and
every call to the terminal application itself — the per-PR consoles the monitor
opens — behind one `Term` instance detected per host (iTerm2 today).

The monitor also watches how you and the agent are working together, and tells **you**
which Claude Code capability would make it go better. While a session is actively
coding — judged by its history still growing — the monitor distills the transcript
(tool-call counts, calls that repeated verbatim, what failed, turn durations, context
re-read) and asks a separate model what to change about the setup. Each cycle looks at
the single most recent session and writes at most one short tutorial on the capability
that would have helped, into that project's `hint.md`:

```
try this
  ## /goal — keep working until a condition holds
  Claude re-checks the condition before it stops, so a long task does not end halfway.
  Try: /goal PR 115607 has green CI and every review comment triaged
  Here: the session stopped three times mid-rebase and you re-prompted it each time.
```

It reads as a tutorial because the reason for the waste is usually that you did not
know the capability existed. `Try:` has to be usable as written; `Here:` names the thing
in this session it would have fixed, which is what separates a hint from documentation.

It goes to the project rather than into the notification stream: it is several lines of
prose about the session in front of you, and mixing that into a list of one-line events
buries both. The topics already taught are replayed to the model each cycle, so it never
teaches the same capability twice.

The waste in the transcript is only the symptom; the hint is the mechanism that
removes it — a skill you already have but aren't reaching for, a hook, a subagent, a
permission rule, a different opening prompt. It is deliberately biased against telling
you to add prompt text: a `CLAUDE.md` rule is paid in tokens on every future session,
so it has to beat a mechanism that costs nothing until it is used. The hinter is handed
an inventory of what you already have configured, and saying nothing is the expected
answer.

A backlog is read newest-first, so what nobody looks at is the least recently touched
thing in it — and that is rarely what matters most, nor what an hour of work would
finish, nor what someone replied to ten minutes ago. Every 15 minutes the monitor picks
all four: `oldest`, `newest` — its mirror, most recent activity rather than least —
`highest`, and `easiest` — the smallest action, which is often reviewing a tiny diff
somebody else wrote rather than finishing anything of your own. Each line carries the action rather than only the title: every item
is one that only you can move, so naming it without saying what it needs leaves out the
point of picking it.

They are state, not history: the picks overwrite `backlog-picks.txt` rather than adding
three more lines to the stream every cycle, and are reprinted whole each time the hook
speaks.

```
backlog
  oldest — <issue title>: nobody picked it up in three years
      https://github.com/<owner>/<repo>/issues/1234
  newest — <pr title>: a reviewer asked a question 20 minutes ago
      https://github.com/<owner>/<repo>/pull/3456
  highest — <pr title>: two pull requests are blocked behind it
      https://github.com/<owner>/<repo>/pull/5678
  easiest — <pr title>: approve it, three lines and CI is green
      https://github.com/<owner>/<repo>/pull/9012
```

What counts as waiting on *you* — a review requested of you, a thread nobody answered, an
issue you filed that never got picked up — and which of them to do first both depend on
your accounts, repositories and priorities, so neither is hard-coded. A headless `claude`
makes the picks, run from this checkout so it loads the same always-loaded prompt an
interactive session here would; those rules live there, and `.claude/modes/backlog-picks.md`
supplies only the generic task. It answers with one labelled URL per line, and the titles
are read back from GitHub so the link text cannot drift from what it points at. One item
that is both prints once, as `oldest + highest`.

The stream is a log, not a window: nothing tails it and no tab is opened for it. A
`UserPromptSubmit` hook replays its tail into the session before the model starts on
the turn — at most once every 5 minutes, and only when a new line has landed since
the last replay. Silence and breakage look identical from the console, so the wait is
readable and writable while the session runs:

```bash
echo 0  > ~/.config/claude-toolkit/config/notify-interval   # every prompt, gates off
echo 60 > ~/.config/claude-toolkit/config/notify-interval   # once a minute
rm        ~/.config/claude-toolkit/config/notify-interval    # back to 5 minutes
```

`config/` is the one toolkit mount a container may write, so that line works from
inside the session. At `0` a prompt always answers, which makes silence mean the hook
is not running rather than that nothing moved. That is the whole delivery, so a notification is read where you are
already working rather than in a tab you would have to remember to look at.

## Nothing is reported to somebody who is not there

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
