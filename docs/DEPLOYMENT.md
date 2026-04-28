<!-- Last updated: 2026-04-27 -->
<!-- Applies to: Stratoclave main @ 48b9533 (or later) -->

# Deployment Guide

> A Japanese translation is available at [ja/DEPLOYMENT.md](./ja/DEPLOYMENT.md).

This guide walks you through deploying Stratoclave into your own AWS account. The target audience is a platform engineer comfortable with the AWS CLI, AWS CDK v2, and a container runtime.

If you only want to use an existing Stratoclave deployment, you do not need this guide; go to [GETTING_STARTED.md](GETTING_STARTED.md) instead.

---

## Contents

1. [Prerequisites](#prerequisites)
2. [What gets deployed](#what-gets-deployed)
3. [Quick deploy](#quick-deploy)
4. [Post-deploy: first admin](#post-deploy-first-admin)
5. [Post-deploy: container image](#post-deploy-container-image)
6. [Regional constraints](#regional-constraints)
7. [Cost estimate](#cost-estimate)
8. [Updates and re-deploys](#updates-and-re-deploys)
9. [Local development](#local-development)
10. [Backend environment variables](#backend-environment-variables)
11. [Teardown](#teardown)
12. [Troubleshooting](#troubleshooting)

---

## Prerequisites

### AWS account

- An AWS account where you have permission to create VPCs, ECS services, IAM roles, Cognito User Pools, DynamoDB tables, S3 buckets, CloudFront distributions, and ALBs. `AdministratorAccess` is the simplest way to get through the first deploy; refine to a least-privilege role for subsequent updates if needed.
- **Amazon Bedrock model access enabled in `us-east-1`** for every model you intend to serve (Claude Opus / Sonnet / Haiku inference profiles). Bedrock access is an account-level opt-in; see the Bedrock console -> **Model access**.
- The AWS CDK v2 must be bootstrapped in the target account and region **before your first `cdk deploy`**. It is not required to run `cdk synth` for inspection:

  ```bash
  npx cdk bootstrap aws://<ACCOUNT_ID>/us-east-1
  ```

### Local tooling

| Tool              | Minimum version | Notes |
| ----------------- | --------------- | ----- |
| AWS CLI           | v2.15+          | SSO or credentials configured for the target account. |
| Node.js           | 20 LTS          | Runs CDK v2. |
| Python            | 3.11+           | Needed to run the backend tests or rebuild the backend image locally. The system Python on recent macOS is 3.9; install a newer one with `brew install python@3.12` or via `pyenv install 3.11.9`. |
| Rust              | 1.75+           | Only needed to rebuild the `stratoclave` CLI. A cold `cargo build --release` takes ~2 minutes on Apple Silicon and compiles ~500 crates — not a hang. |
| Docker            | any recent      | The helper scripts shell out to `docker` by name to build the backend image. Docker Desktop works on macOS / Windows / Linux. |
| jq                | any             | Used by several helper scripts. |

Verify:

```bash
aws --version
node --version
python3 --version   # must print 3.11 or newer
docker --version
```

### Repository

```bash
git clone https://github.com/littlemex/stratoclave.git
cd stratoclave
```

---

## What gets deployed

A successful `deploy-all.sh` run provisions **nine CloudFormation stacks** in dependency order. The stack name prefix is controlled by `STRATOCLAVE_PREFIX` (default: `stratoclave`). `<Prefix>` below is the PascalCase form.

| # | Stack                 | Resources                                                             | Purpose |
| - | --------------------- | --------------------------------------------------------------------- | ------- |
| 1 | `<Prefix>NetworkStack`  | VPC, 2 public subnets, SGs, VPC Flow Logs (CloudWatch, 30 d)          | Public-subnet-only network (no NAT). ALB SG inbound is restricted to the AWS-managed **CloudFront origin-facing prefix list** — direct ALB DNS probes fail at L4. |
| 2 | `<Prefix>DynamodbStack` | 13 DynamoDB tables incl. `users`, `user-tenants`, `tenants`, `usage-logs` (RETAIN), `api-keys` (RETAIN + PITR), **`sso-nonces`** (TTL, for Vouch-by-STS replay defence) | All persistent state, `PAY_PER_REQUEST`. Audit-critical tables survive `cdk destroy`. |
| 3 | `<Prefix>EcrStack`      | Private ECR repository (RETAIN)                                       | Holds the backend container image. Rollback surface of last resort. |
| 4 | `<Prefix>AlbStack`      | Internet-facing ALB, target group, HTTP listener on :80, `deletionProtection=true` in production | Public entry point for the backend; listeners use `open:false` so no 0.0.0.0/0 ingress is punched on the ALB SG. |
| 5 | `<Prefix>WafStack`      | **WAFv2 WebACL** (CLOUDFRONT scope, `us-east-1`): CommonRuleSet, KnownBadInputs, IpReputation, rate-based (5-minute window, per-IP), optional IPSet allowlist | Opt-out with `ENABLE_WAF=false`. ARN is cross-stack referenced by FrontendStack. |
| 6 | `<Prefix>FrontendStack` | S3 bucket (private, SSL-enforced), CloudFront distribution (**OAC**, minTLS 1.2_2021, ResponseHeadersPolicy with HSTS 730 d + strict CSP + `frame-ancestors 'none'`), SPA fallback function | Static web UI hosting. Uses **Origin Access Control** (not legacy OAI) — S3 bucket policy is scoped by `aws:SourceArn`. |
| 7 | `<Prefix>CognitoStack`  | User Pool, app client, hosted-UI domain                               | Authentication. Imports the CloudFront domain to wire up callback URLs. |
| 8 | `<Prefix>EcsStack`      | ECS cluster, Fargate service (1 task), task role, task definition     | Backend runtime. All environment variables are injected here. |
| 9 | `<Prefix>ConfigStack`   | SSM Parameter Store entries                                           | Static runtime values consumed by the backend and helper scripts. |

Several archived stacks (RDS, Redis, CodeBuild, Verified Permissions) live under `iac/lib/_archived/` and are **not** deployed by default. They are kept for reference. The previous `WafStack` archive has been superseded by the live CLOUDFRONT-scope WAF described above.

### Synth-time security checks (cdk-nag)

Every `cdk synth` / `cdk deploy` runs the `AwsSolutionsChecks` Aspect from `cdk-nag`. Known, documented tradeoffs (default CloudFront cert, managed-prefix-list ingress, non-ENFORCED Cognito advanced security, etc.) are suppressed in `iac/bin/iac.ts` with rationale comments; any **new** `[Error at ...]` from cdk-nag will fail `cdk synth`. Set `CDK_NAG=off` only for the rare local debugging session.

---

## Quick deploy

From the repository root:

```bash
# 1. Configure credentials and environment.
export AWS_PROFILE=your-profile
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
export CDK_DEFAULT_REGION=us-east-1
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export STRATOCLAVE_PREFIX=stratoclave    # short, lower-kebab-case

# 2. Install CDK dependencies (first run only).
cd iac && npm install && cd ..

# 3. Deploy all stacks, build the frontend, and publish to CloudFront.
./iac/scripts/deploy-all.sh
```

The script is idempotent; re-running it after a partial failure picks up where CloudFormation left off. On a cold start, a full deployment takes **15 to 20 minutes**, dominated by CloudFront distribution creation.

When the script finishes it prints:

```
[SUCCESS] Deployment completed
  Frontend URL:  https://<your-deployment>.cloudfront.net          # example: your deployment URL
  ALB endpoint:  http://stratoclave-alb-xxxxxxxx.us-east-1.elb.amazonaws.com
  User Pool ID:  us-east-1_EXAMPLE
```

### Options

| Flag             | Effect |
| ---------------- | ------ |
| `--dry-run`      | Print the stack order and exit without deploying. |
| `--skip-build`   | Deploy CDK stacks only; do not rebuild or upload the frontend bundle. |

---

## Post-deploy: first admin

`deploy-all.sh` does **not** create any users. To mint the first administrator:

```bash
# The backend must be reachable and ALLOW_ADMIN_CREATION must be true for this
# one-shot. The CDK default is 'false'; set it in your environment for the
# initial deploy, then flip it back (see "Locking down" below).
export ALLOW_ADMIN_CREATION=true
./iac/scripts/deploy-all.sh        # redeploy so the env var reaches ECS

./scripts/bootstrap-admin.sh --email admin@example.com
```

The script performs three idempotent steps:

1. Create (or reuse) a Cognito user with `email_verified=true`.
2. Set a permanent password (generated, or passed via `--password`).
3. `POST /api/mvp/admin/users` so the backend writes the `admin` row into DynamoDB.

The default `default-org` tenant and the `admin` / `team_lead` / `user` permission rows are seeded automatically on backend startup (see `backend/bootstrap/seed.py`). You do **not** need to pre-populate DynamoDB.

Sample output:

```
[INFO] Resolving deployment outputs in region us-east-1...
[INFO] User Pool : us-east-1_EXAMPLE
[INFO] API       : https://<your-deployment>.cloudfront.net
[INFO] Email     : admin@example.com
[STEP 1/3] Ensuring Cognito user exists
[OK]   Created Cognito user.
[STEP 2/3] Setting permanent password
[OK]   Password set (permanent).
[STEP 3/3] Granting admin role via backend
[OK]   Admin role granted.
============================================
 Bootstrap complete
============================================
  Email:     admin@example.com
  Password:  <generated-once, 20+ chars>
  Login URL: https://<your-deployment>.cloudfront.net
```

### Locking down after bootstrap

Once the admin can log in, **turn the bootstrap endpoint off**:

1. In the environment you use to run CDK, unset `ALLOW_ADMIN_CREATION` (or set it to `false`).
2. Redeploy:

   ```bash
   cd iac && npx cdk deploy <Prefix>EcsStack
   ```

3. Verify the task picked it up:

   ```bash
   aws ecs describe-task-definition \
     --task-definition <PREFIX>-backend \
     --query 'taskDefinition.containerDefinitions[0].environment[?name==`ALLOW_ADMIN_CREATION`]'
   ```

All subsequent admin promotions must go through the authenticated API.

---

## Post-deploy: container image

`deploy-all.sh` deploys infrastructure but **does not build the backend image**. The ECS service will start with whatever `latest` tag is already in ECR. On the very first deploy the repository is empty, so the ECS tasks will fail their health checks until you push an image.

Build and push:

```bash
cd iac
./scripts/build-and-push.sh
```

What it does:

1. Looks up the ECR repository URI from the `<Prefix>EcrStack` outputs.
2. Logs in to ECR (`aws ecr get-login-password`).
3. `docker build -t stratoclave-backend:latest backend/`.
4. Pushes `latest` plus a timestamped tag (`YYYYMMDD-HHMMSS`).
5. Prints the full image URI.

Force the ECS service to pick up the new image:

```bash
aws ecs update-service \
  --cluster <PREFIX>-cluster \
  --service <PREFIX>-backend \
  --force-new-deployment
```

---

## Regional constraints

Stratoclave currently requires **`us-east-1`**. This is enforced at the top of `iac/bin/iac.ts`:

```ts
if (cdkRegion !== 'us-east-1') {
  throw new Error('CDK_DEFAULT_REGION must be "us-east-1" for Stratoclave ...');
}
```

The primary reason is that Cognito hosted UI, Bedrock model inference profiles, and the cross-stack reference between the Frontend CloudFront distribution and the Cognito User Pool all have to live in the same region. Support for additional regions (for example `ap-northeast-1`, `eu-west-1`) is on the roadmap; contributions welcome.

---

## Cost estimate

Representative monthly spend for a low-traffic single-team deployment in `us-east-1`. Figures are USD and **exclude Bedrock token usage**, which is billed separately by AWS.

| Item                                              | Monthly |
| ------------------------------------------------- | ------- |
| ECS Fargate (0.25 vCPU / 0.5 GiB, 1 task)         | ~$12 |
| Application Load Balancer                         | ~$17 |
| CloudFront (~1 GiB egress, first 10 GB free)      | <$1 |
| WAFv2 (5 rules + WebACL association)              | ~$10 to $12 (fixed) |
| VPC Flow Logs → CloudWatch (30 d retention)       | ~$1 |
| DynamoDB (PAY_PER_REQUEST, small write volume)    | ~$1 |
| S3 + CloudWatch Logs + SSM Parameter Store        | ~$1 |
| **Subtotal**                                      | **~$42 to $45** |

Set `ENABLE_WAF=false` to drop the WAF line item (~$10) for throwaway / sandbox stacks.

The per-request cost of Bedrock dominates in practice. Track it from the Admin Usage page in the web UI (tokens per model) and via AWS Cost Explorer's **Bedrock** service filter.

---

## Updates and re-deploys

CDK stacks are designed to be re-applied freely. Typical update flows:

| Change                                           | Command |
| ------------------------------------------------ | ------- |
| Frontend code only                               | `./iac/scripts/deploy-all.sh` (the frontend rebuild is part of the pipeline). |
| Backend code only                                | `./iac/scripts/build-and-push.sh`, then `aws ecs update-service --force-new-deployment`. |
| IaC changes (new DynamoDB table, env var, ...)    | `cd iac && npx cdk diff --all`, then `./scripts/deploy-all.sh`. |
| A specific stack                                 | `cd iac && npx cdk deploy <Prefix>EcsStack` (or another stack name). |

> **Warning: UserPoolClient replacement.** If `npx cdk diff` shows the Cognito app client being replaced (not updated), stop. A replacement rotates the client ID and immediately invalidates every active user session. The most common cause is re-ordering `callbackUrls`; restore the original order and re-run `diff` before deploying.

### Deprecated helper scripts

The following scripts in `iac/scripts/` are leftovers from earlier iterations and are no longer part of the default flow. They will be removed in a future release; do not add them to new documentation or runbooks:

- `validate-config.sh`
- `deploy-with-update.sh`

Use `iac/scripts/deploy-all.sh` (infrastructure + frontend) and `iac/scripts/build-and-push.sh` (backend image) for ordinary deployments. `iac/scripts/cloud-build.sh` is an optional remote build driver kept for environments where the local Docker socket is unavailable; it is safe to ignore unless you explicitly need it.

---

## Local development

> **Note.** Stratoclave does not ship a self-contained local development stack. Cognito, DynamoDB, Bedrock, and CloudFront have no offline equivalents; the supported workflow for iterating on the frontend or backend is to point your local process at a real deployment in AWS. Deploy once with `./iac/scripts/deploy-all.sh`, then develop against the live backing services.

### Frontend

```bash
cd frontend
npm ci
npm run dev
# Opens http://localhost:3003
```

Vite's dev server proxies `/api/*` and `/v1/*` to the deployed ALB, so you do **not** need a local backend to iterate on UI code. The Cognito callback URL `http://localhost:3003/callback` is pre-registered in `iac/lib/cognito-stack.ts`; logging in through the hosted UI redirects back to your dev server transparently.

### Backend

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
# requirements-dev.txt also pulls in pytest, moto, joserfc, ruff so the same
# venv can run the test suite. The production Docker image installs
# requirements.txt only.
pip install -r requirements-dev.txt

ENVIRONMENT=development \
AWS_REGION=us-east-1 \
COGNITO_USER_POOL_ID=$(aws cloudformation describe-stacks --stack-name <Prefix>CognitoStack --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' --output text) \
COGNITO_CLIENT_ID=$(aws cloudformation describe-stacks --stack-name <Prefix>CognitoStack --query 'Stacks[0].Outputs[?OutputKey==`UserPoolClientId`].OutputValue' --output text) \
OIDC_ISSUER_URL=$(aws cloudformation describe-stacks --stack-name <Prefix>CognitoStack --query 'Stacks[0].Outputs[?OutputKey==`OidcIssuerUrl`].OutputValue' --output text) \
OIDC_AUDIENCE=$COGNITO_CLIENT_ID \
DYNAMODB_USERS_TABLE=<PREFIX>-users \
DYNAMODB_USER_TENANTS_TABLE=<PREFIX>-user-tenants \
DYNAMODB_USAGE_LOGS_TABLE=<PREFIX>-usage-logs \
DYNAMODB_TENANTS_TABLE=<PREFIX>-tenants \
DYNAMODB_PERMISSIONS_TABLE=<PREFIX>-permissions \
DYNAMODB_TRUSTED_ACCOUNTS_TABLE=<PREFIX>-trusted-accounts \
DYNAMODB_SSO_PRE_REGISTRATIONS_TABLE=<PREFIX>-sso-pre-registrations \
DYNAMODB_API_KEYS_TABLE=<PREFIX>-api-keys \
CORS_ORIGINS=http://localhost:3003 \
uvicorn main:app --reload --port 8000
```

Then point the Vite proxy at `http://localhost:8000` in `frontend/vite.config.ts` to exercise the full stack against your AWS resources.

### CLI

```bash
cd cli
cargo build --release
./target/release/stratoclave --help
```

See [CLI_GUIDE.md](CLI_GUIDE.md) for the full usage.

---

## Backend environment variables

Injected automatically by `iac/bin/iac.ts` when you deploy; listed here so you can reproduce them locally or diagnose missing-value bugs.

| Variable                                 | Required in production? | Set by CDK? | Notes |
| ---------------------------------------- | ----------------------- | ----------- | ----- |
| `ENVIRONMENT`                            | yes                     | yes (`production`) | |
| `COGNITO_USER_POOL_ID`                   | yes                     | yes         | |
| `COGNITO_CLIENT_ID`                      | yes                     | yes         | |
| `COGNITO_DOMAIN`                         | yes                     | yes         | Full hosted-UI URL. |
| `COGNITO_REGION`                         | yes                     | yes         | |
| `OIDC_ISSUER_URL`                        | yes                     | yes         | |
| `OIDC_AUDIENCE`                          | yes                     | yes (equals client ID) | |
| `BEDROCK_REGION`                         | yes                     | yes         | |
| `DEFAULT_BEDROCK_MODEL`                  | yes                     | yes         | |
| `STRATOCLAVE_API_ENDPOINT`               | yes                     | yes         | Published in `/.well-known/stratoclave-config`. |
| `STRATOCLAVE_PREFIX`                     | yes                     | yes (`stratoclave`) | DynamoDB, Secrets, and SSM key prefix. |
| `DYNAMODB_USERS_TABLE`                   | yes                     | yes         | |
| `DYNAMODB_USER_TENANTS_TABLE`            | yes                     | yes         | |
| `DYNAMODB_USAGE_LOGS_TABLE`              | yes                     | yes         | |
| `DYNAMODB_TENANTS_TABLE`                 | yes                     | yes         | |
| `DYNAMODB_PERMISSIONS_TABLE`             | yes                     | yes         | |
| `DYNAMODB_TRUSTED_ACCOUNTS_TABLE`        | yes                     | yes         | |
| `DYNAMODB_SSO_PRE_REGISTRATIONS_TABLE`   | yes                     | yes         | |
| `DYNAMODB_API_KEYS_TABLE`                | yes                     | yes         | |
| `CORS_ORIGINS`                           | yes (no `localhost`)    | yes         | Must match the CloudFront domain in production. Dev/local backends can set `http://localhost:3003`. |
| `DYNAMODB_SSO_NONCES_TABLE`              | optional                | yes (default `stratoclave-sso-nonces`) | Vouch-by-STS replay defence (`backend/dynamo/sso_nonces.py`). If the table is missing, the backend logs a warning and falls back to the ±5 minute skew check only. |
| `ENABLE_WAF`                             | IaC-only                | n/a         | CDK-side flag (read in `iac/bin/iac.ts`). `false` skips provisioning `<Prefix>WafStack`. Default: `true`. |
| `WAF_RATE_LIMIT_PER_5MIN`                | IaC-only                | n/a         | Per-IP request cap in the rate-based rule. Default: `300`. |
| `WAF_IP_ALLOWLIST_ENABLED`               | IaC-only                | n/a         | Enable SSM-parameter-backed IPSet allowlist. Default: `false`. |
| `CDK_NAG`                                | IaC-only                | n/a         | Set to `off` to skip the cdk-nag synth-time aspect. Default: `on`. |
| `STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL`      | optional                | no          | If set, the backend auto-provisions this email as admin on first startup when no admin exists. Idempotent. |
| `ALLOW_ADMIN_CREATION`                   | bootstrap only          | yes (default `false`) | See [Locking down after bootstrap](#locking-down-after-bootstrap). |
| `EXPOSE_TEMPORARY_PASSWORD`              | optional                | no          | If `true`, `admin user create` returns the one-time password in the response. Default `false` (response field is `null`). Not recommended for production. |

If the backend refuses to start and the CloudWatch log says `environment variable X is required`, compare the task definition's `environment` array against this list.

---

## Teardown

```bash
cd iac
npx cdk destroy --all --profile "$AWS_PROFILE"
```

> **Destructive.** `cdk destroy --all` removes every Stratoclave CloudFormation stack in the target account. All Stratoclave DynamoDB tables are deleted, so every user, tenant, API key, and usage log is lost. Only run this if you truly want to wipe the deployment.

Things that **remain** after `cdk destroy` and need manual cleanup if you want a truly empty account:

- **CloudWatch Logs** groups (retention is set but the groups are not deleted with the stacks).
- **S3 buckets** marked `RemovalPolicy.RETAIN`. The frontend bucket is `DESTROY`; review the stack to confirm if you have customised it.
- **The Cognito domain prefix** is unusable for **24 hours** after deletion. Redeploying immediately with the same `STRATOCLAVE_PREFIX` will fail; either wait a day or choose a new prefix.
- **ECR images** inside the repository. The stack deletes the repository by default; if you disabled that, purge images first.

To also uninstall the client-side CLI, see [GETTING_STARTED.md -> Uninstall](GETTING_STARTED.md#uninstall).

---

## Troubleshooting

### `CDK_DEFAULT_REGION must be "us-east-1"`

You forgot to export one of the three region variables, or your `~/.aws/config` default overrides them. Export all three:

```bash
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
export CDK_DEFAULT_REGION=us-east-1
```

### `[seed_bootstrap_failed]` in backend logs on startup

Usually harmless on first start. The backend tries to seed `default-org` and the permission rows before the DynamoDB tables are fully `ACTIVE`. The seed is idempotent and retries on every startup; the next task should succeed. If the message persists across several restarts, confirm the task role has `dynamodb:PutItem`, `dynamodb:GetItem`, and `dynamodb:Query` on the `permissions` and `tenants` tables.

### ECS task keeps restarting (`STOPPED (Task failed ELB health checks)`)

1. Check `/ecs/<prefix>-backend` in CloudWatch Logs for Python stack traces at startup.
2. `curl http://<YOUR_ALB_DNS>/health` should return `{"status": "healthy"}`. If it 502s, the task never bound to port 8000.
3. Confirm the task role can call Bedrock (`bedrock:InvokeModel`). A missing permission surfaces as a 500 on any model call but not at startup.

### ALB returns 503 `Service Unavailable`

No healthy targets. Either the ECS task has not come up yet (give it 2 to 3 minutes after `build-and-push.sh`) or its health check is failing; see the previous item.

### Frontend shows a red "Configuration Error: config.json missing" screen

The S3 bucket is missing `/config.json`, or CloudFront is serving a stale cached `index.html` referencing old bundles. Re-run:

```bash
./iac/scripts/deploy-all.sh        # rebuilds dist/config.json from stack outputs
aws cloudfront create-invalidation \
  --distribution-id <YOUR_DISTRIBUTION_ID> \
  --paths '/*'
```

### `stratoclave setup <url>` returns 404

The URL points at a backend that predates the `/.well-known/stratoclave-config` endpoint. Pull the latest `main` from [`littlemex/stratoclave`](https://github.com/littlemex/stratoclave), rebuild and push the backend image, and force a new ECS deployment.

### `bootstrap-admin.sh` returns `401 / 403` from the backend

`ALLOW_ADMIN_CREATION` is not `true` in the running task definition. Export `ALLOW_ADMIN_CREATION=true`, redeploy the ECS stack, wait for the new task to go `RUNNING`, then re-run `bootstrap-admin.sh`. Turn the flag off again afterwards; see [Locking down after bootstrap](#locking-down-after-bootstrap).

### `bootstrap-admin.sh` fails with `UsernameExistsException`

A Cognito user already exists for that email. The script treats this as a soft success on step 1 and proceeds; if you see the raw error it came from a different code path. Delete the stale user and retry:

```bash
aws cognito-idp admin-delete-user \
  --user-pool-id <YOUR_USER_POOL_ID> \
  --username 'admin@example.com' \
  --region us-east-1
```

### CDK diff proposes to replace the UserPoolClient

See the warning under [Updates and re-deploys](#updates-and-re-deploys). Do not deploy until the diff is an *update*, not a *replacement*.

### Cognito domain prefix already exists

Either someone else in AWS has already claimed your prefix, or you recently destroyed a Stratoclave deployment with the same prefix. Wait 24 hours or choose a new `STRATOCLAVE_PREFIX`.

---

## Related documents

- [GETTING_STARTED.md](GETTING_STARTED.md) -- end-user onboarding.
- [ADMIN_GUIDE.md](ADMIN_GUIDE.md) -- managing the running deployment.
- [ARCHITECTURE.md](ARCHITECTURE.md) -- stack internals and design rationale.
- [CONTRIBUTING.md](../CONTRIBUTING.md) -- development workflow and PR process.
- [SECURITY.md](../SECURITY.md) -- vulnerability reporting.
