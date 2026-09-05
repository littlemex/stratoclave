# The contract this gateway is judged against

This document is the upstream object. Everything below is a clause the code must
satisfy, the guarantee level it holds at, and what makes that level true. For a
clause at **P** or **E** the third column names the test that fails if the clause
stops holding, and `test_contract_clauses_cite_real_tests.py` checks that the named
test exists — a citation that has drifted onto a deleted test is the failure this
document is most likely to have. For a clause at **B** the third column states the
configuration inside which it holds, and for one at **N** it states what is not
guaranteed; neither is a test, and neither is written as though it were. A clause at
P or E with no test is a statement about one commit, not about the project, so for
those levels the third column is part of the clause rather than a note on it.

It exists because reviewing slices of the code kept surfacing new instances of the
same defect shapes: a value the gateway could not know, substituted by a default;
the identity of a subject taken from the spelling a caller used; two mechanisms
each able to end the same reservation. Fixing an instance does not close a class. A
clause plus an enforcement test does.

## Guarantee levels

| Level | Meaning |
| --- | --- |
| **P** | Machine-checked over a model (Z3), with the model's boundary stated in [EVIDENCE.md](../EVIDENCE.md). |
| **E** | A test in the suite fails if the clause is violated — including a static check where the violation is a shape rather than a value. |
| **B** | True inside a stated configuration, and the boundary is documented wherever the claim appears. |
| **N** | Deliberately not guaranteed, stated so no reader infers it. |

A clause at **B** is honest. A clause claimed unconditionally in the docs while
holding only at **B** in the code is the defect [C10](#c10--claims) is about.

**The level answers "what can this code do", not "what does a default deployment
do."** These are not the same question, and where a clause's answer differs between
them the clause says so in its own row rather than leaving a reader to assemble it
from the flag documentation.

For the two money flags they now agree. `STRATOCLAVE_HARD_CEILING_GATE` and
`STRATOCLAVE_UNOBSERVED_HOLDS` both default ON, so the default artifact exhibits the
properties they gate: admission is priced from a byte-count bound rather than an
estimate, and an outcome the gateway could not observe keeps its reservation instead
of being handed back as though the call were free. Both remain configurable off, which
is a **B** in the opposite direction — an operator who sets either flag falsy gets the
weaker behaviour back, deliberately.

---

## C1 — Admission

No provider call is made unless every limit that applies to the request has already
been decremented in one atomic, conditional write against the authoritative store.

| Clause | Level | Enforced by |
| --- | --- | --- |
| **C1.1** The decrement and the admission decision are the same write; no read-then-write window admits two requests past one limit. | E, with a **P** subset | `test_billing_formal_z3.py` proves it of the modelled POOL transition; the token and per-model-quota dimensions ride the same `TransactWriteItems` and are covered by `test_quota_cascade.py`, not by the proof |
| **C1.2** Every limit CONFIGURED for the request participates in that write. A limit present in configuration that contributes no transaction item is a bypass. | E | `test_quota_cascade.py`, `test_contract_model_policy.py` |
| **C1.3** An input the gateway could not read is never treated as an absent restriction. Unknown ⇒ fail closed. | E | `test_routing_inputs_not_invented.py`, `test_contract_price_identity.py` |
| **C1.4** The identity of a limit's subject is the subject, not the spelling the caller used. Respelling a model must not create a second, empty counter. | E | `test_routing_inputs_not_invented.py`, `test_contract_model_policy.py` |
| **C1.5** The amount reserved is an upper bound on what the settle can charge for the same request. | E in the default deployment; **B** if an operator turns the gate off | `test_billable_legs_registry.py`, `test_reservation_bound_formal_z3.py`, `test_hard_ceiling_pipeline.py::test_enforcement_active_iff_pool_row_exists` (which pins the default). Holds where a byte-count bound prices the reservation, and `STRATOCLAVE_HARD_CEILING_GATE` now defaults ON, so a pooled tenant gets the bound without configuring anything. Setting that flag falsy returns admission to the legacy estimate, which the design document proves is not a bound — an operator can choose that, and the clause then holds only at B for them. See [hard-ceiling.md](hard-ceiling.md) |
| **C1.6** A request is served only by a model inside the tenant's configured policy set. An empty admissible set is a refusal, never a widening. | E | `test_contract_model_policy.py` |
| **C1.7** One admitted reservation buys at most one billable provider attempt. The first delivered stream event is the commit point: before it, a retry or a failover reuses the same reservation; after it, a mid-stream failure propagates rather than re-running the model. | E | `test_infrarouter_faults.py::test_mid_stream_failure_no_retry` (a 500 after the first event does not fail over) and `::test_timeout_first_event_never_settles_as_success` (an attempt that never reached the commit point is not settled as a success). The README claims this and no clause governed it |
| **C1.8** A hard pin is identity, not membership: a pinned request is served by exactly that model or refused, and the model on the charge is the model that was reserved. | E for pin-or-refuse; **B** for the reconciliation | `test_vsr_pin.py` (`test_unservable_pin_400`, `test_valid_pin_serves_200`) refuses rather than substituting. The billed-equals-reserved half is checked offline by `test_vsr_reconcile.py::test_hard_pin_violation_when_billed_differs_from_advice` over the decision→usage join, so it is an audit that detects a violation rather than a gate that prevents one |

**Not guaranteed (N).** A bound on the *bill* rather than on admission. An outcome
the gateway cannot observe is one it cannot price, and Bedrock bills some of those
(measured in [MEASUREMENTS.md](../MEASUREMENTS.md)). Those outcomes are classified
and recorded; the invoice is not promised to match the ledger to the cent.

## C2 — Price identity

There is one definition of what a unit of usage costs, and a charge is rated at the
price the request was admitted under.

| Clause | Level | Enforced by |
| --- | --- | --- |
| **C2.1** The reservation price and the settle price are computed by the same code over the same rate document. | E | `test_contract_price_identity.py`, `test_rating_differential.py` |
| **C2.2** The rate document that priced the admission is recorded with the reservation, and a live rate edit in between cannot change what this request is charged. A rate that cannot be frozen refuses the request before the provider is called. | E | `test_contract_price_identity.py::test_a_rate_flip_between_reserve_and_settle_does_not_move_the_charge` |
| **C2.3** A published derived money figure is replayable from the artifact itself: the same facts, priced the way they were priced, give the same figure however long after issue. | E | `test_savings_certificate.py` (`test_a_replay_holds_its_number_across_a_rate_change`, mutation-checked): the report embeds the four rate legs per pricing key and the model resolutions it used, and a replay handed an incomplete basis raises rather than pricing the gap live. A version stamp alone would not carry this — the effective table is the bundled floor plus a live external source plus the version's override rows, and only the last is versioned. Detail rows past the stored cap are not retained, so a replay reproduces the figure and the class breakdown rather than every line |
| **C2.4** The set of billable legs is declared in one place; adding a leg to the charge without adding it to the estimate is impossible to do silently. | E | `test_billable_legs_registry.py` |
| **C2.5** A rate document is one complete validated value: every leg present, every rate a non-negative integer, a version read whole or refused, and a version's rows and row COUNT immutable once written. Validated at every boundary that consumes a row — including the point read that builds the frozen snapshot, not only the bulk load. An invalid document is not a transient, so it refuses admission instead of quietly leaving the previous rates in place. | E | `test_contract_price_identity.py` |
| **C2.6** A rate is resolved through a fixed ladder — admin override, then a fresh fetch, then the current stored version, then the bundled floor — and a missing rate falls DOWN it. No layer's absence lowers a price: a feed that fails, a rate name in a grammar this build cannot read, a token class the provider does not publish, and a region set that cannot be read each keep the value from the layer below, per leg, and never become zero. A leg with no sourced number at all removes its key from the fetch rather than charging zero. | E | `test_pricing_feeds_composite.py`, `test_pricing_feeds_snapshot.py`. The ladder and the measured behaviour of each price API are documented in [`price-feeds.md`](price-feeds.md) |
| **C2.9** A stored rate version is cut when prices CHANGE, not when they are checked: the version id is the digest of the table and each version is written once, so polling adds no rows and a superseded version stays readable. | E | `test_pricing_feeds_snapshot.py::test_an_unchanged_table_cuts_no_new_version`, `::test_a_changed_price_cuts_a_version_and_moves_the_pointer` |
| **C2.10** A charge is recomputable from what was stored: every terminal money event carries, per leg, the token count and the rate applied, so a period can be repriced at any other table — including a superseded stored version — as arithmetic over recorded facts rather than a reconstruction. As-charged is the ledger's settled delta rather than the rating's self-report, every money event is counted whether or not it carries a rating, and the report states whether it covered the whole period. The recompute reads only: it neither edits the charge of record nor resolves the live price source, so running it cannot change what the gateway charges next. | E | `test_reprice.py`, and `dynamo/credit_ledger.py::rating_replay_mismatches` for the replay of each event against its own rate |
| **C2.7** Where a rate could be one of several published numbers, the charged one is the dearest: across the regions a request can fail over to, across the models that share a pricing key, and when a model id does not say which scope it is billed at. Conversion to integer micro-USD rounds up. A fetch that did not see everything that maximum is taken over — a missing member of a shared key, an unread region, a leg priced out of scope — may raise a rate and never lower one. Completeness is judged per key from positive evidence per member, so trouble in a feed that prices none of a key's models does not freeze it. | E | `test_pricing_feeds_dimensions.py`, `test_pricing_feeds_composite.py` |
| **C2.8** The bundled floor's provenance is recorded per leg, not as one blanket sentence: a provider-published leg is a measured list price, a leg no provider publishes is a stated conservative upper bound (named in the document's own `notes`), and `default` and `vllm` are neither a measured price nor an upper bound — a synthetic ceiling built to dominate every real row, and the operator's own cost-recovery figure. Not a placeholder, and a pricing key holds one price point: a key whose models are published at different prices is charged at the dearest and reported (`price_feed_key_spans_prices`) so it is split rather than left to over-charge the cheaper member. | E | `test_pricing_floor.py`, `test_pricing_feeds_composite.py::test_two_models_on_one_key_are_charged_at_the_dearer` |

**Not guaranteed (N).** That a long-context request is charged at the long-context rate.
Bedrock prices a request above a model's long-context threshold at a higher rate per leg
(for Claude Sonnet 4.6, double the standard input rate), and the rate table holds ONE rate
per leg, so such a request is charged at the standard rate — the only systematic
UNDER-charge in this subsystem, and it is named here rather than hidden. The feed parses
long-context rate names and excludes them deliberately (it does not mistake them for the
standard rate, which would over-charge every ordinary request). Closing it needs a leg per
context band in the rate type and in the estimator, which is a money-path change with its
own proof obligations; it is on the open-items list.

**Not guaranteed (N).** That a rate follows the market. No Bedrock API publishes when a
promotional price ends — every offer's `effectiveDate` reads as the first of the current
month — so a change is visible only as a difference between two fetches, and a model no
feed covers (or whose offer this account may not read) keeps the floor until an operator
edits it. That the published list price is what the account actually pays is also outside
this contract: private pricing, credits and commitments are not in any price list.

## C3 — Termination and recovery

Every reservation reaches exactly one ending, and every crash point has a bounded
recovery.

| Clause | Level | Enforced by |
| --- | --- | --- |
| **C3.1** Exactly one ending per reservation. Two mechanisms must not both be able to end the same one. | E for the mechanisms; **B** against an operator recreating the pool row | `test_contract_termination.py`, `test_money_lifecycle_discipline.py`. A hold does not name the incarnation of the pool row it debited, so deleting and recreating a period's row while a reservation is in flight lets the settle apply to a row that never held the debit — see the open items |
| **C3.2a** …for the tenant dollar pool. | B | The hold row plus the reaper, bounded by the TTL and the next pooled request from that tenant (see C3.3) |
| **C3.2b** …for the per-user token reservation. | **N (today)** | Nothing reaches it. The hold row records only the pool amount and `credit_used` is not period-scoped, so a crash between reserve and settle debits a user permanently — see the open items below |
| **C3.2c** …for the per-model quota counter. | **N (today)** | Same shape as C3.2b, bounded instead by the counter's monthly TTL |
| **C3.3** That mechanism's reachability does not depend on the tenant sending more traffic or on the calendar period. | N (today) | The sweep is request-driven and covers the current and previous period only — see the open items below |
| **C3.4** An ended reservation cannot be ended again in either direction. | P + E | `test_billing_formal_z3.py`, `test_contract_termination.py` |
| **C3.5** After any ending, counters and ledger agree, including when the settle that observed the usage never committed. | E, with one stated residual | `test_billing_write_discipline.py`, and `test_contract_owed_settle.py` (`test_the_reaper_posts_the_charge_instead_of_asserting_zero`, `test_a_second_sweep_cannot_post_the_charge_twice`, both mutation-checked). A settle that exhausts its retries now records what it observed as an OWED_SETTLE row, and the reclaim that follows honours it through the existing LATE_SETTLE recovery instead of asserting a settled delta of zero. At-most-once comes from the LATE_SETTLE sort key, so the row needs no mutation to be marked done and the ledger stays append-only. Both orders of the race are covered rather than one: the reaper looks for an owed row after it commits its reclaim, and the abandoned settle looks for a reclaim after it writes the row, so whichever party is second sees the other's write (`_redrive_owed_after_late_reclaim`). Checking only from the reaper's side left the interleaving where it read first and the row arrived a moment later, after which the hold was gone and nothing revisited it. **Residual:** a task that dies between observing the usage and writing that row still loses it; covering that needs a write-ahead on every settle, which is a cost on every request rather than on a rare one |

## C4 — Ledger sufficiency

The ledger is the charge of record; the counters are a cache of it.

| Clause | Level | Enforced by |
| --- | --- | --- |
| **C4.1** Every **dollar-pool** counter move carries its ledger event. RESERVE and the terminals are in the same transaction as the move; the release path is two writes with the reaper as backstop. | B + E | `test_billing_write_discipline.py`. Scoped to the pool on purpose: the per-user token counter and the per-model quota counter move with NO ledger event, so the unqualified "every counter" reading of this clause is false |
| **C4.2** The **dollar-pool** counters are reconstructible from the events alone for every period the system claims to cover. | B | Stated boundary for pre-P2 periods in `derived_totals`. The token and quota dimensions are not reconstructible at all (C4.1) |
| **C4.3** Events are append-only by the mechanism the docs claim, on both sides of the boundary: the deployed policy refuses a mutating write, and the code contains none. | E | Per-write conditions on each event key; the task role's policy DENIES `UpdateItem`/`DeleteItem`/`BatchWriteItem` on the ledger table (`iac/test/ecs-stack-ledger-append-only.test.ts`) and `test_ledger_is_append_only_in_code.py` refuses such a call in `dynamo/credit_ledger.py`. This used to read "except the idempotency-status update", which was an `UpdateItem` the policy denied and no reader consulted — so it failed silently in production while standing in the document as a permitted exception. It is deleted rather than exempted |
| **C4.4** Every event answers, without the live rate table: what was charged, at which version, for which request — and does not assert a measurement nobody made. | E | `test_contract_reporting.py`, `test_rating_differential.py` |
| **C4.5** The usage projection of a settle agrees with the ledger terminal it projects, and a projection that cannot be written does not change what the ledger charged. | E | `test_contract_usage_projection.py`. The projection is what the savings report and the reconcile join actually price against, so a row that disagreed with the charge of record would be an invisible second answer; the money move completes before the row is attempted, so an outage on the reporting table costs a row rather than freezing a reservation |

## C5 — Idempotency identity

One idempotency key means one authorization, for all time.

| Clause | Level | Enforced by |
| --- | --- | --- |
| **C5.1** A retry that crosses a billing period resolves to the same authorization. | E for records written by this version or later; **B** for older ones until the backfill has run | `test_contract_idempotency.py`. Records written before the identity left the money partition are read where they were written, but only for the period supplied and the one before it — the reader cannot guess an older period. `scripts/local/backfill_idemp_partition.py` copies them into the permanent partition and closes that window; it is an upgrade step, listed in [DEPLOYMENT.md](../DEPLOYMENT.md) |
| **C5.2** The mapping from a client key to a stored row is injective, and a replay verifies the key itself rather than the address it was found at. | E | `test_contract_idempotency.py` |
| **C5.3** A replay returns the original outcome and never mints a second money move. | E | `test_contract_idempotency.py`, `test_billing_authorize.py` |
| **C5.4** A retry can tell committed from not-committed without guessing, whichever protocol wrote the record. | E | `test_contract_idempotency.py::test_a_refused_authorize_replays_as_refused_under_pending`, `::test_an_in_flight_attempt_answers_retry_rather_than_success`, `::test_a_terminal_is_evidence_after_every_other_trace_is_gone`, `::test_the_transactional_path_is_unchanged`, all mutation-checked. One resolver decides for both protocols, and it reads durable evidence rather than the record's presence: a pool marker, an activated hold, a terminal event, or a RESERVE event — each protocol leaves at least one, and an ending is evidence its beginning happened. The ambiguous state (an intent written, the commit not yet reached) answers 503 with the same key, which is a stated non-verdict rather than a guess. Previously the entry point replayed any readable record as an authorization, so under the PENDING protocol a REFUSED authorize replayed as authorized |

## C6 — Authority

A principal's effective authority is exactly the intersection of what its role
grants and what its credential was scoped to.

| Clause | Level | Enforced by |
| --- | --- | --- |
| **C6.1** Every gate on every route evaluates that intersection; a gate that cannot evaluate it refuses. Gates that are not FastAPI dependencies count: a handler that tests `user.roles` itself is a gate. | E for the dependencies and the one helper; **B** for the general statement | `test_contract_authority.py`. The dependency check and the VSR helper are exact. The module-level sweep is a TRIPWIRE, not a proof: it accepts a module in which a role read and `user_has_permission` occur in unrelated functions |
| **C6.2** Tenant data and budget are reachable only by principals of that tenant, admins excepted explicitly. | E | `test_authz_lattice.py` |
| **C6.3** No identity acquires a budget implicitly. Authentication is not registration, and admission does not repair a missing one: it reads authority rather than creating it. | E | `test_contract_authority.py` |
| **C6.4** Revocation and demotion take effect on the next request. | E | `test_jwt_verify.py`, `test_api_key_tombstone.py` |
| **C6.5** Nothing a client sets in a request changes which tenant, identity or budget it is accounted to. | E | `test_authz_lattice.py` |
| **C6.6** An authentication artifact is single-use where the flow requires it, and the replay check fails CLOSED when its store is unreachable. | E | `test_sso_replay_failclosed.py::test_replay_detected_raises_401`, `::test_nonce_storage_unavailable_fails_closed`. The signed `GetCallerIdentity` vouch is the artifact; failing open on an unreachable nonce store would make a table blip into an unlimited replay window |
| **C6.7** A principal's authority comes from the store this gateway controls. An assertion inside a token the caller presents is proof of authentication and nothing else. | E | `test_contract_authority_source.py::test_a_group_claim_in_the_token_grants_nothing` drives a principal whose own token claims `admin` and is refused, and `::test_the_authorization_path_does_not_read_group_claims_at_all` reads the authorization modules and refuses the string at all — a behavioural test samples the inputs, the static one closes the class. Both mutation-checked. The claim had been asserted in `ARCHITECTURE.md` on the strength of a docstring |
| **C6.8** No AWS credential is presented to this gateway on the vouch path: the caller signs a `GetCallerIdentity` request locally and the gateway forwards the signature. | E | `test_contract_authority_source.py::test_the_sso_exchange_has_nowhere_to_put_a_credential`. Structural rather than behavioural because the guarantee is structural: an endpoint with no field a secret could arrive in cannot be sent one, and a field added later fails there rather than in a review someone skipped |

## C7 — Configuration authority

Configuration only restricts, and a restriction the gateway cannot read is never
dropped.

| Clause | Level | Enforced by |
| --- | --- | --- |
| **C7.1** One canonical identity per configured subject, used identically by the write path and the enforcement path. | E | `test_routing_inputs_not_invented.py`, `test_contract_model_policy.py` |
| **C7.2** A read failure never resolves to "no restriction"; stale-but-real is acceptable and visible, invented is not. | E | `test_routing_inputs_not_invented.py` |
| **C7.3** A cap is enforced in the denomination it was written in, or the request is refused. | E | `test_contract_model_policy.py` |
| **C7.4** Every flag that changes money behaviour is documented, and its documented default is the one the code produces. | E | [DEPLOYMENT.md](../DEPLOYMENT.md) names four. `STRATOCLAVE_HARD_CEILING_GATE` and `STRATOCLAVE_UNOBSERVED_HOLDS` default **on**, which is what makes the default artifact exhibit the properties described at the top of this document; `STRATOCLAVE_MEASURE_UNENFORCED_BOUND` defaults off and `STRATOCLAVE_POOL_HOLD_TTL_SECONDS` is derived from a documented floor. The clause is deliberately about agreement rather than about the defaults being "conservative": for a gate that binds a ceiling, on is the conservative setting, so the predicate a reader can act on is that the sentence and the code say the same thing. All four are compared against their owning module by `test_documented_money_flag_defaults.py` |
| **C7.5** The seed that establishes the fallback tenant is idempotent, and that tenant cannot be removed by any ordinary route. | E | `test_bootstrap_seed.py`. Re-running the seed is a no-op once the version is current, because it inserts under `attribute_not_exists(tenant_id)` rather than overwriting; and `default-org` is refused by the tenant delete and archive paths, because it is the fallback for a user with no explicit assignment and removing it would leave those users with no tenant at all. Stated as a clause because six sentences in the public documents were asserting it with nothing in this document behind them |
| **C7.6** Every `cdk synth` and `cdk deploy` runs the `cdk-nag` `AwsSolutionsChecks` aspect. | **B** | Holds in the IaC suite, which is where the enforcement lives: `iac/test/nag-synth.test.ts` fails if a synth skips the aspect, and a synth that skipped it would let an infrastructure change ship without the checks the deployment claims to apply. The boundary is not the property but the checking of this row: `test_contract_clauses_cite_real_tests.py` reads Python test names, so it cannot confirm a TypeScript citation, and a clause at E here would be asserting a verification this document cannot perform. Widening that checker to reach the IaC suite is what would make this E |

## C8 — Reporting

The gateway says what it observed, and no more.

| Clause | Level | Enforced by |
| --- | --- | --- |
| **C8.1** A value the gateway did not observe is reported as absent, never as zero — and absence is the DEFAULT, so a transport that does not parse a leg cannot record a measured zero by omission. | E | `test_contract_reporting.py`, including `test_absence_survives_the_hold_seam`, which drives the real `Hold` rather than the pure rater: the snapshot every route settles through used to coerce the absence away one call before the ledger |
| **C8.2** Any path or parameter the gateway names in an error is one it serves, including in a message relayed from upstream. | **B** | `test_contract_reporting.py` covers the OpenAI-compatible relay and the rewriter. Errors composed elsewhere in the codebase are not swept, so the universal reading is not established |
| **C8.3** An outcome the gateway could not observe is classified and recorded rather than assumed free or assumed chargeable, and the reservation behind it is not handed back on the assumption it was free — provided something reached the provider transport. | E in the default deployment for both halves; **B** if an operator turns retention off | `test_provider_outcome_formal.py`, `test_money_lifecycle_discipline.py`, and `test_contract_owed_settle.py` for the retention, driven through the real `Hold` (`test_a_departed_call_keeps_its_reservation_when_the_flag_is_on`, `test_retention_is_on_by_default`, `test_a_retention_resolves_at_the_figure_an_operator_supplies`, mutation-checked), plus `test_money_branches_on_written_facts.py` for the seam those tests used to fake, and `test_retention_requires_departure.py` for the proviso. That proviso is load-bearing rather than decorative: `classify_exception` ends in the expensive state rather than in a guess, so an exception raised by this gateway's own code before the call reached the transport used to be indistinguishable from a read timeout on a completed generation, and retaining it would consume a tenant's budget for a request that never left. Each route announces the hand-off immediately before invoking the provider client (`Hold.provider_call_starting`), retention requires that fact, and `test_retention_requires_departure.py` fails if a module that ends an unobserved outcome does not announce. The ending records the departure on the hold — the only moment anything knows it, and a path a completed request never takes — and the reaper then retains rather than crediting back: one conditional status write, no counter movement, since the amount was already counted against the limit. A retention ends only by an operator settling it at the figure the provider's own record shows or releasing it when that record shows none, and the status is part of that money transaction rather than flipped back first, so there is no window in which the reaper can end it instead. **Residuals:** a task that dies with no ending at all records nothing, so its hold still reclaims; and a retention is long-lived by design, which makes C3.1 (a hold does not name the incarnation of the pool row it debited) the expected lifecycle for exactly these holds rather than an edge case; and nothing watches `held_microusd`, and the flag now defaults ON, so that watcher stopped being optional — it is the open item below rather than a deferred nicety |
| **C8.4** Every token leg the gateway bills is also reported to the caller, and the reported legs are mutually disjoint, so a caller can reconstruct its own charge from the response alone. | E | `test_contract_reporting.py`. C8.1 governs a leg the gateway could not read; this clause governs a leg it read, billed, and did not pass on. They were not the same property and the second did not hold: the cache legs were parsed by `_converse_core.cache_tokens_from_usage`, charged through `pricing.BILLABLE_LEGS`, and omitted from the response, so a measured request billed 3,538 tokens reported `total_tokens: 14`. `total_tokens` deliberately keeps OpenAI's meaning — `prompt_tokens + completion_tokens` — and therefore excludes the cache legs; the sum a caller must use is over all four reported legs, which is why the clause is about the legs rather than about the total |
| **C8.5** The gateway issues no request to cancel provider-side generation, on any transport. | E | `test_contract_reporting.py`. Stated as a positive fact about this code rather than as a claim about the provider, because what happens inside the provider after a transport is abandoned is not ours to promise. The pair to this is C8.6 |
| **C8.6** A client disconnect or client-side timeout bounds neither the provider's work nor the resulting charge. | **N** | Deliberately not guaranteed. Measured twice, independently and on two model families: a Converse call abandoned at a 2 s read timeout was billed 1,493 output tokens ([MEASUREMENTS.md](../MEASUREMENTS.md) finding 1), and a caller outside this project measured 8,958 output tokens on the same shape. A caller must not infer that its own timeout caps what it pays. What IS bounded is stated elsewhere and a reader should be sent there rather than left with a bare refusal: admission is priced from a byte-count bound over the payload the gateway is about to send (C1.5, C7.4's `STRATOCLAVE_HARD_CEILING_GATE`), the provider cannot exceed the output cap in that payload, and an outcome the gateway could not observe keeps its reservation rather than being handed back as free (C8.3). So the exposure on an abandoned request is bounded by the reservation, not by the timeout |
| **C8.7** No public schema for the ledger or for an export of it is published. | **N** | Deliberately not guaranteed, and stated because its absence is load-bearing for anyone assessing compatibility: there is no export route, no export subcommand, and no `additionalProperties` declaration anywhere in the repository, so no third-party consumer can hold a strict schema against these records. An additive field on an internal record therefore cannot break an external contract, because no external contract exists to break. If one is ever published, that is the commit in which this clause changes |

## C9 — External authorization

The authorize / capture / void surface is a two-phase money API, beside the inline
hold lifecycle rather than inside it. It is a shipped contract and was missing from
this document, which is worse than a misclassified clause: a reader could not tell
whether it had been audited at all.

| Clause | Level | Enforced by |
| --- | --- | --- |
| **C9.1** Once a charge is committed, no later read of that authorization reports it as absent or as a different amount, on either protocol. | E | `test_billing_authorize.py`, and `test_pending_protocol_integration.py::test_a_captured_authorization_still_answers_after_its_hold_is_gone` (mutation-checked) for the window that used to break it: the capture's own transaction deletes the hold and the RESERVE event is an asynchronous projection under the PENDING protocol, so the only surviving record was the terminal — which the C-1 gate did not read (404) and the amount did not come from (zero). Both read it now, and `::test_an_inline_hold_is_still_refused_after_its_terminal` pins that evidence of EXISTENCE did not become evidence of EXTERNALITY |
| **C9.2** Every pair of concurrent operations on one authorization resolves to one terminal state, and the loser learns which state won. | E | `test_billing_authorize.py`, `test_billing_authorize_stateful.py` (a stateful model over interleaved capture / void / reap) |
| **C9.3** Expiry means one thing: past the instant an authorization published, it cannot be captured, and the status read says so. | E | `test_billing_authorize.py` (`test_capture_past_expiry_is_refused_while_the_hold_is_still_live`, `test_status_of_a_live_hold_past_expiry_reads_expired`), both mutation-checked. It used to mean only "reclaimable after this instant", so a capture past the published expiry still charged whenever a sweep had not run — the answer to "can I still capture?" was decided by other tenants' traffic. Void is deliberately still allowed: it returns the headroom the reaper would have returned |
| **C9.4** A client-supplied field cannot produce an unhandled server error, and an amount cannot exceed what was authorized. | E | `test_billing_authorize.py` covers the over-capture refusal on both sides, and `test_amount_above_the_ceiling_is_a_client_error` the amount ceiling (`MAX_AMOUNT_MICROUSD`, 1e15 micro-USD — under both DynamoDB's 38-digit Number and the exact range of the double a browser parses the body into). A value at the ceiling is still a well-formed request refused on budget, so the bound is validation rather than a business limit |
| **C9.5** The answers do not depend on which internal protocol mode the deployment runs. | E | The two clauses that differed by mode were C9.1 and C5.4, and both now hold on either protocol by reading durable evidence rather than the witness one protocol happens to leave: `test_contract_idempotency.py::test_the_transactional_path_is_unchanged` and `::test_a_committed_reservation_still_replays_under_pending` are the same property asked of both modes |

## C10 — Claims

Every guarantee in the public documents is true of the shipped code, or states the
boundary at which its evidence stops.

| Clause | Level | Enforced by |
| --- | --- | --- |
| **C10.1** A sentence that matches the guarantee lexicon declared in [`contracts/claims/config.json`](../../contracts/claims/config.json), found in one of the six documents that same file names as covered, is registered with the reason it is allowed to say that — a clause here, a row in [EVIDENCE.md](../EVIDENCE.md), a limit stated in the sentence, or a named debt. "Guarantee-shaped" means "matches that lexicon", not "reads like a promise", so this clause is true of a declared vocabulary over six named files — never of every guarantee this project's documents make, and never of a document outside the six, which is not read at all. | E | `test_claims_are_anchored.py`, reading the lexicon and the file list from `contracts/claims/config.json` rather than carrying its own copy — currently `README.md`, `docs/SCOPE.md`, `docs/design/hard-ceiling.md`, `docs/ARCHITECTURE.md`, `docs/ADMIN_GUIDE.md`, `docs/DEPLOYMENT.md` — against `contracts/claims/anchored.json`. Adding or editing a matching sentence in one of those files fails the build until its author points at the reason |
| **C10.2** A sentence qualified because the unconditional version is not true yet carries a `debt:` anchor naming the clause that would make it unconditional, and that clause is on the open-items list. | E | `test_claims_are_anchored.py`. This is the clause that keeps honesty from becoming retreat: a weakened sentence and the work that would strengthen it are the same list |
| **C10.4** A clause's own citation resolves: a test named here exists, and a named node is a function defined in the suite. | E | `test_contract_clauses_cite_real_tests.py`. The failure this prevents is the one this document is most exposed to — a test is renamed or deleted, the row keeps citing it, and an unenforced clause goes on reading as enforced while looking audited. It also requires that a clause at P or E cites something at all |
| **C10.5** A clause's guarantee level is not lowered without a major version bump. Lowering one is honest and sometimes required — a clause found untrue at E should drop rather than keep lying — but it breaks what a reader pinned a tag on, so it costs a major release rather than a patch. | E | `test_clause_levels_are_a_ratchet.py`, mutation-checked (weakening C1.6 from E to B inside the 1.x line fails it). The levels are recorded in `contracts/claims/snapshot.json` under `detector.clause_levels`, a clause holding at two levels is recorded at the WEAKER one so a caveat cannot weaken it while the letter stays put, and a strengthening that is not re-recorded also fails — otherwise a clause could go B to E and slide back to B unseen. The released major is read from `CHANGELOG.md` rather than from a git tag, so this runs from a clean checkout under plain pytest. What it does NOT check is whether a level is CORRECT; that is the first permanent human obligation below |
| **C10.3** A verdict word in a comparison table is not contradicted by its own cell. | **B** | The lint treats a table cell as a sentence, so the verdict is registered like any other claim; it cannot check that a cell agrees with itself |

The lint cannot tell whether a sentence is TRUE — only whether someone was made to
point at the reason. That is its honest limit, and it is the difference between "we
are careful" and "carelessness fails the build".

### The permanent human obligations

The mechanism above resolves citations, pins clause wording, and fails a build when
an anchor is missing or a citation has rotted. None of that touches the four
judgements the mechanism sits on top of, and no later version of it will reach them
either — they are not gaps to be closed, they are the floor the closing stops at.

Whether a clause is true is decided by a human reading it. No layer of this
mechanism has ever checked truth and none will: a test witnesses the instances it
runs, and a clause at **E** claims a universal over those and every instance that
was never run. `test_billing_formal_z3.py` staying green forever is a fact about the
model it proves something over, not about whether the shipped code still matches
that model.

Whether a sentence needs a `contract:` anchor at all — rather than `descriptive:`,
`boundary:`, or nothing — is a human judgement, made once by whoever wrote the
sentence and once by whoever reviewed it. The protected-subject floor
(`test_a_protected_subject_cannot_rest_on_a_judgement`: a guarantee word next to a
protected subject — credential, budget, admission, ledger, identity — may not rest
on `descriptive:` or a bare `boundary:`) is a floor under that judgement, not a
detector standing in for it. A claim about a protected subject phrased without any
of the listed words still slips past it unnoticed, the same way `records` and
`ships` slipped past the guarantee lexicon itself until someone went and read what
`every`/`all` had newly caught. CI validates the consequences of a declared status —
that a `contract:` id resolves, that a `boundary:` sentence carries a limit word —
never whether that status was the right one to declare.

Whether a cited test meaningfully enforces its clause is a human reading of the
test. `test_contract_clauses_cite_real_tests.py` and `test_doc_references_resolve.py`
check that a citation resolves: the file exists, the function is defined. Neither
reads what the function's body asserts. A test that exists, is named for the right
clause, and asserts nothing about it would pass every check this repository runs.

Whether a retirement preserved a claim or retracted it is a human reading of both
endpoints — the sentence that left and the successor that is supposed to carry it
forward. The mechanism for this is not conditional: every retired claim in
`contracts/claims/snapshot.json` carries a typed disposition (`replaced-by`,
`moved-to-clause`, `document-removed`, `retracted-false`, `reworded-to`), and
`test_claim_obligations_ratchet.py` verifies that a `replaced-by` or `moved-to-clause`
disposition's target resolves — the named successor claim or clause exists and is
live. What it verifies stops there. Whether that successor says what the claim it
replaced said is not something the mechanism reads; it is the human reading of both
endpoints this paragraph opened with.

Put plainly, because the rest of this document leans on a test for everything else
and cannot lean on one here: **the mechanical layer verifies everything about a
clause except whether it is true.**

---

## C11 — Residency

Prompt bytes leave the operator's configured region set only where the operator
configured that.

| Clause | Level | Enforced by |
| --- | --- | --- |
| **C11.1** The failover set is the operator's list, and an empty list or a `none`/`disabled`/`off` sentinel means single-region: a streaming request then sends prompt bytes to no other region. | E | `test_failover_regions.py` |
| **C11.2** When the failover list is UNSET, the built-in defaults are filtered to the primary's jurisdiction, so a non-US primary does not inherit a US failover. | E | `test_failover_regions.py`, `test_failover_catalog_property.py` |
| **C11.3** A model that is not offered in the configured regions is not catalogued, rather than being served from a region the operator did not choose. | E | `test_failover_catalog_property.py`, `test_routing_inputs_not_invented.py` |
| **C11.4** `STRATOCLAVE_RESIDENCY=strict` fails the CDK synth when any Bedrock call region leaves the deploy region. | B | A synth-time check in `iac/`, not a request-time property: it constrains what can be deployed, and says nothing about a deployment whose synth predates it |

**Not guaranteed (N).** That a region name corresponds to a legal jurisdiction. The
`_jurisdiction` helper is a coarse prefix (`us`, `eu`, `ap`) and does not distinguish
the UK from the EU; it filters the built-in defaults and certifies nothing.

## C12 — Log hygiene

An identity is recorded as a marker, not as plaintext, and a secret is not recorded
at all.

| Clause | Level | Enforced by |
| --- | --- | --- |
| **C12.1** A usage row records an email as a stable digest, never the address. | E | `test_security_hardening_2026_06.py::test_usage_logs_record_stores_email_hash_not_plaintext` |
| **C12.2** A bootstrap password is not written to any log. | E | `test_bootstrap_admin_password_not_logged.py` |
| **C12.3** An error surfaced to a caller carries no internal detail that identifies a principal or a resource. | E | `test_error_sanitization.py` |
| **C12.4** No structured log anywhere writes an email in plaintext. | **B** | `core/logging.py` provides the marker helper and the call sites use it, but nothing sweeps every logging call, so this is the helper plus discipline rather than a checked property. The one writer known to have been violating this clause is the audit writer, and it is now covered at E by C12.6 — that closes an instance and not the class, so the level does not move. The clause is universally quantified over writers and exactly one writer has been checked. What would make it E is the sweep listed under the open items. The shape to look for in this project's own code is a `logger` call whose message argument is `json.dumps(...)`, a formatted mapping, or an interpolation of a non-literal, because the processor chain matches on fields and such a call presents it none. **That sweep is necessary and not sufficient**, and the deployed log group is where that became clear: the addresses currently reaching it come from botocore's own debug logger dumping raw DynamoDB response bodies, which no sweep of this repository's writers can see. So the clause has two residuals of different kinds — our writers, which a static sweep can close, and third-party loggers, which only a log-level policy can |
| **C12.5** Credential material the gateway mints or relays is never written to a persistent store or a log. | **B** | By construction: the ephemeral wrapper key and the provider bearer token exist only in the request's own memory, and no repository method takes them. ONE instance is checked rather than argued: the bootstrap admin's temporary password is written to Secrets Manager and to nothing else, and `test_bootstrap_admin_password_not_logged.py` asserts it reaches neither the logger nor stderr — the ECS log driver forwards both, so stderr is a log. The clause stays at B because the general case is still by construction: there is no check that a future call site could not add one, and what would make this E is a static sweep of the same shape as `test_ledger_is_append_only_in_code.py`, listed under the open items |
| **C12.6** The audit writer emits no email address, by any route into its payload. | E | `test_audit_log_email_scrub.py` and `test_audit_query_names_real_fields.py`. Deliberately narrower than C12.4: this is one writer, checked, and it is the writer that was violating C12.4. Six routes are covered because four were reported and two more only appear once the writer's own serialisation is taken into account — a dict KEY that is an address, and any value whose `default=str` coercion happens after a scrub of the payload tree. The scrub therefore runs over the serialised line rather than over the tree, which covers both for free and is less code. `actor_email` is emitted as `actor_email_hash`, the same digest a usage row carries as `user_email_hash`, so one actor can be matched across the audit trail and the usage ledger; that correspondence is the only reason to keep any form of the address. The digest is unsalted and therefore confirmable by anyone holding a list of candidate addresses: it is pseudonymisation and a log-hygiene measure, **not** anonymisation, and it must not be described as the latter. A keyed HMAC would defeat enumeration and would also break the cross-store correspondence until the usage rows were migrated, which is why it is not done here |

## C13 — Wire compatibility

A parameter this gateway cannot honour is refused, not dropped.

| Clause | Level | Enforced by |
| --- | --- | --- |
| **C13.1** An unsupported request parameter is rejected with a 400 naming it, rather than silently ignored — a silently dropped parameter is a request the caller believes it made. | E | `test_chat_completions.py`, `test_openai_responses_models.py` |
| **C13.2** The bytes on the wire match the API being emulated, so an SDK that validates its own contract sees a conforming response. | E | `test_anthropic_wire_bytes.py` |
| **C13.3** A completeness claim the README makes about itself is checked against the table that carries the evidence. | E | `test_contract_layer_claim.py`: "ships all five layers" is true iff the five-layer table has five rows and each says shipped, so a layer demoted to a roadmap item fails rather than leaving the prose quietly false. It does not establish that a row marked shipped is shipped — the same residue every clause here carries, which is why the status is written where a reader can see it |
| **C13.4** A provider-side vocabulary this gateway reads is treated as open: a member it cannot represent is reported, never dropped in silence. | E | `test_converse_core_normalization.py`. Applies to the `reasoningContent` legs and to the usage block's keys. Not a hypothetical — two members arrived unannounced while this clause was being written: a reasoning delta carrying an empty `text` with a bare `signature`, and a `cacheDetails` key present on one call's usage block and absent from the next otherwise-identical call. The known set is declared once and consumed by every path that reads it, so the check is a set difference rather than an inference from "nothing was emitted"; inferring cannot see a new member that arrives alongside a known one, which is the case that matters. Warned once per stream, because a provider streaming hundreds of them should produce one line. **Boundary:** this governs what the gateway does with a member it READ. It does not say every wire renders every member it holds — `/v1/messages` normalises the reasoning legs and renders none of them, because the Anthropic API represents thinking with its own block type and that wire shape is deliberately deferred. That drop is silent to a caller of that route, and it is a stated deferral rather than a case this clause covers |

## C14 — Tenant ceilings and defaults

An unconfigured tenant — created through the ordinary route, with nothing set by
hand — is bounded by a ceiling denominated in the unit the bill arrives in, and that
ceiling is on by default. See [limits.md](limits.md) for the full statement of the
three ceilings this section is about; this section is the claim that they hold, not
the explanation of what each one is for.

| Clause | Level | Enforced by |
| --- | --- | --- |
| **C14.1** A tenant created through the ordinary route (admin or team-lead) receives a SEAT-TRACKED dollar pool for the current period — one carrying no operator figure at all, since absence of `manual_limit_microusd` is what "follow the seats" is — written at zero seats and tracking `seats x seat_rate` through the membership delta, with no operator action required. **Boundary: the delta is best-effort and the tracking is not exact at every instant.** A membership write must never fail because of the pool's unrelated state, so a delta that does not land leaves the stored seat count and the ceiling one seat SMALLER than the memberships — the refusing direction, never over-admission — and a delta that lands twice leaves them one seat larger in both, consistently, which no equation over the row can see. C14.9's daily source comparison is what detects either, a day late. **A second boundary: this route never writes `manual_limit_microusd = 0`.** Creation always seeds the row seat-tracked at zero seats, so `manual_limit_microusd` is absent, never present-and-zero; a caller coming from the API's `PUT .../pool-budget` with `limit_usd_cents=0` is setting an EXPLICIT figure through the operator-set path (R1's rule: presence, including zero, refuses every request), not something either creation route does on its own. | B | `admin_tenants.create_tenant` / `team_lead.create_tenant` both call the shared `_provision_seat_pool` helper, which writes the BUDGET row before returning. Holds for the two ordinary creation routes; a tenant seeded directly via `TenantsRepository.seed_default` (the bootstrap default-org path) does not go through this and stays unlimited at the pool level until an operator sets one, exactly as an unpooled tenant always has |
| **C14.2** The per-user token quota's default is loose (10,000,000), not the binding ceiling, and raising it changes no admission arithmetic: the item the reserve transaction builds for it is unchanged, only the number `dynamo.tenants._default_credit_fallback` returns. | B | `dynamo/tenants.py`; the reserve-side item this number feeds is `dynamo.user_tenants.UserTenantsRepository.reserve_txn_item`, untouched by this change |
| **C14.3** `pool_limit_microusd` and `pool_headroom_microusd` are never rewritten from a read-then-write of `reserved`/`settled`; every write to them is a guarded conditional write against the row's OWN prior value or attribute state, composing with a concurrent reserve rather than racing it, and every one moves headroom by exactly the amount it moves the limit. The set of those writers is DERIVED from the pool row's declaration (`dynamo.pool_row_schema.ceiling_writers()`) rather than listed in prose, because a list in prose stays green while naming a subset the moment a writer is added. One of those writers moves a delta whose COEFFICIENT it had to read — the seat delta multiplies by the rate stored on the row — so it is guarded on that rate as well as on the operator figure's absence, and retries against a fresh read when either has moved. A delta needs no snapshot only while its multiplier is a constant. | B | `dynamo/tenant_budgets.py`: `set_manual_limit` / `clear_manual_limit` (CAS on the three values the baseline delta was computed from), `adjust_pool_for_seat_delta` (pure `ADD`, guarded on the operator figure's absence), `_seed_pool_row` (creation and rollover, `attribute_not_exists(tenant_id)`), `reserve_txn_item`/`settle_txn_item` (unmodified by this change) |
| **C14.4** A membership change moves a seat-tracked pool's limit and headroom by exactly `±seat_rate`, and moves neither on a row holding an operator's figure — but moves `seat_count` on BOTH, so a row holding a figure can still report that its entitlement has outgrown it. Every membership transition routes through the one seat-delta writer, including the user-deletion path, which previously archived memberships around it and left the ceiling scaled to people who no longer existed. | E | `test_pool_membership_delta_l4.py` — a hire and a departure each move limit and headroom by exactly one seat on a seat-tracked row and move only the seat count on a row holding a figure. The writers are `dynamo/tenant_budgets.py:adjust_pool_for_seat_delta` and its one caller `dynamo/user_tenants.py:_adjust_pool_seat_delta_best_effort`, which the deletion path now reaches through `UserTenantsRepository.archive_membership` |
| **C14.7** The ceiling is `baseline + coalesce(pool_granted_microusd, 0)` where `baseline` is `manual_limit_microusd` when that attribute is PRESENT (including a stored zero, which means every request refused) and `seat_count x seat_rate` when it is ABSENT. The sentinel is absence and not zero, because `limit_usd_cents` accepts `0` today with that meaning, so reading `0` as "follow the seats" would reverse a legal input for every existing caller. | B | `dynamo/tenant_budgets.py`: `baseline_microusd` / `expected_pool_limit_microusd` / `is_seat_tracked` |
| **C14.8** Seat tracking is REVERSIBLE. `{"follow_seats": true}` on the pool-budget endpoint REMOVES the operator's figure and moves the ceiling to the seat term, so the next membership change moves it again. Writing the seat term back as a figure would not do this: the figure would remain and the next hire would not move it. | B | `dynamo/tenant_budgets.py:clear_manual_limit`; `mvp/admin_tenants.py:apply_pool_budget_request`, reached from the admin and team-lead routes |
| **C14.9** The pool row is compared DAILY to its sources — the tenant's active memberships, and the per-seat rate the deployment records — and not only to itself. A membership delta applied twice moves the seat count and the ceiling together, so every intra-row identity still balances; the source comparison is the only check that can see it. The reconciler reports and never repairs, and holds no write grant on either table. | B | `mvp/observability/quota_reconciler.py`, scheduled by `iac/lib/quota-reconciler-stack.ts` with read-only grants |
| **C14.10** Every attribute a pool row can carry is classified in ONE declaration — its rollover class, its writers, a covering reconciler check or a stated exemption, and a maximum value width — and an attribute in no class is a failure rather than an omission. Four mechanisms read that declaration instead of holding their own list: the period rollover's carried set, C14.3's writer list, the reconciler's check completeness, and the row's size bound and its alarm. | B | `dynamo/pool_row_schema.py:POOL_ROW_ATTRIBUTES`, a mapping from attribute name to its spec — its OWN module, so the size guard cannot end up measuring a shape the rollover and the reconciler do not use, and keyed by name so two entries for one attribute are unrepresentable — with `carried_attributes()`, `ceiling_writers()`, `assert_row_fully_classified()` and `worst_case_pool_item_bytes()` as its four readers |
| **C14.11** The per-seat rate is not a live knob. Each row stores the rate its own ceiling was computed at; the deployment records the rate in force once; and a process configured with a different figure REFUSES TO START, because a ceiling recomputed at a rate nobody chose is a plausible number that nothing afterwards can distinguish from a correct one. A rate change is a migration that recomputes every seat-tracked row and leaves rows holding an operator's figure untouched — conditioned on that absence at the moment of the write, not at the moment of the scan, because a row can acquire an operator's figure between the two without moving either figure the recompute compares against. | B | `dynamo/tenant_budgets.py:assert_seat_rate_in_force`, called from `main.py`'s lifespan; `migrations/pool_ceiling_migration.py:recompute_seat_tracked_rows` |
| **C14.12** The migration onto C14.7's rule is ONE-SHOT: no phase of it runs on a table where any pool row carries a granted amount. Its cut-over reads a row carrying neither new attribute as `manual_limit = pool_limit`, which is correct while the stored total is only ever a baseline and destructive once that total can also contain granted money — the grant would be folded permanently into the operator's figure, on every such row at once. The property that makes the cut-over safe beforehand is what makes a re-run unsafe afterwards, so the refusal is a precondition every phase checks rather than a sentence in a document. | B | `migrations/pool_ceiling_migration.py:assert_no_grants_present`, called by every phase, and `phase_m3_cutover`'s fail-stale read |
| **C14.13** A period's pool row exists for every tenant that had one in the period before, created by a SCHEDULED job whose unit of work is "tenants holding the prior period's row" — the opt-in signal, so a tenant that never had a pool never acquires one from a rollover — and by a membership change if that arrives first. A missing row is not read as an unpooled tenant: when the PREVIOUS period's row exists, a priced request is refused with `pool_period_row_missing` rather than admitted with no money ceiling. Both layers are load-bearing; the schedule alone returns the ceiling to fail-open in any month the job fails, and the guard alone refuses every pooled tenant from midnight until the job runs. | B | `mvp/observability/quota_reconciler.py:roll_forward_all_tenants`, scheduled beside the reconciliation in `iac/lib/quota-reconciler-stack.ts` as a separate function with a write grant the reconciler's own role deliberately lacks; the miss guard is `TenantBudgetsRepository.previously_pooled`, called from `mvp/_pipeline.py:reserve_credit` on the miss path only, with no write added to the admission path |
| **C14.14** NOT GUARANTEED. The per-seat rate and the per-user token default are environment variables, not operator-editable settings: there is no surface showing the value in force, no record of who changed either, and no way to queue a rate change for the next period. Changing the rate today is a migration and changing the default is a redeploy. | N | Not implemented. The direction, the reason the two knobs need different treatments, and the four things that must not be built are stated in [limits.md](limits.md) section 6; the open item below names what would close it |
| **C14.5** A team lead may set only their OWN tenant's pool budget, gated by the same ownership check (`_require_owner`) every other team-lead-scoped write in that router uses, and audited by the same event name (`tenant_pool_budget_set`) the admin route uses — through the same shared implementation, so the two routes cannot drift. A team lead may also READ that pool, including the sentence saying which mode it is in: setting a figure is the write that ends seat tracking, so a role able to make that write and unable to see it is the hazard, not a missing convenience. | E | `test_team_lead_pool_budget_l5.py` — a team lead sets their own tenant's pool and is refused on another's, and the refusal is the ownership check rather than a missing route (verified by mutation: removing `_require_owner` from this route fails the test) |
| **C14.6** A tenant whose creation-time figure (`seats x SEAT_MONTHLY_USD`, seats=1) would exceed `MAX_POOL_BUDGET_USD_CENTS` is refused before either the Tenants row or the BUDGET row is written — never silently clamped to the maximum. | E | `test_pool_seat_cap_validation_l8.py` — an oversized per-seat figure refuses tenant creation rather than clamping it |
| **C14.15** A raise is TIME-BOUNDED, and the bound is enforced by a scheduled sweep rather than by anybody remembering. An approval writes an amount onto `pool_granted_microusd` with an `expires_at`; the sweep revokes every grant past that instant, in one transaction per grant that takes the grant terminal and subtracts its amount from the ceiling and the headroom together. Exactly-once rests on ONE condition — the grant is still `ACTIVE` and still holds the amount that was read — so two overlapping sweeps return the capacity once and the loser treats its cancellation as the success it is. A revoked grant leaves the sweep's index in the same transaction that ends it, so the index holds the work outstanding rather than every grant behind a filter. | B | `mvp/grants.py:sweep_expired_grants` and `dynamo/quota_events.py:grant_terminal_txn_item` / `list_active_grants_expiring`; scheduled by `iac/lib/quota-grants-stack.ts` |
| **C14.16** A grant's `expires_at` is at most the END of the period it was granted in, and that pin is what makes F1's rollover safe rather than a policy preference. The rollover resets `pool_granted_microusd` by OMISSION, so a grant outliving its period would have its capacity destroyed at the boundary instead of released — on every granted row at once, on the 1st, silently. An approval also refuses an expiry less than 300 seconds out, and refuses every expiry once fewer than 300 seconds remain in the period, because a grant that expires before it can admit a request delivers nothing while consuming the tenant's cap headroom. | B | `mvp/grants.py:latest_permissible_expiry_for_period`, called by the refusal and by anything that renders the bound so the calendar arithmetic exists once; the dependency is named in `dynamo/pool_row_schema.py`'s classification of `pool_granted_microusd` and in [quota-raises.md](quota-raises.md) section 4 |
| **C14.17** `grant_cap_microusd` bounds what approvers may grant IN AGGREGATE for a period, and its ABSENCE means "derived from the baseline, evaluated now" — not zero and not unlimited. Nothing seeds it and nothing backfills it, because a materialised default freezes at the moment it is written: a tenant that later hires would keep a cap sized to its old baseline and refuse legitimate approvals with nothing saying why. **Boundary: the guard is caller-side.** A condition on a missing attribute fails, so the cap is resolved from a read and the transaction compares the row's LIVE granted sum against that figure — which catches a concurrent approval and does NOT catch a concurrent baseline change. C14.19's daily check closes that window a day late, the same lateness the rest of that reconciler accepts. | B | `dynamo/tenant_budgets.py:effective_grant_cap_for_row` (the one resolution every reader uses) and `grant_apply_txn_item`, whose condition admits the granted attribute's ABSENCE — without which the first grant of every period would be refused as a cap breach |
| **C14.18** "Capacity-bearing" has ONE definition and `REVOKE_BLOCKED` is in it. A grant whose subtraction could not be committed never returned its capacity, so the pool row is right to still count it; a predicate that excluded it would report every row holding a blocked grant as broken for as long as the fault lasted. Such a grant is marked, alarmed, left out of the sweep's index so one poison grant cannot consume every run, and carries the failing reason on its own row because a metric can say only that something is stuck. | B | `mvp/grants.py:is_capacity_bearing`, consumed by the reconciler and by every inventory and deliberately not re-exported from the storage layer; `dynamo/quota_events.py:mark_revoke_blocked` / `clear_revoke_block` |
| **C14.19** The granted term is compared DAILY to the grants it is supposed to be the sum of, per TARGET ROW rather than per tenant, and the aggregate cap is asserted against it. Per target row because grants are pinned to the row they raised: during a late sweep an expired-but-unrevoked grant still bears capacity on the PRIOR period's row, and a tenant-wide sum against the current row would be wrong in both directions at once. The orphan hunt starts FROM GRANTS, since a pass starting from pool rows has no row to start at for a grant whose target is gone. **Boundary: a tenant holding grants and NO pool row is invisible to the fleet pass**, which visits rows; the per-tenant reconciliation finds it when pointed at it. | B | `mvp/grants.py` registers `pool_granted_matches_active_grants`, `grant_cap_not_exceeded` and `grant_target_row_exists` into F1's check loop from F2's own file, and both attributes name their check in `dynamo/pool_row_schema.py` — so a pass in which those checks are unregistered reports them MISSING rather than reporting the fleet clean |
| **C14.20** An approver's authority is a `ConditionCheck` INSIDE the same transaction as the money, bound to the tenant read from the request or grant ROW and never to a value the caller supplied. A route dependency proves the permission was held when the request arrived; this proves it is held at the instant capacity is granted, so a permission revoked mid-flight cancels the whole transaction. The binding is security-critical rather than defensive: the approval permission is deployment-global at the write path, so without it a permission-holder who owns one tenant could grant capacity in another. The ROUTE selects the ownership form or the global form, never the actor's roles — sniffing roles would let a global permission-holder reach the ownership route and be checked by the weaker condition. Nobody may decide their own request. | B | `mvp/grants.py:_authority_condition_check_item`, placed at a NAMED transaction index so a cancellation there is unambiguously an authority failure and not a money one |
| **C14.21** One undecided raise per person per tenant per UTC day, through a single slot row that is ALSO the caller's idempotency anchor — one row doing both jobs rather than two that can disagree about what was admitted today. The same token twice returns the first request; a different token while the slot is held is refused, naming the holder and the reset instant WITH its zone. A decided request frees the day: withdrawn, rejected, and an approval whose grant has stopped bearing capacity all free it, while `PENDING` and `REVOKE_BLOCKED` do not — the latter still holds its share of the ceiling, and treating it as finished would let a second raise stack on capacity nobody returned. | B | `dynamo/quota_events.py:put_slot_if_absent` (a single `attribute_not_exists` write) and `mvp/grants.py:submit_limit_raise`, which claims the slot BEFORE writing the request so a lost race leaves no orphan request an approver could act on |
| **C14.22** Every `402` names the wall that refused and whether it is grantable, and only the grantable one carries a raise hint. Exactly one of the three admission limits is raisable, and being denominated in micro-USD does not make a limit raisable — the per-model quota's user scope is money and is not grantable. Grantability is read from the same declaration the admission transaction enforces, so the refusal path and the raise path cannot disagree. The public name of a wall is derived from its internal one by a TOTAL projection, one-to-many for the per-model quota because the counter that refused is the tenant's or the user's and those have different fixes; when a cascade's candidates were refused by different scopes the refusal declines to name one rather than naming the last. The hint carries the REMAINING CAP, so a surface cannot pre-fill an amount no approver may grant. | B | `mvp/reserve_limits.py:is_grantable_wall`, `mvp/grants.py:blocker_for_wall` / `unmapped_walls` (non-empty means a refusal exists that cannot name itself), `mvp/_pipeline.py:_refusal_body` with `wall` required at every call site |
| **C14.23** A figure that EQUALS the ceiling currently in force is refused while any of that ceiling is granted, and a tenant is not retired while any grant still bears capacity. The first: `set_manual_limit` treats the figure as the new BASELINE and moves `pool_limit_microusd` by the delta against the OLD baseline only, leaving the granted term untouched — so a figure copied straight off the screen (baseline plus a still-live grant) becomes the new baseline, and the setter adds that same grant on top of it again: the ceiling sits at the typed figure PLUS the grant for as long as the grant stays open, one grant's worth above what was on the screen. That excess is temporary, not permanent — the sweep subtracts the grant once at expiry and the ceiling lands exactly on the figure the operator typed, never below it — but a window of extra capacity nobody asked for is indistinguishable, from the figure alone, from an operator who genuinely wants that number as the new baseline, for whom the same jump-then-settle is correct. Refusing forces the operator to say which one was meant instead of granting the ambiguous one a free window. The second: archiving over a live grant leaves it pinned to a row nobody will look at again, counted against a cap for a tenant that no longer exists, with no path that can ever release it. | B | `mvp/admin_tenants.py:apply_pool_budget_request` — shared by the admin and team-lead routes so neither can drift — and `archive_tenant`, which drains through `mvp/grants.py:revoke_all_active_grants` (grant-first) before deleting and refuses on any remainder |
| **C14.24** NOT GUARANTEED. Nothing in the product SETS the aggregate grant cap, and no surface names who may approve a raise. The cap is read wherever it matters and written by no request, so every tenant is on the derived default unless somebody edits the row out of band; and a requester cannot tell "the approver is away" from "this feature is broken". A raise filed against the token quota is refused honestly and has no alternative path to offer, because that quota has no raise mechanism of its own. | N | Not implemented, and each is a product decision rather than a screen. Stated in [quota-raises.md](quota-raises.md) section 11 with what closing it would take |
| **C14.25** `pool_headroom_microusd` is ONE fungible counter, never a set of tagged sub-balances. A settle draws down whichever money is in the pool at that instant — the seat term, an operator's figure, or a live grant — with no attribute anywhere recording which term a given charge drew from. **Boundary: a figure describing spend as "grant-supported" is therefore an upper bound on what the grant covered (the ceiling was genuinely G higher and the tenant could not have spent past its baseline without it), never an attribution of which dollar came from it.** | B | `dynamo/pool_row_schema.py:POOL_ROW_ATTRIBUTES` declares exactly one headroom/reserved/settled counter apiece — no per-source breakdown attribute is classified, so one could not be written without failing C14.10's completeness check; `dynamo/tenant_budgets.py:reserve_txn_item` / `settle_txn_item` move that one counter regardless of which term is currently in force |
| **C14.26** (F3) A hint FILLS with every candidate the routing cascade actually priced, never a candidate it only planned to try. A pool refusal's `candidates` still holds one entry — the cascade leaves on a pool refusal before trying the rest of the chain — and names the untried tail as `unattempted_model_ids`, by id only, with no cost data. A quota cascade's `candidates` holds one entry per candidate genuinely tried (`QuotaExhausted` is caught and the cascade advances), none of them `grantable`, and its `unattempted_model_ids` is empty. `minimum_raise_microusd` is the smallest raise that clears the cheapest GRANTABLE priced candidate, never derived from the untried tail. The 402's `message` states the target's own shortfall in dollars and names any unattempted candidates in prose, so a client that ignores `raise_hint` is still told a number. | B | `mvp/_pipeline.py:_reserve_over_candidates` (the multi-candidate fill and the pool-refusal tail augmentation) and `mvp/grants.py:raise_hint_for_priced_candidates` / `raise_hint_for_pool_row` (the arithmetic; zero renames, zero removals of F2's shipped `RaiseHint` fields) |
| **C14.27** (F3) A refusal caused by a grant expiring within three sweep intervals of its `expires_at`, against the SAME wall that refused, is flagged `grant_expired` on the candidate it explains and named in the refusal's `message` — a boolean beside `blocker` rather than a fifth value inside it, since C14.22 already closed `blocker` to four. A SUCCESSFULLY served request's usage row carries `fallback_reason`, additive and never derived after the fact; today it is only ever `quota_exhausted`, because a pool-wall refusal is never caught and advanced past (C14.26's own "leaves on a pool refusal"), so a completed fallback can never have been caused by a grant expiring at that wall. | B | `mvp/_pipeline.py:fallback_reason_for_expired_grant` / `_expired_grant_reason_for_pool_wall` (the refusal half) and `dynamo/usage_logs.py`'s additive `fallback_reason` attribute, threaded from `mvp/_pipeline.py:settle_reservation_and_log` (the usage-row half) |
| **C14.28** (F3) The self-service request view and the tenant approval view both render a REQUEST's decision — for a decided one, the approved amount, the expiry and the approver id, never the bare status word — and a comment renders as TEXT, via ordinary interpolation, never through a sink that would parse HTML out of it. A submission's pre-filled amount and named wall come ONLY from a `raise_hint` carried in the browser's own navigation state from the refusal that produced it; a screen reached any other way (a deep link, a reload, a bookmark) pre-fills nothing and names no wall, rather than reconstructing an answer to "what refused you" at a later moment that can disagree with the refusal itself. | B | `frontend/src/pages/MeLimitRaises.tsx` (the pre-fill, gated on router state; the comment rendered via `{row.decision_comment}`, no `dangerouslySetInnerHTML` anywhere in the file) and `frontend/src/pages/LimitRaiseApproval.tsx` |
| **C14.29** (F3) A grant inventory reconciles PER TARGET ROW: each row's own capacity-bearing grants sum to that row's own `pool_granted_microusd`, rendered and labelled per row, and a tenant with a stale prior-period row shows two rows rather than one merged total that could be silently wrong in both directions at once. | B | `frontend/src/pages/GrantsInventory.tsx`, rendering `mvp/grants.py:reconcile_tenant_grants`'s existing per-row shape; consumes `is_capacity_bearing` rather than restating which grants count |
| **C14.30** (F3) An approver sees the latest permissible expiry for the current period before typing one, and an ordinary user sees their own tenant's pool status (limit, live remaining, remaining grant cap — no `manual_limit`, no seat internals) before filing a request, both through read-only endpoints gated on the same permission as the write they inform. | B | `mvp/grants.py:admin_latest_permissible_expiry` / `team_lead_latest_permissible_expiry` (pure calendar arithmetic, no tenant id) and `mvp/grants.py:own_tenant_wall_status` (`None`, not a 404, when the tenant has no pool row for the period) |
| **C14.31** NOT GUARANTEED. The routing cascade abandons the whole request on a pool refusal rather than trying a cheaper candidate that would fit under the same headroom the pricier one did not. A candidate is only ever priced in chain order, so once the pool wall refuses the current one, none of the rest is tried even when a smaller reservation would be admitted. This is a defect in money admission, not a gap in the hint or the fallback-cause attribute that sit on top of it. | N | Not implemented, and deliberately its own future change: closing it changes the reserve path's own admission decision, which carries the same proof obligations as the rest of that path. Named under Open items |
| **C14.32** A scheduled job's stack is checked, at build time, against the tables its handler can actually reach: the audit walks the handler's own call graph — imports, constructed repositories, and the check registry a reconciler dispatches through — and the stack's synthesised environment must cover every table that walk arrives at. This exists because the failure it prevents is invisible from either side alone: every repository resolves its table as `os.getenv("DYNAMODB_<X>_TABLE", "stratoclave-<x>")`, so an unset variable does not fail loudly, it names a DIFFERENT deployment's table. The service task never reaches that fallback because its stack passes every table; a scheduled job reaches it whenever its stack passes only the tables somebody remembered. **Boundary: the check sees what it can walk.** A repository reached through `getattr`, a non-literal `importlib` name, a factory, an inherited method, or a registry decorator whose name is not in the audit's list is invisible to it; it checks that a variable is PASSED, not that the IAM grant beside it is sufficient; and the fallback literal itself is untouched, so a job outside the four this audit covers still fails the same way. | B | `backend/scripts/scheduled_lambda_env_audit.py` walks the call graph, `iac/test/scheduled-job-env-wiring.test.ts` compares it against a real `cdk synth --all` across all four scheduled stacks. Both known instances are closed: the pool reconciler's `DYNAMODB_QUOTA_EVENTS_TABLE`, reachable only through the check registry, and the certificate scheduler's `DYNAMODB_TENANTS_TABLE` and `DYNAMODB_USAGE_LOGS_TABLE` — the second of which had no IAM grant at all and was found by writing this audit rather than by reading the stack |

Three of these hold at **B** rather than **E** for reasons stated in their own rows: a
tenant seeded through the bootstrap default-org path does not pass through the
creation routes (C14.1), "changes no admission arithmetic" is a property of a number
rather than of a code path (C14.2), and "no second stored figure the ceiling could be
recomputed from" is a shape a reading confirms rather than a value a test can read
(C14.3). C14.25 holds at **B** for the same reason as C14.3: the absence of a
per-source breakdown attribute is a shape the declaration's completeness check
confirms, not a value a test reads. The others are enforced by tests named in their
rows.

**C14.15, C14.16, C14.20, C14.21 and C14.23 are each enforced by a mechanism and are
recorded at B rather than E, deliberately and for one reason: no test in THIS suite
fails if they are violated.** Each one is a condition inside a transaction, an index
membership, or a refusal — shapes that a test can pin exactly — and the tests for them
were written independently of the code and land separately. Until they are in the same
tree, E would be a claim about a suite that does not yet contain them, and the level is
supposed to answer "what fails if this breaks". Promoting them is a one-word change per
row once each cell can name the file.

**C14.7 through C14.12 hold at B rather than E, and for one reason worth naming.**
The tests that will enforce them are being written independently of this
implementation, from the same contract, precisely so that neither shapes itself around
the other — so this half cannot name a test file it has not seen, and a clause claiming
E while citing none is the kind of green-and-wrong the level record exists to stop.
Each cites the code that implements it. Raising them to E when the enforcing tests land
is a strengthening, which the level ratchet permits; recording E now would not be.

C14.7's identity is written with the `coalesce` from the first day, before any grant
exists, so it is true of every row that exists today rather than becoming true when
granting ships. A later part adds a writer of the granted term and appends to this
section; it does not edit C14.7, because a clause silently overtaken by a later
change is the one shape the claim ratchet cannot see — the ratchet records
weakenings, and that would be a change.

**Not guaranteed (N).** A per-user ceiling denominated in money. The per-user quota is
tokens, so it bounds usage and not spend; the aggregate money ceiling is the pool.
Closing this needs a per-user money counter, which needs a reserve, a settle and a
release path of its own — it is on the open-items list rather than implied here.

## Open items, named rather than implied

These are contract clauses the code does not satisfy yet. They are listed here
because a contract that quietly omits its failures is worse than no contract. A
clause that has been closed leaves this list; a residual stated inside a clause's
own cell is not an open item, because there is nothing outstanding to do about it
without paying a cost the clause names.

- **The long-context rate band, for C2.6 (see the N note there).** Bedrock charges a
  request past a model's long-context threshold at a higher rate per leg; the rate type
  holds one rate per leg, so those requests are charged at the standard rate. This is the
  only systematic under-charge in the pricing subsystem. Closing it means a leg per context
  band through `Rate`, `RateSnapshot`, the estimator and the settle path — a money-path
  change that carries the same proof obligations as the rest of that path, which is why it
  is here rather than done quietly.

- **C3.2b and C3.2c, for the per-user token reservation and the per-model counter.**
  The admission transaction debits up to three counters; the hold row records only the
  pool amount, so a crash between
  reserve and settle leaves `credit_used` debited with nothing to reach it. The
  counter is not period-scoped, so it never resets. The change is small and named:
  carry the token amount and the user key on the hold write that already happens,
  and add one decrement item to the reclaim transaction that already happens. The
  same edit closes C3.2c for the per-model counter. It is the largest distance in
  these documents between how weak a sentence is and how little work would remove
  the weakness, which is why it is stated this precisely.
- **C14.31, the routing cascade never tries a cheaper candidate once a pricier one hits
  the pool wall.** The reserve loop prices candidates in chain order and the pool check
  is amount-dependent, so a cheaper fallback that would fit under the same headroom the
  pricier candidate did not is never priced, let alone tried: the cascade abandons the
  whole request instead. Closing it means catching the pool refusal the way the loop
  already catches `QuotaExhausted` and advancing, which changes what money admission does
  on a path this project treats as its highest-risk one. It is deliberately its own future
  change rather than folded into the hint work, which only names the candidates the code
  actually priced.

- **C14.32, the table a scheduled job reads is not checked against the table its stack
  passes.** The fallback in every repository's table name means an unset environment
  variable is not an error: it points at the same-named table of a deployment called
  `stratoclave`. The service never reaches it, because its stack passes all of them; a
  scheduled job reaches it whenever its stack passes a hand-picked subset. This is not
  hypothetical twice over. The pool reconciler shipped without
  `DYNAMODB_QUOTA_EVENTS_TABLE` and was refused by IAM on every deployment whose prefix is
  not `stratoclave` — fixed in this change, and found only by deploying it. And
  [certificate-scheduler-stack.ts](../../iac/lib/certificate-scheduler-stack.ts) has the
  same shape today for `DYNAMODB_TENANTS_TABLE`, which its documented default path reads to
  enumerate tenants; that one belongs to another feature and is left named rather than
  fixed here, because closing it needs its own deployed run. Neither layer's tests can see
  this: the backend fixture sets every table variable at once, and the stack tests assert
  what is passed without asking what the handler reads. Closing the class means one check
  that compares the repositories a handler's module graph constructs against the
  environment its stack gives it — both facts already exist, nothing compares them.

- **C14.24, the three things the raise path leaves to a person.** Nothing in the
  product SETS the aggregate grant cap: the attribute is already carried across a period
  boundary and already read by the approval guard and by the daily check, so what is
  missing is a writer, an audit event and a read surface — not a mechanism. No surface
  names WHO may approve a raise, so from a requester's seat "the approver is away" and
  "this is broken" are the same observation; closing that needs a new authorization query
  and a new endpoint. And a raise filed against the per-user token quota is refused
  honestly with nothing to offer instead, because that quota has no raise path of its own
  — which is a product decision about the token quota rather than a screen. All three are
  stated in [quota-raises.md](quota-raises.md) section 11.

- **C14.14, operator-editable defaults for the seat rate and the token default.** Both are
  environment variables, so an operator cannot see the value in force, cannot see that it changed, and
  cannot queue a rate change for the next period. The two need different treatments and that is the
  reason this is a named item rather than a ticket: the rate is unsafe to change mid-period, so it
  belongs at a boundary where the new row is written rather than mutated, while the token default is
  already safe to change and merely invisible. The work is a setting for each under the existing
  permissions, one audit event per write, the rollover reading the queued rate instead of the
  environment, and the boot check comparing against the period's stored rate — which is what demotes
  the environment variable to a bootstrap default. [limits.md](limits.md) section 6 states the
  constraints, including the four things that must not be built.

- **C14.1 seat-count drift, and the retraction behind it.** The seat delta is applied
  after the membership write commits, best-effort, so it is not atomic with it: a delta
  that does not land leaves the stored `seat_count` and a seat-tracked ceiling one seat
  SMALL, and one applied twice leaves them one seat LARGE — in both attributes, in the
  same direction, so no equation over the row disagrees. C14.9's daily comparison against
  the memberships is the detection, and it is a day late by construction. Closing it means
  making the seat delta part of the membership transaction, which puts the pool row inside
  every membership write and makes a membership write fail on the pool's unrelated state —
  the property `_adjust_pool_seat_delta_best_effort` exists to guarantee. So it is a real
  trade rather than an oversight, and it is named here because an earlier version of
  `limits.md` asserted the opposite: that the figure *cannot* drift. That sentence was
  retracted as false rather than moved, since nothing carries it forward.

- **C3.3 reachability.** The reaper runs inside a pooled reserve and scans the
  current and previous period, so a hold orphaned in a quiet month is never
  reached. A scheduled reconciler already exists for other work; giving it the
  inline holds is the fix.
- **C3.1 pool-row incarnation.** A hold records the tenant and period it debited but
  not WHICH incarnation of that row. An operator deleting and recreating a period's
  pool row with a reservation in flight makes the settle apply to a row that never
  held the debit: `pool_reserved` goes negative and headroom is minted. Fencing is an
  id on the row, copied onto the hold, and an equality condition on every terminal.
- **The lexicon still decides what counts as a claim.** `every` and `all` are now in it,
  restricted to quantification over behaviour, which is what brought "Every route
  enforces per-user token quotas …" and "Every call is recorded as a structured JSON log"
  into the registry. Two things about that are worth stating rather than leaving for
  someone to discover. The restriction was necessary because the unrestricted quantifier
  also captured statements of configuration ("every table is provisioned in
  `PAY_PER_REQUEST` mode"), which are facts rather than promises and have no honest anchor
  kind — so a judgement about which quantified sentences are guarantees is now encoded in a
  regular expression, and a guarantee phrased outside it is still invisible. And the
  boundary this clause has is a declared word list over six named files, which is a
  narrower thing than the sentence "every guarantee is anchored" would suggest — C10.1 now
  says so in its own row.
- **The joint enforcement point has no clause.** `README.md`'s "who called which model,
  under whose budget, and through which identity — enforced before the model is invoked,
  on every single request" cited C1.1 alone when this list was first written, which was
  wrong in two ways; the first is now fixed and the second is why this item stays. The
  anchor now names every clause its conjuncts need — `contract:C1.1,C6.2,C6.5,C4.4`, each
  pinned — so a reader is no longer told that one clause covers three promises. What
  remains has no clause to name: each conjunct maps to one (the attribution record to
  C4.4, the budget decrement to C1.1 and its tenant scoping to C6.2, the identity a
  request is accounted to under C6.5), but the
  sentence's strongest word is none of those — it is that the three share ONE enforcement
  point ahead of invocation, with no gap between them in which one check has resolved and
  another has not. No clause says that. A clause that covered it would have to state that
  identity resolution, tenant and budget attribution, and admission complete inside a
  single gate that runs to completion before the provider is called, rather than as three
  checks that could in principle run at three separate points with daylight between them.
  Anchoring the three conjuncts to their separate clauses is honest about each part and
  silent about the part that makes the sentence worth writing as one sentence rather than
  three.
- **The operator's duty not to list a directly-assumable role pattern has no clause and
  no test.** `docs/ARCHITECTURE.md` and the allowed-role-patterns row in
  `docs/ADMIN_GUIDE.md` each tell the operator not to list a role pattern for a role whose
  trust policy lets a principal assume it directly, because every identity on that path is
  read out of `RoleSessionName`, and a role reachable by a direct `sts:AssumeRole` call
  lets the caller set that string itself — listing such a pattern hands the session name,
  and therefore every identity it can spell, to whoever can assume the role. A clause that
  covered this would have to say the gateway inspects the trust policy of every configured
  role pattern and refuses to admit one it cannot show is restricted to an identity
  provider, rather than accepting the operator's list on faith. Nothing does that today:
  the gateway reads whatever patterns are configured and treats the operator's list as
  correct. The duty rests on the operator reading the documentation, and it rests there
  now — not as an interim state before a clause arrives, but as the thing this item is
  naming.
- **The protected-subject floor now carries an enumerated exemption list.**
  `contracts/claims/config.json` lists eleven claim ids under `floor_exemptions`, each
  with a reason of its own, for sentences that trip the guarantee-vocabulary-plus-
  protected-subject rule without being a money or identity guarantee this document should
  cover: a compound sentence whose logging half has no clause, a mechanism detail, a
  rate-limiting granularity no clause addresses, idempotency on the accounting path where
  no reservation exists, a sequencing rationale, the site an unbuilt layer would occupy, a
  pair of evidence-territory latency figures, a licensing statement, an alarm-muting
  caution, and the operator's duty named in the item above, twice — once for each document
  it appears in. Widening the floor's rule to pass all eleven for everyone would have
  buried the reasoning inside the detector, invisible until someone re-derived the rule
  from scratch; an enumerated list is reviewable one id at a time, and
  `test_claim_judgement_worklist.py` prints it, so the population stays visible instead of
  growing one exemption at a time with nobody watching the count. Two of the eleven —
  `cl-a12e772054af` and `cl-1ffbef41ba3a` — are the real gap, filed here for lack of a
  clause rather than because the sentence is safe; the remaining nine are sentences a
  clause should not cover at all.
- **C8.3 has no watcher on the retention.** The reaper holds a departed call's
  reservation whose provider call departed. A provider outage can still fill a tenant's
  headroom, but it is no longer silent: see the exposure item below for the alarms, and
  note that draining the queue is still a human's job.
- **C12.5 has no mechanism.** Credential material is kept out of every store and log
  by construction, and nothing stops a future call site from putting it in one. A
  static sweep — no repository or logger call takes a value derived from the wrapper
  key or a provider token — is what would move it from B to E.
- **C10's lint reaches eight documents, not every document.** `README.md`,
  `docs/SCOPE.md`, `docs/design/hard-ceiling.md`, `docs/ARCHITECTURE.md`,
  `docs/ADMIN_GUIDE.md`, `docs/DEPLOYMENT.md`, and, since the price-feeds change,
  `docs/design/price-feeds.md`, and, since this change, `docs/design/limits.md`,
  are read. The ones a reader also consults and this
  lint does not are named here rather than counted, because a bare number goes
  stale silently the day someone adds a document and forgets the list:
  `docs/MEASUREMENTS.md`, `docs/MEASUREMENTS.ja.md`, `docs/CLI_GUIDE.md`,
  `docs/CODEX_GUIDE.md`, `docs/COWORK_INTEGRATION.md`, `docs/GETTING_STARTED.md`,
  `docs/LOCAL.md`, `docs/VSR_CONFIG_CONTRACT.md`, `docs/design/calibrated-mode.md`,
  `docs/design/charge-loss.md`, `docs/design/gateway-capacity.md`,
  `docs/design/ledger-hot-path.md`, `docs/design/pending-protocol.md`,
  `docs/design/vsr-savings-certificate.md`, `docs/EVIDENCE.md`,
  `docs/benchmarks/ledger-latency.md`, `docs/demo/README.md`,
  `docs/demo/savings-certificate-sample.md`, `docs/demo/savings-vs-litellm.md` and
  this document itself, `docs/design/CONTRACTS.md` — the last six found only when
  the price-feeds change made the coverage check bidirectional over the filesystem
  instead of one-directional over the lists, which is the class Q11 closed; naming
  them here is the honest half of that close, since sweeping six unrelated
  documents is not. The list lives in `contracts/claims/config.json` under
  `uncovered_documents_named`, and the covered set grows by a document's name
  entering `covered_documents` there — never by a
  count going up on its own.

  The guarantee lexicon also deliberately leaves out two ordinary absolutes,
  `records` and `ships`: they match product-status assertions ("records the
  outcome", "ships all five layers") far more often than invariants, so widening the
  lexicon by them would register sentences that describe what shipped rather than
  promise anything about it. The omission is named rather than silent —
  `guarantee_terms_deliberately_absent` in the same config — so a reader can tell the
  gap is a judgement made on purpose and not an oversight nobody noticed.
- **Retention exposure is reported and alarmed; what remains is that nothing ENDS a
  retention but a human.** The watcher this item asked for exists: `retention_exposure`
  carries the standing figures per tenant and period, and two alarms read it — saturation
  on the fraction of a pool that retentions hold, and staleness on the oldest unresolved
  one. Saturation and staleness are separate alarms because no single threshold separates
  an incident in progress from an operator who stopped looking. What is still open is the
  ending: a retention ends only when someone settles it at the figure the provider's own
  record shows or releases it when that record shows none, and `charge-loss.md` section 7
  is explicit that automating that needs a capped write-off budget on top of this
  accounting. The alarms make the queue visible; they do not drain it.

- **The archive guarantee's `audit_log` boundary is undocumented, because the
  `audit_log` schema it would qualify has not landed.** `archive_tenant`
  (`mvp/admin_tenants.py`) revokes live grants and marks a tenant archived; nothing in
  README.md, `limits.md`, `quota-raises.md` or this document says whether that
  guarantee reaches a tenant's audit log entries, because no change has shipped a
  designed `audit_log` schema or retention policy for such a sentence to describe —
  there is no shape yet for a boundary sentence to attach to. Closing it means whichever
  change lands that schema adding one paragraph, in the SAME change, stating plainly
  whether an archived tenant's audit entries are retained, purged or left untouched.
  Tracked by `test_audit_log_is_excluded_from_the_archive_guarantee`
  (`backend/tests/test_quota_raise_claim_boundaries_l41.py`), which asserts this bullet
  stays listed for as long as no document names `audit_log`, and switches to asserting
  the boundary sentence itself the moment one does — so the gap stays visible here
  rather than behind a skip nobody but a test run sees.

- **The R39d post-epic benchmark re-run is pending, tracked here rather than only in a
  test's skip reason.** `docs/benchmarks/ledger-latency.md`'s "Post-epic
  re-measurement" section states that the fixture and its composed identity exist and
  have been exercised under moto, but the re-run itself — against real AWS and the
  deployed ALB/Fargate path the document's other figures were captured on — has not
  been executed, for lack of standing access to live infrastructure during this
  implementation pass. Closing it means one benchmark run through
  `bench/ledger-latency/pool_fixture.py` against a deployed revision, naming which of
  the three reachable row shapes it measured, with the pending marker in that document
  replaced by the figure and the run's timestamp. Tracked by
  `test_a_post_epic_figure_names_its_shape_and_only_that_shapes_attributes`
  (`backend/tests/test_ledger_latency_figures_annotated_l39d.py`), which asserts this
  bullet stays listed for as long as the pending marker remains in the document,
  rather than merely skipping silently while nothing requires anyone to re-run it.
