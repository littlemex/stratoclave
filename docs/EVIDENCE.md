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
| Rounding is monotone, so token dominance carries to cost dominance | `…test_g3_ceil_is_monotone_in_the_numerator` (linear), `…test_g3_numerator_is_monotone_in_each_factor` (over the reals) | `…sanity_ceil_monotonicity_needs_the_ordering`, `…sanity_numerator_monotonicity_needs_a_nonnegative_factor` | covered by the fold row | negative rates are out of scope by assumption B3 |
| Pinning is sufficient for the ceiling | `test_pricing_pinning_z3.py::test_g2_a_settle_rate_at_or_below_the_pinned_rate_preserves_the_ceiling` | `…sanity_a_settle_rate_above_the_pinned_rate_breaks_it` | **absent** — that settle does not re-read `CURRENT` is a code property no SMT run can establish | **that the code honours the pinned rate** |
| A version read after its rows cannot dangle | `…test_g2_a_version_read_after_its_rows_cannot_dangle` | `…sanity_flipping_before_writing_dangles` | absent (needs the store; Phase 2) | row immutability, assumption C1 |
| Sentinel biconditional: a real version **iff** a snapshot priced it | `…test_g4_real_version_stamped_if_and_only_if_snapshot_priced_it`, `…test_g4_each_cause_gets_its_own_label` | `…sanity_stamping_current_on_snapshot_failure_breaks_it`, `…sanity_one_shared_sentinel_collapses_the_causes` | `…test_the_modelled_sentinels_are_the_shipped_sentinels` (the constants are real and pairwise distinct) | that the stamping code implements the modelled rule |
| No overflow; refunds cannot go negative | `test_rating_formal_z3.py::test_g6_no_overflow_within_realistic_bounds`, `…refund_cannot_drive_settled_negative` | `…sanity_unbounded_tokens_overflow`, `…sanity_unbounded_refund_goes_negative` | absent | the stated bounds |
| Zero rate / zero tokens cost zero, and the clamp is on tokens | `…test_g7_zero_side_costs_nothing`, `…test_g7_negative_tokens_are_clamped_not_credited` — but note these read back the encoding's own axiom, so on their own they are circular | — | `test_rating_differential.py::test_the_clamp_is_on_tokens_and_a_negative_rate_credits`, `…test_no_usage_report_can_mint_a_credit_at_a_nonnegative_rate` — this is what makes the boundary claims non-circular | **a negative rate in the document would mint credit**: `_mtok_cost(1000, -5_000_000)` returns −5,000. Nothing in the rating path rejects one; the defence is that the document has never held one |

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

**The defect this found, before any of it was implemented.** `rate_usage` charges
four components (input, output, cache_read, **cache_write**) while
`estimate_cost_microusd` prices three (fresh input, warm input at the cache-read
rate, output). There is no cache-write leg, and in the shipped rate document
cache_write is priced **above** input, so a request that writes prompt cache
settles above what was reserved for it. Measured on the shipped `default` rates
with 1,000 input / 100 output / 5,000 cache-write tokens: reserved 7,500 microUSD,
settled 38,750 — an overrun of 31,250. Premise (P) of the ceiling theorem is
therefore false in the shipped implementation, and the dollar pool has no overrun
path of its own: the token dimension has `credit_overrun` with a top-up and clamp,
the pool books the actual. Pinned as
`test_rating_differential.py::test_estimate_omits_the_cache_write_leg`, an
`xfail(strict=True)` so that fixing the estimator fails the suite and forces the
marker out.

Assumptions deliberately left trusted, with the reason: DynamoDB's transactional
atomicity and single-item serialisation (AWS's documented semantics — proving it
here would be waste); pricing-row immutability (a discipline in `set_rates`, not a
condition expression or an IAM boundary); that no rate document contains a
negative rate; and that the settle code honours the pinned rate rather than
re-reading `CURRENT`.

**Bridges still missing, named rather than implied.** Two rows above rest on
models written inside the test files, and until a differential test drives the
real code they are proofs about those models:

- **Snapshot pinning.** No test flips `CURRENT` between reserve and settle and
  checks that the terminal cites the reserve-time version. The Z3 file establishes
  that pinning is sufficient and that violating it breaks the ceiling; it cannot
  establish that the code pins. This is the next discharge and it needs the store.
- **Sentinel stamping.** G4 proves properties of a stamping rule written in the
  test file. The bridge is only that the shipped sentinel constants exist and are
  pairwise distinct. A test that drives the real settle path through snapshot
  failure, an external fixed amount, and a legacy reservation — asserting each
  stamps its own sentinel — does not exist yet.
- **Per-component dominance in the ledger.** Dominance is checked at the total,
  not per component against the ledger's own component records.

Both reviewers of this work (Fable 5 and Codex, run independently) found earlier
drafts of these files carrying vacuous proofs — an expression asserted equal to
itself, sanity checks that were satisfiability of free variables, and a
composition asserted only in prose. Those are fixed and the fixes are noted in the
docstrings so the failure mode stays visible. The lesson is recorded here because
it applies to the whole formal layer: a proof whose sanity counterexample has
nothing to search is not evidence.

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
