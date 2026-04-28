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
 * 構成:
 *   - Network (Public Subnet 2 AZ、NAT なし)
 *   - DynamoDB (10 テーブル、Tenants/Permissions を Phase 2 で追加)
 *   - ECR
 *   - ALB (internet-facing, HTTP only)
 *   - Frontend (S3 + CloudFront + CloudFront Function SPA fallback)
 *   - Cognito User Pool (Frontend の CloudFront ドメインを cross-stack 参照)
 *   - ECS Fargate (Public Subnet 直置き)
 *   - BackendConfig (Parameter Store 固定値)
 *
 * 依存順序 (v2.1): network → dynamodb → ecr → alb → frontend → cognito → ecs → config
 *   Cognito の Callback URL に CloudFront ドメインを cross-stack で渡すため Frontend に依存。
 *   全スタックが同 us-east-1 のため crossRegionReferences は不要。
 *
 * 撤去したもの: RdsStack, RedisStack, WafStack, CodeBuildStack,
 *               FrontendCodeBuildStack, VerifiedPermissionsStack
 *               (iac/lib/_archived/ に退避保管)
 */

const app = new cdk.App();
const prefix = getPrefix();

const DEFAULT_REGION = 'us-east-1';

// Blocker B2 (v2.1): CDK_DEFAULT_REGION=us-east-1 を強制
// 全スタックを同一リージョンに揃え、crossRegionReferences を不要にする
const cdkRegion = process.env.CDK_DEFAULT_REGION || DEFAULT_REGION;
if (cdkRegion !== DEFAULT_REGION) {
  throw new Error(
    `CDK_DEFAULT_REGION must be "${DEFAULT_REGION}" for Stratoclave (got "${cdkRegion}"). ` +
      `Cognito Hosted UI と他スタック間の cross-stack 参照を成立させるため同一リージョン必須。`
  );
}

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: cdkRegion,
};

const cognitoEnv = env; // 同一リージョンに統一 (v2.1)

const cognitoDomainPrefix = process.env.COGNITO_DOMAIN_PREFIX; // optional (未指定なら自動生成)

// Bedrock デフォルトモデル (Backend のマッピング fallback)
const defaultBedrockModel =
  process.env.DEFAULT_BEDROCK_MODEL ||
  'us.anthropic.claude-opus-4-7';

// Admin 作成ゲート (Critical C-D): production では bootstrap 後に unset する運用
const allowAdminCreation = process.env.ALLOW_ADMIN_CREATION || 'false';

// Environment flag drives production-only knobs (deletion protection,
// retain-on-delete tables, stricter cdk-nag rules).
const envName = process.env.ENVIRONMENT || 'development';
const isProd = envName === 'production';

// P1-2 WAF: set ENABLE_WAF=false only for throwaway stacks. Default is on —
// without WAF, /api/* is exposed with no rate limit or managed-rule coverage.
const enableWaf = (process.env.ENABLE_WAF || 'true').toLowerCase() !== 'false';
const wafRateLimit = Number(process.env.WAF_RATE_LIMIT_PER_5MIN || 300);
const wafIpAllowlistEnabled =
  (process.env.WAF_IP_ALLOWLIST_ENABLED || 'false').toLowerCase() === 'true';

// --- 1. Network (Public Subnet 2 AZ、NAT なし) ---
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

// --- 5a. WAF (CLOUDFRONT scope → us-east-1 固定).
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

// --- 6. Cognito (Frontend の CloudFront ドメインを cross-stack 参照) ---
// Blocker B2 (v2.1): crossRegionReferences は削除 (同一 us-east-1)
const cognitoStack = new CognitoStack(app, stackName(prefix, 'cognito'), {
  env: cognitoEnv,
  prefix,
  domainPrefix: cognitoDomainPrefix,
  cloudFrontDomainName: frontendStack.cfnDistribution.attrDomainName,
  description: `[${prefix}] Cognito User Pool (Hosted UI, User/Pass auth for CLI)`,
});
cognitoStack.addDependency(frontendStack);

// --- 7. ECS (Public Subnet 直置き) ---
// Blocker B2 (v2.1): crossRegionReferences は削除
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
  desiredCount: 1, // in-memory state 前提、単一タスク運用
  containerPort: 8000,
  environment: {
    ENVIRONMENT: envName,
    STRATOCLAVE_PREFIX: prefix,
    AWS_REGION: env.region,

    // Backend 実行モード
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

    // DynamoDB テーブル名
    DYNAMODB_USERS_TABLE: dynamoDBStack.usersTable.tableName,
    DYNAMODB_USER_TENANTS_TABLE: dynamoDBStack.userTenantsTable.tableName,
    DYNAMODB_USAGE_LOGS_TABLE: dynamoDBStack.usageLogsTable.tableName,
    DYNAMODB_SESSIONS_TABLE: dynamoDBStack.sessionsTable.tableName,
    DYNAMODB_MESSAGES_TABLE: dynamoDBStack.messagesTable.tableName,
    DYNAMODB_APP_SETTINGS_TABLE: dynamoDBStack.appSettingsTable.tableName,
    DYNAMODB_TAGS_TABLE: dynamoDBStack.tagsTable.tableName,
    DYNAMODB_SSE_TOKENS_TABLE: dynamoDBStack.sseTokensTable.tableName,
    // Phase 2 新規テーブル
    DYNAMODB_TENANTS_TABLE: dynamoDBStack.tenantsTable.tableName,
    DYNAMODB_PERMISSIONS_TABLE: dynamoDBStack.permissionsTable.tableName,
    // Phase S: AWS SSO / STS ログイン用テーブル
    DYNAMODB_TRUSTED_ACCOUNTS_TABLE: dynamoDBStack.trustedAccountsTable.tableName,
    DYNAMODB_SSO_PRE_REGISTRATIONS_TABLE:
      dynamoDBStack.ssoPreRegistrationsTable.tableName,
    // Phase C: 長期 API Key (cowork 等の gateway クライアント用)
    DYNAMODB_API_KEYS_TABLE: dynamoDBStack.apiKeysTable.tableName,

    // CORS
    CORS_ORIGINS: `https://${frontendStack.cfnDistribution.attrDomainName}`,

    // 公開 API エンドポイント (CloudFront の HTTPS URL)
    // /.well-known/stratoclave-config の api_endpoint として返す値。
    // ALB 直 URL を返すと CLI が HTTP 経由で叩きに行くため、必ず CloudFront URL を返す。
    STRATOCLAVE_API_ENDPOINT: `https://${frontendStack.cfnDistribution.attrDomainName}`,

    // Feature flags (MVP)
    VERIFIED_PERMISSIONS_ENABLED: 'false',
    TENANT_ISOLATION_ENABLED: 'false',
    RATE_LIMIT_ENABLED: 'true',
    ADMIN_API_RATE_LIMIT: '60/minute',
    TEAM_API_RATE_LIMIT: '30/minute',
    USAGE_API_RATE_LIMIT: '10/minute',

    // Phase 2 (v2.1): Admin 作成ゲート (Critical C-D)
    ALLOW_ADMIN_CREATION: allowAdminCreation,

    // Tenant
    DEFAULT_ORG_ID: 'default-org',
    DEFAULT_TENANT_CREDIT: '100000',

    // Bedrock
    BEDROCK_REGION: env.region,
    DEFAULT_BEDROCK_MODEL: defaultBedrockModel,
  },
  secrets: {},
  description: `[${prefix}] ECS Fargate (Public Subnet, desiredCount=1)`,
});
ecsStack.addDependency(networkStack);
ecsStack.addDependency(ecrStack);
ecsStack.addDependency(albStack);
ecsStack.addDependency(dynamoDBStack);
ecsStack.addDependency(cognitoStack);
ecsStack.addDependency(frontendStack);

// --- 8. Backend Config (Parameter Store 固定値) ---
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

// --- 全体タグ ---
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
