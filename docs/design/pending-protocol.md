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

**Implementation status (2026-07-20): IMPLEMENTED with the MARKER design, flag-gated
OFF.** Behind `STRATOCLAVE_RESERVE_PROTOCOL` (default `transaction` = today's path,
byte-for-byte unchanged; `pending` = this protocol). A first cut shipped with a
"CCF = replay success" branch and an aggregate-drift reconciler; a Fable money-bug
review + a test-gap audit found oversell / fail-open defects and a flag-on
capture/void/get 404 (the C-1 gate read a RESERVE event the pending path never
writes). The shipped design is the corrected **marker** design:

- **Per-hold marker.** The pool item carries an `applied.<hold_id>` map; the debit
  and the marker are written in ONE conditional `UpdateItem`
  (`pool_reserve_update`) — multi-attribute updates to one item are atomic, so it
  is still a single non-transactional write (the ~88 ms p99 is kept). The marker
  makes "did this hold's debit commit?" a decisive, locally-readable fact (A1
  without a transaction). CCF is resolved by `ALL_OLD`: marker present ⇒ idempotent
  success (no double-debit), absent ⇒ genuine exhaustion (402). The write is
  idempotent, so an SDK auto-retry is harmless.
- **Exactly-once credit-back.** `pool_credit_back` = REMOVE marker + ADD headroom,
  guarded on `attribute_exists(applied.<hold_id>)`; settle/release/reaper REMOVE
  the marker in the same write that moves the counters. Double credit is
  structurally impossible.
- **IDEMP intent (step 0)** persists `hold_sk`/`amount`/`authorization_id`, so a
  duplicate-key replay returns durable addressing and resolves by READING state
  (marker + hold status) — never by assuming success.
- **capture/void/get is HOLD-first** (`_require_external` reads the HOLD's own
  `source`; RESERVE-event fallback only for legacy) — fixes the flag-on 404.
  Capture helps a PENDING hold to ACTIVE via CAS only after confirming the marker.
- **Reconciler is per-hold marker-driven** (credit iff marker present, then retire
  the row) — replaces the aggregate-drift reconciler that livelocked on hot pools.

Code: `dynamo/tenant_budgets.py` (`hold_put_pending`, `pool_reserve_update`,
`pool_credit_back`, `pool_marker_amount`, `ensure_applied_map`, `hold_activate`,
`fence_pending_expired`, status transitions); `dynamo/credit_ledger.py`
(`put_idemp_intent` + finalizers); `mvp/_pipeline.py` (`_reserve_external_pending`,
`_pending_replay_result`, `sweep_fence_pending`, `reconcile_pool`);
`mvp/billing_authorize.py` (HOLD-first `_require_external`). Readers were made
status-aware FIRST (reaper credits only `status = ACTIVE OR attribute_not_exists(
status)`), so the path is inert until the flag is flipped per-tenant. The
write-discipline guard records the deliberate non-transactional counter writes
(`pool_reserve_update`, `pool_credit_back`) as marker-proof-backed exceptions to
axiom A2, still requiring a ConditionExpression on each. The formal model
(`billing/pending_protocol.py` + `test_pending_protocol_*`) promotes the old ghost
`_debited` to the real observable marker and proves exactly-once credit-back.
What remains is NOT code — it is the live per-tenant canary flip, gated on the
observation windows in "Still to measure" below.

### MEASURED 2026-07-20: marker-in-the-hot-item is REJECTED (item-growth blowup)

The step-0c spike (`bench/ledger-latency/bench_marker_shard_spike.py`) measured
the production-faithful marker commit on live DynamoDB (a verification env,
on-demand table),
sharded N=1/2/4/8, 3000 sequential + 3000 concurrent @ c=16.

**Measured facts** (errors=0 throughout):

| shard N | floor(c=1) p99 | c16 p50 | c16 p99 | throttle retries |
|---|---|---|---|---|
| 1 | 185 ms | 399 ms | 27,285 ms | 122 |
| 2 | 74 ms | 182 ms | 25,720 ms | 38 |
| 4 | 67 ms | 75 ms | 25,647 ms | 23 |
| 8 | 64 ms | 67 ms | 6,413 ms | 3 |

For contrast, step-0b's marker-FREE single `UpdateItem` was floor p99 = 8.6 ms,
c16 p99 = 88 ms.

**Discriminating experiment (zero new runs, from the shard1 floor CSV):** the
c=1 (no concurrency) latency rises MONOTONICALLY across the run — mean by quintile
10.6 → 31.4 → 51.9 → 72.4 → 92.9 ms. With no concurrency this cannot be
throttling or a cold partition; it is **`applied` map growth**. Each reserve adds
a marker to the SINGLE pool item; DynamoDB's write WCU is proportional to the
POST-update item size, so as the map grows to hundreds of KB, every 1-byte debit
costs ~150 WCU and the c=1 path alone approaches the ~1000 WCU/s single-partition
ceiling. This is a **positive feedback that degrades super-linearly with load** —
the harder the hot tenant is used, the faster its pool item bloats. Sharding only
divides the growth rate by N; it does not remove it.

**What this table CAN and CANNOT be used for (the honest boundary):** it rejects
the *marker-in-the-hot-item placement*, NOT the marker CONCEPT and NOT the 50 ms
target. The c16 p99 figures are additionally NOT a clean server metric — they mix
backoff accumulation, possible boto3 internal retries, and a 16-thread client
against boto3's default 10-connection pool (client-side queueing). They are a
symptom of the design flaw, not a number to tune against.

**Corrected design (IMPLEMENTED, PR-1): marker in a SEPARATE item via
`TransactWriteItems`.** Pool conditional debit + a marker item Put
(`SK=MARKER#<hold_id>`, `attribute_not_exists`) in ONE transaction. Atomicity
preserved; the marker item is FIXED-SIZE (no map growth) and O(1) per operation
regardless of table size — the structural fix for the item-growth blowup. It stays
on the tenant partition (SK-scoped), which is what kills the growth; the
single-partition WCU ceiling remains bounded by the pool item itself and is a
SEPARATE concern deferred to a future sharded-pool PR (Fable Q1: moving markers to
their own `PK=hold_id` table would gain only ~2× and not touch the real ceiling).
Cost ~2 WCU/item + the transaction tax (warm p50 ≈ 10–20 ms — within the p50
target), predictable instead of "fast then dies". This supersedes the shipped
marker-in-pool-item map. Confirmed with Fable; the PR-1 scope, as landed:

1. `reserve` → `TransactWriteItems([pool conditional debit, marker Put(
   SK=MARKER#<hold_id>, attribute_not_exists)])`
   (`dynamo.tenant_budgets.reserve_commit_txn_items`, executed by
   `mvp._pipeline._pending_commit_transact`). NO `ClientRequestToken` — the marker's
   `attribute_not_exists` is the idempotency guarantee (Fable Q4-item-1).
2. **`CancellationReasons` branch (done):** on `TransactionCanceledException`,
   inspect the MARKER reason FIRST — marker-side CCF ⇒ already applied ⇒ SUCCESS
   (`RESERVE_ALREADY`, idempotent); else pool-side CCF ⇒ insufficient budget ⇒
   `RESERVE_EXHAUSTED` (402). Reversing the order would 402 a hold whose debit
   already landed → re-reserve under a new id → double debit. An ambiguous
   timeout/conflict re-reads the marker (ConsistentRead) BEFORE responding, never
   deferring to the reconciler (Fable Q4-item-2/3).
3. **TTL race fix (done):** no TTL at marker creation (an active hold's marker must
   never expire and let a retry pass `attribute_not_exists` → double-debit). The
   marker is transitioned `RESERVED → SETTLED` + `ttl = now + reconcile-window + 7d`
   only at the settle/void/reconcile terminal — and NOT deleted, so it still dedupes
   a late reserve retry until TTL. Credit-back is a phase CAS (`marker_phase =
   RESERVED`) paired transactionally with the pool return, so it is exactly-once
   (proven in `test_pending_protocol_z3.py::test_phase_gated_credit_back_is_exactly_once`).
4. all deletion timers derive from one shared `_RECONCILE_WINDOW_SECONDS +
   _MARKER_TTL_MARGIN_SECONDS` constant (`dynamo.tenant_budgets`).
5. a reconcile AUDIT SWEEP settles RESERVED markers whose hold row is gone (a lost
   best-effort settle) WITHOUT crediting the pool — the storage-orphan safety net
   (Fable Q2 hole 3, `reconcile_pool` → `list_reserved_markers`).
6. groundwork landed now (does NOT wait for PR-2): PITR enabled on the
   tenant-budgets table (ledger already had it) + native TTL attribute `ttl` for
   marker GC; a UTC `event_day` attribute stamped on every new ledger write (the
   future Parquet partition key — back-filling later is painful).
7. REMAINING before canary: re-benchmark at **2× provisioned WCU** — FIRST prove
   `c=1 × 3000` latency is FLAT (Q1 ≈ Q5, the direct proof the growth bug is gone),
   THEN the concurrency/shard sweep; and a `pool item size` metric (must stay flat).

**Fable adversarial code-review (PR-1 diff), resolved before merge.** Four money
bugs were found and fixed, each with a regression test:
  - *credit-back cancel-reason conflation* — `pool_credit_back` returned `False`
    (="already credited") for ALL `TransactionCanceledException`s, so a transient
    `TransactionConflict`/throttle looked definitive and the reconciler retired the
    hold → stranded RESERVED marker → permanent leak. Fixed: inspect
    `CancellationReasons`, `False` only on the MARKER-index CCF, else raise; the
    reconcile loop `continue`s (no retire) on a raise so the next pass retries.
  - *audit-sweep live-hold settle* — the sweep keyed "is it live?" on the
    EXPIRED_UNCREDITED snapshot, so a PENDING/ACTIVE hold's marker was wrongly
    SETTLED, killing its later credit-back. Fixed: settle only markers with NO hold
    of ANY status (`hold_exists_by_id`, fully paginated) AND older than
    max-hold-TTL+margin (fail-closed on unparseable `created_at`).
  - *cross-period leak* — `list_reserved_markers` returns every period's markers,
    but the existence checks are period-scoped, so a prior period's live hold's
    marker was settled by the current period's reconcile. Fixed: the sweep acts only
    on markers whose stamped `period` == this reconcile's period (fail-closed on a
    missing period); each period's own pass handles its markers.
  - *pool_credit_back period cross-check* — defensive `ValueError` if the marker's
    `period` disagrees with the caller's, surfaced as an alarm (poison-item safe:
    the reconcile loop logs + skips without retiring).

Follow-up (non-blocking, tracked for a later PR):
  - markers older than `previous_period` are settled by no reconcile pass
    (money-neutral — headroom was already returned — but a lingering RESERVED
    storage orphan that never gets TTL);
  - **`vsrEnv` rename** (Fable E-phase review): the ECS env map named `vsrEnv` now
    also carries the reserve-canary var — rename to a generic `extraEnv`;
  - **`PoolItemSizeBytes` alarm notification-repeat** (Fable E-phase review Bug-1
    note): the gauge is emitted only once per reconcile (SPARSE), so with 1/1 +
    `treatMissingData=NOT_BREACHING` a sustained over-threshold can flap
    ALARM→OK→ALARM and re-notify each reconcile. If it proves noisy in the canary,
    switch that alarm's missing-data handling to `MISSING` (keeps the state sticky
    between sparse datapoints). Left 1/1 for now — a re-notifying alarm is far
    better than the original 3/3 alarm that could never fire at all.

**Canary rollout (Shadow → Canary → Full), landed.** The reserve protocol is
resolved per tenant by `mvp._pipeline._reserve_protocol_for(tenant_id)`: it returns
"pending" iff the global `STRATOCLAVE_RESERVE_PROTOCOL=pending` OR the tenant is in
the `STRATOCLAVE_RESERVE_PROTOCOL_TENANTS` allowlist (parsed once at import — no
per-request I/O). Every reserve / settle / release / reclaim / capture marker
branch consults this SAME resolver, so a canary tenant is byte-consistent across
its whole lifecycle (marker written on reserve ⇒ cleaned up on settle even if the
global flag never flips; marker cleanup is money-neutral when no marker exists, so
removing a tenant from the allowlist mid-flight still settles cleanly). CDK wires
it via `EcsStackProps.reserveProtocolCanaryTenants` (dark by default — no env var
until set). Graduation ladder:
  1. **Shadow/Canary**: add ONE low-traffic internal tenant to the allowlist. Watch
     `PoolItemSizeBytes` (must stay flat < ~200 B — the live proof the item-growth
     bug is gone, replacing the redundant 2×-provisioned c=1×3000 re-benchmark),
     the ledger drift alarms (must stay zero), and
     `PoolReconcileCreditBackInvariant` (must never fire). The `pool_item_size`
     gauge is emitted once per reconcile (cold path).
  2. **Full**: once the canary bakes clean, set the global
     `STRATOCLAVE_RESERVE_PROTOCOL=pending` (all tenants) and drop the allowlist.
Observability shipped with this: `PoolItemSizeBytes` gauge + growth alarm (>2 KB),
`PoolReconcileCreditBackInvariant` alarm, and the reconcile summary now splits
`retire_failures` from `credit_back_deferred`.

**Status: MIGRATING to pending via a golden-reference differential oracle
(un-frozen 2026-07-21).** The earlier "shipped, dormant" freeze is withdrawn: a
permanent feature flag guarding two live money paths is itself a liability (bug
surface + branch complexity for zero present benefit). Rather than keep the flag
forever OR blind-delete one path, we adopt a **strangler-fig migration with a
differential oracle** (Fable-designed): `transaction` is declared the GOLDEN
reference and FROZEN; `pending` is exercised while an oracle checks it is
equivalent; once the delete-gate criteria are met, `transaction` + the reference
model + the oracle are all deleted together, leaving `pending` as the single path.

**The oracle (money-safe by construction).** Production executes ONE path
(`pending` for a canary tenant). `transaction` becomes a PURE reference model that
does NOT write. Before `pending` issues its `TransactWriteItems`, the oracle
compares the **write-set it is about to send** (pool_reserved delta, hold-item
transition, conditions) against the reference model's **predicted write-set** for
the same input. Zero extra I/O, no TOCTOU (compared pre-write, not post-read
state), overhead is pure µs computation, gated by `STRATOCLAVE_RESERVE_ORACLE`
(dev default ON, prod OFF). A mismatch NEVER auto-rolls-back — it logs + emits a
metric + alarms (fail-open); money safety is enforced by `pending`'s own condition
expressions, the oracle only DETECTS drift. The write-set oracle proves "intent
equivalence"; "effect equivalence" (retry behaviour on condition failure) is
covered by a CI Hypothesis differential test that runs BOTH paths on moto and
compares post-state. Two-tier by design; neither alone suffices.

**formal (two roles, orthogonal).** (1) A Hypothesis DIFFERENTIAL stateful test
(`backend/tests/test_billing_differential_oracle.py`): the same operation sequence
is applied to both implementations and compared via an abstraction α(state) =
(pool_reserved, admission verdict), including PENDING's in-flight intermediate
states; operation success/failure verdicts must also match (observational
equivalence). (2) A Z3 joint-transition proof
(`backend/tests/test_pending_golden_equivalence_z3.py`, landed 2026-07-22):
applying the same input to both abstract models preserves the coupling
**J: (pool_reserved == reserved) ∧ (pending.limit == limit − settled)** for EVERY
symbolic state — reserve/settle/release/reap/fence/reconcile/set_limit — and
verdict parity follows algebraically from J (proved, with a vacuity guard that
drops the settled-injection and shows Z3 then finds diverging verdicts). A
model-fidelity cross-check in the same file drives those exact transitions through
the REAL `BillingLedger`/`PendingLedger` objects, so a change to either model that
breaks a transition equation fails the suite (enforcing the FREEZE clause below).
formal proves the MODELS are equivalent over all inputs; the runtime oracle detects
MODEL-vs-REALITY drift on real traffic. Both are required for the delete gate;
neither replaces the other.

**The runtime write-set oracle (mvp/reserve_oracle.py, wired into
_pending_commit_transact).** It runs ONLY on the pending path — i.e. only for a
canary tenant — so it is per-tenant by construction, not a global read on every
reserve. It emits THREE signals (Fable review 2): `reserve_oracle_match`
(verdict+reserved-delta agreed with the golden prediction), `reserve_oracle_race`
(disagreed but the pool moved concurrently between the pre-read and the commit — a
benign TOCTOU, a metric only, NOT alarmed), and `reserve_oracle_mismatch` (disagreed
with NO concurrent move — a genuine inequivalence, alarmed, blocks the gate). The
gate counts MATCHES, not just absence of mismatch, so it cannot pass on zero
samples.

**DELETE GATE (fixed up front — Fable requirement; not to be revised downward
mid-migration).** `transaction` + reference model + oracle are deleted only when
ALL hold:
  1. live external authorize/capture/void with **`ReserveOracleMatch` ≥ 1000 AND
     `ReserveOracleMismatch` == 0** (a match COUNT, not merely no mismatch —
     zero-sample must not pass; `ReserveOracleRace` is excluded, it is benign);
  2. **Z3 equivalence proof green AND Hypothesis differential test green** (the
     latter covers the replay/marker-CCF paths the runtime oracle skips)
     — **MET as of 2026-07-22**: both `test_pending_golden_equivalence_z3.py`
     (16 tests: 14 symbolic obligations incl. vacuity guards + 2 model-fidelity
     cross-checks) and `test_billing_differential_oracle.py` are green in CI.
     This leg re-opens automatically if either model changes (the freeze clause);
  3. dev/staging **7 consecutive days with `ReserveOracleMismatch` == 0** while
     matches accrue;
  4. the planned **scenario-based bulk live verification completes with zero
     mismatch across its whole run** (the practical final gate).

**FREEZE (precondition).** From the moment `transaction` is declared golden it is
FROZEN — no logic changes. The reference model is a copy of `transaction`'s logic,
so any `transaction` change would drift the model and invalidate the whole oracle.
If `transaction` must change, the migration is paused and the model re-derived.

**Deferred by decision (NOT an oversight) — sharded-pool throughput.** The single
pool item is still one item on one partition, so a single hot tenant is bounded by
~1000 WCU/s (TransactWrite consumes 2×, so ~half effective). With zero paying
tenants and no real load, that ceiling is many orders of magnitude away, and the
litellm-competition differentiator is the VSR trust-boundary + the provable Savings
Certificate, NOT ledger throughput — a ceiling worth fixing only once it is
approached, with a known solution (sharded counter). TRIGGER to start it: a
throttle event on the pool partition, OR a single tenant sustaining ~300 tx/s. At
that point, move the marker to its own table (PK=hold_id) at the same time. Until
then: do not build it.

**Key-design note (why this bites here).** All of a tenant's pool + holds + (the
mistaken) markers share ONE partition key (`tenant_id`), so a single hot tenant is
bounded by one partition's ~1000 WCU/s AND any per-item bloat lands on that same
partition. The separate-item marker keyed by `hold_id` moves that heat off the
tenant partition.

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
running concurrently under load. Superseded in part by the 2026-07-20 finding:
the separate-item marker (TransactWriteItems) must be A/B'd against the rejected
in-pool-item marker under warm/provisioned conditions BEFORE any target claim.

## Data lifecycle / DynamoDB → S3 tiering (accumulation is a first-class concern)

The item-growth blowup above is the acute form of a general truth: **unbounded
per-tenant accumulation on a single partition degrades both cost and latency.**
Two distinct layers, do not conflate:

1. **Hot-path state (pool item).** NOT solvable by tiering — a hot item must stay
   small and O(1). The fix is structural (separate-item marker, above), plus the
   existing reaper/reconciler that DELETE terminal hold rows so they never
   accumulate. Terminal `RECLAIMED`/`FAILED` rows should carry a short native TTL
   as a backstop.
2. **History (ledger events, decision log, usage logs, terminal audit).** These
   grow forever by design (audit) and DO want tiering: keep the last N days hot in
   DynamoDB for reconcile/certificate reads, then **export older partitions to S3
   (Parquet) and read them via Athena**. `usage_logs` already carries a `ttl`
   attribute; the ledger/decision tables do not and would bloat their tenant
   partitions indefinitely. A scheduled exporter (DynamoDB PITR export to S3, or a
   Streams→Firehose→S3 sink) + a TTL on the hot copy is the standard shape. The
   Savings Certificate / VSR reconcile must then read hot-DDB for recent days and
   S3/Athena for older ones behind the same `reconcile_day` interface. This is a
   prerequisite for running the certificate over long windows without the read
   cost climbing with tenant age.
