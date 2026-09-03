# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This file records **what changed**. What the project guarantees, and how strongly, lives
in [`docs/design/CONTRACTS.md`](docs/design/CONTRACTS.md); a release note here should
point at a clause rather than restate it, because a guarantee written in two places
drifts in one of them.

## What 1.0 promises

Leaving 0.x means committing to a compatibility surface, so this is what it is.

- **The clause levels in [`CONTRACTS.md`](docs/design/CONTRACTS.md) are the surface.** A
  clause's guarantee level (**P** machine-checked, **E** a test fails if violated, **B**
  true inside a stated configuration, **N** deliberately not guaranteed) is not lowered
  in a minor or patch release. Lowering one is a major version. This is clause C10.5 and
  it is enforced: the levels are recorded, and `test_clause_levels_are_a_ratchet.py` fails
  when one weakens while the released major has not moved. It does not check that a level
  is CORRECT — that is a human reading the clause, and it is named as such.
- **The HTTP surface is the second half of it.** The three inference routes, their
  request and response shapes, and the scope names that authorize them do not change
  incompatibly within 1.x.
- **What is NOT covered.** Internal storage layout, the DynamoDB item shapes, the
  environment variable set beyond what [`DEPLOYMENT.md`](docs/DEPLOYMENT.md) documents,
  and anything named in the open items list at the bottom of `CONTRACTS.md`. The open
  items are the honest list of what is known to be incomplete; they are expected to
  move within 1.x.
- **Defaults may change in a minor release**, and when a default changes the entry here
  says so under *Changed* with the variable that restores the previous behaviour. A
  default is a judgement about what is safe, not an interface.

## Unreleased

### Added

- **Live price feeds behind the existing source seam** (`backend/mvp/pricing_feeds/`).
  `STRATOCLAVE_PRICE_SOURCE=bedrock-live` resolves rates from
  `bedrock:ListFoundationModelAgreementOffers` — the only place every current Claude
  price and the OpenAI GPT-5.x prices are published — from the Price List API's
  `AmazonBedrock` offer for the families AWS bills directly, and from an operator
  document for self-hosted vLLM capacity, all through one `Feed` contract. Off by
  default. New IAM: two read-only price actions;
  `bedrock:CreateFoundationModelAgreement` is deliberately not granted, because the
  agreement response carries the signed token that action consumes.
- **A durable last-known-good rate snapshot** (DynamoDB, its own partition of the
  pricing-config table). A restarted task reads the last successful fetch instead of
  falling back to the bundled document, and the stored table is the UNION of what was
  known and what was just read, so a pass that lost coverage cannot erase it. Clauses
  C2.6-C2.8 in [`docs/design/CONTRACTS.md`](docs/design/CONTRACTS.md), design in
  [`docs/design/price-feeds.md`](docs/design/price-feeds.md).
- `python -m mvp.pricing_feeds.fetch [--apply] [--json] [--strict]`: fetch prices now,
  diff against the stored snapshot, optionally store it. `--strict` exits 2 when the pass
  found something a person has to act on, so it works as a deploy gate.
- **Events that make a silent price failure visible**: `price_feed_coverage_regression`
  (a key stopped being readable), `price_feed_leg_regression` (one leg of a key did, and
  is now being charged from the stored value — the shape a lapsed promotional rate takes),
  `price_feed_key_spans_prices`, `price_feed_scope_widened`,
  `price_feed_unparsed_names`, `price_feed_not_authorized`, `price_table_changed`.
- `price_model_id` on a model registry entry: the id the price APIs know a model by, for
  the case where the billed spelling differs from the invoked one.
- **`python -m mvp.reprice`**: recompute a period's charges at another rate table — a
  stored version, the current effective table, or the floor — from the per-leg token counts
  the ledger already stores, reporting as-charged against as-repriced. Read-only: the charge
  of record stands, and a correction remains a separate, idempotent adjustment event.
  Clauses C2.9 and C2.10.
- The usage row now records `cache_read_tokens` and `cache_write_tokens` when the provider
  reported them. That row's stated purpose is that spend be re-derivable from it, and two
  legs out of four could only re-derive a request that used no prompt cache.

### Changed

- **`STRATOCLAVE_CODEX_ENABLED` now defaults to TRUE** (it briefly defaulted to FALSE in
  the change recorded below for 1.0.0 — see that entry for why, and why this reverses
  it). Codex does not itself gate money or safety: every request through it runs the
  same reservation/settlement pipeline and the same pool/quota walls as the Anthropic
  route. Defaulting it off did not make anything safer; it made `stratoclave codex`
  return a bare 503, for a reason nothing at deploy time mentioned, to any operator who
  deployed this gateway and never separately discovered and set an extra env var. Codex
  is one of this gateway's two supported CLIs, not an optional add-on. Set
  `STRATOCLAVE_CODEX_ENABLED=false` to opt back out (e.g. under strict residency, since
  this route's model registry pins the call to us-east-2/us-west-2 regardless of the
  deploy region).
- **The CLI's own fallback default model for `stratoclave codex`** (used only when
  neither an env var nor `~/.stratoclave/config.toml` names one — i.e. a CLI that has
  never run `stratoclave setup` against a codex-enabled deployment) **changed from
  `openai.gpt-5.4` to `openai.gpt-5.6-sol`.** The old value was codex's own upstream
  default, carried over unexamined; it is not in this gateway's model registry at all,
  so a bare `stratoclave codex` (no `--model`) on such a CLI failed immediately with
  `invalid_model`.
- **`stratoclave codex` now clarifies a non-retryable 4xx from the gateway** (a 402
  budget refusal, most concretely) instead of leaving codex's own retry loop to make it
  look like a broken connection. codex logs every failed provider call the same way
  regardless of cause, including "Reconnecting... N/5" for a request the gateway refused
  outright — the wrapper now relays codex's stderr line-for-line as before, but the
  moment it sees a terminal 4xx (anything 400-499 except 429), it also prints the
  gateway's own refusal body as prose: which wall refused, whether it can be raised, and
  the exact shortfall, none of it invented.
- **The bundled rate table now holds measured in-region list prices.** Every row was read
  from the provider's own APIs on 2026-08-31 and is pinned with its provenance in
  `tests/test_pricing_floor.py`. Notable corrections: GPT-5.6 sol was carried at
  $4.40/$22.00 per MTok and lists at $5.50/$33.00; Gemma, Nemotron and Qwen defaulted to
  the Opus tier for want of a published price and list at $0.14-$0.15 input, so they were
  over-charged by roughly 35x; the Claude tiers moved from the global rate to the
  in-region rate this gateway's `us.` profiles are actually billed at.
- **A pricing key is now one price point, not a family.** A shared key can only be
  charged at its dearest member, so `opus` covering Claude Opus 4.1 ($15/$75) and Opus 5
  ($5.50/$27.50) charged every Opus 5 request at nearly three times its rate. The registry
  splits `opus-legacy`, `sonnet-3`, `haiku-3-5` and `haiku-3` out, and a live fetch that
  finds a key whose models disagree now reports which models they are.
- **Claude Sonnet 5 has its own key at $2.20/$11.00 per MTok.** It lists BELOW Sonnet 4.5
  and 4.6 and was charged at their $3.30/$16.50 — 50% over — while it shared the `sonnet`
  key. Found by the new key-disagreement check on its first run against the real APIs,
  which is why that check reports structurally instead of only logging.
- **Cost tiers follow, because `_tier_for` derives a tier from the price rather than the
  name.** Gemma, Nemotron and Qwen move from tier 3 to tier 1, so a breaker downgrade now
  reaches the models it exists to reach.
- `default` is now priced at the dearest rate any registry entry is billed at rather than
  at the Opus tier, because Opus is no longer the ceiling.

## [1.1.0] — 2026-08-31

### Added

- **Exposure accounting and alarms for retained reservations.** `STRATOCLAVE_UNOBSERVED_HOLDS`
  defaults on as of 1.0.0, so retentions hold budget; until now nothing pushed the held
  amount anywhere and the first signal of an outage filling a tenant's headroom was a
  refusal for an unrelated request. The backend emits `retention_exposure` with the
  standing figures per tenant and period — when a retention is taken, when one is
  resolved, and from a sweep while any remain unresolved so the metric cannot go silent
  while the money is still held. Two alarms read it: saturation on the fraction of a pool
  that retentions hold, and staleness on the oldest unresolved one, kept separate because
  no single threshold distinguishes an incident in progress from an operator who stopped
  looking. This is the accounting `charge-loss.md` section 7 names as the precondition for
  ever releasing an unobserved hold automatically; the automatic release itself still
  needs a capped write-off budget and remains unbuilt.

## [1.0.0] — 2026-08-31

The first release that is not alpha. 290 commits since v0.2.0. The theme is that the
enforcement this gateway exists for is now what a default deployment does, and that the
money paths lose nothing on the failure routes.

### Breaking

- **The two money gates default to ON.** `STRATOCLAVE_HARD_CEILING_GATE` previously
  shipped off, so a default deployment priced admission with an estimate that the design
  document proves is not an upper bound; a request could settle above its reservation.
  `STRATOCLAVE_UNOBSERVED_HOLDS` previously shipped off, so a reservation whose provider
  call had departed but whose outcome was never observed was handed back as though the
  call were free — it is not, an abandoned Bedrock call is billed for the full
  generation. Both now default on. Set either to a falsy value to restore the previous
  behaviour; running with the ceiling gate off is the deliberate way to measure a
  refusal rate before refusing anyone. See [C1.5 and
  C8.3](docs/design/CONTRACTS.md).
- **`CODEX_ENABLED` → `STRATOCLAVE_CODEX_ENABLED`, and it now defaults to FALSE.** The
  old name is read as a deprecated alias with a warning. The previous default was
  effectively `true` because the CDK synth injected `'true'` when the operator left it
  unset, so an operator who wanted only the Anthropic route was publishing an
  OpenAI-compatible route without asking for it. A route that exposes a provider surface
  is now opted into.
- **`VLLM_ENDPOINTS` → `STRATOCLAVE_VLLM_ENDPOINTS`.** Old name read as a deprecated
  alias with a warning. No behaviour change beyond the name.
- **The OpenAI-compatible routes moved to the endpoint AWS recommends**
  (`bedrock-runtime`'s `/openai/v1`). Clients that hard-coded the previous host must
  update.
- **One object owns the end of a reservation.** The refactor that made a reservation
  reach exactly one ending changed internal seams that an out-of-tree patch may have
  depended on.

### Added

- **A hard dollar ceiling for strict-mode traffic**: the reservation is an upper bound on
  what the settle can charge, priced from a byte count rather than a token estimate, with
  the three states (`accounting`, `measured`, `shadow`, `enforced`) named and scoped per
  dimension. See [`hard-ceiling.md`](docs/design/hard-ceiling.md).
- **Unobserved provider outcomes are classified, recorded, and retained** rather than
  assumed free or assumed chargeable, with an admin surface to end a retention at the
  figure the provider's own record shows or release it when that record shows none.
- **A credit ledger with a Z3-checked no-double-post invariant** over the money
  transition model, an append-only event log, and at-most-once terminals keyed by sort
  key rather than by a mutable status.
- **Price identity**: reserve and settle price the same request with the same code over
  the same rate document, the rate version is frozen onto the reservation, and a rate
  that cannot be frozen refuses the request before the provider is called. Billable legs
  are declared in one registry, so adding a leg to the charge without adding it to the
  estimate cannot be done silently.
- **A replayable Savings Certificate**: a published money figure is reproducible from the
  artifact itself, months later, across a rate change.
- **A semantic-router seam** (`served_by="semantic-router"`, ships dark): a virtual pool
  entry names a router pool rather than a concrete model and is never the model of
  record; at settle the model that actually executed is normalised from router-replay
  evidence and charged at the ledger's snapshot price. With offline reconciliation
  joining the router's advice to billed usage.
- **A vLLM transport seam** (`served_by="vllm"`) with an operator endpoint allowlist, so
  a self-hosted GPU is a registry entry rather than a new transport.
- **A limits registry**, closing on the admission side what the billable-legs registry
  closed on the pricing side: the limit kinds are declared in one place and a test fails
  both when a declared kind has no builder and when a builder exists with no declaration.
- **A claim registry over the documents**: every guarantee-shaped sentence in the six
  covered documents is registered with the reason it is allowed to say that, and the
  reason is checked mechanically. See `contracts/claims/`.
- **Local operation against real Bedrock** on one machine, per-request timing telemetry
  that can be switched off, and load-based backend scaling in the CDK.
- **The model registry served over both transports**, with Opus 5, Fable 5, GPT-5.6,
  Grok, and Gemma entries plus token-limit controls and one-line installers.

### Fixed

- **The per-user token counter and the per-model quota no longer leak on a crash.** The
  admission transaction debits up to three counters and the hold row recorded only the
  pool amount, so a crash between reserve and settle left the per-user `credit_used`
  debited permanently — it is not period-scoped and has no TTL, so leaked debits
  accumulated until the user was locked out — and the per-model quota inflated until its
  period TTL. The hold now carries what is needed to reverse both, and the reaper's
  reclaim gives them back in the same transaction as the pool restore.
- **Retention now requires evidence that something left this process.** The exception
  classifier ends in the expensive state rather than in a guess, and the departure marker
  used to be written *because* of that classification — so an exception raised by this
  gateway's own code before the call was indistinguishable from a read timeout on a
  completed generation, and retention held a tenant's budget for a request that never
  left. Each route now announces the hand-off immediately before invoking the provider
  client, and retention requires that fact.
- **A settle that exhausted its retries no longer loses the usage it observed.** It
  records an owed-settle row and the reclaim honours it, so the ledger stops asserting a
  settled delta of zero for a request that was charged.
- **An idempotency record's presence now distinguishes "the debit committed" from "an
  attempt began"**, so a refused authorization no longer replays as authorized.
- **A charged authorization no longer 404s at its own client** in the window between the
  hold being deleted and the reserve projection landing.
- **A reservation priced below the charge is not a reservation**: the estimate priced
  three legs where settle charged four, and the missing one costs more than fresh input.
- **A refund decision is made from how far the request got**, rather than refunding on any
  exception.
- **Wire-protocol fixes**: a phantom text block in the stream prologue and a duplicated
  `content_block_stop` in the epilogue.
- **Jurisdiction-filtered default failover** for streaming, and region configuration
  extracted so a residency requirement is expressible.

### Security

- **A token claim grants nothing.** Authority comes from the store this gateway controls;
  the authorization path does not evaluate `cognito:groups` at all, checked statically so
  no branch can start honouring it by accident.
- **No AWS credential is presented to this gateway on the vouch path.** The CLI signs a
  `GetCallerIdentity` request locally and the backend forwards the signature; the request
  model has nowhere a credential could arrive.
- **A reservation HMAC** on the router seam, with a canary and a circuit breaker.

### Known gaps

Named here because they are the honest state, and in full at the bottom of
[`CONTRACTS.md`](docs/design/CONTRACTS.md).

- **Nothing drains the retention queue but a human.** The exposure is now reported and
  alarmed (saturation on the fraction of a pool that retentions hold, staleness on the
  oldest unresolved one), so a provider outage filling a tenant's headroom is visible
  rather than arriving as a refusal. Ending a retention is still manual by design:
  `charge-loss.md` section 7 requires a capped write-off budget before anything releases
  an unobserved hold automatically, and that budget does not exist.
- **The ledger's hot path pays for its atomicity.** The admission transaction is a
  multi-item `TransactWriteItems` on a hot row; single-item conditional updates and
  pool-row sharding are designed and not built.
- **The admin resolution of a retained hold reverses the counters on release but not on
  settle**, so settling a retention below its reserved amount leaves the difference on
  the token and quota counters.

## [0.2.0] — 2026-07-12

Tagged release. No changelog entry was written at the time; see the tag annotation and
the commits in `v0.1.0..v0.2.0`.

## [0.1.0] — 2026-04-26

First tagged release. See the tag annotation.

[1.1.0]: https://github.com/littlemex/stratoclave/releases/tag/v1.1.0
[1.0.0]: https://github.com/littlemex/stratoclave/releases/tag/v1.0.0
[0.2.0]: https://github.com/littlemex/stratoclave/releases/tag/v0.2.0
[0.1.0]: https://github.com/littlemex/stratoclave/releases/tag/v0.1.0
