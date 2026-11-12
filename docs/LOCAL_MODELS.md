# Putting a local / self-hosted LLM behind the gateway

This is the local-mode walkthrough for `served_by="vllm"` — the hybrid-serving
transport seam that lets a registry entry route to any OpenAI-compatible
endpoint (vLLM, Ollama, llama.cpp's `llama-server`, ...) instead of Bedrock,
while still going through the same reserve → settle → ledger path. Read
[`docs/LOCAL.md`](./LOCAL.md) first; this file only covers what changes once
you also want a self-hosted model in the picture.

**The flag is off by default in every deployment**, including this one.
Nothing below changes that default — it shows how an operator opts in, on a
laptop, with the smallest model that proves the path.

## When to use this

- You want to see hybrid serving work end to end before deploying a real
  self-hosted GPU endpoint.
- You are testing a self-hosted pricing document
  (`STRATOCLAVE_SELFHOSTED_RATES_PATH`, see
  [`docs/design/price-feeds.md`](./design/price-feeds.md) Sec. 4) before
  pointing it at a real one.
- You hit something in `backend/mvp/serving/vllm.py` and want to reproduce it
  against a real OpenAI-compatible server rather than the in-memory fakes
  `backend/tests/test_hybrid_serving.py` uses.

## What this verified, concretely (2026-11-13, this machine)

A tiny model (`qwen2.5:0.5b`, 397 MB) running under Ollama on this Mac,
bound to `127.0.0.1` only, behind a local gateway with
`HYBRID_SERVING_ENABLED=true` and one `served_by="vllm"` registry entry:

| Route | Mode | Result |
|---|---|---|
| `POST /v1/messages` | `stream: true` | **Works.** Real reserve, real inference, real settle with the model's own token counts. |
| `POST /v1/messages` | `stream: false` | **Does not work today.** See "What does not work" below. |
| `POST /v1/chat/completions` | either | **Rejected before any reservation** (`400 invalid_model`), by design — see below. |
| Over-budget request | `stream: true` | `402`, before the local model is ever called. No charge. |
| Local server stopped mid-flight | `stream: true` | Clean in-band SSE error, no fabricated success — but the reservation is still charged (see below; this is not specific to hybrid serving). |

## Step by step

Export everything up front. `ollama` is installed with `brew install ollama`
(Homebrew may also upgrade its Python dependency and relink `python3` on the
`PATH` — check `python3 -m pip show boto3` afterwards and use a dedicated
virtualenv for the backend's own dependencies if it did).

```bash
export OLLAMA_HOST=127.0.0.1:11434
export OLLAMA_MODEL=qwen2.5:0.5b
export STRATOCLAVE_REPO=$(pwd)
```

Start Ollama bound to loopback only, and pull the smallest useful model:

```bash
nohup /opt/homebrew/opt/ollama/bin/ollama serve > /tmp/ollama-serve.log 2>&1 &
sleep 2
ollama pull $OLLAMA_MODEL
curl http://127.0.0.1:11434/api/tags
```

Confirm it speaks the OpenAI-compatible chat-completions shape and honours
`stream_options.include_usage` (the vLLM transport requires the usage chunk
to settle on real counts rather than the reservation):

```bash
curl -s http://127.0.0.1:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"$OLLAMA_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"say ok\"}],\"stream\":true,\"stream_options\":{\"include_usage\":true},\"max_tokens\":8}"
```

Add a `served_by="vllm"` entry. `backend/mvp/models.py` loads the registry
from an external JSON document (`STRATOCLAVE_MODEL_REGISTRY_PATH`, falling
back to the shipped `backend/mvp/defaults/models.json`), so the supported way
to add one is a document, not a code edit. `scripts/local/local_models.json`
in this repo is exactly that: the shipped registry plus one entry pointing at
Ollama. Two fields matter and are easy to get wrong:

- `bedrock_model_id` is the id **sent to the self-hosted endpoint verbatim**
  (`_converse_to_openai` in `backend/mvp/serving/vllm.py` puts it straight in
  the OpenAI `"model"` field). For Ollama this must be the exact tag
  (`qwen2.5:0.5b`), not an invented name — sending a name Ollama does not
  recognise is a `404` from Ollama, surfaced as `vllm 404`.
- `provider` must be `"anthropic"` for the entry to be reachable from
  `/v1/messages` today. This is not a hybrid-serving setting; it is a
  pre-existing restriction in `resolve_bedrock_model()`
  (`backend/mvp/models.py`), the legacy Claude-only resolver that route still
  uses to turn an alias into a model id. It filters on `provider`, not
  `served_by`, so a non-`"anthropic"` `served_by="vllm"` entry (which is what
  `backend/tests/test_hybrid_serving.py`'s own fixture uses) resolves fine
  through the router internals the unit tests exercise, but is invisible to
  this specific HTTP route. See "What does not work" below for the full
  picture of which routes can reach this seam at all.

Also add a self-hosted pricing document
(`STRATOCLAVE_SELFHOSTED_RATES_PATH`) keyed by `endpoint_key`, per
[`docs/design/price-feeds.md`](./design/price-feeds.md) Sec. 4.
`scripts/local/local_selfhosted_rates.json` has an illustrative rate for the
`ollama-local` endpoint key.

Credentials: if your `AWS_PROFILE` resolves through a `credential_process`
binary that only exists on the host (an internal SSO wrapper, Isengard,
etc.), the container cannot run it. Export static credentials first and let
the three `AWS_*` variables below carry them in — they are empty/no-op unless
set:

```bash
eval "$(aws configure export-credentials --profile $AWS_PROFILE --format env)"
```

Bring the gateway up with hybrid serving enabled. `make up` interpolates
`docker-compose.yml` from the process environment, but `finch compose`
(unlike a plain shell) does not forward the invoking shell's environment
into that interpolation — write a `.env` file next to `docker-compose.yml`
instead (gitignored; `make down` does not touch it, delete it yourself when
done):

```bash
cat > $STRATOCLAVE_REPO/.env <<EOF
AWS_PROFILE=$AWS_PROFILE
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY
AWS_SESSION_TOKEN=$AWS_SESSION_TOKEN
HYBRID_SERVING_ENABLED=true
STRATOCLAVE_VLLM_ENDPOINTS={"ollama-local": "http://host.docker.internal:11434"}
STRATOCLAVE_MODEL_REGISTRY_PATH=/app/local-dev/local_models.json
STRATOCLAVE_SELFHOSTED_RATES_PATH=/app/local-dev/local_selfhosted_rates.json
EOF
make up
```

`host.docker.internal` is how the gateway's container reaches a service
bound to `127.0.0.1` on the Mac — finch's VM boundary forwards it to the
host's loopback interface specifically, the same mechanism Docker Desktop
uses; nothing on your LAN can reach Ollama, because its own bind stays
`127.0.0.1` throughout. Verified directly against this machine's finch VM.

Send a request:

```bash
export SC_KEY=$(cat $STRATOCLAVE_REPO/data/local/api_key)
curl -N http://127.0.0.1:8080/v1/messages \
  -H "Authorization: Bearer $SC_KEY" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"local-ollama","max_tokens":16,"stream":true,"messages":[{"role":"user","content":"Reply with the single word: pong"}]}'
```

Expected output: an Anthropic-shaped SSE stream ending in `message_stop`,
with `pong` (or similar) in a `content_block_delta`. Read the ledger back —
`stratoclave-usage-logs` on DynamoDB Local records the model Ollama actually
reported (`qwen2.5:0.5b`, not the alias `local-ollama`) and the token counts
Ollama's own usage chunk carried, not the reservation estimate.

## What does not work today, and why

This is a structural property of where the router is wired in, not a config
knob — read it before assuming a flag or a registry field will unblock it.

**`POST /v1/messages` with `stream: false`.** The non-streaming branch of
`backend/mvp/anthropic.py` calls `_bedrock_client().converse(**kwargs)`
directly. Only the **streaming** branch
(`_stream_messages` → `mvp.routing.route_stream` → `infrarouter._attempt_invoke`)
goes through the router machinery that `backend/mvp/serving/vllm.py` is wired
into (its own module docstring: "reachable ONLY from
`infrarouter._attempt_invoke`"). Sending `stream: false` to a `served_by="vllm"`
model therefore reaches real Bedrock with the self-hosted entry's
`bedrock_model_id` as the literal Bedrock model id, and fails with a real
Bedrock `ValidationException: The provided model identifier is invalid`
(`502` to the caller). Reproduced on this machine.

**`POST /v1/chat/completions`, either mode.** `backend/mvp/chat_completions.py`
rejects any `served_by != "bedrock"` entry outright, pre-reservation
(`400 invalid_model`, no charge). Its own non-streaming path also calls
`deployment_client().converse(**kwargs)` directly rather than through the
router, so even lifting that check would not make it work without rewiring
the route's invocation path — out of scope for a config change.

**The custom self-hosted rate is not automatically the charged rate.**
`STRATOCLAVE_SELFHOSTED_RATES_PATH` feeds `SelfHostedFeed`
(`backend/mvp/pricing_feeds/selfhosted.py`), which resolves the right price
card when queried directly — verified in isolation on this machine. But that
feed only becomes the table the gateway actually charges from once
`STRATOCLAVE_PRICE_SOURCE=bedrock-live` is active, and that activation is
validated at **startup**, across **every** `pricing_key` the whole registry
uses, not just `vllm`'s. Flipping it on with only this gateway's default
registry in place fails startup with "price source 'bedrock-live' has no
rate row for pricing_key(s) [...]" — reproduced on this machine. Making
`bedrock-live` the active source for real needs `python -m
mvp.pricing_feeds.fetch --apply` (needs `bedrock:ListFoundationModelAgreementOffers`
and `pricing:GetProducts`, plus a DynamoDB pricing-config table) to seed a
snapshot first — see [`docs/design/price-feeds.md`](./design/price-feeds.md)
Sec. 5. Until then, a servable `served_by="vllm"` entry is still rated
correctly — on the real, per-request token counts the self-hosted server
reported — just at the **bundled floor** for its `pricing_key`
(`backend/mvp/defaults/pricing.json`'s `"vllm"` row, $0.20/$0.20 per Mtok),
not your document's number.

**Stopping the local server charges the reservation, not zero.** When Ollama
is unreachable mid-request, the stream fails cleanly with an in-band SSE
error event (no fabricated success) — but `credit_used` still moves by the
full reservation. This is not a hybrid-serving defect: `backend/mvp/_budget_flow.py`
calls `hold.provider_call_starting()` immediately before dispatching the
attempt, for every transport, Bedrock included, and the whole codebase's
documented posture is "charge on ambiguous provider dispatch" rather than
risk recording a call that actually executed as free. A connection refusal
after that point is billed the same as a Bedrock network blip would be.

## What this does not cover

- No GPU in the deployed stack: Fargate has nothing to run vLLM on, so this
  only ever makes sense with a **reachable** endpoint — this machine's own
  Ollama, or a real GPU box on the other end of a VPC URL in
  `STRATOCLAVE_VLLM_ENDPOINTS`.
- Cache tokens are always zero for a self-hosted entry (vLLM/Ollama report no
  Bedrock-style cache split); `backend/mvp/pricing_feeds/selfhosted.py`
  publishes this as an explicit fact, not an omission.
- The self-hosted rate is **operator cost-recovery**, not a list price —
  nobody publishes one for capacity you own. See
  [`docs/design/price-feeds.md`](./design/price-feeds.md) Sec. 4 for how a
  real rate is supposed to be derived (measured occupancy), not guessed.
- Concurrency, multi-tenant contention, and anything `docs/LOCAL.md` already
  says a laptop run cannot speak to apply here unchanged.

## Cleanup

```bash
make down
pkill -f "ollama serve"
rm -f $STRATOCLAVE_REPO/.env
```

The pulled model stays on disk (`~/.ollama/models`, ~397 MB for
`qwen2.5:0.5b`) unless you also run `ollama rm $OLLAMA_MODEL`.
