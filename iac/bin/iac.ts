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
 *   a cross-stack reference, hence the Frontend dependency. All stacks
 *   live in us-east-1, so crossRegionReferences is not needed.
 *
 * Retired stacks (kept under iac/lib/_archived/): RdsStack, RedisStack,
 *   WafStack (now re-introduced under a different layout), CodeBuildStack,
 *   FrontendCodeBuildStack, VerifiedPermissionsStack.
 */

const app = new cdk.App();
const prefix = getPrefix();

const DEFAULT_REGION = 'us-east-1';

// Blocker B2 (v2.1): CDK_DEFAULT_REGION must be us-east-1.
// All stacks live in one region so crossRegionReferences is not needed
// for the Cognito Hosted UI ↔ Frontend cross-stack reference.
const cdkRegion = process.env.CDK_DEFAULT_REGION || DEFAULT_REGION;
if (cdkRegion !== DEFAULT_REGION) {
  throw new Error(
    `CDK_DEFAULT_REGION must be "${DEFAULT_REGION}" for Stratoclave (got "${cdkRegion}"). ` +
      `Cognito Hosted UI / cross-stack references require all stacks to live in one region.`
  );
}

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: cdkRegion,
};

const cognitoEnv = env; // Unified to the same region (v2.1)

const cognitoDomainPrefix = process.env.COGNITO_DOMAIN_PREFIX; // optional (auto-generated if not specified)

// Default Bedrock model (Backend mapping fallback)
const defaultBedrockModel =
  process.env.DEFAULT_BEDROCK_MODEL ||
  'us.anthropic.claude-opus-4-7';

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

// --- 5a. WAF (CLOUDFRONT scope, fixed to us-east-1).
// We already enforce env.region === 'us-east-1' above, so the WAF stack sits
// in the same region as everything else and no cross-region reference is
// needed. The WebACL ARN is passed to FrontendStack via props.
let wafStack: WafStack | undefined;
if (enableWaf) {
  wafStack = new WafStack(app, stackName(prefix, 'waf'), {
    env,
    prefix,
    rateLimitPer5Min: wafRateLimit,
    ipAllowlistEnabled: wafIpAllowlistEnabled,
    description: `[${prefix}] WAFv2 WebACL for CloudFront (rate-limit + managed rules)`,
  });
}

// --- 5. Frontend (S3 + CloudFront + SPA fallback Function) ---
const frontendStack = new FrontendStack(app, stackName(prefix, 'frontend'), {
  env,
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
// Blocker B2 (v2.1): crossRegionReferences removed (same us-east-1 region)
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

// --- 7. ECS (placed directly in the Public Subnet) ---
// Blocker B2 (v2.1): crossRegionReferences removed
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

    // Bedrock (Anthropic / Claude)
    BEDROCK_REGION: env.region,
    DEFAULT_BEDROCK_MODEL: defaultBedrockModel,

    // OpenAI (codex / GPT-5.x) on Amazon Bedrock — bedrock-mantle endpoint.
    // GPT-5.4 / GPT-5.5 are GA only in us-east-2 and us-west-2 today; the
    // route handler in mvp/openai_responses.py picks the per-model region
    // out of the model registry, so this list is purely a hint surfaced
    // to the CLI through /.well-known/stratoclave-config.
    CODEX_ENABLED: process.env.CODEX_ENABLED || 'true',
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
      value: env.region,
      description: 'Bedrock region',
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

console.error(`[stratoclave-iac] prefix=${prefix} region=${env.region} bedrockModel=${defaultBedrockModel}`);
console.error(`[stratoclave-iac] Parameter Store base: ${paramPath(prefix, '')}`);
console.error(`[stratoclave-iac] ALLOW_ADMIN_CREATION=${allowAdminCreation}`);
console.error(`[stratoclave-iac] enableWaf=${enableWaf} cdkNag=${process.env.CDK_NAG || 'on'}`);
