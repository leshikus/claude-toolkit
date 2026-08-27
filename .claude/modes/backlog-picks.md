# Two picks from the backlog

Pick two items out of everything waiting on the user, and answer with their URLs:

- **oldest** — the one nothing has happened to for the longest.
- **highest** — the one worth doing first.
- **easiest** — the one closest to done.

They are often not the same item, and that contrast is the point: a backlog is read
newest-first, so the oldest thing in it is the one that never gets looked at, while the
one that actually matters today can be an hour old. When they *are* the same item, say
so on each line rather than inventing another item to fill one.

## What counts as waiting

An item waits when **the user is the one who has to move it** — a review requested of
them, a thread of theirs nobody answered, a change of theirs blocked on something they
control, an issue they filed that never got picked up. Something waiting on *someone
else* is not waiting on the user, however old it is.

The user's always-loaded instructions say which accounts, repositories and roles are
theirs, and may define what "actionable" means for them and how their reports select
and order items. Follow that. If a skill exists for assembling their backlog, invoke it
rather than inventing a query.

Read-only. Query, do not comment, label, close, or push anything.

## Choosing each one

**oldest** is measured from the item's last activity, not from when it was created.
An item somebody commented on last week is being looked at, however long ago it was
opened; the one to surface is the one nothing has happened to — no comment, no review,
no push, no label — for the longest. Creation date picks the same ancient issue every
cycle no matter who touched it yesterday, which is the opposite of neglect.

**easiest** is the one that needs the least to finish, and an approving review is the
strongest signal there is: an approved pull request waiting on a rebase, a changelog
entry, or a green re-run is minutes of work holding up something already agreed. Weigh
what is left rather than the size of the diff — a one-line change nobody has reviewed is
not close to done.

**highest** is a judgment, so make it on what the item costs and unblocks, not on how
loud it looks. What is holding up other work, what has someone actively waiting on a
reply, what is nearly finished and needs one push, what breaks or expires if it slips
again. A long queue behind an item beats age; age alone belongs on the other line.

## Answer

One line per label, each a label and a URL, nothing else:

```
oldest: https://github.com/<owner>/<repo>/issues/1234
highest: https://github.com/<owner>/<repo>/pull/5678
easiest: https://github.com/<owner>/<repo>/pull/9012
```

No prose, no titles, no explanation, no markdown links — titles are looked up
separately, and anything else in the output is discarded. Omit a line you cannot fill,
and output nothing at all if nothing is waiting on the user.
