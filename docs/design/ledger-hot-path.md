<!-- Last updated: 2026-07-19 -->
<!-- Applies to: Stratoclave main -->

# Design: a hot-path-fast, multi-tenant credit ledger

This is the design that closes the gap the [ledger-latency benchmark](../benchmarks/ledger-latency.md)
measured: the reserve/authorize p99 misses the < 50 ms target, and a single
tenant's pool row collapses under concurrency. The benchmark isolated **two
distinct causes**, and this design addresses them separately without weakening
any proven invariant.

## The two causes (measured, not assumed)

1. **Contention collapse (the urgent one).** The benchmark showed that with
   concurrency on ONE tenant pool row, end-to-end authorize p99 went 225 ms
   (c=1) → 1,953 ms (c=2) → 6,512 ms (c=16, 6.2% errors), while the **server-side
   `TransactWriteItems` p99 barely moved (58 → 51 ms)**. So the collapse is not
   DynamoDB — it is the application's **snapshot-all-equal optimistic CAS**: the
   reserve pre-reads `pool_reserved` + `pool_settled`, and its condition commits
   only if BOTH are unchanged. When N requests race one row, exactly one wins and
   the other N−1 all fail the condition, re-read, and retry with full-jitter
   backoff. That is an application-level serialization bottleneck.

2. **Transaction tail (the residual one).** Even at zero contention the ledger
   `TransactWriteItems` is p99 = 58 ms — the intrinsic tail of a four-item
   two-phase commit (pool CAS + HOLD put + RESERVE ledger event + IDEMP row).
   Fewer items shrinks it; it cannot be tuned below the transaction floor.

## The core change: snapshot-CAS → headroom conditional ADD

Give the pool row a single counter `pool_headroom_microusd = limit − reserved −
settled` and make a reserve one operation:

```
UpdateItem:
  UpdateExpression:    ADD pool_headroom_microusd :neg_amt
  ConditionExpression: pool_headroom_microusd >= :amt AND #status = :active
```

Why headroom, not a `used` counter with a computed ceiling: DynamoDB's
`ConditionExpression` cannot do cross-attribute arithmetic
(`reserved + settled <= limit - :amt`). A `used` counter would force the caller
to read `limit` first (re-introducing the pre-read and a stale-limit risk). A
headroom counter is **self-contained in one attribute** — no pre-read at all.

What this changes qualitatively:

- **The snapshot-invalidation storm disappears.** The old snapshot CAS made every
  concurrent reserve on a hot row invalidate the others' read-back snapshot, so a
  burst produced a `ConditionalCheckFailed` storm (the measured collapse). The
  headroom condition references only the counter being mutated, so a concurrent
  reserve that still fits does NOT fail it. A pool-item `ConditionalCheckFailed`
  now means the budget is genuinely exhausted — a legitimate 402, not a retry.
  IMPORTANT nuance (Fable review finding 1): the reserve item is composed into a
  `TransactWriteItems` with the HOLD put and the per-user debit, so two reserves
  on the SAME pool row can still collide at the transaction layer and one is
  cancelled with reason `TransactionConflict`. The caller therefore STILL runs a
  bounded retry loop — but it now retries ONLY on `TransactionConflict`/throttling
  (rarer, self-clearing) and never on a pool `ConditionalCheckFailed`. So this
  removes the snapshot-storm; it does not make reserve unconditionally
  retry-free. The projected p99 win below must be MEASURED, not assumed.
- **The pre-read GetItem is gone**, which also trims ~1 round-trip off the
  end-to-end latency (part of cause 2's residual).

Counterpart operations:

- `settle` (unconditional ADD, no retry — a settle must never fail a live
  request): return the hold's reserved amount and remove the actual spend →
  `headroom += (reserved_amount − actual)`.
- `release` / `reclaim` (unconditional ADD): return the hold's full amount →
  `headroom += amount`.
- `set_pool_limit` (raise/lower) is NOT an unconditional ADD (Fable review
  finding 3): it is a **conditional delta-CAS** — `SET pool_limit = :new
  ADD pool_headroom (:new − :old)` guarded by `pool_limit = :old`, with a small
  bounded retry on `ConditionalCheckFailed` (a competing admin write). This
  shifts headroom by the ceiling delta without ever rewriting it from a stale
  read, so a concurrent reserve's headroom ADD composes with it instead of being
  clobbered. First creation is an `attribute_not_exists(tenant_id)` seed; a
  legacy row with no headroom is repaired via the `reconcile_headroom` CAS. A
  lower limit can drive headroom negative, at which point new admissions are all
  correctly refused — the right hard-budget behaviour.

`pool_reserved` / `pool_settled` are kept as unconditional-ADD **mirrors** so the
existing read API and the audit reconciliation (`headroom == limit − reserved −
settled`) still work.

### Why sharding is NOT used

Splitting the pool into N sub-counters would need either a cross-shard headroom
borrowing protocol (which complicates the proven state machine badly) or
accepting false rejections near the boundary (which corrupts hard-budget
accuracy). And the measured collapse was application CAS, not DynamoDB per-item
contention — so sharding solves a problem the data says we do not have. Revisit
only if a single tenant sustains > 500 TPS; then `N = ceil(peak_tps / 250)` with
a static `limit/N` per shard and a monitored false-rejection SLO.

### Invariants (the proof gets simpler)

The Z3 obligation "at admission, `reserved + settled ≤ limit`" becomes "`headroom
≥ 0` is preserved at admission". The reserve is now a **single serialized
transition** — "if `headroom ≥ amt` then `headroom −= amt`" — instead of a
read-then-conditional-write whose interleavings all had to be modelled. That is
strictly easier to prove. Add the reconciliation lemma `headroom = limit −
reserved − settled` against the mirror counters. Zero-double-posting,
settle-once, reclaim-once, and retry-vs-replay are unchanged (they live in the
HOLD/IDEMP items, below).

## Shrinking the transaction tail (cause 2)

From the invariants, the only things that MUST be synchronous are: (i) the
headroom decrement (the hard-budget gate), (ii) the idempotency verdict
(retry vs replay), (iii) the HOLD existence (the settle/release/reclaim target,
double-reserve guard). **(ii) collapses into (iii):** derive the HOLD row's key
deterministically from the idempotency key and Put it with
`attribute_not_exists` — the HOLD row IS the idempotency row.

### HOLD is promoted to the synchronous source of truth (Fable design)

A subtlety the first sketch missed: the external-authorize **capture/void path
reads the RESERVE ledger event synchronously** — both as the C-1 security gate
(`source == "external"`; an inline LLM hold's token must not be capturable) and
to rehydrate the reservation (amount / description / rate_snapshot). So the
RESERVE event cannot simply go async without a capture-right-after-authorize race
producing a false 404 or a C-1 bypass window.

Resolution: **fold `source` / `amount` / `description` / `rate_snapshot` /
`payload_hash` into the HOLD row itself** (written in the same synchronous txn,
under `attribute_not_exists`). capture/void then reads ONLY the HOLD row — which
is synchronously durable at authorize time — so the RESERVE event can become a
pure async audit projection. C-1 becomes `hold.source == "external"`, **default
DENY on a missing attribute** (fail-closed for legacy/unwritten rows). During
migration, capture/void dual-reads (HOLD first, fall back to `get_reserve`) until
the pre-cutover holds' max TTL elapses.

**Synchronous txn — final shape (per path):**

- **External authorize (B):** 2 items = `[pool headroom ADD + condition,
  HOLD Put (attribute_not_exists, carrying the full context, doubling as the
  idempotency row)]`.
- **Inline LLM hold (A):** honestly **3 items**, not 2 = `[per-user debit
  (condition), pool headroom ADD, HOLD Put]` (+ optional quota slice). Async-ing
  the per-user debit *would* reach 2 items but admits per-user budget overrun in
  the lag window. The per-user row is a **low-contention per-user partition**;
  the tail is dominated by the *contended* item (the pool row) lock-hold time, so
  we measure at 3 items first and only async the debit if that is proven
  insufficient. The failure-interpretation map is preserved (pool CCF = 402,
  quota CCF = quota-exhausted, HOLD CCF = idempotent replay, TransactionConflict
  = retry).

### RESERVE event becomes an async audit projection

The RESERVE ledger event goes **asynchronous, derived from DynamoDB Streams** —
not written concurrently by the app. Events are derived from the **HOLD item's
stream records only** (NEW_AND_OLD_IMAGES): INSERT → RESERVE, MODIFY to a
terminal status → SETTLE/VOID/EXPIRE. DynamoDB Streams guarantees per-partition
(per-hold) record order, so a hold's SETTLE record can never arrive before its
RESERVE record. A Lambda writes each event with `event_id = (hold_id, transition)`
under `attribute_not_exists` (idempotent under at-least-once delivery), with
**deterministic content** (no `now()`/random in the Lambda, so a retry can never
produce same-id-different-body). Two additional holes are closed: (1) failed
records use `ReportBatchItemFailures` + a DLQ (a permanent projector bug halts
the audit shard but never touches billing); (2) each terminal event Put is gated
by a `ConditionCheck` on the RESERVE event's existence, so a missing predecessor
is *detected*, not silently reordered.

**I2 is split; authority is the synchronous side:**

- **I2-sync (always holds):** `pool_reserved == Σ(active HOLD.amount)`. The
  synchronous txn updates the pool and HOLD atomically, so this holds at every
  instant and is the sole basis for the billing decision.
- **I2-async (eventually holds):** once Streams drains, `pool_reserved ==
  Σ RESERVE.reserved_delta − Σ terminal returned`. A scheduled reconciler checks
  it and pages only when drift > 0 while IteratorAge is below threshold (drained
  yet divergent). A projector bug is an audit-lemma violation, not a billing
  error, and is repaired by re-projecting from the HOLD rows. **The discipline:
  never reconstruct state from events** — events are a projection of state, not
  its source.

Final option (only if two-item transact still can't beat 50 ms): drop the
transaction entirely — Put HOLD `status=PENDING` (idempotency gate) → headroom
ADD (budget gate) → update HOLD to ACTIVE. A crash leaves a PENDING orphan that
is NOT counted in reserved and cannot be settled (settle requires ACTIVE), so
invariants stay intact; a sweeper or a same-idempotency-key retry resumes/denies
deterministically. Two sequential single-item writes should hit p99 ≈ 25–35 ms.
This adds a PENDING state to the proof model, so it is the **next** step after
measuring the two-item transaction — not the first.

While a transaction remains, keep `ClientRequestToken` for DynamoDB's 10-minute
idempotency window too. Write **hedging is forbidden** (the headroom ADD is not
idempotent).

## Pre-authorization stays hard

"Stop before spend" is guaranteed by exactly one thing: the conditional headroom
decrement. That stays synchronous. Everything else (full HOLD state, ledger
event, projections) may be eventually consistent. Budget *leases* /
client-side token buckets are rejected: they always produce either under-admission
(false rejects) or over-admission (soft-limit = defeat), and add a
time-dependent lease-expiry state machine to the proof. Per SCOPE.md, the
minimal and strongest form of a provable hard budget is one synchronous
conditional write. Speed comes from the condensation above, not from loosening
synchrony.

## Multi-tenant targets (the load-test pass criteria)

- **Per tenant: sustained 300 TPS (burst 500) at ledger-op p99 < 50 ms,
  end-to-end authorize p99 < 100 ms.**
- **1,000+ concurrently-active tenants.** Tenants have distinct PKs, so they
  scale independently at the DynamoDB partition level; PAY_PER_REQUEST reaches
  tens of thousands of TPS table-wide with no design change.
- **Single-tenant contention: c=16 p99 within 1.5× of c=1** (vs the old ~29×,
  225 ms → 6.5 s). Retries are gone, so this is achievable by construction and
  becomes the load-test acceptance gate.

## Migration (never letting go of an invariant)

1. **Backfill / reconcile (value-repairing, not presence-gated).** Set
   `pool_headroom = limit − reserved − settled` on every pool row via
   `TenantBudgetsRepository.reconcile_headroom`. It keys on the VALUE, not the
   attribute's presence: during a rolling deploy a new-code `settle` can fire on
   a not-yet-reconciled row and its unconditional `ADD pool_headroom` CREATES the
   attribute at a WRONG value (`reserved − actual`). A presence-gated backfill
   would then skip that row forever. reconcile instead recomputes the invariant
   from the always-correct `reserved`/`settled` mirrors and repairs any row whose
   stored headroom differs, under a CAS (`attribute_not_exists(pool_headroom) OR
   pool_headroom = :observed`) so a concurrent reserve/settle is never clobbered.
   It is safe to run live and to re-run any number of times (idempotent by
   value). A Streams validator Lambda audits `headroom == limit − reserved −
   settled` on every write. (Fable review findings 2 + 4.)
2. **Cut over reserve** to headroom ADD+condition behind a per-tenant flag
   (settle/release/reclaim return headroom in the same transaction). Delete the
   old snapshot condition. Contention collapse disappears here.
3. **Fold IDEMP into HOLD** (write both during migration, read either, then stop
   the IDEMP write).
4. **Streams→ledger-writer in shadow**, reconcile against the synchronous event
   writes until divergence is zero, then stop the synchronous event write and
   make the transaction two-item.

Each step updates the Z3 model and proves the intermediate (dual-write) lemmas
before proceeding.

## Sequencing

- **Next (single highest-leverage): the headroom ADD cut-over (step 2 core).**
  PROJECTED (to be confirmed by re-running the c=16 contention benchmark, NOT yet
  measured): the snapshot-invalidation storm is removed, so c=16 e2e p99 should
  fall sharply from 6,512 ms and the snapshot-CAS error rate (6.2%) should drop to
  near-zero true-exhaustion-only. Residual `TransactionConflict` retries on a hot
  single row remain (see finding 1), so the post-fix p99 is an empirical question
  — do not quote a specific number until the bench confirms it. The floor
  p99 = 58 ms does NOT move with this one change.
- **DONE: the headroom ADD cut-over.** Measured (see
  [../benchmarks/ledger-latency.md](../benchmarks/ledger-latency.md)): contention
  error rate 6.2% → 0% at c=16, c=16 e2e p99 6,512 → 4,508 ms. The storm is gone;
  single-row p99 still misses target (the residual `TransactionConflict` on one
  hot pool row), which the next step targets.
- **Now: two-item transaction (HOLD promoted) + Streams-derived events.**

### Two-item migration (each step rollback-safe, gated on divergence = 0)

1. **Streams + shadow projector + reconciler.** Enable Streams
   (NEW_AND_OLD_IMAGES); the projector Lambda writes events under a `SHADOW#`
   prefix. A reconciler compares shadow vs the still-synchronous RESERVE event
   and asserts divergence = 0 over an observation window. Rollback: disable the
   event-source mapping. (No hot-path change — pure observability.)
   **DEPLOYED + live-verified** (scverify, us-east-1): an enriched HOLD written to
   the budgets table projects to a shadow RESERVE in ~5s and the reconciler
   reports divergence 0. Operational requirements learned from that verification,
   now encoded in the stack: (a) the Lambda MUST receive `DYNAMODB_CREDIT_LEDGER_
   TABLE` / `DYNAMODB_TENANT_BUDGETS_TABLE` — the code's fallback prefix is
   `stratoclave-`, wrong for a `scverify-` deploy, and an unset env silently drops
   every event into a non-existent table; (b) the reconciler MUST be given
   `PROJECTOR_EPOCH_MS` = the projector's go-live time, because the stream starts
   at LATEST and the pre-existing RESERVE backlog has no shadow by construction —
   without the epoch the gate reads that backlog as permanent divergence and never
   goes green.
2. **HOLD enrichment dual-write + capture/void dual-read.** The synchronous txn
   (still old item count) additionally writes `source` / `amount` /
   `rate_snapshot` / `payload_hash` onto the HOLD row. capture/void dual-reads
   (HOLD first, fall back to `get_reserve`, log any mismatch). Rollback: stop
   writing the extra attributes.
3. **capture/void → HOLD-only** (requires mismatch = 0). Verify the
   authorize-then-immediate-capture race and inline-token capture rejection.
   **PRECONDITION (Fable review-2 finding 2, a hard blocker): a blunt env-flag
   flip is a data-loss hazard** — at flip time any pre-enrichment external hold
   still within its TTL is authorized + reserved but its capture 404s, so the
   caller declines and the reaper voids an authorized transaction. The cut-over
   MUST therefore be gated one of two ways, NOT a bare flag:
   (a) a `created_at >= ENRICHMENT_EPOCH` predicate so only holds minted after
   enrichment take the HOLD-only path and older holds keep the RESERVE-event
   fallback (safe by construction; the fallback code is deleted only after
   ENRICHMENT_EPOCH + max-hold-TTL); or (b) a reconciler gate that scans the HOLD
   table and confirms zero rows with `attribute_not_exists(source)` before the
   flag is allowed to flip. (a) is preferred. The current `STRATOCLAVE_CAPTURE_
   HOLD_ONLY` env flag is the mechanism, but it must not be flipped in prod until
   (a) or (b) is in place. Rollback: flip the flag / relax the predicate.
4. **Promote the projector to the real `event_id`** (conditional Put). The
   synchronous writer and the projector now both target the same event under
   `attribute_not_exists`, so the dual-writer state is safe in both directions —
   this is the core of rollback-safety.
5. **Remove the synchronous RESERVE item from the txn** (per-tenant canary,
   guarded by the I2-async reconciler). Re-measure the c=16 curve. Rollback: put
   the item back (safe by step 4's idempotent-Put property).
   **PRECONDITION (Fable review-2 finding 5, a hard blocker): the RESERVE event's
   derivation source (the HOLD row) is deleted on settle/void, and Streams
   retention is 24h.** If the projector is disabled/broken for > 24h, a
   since-settled hold's RESERVE event becomes permanently underivable — a SETTLE
   orphan with no RESERVE. Before removing the synchronous item, one of these MUST
   exist: (a) a repair job that re-derives a missing RESERVE from the terminal
   event (the synchronous SETTLE/VOID carries amount + run_id), or (b) self-heal:
   terminal processing Puts the RESERVE if absent. And: **the event-derived
   `reserved = ΣRESERVE − Σterminal` is eventually-consistent and MUST NOT be used
   for any admission/refund decision** — the synchronous pool `reserved` mirror is
   the sole authority. This invariant is documented in `dynamo/credit_ledger.py`
   and enforced by the reconciler's continuous pool-mirror cross-check.
6. **Fold IDEMP into the HOLD deterministic key** (write both, verify replay on
   HOLD, then drop the IDEMP item; old-period IDEMP rows remain a read-only
   fallback until they expire).

Between steps 3 and 5, a **TLA+/P (or Z3 stateful) model** checks: I2-sync always
holds, I2-async eventually holds, and C-1 holds — with SETTLE-before-RESERVE,
projector lag, and double-delivery adversarially injected.

### IaC components

DynamoDB Streams (NEW_AND_OLD_IMAGES); Lambda event-source mapping
(`ReportBatchItemFailures`, `ParallelizationFactor`, `MaximumRetryAttempts`,
`OnFailure` = SQS DLQ); projector Lambda + reconciler Lambda (EventBridge
schedule); a sparse GSI for the expiry index (and a hold_id index if needed once
the HOLD key becomes idempotency-derived); a feature flag (AppConfig or env);
CloudWatch on IteratorAge, DLQ depth, divergence count, TransactionConflict/CCF
breakdown, and an I2-drift alarm.

### If two items still miss target → PENDING protocol

Trigger (measured): if after two-item cut-over (i) p99 > 100 ms AND (ii) ≥2
`TransactionConflict` retries occur on > 1% of txns (or the pool row nears the
per-partition write ceiling), move to the PENDING protocol — Put HOLD `PENDING`
(uncontended) → a SINGLE conditional `UpdateItem` on the pool row (ADD reserved,
condition headroom, NO transaction = minimal lock window) → mark HOLD `ACTIVE`.
The crash window (PENDING orphan, pool incremented but not ACTIVE) is closed by a
compensating sweeper. This trades atomicity for dropping the contended write to a
single non-transactional item, and requires formal verification of the
compensation logic first.

Contention collapse was a customer-visible failure (now fixed); the floor tail is
the debt this step retires. This order is fixed.
