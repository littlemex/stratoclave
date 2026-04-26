# Architecture

Stratoclave is a **thin proxy gateway in front of Amazon Bedrock** that adds
multi-tenancy, role-based access control, credit quotas, and a unified login
surface (Amazon Cognito *or* AWS SSO) without introducing any non-AWS
dependencies. The deployment runs entirely inside a single AWS account and
region, and is deliberately composed of a very small number of moving parts so
that an operator can understand the whole system from this document alone.

This document describes the components, the data model, the authentication
and authorization flows, and the invariants that Stratoclave relies on. It is
intended for operators deploying Stratoclave, contributors reading the code
for the first time, and security reviewers. If you are looking for a
step-by-step setup guide, see [GETTING_STARTED.md](GETTING_STARTED.md); for
deployment and day-2 operations, see [ADMIN_GUIDE.md](ADMIN_GUIDE.md) and
[DEPLOYMENT.md](DEPLOYMENT.md).

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
   hosted metadata service, and no third-party dependency that could become
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
│   ├── user_tenants.py        # Credit accounting with optimistic locking
│   ├── usage_logs.py
│   ├── tenants.py
│   ├── permissions.py
│   ├── api_keys.py            # SHA-256 stored, plaintext never persisted
│   ├── trusted_accounts.py
│   └── sso_pre_registrations.py
└── mvp/                       # FastAPI routers + auth/authz helpers
    ├── deps.py                # JWT verification + API key path
    ├── authz.py               # has_permission, require_permission, audit log
    ├── anthropic.py           # POST /v1/messages, GET /v1/models
    ├── me.py                  # /api/mvp/me + usage summaries
    ├── admin_users.py
    ├── admin_tenants.py
    ├── admin_usage.py
    ├── admin_api_keys.py
    ├── admin_trusted_accounts.py
    ├── admin_sso_invites.py
    ├── team_lead.py
    ├── cognito_auth.py        # Email + password login
    ├── sso_sts.py             # Presigned URL validation + STS round-trip
    ├── sso_gate.py            # Identity-type classifier + allowlist gates
    ├── sso_exchange.py        # POST /api/mvp/auth/sso-exchange
    ├── me_api_keys.py         # Self-serve long-lived API key management
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
(1) a `?token=` URL parameter written by the CLI during `stratoclave ui open`,
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
│   ├── claude_cmd.rs          # Wraps `claude` with ANTHROPIC_BASE_URL injected
│   ├── api.rs                 # reqwest client, error rendering
│   ├── tokens.rs              # ~/.stratoclave/mvp_tokens.json (0600)
│   ├── config.rs              # STRATOCLAVE_* env + config.toml
│   ├── admin.rs / team_lead.rs / usage.rs
│   └── ...
└── commands/ui.rs             # stratoclave ui open (opens browser)
```

The CLI exists to absorb the two jobs that a browser cannot comfortably do:
calling AWS SDK APIs (`sts:GetCallerIdentity`) and launching Claude SDK
tools with `ANTHROPIC_BASE_URL` set. Everything else — RBAC, Bedrock proxy,
credit accounting — is performed server-side.

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
(`/api/*`, `/v1/*`, `/.well-known/*`) pass through untouched, so legitimate
`403` / `404` responses from the backend are never rewritten to `index.html`.

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
    │  Bearer sk-stratoclave-...   │
    │ ────────────────────────────▶│
    │                              │  Token starts with "sk-stratoclave-"?
    │                              │   → SHA-256 hash
    │                              │   → Lookup ApiKeys by hash
    │                              │   → Check revoked_at, expires_at
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
SHA-256 hash is persisted. Revoking a key is a conditional update that takes
effect on the next request (no in-process caching).

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
| `admin` | `users:*`, `tenants:*`, `usage:*`, `permissions:*`, `accounts:*`, `apikeys:*`, `messages:send` |
| `team_lead` | `tenants:create`, `tenants:read-own`, `usage:read-own-tenant`, `usage:read-self`, `apikeys:read-self`, `apikeys:create-self`, `apikeys:revoke-self`, `messages:send` |
| `user` | `messages:send`, `usage:read-self`, `apikeys:read-self`, `apikeys:create-self`, `apikeys:revoke-self` |

### How permissions are seeded

`backend/permissions.json` is the human-editable source of truth. On
application startup, `backend/bootstrap/seed.py` compares the file's
`version` field to whatever is in the `Permissions` DynamoDB table and
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

`backend/dynamo/user_tenants.py` performs the decrement with a conditional
update that uses the previous `credit_used` value as a precondition:

```
UpdateItem(
    Key = {user_id, tenant_id},
    UpdateExpression    = "SET credit_used = :new_used",
    ConditionExpression = "credit_used = :old_used AND total_credit >= :new_used"
)
```

If another request decremented the balance since we last read it, this call
raises `ConditionalCheckFailedException`, which the backend translates into
a `503 Service Unavailable` with a retry hint.

### Priority order for initial balance

When a user is attached to a tenant, the starting `total_credit` is chosen
by the first matching rule:

1. Explicit `--total-credit N` on user creation → `credit_source = user_override`.
2. Otherwise the tenant's `default_credit` → `credit_source = tenant_default`.
3. Otherwise the deployment-wide default (100 000 tokens) → `credit_source = global_default`.

### Post-call decrement (and the credit-debt window)

`/v1/messages` checks only that `remaining > 0` *before* the call, then
decrements by the real `input_tokens + output_tokens` reported by Bedrock
*after* the response. This means a balance can transiently go slightly
negative if a single request consumes more tokens than are left. The
behavior is documented in the endpoint's docstring and is acceptable for the
alpha release; a future reservation-based model will close the window.

### Refills

Refills are performed by an administrator via
`PATCH /api/mvp/admin/users/{user_id}/credit`, which overwrites
`total_credit` and optionally resets `credit_used`. Refill operations are
audited (`event=credit_overwritten`).

---

## Audit logging

Privileged operations emit a structured JSON line to CloudWatch Logs via a
dedicated logger (`stratoclave.audit`). The logger is isolated from the
application logger so that downstream log routing can subscribe to audit
events without pulling in normal request traffic.

| Event | Emitted by | Key fields |
|-------|------------|------------|
| `admin_created` | `admin_users.py` | `actor_id`, `target_id`, `email` |
| `user_deleted` | `admin_users.py` | `actor_id`, `target_id`, `tenant_id` |
| `user_tenant_switched` | `admin_users.py` | `actor_id`, `target_id`, `before`, `after` |
| `credit_overwritten` | `admin_users.py` | `actor_id`, `target_id`, `before`, `after` |
| `tenant_owner_changed` | `admin_tenants.py` | `actor_id`, `target_id`, `before`, `after` |
| `sso_login_success` | `sso_exchange.py` | `actor_id`, `account_id`, `identity_type`, `arn`, `new_user` |
| `sso_login_denied` | `sso_exchange.py` | `reason`, `account_id`, `identity_type`, `arn` |
| `sso_user_provisioned` | `sso_exchange.py` | `target_id`, `email`, `role`, `tenant_id` |
| `api_key_created` | `me_api_keys.py`, `admin_api_keys.py` | `actor_id`, `target_id`, `scopes`, `expires_at`, `on_behalf_of` |
| `api_key_revoked` | `me_api_keys.py`, `admin_api_keys.py` | `actor_id`, `target_id`, `owner_user_id` |
| `trusted_account_created` / `_updated` / `_deleted` | `admin_trusted_accounts.py` | `actor_id`, `target_id` (=account), `details` |

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
  "api_endpoint": "https://<cloudfront-domain>",
  "cognito": {
    "user_pool_id": "us-east-1_XXXXXXXX",
    "client_id": "1abcd2efgh3ijkl4mnop5qrstu",
    "domain": "https://<hosted-ui-subdomain>.auth.us-east-1.amazoncognito.com",
    "region": "us-east-1"
  },
  "cli": {
    "default_model": "us.anthropic.claude-opus-4-7",
    "callback_port": 18080
  }
}
```

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
stratoclave setup https://<cloudfront-domain>
```

This fetches `GET /.well-known/stratoclave-config` and writes
`~/.stratoclave/config.toml`. Subsequent commands read that file; no other
out-of-band configuration is required.

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

A future release will add an STS signature-nonce table (TTL 5 minutes) to
close the replay window within the 5-minute skew; this is deliberately
omitted for the alpha release because the replay attack requires
simultaneous possession of the signed request and network access to the
backend.

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

- **STS nonce table.** A DynamoDB table keyed by the `X-Amz-Signature`
  value with a 5-minute TTL; `ConditionExpression = attribute_not_exists` on
  insert fully eliminates the 5-minute replay window.
- **Audit log table.** Promote `stratoclave.audit` events from CloudWatch to
  a dedicated DynamoDB table with a search UI so that compliance queries
  don't require CloudWatch Insights.
- **Tenant hierarchy.** A `parent_tenant_id` attribute on `tenants` would
  enable nested organizations (for example, departments inside a company).
- **Reservation-based credits.** Pre-reserve an estimate before the Bedrock
  call and settle the difference afterwards, eliminating the transient
  credit-debt window described in [Credit model](#credit-model).
- **Verified Permissions.** For deployments that need conditional
  authorization (e.g. "allow up to $X per day"), integrate Amazon Verified
  Permissions at the `require_permission` boundary.

Contributions are welcome — see
[CONTRIBUTING.md](../CONTRIBUTING.md) for the process and
[SECURITY.md](../SECURITY.md) for how to report vulnerabilities.
