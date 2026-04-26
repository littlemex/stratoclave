# Deployment Guide

This guide walks you through deploying Stratoclave to your own AWS account. The target audience is a platform engineer comfortable with the AWS CLI, AWS CDK, and a container runtime.

If you only want to **use** an existing Stratoclave deployment, you do not need this guide — go to [GETTING_STARTED.md](./GETTING_STARTED.md) instead.

---

## Table of contents

1. [Prerequisites](#prerequisites)
2. [What gets deployed](#what-gets-deployed)
3. [Quick deploy](#quick-deploy)
4. [Post-deploy: first admin](#post-deploy-first-admin)
5. [Post-deploy: container image](#post-deploy-container-image)
6. [Regional constraints](#regional-constraints)
7. [Cost estimate](#cost-estimate)
8. [Updates and re-deploys](#updates-and-re-deploys)
9. [Local development](#local-development)
10. [Teardown](#teardown)
11. [Troubleshooting](#troubleshooting)

---

## Prerequisites

### AWS account

- An AWS account where you have permission to create VPCs, ECS services, IAM roles, Cognito User Pools, DynamoDB tables, S3 buckets, CloudFront distributions, and ALBs. An `AdministratorAccess` role is the simplest way to get through the first deploy; refine to a least-privilege role for subsequent updates if needed.
- **Amazon Bedrock model access enabled in `us-east-1`** for every model you intend to serve (Claude Opus / Sonnet / Haiku inference profiles). Bedrock access is an account-level opt-in; see the AWS Bedrock console → **Model access**.
- The AWS CDK must be bootstrapped in the target account/region:

  ```bash
  npx cdk bootstrap aws://<ACCOUNT_ID>/us-east-1
  ```

### Local tooling

| Tool                  | Minimum version | Notes                                                              |
| --------------------- | --------------- | ------------------------------------------------------------------ |
| AWS CLI               | v2.15+          | SSO or credentials configured for the target account.              |
| Node.js               | 20 LTS          | Runs CDK v2.                                                       |
| Python                | 3.11+           | Only needed if you rebuild the Backend image locally.              |
| Rust                  | stable (1.75+)  | Only needed to rebuild the `stratoclave` CLI.                      |
| Container runtime     | finch or Docker | finch is recommended on macOS; Docker Desktop works on any OS.     |
| jq                    | any             | Used by several helper scripts.                                    |

Verify:

```bash
aws --version
node --version
python3 --version
docker --version   # or: finch --version
```

---

## What gets deployed

A successful `deploy-all.sh` run provisions **eight CloudFormation stacks** in this order. The prefix is controlled by `STRATOCLAVE_PREFIX` (default: `stratoclave`); the `<Prefix>` placeholder below is the PascalCase form.

| # | Stack                         | Resources                                                                                            | Purpose                                                                                   |
| - | ----------------------------- | ---------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| 1 | `<Prefix>NetworkStack`        | VPC, 2 public subnets, security groups                                                               | Public-subnet-only network (no NAT, minimal cost).                                        |
| 2 | `<Prefix>DynamodbStack`       | 12 DynamoDB tables (`users`, `user-tenants`, `tenants`, `permissions`, `trusted-accounts`, …)        | All persistent state. Billing mode `PAY_PER_REQUEST`.                                     |
| 3 | `<Prefix>EcrStack`            | Private ECR repository                                                                               | Holds the Backend container image.                                                        |
| 4 | `<Prefix>AlbStack`            | Internet-facing ALB, target group, HTTP listener on :80                                              | Public entry point for the Backend.                                                       |
| 5 | `<Prefix>FrontendStack`       | S3 bucket, CloudFront distribution, SPA fallback function                                            | Static Web UI hosting.                                                                    |
| 6 | `<Prefix>CognitoStack`        | User Pool, app client, Hosted UI domain                                                              | Authentication. Imports the CloudFront domain name to wire up callback URLs.              |
| 7 | `<Prefix>EcsStack`            | ECS cluster, Fargate service (1 task), task role, task definition                                    | Backend runtime. All environment variables are injected here.                             |
| 8 | `<Prefix>ConfigStack`         | SSM Parameter Store entries                                                                          | Static runtime values consumed by the Backend and by helper scripts.                      |

A number of archived stacks (RDS, Redis, WAF, CodeBuild, Verified Permissions) live under `iac/lib/_archived/` and are **not** deployed by default — they are kept for reference only.

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
export STRATOCLAVE_PREFIX=stratoclave   # choose a short, lower-kebab-case name

# 2. Install CDK dependencies (first run only).
cd iac && npm install && cd ..

# 3. Deploy all stacks, build the Frontend, and publish to CloudFront.
./iac/scripts/deploy-all.sh
```

The script is idempotent: re-running it after a partial failure picks up where CloudFormation left off. On a cold start, a full deployment takes **15–20 minutes**, dominated by CloudFront distribution creation.

When the script finishes it prints:

```
[SUCCESS] Deployment completed
  Frontend URL:  https://<YOUR_CLOUDFRONT_URL>
  ALB endpoint:  http://<YOUR_ALB_DNS>
  User Pool ID:  us-east-1_EXAMPLE
```

### Options

| Flag              | Effect                                                                           |
| ----------------- | -------------------------------------------------------------------------------- |
| `--dry-run`       | Print the stack order and exit without deploying.                                |
| `--skip-build`    | Deploy CDK stacks only; do not rebuild or upload the Frontend bundle.            |

<!-- TODO(docs): Insert screenshot of a successful deploy-all.sh run showing the summary block -->

### Using finch instead of Docker

The helper scripts call `docker` by name. If you only have finch installed, add a shim to your `PATH`:

```bash
mkdir -p /tmp/docker-shim
cat > /tmp/docker-shim/docker <<'EOF'
#!/usr/bin/env bash
exec finch "$@"
EOF
chmod +x /tmp/docker-shim/docker
export PATH="/tmp/docker-shim:$PATH"
```

Then re-run `./iac/scripts/deploy-all.sh`.

---

## Post-deploy: first admin

`deploy-all.sh` does **not** create any users. To mint the first administrator:

```bash
# The Backend must be reachable and ALLOW_ADMIN_CREATION must be true for this
# one-shot. The CDK default is 'false'; set it in your environment for the
# initial deploy, then flip it back (see "Locking down" below).
export ALLOW_ADMIN_CREATION=true
./iac/scripts/deploy-all.sh        # redeploy so the env var reaches ECS

./scripts/bootstrap-admin.sh --email admin@example.com
```

The script performs three idempotent steps:

1. Create (or reuse) a Cognito user with `email_verified=true`.
2. Set a permanent password (generated, or passed via `--password`).
3. `POST /api/mvp/admin/users` so the Backend writes the `admin` row into DynamoDB.

The default `default-org` tenant and the `admin` / `team_lead` / `user` permission rows are seeded automatically on Backend startup (see `backend/bootstrap/seed.py`). You do **not** need to pre-populate DynamoDB.

Sample output:

```
[INFO] Resolving deployment outputs in region us-east-1...
[INFO] User Pool : us-east-1_EXAMPLE
[INFO] API       : https://<YOUR_CLOUDFRONT_URL>
[INFO] Email     : admin@example.com
[STEP 1/3] Ensuring Cognito user exists
[OK]   Created Cognito user.
[STEP 2/3] Setting permanent password
[OK]   Password set (permanent).
[STEP 3/3] Granting admin role via Backend
[OK]   Admin role granted.
============================================
 Bootstrap complete
============================================
  Email:     admin@example.com
  Password:  <generated-once, 20+ chars>
  Login URL: https://<YOUR_CLOUDFRONT_URL>
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

`deploy-all.sh` deploys infrastructure but **does not build the Backend image** — the ECS service will start with whatever `latest` tag is already in ECR. On the very first deploy the repository is empty, so the ECS tasks will fail health checks until you push an image.

Build and push:

```bash
cd iac
./scripts/build-and-push.sh
```

What it does:

1. Looks up the ECR repository URI from the `<Prefix>EcrStack` outputs.
2. Logs in to ECR (`aws ecr get-login-password`).
3. `docker build -t stratoclave-backend:latest backend/`.
4. Pushes `latest` and a timestamped tag (`YYYYMMDD-HHMMSS`).
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

The primary reason is that Cognito Hosted UI, Bedrock model inference profiles, and the cross-stack reference between the Frontend CloudFront distribution and the Cognito User Pool all have to live in the same region. Support for additional regions (e.g., `ap-northeast-1`, `eu-west-1`) is on the roadmap; contributions welcome.

---

## Cost estimate

Representative monthly spend for a low-traffic single-team deployment in `us-east-1`. All figures are USD and exclude Bedrock token usage (pay-per-token, billed separately by AWS).

| Item                                                   | Monthly |
| ------------------------------------------------------ | ------- |
| ECS Fargate (0.25 vCPU / 0.5 GiB, 1 task)              | ~$12    |
| Application Load Balancer                              | ~$17    |
| CloudFront (~1 GiB egress, first 10 GB free)           | <$1     |
| DynamoDB (PAY_PER_REQUEST, small write volume)         | ~$1     |
| S3 + CloudWatch Logs + SSM Parameter Store             | ~$1     |
| **Subtotal**                                           | **~$30–35** |

The per-request cost of Bedrock is dominant in practice. Track it via the **Usage** tab in the Web UI (tokens per model) and via AWS Cost Explorer's **Bedrock** service filter.

---

## Updates and re-deploys

CDK stacks are designed to be re-applied freely. Typical update flows:

| Change                                        | Command                                                                 |
| --------------------------------------------- | ----------------------------------------------------------------------- |
| Frontend code only                            | `./iac/scripts/deploy-all.sh` (Frontend rebuild is part of the pipeline) |
| Backend code only                             | `./iac/scripts/build-and-push.sh` then `aws ecs update-service --force-new-deployment` |
| IaC changes (new DynamoDB table, env var, …)  | `cd iac && npx cdk diff --all` then `./scripts/deploy-all.sh`           |
| A specific stack                              | `cd iac && npx cdk deploy <Prefix>EcsStack` (etc.)                      |

> **Warning: UserPoolClient replacement.** If `npx cdk diff` shows the Cognito app client being replaced (not updated), stop. A replacement rotates the client ID and immediately invalidates every active user session. The most common cause is re-ordering `callbackUrls`; restore the original order and re-run `diff` before deploying.

### Deprecated helper scripts

The following scripts in `iac/scripts/` are leftovers from earlier iterations and are no longer part of the supported flow. They will be removed in a future release — do not add them to new documentation or runbooks:

- `validate-config.sh`
- `deploy-with-update.sh`
- `cloud-build.sh`

Use `iac/scripts/deploy-all.sh` (infrastructure + Frontend) and `iac/scripts/build-and-push.sh` (Backend image) exclusively.

---

## Local development

### Frontend

```bash
cd frontend
npm install
npm run dev
# Opens http://localhost:3003
```

Vite's dev server proxies `/api/*` and `/v1/*` to the deployed ALB, so you do **not** need a local Backend running to iterate on UI code. The Cognito callback URL `http://localhost:3003/callback` is pre-registered in `iac/lib/cognito-stack.ts`; logging in through Hosted UI redirects back to your dev server transparently.

### Backend

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

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

### Backend environment variables (reference)

Injected automatically by `iac/bin/iac.ts` when you deploy; listed here so you can reproduce them locally or diagnose missing-value bugs.

| Variable                                 | Required in production? | Set by CDK?         |
| ---------------------------------------- | ----------------------- | ------------------- |
| `ENVIRONMENT`                            | yes                     | yes (`production`)  |
| `COGNITO_USER_POOL_ID`                   | yes                     | yes                 |
| `COGNITO_CLIENT_ID`                      | yes                     | yes                 |
| `OIDC_ISSUER_URL`                        | yes                     | yes                 |
| `OIDC_AUDIENCE`                          | yes                     | yes (equals client ID) |
| `DYNAMODB_USERS_TABLE`                   | yes                     | yes                 |
| `DYNAMODB_USER_TENANTS_TABLE`            | yes                     | yes                 |
| `DYNAMODB_USAGE_LOGS_TABLE`              | yes                     | yes                 |
| `DYNAMODB_TENANTS_TABLE`                 | yes                     | yes                 |
| `DYNAMODB_PERMISSIONS_TABLE`             | yes                     | yes                 |
| `DYNAMODB_TRUSTED_ACCOUNTS_TABLE`        | yes                     | yes                 |
| `DYNAMODB_SSO_PRE_REGISTRATIONS_TABLE`   | yes                     | yes                 |
| `DYNAMODB_API_KEYS_TABLE`                | yes                     | yes                 |
| `CORS_ORIGINS`                           | yes (no `localhost`!)   | yes                 |
| `BEDROCK_REGION`                         | yes                     | yes                 |
| `DEFAULT_BEDROCK_MODEL`                  | yes                     | yes                 |
| `ALLOW_ADMIN_CREATION`                   | bootstrap only          | yes (default `false`) |

If the Backend refuses to start and the CloudWatch log says `environment variable X is required`, compare the task definition's `environment` array against this list.

---

## Teardown

```bash
cd iac
npx cdk destroy --all --profile "$AWS_PROFILE"
```

Things that **remain** after `cdk destroy` and need manual cleanup if you want a truly empty account:

- **CloudWatch Logs** groups (retention is set but not deleted with the stack).
- **S3 buckets** marked `RemovalPolicy.RETAIN` (the Frontend bucket is `DESTROY`; review the stack to confirm).
- **The Cognito domain prefix** is unusable for **24 hours** after deletion. Redeploying immediately with the same `STRATOCLAVE_PREFIX` will fail — either wait a day or pick a new prefix.
- **ECR images** inside the repository (the stack deletes the repository; if you disabled that, purge images first).

---

## Troubleshooting

### `CDK_DEFAULT_REGION must be "us-east-1"`

You forgot to export one of the three region variables, or your `~/.aws/config` default overrides them. Export all three:

```bash
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
export CDK_DEFAULT_REGION=us-east-1
```

### `[seed_bootstrap_failed]` in Backend logs on startup

Usually harmless on first start — the Backend tries to seed `default-org` and the permission rows before the DynamoDB tables are fully `ACTIVE`. The seed is idempotent and retries on every startup; the next task should succeed. If the message persists across several restarts, confirm the task role has `dynamodb:PutItem`, `dynamodb:GetItem`, and `dynamodb:Query` on the `permissions` and `tenants` tables.

### ECS task keeps restarting (`STOPPED (Task failed ELB health checks)`)

1. Check `/ecs/<prefix>-backend` CloudWatch Logs for Python stack traces at startup.
2. `curl http://<YOUR_ALB_DNS>/health` — should return `{"status": "healthy"}`. If it 502s, the task never bound to port 8000.
3. Confirm the task role can call Bedrock (`bedrock:InvokeModel`). A missing permission shows up as a 500 on any model call but not at startup.

### ALB returns 503 `Service Unavailable`

No healthy targets. Either the ECS task has not come up yet (give it 2–3 minutes after `build-and-push.sh`) or its health check is failing — see the previous item.

### Frontend shows a red "Configuration Error: config.json missing" screen

The S3 bucket is missing `/config.json`, or CloudFront is serving a stale cached `index.html` referencing old bundles. Re-run:

```bash
./iac/scripts/deploy-all.sh        # rebuilds dist/config.json from stack outputs
aws cloudfront create-invalidation \
  --distribution-id <YOUR_DISTRIBUTION_ID> \
  --paths '/*'
```

### `stratoclave setup <url>` returns 404

The URL points at a Backend that predates the `/.well-known/stratoclave-config` endpoint. Pull the latest `main`, rebuild and push the Backend image, and force a new ECS deployment.

### `bootstrap-admin.sh` returns `401 / 403` from the Backend

`ALLOW_ADMIN_CREATION` is not `true` in the running task definition. Export `ALLOW_ADMIN_CREATION=true`, redeploy the ECS stack, wait for the new task to go `RUNNING`, then re-run `bootstrap-admin.sh`. Remember to turn the flag off again afterward (see [Locking down](#locking-down-after-bootstrap)).

### `bootstrap-admin.sh` fails with `UsernameExistsException`

A Cognito user already exists for that email. The script treats this as a soft success on step 1 and proceeds; if you see the raw error it came from a different code path. Delete the stale user and retry:

```bash
aws cognito-idp admin-delete-user \
  --user-pool-id <YOUR_USER_POOL_ID> \
  --username 'admin@example.com' \
  --region us-east-1
```

### CDK diff proposes to replace the UserPoolClient

See the warning under [Updates and re-deploys](#updates-and-re-deploys). Do not deploy until the diff is an *update*, not a replacement.

### Cognito domain prefix already exists

Either someone else in AWS already claimed your prefix or you recently destroyed a Stratoclave deployment with the same prefix. Wait 24 hours or choose a new `STRATOCLAVE_PREFIX`.

---

## Related documents

- [GETTING_STARTED.md](./GETTING_STARTED.md) — end-user onboarding.
- [ADMIN_GUIDE.md](./ADMIN_GUIDE.md) — managing the running deployment.
- [ARCHITECTURE.md](./ARCHITECTURE.md) — stack internals and design rationale.
- [CONTRIBUTING.md](../CONTRIBUTING.md) — development workflow and PR process.
- [SECURITY.md](../SECURITY.md) — vulnerability reporting.
