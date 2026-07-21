# vLLM Semantic Router — integration contract (verified from upstream)

Source of truth for the SR adapter (stage 2) and the observability join (stage 3).
Facts below are taken from the upstream project (github.com/vllm-project/semantic-router,
vllm-sr.ai) as of this migration; re-verify at each SR version bump.

## What SR is

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

## Stratoclave integration invariants (Fable design)

- **SR chooses, Stratoclave accounts.** SR only supplies a `RouteDecision`
  (mvp/sr/port.py). The atomic reserve→settle ledger is the sole charge-of-record
  and the sole gate on spend.
- **Routing fail-open, money fail-closed.** SR down/slow ⇒ tenant default model
  (never fail the request on routing); no execution path exists without a reserve.
- SR usage/cost + router replay are **evidence** joined to the ledger by span_id,
  never the charge. The number shown to customers is always the ledger's.
- Integration point: `_pipeline.py` `served_by` seam gains a `"semantic-router"`
  sibling; `decision_log.build_decision_item(vsr=...)` (keyed by run_id, span_id)
  is the observability receptacle, extended to carry the SR replay id + signals.
