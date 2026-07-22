# vLLM Semantic Router — integration contract (verified from upstream + live)

Source of truth for the SR adapter (decide layer) and the observability join.
Facts below are taken from the upstream project (github.com/vllm-project/semantic-router,
vllm-sr.ai) AND from a live run of vllm-sr v0.3.0; re-verify at each SR version bump.

## What SR exposes — TWO surfaces (corrected by live verification)

An earlier revision of this document asserted, from the API docs alone, that SR
has **no decision-only endpoint** and is therefore a pure executing gateway. **A
live run disproved that.** The `vllm-sr eval` CLI issues `POST /api/v1/eval`
against the management API (:8080), documented as "Evaluate a prompt/messages
against the router" — it returns the routing decision **without running
inference**. So SR has two distinct surfaces:

1. **Decide surface** — `POST /api/v1/eval` on the management API (:8080).
   Returns the routing decision (chosen model / decision name, the signals used,
   confidences) for a prompt/messages. Requires auth (401 without). **No billable
   inference is started.** This is the surface Stratoclave uses.
2. **Execute surface** — `/v1/chat/completions` on the data-plane listener
   (envoy ingress :8899), routed by the `x-selected-model` header. Sending a
   request here **starts a billable inference** on SR's own backend pool.

The 16 signal families (Domain, PII, Jailbreak, Preference, Embedding,
Complexity, History, Tool use, …) and the session-aware policy (the vLLM "SAAR"
blog, 2026-06-02 — "SAAR adds a session-control layer around that result") drive
the decide surface. SR is **CPU-only** (~1.5GB of BERT models downloaded once).

## Chosen architecture — A' (eval-decide, Stratoclave executes)

**"SR chooses, Stratoclave accounts — and Stratoclave also executes."**

Because a decision-only endpoint exists, Stratoclave does NOT need to put itself
in front of an executing SR and reserve at a whole-pool maximum. Instead:

    consult SR /api/v1/eval  →  RouteDecision (a single chosen model)
    →  reserve that ONE model at its exact registry price  (fail-closed)
    →  execute on Stratoclave's OWN transport (bedrock / self-hosted vLLM)
    →  settle from Stratoclave's OWN first-party usage

Why A' over the earlier option B (reserve pool-max → forward → replay-settle):

- **Money is far simpler and safer.** The reserve is the exact price of the
  single chosen model, not a pool-max over-reserve. Usage is first-party, not
  SR-reported. The whole "distrust SR's money" apparatus — pool-max reserve,
  two-phase settle, replay-evidence dependence, the 15-minute replay deadline,
  reservation HMAC, mTLS-only ingress — becomes UNNECESSARY. Fewer money failure
  modes, smaller attack surface.
- **Observability is synchronous.** The eval response (chosen model, used
  signals, confidences) is available in-request, so the decision_log join needs
  no async replay worker. Divergence becomes "SR-suggested model vs the model we
  actually billed", computed inline.
- **The only thing B does better is execute-breadth** (using providers that live
  only behind SR's pool). That is not worth trading away first-party money
  control. If a model exists only behind SR and is genuinely needed, the correct
  answer is to add that provider to Stratoclave's own `serving/` layer as a
  separate workstream — NOT to route money through SR's execution.

This is faithful to the original intent: the user always meant "delegate the
**judgement** to SR". Breadth splits into *decide breadth* (SR's model space + 16
signals, which we delegate) and *execute breadth* (transport, which we keep under
first-party money control). A' delegates the former and retains the latter.

### Implementation status (wired, gated off by default)

The A' decide path is IMPLEMENTED and wired into all three request handlers
(`anthropic.py`, `chat_completions.py`, `openai_responses.py`), gated off by
default so a default deploy is byte-identical:

- `mvp/sr/eval_client.py` — POSTs `{messages, evaluate_all_signals:true}` to the
  router's `/api/v1/eval`, parses the response (prefers `recommended_models[0]`,
  then `decision_result.decision_name`, then `routing_decision`; the ~300ms
  deadline + any error/timeout ⇒ `NO_DECISION`, fail-open).
- `mvp/sr/decision_map.py` — maps the returned decision to a registry model
  (explicit `SEMANTIC_ROUTER_DECISION_MAP` entry, else identity if already a
  registry model, else `NO_DECISION` + alert). The `validate_map_against_registry`
  helper is the CI gate (every mapped value must be priced+enabled).
- `adapter.decide()` returns a SOFT `prefer_model` fed into the SAME
  `saar_prefer_model` reserve input as a client pin — so it passes the SAME
  allowlist/servability enforcement and can never expand access or touch money.
- `adapter.sr_should_consult()` is the one gate: `sr_mode` off ⇒ never; active ⇒
  always; canary ⇒ the deterministic session-sticky slice, breaker permitting.

Flags default off: `SEMANTIC_ROUTER_ENABLED` (unused by the decide path directly),
`SEMANTIC_ROUTER_BASE_URL` (empty ⇒ no-op), per-tenant `sr_mode` default `off`,
plus `STRATOCLAVE_SR_FORCE_OFF` kill-switch.

**Live-verified (2026-07):** a real vLLM SR v0.3 router with a runnable config
(semantic_cache disabled to drop the Milvus dep) returns a 200 from
`/api/v1/eval` with `auth_mode: disabled` on the management port — e.g.
`{"decision_result":{"decision_name":"default-route"}, "recommended_models":["sim-default"], "routing_decision":"default-route", "metrics":{...18 signal families}}`.
The parser handles this exact shape. The earlier 401 was setup-mode (no runnable
config), NOT an auth requirement — so production auth is a NETWORK-boundary
concern (netpol on :8080), not a bearer token.

### Preconditions before turning A' on in production (must pass)

- **`/api/v1/eval` hot-path fitness.** It is a management-API endpoint that looks
  built for the CLI. Confirm with upstream that it is committed as a per-request
  production decision API (rate, latency, backward-compat). BERT on CPU should be
  tens of ms; measure live p99 and set an adapter deadline (~300ms) after which
  `decide()` returns `NO_DECISION` (fail-open to the normal resolver).
- **Management-plane exposure (Fable).** `/api/v1/eval` lives on the management
  API (:8080), which very likely also mutates router config. Putting it on the
  request hot path means: (a) the eval token MUST be scoped to eval-only, never a
  config-write token; (b) :8080 needs a network policy so only the money-path
  service can reach it; (c) the management API's own rate limits / redeploys now
  couple to every routing decision — budget and alarm for that coupling. "Upstream
  committed it as a production API" is necessary but not sufficient.
- **Pre-reserve eval consumption (Fable).** eval runs BEFORE the money gate, so a
  request that will fail to reserve (insufficient balance, etc.) still spends SR
  CPU + ships its prompt — a free amplification surface. Gate eval behind a cheap
  balance pre-check, or rate-limit eval per tenant, before enabling.
- **Prompt data-flow / PII (Fable).** A' sends the full prompt to a new component
  (SR) on every decided request. Record the data-flow change in the PII/data
  governance review before enabling; it is a new egress of customer content.
- **eval↔data-plane fidelity (Fable).** eval need not reproduce the ExtProc /
  data-plane pipeline exactly (esp. the SAAR session layer). The divergence metric
  (suggested vs billed) CANNOT catch this drift — there is no data-plane baseline
  to compare against. Require a per-SR-version eval regression test in CI.
- **Decision→registry surjection is a CI gate.** The `routing_decision` /
  `decision_name` namespace SR returns must map onto Stratoclave registry
  `model_id`s. An unmapped decision ⇒ deploy rejected (reuse of the existing
  pricing CI gate). At runtime an unmapped decision ⇒ `NO_DECISION` + alert.
- **NO_DECISION SLO (Fable).** fail-open is correct, but silent degradation is
  not: the timeout/error-driven NO_DECISION rate needs an SLO + alert, else
  routing quality rots invisibly. The A' circuit-breaker trip conditions are eval
  timeout/error rate and unmapped-decision rate (NOT the option-B out-of-snapshot
  / replay-miss vocabulary).
- **SAAR/session note.** Execute traffic does NOT flow through SR, so SR's
  internal session state does not grow from our traffic. As long as we send the
  conversation history in `messages`, the History signal still functions —
  confirm live.

## Money contract under A'

- **charge-of-record = Stratoclave ledger unit price × first-party measured
  tokens.** Same atomic reserve→settle ledger as every other served_by path; the
  SR decision only picks *which* single model to reserve+execute.
- **Routing fail-open, money fail-closed.** SR down / slow / garbage ⇒
  `NO_DECISION` ⇒ the normal resolver picks the model, reserve+execute proceed
  unchanged. No new money path is created by SR; the reserve gate is exactly
  today's.
- SR's self-reported cost (if surfaced via eval) is **evidence** joined into
  `decision_log.build_decision_item(vsr=...)`, never the charge.

## Config keys (config/config.yaml) — for the deploy/CI gate

- `providers.models[].backend_refs[]`, `providers.defaults.default_model`,
  `routing.modelCards[]`, `global.router.auto_model_names` (logical names clients
  send, e.g. `auto`), `external_model_ids.openai`.
- `global.services.observability.tracing` (otlp exporter), `router_replay`
  (postgres) — used only for evidence, not money.
- Ports: management API `management_api.port: 8080` (the eval/decide surface);
  data-plane listener `:8899` (the execute surface, which A' does NOT use).

## Frozen (option-B) assets — kept dark, do NOT delete

The option-B money apparatus is preserved but inert (`sr_is_servable()` returns
False; nothing on the hot path constructs it). It is frozen, not deleted, so a
future "execute-breadth" workstream can reopen it — but only after re-verifying,
because A' is the shipping path.

| Asset | State under A' |
|---|---|
| `port.py` (RouteDecision/RoutePort/NO_DECISION) | **ACTIVE** — the decide-layer type; eval → RouteDecision maps straight in. |
| `adapter.py` (sr_mode tri-state, kill-switch, `decide()`) | **ACTIVE + WIRED** — `decide()` consults `eval_client` and is called from all three handlers via `sr_should_consult`. |
| `canary.py` (deterministic sampling + per-process breaker) | **ACTIVE** — canary control is path-agnostic. |
| `observability.py` (SR-vs-ledger divergence) | **ACTIVE** — join source becomes eval signals; divergence = suggested vs billed. |
| pricing CI gate | **ACTIVE (repurposed)** — "eval decision space ↔ registry map". |
| `reservation.py` (PoolReservation/ConsumedProof/CandidatePool) | **FROZEN** — A' reserves a single model at exact price via the existing `reserve_credit_for_model`. |
| `settle.py` (two-phase SR settle) | **FROZEN** — first-party usage means the existing settle applies. |
| `hardening.py` (reservation HMAC) | **FROZEN** — no money-bearing forward to sign; eval auth is a normal service token. |
| `serving/semantic_router.py` `forward_to_sr` | **FROZEN** — only unfrozen if the Q4 execute-forward compromise is ever built. |

The P1/P2/P3 review findings on the frozen modules were **fixed before freezing**
(immutable reservation + thread-safe consume + partial-usage fail-closed settle +
proof↔request tenant/pool binding + honest per-process breaker docstring +
conservative max(input,output) pool pricing + length-prefixed HMAC framing), so a
future unfreeze inherits corrected code, not a "verified" label over latent bugs.

## Unfreeze condition (explicit, binding)

Reopen the option-B forward path ONLY when there is measured demand for a model
that exists solely behind SR's pool and not in Stratoclave's own transport. Until
then B stays dark at `sr_is_servable() == False`.

An unfreeze PR is REQUIRED to (not merely encouraged): (a) re-run the full P-level
money-path review on the modules being unfrozen; (b) re-run the live verification
and update the "live" facts in this document; (c) re-confirm every Precondition
above; (d) IMPLEMENT AND VERIFY the re-forward fence that does not yet exist —
add a reservation_id / idempotency-key field to `SrForwardRequest`, burn the
ConsumedProof on forward (a proof is single-MINT today but not single-FORWARD),
and verify SR-side dedupe upstream, because the ledger's (reservation_id, phase)
unique constraint stops double-CHARGE but not double-EXECUTION (the 2nd run's
provider cost lands outside the reserve). A "verified" label older than the freeze
does not carry over — the freeze snapshot's guarantees are void until
re-established.
