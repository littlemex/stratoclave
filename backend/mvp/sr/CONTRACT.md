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

### Preconditions before turning A' on (must pass)

- **`/api/v1/eval` hot-path fitness.** It is a management-API endpoint that looks
  built for the CLI. Confirm with upstream that it is committed as a per-request
  production decision API (rate, latency, backward-compat). BERT on CPU should be
  tens of ms; measure live p99 and set an adapter deadline (~300ms) after which
  `decide()` returns `NO_DECISION` (fail-open to the normal resolver).
- **Decision→registry surjection is a CI gate.** The `routing_decision` /
  `decision_name` namespace SR returns must map onto Stratoclave registry
  `model_id`s. An unmapped decision ⇒ deploy rejected (reuse of the existing
  pricing CI gate). At runtime an unmapped decision ⇒ `NO_DECISION` + alert.
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
| `adapter.py` (sr_mode tri-state, kill-switch, `decide()`) | **ACTIVE** — `decide()` gains the eval client. |
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

## Unfreeze condition (explicit)

Reopen the option-B forward path ONLY when there is measured demand for a model
that exists solely behind SR's pool and not in Stratoclave's own transport. Until
then B stays dark at `sr_is_servable() == False`.
