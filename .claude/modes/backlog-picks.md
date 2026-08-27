# Two picks from the backlog

Pick two items out of everything waiting on the user, and answer with their URLs:

- **oldest** — the one nothing has happened to for the longest.
- **newest** — the one something has just happened to.
- **highest** — the one worth doing first.
- **approved** — a pull request of theirs that already has an approving review.

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

**newest** is the mirror of oldest: most recent activity rather than least. It has to
still be waiting on the user, which is the trap — an item usually moves *because* someone
else answered, and then the ball is with them and it does not belong in this set at all.
Pick it only when the thing that just happened is what hands it back: a review posted, a
question asked of them, CI going red on their branch.

**approved** is a fact, not a judgment: a pull request the user authored that carries an
approving review. Agreement has already been given, so only mechanics stand between it
and merged — a rebase, a changelog entry, a red job to re-run, or nothing at all. Say
which in the action clause, because that is the whole of what is left.

**Omit this line when there is no such pull request.** It is the one label with an
objective test, so there is never a reason to stretch it: no approval, no line. If
several are approved, take the one whose remaining mechanics are smallest.

**highest** is a judgment, so make it on what the item costs and unblocks, not on how
loud it looks. What is holding up other work, what has someone actively waiting on a
reply, what is nearly finished and needs one push, what breaks or expires if it slips
again. A long queue behind an item beats age; age alone belongs on the other line.

## Answer

One line per label: the label, the URL, then what the user has to do about it. Every
item here is one only they can move, so a line naming the item and not the action has
left out the point of picking it.

```
oldest: https://github.com/<owner>/<repo>/issues/1234 — nobody picked it up; close it or find an owner
newest: https://github.com/<owner>/<repo>/pull/3456 — a reviewer asked a question 20 minutes ago
highest: https://github.com/<owner>/<repo>/pull/5678 — two PRs are blocked behind it
approved: https://github.com/<owner>/<repo>/pull/9012 — rebase and it can merge
```

Keep the action to a clause. Titles are looked up separately, so do not repeat one.

No prose beyond that clause, no titles, no markdown links — anything else in the output
is discarded. Omit a line you cannot fill,
and output nothing at all if nothing is waiting on the user.
