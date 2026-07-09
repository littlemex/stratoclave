<!-- Last updated: 2026-07-10 -->
<!-- Applies to: Stratoclave main -->

# Architecture

Stratoclave is a **thin proxy gateway in front of Amazon Bedrock and the
OpenAI Responses API on Bedrock** that adds multi-tenancy, role-based access
control, credit quotas, and a unified login surface (Amazon Cognito or AWS
SSO). The control plane runs entirely inside a single AWS account and
region (us-east-1) without introducing non-AWS control-plane dependencies;
the optional OpenAI Responses path makes cross-region HTTPS calls to the
Bedrock-hosted bedrock-mantle endpoint (us-east-2 / us-west-2). The system
is deliberately composed of a small number of moving parts so that an
operator can understand the whole stack from this document alone.

This document describes the components, the data model, the authentication
and authorization flows, and the invariants Stratoclave relies on. It is
intended for operators, contributors reading the code for the first time, and
security reviewers. If you are looking for a step-by-step setup guide, see
[GETTING_STARTED.md](GETTING_STARTED.md); for deployment and day-2 operations,
see [DEPLOYMENT.md](DEPLOYMENT.md) and [ADMIN_GUIDE.md](ADMIN_GUIDE.md). The
repository lives at [`https://github.com/littlemex/stratoclave`](https://github.com/littlemex/stratoclave).

<!-- TODO(docs): Insert architecture diagram (hero image) here -->

## Table of contents

- [Design principles](#design-principles)
- [System diagram](#system-diagram)
- [Components](#components)
- [Authentication flows](#authentication-flows)
- [Authorization (RBAC)](#authorization-rbac)
- [Credit model](#credit-model)
- [Audit logging](#audit-logging)
- [Well-known configuration](#well-known-configuration)
- [Data model](#data-model)
- [Scaling considerations](#scaling-considerations)
- [Security considerations](#security-considerations)
- [Extension points](#extension-points)

---

## Design principles

1. **One region, one account, no SaaS.** Stratoclave deploys to a single AWS
   region inside your own account. There is no external control plane, no
   hosted metadata service, and no third-party control-plane dependency
   beyond the optional cross-region call to bedrock-mantle. Nothing third-party
   could become
   a single point of failure or data leak.
2. **Stateless backend.** The FastAPI container holds no per-user state
   beyond short-lived in-memory caches. Every piece of mutable state lives
   in DynamoDB and is updated with conditional writes. A single Fargate task
   is sufficient for correctness; multiple tasks scale horizontally without
   coordination.
3. **DynamoDB only for persistence.** No RDS, no Redis, no external queue.
   Credit accounting uses conditional `UpdateItem`. Seeding is idempotent
   (`attribute_not_exists` for one-shot inserts, version comparison for
   config-like tables).
4. **Cognito is for token issuance, DynamoDB is the source of truth.**
   Stratoclave *never* reads `cognito:groups` or relies on Cognito for
   authorization. The `Users` table is the authoritative record of a user's
   roles and tenant membership. Cognito is treated as a narrow token vendor.
5. **Vouch-by-STS for passwordless login.** The SSO flow uses the same
   pattern as HashiCorp Vault's AWS auth backend: the CLI signs a
   `sts:GetCallerIdentity` request, the backend forwards it to STS, and the
   backend trusts only the STS reply. Credentials are never transmitted to
   Stratoclave.
6. **Least privilege for the task role.** The ECS task role is scoped to
   only the specific DynamoDB tables, Cognito admin actions, and Bedrock
   inference profiles that the backend needs. It has no `iam:*`, no
   `ec2:*`, and no S3 access.

---

## System diagram

```
   Clients                          CloudFront (TLS termination, WAF-ready)
  ┌───────────┐                     ┌─────────────────────────────┐
  │ Web UI    │───── HTTPS ───────▶ │  /              → S3 (SPA)  │
  │ CLI       │                     │  /config.json   → S3        │
  │ SDKs      │                     │  /api/*         → ALB       │
  │ Cowork    │                     │  /v1/*          → ALB       │
  └───────────┘                     │  /.well-known/* → ALB       │
                                    └──────────┬──────────────────┘
                                               │
                     ┌─────────────────────────┴─────────────────────────┐
                     │                                                   │
                     ▼                                                   ▼
           ┌──────────────────┐                              ┌──────────────────────┐
           │  S3 (static SPA) │                              │  ALB (HTTP)          │
           │  Vite build      │                              └──────────┬───────────┘
           └──────────────────┘                                         │
                                                                        ▼
                                                         ┌──────────────────────────┐
                                                         │  ECS Fargate (public)    │
                                                         │  FastAPI backend         │
                                                         │   /api/mvp/*   RBAC      │
                                                         │   /v1/messages Bedrock   │
                                                         │   /v1/models             │
                                                         │   /openai/v1/responses   │
                                                         │   /openai/v1/models      │
                                                         │   /.well-known/...       │
                                                         │  Audit logger (JSON)     │
                                                         └─────┬───────────┬────────┘
                                                               │           │
                                               ┌───────────────┘           └──────────────┐
                                               ▼                                          ▼
                                 ┌──────────────────────────┐                ┌───────────────────────┐
                                 │  DynamoDB                │                │  Amazon Bedrock       │
                                 │   Users / Tenants        │                │   converse            │
                                 │   UserTenants (credit)   │                │   converseStream      │
                                 │   Permissions            │                │   inference profiles  │
                                 │   UsageLogs (TTL)        │                └───────────────────────┘
                                 │   ApiKeys                │
                                 │   TrustedAccounts        │
                                 │   SsoPreRegistrations    │
                                 │   SsoNonces (10-min TTL) │
                                 │   UiTickets (30-s TTL)   │
                                 └──────────────────────────┘

                                 ┌──────────────────────────┐
                                 │  Amazon Cognito          │
                                 │   User Pool              │
                                 │   Hosted UI + App Client │
                                 │   access_token issuer    │
                                 └──────────────────────────┘
```

The AWS SSO path adds one hop off-cluster. The CLI signs a call to the public
STS endpoint; the backend replays that signed call to STS and receives a
canonical `<Arn>/<UserId>/<Account>` tuple. No credentials ever cross the
backend's boundary.

```
  CLI     aws-sdk creds chain      Backend                 STS (global)         Cognito
   │                                  │                      │                    │
   │  sigv4-sign GetCallerIdentity    │                      │                    │
   │─────────────────────────────────▶│                      │                    │
   │  POST /api/mvp/auth/sso-exchange │                      │                    │
   │                                  │  forward signed req  │                    │
   │                                  │─────────────────────▶│                    │
   │                                  │  <Arn><UserId><Acc>  │                    │
   │                                  │◀─────────────────────│                    │
   │                                  │  Gate: trusted acct  │                    │
   │                                  │  Gate: role pattern  │                    │
   │                                  │  Gate: identity type │                    │
   │                                  │  Gate: invite policy │                    │
   │                                  │  admin_create_user / │                    │
   │                                  │  set_password /      │                    │
   │                                  │  admin_initiate_auth │                    │
   │                                  │─────────────────────────────────────────▶ │
   │                                  │  access_token                             │
   │                                  │◀───────────────────────────────────────── │
   │  access_token + user profile     │                      │                    │
   │◀─────────────────────────────────│                      │                    │
```

---

## Components

### Backend — FastAPI (Python)

**Path:** [`backend/`](../backend)

The backend is a single FastAPI application packaged as a container image and
run on ECS Fargate behind an internal-facing ALB. It is deliberately small:
most files under `backend/mvp/` are thin HTTP adapters over
`backend/dynamo/` repositories.

```
backend/
├── main.py                    # App factory, router wiring, lifespan seed
├── permissions.json           # Seed source of truth for Permissions table
├── bootstrap/
│   └── seed.py                # Idempotent Permissions + default tenant seed
├── core/
│   └── logging.py             # structlog + python-json-logger
├── middleware/
│   └── correlation.py         # X-Correlation-ID propagation
├── dynamo/                    # Repository layer (one module per table)
│   ├── users.py
│   ├── user_tenants.py        # Credit accounting with pessimistic reservation (reserve/refund)
│   ├── usage_logs.py
│   ├── tenants.py
│   ├── permissions.py
│   ├── api_keys.py            # SHA-256 stored, plaintext never persisted
│   ├── trusted_accounts.py
│   ├── sso_pre_registrations.py
│   ├── sso_nonces.py          # Vouch-by-STS replay-protection nonce store (10-min TTL, fail-closed)
│   └── ui_tickets.py          # Short-lived one-time CLI→SPA handoff tickets (30-s TTL)
└── mvp/                       # FastAPI routers + auth/authz helpers
    ├── deps.py                # JWT verification + API key path (Bearer and x-api-key headers)
    ├── authz.py               # has_permission, require_permission, audit log
    ├── anthropic.py           # POST /v1/messages, GET /v1/models
    ├── openai_responses.py    # POST /openai/v1/responses, GET /openai/v1/models
    ├── _pipeline.py           # Shared credit reservation + UsageLogs settle
    ├── _bedrock_clients.py    # Per-region boto3 / httpx Bedrock clients
    ├── models.py              # ModelEntry registry (Anthropic + OpenAI)
    ├── me.py                  # /api/mvp/me + usage summaries
    ├── admin_users.py
    ├── admin_tenants.py
    ├── admin_usage.py
    ├── admin_api_keys.py
    ├── admin_trusted_accounts.py
    ├── admin_sso_invites.py
    ├── team_lead.py
    ├── cognito_auth.py        # Email + password login
    ├── sso_sts.py             # Presigned URL validation + STS round-trip (nonce consume is inline)
    ├── sso_gate.py            # Identity-type classifier + allowlist gates
    ├── sso_exchange.py        # POST /api/mvp/auth/sso-exchange
    ├── me_api_keys.py         # Self-serve long-lived API key management
    ├── ui_ticket.py           # POST /api/mvp/auth/ui-ticket + /consume
    └── well_known.py          # GET /.well-known/stratoclave-config
```

**Responsibilities.**

- Validate inbound credentials (Cognito `access_token`, long-lived API key, or
  presigned STS request).
- Resolve each authenticated request to an `AuthenticatedUser` whose roles
  come from DynamoDB, never from Cognito groups.
- Enforce RBAC via `require_permission(...)` FastAPI dependencies.
- Translate Anthropic `Messages API` requests to Bedrock `converse` /
  `converseStream` calls, stream results back, and book the token usage.
- Decrement credit atomically and emit audit logs for privileged actions.
- Seed the `Permissions` table and default tenant at startup.

**Key dependencies.** `boto3` (DynamoDB, Bedrock, Cognito Identity Provider),
`PyJWT` with `PyJWKClient` for Cognito JWKS verification, `httpx` for the
STS vouch round-trip, `structlog` + `python-json-logger` for structured
logs.

**Why stateless.** All per-user data is in DynamoDB. Permissions are cached
for 10 seconds in-process to absorb hot paths, and the JWKS keys are cached
by `PyJWKClient`. A rolling deploy replaces tasks without draining anything
beyond in-flight HTTP requests.

### Frontend — Vite + React (TypeScript)

**Path:** [`frontend/`](../frontend)

A single-page application served from S3 through CloudFront. The SPA is a
static build; all configuration is fetched at runtime from `/config.json`
(the same 4 Cognito values that appear in the Cognito Hosted UI URL, plus
the CloudFront domain). No secrets are baked into the bundle.

```
frontend/src/
├── main.tsx                   # Entry + QueryClientProvider + ErrorProvider
├── App.tsx                    # Routing, AuthProvider gate
├── contexts/AuthContext.tsx   # access_token lifecycle (3 ingress paths)
├── lib/
│   ├── api.ts                 # Typed client + TanStack Query
│   ├── authFetch.ts           # Bearer injection + 401 soft-logout
│   ├── cognito.ts             # PKCE + refresh_token rotation
│   └── runtimeConfig.ts       # Fetches /config.json at startup
├── pages/                     # Login, Callback, Dashboard, Me, Admin, Team Lead
└── components/
    ├── layout/AppShell.tsx
    └── ui/                    # Primitives (shadcn/ui-style)
```

**AuthContext** accepts `access_token` through three ingress paths:
(1) a `?ui_ticket=<nonce>` URL parameter written by the CLI during
`stratoclave ui open` — the SPA exchanges the nonce for the real token bundle
via `POST /api/mvp/auth/ui-ticket/consume`, then strips the query parameter
from `window.location` immediately so the plaintext token never appears in the
URL bar or server access logs (session-fixation fix, P0-8 2026-04 review;
nonce has a 30-second TTL and is single-use),
(2) the Cognito Hosted UI callback with PKCE, and (3) a previously saved token
in `localStorage`. The backend enforces `token_use=access`, so the frontend
never passes `id_token`.

Vite's dev server proxies `/api/*`, `/v1/*`, and `/.well-known/*` to the
same ALB the frontend would hit in production, so local and production
behavior only differ in the CloudFront cache layer.

### CLI — Rust (`stratoclave`)

**Path:** [`cli/`](../cli)

A single static binary. It is stateless aside from `~/.stratoclave/config.toml`
(server URL and defaults) and `~/.stratoclave/mvp_tokens.json` (tokens,
mode `0600`).

```
cli/src/
├── main.rs                    # clap derive dispatch
├── mvp/
│   ├── auth.rs                # Cognito password login + NEW_PASSWORD_REQUIRED
│   ├── sso.rs                 # aws-sdk-sts presign → backend
│   ├── child_launcher.rs      # Shared spawner (env scrub + revoke lifecycle)
│   ├── ephemeral_key.rs       # Mint/revoke responses:send / messages:send keys
│   ├── claude_cmd.rs          # Wraps `claude` with ANTHROPIC_BASE_URL injected
│   ├── codex_cmd.rs           # Wraps `codex` with CODEX_HOME tempdir + STRATOCLAVE_OPENAI_KEY
│   ├── api.rs                 # reqwest client, error rendering
│   ├── tokens.rs              # ~/.stratoclave/mvp_tokens.json (0600)
│   ├── config.rs              # STRATOCLAVE_* env + config.toml
│   ├── admin.rs / team_lead.rs / usage.rs
│   └── ...
└── commands/
    ├── ui.rs                  # stratoclave ui open (opens browser)
    └── setup.rs               # stratoclave setup [--codex] (writes config.toml)
```

The CLI exists to absorb the three jobs that a browser cannot comfortably do:
calling AWS SDK APIs (`sts:GetCallerIdentity` for SSO Vouch), launching
Claude tools with `ANTHROPIC_BASE_URL` set, and launching OpenAI codex with
`CODEX_HOME` pointed at an ephemeral config and `STRATOCLAVE_OPENAI_KEY`
holding a short-lived `responses:send` key. Everything else — RBAC, Bedrock
proxy, credit accounting — is performed server-side.

**Bootstrap.** On a new machine, the CLI is configured with a single
command: `stratoclave setup <server-url>`. That command calls
`GET /.well-known/stratoclave-config` and materializes `config.toml`. See
[Well-known configuration](#well-known-configuration) below.

### IaC — AWS CDK v2 (TypeScript)

**Path:** [`iac/`](../iac)

Eight stacks, all deployed to a single region. Stack names are namespaced by
a configurable `prefix`.

```
iac/lib/
├── network-stack.ts           # VPC, 2 public subnets
├── dynamodb-stack.ts          # All DynamoDB tables + GSIs (PAY_PER_REQUEST)
├── ecr-stack.ts               # Backend image repository
├── alb-stack.ts               # ALB + target group
├── frontend-stack.ts          # S3 bucket + CloudFront distribution + SPA fallback Function + OAC
├── cognito-stack.ts           # User Pool + domain + App Client (Callback URL from frontend)
├── ecs-stack.ts               # Fargate task, env vars, task role, Secrets Manager wiring
└── config-validator.ts        # Runtime validation of synth-time inputs
```

**Dependency order** (enforced by `addDependency`):
`network → dynamodb → ecr → alb → frontend → cognito → ecs → config`.

The Cognito stack intentionally depends on the frontend stack so that the
CloudFront domain name can be injected as an App Client Callback URL through
the ordinary CloudFormation `Fn::ImportValue` mechanism — no
`crossRegionReferences` and no manual post-deploy scripting.

**SPA fallback** is implemented by a CloudFront Function (viewer-request)
attached only to the S3 origin's default behavior. API paths
(`/api/*`, `/v1/*`, `/openai/*`, `/.well-known/*`) pass through untouched,
so legitimate `403` / `404` responses from the backend are never rewritten
to `index.html`.

**ECS task role** is scoped to:
- `bedrock:InvokeModel` / `InvokeModelWithResponseStream` (us-east-1 inference profiles).
- DynamoDB `GetItem`/`PutItem`/`UpdateItem`/`DeleteItem`/`Query`/`Scan` on the Stratoclave tables and their GSIs.
- Cognito `AdminCreateUser`, `AdminGetUser`, `AdminInitiateAuth`, `AdminRespondToAuthChallenge`, `AdminDeleteUser`, `AdminSetUserPassword`, `AdminUpdateUserAttributes`, `AdminUserGlobalSignOut`, `ListUsers`.
- `secretsmanager:GetSecretValue` on the task's own Secret ARN only.
- `ssm:GetParameter` / `ssm:GetParametersByPath` under `/${prefix}/*` only.

---

## Authentication flows

Stratoclave supports three authentication paths that all converge on the same
`AuthenticatedUser` abstraction consumed by authorization.

### 1. Cognito password flow

Used for local / offline administration and for the initial admin bootstrap.

```
  CLI or Web UI                  Backend                Cognito
    │  POST /api/mvp/auth/login    │                     │
    │  { email, password }         │                     │
    │ ────────────────────────────▶│  admin_initiate_auth│
    │                              │  (ADMIN_USER_       │
    │                              │   PASSWORD_AUTH)    │
    │                              │ ───────────────────▶│
    │                              │◀────────────────────│
    │  { access_token, ... }       │                     │
    │◀─────────────────────────────│                     │
    │                              │                     │
    │  (If NEW_PASSWORD_REQUIRED)  │                     │
    │  POST /api/mvp/auth/respond  │                     │
    │  { new_password, session }   │                     │
    │ ────────────────────────────▶│ admin_respond_      │
    │                              │ to_auth_challenge   │
```

### 2. AWS SSO — vouch-by-STS

Used for day-to-day engineering access. The user has already run
`aws sso login` (or has any other credential in their provider chain), and
`stratoclave auth sso` converts that local AWS identity into a Stratoclave
access token without transmitting the credentials.

```
  CLI                               Backend            STS (global)       Cognito
    │  pick credentials from        │                  │                   │
    │  provider chain               │                  │                   │
    │  sigv4-sign a POST to         │                  │                   │
    │  https://sts.<region>         │                  │                   │
    │  .amazonaws.com/?             │                  │                   │
    │  Action=GetCallerIdentity     │                  │                   │
    │                               │                  │                   │
    │  POST /api/mvp/auth/          │                  │                   │
    │  sso-exchange                 │                  │                   │
    │  { method, url, headers, body}│                  │                   │
    │  ────────────────────────────▶│                  │                   │
    │                               │  forward signed  │                   │
    │                               │  request         │                   │
    │                               │ ────────────────▶│                   │
    │                               │ <Arn><UserId><Account>               │
    │                               │◀─────────────────│                   │
    │                               │                  │                   │
    │                               │  Classify identity_type:             │
    │                               │   sso_user |                         │
    │                               │   federated_role |                   │
    │                               │   iam_user |                         │
    │                               │   instance_profile                   │
    │                               │                  │                   │
    │                               │  Gate 1: account in TrustedAccounts? │
    │                               │  Gate 2: role_name matches allow?    │
    │                               │  Gate 3: identity_type permitted?    │
    │                               │  Gate 4: invite policy (hybrid)      │
    │                               │                  │                   │
    │                               │  Mint random password, set it,       │
    │                               │  run admin_initiate_auth, discard    │
    │                               │  the password                        │
    │                               │ ────────────────────────────────────▶│
    │                               │  { AccessToken, IdToken, … }         │
    │                               │◀──────────────────────────────────── │
    │  { access_token, email,       │                  │                   │
    │    user_id, roles, org_id,    │                  │                   │
    │    identity_type }            │                  │                   │
    │◀──────────────────────────────│                  │                   │
```

The four gates are evaluated in order and each can deny the login:

| Gate | Check | Default if not explicitly allowed |
|------|-------|-----------------------------------|
| 1. Trusted account | `account_id` present in `TrustedAccounts` | **deny** |
| 2. Role pattern | `role_name` matches an entry in `allowed_role_patterns` | allow if list is empty |
| 3. Identity type | `instance_profile` requires `allow_instance_profile=true`; `iam_user` requires `allow_iam_user=true` | **deny** (default DENY for shared identities) |
| 4. Provisioning | `invite_only` looks up `SsoPreRegistrations`; `auto_provision` derives email from the session name | `invite_only` if unset |

Existing invitations are always consulted before the account-level policy —
this is intentional so that administrators can mix "auto-provision this
role pattern, but invite these named individuals" on the same account.

### 3. Long-lived API key flow

Used by headless clients (e.g. Claude Desktop Cowork, CI) that need a
bearer token with a lifetime longer than an `access_token`'s 1 hour.

```
  Client (Cowork / SDK)          Backend
    │  Authorization:              │
    │  Bearer sk-stratoclave-...   │   (also accepted via x-api-key header)
    │ ────────────────────────────▶│
    │                              │  Token starts with "sk-stratoclave-"?
    │                              │   → SHA-256 hash
    │                              │   → Lookup ApiKeys by hash
    │                              │   → Check revoked_at, expires_at
    │                              │   → Check owner is_user_deleted (tombstone)
    │                              │   → Check key.created_at >= Users.token_revoked_after
    │                              │   → Load owner from Users
    │                              │   → AuthenticatedUser(
    │                              │       auth_kind="api_key",
    │                              │       key_scopes=[...])
    │                              │
    │                              │  On require_permission(X):
    │                              │   allow iff owner.roles.covers(X)
    │                              │   AND key.scopes.covers(X)
```

The plaintext of an API key is returned **once**, in the response to
`POST /api/mvp/me/api-keys`, and is never stored on the server. Only the
SHA-256 hash is persisted. The `x-api-key` header is accepted as an
alternative to `Authorization: Bearer` for clients that cannot set the
`Authorization` header.

Revoking a key is a conditional update that takes effect on the next request
(no in-process caching). When a user is deleted or switched to a new tenant,
`ApiKeysRepository.revoke_all_for_user` bulk-revokes all keys belonging to
that user, and a `token_revoked_after` watermark is stamped on the `Users`
row; any key minted before the watermark is refused even if its `revoked_at`
field has not been individually set.

---

## Authorization (RBAC)

### Roles and permissions

Three roles are shipped by default: `admin`, `team_lead`, and `user`. Roles
are stored in `Users.roles: list[str]` — a user can carry multiple roles and
permissions are unioned across them.

Permissions are strings of the form `<resource>:<action>[:<scope>]`. The
wildcard `resource:*` matches any action under *the same resource*; wildcard
matching requires exact resource-name equality, so `users:*` covers
`users:create` but does **not** cover `users-admin:create`. To make this
guarantee safe, resource names must not contain `-` or `_` (actions may).

The shipped permissions table:

| Role | Permissions |
|------|-------------|
| `admin` | `users:*`, `tenants:*`, `usage:*`, `permissions:*`, `accounts:*`, `apikeys:*`, `messages:send`, `responses:send` |
| `team_lead` | `tenants:create`, `tenants:read-own`, `usage:read-own-tenant`, `usage:read-self`, `apikeys:read-self`, `apikeys:create-self`, `apikeys:revoke-self`, `messages:send`, `responses:send` |
| `user` | `messages:send`, `responses:send`, `usage:read-self`, `apikeys:read-self`, `apikeys:create-self`, `apikeys:revoke-self` |

The `messages:send` scope gates `POST /v1/messages` (Anthropic Messages
API). `responses:send` gates `POST /openai/v1/responses` (OpenAI Responses
API on bedrock-mantle). The two are independent: a key issued with only
`messages:send` cannot reach the OpenAI route, and vice versa.

### How permissions are seeded

`backend/permissions.json` is the human-editable source of truth. The file's
top-level `"version"` field uses a `"YYYY-MM-DD.N"` scheme (e.g.
`"2026-06-02.1"`). On application startup, `backend/bootstrap/seed.py`
compares this version to whatever is in the `Permissions` DynamoDB table and
writes only when they differ. The seed is idempotent: re-running it (by
redeploying or restarting) is always safe, and it is a no-op when the
version is already current.

At runtime, `backend/mvp/authz.py` caches role → permissions in-process for
10 seconds. This means a permission change is effective across all Fargate
tasks within that TTL without any coordination.

### Tenant isolation

Team leads must be *structurally* unable to see other tenants, not merely
denied. Two mechanisms enforce this:

1. `require_tenant_owner(tenant_id)` — a FastAPI dependency that returns
   `404 Not Found` for any tenant the caller does not own. Admins are
   exempt. Non-existent tenants return the same `404`. The caller cannot
   distinguish "this tenant doesn't exist" from "this tenant isn't yours".
2. `Tenants.team_lead_user_id` is a GSI partition key. Team-lead listings
   query by the caller's own `user_id`, so other tenants are never fetched
   server-side in the first place.

For admin-created tenants that are not owned by any team lead, the
`team_lead_user_id` column is set to the sentinel string `admin-owned`.
This keeps the GSI partition key non-null while preventing any real user
from accidentally becoming the owner.

### Scope narrowing for API keys

When a request arrives with a long-lived API key, authorization runs a
double check. The caller is admitted only if the requested permission is
held by **both** the key owner's roles **and** the key's scopes:

```
allow(permission) ≡
    owner_roles.covers(permission)  AND  key_scopes.covers(permission)
```

This is important because roles can be revoked at any time (e.g. an admin
demoted to user). If a key was issued with `apikeys:*` at the time an
admin owned it, the scope narrowing ensures that the key cannot exceed the
owner's current privileges — the next request after the demotion will fail
the `owner_roles.covers(permission)` side of the AND.

---

## Credit model

Credit is a budget denominated in Bedrock tokens (input + output) and
scoped to a `(user_id, tenant_id)` pair. A user's credit does not transfer
across tenants; moving a user to a new tenant initializes a fresh balance.

### Accounting

`backend/dynamo/user_tenants.py` atomically reserves credit **before** the
Bedrock call via `reserve()`, then settles the difference via `refund()` after
the call returns:

```
# reserve() — called before Bedrock
UpdateItem(
    Key = {user_id, tenant_id},
    UpdateExpression    = "ADD credit_used :tokens SET updated_at = :now",
    ConditionExpression = "credit_used <= :max_allowed_used AND
                           total_credit = :expected_total AND
                           (attribute_not_exists(#s) OR #s = :active)"
)

# refund() — returns the unused portion (reservation - actual) after Bedrock
UpdateItem(
    Key = {user_id, tenant_id},
    UpdateExpression    = "ADD credit_used :neg_tokens SET updated_at = :now",
    ConditionExpression = "credit_used >= :tokens"
)
```

If `ConditionalCheckFailedException` is raised during `reserve()` — because
a concurrent reserve or admin overwrite changed `total_credit` — the backend
re-reads the row and retries up to five times. If the balance is genuinely
exhausted, `reserve_credit()` in `backend/mvp/_pipeline.py` raises
**HTTP 402 Payment Required** with a `credit_exhausted` detail body.

### Priority order for initial balance

When a user is attached to a tenant, the starting `total_credit` is chosen
by the first matching rule:

1. Explicit `--total-credit N` on user creation → `credit_source = user_override`.
2. Otherwise the tenant's `default_credit` → `credit_source = tenant_default`.
3. Otherwise the deployment-wide default (100 000 tokens) → `credit_source = global_default`.

### Pre-call reservation and post-call settlement

Both `/v1/messages` and `/openai/v1/responses` use a pessimistic reservation
model implemented in `backend/mvp/_pipeline.py`:

1. `reserve_credit()` atomically debits an estimated token budget before the
   Bedrock call. If the balance is insufficient the request is refused
   immediately with **HTTP 402**; the Bedrock API is never reached.
2. After Bedrock returns (or the stream completes), `settle_reservation_and_log()`
   refunds the difference between the reservation and actual usage, then
   writes an immutable `UsageLogs` row with the true token counts.
   - If actual usage exceeds the reservation (rare for reasoning-heavy workloads),
     the pipeline attempts a best-effort top-up reserve for the overrun; if
     the user is now out of credit the overage is clamped and a
     `credit_overrun` warning is emitted to CloudWatch.

The net result is that the balance never permanently exceeds `total_credit`
except for the bounded, logged overrun case.

### Refills

Refills are performed by an administrator via
`PATCH /api/mvp/admin/users/{user_id}/credit`, which overwrites
`total_credit` and optionally resets `credit_used`. Refill operations are
audited (`event=credit_overwritten`).

---

## OpenAI Responses API (bedrock-mantle path)

`POST /openai/v1/responses` is the OpenAI counterpart of `/v1/messages`.
It accepts an OpenAI Responses API payload, runs the same credit pipeline
as the Anthropic route (`backend/mvp/_pipeline.py`), and forwards the body
to `bedrock-mantle.{region}.api.aws/openai/v1/responses` via `httpx`. The
target region is per-model (`ModelEntry.bedrock_region`), so the call from
the us-east-1 ECS task is cross-region (us-west-2 for `gpt-5.4`,
us-east-2 for `gpt-5.5`).

The bedrock-mantle endpoint authenticates with a bearer token that
Stratoclave mints on demand from `aws-bedrock-token-generator.provide_token(
region=…, expiry=timedelta(seconds=900))`. The token TTL is capped at 15
minutes; the token is held only on the request stack and is never
persisted to DynamoDB or logs.

**Differences from the Anthropic path:**

- Reservation accounts for reasoning effort. `_estimate_reservation_tokens`
  multiplies `max_output_tokens` by 1× / 2× / 4× / 8× for `low` / `medium`
  / `high` / `xhigh` reasoning effort, then floors the total at 8 192
  tokens. Reasoning tokens count toward `output_tokens` in `usage`.
- Streaming relays SSE events byte-for-byte after sanitizing
  `event: error` lines through `core.error_handler.sanitize_exception_message`
  to scrub ARNs and account IDs.
- The route is gated by the `CODEX_ENABLED` ECS env flag; when off, both
  `POST /openai/v1/responses` and `GET /openai/v1/models` return HTTP 503.
- Image / file inputs are rejected at the Pydantic layer (HTTP 422); the
  proxy does not yet model image-token cost.

The `bedrock-mantle:CallWithBearerToken` IAM action does not currently
support resource-level conditions (verified at deploy time, 2026-06), so
the task role's policy uses `Resource: '*'` for that action only.
`bedrock-mantle:CreateInference` and friends are scoped to
`arn:aws:bedrock-mantle:{us-east-2,us-west-2}:<account>:project/*`.

For the user-facing setup, see [`CODEX_GUIDE.md`](./CODEX_GUIDE.md).

---

## Audit logging

Privileged operations emit a structured JSON line to CloudWatch Logs via a
dedicated logger (`stratoclave.audit`). The logger is isolated from the
application logger so that downstream log routing can subscribe to audit
events without pulling in normal request traffic.

| Event | Emitted by | Key fields |
|-------|------------|------------|
| `admin_created` | `admin_users.py` | `actor_id`, `target_id`, `email`, `role` |
| `user_created` | `admin_users.py` | `actor_id`, `target_id`, `email`, `role` |
| `user_deleted` | `admin_users.py` | `actor_id`, `target_id`, `email`, `roles` |
| `user_locale_updated_by_admin` | `admin_users.py` | `actor_id`, `target_id`, `before`, `after` |
| `user_locale_updated` | `me.py` | `actor_id`, `before`, `after` |
| `user_tenant_switched` | `admin_users.py` | `actor_id`, `target_id`, `before`, `after` |
| `api_keys_revoked_on_user_delete` | `admin_users.py` | `actor_id`, `target_id`, `count` |
| `credit_overwritten` | `admin_users.py` | `actor_id`, `target_id`, `before`, `after` |
| `tenant_created` | `admin_tenants.py` | `actor_id`, `target_id` (=tenant) |
| `tenant_updated` | `admin_tenants.py` | `actor_id`, `target_id`, `before`, `after` |
| `tenant_archived` | `admin_tenants.py` | `actor_id`, `target_id` |
| `tenant_owner_changed` | `admin_tenants.py` | `actor_id`, `target_id`, `before`, `after` |
| `team_lead_tenant_created` | `team_lead.py` | `actor_id`, `target_id` (=tenant) |
| `team_lead_tenant_updated` | `team_lead.py` | `actor_id`, `target_id`, `before`, `after` |
| `sso_invite_created` | `admin_sso_invites.py` | `actor_id`, `target_id` (=email) |
| `sso_invite_deleted` | `admin_sso_invites.py` | `actor_id`, `target_id` |
| `sso_login_success` | `sso_exchange.py` | `actor_id`, `account_id`, `identity_type`, `arn`, `new_user` |
| `sso_login_denied` | `sso_exchange.py` | `reason`, `account_id`, `identity_type`, `arn` |
| `sso_user_provisioned` | `sso_exchange.py` | `target_id`, `email`, `role`, `tenant_id` |
| `api_key_created` | `me_api_keys.py`, `admin_api_keys.py` | `actor_id`, `target_id`, `scopes`, `expires_at`, `on_behalf_of` |
| `api_key_revoked` | `me_api_keys.py`, `admin_api_keys.py` | `actor_id`, `target_id`, `owner_user_id` |
| `trusted_account_created` / `_updated` / `_deleted` | `admin_trusted_accounts.py` | `actor_id`, `target_id` (=account), `details` |
| `ui_ticket_minted` | `ui_ticket.py` | `actor_id`, `target_id="(ui-handoff)"`, `expires_at` |
| `ui_ticket_mint_subject_mismatch` | `ui_ticket.py` | `actor_id`, `reason` |

Every audit event carries `timestamp` in RFC 3339 UTC. A future release will
promote audit events to a dedicated DynamoDB table with a search UI; the
wire format is designed to be forward-compatible.

---

## Well-known configuration

`GET /.well-known/stratoclave-config` is an unauthenticated endpoint that
returns the information a CLI needs to bootstrap itself: the backend URL,
the Cognito User Pool ID, the App Client ID, the Cognito domain, and a few
CLI hints. The response is cached (`Cache-Control: public, max-age=300`)
and the backend derives `api_endpoint` from `X-Forwarded-Host` +
`X-Forwarded-Proto` so that the CLI only needs the CloudFront URL.

Response shape (schema_version = `"1"`):

```json
{
  "schema_version": "1",
  "api_endpoint": "https://<your-deployment>.cloudfront.net",
  "cognito": {
    "user_pool_id": "us-east-1_XXXXXXXX",
    "client_id": "1abcd2efgh3ijkl4mnop5qrstu",
    "domain": "https://stratoclave.auth.us-east-1.amazoncognito.com",
    "region": "us-east-1"
  },
  "cli": {
    "default_model": "us.anthropic.claude-opus-4-7",
    "callback_port": 18080,
    "codex": {
      "default_model": "openai.gpt-5.4",
      "openai_base_path": "/openai/v1",
      "supported_regions": ["us-east-2", "us-west-2"]
    }
  }
}
```

The `cli.codex` block is present **only when `CODEX_ENABLED=true`** is set on
the ECS task; it is absent entirely when the OpenAI Responses path is
disabled. Old CLIs that never see `cli.codex` simply never offer the
`codex` subcommand bootstrap.

`api_endpoint` above is the sample deployment URL used throughout these docs;
your actual value is whatever CloudFront URL `deploy-all.sh` prints at the end
of a deploy.

### Why this is safe to publish unauthenticated

Every field returned is already visible in the browser when a user loads
the Cognito Hosted UI (the User Pool ID, App Client ID, Cognito domain,
and region are all embedded in the OAuth URL). None of the fields grants
any capability on its own — Cognito still requires a valid user credential,
a PKCE verifier, or a signed STS request to mint a token. The endpoint
explicitly refuses to include any field whose name matches `secret`,
`password`, `private_key`, or `aws_secret_access_key`; a runtime guard
enforces this as a regression safety net.

### What is *not* in the response

- Long-lived API keys (`sk-stratoclave-...`) — these are per-user secrets.
- The backend's Bedrock IAM role, Secrets Manager ARNs, or any other
  internal identifier.
- Any Cognito client secret. The App Client Stratoclave uses is a *public*
  client (no client secret), as is standard for SPA + PKCE + native CLI.

### CLI usage

A fresh CLI install is configured by running:

```bash
stratoclave setup https://<your-deployment>.cloudfront.net   # your deployment URL
```

This fetches `GET /.well-known/stratoclave-config` and writes
`~/.stratoclave/config.toml`. Subsequent commands read that file; no other
out-of-band configuration is required. See [CLI_GUIDE.md](CLI_GUIDE.md#setup)
for the full command reference.

---

## Data model

All persistent state lives in DynamoDB. Every table is provisioned in
`PAY_PER_REQUEST` (on-demand) mode; this is appropriate for the expected
access pattern (small, bursty) and removes the need to tune capacity.

### Tables

| Table | PK | SK | GSIs | Purpose |
|-------|----|----|------|---------|
| `users` | `user_id` (Cognito sub) | `sk="PROFILE"` | `email-index`, `auth-provider-user-id-index` | Authoritative user record: email, roles, tenant, SSO metadata |
| `user-tenants` | `user_id` | `tenant_id` | `tenant-id-index` | Per-membership credit balance, role, status (active/archived) |
| `tenants` | `tenant_id` | — | `team-lead-index` | Tenant metadata; GSI enables team-lead listings |
| `permissions` | `role` | — | — | RBAC source of truth (seeded from `permissions.json`) |
| `usage-logs` | `tenant_id` | `timestamp_log_id` | `user-id-index` | Immutable per-call record; 90-day TTL |
| `api-keys` | `key_hash` (SHA-256) | — | `user-id-index` | Long-lived API keys; plaintext never stored |
| `trusted-accounts` | `account_id` | — | — | SSO allowlist + provisioning policy |
| `sso-pre-registrations` | `email` | — | `iam-user-index` | Invitations for invite-only provisioning |
| `sso-nonces` | `nonce` (SHA-256 of `Authorization + \x00 + X-Amz-Date`) | — | — | Vouch-by-STS replay defence. 10-minute TTL. Written with `attribute_not_exists(nonce)` before the STS round trip — a second submission of the same signed request returns 401. Fail-closed: any store error returns 401. |
| `ui-tickets` | `ticket_hash` (SHA-256 of the plaintext nonce) | — | — | CLI→SPA one-time handoff. Plaintext nonce (`stt_` prefix + 32 CSPRNG bytes) is returned to the CLI and placed in the `?ui_ticket=` URL parameter. SPA exchanges it via `POST /api/mvp/auth/ui-ticket/consume` (atomic delete-and-return). 30-second TTL. Plaintext never stored on the backend. |

### Notable invariants

- **Users are created once, never renamed.** The PK is Cognito's immutable
  `sub`; an email change updates the `email-index` but not the PK.
- **Usage logs are immutable.** When a user is moved to another tenant,
  historical logs keep their original `tenant_id`. A team lead's tenant
  view naturally only contains records from the period during which a
  user was a member.
- **`default-org` always exists.** The seed inserts it with
  `ConditionExpression = attribute_not_exists(tenant_id)`. A tenant deletion
  will never remove `default-org`.
- **Credit balances are decremented atomically** via conditional updates;
  simultaneous admin edits (e.g. a refill) and user requests are serialized
  by the `credit_used` precondition.

---

## Scaling considerations

- **Fargate.** One task handles several hundred req/s comfortably thanks to
  FastAPI + `uvloop`. Horizontal scaling is driven by CPU and ALB target
  response time; because the backend is stateless, scaling does not require
  session affinity.
- **DynamoDB.** Credit accounting is the hottest path; it's a single
  conditional `UpdateItem` per request. `usage-logs` writes are
  partition-balanced by `tenant_id`, so the only potential hot partition is
  the `default-org` tenant. In deployments where `default-org` is heavily
  trafficked, move heavy users onto dedicated tenants; the
  `user-id-index` GSI still lets users see their own history globally.
- **CloudFront.** Static assets cache indefinitely (content-hashed filenames).
  `/config.json` is served fresh at each page load so that Cognito config
  changes propagate without invalidations. `/api/*` and `/v1/*` are
  pass-through with caching disabled.
- **Cognito.** The password path hits `AdminInitiateAuth`; the SSO path hits
  `AdminCreateUser`, `AdminSetUserPassword`, and `AdminInitiateAuth` in
  sequence. All three are within Cognito's default admin-API rate limits for
  realistic workloads (login is infrequent compared to `/v1/messages`).
- **Bedrock.** Model access limits are enforced by Bedrock itself;
  Stratoclave surfaces 4xx/5xx responses to callers but does not retry.

---

## Security considerations

### Authentication

- `token_use == "access"` is mandatory. `id_token` is rejected with `401`.
- The `client_id` claim must match the configured App Client. No wildcard,
  no `aud` (access tokens don't carry `aud`).
- JWKS keys are fetched lazily and cached by `PyJWKClient`; key rotation is
  transparent.
- Long-lived API keys are SHA-256 hashed at rest. The plaintext never touches
  disk, never appears in a log, and is returned only in the create response.

### Authorization

- Roles come from DynamoDB only. Cognito groups, custom claims, and user
  attributes are ignored.
- Permission checks are *deny by default*. Every protected route uses
  `require_permission("...")` as a FastAPI `Depends`.
- Scope narrowing (owner roles ∩ key scopes) means a compromised key cannot
  exceed the current privileges of its owner.

### Enumeration defense

- Team-lead routes return `404` uniformly for "not yours" and "does not
  exist". An admin-level `GET` is the only way to learn whether a given
  `tenant_id` is in use.
- Admin user lookups similarly standardize on `404` for unknown IDs.

### Transport and browser

- HSTS with a one-year max-age + `includeSubDomains`.
- CSP: `script-src 'none'; default-src 'self'; object-src 'none';
  frame-ancestors 'none'; base-uri 'self'; form-action 'self'`. The SPA is
  built without any inline `<script>`; the CSP has been verified with
  production builds.
- All input models use `ConfigDict(extra="forbid")`, so unexpected request
  fields are rejected at the edge.

### SSRF

The SSO vouch flow is the only place the backend makes an outbound HTTP
call on behalf of a caller. `backend/mvp/sso_sts.py` defends:

- The request URL's host must be in a hard-coded allowlist of STS regional
  endpoints.
- The URL scheme must be `https`.
- The query must contain exactly `Action=GetCallerIdentity`.
- The HTTP method must be `POST`.
- The `X-Amz-Date` header must be within ±5 minutes of the backend's
  wall clock.
- The request is made with a 10-second timeout.
- The `Authorization` + `X-Amz-Date` headers are SHA-256 fingerprinted and
  consumed via `SsoNoncesRepository.consume()` before the STS forward, using
  an `attribute_not_exists(nonce)` conditional put with a 10-minute TTL. A
  second submission of the same signed request within that window is
  rejected with **401**. The check is **fail-closed**: if the nonce store is
  unreachable for any reason (DynamoDB error, IAM drift, unprovisioned table),
  the entire SSO exchange returns 401 rather than silently degrading to
  skew-only protection.

### Production bootstrap gate

`POST /api/mvp/admin/users` is gated by `backend/mvp/authz.py`'s
`admin_creation_allowed()` for the `admin` role. The bootstrap window
behaves differently per environment:

- **Development / staging** (`ENVIRONMENT` ≠ `production`): set
  `ALLOW_ADMIN_CREATION=true` for the classic sticky-flag behavior.
- **Production** (`ENVIRONMENT=production`): the boolean flag alone is
  **not sufficient**. The operator must also set
  `ALLOW_ADMIN_CREATION_UNTIL=<epoch-seconds>` to a future instant. The
  gate auto-closes when `now > epoch`, so forgetting to clear the flag
  after bootstrap is not a permanent exposure. A malformed or absent
  `ALLOW_ADMIN_CREATION_UNTIL` value is treated as 0 (gate closed).

A per-request audit warning is emitted to CloudWatch whenever the gate is
open in production, rate-limited to once per 5 minutes per process.

### ECS network egress restriction

The ECS task security group (`network-stack.ts`) allows only:
- TCP/443 outbound — all AWS SDK calls (Bedrock, STS, Cognito, DynamoDB,
  ECR, CloudWatch Logs, SSM) use HTTPS.
- UDP/53 outbound — Route 53 Resolver for AWS endpoint DNS.

All other egress is denied. This limits the blast radius of a container
compromise to the above AWS service endpoints and prevents arbitrary
outbound connections.

### Request body size limit

`MaxBodySizeASGIMiddleware` in `backend/main.py` enforces a 2 MiB cap on
`POST`/`PUT`/`PATCH` bodies at the ASGI layer — before Pydantic or any
handler sees the payload. Both `Content-Length` fast-path and streaming
chunked-transfer slow-path are covered. Oversized requests return HTTP 413.
The limit is configurable via `REQUEST_MAX_BODY_BYTES`.

### Secrets management

The only secret that the backend holds is the Cognito App Client's secret
(if you enable client-secret flows; the default configuration is
secret-less). It is loaded from AWS Secrets Manager by ARN at task
startup. The task role's `secretsmanager:GetSecretValue` is scoped to
that one ARN.

---

## Extension points

Stratoclave is intentionally under-featured; the items below are designed
but not yet implemented.

- **Audit log table.** Promote `stratoclave.audit` events from CloudWatch to
  a dedicated DynamoDB table with a search UI so that compliance queries
  don't require CloudWatch Insights.
- **Tenant hierarchy.** A `parent_tenant_id` attribute on `tenants` would
  enable nested organizations (for example, departments inside a company).
- **Verified Permissions.** For deployments that need conditional
  authorization (e.g. "allow up to $X per day"), integrate Amazon Verified
  Permissions at the `require_permission` boundary.

Contributions are welcome; see
[CONTRIBUTING.md](../CONTRIBUTING.md) for the process and
[SECURITY.md](../SECURITY.md) for how to report vulnerabilities. The
repository URL is
[`https://github.com/littlemex/stratoclave`](https://github.com/littlemex/stratoclave).

---

## Known limitations

The following are known, tracked gaps. Each is slated for a follow-up
release; in the meantime, work around them as described.

- **`api-key revoke` requires the SHA-256 hash.** The CLI output of
  `api-key list` currently shows the masked `key_id`
  (`sk-stratoclave-XXXX...YYYY`) but not `key_hash`, so revoking from the CLI
  requires the hash to come from the Admin UI or a direct HTTP call. See
  [CLI_GUIDE.md -> Known limitations](CLI_GUIDE.md#known-limitations).
- **`admin user create` does not return a temporary password by default.**
  The response field is `null` unless `EXPOSE_TEMPORARY_PASSWORD=true` is set
  on the backend. The recommended workflow is to issue the first-login
  credential via `aws cognito-idp admin-set-user-password --no-permanent`.
  See [ADMIN_GUIDE.md -> Provisioning a new user](ADMIN_GUIDE.md#provisioning-a-new-user).
- **`admin trusted-accounts` has no CLI subcommand.** Administered via the
  web UI or direct HTTP calls until the CLI catches up.
- **Single control-plane region.** All Stratoclave infrastructure runs in
  `us-east-1`; see
  [DEPLOYMENT.md -> Regional constraints](DEPLOYMENT.md#regional-constraints).
  Bedrock for the Claude path is also us-east-1; the OpenAI Responses path
  makes cross-region HTTPS calls to bedrock-mantle (us-east-2 / us-west-2),
  but no second control-plane region is deployed.
