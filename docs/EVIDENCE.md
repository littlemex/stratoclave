<!-- Last updated: 2026-09-02. Every figure carries the commit that produced it. -->

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

### Added 2026-08-29: what an unobserved outcome actually costs

AWS does not document which failures are billed, so it was measured against CloudWatch's own
`AWS/Bedrock` counters, one condition per minute, on models this account otherwise never invokes.
The write-up is [`MEASUREMENTS.md`](MEASUREMENTS.md) ([日本語](MEASUREMENTS.ja.md)); the harness
that repeats the probes is `scripts/local/measure_provider_outcome.py`, which reports a condition
whose counter minute came back empty as `no_data` rather than as a zero. The measurement is the
evidence for the liability policy rows in `backend/mvp/provider_outcome.py`, and the design those
rows serve is [`design/charge-loss.md`](design/charge-loss.md).

| Claim | Strongest evidence | Tier | Not covered | Commit |
|---|---|---|---|---|
| **"No response received" does not mean "not billed"** | a Converse call abandoned at a 2 s client read timeout, exactly one SDK attempt: 1 invocation, **1,493 output tokens**, caller received nothing | **provider-live** (real Bedrock, provider's own counters) | one trial per condition, one model family; on-demand token pricing only | this change |
| **The line is whether the model ran, not whether the call failed** | a service rejection (`ValidationException`, HTTP 400) records 1 invocation and 1 `InvocationClientErrors` with **no token counters**; a stream closed by the consumer after two events records **nothing**, while a completed stream records exact usage | **provider-live** | `Invocations` alone is not a billing proxy — it counts rejections; the token counters are | this change |
| **One reservation could pay for three provider invocations** | botocore rewrites `Config(retries={"max_attempts": N})` to `total_max_attempts = N + 1`; measured, `max_attempts=1` produced two counted invocations and `total_max_attempts=1` produced one. The double charge lands on the **success** path, so no error handling can see it | **provider-live** + botocore source | fixed by making the SDK single-attempt; the router's own retry loop is still the retry budget | this change |
| **An abandoned call is reconcilable per attempt on Converse** | model invocation logging (text delivery off) records the abandoned call with `in 22 / out 1,493`, retrieved by the `requestMetadata` marker the gateway stamps; delivery lag **35–41 s** | **provider-live** | Converse only. On the OpenAI-compatible endpoint the log record exists but `requestMetadata` is recorded as `null`, so those attempts are aggregate evidence, not per-hold attribution. `bedrock-mantle` produces no record at all | this change |
| **The reclaim's `actual = 0` is an assertion, not an observation** | every reclaim now copies the reaped hold's `source` / `created_at` / `expires_at` / amount into its RECLAIM terminal, so the exposure is derivable from the ledger | formal / shipped | the number itself needs real traffic; retaining the reservation instead of returning it is **not built** — it needs a durable sweep cursor, pool-incarnation fencing and a settle-dispatcher branch | this change |
| **The hold owns every ending, and the liability policy governs every unobserved one** | a reservation can now be ended only through `mvp._money.Hold`; the nine hand-written endings that disagreed (five of which refunded a call that may have been billed) are gone, and `test_money_lifecycle_discipline.py` fails the build on any module under `mvp/` that calls `refund` / `release_pool` / `settle_reservation_and_log` outside its one hold factory | formal / shipped | the policy's *rows* are still one-trial measurements (above), and withholding a reservation stays opt-in via `STRATOCLAVE_UNOBSERVED_HOLDS`. With the gate off the money behaviour is unchanged **except for two defects the unification fixed**, both on the OpenAI-compatible routes and both pinned by a test: a mid-stream failure after the provider reported usage used to refund the whole reservation, and a 200 whose body would not parse used to strand the hold entirely. The guard is an AST check, so it sees direct calls — not an alias, a new helper, or a reflective call | this change |
| **A cancellation cannot silently change the KIND of ending, or lose one** | every ending claims synchronously — before the frame that announces it goes out — and the write is dispatched by the claimed `Ending` (shielded when awaited), so neither a `CancelledError` at the write nor a consumer closing on that frame can let the disconnect `finally` claim first. A claim whose write was interrupted is completed by `Hold.dispatch_pending()` from each generator's `finally`, so a claimed ending cannot be lost either. `test_budget_flow_disconnect.py` fails if the claim moves back into the worker or if the shield is removed | formal / shipped | **one claimed ending, dispatched at most once — not one write** (returning a reservation is two: the token credit, then the pool hold), and **not exactly-once**: a write that RAISES after its claim is not retried, because a raised write may still have committed — the reaper is the backstop. On the two OpenAI-compatible routes a cancellation while the request is in flight ends as the disconnect reading rather than as an invoke error; the money is the same under the gate (both hold when nothing came back) but the span label differs. `_return_reservation` is two writes, so a failure between them leaves the pool hold for the reaper | this change |

Recorded as **exposure**, not as a leak: the amount is the reservation rather than the provider's
bill, a request that died before its provider call cost nothing, and a settle arriving after the
reclaim is already recovered by `LATE_SETTLE`. Neither classic CUR nor CUR 2.0 carries a
per-request identifier, so invoice-level per-request reconciliation is not available to anyone.

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

**What this run did not touch**: tenant isolation, contention between two users of the
same tenant, server-side latency breakdown, and the expired-hold reaper. The hard budget
limit under concurrency was the largest gap here and is now measured — see the next
section.

### The dollar ceiling under concurrency, on a deployed stack (2026-08-29)

The claim this project cares most about, measured rather than proved. A dedicated
deployment (`sc-verify`, us-west-2, its own nine stacks and its own tables, Bedrock
primary deliberately in a different region from production so the load test could not
consume production's per-region model quota) with the hard-ceiling gate on, so
admission reserved the sound bound rather than the legacy estimate.

`scripts/local/prove_ceiling.py --deployment sc-verify`. Admissions counted from the
ledger rather than from the client, every amount recomputed from the tokens and rates
the event itself carries, a quiescence barrier whose timeout is a failure, and a run
declared INCONCLUSIVE unless it produced at least one admit, at least one refusal whose
reason was pool headroom, and an overlapping reservation window witnessed from the
pool's own reserved counter.

| Claim | Evidence | Tier | Not covered |
|---|---|---|---|
| **Concurrent callers cannot overspend the dollar pool** | 4 repetitions at 8 concurrent probes against an occupier holding a reservation open; 3 repetitions witnessed overlap, 24 contended attempts, 11 admits and 16 pool-headroom refusals; no ledger-reconstructed state showed settled plus reserved above the limit | **deployed-live** | 24 attempts is a small number, and contention is timing-dependent rather than a Bernoulli trial — a larger claim needs more repetitions, not a confidence interval computed from these |
| **Every bound-priced reservation dominated its settle** | 11 of 11, on real Bedrock traffic | **deployed-live** | reservations priced by the legacy estimate are excluded by construction: a deployment with the gate off reserves the estimate, and the estimate being exceeded is the defect this work fixes |
| **The headroom identity survives real concurrency** | held on every reconstruction | **deployed-live** | — |
| **A request that cannot fit the budget is refused distinctly** | `402 request_does_not_fit_pool_limit`, observed when the occupier's bound exceeded the whole pool | **deployed-live** | — |

**What the run does not reach**, and no invariant here notices: a charge that lands at
Bedrock while the process dies before settling it. The reaper releases the hold, the
spend leaves the ledger, and later admissions can carry the pool past its limit with
every proved property intact. That is the atomicity gap recorded above, and it is not
closeable from this side.

Three defects in the harness itself were found by running it, and each would have made
a green run meaningless: it reported only pool-level totals, so a five-micro-USD
per-request breach vanished into a headroom of thousands; it asked for a pool limit
rather than a headroom, so spend from earlier runs made the budget stop binding for
reasons unrelated to the system; and it evaluated every terminal in the period, so a
defect fixed in the morning would fail every run until the month rolled over. A fourth:
its overlap witness read the pool once after the probes finished, which assumed the
occupier was still in flight — true for a reasoning model on a long prompt, false on a
trivial one, and it reported genuine overlap as inconclusive until the witness became a
peak sampled across the probe window.

### The limit-raise mechanism (F1+F2), on a from-scratch deployed stack (2026-09-02)

A dedicated deployment (`scquota`, us-east-1, `impl/quota-f2` at `22b5eb9`), built
from scratch for this verification and left standing rather than torn down. New
harness `scripts/local/prove_raise.py --deployment scquota`, following
`prove_ceiling.py`'s conventions: it refuses the production prefix by name, seeds a
dedicated tenant plus two DIFFERENT identities (a requester holding only
`limits:raise-self` and an approver holding `limits:approve`), and reads every
figure back from the deployment's own `TenantBudgetsRepository.pool_summary` and
`QuotaEventsRepository.get_grant` rather than recomputing it locally.

| Claim | Evidence | Tier | Not covered |
|---|---|---|---|
| **A tenant at its ceiling is refused, and the refusal names the raise path** | `402 request_does_not_fit_pool_limit`, `grantable: true`, `raise_hint.candidates` naming `wall=tenant_dollar_pool` / `blocker=tenant_pool` | **deployed-live** | only the "does not fit at all" refusal shape was forced (headroom-exhausted-but-nonzero was exercised only in the `prove_ceiling.py` regression run below) |
| **A raise approved for LESS than asked reaches the requester's own view as the approved figure, not the asked one** | approver granted 10,000 of 20,000 microUSD asked; `GET /me/limit-raises` for the requester returns `approved_amount_microusd: 10000` beside `asked_amount_microusd: 20000` | **deployed-live** | — |
| **Work refused before the grant is admitted after it** | the same oversized probe's cost class, admitted `200` once the grant raised the ceiling from 20,000 to 30,000 microUSD | **deployed-live** | — |
| **An operator setting a figure while a grant is live lands where they meant** | `PUT .../pool-budget` while the grant was ACTIVE returned `baseline_microusd: 520000`, `pool_granted_microusd: 10000`, `pool_limit_microusd: 530000` — the new baseline, not the baseline plus the grant swallowed into it | **deployed-live** | — |
| **Resending the figure already in force (baseline + a live grant) is refused, not silently doubled** | `409 figure_includes_active_grant`, carrying `figure_microusd`, `pool_limit_microusd`, `pool_granted_microusd`, `baseline_microusd` | **deployed-live** | — |
| **Negative headroom is reported signed and unclamped, to both sides** | baseline forced to 0 against 50,043 microUSD of injected committed spend: `remaining_microusd: -40043`, `over_ceiling_microusd: 40043` (exact negation, both fields present in one response) | **deployed-live** | the committed spend was injected directly on `pool_headroom_microusd`/`pool_settled_microusd` as a test fixture (a short model reply settles for far less than a `max_tokens` bound, so relying on real generation to reach a large settle was not viable in the time available) rather than reached by driving enough real traffic through the gateway |
| **The grant expires with nobody acting, and the ceiling returns to EXACTLY the pre-grant figure** | forced the grant's `expires_at` five seconds into the past; CloudWatch Logs (`/aws/lambda/scquota-quota-grant-sweeper`) show the real EventBridge schedule (`rate(5 minutes)`) firing autonomously at `2026-09-02T00:56:00.410Z` with `grants_revoked: 1`, `grant_revocation_late_seconds: 197` — **3.5 seconds before** this run's own manual-invoke fallback, which found nothing left to do (`grants_revoked: 0`). Post-sweep `pool_limit_microusd` was exactly 20,000, the recorded baseline before the grant | **deployed-live** | the harness's own schedule-detection code had a bug on this run (it gave up after one premature CloudWatch Logs read, so it self-reported "not witnessed" and ran the fallback anyway) — fixed in the script; the finding above is from a retrospective `filter-log-events` query, not from the script's own field |
| **The next admission after expiry is refused again, in terms of the expiry rather than repeating the first refusal verbatim** | **not met**: the post-expiry refusal is byte-identical to the pre-grant refusal (`reason`, `message`, `raise_hint` all equal) | **deployed-live** | this is the shipped behaviour, not a bug this run found by accident — `mvp._pipeline._err_402`'s docstring cites A-08-credit ("never leak precise balances/limits to the caller... a generic message") as the reason the body is deliberately generic, and F2's "Not in this part" explicitly excludes a `/me` grant join a client could use to tell the two refusals apart. The plan this run followed expected a distinguishing message; the shipped contract does not promise one |
| **The dollar-ceiling-under-concurrency claim still holds after this change rewrites how the ceiling is computed** | `prove_ceiling.py --deployment scquota`: 1 of 2 repetitions witnessed an overlapping reservation window, 6 contended attempts, 11 admits / 3 pool-headroom refusals, no ledger-reconstructed state showed settled plus reserved above the limit, the headroom identity held | **deployed-live** | 6 contended attempts is a small number for the reason stated in the 2026-08-29 section above; the first attempt (default `--headroom-microusd`) produced zero refusals because the occupier route (`openai.gpt-5.6-sol`, gated by `STRATOCLAVE_CODEX_ENABLED`) was not enabled on the first ECS deploy and had to be redeployed with it on — see the deploy-defects note below |
| **The four DynamoDB facts R15 rests on (ADD creates a missing attribute; a condition on a missing attribute fails; a negative ADD is not floored; cross-table `TransactWriteItems` is atomic)** | re-measured against two throwaway tables created in the real account and deleted immediately after: all four matched moto exactly (`ADD` → 5, `ConditionalCheckFailedException`, `-95`, `TransactionCanceledException` with no partial write) | **deployed-live** (real DynamoDB, not moto) | single trial per fact; no divergence from moto was found, which is itself the result — F4's moto-based facts were not hiding a difference |
| **Ledger latency floor (server-side `TransactWriteItems` span), re-measured** | `n=40`, `POST /api/mvp/billing/authorize`, current commit, on the `scquota` ECS task (**1024 CPU / 2048 MiB — 4× the 256/512 task the 2026-07-19 benchmark in [`ledger-latency.md`](benchmarks/ledger-latency.md) used**): `p50=25.0 ms, p90=30.0 ms, p99=45.6 ms, max=45.6 ms`, read from the `ledger_transact_latency` structured log line the app itself emits | **deployed-live** | comparable in *shape* to the documented floor (p50 20 ms / p99 58 ms, n=3,734) despite 4× the CPU — consistent with that document's own finding that CPU is not the limiter — but n=40 vs n=3,734 is not the same statistical weight, and the task-size difference means this is not a controlled re-run of that number. **The end-to-end "A-layer" figure and the full concurrency sweep (c=2/8/16) were NOT re-measured**: the documented methodology requires a same-region, no-WAN load generator, which this pass did not provision — a client on this laptop measures WAN latency, not the ledger, and would misrepresent the number if reported as a re-run |

**Deploy-time defects found only by attempting this deploy, not by reading the code**:

1. `scquota-quota-reconciler`'s CDK-Nag findings are unsuppressed — `bin/iac.ts` already
   says so in a comment ("NOTE for whoever reads this next: `quotaReconcilerStack` is
   missing from this list... nag findings on a context-gated stack are invisible until
   somebody deploys it") — and the documented `npx cdk deploy ... quotaReconciler=true`
   path fails outright with three `AwsSolutions-IAM4`/`IAM5` errors before it deploys
   anything. Worked around with the existing `CDK_NAG=off` escape hatch.
2. `QuotaGrantsStack` and `QuotaReconcilerStack` both import their Lambda's CloudWatch Logs
   log group with `logs.LogGroup.fromLogGroupName` (an import of something assumed to
   already exist) rather than creating it, but a Lambda's log group is created lazily on
   its first invocation — which has not happened on a fresh deploy. Both stacks'
   `AWS::Logs::MetricFilter` resources therefore `CREATE_FAILED` with "The specified log
   group does not exist" on the very first deploy of a fresh account, and the whole stack
   rolls back. `certificate-scheduler-stack.ts`, the sibling stack the same convention
   note points to, gets this right (`new logs.LogGroup(...)`). Worked around by
   pre-creating the three log groups (`aws logs create-log-group`) before deploying.
3. The three new scheduled Lambdas (`quota-grant-sweeper`, `quota-reconciler`,
   `quota-period-rollover`) are wired to `DockerImageCode.fromEcr(lambdaRepository, {tagOrDigest: lambdaImageTag})`
   pointing at the **same** ECR repository and, by default, the **same** `:latest` tag
   the ECS backend uses — but they need an image built from `backend/Dockerfile.lambda`
   (a different base image, `public.ecr.aws/lambda/python:3.11`, with the Lambda Runtime
   Interface Client), not the `backend/Dockerfile` image the ECS service runs. Nothing in
   the repository builds or pushes that image, and nothing sets `LAMBDA_IMAGE_TAG`; a
   deploy that follows only the documented path would either fail to find a Lambda-shaped
   image or — worse, if an operator ever pushes one to `:latest` — silently break the ECS
   service, since both point at the same tag. Worked around by building
   `backend/Dockerfile.lambda` under a separate tag and passing `LAMBDA_IMAGE_TAG` at
   deploy time; confirmed working by invoking all three Lambdas directly.
4. Unrelated to F1/F2 but hit while reaching them: `iac/scripts/build-and-push.sh` never
   passes `--platform` to the container build. On an arm64 build host this silently
   produces an arm64 image that Fargate (x86_64 by default) cannot run — surfacing only
   as `exec /app/entrypoint.sh: exec format error` in the task's CloudWatch logs, exactly
   as `05-verify/PLAN-real-machine.md` warned. Compounding it: the ECR repository is
   `imageTagMutability: IMMUTABLE`, so once a wrong-architecture image is pushed as
   `:latest`, pushing a corrected image under the same tag is rejected outright
   (`400 Bad Request`) until the old tag is explicitly deleted first — the documented
   "just rebuild and push" recovery path does not work as written.

### The full change (F1+F2+F3+F4+the raise/status/scan fixes), re-verified after re-image (2026-09-02, later)

The same `scquota` deployment, re-imaged from `epic/quota-integration` at `8b8983a` (backend
digest `sha256:2941bd6c…`, frontend bundle `index-D6eD_V6P.js`, Lambda image tag
`lambda-20260902-143116`) — the F1+F2-only image the section above ran is gone. This is the
**most important row in this whole file**: the one item the previous pass recorded as
**"not met"** turned out to already be fixed by F3, and this pass is what finally witnessed it.

| Claim | Evidence | Tier | Not covered |
|---|---|---|---|
| **The next admission after expiry is refused again, in terms of the expiry — reversing the 2026-09-02 (earlier) "not met" finding** | Pre-grant refusal: `402 credit_exhausted`, `message` ends "...Reduce the request size or ask your admin to raise the budget.". Post-expiry refusal on the identical oversized probe: `402 credit_exhausted`, `message` ends "...This request is $0.02 short of this tenant's pool. **A grant that covered this wall expired recently — ask your admin whether it should be renewed rather than filing a new request for the same amount.**", and `raise_hint.candidates[0].grant_expired: true`. `item5_reason_identical_to_item1: false`, `item5_message_mentions_expiry: true` | **deployed-live** | one wall (`tenant_dollar_pool`); the cold path that adds this line is described as running only after the refusal is already decided |
| **The grant expires with nobody acting, and the real EventBridge schedule (not a manual invoke) is what revoked it** | Two independent witnesses. (a) A direct `filter-log-events` query against `/lambda/scquota-quota-grant-sweeper` found the schedule ticking every ~5 min (`sweeper_ran` at `:50:50`, `:55:56`, `:00:51` UTC) and firing autonomously at `2026-09-02T05:40:51Z` with `grants_revoked: 1, grant_revocation_late_seconds: 78` for this run's own grant — 10 s **before** the manual-invoke fallback ran and found nothing left (`grants_revoked: 0`). (b) After fixing the harness bug below and re-running end to end on a second grant: `item45_scheduled_witnessed: true`, `"1 sweeper_ran heartbeat(s) in CloudWatch Logs..., grant now status=EXPIRED"` — no manual invoke needed this time | **deployed-live** | — |
| **The ceiling returns to EXACTLY the pre-grant figure** | `item4_ceiling_after_expiry: 20000` / `20043` on the two runs, matching each run's own `baseline_ceiling`, `item4_ceiling_returns_to_exact_baseline: true` both times | **deployed-live** | — |
| **A raise approved for LESS than asked reaches the requester's own view, and now also survives an actual browser round-trip through the real Cognito Hosted UI and the real React app** | API: `approved_amount_microusd: 10000` beside `asked_amount_microusd: 20000`. Browser (Playwright, real login, real form submit, real approval form, real page re-fetch): screenshot shows "$0.01 を承認、Sep 9, 2026 06:23 UTC に失効 / 承認者: 64287428-…" against a $2.00 ask | **deployed-live** | — |
| **status is uppercase on the wire, as stored** | `"status": "APPROVED"`, `"status": "ACTIVE"`, `"status": "EXPIRED"`, `"status": "PENDING"` read directly off real HTTP responses (`/api/mvp/me/limit-raises`, `/api/mvp/admin/limit-raises`, the grant record) | **deployed-live** | — |
| **The 500-grant read cap is gone on the correctness path, and the human-facing page reports truncation instead of hiding it** | 501 synthetic grant rows written directly to the real `scquota-quota-events` table under a throwaway tenant: `list_grants_for_tenant` (no cap) returned all 501; `list_grants_for_tenant_page(limit=500)` returned exactly 500 with `truncated=True`. Cleaned up immediately after (0 rows remain) | **deployed-live** (real DynamoDB, not moto) | one trial; the 501st-grant scenario itself was synthetic data (writing 501 real limit-raise requests through the API is gated to one per tenant per day) — the READ code under test is the real shipped method against the real table either way |
| **An operator setting a figure while a grant is live lands where they meant** | `PUT .../pool-budget` while ACTIVE returned `baseline_microusd: 520000`, `pool_granted_microusd: 10000`, `pool_limit_microusd: 530000`; also witnessed live in the browser as "$0.02 (ベースラインからの導出値)" against a seat-derived $400.00 that the smaller manual figure overrides | **deployed-live** | — |
| **Negative headroom is reported signed and unclamped** | `remaining_microusd: -40080`, `over_ceiling_microusd: 40080` (exact negation), both fields in one response | **deployed-live** | committed spend was injected directly on the pool counter, same as the 2026-09-02 (earlier) run — reaching a large settle through real generation was not viable in the time available |
| **certificate-scheduler's default route (`CERT_TENANT_IDS` unset) still works after the scheduled-job wiring fix** | Direct invoke of `scquota-certificate-issuer` with a synthetic EventBridge `time`: `_resolve_tenant_ids()` enumerated the real `scquota-tenants` table, `expected: 5`, all 5 skipped for the honest reason `no_vsr_acted_traffic`, `unaccounted: 0` | **deployed-live** | no tenant with real VSR-acted traffic in this window, so the issued-certificate path itself was not exercised, only the enumeration + accounting path |
| **The dollar-ceiling-under-concurrency claim still holds** | `prove_ceiling.py --deployment scquota --repetitions 3 --probes 12 --headroom-microusd 13500`: 2 of 3 repetitions witnessed overlap, 24 contended attempts, 9+9 admits / 4+11 pool-headroom refusals across the witnessed repetitions, no ledger-reconstructed state showed settled plus reserved above the limit, headroom identity held. The default parameters (`--headroom-microusd 16000 --probes 6`) produced zero refusals on this run — the occupier's own bound (peak ≈ 12,377 µUSD) never left enough of a 16,000 µUSD headroom for 6 small probes to exhaust; this is the harness's documented `INCONCLUSIVE` outcome working as designed, not a defect, and disappeared once the pool was sized close to the occupier's bound | **deployed-live** | 24 attempts is a small number, same caveat as every prior concurrency row |
| **A human can actually get through the console** | Real Playwright browser, real Cognito Hosted UI, real backend: two Cognito personas (`role=user` requester via `STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL`-seeded admin + `POST /api/mvp/admin/users`, `role=admin` approver via the bootstrap seed itself). All three F3 console surfaces visited and screenshotted: `/me/limit-raises` (wall-status-card, request form, own-request history), `/admin/tenants/default-org/limit-raises` (pool decomposition + pending-request approval form), `/admin/tenants/default-org/limit-grants` (grants inventory, revoke button). Screenshots: `~/tmp/stratoneed/console-screenshots/01`–`11` | **deployed-live** | login was completely broken until a deploy-script defect (below) was fixed mid-run; this row is therefore also the reproduce/verify pair for that defect |

**Two real defects found only by attempting this deploy end to end, neither specific to F1–F4, both fixed in this branch:**

1. **`iac/scripts/deploy-all.sh` generated a config that could never log in.** `CognitoStack.cognitoDomainUrl` (`iac/lib/cognito-stack.ts:146`) already includes the `https://` scheme; `deploy-all.sh` wrote `"domain": "https://$COGNITO_DOMAIN"` into `dist/config.json` anyway, producing `https://https://scquota-auth-….amazoncognito.com`. Clicking "Sign in with Cognito" therefore navigated to a URL Chrome could not even resolve (`net::ERR_NAME_NOT_RESOLVED`) — the console has been unreachable by anyone who deployed with this script, on any prefix, until this run. Reproduced live (`curl https://…/config.json` showed the doubled scheme; Playwright showed the failed navigation), fixed by dropping the redundant prefix, redeployed, and reproduced the fix live (successful login, screenshots above).
2. **The frontend silently downgraded every structured backend refusal to an opaque HTTP status string.** `credit_exhausted`, `grant_cap_exceeded`, `limit_raise_daily_slot_occupied`, `figure_includes_active_grant`, … all send `detail` as a JSON **object** carrying its own `.message`; `frontend/src/lib/api.ts`'s `jsonRequest` only kept `.detail` as a string when the body's `detail` **was** a string, so every page that falls back to `e.detail ?? e.message` (both `LimitRaiseApproval.tsx` and `MeLimitRaises.tsx`) showed a bare `"422 Unprocessable Entity"` / `"409 Conflict"` instead of the message the backend wrote for exactly this case — the `isCreditExhaustedDetail` guard in `api.ts` exists for this and is dead code, called from nowhere. Reproduced live twice (an over-cap approval, a same-day resubmission) showing the opaque text; fixed at the one shared parsing site so every current and future caller benefits; re-reproduced live post-fix and confirmed the legible message now renders (screenshot `10-requester-second-submit-error.png`: "You have already filed a limit raise today and it has not been resolved yet.").

**One defect found in the verification harness itself, also fixed:** `scripts/local/prove_raise.py` queried CloudWatch Logs at `/aws/lambda/<sweeper-fn>` (the Lambda-default log group) to detect the real schedule, but `QuotaGrantsStack` creates the group itself at `/lambda/<sweeper-fn>` (`iac/lib/quota-grants-stack.ts:80`) for the same reason `DEPLOYMENT.md` documents for the other scheduled jobs. The query threw `ResourceNotFoundException` every time, so the harness always reported "not witnessed" and ran its manual-invoke fallback — even on the first run above, where the real schedule DID fire on time and the manual invoke found nothing left to do. Fixed the prefix; the second run then set `item45_scheduled_witnessed: true` with no fallback.

**Also fixed while reaching the above, still real defects, both pre-existing and unrelated to F1–F4:** `iac/scripts/build-and-push.sh` (a) never passed `--platform` to the ECS backend image build itself (only the Lambda image build did), so this arm64 build host would have silently produced an incompatible image were it not for the fix, exactly the failure mode `05-verify/PLAN-real-machine.md` warned about; and (b) re-pushing `:latest` against the `IMMUTABLE` ECR repository 400s outright, so the script now best-effort deletes the existing `:latest` tag before pushing, matching the recovery the previous pass had to do by hand. `docs/DEPLOYMENT.md`'s "Post-deploy: first admin" section was also re-verified and found completely stale: `POST /api/mvp/admin/users` unconditionally requires an authenticated actor with `users:create` (`require_permission`), so `bootstrap-admin.sh`'s unauthenticated step 3 401s with `{"detail":"Missing bearer token"}` before the documented `ALLOW_ADMIN_CREATION` gate is ever reached. The real, working path — `STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL`, seeded once at startup, password in Secrets Manager at `${prefix}/bootstrap-admin-temp-password` — is what this run actually used, and is now what the doc (and `deploy-all.sh`'s own "Next steps" banner) says.

**Left unverified, named rather than padded:** the `benchmarks/ledger-latency.md` end-to-end figure and the c=2/8/16 concurrency sweep still need a same-region, no-WAN load generator (e.g. a small EC2 instance in `us-east-1`); not provisioned this pass for lack of remaining time in the window, so the debt stands exactly as the 2026-09-02 (earlier) row states it. No number for it is reported here.

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

### Formal coverage of the money arithmetic (added 2026-08-28)

Completeness here is measured as **the number of assumptions still merely
trusted**, not the number of files. Each property wants three things: a proof, a
counterexample that deletes the guard and shows the property break, and a
differential link to the real Python. A row with a proof and no differential link
is a claim about an encoding.

| Property | Proof | Counterexample | Differential | Trusted assumption left |
|---|---|---|---|---|
| Ceiling soundness **given** per-component estimate dominance | `test_rating_formal_z3.py::test_g1_dominance_implies_actual_not_above_reserved`, `…settle_never_lowers_headroom_under_dominance`, and the composed rounding theorem `…test_g1_the_composed_monotone_rounding_theorem` | `…sanity_without_dominance_ceiling_breaks` (one undominated component), `…sanity_repricing_at_settle_breaks_the_ceiling`, `…sanity_composed_theorem_needs_a_nonnegative_rate` | `test_rating_differential.py::test_dominance_holds_on_the_components_the_estimator_prices` | **the premise itself is FALSE today — see the row below** |
| The rating fold is a function of the recorded components | `…test_g3_the_fold_is_a_function_of_the_recorded_components` | `…sanity_an_unconstrained_quotient_makes_the_fold_ambiguous` | `test_rating_differential.py::test_rating_total_matches_an_independent_recomputation` (driven from the inputs, not from the returned components, and it checks the components are the ones asked for at the rates asked for), `…test_t1_the_event_recomputes_from_itself_alone` | none |
| Recording the rounding rule is load-bearing | `…test_g3_per_component_and_post_total_rounding_differ`, `…test_g3_ceil_never_undercharges` | `…test_g3_sanity_floor_undercharges` | `…test_an_unknown_rounding_policy_is_refused_not_guessed`, `…test_ceil_never_undercharges_the_exact_rational_cost` | none |
| Rounding is monotone, so token dominance carries to cost dominance | `…test_g3_ceil_is_monotone_in_the_numerator` (linear), `…test_g3_numerator_is_monotone_in_the_varying_factor` (over the reals) | `…sanity_ceil_monotonicity_needs_the_ordering`, `…sanity_numerator_monotonicity_needs_a_nonnegative_factor` | covered by the fold row | negative rates are out of scope by assumption B3 |
| Pinning is sufficient for the ceiling | `test_pricing_pinning_z3.py::test_g2_a_settle_rate_at_or_below_the_pinned_rate_preserves_the_ceiling` | `…sanity_a_settle_rate_above_the_pinned_rate_breaks_it` | `test_contract_price_identity.py::test_a_rate_flip_between_reserve_and_settle_does_not_move_the_charge` — flips `CURRENT` between reserve and settle and asserts the terminal cites, and charges at, the admitted version | none for the inline settle path. Late settle and external capture restore the same snapshot from the ledger; that they do is code review, not a differential yet |
| A version read after its rows cannot dangle | `…test_g2_the_write_order_makes_a_dangling_read_impossible_in_time` | `…sanity_flipping_before_writing_dangles` | absent (needs the store; Phase 2) | row immutability, assumption C1 |
| Sentinel biconditional: a real version **iff** a snapshot priced it | `…test_g4_real_version_stamped_if_and_only_if_snapshot_priced_it`, `…test_g4_each_cause_gets_its_own_label` | `…sanity_stamping_current_on_snapshot_failure_breaks_it`, `…sanity_one_shared_sentinel_collapses_the_causes` | `…test_the_modelled_sentinels_are_the_shipped_sentinels` (the constants are real and pairwise distinct) | that the stamping code implements the modelled rule |
| No overflow; refunds cannot go negative | `test_rating_formal_z3.py::test_g6_no_overflow_within_realistic_bounds`, `…refund_cannot_drive_settled_negative` | `…sanity_unbounded_tokens_overflow`, `…sanity_unbounded_refund_goes_negative` | absent | the stated bounds |
| Zero rate / zero tokens cost zero, and the clamp is on tokens | `…test_g7_zero_side_costs_nothing`, `…test_g7_negative_tokens_are_clamped_not_credited` — but note these read back the encoding's own axiom, so on their own they are circular | — | `test_rating_differential.py::test_the_clamp_is_on_tokens_and_a_negative_rate_is_refused`, `…test_no_usage_report_can_mint_a_credit_at_a_nonnegative_rate` — this is what makes the boundary claims non-circular | none. A negative rate used to mint credit (`_mtok_cost(1000, -5_000_000)` returned −5,000) with the document's contents as the only defence; it is now refused at both boundaries — `PricingConfigRepository.set_rates` rejects the write and `_mtok_cost` refuses the charge |

**The limit no timing calculation removes.** A charge landing at Bedrock and the ledger
write recording it are not atomic, and cannot be made so from this side. If Bedrock
accepts and bills a request and the gateway process then dies, the hold is never settled,
the reaper eventually releases it, and the real external spend disappears from the
ledger — after which later admissions can carry the pool past its limit while every
invariant this work proves still holds, because the ledger no longer contains the charge.
Getting the reap timeout right narrows the window; it does not close it. This is the
honest ceiling on the ceiling, and it is why the guarantee is stated with named failure
modes rather than as an absolute. Both reviewers arrived at it independently, one of them
naming it as the residual after every other item is done.

**The ceiling can be made hard, and the mechanism already exists.** Two
independent reviewers concluded a hard ceiling was unobtainable because prompt-cache
writes are decided by the provider mid-call. That was an efficiency argument
mistaken for an impossibility argument, and it does not survive the numbers. The
pool reserve is already a conditional write on `pool_headroom_microusd >= :amt`, so
an insufficient pool already refuses admission and no upstream call happens; the
ceiling is soft for exactly one reason, which is that `:amt` is an estimate rather
than a bound. And a bound is computable, because the provider cannot bill for
content it was never sent: pricing every input-side token at
`max(input, cache_read, cache_write)` covers cache behaviour with no assumption
about provider choices, at 1.25x on that leg — not the "one fifth to one eighth of
the concurrency" that was claimed.

`test_reservation_bound_formal_z3.py` proves the implication: with a sound
reservation, settle cannot raise `settled + reserved`, so the existing transaction
becomes a hard ceiling with nothing else changed. It also proves the reaper guard —
a hold released before its own settle lets a second request borrow the same
headroom — and it records what the bound cannot cover. Measured cost of a bound
that assumes nothing about the tokeniser (UTF-8 bytes at the worst input-side
rate), against today's estimate with output at 2,000 tokens: English 3.48x,
Japanese 9.60x, emoji-mixed 4.20x. That is in-flight admission headroom, not money,
since settle returns the difference.

Two holes no proof can close, and they bound the claim rather than defeat it: image
tokens scale with pixels rather than bytes, so a few hundred bytes of flat-colour
PNG can be thousands of tokens; and tool scaffolding plus server-side tool results
are billable tokens that were never in the bytes the gateway sent. A bound is sound
only inside a stated content envelope, and enforcing that envelope at the door is
an implementation obligation, not a theorem.

**The defect this found, before any of it was implemented, and what closing it
required.** `rate_usage` charged four components (input, output, cache_read,
**cache_write**) while `estimate_cost_microusd` priced three (fresh input, warm input
at the cache-read rate, output). There was no cache-write leg, and in the shipped rate
document cache_write is priced **above** input, so a request that wrote prompt cache
settled above what was reserved for it. Measured on the shipped `default` rates with
1,000 input / 100 output / 5,000 cache-write tokens: reserved 7,500 microUSD, settled
38,750 — an overrun of 31,250. The dollar pool has no overrun path of its own: the
token dimension has `credit_overrun` with a top-up and clamp, the pool books the
actual.

Two things were wrong, both of them in code written to prevent exactly this.
`worst_input_side_rate_microusd`, whose job was to make the bound cover any caching
behaviour, hard-coded the same three fields while its docstring claimed a fifth leg
could not slip past it. And the bound rounded each group's total up once where settle
rounds each leg up — ceiling is not subadditive, so the "sound bound" was not an upper
bound on the settle: three input-side legs at 1 microUSD/MTok with one token each
settle at 3 while the total rounds to 1.

Fixed by declaring the legs once, in `mvp.pricing.BILLABLE_LEGS`, with the group each
belongs to; the worst-rate helper, the settle rater and both bounds read it, and the
slack is derived as `min(legs, tokens) - 1` per group rather than written as a number.
`backend/tests/test_billable_legs_registry.py` fails if a billable rate column has no
leg, brute-forces every partition of up to six input-side tokens over four rate sets,
and carries a 400-example property test that no partition settles above the bound.
Premise (P) now holds on the **rate** axis. On the **token** axis it holds only where
the token count is bounded from bytes — the `measured`, `shadow` and `enforced` states
— because an estimate is not a bound, which is pinned by
`test_rating_differential.py::test_the_estimate_path_is_not_a_bound_on_the_token_count`.

**A behaviour this removed.** SAAR passed `warm_prefix_tokens` into the estimator and
that many input tokens were re-priced at the cheaper cache-read rate, so staying on a
warm model reserved less than switching to a cold one, and a switch that would breach
the pool was gated at the 402 while a stay fitted. The gateway does not decide which
leg a token settles at, so that was a reservation below the possible charge. The
estimator no longer accepts cache evidence at all; the warm preference is expressed by
candidate order and the money claim about it by `switch_cost_delta_microusd`, which is
a comparison recorded on the decision rather than an amount that gates admission. The
two SAAR invariants that used to prove "warm ≤ cold up to a bounded rounding slack"
were replaced by ones that prove no SAAR state can move an admission amount.

Assumptions deliberately left trusted, with the reason: DynamoDB's transactional
atomicity and single-item serialisation (AWS's documented semantics — proving it
here would be waste); and pricing-row immutability (enforced by an
`attribute_not_exists(sk)`-style condition on each row Put in `set_rates`, not by
an IAM boundary).

Two assumptions that used to be listed here are now mechanisms rather than
trust: a rate document holding a negative rate is refused at the write and at the
charge, and that the settle honours the pinned rate is checked by a differential
that flips `CURRENT` mid-request. A third has moved the other way and is stated
where it belongs: a priced reservation whose rate cannot be frozen is REFUSED
before the provider is called, so there is no longer a live-rate settle path to
assume anything about.

**Bridges still missing, named rather than implied.** Rows above that rest on
models written inside the test files are proofs about those models until a
differential drives the real code:

- **Snapshot pinning — discharged for the inline settle.**
  `test_contract_price_identity.py::test_a_rate_flip_between_reserve_and_settle_does_not_move_the_charge`
  writes a version, admits a request, flips `CURRENT` to a version priced ten times
  higher, settles, and asserts the terminal cites the admitted version and charges
  at its rates. What is still unbridged is the same property for LATE_SETTLE and
  for external capture, which restore the snapshot from the ledger rather than from
  a live context.
- **Sentinel stamping.** G4 proves properties of a stamping rule written in the
  test file. The bridge is only that the shipped sentinel constants exist and are
  pairwise distinct. One of the modelled causes no longer exists in the code — a
  reservation whose rate could not be frozen is refused rather than stamped
  `snapshot-failed` — so the rule to bridge is now narrower than the one G4
  models, and `test_contract_price_identity.py::test_the_snapshot_failed_sentinel_is_no_longer_reachable`
  pins that nothing stamps the retired label.
- **Per-component dominance in the ledger.** Dominance is checked at the total,
  not per component against the ledger's own component records.

Both reviewers of this work (Fable 5 and Codex, run independently) found earlier
drafts of these files carrying vacuous proofs — an expression asserted equal to
itself, sanity checks that were satisfiability of free variables, and a
composition asserted only in prose. Those are fixed and the fixes are noted in the
docstrings so the failure mode stays visible. The lesson is recorded here because
it applies to the whole formal layer: a proof whose sanity counterexample has
nothing to search is not evidence.

### Dark code: what is proven about a path that is not switched on (added 2026-08-30)

Two routing features are built and dark, and this is where their claims stop, so a
reader does not have to infer it from a status column.

**The vLLM Semantic Router integration (`mvp/sr/`).** The money invariants for this
path are proven (Z3 + Hypothesis) against a fake-SR harness, which means the
reserve/settle behaviour is verified for every decision the port can return —
including a refusal and a timeout — but nothing has yet been verified against the
real `/api/v1/eval` surface, because the client that would call it is unwritten and
`decide()` returns `NO_DECISION`. A proof against a harness is a proof about the
port's contract, not about the router's behaviour.

**Session-aware routing (SAAR).** Superseded by the real router's own session-aware
policy, retained because its provider-state lock (`previous_response_id` pinning) is
a correctness need the replacement does not cover. Its invariants were narrowed after
the incident recorded above: they now prove that no SAAR state can move an admission
amount, rather than bounding how much it may move one. Nothing about SAAR's routing
policy is claimed to be better than the router it stands in for.

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
