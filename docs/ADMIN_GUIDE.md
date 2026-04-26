# Administrator Guide

This guide is for operators with the **`admin`** role in a Stratoclave deployment. It covers the day-to-day tasks an administrator is expected to perform: managing users and tenants, issuing API keys, allow-listing AWS accounts for SSO, inspecting usage, and locking down the control plane after bootstrap.

If you are a **user** and just want to chat or run the CLI, start with [GETTING_STARTED.md](./GETTING_STARTED.md). If you are **deploying** Stratoclave to your own AWS account, see [DEPLOYMENT.md](./DEPLOYMENT.md) first and come back here once the bootstrap admin exists.

---

## Table of contents

1. [The RBAC model](#the-rbac-model)
2. [Logging in as administrator](#logging-in-as-administrator)
3. [Managing users](#managing-users)
4. [Managing tenants](#managing-tenants)
5. [SSO: Trusted AWS accounts](#sso-trusted-aws-accounts)
6. [API keys](#api-keys)
7. [Viewing usage](#viewing-usage)
8. [Handing the Web URL to CLI users](#handing-the-web-url-to-cli-users)
9. [Locking down after bootstrap](#locking-down-after-bootstrap)
10. [Audit log reference](#audit-log-reference)

---

## The RBAC model

Stratoclave has three roles, stored in the `stratoclave-users` DynamoDB table and evaluated by the Backend on every request.

| Role        | Can do                                                                                                                                   |
| ----------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `admin`     | Everything. Manage users, tenants, trusted accounts, SSO invites, API keys, view global usage, set credits.                              |
| `team_lead` | Manage only tenants they own: invite/remove members, adjust their credits, view per-tenant usage. Cannot see other tenants.              |
| `user`      | Send messages, view their own usage, rotate their own API keys. Cannot see other users.                                                  |

**Tenant isolation.** Every user belongs to exactly one *active* tenant at a time. A `team_lead` can only see the tenants they own. An `admin` can see all tenants. The `default-org` tenant is seeded automatically on first Backend startup and cannot be deleted — it is the fallback for users with no explicit tenant assignment.

**Permissions seeding.** The `admin`, `team_lead`, and `user` permission rows in DynamoDB (`stratoclave-permissions`) are seeded idempotently on Backend startup by `bootstrap/seed.py`. Administrators do **not** need to run any DynamoDB scripts manually.

<!-- TODO(docs): Insert screenshot showing the admin home dashboard with user / tenant / trusted-account tiles -->

---

## Logging in as administrator

Administrators log in through the Web UI, the same as any other user. The bootstrap admin is created by [`scripts/bootstrap-admin.sh`](../scripts/bootstrap-admin.sh); see [DEPLOYMENT.md](./DEPLOYMENT.md#post-deploy-first-admin) for how to create one.

1. Open `https://<YOUR_CLOUDFRONT_URL>` in your browser.
2. Enter the admin email and the password printed by `bootstrap-admin.sh`.
3. The header now shows the admin-only navigation items: **Users**, **Tenants**, **Trusted Accounts**, **Usage**.

You can also drive every admin operation from the CLI:

```bash
stratoclave setup https://<YOUR_CLOUDFRONT_URL>
stratoclave auth login --email admin@example.com
stratoclave admin users list
```

---

## Managing users

### Inviting a new user

From the Web UI: **Users → New user**. Fill in:

| Field            | Notes                                                                                                         |
| ---------------- | ------------------------------------------------------------------------------------------------------------- |
| Email            | Used as the Cognito username. Must be unique across the deployment.                                           |
| Role             | `user` or `team_lead`. The `admin` option is disabled by default (see [Locking down](#locking-down-after-bootstrap)). |
| Tenant           | Leave blank for `default-org`.                                                                                |
| Credit override  | Leave blank to inherit the tenant's `default_credit`.                                                         |

<!-- TODO(docs): Insert screenshot of the "New user" modal -->

On success, a one-time **temporary password modal** appears. The modal is designed to be unmissable:

- The close button is disabled until you click **Copy**.
- Escape and backdrop clicks are blocked.
- Once closed, the password cannot be shown again — Cognito stores only the hash.

Hand the temporary password to the user over a secure channel (e.g., a password manager share). At first login they will be required to set a permanent password.

Equivalent CLI:

```bash
stratoclave admin users create \
  --email newuser@example.com \
  --role user \
  --tenant default-org
```

### Removing a user

**Users → \<email\> → Delete.** Type the user's email into the confirmation field and submit.

What this does:

1. Deletes the user from Cognito (they can no longer log in).
2. Deletes the row from the `stratoclave-users` DynamoDB table.
3. Archives (not deletes) the user's rows in `stratoclave-user-tenants`.
4. **Preserves `stratoclave-usage-logs`** — audit history is retained.

Guardrails:

- Deleting yourself is rejected with HTTP `409`.
- Deleting the last remaining `admin` is rejected with HTTP `409`.

### Changing a user's tenant

**Users → \<email\> → Change tenant.** A two-step confirmation: after selecting the new tenant, you must type the user's email to confirm.

On commit, the Backend:

1. Archives the current `user_tenants` row.
2. Creates a new `user_tenants` row with `status=active`, applying the tenant's `default_credit` (or the override you provide).
3. Updates the Cognito attribute `custom:org_id` to match.
4. Calls `AdminUserGlobalSignOut` to invalidate all of the user's sessions, forcing them to re-authenticate.

### Adjusting a user's credit

**Users → \<email\> → Adjust credit.** You can:

- Set a new `total_credit` value.
- Check **Reset used** to zero out `credit_used` (useful at the start of a new billing period).

The change is immediate; the user's next request will be evaluated against the new values.

---

## Managing tenants

A **tenant** in Stratoclave is an organizational unit that owns a credit pool. Tenants typically correspond to teams, departments, or customer accounts.

### Creating a tenant

**Tenants → New tenant.**

| Field            | Notes                                                                                              |
| ---------------- | -------------------------------------------------------------------------------------------------- |
| Name             | Display name.                                                                                      |
| Owner            | Either `admin-owned` (shared, visible only to admins) or a specific `team_lead` user.              |
| `default_credit` | Initial token budget granted to each new member.                                                   |

<!-- TODO(docs): Insert screenshot of the "New tenant" modal -->

If an owner is assigned, that `team_lead` can immediately see the tenant under **My tenants** in their UI and manage its members.

### Viewing tenant members and usage

**Tenants → \<name\>** shows:

- The member table (email, role, credit remaining, credit used).
- A bar chart of token consumption grouped by model.
- A bar chart of token consumption grouped by user (email-labeled for admins).

By default the charts aggregate the last 30 days, capped at 1 000 samples per axis.

### Changing tenant ownership

**Tenants → \<name\> → Change owner.** Assign to another `team_lead` or to `admin-owned`. The previous owner immediately loses visibility into the tenant.

### Deleting (archiving) a tenant

**Tenants → \<name\> → Archive.**

- The `default-org` tenant cannot be archived.
- Archiving is soft: the row is flagged `archived`, but usage logs and user-tenant history are kept.
- Users who had this tenant as their active tenant are **not** automatically reassigned; archive only tenants that already have zero active members (the UI warns if not).

---

## SSO: Trusted AWS accounts

Stratoclave accepts federated logins from AWS-native identities (IAM Identity Center users, SAML-federated roles, IAM users, and EC2 instance profiles). You allow-list the AWS accounts that are permitted to federate, and optionally specify per-account provisioning rules.

<!-- TODO(docs): Insert screenshot of the "Trusted accounts" list page -->

### Supported identity types

| Identity type      | `identity_type`      | Typical source                                           |
| ------------------ | -------------------- | -------------------------------------------------------- |
| SSO user           | `sso_user`           | IAM Identity Center (`session_name == email`).           |
| Federated role     | `federated_role`     | SAML / Isengard / enterprise IdP via `AssumeRoleWithSAML`. |
| IAM user           | `iam_user`           | Long-lived IAM user with access keys.                    |
| EC2 instance profile | `instance_profile` | Role assumed via EC2 instance metadata.                  |

Only `sso_user` and `federated_role` are accepted by default; `iam_user` and `instance_profile` must be opted in per trusted account because multiple humans can share them.

### Adding a trusted account

**Trusted Accounts → Add account.**

| Field                     | Notes                                                                                                                        |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| AWS Account ID            | 12-digit account number.                                                                                                     |
| Provisioning policy       | `invite_only` (default, safest) or `auto_provision`.                                                                         |
| Allowed role patterns     | List of glob patterns matched against the assumed-role ARN. Empty list means "any role".                                     |
| Allow IAM user            | Off by default. Turn on only for break-glass or automation accounts.                                                         |
| Allow instance profile    | Off by default. **Strongly discouraged** for interactive use; turn on only for single-tenant runners.                        |
| Default tenant / credit   | Applied to auto-provisioned users if not overridden by an invite.                                                            |

CLI equivalent:

```bash
stratoclave admin trusted-accounts create \
  --account-id 123456789012 \
  --policy invite_only \
  --default-tenant default-org
```

### Invite-only vs auto-provisioning

- `invite_only` (recommended): no user can log in until an administrator pre-registers their email. Safest for production.
- `auto_provision`: any caller from the account whose session name matches a valid email is created on demand with role `user`. Suitable for internal-only, IdP-backed accounts where every SSO user is trusted.

**Invites always win.** Even under `auto_provision`, if an invite exists for the incoming email, its role / tenant / credit override the default.

### Creating an invite

**Trusted Accounts → \<account\> → Add invite.**

| Field           | Notes                                                                                                                             |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Email           | The Stratoclave user account this invite will create.                                                                             |
| Role            | `user` or `team_lead`. Granting `admin` via SSO is intentionally not supported.                                                   |
| IAM user name   | Required for `iam_user` logins, or to map an enterprise session name (e.g., Isengard) to an email.                                |
| Tenant / credit | Overrides the account-level default.                                                                                              |

### Handling enterprise SAML where `session_name != email`

Some enterprise IdPs (including Amazon's internal Isengard) set the session name to an opaque identifier instead of the user's email. To map these:

1. Collect the session name (visible in CloudTrail after a failed login).
2. Create an invite with `Email = user@example.com` **and** `IAM user name = <session-name>`.
3. The next login from that session produces a Stratoclave user keyed on the email.

### Disabling a trusted account

**Trusted Accounts → \<account\> → Delete.** Pending invites for that account are deleted too. Existing Stratoclave users that were provisioned from the account keep working until you delete them explicitly.

---

## API keys

Stratoclave supports long-lived API keys of the form `sk-stratoclave-...` for machine-to-machine access, CI jobs, and integrations (including the bundled CLI and `cowork` gateway).

Keys carry two scopes:

- `messages:send` — call `POST /v1/messages`.
- `usage:read-self` — read the owner's own usage.

### Issuing a key for yourself

From the Web UI: **Account → API keys → New key.** Or on the CLI:

```bash
stratoclave me api-keys create --name my-ci-key
```

The full secret is shown once. Store it immediately — the Backend only keeps the SHA-256 hash.

### Issuing a key on behalf of another user

As an admin you can proxy-issue a key for any user. This is useful for onboarding a headless service account without that account having to log in first.

**Users → \<email\> → API keys → New key.** Or:

```bash
stratoclave admin users api-keys create \
  --user-id <USER_ID> \
  --name pipeline-prod \
  --expires-days 90
```

The caller still receives the plaintext secret once; hand it off over a secure channel.

### Revoking a key

- Self-service: **Account → API keys → Revoke.**
- Admin override: **Admin → API keys → Revoke any key** (search by last-four or owner).

Revocation is immediate: the next request using that key returns `401 Unauthorized`.

---

## Viewing usage

### Global usage (admin only)

**Usage** in the header shows the `stratoclave-usage-logs` DynamoDB table with filters:

- `tenant_id` and/or `user_id` — translated to a DynamoDB Query (fast).
- `since` / `until` — ISO 8601 range filter (e.g., `2026-01-01T00:00:00Z`).
- Pagination is cursor-based; **Next** / **Previous** walk the result set.

<!-- TODO(docs): Insert screenshot of the admin usage logs page with filters populated -->

### Per-tenant summary

On a tenant detail page you get the same two charts described under [Viewing tenant members and usage](#viewing-tenant-members-and-usage), plus a CSV export of the raw rows.

### Per-user summary

Each user detail page shows `credit_remaining`, `credit_used`, and a sparkline of the last 30 days of activity. The user-list page also surfaces `credit_remaining` / `credit_used` columns so you can spot near-exhausted accounts at a glance.

---

## Handing the Web URL to CLI users

Once you have deployed and bootstrapped an admin, share the CloudFront URL with your users and tell them to run:

```bash
stratoclave setup https://<YOUR_CLOUDFRONT_URL>
stratoclave auth login --email user@example.com
```

`stratoclave setup` fetches `/.well-known/stratoclave-config` from the Backend and writes `~/.stratoclave/config.toml`. Users do **not** need to know the Cognito pool ID or client ID — `setup` discovers them automatically.

---

## Locking down after bootstrap

`scripts/bootstrap-admin.sh` requires the Backend to be running with the environment variable `ALLOW_ADMIN_CREATION=true`. This exposes `POST /api/mvp/admin/users` with `roles=['admin']` to any caller, which is intentional for the zero-state case but **must not remain enabled in production**.

After the first admin can log in, disable the flag:

1. Edit `iac/bin/iac.ts` (or your wrapper) so `allowAdminCreation = 'false'`.
2. Redeploy the ECS stack:

   ```bash
   cd iac
   npx cdk deploy <Prefix>EcsStack
   ```

3. (Optional) Force a new task to pick up the env var immediately:

   ```bash
   aws ecs update-service \
     --cluster <PREFIX>-cluster \
     --service <PREFIX>-backend \
     --force-new-deployment
   ```

From this point on, new admins can only be promoted by an existing admin through the Web UI.

---

## Audit log reference

The Backend emits structured JSON logs (CloudWatch Logs group `/ecs/<prefix>-backend`) for every privileged action. Useful CloudWatch Logs Insights queries:

```sql
-- All admin actions in the last 24h
fields @timestamp, event, actor_email, target_email, tenant_id
| filter event like /^admin_|^user_|^tenant_|^sso_|^trusted_account_/
| sort @timestamp desc
```

| Event                                                                                     | Emitted by                        |
| ----------------------------------------------------------------------------------------- | --------------------------------- |
| `admin_created`, `user_created`, `user_deleted`                                           | `POST/DELETE /api/mvp/admin/users` |
| `tenant_created`, `tenant_updated`, `tenant_archived`, `tenant_owner_changed`             | `/api/mvp/admin/tenants[*]`       |
| `user_tenant_switched`, `credit_overwritten`                                              | user mutations                    |
| `sso_login_success`, `sso_login_denied`, `sso_user_provisioned`                           | `POST /api/mvp/auth/sso-exchange` |
| `sso_invite_created`, `sso_invite_deleted`                                                | `/api/mvp/admin/sso-invites[*]`   |
| `trusted_account_created`, `trusted_account_updated`, `trusted_account_deleted`           | `/api/mvp/admin/trusted-accounts[*]` |
| `api_key_created`, `api_key_revoked`                                                      | `/api/mvp/{me,admin}/api-keys[*]` |

All events include `request_id` (propagated from the ALB header) so you can correlate a UI action with the exact Backend log line.

---

## Password reset (administrator-assisted)

If a user forgets their password, force a reset from the AWS CLI:

```bash
aws cognito-idp admin-set-user-password \
  --user-pool-id <YOUR_USER_POOL_ID> \
  --username 'user@example.com' \
  --password 'TempPassword!23' \
  --no-permanent \
  --region us-east-1
```

`--no-permanent` returns the user to the `FORCE_CHANGE_PASSWORD` state; their next `stratoclave auth login` will prompt for a new permanent password.

---

## Related documents

- [GETTING_STARTED.md](./GETTING_STARTED.md) — for end users.
- [CLI_GUIDE.md](./CLI_GUIDE.md) — reference for the `stratoclave` command, including the `admin` subcommands.
- [DEPLOYMENT.md](./DEPLOYMENT.md) — how to stand up a new Stratoclave deployment.
- [ARCHITECTURE.md](./ARCHITECTURE.md) — how the pieces fit together.
- [SECURITY.md](../SECURITY.md) — reporting vulnerabilities and threat model.
