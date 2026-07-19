<!-- Last updated: 2026-07-19 -->
<!-- Applies to: Stratoclave main -->

# Scope and Responsibility Boundary

This document is the **canonical statement of what Stratoclave is, what it is
NOT, and the rules used to decide whether a proposed feature belongs in it.**
It exists so that a future "let's add X to Stratoclave" proposal can be judged
against a written boundary instead of taste, and so that the two ways this
product can bloat into a worse version of a bigger competitor are named and
guarded against.

If you are about to add a feature, read [Decision rules](#decision-rules) and
[What Stratoclave must NOT own](#what-stratoclave-must-not-own) first.

## The three co-equal responsibilities

Stratoclave has **three responsibilities of equal weight.** It is not a billing
back-office with a proxy bolted on, nor a router with a ledger bolted on. It is
the single pass-through point for a tenant's LLM traffic, and it turns every
pass-through into fact recorded in three ledgers: the **money** ledger, the
**operational** ledger (the gateway itself), and the **learning** ledger.

1. **AI Gateway (a LiteLLM competitor).** A multi-provider, OpenAI-compatible
   unified API that applications call directly. Provider adapters, streaming
   relay, virtual keys with per-team budgets, fallback/retry, rate limiting,
   response cache. This is the front door for LLM traffic — not a hidden
   billing sidecar. If this layer is weak there is nothing to carry the other
   two.

2. **Billing gateway.** Metering, rating (physical units → micro-USD, with
   margin / provider-cost allocation), pre-flight authorization (two-phase
   authorize/capture, tiered circuit breaker), and an event-sourced,
   idempotent, tenant-isolated credit ledger whose **zero-double-posting
   invariant is formally proven (Z3)**. This is the money ledger.

3. **Routing optimization + learning-signal supply, in loose coupling with an
   external VSR.** Stratoclave connects to an external VSR (vLLM Semantic
   Router) and *executes* its routing advice under budget/authorization
   constraints, and — as a first-class duty — **collects and supplies the
   information needed to optimize and learn routing.** The `decision_log`,
   `signals`, and the SAAR router memory are not write-and-forget: they are a
   **data product with a defined export contract** to the VSR and to learning
   systems. The gateway is the *only* place that can completely observe "which
   request routed where, cost how much, waited how long, and succeeded or not."
   That observation position is not replaceable by the VSR or by a learning
   system, so producing it — with a fixed schema, completeness, ordering, and
   re-delivery guarantees — is Stratoclave's job.

These three are not independent. The ledger's accuracy makes routing
optimization *measurable*; routing optimization shows up as cost reduction *in
the ledger*; the learning ledger *feeds the routing that the ledger measures*.
That closed loop is the product's coherence, and the thing neither a pure
billing SaaS nor a pure router can reproduce.

## The boundary, in one sentence

> **Stratoclave is the single pass-through point for LLM traffic; it confirms
> and supplies the facts of that pass-through as three ledgers — money,
> operational, learning — while keeping the _judgment_ external and the
> _execution and recording_ internal.**

The operating maxim is: **judgment is the VSR's, execution and recording are
Stratoclave's.**

## Decision rules

Use these to judge whether a proposed capability belongs in Stratoclave. A
capability that fails the relevant test is someone else's job.

1. **Pass-through-fact test.** Can the feature be derived directly from the
   fact that a request/response passed through the gateway? If yes, it is a
   candidate to own. If it needs inference, classification, or training, it is
   external.
2. **Judgment-vs-execution test.** Does it *make* a routing/quality judgment?
   Then it is the VSR's. Does it *execute, constrain, or record* a judgment?
   Then it is Stratoclave's. (A static fallback target when the VSR is down is
   execution — a default value, not an algorithm — so Stratoclave keeps it.)
3. **Charge-amount test** (scoped to responsibility #2). If it touches the
   money ledger, does it serve the correctness of rating and authorization?
4. **Consumer test.** For anything collected under responsibility #3, is there
   a defined schema AND a defined export destination (the VSR or a learning
   system)? Data collected with no consumer is not a learning ledger, it is a
   junk pile — do not collect it. (SAAR router memory passes this test: its
   consumer is routing optimization. That is why it is core, not "scope creep.")
5. **Training test.** Does it train, hold, or update a model? Then it is
   external. Emitting data in a trainable form is the boundary; running the
   training job or owning a feature store is across it.

## What Stratoclave must NOT own

Owning three responsibilities does not mean owning everything. The line still
holds.

- **Routing intelligence (the VSR's domain).** Semantic classifiers, the
  routing algorithm itself, routing-quality evaluation models, model-selection
  intelligence. Stratoclave passes request features + router memory to the VSR,
  executes the verdict under budget/authorization constraints, and records the
  outcome. It never re-implements the judgment. **This is the single most
  important line: the moment Stratoclave writes its own semantic classifier or
  quality scorer, the loose coupling collapses and responsibility #3 mutates
  into "build a VSR replacement" — a different product.**
- **Model training / ML platform.** Training jobs, feature engineering,
  evaluation pipelines, feature stores. Stratoclave supplies data in a
  trainable form; it does not train.
- **Commerce.** Payment processing, invoicing, tax, dunning, plan-management
  UI, per-seat / outcome-based / agent-specific price *packaging*. The ledger
  confirms consumption; the movement of money and its packaging is external
  (Stripe / Metronome / Orb / Lago). Outcome-based pricing ($/resolved ticket)
  is explicitly out: verifying "resolved" needs business-domain ground truth
  the gateway cannot access, and it would make Stratoclave the arbiter of a
  billing dispute it has no evidence for. If outcome billing is ever needed, it
  is an **external** layer that consumes the ledger export and joins on
  `span_id`; it is never built into the core.
- **Generic infrastructure best bought off the shelf.** Dashboards, alerting
  backends, generic messaging pipes, observability UIs. Stratoclave owns the
  *content and schema* of the decision log; it does not own the pipe that
  carries it or the screen that renders it.

## LiteLLM differentiation

Stratoclave is **open source (Apache 2.0)**, so the comparison is on capability,
not on distribution.

Split the "provider network" axis in two — conflating them is an evaluation
error:

- **Breadth of connection** — LiteLLM wins. Its 100+ adapters are not a list of
  URLs; they are years of absorbed per-provider streaming quirks, tool-call
  schema differences, retry semantics, and auth. "OpenAI-compatible, so anything
  connects" is ~80% true; the remaining ~20% (Anthropic-native features, Gemini
  multimodal, provider-specific tool shapes) is LiteLLM's real moat and the vLLM
  seam does not auto-fill it. On the **parity layer** the goal is to not lose,
  not to win: OpenAI-compatible unified API, adapters for the major providers,
  virtual keys with per-team budgets, fallback/retry, response cache.
- **Depth of integration** — Stratoclave wins, and not narrowly. Bedrock, a
  self-hosted GPU (the `served_by="vllm"` transport seam + `VLLM_ENDPOINTS`
  allowlist — vLLM speaks OpenAI-compatible, so effectively any open model), and
  any OpenAI-compatible endpoint all flow through the **same** reserve / rating /
  settle and the same Z3-proven ledger. LiteLLM *connects* many backends; their
  billing and idempotency are best-effort. Binding an arbitrary backend under a
  formally-proven charge of record is a different category of capability.

The three **weapons** LiteLLM does not have, all deriving from the strength of
"fact confirmation":

1. **A formally-proven billing ledger.** LiteLLM's cost tracking is approximate
   observability that cannot stand as a charge of record. A pre-authorized,
   exact, Z3-proven ledger is the "buy" side of build-vs-buy for any product
   that bills tenants for LLM usage.
2. **The decision log as a first-class data product.** LiteLLM's logs are
   operational logs, not schema'd, trainable decision records with a defined
   export contract.
3. **Full VSR integration.** LiteLLM routing is static rules plus load
   balancing. Stratoclave executes the VSR's semantic routing woven together
   with budget constraints, pre-authorization, and session affinity (SAAR
   router memory).

Concretely, VSR integration is three pieces: (a) a **hint protocol** carrying
request features + router memory + tenant budget state to the VSR; (b) an
**execution layer** that enforces the VSR verdict under authorization
constraints; (c) a **feedback channel** returning real cost / latency /
success as an outcome to the VSR and learning systems. This triple is the only
construction in which "did the routing decision actually get cheaper" is
verifiable *against the money ledger* — something neither LiteLLM nor a
stand-alone VSR can build. The differentiation is not any single feature; it is
the **architecture that keeps judgment (VSR) and execution+recording
(Stratoclave) separate while closing the loop between them.**

**The asymmetry.** For Stratoclave to absorb LiteLLM's edge is a thin
compatibility shim for the non-OpenAI-compatible providers (weeks–months), and
that traffic then flows through the proven ledger too. For LiteLLM to absorb
Stratoclave's edge — an event-sourced, idempotent, formally-proven
reserve/rating/settle ledger — is a re-design, not a retrofit
(quarters–a year), carried out while preserving an existing best-effort billing
base. The moat is on Stratoclave's side. The one thing that keeps the lead
"provisional" rather than realized is the ledger's _speed_ — now **measured, not
asserted** ([benchmarks/ledger-latency.md](benchmarks/ledger-latency.md)): the
p50 is fast (ledger write 20 ms, end-to-end authorize 57 ms) but the **p99 does
NOT meet the < 50 ms target** — the ledger `TransactWriteItems` is p99 = 58 ms
even at the zero-contention floor, and single-pool-row contention degrades it
further. The miss is neither CPU- nor client-bound; it is the DynamoDB
transaction tail plus single-row optimistic-CAS retry. Honest current state:
_correctness_ proven, _speed_ measured and **below target on the current
synchronous four-item-transaction design** — closing it needs a design change
(single-item conditional update where the money-move allows, and/or pool-row
sharding), not tuning. See [next-and-not-next](#next-and-not-next).

## Two gravity wells (named warnings)

Stratoclave can bloat in exactly two directions, each toward a bigger, better
competitor:

- **Toward routing intelligence** — SAAR overreach, in-housing the learning
  loop's *consumer*, re-inventing the VSR. Endpoint: a worse AI-gateway
  platform. The discipline: dissatisfaction with a VSR verdict is expressed as
  feedback *on the decision record*, and the improvement is bet on the VSR's
  evolution — never on a home-grown classifier.
- **Toward commerce** — in-housing price models, seat management, invoicing.
  Endpoint: a worse Stripe.

Stratoclave wins on one thing only: **a formally-proven ledger and a
synchronous enforcement point, wired to an external judgment engine through a
schema'd feedback loop.** The Z3-proven zero-double-posting invariant is
diluted every time a feature adds code outside the proven region. **Before
adding a feature, ask whether it carries a provable invariant. If it does not,
it is someone else's job.**

## Where things stand today (2026-07)

- **Open source (Apache 2.0).** The comparison is on capability, not on
  distribution; the ledger's proof and code are inspectable.
- **Owned and built:** the AI-gateway core (unified API, adapters, streaming,
  virtual keys, fallback, rate limit); the billing core (metering, rating,
  two-phase authorize/capture, tiered breaker, Z3-proven ledger); LLM router
  execution (allowlist / breaker / VSR hard-pin / fallback chain); a transport
  seam (`served_by="vllm"`) that binds a **self-hosted GPU / any
  OpenAI-compatible backend** to the *same* reserve/rating/settle path; version-
  pinned external VSR consult with the "advice quality is the VSR's, honoring
  and charging is ours" boundary; SAAR (sticky, tool-loop lock, idle reset,
  decision drift, provider-state lock) as routing-optimization state; the
  offline VSR billing reconciliation.
- **Producer, not yet a defined data product:** `signals` / `decision_log` are
  written but their VSR/learning **export contract** is not yet fixed. This is
  the gap responsibility #3 most wants closed — see "next".
- **Deliberately not owned:** semantic classification / routing algorithm /
  quality scoring (VSR); model training (external); price packaging, invoicing,
  tax, outcome-based billing (commerce / external); MCP hub / registry.
- **Measured, and it points at a design change:** the ledger p99<50ms target is
  now benchmarked ([benchmarks/ledger-latency.md](benchmarks/ledger-latency.md))
  and **missed** — p50 fast (20 ms ledger / 57 ms e2e), p99 = 58 ms at the
  zero-contention floor, worse under single-row contention, and NOT CPU-bound.
  "A proven ledger you can afford on the hot path" is the entire pitch, so this
  is the highest-leverage item: it needs a design change (single-item
  conditional update and/or pool-row sharding), not tuning.
- **Hygiene, not features:** reconciliation runs from a manual CLI, not a
  scheduled job; the VSR-down path degrades to static routing but the
  graceful-degradation behaviour is not yet benchmark-exercised (fold it into a
  load test as a fault-injection
  scenario).

## Next and not-next

- **Next (single most-aligned feature): the decision-record pipeline.** One
  record per request — input features, the VSR (or static-rule) verdict,
  Stratoclave's execution result, real cost / latency / success, SAAR state
  transition — persisted with a fixed schema and streamed to the VSR and
  learning systems. It fixes the VSR-integration *contract* in code before the
  integration deepens (so loose coupling is not dragged into the VSR's internal
  schema), reuses responsibility #2's "never drop a fact" muscle, and turns
  accumulated records into a time-compounding learning asset even while the
  VSR's accuracy is still low.
- **Not-next (decline on sight): in-housing routing intelligence.** When a VSR
  verdict is unsatisfying, the temptation is to write "just a little" semantic
  classification or quality scoring. That is the moment the loose coupling
  dies. Express the dissatisfaction as a decision-record signal; bet the
  improvement on the VSR. If a proposal to in-house routing judgment resurfaces,
  this document is the rejection.
