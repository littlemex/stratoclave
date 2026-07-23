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

**Already shown (gateway path, client-measured):** gateway-path TTFT/TPOT and the
**gateway overhead** vs a direct call are measured in
`scenarios/usage/small-team/live_gateway.py` (committed evidence:
`results/live-gateway-gw1.json` — gateway TTFT p50≈2384ms, paired overhead median
≈249ms, N=10). So the workshop does NOT lack a gateway perf number.

**The actual gap — the gateway EMITTING its own timing telemetry:** the above is
measured with a client stopwatch around `/v1/messages`. The gateway's streaming
path (`backend/mvp/anthropic.py::_stream_messages`) yields frames but timestamps
neither the first token nor inter-token gaps, so the gateway cannot emit its OWN
TTFT as telemetry (it emits only `ledger_transact_latency`, the billing write).
Attributing overhead from the gateway's own metrics — in production, without a
client harness in front — needs that hook.

**Smallest honest first step:** a token-timing hook on the stream generator that
records first-token wall-clock and an inter-token histogram onto the existing
span, behind the same telemetry seam as `ledger_transact_latency`. The client
baseline + gateway paired measurement already exist to validate it against. Load
generation, SLO judgement, and availability targets remain the operator's
responsibility.

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
