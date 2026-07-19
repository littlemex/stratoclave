<!-- Last updated: 2026-07-20 -->
<!-- Applies to: Stratoclave main -->

# Design: the PENDING protocol (non-transactional hot-path reserve)

This is the confirmed hot-path reserve design that
[ledger-hot-path.md](./ledger-hot-path.md) arrived at once the measurements
killed the transactional approach. The item-count spike proved a 4/3/2-item
`TransactWriteItems` has an identical ~1,190 ms c=16 p99 (the tail is *the
transaction on a hot row*, not item count); the PENDING spike proved a single
non-transactional conditional `UpdateItem` cuts the floor p99 to 8.6 ms and c=16
to 88 ms with zero conflict retries (see
[../benchmarks/ledger-latency.md](../benchmarks/ledger-latency.md)). This document
is the design and the **proof obligations** — the protocol trades transactional
atomicity for latency, so the correctness machinery IS the work, and it is
specified here before implementation.

**Implementation status (2026-07-20): IMPLEMENTED, flag-gated OFF.** The production
code is in place behind `STRATOCLAVE_RESERVE_PROTOCOL` (default `transaction` =
today's path, byte-for-byte unchanged; `pending` = this protocol):
`dynamo/tenant_budgets.py` carries the primitives (`hold_put_pending`,
`pool_reserve_update`, `hold_activate`, `fence_pending_expired`,
`reconcile_credit_back`, `list_holds`, status transitions); `mvp/_pipeline.py`
carries `_reserve_external_pending` (the 3-write reserve), `sweep_fence_pending`,
and `reconcile_pool`. The readers were made status-aware FIRST (reaper credits
only `status = ACTIVE OR attribute_not_exists(status)`), so the whole path is
inert until the flag is flipped per-tenant. The write-discipline guard
(`test_billing_write_discipline`) records the deliberate non-transactional counter
writes as PENDING-proof-backed exceptions to axiom A2. What remains is NOT code —
it is the live per-tenant canary flip, which is gated on the observation windows
in "Still to measure" below and cannot be short-cut. Turning the flag on globally
is an operational rollout, not an engineering task.

The whole design derives mechanically from ONE decision, fixed first: **when it is
uncertain whether the pool was debited, never credit it back.** Crediting an
un-debited hold is oversell (unrecoverable, customer-visible over-admission); not
crediting a debited-then-orphaned hold is a leak (recoverable by a reconciliation
job). Every ambiguous case below resolves to the leak-safe side.

## The protocol

`reserve` is three writes; only the middle one is contended and it is the commit
point:

1. **Put HOLD `status=PENDING`** — unique key, distributed partition,
   `attribute_not_exists`, uncontended. This write MUST precede the pool debit: it
   is the *write-ahead intent* that guarantees every pool debit has a discoverable
   HOLD record (the reconciler's recovery basis). **Parallelising step 1 and 2 is
   forbidden** — a "pool debited, no HOLD record" state is an unfindable leak.
2. **A single conditional `UpdateItem` on the pool row** — `ADD
   pool_headroom_microusd :neg, pool_reserved_microusd :amt`, condition
   `headroom >= amt AND status = active`, **no transaction**. This is the COMMIT
   POINT: on success the caller is answered success. Its SDK call is configured
   `max_attempts = 1` (see idempotency, below).
3. **Mark HOLD `ACTIVE`** — OFF the synchronous critical path, issued async
   (fire-with-retry) after the caller is answered, conditional on the row being
   still PENDING. Client-observed e2e is therefore 2 round-trips.

## State machine

```
(none) --putPending--> PENDING --commit(step2 ok)+activate(step3)--> ACTIVE
PENDING --sweeper fence (still PENDING, timed out)--> EXPIRED_UNCREDITED
PENDING --client saw definitive fail (CCF)--> FAILED         (optional; leak-safe)
ACTIVE  --settle--> SETTLED
ACTIVE  --release--> RELEASED
ACTIVE  --reaper (expiry, credited)--> EXPIRED
EXPIRED_UNCREDITED --reconciler aggregate recovery--> RECLAIMED
```

Two distinct expiry terminals, on purpose (Fable review): `ACTIVE → EXPIRED` is
credited (the debit is known to have happened, so the reaper credits back +
writes a RECLAIM terminal, as today); `PENDING → EXPIRED_UNCREDITED` touches the
pool NOT AT ALL (the sweeper cannot know whether the debit happened, so it never
credits — the debited-but-orphaned amount leaks until the reconciler recovers it
in aggregate). Merging these two into one EXPIRED would corrupt the reconciler's
Σ and seed a double-credit; they must stay separate.

**One arbiter for mutual exclusion.** Terminal exclusivity is decided by the HOLD
`status` conditional transition — NOT by both that AND the terminal-ledger-item
`attribute_not_exists`. Using two arbiters admits a split ("status = SETTLED but
terminal item = RECLAIM"). Status is the arbiter; the terminal ledger item is a
projection of it.

## The debited/undebited ambiguity — ghost state, never in the schema

Do NOT add a `PENDING_COMMITTED` status. A mark written after step 2 is not atomic
with step 2, so it only moves the ambiguity window one step later — the window
between the last mark and the debit is intrinsic to two-phase intent and no number
of writes closes it. Instead the ambiguity lives ONLY in the formal model as a
**ghost variable** `decrement_applied[hold_id]: bool`, known to the environment
(the fake DynamoDB, which actually ran step 2) and unreadable by the code under
test (sweeper, reconciler, client). Ambiguous-failure injection is a rule that
*hides* step 2's outcome from the SUT (adversarially choosing to hide a success or
a failure).

The pool row is a counter, not a ledger: whether a given hold's debit is included
in `pool_reserved` CANNOT be recovered by reading the counter. A counter read
yields only aggregate drift, never per-hold attribution. This is why the sweeper
and reconciler operate on aggregates and never on per-hold credit decisions.

## Invariants (proof obligations)

Proven two ways: **Z3** for inductive invariant preservation (`Inv(s) ∧ Guard(s) ⇒
Inv(post(s))` over every transition, amounts/counters as unbounded symbolic ints —
the ambiguous transition is proven for BOTH ghost values), and **Hypothesis
stateful** for adversarial interleavings against the real implementation.

- **I1' (no oversell):** `pool_reserved == Σ amount over holds where
  decrement_applied ∧ ¬credited_back`. Every code decision must preserve this for
  *both* ghost values of an ambiguous hold — crediting an undebited hold breaks
  I1' (oversell), not crediting a debited orphan is within I2 (leak). This
  asymmetry is the formal meaning of "never credit back on uncertainty".
- **I2 (bounded leak):** a debited hold with no live entitlement persists only
  until the reconciler recovers it; the outstanding leak is bounded by the count
  of ambiguous in-flight reserves.
- **I3 (sweeper never races async ACTIVE):** a concurrent `PENDING → EXPIRED_
  UNCREDITED` fence and `PENDING → ACTIVE` (step 3) converge to exactly one, by the
  single-item conditional-write serialization axiom (A2) — Hypothesis-checked, no
  Z3 needed.
- **I4 (no double-debit):** step 2 is never re-sent — `max_attempts = 1`, and an
  ambiguous outcome mints a fresh hold rather than retrying the debit.
- **I5 (crash-safety / quiescence):** after injection stops and the sweeper +
  reconciler each run one pass, `drift == 0` AND no timed-out non-terminal hold
  remains. Checked as a stateful-machine teardown assertion (I5 was previously
  only prose; it is now an executable quiescence check).
- **I6 (idempotency):** a repeated Idempotency-Key yields at most one committed
  debit and a stable response; an ambiguous-failure retry that mints a fresh hold
  leaves the IDEMP record pointing at the committed hold. Enforced by deriving
  `hold_id` deterministically from the Idempotency-Key so step 1's
  `attribute_not_exists` doubles as duplicate detection.
- **I-biz (no premature fence of a committed reserve):** a hold the client was
  told succeeded is not moved to EXPIRED_UNCREDITED before its natural expiry.
  Guaranteed by the design constraint `PENDING timeout ≫ step-3 retry horizon`;
  if step 3 nonetheless finds the row already EXPIRED it MUST alert (never
  swallow). Accounting stays correct via the reconciler even if this is violated,
  but the client-visible contract does not — so it is a first-class invariant.

### Axioms (documented model assumptions)

- **A1 (capability):** `hold_id` is unguessable and disclosed to the client ONLY
  in the step-2 success response. Holding a `hold_id` therefore implies the debit
  committed — which is why `settle`/`release` (which have the id) may act on a
  PENDING hold, while the sweeper (which does not) must not credit. The
  implementation must keep `hold_id` from leaking into the settle path via logs,
  GSIs, or error messages.
- **A2 (single-item serialization):** DynamoDB evaluates a single-item conditional
  write atomically and serializes concurrent conditional writes to the same item.
  The Hypothesis fake must reproduce this faithfully (condition+write atomicity,
  serialized concurrent conditional writes); the fake is itself a review target,
  because if it is loose here the whole proof is vacuous.
- **A3 (failure taxonomy):** step 2 outcomes classify as definitive-fail
  (ConditionalCheckFailed; throttle-before-send) vs ambiguous (timeout, 5xx). A
  Hypothesis rule adversarially mis-classifies (ambiguous read as definitive) and
  the model must show a mis-classification falls to the leak-safe side.

## The reconciliation job (aggregate recovery only)

Authority for admission is ALWAYS step 2's conditional headroom check; the
reconciler only recovers leaks after the fact and is NEVER an admission authority.
Three precision requirements without which the reconciler itself becomes an
oversell source:

- **(a) Read order:** compute `drift = pool_reserved − Σ entitled` by reading the
  **counter FIRST, the hold set SECOND**. The snapshots are not atomic; reading
  holds first lets a reserve that commits in between appear only in the counter,
  overestimating drift → over-credit → oversell. Counter-first underestimates
  drift → leak-safe. This order is a spec requirement and a Z3 lemma.
- **(b) Hysteresis — defer while any PENDING is in flight (sharpened by the
  formal model).** A PENDING hold may be debited (committed, awaiting activate) OR
  undebited (rejected / ambiguous-lost, awaiting fence), and the reconciler cannot
  tell which. Counting it as entitled can hide a real leak (drift too low);
  NOT counting it can oversell a live reserve (drift too high). The stateful model
  produced a concrete counterexample where an undebited PENDING coexisting with a
  debited EXPIRED_UNCREDITED made the naive aggregate flip a real leak to RECLAIMED
  without crediting it (a silent I1' break). The resolution: **recovery runs only
  when no PENDING is in flight** — defer until the confounding PENDINGs drain to
  ACTIVE (always debited) or EXPIRED_UNCREDITED (the leak candidates). With no
  PENDING present, `drift = counter − Σ(ACTIVE)` is EXACTLY the debited leak, and
  a negative drift there is impossible under I1' (it is raised as a model bug, not
  silently absorbed). Operationally this is the ≥ 2-scan spacing (longer than
  `PENDING timeout + step-3 retry horizon`) — by the second scan the in-flight
  PENDINGs of the first have resolved.
- **(c) Idempotent, atomic recovery:** this is a COLD path, so a transaction is
  allowed here (the ban was hot-path only). Credit the aggregate drift and flip
  the covered `EXPIRED_UNCREDITED → RECLAIMED` in one conditional
  `TransactWriteItems`; beyond 25 items, idempotency via an epoch-stamped RECON
  ledger item. Recovery is always aggregate — per-hold "was it really debited" stays
  forever unknown; RECLAIMED means "settled in aggregate".

## Implementation sequencing (readers first, writer last)

The naive order (schema → new writer → sweeper) is an oversell hazard: the moment
a PENDING hold exists while the reaper still means "exists = active", the reaper
credits back a maybe-undebited PENDING and oversells. Correct order:

1. **Teach every READER the `status` semantics (absent = ACTIVE).** reaper credit
   condition becomes `status = ACTIVE OR attribute_not_exists(status)`; settle /
   release / reconciler likewise. No PENDING exists yet, so this is a verifiable
   no-op deploy.
2. **Deploy the sweeper fence** (`PENDING → EXPIRED_UNCREDITED`, pool untouched),
   spinning over zero targets.
3. **Deploy the reconciler** and confirm `drift ≈ 0` on existing data — calibrate
   the detector's baseline BEFORE any new writer (a non-zero drift here is itself a
   pre-existing-bug find).
4. **Only then** flip the feature flag to the 3-write reserve, per-tenant canary.

Notes:
- The sweeper and reconciler are **flag-independent** — flipping a canary tenant
  back orphans its in-flight PENDINGs, whose recovery must keep running regardless.
- **No migration of existing holds** — holds are short-lived; let them drain under
  the absent = ACTIVE rule. A mass backfill is write-amplification for no gain.
- **Terminal cleanup:** the expiry-embedded SK range scan will keep hitting
  EXPIRED_UNCREDITED / RECLAIMED rows; TTL them after RECLAIMED (TTL longer than
  the reconciler hysteresis horizon) or add a status filter to the scan.
- **Clock skew:** the sweeper's timeout is local-clock based (DynamoDB has no
  server time); add a writer/sweeper skew margin to the timeout. Pick ONE
  authority between SK-embedded expiry and status-based judgement.

## Idempotency anchor (moved off ClientRequestToken)

Dropping `TransactWriteItems` loses its 10-minute idempotency token, so a blind
SDK retry of `ADD reserved` on a timeout would double-debit. Anchor: derive
`hold_id` deterministically from the Idempotency-Key; step 1's
`attribute_not_exists(sk)` is then also the duplicate-Key detector. A same-Key
replay after success returns the same hold and result; an ambiguous-failure retry
mints a fresh hold and the IDEMP record points at the committed one. This is I6;
"do not re-send the debit" is only its other half.

## Observability (part of correctness for a leak-safe design)

A leak-safe design is "healthy as long as the leak is observed", so these are not
optional: ambiguous-failure rate, fence count, a **drift gauge with a rate-of-
increase alarm**, step-3 retry depth, cumulative RECLAIMED. Plus the I-biz alarm
(step 3 hitting an already-EXPIRED row).

## Still to measure before production (not yet claimed)

Carried from the spike review: SDK-level throttle/retry visibility (CloudWatch
`WriteThrottleEvents` + boto3 `RetryAttempts`) to confirm the c=64 mechanism;
open-loop (arrival-rate) load at target writes/s to avoid coordinated-omission
bias; read+write mixed load on the hot row; the pool-counter shard-N sweep
(N=2/4/8 × c=16/64) with near-exhaustion condition-fail behaviour; the sweeper
running concurrently under load.
