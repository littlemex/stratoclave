# Contract: asking for more of a money ceiling, and getting it back

A tenant refused at its dollar pool has, until now, had two options: wait for the
month to end, or find an operator and have them type a bigger number. The second one
is not reversible by anything except somebody remembering, so the number typed under
pressure on a Tuesday is still the ceiling in March. This document is the third
option: a raise that is **asked for, decided by a person, and ends by itself**.

Read [limits.md](limits.md) section 4 first. This document adds one term to the rule
stated there and changes nothing else about it.

## 1. What a grant is

```
seat_term  = seat_count x seat_rate
baseline   = manual_limit  if manual_limit is PRESENT  else seat_term
pool_limit = baseline + coalesce(pool_granted, 0)
```

A **grant** is an amount on `pool_granted_microusd` with an expiry. It raises
`pool_limit` and `pool_headroom` by exactly that amount when it is applied, and
lowers both by exactly that amount when it ends. It never touches the baseline, and
that is the whole reason the two writes are safe to make without a compare-and-swap.

**A figure describing spend as "grant-supported" is an upper bound on what the
grant covered, not an attribution of which dollar came from it.** The pool's
headroom counter is one fungible number, not a set of tagged sub-balances; a
settle draws down `pool_headroom_microusd` regardless of whether the amount it
consumes traces to the seat term, an operator's figure, or a live grant. So a
report saying "up to $G of this period's spend was covered by the grant" is
sound — the grant genuinely raised the ceiling by `G` and the tenant could not
have spent past its baseline without it — but a report attributing a
*specific* charge to the grant rather than to the baseline is stating something
this row does not track and cannot answer.

**Why the grant writes carry no CAS while every other writer of this ceiling does.**
The other writers move the *baseline*, and they compute their delta from values that
can move under them — a seat count, a prior figure — so each one has to check those
values are still what it read. A grant is `+G` on top of whichever baseline is in
force at the instant it commits. There is nothing for a CAS to protect, so an
approval composes with a concurrent hire, a concurrent operator set and a concurrent
reserve rather than racing all three. Making it a CAS anyway would not be extra
safety; it would be an approval that fails whenever a tenant hires somebody.

## 2. The lifecycle, and which states hold capacity

A **request** is `PENDING`, then exactly one of `APPROVED`, `REJECTED`, `WITHDRAWN`.
A **grant** exists only for an `APPROVED` request, and is `ACTIVE`, then one of
`EXPIRED` (the sweep), `REVOKED` (a person, early), or `REVOKE_BLOCKED`.

Two of those four grant states **bear capacity**, meaning the pool row is currently
counting their amount: `ACTIVE`, obviously, and `REVOKE_BLOCKED`. The second one is
the case worth stating. A blocked grant is one whose subtraction could not be made
to commit, so the capacity was never actually given back and the row is right to
still be counting it. A reconciler that treated it as returned would report every row
holding a blocked grant as broken, continuously, for exactly as long as the fault
lasted — which is when an operator most needs it quiet about everything else.

That predicate has **one definition**, `mvp.grants.is_capacity_bearing`. The
reconciler and every inventory call it. Three independent statements of a lifecycle
rule drift, and this one is only ever wrong in a direction that either hides granted
money or invents an alarm.

## 3. The expiry is the mechanism, so the sweep is the mechanism

A grant's window is only real because something enforces it. Nothing else in the
system can: the request path reads a ceiling, it does not audit how the ceiling got
there, so a grant nobody revokes is a permanent raise recorded as a temporary one.

The sweep runs every five minutes, queries the grants past their expiry, and for each
one commits a single transaction that takes the grant terminal and subtracts its
amount. Three properties make that trustworthy, and each one is load-bearing:

- **Exactly-once rests on one condition, not on the sweeper being careful.** The
  grant's own update is conditional on `status = ACTIVE AND approved_amount_microusd =
  <the amount that was read>`. Two overlapping sweeps racing one grant: one commits,
  the other's transaction cancels, and the loser treats that as success because it
  is — the capacity was returned once. The second half of the condition is what stops
  a stale amount being subtracted for a grant that changed between the read and the
  write.
- **A revoked grant leaves the sweeper's index by construction.** The expiry index is
  partitioned on an attribute written only while a grant is `ACTIVE` and removed in
  the *same transaction* as every terminal transition. So the index holds exactly the
  work outstanding, rather than every grant that ever existed behind a filter — and
  a filter is what `Limit` is applied before, which is how a bounded sweep ends up
  never reaching the work sitting behind a full page of finished rows.
- **The heartbeat is emitted after pagination completes.** A sweep that dies on page
  two has left grants unrevoked. If it had already claimed to have run, the absence
  alarm — the only thing that can notice a sweeper that stopped — would have been
  satisfied by a run that did not finish. The heartbeat also fires on **empty** runs,
  because a signal that only appears when there was work cannot tell "nothing
  expired" from "nobody is looking".

**A grant that cannot be revoked becomes `REVOKE_BLOCKED` and is alarmed, not retried
forever.** After a bounded number of attempts it is marked, leaves the index so one
poison grant cannot consume every run from then on, and keeps counting its amount —
honestly. The pool is deliberately untouched: the capacity was never returned, and
writing it back would be the system asserting something it does not know. The grant
row carries both a flag and the reason the transaction gave, because the metric can
only say that something is stuck and an operator needs to know which. The repair is
a revoke through the ordinary endpoint, which clears the block and retries the same
transaction.

## 4. `expires_at <= period end`, and why F1 depends on it

An approval refuses any expiry later than the end of the billing period the grant is
granted in. That looks like a policy choice and is not: the period rollover **resets
`pool_granted_microusd` by omission**, so if a grant could outlive its period, the
new month's row would arrive without its capacity while the grant was still live and
believed itself to be holding it. The reset destroys the money instead of releasing
it, on every granted row at once, on the 1st, silently.

Both halves say so where they are written. If this pin is ever loosened,
`dynamo/pool_row_schema.py`'s classification of `pool_granted_microusd` has to change
with it, and the classification's note names this document.

An approval also refuses an expiry less than five minutes out, and refuses **every**
expiry when fewer than five minutes remain in the period — the window is then
unsatisfiable, and saying so is better than accepting a grant that expires before it
can admit a request while still consuming the tenant's cap headroom.

## 5. The aggregate cap, and the trade it makes

`grant_cap_microusd` bounds what approvers may grant **in total** for a period, not
per grant. Without it, thirty people at seven-day windows is two hundred and ten
concurrent legal grants.

**An absent cap means "derived from the baseline, evaluated now".** It is not zero and
not unlimited. This is the same sentinel shape the ceiling already uses for
`manual_limit_microusd`, and the argument for it is not economy of work:

> A materialised default **freezes at the moment it is written**. A tenant that later
> hires keeps a cap sized to the baseline it had then, refusing legitimate approvals,
> with nothing anywhere saying why.

So nothing seeds it and nothing backfills it. An explicitly-set cap is carried across
a period boundary; an absent cap carries as absence, naturally.

**The cost, stated because it is real.** A DynamoDB condition on a *missing* attribute
fails, so the apply guard cannot be a row-side condition on the cap. The cap is
resolved caller-side from a read, and the transaction's condition compares the row's
**live** granted sum against that figure — which still catches a concurrent approval,
because the value the condition sees is not the value that was read. What it does not
catch is a concurrent **baseline** change: a hire or an operator set landing between
the read and the commit moves the derived cap, and this approval was checked against
the old one. The daily `grant_cap_not_exceeded` check is what notices, a day late,
which is the same lateness the rest of that reconciler already accepts.

There is a second thing about that guard worth writing down, because it is the easy
one to get wrong. `pool_granted_microusd` is absent on any row that has not been
granted to yet, and a comparison against a missing attribute fails — so a guard
written as `pool_granted_microusd <= :remaining` **alone** refuses the first grant of
every period on every tenant, and reports it as the cap being exceeded when nothing
has been granted at all. The condition therefore admits the attribute's absence
explicitly.

## 6. One raise per person per day, and the token that makes a retry safe

A person may hold one undecided raise per tenant per UTC day. The mechanism is a
single **slot row** keyed by user, tenant and date, which is also the anchor for the
caller's idempotency token — one row doing both jobs rather than two that can
disagree. A second submission with the *same* token returns the request the first one
created; a *different* token while the slot is still held is refused, naming the
request holding it and when the day resets, with the zone spelled out. A reset time
read in the reader's own timezone is wrong for most of the world by up to a day, and
for a once-a-day allowance that is the whole allowance.

**Slot rows are declared harmless rather than swept, and this sentence is the
declaration.** They are keyed by user with no expiry, they hold a token and a request
id and nothing else, and nothing reads a slot for a day that has passed. The
orphan hunt starts from grants and would never see them, and the user-deletion path
archives memberships without touching them — so a deleted user's old slots stay. They
are dead rows of fixed size, at most one per user per tenant per day they filed on,
and the cost of leaving them is storage. If that ever stops being true, the fix is a
sweep on the date component of the sort key, not a change to the mechanism above.

**A decided request frees the day.** Withdrawn and rejected free it; so does an
approval whose grant has stopped bearing capacity. `PENDING` does not, and
`REVOKE_BLOCKED` does not — that grant is still holding its share of the ceiling, and
treating it as finished would let a second raise stack on capacity nobody has given
back. The freeing is **lazy**: the next submission reads the slot and establishes the
fate, rather than a sweeper visiting every slot of every user every day to find the
few that were decided.

## 7. Authority is checked inside the transaction, not at the door

A route dependency proves the caller held the permission when the request *arrived*.
The approval also carries a `ConditionCheck` on the tenant row **inside the same
transaction as the money**, which proves they still hold the authority at the instant
the grant commits. A permission revoked mid-flight cancels the whole transaction, so
there is no window in which capacity is granted on authority somebody has already
lost.

**The tenant it binds is the one read from the request or grant row**, never a value
the caller supplied. This is the security-critical part rather than the defensive
part: the approval permission is deployment-global at the write path, so without the
binding a permission-holder who owns tenant A could approve a raise for tenant B and
nothing on the write path would have anything to say about it.

The team-lead form of that check is ownership (`team_lead_user_id = <the actor>`); the
global form confirms the tenant still exists, which is not a no-op — a tenant deleted
between the read and the commit would otherwise take a grant pinned to a row nobody
will reconcile. **The route decides which form applies, not the actor's roles.**
Sniffing roles would let somebody who happens to hold the global permission reach the
ownership route and have the weaker check applied, so the route that claims to
enforce ownership would not be enforcing it.

Nobody may decide their own request. It matters most for the tenant owner, who holds
the approval authority for their own tenant and would otherwise be the one path in
this feature that never expires.

## 8. What a refusal now tells a client

Every `402` names the wall that refused, whether that wall can be raised, and — for
the one that can — what an approver still has room to grant.

Naming the wall is not cosmetic. Three limits can refuse a request and only one is
raisable, so before this a client's only generic response to any of them was "ask an
admin", including for the two walls where asking an admin does nothing. **Being
denominated in money does not make a limit raisable**: the per-model quota's
user scope is micro-USD and is not grantable, and it is precisely the wall somebody
would otherwise assume was.

Grantability is read from the same declaration the admission path enforces, so the
refusal path and the raise path cannot disagree about which wall is which. The public
name of a wall is derived from its internal one through a single **total** projection,
and that projection is one-to-many in one place: the per-model quota is a single
declared limit and two things a reader can act on, because the counter that refused is
the tenant's or the user's and they have different fixes. When a routing cascade's
candidates were refused by *different* scopes there is no single true answer, and the
refusal says so rather than naming the last one it saw.

The hint carries the **remaining cap**. Without it a surface can pre-fill an amount no
approver is permitted to grant, and the requester discovers that a day later from a
`grant_cap_exceeded`. The shape was shipped final, with degenerate (one-candidate)
content, so a client written against the early shape renders a fuller one unchanged —
filling it later is an append, never a second wire change.

**What "filled" turned out to mean, exactly (F3).** At the moment of a *pool* refusal
exactly one candidate has been priced — the routing cascade leaves on a pool refusal
before trying the rest of the chain — so that hint's `candidates` still holds one
element, and the rest of the chain is named in a new field, `unattempted_model_ids`, by
id only, with no cost data: stating that cheaper candidates were not attempted is not
the same as knowing what they would have cost. A *quota* cascade is the opposite case:
`QuotaExhausted` is caught and the cascade genuinely tries every candidate up to the
one that finally 402s, so that hint's `candidates` holds one entry per candidate
actually priced, none of them grantable (only the pool wall is), and
`unattempted_model_ids` is empty because nothing here was skipped. Both cases add
`minimum_raise_microusd` (the smallest raise that clears the cheapest **grantable**
priced candidate, zero when none was), `requested_model_id` and
`target_shortfall_microusd` (the candidate the caller actually wanted, and its own
shortfall — "her own number", never a cheaper candidate's), `router_mode`
(`cascade`/`pin`/`fallback_disabled`), `pricing_version` and `priced_at`. A refusal
caused by a grant expiring within three sweep intervals of `expires_at` sets
`grant_expired` on the candidate it explains — a boolean beside `blocker` rather than
a fifth value inside it, because `blocker` is a closed four-value enum. The 402 body's
`message` states the target's own shortfall in dollars and names any unattempted
candidates in prose, so a client that ignores `raise_hint` entirely is still told a
number: "must state, not just carry" applies to the string a human reads, not only the
object a script reads.

**Where the fill lands.** `mvp/_pipeline.py`'s `_reserve_over_candidates` builds the
multi-candidate hint from `priced_tried` at the single `_err_402` that ends the cascade;
its per-candidate `reserve_credit` calls (the pool-wall path) build the one-candidate
hint and, on a pool refusal, the caller augments it with the tail it never got to try.
Neither path adds a DynamoDB call to price anything that was not already being priced —
the remaining cap is read once, best-effort, from a row the refusal already has in hand
or a fresh read that degrades to `remaining_cap_microusd: 0` rather than a 500.

## 9. Setting a figure while a grant is live

An operator setting a figure that **equals the ceiling currently in force**, while
part of that ceiling is granted, is refused. The setter treats the figure as the new
baseline and moves the ceiling by the delta against the OLD baseline only, leaving
the granted term untouched — so a figure copied straight off the screen (baseline
plus a still-live grant) becomes the new baseline, and the same grant is added on top
of it again: the ceiling sits at the typed figure **plus** the grant for as long as
the grant stays open, one grant's worth above what was on the screen. That excess is
temporary, not permanent — the sweep subtracts the grant once at expiry and the
ceiling lands exactly on the figure the operator typed, never below it. But a window
of extra capacity nobody asked for is indistinguishable, from the figure alone, from
an operator who genuinely wants that number as the new baseline, for whom the same
jump-then-settle is correct — the number alone cannot tell the two readings apart,
which is why it is a refusal naming the composition rather than a silent
reinterpretation.

The read surface carries the composition for the same reason: the granted term, the
baseline, and the cap as **three** fields, because there are three facts and
collapsing them loses the one that matters. Whether anybody set a cap, what number is
in force, and which of those two you are looking at are different questions, and a
console showing a single figure cannot tell an operator whether it will move when the
tenant hires.

## 10. Retirement drains before it deletes

Retiring a tenant revokes its live grants first and is **refused** while any remain.
Archiving over one leaves a grant pinned to a pool row nobody will look at again: the
sweep keeps trying to revoke it, the reconciler keeps counting it against a cap for a
tenant that no longer exists, and its capacity can never be released because
releasing means moving a retired tenant's ceiling.

The drain, and the reconciler's orphan hunt, both **start from grants** rather than
from pool rows. A sweep that starts from pool rows has no row to start at for a grant
whose target is gone, so the one defect it most needs to find is the one it is
structurally unable to see.

## 11. What is deliberately not solved here

Two of these are missing **product**, not missing tests, and are stated rather than
patched over because the answer to each is a decision and not a surface.

- **The token wall names no way out.** A money raise filed against a token-quota
  refusal is refused, correctly — the per-user token quota is not a money ceiling and
  no approver can grant it. Nothing anywhere tells the person what to do instead,
  because there is nothing: the token quota has no raise path of its own. The refusal
  is honest about having no answer, which is better than inventing one, and the answer
  is a product decision about that quota rather than a screen.
- **No surface names who can approve.** A deploy-time check proves somebody holds the
  approval permission; nothing tells a requester who. From her seat, "the approver is
  on holiday" and "this feature is broken" are the same observation. Closing it needs
  a new authorization query and a new endpoint, and adding either inside an
  integrating change is how scope grows.
- **Nothing sets the aggregate cap.** It is read wherever it matters and no request
  writes it, so today it is either absent — the derived default, which is the case
  every tenant is in — or set out of band. The direction for operator-editable
  defaults is in [limits.md](limits.md) section 6.
- **A tenant with grants and no pool row at all is invisible to the daily loop.**
  That loop visits pool rows, so a tenant whose every grant is an orphan has no row to
  be noticed from. Pointing the per-tenant reconciliation at it finds them; the fleet
  pass does not.

## 12. A fallback caused by an expiring grant is visible as such (F3)

Naming a wall is not the same as naming a *cause*, and today only one cause is
reachable from either surface this section describes.

**The refusal.** A pool refusal's candidate carries `grant_expired: true` when a grant
against the same wall has `expires_at` within `[expires_at, expires_at + 15 minutes]` of
now — three five-minute sweep intervals, so a late sweep still gets to name the cause —
computed by the pure function `fallback_reason_for_expired_grant(grant_expires_at,
now_epoch, grant_wall, blocked_wall)`, which also requires the grant's wall and the
refusing wall to be the *same* one: a grant against the pool cannot explain a per-model
refusal just because it expired recently. The refusal's `message` names it in prose too.

**The usage row.** `usage_logs.record()` takes one additive attribute,
`fallback_reason`, set at settle time — never derived after the fact, because the cause
is a fact about the router's decision, not something the two model ids can reconstruct.
Today it can only ever be `"quota_exhausted"`: the cascade advances past a candidate on
`QuotaExhausted` alone, and a pool-wall refusal aborts the whole request rather than
being caught and advanced past (section 8's own "leaves on a pool refusal"), so a
*successfully served* fallback can never have been caused by a grant expiring at the
pool wall — only a refusal can, which is what the candidate flag above is for. A cheaper
candidate that would fit is genuinely never tried once a pricier one hits the pool wall;
that is a defect in the cascade itself, not a gap in this attribute, and it belongs to
whichever change eventually lets the loop advance past a pool refusal rather than
abandoning the request.

## 13. Surfaces: the console and the CLI (F3)

Two console views. The **self-service request view**
(`frontend/src/pages/MeLimitRaises.tsx`, route `/me/limit-raises`) shows the walls that
apply to the caller before any refusal — a new endpoint,
`GET /me/limit-raises/wall-status`, reduced to exactly enough for a requester to see
whether asking makes sense (no `manual_limit`, no seat internals: those are an
approver's business) — and the caller's own requests, joined to their decision (R24: a
decided request states its approved amount and expiry, never the bare status word). A
submission pre-fills from the `raise_hint` of the 402 that sent the caller there,
carried in the browser's own navigation state rather than reconstructed: a screen
opened without that state (a deep link, a reload, a bookmark) shows no pre-filled
amount and names no wall, because a reconstructed answer to "what refused you" is a
second answer computed at a different moment, and the two can disagree while both look
authoritative. When the hint's `minimum_raise_microusd` exceeds its
`remaining_cap_microusd`, the view renders that conflict instead of a pre-filled figure
that approval would refuse a day later with `422 grant_cap_exceeded`.

The **tenant approval view** (`frontend/src/pages/LimitRaiseApproval.tsx`, routes
`/{admin,team-lead}/tenants/:tenantId/limit-raises`) renders the queue, the tenant's
live position and its ceiling composition through the same `PoolBudgetCard` the tenant
detail pages already use — one renderer, not two that could disagree — and a new
endpoint, `GET .../limit-raises/latest-permissible-expiry`, so the bound on an
approval's `expires_at` is shown before it is typed rather than discovered from a
refusal. It states plainly, rather than inventing, that the request's own position
*at the time it was filed* has no data to render: nothing in the raise mechanism
captures that snapshot today, which is a gap in the mechanism this view sits over, not
a computation this view was ever positioned to perform.

The **grant inventory** (`frontend/src/pages/GrantsInventory.tsx`, standalone routes
`/{admin,team-lead}/tenants/:tenantId/limit-grants`) renders one total per target row,
never a tenant-wide sum, and a `REVOKE_BLOCKED` grant's reason next to its row.

The **CLI** (`cli/src/client.rs`) renders the same hint in a 402's error message: each
priced candidate with its dollar shortfall, the unattempted tail by name, and — when a
priced candidate is grantable and affordable — the exact `stratoclave limit-raise
request` invocation to run next, or B6's conflict sentence in its place.

## 14. Where each rule lives

| Rule | Owner |
| --- | --- |
| The identity, and the two grant writers | `dynamo/tenant_budgets.py` |
| The row's shape, and both grant attributes' classification | `dynamo/pool_row_schema.py` |
| Request, grant and slot storage; both indexes | `dynamo/quota_events.py` |
| Every lifecycle rule, the refusals, the sweep, the checks | `mvp/grants.py` |
| Which walls exist and which are grantable | `mvp/reserve_limits.py` |
| The refusal body, its hint, and the expired-grant classification | `mvp/_pipeline.py` |
| The setter guard, the composition read, the retirement drain | `mvp/admin_tenants.py` |
| The fallback cause, additive on the usage row | `dynamo/usage_logs.py` |
| The self-service and approval console views | `frontend/src/pages/MeLimitRaises.tsx`, `frontend/src/pages/LimitRaiseApproval.tsx` |
| The grant inventory console view | `frontend/src/pages/GrantsInventory.tsx` |
| The CLI's rendering of a refusal's hint | `cli/src/client.rs` |
| The sweep's schedule and its three alarms | `iac/lib/quota-grants-stack.ts` |
