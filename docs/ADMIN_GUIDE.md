<!-- Last updated: 2026-04-27 -->
<!-- Applies to: Stratoclave main @ 48b9533 (or later) -->

# Administrator Guide

> A Japanese translation is available at [ja/ADMIN_GUIDE.md](./ja/ADMIN_GUIDE.md).

This guide is for operators holding the `admin` role on a Stratoclave deployment. It covers the day-to-day tasks an administrator is expected to perform: managing users and tenants, issuing API keys, allow-listing AWS accounts for SSO, inspecting usage, and locking down the control plane after bootstrap.

If you are a user who just wants to chat or run the CLI, start with [GETTING_STARTED.md](GETTING_STARTED.md). If you are **deploying** Stratoclave to your own AWS account, see [DEPLOYMENT.md](DEPLOYMENT.md) first and come back here once the bootstrap admin exists. For the authoritative CLI reference, see [CLI_GUIDE.md](CLI_GUIDE.md).

---

## Contents

1. [The RBAC model](#the-rbac-model)
2. [Logging in as administrator](#logging-in-as-administrator)
3. [Managing users](#managing-users)
4. [Managing tenants](#managing-tenants)
5. [SSO: trusted AWS accounts](#sso-trusted-aws-accounts)
6. [API keys](#api-keys)
7. [Viewing usage](#viewing-usage)
8. [Handing the web URL to CLI users](#handing-the-web-url-to-cli-users)
9. [Locking down after bootstrap](#locking-down-after-bootstrap)
10. [Audit log reference](#audit-log-reference)
11. [Password reset (administrator-assisted)](#password-reset-administrator-assisted)
12. [Troubleshooting](#troubleshooting)

---

## The RBAC model

Stratoclave ships three roles, stored in the `stratoclave-users` DynamoDB table and evaluated by the backend on every request.

| Role        | Scope |
| ----------- | ----- |
| `admin`     | Everything. Manage users, tenants, trusted accounts, SSO invites, API keys, view global usage, set credits. |
| `team_lead` | Manage only tenants they own: invite or remove members, adjust credits, view per-tenant usage. Cannot see other tenants. |
| `user`      | Send messages, view their own usage, rotate their own API keys. Cannot see other users. |

**Tenant isolation.** Every user belongs to exactly one active tenant at a time. A `team_lead` can only see the tenants they own. An `admin` can see all tenants. The `default-org` tenant is seeded automatically on first backend startup and cannot be deleted; it is the fallback for users with no explicit tenant assignment.

**Permissions seeding.** The `admin`, `team_lead`, and `user` permission rows in DynamoDB (`stratoclave-permissions`) are seeded idempotently on backend startup by `bootstrap/seed.py`. Administrators do **not** need to run any DynamoDB scripts manually.

---

## Logging in as administrator

Administrators log in through the web UI, the same as any other user. The bootstrap admin is created by [`scripts/bootstrap-admin.sh`](../scripts/bootstrap-admin.sh); see [DEPLOYMENT.md](DEPLOYMENT.md#post-deploy-first-admin) for how to create one.

1. Open your deployment URL (for example `https://d8b03j8erit4k.cloudfront.net`) in the browser.
2. Enter the admin email and the password printed by `bootstrap-admin.sh`.
3. The header now shows the admin-only navigation items: **Users**, **Tenants**, **Trusted Accounts**, **Usage**.

You can also drive every admin operation from the CLI once you have set `STRATOCLAVE_API_ENDPOINT`:

```bash
export STRATOCLAVE_API_ENDPOINT="https://d8b03j8erit4k.cloudfront.net"   # your deployment URL
stratoclave setup https://d8b03j8erit4k.cloudfront.net
stratoclave auth login --email admin@example.com
stratoclave admin user list
```

> **CLI noun is singular.** The CLI subcommand is `stratoclave admin user ...` and `stratoclave admin tenant ...`. The plural forms `admin users` / `admin tenants` do **not** exist.

---

## Managing users

### Provisioning a new user

The Stratoclave `user create` flow intentionally does **not** issue a first-login password. You create the user record, then set a temporary password on the Cognito side and deliver it to them over a secure channel.

#### From the CLI (recommended)

```bash
# 1. Create the Stratoclave user record.
stratoclave admin user create \
  --email newuser@example.com \
  --role user \
  --tenant default-org

# 2. Set a temporary password in Cognito. `--no-permanent` puts the user into
#    the FORCE_CHANGE_PASSWORD state so their next login requires a new password.
aws cognito-idp admin-set-user-password \
  --user-pool-id us-east-1_EXAMPLE \
  --username newuser@example.com \
  --password 'TempPassword!23' \
  --no-permanent \
  --region us-east-1
```

Hand the temporary password to the user via a password manager share, a 1Password link, or another secure channel. At first login they will be required to set a permanent password.

> **Why two steps?** By default the `admin user create` response does **not** include a `temporary_password` field. The backend sets it to `null` so that creator logs, screenshots, and shell history never contain a reusable credential. If you need the legacy behaviour (the plaintext in the response), set `EXPOSE_TEMPORARY_PASSWORD=true` on the backend ECS task definition before deploying. This is not recommended for production.

#### From the web UI

Go to **Users -> New user**. Fill in:

| Field            | Notes |
| ---------------- | ----- |
| Email            | Used as the Cognito username. Must be unique in the deployment. |
| Role             | `user` or `team_lead`. The `admin` option is disabled unless `ALLOW_ADMIN_CREATION=true` is set on the backend. |
| Tenant           | Leave blank for `default-org`. |
| Credit override  | Leave blank to inherit the tenant's `default_credit`. |

The UI does **not** surface a temporary password either; after submitting the form, follow the same `aws cognito-idp admin-set-user-password --no-permanent` step described above.

### Listing users

```bash
stratoclave admin user list [--role R] [--tenant T] [--limit N]
```

`--limit` defaults to `50`. The command prints a fixed-width table of email, user_id, tenant, roles, total credit, and remaining credit.

### Inspecting a single user

```bash
stratoclave admin user show <user_id>
```

### Moving a user to another tenant

```bash
stratoclave admin user assign-tenant <user_id> \
  --tenant <new_tenant_id> \
  [--new-role user|team_lead] \
  [--total-credit N]
```

The backend:

1. Archives the current `user_tenants` row.
2. Creates a new `user_tenants` row with `status=active`, applying either `--total-credit` or the new tenant's `default_credit`.
3. Updates the Cognito attribute `custom:org_id`.
4. Calls `AdminUserGlobalSignOut` to invalidate all of the user's sessions, forcing them to re-authenticate.

### Adjusting a user's credit

```bash
stratoclave admin user set-credit <user_id> --total N [--reset-used]
```

`--reset-used` zeros `credit_used`, which is useful at the start of a new billing period. The change is immediate; the user's next request is evaluated against the new values.

### Removing a user

```bash
stratoclave admin user delete <user_id>
```

What happens:

1. The user is deleted from Cognito (they can no longer log in).
2. The row in `stratoclave-users` is removed.
3. Rows in `stratoclave-user-tenants` are archived (not deleted), preserving historical membership.
4. `stratoclave-usage-logs` entries are preserved, so audit history remains attributable.

Guardrails:

- Deleting yourself is rejected with HTTP `409`.
- Deleting the last remaining `admin` is rejected with HTTP `409`.

---

## Managing tenants

A **tenant** is an organizational unit that owns a credit pool. Tenants typically correspond to teams, departments, or customer accounts.

### Creating a tenant

```bash
stratoclave admin tenant create --name "Team A" \
  [--team-lead <user_id> | --team-lead-email lead@example.com] \
  [--default-credit N]
```

At most one of `--team-lead` or `--team-lead-email` may be provided. If both are omitted, ownership defaults to the sentinel string `admin-owned`, which means "shared, visible only to admins". The `--team-lead-email` form is resolved to a Cognito sub on the client side via `admin user list`.

### Listing and inspecting tenants

```bash
stratoclave admin tenant list [--limit N]
stratoclave admin tenant show <tenant_id>
stratoclave admin tenant members <tenant_id>
stratoclave admin tenant usage <tenant_id> [--since-days N]
```

Compared to the `team-lead` variant, `admin tenant members` includes `user_id` in the output.

### Reassigning ownership

```bash
stratoclave admin tenant set-owner <tenant_id> \
  [--team-lead <user_id> | --team-lead-email lead@example.com]
```

Critical operation: audit logs capture both actor and previous owner. The previous owner immediately loses visibility into the tenant.

### Archiving a tenant

```bash
stratoclave admin tenant delete <tenant_id>
```

Archiving is soft: the row is flagged `status=archived`, but usage logs and user-tenant history are preserved. The `default-org` tenant cannot be archived. Users who had the tenant as their active tenant are not automatically reassigned; archive only tenants that already have zero active members.

---

## SSO: trusted AWS accounts

Stratoclave accepts federated logins from AWS-native identities: IAM Identity Center users, SAML-federated roles, IAM users, and EC2 instance profiles. You allow-list the AWS accounts that are permitted to federate and optionally specify per-account provisioning rules.

### Supported identity types

| Identity type        | `identity_type`    | Typical source |
| -------------------- | ------------------ | -------------- |
| SSO user             | `sso_user`         | IAM Identity Center (`session_name == email`). |
| Federated role       | `federated_role`   | SAML or enterprise IdP via `AssumeRoleWithSAML`. |
| IAM user             | `iam_user`         | Long-lived IAM user with access keys. |
| EC2 instance profile | `instance_profile` | Role assumed via EC2 instance metadata. |

Only `sso_user` and `federated_role` are accepted by default. `iam_user` and `instance_profile` must be explicitly opted in per trusted account because multiple humans can share them.

### Adding a trusted account

Administered through the web UI (**Trusted Accounts -> Add account**) or by calling `POST /api/mvp/admin/trusted-accounts` directly. A `stratoclave admin trusted-account ...` family of CLI subcommands is not yet available (see [Known limitations](CLI_GUIDE.md#known-limitations)).

| Field                   | Notes |
| ----------------------- | ----- |
| AWS Account ID          | 12-digit account number. |
| Provisioning policy     | `invite_only` (default, safest) or `auto_provision`. |
| Allowed role patterns   | Glob patterns matched against the assumed-role ARN. Empty list means "any role". |
| Allow IAM user          | Off by default. Opt in only for break-glass or automation accounts. |
| Allow instance profile  | Off by default. Strongly discouraged for interactive use. |
| Default tenant / credit | Applied to auto-provisioned users unless an invite overrides it. |

### Invite-only vs. auto-provisioning

- `invite_only` (recommended): no user can log in until an administrator pre-registers their email. Safest for production.
- `auto_provision`: any caller from the account whose session name matches a valid email is created on demand with role `user`. Suitable for internal-only, IdP-backed accounts where every SSO user is trusted.

**Invites always win.** Even under `auto_provision`, if an invite exists for the incoming email, its role, tenant, and credit override the account-level default.

### Enterprise SAML where `session_name != email`

Some enterprise IdPs set the session name to an opaque identifier instead of the user's email. To map these:

1. Collect the session name (visible in CloudTrail after a failed login).
2. Create an invite with `Email = user@example.com` **and** `IAM user name = <session-name>`.
3. The next login from that session provisions a Stratoclave user keyed on the email.

### Disabling a trusted account

Deleting a trusted account also deletes its pending invites. Existing Stratoclave users that were provisioned from the account keep working until you delete them explicitly with `stratoclave admin user delete <user_id>`.

---

## API keys

Stratoclave issues long-lived API keys of the form `sk-stratoclave-...` for machine-to-machine access, CI jobs, and integrations including the bundled CLI and Claude Desktop Cowork.

Scopes carried by a key:

- `messages:send` -- call `POST /v1/messages`.
- `usage:read-self` -- read the owner's own usage.

### Issuing a key for yourself

Web UI: **Account -> API keys -> New key.** Or on the CLI:

```bash
stratoclave api-key create \
  --name "my-ci-key" \
  --scope messages:send \
  --scope usage:read-self \
  --expires-days 30
```

The full secret is shown exactly once. Store it immediately; the backend retains only the SHA-256 hash.

### Issuing a key on behalf of another user

The backend exposes `POST /api/mvp/admin/users/{user_id}/api-keys` for proxy issuance, useful for onboarding a headless service account that will never log in interactively. The audit log records both the actor and the `on_behalf_of` user.

A dedicated CLI subcommand is not yet available; until then, call the HTTP API directly:

```bash
curl -X POST "https://d8b03j8erit4k.cloudfront.net/api/mvp/admin/users/$USER_ID/api-keys" \
  -H "Authorization: Bearer $(jq -r .access_token ~/.stratoclave/mvp_tokens.json)" \
  -H 'Content-Type: application/json' \
  -d '{"name": "pipeline-prod", "scopes": ["messages:send"], "expires_in_days": 90}'
```

Hand the returned plaintext to the user via a secure channel.

### Revoking a key

Revocation is immediate: the next request using the key returns `401 Unauthorized`.

- Self-service: **Account -> API keys -> Revoke**, or `stratoclave api-key revoke <key_hash>`.
- Admin override: **Admin -> API keys -> Revoke any key**, or `stratoclave api-key admin-revoke <key_hash>`.

> **Known limitation.** `stratoclave api-key list` does not currently include `key_hash` in its output, so revoking via the CLI requires the SHA-256 hash to come from elsewhere (the admin Web UI, or a direct HTTP call to `GET /api/mvp/admin/api-keys`). See [CLI_GUIDE.md](CLI_GUIDE.md#known-limitations) for the workaround.

### Viewing all API keys (admin only)

```bash
stratoclave api-key admin-list [--include-revoked]
```

Each row includes the masked `key_id`, the `owner=<user_id>`, and the key's name.

---

## Viewing usage

### Global usage

`stratoclave admin usage show` (or the Admin Usage page in the web UI) queries `stratoclave-usage-logs`:

```bash
stratoclave admin usage show \
  [--tenant T] \
  [--user U] \
  [--since 2026-04-01T00:00:00Z] \
  [--until 2026-04-30T23:59:59Z] \
  [--limit N]
```

The backend picks an index in the order `tenant_id > user_id > full scan`. Always pass `--tenant` or `--user` when possible to avoid table scans.

### Per-tenant summary

```bash
stratoclave admin tenant usage <tenant_id> [--since-days N]
```

The web UI tenant detail page renders the same data as two bar charts (by model, by user) plus a CSV export.

### Per-user summary

Each user detail page in the web UI shows `credit_remaining`, `credit_used`, and a sparkline of the last 30 days of activity. The user list surfaces the same columns for at-a-glance review.

---

## Handing the web URL to CLI users

Once you have deployed and bootstrapped an admin, share the deployment URL with your users:

```bash
stratoclave setup https://d8b03j8erit4k.cloudfront.net    # your deployment URL
export STRATOCLAVE_API_ENDPOINT="https://d8b03j8erit4k.cloudfront.net"
stratoclave auth login --email user@example.com
```

`stratoclave setup` fetches `/.well-known/stratoclave-config` from the backend and writes `~/.stratoclave/config.toml`. Users do **not** need to know the Cognito pool ID or client ID; `setup` discovers them automatically.

---

## Locking down after bootstrap

`scripts/bootstrap-admin.sh` requires the backend to be running with `ALLOW_ADMIN_CREATION=true`. This exposes `POST /api/mvp/admin/users` with `roles=['admin']` to any caller, which is intentional for the zero-state case but **must not remain enabled in production**.

After the first admin can log in, disable the flag:

1. In the environment used to run CDK, set `ALLOW_ADMIN_CREATION=false` (or unset it).
2. Redeploy the ECS stack:

   ```bash
   cd iac && npx cdk deploy <Prefix>EcsStack
   ```

3. (Optional) Force a new task to pick up the environment variable immediately:

   ```bash
   aws ecs update-service \
     --cluster <PREFIX>-cluster \
     --service <PREFIX>-backend \
     --force-new-deployment
   ```

From this point on, new admins can only be promoted by an existing admin via the Web UI.

---

## Audit log reference

The backend emits structured JSON logs to CloudWatch Logs group `/ecs/<prefix>-backend` for every privileged action. Useful CloudWatch Logs Insights query:

```
fields @timestamp, event, actor_email, target_email, tenant_id
| filter event like /^admin_|^user_|^tenant_|^sso_|^trusted_account_|^api_key_|^credit_/
| sort @timestamp desc
```

| Event                                                                           | Emitted by |
| ------------------------------------------------------------------------------- | ---------- |
| `admin_created`, `user_created`, `user_deleted`                                 | `POST /DELETE /api/mvp/admin/users` |
| `tenant_created`, `tenant_updated`, `tenant_archived`, `tenant_owner_changed`   | `/api/mvp/admin/tenants[*]` |
| `user_tenant_switched`, `credit_overwritten`                                    | user mutation endpoints |
| `sso_login_success`, `sso_login_denied`, `sso_user_provisioned`                 | `POST /api/mvp/auth/sso-exchange` |
| `sso_invite_created`, `sso_invite_deleted`                                      | `/api/mvp/admin/sso-invites[*]` |
| `trusted_account_created`, `trusted_account_updated`, `trusted_account_deleted` | `/api/mvp/admin/trusted-accounts[*]` |
| `api_key_created`, `api_key_revoked`                                            | `/api/mvp/{me,admin}/api-keys[*]` |

Every event carries `request_id` (propagated from the ALB header) so you can correlate a UI or CLI action with the exact backend log line.

---

## Password reset (administrator-assisted)

If a user forgets their password, force a reset via the AWS CLI:

```bash
aws cognito-idp admin-set-user-password \
  --user-pool-id <YOUR_USER_POOL_ID> \
  --username 'user@example.com' \
  --password 'TempPassword!23' \
  --no-permanent \
  --region us-east-1
```

`--no-permanent` returns the user to the `FORCE_CHANGE_PASSWORD` state; their next `stratoclave auth login` prompts for a new permanent password.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `stratoclave admin user create` returns an object where `temporary_password` is `null` | This is the intentional default. See [Provisioning a new user](#provisioning-a-new-user) and follow up with `aws cognito-idp admin-set-user-password --no-permanent`. |
| `stratoclave admin user create ... --role admin` returns `403` | `ALLOW_ADMIN_CREATION` is not `true` on the running backend task. Re-enable it for the duration of the bootstrap, then turn it back off. See [Locking down after bootstrap](#locking-down-after-bootstrap). |
| `stratoclave admin users list` returns a `clap` parse error | The noun is singular; run `stratoclave admin user list`. Same for `admin tenant list`. |
| `stratoclave api-key revoke` fails with "key not found" | The command expects the SHA-256 hash, not the masked `sk-stratoclave-XXXX...YYYY` identifier printed by `api-key list`. Use the Admin UI, or call `DELETE /api/mvp/admin/api-keys/{key_hash}` with the hash from `GET /api/mvp/admin/api-keys`. |
| Admin UI tiles look wrong after the initial deploy | Hard-reload with `Cmd+Shift+R` / `Ctrl+Shift+R` to bust CloudFront's cache of the SPA bundle. |
| Admin actions return `403` while other requests succeed | Your user record still has `roles=["user"]`. Use the web UI or call `PATCH /api/mvp/admin/users/{user_id}/roles` to add `admin`. |
| SSO login is rejected with `sso_login_denied` | Inspect the `reason` field in the matching `sso_login_denied` audit event. Common causes: trusted-account entry missing, role pattern mismatch, or identity type not opted in. |

Still stuck? Open an issue at [`littlemex/stratoclave`](https://github.com/littlemex/stratoclave/issues) with the `request_id` from the relevant audit event.

---

## Related documents

- [GETTING_STARTED.md](GETTING_STARTED.md) -- for end users.
- [CLI_GUIDE.md](CLI_GUIDE.md) -- reference for every `stratoclave` subcommand.
- [DEPLOYMENT.md](DEPLOYMENT.md) -- how to stand up a new Stratoclave deployment.
- [ARCHITECTURE.md](ARCHITECTURE.md) -- how the pieces fit together.
- [SECURITY.md](../SECURITY.md) -- reporting vulnerabilities and threat model.
