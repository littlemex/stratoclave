<!-- Last updated: 2026-07-10 -->
<!-- Applies to: Stratoclave main @ 48b9533 (or later) -->

# Deployment Guide

This guide walks you through deploying Stratoclave into your own AWS account. The target audience is a platform engineer comfortable with the AWS CLI, AWS CDK v2, and a container runtime.

If you only want to use an existing Stratoclave deployment, you do not need this guide; go to [GETTING_STARTED.md](GETTING_STARTED.md) instead.

---

## Contents

1. [Prerequisites](#prerequisites)
2. [What gets deployed](#what-gets-deployed)
3. [Quick deploy](#quick-deploy)
4. [Post-deploy: first admin](#post-deploy-first-admin)
5. [Post-deploy: container image](#post-deploy-container-image)
6. [Post-deploy: quota gated stacks](#post-deploy-quota-gated-stacks)
7. [Regional constraints](#regional-constraints)
8. [Private entry (PrivateLink), headless](#private-entry-privatelink-headless)
9. [Cost estimate](#cost-estimate)
10. [Updates and re-deploys](#updates-and-re-deploys)
11. [Local development](#local-development)
12. [Binding AWS sign-in to this gateway](#binding-aws-sign-in-to-this-gateway)
13. [Rate limits on sign-in routes](#rate-limits-on-sign-in-routes)
14. [Backend environment variables](#backend-environment-variables)
15. [Teardown](#teardown)
16. [Troubleshooting](#troubleshooting)

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
| 2 | `<Prefix>DynamodbStack` | 23 DynamoDB tables incl. `users`, `user-tenants`, `tenants`, `usage-logs` (RETAIN; PITR in production only), `api-keys` (RETAIN + PITR always), `credit-ledger` and `quota-events` (RETAIN), **`sso-nonces`** (TTL, for Vouch-by-STS replay defence), `ui-tickets` (TTL, CLI→SPA handoff) | All persistent state, `PAY_PER_REQUEST`. Audit tables (`api-keys`, `usage-logs`, `credit-ledger`, `quota-events`) survive `cdk destroy`; see [Teardown](#teardown). |
| 3 | `<Prefix>EcrStack`      | Private ECR repository (RETAIN)                                       | Holds the backend container image. Rollback surface of last resort. |
| 4 | `<Prefix>AlbStack`      | Internet-facing ALB, target group, HTTP listener on :80, `deletionProtection=true` in production | Public entry point for the backend; listeners use `open:false` so no 0.0.0.0/0 ingress is punched on the ALB SG. |
| 5 | `<Prefix>WafStack`      | **WAFv2 WebACL** (CLOUDFRONT scope, `us-east-1`): CommonRuleSet, KnownBadInputs, IpReputation, rate-based (5-minute window, per-IP), optional IPSet allowlist | Opt-out with `ENABLE_WAF=false`. ARN is cross-stack referenced by FrontendStack. |
| 6 | `<Prefix>FrontendStack` | S3 bucket (private, SSL-enforced), CloudFront distribution (**OAC**, minTLS 1.2_2021, ResponseHeadersPolicy with HSTS 730 d + preload + strict CSP + `frame-ancestors 'none'`), SPA fallback function | Static web UI hosting. Uses **Origin Access Control** (not legacy OAI) — S3 bucket policy is scoped by both `aws:SourceArn` **and** `aws:SourceAccount`. API responses (`/api/*`, `/v1/*`) carry HSTS set by the backend middleware (365 d, no preload). |
| 7 | `<Prefix>CognitoStack`  | User Pool, app client, hosted-UI domain                               | Authentication. Imports the CloudFront domain to wire up callback URLs. |
| 8 | `<Prefix>EcsStack`      | ECS cluster, Fargate service (1 task), task role, task definition     | Backend runtime. All environment variables are injected here. |
| 9 | `<Prefix>ConfigStack`   | SSM Parameter Store entries                                           | Static runtime values consumed by the backend and helper scripts. |

Several archived stacks (RDS, Redis, CodeBuild, Verified Permissions) live under `iac/lib/_archived/` and are **not** deployed by default. They are kept for reference. The previous `WafStack` archive has been superseded by the live CLOUDFRONT-scope WAF described above.

The nine-stack table above is `STRATOCLAVE_ENTRY=cloudfront`, the default. `STRATOCLAVE_ENTRY=private` builds a different, fresh deployment with no `WafStack` and no `FrontendStack` at all, and an internal (not internet-facing) `AlbStack`; in their place it adds a `<Prefix>PrivateLinkStack` (internal NLB + VPC endpoint service). See [Private entry (PrivateLink), headless](#private-entry-privatelink-headless).

### Synth-time security checks (cdk-nag)

Every `cdk synth` / `cdk deploy` runs the `AwsSolutionsChecks` Aspect from `cdk-nag`. Known, documented tradeoffs (default CloudFront cert, managed-prefix-list ingress, non-ENFORCED Cognito advanced security, etc.) are suppressed in `iac/bin/iac.ts` with rationale comments; any **new** `[Error at ...]` from cdk-nag will fail `cdk synth`. Set `CDK_NAG=off` only for the rare local debugging session.

### Security headers: two-layer architecture

Security headers are applied by **two independent layers**; each layer controls a separate traffic path:

| Layer | Applies to | HSTS | CSP |
|-------|-----------|------|-----|
| CloudFront `ResponseHeadersPolicy` (`iac/lib/frontend-stack.ts`) | SPA responses (default cache behaviour, S3 origin) | `max-age=63072000` (730 d) + `includeSubDomains` + `preload` | Permissive SPA policy: `script-src 'self'`, `connect-src 'self' https: wss:`, Google Fonts allowed |
| Backend `SecurityHeadersMiddleware` (`backend/main.py`) | API responses (`/api/*`, `/v1/*`, `/openai/*`) | `max-age=31536000` (365 d) + `includeSubDomains`, **no preload** | Restrictive API policy: `script-src 'none'`, `connect-src 'self'`, `object-src 'none'` |

CloudFront does **not** attach the `ResponseHeadersPolicy` to the API or `.well-known` cache behaviours; those responses flow straight from the backend unchanged, so the backend middleware is the sole source of security headers for API paths.

### Logging and audit retention

| Log source | Retention | Notes |
|-----------|-----------|-------|
| ECS backend logs (`/ecs/<prefix>-backend`, CloudWatch) | **90 days** (`THREE_MONTHS`) | Covers most SOC2/ISO27001 audit windows. `RemovalPolicy.RETAIN` — survives `cdk destroy`. |
| VPC Flow Logs (CloudWatch) | **30 days** | Provisioned by `NetworkStack`. |

### Network egress restrictions

The ECS task security group (`iac/lib/network-stack.ts`) allows outbound traffic only on **port 443 (HTTPS)** and **port 53 (DNS)**. Arbitrary outbound TCP is blocked; a compromised container cannot exfiltrate data over non-standard ports.

### Request body limit

The backend enforces a hard cap of **2 MB** (`REQUEST_MAX_BODY_BYTES`, default `2 * 1024 * 1024`) via an ASGI-layer middleware (`MaxBodySizeASGIMiddleware` in `backend/main.py`). Requests exceeding the cap are rejected with HTTP `413` before the body is buffered. The cap applies to `POST`, `PUT`, and `PATCH` requests and works correctly for both `Content-Length` and chunked transfers.

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
# STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL is read once at backend STARTUP, not at
# request time, and only when no admin-role user exists yet -- so this is a
# one-shot: after the first admin is created, leave the variable set (it is a
# no-op from then on) or unset it, either is safe.
export STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL=admin@example.com
cd iac && npx cdk deploy "${STRATOCLAVE_PREFIX:-stratoclave}-ecs" --require-approval never
```

On the next container start, `backend/bootstrap/seed.py::seed_bootstrap_admin` runs three idempotent steps against the real Cognito user pool and DynamoDB tables:

1. `AdminCreateUser` for the email (or adopt the existing Cognito user on `UsernameExistsException`).
2. `AdminSetUserPassword` with a random 20+ character permanent password.
3. Write the `Users` + `UserTenants` rows with `roles=["admin", "user"]`.

The password is **never** written to stdout/stderr (and therefore never reaches CloudWatch Logs); it is stashed once in AWS Secrets Manager:

```bash
aws secretsmanager get-secret-value \
  --secret-id "${STRATOCLAVE_PREFIX:-stratoclave}/bootstrap-admin-temp-password" \
  --query SecretString --output text
# {"email": "admin@example.com", "password": "<generated-once, 20+ chars>"}
```

Retrieve it once, log in, then delete the secret (or let its 7-day recovery window expire):

```bash
aws secretsmanager delete-secret \
  --secret-id "${STRATOCLAVE_PREFIX:-stratoclave}/bootstrap-admin-temp-password" \
  --force-delete-without-recovery
```

The default `default-org` tenant and the `admin` / `team_lead` / `user` permission rows are seeded automatically on backend startup (see `backend/bootstrap/seed.py`). You do **not** need to pre-populate DynamoDB.

> **`ALLOW_ADMIN_CREATION` and `scripts/bootstrap-admin.sh` are stale.** They
> describe an older contract in which `POST /api/mvp/admin/users` had an
> unauthenticated bootstrap path. It no longer does: the endpoint is gated by
> `require_permission("users:create")` unconditionally (`backend/mvp/admin_users.py`),
> so `bootstrap-admin.sh`'s un-authenticated step 3 now fails outright with
> `401 {"detail":"Missing bearer token"}` on a real deployment, before the
> `ALLOW_ADMIN_CREATION` gate is ever reached. `ALLOW_ADMIN_CREATION` /
> `ALLOW_ADMIN_CREATION_UNTIL` remain real and load-bearing for a DIFFERENT
> case -- an already-authenticated admin promoting a second user to `admin`
> through the same endpoint -- just not for minting the first one. Verified
> against a real deployment on 2026-09-02: the documented `ALLOW_ADMIN_CREATION`
> + `bootstrap-admin.sh` path 401s as described above; `STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL`
> is the path that actually works.

All subsequent admin promotions go through the authenticated `POST /api/mvp/admin/users` (an existing admin, with `ALLOW_ADMIN_CREATION=true` and, in production, `ALLOW_ADMIN_CREATION_UNTIL` set to a future epoch).

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
3. Builds `backend/` for `linux/amd64` (override with `PLATFORM`), so an arm64 build host still produces an image Fargate can run.
4. Pushes a build tag, `YYYYMMDD-HHMMSS-<commit>` (with `-dirty` when the working tree has uncommitted changes; set `BUILD_TAG` to choose another), and then moves `latest` to it. The repository's tags are immutable, so the build tag is pushed first and `latest` is best-effort: a build whose `latest` could not move still leaves its build tag in ECR.
5. Prints the deploy command for that build tag.

Deploy the build by its tag:

```bash
cd iac
IMAGE_TAG=<build tag printed above> npx cdk deploy "${STRATOCLAVE_PREFIX:-stratoclave}-ecs"
```

The ECS task definition pins the tag it was synthesised with. Pushing an image therefore changes nothing that is running, and `aws ecs update-service --force-new-deployment` only restarts the image already pinned. A `cdk deploy` of the ECS stack without `IMAGE_TAG` pins `latest`.

The same deploy also re-applies every other setting the ECS stack reads from the environment, including the task size and scaling (`BACKEND_TASK_CPU`, `BACKEND_TASK_MEMORY`, `BACKEND_MIN_TASKS`, `BACKEND_MAX_TASKS`, `BACKEND_REQUESTS_PER_TARGET`, `BACKEND_CONCURRENCY_TARGET`), which fall back to their defaults when unset. Pass the values the running deployment was deployed with, and read `npx cdk diff "${STRATOCLAVE_PREFIX:-stratoclave}-ecs"` before deploying: the only change it should show is the image.

---

## Post-deploy: quota gated stacks

Two more stacks stay off until a second, explicit `cdk deploy` names them: `<Prefix>QuotaReconcilerStack` and `<Prefix>QuotaGrantsStack`. This is a required second pass after the container image step above, not an optional extra for later — skip it and the deployment is missing the two jobs that keep a tenant's spending pool honest over time. What that looks like in practice is below.

Both stacks run scheduled Lambda functions built from `backend/Dockerfile.lambda`, a different image from the one ECS runs (it bundles the AWS Lambda Runtime Interface Client). `iac/scripts/build-and-push.sh` builds and pushes this image right after the backend image and prints the tag to export:

```bash
cd iac
./scripts/build-and-push.sh
# prints: export LAMBDA_IMAGE_TAG=lambda-YYYYMMDD-HHMMSS

export LAMBDA_IMAGE_TAG=lambda-YYYYMMDD-HHMMSS   # the tag it just printed
npx cdk deploy "${STRATOCLAVE_PREFIX:-stratoclave}-quota-reconciler" \
               "${STRATOCLAVE_PREFIX:-stratoclave}-quota-grants" \
  -c quotaReconciler=true -c quotaGrants=true \
  --require-approval never
```

`LAMBDA_IMAGE_TAG` has to differ from the backend's own `IMAGE_TAG` (default `latest`); `cdk synth` refuses to proceed otherwise, because the two images share one ECR repository and a shared tag would let a push meant for one silently retag the other.

### What skipping this pass looks like

Not deploying `<Prefix>QuotaGrantsStack` leaves the sweeper that revokes a time-bounded limit raise absent. An admin who raises a tenant's ceiling for a day gets exactly that: the pool row keeps the raised figure past the day's end with nothing walking it back down, so a raise an admin thought was temporary reads as a permanent increase on the tenant's pool until somebody notices.

Not deploying `<Prefix>QuotaReconcilerStack` leaves the monthly period rollover absent. A tenant's pool row is keyed to the calendar month; on the 1st, a tenant whose membership did not change that month gets no new row for the new period, so its pool disappears for the whole month until an operator notices and reruns the rollover by hand.

### Confirming the Lambdas ran

Each function logs to its own CloudWatch Logs group, created by the stack rather than left to appear on first invocation:

```bash
aws logs tail "/lambda/${STRATOCLAVE_PREFIX:-stratoclave}-quota-grant-sweeper" --since 1h
aws logs tail "/lambda/${STRATOCLAVE_PREFIX:-stratoclave}-quota-reconciler" --since 1h
aws logs tail "/lambda/${STRATOCLAVE_PREFIX:-stratoclave}-quota-period-rollover" --since 1h
```

A clean run of the sweeper logs a `sweeper_ran` line on its five-minute schedule; the reconciler and the period rollover log once a day. If a group is empty after the schedule's own interval has passed, the function has not fired yet — re-check the `-c quotaReconciler=true -c quotaGrants=true` deploy above actually completed (`aws cloudformation describe-stacks --stack-name <Prefix>-quota-grants`) and that `LAMBDA_IMAGE_TAG` pointed at a real image (`aws ecr describe-images --repository-name <Prefix>-backend --image-ids imageTag=<tag>`).

---

## Upgrade step: idempotency records

Deployments that ran a version before the one carrying
`scripts/local/backfill_idemp_partition.py` stored external-authorize idempotency
records inside each period's money partition, which made a client key's identity
expire with the billing period. Records are written to a permanent per-tenant
partition now, and the reader still checks the old locations — but only for the
period it was handed and the one before it, because nothing tells it which older
period a given record was written in.

Run the backfill once per ledger table after deploying:

```bash
python3 scripts/local/backfill_idemp_partition.py --dry-run
python3 scripts/local/backfill_idemp_partition.py
```

It copies each old record into the permanent partition, conditional on the target
not existing, so re-running is safe and a record written by the new code is never
overwritten by an older copy. Sources are left in place because the ledger table is
append-only. Until it has run, a retry of a PRE-UPGRADE key more than one period
after its first use can mint a second authorization for that key — the boundary is
stated in [`docs/design/CONTRACTS.md`](design/CONTRACTS.md) under C5.1.

---

## Regional constraints

Stratoclave deploys to an operator-chosen **body region** `R`. Only one piece is
pinned: the **WAF stack** must live in `us-east-1`, because AWS requires
CLOUDFRONT-scope WAFv2 WebACLs to be created there. Everything else — Network,
DynamoDB, ECR, ALB, Frontend (S3 + the global CloudFront distribution), Cognito,
ECS, and BackendConfig — deploys to `R`. CloudFront itself is global and uses the
default `*.cloudfront.net` certificate (no custom ACM), so WAF is the *only*
genuine `us-east-1` dependency.

Set the body region with `STRATOCLAVE_REGION` (falls back to `CDK_DEFAULT_REGION`,
then `us-east-1`):

```bash
export STRATOCLAVE_REGION=eu-west-1          # body region R
export BEDROCK_PRIMARY_REGION=us-east-1      # required when R != us-east-1 (see below)
```

- **When `R == us-east-1`** (the default) nothing changes: all stacks live in one
  region, no cross-region references are created, and existing deployments see no
  functional diff. (One benign exception: the SPA's CSP `form-action` header now
  lists only `*.auth.<R>.amazoncognito.com` instead of the old hardcoded
  us-east-1/us-west-2 pair — a ResponseHeadersPolicy update with no behavior
  change for a us-east-1 Cognito.)
- **When `R != us-east-1`** the WAF stack stays in `us-east-1` and its WebACL ARN
  is consumed by the CloudFront distribution via CDK `crossRegionReferences`. You
  must `cdk bootstrap` **both** `R` and `us-east-1` (the deploy script checks this
  and fails fast otherwise). GovCloud (`us-gov-*`) and China (`cn-*`) partitions
  are rejected at synth — CloudFront does not exist there.

### Bedrock model region is independent of the deploy region

The region Stratoclave *runs in* (`R`) is decoupled from the region it *calls
Bedrock in*. The Claude model primary region is `BEDROCK_PRIMARY_REGION`; when `R
!= us-east-1` it must be set explicitly (Stratoclave refuses to guess — an unset
value would otherwise silently fall back to the task's `AWS_REGION` = `R`, which
may not host the model). Cross-region streaming failover is
`STRATOCLAVE_FAILOVER_REGIONS` (comma-separated; `disabled`/`none`/`off`/empty =
single-region).

The **OpenAI/codex path is different**: its call region is hardwired per-model in
the backend registry (`backend/mvp/models.py` — currently `us-west-2` /
`us-east-2`) and **cannot be relocated by an environment variable**.
`OPENAI_BEDROCK_REGIONS` is only a display hint surfaced to the CLI; it does not
move the actual call. The only residency lever for codex is therefore
`STRATOCLAVE_CODEX_ENABLED=false`.

### Data-residency recipe (e.g. EU-only prompts)

Residency here means **strict single-region** (exact region equality — a region
prefix like `eu` is *not* treated as one jurisdiction, since `eu-west-2` is the
UK and `eu-central-2` is Switzerland). To keep every prompt byte in one region,
**all** of the following must line up — pinning only the deploy region is *not*
sufficient, because failover and the codex path otherwise reach US regions:

```bash
export STRATOCLAVE_REGION=eu-west-1            # gateway + state in the EU
export BEDROCK_PRIMARY_REGION=eu-west-1        # Claude calls stay in eu-west-1
export STRATOCLAVE_FAILOVER_REGIONS=disabled   # no cross-region failover (default reaches us-west-2)
export STRATOCLAVE_CODEX_ENABLED=false                     # OpenAI/codex is hardwired to us-west-2/us-east-2 — disable it
export DEFAULT_BEDROCK_MODEL=anthropic.claude-…  # a DIRECTLY-HOSTED (non-geo) model id — see below
export STRATOCLAVE_RESIDENCY=strict            # hard-fail synth if ANY Bedrock region != the deploy region
```

`STRATOCLAVE_RESIDENCY=strict` turns the residency check into a synth error;
`warn` (or leaving it unset) only emits a `cdk synth` warning — and the warning
fires only when `R` is non-default or `STRATOCLAVE_RESIDENCY` is set, so the plain
us-east-1 default stays quiet (backward compatible). Note that
`STRATOCLAVE_RESIDENCY=strict` on a **us-east-1** deploy will itself fail, because
the default failover set reaches `eu-west-1` — that is intended (strict means
strict); set `STRATOCLAVE_FAILOVER_REGIONS=disabled` too.

**Geo inference profiles break single-region residency.** A model id prefixed
`us.` / `eu.` / `apac.` / `jp.` / `au.` / `ca.` / `global.` (e.g. the default
`us.anthropic.claude-opus-4-7`) is a *geography* cross-region inference profile —
AWS routes inference to any region within that geography at its discretion, so
no single-region guarantee is possible. Under `STRATOCLAVE_RESIDENCY=strict` a
geo-profile model is **refused at synth**; you must set `DEFAULT_BEDROCK_MODEL` to
a directly-hosted, region-specific model id (no geo prefix). If geography-level
residency (rather than single-region) is acceptable, set
`STRATOCLAVE_ALLOW_GEO_INFERENCE=true` to downgrade the refusal to a warning. Also
verify the model is actually available in the target region:
`aws bedrock list-foundation-models --region eu-west-1`.

### Keeping inference inside a set of regions

`STRATOCLAVE_RESIDENCY` (above) only ever warns, or hard-fails `cdk synth`, on a
leak the app itself would reach — it is a synth-time check, not an AWS-enforced
boundary. `STRATOCLAVE_INFERENCE_REGIONS` is a different, stronger mechanism: a
comma-separated allowlist of AWS regions Bedrock inference may run in, enforced
by AWS IAM itself, not merely checked at synth. The two mechanisms address the
same concern and cannot both be set — pick one.

```bash
# Japan: keep every Bedrock call inside Tokyo / Osaka.
export STRATOCLAVE_INFERENCE_REGIONS=ap-northeast-1,ap-northeast-3
export BEDROCK_PRIMARY_REGION=ap-northeast-1       # must be inside the boundary
export DEFAULT_BEDROCK_MODEL=jp.anthropic.claude-opus-4-7   # a jp.-bounded profile; see below
export STRATOCLAVE_CODEX_ENABLED=false             # see below

# US: keep every Bedrock call inside the three Claude US regions.
export STRATOCLAVE_INFERENCE_REGIONS=us-east-1,us-east-2,us-west-2
export BEDROCK_PRIMARY_REGION=us-east-1
export DEFAULT_BEDROCK_MODEL=us.anthropic.claude-opus-4-7
```

`DEFAULT_BEDROCK_MODEL` must be set explicitly under a boundary. At synth it is
looked up in the bundled model registry (or the file `STRATOCLAVE_MODEL_REGISTRY_PATH`
names): its recorded destination regions must lie inside the boundary, not be
"any region" (a `global.` profile), and have been recorded for calls from
`BEDROCK_PRIMARY_REGION`. An id the file does not contain is accepted with a
warning, because it may be a model activated from the console; the gateway checks
it again at startup and refuses to start if it cannot be served.

What is enforced, and where:

- **The gateway's model listing** (what `/v1/models` offers) is filtered by the
  backend container, which reads the same `STRATOCLAVE_INFERENCE_REGIONS` value
  from its own environment and serves only the models whose destination
  region(s) all lie inside it.
- **AWS IAM** independently refuses anything outside the boundary. The ECS task
  role's Bedrock permissions are generated from
  `STRATOCLAVE_INFERENCE_REGIONS`: every `foundation-model` / `inference-profile`
  resource is scoped to one ARN per region in the boundary (instead of a
  wildcard region), and an explicit `Deny` is added on the region-less
  foundation-model ARN. Calling a geo inference profile authorizes
  `bedrock:InvokeModel` on EVERY destination region's foundation-model ARN up
  front, so the per-destination-region grants above are what gate a geo
  profile's reachable destinations, and the region-less `Deny` closes the one
  shape (no region at all) none of those grants ever produces — the shape a
  `global.*` profile's destination call is authorized against, which is what
  makes `global.*` unreachable under a boundary. So a boundary is enforced
  twice, independently: even a backend defect that served a model outside the
  boundary would still have its Bedrock call refused by AWS. (Measured
  2026-11-14 against the real Bedrock API; see `docs/design/CONTRACTS.md`,
  C11.)
- **Models activated from the console before this release carry no recorded
  destinations.** The gateway's model listing (above) can only filter a model
  against the boundary using a destination it actually has on record; a model
  that was promoted/activated through the console before this release has no
  such record and is therefore left out of the listing under a boundary until
  it is re-activated.
- **BEDROCK_PRIMARY_REGION decides which bundled entries can be served.** The
  bundled Claude entries are recorded for calls from `us-east-1` (`us.`
  profiles) or `ap-northeast-1` (`jp.` profiles); `nvidia.nemotron-super-3-120b`
  and `qwen.qwen3-next-80b-a3b` are recorded for `us-east-1` only — a boundary
  whose `BEDROCK_PRIMARY_REGION` is not `us-east-1` leaves them out of the
  gateway's model listing entirely, regardless of which other regions are in
  `STRATOCLAVE_INFERENCE_REGIONS`. The IAM boundary itself still grants a
  foundation-model ARN for these ids in every region in
  `STRATOCLAVE_INFERENCE_REGIONS`, so an unused grant in a region a given
  entry is not actually recorded for is harmless, not a security gap.
- **Codex (the OpenAI/xAI path) must be disabled for a Japan boundary, but may
  stay on for a US boundary.** OpenAI GPT and xAI models on Bedrock have no
  Japan-bounded cross-region profile — only `us.` and `global.` — and the
  codex path's call region is hardwired in the backend registry to
  `us-east-2` / `us-west-2` regardless of this variable. A boundary that
  excludes either of those two regions and leaves codex enabled fails
  `cdk synth` outright, naming `STRATOCLAVE_CODEX_ENABLED=false` as the fix;
  a US boundary that contains both `us-east-2` and `us-west-2` (as in the
  recipe above) needs no such change.
- **Same-id cross-region failover is off.** `STRATOCLAVE_FAILOVER_REGIONS` is
  ignored once a boundary is set (the backend turns same-id streaming failover
  off under a boundary) — a geo inference profile already fails over inside its
  own geography, and same-id failover would otherwise be the one path that could
  still cross the boundary. Setting it anyway only produces a `cdk synth`
  warning, not an error.
- **The boundary covers Bedrock inference only.** It says nothing about where
  the gateway's own compute, logs, or CloudFront distribution run — those follow
  `STRATOCLAVE_REGION` / the WAF's fixed `us-east-1`, as always. A deploy region
  outside the boundary produces a `cdk synth` warning naming this explicitly.
- **The effective boundary is always named** in a `cdk synth` / `cdk diff`
  informational annotation ("none" when unset), so redeploying from a shell that
  forgot to export `STRATOCLAVE_INFERENCE_REGIONS` is visible as the boundary
  silently widening back to "none", rather than invisible.
- **The boundary has a region-count ceiling.** Every region added repeats
  roughly 15 Bedrock ARNs across this task role's statements, and all of this
  role's statements share one IAM inline-policy size quota (10,240 characters
  per role). `cdk synth` fails fast, naming the character count, once the
  Bedrock statements alone would use more than 8,000 of that budget — a
  boundary that size needs fewer regions, since this stack has no separate
  managed policy to move the Bedrock grants onto today.

To audit what actually ran, Bedrock model-invocation logging (when turned on)
records the destination region per call as `inferenceRegion`. This CloudWatch
Logs Insights query lists any invocation whose `inferenceRegion` fell outside a
Japan boundary:

```
fields @timestamp, modelId, inferenceRegion
| filter inferenceRegion not in ["ap-northeast-1", "ap-northeast-3"]
| stats count() by modelId, inferenceRegion
```

An empty result is the expected state: the IAM boundary above denies anything
outside it before it would ever reach invocation logging. Treat ANY row this
query returns as the alarm it is — it means a call to a region outside the
intended boundary actually succeeded, which should not be reachable while the
IAM boundary is in place; investigate immediately rather than assuming it is
benign.

---

## Private entry (PrivateLink), headless

By default Stratoclave's only entry point is CloudFront, which cannot be made
private. `STRATOCLAVE_ENTRY=private` is a different, fresh deployment shape
with no internet-facing path to the gateway API: the gateway's API is
reachable only through AWS PrivateLink, from VPCs an operator has explicitly
approved. It is for a client that has no route to the public internet at
all, or for an operator whose compliance posture forbids an internet-facing
path to the API. There is no web console on this path and no CloudFront
distribution, S3 bucket, or CLOUDFRONT-scope WAF — this mode is API and CLI
only. This is narrower than "no internet-facing entry point at all" for the
whole deployment: see [What this path does not
provide](#what-this-path-does-not-provide) for what stays reachable from the
internet regardless (the Cognito user pool) and what else is not provided on
this path.

`STRATOCLAVE_ENTRY` is read once at synth and decides which stacks get built;
switching an existing deployment between `cloudfront` and `private` is not
supported — `iac/scripts/deploy-all.sh` refuses it outright at its
pre-flight check, because the ALB would change from internet-facing to
internal in place, which CloudFormation does by replacing the load balancer
and would take a live deployment down rather than failing cleanly. Deploy
`private` under a fresh `STRATOCLAVE_PREFIX`.

`iac/scripts/deploy-all.sh` supports both entry modes: with
`STRATOCLAVE_ENTRY=private` set, it deploys the same stacks `cdk deploy --all`
would and skips the frontend build/upload/invalidation step entirely (there
is no S3 bucket or CloudFront distribution in this mode to put anything
into). `STRATOCLAVE_ENTRY=private` on its own is not enough to synth — the
three `STRATOCLAVE_PRIVATE_*` variables below are required alongside it, or
synth refuses outright naming whichever is missing. The exact environment to
set, with illustrative values in place of your own certificate, domain, and
consumer account, then the same deploy command as `cloudfront` mode:

```bash
export STRATOCLAVE_ENTRY=private
export STRATOCLAVE_PRIVATE_CERTIFICATE_ARN=arn:aws:acm:us-east-1:111111111111:certificate/00000000-0000-0000-0000-000000000000
export STRATOCLAVE_PRIVATE_DOMAIN=stratoclave.internal.example.com
export STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS=arn:aws:iam::222222222222:root
./iac/scripts/deploy-all.sh
```

`STRATOCLAVE_PRIVATE_CERTIFICATE_ARN` and `STRATOCLAVE_PRIVATE_DOMAIN` are
yours to supply: the private ALB terminates HTTPS with your certificate on
port 443 only, and opens no port 80 at all, not even a redirect — the first
request on that port would already have sent a key in cleartext before any
redirect could run. `STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS` is a
comma-separated list of IAM principal ARNs (an account root, a role, or a
user) allowed to request a connection to the endpoint service; it must be
non-empty and must never contain a wildcard, which `cdk synth` refuses
outright. `STRATOCLAVE_ENTRY=both` is named but refused at synth for now: as
specified it would add reach without removing any — a key meant to stay
private would still work from the internet through CloudFront — and it needs
a second internal ALB and a way to tell which entry a request arrived on,
neither of which this slice builds.

### What gets built

An internal Application Load Balancer, carrying only the HTTPS :443 listener
described above, sits behind an internal Network Load Balancer with its own
security group. The NLB's TCP :443 listener forwards to an ALB-type target
group pointed at the internal ALB — this is the only shape AWS PrivateLink's
endpoint service configuration accepts; it takes a network or gateway load
balancer, never an ALB directly. A VPC endpoint service publishes the NLB,
with connection acceptance required and the allowed-principals list above.
The internal ALB's own security group admits only the NLB's security group,
on port 443; nothing else, inside the VPC or outside it, can reach the ALB.

Use `./iac/scripts/deploy-all.sh` (shown above), not a direct `npx cdk deploy
--all`: the script's pre-flight is what refuses an in-place entry-mode
switch (see "Switching an existing deployment" above) — a direct `cdk
deploy` has no such guard and will attempt the switch, replacing the ALB in
place. The script prints the stack outputs you need:

```
Outputs:
stratoclave-private-link.EndpointServiceName = com.amazonaws.vpce.us-east-1.vpce-svc-00000000000000000
stratoclave-private-link.PrivateUrl = https://stratoclave.internal.example.com
stratoclave-alb.AlbDnsName = stratoclave-alb-0000000000.us-east-1.elb.amazonaws.com
```

(Illustrative values above; your actual output names the real endpoint
service, the domain you set, and the ALB's own DNS name — which is an
output of the `<Prefix>-alb` stack, not `<Prefix>-private-link`; the latter
deliberately does not repeat it under a second name.)

### The operator's side

Everything below happens outside this repository, in each consumer account
(to create an endpoint) and in the provider account (to accept it) — in that
order, since there is nothing for the provider to accept until a consumer has
asked. The commands below use illustrative ids; substitute your own consumer
VPC/subnet/security-group ids, pending connection id, and region.

1. In each consumer VPC, create an interface endpoint pointed at the service
   name from the `EndpointServiceName` output:

   ```bash
   aws ec2 create-vpc-endpoint --region us-east-1 --vpc-endpoint-type Interface --vpc-id vpc-00000000000000000 --subnet-ids subnet-00000000000000000 --security-group-ids sg-00000000000000000 --service-name com.amazonaws.vpce.us-east-1.vpce-svc-00000000000000000
   ```

   This creates the endpoint in a `pendingAcceptance` state until the next step runs.

2. In the provider account, review and accept the pending connection:

   ```bash
   aws ec2 describe-vpc-endpoint-connections --region us-east-1
   aws ec2 accept-vpc-endpoint-connections --region us-east-1 --service-id vpce-svc-00000000000000000 --vpc-endpoint-ids vpce-00000000000000000
   ```

3. Point `STRATOCLAVE_PRIVATE_DOMAIN` at the interface endpoint from inside
   the consumer VPC, with a private hosted zone (not a public record, since
   the endpoint's DNS name only resolves inside VPCs that have the endpoint).
   The consumer VPC needs `enableDnsSupport` and `enableDnsHostnames` both
   on for this to resolve at all. The hosted zone name here is exactly
   `STRATOCLAVE_PRIVATE_DOMAIN` — the record you add sits at the zone apex,
   where Route 53 does not allow a CNAME, so this must be an alias record,
   not a CNAME:

   ```bash
   aws route53 create-hosted-zone --name stratoclave.internal.example.com --vpc VPCRegion=us-east-1,VPCId=vpc-00000000000000000 --caller-reference stratoclave-private-dns-2026-11-14
   ```

   Read the endpoint's own regional DNS entry (its hosted-zone id, needed
   for the alias target, is a property of the ENDPOINT, not something you
   choose):

   ```bash
   aws ec2 describe-vpc-endpoints --vpc-endpoint-ids vpce-00000000000000000 --query 'VpcEndpoints[0].DnsEntries[0]'
   ```

   Then create the alias record in the hosted zone from step 3, using that
   entry's `HostedZoneId` and `DnsName`:

   ```bash
   aws route53 change-resource-record-sets --hosted-zone-id Z00000000000000000000 --change-batch '{"Changes":[{"Action":"UPSERT","ResourceRecordSet":{"Name":"stratoclave.internal.example.com","Type":"A","AliasTarget":{"HostedZoneId":"<DnsEntries[0].HostedZoneId from the previous command>","DNSName":"<DnsEntries[0].DnsName from the previous command>","EvaluateTargetHealth":false}}}]}'
   ```

4. Revoking access is two separate actions, and doing only one of them
   leaves the other half of the door open:

   - An **already-accepted** connection keeps working until the provider
     explicitly rejects it. Removing the principal from
     `STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS` and redeploying the
     `private-link` stack only stops a NEW connection request from that
     principal — it does nothing to a connection already accepted, which
     stays connected indefinitely. To actually disconnect it:

     ```bash
     aws ec2 reject-vpc-endpoint-connections --region us-east-1 --service-id vpce-svc-00000000000000000 --vpc-endpoint-ids vpce-00000000000000000
     ```

   - Then remove the principal from `STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS`
     and redeploy the `private-link` stack, so the same principal cannot
     simply reconnect. Do both; either one alone is a half-revocation.

### Gaps a first-time PrivateLink setup will hit

Synth cannot check any of these; they are entirely on the operator:

- **Certificate.** `STRATOCLAVE_PRIVATE_CERTIFICATE_ARN` must cover
  `STRATOCLAVE_PRIVATE_DOMAIN` — synth only checks the ARN's region, never
  the certificate's own domain names. A public ACM certificate for a
  domain you do not control publicly still needs DNS validation in a
  public hosted zone for that domain (ACM validates ownership there even
  though the deployment itself is private); a certificate issued by a
  Private CA instead needs every client to be configured to trust that CA,
  which a public ACM certificate does not require.
- **Region and AZ.** The consumer's interface endpoint must be created in
  the SAME region as this deployment, and in subnets whose Availability
  Zone **ids** (`use1-az1`, not the account-relative name `us-east-1a`)
  overlap the provider's two AZs — AZ names are mapped to physical AZs
  per-account, so "us-east-1a" in the consumer's account is not
  necessarily the same physical AZ as "us-east-1a" in the provider's.
  `aws ec2 describe-availability-zones --region us-east-1` (run in each
  account) shows the id-to-name mapping for that account.
- **Endpoint security group.** The `--security-group-ids` group passed to
  `create-vpc-endpoint` above must itself allow inbound 443 from whatever
  will call the endpoint in the consumer VPC — the endpoint's own security
  group is a real gate the consumer's network team owns, separate from
  everything this deployment's own security groups control.
- **Bootstrapping is a chicken-and-egg the operator hits first.** The
  gateway's own VPC has no path to its own NLB other than through
  PrivateLink (the NLB's security group admits nothing else at all, not
  even from inside that VPC) — so testing the connection, and every step
  under "Bootstrapping without a console" below, only works from a host
  that is itself inside an ACCEPTED consumer VPC. In practice this means
  the operator's own account (or whichever jump host they intend to
  administer from) must be one of the principals in
  `STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS` and must go through steps 1-2
  above for itself, before anyone can bootstrap the first admin.

### Sign-in options on this path

API keys (`sk-stratoclave-*`) need no sign-in step at all.

Vouch-by-STS: the CLI signs a `GetCallerIdentity` request **locally** —
SigV4 signing is a cryptographic operation over credentials the CLI already
holds, not a network call — and sends the signed request to the gateway,
which is the one that calls AWS STS over the network to verify it. The
client itself needs no direct network path to STS for this verification
step. The one case that does need one is upstream of signing: if the
client's own AWS credentials were themselves obtained by assuming a role
(AssumeRole, AWS SSO, federation), that assumption is a real STS call the
client makes before it ever signs anything, and that call needs network
access to STS — an STS VPC endpoint in the consumer VPC
(`com.amazonaws.<region>.sts`) satisfies it with no public internet route.
A client holding long-lived IAM credentials directly needs no STS network
access at all, for either step.

`stratoclave auth login --email <email>` (password typed at the terminal, no
browser) is also proxied entirely through the gateway:
`POST /api/mvp/auth/login` (`backend/mvp/cognito_auth.py`) calls Cognito's
`AdminInitiateAuth` server-side, because "the CLI does not have IAM
permissions to call Cognito InitiateAuth directly" (that file's own
docstring). So this sign-in path needs no direct client-to-Cognito network
access either — only the browser Hosted-UI OAuth redirect (used by the web
console, which this path does not have at all) needs the client to reach
Cognito's own domain directly over the public internet. Cognito itself is
still deployed on this path regardless
(`/.well-known/stratoclave-config` requires `COGNITO_DOMAIN` regardless of
entry mode).

### Bootstrapping without a console

This path has no web console, so the first admin and the first API key are
both minted purely through API calls, exactly the mechanism "Post-deploy:
first admin" above already describes — nothing here is specific to private
mode:

Run every step below from a host inside an accepted consumer VPC — see
"Gaps a first-time PrivateLink setup will hit" above for why nothing else
can reach this deployment at all:

1. Set `STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL` and redeploy the ECS stack, as in
   "Post-deploy: first admin". `backend/bootstrap/seed.py` mints the admin
   at container startup by calling Cognito's `AdminCreateUser` and
   `AdminSetUserPassword` itself — a Cognito API call the backend makes, not
   something a browser does — and this is identical in both entry modes.
2. Retrieve the one-time password from Secrets Manager, as already shown
   above.
3. Point the CLI at this deployment: `stratoclave setup
   https://stratoclave.internal.example.com` (your own
   `STRATOCLAVE_PRIVATE_DOMAIN`). This reads `/.well-known/stratoclave-config`
   from the gateway and writes the local CLI config; it must run before
   `auth login` below, which depends on it.
4. Sign in with `stratoclave auth login --email <email>` (the password is
   typed into the terminal; see "Sign-in options on this path" above for why
   this needs no direct client-to-Cognito network path).
5. Mint a long-lived API key for yourself with `stratoclave api-key create`
   (see [`api-key create`](CLI_GUIDE.md#api-key)). From then on, use the
   `sk-stratoclave-*` key for daily and unattended use; Cognito is no longer
   needed at all once this key exists.

### What this path does not provide

There is no web console: this mode is headless by design, API and CLI only.

There is no WAF on this path — a CLOUDFRONT-scope web ACL cannot attach to
an internal ALB, and this slice does not add a REGIONAL one.

**"No internet-facing path to this deployment's own API" is narrower than "no
internet-facing entry point at all", except for two things that stay
reachable from the public internet regardless of entry mode:**

- **The Cognito user pool and its Hosted UI domain are still public.** The
  app client has no client secret and `USER_PASSWORD_AUTH` is enabled (both
  needed for the CLI's own `auth login` flow, in either entry mode), so
  password-guessing against the pool is reachable from the internet on this
  path exactly as it is on `cloudfront`. Nothing about `private` mode
  narrows that.
- **ECS tasks run in the public subnet with a public IP, and the VPC keeps
  its internet gateway**, the same placement as `cloudfront` mode (see
  `lib/network-stack.ts`: no NAT, no private subnets, in either mode). The
  task security group (admits only the ALB on :8000) is the only barrier
  between a task and the internet reaching it directly; this entry mode
  does not change that placement or add a second one.

**The per-IP rate limit is a shared-fate availability risk here, not merely
"coarse", for the routes it still applies to.** Every request on this path
arrives from the internal NLB, not from the original client, so a limiter
keyed on the immediate peer address puts every principal reaching the
gateway through PrivateLink into the same bucket — with no WAF in front to
catch a flood earlier, one noisy or compromised consumer can exhaust that
shared bucket and produce 429s for every other consumer, not just itself.

An AUTHENTICATED request is bucketed by the verified user rather than the
address (`core.rate_limit._request_key`), so this path's per-user limits are
exactly as precise as `cloudfront`'s, and a caller behind the NLB cannot be
locked out by another authenticated caller sharing the same address. What
stays genuinely shared on this path is the UNAUTHENTICATED sign-in routes
(`/api/mvp/auth/login`, `/respond`, `/sso-exchange`, `/ui-ticket/consume`):
with no verified identity yet, these are rate-limited twice (see "Rate
limits on sign-in routes" below) — a tight limit keyed on the identity
TOGETHER WITH the address (the email, or, for `/sso-exchange`, the account
STS verifies), and each route's own per-address flood guard
(`AUTH_LOGIN_RATE_LIMIT` etc.), which bounds total sign-in volume from one
address regardless of how many distinct (possibly made-up) identities it
tries. Because every client on this path shares one address, the
identity-plus-address bucket is effectively identity-only here: one
accepted consumer can still lock a named user out of signing in from
anywhere else on this entry. Size `AUTH_LOGIN_RATE_LIMIT` /
`AUTH_RESPOND_RATE_LIMIT` / `SSO_EXCHANGE_RATE_LIMIT` for that topology
specifically — see "Rate limits on sign-in routes" below for the full
threat model and the operator lever.

This is deliberate, not simply unfixed: the ALB attribute
`routing.http.xff_header_processing.mode` is set to `remove` specifically
so that a client-supplied `X-Forwarded-For` can never reach the application
and be trusted as if it were a real client address — the alternative
(trusting it) would let any consumer forge a distinct address and evade the
shared bucket entirely, which is worse. Per-key budgets and per-tenant
dollar pools still apply exactly as they do on the `cloudfront` path; they
never depended on IP at all.

**There is no full client-address attribution, even with the ALB access log
now enabled** (see "Cost estimate" and `lib/alb-stack.ts`'s `enableAccessLogs`).
What the ALB's access log records as the connection's address, for
PrivateLink traffic specifically, is governed by AWS's own behaviour at
that boundary; this document does not assert a specific value for it
without having measured one on a real deployment. What it reliably is
**not** is an application-level identity — the API key, the authenticated
user, the tenant — all of which the application's own logs already carry,
in both entry modes, from the request's own auth header.

### TLS

TLS terminates at the internal ALB, inside the gateway's own VPC, using the
certificate you supply. The NLB in front of it carries a TCP listener, not a
TLS one — it passes the connection straight through rather than terminating
it, so it never holds key material of its own. The ALB-to-task hop, inside
the same VPC, is plain HTTP, exactly as it is on the `cloudfront` path.

---

## Cost estimate

Representative monthly spend for a low-traffic single-team deployment in `us-east-1`. Figures are USD and **exclude Bedrock token usage**, which is billed separately by AWS.

| Item                                              | Monthly |
| ------------------------------------------------- | ------- |
| ECS Fargate (0.25 vCPU / 0.5 GiB, 1 task)         | ~$12 |
| Application Load Balancer                         | ~$17 |
| CloudFront (~1 GiB egress, first 10 GB free)      | <$1 |
| WAFv2 (4 rules + WebACL association; +1 rule if `WAF_IP_ALLOWLIST_ENABLED=true`) | ~$10 to $12 (fixed) |
| VPC Flow Logs → CloudWatch (30 d retention)       | ~$1 |
| DynamoDB (PAY_PER_REQUEST, small write volume)    | ~$1 |
| S3 + CloudWatch Logs + SSM Parameter Store        | ~$1 |
| **Subtotal**                                      | **~$42 to $45** |

Set `ENABLE_WAF=false` to drop the WAF line item (~$10) for throwaway / sandbox stacks.

The per-request cost of Bedrock dominates in practice. Track it from the Admin Usage page in the web UI (tokens per model) and via AWS Cost Explorer's **Bedrock** service filter.

### `STRATOCLAVE_ENTRY=private`: the line items that differ

CloudFront, the S3 console bucket, and WAF are never provisioned, and are
replaced by an internal NLB and a VPC endpoint service:

| Item                                                        | Monthly | Paid by |
| ------------------------------------------------------------ | ------- | ------- |
| Network Load Balancer (base hourly rate, 2 AZs; NLCU usage is additional and negligible at low traffic) | ~$16 to $17 | this deployment's account |
| VPC endpoint service (the provider-side resource itself has no separate hourly charge beyond the NLB above) | $0 | — |
| ALB access-log S3 bucket (small object count at low traffic, 90-day lifecycle expiry) | <$1 | this deployment's account |
| CloudFront, S3 console bucket, WAFv2                        | removed | — |

Each **consumer's** interface endpoint is a separate cost the consumer's own
account pays, not this deployment's: roughly $0.01/hour per Availability
Zone the endpoint spans (about $7 to $8/month per AZ, so about $14 to $16/month
for the usual 2-AZ setup) plus $0.01/GB of data processed through it. That
cost scales with how many consumer accounts connect, not with how this
gateway is deployed — budget for it separately in each consumer account.

---

## Updates and re-deploys

CDK stacks are designed to be re-applied freely. Typical update flows:

| Change                                           | Command |
| ------------------------------------------------ | ------- |
| Frontend code only                               | `./iac/scripts/deploy-all.sh` (the frontend rebuild is part of the pipeline). |
| Backend code only                                | `./iac/scripts/build-and-push.sh`, then `IMAGE_TAG=<build tag> npx cdk deploy <Prefix>-ecs` (see [container image](#post-deploy-container-image)). |
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

## Binding AWS sign-in to this gateway

Signing in with an AWS identity (`stratoclave auth sso`, Vouch-by-STS) works by the CLI signing an STS `GetCallerIdentity` request and handing it to the gateway, which forwards it to STS to learn who the caller is. A signed request of that kind is not specific to the service it was made for: another service that verifies identity the same way (HashiCorp Vault's AWS auth is the best-known) could pass on a request it received, and the gateway would accept it inside the 5 minute window it itself allows for a signature's age (AWS SigV4 allows up to 15 minutes; this gateway refuses anything older than 5, its own tighter limit, not AWS's). To stop that, CLI 0.2.1 and later add a header to the request before signing it, `X-Stratoclave-Audience`, set to the host of the gateway URL the request is sent to. Because the header is signed, STS refuses any change to it.

Two separate things happen with that header, on two separate schedules:

- **A request bound to a host that is not this gateway's own is always refused — unconditionally, regardless of `STRATOCLAVE_STS_AUDIENCE_REQUIRED` below.** If `X-Stratoclave-Audience` is present at all, its value must be a host in `STRATOCLAVE_API_ENDPOINT` or `STRATOCLAVE_STS_AUDIENCES`, or the gateway refuses the request. This is why every host users reach the gateway through — every custom domain, every CloudFront alias, the PrivateLink domain on the private entry — has to be listed in `STRATOCLAVE_STS_AUDIENCES` **before** distributing CLI 0.2.1 or later: a CLI configured against a host the gateway does not know about gets 401 on every sign-in attempt from the moment it is upgraded, not only after the requirement below is turned on.
- **A request with no `X-Stratoclave-Audience` header at all is still accepted**, so CLIs older than 0.2.1 keep working, until the gateway is told to stop accepting them.

To turn the second one off (require every sign-in to carry a bound audience):

1. Make sure `STRATOCLAVE_API_ENDPOINT` (and `STRATOCLAVE_STS_AUDIENCES` if the gateway is reached under more than one name) lists every URL users set the CLI up against. `STRATOCLAVE_API_ENDPOINT` is composed by `iac/bin/iac.ts` itself; `STRATOCLAVE_STS_AUDIENCES` is sourced from CDK CONTEXT (see the environment variable table below) — set it by editing `iac/cdk.json`'s own "context" object directly. A value set by hand on the task definition, or passed only as a `-c` flag on one `cdk deploy` invocation, is dropped on the next deploy that does not repeat it.
2. Have everyone upgrade to CLI 0.2.1 or later.
3. Watch the gateway's `sso_sts_exchange_verified` log events (success) and `sso_sts_audience_refused` (WARNING, logged on every refusal, with the host that was presented and the reason — a host missing from step 1 shows up here as refusals from real users, not merely as a number that failed to reach 100%). The success event carries `audience_bound`: true for an upgraded CLI, false for an older one or for a caller that is not this CLI at all (a `boto3`/AWS SDK script signing its own `GetCallerIdentity` by hand has no reason to set this header, and is "unbound" exactly as an unupgraded CLI is — this is a property of the request, not of the CLI version). The criterion to move on is **`audience_bound=true` events are present and `audience_bound=false` events are not** for as long as your slowest user takes to upgrade — not merely "no `false` has appeared yet", which is also true before anyone has signed in at all.
4. Set `STRATOCLAVE_STS_AUDIENCE_REQUIRED=true` and redeploy. The gateway refuses to start if it has no API URL to compare with, and refuses any value for this flag that is not a recognised boolean spelling (`1`/`true`/`yes`/`on` for on, `0`/`false`/`no`/`off`/empty for off) — a typo here is a startup failure, not a silent "off".

A CloudWatch Logs Insights query for step 3:

```text
fields @timestamp, arn, audience_bound
| filter @message like /sso_sts_exchange_verified/ and @message like /"audience_bound": ?false/
| stats count() by arn
```

The second `filter` clause matches the literal JSON substring rather than comparing a discovered `audience_bound` field to a boolean literal: CloudWatch Logs Insights' own auto-discovery of JSON fields, and whether `= false` compares correctly against whatever type it infers, is not something this document has run against a real log group to confirm. A string match on `"audience_bound": false` (optional space after the colon, to tolerate either JSON serializer convention) is read directly off the line `core.logging`'s `JSONRenderer` actually produces, so it cannot silently match nothing the way an unproven field comparison could — which would make "no `false` events" trivially, and wrongly, true.

```text
fields @timestamp, presented_host, reason
| filter @message like /sso_sts_audience_refused/
| stats count() by presented_host, reason
```

**Upgrading the gateway and the CLI can happen in either order.** A CLI 0.2.1 request against a gateway that predates this feature works unchanged: that gateway's `verify_and_call_sts` forwards every header it receives to STS verbatim and never reads `X-Stratoclave-Audience` at all, so the extra signed header is simply one more header STS verifies and ignores on the way back — nothing refuses it and nothing requires it. The only ordering constraint is the one stated above: every host in `STRATOCLAVE_STS_AUDIENCES` has to be configured on THIS gateway before a CLI bound to an unlisted host is handed to anyone, because the foreign-host refusal does not wait for `STRATOCLAVE_STS_AUDIENCE_REQUIRED`.

**Local development:** a gateway with `ENVIRONMENT` set to `development`, `local` or `test` may run with no `STRATOCLAVE_API_ENDPOINT` at all (`core.deployment.is_local_environment`), but `mvp.sso_sts.expected_audiences()` then has no host to compare against, so `STRATOCLAVE_STS_AUDIENCE_REQUIRED=true` is refused at startup (there would be nothing for a bound request to match) and `audience_bound` is always reported false for every sign-in on that process. Set `STRATOCLAVE_API_ENDPOINT=http://127.0.0.1:<port>` (the port the backend listens on) to exercise the bound path locally.

Two gaps remain, both covered in "Rate limits on sign-in routes" immediately below rather than here: the coarse per-address flood guard every sign-in route shares, and the fact that a request which fails authentication on an AUTHENTICATED route is not rate-limited at all.

## Rate limits on sign-in routes

An authenticated route is bucketed by the verified user (`core.rate_limit._request_key`); a user behind a shared address, or on the private entry where every request arrives from the load balancer, is not throttled by anyone else sharing it. Sign-in routes have no verified user yet, so they are rate-limited differently, and twice:

- **Keyed on the identity the request names, TOGETHER WITH the client's own address.** `/login` and `/respond` key on the normalised (lowercased) email in the request body plus the caller's address; `/sso-exchange` keys on the account STS actually verifies the signature belongs to — the account id plus the AWS-assigned prefix of the UserId, never the RoleSessionName tail of the ARN, which whoever calls AssumeRole picks — and ONLY after `verify_and_call_sts` has proven the caller holds the credential; an access key id merely parsed out of an unverified `Authorization` string is never charged against anything, because it is not evidence its holder made the request. This is `LOGIN_RATE_LIMIT` / `RESPOND_RATE_LIMIT` / `SSO_EXCHANGE_RATE_LIMIT` (default 10/minute, 10/minute, 20/minute), run explicitly inside each handler (`core.rate_limit.check_identity_rate_limit`) rather than through the route decorator, because the identity (and, for `/sso-exchange`, the verification) is not available at decoration time. The address half of the key is the fix for a real attack, not an optimisation: an identity-ONLY bucket lets anyone who merely KNOWS (never needs to hold) a victim's email lock that victim out — ten bad guesses from one address, discard it, repeat from another, and the victim's own sign-in attempts share that same exhausted bucket regardless of which address they come from. Binding the address means every attacking address only ever spends its OWN (identity, that address) bucket; the victim's own address is untouched by anything an attacker's addresses did. The identity half is hashed (SHA-256) before it becomes a DynamoDB key, and capped in length, so neither an unbounded string nor the raw email ends up stored there.
- **Keyed on the client address alone, one bucket per route (not shared across routes).** Each of `/login`, `/respond`, `/sso-exchange` and `/ui-ticket/consume` keeps its OWN per-address flood guard at the SAME numbers above (`AUTH_LOGIN_RATE_LIMIT`/`AUTH_RESPOND_RATE_LIMIT` default 10/minute, `SSO_EXCHANGE_RATE_LIMIT` default 20/minute) — this is the route's existing `@limiter.limit(...)` decorator, run BEFORE the identity-and-address check above. It bounds total request volume (and so DynamoDB counter growth) from one address regardless of how many different identities it tries, which the identity-and-address bucket alone cannot: that bucket gives a fresh quota to every new (identity, address) pair, so an attacker trying many DIFFERENT made-up identities from one address would otherwise write an unbounded number of counters. A tripped address guard is checked first, so a flood never reaches the identity-and-address counters at all.
- **`/ui-ticket/consume` has no identity-keyed check.** The single-use ticket it redeems is itself the only identity a request could name, and 256-bit entropy already makes guessing one uneconomic; its own per-address flood guard above is belt-and-braces against guessing at volume, not a credential-guessing control.
- **On the private entry, every client shares one address** (the internal NLB-facing one — see "Private entry (PrivateLink), headless" above). There, the identity-and-address bucket is effectively back to identity-ONLY: any client inside an accepted consumer VPC can still lock a named user out of signing in from anywhere else on that same private entry, because every address involved is the same one. Size `AUTH_LOGIN_RATE_LIMIT`/`AUTH_RESPOND_RATE_LIMIT` for that topology specifically (lower is not necessarily safer — too low and the shared address's own legitimate traffic trips it) and treat guessing spread across MANY different accounts as Cognito's own job: Cognito's account-level lockout sees distributed guessing against one account regardless of address, which this gateway's per-(identity, address) bucket structurally cannot.
- **A request that fails authentication on an AUTHENTICATED route is not rate-limited at all**, not merely "not counted against any user". FastAPI resolves a `Depends(get_current_user)` before calling the decorated handler, so when the credential does not verify and `get_current_user` raises, `@limiter.limit(...)`'s own wrapper body — the only place either the per-user or the per-address counter is written — never runs. Unlimited credential guessing against, for example, `POST /api/mvp/billing/authorize` with a bearer token or API key is bounded only by whatever TLS/compute cost each attempt itself has, not by this limiter. `test_rate_limit_xff.py::test_a_rejected_credential_never_sets_rate_principal_and_never_reaches_the_limiter` pins this as a documented gap, not a guarantee. The same is true of a request whose BODY fails Pydantic field validation on a sign-in route (a 422): FastAPI resolves the body before calling the decorated handler too, so that request touches neither the address guard nor the identity check — bounded instead by the existing, separate `MaxBodySizeASGIMiddleware` (`main.py`) for the oversized case, and by ordinary 422 response cost for a merely-malformed one.
- A non-CLI SigV4 client (a `boto3`/AWS SDK script that signs its own `GetCallerIdentity`) is rate-limited exactly like any other `/sso-exchange` caller once STS has verified it — by the account its own signature proves. In the `sso_sts_exchange_verified` log line "Binding AWS sign-in to this gateway" (above) asks an operator to watch, it is reported `audience_bound=false`, the same as an unupgraded CLI, since it has no reason to set `X-Stratoclave-Audience` either. Turning `STRATOCLAVE_STS_AUDIENCE_REQUIRED` on locks such a script out exactly as it would an old CLI.
- `AUTH_LOGIN_RATE_LIMIT`, `AUTH_RESPOND_RATE_LIMIT` and `SSO_EXCHANGE_RATE_LIMIT` are sourced from CDK context (`authLoginRateLimit`/`authRespondRateLimit`/`ssoExchangeRateLimit` in `iac/bin/iac.ts`), not `process.env` — set them by editing `iac/cdk.json`'s own "context" object directly, not with a per-deploy `-c` flag, which a later `cdk deploy` from a different shell would not repeat.

## Backend environment variables

Injected automatically by `iac/bin/iac.ts` when you deploy; listed here so you can reproduce them locally or diagnose missing-value bugs.

| Variable                                 | Required in production? | Set by CDK? | Notes |
| ---------------------------------------- | ----------------------- | ----------- | ----- |
| `ENVIRONMENT`                            | yes                     | yes (always `'production'`, regardless of the resource tier below) | The backend's one question: "has this process been deployed" (`core.deployment.is_local_environment`; every bare `environment == "production"` check in `main.py`/`mvp/authz.py`). NOT the same value as the shell's own `ENVIRONMENT`, which `bin/iac.ts` separately reads for the RESOURCE TIER (DynamoDB deletion protection/PITR, Cognito retention, ECS exec default) — an operator may still deploy a disposable sandbox stack with that unset or `development`; the container's own `ENVIRONMENT` is `'production'` either way, because an ECS Fargate task is a deployment by construction, never a developer's own machine. |
| `COGNITO_USER_POOL_ID`                   | yes                     | yes         | |
| `COGNITO_CLIENT_ID`                      | yes                     | yes         | |
| `COGNITO_DOMAIN`                         | yes                     | yes         | Full hosted-UI URL. |
| `COGNITO_REGION`                         | yes                     | yes         | |
| `OIDC_ISSUER_URL`                        | yes                     | yes         | |
| `OIDC_AUDIENCE`                          | yes                     | yes (equals client ID) | |
| `BEDROCK_REGION`                         | yes                     | yes (= `BEDROCK_PRIMARY_REGION`) | Region Claude models are invoked in — the **model** primary region, independent of the deploy region. Always set explicitly by CDK. See `STRATOCLAVE_REGION` / `BEDROCK_PRIMARY_REGION`. |
| `DEFAULT_BEDROCK_MODEL`                  | yes                     | yes         | |
| `STRATOCLAVE_API_ENDPOINT`               | yes                     | yes         | Published in `/.well-known/stratoclave-config`. Outside a local environment (`ENVIRONMENT` `development`, `local` or `test`) the gateway refuses to start without it, and the document is never built from request headers. Its host is also what a signed AWS sign-in must name (see [Binding AWS sign-in to this gateway](#binding-aws-sign-in-to-this-gateway)). |
| `STRATOCLAVE_STS_AUDIENCES`              | no                      | yes, from CDK context `stsAudiences` (default empty) | Comma-separated extra URLs this gateway is reached under, whose hosts a signed AWS sign-in may also name. Every entry must be an absolute http(s) URL, or the gateway refuses to start. Sourced from CDK context, not `process.env` — set it in `iac/cdk.json`'s own "context" object (a `-c` flag on one `cdk deploy` invocation is not repeated by the next), so a value configured once is not silently dropped by a later deploy from a different shell. |
| `STRATOCLAVE_STS_AUDIENCE_REQUIRED`      | no                      | yes, from CDK context `stsAudienceRequired` (default empty = off) | When on, an AWS sign-in whose signed request carries no `X-Stratoclave-Audience` at all is refused; a request bound to a host that IS NOT this gateway's is refused regardless of this setting (see [Binding AWS sign-in to this gateway](#binding-aws-sign-in-to-this-gateway)). Off by default so CLIs older than 0.2.1 keep working; see that same section's runbook. Read strictly, both by the backend (`core.deployment.strict_flag`) and at synth time (`iac/bin/iac.ts`): `1`/`true`/`yes`/`on` for on, `0`/`false`/`no`/`off`/empty for off, anything else fails synth (and, independently, the backend's own startup) rather than silently reading as off. |
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
| `STRATOCLAVE_ENTRY`                      | IaC-only                | n/a         | `cloudfront` (default) or `private` (see [Private entry](#private-entry-privatelink-headless)). `both` is named but refused at synth. |
| `STRATOCLAVE_PRIVATE_CERTIFICATE_ARN`    | required for `private`  | n/a         | ACM certificate for the internal ALB's HTTPS :443 listener. |
| `STRATOCLAVE_PRIVATE_DOMAIN`             | required for `private`  | no          | The operator's domain name for the PrivateLink publication. Not itself set on the container; CDK composes `https://<this>` into `STRATOCLAVE_API_ENDPOINT` and `CORS_ORIGINS` instead. |
| `STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS` | required for `private`  | n/a         | Comma-separated IAM principal ARNs allowed to request a connection to the endpoint service. Must be non-empty; a wildcard is refused at synth. |
| `ENABLE_WAF`                             | IaC-only                | n/a         | CDK-side flag (read in `iac/bin/iac.ts`). `false` skips provisioning `<Prefix>WafStack`. Default: `true`. Has no effect under `STRATOCLAVE_ENTRY=private`, which never provisions a WAF stack at all. |
| `WAF_RATE_LIMIT_PER_5MIN`                | IaC-only                | n/a         | Per-IP request cap in the rate-based rule. Default: `300`. |
| `WAF_IP_ALLOWLIST_ENABLED`               | IaC-only                | n/a         | Enable SSM-parameter-backed IPSet allowlist. Default: `false`. |
| `STRATOCLAVE_REGION`                      | IaC-only                | n/a         | Body-stack deploy region `R` (Network/DynamoDB/ECR/ALB/Frontend/Cognito/ECS/Config). Falls back to `CDK_DEFAULT_REGION`, then `us-east-1`. The WAF stack always stays in `us-east-1`. GovCloud/China partitions are rejected. When `R != us-east-1` you must `cdk bootstrap` both `R` and `us-east-1`. See [Regional constraints](#regional-constraints). |
| `BEDROCK_PRIMARY_REGION`                 | IaC-only                | n/a         | Bedrock **model** primary region, independent of `R`. Becomes the task's `BEDROCK_REGION`. **Required at synth when `STRATOCLAVE_REGION != us-east-1`** (Stratoclave refuses to guess). Defaults to `us-east-1` when `R == us-east-1`. |
| `STRATOCLAVE_RESIDENCY`                   | IaC-only                | n/a         | `strict` \| `warn` (any other value is rejected at synth). `strict` makes `cdk synth` **hard-fail** if any region Bedrock is actually called in (model + failover + codex) differs from the exact deploy region, or if the default model is a geo inference profile. `warn` (or unset) only warns, and only when `R` is non-default or this var is set. See the data-residency recipe under [Regional constraints](#regional-constraints). |
| `STRATOCLAVE_ALLOW_GEO_INFERENCE`        | IaC-only                | n/a         | `true` downgrades the strict-mode refusal of a geo inference-profile model (`us./eu./apac./jp./au./ca./global.`-prefixed) to a warning — i.e. accept geography-level residency instead of single-region. Default: unset (strict refuses geo profiles). |
| `OPENAI_BEDROCK_REGIONS`                 | IaC-only                | yes (default `us-east-2,us-west-2`) | **Display-only hint** surfaced to the CLI via `/.well-known/stratoclave-config`. It does **not** change the codex call region — that is hardwired per-model in `backend/mvp/models.py`. For residency, disable codex with `STRATOCLAVE_CODEX_ENABLED=false`; setting this var does not keep codex prompts in-region. |
| `CDK_NAG`                                | IaC-only                | n/a         | Set to `off` to skip the cdk-nag synth-time aspect. Default: `on`. |
| `STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL`      | optional                | no          | If set, the backend auto-provisions this email as admin on first startup when no admin exists. Idempotent. |
| `ALLOW_ADMIN_CREATION`                   | bootstrap only          | yes (default `false`) | See [Locking down after bootstrap](#locking-down-after-bootstrap). |
| `STRATOCLAVE_CODEX_ENABLED`                          | optional                | yes (default `true`)  | Enables the OpenAI Responses API endpoint (`/openai/v1/*`). Set `STRATOCLAVE_CODEX_ENABLED=false` to disable. |
| `DYNAMODB_RATE_LIMITS_TABLE`             | optional                | yes (default `stratoclave-rate-limits`) | DynamoDB table backing the per-IP auth rate limiter (`backend/core/rate_limit_ddb.py`). Fixed-window counters with a TTL attribute (`expires_at`) are **shared across every ECS task**, so caps hold under multi-task / multi-AZ scale-out with no Redis. Failure policy: a **misconfiguration** (missing/mis-named table, missing IAM grant → `ResourceNotFound`/`AccessDenied`/`Validation`) fails **CLOSED** — auth endpoints return `429` — so a broken limiter can't silently run unlimited. **⚠️ If every login/SSO returns 429 right after a deploy, check this table name + the task role's DynamoDB grant first.** A **transient outage** (connectivity/timeout) degrades to an in-process per-task limiter (not fully open). Wire a CloudWatch metric filter + alarm on the log line `rate_limit_degrade_to_local` to detect degraded mode. |
| `RATE_LIMIT_TRUSTED_HOPS`                | optional                | no          | Number of right-side `X-Forwarded-For` entries written by your own proxy chain (not counting the viewer IP CloudFront appends). Default `1` matches CloudFront → ALB → ECS. Use `0` for CloudFront-only and `2` for custom WAF → CloudFront → ALB → ECS. See `backend/core/rate_limit.py`. |
| `AUTH_LOGIN_RATE_LIMIT` / `AUTH_RESPOND_RATE_LIMIT` / `SSO_EXCHANGE_RATE_LIMIT` | optional | yes, from CDK context `authLoginRateLimit`/`authRespondRateLimit`/`ssoExchangeRateLimit` (default `10/minute`, `10/minute`, `20/minute`) | Each route's OWN two limits (see [Rate limits on sign-in routes](#rate-limits-on-sign-in-routes)): a per-address flood guard at this number, and an identity-plus-address bucket at the SAME number keyed on the email (`/login`, `/respond`) or the verified account (`/sso-exchange`, only after STS has confirmed it). `/ui-ticket/consume` also uses `SSO_EXCHANGE_RATE_LIMIT` for its own (address-only) flood guard. Sourced from CDK context, not `process.env`, for the same redeploy-persistence reason as the audience settings above — edit `iac/cdk.json`'s "context" object directly. |
| `EXPOSE_TEMPORARY_PASSWORD`              | optional                | no          | If `true`, `admin user create` returns the one-time password in the response. Default `false` (response field is `null`). Not recommended for production. |
| `STRATOCLAVE_FAILOVER_REGIONS`           | optional                | no          | Comma-separated cross-region Bedrock failover targets for the **streaming** path, in preference order (the primary `BEDROCK_REGION` is always tried first and is stripped if listed). Default `us-west-2,eu-west-1`. **Data residency:** set a same-jurisdiction list, or **`none`/`disabled`/`off`** (or an empty string) **to disable failover entirely** — single-region, streaming requests never send prompt bytes to another region. Prefer the `none` sentinel over `""` if your deploy tooling strips empty env vars. Non-streaming requests are single-region regardless. The effective set is logged at startup (`failover_regions_effective`). See `backend/mvp/routing/chains.py`. |

| `STRATOCLAVE_UNOBSERVED_HOLDS`            | optional                | no          | Whether a reservation is **kept** when the gateway never observed the provider's outcome (a read timeout, a 5xx, a 200 it could not parse). Default **on** — `provider_outcome.unobserved_holds_enforced()` returns true when the variable is unset, so the shipped artifact keeps the reservation. Setting it to a falsy value gives back the weaker behaviour: the outcome is still classified and logged (`provider_attempt_failed`) but the reservation is returned as though the call were free, which it was not. Leaving it on makes the dollar ceiling bind on calls that may have been billed while the caller saw nothing, and the reaper no longer hands the headroom back either: a hold whose provider call had departed moves to `RETAINED` and is ended only by an operator, at the figure the provider's own record shows or as a release when it shows none (`GET`/`POST /api/mvp/admin/tenants/{tenant_id}/pool-retained…`). So read [`design/charge-loss.md`](design/charge-loss.md) section 7 first: the exposure accounting and saturation alarms it asks for do not exist yet, so under a provider outage a tenant's headroom can fill with retentions and the first signal is a refusal. `held_microusd` on the list endpoint is the figure to watch. Measured evidence for the rows this reads: [`MEASUREMENTS.md`](MEASUREMENTS.md). |
| `STRATOCLAVE_HARD_CEILING_GATE`           | optional                | no          | Turns the sound reservation **bound** into an admission gate, so a request that cannot fit the pool is refused with `402 request_does_not_fit_pool_limit` instead of admitted on an estimate. Default **on** — `reservation_bound.py` reads it with `default=True`, so the shipped artifact prices admission from a byte-count bound rather than an estimate. Before turning it OFF, read the shadow measurement — [`design/hard-ceiling.md`](design/hard-ceiling.md) section 9b states the refusal-rate target, and section 4 the assumptions the guarantee rests on. |
| `STRATOCLAVE_MEASURE_UNENFORCED_BOUND`    | optional                | no          | Computes the bound and records it **without** gating on it, which is the shadow state the gate above requires as its precondition. Default **off**. It costs a payload survey per request, which is why it is not on by default. |
| `STRATOCLAVE_POOL_HOLD_TTL_SECONDS`       | optional                | no          | How long an unsettled pool hold survives before the reaper reclaims it (default `3600`). An import-time check refuses a value that is too small. It must exceed the longest provider deadline this deployment can produce, or the reaper releases budget for a request that is still running — see [`design/hard-ceiling.md`](design/hard-ceiling.md) section 5, which derives it from the transport timeouts rather than from a guess. |
| `STRATOCLAVE_POOL_SWEEP_MAX_HOLDS`        | optional                | no          | Upper bound on how many expired holds one reaper pass reclaims (default `5`), so a backlog cannot turn into one unbounded transaction. |
| `STRATOCLAVE_IMAGE_PIXELS_PER_TOKEN`      | optional                | no          | Pixels per token used when bounding an image payload (default `750`). A bound is sound only inside a stated content envelope; this is that statement for images — [`design/hard-ceiling.md`](design/hard-ceiling.md) section 3b. |
| `STRATOCLAVE_TOKENS_PER_BYTE_<MODEL>`     | optional                | no          | Per-model override for the bytes-to-tokens factor the bound uses, for a tokeniser that is denser than the default envelope. Stated per model on purpose: a single global factor is either unsound for one script or wasteful for the rest. |
| `STRATOCLAVE_MONOPOLISE_WARN_N`           | optional                | no          | Logs `reservation_will_monopolise_pool` when one reservation would take more than 1/N of a tenant's pool (default `N=100`). Diagnostic only; it changes no decision. |
| `STRATOCLAVE_DISABLE_SEED`                | optional                | no          | Set to `true` to skip first-startup table seeding. For a container that must not write to DynamoDB on boot. |
| `STRATOCLAVE_BOOTSTRAP_ADMIN_ORG_ID`      | optional                | no          | Tenant the bootstrap admin is created in, when `STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL` is set. Defaults to the default org. |
| `STRATOCLAVE_SR_MODE_DEFAULT` / `STRATOCLAVE_SR_CANARY_BPS` / `STRATOCLAVE_SR_FORCE_OFF` | optional | no | Semantic-router mode for tenants with no explicit setting (default `off`), the canary share in basis points, and an operator kill switch that forces off for everyone. The integration is built but dark (`decide()` returns `NO_DECISION`), so these change nothing today — see `backend/mvp/sr/CONTRACT.md`. |
| `STRATOCLAVE_SHADOW_SIMPLE_MAX_TOKENS`    | optional                | no          | Input-size ceiling below which the advisory shadow judge treats a request as simple enough for the cheapest tier to be a defensible counterfactual (default `1500`). Advisory only: it never steers execution, routing, or money. |

**Money-affecting flags are the four at the top of that group** (`STRATOCLAVE_UNOBSERVED_HOLDS`, `STRATOCLAVE_HARD_CEILING_GATE`, `STRATOCLAVE_MEASURE_UNENFORCED_BOUND`, `STRATOCLAVE_POOL_HOLD_TTL_SECONDS`). The first two **default on**, so the shipped artifact exhibits the properties they gate; the other two are off or derived. Each has a design document stating what its setting assumes, and the direction that needs the argument is turning the first two off, not on. The default stated in each row is compared against the code that produces it by `test_documented_money_flag_defaults.py`, so a flag whose default changes cannot leave this table behind.

If the backend refuses to start and the CloudWatch log says `environment variable X is required`, compare the task definition's `environment` array against this list.

---

## Teardown

```bash
cd iac
npx cdk destroy --all --profile "$AWS_PROFILE"
```

> **Destructive.** `cdk destroy --all` removes every Stratoclave CloudFormation stack in the target account. The working tables are deleted, so every user and tenant is lost; the audit tables listed below are kept. Only run this if you truly want to wipe the deployment.

**`STRATOCLAVE_ENTRY=private` only: reject every connection before destroying.** A VPC endpoint service with an active or a still-pending connection cannot be deleted; `cdk destroy` of the `<Prefix>-private-link` stack fails until every connection is gone. Reject them first, in the provider account:

```bash
aws ec2 describe-vpc-endpoint-connections --region us-east-1 --filters Name=service-id,Values=<your endpoint service id>
aws ec2 reject-vpc-endpoint-connections --region us-east-1 --service-id <your endpoint service id> --vpc-endpoint-ids <every vpc-endpoint-id the previous command listed>
```

Then `cdk destroy --all` as above.

Things that **remain** after `cdk destroy`. They are kept on purpose (`RemovalPolicy.RETAIN`), and each has a fixed name derived from `STRATOCLAVE_PREFIX`, so **a redeploy with the same prefix fails with "already exists"** until they are deleted or the prefix is changed:

| Resource | Name | Why it is kept |
|---|---|---|
| ECR repository | `<prefix>-backend` | Released images are the rollback of last resort. |
| DynamoDB audit tables | `<prefix>-api-keys`, `<prefix>-usage-logs`, `<prefix>-credit-ledger`, `<prefix>-quota-events` | Audit record; kept in every environment. |
| Secrets Manager secret | `<prefix>/bootstrap-admin-temp-password` | Pre-created so the task role needs only `PutSecretValue`; it holds the first admin's temporary password. |
| CloudWatch Logs groups | `/ecs/<prefix>-backend`, `/aws/ecs/containerinsights/<prefix>-cluster/performance`, and the custom-resource Lambda groups `/aws/lambda/<prefix>-*` | Incident evidence. |
| S3 bucket (only if the semantic router config bucket is enabled) | `<prefix>-vsr-config-<account>` | Versioned configuration. |

To start over with the same prefix, export what you need from the audit tables first, then delete them:

```bash
export PREFIX=stratoclave
aws ecr delete-repository --repository-name "$PREFIX-backend" --force
for t in api-keys usage-logs credit-ledger quota-events; do aws dynamodb delete-table --table-name "$PREFIX-$t"; done
aws secretsmanager delete-secret --secret-id "$PREFIX/bootstrap-admin-temp-password" --force-delete-without-recovery
for g in "/ecs/$PREFIX-backend" "/aws/ecs/containerinsights/$PREFIX-cluster/performance" $(aws logs describe-log-groups --log-group-name-prefix "/aws/lambda/$PREFIX-" --query 'logGroups[].logGroupName' --output text); do aws logs delete-log-group --log-group-name "$g"; done
```

Also note:

- **The Cognito domain prefix** may be unusable for a while after deletion. If a redeploy with the same `STRATOCLAVE_PREFIX` fails on the user pool domain, wait or choose a new prefix.
- **ECR images** are deleted together with the repository by the `--force` above.

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
