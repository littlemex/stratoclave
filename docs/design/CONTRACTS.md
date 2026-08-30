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
do."** Two of the clauses below hold at **B** where the stated configuration is not
the shipped default (`STRATOCLAVE_HARD_CEILING_GATE` and
`STRATOCLAVE_UNOBSERVED_HOLDS` are both off), so the default artifact does not
exhibit them. Where that is the case the clause says so in its own row rather than
leaving a reader to assemble it from the flag documentation.

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
| **C1.5** The amount reserved is an upper bound on what the settle can charge for the same request. | **B — and NOT in the default deployment** | `test_billable_legs_registry.py`. Holds where a byte-count bound prices the reservation and `STRATOCLAVE_HARD_CEILING_GATE` gates admission. That flag ships OFF, so a default deployment prices admission with an estimate its own design document proves is not a bound — see [hard-ceiling.md](hard-ceiling.md) |
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
| **C3.5** After any ending, counters and ledger agree, including when the settle that observed the usage never committed. | E, with one stated residual | `test_billing_write_discipline.py`, and `test_contract_owed_settle.py` (`test_the_reaper_posts_the_charge_instead_of_asserting_zero`, `test_a_second_sweep_cannot_post_the_charge_twice`, both mutation-checked). A settle that exhausts its retries now records what it observed as an OWED_SETTLE row, and the reclaim that follows honours it through the existing LATE_SETTLE recovery instead of asserting a settled delta of zero. At-most-once comes from the LATE_SETTLE sort key, so the row needs no mutation to be marked done and the ledger stays append-only. **Residual:** a task that dies between observing the usage and writing that row still loses it; covering that needs a write-ahead on every settle, which is a cost on every request rather than on a rare one |

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
| **C8.3** An outcome the gateway could not observe is classified and recorded rather than assumed free or assumed chargeable, and the reservation behind it is not handed back on the assumption it was free. | E for the classification; **B** for holding the headroom — inside `STRATOCLAVE_UNOBSERVED_HOLDS=on`, which ships off | `test_provider_outcome_formal.py`, `test_money_lifecycle_discipline.py`, and `test_contract_owed_settle.py` (`test_a_departed_call_keeps_its_reservation_when_the_flag_is_on`, `test_a_retention_resolves_at_the_figure_an_operator_supplies`, `test_retention_is_off_by_default`, all mutation-checked) for the retention. The reaper used to return a reservation whose provider call had departed and record that nothing was charged; with the flag on it retains it instead — one conditional status write, no counter movement, so the amount goes on being counted against the limit exactly as it already was. A retention is ended deliberately, by an operator settling it at the figure the provider's own record shows or releasing it when that record shows no charge; the gateway supplies neither, which is why the reservation was held |

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
| **C10.1** Every guarantee-shaped sentence in the covered documents is registered with the reason it is allowed to say that — a clause here, a row in [EVIDENCE.md](../EVIDENCE.md), a limit stated in the sentence, or a named debt. | E | `test_claims_are_anchored.py` over `README.md`, `docs/SCOPE.md`, `docs/design/hard-ceiling.md`, against `contracts/claims/anchored.json`. Adding or editing such a sentence fails the build until its author points at the reason |
| **C10.2** A sentence qualified because the unconditional version is not true yet carries a `debt:` anchor naming the clause that would make it unconditional, and that clause is on the open-items list. | E | `test_claims_are_anchored.py`. This is the clause that keeps honesty from becoming retreat: a weakened sentence and the work that would strengthen it are the same list |
| **C10.4** A clause's own citation resolves: a test named here exists, and a named node is a function defined in the suite. | E | `test_contract_clauses_cite_real_tests.py`. The failure this prevents is the one this document is most exposed to — a test is renamed or deleted, the row keeps citing it, and an unenforced clause goes on reading as enforced while looking audited. It also requires that a clause at P or E cites something at all |
| **C10.3** A verdict word in a comparison table is not contradicted by its own cell. | **B** | The lint treats a table cell as a sentence, so the verdict is registered like any other claim; it cannot check that a cell agrees with itself |

The lint cannot tell whether a sentence is TRUE — only whether someone was made to
point at the reason. That is its honest limit, and it is the difference between "we
are careful" and "carelessness fails the build".

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

## Open items, named rather than implied

These are contract clauses the code does not satisfy yet. They are listed here
because a contract that quietly omits its failures is worse than no contract. A
clause that has been closed leaves this list; a residual stated inside a clause's
own cell is not an open item, because there is nothing outstanding to do about it
without paying a cost the clause names.

- **C3.2 for the per-user token reservation.** The admission transaction debits up
  to three counters; the hold row records only the pool amount, so a crash between
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
- **C12.5 has no mechanism.** Credential material is kept out of every store and log
  by construction, and nothing stops a future call site from putting it in one. A
  static sweep — no repository or logger call takes a value derived from the wrapper
  key or a provider token — is what would move it from B to E.
- **C10's lint covers three documents.** `README.md`, `docs/SCOPE.md` and
  `docs/design/hard-ceiling.md` are anchored. `ARCHITECTURE.md`, `ADMIN_GUIDE.md` and
  `DEPLOYMENT.md` make narrower operational claims and are not swept yet.
- **The default deployment does not exhibit C1.5.** `STRATOCLAVE_HARD_CEILING_GATE`
  ships off so admission is priced by an estimate, and `STRATOCLAVE_UNOBSERVED_HOLDS`
  ships off so a reclaim returns budget for a call the provider measurably billed.
  Both defaults are deliberate — an operator measures a refusal rate before enforcing
  one — and what the flags gate is C1.5 specifically: a *bound* on the settle, and
  retention of an unobserved charge. They do not gate the differentiator. With every
  flag at its shipped default, admission and the money move are still one conditional
  transition in one authoritative store (C1.1, C1.3), a reservation still reaches
  exactly one ending (C3.1), the ledger is still the charge of record (C4.1, C4.3),
  and the Z3-checked no-double-post invariant still holds over the transition model
  the default path takes. The distance between the default and C1.5 is a real gap and
  it is stated; it is not the gap between this artifact and its own headline.
