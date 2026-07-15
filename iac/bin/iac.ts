#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { AwsSolutionsChecks, NagSuppressions } from 'cdk-nag';
import { getPrefix, stackName, paramPath, putStringParameter } from '../lib/_common';
import { NetworkStack } from '../lib/network-stack';
import { EcrStack } from '../lib/ecr-stack';
import { AlbStack } from '../lib/alb-stack';
import { EcsStack } from '../lib/ecs-stack';
import { FrontendStack } from '../lib/frontend-stack';
import { CognitoStack } from '../lib/cognito-stack';
import { DynamoDBStack } from '../lib/dynamodb-stack';
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
const DEFAULT_REGION = 'us-east-1'; // historical single-region default (body)
const WAF_REGION = 'us-east-1'; // AWS hard requirement: CLOUDFRONT-scope WebACL

// Reject non-region strings AND unsupported partitions. CloudFront (hence the
// WAF stack) does not exist in the GovCloud / China partitions, and a single
// app/credential set cannot span partitions, so a us-gov-/cn- body region would
// fail confusingly mid-deploy. Fail loudly at synth instead. (Fable review M-2)
function assertRegion(label: string, value: string): void {
  if (!/^[a-z]{2}(-[a-z]+)+-\d$/.test(value)) {
    throw new Error(
      `Invalid ${label} "${value}" (expected an AWS region id like "us-east-1" / "eu-west-1").`
    );
  }
  if (value.startsWith('us-gov-') || value.startsWith('cn-')) {
    throw new Error(
      `${label} "${value}": Stratoclave supports the "aws" partition only ` +
        `(GovCloud / China partitions have no CloudFront for the WAF stack).`
    );
  }
}

// Body region R. STRATOCLAVE_REGION wins; else the CDK ambient region; else
// us-east-1 (preserves the historical single-region default → zero diff on
// existing deployments).
const bodyRegion =
  process.env.STRATOCLAVE_REGION || process.env.CDK_DEFAULT_REGION || DEFAULT_REGION;
assertRegion('deploy region (STRATOCLAVE_REGION / CDK_DEFAULT_REGION)', bodyRegion);

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: bodyRegion,
};

// WAF lives in us-east-1 regardless of the body region. NEVER derive this from
// bodyRegion — pinning WAF to R fails loudly at deploy (WAFv2 CLOUDFRONT scope
// must be us-east-1), but keeping it a distinct literal makes the intent
// unmistakable and immune to a copy-paste that reuses `env`.
const wafEnv = { account: process.env.CDK_DEFAULT_ACCOUNT, region: WAF_REGION };

const cognitoEnv = env; // same region as the body: Cognito is region-agnostic

// Bedrock model *primary* region — independent of the deploy region (Goal 2).
// Mirrors the OPENAI_BEDROCK_REGIONS precedent (gateway region != model region).
// When body != us-east-1 the operator MUST declare it: there is no correct
// default when the regions diverge, and silently falling back to AWS_REGION (=R)
// would call Bedrock in a region that may not host the model (silent 404 / wrong
// variant under load). Refuse to guess.
const bedrockPrimaryRegionRaw =
  process.env.BEDROCK_PRIMARY_REGION ||
  (bodyRegion === DEFAULT_REGION ? DEFAULT_REGION : undefined);
if (!bedrockPrimaryRegionRaw) {
  throw new Error(
    `BEDROCK_PRIMARY_REGION must be set explicitly when the deploy region ` +
      `(${bodyRegion}) != us-east-1. Refusing to guess the Bedrock model region. ` +
      `NOTE: this is also required for \`cdk bootstrap\` (bootstrap synthesizes ` +
      `this app); set BEDROCK_PRIMARY_REGION=<model-region> before bootstrapping, ` +
      `or bootstrap without synth via \`cdk bootstrap --app "" aws://<acct>/${bodyRegion}\`.`
  );
}
// Guaranteed-string binding: the narrowing above is lost inside the
// BackendConfigStack class closure, so bind it here for use everywhere.
const bedrockPrimaryRegion: string = bedrockPrimaryRegionRaw;
// Validate the model region too — it flows to the ECS BEDROCK_REGION env and
// the SSM param; a garbage value would fail only at runtime. (Fable review H-1)
assertRegion('BEDROCK_PRIMARY_REGION', bedrockPrimaryRegion);

// Default Bedrock model (Backend mapping fallback). Defined here (earlier than
// its ECS use) so the residency analysis can inspect its inference-profile
// prefix. (Fable review NEW-9)
const defaultBedrockModel =
  process.env.DEFAULT_BEDROCK_MODEL || 'us.anthropic.claude-opus-4-7';

// --- Residency leak analysis (Fable review C-1, corrected for NEW-1/NEW-3) --
// The dangerous case is model == body == eu-west-1 while the operator FORGETS
// the OTHER regions Bedrock is actually called in. We reconstruct the TRUE set
// of runtime regions from the ACTUAL sources of truth, not from display hints:
//
//   * Claude failover  -> STRATOCLAVE_FAILOVER_REGIONS (default us-west-2 +
//                         eu-west-1). Governs routing (mvp/routing/chains.py).
//   * OpenAI/codex     -> HARDCODED per-model regions in the backend registry
//                         (mvp/models.py: gpt-5.4=us-west-2, gpt-5.5=us-east-2).
//                         NEW-1: OPENAI_BEDROCK_REGIONS is a DISPLAY-ONLY hint
//                         (read only in well_known.py) — it does NOT move the
//                         codex call region, so it must NOT feed this analysis.
//                         The only residency lever for codex is CODEX_ENABLED.
//
// Residency here means STRICT SINGLE-REGION (exact-region equality), NOT a
// jurisdiction/prefix heuristic — NEW-3: "eu"/"ap" prefixes span legal
// boundaries (UK=eu-west-2, CH=eu-central-2; ap-southeast-2 AU vs ap-northeast-1
// JP), so a prefix match cannot certify residency. Anything other than the exact
// deploy region is treated as a leak.
const DEFAULT_FAILOVER_REGIONS = ['us-west-2', 'eu-west-1']; // mirror mvp/routing/chains.py
const FAILOVER_DISABLE_SENTINELS = new Set(['', 'none', 'disabled', 'off']);
// Hardcoded in mvp/models.py — the codex path calls bedrock-mantle in these
// regions regardless of OPENAI_BEDROCK_REGIONS. Update BOTH if the registry
// changes (a cross-repo drift test guards this — see backend tests).
const OPENAI_REGISTRY_REGIONS = ['us-west-2', 'us-east-2'];

// Pass the failover knob through to the backend ONLY when the operator set it,
// so unset preserves the backend's own historical default.
const failoverRegionsEnv = process.env.STRATOCLAVE_FAILOVER_REGIONS;
const codexEnabled = (process.env.CODEX_ENABLED || 'true').toLowerCase() !== 'false';

function parseRegionList(raw: string): string[] {
  return raw
    .split(',')
    .map((r) => r.trim())
    .filter((r) => r.length > 0);
}

const effectiveFailover =
  failoverRegionsEnv === undefined
    ? DEFAULT_FAILOVER_REGIONS
    : FAILOVER_DISABLE_SENTINELS.has(failoverRegionsEnv.trim().toLowerCase())
      ? []
      : parseRegionList(failoverRegionsEnv);

// Validate every failover region token that reaches the backend (H-1 continued).
for (const r of effectiveFailover) assertRegion('STRATOCLAVE_FAILOVER_REGIONS entry', r);

// Every region a PROMPT can actually reach at runtime, tagged with its source.
const bedrockCallRegions: { region: string; source: string }[] = [
  { region: bedrockPrimaryRegion, source: 'model' },
  ...effectiveFailover.map((r) => ({ region: r, source: 'failover' })),
  ...(codexEnabled
    ? OPENAI_REGISTRY_REGIONS.map((r) => ({ region: r, source: 'codex' }))
    : []),
];
// STRICT single-region residency: any region != the deploy region is a leak.
const residencyLeaks = Array.from(
  new Set(bedrockCallRegions.filter((c) => c.region !== bodyRegion).map((c) => `${c.region}(${c.source})`))
);

const residencyRaw = (process.env.STRATOCLAVE_RESIDENCY || '').toLowerCase();
if (residencyRaw && residencyRaw !== 'strict' && residencyRaw !== 'warn') {
  // NEW-6: reject typos so 'strickt'/'off' can't silently downgrade the guard.
  throw new Error(
    `STRATOCLAVE_RESIDENCY must be "strict" or "warn" (got "${process.env.STRATOCLAVE_RESIDENCY}").`
  );
}
const residencyStrict = residencyRaw === 'strict';

// The leak analysis only runs when the operator has signaled residency intent —
// either by deliberately choosing a non-default body region (nobody deploys to
// eu-west-1 by accident) or by setting STRATOCLAVE_RESIDENCY. This keeps the
// historical us-east-1 default deploy SILENT (its default failover reaches
// eu-west-1 — benign for a US operator, never flagged before → backward
// compatible), while still catching Fable's trap. (C-1, refined)
const residencyIntent =
  bodyRegion !== DEFAULT_REGION || process.env.STRATOCLAVE_RESIDENCY !== undefined;

// Geo cross-region inference profiles (NEW-9): a model id prefixed `us.` / `eu.`
// / `apac.` / `global.` is a GEOGRAPHY profile — AWS routes inference to any
// region within that geography at its discretion, so the configured region can
// NOT certify the actual inference region. `global.` is worst (any region on
// Earth). Under strict residency we must refuse these; the exact-region check
// alone would otherwise falsely certify (e.g. default model
// `us.anthropic.claude-opus-4-7` in an eu-west-1 deploy). An explicit escape
// hatch (STRATOCLAVE_ALLOW_GEO_INFERENCE=true) downgrades to a warning for
// operators who accept geo-level (not region-level) residency.
// Denylist of geo (cross-region) inference-profile prefixes. This is a denylist
// and will rot silently as AWS ships new geographies — add prefixes as they
// appear. `us-gov.` is included for completeness though the gov partition is
// rejected earlier. (Fable review NEW-15)
const GEO_PROFILE_RE = /^(us|eu|apac|global|us-gov)\./;
const modelIsGeoProfile = GEO_PROFILE_RE.test(defaultBedrockModel);
const allowGeoInference =
  (process.env.STRATOCLAVE_ALLOW_GEO_INFERENCE || '').toLowerCase() === 'true';

// Messages are surfaced as CDK Annotations on ecsStack (below) so they appear
// in `cdk synth`/`cdk diff` output, not just a scrollable console line. (M-3)
const residencyWarnings: string[] = [];
// Model-region != deploy-region is always noteworthy (prompt bytes leave R),
// independent of residency intent.
if (bedrockPrimaryRegion !== bodyRegion) {
  residencyWarnings.push(
    `[residency] prompt data leaves the deploy region ${bodyRegion}: ` +
      `Bedrock primary = ${bedrockPrimaryRegion}.`
  );
}
// Geo-profile residency check: only meaningful under residency intent.
if (residencyIntent && modelIsGeoProfile) {
  const geoMsg =
    `[residency] DEFAULT_BEDROCK_MODEL="${defaultBedrockModel}" is a geo cross-region ` +
    `inference profile — AWS routes inference anywhere within its geography, so a ` +
    `single-region residency guarantee for the deploy region ${bodyRegion} cannot ` +
    `be made. Use a directly-hosted (region-specific, non-"us./eu./apac./global."-` +
    `prefixed) model id, or set STRATOCLAVE_ALLOW_GEO_INFERENCE=true to accept ` +
    `geography-level (not region-level) residency.`;
  if (residencyStrict && !allowGeoInference) {
    throw new Error(
      `STRATOCLAVE_RESIDENCY=strict: refusing to synth — model is a geo inference ` +
        `profile.\n${geoMsg}`
    );
  }
  residencyWarnings.push(geoMsg);
}
if (residencyIntent && residencyLeaks.length > 0) {
  const hints: string[] = [];
  if (effectiveFailover.some((r) => r !== bodyRegion)) {
    hints.push('set STRATOCLAVE_FAILOVER_REGIONS=disabled (or to same-region only)');
  }
  if (codexEnabled && OPENAI_REGISTRY_REGIONS.some((r) => r !== bodyRegion)) {
    hints.push(
      `set CODEX_ENABLED=false (the OpenAI/codex path is hardwired to ` +
        `${OPENAI_REGISTRY_REGIONS.join(', ')} in the model registry and cannot be relocated)`
    );
  }
  if (bedrockPrimaryRegion !== bodyRegion) {
    hints.push(`set BEDROCK_PRIMARY_REGION=${bodyRegion}`);
  }
  const msg =
    `[residency] prompts can reach region(s) other than the deploy region ` +
    `${bodyRegion}: ${residencyLeaks.join(', ')}. ` +
    `For strict single-region residency: ${hints.join('; ')}.`;
  if (residencyStrict) {
    throw new Error(
      `STRATOCLAVE_RESIDENCY=strict: refusing to synth — Bedrock is reachable ` +
        `outside the deploy region.\n${msg}`
    );
  }
  residencyWarnings.push(msg);
}

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
const wafRateLimit = Number(process.env.WAF_RATE_LIMIT_PER_5MIN || 300);
// AWS WAF rate-based rule minimum is 100 req / 5 min; a typo like "3oo" yields
// NaN and fails at CFN deploy with an unhelpful message. Fail at synth instead.
// (Fable review L-3)
if (!Number.isInteger(wafRateLimit) || wafRateLimit < 100) {
  throw new Error(
    `WAF_RATE_LIMIT_PER_5MIN must be an integer >= 100 (got "${process.env.WAF_RATE_LIMIT_PER_5MIN}").`
  );
}
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
  wafStack = new WafStack(app, stackName(prefix, 'waf'), {
    env: wafEnv,
    crossRegionReferences: true,
    prefix,
    rateLimitPer5Min: wafRateLimit,
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
const ecsStack = new EcsStack(app, stackName(prefix, 'ecs'), {
  env,
  prefix,
  vpc: networkStack.vpc,
  securityGroup: networkStack.ecsSecurityGroup,
  repository: ecrStack.repository,
  targetGroup: albStack.targetGroup,
  userPoolArn: cognitoStack.userPool.userPoolArn,
  dynamoDbTableArns: dynamoDBStack.allTableArns,
  cpu: 256,
  memory: 512,
  // Two tasks so the ECS service spreads across both AZs (the VPC has
  // maxAzs=2): no single task or AZ is a SPOF, and rolling deploys keep
  // a task serving. Safe now that per-IP rate limits live in DynamoDB
  // (no in-memory state that a second task would diverge on); budget
  // reserve/settle was already atomic in DynamoDB, and the InfraRouter
  // cooldown map / config cache are per-task advisory by design.
  desiredCount: 2,
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
    DYNAMODB_PRICING_CONFIG_TABLE: dynamoDBStack.pricingConfigTable.tableName,
    // Per-IP rate-limit counters, shared across ECS tasks (multi-task/AZ safe).
    DYNAMODB_RATE_LIMITS_TABLE: dynamoDBStack.rateLimitsTable.tableName,
    // P0-11: per-model quota counters (charged atomically with the budget pool).
    DYNAMODB_MODEL_QUOTAS_TABLE: dynamoDBStack.modelQuotasTable.tableName,
    // P0-13/14: dual-track observability (span records + workflow_run rollups).
    DYNAMODB_OBSERVABILITY_TABLE: dynamoDBStack.observabilityTable.tableName,
    // P0-16: routing-signals write-only seam (writer live; consumer stubbed).
    DYNAMODB_ROUTING_SIGNALS_TABLE: dynamoDBStack.routingSignalsTable.tableName,

    // CORS
    CORS_ORIGINS: `https://${frontendStack.cfnDistribution.attrDomainName}`,

    // Public API endpoint (CloudFront HTTPS URL).
    // This is the value returned as api_endpoint in /.well-known/stratoclave-config.
    // Always return the CloudFront URL; returning the ALB URL directly would cause the CLI to call it over HTTP.
    STRATOCLAVE_API_ENDPOINT: `https://${frontendStack.cfnDistribution.attrDomainName}`,

    // Feature flags (MVP)
    VERIFIED_PERMISSIONS_ENABLED: 'false',
    TENANT_ISOLATION_ENABLED: 'false',
    RATE_LIMIT_ENABLED: 'true',
    ADMIN_API_RATE_LIMIT: '60/minute',
    TEAM_API_RATE_LIMIT: '30/minute',
    USAGE_API_RATE_LIMIT: '10/minute',

    // Phase 2 (v2.1): Admin creation gate (Critical C-D).
    // P1-A: in production, ALLOW_ADMIN_CREATION_UNTIL=<epoch> is also required.
    // An empty string is treated as unset and the backend will reject the request.
    ALLOW_ADMIN_CREATION: allowAdminCreation,
    ALLOW_ADMIN_CREATION_UNTIL: allowAdminCreationUntil,

    // Tenant
    DEFAULT_ORG_ID: 'default-org',
    DEFAULT_TENANT_CREDIT: '100000',

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

    // OpenAI (codex / GPT-5.x) on Amazon Bedrock — bedrock-mantle endpoint.
    // GPT-5.4 / GPT-5.5 are GA only in us-east-2 / us-west-2; the route handler
    // in mvp/openai_responses.py picks the per-model region out of the model
    // REGISTRY (mvp/models.py), NOT from OPENAI_BEDROCK_REGIONS — that var is a
    // DISPLAY-ONLY hint surfaced to the CLI via /.well-known/stratoclave-config
    // (read only in well_known.py). RESIDENCY NOTE: the codex path is therefore
    // hardwired to us-east-2/us-west-2 and cannot be relocated by an env var —
    // the only residency lever for codex is CODEX_ENABLED=false (see the
    // residency analysis above).
    // Pass the SAME normalized boolean the residency analysis used (`codexEnabled`),
    // not the raw operator string. This makes the task-def value provably equal to
    // what STRATOCLAVE_RESIDENCY=strict assumed — otherwise strict could pass with
    // codex "off" at synth while the container runs it "on" (NEW-8). The backend
    // parses it as `.lower() == "true"`, so 'true'/'false' are exact. (NEW-8/NEW-11)
    CODEX_ENABLED: String(codexEnabled),
    DEFAULT_CODEX_MODEL: process.env.DEFAULT_CODEX_MODEL || 'openai.gpt-5.4',
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
        'MFA is deferred (tracked in HANDOVER_SECURITY_HARDENING.md). Enabling it now requires an email/SMS channel rollout that has its own provisioning surface.',
    },
    {
      id: 'AwsSolutions-COG3',
      reason:
        'Cognito AdvancedSecurityMode is deferred alongside MFA (HANDOVER_SECURITY_HARDENING.md). Non-essentials get punted until after the comprehensive audit lands.',
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
}

console.error(`[stratoclave-iac] prefix=${prefix} bodyRegion=${env.region} wafRegion=${WAF_REGION} bedrockPrimary=${bedrockPrimaryRegion} bedrockModel=${defaultBedrockModel}`);
console.error(`[stratoclave-iac] Parameter Store base: ${paramPath(prefix, '')}`);
console.error(`[stratoclave-iac] ALLOW_ADMIN_CREATION=${allowAdminCreation}`);
console.error(`[stratoclave-iac] enableWaf=${enableWaf} cdkNag=${process.env.CDK_NAG || 'on'}`);
