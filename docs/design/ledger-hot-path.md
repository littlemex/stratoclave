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
`attribute_not_exists` — the HOLD row IS the idempotency row. So the synchronous
transaction shrinks from four items to **two: (1) headroom ADD+condition,
(2) HOLD put (attribute_not_exists)**.

The RESERVE ledger event goes **asynchronous, derived from DynamoDB Streams** —
not written concurrently by the app. Committed writes to the pool and HOLD rows
flow to Streams (at-least-once, per-key ordered); a Lambda derives each event
deterministically and writes it with `event_id = (hold_id, transition)` under
`attribute_not_exists`. Zero-double-posting is preserved by the idempotent event
Put; "the ledger is a complete image of committed state" is preserved by Streams
delivery. What breaks is only "the event exists at the same instant as the
commit" — which is neither an audit requirement nor a Z3 invariant. It is in
fact *better*: the event becomes a derivation of the state change, so
state/event divergence cannot occur. Audit lemma: "every pool/HOLD transition
has a corresponding event eventually exactly-once."

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
- **Then: two-item transaction + Streams-derived events** for the floor tail →
  ledger-op p99 ≈ 40 ms; PENDING protocol after that if sub-30 ms is required.

Contention collapse is a customer-visible failure now; the floor overshoot is a
debt the next step retires. This order is fixed.
