import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { Construct } from 'constructs';
import { applyCommonTags, putStringParameter } from './_common';

export interface DynamoDBStackProps extends cdk.StackProps {
  prefix: string;
  /** environment (development, staging, production) */
  environment?: string;
}

/**
 * MVP DynamoDB Stack
 *
 * 全テーブル: PAY_PER_REQUEST, AWS_MANAGED 暗号化, prefix 命名
 * - SseTokens テーブルは Redis 撤去に伴い新設（TTL 付き）
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

    const baseTableProps = {
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy,
      pointInTimeRecovery: isProd,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
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

    // 4. UserTenants (credit 情報を保持)
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

    // 5. UsageLogs (TTL 付き) — RETAIN: tenant billing history, must survive
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

    // 8. SseTokens (Redis 代替、TTL 5 分想定)
    this.sseTokensTable = new dynamodb.Table(this, 'SseTokensTable', {
      ...baseTableProps,
      pointInTimeRecovery: false,
      tableName: `${prefix}-sse-tokens`,
      partitionKey: { name: 'token_id', type: dynamodb.AttributeType.STRING },
      timeToLiveAttribute: 'ttl',
    });

    // 9. Tenants (Phase 2 新設、tenant メタデータ)
    //    Item schema:
    //      tenant_id: String (PK)
    //      name: String
    //      team_lead_user_id: String  (Cognito sub、Admin 所有は "admin-owned" 固定値)
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

    // 10. Permissions (Phase 2 新設、RBAC 真実源)
    //     Item schema:
    //       role: String (PK)   e.g. "admin", "team_lead", "user"
    //       permissions: List<String>  e.g. ["users:*", "messages:send"]
    //       description: String
    //       updated_at: String (ISO 8601)
    //       version: String
    this.permissionsTable = new dynamodb.Table(this, 'PermissionsTable', {
      ...baseTableProps,
      pointInTimeRecovery: false,
      tableName: `${prefix}-permissions`,
      partitionKey: { name: 'role', type: dynamodb.AttributeType.STRING },
    });

    // 11. TrustedAccounts (Phase S 新設、SSO login 時の account_id allowlist)
    //     Item schema:
    //       account_id: String (PK)         AWS Account ID (12 桁)
    //       description: String
    //       provisioning_policy: String     "invite_only" | "auto_provision"
    //       allowed_role_patterns: List<String>  glob パターン (空 list = 全 role 許可)
    //       allow_iam_user: Bool            default false
    //       allow_instance_profile: Bool    default false
    //       default_tenant_id: String       新規 user 初期テナント (null なら default-org)
    //       default_credit: Number          新規 user 初期クレジット (null なら tenant default)
    //       created_at / updated_at: String (ISO 8601)
    //       created_by: String
    this.trustedAccountsTable = new dynamodb.Table(this, 'TrustedAccountsTable', {
      ...baseTableProps,
      tableName: `${prefix}-trusted-accounts`,
      partitionKey: { name: 'account_id', type: dynamodb.AttributeType.STRING },
    });

    // 12. SsoPreRegistrations (Phase S 新設、invite_only ポリシー用)
    //     Item schema:
    //       email: String (PK)         lowercase
    //       account_id: String         どの trusted_account 経由で入るか
    //       invited_role: String       "user" | "team_lead" (admin 自動 provision 禁止)
    //       tenant_id: String?         null なら trusted_account.default_tenant_id
    //       total_credit: Number?
    //       iam_user_lookup_key: String?  "<account_id>#<iam_user_name>" (IAM user 招待時)
    //       invited_by: String         Admin user_id
    //       invited_at: String (ISO 8601)
    //       consumed_at: String?       初回 SSO login で consume、null なら未使用
    this.ssoPreRegistrationsTable = new dynamodb.Table(this, 'SsoPreRegistrationsTable', {
      ...baseTableProps,
      pointInTimeRecovery: false,
      tableName: `${prefix}-sso-pre-registrations`,
      partitionKey: { name: 'email', type: dynamodb.AttributeType.STRING },
    });
    // IAM user を招待する場合、arn から email を逆引きできないため
    // "<account_id>#<iam_user_name>" で lookup する GSI を用意
    this.ssoPreRegistrationsTable.addGlobalSecondaryIndex({
      indexName: 'iam-user-index',
      partitionKey: { name: 'iam_user_lookup_key', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // 13. ApiKeys (Phase C、長期 API キー)
    //     Item schema:
    //       key_hash: String (PK)     sha256(plaintext) の hex
    //       key_id: String            表示用 "sk-stratoclave-<first 4 chars>...<last 4 chars>"
    //       user_id: String           所有者
    //       name: String              ユーザーラベル
    //       scopes: List<String>      付与された permission 文字列
    //       created_at / expires_at / revoked_at / last_used_at: String ISO 8601
    //       created_by: String        Admin 代理発行時は actor の user_id
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
    ];

    // Parameter Store エクスポート
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

    applyCommonTags(this, prefix, 'DynamoDB');
  }
}
