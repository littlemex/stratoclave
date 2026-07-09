<!-- Last updated: 2026-07-10 -->
<!-- Applies to: Stratoclave main with `feature/openai-responses-proxy` (or later) -->

# Using Stratoclave with OpenAI Codex CLI

OpenAI's `codex` CLI runs long-form, agentic coding sessions against an
OpenAI Responses-compatible model. This guide shows how to point it at a
Stratoclave deployment so every call is authenticated, credit-accounted,
and audit-logged per user and per tenant, while the inference itself
continues to run on Amazon Bedrock (`bedrock-mantle`).

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

- A working Stratoclave deployment where `CODEX_ENABLED=true` is set on
  the ECS task (the default in `iac/bin/iac.ts`).
- `codex` CLI installed locally and able to reach the deployment's
  CloudFront URL over HTTPS. Test with
  `codex --version` (≥ 0.136.0 recommended).
- The Bedrock account behind your deployment must have model access
  enabled for the OpenAI families you intend to call:
    - `openai.gpt-5.4` — us-west-2 only
    - `openai.gpt-5.5` — us-east-2 only
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
stratoclave codex --model openai.gpt-5.5 -- "Use 5.5 for this run"
```

What `stratoclave codex` does under the hood:

1. Mints an ephemeral `sk-stratoclave-*` key with **only** the
   `responses:send` scope, expiring in 30 minutes, marked `ephemeral=true`
   so it does not count against your 5-active-key cap.
2. Creates a temporary directory and writes a `config.toml` describing
   a `stratoclave` model provider that targets `<your>.cloudfront.net/openai/v1`.
3. Runs `codex` with `CODEX_HOME=<tempdir>` and
   `STRATOCLAVE_OPENAI_KEY=<plaintext>` in the child environment. The
   user's persistent `~/.codex/config.toml` is **never read** during
   this invocation.
4. On exit (success, failure, or `Ctrl-C`), revokes the ephemeral key
   via `DELETE /api/mvp/me/api-keys/by-key-id/{key_id}`. The 30-minute
   TTL is the safety net if revoke fails.

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
model = "openai.gpt-5.4"

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
model_context_window = 400000

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
codex --model openai.gpt-5.5 exec "Use 5.5 once"
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
model = "openai.gpt-5.4"

[model_providers.amazon-bedrock.aws]
region = "us-west-2"
profile = "your-aws-profile"     # uses AWS SDK credential chain
```

Or with a Bedrock API key (`~/.codex/.env`):

```sh
export AWS_BEARER_TOKEN_BEDROCK=<your-bedrock-api-key>
export AWS_REGION=us-east-2
```

This works when your AWS principal already holds
`bedrock-mantle:CreateInference` and `bedrock-mantle:CallWithBearerToken`
on the appropriate project ARNs. **No tenant-level credit reservation
or audit happens — every dollar lands directly on the AWS bill, and
nothing shows up in Stratoclave's UsageLogs.**

If you need to attribute spend to users or enforce quotas, use Path A
or B instead.

## Verifying a successful call

After running codex through Stratoclave:

```bash
# Self usage summary (CLI). Should show an openai.gpt-5.4 row.
stratoclave usage show --since-days 1 --limit 5

# Or open the web console:
stratoclave ui open
# → "My usage" → "Tokens by model" includes openai.gpt-5.4
# → "API keys" → last_used_at on your key updates
```

The credit_used counter increments by `input_tokens + output_tokens`
of the actual usage. Reasoning traces (when `reasoning.effort = high`
or `xhigh`) are billed as part of `output_tokens`; the upfront
reservation already accounts for them via a multiplier (1× / 2× / 4× / 8×).

## Choosing a model and region

| Model            | Bedrock region    | Stratoclave aliases                  |
|------------------|-------------------|--------------------------------------|
| `openai.gpt-5.4` | `us-west-2`       | `gpt-5.4`, `openai.gpt-5.4`          |
| `openai.gpt-5.5` | `us-east-2`       | `gpt-5.5`, `openai.gpt-5.5`          |

The region is per-model, not per-deployment. The Stratoclave control
plane runs in us-east-1 and makes a cross-region HTTPS call to
`bedrock-mantle.{region}.api.aws/openai/v1/responses` for each
inference. To add a new model: append a `ModelEntry` to
`backend/mvp/models.py:_REGISTRY` and redeploy.

## Troubleshooting

**`HTTP 503 OpenAI Responses API is not enabled`**
— `CODEX_ENABLED` is not `"true"` on the ECS task. Check the env
on the running task definition; redeploy with `CODEX_ENABLED=true` in
`iac/bin/iac.ts`.

**`HTTP 403 Missing permission: responses:send`**
— Either your role does not carry the scope (check `backend/permissions.json`),
or the key you minted did not include it (check `--scope` on
`stratoclave api-key create`, or the Custom scopes textbox in the web
console).

**`HTTP 400 Tool type 'web_search' is not supported`**
— Bedrock's bedrock-mantle endpoint does not implement the `web_search`
tool. Add `web_search = "disabled"` at the top level of your codex
config. Path A injects this automatically.

**`HTTP 401 not authorized to perform: bedrock-mantle:CallWithBearerToken`**
— The ECS task role does not have the `AllowBedrockMantleBearerTokenMint`
IAM statement, or it is scoped too tightly. AWS does not currently
support resource-level conditions on this action; the policy must use
`Resource: "*"`. See `iac/lib/ecs-stack.ts`.

**`stream disconnected before completion`**
— Check the backend logs (`/ecs/stratoclave-backend` in CloudWatch) for
the `bedrock_mantle_stream_4xx_5xx` event; the sanitized error message
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
- Stratoclave's bedrock-mantle bearer token is minted per-request with
  a 15-minute TTL cap. The token lives only in the ECS task heap for
  the duration of one invocation.

## Internals (for the curious)

The codex client speaks the OpenAI Responses API
(`POST /v1/responses` with SSE streaming). Stratoclave terminates that
at `POST /openai/v1/responses` (in `backend/mvp/openai_responses.py`),
runs the same credit-reservation pipeline as `/v1/messages`
(`backend/mvp/_pipeline.py`), and forwards the body via `httpx` to
`bedrock-mantle.{region}.api.aws/openai/v1/responses`. The bearer
token is minted on demand by `aws-bedrock-token-generator.provide_token(
region=…, expiry=timedelta(seconds=900))` from the ECS task role.

The IAM trust path:

```
ECS task role
  → bedrock-mantle:CallWithBearerToken   (Resource: *, AWS constraint)
  → bedrock-mantle:CreateInference / Get* / List*
       (Resource: arn:aws:bedrock-mantle:{us-east-2,us-west-2}:<account>:project/*)
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
