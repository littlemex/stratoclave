<!-- Last updated: 2026-08-30 -->

# Contract: a hard dollar ceiling for strict-mode traffic

The normative source for the dollar-ceiling behaviour: what strict mode guarantees,
what it refuses, and the assumptions the guarantee rests on. Code that implements a
rule here cites the section, so a change to the behaviour is a change to this
document first.

Every rule is stated as a property rather than as a target number, because the
numbers follow from the properties and drift when they are copied. The verification
lives in `backend/tests/` — `test_rating_formal_z3.py`,
`test_pricing_pinning_z3.py`, `test_reservation_bound_formal_z3.py`,
`test_rating_differential.py`, `test_reservation_bound_differential.py`,
`test_billing_formal_z3.py` — and an implementation should satisfy the properties
here rather than the mutations those files happen to check.

An ambiguity in this document is a defect in it. The shadow measurement and the
refusal-rate target are in section 9b, because the gate cannot be switched on
responsibly without them. [`calibrated-mode.md`](calibrated-mode.md) holds calibrated
mode and the shortage-versus-sizing reporting, which are a later stage of the same
design.

## Status in the shipped code

This document is normative for the behaviour, not a claim that the behaviour is
fully switched on. As of 2026-08-30:

- **The three states below are implemented** in
  `backend/mvp/reservation_bound.dollar_pool_bound_state`. The gate is
  `STRATOCLAVE_HARD_CEILING_GATE` and it is **on by default**, so a tenant with a
  dollar pool sits in `enforced` without an operator configuring anything. Setting
  that variable to a falsy value puts a pooled tenant in `shadow` instead, which is
  the deliberate way to measure a refusal rate before refusing anyone.
- **The bound, the payload survey and rate pinning ship**, and the reaper writes its
  RECLAIM terminal in the same transaction as the counter move (section 5).
- **Premise (P) holds on the rate axis and not on the token axis.** It used to fail on
  both: the estimator priced three legs where settle charged four, and the missing one
  — cache_write — costs more than fresh input, so any request that wrote prompt cache
  settled above its reservation. Both sides now read one leg registry
  (`mvp.pricing.BILLABLE_LEGS`) and every input-side token is priced at the worst rate
  an input-side leg can bill it at, so no classification the provider chooses can push
  the settle above the reservation. What remains is the token count: on the
  `accounting` path it is an estimate, and an estimate is not a bound, so a prompt that
  tokenises to more than the estimate still settles above its reservation. The ceiling
  theorem's precondition is met only where the bound is computed from bytes — the
  `measured`, `shadow` and `enforced` states below.
- **Per-leg rounding is covered explicitly.** Settle rounds each leg up; the bound
  rounds each group's total up once, and ceiling is not subadditive, so the bound
  carries `min(legs, tokens) - 1` microUSD of slack per group. Without it the "sound
  bound" was not an upper bound: three input-side legs at 1 microUSD/MTok with one
  token each settle at 3 while the group total rounds to 1.
- **Retaining a reservation instead of returning it is on by default**, under
  `STRATOCLAVE_UNOBSERVED_HOLDS`. A reaper that meets a hold whose provider call had
  departed holds the reservation rather than handing the budget back and recording
  that nothing was charged. Departure is a recorded fact rather than an inference from
  the exception type: each route announces the hand-off immediately before invoking the
  provider client, so an exception raised by this gateway's own code beforehand is
  refunded rather than held against a tenant. Holding it costs no
  new counter: the amount was already counted against the limit and goes on being
  counted, so one conditional status write is the whole mechanism. What the gateway
  cannot do is decide what the call cost, so a retention ends only when an operator
  settles it at the figure the provider's own record shows or releases it when that
  record shows none. The departure is recorded by the ENDING, which is the only moment
  anything knows a call left, so a task that dies with no ending at all records nothing
  and its hold still reclaims — the same residual C3.5 names, for the same reason.
  [`../EVIDENCE.md`](../EVIDENCE.md) is the live map of how far
  each claim is verified; where this document and that one disagree, that one is
  describing today.

## 0. Three states, named, because every rule below is scoped to one

Most of the rules here apply to some requests and not others, and leaving that
implicit is what makes a document like this contradict itself. So the states have
names and every rule and every acceptance criterion says which it applies to.

| State | When | Bound | Shared-item writes | Refusals from this work |
|---|---|---|---|---|
| **enforced** | at least one limit exists for the request — a tenant dollar pool, or a per-user token quota below `UNLIMITED_CREDIT` | computed, gates admission | only on the dimensions that have a limit | yes |
| **accounting** | no limit on any dimension | not computed | none | **none, ever** |
| **measured** | `accounting`, plus the measurement flag on | computed and recorded | none | **none, ever** |
| **shadow** | a limit exists, but gating is not yet switched on | computed and recorded | only on dimensions with a limit | none from this work |

`shadow` is the state section 9b runs in, and it is temporary by design: it exists so
the refusal rate can be measured before anyone is refused. Every rule written for
`enforced` applies in `shadow` except the refusals and the criteria that test them.

`enforced` is per dimension, not per tenant: a tenant with a token quota and no pool is
enforced on tokens and accounting on dollars, and touches the balance row but never the
pool row.

So the rules below say which dimension they belong to, because most of them are about
dollars. **Everything concerning the bound, `reserved_microusd`, the pool row, the
refusals of section 6 and the reaper of section 5 belongs to the dollar dimension and
applies only where a pool limit exists.** A tenant enforced on tokens alone gets none of
it: no bound, no dollar reservation, no `reserved_microusd` on its terminal, and no new
refusal. Its token quota keeps working exactly as it does today.

`strict` and `calibrated` are settings **within** `enforced`; they change how the bound
is derived, not whether it gates. `accounting` and `measured` are not modes of
enforcement and the word "mode" is not used for them.

## 1. The defect

`estimate_cost_microusd` produces an estimate, and the admission condition
`pool_headroom_microusd >= :amt` is handed that estimate. Settle records the actual,
which can be larger. Two independent causes:

- **No cache-write leg.** `rate_usage` billed four components — input, output,
  cache_read, cache_write — and the estimate priced three. Read the shipped rate
  document for how the cache-write rate compares with input. **Fixed**: the legs are
  enumerated once and the estimate prices the input side at the worst input-side rate,
  so this cause is closed on both the estimate and the bound path.
- **The input count is a heuristic, not a bound.** It is `char_count // 3`, calibrated
  for English. Scripts that tokenise near one token per character are under-reserved
  by roughly that factor, with no prompt caching involved. Reproduce it on text in a
  language you can check. **This cause is what the bound below exists for**, and it is
  still live wherever the bound is not computed — that is, in `accounting`.

## 2. What stays as it is

- The pool transaction and its condition expression. Admission already refuses
  correctly: if the headroom does not cover the amount the write fails, no hold
  exists, and no upstream call happens.
- The **semantics** of the existing 402: budget exhausted still means budget
  exhausted. New refusal reasons are added alongside it.
- **Booking at settle is unconditional.** You will add checks at settle and none of
  them may suppress or reduce the booking. The recorded amount is the truth even when
  it breaks a bound; that is what the overrun signal is for.
- **Token accounting.** Settle records Bedrock's own usage block — `inputTokens`,
  `outputTokens`, `cacheReadInputTokens`, `cacheWriteInputTokens` — so every token the
  provider billed is already captured, including injected and image tokens. Do not
  touch that path.

## 3. The bound

A function returning an amount no smaller than any amount settle can produce for that
request, under the assumptions in section 4. **This is the `strict` bound.** Calibrated
mode replaces the derivation with a measured one and gives up soundness deliberately;
soundness is required here and in every criterion that names `strict`.

- **Every billable component has a term.** Today: input, output, cache_read,
  cache_write — and not as a list written here and repeated in two functions. The legs
  are declared once in `mvp.pricing.BILLABLE_LEGS` with the group each belongs to, the
  worst-rate helper and the settle rater both read it, and a rate column that charges
  money with no leg fails `backend/tests/test_billable_legs_registry.py`. A fifth leg
  therefore reaches the bound and the estimate in the change that adds it.
- **All input-side tokens are priced at the worst input-side rate**,
  `max(input, cache_read, cache_write)`. The provider cannot cache content it was not
  sent, so this covers any caching behaviour with no assumption about what it caches.
  This rate applies to the image term of section 3b as well.
- **The token count is bounded, not estimated.** UTF-8 **bytes** is a sound unit
  because a token consumes at least one byte. Characters are NOT sound: a byte-level
  tokeniser can split one multi-byte character into several tokens.
- **Output is bounded by `max_output_tokens * effort_multiplier`.** Both inputs must be
  resolved before the bound is computed:
  - `max_output_tokens` comes from the request. If absent, inject the tenant's
    configured ceiling; if the tenant has none configured, refuse.
  - `effort_multiplier` is the reasoning-effort factor the OpenAI-compatible route
    already derives per request. Resolve it from the same source the route uses today,
    and where a route has no notion of it, use 1. If a route can produce a value the
    gateway cannot determine before the call, refuse rather than assume 1.
- **Monotone in every input.** More bytes, a higher output ceiling or a larger effort
  multiplier may never decrease the bound. A non-monotone bound lets a caller shrink
  its reservation by sending more, which is the shape of every quota-evasion bug.
- **Total.** Every request either gets a bound or is refused.

### 3a. Bound the payload that is actually sent, and pin it

Compute the byte count over the **canonical payload the gateway will send to
Bedrock**, serialised, at the last point before the client library — not over the
request body received. Between those points the gateway may add a system prompt,
retrieved context, tool schemas, a guardrail wrapper or a converted image, and every
one of those is billed.

- Record on the hold: the **byte length of the non-image parts**, the image terms of
  section 3b, and a **hash over the entire serialised payload including image bytes**.
  The length excludes image bytes because they are priced by dimension instead; the
  hash does not, because a retry that swapped image bytes while keeping the length
  must not pass the pin.
- **A retry may resend only the byte-identical pinned payload, reusing its hold.** A
  retry whose payload differs by one byte needs a new reservation.
- **After an attempt whose billing outcome is unknown — an upstream timeout, a dropped
  connection, any case where a charge may have landed without a usage block — a retry
  requires its own full reservation.** Reusing the hold would let two billable
  attempts sit behind one reservation. If the tenant's headroom cannot cover a second
  full reservation, the retry is refused; that is the correct outcome, not a
  regression.
- A hold that may still settle must not be reaped. See section 5.
- At most one settle per hold; the terminal's `attribute_not_exists` key enforces that.
- If any code path can modify the payload after the bound is computed, either move the
  computation after it or make that path fail. A convention is a silent breach.
  **Check whether the current code already has this ordering problem and report what
  you find.**

### 3b. Images

Image token cost scales with pixel dimensions, and dimensions are not a declared
field — Bedrock Converse carries images as raw bytes or an S3 reference. Deriving them
means parsing the image header.

- Parse dimensions from the header, per format. Do not decode pixel data and do not
  expand compressed data.
- **Parse the image that will actually be sent.** If the gateway converts or resizes
  an image, the bound is computed on the converted result, because that is what the
  provider receives. Provider-side downscaling after that is safe: a bound from the
  dimensions sent still dominates a smaller image the provider derives.
- Convert dimensions to a token upper bound using **the provider's documented formula
  for the target model**, and cite the source in a comment. If no formula is published
  for a model, use the most conservative published formula across the models you
  support and multiply by a safety factor you state and justify. Price the result at
  the worst input-side rate.
- **Fail closed.** An image whose dimensions cannot be determined — unsupported
  format, truncated or malformed header, a header disagreeing with the payload — is
  refused, never admitted with the term skipped.
- **S3-referenced images are refused.** Sizing the object then sending a reference is
  a time-of-check-to-time-of-use race: the object can be replaced between the two, and
  pinning would need a versioned reference the provider is not documented to honour.

## 4. The assumptions the guarantee rests on

Put these where a reader of the guarantee will see them. The guarantee is conditional
on all four, and each is either checked or explicitly unchecked.

- **The provider respects `max_output_tokens * effort_multiplier`** for total output
  including reasoning tokens. Checked at settle, per request, with its own cause code
  on violation. Checking after the fact is not prevention.
- **The input-side counts partition the prompt**, so their sum is bounded by the
  payload's byte length. Unchecked; if a provider ever counted a token as both read
  and written, the bound breaks.
- **A hold is not reaped while its charge can still arrive.** Enforced by section 5.
- **Every charge passes through reserve.** Audited by section 7.

**Tool use needs no special rule, and this was checked rather than assumed.**
`_reject_server_side_tools` in `backend/mvp/anthropic.py` already refuses Anthropic's
server-executed tools — web search, web fetch, code execution, computer, bash, text
editor — on every route and in every mode, because Bedrock's Converse API cannot
express them. Only client-side tools pass through, and their schemas travel in the
payload the gateway sends, so the byte term covers them. Tool results arrive in a
later request whose bytes the gateway also measures. Nothing about tool use is
unbounded on this route.

Read that function before you build anything that touches tools, and if you find a
path where a server-executed tool can reach Bedrock, that is a defect worth reporting
on its own — it would put an unbounded term back into the bound.

## 5. The reaper is part of the guarantee, not housekeeping

A sound bound does not make the ceiling hard if a live hold can be released early: the
returned headroom admits a second request, and when the first charge lands both are
booked. So:

- **The reap timeout must exceed the maximum time a charge can still arrive** for a
  hold — the request deadline plus the retry budget plus a margin for clock skew.
  Derive it from those values in code rather than choosing a constant, so a change to
  a timeout cannot silently invalidate it.
- Assert that relationship at startup, and fail startup if it does not hold. A
  guarantee that depends on two independently configured durations must not depend on
  someone noticing.
- **A retry attempted against a hold that has been reaped is refused**, not silently
  re-reserved. The caller may retry as a new request, which takes a new reservation.
- **A settle arriving after its hold was reaped** is still booked, marked
  reservation-less with its own cause code, and alarmed. Do not drop it and do not
  recreate a reservation for it. The ledger can then legitimately show
  `settled + reserved > pool_limit`, so the acceptance criteria name this exception
  explicitly.
- A charge for an attempt that returned no usage block never appears in the ledger at
  all, which is **out of scope by the owner's decision** rather than a gap waiting to
  be closed: what the gateway cannot observe is outside the cost model. Do not attempt
  to infer it. (An attempt whose usage WAS observed and whose settle failed to commit
  is a different case and is closed — see C3.5.)

## 6. What strict mode refuses, and what it guarantees

Refused at admission, before any upstream call, **in `enforced` only**:

- an image whose dimensions cannot be read, or an S3-referenced image
- a request with no output ceiling and no tenant default to inject
- a request whose `effort_multiplier` cannot be determined before the call
- a request whose bound exceeds `pool_limit`, with a reason distinct from ordinary
  exhaustion. Here the operator action really is to resize, because no amount of
  waiting makes the request fit. That is not in tension with the rule that a high
  refusal *rate* is answered by a tighter bound and never by a larger limit: one is a
  single request that cannot fit, the other is a bound that is too loose.

In `accounting` and `measured`, none of these refuses. A request whose bound cannot be
computed in `measured` is recorded with the bound absent and a reason, and proceeds.
"Every request either gets a bound or is refused" is a rule of `enforced`.

The guarantee, stated with its boundary rather than as a slogan: **for admitted
requests, the pool cannot be overspent, except for two cases, both alarmed and both
recorded with their own cause code — a charge whose hold was reaped before it settled,
and a charge where the provider exceeded the output ceiling it was given.** Those are the
visible failure modes; there are two, not one. Do not write "overspend is impossible"
without naming both.

And one limit that no rule in this document removes: **a charge landing at Bedrock and
the ledger write recording it are not atomic.** If Bedrock bills a request and this
process then dies, the hold is never settled, the reaper releases it, and the real spend
vanishes from the ledger — after which later admissions can carry the pool past its limit
with every invariant here still intact, because the ledger no longer holds the charge.
Deriving the reap timeout correctly narrows that window and does not close it. Say so
wherever the guarantee is stated; it is the ceiling on the ceiling.

The envelope is wider than an earlier reading of it suggested: text, inline images, and
client-side tool use are all inside it, which is the ordinary shape of chat,
summarisation, extraction, classification and image understanding traffic.

## 7. Audit for bypasses

Every charge must pass through reserve. Find and either close or document: an
additional mid-stream charge, an external fixed-amount capture, any admission path
that does not go through reserve. One bypass voids the ceiling regardless of how sound
the bound is. Hold reuse on a byte-identical retry after a *non-ambiguous* failure is
permitted by section 3a and is not a bypass.

## 7a. Budget enforcement is opt-in, and must stay that way

Two levels of limit already exist and both can be absent:

- **No pool row for a tenant** means no dollar ceiling; only the token quota applies.
- **A token quota at or above `UNLIMITED_CREDIT`** (`backend/dynamo/user_tenants.py`)
  means no token ceiling either.

A tenant with both absent is a **pure accounting tenant**: every token is still
recorded and every charge still rated, and nothing is ever refused for budget. That is
a supported and useful configuration — benchmarking, and any case where the point is
measurement rather than control — and this change must not break it.

So: **the bound gates admission only where a limit exists**, and section 0's table says
where it is computed at all. A pure accounting tenant must see no new refusals from
this work: not for an unreadable image, not for a missing output ceiling, not for a
bound larger than a limit that does not exist. Refusals belong to enforcement, and a
tenant that has not opted into enforcement has not opted into refusals.

### 7b. Serialisation exists to serve a limit, and only a limit

The principle, from which every rule below follows: **the only reason to serialise
requests on a shared item is to prove that a limit was not exceeded.** Measurement
needs no such proof, so measurement needs no serialisation. Where there is no limit,
there must be no contention.

Read that as a per-dimension rule, not a per-tenant one. There are two limits and each
has its own shared item:

- the **per-user token quota** lives on the user's balance row, and a write to it
  serialises that user's own requests
- the **tenant dollar pool** lives on one row per tenant per period, so a write to it
  serialises every request from every user of that tenant

A request must touch the item for a limit **if and only if that limit exists for it**.
A tenant with a token quota and no dollar pool must not touch the pool row. A tenant
with a pool and an unlimited token quota must not read-modify-write the balance row. A
tenant with neither must touch neither.

Concretely, on the request path, for a dimension with no limit:

- no read-modify-write on its shared counter
- no conditional write, even one whose condition cannot fail — an always-true condition
  still costs a round trip and still contends on the item
- no reservation and no release, because there is nothing to reserve against

**What measurement legitimately needs, and may always do:** write the usage row for the
request. It is keyed per request, so it is an append against no shared item and
contends with nothing. Rating the charge is arithmetic and needs no item at all. That
is the floor, it is enough for per-tenant and per-user accounting, and it is what makes
a benchmarking tenant cost the same as a bare proxy.

**Do not compute the bound where it cannot be enforced**, unless a measurement flag is
on, default off. Computing it means serialising the outbound payload and parsing image
headers, which is real work for a request that can never be refused. This overrides the
"record the bound for every request" line in section 7a: compute it where it gates
admission, or where measurement is deliberately switched on and someone has accepted
the cost.

The test to hold yourself to: **a request against no limit should touch no item that
another concurrent request also touches.** If one does, name the item and say why it
could not be avoided.

## 7c. Changing a tenant's setting mid-period

A calibrated reservation can be smaller than a strict bound, so after a switch to
strict the pool can hold pre-switch holds whose reservations were never sound. Do not
attempt to reconcile them: booking is unconditional and there is nothing to adjust.

The rule: **a switch applies to admissions from that moment, and the guarantee is
claimed only once every pre-switch hold has settled or been reaped.** Record the switch
in the ledger with its timestamp so the boundary is reconstructible, and do not halt
admissions, drain, or top up — those all cost availability to buy a property that
waiting gives for free.

## 8. Rates must not move under a request in flight

A request in flight when rates rise would settle above a bound computed at the old
rate. The rule: **settle at the reserve-time pinned snapshot.** The snapshot mechanism
appears to do this already — verify it and report whether it holds on every path
including late settle. Do not add a headroom margin for rate changes: a margin is an
estimate, which reintroduces the defect being fixed.

## 9. What to record

On the terminal event, in `enforced`:

- `reserved_microusd` — the amount admission checked
- the bound's inputs: non-image byte length, payload hash, the image terms,
  `max_output_tokens`, `effort_multiplier` — so the reservation is recomputable rather
  than an opaque number
- the reserve-time pricing version
- `overrun_microusd = max(0, actual - reserved)` with a cause code distinguishing:
  bound exceeded, provider exceeded the output ceiling, **hold reaped before settle**,
  and **no limit configured**

Those last two must be separate codes. `hold reaped before settle` is alarmed;
`no limit configured` is not. In `accounting`
every settle legitimately arrives with no reservation behind it, so a single code
meaning "no reservation found" would fire on every request of every accounting tenant.
It would be muted within a week, and the moment it is muted the one visible failure
mode of the guarantee — a hold reaped while its charge was still coming — goes silent
with it. `no limit configured` is normal and is not alarmed; `hold reaped before
settle` is a defect and is.

In `measured`, record the bound and its inputs but no `reserved_microusd`, because
nothing was reserved. In `accounting`, record the usage row and the rating; there is no
bound and no reservation to record, and the terminal carries neither field rather than
carrying zeros that would read as "reserved nothing and spent nothing".

In the ledger, in `enforced`: **hold creation and hold reaping events**, so that pool
state is reconstructible from the ledger rather than only assertable.

Under a sound bound the overrun is zero except for reservation-less charges.
**An overrun is a defect report about the bound, not an operating mode.** Design the
alarm on that basis: an alarm that fires in normal operation gets muted, and a muted
alarm returns the system to a silently soft ceiling while still paying the headroom
cost.

## 9b. Measure before enforcing, in that order

Enforcement cannot be switched on responsibly without knowing what it will refuse, and
that knowledge cannot come from anywhere but real traffic. So this is part of the first
change, not a later one:

1. Ship the bound, computing and recording it in `measured` for tenants that opt in,
   and in `enforced` **without gating** behind a flag that defaults to off.
2. Agree a maximum acceptable refusal rate **before** looking at the data.
3. Run the shadow: for real traffic, record the bound alongside the actual settle.
   Report the bound-to-actual ratio distribution, the simulated refusal rate at current
   pool sizes, and the realised tokens-per-byte, per tenant and per script. Identify
   script by the dominant Unicode block of the request text and say so, so the
   breakdown means something specific.
4. Only then turn gating on, and only if the simulated refusal rate is under the agreed
   figure. If it is not, tighten the bound — never raise a pool limit, which converts a
   correctness property into a capacity setting.

Without step 3 the refusal rate is learned from an incident. Without step 2 the figure
is chosen to match whatever the data happened to show.

## 10. Acceptance criteria

Each names the state it applies to. A criterion with no state named applies to all
three.

1. `strict`, with a pool limit: for every admitted request, the amount settled is not
   greater than the amount admission checked, excluding charges whose cause code is
   `hold reaped before settle` or `provider exceeded the output ceiling`. Read both from
   the ledger. Not required of `calibrated`, which gives up soundness by design.
2. `enforced` with a pool limit, once gating is on: every request in section 6's refusal
   list is refused before any upstream call. Not required in `shadow`, where gating is
   off by definition. `accounting` and `measured`: no request is refused by this work, including
   requests whose bound cannot be computed.
3. `strict` with a pool limit, once gating is on: concurrent callers cannot drive
   `settled + reserved` above `pool_limit`
   in any state reconstructible from the ledger, excluding the two excluded cause codes.
4. Wherever a bound was computed: the terminal event carries enough to recompute it, and
   where a reservation was taken, the reservation too, from recorded values alone. A
   request whose bound section 6 permits to be absent is exempt and carries the reason
   instead.
5. `strict`, with a pool limit: overrun is zero, excluding the two excluded cause codes.
6. The ledger's token fields equal the usage block in the provider's response.
7. `enforced`: the non-image byte length and the payload hash recorded on the hold equal
   those of the outbound bytes captured **at the client-library boundary**, for every
   admitted request including retries. Compare against an independent capture, not
   against the same code that recorded them. If the client library appends bytes after
   that boundary, say so and say what you did about it.
8. `enforced`: startup fails when the reap timeout does not exceed the request deadline
   plus the retry budget plus the skew margin.
9. `accounting` and `measured`: a request touches no item that another concurrent request
   also touches.
   Demonstrate it, by listing the writes the request path performs.
10. `accounting` and `measured`: an ambiguous-failure retry does not record the usage
    twice. The reservation machinery is absent here, so the idempotency that protects
    `enforced` must be shown to protect the counts as well — a measurement tenant with
    double-counted tokens is worse than useless.
11. The shadow report of section 9b exists, covers real traffic, is broken down per
    tenant and per script, and the measured refusal rate is under the figure agreed
    before the run — or the bound was tightened until it was, and both numbers are
    recorded.

## 11. Out of scope

Whether the rate used was the officially correct price: the administrator's
responsibility. Reconciling with the AWS invoice: Cost Explorer holds the authoritative
money, and this system exists to account for tokens per tenant and per user and derive
cost from that. Charges the gateway cannot observe. A separate sub-pool for tool
traffic. Clamping, rounding or adjusting a recorded amount to fit the ceiling: the
recorded amount is the truth, and a ceiling maintained by editing the ledger is worse
than no ceiling. Raising the pool limit as the answer to refusals: that converts a
correctness property into a capacity setting and leaves the number meaningless.
