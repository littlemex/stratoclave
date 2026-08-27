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
| **deployed-live** | ran through the **deployed** stack from outside it: public CloudFront hostname → ALB → ECS Fargate → real DynamoDB → real Bedrock. No mocks, no in-process ASGI. The strongest tier |
| **gateway-live** | ran THROUGH the gateway request path (`/v1/messages`: auth → reserve → real Bedrock → settle → ledger) on real model traffic, but in-process + moto |
| **local-store** | ran over HTTP against the gateway on one machine, on **DynamoDB Local** (not a mock) and real Bedrock. Weaker than `deployed-live`: a single-node store, nothing crossing a network, no concurrency |
| **direct-baseline** | ran against real Bedrock **directly**, gateway NOT in path — a measuring stick, verifies the method not the gateway |
| **formal / offline** | proved by Z3 / property tests, or a deterministic offline fold — no network |
| **moto · in-process** | exercised the real code, but on a mocked DynamoDB and in-process ASGI (no real DynamoDB, no network/ALB/TLS) |
| **unverified** | honestly not yet done |

## The map

| Claim | Strongest evidence | Tier | Not covered (same-visibility limits) | Commit |
|---|---|---|---|---|
| **Billing ledger is correct** (atomic reserve/settle, zero double-post) | Z3 formal proofs of the money invariants + Hypothesis stateful | formal | proof is over the executable model, not real DynamoDB | `7ac3214`, `f24cfac` |
| **PENDING-protocol migration is safe** (`transaction` golden ↔ `pending` equivalent) | Z3 joint-transition equivalence (golden ↔ pending) + Hypothesis differential — delete-gate condition (2) MET | formal | delete-gate (1),(3),(4) need live/soak traffic — still open | `27d86db` |
| **Ledger fits the hot path** (target p99 < 50 ms) | measured: end-to-end authorize p50 **57 ms**, `TransactWriteItems` p50 **20 ms** | direct-baseline (real DynamoDB, EC2) | **end-to-end authorize p99 = 225 ms — MISSES the < 50 ms target** (`TransactWriteItems` p99 is 58 ms at the zero-contention floor — always name which metric a p99 belongs to); needs single-item CAS / pool sharding, not tuning; **the deployed Fargate + ALB + CloudFront path is not measured at all** | `354f0d5` |
| **Charge-of-record through the gateway** (a bill only a real ledger can produce) | `/v1/messages` ran auth→reserve→real Bedrock→settle→ledger; gateway settled **$0.000492** (client-side estimate $0.000562) | **gateway-live** | ledger = **moto** (real DynamoDB behaviour not exercised); transport = **in-process-asgi** | `3378271` |
| **Reproducible Savings Report** (counterfactual "if you'd followed the VSR", conservative; computing savings is not unique — LiteLLM has `savings_baseline` — the difference is that the inputs are explicit so the number can be recomputed) | offline demo vs a passthrough spend log, real engine over a checked-in workload | formal / offline | synthetic workload; a real tenant number needs the tenant's own traffic | `f0db754` |
| **Gateway TTFT / TPOT is measurable** | gateway-path TTFT p50 **2384 ms** vs direct p50 **2089 ms**; **paired overhead median 248.7 ms** | **gateway-live** (paired, same run) | **N = 10, point estimate — no distribution/CI claim**; overhead = auth+reserve+ASGI only, **network/ALB/TLS EXCLUDED** (transport = in-process-asgi) | `3378271` |
| **Routing quality** | a tiny conservative exact-match scorer (gateway response scored 10/10, N=10) | gateway-live | `quality.measured = false` — Stratoclave does NOT claim quality without a tenant eval; N=10 is a mechanism demo, not a benchmark | `3378271` |
| **Workshops surface the gaps** (machine-checked roadmap) | `scenarios/` coverage.yaml → auto-generated [`../scenarios/COVERAGE.md`](../scenarios/COVERAGE.md), CI-linted | formal / offline | — | `77c3f68` |

### Added 2026-08-27: deployed-path verification

Run from a laptop against the public CloudFront hostname of a live deployment, with a
`user`-role API key only. Scripts and full write-up: see the run log referenced below.

| Claim | Evidence | Tier | Not covered |
|---|---|---|---|
| **The deployed path is the path** | response headers on `GET /v1/models`: `Via: 1.1 <id>.cloudfront.net (CloudFront)`, `X-Amz-Cf-Id`, `X-Amz-Cf-Pop: SFO53-P12`, `server: uvicorn`; HSTS and the restrictive API CSP both present | **deployed-live** | which ECS task / AZ served it; CloudFront-to-ALB internals |
| **All three inference routes work end to end on real Bedrock** | `/v1/messages` 200 (13 in / 4 out), `/v1/chat/completions` 200 (13 / 4), `/openai/v1/responses` 200 (12 / 5) | **deployed-live** | streaming paths were not exercised in this run; single request each |
| **Server-side records match what the client was told** | `usage show` history rows carry exactly the token counts the client received, for all three calls, and record the **resolved** model (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) for the alias the client sent (`claude-haiku-4-5`) | **deployed-live** | the dollar rating itself — see the row below |
| **Token quota decrements exactly** | two probes: 13 in + 4 out → 17 units; 22 in + 243 out → 265 units. Exactly one unit per token in both, i.e. the token quota, not a dollar rating | **deployed-live** | — |
| **The dollar rating is correct** | — | **unverified** | the pool side of settle only runs when the reservation was pooled, and the test tenant has no dollar pool, so no ledger charge lines exist and `/me/billing/runs/{id}` correctly 404s. Verifying this needs an admin-configured dollar pool |
| **Permissions are enforced at the deployed edge, not only in tests** | `POST /me/billing/authorizations` → **403 Missing permission: billing:write** for a `user` role, even though the CLI exposes the subcommand | **deployed-live** | — |
| **API key revocation takes effect immediately** | create → `GET /v1/models` 200 → revoke → **401 at once**, and still 401 at +5 s and +20 s (no cache lag) | **deployed-live** | — |
| **Deployed-path latency, first measurement** | `GET /v1/models` (auth + app only, no Bedrock, no reserve), connection reused: n=25, min 193 ms, **p50 253 ms**, p90 335, p95 349, max 882. Fresh connection per call: p50 568 ms. With inference: haiku ≈ 1.1–1.2 s, gpt-5.6-sol ≈ 2.3–3.9 s | **deployed-live** | **this is NOT the authorize p99** — it excludes the budget reserve and the Bedrock call, so it cannot be compared with the 225 ms row above. Client-side total only: no split of CloudFront / ALB / app / DynamoDB, no retry count, cold start neither induced nor identifiable. Measured from a SFO edge against a us-east-1 origin, so geography dominates |

**What this run did not touch**: the hard budget limit under concurrency (needs a small
dollar budget, which is an admin operation), tenant isolation, contention between two users
of the same tenant, server-side latency breakdown, and the expired-hold reaper.

### Local mode, against DynamoDB Local and real Bedrock (2026-08-28)

One machine: DynamoDB Local 2.x with `-sharedDb` for state, the backend run both natively
and from the compose stack, and inference going to real Bedrock. Weaker than `deployed-live` — the store is a single node
and nothing crosses a network — but stronger than moto, because it is the store the
documented local path actually runs. Setup and caveats: `docs/LOCAL.md`.

| Claim | Evidence | Tier | Not covered |
|---|---|---|---|
| **The documented local path runs end to end, in containers** | `make up` then `make demo` from a clean state through `finch compose` (nerdctl), building `backend/Dockerfile`: 23 tables + 11 GSIs created, seeding idempotent, all three inference routes 200 on real Bedrock, token counts and the resolved model read back from the containerised ledger | **local-store** | the `docker compose` builder specifically — same compose file, different build implementation. The `compose` job in `e2e-nightly.yml` is what covers that |
| **The ledger is not the cost, once the store is real** | `/v1/chat/completions`: `reserve_ms=13.7`, `settle_ms=13.3`, `upstream_ms=1217.2`, `total_ms=1255.6` — 27 ms of ledger in a 1.26 s request, the same shape as the 1112 ms deployed call | **local-store** | anything under concurrency: single caller, single node, no throttling, no partition contention. This is not an authorize p99 |
| **A stand-in store makes local timing meaningless, not merely inflated** | the same call on moto: `reserve_ms=9485.2`, `settle_ms=3866.3` — reserve alone ~690× the DynamoDB Local figure | **local-store** | — |
| **The local scripts cannot write to a real account** | `scripts/local/_local_guard.py` checks the endpoint botocore *resolved*, not just the variable that was set, and exits if it is an AWS host. Both refusal paths exercised | **moto · in-process** | — |

## The honest borders (stated once, plainly)

- **`deployed-live` rows are the only ones that ran outside the process.** Everything else,
  including the charge-of-record row below, is in-process.
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
