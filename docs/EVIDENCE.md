<!-- Last updated: 2026-07-23. Every figure carries the commit that produced it. -->

# Evidence map — what is claimed, and how far it is verified

Stratoclave attacks LiteLLM head-on with three weapons LiteLLM structurally lacks
(see [SCOPE.md](SCOPE.md)). This page is the honest reach-map: for each claim, the
**strongest evidence that exists today** and — at the same visibility as the
claim — **what that evidence does not cover**. It is meant to be read in five
minutes before reviewing or adopting the branch.

## Evidence tiers

| Tier | Meaning |
|---|---|
| **gateway-live** | ran THROUGH the gateway request path (`/v1/messages`: auth → reserve → real Bedrock → settle → ledger) on real model traffic |
| **direct-baseline** | ran against real Bedrock **directly**, gateway NOT in path — a measuring stick, verifies the method not the gateway |
| **formal / offline** | proved by Z3 / property tests, or a deterministic offline fold — no network |
| **moto · in-process** | exercised the real code, but on a mocked DynamoDB and in-process ASGI (no real DynamoDB, no network/ALB/TLS) |
| **unverified** | honestly not yet done |

## The map

| Claim | Strongest evidence | Tier | Not covered (same-visibility limits) | Commit |
|---|---|---|---|---|
| **Billing ledger is correct** (atomic reserve/settle, zero double-post) | Z3 formal proofs of the money invariants + Hypothesis stateful | formal | proof is over the executable model, not real DynamoDB | `7ac3214`, `f24cfac` |
| **PENDING-protocol migration is safe** (`transaction` golden ↔ `pending` equivalent) | Z3 joint-transition equivalence (golden ↔ pending) + Hypothesis differential — delete-gate condition (2) MET | formal | delete-gate (1),(3),(4) need live/soak traffic — still open | `27d86db` |
| **Ledger fits the hot path** (target p99 < 50 ms) | measured: end-to-end authorize p50 **57 ms**, `TransactWriteItems` p50 **20 ms** | direct-baseline (real DynamoDB, EC2) | **p99 = 225 ms — MISSES the < 50 ms target**; needs single-item CAS / pool sharding, not tuning | `354f0d5` |
| **Charge-of-record through the gateway** (a bill only a real ledger can produce) | `/v1/messages` ran auth→reserve→real Bedrock→settle→ledger; gateway settled **$0.000492** (client-side estimate $0.000562) | **gateway-live** | ledger = **moto** (real DynamoDB behaviour not exercised); transport = **in-process-asgi** | `3378271` |
| **Savings Certificate** (counterfactual "if you'd followed the VSR", conservative) | offline demo vs a passthrough spend log, real engine over a checked-in workload | formal / offline | synthetic workload; a real tenant number needs the tenant's own traffic | `f0db754` |
| **Gateway TTFT / TPOT is measurable** | gateway-path TTFT p50 **2384 ms** vs direct p50 **2089 ms**; **paired overhead median 248.7 ms** | **gateway-live** (paired, same run) | **N = 10, point estimate — no distribution/CI claim**; overhead = auth+reserve+ASGI only, **network/ALB/TLS EXCLUDED** (transport = in-process-asgi) | `3378271` |
| **Routing quality** | a tiny conservative exact-match scorer (gateway response scored 10/10, N=10) | gateway-live | `quality.measured = false` — Stratoclave does NOT claim quality without a tenant eval; N=10 is a mechanism demo, not a benchmark | `3378271` |
| **Workshops surface the gaps** (machine-checked roadmap) | `scenarios/` coverage.yaml → auto-generated [`../scenarios/COVERAGE.md`](../scenarios/COVERAGE.md), CI-linted | formal / offline | — | `77c3f68` |

## The honest borders (stated once, plainly)

- **`gateway-live` here means in-process + moto.** The gateway's *code path and its
  billing accuracy* ran on real Bedrock traffic; a **real DynamoDB** ledger and a
  **deployed** path (uvicorn + ALB + TLS) are **unverified**. The overhead figure
  (248.7 ms) therefore measures auth + reserve + ASGI dispatch, **not** a
  production latency SLO.
- **Small N.** All live numbers are `N = 10`–`30` point estimates with raw runs
  kept; no percentile beyond p50 is named, no significance is claimed.
- **Provenance is stamped, not inferred.** Every live result JSON carries
  `source=real`, `gateway_in_path`, `transport`, `ledger`, `excluded`, model,
  region, timestamp, run_id. Synthetic demo assets carry a `SYNTHETIC` banner.

## The next move (deliberately left as a gap, not padded here)

The workshop's [`COVERAGE.md`](../scenarios/COVERAGE.md) lists exactly two
`not-implemented` capabilities, which are the next branch's first steps — kept as
open gaps on purpose, because "the workshop makes the next feature machine-visible"
is itself one of the claims above:

1. **gateway-emitted token-timing telemetry** — today TTFT is measured with a
   client stopwatch; the gateway does not emit its own first-token metric, so
   overhead cannot be attributed in production without a client harness.
2. **eval-tap** — export `(span_id, prompt, response)` so an operator can score
   real traffic (the scorer and acceptance bar stay the operator's responsibility).
