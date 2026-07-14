import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import * as fs from 'fs';
import * as path from 'path';
import { applyCommonTags, putStringParameter } from './_common';

export interface DynamoDBStackProps extends cdk.StackProps {
  prefix: string;
  /** environment (development, staging, production) */
  environment?: string;
}

/**
 * MVP DynamoDB Stack
 *
 * Every table: PAY_PER_REQUEST, AWS_MANAGED encryption, prefix-named.
 * - SseTokens was added to replace the retired Redis dependency (TTL-bearing).
 */
export class DynamoDBStack extends cdk.Stack {
  public readonly sessionsTable: dynamodb.Table;
  public readonly messagesTable: dynamodb.Table;
  public readonly usersTable: dynamodb.Table;
  public readonly userTenantsTable: dynamodb.Table;
  public readonly usageLogsTable: dynamodb.Table;
  public readonly appSettingsTable: dynamodb.Table;
  public readonly tagsTable: dynamodb.Table;
  public readonly sseTokensTable: dynamodb.Table;
  /** Phase 2: Tenant metadata (name, owner, default_credit) */
  public readonly tenantsTable: dynamodb.Table;
  /** Phase 2: Role -> permissions mapping (source of truth, seeded from permissions.json) */
  public readonly permissionsTable: dynamodb.Table;
  /** Phase S: Trusted AWS Accounts (account_id allowlist for SSO login) */
  public readonly trustedAccountsTable: dynamodb.Table;
  /** Phase S: Pre-registered SSO users for invite_only provisioning */
  public readonly ssoPreRegistrationsTable: dynamodb.Table;
  /** Phase C: Long-lived API keys (sk-stratoclave-*) for gateway clients like cowork */
  public readonly apiKeysTable: dynamodb.Table;
  /** Phase S: Replay defence nonces for the Vouch-by-STS signed-request flow */
  public readonly ssoNoncesTable: dynamodb.Table;
  /** P0-8 follow-up: single-use CLI → SPA handoff tickets */
  public readonly uiTicketsTable: dynamodb.Table;
  /** A-1: per-tenant dollar pool budgets, debited atomically with per-user tokens */
  public readonly tenantBudgetsTable: dynamodb.Table;
  /** A-2: admin-editable per-model pricing (token → micro-USD conversion) */
  public readonly pricingConfigTable: dynamodb.Table;
  /** Per-IP fixed-window rate-limit counters, shared across ECS tasks (TTL-reaped) */
  public readonly rateLimitsTable: dynamodb.Table;
  /** P0-11: per-model quota counters (one `used` counter per scope/model/period), TTL-reaped */
  public readonly modelQuotasTable: dynamodb.Table;

  public readonly allTableArns: string[];

  constructor(scope: Construct, id: string, props: DynamoDBStackProps) {
    super(scope, id, props);

    const { prefix } = props;
    const env = props.environment || 'development';
    const removalPolicy =
      env === 'production' ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY;
    const isProd = env === 'production';

    // Audit-critical tables: always RETAIN, even in development, because
    // these carry cross-tenant billing / API-key provenance that a dev
    // rebuild must not erase. If you really need to wipe them, delete by
    // hand with `aws dynamodb delete-table`.
    const auditRemovalPolicy = cdk.RemovalPolicy.RETAIN;

    // A-07-dynamo: prod tables MUST opt into DynamoDB delete-protection
    // so that a stray `cdk destroy`, mis-targeted CloudFormation rollback,
    // or compromised CI session cannot silently drop billing / audit
    // tables. Dev environments leave it off so disposable stacks are
    // still tear-down-friendly.
    const baseTableProps = {
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy,
      pointInTimeRecovery: isProd,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      deletionProtection: isProd,
    };

    // 1. Sessions
    this.sessionsTable = new dynamodb.Table(this, 'SessionsTable', {
      ...baseTableProps,
      tableName: `${prefix}-sessions`,
      partitionKey: { name: 'session_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
    });
    this.sessionsTable.addGlobalSecondaryIndex({
      indexName: 'user-id-index',
      partitionKey: { name: 'user_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'created_at', type: dynamodb.AttributeType.NUMBER },
    });
    this.sessionsTable.addGlobalSecondaryIndex({
      indexName: 'tenant-id-index',
      partitionKey: { name: 'tenant_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'created_at', type: dynamodb.AttributeType.NUMBER },
    });

    // 2. Messages
    this.messagesTable = new dynamodb.Table(this, 'MessagesTable', {
      ...baseTableProps,
      tableName: `${prefix}-messages`,
      partitionKey: { name: 'session_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'message_id', type: dynamodb.AttributeType.STRING },
    });

    // 3. Users
    this.usersTable = new dynamodb.Table(this, 'UsersTable', {
      ...baseTableProps,
      tableName: `${prefix}-users`,
      partitionKey: { name: 'user_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
    });
    this.usersTable.addGlobalSecondaryIndex({
      indexName: 'email-index',
      partitionKey: { name: 'email', type: dynamodb.AttributeType.STRING },
    });
    this.usersTable.addGlobalSecondaryIndex({
      indexName: 'auth-provider-user-id-index',
      partitionKey: { name: 'auth_provider_user_id', type: dynamodb.AttributeType.STRING },
    });

    // 4. UserTenants (holds credit information)
    this.userTenantsTable = new dynamodb.Table(this, 'UserTenantsTable', {
      ...baseTableProps,
      tableName: `${prefix}-user-tenants`,
      partitionKey: { name: 'user_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'tenant_id', type: dynamodb.AttributeType.STRING },
    });
    this.userTenantsTable.addGlobalSecondaryIndex({
      indexName: 'tenant-id-index',
      partitionKey: { name: 'tenant_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'user_id', type: dynamodb.AttributeType.STRING },
    });

    // 5. UsageLogs (with TTL) — RETAIN: tenant billing history, must survive
    // dev stack rebuilds / accidental teardowns.
    this.usageLogsTable = new dynamodb.Table(this, 'UsageLogsTable', {
      ...baseTableProps,
      removalPolicy: auditRemovalPolicy,
      tableName: `${prefix}-usage-logs`,
      partitionKey: { name: 'tenant_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'timestamp_log_id', type: dynamodb.AttributeType.STRING },
      timeToLiveAttribute: 'ttl',
    });
    this.usageLogsTable.addGlobalSecondaryIndex({
      indexName: 'user-id-index',
      partitionKey: { name: 'user_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'timestamp_log_id', type: dynamodb.AttributeType.STRING },
    });

    // 6. AppSettings
    this.appSettingsTable = new dynamodb.Table(this, 'AppSettingsTable', {
      ...baseTableProps,
      tableName: `${prefix}-app-settings`,
      partitionKey: { name: 'setting_key', type: dynamodb.AttributeType.STRING },
    });

    // 7. Tags
    this.tagsTable = new dynamodb.Table(this, 'TagsTable', {
      ...baseTableProps,
      pointInTimeRecovery: false,
      tableName: `${prefix}-tags`,
      partitionKey: { name: 'session_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'tag_name', type: dynamodb.AttributeType.STRING },
    });

    // 8. SseTokens (Redis replacement, TTL ~5 minutes)
    this.sseTokensTable = new dynamodb.Table(this, 'SseTokensTable', {
      ...baseTableProps,
      pointInTimeRecovery: false,
      tableName: `${prefix}-sse-tokens`,
      partitionKey: { name: 'token_id', type: dynamodb.AttributeType.STRING },
      timeToLiveAttribute: 'ttl',
    });

    // 9. Tenants (new in Phase 2, tenant metadata)
    //    Item schema:
    //      tenant_id: String (PK)
    //      name: String
    //      team_lead_user_id: String  (Cognito sub; "admin-owned" constant for admin-owned tenants)
    //      default_credit: Number
    //      status: String  ("active" | "archived")
    //      created_at: String (ISO 8601)
    //      updated_at: String (ISO 8601)
    //      created_by: String
    this.tenantsTable = new dynamodb.Table(this, 'TenantsTable', {
      ...baseTableProps,
      tableName: `${prefix}-tenants`,
      partitionKey: { name: 'tenant_id', type: dynamodb.AttributeType.STRING },
    });
    this.tenantsTable.addGlobalSecondaryIndex({
      indexName: 'team-lead-index',
      partitionKey: { name: 'team_lead_user_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'created_at', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // 10. Permissions (added in Phase 2; the RBAC source of truth)
    //     Item schema:
    //       role: String (PK)              e.g. "admin", "team_lead", "user"
    //       permissions: List<String>      e.g. ["users:*", "messages:send"]
    //       description: String
    //       updated_at: String (ISO 8601)
    //       version: String
    this.permissionsTable = new dynamodb.Table(this, 'PermissionsTable', {
      ...baseTableProps,
      pointInTimeRecovery: false,
      tableName: `${prefix}-permissions`,
      partitionKey: { name: 'role', type: dynamodb.AttributeType.STRING },
    });

    // 11. TrustedAccounts (new in Phase S, account_id allowlist for SSO login)
    //     Item schema:
    //       account_id: String (PK)         AWS Account ID (12 digits)
    //       description: String
    //       provisioning_policy: String     "invite_only" | "auto_provision"
    //       allowed_role_patterns: List<String>  glob patterns (empty list = all roles allowed)
    //       allow_iam_user: Bool            default false
    //       allow_instance_profile: Bool    default false
    //       default_tenant_id: String       initial tenant for new users (null defaults to default-org)
    //       default_credit: Number          initial credit for new users (null defaults to tenant default)
    //       created_at / updated_at: String (ISO 8601)
    //       created_by: String
    this.trustedAccountsTable = new dynamodb.Table(this, 'TrustedAccountsTable', {
      ...baseTableProps,
      tableName: `${prefix}-trusted-accounts`,
      partitionKey: { name: 'account_id', type: dynamodb.AttributeType.STRING },
    });

    // 12. SsoPreRegistrations (new in Phase S, for invite_only policy)
    //     Item schema:
    //       email: String (PK)         lowercase
    //       account_id: String         which trusted_account the user enters through
    //       invited_role: String       "user" | "team_lead" (admin auto-provisioning is prohibited)
    //       tenant_id: String?         null falls back to trusted_account.default_tenant_id
    //       total_credit: Number?
    //       iam_user_lookup_key: String?  "<account_id>#<iam_user_name>" (used for IAM user invitations)
    //       invited_by: String         Admin user_id
    //       invited_at: String (ISO 8601)
    //       consumed_at: String?       consumed on first SSO login; null means not yet used
    this.ssoPreRegistrationsTable = new dynamodb.Table(this, 'SsoPreRegistrationsTable', {
      ...baseTableProps,
      pointInTimeRecovery: false,
      tableName: `${prefix}-sso-pre-registrations`,
      partitionKey: { name: 'email', type: dynamodb.AttributeType.STRING },
    });
    // When inviting IAM users, the email cannot be reverse-looked-up from the ARN,
    // so a GSI keyed on "<account_id>#<iam_user_name>" is provided for lookups.
    this.ssoPreRegistrationsTable.addGlobalSecondaryIndex({
      indexName: 'iam-user-index',
      partitionKey: { name: 'iam_user_lookup_key', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // 13. ApiKeys (Phase C, long-lived API keys)
    //     Item schema:
    //       key_hash: String (PK)     hex of sha256(plaintext)
    //       key_id: String            display identifier "sk-stratoclave-<first 4 chars>...<last 4 chars>"
    //       user_id: String           owner
    //       name: String              user-supplied label
    //       scopes: List<String>      granted permission strings
    //       created_at / expires_at / revoked_at / last_used_at: String ISO 8601
    //       created_by: String        actor user_id when issued on behalf of another user by an admin
    // RETAIN: long-lived API key hashes are the only link between a key
    // string in the wild and the user it was issued to. Losing this table
    // silently turns valid keys into 401s *and* erases the audit trail.
    this.apiKeysTable = new dynamodb.Table(this, 'ApiKeysTable', {
      ...baseTableProps,
      removalPolicy: auditRemovalPolicy,
      pointInTimeRecovery: true,
      tableName: `${prefix}-api-keys`,
      partitionKey: { name: 'key_hash', type: dynamodb.AttributeType.STRING },
    });
    this.apiKeysTable.addGlobalSecondaryIndex({
      indexName: 'user-id-index',
      partitionKey: { name: 'user_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'created_at', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // 14. SsoNonces (Phase S replay defence).
    //     Backend writes a SHA-256 fingerprint of each accepted
    //     `sts:GetCallerIdentity` signed request with
    //     `attribute_not_exists(nonce)`, so the same signature cannot
    //     be replayed within the ±5-minute skew window. DynamoDB TTL
    //     (`expires_at`) reaps entries ~10 minutes after acceptance.
    //     Retaining PITR is not useful here — the rows are ephemeral.
    this.ssoNoncesTable = new dynamodb.Table(this, 'SsoNoncesTable', {
      ...baseTableProps,
      pointInTimeRecovery: false,
      tableName: `${prefix}-sso-nonces`,
      partitionKey: { name: 'nonce', type: dynamodb.AttributeType.STRING },
      timeToLiveAttribute: 'expires_at',
    });

    // 15. UiTickets (P0-8 follow-up, replaces `?token=` handoff).
    //     CLI mints an opaque 256-bit nonce (SHA-256 hash is the PK)
    //     bound to the authenticated session's tokens; the browser
    //     swaps that nonce back for the tokens via
    //     /api/mvp/auth/ui-ticket/consume. Single-use (delete-on-read)
    //     plus a 30 s TTL via `expires_at` bound the exposure window
    //     even if a DynamoDB row is read by a compromised ECS task.
    this.uiTicketsTable = new dynamodb.Table(this, 'UiTicketsTable', {
      ...baseTableProps,
      pointInTimeRecovery: false,
      tableName: `${prefix}-ui-tickets`,
      partitionKey: { name: 'ticket_hash', type: dynamodb.AttributeType.STRING },
      timeToLiveAttribute: 'expires_at',
    });

    // A-1: tenant dollar pool budgets. PK tenant_id, SK "BUDGET#<period>".
    // Debited atomically with the per-user token balance in a single
    // TransactWriteItems so a tenant can never overspend its pool under
    // concurrency. Audit-critical (spend record) → RETAIN + PITR in prod.
    this.tenantBudgetsTable = new dynamodb.Table(this, 'TenantBudgetsTable', {
      ...baseTableProps,
      tableName: `${prefix}-tenant-budgets`,
      partitionKey: { name: 'tenant_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
    });

    // A-2: admin-editable per-model pricing. PK "CONFIG#pricing", SK versioned
    // rate rows + a CURRENT pointer. Small, rarely written, keyed reads only.
    this.pricingConfigTable = new dynamodb.Table(this, 'PricingConfigTable', {
      ...baseTableProps,
      tableName: `${prefix}-pricing-config`,
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
    });

    // Per-IP rate-limit counters (fixed-window). PK "RL#<scope>#<ip>#<window>",
    // one atomic `ADD hits` per request, TTL (`expires_at`) reaps expired
    // windows. Shared across all ECS tasks so per-IP auth caps hold under
    // horizontal scale-out — no Redis, no new infra class. Ephemeral →
    // no PITR.
    //
    // MUST stay PAY_PER_REQUEST (inherited from baseTableProps). The limiter
    // (core/rate_limit_ddb.py) fails CLOSED on a Throttling error, treating it
    // as mostly a per-partition (per-key) signal. On-demand keeps throttling as
    // per-partition as DynamoDB allows; provisioned mode would make a table-wide
    // WCU exhaustion lock out every auth user. Do not change the billing mode
    // without revisiting that failure policy.
    this.rateLimitsTable = new dynamodb.Table(this, 'RateLimitsTable', {
      ...baseTableProps,
      pointInTimeRecovery: false,
      tableName: `${prefix}-rate-limits`,
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      timeToLiveAttribute: 'expires_at',
    });

    // P0-11: per-model quota counters. One item per (scope, model, period):
    //   PK "TENANT#<id>" | "TENANT#<id>#USER#<uid>", SK "MQ#<model>#<period>".
    // A single monotonic `used` counter (reserved-in-flight + settled) is
    // charged inside the SAME TransactWriteItems as the pooled-budget debit, so
    // budget + quota commit atomically. `expires_at` TTL reaps a period's rows a
    // few days after month-end. No new infra class — DynamoDB only.
    //
    // Audit note: unlike the budget pool this is a soft policy limit, not a
    // spend record, so it does not need PITR/RETAIN — the tenant-budgets table
    // remains the source of truth for money. Ephemeral → no PITR.
    this.modelQuotasTable = new dynamodb.Table(this, 'ModelQuotasTable', {
      ...baseTableProps,
      pointInTimeRecovery: false,
      tableName: `${prefix}-model-quotas`,
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      timeToLiveAttribute: 'expires_at',
    });

    this.allTableArns = [
      this.sessionsTable.tableArn,
      this.messagesTable.tableArn,
      this.usersTable.tableArn,
      this.userTenantsTable.tableArn,
      this.usageLogsTable.tableArn,
      this.appSettingsTable.tableArn,
      this.tagsTable.tableArn,
      this.sseTokensTable.tableArn,
      this.tenantsTable.tableArn,
      this.permissionsTable.tableArn,
      this.trustedAccountsTable.tableArn,
      this.ssoPreRegistrationsTable.tableArn,
      this.apiKeysTable.tableArn,
      this.ssoNoncesTable.tableArn,
      this.uiTicketsTable.tableArn,
      this.tenantBudgetsTable.tableArn,
      this.pricingConfigTable.tableArn,
      this.rateLimitsTable.tableArn,
      this.modelQuotasTable.tableArn,
    ];

    // Parameter Store exports
    const tableParams: Array<[string, string, dynamodb.Table]> = [
      ['TableUsersParam', 'dynamodb/table-users', this.usersTable],
      ['TableUserTenantsParam', 'dynamodb/table-user-tenants', this.userTenantsTable],
      ['TableUsageLogsParam', 'dynamodb/table-usage-logs', this.usageLogsTable],
      ['TableSessionsParam', 'dynamodb/table-sessions', this.sessionsTable],
      ['TableMessagesParam', 'dynamodb/table-messages', this.messagesTable],
      ['TableAppSettingsParam', 'dynamodb/table-app-settings', this.appSettingsTable],
      ['TableTagsParam', 'dynamodb/table-tags', this.tagsTable],
      ['TableSseTokensParam', 'dynamodb/table-sse-tokens', this.sseTokensTable],
      ['TableTenantsParam', 'dynamodb/table-tenants', this.tenantsTable],
      ['TablePermissionsParam', 'dynamodb/table-permissions', this.permissionsTable],
      ['TableTrustedAccountsParam', 'dynamodb/table-trusted-accounts', this.trustedAccountsTable],
      ['TableSsoPreRegistrationsParam', 'dynamodb/table-sso-pre-registrations', this.ssoPreRegistrationsTable],
      ['TableApiKeysParam', 'dynamodb/table-api-keys', this.apiKeysTable],
      ['TableSsoNoncesParam', 'dynamodb/table-sso-nonces', this.ssoNoncesTable],
      ['TableUiTicketsParam', 'dynamodb/table-ui-tickets', this.uiTicketsTable],
      ['TableTenantBudgetsParam', 'dynamodb/table-tenant-budgets', this.tenantBudgetsTable],
      ['TablePricingConfigParam', 'dynamodb/table-pricing-config', this.pricingConfigTable],
      ['TableRateLimitsParam', 'dynamodb/table-rate-limits', this.rateLimitsTable],
      ['TableModelQuotasParam', 'dynamodb/table-model-quotas', this.modelQuotasTable],
    ];
    for (const [id, rel, table] of tableParams) {
      putStringParameter(this, id, {
        prefix,
        relativePath: rel,
        value: table.tableName,
        description: `DynamoDB table: ${table.tableName}`,
      });
    }

    new cdk.CfnOutput(this, 'UsersTableName', { value: this.usersTable.tableName });
    new cdk.CfnOutput(this, 'UserTenantsTableName', {
      value: this.userTenantsTable.tableName,
    });
    new cdk.CfnOutput(this, 'UsageLogsTableName', {
      value: this.usageLogsTable.tableName,
    });
    new cdk.CfnOutput(this, 'SseTokensTableName', {
      value: this.sseTokensTable.tableName,
    });
    new cdk.CfnOutput(this, 'TenantsTableName', {
      value: this.tenantsTable.tableName,
    });
    new cdk.CfnOutput(this, 'PermissionsTableName', {
      value: this.permissionsTable.tableName,
    });

    // ----------------------------------------------------------------
    // Permissions reseed (deploy-time custom resource).
    //
    // The ECS task's lifespan also calls `bootstrap.seed.seed_all()`
    // when it starts, but on a deploy that adds a new permission scope
    // (e.g. `responses:send`) there is a window during the rolling
    // restart where the *new* code is live but the DynamoDB row still
    // shows the *old* permission set. During that window, every
    // `mint_ephemeral_key_scoped("responses:send")` from a `user`
    // role returns 403.
    //
    // To close that window, every deploy issues an `UpdateItem` per
    // role here, taking the source of truth from
    // `backend/permissions.json` at synth time. The update is idempotent
    // — same JSON → same write → no churn — and runs before the ECS
    // service swaps to the new task definition (DynamoDB stack is
    // upstream of the ECS stack in iac.ts).
    seedPermissionsAtDeploy(this, prefix, this.permissionsTable);

    applyCommonTags(this, prefix, 'DynamoDB');
  }
}

/**
 * On every deploy, write each role's permission set from
 * `backend/permissions.json` into the Permissions table.
 *
 * Implemented as one `AwsCustomResource` per role so an `UpdateItem`
 * failure in one role surfaces in CloudFormation events without
 * blocking the others. The ListPermissions / Roles set is read at synth
 * time (Node.js `fs`) so the JSON is baked into the synthesized
 * template — runtime drift is captured by the next `cdk deploy`.
 */
function seedPermissionsAtDeploy(
  scope: Construct,
  prefix: string,
  permissionsTable: dynamodb.Table,
): void {
  const permsJsonPath = path.join(
    __dirname,
    '..',
    '..',
    'backend',
    'permissions.json',
  );
  if (!fs.existsSync(permsJsonPath)) {
    // Synth fails loudly; we never silently skip the reseed because
    // that is the behaviour we are trying to prevent.
    throw new Error(
      `permissions.json not found at ${permsJsonPath}; ` +
        'the Permissions reseed custom resource cannot be configured. ' +
        'Ensure the iac stack is synthed from a tree containing backend/.',
    );
  }
  const raw = fs.readFileSync(permsJsonPath, 'utf-8');
  const parsed = JSON.parse(raw) as {
    version: string;
    roles: Record<
      string,
      { description: string; permissions: string[] }
    >;
  };

  for (const [role, body] of Object.entries(parsed.roles)) {
    // The same DynamoDB UpdateItem call is wired to both `onCreate` and
    // `onUpdate` so greenfield deploys seed the row immediately. With
    // only `onUpdate`, a brand-new stack would create the table empty
    // and rely on the ECS task's in-process `seed_all()` — which is
    // a fallback we want, not the primary mechanism.
    const updateItem = {
      service: 'DynamoDB',
      action: 'updateItem',
      parameters: {
        TableName: permissionsTable.tableName,
        Key: { role: { S: role } },
        UpdateExpression: 'SET #p = :p, #d = :d, #v = :v, #u = :u',
        ExpressionAttributeNames: {
          '#p': 'permissions',
          '#d': 'description',
          '#v': 'version',
          '#u': 'updated_at',
        },
        ExpressionAttributeValues: {
          ':p': { L: body.permissions.map((s) => ({ S: s })) },
          ':d': { S: body.description },
          ':v': { S: parsed.version },
          // The custom resource invocation timestamp is generated by
          // CloudFormation; we record the source version here.
          ':u': { S: parsed.version },
        },
      },
      physicalResourceId: cr.PhysicalResourceId.of(
        `${prefix}-permissions-${role}-${parsed.version}`,
      ),
    } as const;

    new cr.AwsCustomResource(scope, `SeedPermissions-${role}`, {
      onCreate: updateItem,
      onUpdate: updateItem,
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({
          actions: ['dynamodb:UpdateItem'],
          resources: [permissionsTable.tableArn],
        }),
      ]),
      installLatestAwsSdk: false,
    });
  }
}
