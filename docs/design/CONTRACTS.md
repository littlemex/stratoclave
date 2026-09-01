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
| **C7.4** Every flag that changes money behaviour is documented and defaults conservative. | B | [DEPLOYMENT.md](../DEPLOYMENT.md) lists them; `STRATOCLAVE_HARD_CEILING_GATE` and `STRATOCLAVE_UNOBSERVED_HOLDS` default off for reasons stated there |

## C8 — Reporting

The gateway says what it observed, and no more.

| Clause | Level | Enforced by |
| --- | --- | --- |
| **C8.1** A value the gateway did not observe is reported as absent, never as zero — and absence is the DEFAULT, so a transport that does not parse a leg cannot record a measured zero by omission. | E | `test_contract_reporting.py`, including `test_absence_survives_the_hold_seam`, which drives the real `Hold` rather than the pure rater: the snapshot every route settles through used to coerce the absence away one call before the ledger |
| **C8.2** Any path or parameter the gateway names in an error is one it serves, including in a message relayed from upstream. | **B** | `test_contract_reporting.py` covers the OpenAI-compatible relay and the rewriter. Errors composed elsewhere in the codebase are not swept, so the universal reading is not established |
| **C8.3** An outcome the gateway could not observe is classified and recorded rather than assumed free or assumed chargeable, and the reservation behind it is not handed back on the assumption it was free — provided something reached the provider transport. | E in the default deployment for both halves; **B** if an operator turns retention off | `test_provider_outcome_formal.py`, `test_money_lifecycle_discipline.py`, and `test_contract_owed_settle.py` for the retention, driven through the real `Hold` (`test_a_departed_call_keeps_its_reservation_when_the_flag_is_on`, `test_retention_is_on_by_default`, `test_a_retention_resolves_at_the_figure_an_operator_supplies`, mutation-checked), plus `test_money_branches_on_written_facts.py` for the seam those tests used to fake, and `test_retention_requires_departure.py` for the proviso. That proviso is load-bearing rather than decorative: `classify_exception` ends in the expensive state rather than in a guess, so an exception raised by this gateway's own code before the call reached the transport used to be indistinguishable from a read timeout on a completed generation, and retaining it would consume a tenant's budget for a request that never left. Each route announces the hand-off immediately before invoking the provider client (`Hold.provider_call_starting`), retention requires that fact, and `test_retention_requires_departure.py` fails if a module that ends an unobserved outcome does not announce. The ending records the departure on the hold — the only moment anything knows it, and a path a completed request never takes — and the reaper then retains rather than crediting back: one conditional status write, no counter movement, since the amount was already counted against the limit. A retention ends only by an operator settling it at the figure the provider's own record shows or releasing it when that record shows none, and the status is part of that money transaction rather than flipped back first, so there is no window in which the reaper can end it instead. **Residuals:** a task that dies with no ending at all records nothing, so its hold still reclaims; and a retention is long-lived by design, which makes C3.1 (a hold does not name the incarnation of the pool row it debited) the expected lifecycle for exactly these holds rather than an edge case; and nothing watches `held_microusd`, and the flag now defaults ON, so that watcher stopped being optional — it is the open item below rather than a deferred nicety |

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
| **C12.4** No structured log anywhere writes an email in plaintext. | **B** | `core/logging.py` provides the marker helper and the call sites use it, but nothing sweeps every logging call, so this is the helper plus discipline rather than a checked property |
| **C12.5** Credential material the gateway mints or relays is never written to a persistent store or a log. | **B** | By construction: the ephemeral wrapper key and the provider bearer token exist only in the request's own memory, and no repository method takes them. There is no check that a future call site could not add one — what would make this E is a static sweep of the same shape as `test_ledger_is_append_only_in_code.py`, listed under the open items |

## C13 — Wire compatibility

A parameter this gateway cannot honour is refused, not dropped.

| Clause | Level | Enforced by |
| --- | --- | --- |
| **C13.1** An unsupported request parameter is rejected with a 400 naming it, rather than silently ignored — a silently dropped parameter is a request the caller believes it made. | E | `test_chat_completions.py`, `test_openai_responses_models.py` |
| **C13.2** The bytes on the wire match the API being emulated, so an SDK that validates its own contract sees a conforming response. | E | `test_anthropic_wire_bytes.py` |
| **C13.3** A completeness claim the README makes about itself is checked against the table that carries the evidence. | E | `test_contract_layer_claim.py`: "ships all five layers" is true iff the five-layer table has five rows and each says shipped, so a layer demoted to a roadmap item fails rather than leaving the prose quietly false. It does not establish that a row marked shipped is shipped — the same residue every clause here carries, which is why the status is written where a reader can see it |

## C14 — Tenant ceilings and defaults

An unconfigured tenant — created through the ordinary route, with nothing set by
hand — is bounded by a ceiling denominated in the unit the bill arrives in, and that
ceiling is on by default. See [limits.md](limits.md) for the full statement of the
three ceilings this section is about; this section is the claim that they hold, not
the explanation of what each one is for.

| Clause | Level | Enforced by |
| --- | --- | --- |
| **C14.1** A tenant created through the ordinary route (admin or team-lead) receives a SEAT-TRACKED dollar pool for the current period — one carrying no operator figure at all, since absence of `manual_limit_microusd` is what "follow the seats" is — written at zero seats and tracking `seats x seat_rate` through the membership delta, with no operator action required. **Boundary: the delta is best-effort and the tracking is not exact at every instant.** A membership write must never fail because of the pool's unrelated state, so a delta that does not land leaves the stored seat count and the ceiling one seat SMALLER than the memberships — the refusing direction, never over-admission — and a delta that lands twice leaves them one seat larger in both, consistently, which no equation over the row can see. C14.9's daily source comparison is what detects either, a day late. | B | `admin_tenants.create_tenant` / `team_lead.create_tenant` both call the shared `_provision_seat_pool` helper, which writes the BUDGET row before returning. Holds for the two ordinary creation routes; a tenant seeded directly via `TenantsRepository.seed_default` (the bootstrap default-org path) does not go through this and stays unlimited at the pool level until an operator sets one, exactly as an unpooled tenant always has |
| **C14.2** The per-user token quota's default is loose (10,000,000), not the binding ceiling, and raising it changes no admission arithmetic: the item the reserve transaction builds for it is unchanged, only the number `dynamo.tenants._default_credit_fallback` returns. | B | `dynamo/tenants.py`; the reserve-side item this number feeds is `dynamo.user_tenants.UserTenantsRepository.reserve_txn_item`, untouched by this change |
| **C14.3** `pool_limit_microusd` and `pool_headroom_microusd` are never rewritten from a read-then-write of `reserved`/`settled`; every write to them is a guarded conditional write against the row's OWN prior value or attribute state, composing with a concurrent reserve rather than racing it, and every one moves headroom by exactly the amount it moves the limit. The set of those writers is DERIVED from the pool row's declaration (`dynamo.pool_row_schema.ceiling_writers()`) rather than listed in prose, because a list in prose stays green while naming a subset the moment a writer is added. | B | `dynamo/tenant_budgets.py`: `set_manual_limit` / `clear_manual_limit` (CAS on the three values the baseline delta was computed from), `adjust_pool_for_seat_delta` (pure `ADD`, guarded on the operator figure's absence), `_seed_pool_row` (creation and rollover, `attribute_not_exists(tenant_id)`), `reserve_txn_item`/`settle_txn_item` (unmodified by this change) |
| **C14.4** A membership change moves a seat-tracked pool's limit and headroom by exactly `±seat_rate`, and moves neither on a row holding an operator's figure — but moves `seat_count` on BOTH, so a row holding a figure can still report that its entitlement has outgrown it. Every membership transition routes through the one seat-delta writer, including the user-deletion path, which previously archived memberships around it and left the ceiling scaled to people who no longer existed. | E | `test_pool_membership_delta_l4.py` — a hire and a departure each move limit and headroom by exactly one seat on a seat-tracked row and move only the seat count on a row holding a figure. The writers are `dynamo/tenant_budgets.py:adjust_pool_for_seat_delta` and its one caller `dynamo/user_tenants.py:_adjust_pool_seat_delta_best_effort`, which the deletion path now reaches through `UserTenantsRepository.archive_membership` |
| **C14.7** The ceiling is `baseline + coalesce(pool_granted_microusd, 0)` where `baseline` is `manual_limit_microusd` when that attribute is PRESENT (including a stored zero, which means every request refused) and `seat_count x seat_rate` when it is ABSENT. The sentinel is absence and not zero, because `limit_usd_cents` accepts `0` today with that meaning, so reading `0` as "follow the seats" would reverse a legal input for every existing caller. | B | `dynamo/tenant_budgets.py`: `baseline_microusd` / `expected_pool_limit_microusd` / `is_seat_tracked` |
| **C14.8** Seat tracking is REVERSIBLE. `{"follow_seats": true}` on the pool-budget endpoint REMOVES the operator's figure and moves the ceiling to the seat term, so the next membership change moves it again. Writing the seat term back as a figure would not do this: the figure would remain and the next hire would not move it. | B | `dynamo/tenant_budgets.py:clear_manual_limit`; `mvp/admin_tenants.py:apply_pool_budget_request`, reached from the admin and team-lead routes |
| **C14.9** The pool row is compared DAILY to its sources — the tenant's active memberships, and the per-seat rate the deployment records — and not only to itself. A membership delta applied twice moves the seat count and the ceiling together, so every intra-row identity still balances; the source comparison is the only check that can see it. The reconciler reports and never repairs, and holds no write grant on either table. | B | `mvp/observability/quota_reconciler.py`, scheduled by `iac/lib/quota-reconciler-stack.ts` with read-only grants |
| **C14.10** Every attribute a pool row can carry is classified in ONE declaration — its rollover class, its writers, a covering reconciler check or a stated exemption, and a maximum value width — and an attribute in no class is a failure rather than an omission. Four mechanisms read that declaration instead of holding their own list: the period rollover's carried set, C14.3's writer list, the reconciler's check completeness, and the row's size bound and its alarm. | B | `dynamo/pool_row_schema.py:POOL_ROW_ATTRIBUTES` — its OWN module, so the size guard cannot end up measuring a shape the rollover and the reconciler do not use — with `carried_attributes()`, `ceiling_writers()`, `assert_row_fully_classified()` and `worst_case_pool_item_bytes()` as its four readers |
| **C14.11** The per-seat rate is not a live knob. Each row stores the rate its own ceiling was computed at; the deployment records the rate in force once; and a process configured with a different figure REFUSES TO START, because a ceiling recomputed at a rate nobody chose is a plausible number that nothing afterwards can distinguish from a correct one. A rate change is a migration that recomputes every seat-tracked row and leaves rows holding an operator's figure untouched. | B | `dynamo/tenant_budgets.py:assert_seat_rate_in_force`, called from `main.py`'s lifespan; `migrations/pool_ceiling_migration.py:recompute_seat_tracked_rows` |
| **C14.12** The migration onto C14.7's rule is ONE-SHOT: no phase of it runs on a table where any pool row carries a granted amount. Its cut-over reads a row carrying neither new attribute as `manual_limit = pool_limit`, which is correct while the stored total is only ever a baseline and destructive once that total can also contain granted money — the grant would be folded permanently into the operator's figure, on every such row at once. The property that makes the cut-over safe beforehand is what makes a re-run unsafe afterwards, so the refusal is a precondition every phase checks rather than a sentence in a document. | B | `migrations/pool_ceiling_migration.py:assert_no_grants_present`, called by every phase, and `phase_m3_cutover`'s fail-stale read |
| **C14.5** A team lead may set only their OWN tenant's pool budget, gated by the same ownership check (`_require_owner`) every other team-lead-scoped write in that router uses, and audited by the same event name (`tenant_pool_budget_set`) the admin route uses — through the same shared implementation, so the two routes cannot drift. A team lead may also READ that pool, including the sentence saying which mode it is in: setting a figure is the write that ends seat tracking, so a role able to make that write and unable to see it is the hazard, not a missing convenience. | E | `test_team_lead_pool_budget_l5.py` — a team lead sets their own tenant's pool and is refused on another's, and the refusal is the ownership check rather than a missing route (verified by mutation: removing `_require_owner` from this route fails the test) |
| **C14.6** A tenant whose creation-time figure (`seats x SEAT_MONTHLY_USD`, seats=1) would exceed `MAX_POOL_BUDGET_USD_CENTS` is refused before either the Tenants row or the BUDGET row is written — never silently clamped to the maximum. | E | `test_pool_seat_cap_validation_l8.py` — an oversized per-seat figure refuses tenant creation rather than clamping it |

Three of these hold at **B** rather than **E** for reasons stated in their own rows: a
tenant seeded through the bootstrap default-org path does not pass through the
creation routes (C14.1), "changes no admission arithmetic" is a property of a number
rather than of a code path (C14.2), and "no second stored figure the ceiling could be
recomputed from" is a shape a reading confirms rather than a value a test can read
(C14.3). The others are enforced by tests named in their rows.

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
