#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { getPrefix, stackName, paramPath, putStringParameter } from '../lib/_common';
import { NetworkStack } from '../lib/network-stack';
import { EcrStack } from '../lib/ecr-stack';
import { AlbStack } from '../lib/alb-stack';
import { EcsStack } from '../lib/ecs-stack';
import { FrontendStack } from '../lib/frontend-stack';
import { CognitoStack } from '../lib/cognito-stack';
import { DynamoDBStack } from '../lib/dynamodb-stack';
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
  environment: process.env.ENVIRONMENT || 'development',
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
  description: `[${prefix}] Internet-facing ALB`,
});
albStack.addDependency(networkStack);

// --- 5. Frontend (S3 + CloudFront + SPA fallback Function) ---
const frontendStack = new FrontendStack(app, stackName(prefix, 'frontend'), {
  env,
  prefix,
  albDnsName: albStack.alb.loadBalancerDnsName,
  description: `[${prefix}] Frontend S3 + CloudFront`,
});
frontendStack.addDependency(albStack);

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
    ENVIRONMENT: process.env.ENVIRONMENT || 'development',
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

console.error(`[stratoclave-iac] prefix=${prefix} region=${env.region} bedrockModel=${defaultBedrockModel}`);
console.error(`[stratoclave-iac] Parameter Store base: ${paramPath(prefix, '')}`);
console.error(`[stratoclave-iac] ALLOW_ADMIN_CREATION=${allowAdminCreation}`);
