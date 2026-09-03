<!-- Last updated: 2026-09-03 -->
<!-- Applies to: Stratoclave main with `feature/openai-responses-proxy` (or later) -->

# Using Stratoclave with OpenAI Codex CLI

OpenAI's `codex` CLI runs long-form, agentic coding sessions against an
OpenAI Responses-compatible model. This guide shows how to point it at a
Stratoclave deployment so every call is authenticated, credit-accounted,
and audit-logged per user and per tenant, while the inference itself
continues to run on Amazon Bedrock (`bedrock-runtime`).

Codex is the OpenAI counterpart of Claude Code, and Stratoclave handles
both with the same primitives: a wrapper subcommand for ergonomic
ephemeral keys, plus a long-lived `sk-stratoclave-*` key path for
configurations that need to survive across runs (CI, remote workers).

## Contents

- [Prerequisites](#prerequisites)
- [Path A — `stratoclave codex` wrapper (recommended)](#path-a--stratoclave-codex-wrapper-recommended)
- [Path B — long-lived API key + your `~/.codex/config.toml`](#path-b--long-lived-api-key--your-codexconfigtoml)
- [Path C — direct Bedrock (no Stratoclave)](#path-c--direct-bedrock-no-stratoclave)
- [Verifying a successful call](#verifying-a-successful-call)
- [Choosing a model and region](#choosing-a-model-and-region)
- [Troubleshooting](#troubleshooting)
- [Security notes](#security-notes)
- [Internals (for the curious)](#internals-for-the-curious)

---

## Prerequisites

- A Stratoclave deployment where `STRATOCLAVE_CODEX_ENABLED` is not explicitly set to
  a falsy value. **This is the default**: with neither `STRATOCLAVE_CODEX_ENABLED` nor
  the deprecated `CODEX_ENABLED` present in the deploy environment, the stack
  synthesises `true`. The one reason to turn it off is residency — this route's model
  registry currently pins every OpenAI model to `us-east-2` regardless of the deploy
  region — in which case set `STRATOCLAVE_CODEX_ENABLED=false` at deploy time.
- `codex` CLI installed locally and able to reach the deployment's
  CloudFront URL over HTTPS. Test with
  `codex --version` (≥ 0.136.0 recommended).
- The Bedrock account behind your deployment must have model access
  enabled for the OpenAI families you intend to call, and **the deployment's own
  allowlist is the authority on which names it accepts** — ask it rather than
  trusting a list in a document, because these names turn over every few months:

  ```
  curl -sS -H "Authorization: Bearer $TOKEN" "$STRATOCLAVE_URL/openai/v1/models"
  ```

  A name outside that list is refused with `invalid_model`, and the refusal names the
  accepted alternatives.
- Your stratoclave user role must carry the `responses:send` scope.
  All three default roles (`admin`, `team_lead`, `user`) carry it
  out-of-the-box; check `backend/permissions.json` for the live
  permission table.

## Path A — `stratoclave codex` wrapper (recommended)

The wrapper handles key minting, config isolation, and revocation
automatically. **Use this path for interactive work.**

```bash
# One-time per shell or per machine
cd cli && cargo build --release
export PATH="$PWD/target/release:$PATH"

# Bootstrap your CLI config (idempotent; overwrites with backup)
stratoclave setup https://<your>.cloudfront.net

# Sign in once (token lasts ~1 h; re-run when expired)
stratoclave auth login --email you@example.com           # password
# or
stratoclave auth sso --profile your-aws-sso-profile      # SSO / saml2aws / IAM user

# Run codex through Stratoclave. Trailing args are passed through.
stratoclave codex -- exec --skip-git-repo-check "Explain this repo"
stratoclave codex -- "Open codex TUI through Stratoclave"

# Or pin a specific model instead of the deployment's default:
stratoclave codex --model "$CODEX_MODEL" -- exec --skip-git-repo-check "Explain this repo"
```

**`--model` is optional.** `codex`'s own built-in default is not a name this
gateway's allowlist has any reason to contain, so the wrapper never lets that default
reach codex: omitting `--model` makes it pass the deployment's advertised default
instead (`stratoclave setup` reads it from `.well-known/stratoclave-config` into
`~/.stratoclave/config.toml`'s `[defaults].codex_model`; re-run `setup` after the
deployment changes its default). A CLI that has never run `setup` against a
codex-enabled deployment falls back to a literal the CLI carries itself
(`openai.gpt-5.6-sol` as of this writing) — still a name the registry accepts, just not
necessarily the deployment's preferred one. Set `CODEX_MODEL` (or `--model` directly)
to a name the models endpoint above returned when you want something else.

What `stratoclave codex` does under the hood:

1. Mints an ephemeral `sk-stratoclave-*` key with **only** the
   `responses:send` scope, expiring in 30 minutes, marked `ephemeral=true`
   so it does not count against your 5-active-key cap.
2. Writes a `config.toml` describing a `stratoclave` model provider that targets
   `<your>.cloudfront.net/openai/v1` into the state directory it will use as
   `CODEX_HOME` (`~/.stratoclave/codex-state` by default — see below).
3. Runs `codex` with that `CODEX_HOME` and `STRATOCLAVE_OPENAI_KEY=<plaintext>`
   in the child environment. The user's persistent `~/.codex/config.toml` is
   **never read** during this invocation.
4. On exit (success, failure, or `Ctrl-C`), revokes the ephemeral key
   via `DELETE /api/mvp/me/api-keys/by-key-id/{key_id}`. The 30-minute
   TTL is the safety net if revoke fails.

### Session persistence and `codex resume`

`CODEX_HOME` holds two different things: the configuration codex reads, and the
state codex writes — `sessions/` (the rollout transcripts `codex resume` reads),
`history.jsonl`, `log/`, and the per-directory trust answers it records in
`config.toml`. Keeping your `~/.codex/config.toml` out of a proxied run must not
mean throwing the conversation away with it, so the directory is durable and
`config.toml` is the only file the wrapper owns there, rewritten per run. Trust
answers and any other codex-owned keys are carried across that rewrite. Setting
`CODEX_HOME` yourself has no effect: the wrapper owns that variable.

The default is `~/.stratoclave/codex-state`, never `~/.codex`, so proxied sessions
and direct-OpenAI sessions never show up in each other's `resume` picker. On Unix
the directory is restricted to `0700`, because rollouts contain the conversation;
if that cannot be applied the run stops rather than writing transcripts into a
directory whose permissions are unknown. On platforms without Unix permission
bits the wrapper says so instead of claiming a guarantee it cannot make.

```bash
# Resume the most recent proxied session.
stratoclave codex -- resume --last

# Keep state per project instead of one shared directory.
stratoclave codex --codex-state-dir ./.codex-state -- "continue where we left off"

# Keep nothing: temp CODEX_HOME, deleted on exit. `codex resume` cannot find it.
stratoclave codex --ephemeral-codex-state -- exec "one-off question"
```

`STRATOCLAVE_CODEX_STATE_DIR` sets the same directory by environment variable,
and `--ephemeral-codex-state` is rejected together with `--codex-state-dir`
rather than silently ignoring it.

Pointing the flag at your own `~/.codex` is refused: the wrapper rewrites
`config.toml` in whatever directory it is given, which would drop your custom
`model_providers`, your `model`, and every comment in the file. If you point it at
some other directory that already holds a codex config, that file is copied to
`config.toml.bak` first, and the run stops if the copy cannot be made.

One run at a time owns a state directory. `config.toml` carries the run's model
and its `x-sc-*` attribution headers and codex reads it at startup, so sharing it
could hand one run's billing group to another's child. A second concurrent run in
the same directory therefore keeps no state — it says so and falls back to a temp
`CODEX_HOME` — and a lock left by a crashed run is taken over after 24 hours. Give
concurrent work separate `--codex-state-dir` values to keep both resumable.

Sensitive env vars (`AWS_PROFILE`, `AWS_REGION`, `AWS_BEARER_TOKEN_BEDROCK`,
`CLAUDE_CODE_USE_BEDROCK`, `STRATOCLAVE_*`) are scrubbed before spawning
the child, so the codex agent — and anything it execs (MCP servers, tool
subprocesses) — cannot pivot back into your AWS or Cognito session.

## Path B — long-lived API key + your `~/.codex/config.toml`

Use this path for CI, remote agents, and any setup that must survive
across stratoclave login expiry.

### Step 1. Issue a long-lived key with the `responses:send` scope

**From the web console** (recommended for visual confirmation):

1. Open `https://<your>.cloudfront.net/me/api-keys`
2. Click **New key**
3. Set **Label** to something descriptive (e.g. `codex-on-laptop`)
4. **Custom scopes**: enter `responses:send`
5. **Lifetime**: pick the shortest acceptable for your use case
6. Click **Mint** and copy the plaintext key (shown once)

**Or from the CLI:**

```bash
stratoclave api-key create \
  --name "codex-on-laptop" \
  --scope responses:send \
  --expires-days 30
# Output includes: sk-stratoclave-XXXXXXXX...
```

### Step 2. Configure codex

`stratoclave setup --codex` will append the right block automatically
(with backup, and a prompt before changing your `model_provider`):

```bash
stratoclave setup https://<your>.cloudfront.net --codex
```

The result in `~/.codex/config.toml`:

```toml
model_provider = "stratoclave"
model = "openai.gpt-5.6-sol"

# Bedrock's OpenAI Responses endpoint does not implement the
# `web_search` tool today; codex must not send it as a tool type
# or every request returns a 400 validation_error.
web_search = "disabled"

# codex 0.136 walks up from `cwd` looking for a project-local
# `.codex/config.toml`. When the user is anywhere under $HOME
# the search reaches `~/.codex/config.toml` itself and emits
# "Ignored unsupported project-local config keys" for any
# `model_provider` / `model_providers` entries. An empty list
# short-circuits the walk so only this file loads.
project_root_markers = []

# codex's built-in model catalog does not list the GPT-5 family.
# Without an explicit context window codex warns "Model metadata for
# ... not found. Defaulting to fallback metadata" on every startup.
model_context_window = 200000

[model_providers.stratoclave]
name                   = "Stratoclave (OpenAI via Bedrock)"
base_url               = "https://<your>.cloudfront.net/openai/v1"
wire_api               = "responses"
env_key                = "STRATOCLAVE_OPENAI_KEY"
request_max_retries    = 3
stream_max_retries     = 5
stream_idle_timeout_ms = 600000
```

### Step 3. Export the key and run codex

```bash
export STRATOCLAVE_OPENAI_KEY="sk-stratoclave-XXXXXXXX..."
codex exec --skip-git-repo-check "Reply with: PONG"
codex                                            # interactive TUI
codex --model openai.gpt-5.6-terra exec "Use the other tier once"
```

### Step 4. Revoking when finished

From the web console: open `/me/api-keys` and click the trash icon on
the row. The key is invalidated immediately; the row drops out of the
ACTIVE list.

From the CLI:

```bash
stratoclave api-key revoke <key_hash>
```

`<key_hash>` is the SHA-256 hex digest of the plaintext key. Note that
`stratoclave api-key create` does **not** print the hash in its output
(it shows only `key_id`, scopes, and `expires_at`), and
`stratoclave api-key list` also does not expose `key_hash`. Until the
list output is enriched, use the web UI (**Account -> API keys -> Revoke**)
or call `DELETE /api/mvp/me/api-keys/{key_hash}` directly. See
[CLI_GUIDE.md -> Known limitations](CLI_GUIDE.md#known-limitations).

## Path C — direct Bedrock (no Stratoclave)

This is the upstream codex configuration documented by AWS and is
included here only to clarify the difference. **It bypasses
Stratoclave's auth, credit, and audit layers.**

`~/.codex/config.toml`:

```toml
model_provider = "amazon-bedrock"
model = "openai.gpt-5.6-sol"

[model_providers.amazon-bedrock.aws]
region = "us-east-2"
profile = "your-aws-profile"     # uses AWS SDK credential chain
```

Or with a Bedrock API key (`~/.codex/.env`):

```sh
export AWS_BEARER_TOKEN_BEDROCK=<your-bedrock-api-key>
export AWS_REGION=us-east-2
```

This works when your AWS principal already holds
`bedrock-runtime:CreateInference` and `bedrock-runtime:CallWithBearerToken`
on the appropriate project ARNs. **No tenant-level credit reservation
or audit happens — every dollar lands directly on the AWS bill, and
nothing shows up in Stratoclave's UsageLogs.**

If you need to attribute spend to users or enforce quotas, use Path A
or B instead.

## Verifying a successful call

After running codex through Stratoclave:

```bash
# Self usage summary (CLI). Should show an openai.gpt-5.6-sol row (or
# whichever model you passed via --model).
stratoclave usage show --since-days 1 --limit 5

# Or open the web console:
stratoclave ui open
# → "My usage" → "Tokens by model" includes openai.gpt-5.6-sol
# → "API keys" → last_used_at on your key updates
```

The credit_used counter increments by `input_tokens + output_tokens`
of the actual usage. Reasoning traces (when `reasoning.effort = high`
or `xhigh`) are billed as part of `output_tokens`; the upfront
reservation already accounts for them via a multiplier (1× / 2× / 4× / 8×).

## Choosing a model and region

| Model                  | Bedrock region | Stratoclave aliases                     |
|------------------------|-----------------|-----------------------------------------|
| `openai.gpt-5.6-sol`   | `us-east-2`     | `gpt-5.6-sol`, `openai.gpt-5.6-sol`     |
| `openai.gpt-5.6-terra` | `us-east-2`     | `gpt-5.6-terra`, `openai.gpt-5.6-terra` |

This table is a point-in-time snapshot; the model catalog turns over every few months
(the Prerequisites section above already says so once — this note is here so a reader
who jumped straight to this table sees it too). `GET /openai/v1/models` on your own
deployment is the authority.

The region is per-model, not per-deployment. The Stratoclave control
plane runs in us-east-1 and makes a cross-region HTTPS call to
`bedrock-runtime.{region}.amazonaws.com/openai/v1/responses` for each
inference. To add a new model: append a `ModelEntry` to
`backend/mvp/models.py:_REGISTRY` and redeploy.

## Troubleshooting

**`HTTP 503 OpenAI Responses API is not enabled`**
— `STRATOCLAVE_CODEX_ENABLED` was explicitly set to a falsy value on the ECS task
(codex defaults to enabled, so this does not happen on its own). Check the env on the
running task definition; redeploy with `STRATOCLAVE_CODEX_ENABLED=true` (or simply
unset) in `iac/bin/iac.ts`.

**`stratoclave codex` prints "ERROR: Reconnecting... 1/5" through "5/5" and then fails**
— This looks like a network problem but usually is not: codex logs every failed
provider call the same way, including a request the gateway refused outright (most
commonly `402 Payment Required` — the tenant's budget for the period is exhausted).
Once the retries are exhausted, codex prints the gateway's raw JSON refusal on the same
line, and `stratoclave codex` follows it with a `[STRATOCLAVE]` block naming which wall
refused, whether it can be raised, and the exact shortfall — read that line rather than
the "Reconnecting" ones above it. If it says `grantable=true`, ask an admin, or run
`stratoclave limit-raise request --limit-usd <amount> --reason <reason>` yourself if
your role permits it.

**`HTTP 403 Missing permission: responses:send`**
— Either your role does not carry the scope (check `backend/permissions.json`),
or the key you minted did not include it (check `--scope` on
`stratoclave api-key create`, or the Custom scopes textbox in the web
console).

**`HTTP 400 Tool type 'web_search' is not supported`**
— Bedrock's bedrock-runtime endpoint does not implement the `web_search`
tool. Add `web_search = "disabled"` at the top level of your codex
config. Path A injects this automatically.

**`HTTP 401 not authorized to perform: bedrock-runtime:CallWithBearerToken`**
— The ECS task role does not have the `AllowBedrockBearerTokenMint`
IAM statement, or it is scoped too tightly. AWS does not currently
support resource-level conditions on this action; the policy must use
`Resource: "*"`. See `iac/lib/ecs-stack.ts`.

**`stream disconnected before completion`**
— Check the backend logs (`/ecs/stratoclave-backend` in CloudWatch) for
the `openai_transport_stream_4xx_5xx` event; the sanitized error message
explains the upstream rejection.

**`codex` waits forever after "Reading additional input from stdin..."**
— `codex exec` in some environments waits on stdin even with a prompt
arg. Pipe in `</dev/null` or use a fully interactive terminal.

## Security notes

- The wrapper key minted by Path A holds only `responses:send`. It
  cannot list users, manage tenants, or reach `/v1/messages`.
  Compromising the codex child process bounds blast radius to the
  per-user credit budget over the 30-minute key lifetime.
- The Cognito bearer is **never** exported into the codex child
  environment. MCP servers and tool processes started by codex cannot
  read it via `/proc/<pid>/environ`.
- Long-lived keys (Path B) carry whatever scopes you grant, for as
  long as you choose. Default to `--expires-days 30` and `responses:send`
  only. Keys are stored as SHA-256 hashes; the plaintext is never
  written to DynamoDB or logs.
- Stratoclave's bedrock-runtime bearer token is minted per-request with
  a 15-minute TTL cap. The token lives only in the ECS task heap for
  the duration of one invocation.

## Internals (for the curious)

The codex client speaks the OpenAI Responses API
(`POST /v1/responses` with SSE streaming). Stratoclave terminates that
at `POST /openai/v1/responses` (in `backend/mvp/openai_responses.py`),
runs the same credit-reservation pipeline as `/v1/messages`
(`backend/mvp/_pipeline.py`), and forwards the body via `httpx` to
`bedrock-runtime.{region}.amazonaws.com/openai/v1/responses`. The bearer
token is minted on demand by `aws-bedrock-token-generator.provide_token(
region=…, expiry=timedelta(seconds=900))` from the ECS task role.

The IAM trust path:

```
ECS task role
  → bedrock-runtime:CallWithBearerToken   (Resource: *, AWS constraint)
  → bedrock-runtime:CreateInference / Get* / List*
       (Resource: arn:aws:bedrock-runtime:{us-east-2,us-west-2}:<account>:project/*)
```

Reasoning effort maps to a reservation multiplier:

| `reasoning.effort` | multiplier | typical use                  |
|--------------------|-----------|------------------------------|
| (none / `low`)     | 1×        | quick completions             |
| `medium`           | 2×        | default for codex             |
| `high`             | 4×        | analysis tasks                |
| `xhigh`            | 8×        | long-form planning            |

Minimum reservation per request is 8192 tokens regardless of multiplier.
Refunds reconcile against actual usage from the `response.completed`
event's `response.usage` block.
