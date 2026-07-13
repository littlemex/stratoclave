<div align="center">

# Stratoclave

**A tenant-aware credit gateway for Amazon Bedrock.**

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](./LICENSE)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#project-status)
[![Backend: Python 3.11](https://img.shields.io/badge/backend-Python_3.11-3776AB.svg)](./backend)
[![CLI: Rust](https://img.shields.io/badge/cli-Rust-dea584.svg)](./cli)
[![Infra: AWS CDK v2](https://img.shields.io/badge/infra-AWS_CDK_v2-FF9900.svg)](./iac)
[![API: Anthropic Messages](https://img.shields.io/badge/API-Anthropic_Messages-C1572F.svg)](#api-compatibility)
[![API: OpenAI Responses](https://img.shields.io/badge/API-OpenAI_Responses-412991.svg)](#api-compatibility)

</div>

---

## Overview

Stratoclave is a self-hosted **inference gateway** that sits in the data path
in front of Amazon Bedrock and adds the three things raw Bedrock does not give
you on its own: **who called which model, under whose budget, and through
which identity** — enforced *before* the model is invoked, on every single
request.

It exposes two independent inference routes: an Anthropic `Messages API`-
compatible endpoint at `/v1/messages` (for the Anthropic SDKs, Claude Code,
and Claude Desktop Cowork) and an OpenAI Responses API-compatible endpoint
at `/openai/v1/responses` (for the OpenAI SDK and the `codex` CLI, backed by
GPT-5.x on Amazon Bedrock via the bedrock-mantle service). Both routes
enforce per-user token quotas and optional per-tenant **dollar pool** budgets
with atomic DynamoDB reservations, record every call in an audit log, and
accept three orthogonal identity paths (Amazon Cognito password, AWS SSO via a
Vault-style STS vouch, and long-lived `sk-stratoclave-*` keys).

Stratoclave is deliberately AWS-native and small: a single region in your own
account, one FastAPI service on ECS Fargate, DynamoDB for all state, Cognito
for token issuance, and AWS CDK v2 for the entire topology. There is no
Postgres, no Redis, no external control plane, and no SaaS dependency.

## Why a gateway? (what a credential broker cannot do)

There are two ways to put Claude Code / codex on Bedrock in front of an
organization. A **credential broker** (such as the AWS *Guidance for Claude
Code with Amazon Bedrock*) hands each machine short-lived STS credentials and
lets the client call Bedrock **directly** — nothing sits in the data path.
Stratoclave takes the other route: it is a **gateway** that terminates every
inference call. That single architectural choice is what unlocks the
following, none of which a broker can offer because it has no request-time
choke point:

- **Real tenants, not just users.** `admin tenant create` provisions a tenant
  as a first-class object; users are assigned into it and can be moved between
  tenants atomically (`TransactWriteItems`). A broker has only the identity the
  IdP already emits — there is no "tenant" to create, budget, or reassign.
- **Budget enforced *per request*, before the call — not after.** Every call
  reserves `max_tokens + input_estimate` with a conditional DynamoDB write
  *before* Bedrock is invoked, and refunds the unused remainder afterwards.
  Concurrent requests that would overshoot a quota lose the conditional write
  and are rejected with `402`. A broker can only check a counter at credential
  **refresh** time (≈ every hour), and its usage numbers come from client-side
  telemetry the user can simply stop sending.
- **Both Claude and codex through one control plane.** The same identity,
  budget, and audit primitives cover the Anthropic Messages API *and* the
  OpenAI Responses API (GPT-5.x via bedrock-mantle). A Bedrock credential
  broker is Anthropic-only.
- **App-layer model / capability policy.** The model allowlist, and (on the
  roadmap) per-tenant reasoning-effort and tool caps, are enforced in the
  request handler. IAM can allow or deny a *model ARN*, but it cannot express
  "this tenant may not use `reasoning.effort = xhigh`".
- **A web console for non-engineers.** Tenants, users, credit, API keys and
  usage are managed from a React admin UI, not only a CLI.

The trade-off is honest and stated up front: a gateway is a component **you**
run and secure, it sits on the availability path of every call, and it sees
prompt text. See [Non-goals and honest limitations](#non-goals-and-honest-limitations)
and [Stratoclave vs. LiteLLM vs. a credential broker](#stratoclave-vs-litellm-vs-a-credential-broker)
for where a broker is the better choice.

## Highlights

- **Anthropic-compatible endpoint.** `POST /v1/messages` and `GET /v1/models`
  accept the same payloads as `api.anthropic.com`. Point `ANTHROPIC_BASE_URL`
  at your deployment and the Anthropic SDKs, Claude Code, and Claude Desktop
  work unchanged. Supports streaming, tool calling, vision (base64
  images), extended thinking, and prompt caching (`cache_control`).
- **OpenAI Chat Completions endpoint.** `POST /v1/chat/completions` accepts
  the same payloads as the OpenAI Chat Completions API — point
  `OPENAI_BASE_URL` at your deployment and use the OpenAI SDKs directly.
  Supports streaming, tool calling (including streaming `tool_calls`
  chunks), system messages, and `stop` sequences. Unsupported parameters —
  `n > 1`, `logprobs`, `response_format`, `image_url` content parts, and
  `parallel_tool_calls: false` — are rejected with an explicit 400 rather
  than silently dropped, so incompatible requests fail loudly instead of
  degrading quietly. For vision, use `/v1/messages` with base64 images.
  Both endpoints route to the same backend, so model behavior, limits, and
  credit accounting are identical regardless of which API shape you use.
  Auth: set your Stratoclave API key (`sk-stratoclave-*`) as
  `OPENAI_API_KEY`. Model names use Bedrock identifiers (e.g.
  `us.anthropic.claude-sonnet-4-6`); see `GET /v1/models` for the full
  list.
- **OpenAI Responses API endpoint.** `POST /openai/v1/responses` and
  `GET /openai/v1/models` accept OpenAI Responses-API payloads and forward
  them to GPT-5.x models on Amazon Bedrock via the bedrock-mantle service
  (GPT-5.4 → us-west-2, GPT-5.5 → us-east-2). The `stratoclave codex` CLI
  subcommand wraps the `codex` binary against this endpoint with an ephemeral
  key; the `--codex` flag on `stratoclave setup` patches `~/.codex/config.toml`
  for direct use. Controlled by the `CODEX_ENABLED` ECS env flag, which
  **defaults to `true`**; set `CODEX_ENABLED=false` to disable the OpenAI
  routes (they then return `503`).
- **Two-level credit governance, enforced pre-flight.** Every tenant has a
  default credit, every user can carry a per-user override, and every
  inference call — to `/v1/messages` (Anthropic), `/v1/chat/completions`
  (OpenAI Chat), or `/openai/v1/responses` (OpenAI Responses) — reserves
  tokens atomically with a conditional DynamoDB write
  *before* Bedrock is invoked (`backend/dynamo/user_tenants.py:reserve`).
  Unused credit is refunded from the real token counts on return. Because the
  reservation is a conditional `UpdateItem`, concurrent requests that would
  push a balance past its limit lose the condition and are rejected — quotas
  cannot be raced past.
- **Dollar pool budgets, priced per model.** A tenant can additionally carry a
  shared **dollar pool** for a billing period. Each request debits the caller's
  per-user tokens *and* reserves the request's cost in integer micro-USD from
  the pool in **one** `TransactWriteItems`, so neither ceiling can be raced
  past; a breach returns `402` with a `reason` distinguishing
  `personal_budget_exhausted` from `tenant_pool_exhausted`. Cost is derived from
  an admin-editable per-model price table (`PricingConfig`), so Opus and Haiku
  spend are counted differently — all in integer micro-USD, never floating
  point. A tenant with no pool row keeps the token-only behaviour unchanged
  (pools are opt-in per tenant/period).
- **Crash-resilient budget accounting.** A pooled reservation writes a sibling
  *hold* record in the same atomic write; settle and release delete it, and if
  a task is killed (OOM, deploy drain) between reserve and settle, a bounded,
  self-healing sweep on later requests reclaims the orphaned hold so a crash
  can never permanently strand pool budget — with no reaper process, timer, or
  any infrastructure beyond the single DynamoDB table.
- **Three role RBAC, tenant-scoped.** `admin`, `team_lead`, and `user` roles
  are normalized into DynamoDB from a versioned
  [`permissions.json`](./backend/permissions.json). Team leads see only the
  tenants they own; other tenants respond 404 even by direct URL.
- **Three identity paths, one backend.** Cognito email + password, passwordless
  AWS SSO (and saml2aws, and any AWS profile) through the Vouch-by-STS flow,
  and long-lived `sk-stratoclave-*` API keys with scope narrowing and
  per-user active-key caps.
- **Claude Desktop Cowork ready.** Cowork's Gateway mode discovers the model
  list via `/v1/models` and streams through `/v1/messages`. A CloudFront
  Function guards against the `/v1/v1/...` double-prefix pitfall.
- **CDK v2, one command.** A single `./scripts/deploy-all.sh` from
  [`iac/`](./iac) provisions the VPC (with Flow Logs), DynamoDB tables, ECR
  repository, ALB, **WAFv2 WebACL (CloudFront scope)**, CloudFront + S3
  frontend, Cognito User Pool, and the Fargate service in a single AWS
  region. `cdk-nag` runs at every synth so regressions in security posture
  fail the build.
- **Defense in depth at the edge.** CloudFront terminates TLS at 1.2_2021+.
  The SPA responses carry HSTS 730 d + preload and a strict CSP via a
  CloudFront `ResponseHeadersPolicy`; the API responses carry their own
  headers from the backend middleware (HSTS 365 d, `script-src 'none'`,
  `frame-ancestors 'none'`). A managed WAFv2 WebACL applies four rules by
  default — CommonRuleSet, KnownBadInputs, IpReputation, and a per-IP
  rate-limit rule (an optional IP-allowlist rule makes five). The S3 origin
  uses **Origin Access Control** with a bucket policy scoped by both
  `aws:SourceArn` and `aws:SourceAccount`, and the ALB security group only
  accepts the AWS-managed CloudFront origin-facing prefix list — direct ALB
  DNS probes fail at L4. The Fargate task's own egress is locked to TCP/443
  and UDP/53 (`allowAllOutbound: false`).
- **Auditable by construction.** Every privileged action is emitted as a
  structured JSON log to CloudWatch, keyed by the correlation ID the backend
  injects on ingress. Emails are redacted into stable SHA-256 markers so
  logs never leak PII.

## Architecture at a glance

<p align="center">
  <img src="./docs/diagrams/architecture.png" alt="Stratoclave architecture: clients to CloudFront to ALB to ECS Fargate, with DynamoDB, Cognito, Bedrock, STS, and CloudWatch Logs." width="100%">
</p>

The Stratoclave control plane lives inside a single AWS region (us-east-1)
in your own account. Clients reach CloudFront for TLS termination; static
paths hit S3, API paths hit an internal ALB fronting a single-task Fargate
service. The backend is stateless — all mutable state lives in DynamoDB,
authenticated by either a Cognito `access_token` or a `sk-stratoclave-*` API
key.

Inference fans out to two Bedrock surfaces. Anthropic Messages calls
(`/v1/messages`) invoke Bedrock `converse` / `converseStream` in us-east-1
against an inference-profile allowlist. OpenAI Responses calls
(`/openai/v1/responses`) are forwarded by httpx to the bedrock-mantle service
at `bedrock-mantle.{region}.api.aws/openai/v1`, where the region is per-model
(GPT-5.4 → us-west-2, GPT-5.5 → us-east-2). The cross-region bedrock-mantle
calls originate from the single Fargate task in us-east-1; no second
control-plane region is deployed.

For a detailed walkthrough of components, data model, and invariants, see
[`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md).

## Quick start

### Deploy to your AWS account

Prerequisites: AWS CLI with an administrator profile, Node.js 20 LTS, Docker,
and Bedrock model access enabled for the Claude family in your region.

```bash
# Clone
git clone https://github.com/littlemex/stratoclave.git
cd stratoclave

# Set your profile / region / deployment prefix. us-east-1 is enforced today
# (see "Regional constraints" in docs/DEPLOYMENT.md).
export AWS_PROFILE=your-admin-profile
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
export CDK_DEFAULT_REGION=us-east-1
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export STRATOCLAVE_PREFIX=stratoclave

# One-shot deploy: network (+ Flow Logs), DynamoDB, ECR, ALB, WAF,
# CloudFront (OAC), Cognito, Fargate. cdk-nag runs during synth.
cd iac
npm install
./scripts/deploy-all.sh
```

The script prints the CloudFront URL at the end — hand that URL to your CLI
users. The first admin user is seeded by a bootstrap script in
[`iac/scripts/`](./iac/scripts); see [`docs/DEPLOYMENT.md`](./docs/DEPLOYMENT.md)
for day-2 operations.

### Use it from the CLI

```bash
# Build the Rust CLI (pre-built releases will follow)
cd cli
cargo build --release
export PATH="$PWD/target/release:$PATH"

# Bootstrap config from /.well-known/stratoclave-config
stratoclave setup https://d111111abcdef8.cloudfront.net

# (Optional) Also patch ~/.codex/config.toml for direct codex use
stratoclave setup https://d111111abcdef8.cloudfront.net --codex

# Sign in (pick one path)
stratoclave auth login --email you@example.com               # Cognito password
stratoclave auth sso   --profile your-aws-sso-profile        # AWS SSO / saml2aws

# Run Claude Code through Stratoclave (claude-code must be installed separately)
stratoclave claude -- "Summarize this repository in one sentence"

# Run OpenAI codex through Stratoclave (codex must be installed separately).
# Mints a short-lived responses:send-only key; ~/.codex/config.toml is untouched.
stratoclave codex -- "Summarize this repository in one sentence"

# Open the web console in a pre-authenticated tab
stratoclave ui open
```

### Use it from the Anthropic SDK

```python
import anthropic

client = anthropic.Anthropic(
    base_url="https://d111111abcdef8.cloudfront.net",
    api_key="sk-stratoclave-xxxxxxxx...",  # issue via CLI or web console
)
resp = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}],
)
print(resp.content[0].text)
```

### Use it from the OpenAI SDK / codex

```python
import openai

client = openai.OpenAI(
    base_url="https://d111111abcdef8.cloudfront.net/openai/v1",
    api_key="sk-stratoclave-xxxxxxxx...",  # issue via CLI or web console
)
resp = client.responses.create(
    model="openai.gpt-5.4",
    input="Hello",
)
print(resp.output_text)
```

The `responses:send` scope is required; all three roles (`admin`, `team_lead`,
`user`) carry it by default. GPT-5.4 is served from us-west-2 and GPT-5.5
from us-east-2 via the bedrock-mantle service; both are gated by the
`CODEX_ENABLED` feature flag on the ECS task. See
[`docs/CODEX_GUIDE.md`](./docs/CODEX_GUIDE.md) for the full codex setup.

For a complete walkthrough including the web console, administrative
workflows, Cowork configuration, and codex setup, see
[`docs/GETTING_STARTED.md`](./docs/GETTING_STARTED.md).

## How it works

### Vouch by STS (passwordless login)

Stratoclave's SSO flow does not parse AWS credentials and never holds an IdP
refresh token. It is the same pattern
[HashiCorp Vault has used for a decade](https://developer.hashicorp.com/vault/docs/auth/aws)
in its AWS `iam` auth method: the client signs
`sts:GetCallerIdentity`, the backend replays the signed request to STS, and
the backend trusts only the `Arn` / `UserId` / `Account` that STS returns.

<p align="center">
  <img src="./docs/diagrams/vouch-by-sts.png" alt="Vouch-by-STS flow: CLI signs GetCallerIdentity, backend forwards to STS, backend provisions or resolves a Cognito user and issues an access_token." width="100%">
</p>

This is what makes the SSO path identity-provider agnostic. Anything that
populates `~/.aws/credentials` works the same way: `aws sso login`,
`saml2aws login`, Entra ID / Okta / ADFS SAML federation, a regular IAM user
with long-lived keys (default DENY unless explicitly allowed per trusted
account), and so on. EC2 instance profiles are rejected by default because
they cannot be attributed to a single human.

Because Stratoclave never stores an IdP refresh token, a full backend
compromise cannot pivot into the customer's IAM Identity Center or SAML IdP.
The worst-case blast radius is bounded to Stratoclave's own resources —
Bedrock overspend, DynamoDB tampering, impersonation within this deployment.
See [`SECURITY.md`](./SECURITY.md) and the *Security considerations* section
of [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) for the full threat
model.

### Credit reservation

Concurrent requests that would overshoot a quota cannot race. Both
inference routes share the same reservation pipeline (`backend/mvp/_pipeline.py`):
the request reserves `max_tokens + input_estimate` with a conditional
`UpdateItem` on `UserTenants`, invokes the upstream service (Bedrock
`converse` for `/v1/messages`, bedrock-mantle Responses for
`/openai/v1/responses`), then refunds the difference from the real token
counts. `UsageLogs` always records the actual spend, not the reservation.

The OpenAI Responses route applies a reasoning-effort multiplier to the
upfront reservation (1× / 2× / 4× / 8× for `low` / `medium` / `high` /
`xhigh`) because reasoning traces can emit far more output tokens than
`max_output_tokens` alone implies. The minimum reservation is 8 192 tokens
per request regardless of multiplier.

<p align="center">
  <img src="./docs/diagrams/credit-reservation.png" alt="Credit reservation flow: authenticate, conditional UpdateItem to reserve tokens, invoke Bedrock, refund the diff, write a UsageLogs row." width="100%">
</p>

The credit model, role matrix, and the underlying DynamoDB tables are
documented in [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) and
[`docs/ADMIN_GUIDE.md`](./docs/ADMIN_GUIDE.md).

### Dollar pool budgets and crash-safe accounting

When a tenant has a **dollar pool** for the current period, the same
reservation additionally reserves the request's cost — priced from the
per-model `PricingConfig` table — as integer **micro-USD** (1 USD =
1 000 000 micro-USD; never a float, and cost is rounded *up* so a request is
never under-charged). The per-user token debit and the pool debit are one
`TransactWriteItems`: either both commit or neither does, so under concurrency
a tenant can no more overshoot its dollar pool than a user can overshoot their
token balance. On settle, the reserved amount is released and the *actual*
cost — including cache-read/write tokens — is recorded to `pool_settled`.

Because a reservation is money held before the model call, a process killed
between reserve and settle (OOM, deploy drain) would otherwise strand that
amount forever. To prevent that **without adding any infrastructure**, each
reservation writes a sibling *hold* row in the same atomic write, carrying its
amount and an expiry encoded in the sort key. Settle and release delete the
hold in the same transaction that adjusts the pool. A killed request leaves its
hold behind; later pooled requests run a small, bounded **sweep** that reclaims
expired holds — decrement and delete in one conditional transaction, so a hold
is reclaimed at most once and the pool can never be double-credited or driven
negative, even when many requests sweep concurrently. There is no reaper
process, no timer, and no store beyond the single `TenantBudgets` table.

Set a pool with `stratoclave admin tenant pool-budget set` or the web console;
the `TenantBudgets` schema and the reclaim invariants are documented in
[`docs/ADMIN_GUIDE.md`](./docs/ADMIN_GUIDE.md) and
[`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md).

## API compatibility

| Endpoint                               | Behavior                                                            |
|----------------------------------------|---------------------------------------------------------------------|
| `POST /v1/messages`                    | Anthropic `Messages API` payload; translated to Bedrock `converse` / `converseStream`. Requires the `messages:send` scope. |
| `GET  /v1/models`                      | Returns the Claude-family inference profiles mapped by the backend. |
| `POST /openai/v1/responses`            | OpenAI Responses API payload; forwarded to `bedrock-mantle.{region}.api.aws/openai/v1`. Requires the `responses:send` scope. Gated by the `CODEX_ENABLED` ECS env flag. |
| `GET  /openai/v1/models`               | Returns OpenAI-family entries from the model registry, in the OpenAI `/v1/models` shape. Requires the `responses:send` scope. |
| `GET  /.well-known/stratoclave-config` | Unauthenticated discovery document; drives `stratoclave setup`.    |
| `POST /api/mvp/auth/sso-exchange`      | Vouch-by-STS entry point for CLI SSO login.                         |
| `/api/mvp/admin/*`                     | Admin and team-lead operations (user, tenant, credit, usage, trusted accounts, invites). |

The Claude family (via Bedrock `converse`) and the OpenAI family (via the
bedrock-mantle Responses API, currently `gpt-5.4` and `gpt-5.5`) are the
supported providers. Any model outside the explicit allowlist is rejected
with HTTP 400. The full registry lives in
[`backend/mvp/models.py`](./backend/mvp/models.py).

## Stratoclave vs. LiteLLM vs. a credential broker

Three different answers to "let my org use Claude/codex on Bedrock safely".
A **credential broker** (e.g. the AWS *Guidance for Claude Code with Amazon
Bedrock*) is not a proxy at all — it federates identities to short-lived STS
credentials and the client calls Bedrock directly. **LiteLLM** is a
general-purpose proxy across 100+ providers. **Stratoclave** is a focused,
AWS-native gateway that trades breadth for depth of per-tenant control.

| Dimension | Stratoclave | LiteLLM Proxy | AWS credential broker |
|---|---|---|---|
| Sits in the data path? | **Yes** (gateway) | **Yes** (gateway) | **No** (client → Bedrock direct) |
| Providers | Amazon Bedrock (Claude via `converse`; OpenAI GPT-5.x via bedrock-mantle) | 100+ (OpenAI, Anthropic, Bedrock, Vertex, Azure, Gemini, …) | Amazon Bedrock, Anthropic models only |
| Tenants as a managed object | **Yes** — create / assign / atomically reassign | Teams (global/team/user/key budgets) | **No** — only the IdP identity |
| Budget model | **Dollar pool + per-user tokens, reserved *pre-flight* in one atomic write; priced per model in micro-USD** | Per key/user/team `max_budget`, rpm/tpm | Per-user/team counter checked at **credential refresh** (~1 h) |
| Can a user race/bypass the budget? | No (single `TransactWriteItems` over both ceilings) | No (server-side) | Yes — usage is client-emitted telemetry; stop it and the counter stalls |
| Crash-safe budget accounting | **Yes** — a killed request's reservation self-heals via a bounded sweep; no leak, no reaper process | Relies on the proxy + its Postgres/Redis | N/A (no server-side reservation) |
| OpenAI codex on Bedrock | **Yes** (`stratoclave codex`, `responses:send` scope) | Via generic OpenAI routing | **No** |
| Model / capability policy | App-layer allowlist (+ per-tenant effort/tool caps on roadmap) | Per-key model list | IAM model-ARN allow/deny only |
| Identity | Cognito password, **Vouch-by-STS** (aws sso / saml2aws / IAM user), `sk-stratoclave-*` keys | Enterprise tier (Okta/Entra/OIDC/SAML) | **Native OIDC federation** (Okta/Entra/Auth0/Google/Cognito/IDC) |
| State / ops footprint | DynamoDB only (serverless), one Fargate task | Postgres required, Redis recommended | No data-path infra (just optional OTEL + quota Lambda) |
| Admin surface | **React web console** + CLI | Web UI + CLI | CLI (`ccwb`) |
| Fleet distribution (MDM, Claude Desktop) | Not built-in | Not built-in | **Yes** — MDM (Jamf/Intune/GPO), bootstrap server |
| Data residency / GovCloud | us-east-1 today | Wherever you host it | **US / EU / AU inference profiles, GovCloud** |
| Availability of inference | Depends on the gateway (single region today) | Depends on the proxy | **No added SPOF** (direct to Bedrock) |
| License | Apache 2.0 (all features OSS) | MIT + Commercial (SSO/audit are commercial) | MIT (AWS Solutions sample) |

**Pick Stratoclave** when you need tenant-scoped, pre-flight, per-request
budget enforcement that a user cannot bypass; when you want Claude *and* codex
under one control plane; or when you want a web console to run tenants and
keys. **Pick a credential broker** when you must distribute to a large fleet
via MDM, need GovCloud / EU data residency, or cannot accept any new component
on the inference availability path — and post-hoc audit (Bedrock invocation
logging + CloudTrail) is sufficient. **Pick LiteLLM** when you need one proxy
across many non-Bedrock providers or already operate Postgres.

## Documentation

| Document                                                        | For                                                          |
|-----------------------------------------------------------------|--------------------------------------------------------------|
| [`docs/GETTING_STARTED.md`](./docs/GETTING_STARTED.md)          | First run: install the CLI, sign in, make a call.            |
| [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md)                | Components, data model, auth flows, invariants.              |
| [`docs/DEPLOYMENT.md`](./docs/DEPLOYMENT.md)                    | CDK stacks, environment variables, day-2 operations.         |
| [`docs/ADMIN_GUIDE.md`](./docs/ADMIN_GUIDE.md)                  | Tenant / user / credit / trusted-account management.         |
| [`docs/CLI_GUIDE.md`](./docs/CLI_GUIDE.md)                      | `stratoclave` subcommand reference.                          |
| [`docs/CODEX_GUIDE.md`](./docs/CODEX_GUIDE.md)                  | OpenAI codex CLI setup via `stratoclave codex` and Path B (long-lived key). |
| [`docs/COWORK_INTEGRATION.md`](./docs/COWORK_INTEGRATION.md)    | Claude Desktop Cowork (Gateway mode) setup.                  |

Diagram sources are in [`docs/diagrams/`](./docs/diagrams) as both
`*.drawio` (editable in [diagrams.net](https://www.diagrams.net/)) and `*.png`.

## Security

Do **not** open a public issue for suspected vulnerabilities. Use the
private channels described in [`SECURITY.md`](./SECURITY.md) — preferably the
repository's **Security → Report a vulnerability** tab.

In short: Stratoclave's backend task role holds no `iam:*`, no
`sts:AssumeRole`, no `ec2:*`, and no S3 permissions beyond its own
deployment artifacts. It does not store IdP refresh tokens. A full backend
compromise is bounded to this deployment — Bedrock overspend, DynamoDB
tampering, impersonation within this User Pool — and does not reach the
customer's identity source or other AWS services.

Infrastructure-level hardening is enforced at synth time by `cdk-nag`
(CommonSolutionsChecks) and, at runtime, by:

- WAFv2 managed rules + per-IP rate limit on the CloudFront distribution,
- CloudFront OAC with a `aws:SourceArn`-scoped S3 bucket policy,
- ALB SG inbound restricted to the CloudFront origin-facing prefix list,
- DynamoDB `RETAIN` on audit-critical tables (`usage-logs`, `api-keys`); PITR
  is always on for `api-keys` and enabled for `usage-logs` in production,
- Vouch-by-STS replay defence via the `sso-nonces` TTL table,
- VPC Flow Logs (CloudWatch, 30 d),
- structured-log PII redaction (emails → SHA-256 markers),
- bedrock-mantle bearer tokens minted with a 15-minute TTL (capped in
  `openai_responses.py`); the token lives only in the ECS task heap for
  the duration of one request and is never persisted to DynamoDB or logs,
- a dedicated `responses:send` scope on `POST /openai/v1/responses` and
  `GET /openai/v1/models`; all three identity paths (Cognito, STS vouch,
  `sk-stratoclave-*`) must carry this scope to reach the OpenAI routes.

Vouch-by-STS replay is closed by the `sso-nonces` table: each signed
`GetCallerIdentity` request is consumed once (`attribute_not_exists(nonce)`,
10-minute TTL) and the check is fail-closed — if the nonce store is
unreachable the exchange returns `401` rather than trusting the request. See
the *Security considerations* section of
[`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) for the detailed attack model
and the residual risks that are explicitly called out (unauthenticated
availability of the SSO exchange endpoint, invite-policy edge cases).

## Non-goals and honest limitations

Stratoclave is opinionated, and the gateway model has real costs. What it
deliberately does **not** do today:

- **It is not a fleet-distribution tool.** There is no MDM integration and no
  managed Claude Desktop rollout. If your primary need is pushing Claude Code /
  Claude Desktop to thousands of managed laptops with per-device policy, a
  credential broker with MDM support fits that better; Stratoclave governs the
  *inference*, not the *endpoint fleet*.
- **Single region, single control plane.** Everything runs in `us-east-1`
  today. There is no GovCloud partition support and no EU/AU data-residency
  selection. Because the gateway is in the data path, its availability is the
  availability of your inference — a broker that lets clients call Bedrock
  directly has no such single point of failure.
- **The gateway sees prompt text.** Every request transits the FastAPI service,
  which is what makes pre-flight DLP and enforcement possible — but it also
  means the operator is in scope as a data processor. Weigh this for regulated
  workloads. (Note: full-fidelity *audit* does not require a proxy — Bedrock
  model invocation logging + CloudTrail give you that with a broker too. A
  proxy is for *intervention before the call*, not merely observation.)
- **Rate limiting is in-process by default.** The `slowapi` counters reset when
  an ECS task restarts and are per-task; for multi-task deployments set
  `STRATOCLAVE_RATELIMIT_STORAGE_URI` to a shared store. Credit reservation is
  *not* affected by this — it is always atomic in DynamoDB.
- **Alpha, single-maintainer, no external audit.** Treat it accordingly: pin a
  commit, run it in an account you control, and read the threat model in
  [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) before production use.

If these are dealbreakers, the honest recommendation is a credential broker
for distribution plus (if you need inline control) a supported commercial
gateway — and to treat Stratoclave's design as a reference for what that
control plane should enforce.

## Project status

Stratoclave is **alpha** software. Public HTTP surfaces, DynamoDB schemas,
and CDK construct props may still change between minor releases while on the
`0.x` series. Breaking changes are called out in the release notes for each
tagged version. Issues and pull requests are welcome; see
[`CONTRIBUTING.md`](./CONTRIBUTING.md).

## Contributing

- [`CONTRIBUTING.md`](./CONTRIBUTING.md) — build, test, and submit changes.
- [`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md) — community expectations.

The codebase is three languages and one IaC framework (Python FastAPI, Rust
CLI, TypeScript + React frontend, TypeScript CDK). Each component has a
README and can be developed and tested in isolation; the Vite dev server
proxies to the same ALB paths as the production deployment, so you rarely
need the full stack running locally.

## License

Licensed under the [Apache License, Version 2.0](./LICENSE). All features of
Stratoclave are part of the OSS distribution; there is no enterprise tier.

## Acknowledgments

- **[Amazon Bedrock](https://aws.amazon.com/bedrock/)** — the upstream model
  runtime that Stratoclave proxies.
- **[HashiCorp Vault AWS auth method](https://developer.hashicorp.com/vault/docs/auth/aws)**
  — the origin of the signed `GetCallerIdentity` pattern used by Stratoclave's
  Vouch-by-STS flow.
- **[LiteLLM](https://github.com/BerriAI/litellm)** — the gold standard for
  multi-provider LLM proxies and the reference point for Stratoclave's
  design trade-offs.
- **[AWS CDK](https://aws.amazon.com/cdk/)** — the IaC foundation that makes
  `./scripts/deploy-all.sh` possible.
- **[Anthropic SDKs and Claude Code](https://github.com/anthropics)** — the
  client surface Stratoclave is wire-compatible with.
- **[shadcn/ui](https://ui.shadcn.com/)** — primitives used by the web
  console.
