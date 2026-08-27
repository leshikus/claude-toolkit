# Two picks from the backlog

Pick two items out of everything waiting on the user, and answer with their URLs:

- **oldest** — the one nothing has happened to for the longest.
- **newest** — the one something has just happened to.
- **highest** — the one worth doing first.
- **easiest** — the one whose action takes the least work.

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

**easiest** is the smallest *action*, not the item nearest to merged. Whatever the user
has to do on it — approve a three-line change, answer one question, rebase, add a
changelog entry, re-run a job — pick the one that is least work. It does not have to be
their own pull request: reviewing a tiny diff somebody else wrote is often the easiest
thing in the whole set.

Weigh the action, not the item. A one-line change of their own that nobody has reviewed
needs a reviewer, so it is not easy; a two-hundred-line diff they only have to approve
may be.

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
easiest: https://github.com/<owner>/<repo>/pull/9012 — approved; needs a rebase and a changelog entry
```

Keep the action to a clause. Titles are looked up separately, so do not repeat one.

No prose beyond that clause, no titles, no markdown links — anything else in the output
is discarded. Omit a line you cannot fill,
and output nothing at all if nothing is waiting on the user.
