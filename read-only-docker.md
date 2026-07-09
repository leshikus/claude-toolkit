# Read-Only Mode (Docker)

If the current session is in read-only mode, do not create PRs, push commits, edit PR titles/bodies, or post comments/reviews/review-comment replies to GitHub directly.

Instead, create one atomic file per operation in the `~/.claude/pending-writes/` directory (named `<YYYY-MM-DD-HHMM>-<short-slug>.md`), following the queue format below. Each file has the exact command(s) to run and any payload text (PR body, comment/reply text) the command consumes. A separate write-capable agent reads each pending file, executes its commands, and deletes it on success.

Create new files only — never edit or delete existing files.

## Queue format

The `~/.claude/pending-writes/` directory is a hand-off queue between a **read-only agent** (which cannot perform write operations) and a **write-capable agent** (which executes them). Each pending write is **one atomic file**.

Create one file per operation, named `<YYYY-MM-DD-HHMM>-<short-slug>.md`:

    ### <YYYY-MM-DD HH:MM> — <short title>
    Context: <why this is needed; link to PR/issue/review comment if any>

    Commands:
    ```bash
    <exact command(s) to run, ready to copy-paste>
    ```

    Payload (if the command reads text from a file/stdin, put it here verbatim):
    ```
    <PR body, comment text, review reply, etc.>
    ```

Rules:

- Read-only agents: **create new files only**; never edit or delete existing files.
- Keep commands exact and self-contained (include `--repo`, full URLs, etc.) so the executing agent needs no extra context.
- Put any multi-line text a command consumes (PR body, comment) in the Payload block and have the command read it from there, so quoting is unambiguous.

## Executing pending writes

When acting as the write-capable agent executing pending writes, first give Alexei a short summary of the pending operations (what each file does) before running any commands. Then, for each task file:

- **On error-less completion:** delete the file — successful completion removes it from the queue.
- **On failure:** keep the file, append a `Status: failed <YYYY-MM-DD HH:MM>` line with the error, and do not delete it.

## Continuous monitoring

Continuously monitor the `~/.claude/pending-writes/` directory. When a new task file appears, start processing it right away following the rules above (summarize for Alexei, execute, delete on success). Do not wait to be asked — as soon as a new pending write shows up, pick it up and process it.
