<!-- Last updated: 2026-07-22 -->

# Gaps surfaced by the workshops

Capabilities the workshop scenarios try to exercise but the gateway does **not**
provide yet. Each `not-implemented` step in a scenario's `coverage.yaml` links
here, and the auto-generated [`COVERAGE.md`](COVERAGE.md) ranks these gaps by how
many scenarios hit them — so this file is a demand-driven implementation to-do
list, not a wishlist. Turning a gap `covered` is the point of the workshops.

This is scoped to gaps in the gateway's **mechanism** (what Stratoclave should
emit or provide). Measurement targets, dataset choice, and quality acceptance
bars stay the operator's responsibility and are marked `user-responsibility` in
coverage, not listed here.

## perf-token-timing

**Wanted:** per-request **TTFT** (time to first token) and **TPOT** (time per
output token), emitted as telemetry the way `ledger_transact_latency` already is.

**Today:** the streaming path (`backend/mvp/anthropic.py::_stream_messages`)
yields frames but timestamps neither the first token nor inter-token gaps. The
only latency the gateway emits is `ledger_transact_latency` — the billing write,
not token timing. A perf workshop therefore cannot report TTFT/TPOT; it can only
show the billing-write latency and name this gap.

**Smallest honest first step:** a token-timing hook on the stream generator that
records first-token wall-clock and an inter-token histogram onto the existing
span, behind the same telemetry seam. Load generation, SLO judgement, and
availability targets remain the operator's responsibility.

## quality-eval-tap

**Wanted:** an **eval tap** — an opt-in export of `(span_id, prompt, response)`
as JSONL — so an operator can feed real traffic into a scorer.

**Today:** the exact-match scorer in `scenarios/usage/small-team/run.py` runs
against a **checked-in** task set (it demonstrates the scoring *mechanism*). There
is no way to feed it from a team's real request/response traffic, because the
gateway does not emit prompt+response pairs by `span_id`.

**Smallest honest first step:** a per-tenant, opt-in tap that writes
`(span_id, prompt, response)` to a JSONL sink the operator controls (privacy is
theirs to gate). The scorer, the task set, and the acceptance bar stay the
operator's responsibility — the gateway provides the tap and the pure scoring
fold template, never the quality claim.
