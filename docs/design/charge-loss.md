<!-- Last updated: 2026-08-30 -->

# Contract: attempts, evidence, and liability

The sequel to [`hard-ceiling.md`](hard-ceiling.md). That contract made the pool limit bind for
requests the gateway observed. This one covers the requests it did not observe, which is
the only documented way the ceiling still breaks.

The verification lives in the test files [`hard-ceiling.md`](hard-ceiling.md) names, plus
`backend/tests/test_provider_outcome.py`, `test_provider_outcome_formal.py` and
`test_money_lifecycle_discipline.py` for the states and the endings.

Two designs that look reasonable are ruled out here rather than left open: converting an
unobserved hold into recorded spend after an interval, and a per-error-code billing table in
code. Section 8 records why, because both are easy to re-propose.

## Status in the shipped code

As of 2026-08-30:

- **The states and the liability table ship**, in
  `backend/mvp/provider_outcome.py`, and every ending of a reservation goes through
  one object (`backend/mvp/_money.py`), so the classification is recorded on every
  route.
- **Withholding an unobserved hold is gated** by `STRATOCLAVE_UNOBSERVED_HOLDS`,
  **off by default**. With it off, an unobserved outcome still returns the
  reservation, so the automatic release section 7 forbids is still the default
  behaviour. With it **on**, section 7's first step now holds end to end: the ending
  keeps the reservation, and the reaper no longer credits it back either — it moves
  the hold to `RETAINED` and records a `RETAINED` ledger event carrying the attempt
  marker, so the amount stays counted against the limit with no counter moving and
  nothing offers to return it again. A retention ends only by an operator's
  decision, at the figure the provider's own record shows (`POST
  /admin/tenants/{t}/pool-retained/{hold_id}/resolve`) or as a release when that
  record shows no charge; both go through the same money primitives a request uses.
  There is **no automatic release**, by reaper, timer or policy, which is what
  section 7 forbids.
  **What still does not exist** is section 7's other half: exposure accounting with
  saturation alarms. `GET /admin/tenants/{t}/pool-retained` reports `held_microusd`,
  so the figure is available, but nothing watches it — a provider outage under the
  flag can saturate a tenant's period budget and the first signal is a refusal.
  That is the reason the flag is still off by default, and it is the work that would
  change that.
- **The per-attempt marker ships** (`attempt_request_metadata`), and the reaper
  records the reaped hold's own facts in its RECLAIM terminal rather than asserting
  a zero.
- The measurements this rests on, with the point where each one's evidence stops,
  are in [`../MEASUREMENTS.md`](../MEASUREMENTS.md).

## 1. What was measured, because the premise was not a fact

AWS does not document which failures are billed, so it was measured: CloudWatch
`AWS/Bedrock` counters, one condition per minute, on models the account otherwise never
invokes. Probes are in `~/tmp/hogehoge/chargeloss/`.

| Condition | Caller saw | Invocations | Output tokens |
|---|---|---|---|
| normal completion | success, usage 2/16 | 1 | 16 — exactly the reported usage |
| read timeout, one attempt | ReadTimeoutError | 1 | **1,493** |
| stream closed after 2 events | partial content | none | none |
| stream run to completion (control) | success, usage 13/189 | 1 | 189 |
| service rejection, `maxTokens` over limit | ValidationException 400 | 1 | **none** |
| service rejection, empty text block | ValidationException 400 | 1 | **none** |
| SDK-side rejection | ParamValidationError | none | none |

Four conclusions:

1. **The line is not failure versus success. It is whether the model ran.** A service
   rejection costs nothing; a generation the caller abandoned is billed in full. Both look
   like "an exception" from inside the gateway, and treating them alike is the defect.
2. **`Invocations` is not a billing proxy** — it counts rejected requests too. The token
   counters are the billing-relevant instrument. Reading `Invocations` alone gives the
   opposite answer for rejections.
3. **The counters are not stream-blind.** A completed stream is counted with exact usage, so
   the abandoned stream's total absence is evidence that an aborted stream is not counted,
   rather than evidence that streams are invisible. The control is what makes this readable.
4. **Attempts.** `Config(retries={"max_attempts": N})` means N *retries*; botocore 1.35.99's
   `_compute_retry_max_attempts` sets `total_max_attempts = N + 1`. Measured: `max_attempts=1`
   produced two invocations, `total_max_attempts=1` produced exactly one. The shipped
   `max_attempts=2` is therefore **up to three provider invocations against a reservation
   priced for one**.

Every number above is one trial per condition on one model family. It is evidence for a
policy row, not a specification — which is precisely why section 4 puts it in data.

## 2. The three broken paths

1. **Read timeout.** `mvp/anthropic.py`'s generic `except Exception` refunds the whole
   reservation and releases the hold, for a call the provider billed in full.
2. **SDK retry, on the success path.** Attempt 1 times out at the client and completes at
   the provider; botocore retries; attempt 2 succeeds; the caller sees **an ordinary
   successful response** and one settle is recorded. Measured: a call with `read_timeout=2`
   returned success after 4.1 s. **No exception is raised, so no error classification can
   ever see this path.** Largest of the three.
3. **Process death.** The reaper credits an expired hold back unconditionally
   (`actual_microusd=0` in `_pipeline.py`'s `_sweep_one_period`), with no way for a hold row
   to say the provider may already have charged it.

None breaks an invariant. They are a false premise underneath correct arithmetic, which is
why the ceiling can be exceeded with every proof intact.

## 3. The retry must move, not disappear

`mvp/routing/infrarouter.py` retries and fails over across regions on purpose, and
`mvp/routing/classify.py` maps `ReadTimeoutError` to FAILOVER — the availability design
deliberately re-sends the request this contract calls unsettled. Deleting that trades a
money defect for an availability regression.

So: **the transport makes exactly one attempt** (`total_max_attempts: 1`, the unambiguous
key, on both the Bedrock and streaming clients), and retry lives only where it is already
recorded as an `AttemptRecord`. A retry the gateway cannot see is an unaccounted attempt.

Related, in the same file: `_bedrock_clients.py` justifies `standard` mode with "keeps boto3
quiet retries off for streaming responses". No retry mode retries mid-stream once the event
stream is returned, and standard mode *will* silently retry the initial `ConverseStream`
call. The reasoning is wrong even where the conclusion is convenient.

Also latent: the reap-timeout derivation multiplies `(connect + read)` by
`RETRY_MAX_ATTEMPTS` believing it is an attempt count, understating the Bedrock leg by one
attempt. It does not break the invariant today only because `max()` is dominated by
mantle's 600 s read timeout. Fix the meaning, not the number.

## 4. The design: attempts, evidence, liability

**The accounting unit is the wire attempt, not the logical request.** Each attempt opens a
liability equal to its priced ceiling. Only *evidence* discharges it: a usage block in a
successful response, observed streamed chunks (as a floor), or later reconciliation.

**Invariant.** `settled_final + observed_floors + open_liability <= pool_limit`, per tenant,
at every ledger-reconstructible state.

**States are gateway-observable facts; the liability each carries is data, not code.**

| State | Evidence defining it | Liability |
|---|---|---|
| `NOT_SUBMITTED` | transport failed before the request was written (DNS, TLS, serialisation) | 0 |
| `REJECTED_PRE_INFERENCE` | provider 4xx with a request id, no token counters | 0 **by policy row**, loss risk explicitly accepted |
| `SUBMITTED_UNSETTLED` | bytes sent, no terminal evidence — **the default for anything unrecognised** | full attempt ceiling |
| `PARTIAL_OBSERVED` | chunks the gateway counted, `is_final=false` | floor settled, residual ceiling open |
| `SETTLED_FINAL` | usage block in a successful response | the observed amount |
| `WRITTEN_OFF` | operator policy, inside the write-off budget | 0, charged to the loss budget, alarmed |

This is what "do not encode today's billing semantics" means concretely: if the provider
starts billing rejections, **one policy row changes** and the invariant, the ledger schema
and the proofs are untouched. Requirements on the table itself:

- Each row is scoped (provider, API, region, model family, pricing mode, SDK path, date) and
  **cites the measurement that justifies it**.
- Changes are versioned migrations, not config edits.
- A zero-liability row is only ever allowed as an operator's accepted risk, never as a fact.
- **Staleness detection is part of this contract.** A canary re-runs the section 1
  measurements on an isolated model and minute, and compares token counters — not
  `Invocations` — against gateway-observed usage. Without it a provider change is discovered
  by money going missing.

**Holds are immutable, one per attempt.** Growing a hold's amount in place would mutate a row
carrying a pinned payload hash and a single PENDING marker that the leak reconciler and the
dedup path both key on. One attempt, one ceiling, one marker, one settlement path; the extra
row is cheaper than corrupting recovery.

**Nothing fabricates an amount.** `actual=0` (today's reaper) and `actual=ceiling` (the timed-conversion design)
are both numbers the gateway never observed. A failed attempt is a ledger event **carrying no
amount** — which is also the answer to "failures must be recorded": one typed event per
attempt, in the ledger rather than a side log, because an unobserved liability that stops
counting against the pool stops the ceiling from binding.

A `PARTIAL_OBSERVED` event must be visibly not final: `is_final=false`, the observed token
facts and how they were observed, the attempt ceiling, the floor, the residual, the reason,
and the evidence source. The recomputable quantity is then "observed spend floor plus open
liability", not "actual spend".

**The ledger algebra is restated, not patched.** The existing model's spine is that every
terminal closes a hold with an amount. Amount-less events break that closure, so the top
invariant is restated as above and re-verified, with the old theorems preserved as the
special case where the open-liability set is always empty. Inserting amount-less terminals
into an amount-based model would silently change the algebra.

## 5. The operator knob is a loss budget, not a timer

"Release after T" decides when money might be lost, not how much. The knob is: release
unverified holds oldest-first, never writing off more than X per tenant and Y account-wide
per day; when the budget is spent the remaining holds stay pinned and that tenant's headroom
shrinks; alarm on saturation. A saturated write-off budget means evidence is systematically
not arriving — an incident, not a setting.

The asymmetry is decided deliberately: **the design is permitted to over-hold, never to
under-hold.** Under-holding breaks a sold guarantee; over-holding degrades a service level.

## 6. What cannot be done from inside the gateway

- The actual cost of an unsettled attempt. The gateway can bound it, not know it.
- Stopping the provider from executing an abandoned request. Once bytes are sent the money
  may be committed; abort semantics are the provider's.
- Nothing else. **Per-attempt reconciliation is verified to work** — see section 6b, which
  supersedes the reading that it was impossible.
- Tightness. The ceiling can bind or holds can release quickly, not both — though section 6b
  moves the release from "period rollover" to roughly a minute.

## 6b. The evidence channel, measured end to end

Model invocation logging was enabled in us-east-1 with text, image and embedding delivery
**off**, delivering to a CloudWatch log group. What the record carries was then measured for
exactly the cases the gateway is blind to.

| Case the gateway cannot observe | Record exists | Token counts in the record | Lag |
|---|---|---|---|
| non-stream call abandoned on read timeout | yes, found by our own `requestMetadata` marker | `in 22 / out 1,493` — the exact charge | 41 s |
| stream closed by the consumer after 2 events | yes, `operation: ConverseStream` | `in 0 / out 0` | 35 s |

Consequences, all of which tighten the design rather than complicate it:

1. **`SUBMITTED_UNSETTLED` has a real discharge path.** The record is keyed by the marker the
   gateway stamped, so the amount is settled exactly, per attempt, about a minute later. The
   write-off budget becomes a safety valve for when evidence fails to arrive, not the sole
   settlement path for every ambiguous attempt.
2. **The abandoned stream needs no policy guess.** Two independent instruments — the metric
   counters and the log record — agree it produced zero tokens. The reconciler settles it at
   zero from evidence instead of the table asserting it.
3. **The reconcile window has a measured floor**, 35-41 s (n=2). A hold cannot be released
   before the lag has passed, and the window must be derived from a re-measured lag, not a
   chosen constant — the same discipline the first contract applied to the reap timeout.
4. The record's `identity.arn` is the caller's role, which for the gateway is one task role
   for all tenants. Identity cannot attribute; `requestMetadata` is the only handle, so
   stamping it is a correctness requirement, not an optimisation.
5. Logging is account-wide per region: it now captures every Bedrock call in this account in
   us-east-1, metadata only, no prompt or completion text. Other regions remain unset. The
   probe log group carries 7-day retention, so a reconciler must run well inside that.

## 7. What ships first, and what must not ship yet

**First:** transport retries off; every attempt a typed ledger event including failures;
immutable per-attempt holds; settle-final only from a response usage block; and the reaper's
unconditional `actual=0` credit deleted so an expired hold stays open. That closes the
measured double-charge hole and satisfies the record-the-failure requirement.

**Must not ship until the capped write-off budget with per-tenant and account exposure
accounting and saturation alarms exists:** any automatic release of an unobserved hold, by
reaper, timer, or policy. An uncapped timed release is the timed-conversion design under a different name.

## 8. What was proposed and discarded

- **Converting an unobserved hold to spend after an interval.** Records an amount
  that may never have been spent, priced at the bound, into a ledger sold as recomputable.
  Rejected independently twice. It also makes a provider outage permanently consume every
  tenant's period budget.
- **Matching provider records by model, region and time window.** Not sound enough to charge
  a tenant on: account-wide logging admits foreign traffic, two tenants overlap on one
  popular model, retries multiply records. Superseded by `requestMetadata`.
- **A per-error-code billing table in code.** Relocated to a versioned policy table
  with cited evidence and a liability default, because the codes are a provider behaviour.
- **Reserving the whole failover chain up front.** Costs a small tenant its availability for
  a worst case that usually does not happen; replaced by admission per attempt.
- **Growing a hold in place.** Mutates a proof object; replaced by one hold per attempt.
- **A third class for "billed, amount unknown" and a drift-attribution pipeline.** Both
  existed only to manage ambiguity that exact correlation removes.

## 9. Acceptance criteria

1. A read timeout against a real provider call does not refund, and the amount stays held.
2. Every provider invocation an admitted request may cause is covered by admission, shown by
   counting the provider's token counters for a request whose first attempt times out. State
   the attempt count and the setting that produces it, in the terms botocore honours.
3. A charge that lands while the gateway dies before settling is never returned, shown by
   killing the task between the provider charge and the settle on a deployed stack and
   reconstructing the pool from the ledger.
4. State is assigned by one function used by all three routes, with a test per state and an
   unrecognised-error test that lands in `SUBMITTED_UNSETTLED`.
5. No code path converts an unsettled attempt into spend, and no code path records an amount
   that was not observed. A reader can tell a floor from a final amount from the event alone.
6. Open liability per tenant is a metric an operator can alarm on, and write-off is refused
   once the budget is spent.
7. The canary re-runs the section 1 measurements and fails when a policy row's cited
   evidence no longer holds.
9. Every provider call carries the attempt's id in `requestMetadata`, and the reconciler
   settles a `SUBMITTED_UNSETTLED` attempt from the matching log record's token counts. The
   reconcile window is derived from a re-measured delivery lag, and a missing record after
   that window does not release the hold on its own — only the write-off budget does.
8. The settled charge and recorded token fields for an observed request are unchanged. Only
   the unobserved paths move.
