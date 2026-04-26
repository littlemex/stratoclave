<div align="center">

# Stratoclave

**A tenant-aware proxy gateway for Amazon Bedrock**

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](./LICENSE)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#project-status)
<!-- [![CI](https://github.com/littlemex/stratoclave/actions/workflows/ci.yml/badge.svg)](https://github.com/littlemex/stratoclave/actions) -->

</div>

---

## What is Stratoclave?

Stratoclave sits in front of Amazon Bedrock and turns it into a **multi-tenant,
credit-governed inference gateway**. It unifies authentication (Amazon Cognito
or AWS SSO), enforces per-tenant and per-user credit quotas, emits audit logs,
and exposes an Anthropic `Messages API`-compatible endpoint that drop-in
replaces `ANTHROPIC_BASE_URL` for Claude SDKs, Claude Code, and Claude
Desktop Cowork.

Direct Bedrock access via IAM gives you *everything or nothing*. Stratoclave
adds the missing layer: **who used which model, how much, and under whose
budget** — without forcing every caller to juggle IAM credentials.

```
    Clients                Stratoclave                  Amazon Bedrock
  ┌─────────┐            ┌─────────────────┐           ┌─────────────┐
  │ CLI     │            │  AuthN (Cognito │           │             │
  │ Web UI  │── HTTPS ──▶│         / STS)  │── IAM ──▶ │  Claude     │
  │ SDKs    │            │  RBAC           │           │  (family)   │
  └─────────┘            │  Credit quota   │           └─────────────┘
                         │  Audit log      │
                         └─────────────────┘
```

## Features

- **Two login paths.** Email + password via Amazon Cognito, *or* passwordless
  `aws sso login` via an STS-vouch flow (IAM user / federated role /
  `AWSReservedSSO_*` all recognized; EC2 instance profiles deny by default).
- **Tenants × users RBAC.** `admin`, `team_lead`, `user` roles with per-tenant
  default credits and per-user overrides. Team Leads see only their own
  tenants — other tenants don't appear to exist.
- **Anthropic API compatibility.** `POST /v1/messages` and `GET /v1/models`
  work with the Anthropic SDKs, Claude Code, and Claude Desktop Cowork by
  changing `ANTHROPIC_BASE_URL`.
- **Long-lived API keys.** `sk-stratoclave-...` keys for headless clients,
  with scope narrowing (`messages:send`, `usage:read-self`) and per-user
  active-key limits.
- **Usage visibility.** Per-call input/output tokens written to DynamoDB;
  filterable history per user / per tenant / globally. Audit events stream to
  CloudWatch Logs as structured JSON.
- **Deploys to your AWS account.** AWS CDK v2 (TypeScript) with ECS Fargate,
  DynamoDB, Cognito User Pool, ALB, and CloudFront — no external SaaS
  dependencies.

## Quick Start

> **Alpha status.** The end-to-end setup flow described below is being
> finalized on the `feature/draft-version` branch. Expect breakage on `main`.

### Deploy to your AWS account

```bash
export AWS_PROFILE=your-profile
export AWS_REGION=us-east-1
export STRATOCLAVE_PREFIX=stratoclave

cd iac
npm install
npx cdk deploy --all
```

Then bootstrap the first admin user and the default tenant:

```bash
./scripts/bootstrap-admin.sh --email admin@example.com
```

Copy the printed CloudFront URL — you'll hand it to CLI users below.

### Use Stratoclave (CLI)

```bash
# 1. Install the CLI from source (pre-built releases coming)
cd cli && cargo build --release

# 2. Bootstrap with the URL your admin gave you
stratoclave setup https://d111111abcdef8.cloudfront.net

# 3. Authenticate (choose one)
stratoclave auth login --email you@example.com        # Cognito password
stratoclave auth sso   --profile your-aws-sso-profile # AWS SSO

# 4. Call an Anthropic-compatible endpoint
stratoclave claude -- "Summarize this repository in one sentence"
```

### Use Stratoclave (Anthropic SDK)

```python
import anthropic

client = anthropic.Anthropic(
    base_url="https://d111111abcdef8.cloudfront.net",
    api_key="sk-stratoclave-xxxxxxxx...",  # issue via Web UI or CLI
)
resp = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}],
)
```

## Tech Stack

| Layer          | Stack                                                       |
|----------------|-------------------------------------------------------------|
| Backend        | FastAPI (Python 3.11), boto3, Pydantic v2, PyJWT            |
| Frontend       | Vite 5, React 18, TypeScript, Tailwind v3, shadcn/ui        |
| CLI            | Rust (clap derive), `aws-sdk-sts`, `aws-sigv4`, reqwest     |
| Infrastructure | AWS CDK v2 (TypeScript)                                     |
| Compute        | ECS Fargate in a minimal VPC                                |
| Storage        | DynamoDB (PAY_PER_REQUEST)                                  |
| AuthN          | Amazon Cognito User Pool (access tokens only)               |
| CDN / Edge     | Amazon CloudFront + SPA fallback via CloudFront Function    |
| Container      | `finch` or Docker                                           |

## Documentation

Detailed guides (installation, administration, CLI reference, architecture)
are being reworked and will ship under `docs/` in a subsequent release.
Meanwhile, see:

- [`CONTRIBUTING.md`](./CONTRIBUTING.md) — how to build, test, and submit changes
- [`SECURITY.md`](./SECURITY.md) — how to report vulnerabilities
- [`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md) — community expectations

## Project Status

Stratoclave is **alpha** software under active development. Interfaces,
database schemas, and IaC constructs may change without notice until we cut
a `v0.1.0` release. We welcome issues and pull requests — see
[`CONTRIBUTING.md`](./CONTRIBUTING.md).

## Security

Do **not** open a public issue for suspected vulnerabilities. See
[`SECURITY.md`](./SECURITY.md) for the private disclosure process.

## License

Licensed under the [Apache License, Version 2.0](./LICENSE).

## Acknowledgments

- [Amazon Bedrock](https://aws.amazon.com/bedrock/) — the upstream model runtime
- [HashiCorp Vault AWS auth method](https://developer.hashicorp.com/vault/docs/auth/aws) — the origin of the "vouch by STS" pattern used in SSO login
- [shadcn/ui](https://ui.shadcn.com/) — UI primitives for the Web console
- The broader AWS and Anthropic open-source ecosystems
