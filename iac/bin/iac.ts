#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { AwsSolutionsChecks, NagSuppressions } from 'cdk-nag';
import {
  capacityPlan,
  getPrefix,
  impliedRatePer5Min,
  workersForCpuUnits,
  optionalPositiveIntFromEnv,
  pinnedStackName,
  stackName,
  paramPath,
  positiveIntFromEnv,
  putStringParameter,
} from '../lib/_common';
import { resolveRegionConfig } from '../lib/region-config';
import { NetworkStack } from '../lib/network-stack';
import { EcrStack } from '../lib/ecr-stack';
import { AlbStack } from '../lib/alb-stack';
import { EcsStack } from '../lib/ecs-stack';
import { FrontendStack } from '../lib/frontend-stack';
import { CognitoStack } from '../lib/cognito-stack';
import { DynamoDBStack } from '../lib/dynamodb-stack';
import { LedgerProjectorStack } from '../lib/ledger-projector-stack';
import { CertificateSchedulerStack } from '../lib/certificate-scheduler-stack';
import { QuotaReconcilerStack } from '../lib/quota-reconciler-stack';
import { QuotaGrantsStack } from '../lib/quota-grants-stack';
import { WafStack } from '../lib/waf-stack';
import { Stack } from 'aws-cdk-lib';
import { Construct } from 'constructs';

/**
 * Stratoclave IaC entrypoint (Phase 2 v2.1)
 *
 * Topology (9 stacks):
 *   - Network (Public Subnet, 2 AZ, no NAT)
 *   - DynamoDB (17 tables; Tenants/Permissions added in Phase 2, TrustedAccounts/SsoPreRegistrations/SsoNonces in Phase S, ApiKeys in Phase C, UiTickets in P0-8 follow-up, TenantBudgets/PricingConfig in A-1/A-2)
 *   - ECR
 *   - ALB (internet-facing, HTTP only)
 *   - WAF (CloudFront-scope WebACL; opt-out with ENABLE_WAF=false)
 *   - Frontend (S3 + CloudFront + CloudFront Function SPA fallback)
 *   - Cognito User Pool (cross-stack references the Frontend domain)
 *   - ECS Fargate (placed directly in the public subnet)
 *   - BackendConfig (static Parameter Store values)
 *
 * Dependency order (v2.1): network → dynamodb → ecr → alb → frontend
 *   → cognito → ecs → config. Cognito reads the CloudFront domain via
 *   a cross-stack reference, hence the Frontend dependency.
 *
 * Region layout (v2.2): the body stacks deploy to an operator-chosen region R
 *   (STRATOCLAVE_REGION, default us-east-1); only WafStack is pinned to
 *   us-east-1 (CLOUDFRONT-scope WebACL requirement). When R != us-east-1 the
 *   WAF→Frontend edge crosses regions (crossRegionReferences); everything else,
 *   including Cognito↔Frontend, is same-region. The Bedrock model primary
 *   region (BEDROCK_PRIMARY_REGION) is independent of R.
 *
 * Retired stacks (kept under iac/lib/_archived/): RdsStack, RedisStack,
 *   WafStack (now re-introduced under a different layout), CodeBuildStack,
 *   FrontendCodeBuildStack, VerifiedPermissionsStack.
 */

const app = new cdk.App();
const prefix = getPrefix();

// v2.2 region decoupling: the *body* stacks (Network/DynamoDB/Ecr/Alb/
// Frontend/Cognito/Ecs/Config) deploy to an operator-chosen region R; only
// WafStack is pinned to us-east-1 because AWS requires CLOUDFRONT-scope
// WAFv2 WebACLs to live there. CloudFront itself is global and uses the
// default *.cloudfront.net certificate (no custom ACM), so WAF is the ONLY
// genuine us-east-1 dependency. Cognito is region-agnostic (it builds its
// Hosted UI / issuer URLs from `this.region`), so Cognito↔Frontend stays a
// same-region (R) edge and needs no crossRegionReferences. Only the
// WAF→Frontend edge crosses regions when R != us-east-1. See
// docs/DEPLOYMENT.md ("Region decoupling") for the residency recipe.
// Region / residency resolution lives in a pure, in-process-testable module
// (lib/region-config.ts). It throws with an actionable message on any invalid
// or residency-unsafe configuration, and returns the resolved regions + the
// warnings to surface as CDK Annotations. See region-decoupling.test.ts.
const regionCfg = resolveRegionConfig(process.env);
const bodyRegion = regionCfg.bodyRegion;
const bedrockPrimaryRegion = regionCfg.bedrockPrimaryRegion;
const defaultBedrockModel = regionCfg.defaultBedrockModel;
const failoverRegionsEnv = regionCfg.failoverRegionsEnv;
const codexEnabled = regionCfg.codexEnabled;
const residencyWarnings = regionCfg.residencyWarnings;

// Deprecated bare env-var names (e.g. CODEX_ENABLED -> STRATOCLAVE_CODEX_ENABLED)
// used as a fallback this synth. Surfaced immediately as CDK Annotations on the
// app so they show up in `cdk synth`/`cdk diff` regardless of which stacks end
// up being created.
for (const w of regionCfg.deprecationWarnings) {
  cdk.Annotations.of(app).addWarning(w);
}
if (process.env.STRATOCLAVE_VLLM_ENDPOINTS === undefined && process.env.VLLM_ENDPOINTS !== undefined) {
  cdk.Annotations.of(app).addWarning(
    'VLLM_ENDPOINTS is deprecated; set STRATOCLAVE_VLLM_ENDPOINTS instead. ' +
      'The bare name is still passed through to the task for this synth but ' +
      'support for it will be removed in a future release.',
  );
}

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: bodyRegion,
};

// WAF lives in us-east-1 regardless of the body region. NEVER derive this from
// bodyRegion — pinning WAF to R fails loudly at deploy (WAFv2 CLOUDFRONT scope
// must be us-east-1), but keeping it a distinct literal makes the intent
// unmistakable and immune to a copy-paste that reuses `env`.
const wafEnv = { account: process.env.CDK_DEFAULT_ACCOUNT, region: regionCfg.wafRegion };

const cognitoEnv = env; // same region as the body: Cognito is region-agnostic

// FUTURE GUARD: if a custom CloudFront domain + ACM certificate is ever added,
// that certificate stack MUST also be pinned to us-east-1 (like wafEnv) — do
// NOT attach it to a body-region stack, or CloudFront will reject it at deploy.

const cognitoDomainPrefix = process.env.COGNITO_DOMAIN_PREFIX; // optional (auto-generated if not specified)

// Admin creation gate (Critical C-D): unset after bootstrap in production
const allowAdminCreation = process.env.ALLOW_ADMIN_CREATION || 'false';
// P1-A: in production, `ALLOW_ADMIN_CREATION_UNTIL=<epoch>` is also required.
// Accepting the value here and passing it to the ECS environment lets
// operators control it via the CDK deploy path without touching SSM directly.
const allowAdminCreationUntil = process.env.ALLOW_ADMIN_CREATION_UNTIL || '';

// Environment flag drives production-only knobs (deletion protection,
// retain-on-delete tables, stricter cdk-nag rules).
const envName = process.env.ENVIRONMENT || 'development';
const isProd = envName === 'production';

// P1-C (2026-04 review): `enableExecuteCommand` defaults OFF in
// production so a compromised AWS credential cannot simply
// `aws ecs execute-command` into a live backend task. Non-production
// keeps it on for developer convenience. `ENABLE_ECS_EXEC=true`
// explicitly re-opens shell access for an operator-run deploy cycle.
const ecsExecExplicit = process.env.ENABLE_ECS_EXEC;
const enableEcsExec = ecsExecExplicit !== undefined
  ? ecsExecExplicit.toLowerCase() === 'true'
  : !isProd;

// P1-2 WAF: set ENABLE_WAF=false only for throwaway stacks. Default is on —
// without WAF, /api/* is exposed with no rate limit or managed-rule coverage.
const enableWaf = (process.env.ENABLE_WAF || 'true').toLowerCase() !== 'false';
// The per-IP ceiling for AUTHENTICATED traffic. Derived from the concurrency
// target rather than fixed: at 300 req / 5 min a single host driving the target
// was blocked with 403 on 1018 of 1024 requests while the service itself served
// 60 req/s comfortably, because an aggregator in front of the gateway is one IP
// carrying many users' traffic. Cost for that traffic is bounded per identity by
// the token quota; this rule's remaining job is to bound a flood.
const wafConcurrencyTarget = positiveIntFromEnv('BACKEND_CONCURRENCY_TARGET', 1024);
const wafRateFloor = impliedRatePer5Min(wafConcurrencyTarget);
const wafRateLimit = Number(process.env.WAF_RATE_LIMIT_PER_5MIN || wafRateFloor);
// AWS WAF rate-based rule minimum is 100 req / 5 min; a typo like "3oo" yields
// NaN and fails at CFN deploy with an unhelpful message. Fail at synth instead.
// (Fable review L-3)
if (!Number.isInteger(wafRateLimit) || wafRateLimit < 100) {
  throw new Error(
    `WAF_RATE_LIMIT_PER_5MIN must be an integer >= 100 (got "${process.env.WAF_RATE_LIMIT_PER_5MIN}").`
  );
}
if (wafRateLimit < wafRateFloor) {
  cdk.Annotations.of(app).addWarning(
    `WAF_RATE_LIMIT_PER_5MIN (${wafRateLimit}) is below the ${wafRateFloor} a single ` +
      `client at BACKEND_CONCURRENCY_TARGET (${wafConcurrencyTarget}) can produce: ` +
      'traffic the gateway is sized to serve will be blocked with 403 before it ' +
      'reaches the service.',
  );
}
// Unattributable requests keep the original tight ceiling: with no user to charge,
// the address is the only key available.
const wafUnauthenticatedRateLimit = positiveIntFromEnv(
  'WAF_UNAUTH_RATE_LIMIT_PER_5MIN',
  300,
);
const wafIpAllowlistEnabled =
  (process.env.WAF_IP_ALLOWLIST_ENABLED || 'false').toLowerCase() === 'true';

// --- 1. Network (Public Subnet 2 AZ, no NAT) ---
const networkStack = new NetworkStack(app, stackName(prefix, 'network'), {
  env,
  prefix,
  description: `[${prefix}] VPC + Public Subnets + SGs`,
});

// --- 2. DynamoDB ---
const dynamoDBStack = new DynamoDBStack(app, stackName(prefix, 'dynamodb'), {
  env,
  prefix,
  environment: envName,
  description: `[${prefix}] DynamoDB tables (serverless, incl. Tenants/Permissions)`,
});

// --- 3. ECR ---
const ecrStack = new EcrStack(app, stackName(prefix, 'ecr'), {
  env,
  prefix,
  description: `[${prefix}] ECR repository for backend image`,
});

// --- 4. ALB ---
const albStack = new AlbStack(app, stackName(prefix, 'alb'), {
  env,
  prefix,
  vpc: networkStack.vpc,
  securityGroup: networkStack.albSecurityGroup,
  internal: false,
  healthCheckPath: '/health',
  targetPort: 8000,
  // Prod: protect against accidental `cdk destroy` tearing the ALB down.
  deletionProtection: isProd,
  description: `[${prefix}] Internet-facing ALB`,
});
albStack.addDependency(networkStack);

// --- 5a. WAF (CLOUDFRONT scope, pinned to us-east-1 via `wafEnv`).
// The WebACL ARN is consumed by FrontendStack (below). When the body region
// R != us-east-1 this becomes a cross-region reference, so both producer and
// consumer set `crossRegionReferences: true`. When R == us-east-1 the flag is
// a no-op — CDK emits an ordinary Export/Fn::ImportValue — so existing
// single-region deployments see zero template diff. Passing the ARN as a plain
// string instead was rejected: a WebACL replacement would leave a stale ARN and
// silently disable WAF (the token-based reference makes that structurally
// impossible and also enforces deploy ordering).
let wafStack: WafStack | undefined;
if (enableWaf) {
  wafStack = new WafStack(
    app,
    // Qualified by the body region: see `pinnedStackName`. Two deployments that
    // share a prefix and differ only in body region would otherwise both name
    // this stack `<prefix>-waf` in us-east-1, and the second deploy would update
    // the first one's WebACL.
    pinnedStackName(prefix, 'waf', regionCfg.wafRegion, regionCfg.bodyRegion),
    {
    env: wafEnv,
    crossRegionReferences: true,
    prefix,
    rateLimitPer5Min: wafRateLimit,
    unauthenticatedRateLimitPer5Min: wafUnauthenticatedRateLimit,
    ipAllowlistEnabled: wafIpAllowlistEnabled,
    description: `[${prefix}] WAFv2 WebACL for CloudFront (rate-limit + managed rules)`,
  });
}

// --- 5. Frontend (S3 + CloudFront + SPA fallback Function) ---
const frontendStack = new FrontendStack(app, stackName(prefix, 'frontend'), {
  env,
  // Consumes the us-east-1 WebACL ARN. crossRegionReferences must match the
  // producer (wafStack); only relevant when the body region != us-east-1
  // (no-op otherwise). Enabled only when WAF is on, so there is no reference to
  // resolve when ENABLE_WAF=false.
  crossRegionReferences: enableWaf ? true : undefined,
  prefix,
  albDnsName: albStack.alb.loadBalancerDnsName,
  webAclArn: wafStack?.webAclArn,
  description: `[${prefix}] Frontend S3 + CloudFront`,
});
frontendStack.addDependency(albStack);
if (wafStack) {
  frontendStack.addDependency(wafStack);
}

// --- 6. Cognito (cross-stack reference to the Frontend CloudFront domain) ---
// Same region as Frontend (both in R): a plain same-region cross-stack export,
// no crossRegionReferences needed. Cognito is region-agnostic.
const cognitoStack = new CognitoStack(app, stackName(prefix, 'cognito'), {
  env: cognitoEnv,
  prefix,
  // A-09-cognito / A-20-cognito: cap refresh-token TTL at 7 days and
  // RETAIN the User Pool on stack delete when this is the production
  // environment. Without `environment` the stack falls back to the
  // legacy 30-day refresh + DESTROY behaviour, which is appropriate
  // for disposable dev stacks only.
  environment: envName,
  domainPrefix: cognitoDomainPrefix,
  cloudFrontDomainName: frontendStack.cfnDistribution.attrDomainName,
  description: `[${prefix}] Cognito User Pool (Hosted UI, User/Pass auth for CLI)`,
});
cognitoStack.addDependency(frontendStack);

// --- 7. ECS (placed directly in the Public Subnet, region R) ---
// Per-task ceilings are read here so they can be cross-checked before synth: a
// set that admits more requests than it can carry connections for does not fail,
// it just queues in a place nobody is looking at.
// Per process. 32 is near the knee of the measured latency curve; 128 admitted the
// traffic at 7.7 s p50, which is not serving it.
const backendSyncRouteThreads = positiveIntFromEnv('GATEWAY_SYNC_ROUTE_THREADS', 32);
const backendOffloadThreads = positiveIntFromEnv('GATEWAY_OFFLOAD_THREADS', 32);
const backendOpenAiConnections = positiveIntFromEnv('OPENAI_TRANSPORT_MAX_CONNECTIONS', 256);
// One worker per vCPU by default. 1024 CPU units is one vCPU, so a 1 vCPU task
// stays single-process (which is what it was) and a larger task gets the workers
// its cores can actually run.
const backendTaskCpu = positiveIntFromEnv('BACKEND_TASK_CPU', 1024);
const backendUvicornWorkers = positiveIntFromEnv(
  'GATEWAY_UVICORN_WORKERS',
  workersForCpuUnits(backendTaskCpu),
);
if (backendOpenAiConnections < backendSyncRouteThreads) {
  cdk.Annotations.of(app).addWarning(
    `OPENAI_TRANSPORT_MAX_CONNECTIONS (${backendOpenAiConnections}) is below ` +
      `GATEWAY_SYNC_ROUTE_THREADS (${backendSyncRouteThreads}): a task can admit more ` +
      'mantle requests than it holds connections for, so the surplus waits on the ' +
      'connection pool rather than on the model.',
  );
}
// What this configuration can hold, against what it was asked to hold. Stated at
// synth because the target is a product decision and the knobs are three numbers
// that multiply: a fleet nominally sized for 1024 that has no policy to grow it,
// or a floor that absorbs a quarter of it, is a configuration mistake worth
// seeing before the deploy rather than during an incident.
const backendMinTasks = positiveIntFromEnv('BACKEND_MIN_TASKS', 2);
const backendMaxTasks = positiveIntFromEnv('BACKEND_MAX_TASKS', 8);
const backendRequestsPerTarget = optionalPositiveIntFromEnv('BACKEND_REQUESTS_PER_TARGET');
const backendCapacity = capacityPlan({
  target: positiveIntFromEnv('BACKEND_CONCURRENCY_TARGET', 1024),
  minTasks: backendMinTasks,
  maxTasks: backendMaxTasks,
  perProcessRequests: backendSyncRouteThreads,
  workersPerTask: backendUvicornWorkers,
  requestsPerTarget: backendRequestsPerTarget,
});
for (const note of backendCapacity.notes) {
  cdk.Annotations.of(app).addInfo(note);
}
for (const warning of backendCapacity.warnings) {
  cdk.Annotations.of(app).addWarning(warning);
}
const ecsStack = new EcsStack(app, stackName(prefix, 'ecs'), {
  env,
  prefix,
  vpc: networkStack.vpc,
  securityGroup: networkStack.ecsSecurityGroup,
  repository: ecrStack.repository,
  targetGroup: albStack.targetGroup,
  userPoolArn: cognitoStack.userPool.userPoolArn,
  dynamoDbTableArns: dynamoDBStack.allTableArns,
  // Per-tenant VSR config bucket (opaque blobs, versioned). Provisioned only
  // when the external VSR feature is switched on: without it the admin surface
  // 404s and no bucket/grant/env is created (feature ships dark). The bucket
  // name is injected as VSR_CONFIG_BUCKET into the container by EcsStack.
  enableVsrConfigBucket: (process.env.EXTERNAL_VSR_ENABLED || 'false') === 'true',
  // Live pricing subsystem (docs/design/price-feeds.md). Unset/empty =>
  // `undefined`, same treatment as the numeric knobs below, so `priceSource` is
  // absent rather than `''` and EcsStack's `if (props.priceSource)` gate — which
  // decides both the env var and the price-discovery IAM statement together —
  // stays off exactly as it is today. An unknown name is not rejected here: the
  // registry of valid names lives in the backend (`price_sources.register_price_source`,
  // Python), not in this CDK app, and the existing guard pattern for a
  // synth-time enum rejection (`assertRegion` in region-config.ts) is for values
  // this app can validate against a fixed, language-independent rule (an AWS
  // region id). There is no such rule for a plugin name registered at Python
  // import time, so inventing one here would either hardcode the backend's
  // registry into the CDK app or duplicate a validation the backend already
  // does loudly: `active_source()` raises `PriceSourceConfigError` at process
  // start on an unknown name, which is the point in the system that actually
  // knows the registered set.
  priceSource: process.env.STRATOCLAVE_PRICE_SOURCE || undefined,
  priceFeed: {
    intervalSeconds: optionalPositiveIntFromEnv('STRATOCLAVE_PRICE_FEED_INTERVAL_SECONDS'),
    budgetSeconds: optionalPositiveIntFromEnv('STRATOCLAVE_PRICE_FEED_BUDGET_SECONDS'),
    staleAfterSeconds: optionalPositiveIntFromEnv('STRATOCLAVE_PRICE_FEED_STALE_AFTER_SECONDS'),
  },
  // Task size and the per-task concurrency ceilings are one decision: threads
  // that have no CPU behind them queue instead of serving. Both are overridable
  // so a deployment can be sized for its own concurrency target without a code
  // change.
  cpu: backendTaskCpu,
  memory: positiveIntFromEnv('BACKEND_TASK_MEMORY', 2048),
  // Two tasks so the ECS service spreads across both AZs (the VPC has
  // maxAzs=2): no single task or AZ is a SPOF, and rolling deploys keep
  // a task serving. Safe now that per-IP rate limits live in DynamoDB
  // (no in-memory state that a second task would diverge on); budget
  // reserve/settle was already atomic in DynamoDB, and the InfraRouter
  // cooldown map / config cache are per-task advisory by design.
  // The floor, not just the multi-AZ baseline: autoscaling is reactive, so a
  // burst is served by the tasks already running while the scaler catches up. A
  // deployment that must absorb its full concurrency target instantly raises this
  // to the ceiling and pays for the idle capacity; two tasks absorb 256 in flight
  // and grow from there.
  desiredCount: backendMinTasks,
  // Fleet concurrency is the per-task ceiling times the running task count, so
  // the ceiling below and this maximum are what the concurrency target is made
  // of: 8 tasks admitting 128 requests each hold 1024 in flight.
  autoScaling: {
    maxCapacity: backendMaxTasks,
    // No default on purpose. The only honest source for this number is a sweep
    // against the deployed task size and request path, and the measurement that
    // exists (~135 requests/min/task) predates both the 4x larger task and
    // connection pooling — carrying it over would size the fleet from a number
    // this very change invalidated. Until `BACKEND_REQUESTS_PER_TARGET` is set
    // from a fresh sweep, the request policy is not registered and CPU tracking
    // is the only signal.
    requestsPerTarget: backendRequestsPerTarget,
  },
  containerPort: 8000,
  // A-01-ecr follow-through: with the repo IMMUTABLE, every deploy
  // must point at a content-addressed tag. Operators export
  // IMAGE_TAG=<sha-or-release-tag> alongside the deploy command.
  imageTag: process.env.IMAGE_TAG || 'latest',
  environment: {
    ENVIRONMENT: envName,
    STRATOCLAVE_PREFIX: prefix,
    AWS_REGION: env.region,

    // Backend runtime mode
    DATABASE_TYPE: 'dynamodb',
    AUTH_MODE: 'cognito',

    // How many requests one task may hold at once. The framework defaults
    // (anyio's 40 worker threads, and a loop executor sized from the HOST's core
    // count) capped a task far below what its CPU could serve, so both are set
    // explicitly here next to the task size and the scaling ceiling they have to
    // agree with. The OpenAI-transport connection ceiling matches: a pooled client left at
    // httpx's default of 100 would queue while threads and CPU were still free.
    GATEWAY_OFFLOAD_THREADS: String(backendOffloadThreads),
    GATEWAY_SYNC_ROUTE_THREADS: String(backendSyncRouteThreads),
    OPENAI_TRANSPORT_MAX_CONNECTIONS: String(backendOpenAiConnections),
    // The Converse transport's equivalent. botocore defaults this to 10, which
    // would put a TLS handshake back on every request past the tenth however many
    // threads the task has.
    BEDROCK_MAX_POOL_CONNECTIONS: String(
      positiveIntFromEnv('BEDROCK_MAX_POOL_CONNECTIONS', 128),
    ),
    // Per-phase request timing. On by default because the alternative is having no
    // answer when a request is slow while every dependency is fast; switchable
    // because one log line per request is a real cost at this concurrency.
    GATEWAY_REQUEST_TIMING: process.env.GATEWAY_REQUEST_TIMING || 'true',
    // uvicorn processes per task. Sized WITH the task's CPU, never instead of it:
    // workers beyond the task's vCPU count share one core and gain nothing, since
    // what they exist to escape is one GIL per process rather than a shortage of
    // threads. Defaults to one CPU unit worth of vCPU per worker.
    GATEWAY_UVICORN_WORKERS: String(backendUvicornWorkers),

    // Cognito
    COGNITO_USER_POOL_ID: cognitoStack.userPoolId,
    COGNITO_CLIENT_ID: cognitoStack.clientId,
    COGNITO_DOMAIN: cognitoStack.cognitoDomainUrl,
    COGNITO_REGION: cognitoEnv.region,
    OIDC_ISSUER_URL: cognitoStack.oidcIssuerUrl,
    OIDC_AUDIENCE: cognitoStack.clientId,
    OIDC_ORG_CLAIM: 'custom:org_id',

    // DynamoDB table names
    DYNAMODB_USERS_TABLE: dynamoDBStack.usersTable.tableName,
    DYNAMODB_USER_TENANTS_TABLE: dynamoDBStack.userTenantsTable.tableName,
    DYNAMODB_USAGE_LOGS_TABLE: dynamoDBStack.usageLogsTable.tableName,
    DYNAMODB_SESSIONS_TABLE: dynamoDBStack.sessionsTable.tableName,
    DYNAMODB_MESSAGES_TABLE: dynamoDBStack.messagesTable.tableName,
    DYNAMODB_APP_SETTINGS_TABLE: dynamoDBStack.appSettingsTable.tableName,
    DYNAMODB_TAGS_TABLE: dynamoDBStack.tagsTable.tableName,
    DYNAMODB_SSE_TOKENS_TABLE: dynamoDBStack.sseTokensTable.tableName,
    // Phase 2 new tables
    DYNAMODB_TENANTS_TABLE: dynamoDBStack.tenantsTable.tableName,
    DYNAMODB_PERMISSIONS_TABLE: dynamoDBStack.permissionsTable.tableName,
    // Phase S: tables for AWS SSO / STS login
    DYNAMODB_TRUSTED_ACCOUNTS_TABLE: dynamoDBStack.trustedAccountsTable.tableName,
    DYNAMODB_SSO_PRE_REGISTRATIONS_TABLE:
      dynamoDBStack.ssoPreRegistrationsTable.tableName,
    // Phase C: long-lived API keys (for gateway clients such as cowork)
    DYNAMODB_API_KEYS_TABLE: dynamoDBStack.apiKeysTable.tableName,
    // Phase S: SSO replay-defence nonces (sso_sts.py falls back safely
    // if the table is unreachable, but set it so the fast path is used).
    DYNAMODB_SSO_NONCES_TABLE: dynamoDBStack.ssoNoncesTable.tableName,
    // P0-8 follow-up: single-use CLI → SPA handoff tickets. Required
    // for `stratoclave ui open` since ?token= handoff was retired.
    DYNAMODB_UI_TICKETS_TABLE: dynamoDBStack.uiTicketsTable.tableName,
    // A-1/A-2: tenant dollar pool budgets + admin-editable model pricing.
    DYNAMODB_TENANT_BUDGETS_TABLE: dynamoDBStack.tenantBudgetsTable.tableName,
    // Limit raises: the request, the grant, and the daily slot. Set on the SERVICE
    // task because the raise endpoints live in the API, not only on the sweeper.
    DYNAMODB_QUOTA_EVENTS_TABLE: dynamoDBStack.quotaEventsTable.tableName,
    DYNAMODB_PRICING_CONFIG_TABLE: dynamoDBStack.pricingConfigTable.tableName,
    // Per-IP rate-limit counters, shared across ECS tasks (multi-task/AZ safe).
    DYNAMODB_RATE_LIMITS_TABLE: dynamoDBStack.rateLimitsTable.tableName,
    // P0-11: per-model quota counters (charged atomically with the budget pool).
    DYNAMODB_MODEL_QUOTAS_TABLE: dynamoDBStack.modelQuotasTable.tableName,
    // P0-13/14: dual-track observability (span records + workflow_run rollups).
    DYNAMODB_OBSERVABILITY_TABLE: dynamoDBStack.observabilityTable.tableName,
    // P0-16: routing-signals write-only seam (writer live; consumer stubbed).
    DYNAMODB_ROUTING_SIGNALS_TABLE: dynamoDBStack.routingSignalsTable.tableName,
    // SAAR: session-aware routing memory (read/written only when SAAR_ENABLED).
    DYNAMODB_SAAR_MEMORY_TABLE: dynamoDBStack.saarMemoryTable.tableName,
    // Ledger P0-1: event-sourced credit ledger (money source of truth).
    DYNAMODB_CREDIT_LEDGER_TABLE: dynamoDBStack.creditLedgerTable.tableName,

    // CORS
    CORS_ORIGINS: `https://${frontendStack.cfnDistribution.attrDomainName}`,

    // Public API endpoint (CloudFront HTTPS URL).
    // This is the value returned as api_endpoint in /.well-known/stratoclave-config.
    // Always return the CloudFront URL; returning the ALB URL directly would cause the CLI to call it over HTTP.
    STRATOCLAVE_API_ENDPOINT: `https://${frontendStack.cfnDistribution.attrDomainName}`,

    // Feature flags (MVP)
    VERIFIED_PERMISSIONS_ENABLED: 'false',
    TENANT_ISOLATION_ENABLED: 'false',
    // SAAR (session-aware routing) master switch. Ships DARK: when 'false' the
    // backend never reads or writes the SAAR memory table and routing is
    // byte-identical to pre-SAAR. Flip to 'true' to enable session-aware sticky
    // routing + switch-cost budget gating. Per-tenant opt-in still applies on top.
    SAAR_ENABLED: process.env.SAAR_ENABLED || 'false',
    // Hybrid serving (self-hosted vLLM) master switch. Ships DARK: when 'false'
    // every vLLM registry entry is unservable and the invoke path is
    // byte-behaviour-identical to Bedrock-only. Flip to 'true' (and populate
    // STRATOCLAVE_VLLM_ENDPOINTS) to route vLLM-served models to an internal
    // endpoint.
    HYBRID_SERVING_ENABLED: process.env.HYBRID_SERVING_ENABLED || 'false',
    // Operator allowlist of internal vLLM endpoints as a JSON object
    // {"<endpoint_key>": "<internal-url>"}. The URL set is closed here (SSRF
    // guard): the registry and clients only ever reference the opaque key.
    // Passed through ONLY when set, so unset => no vLLM endpoints => all vLLM
    // entries unservable. VLLM_ENDPOINTS (bare) is the deprecated predecessor
    // name; the backend (mvp/serving/vllm.py) reads it as a fallback when
    // STRATOCLAVE_VLLM_ENDPOINTS is unset, so passing through whichever the
    // operator set keeps an un-migrated deployment working.
    ...(process.env.STRATOCLAVE_VLLM_ENDPOINTS
      ? { STRATOCLAVE_VLLM_ENDPOINTS: process.env.STRATOCLAVE_VLLM_ENDPOINTS }
      : process.env.VLLM_ENDPOINTS
      ? { VLLM_ENDPOINTS: process.env.VLLM_ENDPOINTS }
      : {}),
    // External VSR (Value/Session Router) master switch. Ships DARK: when
    // 'false' no version handshake runs and no consult is attempted, so routing
    // is exactly today's. The VSR is version-pinned (VSR_EXPECTED_CONTRACT +
    // VSR_EXPECTED_BUILDS): a build outside the pinned set is REFUSED and never
    // followed. Its suggestion passes the SAME allowlist enforcement as a client
    // x-sc-model-pin.
    EXTERNAL_VSR_ENABLED: process.env.EXTERNAL_VSR_ENABLED || 'false',
    ...(process.env.VSR_BASE_URL ? { VSR_BASE_URL: process.env.VSR_BASE_URL } : {}),
    ...(process.env.VSR_EXPECTED_CONTRACT
      ? { VSR_EXPECTED_CONTRACT: process.env.VSR_EXPECTED_CONTRACT }
      : {}),
    ...(process.env.VSR_EXPECTED_BUILDS
      ? { VSR_EXPECTED_BUILDS: process.env.VSR_EXPECTED_BUILDS }
      : {}),
    RATE_LIMIT_ENABLED: 'true',
    ADMIN_API_RATE_LIMIT: '60/minute',
    TEAM_API_RATE_LIMIT: '30/minute',
    USAGE_API_RATE_LIMIT: '10/minute',

    // Phase 2 (v2.1): Admin creation gate (Critical C-D).
    // P1-A: in production, ALLOW_ADMIN_CREATION_UNTIL=<epoch> is also required.
    // An empty string is treated as unset and the backend will reject the request.
    ALLOW_ADMIN_CREATION: allowAdminCreation,
    ALLOW_ADMIN_CREATION_UNTIL: allowAdminCreationUntil,

    // Tenant. The per-user token quota is a loose fairness backstop, not the
    // binding ceiling — that is the seat-scaled dollar pool a tenant now gets at
    // creation (docs/design/limits.md, C14). A hardcoded '100000' here would have
    // pinned every deployment to the old ceiling no matter what the application
    // default says, so the figure is forwarded from the deploy environment with
    // the SAME default the application uses.
    DEFAULT_ORG_ID: 'default-org',
    DEFAULT_TENANT_CREDIT: String(optionalPositiveIntFromEnv('DEFAULT_TENANT_CREDIT') ?? 10_000_000),
    STRATOCLAVE_SEAT_MONTHLY_USD: String(
      optionalPositiveIntFromEnv('STRATOCLAVE_SEAT_MONTHLY_USD') ?? 200),

    // Bedrock (Anthropic / Claude). BEDROCK_REGION is ALWAYS set explicitly to
    // the model primary region (which is independent of the deploy region) — it
    // is NEVER omitted and NEVER derived from env.region. If it were unset, the
    // backend's default_region() would fall back to AWS_REGION (= the deploy
    // region R), silently calling Bedrock in R even when R does not host the
    // model. See bin/iac.ts::bedrockPrimaryRegion.
    BEDROCK_REGION: bedrockPrimaryRegion,
    DEFAULT_BEDROCK_MODEL: defaultBedrockModel,
    // Cross-region streaming failover set (mvp/routing/chains.py). Passed
    // through ONLY when the operator set it, so unset preserves the backend
    // default (us-west-2 + eu-west-1). Set to "disabled"/empty for single-
    // region residency.
    ...(failoverRegionsEnv !== undefined
      ? { STRATOCLAVE_FAILOVER_REGIONS: failoverRegionsEnv }
      : {}),

    // Fault injection for live failover verification (mvp/routing/fault.py).
    // Passed through ONLY when explicitly set to "1", so it is ABSENT by default
    // and MUST never be set on a production task. When present it lets an
    // operator-issued request carry an `x-sc-fault` header to trigger synthetic
    // Bedrock errors (throttle/unavailable/timeout) and exercise the real
    // cross-region failover path on a staging deploy.
    ...(process.env.SC_FAULT_INJECTION === '1'
      ? { SC_FAULT_INJECTION: '1' }
      : {}),

    // OpenAI (codex / GPT-5.x) on Amazon Bedrock — bedrock-mantle endpoint.
    // GPT-5.4 / GPT-5.5 are GA only in us-east-2 / us-west-2; the route handler
    // in mvp/openai_responses.py picks the per-model region out of the model
    // REGISTRY (mvp/models.py), NOT from OPENAI_BEDROCK_REGIONS — that var is a
    // DISPLAY-ONLY hint surfaced to the CLI via /.well-known/stratoclave-config
    // (read only in well_known.py). RESIDENCY NOTE: the codex path is therefore
    // hardwired to us-east-2/us-west-2 and cannot be relocated by an env var —
    // the only residency lever for codex is STRATOCLAVE_CODEX_ENABLED=false
    // (see the residency analysis above).
    // Pass the SAME normalized boolean the residency analysis used (`codexEnabled`),
    // not the raw operator string. This makes the task-def value provably equal to
    // what STRATOCLAVE_RESIDENCY=strict assumed — otherwise strict could pass with
    // codex "off" at synth while the container runs it "on" (NEW-8). The backend
    // parses it as `.lower() == "true"`, so 'true'/'false' are exact. (NEW-8/NEW-11)
    // Injected under the new namespaced name only: the container reads
    // STRATOCLAVE_CODEX_ENABLED first and CODEX_ENABLED only as a fallback
    // when the new name is entirely unset, so writing the normalized boolean
    // under the new name here is what a fresh deploy should do; an operator
    // upgrading an existing task definition in place (outside this CDK app)
    // is the only path that would still be relying on the bare name.
    STRATOCLAVE_CODEX_ENABLED: String(codexEnabled),
    DEFAULT_CODEX_MODEL: process.env.DEFAULT_CODEX_MODEL || 'openai.gpt-5.6-sol',
    OPENAI_BEDROCK_REGIONS:
      process.env.OPENAI_BEDROCK_REGIONS || 'us-east-2,us-west-2',
    OPENAI_BASE_PATH: process.env.OPENAI_BASE_PATH || '/openai/v1',
  },
  secrets: {},
  // P1-C: shell-into-task is opt-in (false in production by default).
  enableExecuteCommand: enableEcsExec,
  description: `[${prefix}] ECS Fargate (Public Subnet, desiredCount=2, multi-AZ)`,
});
ecsStack.addDependency(networkStack);
ecsStack.addDependency(ecrStack);
ecsStack.addDependency(albStack);
ecsStack.addDependency(dynamoDBStack);
ecsStack.addDependency(cognitoStack);
ecsStack.addDependency(frontendStack);

// Surface residency warnings as CDK Annotations (not just console.warn) so they
// appear in `cdk synth`/`cdk diff` and can be escalated via aspects. Attached to
// ecsStack because that is where the Bedrock-call env vars live. (Fable M-3/C-1)
for (const w of residencyWarnings) {
  cdk.Annotations.of(ecsStack).addWarning(w);
}

// --- 7b. Ledger Streams projector + reconciler (two-item migration step 1) ---
// Opt-in: needs the Lambda image (backend/Dockerfile.lambda) built + pushed to
// the backend ECR repo under LAMBDA_IMAGE_TAG. Gated on the `ledgerProjector`
// context flag so a normal deploy is unaffected until the image exists. Writes
// SHADOW# events by default (step 1); the async cut-over sets `-c projectorShadow=false`.
let ledgerProjectorStack: LedgerProjectorStack | undefined;
if (app.node.tryGetContext('ledgerProjector') === true ||
    app.node.tryGetContext('ledgerProjector') === 'true') {
  ledgerProjectorStack = new LedgerProjectorStack(app, stackName(prefix, 'ledger-projector'), {
    env,
    prefix,
    lambdaRepository: ecrStack.repository,
    lambdaImageTag: process.env.LAMBDA_IMAGE_TAG || process.env.IMAGE_TAG || 'latest',
    tenantBudgetsTable: dynamoDBStack.tenantBudgetsTable,
    creditLedgerTable: dynamoDBStack.creditLedgerTable,
    shadow: app.node.tryGetContext('projectorShadow') !== 'false',
    // Set PROJECTOR_EPOCH_MS to the projector's deploy time so the reconciler
    // excludes the pre-existing RESERVE backlog (no shadow by construction — the
    // stream starts at LATEST) from the divergence gate.
    projectorEpochMs: process.env.PROJECTOR_EPOCH_MS
      ? parseInt(process.env.PROJECTOR_EPOCH_MS, 10)
      : undefined,
    // ENRICHMENT_EPOCH_MS = the time the HOLD-enrichment deploy FINISHED rolling
    // out (step 3). The reconciler flags any post-epoch HOLD with no `source` —
    // the epoch-set-too-early detector that gates the capture/void HOLD-only
    // cut-over and the eventual RESERVE-event fallback deletion.
    enrichmentEpochMs: process.env.ENRICHMENT_EPOCH_MS
      ? parseInt(process.env.ENRICHMENT_EPOCH_MS, 10)
      : undefined,
    description: `[${prefix}] Ledger Streams RESERVE-event projector + reconciler (shadow)`,
  });
  ledgerProjectorStack.addDependency(ecrStack);
  ledgerProjectorStack.addDependency(dynamoDBStack);
}

// Daily Savings Certificate auto-issuer (litellm wedge slice-4). Same posture as
// the projector: a scheduled Lambda gated behind the `certificateScheduler`
// context flag, inert to a normal deploy until the image exists and the flag is
// set. Read-mostly + write-once (no request/money path). `certTenantIds` declares
// the coverage set explicitly (Fable slice-4 (a)); unset = enumerate tenants.
let certificateSchedulerStack: CertificateSchedulerStack | undefined;
if (app.node.tryGetContext('certificateScheduler') === true ||
    app.node.tryGetContext('certificateScheduler') === 'true') {
  const certTenantIds = app.node.tryGetContext('certTenantIds') as string | undefined;
  certificateSchedulerStack = new CertificateSchedulerStack(
    app, stackName(prefix, 'certificate-scheduler'), {
      env,
      prefix,
      lambdaRepository: ecrStack.repository,
      lambdaImageTag: process.env.LAMBDA_IMAGE_TAG || process.env.IMAGE_TAG || 'latest',
      routingSignalsTable: dynamoDBStack.routingSignalsTable,
      tenantBudgetsTable: dynamoDBStack.tenantBudgetsTable,
      tenantsTable: dynamoDBStack.tenantsTable,
      usageLogsTable: dynamoDBStack.usageLogsTable,
      certTenantIds,
      settleWindowDays: process.env.CERT_SETTLE_WINDOW_DAYS
        ? parseInt(process.env.CERT_SETTLE_WINDOW_DAYS, 10)
        : undefined,
      description: `[${prefix}] Daily Savings Certificate auto-issuer (slice-4)`,
    });
  certificateSchedulerStack.addDependency(ecrStack);
  certificateSchedulerStack.addDependency(dynamoDBStack);
}

// The three Lambda functions below (reconciler, period-rollover, grant sweeper)
// run backend/Dockerfile.lambda — a DIFFERENT image from the uvicorn one ECS
// runs (it bakes in the AWS Lambda Runtime Interface Client). The older
// scheduled-Lambda stacks above (ledgerProjectorStack, certificateSchedulerStack)
// resolve their tag as `LAMBDA_IMAGE_TAG || IMAGE_TAG || 'latest'`, which lets an
// operator who has never built backend/Dockerfile.lambda deploy anyway, silently
// pointing the Lambda at the ECS backend's own image — it starts, but the RIC
// entrypoint the backend image lacks means every invocation fails, not synth.
// Worse, the reverse mistake (pushing a Lambda image under the tag the backend
// is running) would retarget ECS's running image, and the ECR repository's
// IMMUTABLE tag policy does not stop that: it only blocks re-pushing a tag that
// already exists, not two different images sharing a fresh one.
//
// `resolveLambdaImageTag` closes both directions at synth time rather than
// leaving either to be discovered at runtime or by an accidental push: it
// requires an explicit, distinct tag instead of falling back to the backend's.
function resolveLambdaImageTag(stackKind: string): string {
  const tag = process.env.LAMBDA_IMAGE_TAG;
  if (!tag) {
    throw new Error(
      `LAMBDA_IMAGE_TAG must be set to deploy ${stackKind}. Its Lambda functions run ` +
        'backend/Dockerfile.lambda (the AWS Lambda Runtime Interface Client image), a ' +
        'different artifact from the ECS backend image that iac/scripts/build-and-push.sh ' +
        'now also builds and pushes. Run it, then export the LAMBDA_IMAGE_TAG it prints. ' +
        'See docs/DEPLOYMENT.md, "Post-deploy: quota gated stacks".',
    );
  }
  const backendTag = process.env.IMAGE_TAG || 'latest';
  if (tag === backendTag) {
    throw new Error(
      `LAMBDA_IMAGE_TAG ("${tag}") must not equal the ECS backend's IMAGE_TAG ` +
        `("${backendTag}"). They name different images sharing one ECR repository; a ` +
        'shared tag means a push meant for one can silently retarget the other. Use a ' +
        'distinct tag (build-and-push.sh prefixes its Lambda tag with "lambda-").',
    );
  }
  return tag;
}

// Daily tenant pool-ceiling reconciler. Its OWN stack, on the same convention as
// the projector and the certificate scheduler above: every scheduled job in this
// repository is its own stack, and putting this one on the service stack would
// have made it the exception. Same posture too — gated behind the
// `quotaReconciler` context flag, inert until the Lambda image exists.
let quotaReconcilerStack: QuotaReconcilerStack | undefined;
if (app.node.tryGetContext('quotaReconciler') === true ||
    app.node.tryGetContext('quotaReconciler') === 'true') {
  quotaReconcilerStack = new QuotaReconcilerStack(
    app, stackName(prefix, 'quota-reconciler'), {
      env,
      prefix,
      lambdaRepository: ecrStack.repository,
      lambdaImageTag: resolveLambdaImageTag('quota-reconciler'),
      tenantBudgetsTable: dynamoDBStack.tenantBudgetsTable,
      // The seat counts the pool rows are compared AGAINST. The reconciler exists
      // because an equation over the pool row alone cannot see a membership delta
      // applied twice, so the membership table is not optional here.
      userTenantsTable: dynamoDBStack.userTenantsTable,
      // grant_target_row_exists (mvp/grants.py) reads this on every pass; see the
      // prop doc on QuotaReconcilerStackProps for the AccessDeniedException a real
      // deploy hit without it.
      quotaEventsTable: dynamoDBStack.quotaEventsTable,
      description: `[${prefix}] Daily tenant pool-ceiling reconciliation against sources`,
    });
  quotaReconcilerStack.addDependency(ecrStack);
  quotaReconcilerStack.addDependency(dynamoDBStack);
}

// The sweep that ends granted capacity when its window closes. Its own stack, on
// the same convention as the reconciler above, and gated behind the same kind of
// context flag so a normal deploy is unaffected until the Lambda image exists.
//
// An APPEND rather than an edit anywhere above: this block adds a stack and touches
// nothing that was already here, which is what keeps a shared file like this one
// merging mechanically.
let quotaGrantsStack: QuotaGrantsStack | undefined;
if (app.node.tryGetContext('quotaGrants') === true ||
    app.node.tryGetContext('quotaGrants') === 'true') {
  quotaGrantsStack = new QuotaGrantsStack(
    app, stackName(prefix, 'quota-grants'), {
      env,
      prefix,
      lambdaRepository: ecrStack.repository,
      lambdaImageTag: resolveLambdaImageTag('quota-grants'),
      quotaEventsTable: dynamoDBStack.quotaEventsTable,
      // The rows a revocation moves. Not optional: a sweep that could read grants
      // and not write pools would take grants terminal while leaving their capacity
      // on the ceiling forever.
      tenantBudgetsTable: dynamoDBStack.tenantBudgetsTable,
      description: `[${prefix}] Revokes expired limit-raise grants and returns their capacity`,
    });
  quotaGrantsStack.addDependency(ecrStack);
  quotaGrantsStack.addDependency(dynamoDBStack);
}

// --- 8. Backend Config (static Parameter Store values) ---
class BackendConfigStack extends Stack {
  constructor(scope: Construct, id: string, stackProps: cdk.StackProps) {
    super(scope, id, stackProps);

    putStringParameter(this, 'DefaultOrgIdParam', {
      prefix,
      relativePath: 'backend/default-org-id',
      value: 'default-org',
      description: 'Default tenant/org ID',
    });
    putStringParameter(this, 'DefaultTenantCreditParam', {
      prefix,
      relativePath: 'backend/default-tenant-credit',
      value: '100000',
      description: 'Default tenant credit (string, parsed as int by backend)',
    });
    putStringParameter(this, 'DatabaseTypeParam', {
      prefix,
      relativePath: 'backend/database-type',
      value: 'dynamodb',
      description: 'Backend persistence type',
    });
    putStringParameter(this, 'AuthModeParam', {
      prefix,
      relativePath: 'backend/auth-mode',
      value: 'cognito',
      description: 'Backend authentication mode',
    });
    putStringParameter(this, 'BedrockRegionParam', {
      prefix,
      relativePath: 'bedrock/region',
      // Model primary region, independent of the deploy region (matches the
      // BEDROCK_REGION task env). NOT env.region.
      value: bedrockPrimaryRegion,
      description: 'Bedrock model primary region',
    });
    putStringParameter(this, 'DefaultBedrockModelParam', {
      prefix,
      relativePath: 'bedrock/default-model',
      value: defaultBedrockModel,
      description: 'Bedrock fallback model inference profile ID',
    });

    cdk.Tags.of(this).add('Project', 'Stratoclave');
    cdk.Tags.of(this).add('Prefix', prefix);
    cdk.Tags.of(this).add('Stack', 'Config');
    cdk.Tags.of(this).add('ManagedBy', 'CDK');
  }
}

new BackendConfigStack(app, stackName(prefix, 'config'), {
  env,
  description: `[${prefix}] Static Parameter Store values`,
});

// --- Global tags ---
cdk.Tags.of(app).add('Project', 'Stratoclave');
cdk.Tags.of(app).add('Prefix', prefix);
cdk.Tags.of(app).add('ManagedBy', 'CDK');

// --- cdk-nag (AWS Solutions) — run on every synth.
// Opt-out only with CDK_NAG=off for the odd debugging session; default is on
// so that regressions in security posture surface at CI time.
if ((process.env.CDK_NAG || 'on').toLowerCase() !== 'off') {
  cdk.Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));

  // Blanket suppressions for tradeoffs that are deliberate in the Stratoclave
  // design. Each entry documents *why* we are knowingly out of the rule's
  // default posture; narrow, construct-level suppressions stay alongside the
  // construct they apply to (see waf-stack.ts / cognito-stack.ts).
  const appLevelSuppressions = [
    {
      id: 'AwsSolutions-IAM4',
      reason:
        'AWS managed policies are used for ECS task execution (pull ECR, ship logs) and for the service-linked roles CDK creates for ALB/VPCFlowLogs. All scoped by account+service, no wildcard actions at the tenant data layer.',
    },
    {
      id: 'AwsSolutions-IAM5',
      reason:
        'Wildcard resource patterns are limited to the prefix-scoped DynamoDB tables and ECR repository we provision ourselves. Tenant isolation is enforced at the application layer (OIDC claims + Permissions table), not via IAM.',
    },
    {
      id: 'AwsSolutions-VPC7',
      reason:
        'VPC Flow Logs are enabled (CloudWatch, 30-day retention) in network-stack.ts. cdk-nag misreports when the destination is CloudWatch instead of S3.',
    },
    {
      id: 'AwsSolutions-EC23',
      reason:
        'ALB inbound 80/443 is restricted to the com.amazonaws.global.cloudfront.origin-facing managed prefix list in network-stack.ts, not 0.0.0.0/0. cdk-nag cannot distinguish prefix-list CIDRs from "any" so flags it anyway.',
    },
    {
      id: 'AwsSolutions-ELB2',
      reason:
        'ALB access logs are not enabled; VPC Flow Logs + CloudFront logs at the edge already give us request-level forensics without doubling S3 cost.',
    },
    {
      id: 'AwsSolutions-L1',
      reason:
        'The flagged Lambda is the framework-provided AwsCustomResource handler (CloudFront prefix-list lookup, S3 autoDeleteObjects). Its runtime is controlled by CDK itself and updates automatically on the next cdk upgrade.',
    },
    {
      id: 'AwsSolutions-DDB3',
      reason:
        'Point-in-time recovery is enabled on all audit/billing tables (usage-logs, api-keys) and on everything in production. Dev-only tables for ephemeral state (sessions, messages, sse-tokens) are intentionally excluded to keep cost predictable on throwaway stacks.',
    },
    {
      id: 'AwsSolutions-S1',
      reason:
        'S3 server access logs are disabled by design. CloudFront sits in front with its own edge request logs, and the bucket is private + OAC-restricted so only SigV4 GetObject from that distribution is accepted. Duplicating access logs in S3 adds cost with no forensic gain.',
    },
    {
      id: 'AwsSolutions-CFR1',
      reason:
        'CloudFront geo restriction is intentionally not enabled: Stratoclave is a tenant-scoped auth-gated API where access control is enforced at Cognito + WAF managed rules + application-level RBAC. Geo blocking would only hurt remote team members.',
    },
    {
      id: 'AwsSolutions-CFR2',
      reason:
        'WAF integration is wired up in bin/iac.ts when ENABLE_WAF is on (the default). cdk-nag runs before the cross-stack webAclArn resolves, so this rule fires spuriously on the first synth pass.',
    },
    {
      id: 'AwsSolutions-CFR3',
      reason:
        'CloudFront standard logs are disabled because the deprecated CloudFront log delivery model requires a named S3 bucket + ACL — we rely on WAF sampled requests + CloudWatch metrics for traffic forensics instead.',
    },
    {
      id: 'AwsSolutions-CFR4',
      reason:
        'The viewer certificate is the default cloudfront.net cert, whose minimum TLS policy cannot be raised below TLSv1. We explicitly set minimumProtocolVersion=TLSv1.2_2021 so that any custom-domain rollout will pick up modern TLS automatically. No custom domain is attached today.',
    },
    {
      id: 'AwsSolutions-CFR5',
      reason:
        'Origin protocol policy is http-only because the backend ALB is HTTP-only by design (HTTPS is terminated at CloudFront, and the ALB SG only accepts the CloudFront origin-facing prefix list — see network-stack.ts).',
    },
    {
      id: 'AwsSolutions-COG2',
      reason:
        'MFA is not enabled. Turning it on requires an email/SMS channel rollout with its own provisioning surface, so it is a deployment decision rather than a stack default.',
    },
    {
      id: 'AwsSolutions-COG3',
      reason:
        'Cognito AdvancedSecurityMode is not enabled, for the same reason as MFA above: it is priced and configured per deployment rather than fixed by this stack.',
    },
    {
      id: 'AwsSolutions-COG8',
      reason:
        'Cognito User Pool tier upgrade (Essentials -> Plus) is deferred alongside MFA and AdvancedSecurityMode. The Plus tier unlocks advanced security features that we have already chosen to defer in COG2 / COG3.',
    },
    {
      id: 'AwsSolutions-ECS2',
      reason:
        'The ECS task environment variables injected here are all non-secret: table names, region, prefix, feature flags. Secrets (Cognito user pool id is public; there are no long-lived keys) do not pass through env at all.',
    },
    {
      id: 'AwsSolutions-SMG4',
      reason:
        'BootstrapAdminTempPasswordSecret is single-use: the operator reads it exactly once and rotates the admin password through Cognito (`admin-set-user-password`) immediately. Secrets Manager rotation does not apply to a placeholder that is overwritten by the seed code on first boot, and there is no managed service that knows how to rotate a temporary Cognito password on our behalf.',
    },
    {
      id: 'AwsSolutions-SQS3',
      reason:
        'The ledger-projector queue IS the terminal dead-letter queue for the Streams event-source mapping (OnFailure). A DLQ does not need its own DLQ; a record that lands here is already the end of the retry chain and is surfaced by the DLQ-depth alarm.',
    },
  ];
  NagSuppressions.addStackSuppressions(networkStack, appLevelSuppressions);
  NagSuppressions.addStackSuppressions(albStack, appLevelSuppressions);
  NagSuppressions.addStackSuppressions(ecsStack, appLevelSuppressions);
  NagSuppressions.addStackSuppressions(dynamoDBStack, appLevelSuppressions);
  NagSuppressions.addStackSuppressions(frontendStack, appLevelSuppressions);
  NagSuppressions.addStackSuppressions(cognitoStack, appLevelSuppressions);
  if (wafStack) {
    NagSuppressions.addStackSuppressions(wafStack, appLevelSuppressions);
  }
  if (ledgerProjectorStack) {
    NagSuppressions.addStackSuppressions(ledgerProjectorStack, appLevelSuppressions);
  }
  if (certificateSchedulerStack) {
    NagSuppressions.addStackSuppressions(certificateSchedulerStack, appLevelSuppressions);
  }
  // The same app-level suppressions every other scheduled-Lambda stack here takes:
  // the managed basic-execution policy, and the `/index/*` wildcard a table grant
  // necessarily produces for a job that reads through a GSI.
  //
  // `quotaReconcilerStack` carries the identical findings (AwsSolutions-IAM4 x2,
  // AwsSolutions-IAM5 x1) and is suppressed here on the same list. A previous
  // version of this comment predicted exactly this stack would fail a real
  // deploy because it was missing from this block and `cdk synth` was not part
  // of the jest suite, so the gap was invisible until somebody deployed it — it
  // did, and it failed with those three errors. `nag-synth.test.ts` now runs a
  // real `cdk synth --all` with every context-gated stack enabled, specifically
  // so a stack missing from this list fails CI instead of a real deploy.
  if (quotaReconcilerStack) {
    NagSuppressions.addStackSuppressions(quotaReconcilerStack, appLevelSuppressions);
  }
  if (quotaGrantsStack) {
    NagSuppressions.addStackSuppressions(quotaGrantsStack, appLevelSuppressions);
  }
}

console.error(`[stratoclave-iac] prefix=${prefix} bodyRegion=${env.region} wafRegion=${regionCfg.wafRegion} bedrockPrimary=${bedrockPrimaryRegion} bedrockModel=${defaultBedrockModel}`);
console.error(`[stratoclave-iac] Parameter Store base: ${paramPath(prefix, '')}`);
console.error(`[stratoclave-iac] ALLOW_ADMIN_CREATION=${allowAdminCreation}`);
console.error(`[stratoclave-iac] enableWaf=${enableWaf} cdkNag=${process.env.CDK_NAG || 'on'}`);
