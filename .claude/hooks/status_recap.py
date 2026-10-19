#!/usr/bin/env python3
"""Stop hook: make every working turn end with a status block the reader can act on.

Claude Code's own recaps are dim, sometimes absent and blind to background work. This
hook checks the last assistant message for a `**Status**` block; if a turn that used
tools ends without one, it blocks the stop and hands the model the monitor state it
cannot see -- `pending-reads/` items that arrived during the turn and open
`pending-monitoring/` watches -- so the block it writes covers background work too.

A turn without tool calls is a plain answer and passes. `stop_hook_active` means the
model is already answering this hook, so it passes too, which bounds the cost at one
extra turn. Fails open on anything unexpected: a broken recap must not wedge a session.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(os.path.expanduser("~/.config/claude-toolkit/project"))
READS_DIR = PROJECT / "pending-reads"
WATCH_DIR = PROJECT / "pending-monitoring"
MARKER = "**Status**"
FORMAT = """\
**Status** — waiting on: **you** | CI | a reviewer | nothing
- Done: what this turn achieved, with PR and run URLs.
- Background: every background task, CI run and monitor watch, running or finished, with its result.
- **Next:** the next step, and who takes it."""


def last_turn(transcript: Path) -> list:
    """The transcript records after the last real user prompt."""
    records = []
    for line in transcript.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        content = (rec.get("message") or {}).get("content")
        if rec.get("type") == "user" and (isinstance(content, str) or any(
                isinstance(c, dict) and c.get("type") == "text" for c in content or [])):
            records = []
        records.append(rec)
    return records


def blocks(records: list, kind: str) -> list:
    """Every content block of type `kind` in the assistant records."""
    return [c for r in records if r.get("type") == "assistant"
            for c in (r.get("message") or {}).get("content") or []
            if isinstance(c, dict) and c.get("type") == kind]


def since(directory: Path, start: float) -> list:
    """Names of the files in `directory` modified at or after `start`."""
    try:
        return sorted(p.name for p in directory.iterdir() if p.stat().st_mtime >= start)
    except OSError:
        return []


def main() -> int:
    event = json.load(sys.stdin)
    if event.get("stop_hook_active"):
        return 0
    records = last_turn(Path(event["transcript_path"]))
    texts = blocks(records, "text")
    if not blocks(records, "tool_use") or (texts and MARKER in texts[-1].get("text", "")):
        return 0

    stamps = [r["timestamp"] for r in records if r.get("timestamp")]
    start = 0.0
    if stamps:
        start = datetime.fromisoformat(stamps[0].replace("Z", "+00:00")).timestamp()
    facts = []
    if reads := since(READS_DIR, start):
        facts.append(f"- new in pending-reads/: {', '.join(reads)}")
    if watches := since(WATCH_DIR, 0):
        facts.append(f"- open monitor watches: {', '.join(watches)}")
    reason = ("End this turn with a status block for a reader returning after a pause. "
              "Write only the block, in this form:\n\n" + FORMAT)
    if facts:
        reason += "\n\nMonitor state to cover (read the pending-reads files first):\n" + "\n".join(facts)
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"status_recap: {exc}", file=sys.stderr)
        sys.exit(0)
