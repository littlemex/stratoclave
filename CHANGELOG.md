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
  in a minor or patch release. Lowering one is a major version.
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

- **Nothing watches `held_microusd`.** With retention on by default, a provider outage
  accumulates retentions against a tenant's headroom and the first signal an operator
  gets is a refusal. Retention is the correct behaviour — the money may really have been
  spent — but the exposure needs an alarm, and that alarm is the next piece of work.
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

[1.0.0]: https://github.com/littlemex/stratoclave/releases/tag/v1.0.0
[0.2.0]: https://github.com/littlemex/stratoclave/releases/tag/v0.2.0
[0.1.0]: https://github.com/littlemex/stratoclave/releases/tag/v0.1.0
