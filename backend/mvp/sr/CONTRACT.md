# vLLM Semantic Router — integration contract (verified from upstream)

Source of truth for the SR adapter (stage 2) and the observability join (stage 3).
Facts below are taken from the upstream project (github.com/vllm-project/semantic-router,
vllm-sr.ai) as of this migration; re-verify at each SR version bump.

## What SR is — an EXECUTING gateway (decisive fact)

- SR is an **executing gateway**, not a decision service. Per the upstream API
  docs (vllm-sr.ai/docs/api/router): "The router is the data-plane HTTP surface",
  `/v1/chat/completions` is "the main router ingress for routed inference". There
  is **no decision-only endpoint** (classify/route/dry-run — 記載なし). Sending a
  request to SR **starts a billable inference** on SR's own backend pool.
- Consequence: "Stratoclave asks SR which model, then executes itself" is
  IMPOSSIBLE. The money gate cannot come *after* asking SR. So Stratoclave sits
  in FRONT and reserves BEFORE forwarding — SR is a `served_by="semantic-router"`
  execute backend (Fable pivot decision, option B). Order:
  **reserve(candidate-set pool-max) → forward(SR decides+executes) → settle
  (replay evidence)**. "If you can't extract the decision before execution, put
  the money gate before the decision."
- A routing layer that exposes **one OpenAI-compatible endpoint** and dispatches
  each request to the best model pool ("The app calls one model. The router
  builds the team.").
- Classifies with **16 signal families** (Domain, PII, Jailbreak, Preference,
  Embedding, Complexity, History, Tool use, …) — heuristic + learned detectors.
- **CPU-only**, downloads ~1.5GB of BERT models once. `vllm-sr serve` brings up
  the OpenAI-compatible listener. Deployable as Envoy ExtProc / K8s Operator /
  local sidecar / Gateway API — "Same router, any infrastructure".
- Session-aware routing (the vLLM "SAAR" blog, 2026-06-02) is a routing-policy
  layer inside SR ("SAAR adds a session-control layer around that result").

## Config keys (config/config.yaml)

- `providers.models[].backend_refs[]`: real backends — `{name, endpoint,
  protocol, provider, api_key_env|api_key, auth_header, base_url}`.
- `providers.defaults.default_model`, `providers.models[].{name,
  provider_model_id, api_format}`.
- `routing.modelCards[]`: `{name, param_size, capabilities:[chat,reasoning,tools]}`.
- `global.router.auto_model_names`: logical names clients send — e.g.
  `vllm-sr/auto`, `auto`. `external_model_ids.openai` = external-facing name.
- `global.services.observability.tracing`: `{enabled, provider: opentelemetry,
  exporter: {type: otlp, endpoint}}`.
- `router_replay`: `{store_backend: postgres, postgres: {...}}`.
- Ports: listener `listeners[0].port: 8899` (docs quickstart also cites 8888),
  management API `management_api.port: 8080`.

## Observability outputs (stage-3 receptacle)

- Response header **`x-vsr-replay-id`** — "operators can jump to the exact
  routing record".
- **Router replay** record per request: signals, model selection (chosen +
  candidates not taken), token usage, cost — "Replay records decision metadata
  and usage/cost — summaries for browsing, detail on demand".
- **OpenTelemetry** spans exported via OTLP.

## Stratoclave integration invariants (Fable pivot, option B)

- **Reserve before forward. Stratoclave accounts.** Because SR executes,
  Stratoclave reserves FIRST — at the **candidate-set pool-max** unit price
  (`max_{m ∈ tenant_allowlist ∩ SR_backend_pool} unit_price(m) × (est_input +
  max_tokens_cap)`) — then forwards. Over-reserve errs fail-closed and is refunded
  at settle. The atomic reserve→settle ledger is the sole charge-of-record and
  the sole gate on spend.
- **`max_tokens` is force-injected** into the forwarded request equal to the cap
  used in the reserve, so reserve ≥ real cost holds identically.
- **Routing fail-open, money fail-closed.** SR down/slow ⇒ fall back to the
  default path (direct Bedrock); the default model ∈ candidate set so the pool-max
  reserve already covers it. No path reaches SR without a reservation token
  (enforced three ways: code requires the token by type; SR ingress is
  mTLS/service-token only, never public; backend_refs provider keys live ONLY on
  SR so nothing spends them except via Stratoclave).
- **charge-of-record = Stratoclave ledger unit price × replay-measured tokens.**
  SR's usage/cost are **evidence** joined by span_id ↔ `x-vsr-replay-id`, never
  the charge. settle is two-phase: provisional from the OpenAI-compatible
  response `model`/`usage`; final async from router replay. Replay missing within
  window T ⇒ settle at the reserve amount (fail-closed) + alert. The number shown
  to customers is always the ledger's.
- **backend_refs ↔ registry is a CI gate**: every SR `backend_ref` must have a
  Stratoclave registry unit-price entry or deploy is rejected; pool-max is
  precomputed from this mapping. An unknown model in replay ⇒ settle at reserve +
  quarantine that backend + P1 alert.
- Integration point: `_pipeline.py:1343` `served_by` seam gains a
  `"semantic-router"` sibling; `decision_log.build_decision_item(vsr=...)` (keyed
  by run_id, span_id) is the observability receptacle, extended to carry the SR
  replay id + signals + the SR-vs-ledger cost divergence.
