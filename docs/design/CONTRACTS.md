# The contract this gateway is judged against

This document is the upstream object. Everything below is a clause the code must
satisfy, the guarantee level it holds at, and the test that fails if it stops
holding. A clause with no test is a statement about one commit, not about the
project, so the third column is part of the clause rather than a note on it.

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
| **C2.3** A published derived money figure is reproducible from stored facts. | B | Recomputable from the rows and a pinned rate table; the report does not yet embed the rates it priced with, and detail rows past the stored cap are not retained |
| **C2.4** The set of billable legs is declared in one place; adding a leg to the charge without adding it to the estimate is impossible to do silently. | E | `test_billable_legs_registry.py` |
| **C2.5** A rate document is one complete validated value: every leg present, every rate a non-negative integer, a version read whole or refused, and a version's rows and row COUNT immutable once written. Validated at every boundary that consumes a row — including the point read that builds the frozen snapshot, not only the bulk load. An invalid document is not a transient, so it refuses admission instead of quietly leaving the previous rates in place. | E | `test_contract_price_identity.py` |

## C3 — Termination and recovery

Every reservation reaches exactly one ending, and every crash point has a bounded
recovery.

| Clause | Level | Enforced by |
| --- | --- | --- |
| **C3.1** Exactly one ending per reservation. Two mechanisms must not both be able to end the same one. | E | `test_contract_termination.py`, `test_money_lifecycle_discipline.py` |
| **C3.2a** …for the tenant dollar pool. | B | The hold row plus the reaper, bounded by the TTL and the next pooled request from that tenant (see C3.3) |
| **C3.2b** …for the per-user token reservation. | **N (today)** | Nothing reaches it. The hold row records only the pool amount and `credit_used` is not period-scoped, so a crash between reserve and settle debits a user permanently — see the open items below |
| **C3.2c** …for the per-model quota counter. | **N (today)** | Same shape as C3.2b, bounded instead by the counter's monthly TTL |
| **C3.3** That mechanism's reachability does not depend on the tenant sending more traffic or on the calendar period. | N (today) | The sweep is request-driven and covers the current and previous period only — see the open items below |
| **C3.4** An ended reservation cannot be ended again in either direction. | P + E | `test_billing_formal_z3.py`, `test_contract_termination.py` |
| **C3.5** After any ending, counters and ledger agree. | **B** — for an ending whose terminal transaction COMMITTED | `test_billing_write_discipline.py`. A settle that exhausts its retries is not re-driven, so the reaper later ends the hold with a settled delta of zero and the observed usage never reaches the ledger. That is the negation of this clause, and it is in the open items rather than in this cell only because the cell cannot hold it |

## C4 — Ledger sufficiency

The ledger is the charge of record; the counters are a cache of it.

| Clause | Level | Enforced by |
| --- | --- | --- |
| **C4.1** Every **dollar-pool** counter move carries its ledger event. RESERVE and the terminals are in the same transaction as the move; the release path is two writes with the reaper as backstop. | B + E | `test_billing_write_discipline.py`. Scoped to the pool on purpose: the per-user token counter and the per-model quota counter move with NO ledger event, so the unqualified "every counter" reading of this clause is false |
| **C4.2** The **dollar-pool** counters are reconstructible from the events alone for every period the system claims to cover. | B | Stated boundary for pre-P2 periods in `derived_totals`. The token and quota dimensions are not reconstructible at all (C4.1) |
| **C4.3** Events are append-only by the mechanism the docs claim. | B | Per-write conditions on each event key; IAM excludes update/delete on the ledger table, and the idempotency-status update is the one write that needs it |
| **C4.4** Every event answers, without the live rate table: what was charged, at which version, for which request — and does not assert a measurement nobody made. | E | `test_contract_reporting.py`, `test_rating_differential.py` |

## C5 — Idempotency identity

One idempotency key means one authorization, for all time.

| Clause | Level | Enforced by |
| --- | --- | --- |
| **C5.1** A retry that crosses a billing period resolves to the same authorization. | E for records written by this version or later; **B** for older ones until the backfill has run | `test_contract_idempotency.py`. Records written before the identity left the money partition are read where they were written, but only for the period supplied and the one before it — the reader cannot guess an older period. `scripts/local/backfill_idemp_partition.py` copies them into the permanent partition and closes that window; it is an upgrade step, listed in [DEPLOYMENT.md](../DEPLOYMENT.md) |
| **C5.2** The mapping from a client key to a stored row is injective, and a replay verifies the key itself rather than the address it was found at. | E | `test_contract_idempotency.py` |
| **C5.3** A replay returns the original outcome and never mints a second money move. | E | `test_contract_idempotency.py`, `test_billing_authorize.py` |
| **C5.4** A retry can tell committed from not-committed without guessing. | B on the transactional path; **N under the PENDING protocol** | The transactional path writes the record inside the reserve transaction. Under `STRATOCLAVE_RESERVE_PROTOCOL=pending` the intent is written before the commit point and finalized best-effort, and one replay path reads the intent's presence rather than the marker — so a REFUSED authorize can replay as authorized. There is no configuration of that protocol in which this clause holds |

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
| **C8.3** An outcome the gateway could not observe is classified and recorded rather than assumed free or assumed chargeable. | E | `test_provider_outcome_formal.py`, `test_money_lifecycle_discipline.py` |

## C9 — External authorization

The authorize / capture / void surface is a two-phase money API, beside the inline
hold lifecycle rather than inside it. It is a shipped contract and was missing from
this document, which is worse than a misclassified clause: a reader could not tell
whether it had been audited at all.

| Clause | Level | Enforced by |
| --- | --- | --- |
| **C9.1** Once a charge is committed, no later read of that authorization reports it as absent or as a different amount. | B | `test_billing_authorize.py`. Holds on the transactional path. Under the PENDING protocol a terminal-then-retry window can answer 404 before the asynchronous RESERVE projection lands — see the open items |
| **C9.2** Every pair of concurrent operations on one authorization resolves to one terminal state, and the loser learns which state won. | E | `test_billing_authorize.py`, `test_billing_authorize_stateful.py` (a stateful model over interleaved capture / void / reap) |
| **C9.3** Expiry means one thing, stated. | **N (today)** | It means "reclaimable after this instant", NOT "no capture after this instant": a capture that writes its terminal before the reaper wins, even past the expiry the API reported. Enforcing the other reading needs an expiry condition inside the external capture transaction |
| **C9.4** A client-supplied field cannot produce an unhandled server error, and an amount cannot exceed what was authorized. | B | `test_billing_authorize.py` covers the over-capture refusal on both sides. `amount_microusd` has no upper bound, so a value beyond DynamoDB's numeric range is a client-triggered 500 |
| **C9.5** The answers do not depend on which internal protocol mode the deployment runs. | **N (today)** | C9.1 and C5.4 both differ by protocol mode |

## C10 — Claims

Every guarantee in the public documents is true of the shipped code, or states the
boundary at which its evidence stops.

| Clause | Level | Enforced by |
| --- | --- | --- |
| **C10.1** A verdict word in a comparison table is not contradicted by its own cell. | **N (today)** | Nothing checks it |
| **C10.2** Every guarantee-shaped sentence anchors to an [EVIDENCE.md](../EVIDENCE.md) row or to a clause here. | **N (today)** | Nothing checks it |

This is the one contract with no enforcement, which by this document's own opening
rule makes it a statement about the present commit. Both reviewers of the audit said
so independently. The mechanism that would fix it is a documentation lint — a test
that fails when a guarantee-shaped sentence in README/SCOPE has no anchor — and it
is not written yet. Until it is, C10 is held by review, and the record of what
review found is [EVIDENCE.md](../EVIDENCE.md).

---

## Open items, named rather than implied

These are contract clauses the code does not satisfy yet. They are listed here
because a contract that quietly omits its failures is worse than no contract.

- **C3.2 for the per-user token reservation.** The admission transaction debits up
  to three counters; the hold row records only the pool amount, so a crash between
  reserve and settle leaves `credit_used` debited with nothing to reach it. The
  counter is not period-scoped, so it never resets.
- **C3.3 reachability.** The reaper runs inside a pooled reserve and scans the
  current and previous period, so a hold orphaned in a quiet month is never
  reached. A scheduled reconciler already exists for other work; giving it the
  inline holds is the fix.
- **C3.5 after a settle that never commits.** When the settle transaction exhausts
  its retries, nothing re-drives it: the reaper later writes `RECLAIM` with a
  settled delta of zero and the observed usage never reaches the ledger. (A
  reservation restored from a PRE-SNAPSHOT ledger event is a different case and is
  closed: it settles at the amount the admission debited, which is an upper bound,
  rather than being refused into that same hole.)
- **C5.4 under the PENDING protocol.** The intent is written before the commit
  point and finalized best-effort, and one replay path reads the intent's presence
  rather than the marker, so a refused authorize can replay as authorized.
- **C2.3 embedding.** Published savings figures cite a rate version but do not
  embed the rates they were computed from, so a re-run after a rate change
  reprices rather than replays.
- **C9.1 under the PENDING protocol.** A capture retry in the window after the
  terminal commits but before the asynchronous RESERVE projection lands answers 404
  for an authorization that has already been charged.
- **C9.3 expiry.** The instant the API reports is when the reaper MAY reclaim, not
  when capture stops being possible. Two readings of one field.
- **C10 has no enforcement.** The documentation lint that would make it an E clause
  does not exist, so the claim-versus-code gap is held closed by review.
- **The default deployment does not exhibit C1.5.** `STRATOCLAVE_HARD_CEILING_GATE`
  ships off so admission is priced by an estimate, and `STRATOCLAVE_UNOBSERVED_HOLDS`
  ships off so a reclaim returns budget for a call the provider measurably billed.
  Both defaults are deliberate — an operator measures a refusal rate before
  enforcing one — but the headline guarantee belongs to the other position of both
  flags, and that is a property of this artifact rather than a footnote.
