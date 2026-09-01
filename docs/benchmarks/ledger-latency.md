<!-- Last updated: 2026-07-19 -->
<!-- Applies to: Stratoclave main -->

# Ledger latency benchmark

This is the measured answer to the one claim [SCOPE.md](../SCOPE.md) flagged as
"a design target, not yet shown": **can the formally-proven credit ledger sit on
the synchronous hot path (target p99 < 50 ms)?** The ledger's *correctness* is
proven (Z3, zero double-posting); this document measures its *speed*, honestly,
and states plainly where the measurement lands relative to the target.

**Headline, stated without spin: the p50 is fast (ledger TransactWriteItems
p50 = 20 ms, end-to-end authorize p50 = 57 ms), but the p99 does NOT meet the
< 50 ms target — the ledger write alone is p99 = 58 ms even with zero
contention, and the end-to-end p99 is 225 ms. The bottleneck is not CPU (the
task never saturated) and not the client; it is the DynamoDB `TransactWriteItems`
tail plus, under concurrency on a single tenant pool row, the application's
optimistic-CAS retry.** See [Interpretation](#interpretation).

## What was measured

`POST /api/mvp/billing/authorize` — the external-authorize path that reserves a
dollar amount from a tenant pool with a single synchronous DynamoDB
`TransactWriteItems` (pool CAS + HOLD put + RESERVE ledger event + idempotency
row). It does **not** call Bedrock, so it is the isolated cost of putting the
ledger on the hot path. Two layers:

- **A (end-to-end, headline):** an in-region client → ALB → Fargate → DynamoDB,
  over a persistent keep-alive connection.
- **Server-side ledger span (decomposition):** the wall-clock of the
  `TransactWriteItems` call itself, logged as `ledger_transact_latency` on the
  committed path (permanent telemetry, `mvp/_pipeline.py`), so the ledger
  round-trip is separable from the HTTP/ALB shell.

## Interval definition (stated so nothing is hidden)

> **A-layer interval:** from sending the request on an already-established
> keep-alive connection to receiving the final response byte. **Includes** ALB +
> Fargate app + DynamoDB round-trip. **Excludes** TLS/connection setup (a real
> proxy client reuses connections) and CloudFront / WAN (unrelated to ledger
> speed — measuring through it would measure geography, not the ledger).
>
> The load generator is a same-region (us-east-1) EC2 in a **different VPC** than
> the target, reaching the ALB by public DNS (CloudFront excluded). This adds a
> VPC-crossing hop, measured directly: **TCP connect p50 = 0.90 ms, p99 = 2.27 ms,
> max = 7.6 ms (n = 1000)**. The hop stays within the AWS backbone (no WAN) and
> its bias is **conservative** — it can only make the measured latency worse, so
> a passing result would be understated, not inflated. It is disclosed here
> rather than hidden.
>
> **Server-span interval:** wall-clock of `TransactWriteItems` inside the app.
> No HTTP stack.

## Environment

| Item | Value |
|---|---|
| Measured commit | `f0bab1c` (ledger-latency telemetry) on `feat/credit-ledger-phase1` |
| Backend image | `scverify-backend:v51` (amd64) |
| Task size | ECS Fargate **256 CPU units (0.25 vCPU)**, 512 MiB — the real deployed size |
| Region / tables | us-east-1, DynamoDB **PAY_PER_REQUEST** (`scverify-tenant-budgets`, `scverify-credit-ledger`) |
| Logging | `ENVIRONMENT=production` (JSON, INFO). See the note below on a development-logging finding. |
| Rate limit | `BILLING_WRITE_RATE_LIMIT` relaxed to `100000/minute` for the bench (production default is `60/minute`; a rate limit is an abuse policy, not a performance component — relaxing it does not change what is measured, and it is disclosed here). |
| Load generator | zenn3s EC2 (us-east-1f, c7i.4xlarge, different VPC), keep-alive, CloudFront excluded |
| Tenant | a single tenant pool row (`default-org`) — see the contention note |

A finding worth recording: the environment was initially running
`ENVIRONMENT=development`, which selects console logging at **DEBUG** level and
made botocore emit per-request debug output. That is both a benchmark
contaminant and a mis-configuration for a production-like environment; the bench
was re-run under `ENVIRONMENT=production` (JSON/INFO) and all numbers below are
from that configuration.

## Results

### Floor — zero contention (concurrency = 1)

The lowest latency the design can reach: one request at a time, no CAS
contention. This is the number no amount of tuning can beat.

| Interval | n | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|---|
| Ledger `TransactWriteItems` (server span) | 3,734 | **20 ms** | 27 ms | **58 ms** | 166 ms | 245 ms |
| End-to-end authorize (A-layer) | 3,639 | **57 ms** | 78 ms | **225 ms** | 356 ms | 395 ms |

Decomposition at p50: of the 57 ms end-to-end, ~20 ms is the ledger round-trip
and ~37 ms is ALB + app + network (of which the VPC-crossing hop is ~1 ms; the
rest is app processing on 0.25 vCPU).

> **R39d annotation.** Captured at commit `f0bab1c` (see [Environment](#environment)),
> before the quota-raise-and-archive epic: the pool row carried the pre-epic
> attribute set (`sizing`, no `seat_count`/`manual_limit_microusd`/
> `seat_rate_microusd`/`pool_granted_microusd`/`grant_cap_microusd`). Not
> re-measured for this epic and no re-measurement is claimed: the 58 ms
> transaction tail is dominated by round trips and DynamoDB's own commit
> latency at this item size, not by the pool item's byte count, and the epic's
> attribute additions still leave the item inside its first write unit (see
> [`../design/ledger-hot-path.md`](../design/ledger-hot-path.md)'s flatness
> paragraph). A reader should not assume this number describes today's schema
> from the fact that it is unchanged — it describes `f0bab1c`'s.

### Contention curve — single tenant pool row (worst case), 0.25 vCPU

All load aimed at ONE pool row. This is the **worst case by construction**: a
multi-tenant workload spreads across partition keys, so per-tenant contention is
strictly better than this curve. Multi-tenant aggregate performance was **not
measured** (see [Not measured](#not-measured)).

| Concurrency | n | e2e p50 | e2e p99 | e2e max | error rate | server-span p99 |
|---|---|---|---|---|---|---|
| 1 (floor) | 3,639 | 57 ms | 225 ms | 395 ms | 0% | 58 ms |
| 2 | 379 | 206 ms | 1,953 ms | 2,515 ms | 1.3% | 51 ms |
| 8 | 360 | 855 ms | 4,903 ms | 5,611 ms | 2.4% | — |
| 16 | 470 | 989 ms | 6,512 ms | 8,589 ms | 6.2% | — |

The key signal: from c=1 to c=2 the **server-side `TransactWriteItems` p99
barely moved (58 → 51 ms)**, yet the **end-to-end p99 exploded (225 → 1,953 ms)**.
The degradation is therefore NOT DynamoDB — it is the application's
optimistic-CAS retry (snapshot re-read + full-jitter backoff) on the single hot
pool row. A single pool row is a structural hot spot under concurrency.

> **R39d annotation.** Pre-epic schema, commit `f0bab1c`, snapshot-all-equal
> CAS — this is the design the headroom-ADD gate below **replaced**. Not
> re-measured for this epic: re-measuring a design already abandoned adds
> nothing, and this curve is not sensitive to the pool item's byte count (it is
> measuring application-level CAS retry storms, not item size).

### After: headroom ADD gate (same worst case, re-measured)

The snapshot-all-equal CAS above was replaced with a single conditional ADD to a
derived `pool_headroom_microusd` counter (see
[../design/ledger-hot-path.md](../design/ledger-hot-path.md)). The reserve's
condition now references only the counter it mutates, so a concurrent reserve
that still fits no longer invalidates the others — the snapshot-invalidation
`ConditionalCheckFailed` storm cannot occur. Re-measured on the same ALB and the
same in-region load-gen host, same single hot pool row:

| Concurrency | e2e p50 (before → after) | e2e p99 (before → after) | error rate (before → after) |
|---|---|---|---|
| 2  | 206 → 151 ms   | 1,953 → 556 ms   | 1.3% → **0%** |
| 8  | 855 → 480 ms   | 4,903 → 2,112 ms | 2.4% → **0%** |
| 16 | 989 → 860 ms   | 6,512 → 4,508 ms | 6.2% → **0%** |

What changed, stated plainly:

- **The contention error rate went to zero at every step** (6.2% → 0% at c=16).
  The snapshot-invalidation storm — the thing that made concurrent reserves fail
  and retry — is gone, exactly as the design intended.
- **p99 improved materially under contention** (c=2 −72%, c=8 −57%, c=16 −31%).
- **But single-row p99 still climbs steeply with concurrency and still misses the
  target.** The projected "6,512 → ~200 ms" in the design doc was **NOT met**:
  measured c=16 p99 is 4,508 ms. The reason is exactly the residual the design
  doc flagged (finding 1): the reserve item is composed into a multi-item
  `TransactWriteItems`, so two reserves on the *same* pool row still collide at
  the transaction layer with reason `TransactionConflict` and the caller still
  bounded-retries. Headroom removed the application-CAS storm; it did **not** make
  a single hot row's p99 flat. Flattening that tail is the next step's job (the
  two-item transaction + Streams-derived events), not this change's.

(The after-run's c=1 step was a short 45 s sample and is not comparable to the
long dedicated floor run above; the floor p99 = 58 ms is unchanged by this
change, as the design predicted.) Raw CSVs: `bench/ledger-latency/results/headroom_c{1,2,8,16}.csv`.

> **R39d annotation.** Pre-epic schema, commit `f0bab1c`. Not re-measured for
> this epic, for the same reason as the floor above: this curve measures the
> headroom-ADD gate's effect on transaction-conflict retries under contention,
> which is orthogonal to the pool item's byte count. No re-measurement is
> justified by the epic's attribute additions alone.

### CPU is not the limiter

During the runs, ECS service `CPUUtilization` averaged 7–37 % (max 52–84 %) — the
0.25 vCPU task never saturated. Raising vCPU would not move the p99: the floor
p99 is the DynamoDB `TransactWriteItems` tail, and the contention p99 is
application CAS retry, neither of which is CPU-bound.

### Invariant audit — correct under the contention storm

After all runs (including the c=16 storm with a 6.2 % error rate), every HOLD row
for the tenant was distinct: **1,376 hold rows, 1,376 distinct, 0 duplicates.**
A failed authorize (the errors above) created no hold — the `TransactWriteItems`
is atomic — and no successful authorize created two. **Zero double-reserve held
through the storm.** This is the half a "fast" number alone cannot show: the
ledger is not just measured for speed, it is measured for correctness *under the
same load*, and the correctness invariant survived.

## Interpretation

Against the target (p99 < 50 ms), read honestly:

- **p50 is fast and on-target-ish** (ledger 20 ms, e2e 57 ms).
- **p99 misses the target**, even at the zero-contention floor (ledger 58 ms,
  e2e 225 ms), and misses badly under single-row contention.
- The miss is **not** fixable by scaling the task (CPU is idle) or the client.
  The floor p99 is the DynamoDB `TransactWriteItems` tail; the contention p99 is
  the single-pool-row optimistic-CAS retry.

So the benchmark's job is done — not by proving "< 50 ms", but by turning the
target into a **measured design signal**: to hit p99 < 50 ms on the hot path,
the design must change, and the data points at where —

1. **Reduce the ledger tail:** the four-item `TransactWriteItems` has a tail
   that a single-item conditional `UpdateItem` (where the money-move allows it)
   would not; or accept the reserve as eventually-durable and move part of it
   off the synchronous path.
2. **Remove the single-row hot spot:** shard the tenant pool row (e.g. N
   sub-counters reconciled) so concurrent authorizes for one tenant do not all
   CAS the same item — this is what the contention curve is really measuring.

These are design changes, not tuning, and choosing among them is follow-up work.
Publishing the miss is the point; engineering a 50 ms number is not.

## Item-count spike — does reducing the transaction to 2 items help? (measured: NO)

The two-item migration's performance premise was "moving the RESERVE ledger event
(and IDEMP row) off the synchronous transaction — 4 items → 2 — shrinks the p99
tail." Before investing the correctness work, that premise was measured directly:
the same real DynamoDB (us-east-1), the same single hot pool row, the same
production item builders, composed into 4- / 3- / 2-item `TransactWriteItems`
variants, with per-transaction attribution (SDK retries, `TransactionConflict`
count). 3,000 sequential + 3,000 concurrent (c=16) per variant. This is a
throwaway spike (correctness intentionally not preserved; bench namespace cleaned
up), run from `bench/ledger-latency/bench_itemcount_spike.py`.

**Floor (c=1, zero contention) — item count barely moves the tail:**

| Transaction | p50 | p99 | p99.9 | max |
|---|---|---|---|---|
| 4-item (pool + HOLD + RESERVE + IDEMP, today) | 21.9 ms | 90.2 ms | 241 ms | 265 ms |
| 3-item (RESERVE async'd) | 20.6 ms | 60.5 ms | 157 ms | 168 ms |
| 2-item (RESERVE + IDEMP removed) | 19.0 ms | 69.7 ms | 176 ms | 239 ms |

**Single-row contention (c=16) — item count is IRRELEVANT:**

| Transaction | p50 | p99 | max | errors | TransactionConflict total |
|---|---|---|---|---|---|
| 4-item | 25.5 ms | 1,194 ms | 1,888 ms | 35 | 3,291 |
| 3-item | 23.5 ms | 1,188 ms | 1,954 ms | 35 | 3,325 |
| 2-item | 22.3 ms | 1,189 ms | 1,767 ms | 38 | 3,193 |

**Conclusion (decisive): the two-item migration does NOT, on its own, meet the p99
target.** Neither the floor nor the contention tail responds to item count. The
c=16 p99 is quantized at ~1,190 ms across all three variants because the tail is
100% SDK-backoff accumulation on `TransactionConflict` — the optimistic
serialization of concurrent transactions touching the SAME pool row, which is a
function of *there being a transaction on a hot row*, not of how many items it
carries. (The 3-item < 2-item floor inversion is sampling noise: at n=3,000 the
p99 confidence interval comfortably spans a 10 ms gap.) A further signal: at c=16,
35–38 of 3,000 transactions still FAILED after 8 retries — under a burst past
c=16 the current transactional design does not just get slow, it drops requests.

> **R39d annotation.** Pre-epic schema, throwaway namespace, run from
> `bench/ledger-latency/bench_itemcount_spike.py`. This spike varies item
> **count** (4/3/2 items in the transaction), not item **bytes** — none of the
> three variants carry `seat_count`/`manual_limit_microusd`/
> `seat_rate_microusd`/`pool_granted_microusd`/`grant_cap_microusd`, and the
> pool row's byte size in this spike is unrelated to the epic's attribute
> additions. Not re-measured: the conclusion (item count is a dead end) does
> not depend on the pool item's byte size at all, and this script now seeds
> its pool row through `bench/ledger-latency/pool_fixture.py` (R39c) so a
> future re-run is a real tenant-shaped row rather than a bare figure.

**What this redirects the design to.** Because the killer is *the transaction
itself* on a hot row, the next measured step is the **PENDING protocol** (design
doc): Put HOLD `PENDING` → a SINGLE conditional `UpdateItem` on the pool row (no
transaction, so no `TransactionConflict` failure mode — concurrent single-item
writes queue at the leader replica rather than cancel) → mark HOLD `ACTIVE`. This
attacks the floor AND the contention tail; sharding (which only divides contention
by N and cannot touch the transaction floor that already misses at c=1) is held as
a composable second stage. Crucially, the two-item migration is NOT abandoned — it
is re-scoped as the **prerequisite path to PENDING** (PENDING's write path touches
only pool + HOLD, so moving RESERVE/IDEMP off the synchronous transaction is a
precondition), and the deployed shadow projector + reconciler become the
drift/orphan safety net that de-transactionalization requires. See
[docs/design/ledger-hot-path.md](../design/ledger-hot-path.md).

## Post-epic re-measurement (R39d, B5) — pending, named rather than fabricated

None of the figures above needs re-running **because of** this epic: none were
sensitive to the pool item's byte count in the first place (each measures
transaction/round-trip latency or an application-level retry storm), and the
item stays inside its first write unit either way (see
[`../design/ledger-hot-path.md`](../design/ledger-hot-path.md)'s flatness
paragraph, `599` bytes worst case as of this epic's F1+F2 schema). What they
needed was the annotation above making that explicit, so a reader does not
independently conclude "these numbers are stale."

`SEAMS.md` B5 names the evidence R39d anchors to as one benchmark re-run
through `bench/ledger-latency/pool_fixture.py`, after F1's and F2's attributes
are both present on the same row simultaneously, with the figure difference
attributed to the change or to the harness. That fixture and its identity
check exist as of this contract's implementation (`bench/ledger-latency/`, all
five scripts routed through `seed_verified_pool`) and have been exercised
functionally under moto — see the fixture's own docstring for the identity it
enforces. **The re-run itself, against real AWS and the deployed ALB/Fargate
path this document's other figures were captured on, has not been executed**:
it needs a deployed bench revision and live infrastructure this implementation
pass does not have standing access to, and is recorded here as pending rather
than fabricated. A single benchmark run captures ONE of the three reachable
row shapes — never granted; granted, cap derived; granted, cap explicit (the
cap sentinel is documented in `docs/design/quota-raises.md` and
`docs/design/CONTRACTS.md` C14.17) — and whoever runs it must name which shape
the run was in and list only that shape's attributes, per this row's R39d
"Verified by" clause, rather than describing "the post-epic row" as if there
were one.

## Not measured (the honest limits)

- **Multi-tenant aggregate performance.** Authorize targets the caller's own
  `org_id`, so a multi-tenant spread needs many authenticated principals; not
  built. Only the single-row worst case is measured. Per-tenant latency in a
  real multi-tenant deployment is bounded ABOVE by the contention curve here.
- **Bedrock-path end-to-end.** This measures the pool-only authorize, not an
  inference request.
- **Sustained load / partition-split behaviour** of the PAY_PER_REQUEST tables
  beyond these runs.
- **DynamoDB's own throttling / fault behaviour** — delegated to the AWS SLA;
  not injectable honestly, so not claimed.
- **Task-loss (A-3) resilience** under a *clean* baseline — the single-tenant
  contention already dominates the signal, so a task-kill run was not separated
  out; folded into "not measured" rather than reported as a clean number.

## Reproduce

Scripts: [`bench/ledger-latency/`](../../bench/ledger-latency). Raw per-request
latency CSVs from this run are in
[`bench/ledger-latency/results/`](../../bench/ledger-latency/results) (summary
CSVs; full raw sets were captured on the load host).

1. Deploy a bench rev: `scverify-backend:v51`, `ENVIRONMENT=production`,
   `BILLING_WRITE_RATE_LIMIT=100000/minute`.
2. Confirm the ALB security group; if it is CloudFront-prefix-list-only, add the
   load host's `/32` on the ALB port temporarily (record add/remove times) and
   remove it after.
3. From an in-region host, measure route overhead: `route_overhead.py <alb-dns>`.
4. Floor + curve: `bench_live.py --base-url http://<alb-dns> --token-file <tok>
   --mode a1|a2 --tenants <t> --concurrency <c> --duration-s <s> --out <csv>`.
5. Server span: filter `ledger_transact_latency` from the backend logs for the
   run window.
6. Invariant audit: query the tenant's HOLD rows; assert count == distinct.
7. Tear down: remove the SG rule, restore the flags-off rev.
