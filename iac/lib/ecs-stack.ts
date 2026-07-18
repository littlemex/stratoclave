import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';
import { applyCommonTags, putStringParameter } from './_common';

export interface EcsStackProps extends cdk.StackProps {
  prefix: string;
  vpc: ec2.IVpc;
  securityGroup: ec2.ISecurityGroup;
  repository: ecr.IRepository;
  targetGroup: elbv2.IApplicationTargetGroup;

  /** Cognito User Pool ARN (used to scope Task Role permissions) */
  userPoolArn: string;

  /** List of DynamoDB table ARNs (used to scope Task Role permissions) */
  dynamoDbTableArns: string[];

  /** CPU units @default 256 */
  cpu?: number;
  /** Memory MiB @default 512 */
  memory?: number;
  /** desired task count @default 1 */
  desiredCount?: number;
  /** container port @default 8000 */
  containerPort?: number;

  environment?: { [key: string]: string };
  secrets?: { [key: string]: ecs.Secret };

  /**
   * When true, create the per-tenant VSR config bucket (versioned, private,
   * TLS-enforced) and grant the backend task role Get/Put/Delete ONLY on the
   * ``vsr-config/*`` prefix, and inject ``VSR_CONFIG_BUCKET`` into the container
   * environment. Absent/false => no bucket, no grant, no env (feature ships
   * dark; the admin surface 404s until this is provisioned).
   */
  enableVsrConfigBucket?: boolean;

  /**
   * P1-C (2026-04 security review).
   *
   * `enableExecuteCommand: true` means any principal with
   * `ecs:ExecuteCommand` on this service gets a shell inside the
   * live backend container. That is useful for incident debugging
   * but hugely expensive if the AWS credentials that carry that
   * permission are ever compromised — the attacker walks straight
   * into a process that holds every backend env var and DynamoDB
   * grant.
   *
   * Default off in production. Operators that genuinely need shell
   * access can set `ENABLE_ECS_EXEC=true` for a time-boxed window,
   * redeploy, and then unset it. Non-production environments leave
   * it on so dev smoke tests keep working.
   */
  enableExecuteCommand?: boolean;

  /**
   * ECR image tag the task definition resolves at deploy time.
   *
   * Defaults to `latest` for backwards compatibility, but the ECR
   * repository was switched to ``IMMUTABLE`` (A-01-ecr) so production
   * deployments MUST pass an immutable, content-addressed tag here
   * (e.g. ``sec-2026-06-11`` or a 12-char SHA prefix). The bin
   * entrypoint reads ``IMAGE_TAG`` from the deployer's environment so
   * CI / `cdk deploy` invocations can switch tags without editing
   * the stack.
   */
  imageTag?: string;
}

/**
 * MVP ECS Stack
 *
 * - Fargate placed **directly in the Public Subnet**, no NAT Gateway
 * - Task Role granted least-privilege scoped to the prefix
 * - Container Insights enabled
 */
export class EcsStack extends cdk.Stack {
  public readonly cluster: ecs.Cluster;
  public readonly service: ecs.FargateService;
  public readonly taskDefinition: ecs.FargateTaskDefinition;
  /** The per-tenant VSR config bucket, when enabled (else undefined). */
  public readonly vsrConfigBucket?: s3.Bucket;

  constructor(scope: Construct, id: string, props: EcsStackProps) {
    super(scope, id, props);

    const { prefix } = props;
    const region = cdk.Stack.of(this).region;
    const account = cdk.Stack.of(this).account;

    this.cluster = new ecs.Cluster(this, 'Cluster', {
      vpc: props.vpc,
      clusterName: `${prefix}-cluster`,
      containerInsights: true,
    });

    // A-06-iam: pre-create the bootstrap-admin secret with an empty
    // placeholder so the ECS task role only ever needs `PutSecretValue`
    // (not `CreateSecret`). The seed code on first boot overwrites the
    // placeholder with the freshly generated temp password.
    const bootstrapAdminSecret = new secretsmanager.Secret(
      this,
      'BootstrapAdminTempPasswordSecret',
      {
        secretName: `${prefix}/bootstrap-admin-temp-password`,
        description:
          'Stratoclave bootstrap admin temporary password (rewritten by seed.py at first boot).',
        removalPolicy: cdk.RemovalPolicy.RETAIN,
        generateSecretString: {
          // The placeholder MUST be valid JSON so seed.py's
          // `put_secret_value` write replaces it cleanly. The real
          // {email,password} payload is filled in at lifespan time.
          secretStringTemplate: JSON.stringify({ placeholder: true }),
          generateStringKey: 'token',
          excludePunctuation: true,
          passwordLength: 32,
        },
      },
    );
    // cdk-nag SMG4 (rotation): bootstrap secret is single-use — the
    // operator reads it once, then rotates the admin password through
    // Cognito directly. Secrets Manager rotation does not apply to
    // a placeholder that is overwritten by the seed code on first boot.
    // Suppressed via the bin/iac.ts stack-level suppression list.
    void bootstrapAdminSecret;

    // A-08-log: 7-day retention used to be the default. That is below
    // the typical SOC2 / ISO27001 90-day audit window, and below the
    // window during which most upstream auth incidents are detected.
    // Default to 90 days; container logs are cheap relative to the
    // forensic value of the extra runway.
    const logGroup = new logs.LogGroup(this, 'BackendLogGroup', {
      logGroupName: `/ecs/${prefix}-backend`,
      retention: logs.RetentionDays.THREE_MONTHS,
      // RETAIN in any environment — log groups carry incident
      // forensics that survive a stack rebuild.
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Ledger P2-d: turn the ledger's structured log events into CloudWatch
    // metrics + alarms. The backend logs one JSON line per event (structlog);
    // a metric filter matching the event name emits a count metric, and an
    // alarm fires when it is non-zero. These are the money-integrity signals:
    //
    //   LedgerDriftSettled/Reserved/Reclaimed — the reconciliation endpoint saw
    //     the budget counter diverge from the ledger's derived total. A money
    //     source of truth tolerates NO drift, so the alarm needs 3 consecutive
    //     non-zero datapoints (the recon may transiently read mid-txn and the
    //     endpoint already suppresses unstable snapshots, so 3× is belt-and-
    //     braces against a flapping false positive).
    //   LateSettleActualMismatch — a late-settle retry arrived with a different
    //     actual than first recorded (client bug); first-writer-wins keeps money
    //     correct, but it must be investigated → alarm on a single occurrence.
    //   LegacyHoldNoTerminal — a pre-Phase-2 hold was settled via the legacy
    //     fallback. Expected to trend to zero after rollout; the alarm is the
    //     signal that the legacy fallback can be removed (rollout step 7). Not a
    //     defect on its own → treated as an operational (info) alarm.
    const METRIC_NS = `${prefix}/CreditLedger`;
    const mkFilter = (event: string, metricName: string) =>
      logGroup.addMetricFilter(`LedgerMF${metricName}`, {
        filterName: `${prefix}-ledger-${metricName}`,
        // structlog renders `event` as a JSON field; match on it.
        filterPattern: logs.FilterPattern.stringValue('$.event', '=', event),
        metricNamespace: METRIC_NS,
        metricName,
        metricValue: '1',
        defaultValue: 0,
      });

    const driftAlarmConfigs: Array<[string, string]> = [
      ['LedgerDriftSettled', 'LedgerDriftSettled'],
      ['LedgerDriftReserved', 'LedgerDriftReserved'],
      ['LedgerDriftReclaimed', 'LedgerDriftReclaimed'],
    ];
    for (const [event, metricName] of driftAlarmConfigs) {
      const mf = mkFilter(event, metricName);
      new cloudwatch.Alarm(this, `LedgerAlarm${metricName}`, {
        alarmName: `${prefix}-${metricName}`,
        alarmDescription: `Credit-ledger ${metricName}: budget counter diverged from the ledger source of truth (money integrity).`,
        metric: mf.metric({ statistic: 'Sum', period: cdk.Duration.minutes(5) }),
        threshold: 0,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluationPeriods: 3,
        datapointsToAlarm: 3,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
    }

    const mismatchMf = mkFilter('LateSettleActualMismatch', 'LateSettleActualMismatch');
    new cloudwatch.Alarm(this, 'LedgerAlarmLateSettleMismatch', {
      alarmName: `${prefix}-LateSettleActualMismatch`,
      alarmDescription:
        'Credit-ledger LATE_SETTLE retry arrived with a different actual than first recorded (client bug; first-writer-wins keeps money correct).',
      metric: mismatchMf.metric({ statistic: 'Sum', period: cdk.Duration.minutes(5) }),
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      datapointsToAlarm: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // Legacy fallback usage: a metric only (no alarm). It is expected to be
    // non-zero briefly after rollout, then drain to zero — operators watch it to
    // decide when to delete the legacy fallback, not to page on.
    mkFilter('LegacyHoldNoTerminal', 'LegacyHoldNoTerminal');

    // Unrecoverable spend / invariant-violation signals (Fable P2 review-2
    // R2-1/R2-4): because settle runs at the streaming tail with no client retry,
    // a raised recovery error is absorbed by the outer best-effort settle — so
    // these are ALARM signals, not self-healing. Each means real spend may be
    // unrecorded and needs a human: alarm on a single occurrence.
    //   pool_settle_late_settle_retries_exhausted — LATE_SETTLE recovery gave up
    //     after retrying a transient conflict; the spend is not recorded and
    //     reconciliation can't see it (counter+ledger miss it atomically).
    //   pool_settle_terminal_unclassified — a terminal CCF read back None/unknown
    //     (a pk/index defect); spend dropped rather than mis-recorded.
    for (const event of [
      'pool_settle_late_settle_retries_exhausted',
      'pool_settle_terminal_unclassified',
      'pool_settle_late_settle_missing_after_ccf',
    ]) {
      // camelCase metric name from the snake_case event.
      const metricName = event
        .split('_')
        .map((w, i) => (i === 0 ? w : w.charAt(0).toUpperCase() + w.slice(1)))
        .join('');
      const mf = mkFilter(event, metricName);
      new cloudwatch.Alarm(this, `LedgerAlarm_${metricName}`, {
        alarmName: `${prefix}-${metricName}`,
        alarmDescription: `Credit-ledger: ${event} — spend may be unrecorded, needs investigation.`,
        metric: mf.metric({ statistic: 'Sum', period: cdk.Duration.minutes(5) }),
        threshold: 0,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluationPeriods: 1,
        datapointsToAlarm: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
    }
    // Alarms are metric-only for now (no SNS action wired). A follow-up can
    // attach an SNS topic via alarm.addAlarmAction once an ops topic exists;
    // the alarms are already visible in the console and queryable by API.

    this.taskDefinition = new ecs.FargateTaskDefinition(this, 'BackendTaskDefinition', {
      cpu: props.cpu || 256,
      memoryLimitMiB: props.memory || 512,
      family: `${prefix}-backend`,
    });

    // DynamoDB: restrict to the actual table ARNs only.
    //
    // Ledger P2-d: the credit-ledger table is APPEND-ONLY. It is excluded from
    // the blanket CRUD grant below and given its own PutItem/ConditionCheck/
    // GetItem/Query ALLOW plus an explicit DENY of UpdateItem/DeleteItem/
    // BatchWriteItem. This append-only property is a PREMISE of the ledger's
    // correctness proof (a terminal event is immutable, so the settle routing's
    // "read RECLAIM ⇒ it stays RECLAIM" reasoning and the reserved-return
    // exclusion hold) — not merely an operational guard. The DENY makes it
    // enforced even if a future edit re-adds the ledger to a CRUD grant.
    const ledgerArn = `arn:aws:dynamodb:${region}:${account}:table/${props.prefix}-credit-ledger`;
    const isLedger = (arn: string) => arn === ledgerArn;
    const crudArns = props.dynamoDbTableArns.filter((arn) => !isLedger(arn));
    const dynamoResources = [...crudArns, ...crudArns.map((arn) => `${arn}/index/*`)];

    // P0-10 (2026-04 security review): the blanket Statement below used
    // to include `dynamodb:Scan` across every table. The review wanted
    // Scan narrowed to the tables that legitimately need it; granting
    // Scan on usage-logs / sso-nonces / messages / sse-tokens made a
    // backend RCE into a one-shot bulk-exfil.
    //
    // We split the policy in two:
    //
    //   1. Everyday CRUD on every prefix-scoped table *without* Scan.
    //   2. A second Statement granting Scan only on the tables whose
    //      admin code paths actually need it today:
    //        - users               (scan_admins + admin list paging)
    //        - api-keys            (find_any_by_key_id for admin revoke)
    //        - tenants             (admin tenant list)
    //        - trusted-accounts    (SSO allowlist console)
    //        - sso-pre-registrations (admin invite list)
    //        - permissions         (RBAC seed / role dump)
    //        - user-tenants        (tenants.py rollup of archived rows)
    //
    //      A Query / GSI migration that removes these scans is on the
    //      P1 roadmap; the rest of the audit-critical tables (usage-logs,
    //      sessions, messages, sse-tokens, sso-nonces) stay Scan-denied.
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'TableCrudWithoutScan',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:Query',
          'dynamodb:BatchGetItem',
          'dynamodb:BatchWriteItem',
          'dynamodb:ConditionCheckItem',
        ],
        resources: dynamoResources,
      }),
    );

    // Ledger P2-d: append-only ALLOW for the credit-ledger table + its GSI.
    // Writes are always via TransactWriteItems (PutItem + ConditionCheckItem);
    // reads via GetItem (terminal routing) + Query (balance derivation / recon /
    // run audit). No UpdateItem/DeleteItem/BatchWriteItem — see the DENY below.
    const ledgerResources = [ledgerArn, `${ledgerArn}/index/*`];
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'CreditLedgerAppendOnly',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:PutItem',
          'dynamodb:ConditionCheckItem',
          'dynamodb:GetItem',
          'dynamodb:Query',
        ],
        resources: ledgerResources,
      }),
    );
    // Explicit DENY: the ledger is immutable once written. BatchWriteItem can
    // carry deletes, so it is denied wholesale (all ledger writes go through
    // TransactWriteItems/PutItem). An explicit DENY overrides any ALLOW, so this
    // survives a future accidental re-grant — the append-only invariant is
    // pinned by iac/test (see ecs-stack ledger append-only test).
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'CreditLedgerNoMutateOrDelete',
        effect: iam.Effect.DENY,
        actions: ['dynamodb:UpdateItem', 'dynamodb:DeleteItem', 'dynamodb:BatchWriteItem'],
        resources: ledgerResources,
      }),
    );

    // Sweep-4 (2026-04-30) tightens sweep-1 C-D by dropping the
    // `permissions` table from the Scan allowlist. `permissions` is
    // only accessed via `PermissionsRepository.get(role)` (deterministic
    // key lookup) in production code — `list_all()` exists as a helper
    // but is unreferenced — so granting Scan on it is pure attack
    // surface. We deliberately KEEP `api-keys` here for now: the admin
    // console's `/api/mvp/admin/api-keys` listing page still uses
    // `ApiKeysRepository.list_all()` which is implemented as a Scan.
    // A follow-up PR will migrate that page to a user-keyed GSI view
    // and then this allowlist can drop to five. Until then, removing
    // `api-keys` from here breaks the admin UI with a 403 at runtime.
    //
    // DO NOT add `permissions` back here — the invariant is pinned by
    // iac/test/ecs-stack-scan-allowlist.test.ts.
    const scanTableSuffixes = [
      'users',
      'api-keys',
      'tenants',
      'trusted-accounts',
      'sso-pre-registrations',
      'user-tenants',
    ];
    const scanResources: string[] = [];
    for (const suffix of scanTableSuffixes) {
      const arn = `arn:aws:dynamodb:${region}:${account}:table/${props.prefix}-${suffix}`;
      scanResources.push(arn, `${arn}/index/*`);
    }
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'ScanLimitedToAdminConsoleTables',
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:Scan'],
        resources: scanResources,
      }),
    );

    // Bedrock: Anthropic (Claude) only — both the cross-region
    // inference profile (CRIS) and the underlying foundation-model are
    // allowlisted. `Resource: *` would let an RCE invoke Llama / Nova /
    // Mistral and blow up cost, so we scope strictly to the Anthropic
    // prefix.
    //
    //  - foundation-model: Bedrock-owned, no account boundary → `::`.
    //  - inference-profile: created in this account, prefixed by
    //    `us./apac./eu./global.` per region.
    //
    // The wildcard region in each ARN covers cross-region inference
    // routes that originate outside us-east-1.
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'AllowAnthropicBedrockInvoke',
        effect: iam.Effect.ALLOW,
        actions: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
          'bedrock:Converse',
          'bedrock:ConverseStream',
        ],
        resources: [
          // foundation-model (region-less, account-less)
          `arn:aws:bedrock:*::foundation-model/anthropic.*`,
          // inference-profile in this account (us./apac./eu./global. prefix, all regions)
          `arn:aws:bedrock:*:${account}:inference-profile/us.anthropic.*`,
          `arn:aws:bedrock:*:${account}:inference-profile/apac.anthropic.*`,
          `arn:aws:bedrock:*:${account}:inference-profile/eu.anthropic.*`,
          `arn:aws:bedrock:*:${account}:inference-profile/global.anthropic.*`,
        ],
      }),
    );
    // Bedrock read-only operations (model discovery / /v1/models).
    // ListFoundationModels / ListInferenceProfiles do not support
    // resource-level scoping, so the resource list stays at `*`.
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'AllowBedrockReadOnly',
        effect: iam.Effect.ALLOW,
        actions: [
          'bedrock:ListFoundationModels',
          'bedrock:ListInferenceProfiles',
          'bedrock:GetFoundationModel',
          'bedrock:GetInferenceProfile',
        ],
        resources: ['*'],
      }),
    );

    // OpenAI (codex / GPT-5.x) on Amazon Bedrock — separate IAM namespace.
    //
    // Bedrock's OpenAI-compatible endpoint lives at
    // `bedrock-mantle.{region}.api.aws/openai/v1/...` with its own
    // `bedrock-mantle:*` action set. GPT-5.4 / GPT-5.5 are GA only in
    // us-east-2 and us-west-2 today, so we scope by region. Unlike the
    // Anthropic statement above this one is intentionally separate so
    // that future provider expansion (Llama / Nova / Mistral) can each
    // ship their own statement without widening Anthropic or OpenAI.
    //
    // We list only the project-scoped resource ARNs we expect AWS to
    // accept; the inference action set covers `CreateInference`,
    // discovery (`Get*` / `List*`). If AWS later accepts wildcards on
    // the bedrock-mantle namespace at the resource level (similar to
    // `bedrock:*`-style ARNs), we should tighten further.
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'AllowOpenAIBedrockMantleInvoke',
        effect: iam.Effect.ALLOW,
        actions: ['bedrock-mantle:CreateInference', 'bedrock-mantle:Get*', 'bedrock-mantle:List*'],
        resources: [
          `arn:aws:bedrock-mantle:us-east-2:${account}:project/*`,
          `arn:aws:bedrock-mantle:us-west-2:${account}:project/*`,
        ],
      }),
    );

    // The bearer-token mint action that `aws-bedrock-token-generator`
    // performs in `mvp/openai_responses.py`.
    //
    // Verified at deploy time (2026-06-02): bedrock-mantle does NOT
    // accept resource-level conditions on `CallWithBearerToken`. A
    // region-scoped ARN yields:
    //   "User: ... is not authorized to perform: bedrock-mantle:CallWithBearerToken
    //    on resource: * because no identity-based policy allows ..."
    // `resources: ['*']` is therefore the documented AWS constraint,
    // not a posture choice. If AWS later supports resource-level scoping
    // (parity with bedrock:InvokeModel), tighten back to the project
    // ARN list used by AllowOpenAIBedrockMantleInvoke above.
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'AllowBedrockMantleBearerTokenMint',
        effect: iam.Effect.ALLOW,
        actions: ['bedrock-mantle:CallWithBearerToken'],
        resources: ['*'],
      }),
    );

    // SSM messages permissions required by ECS Exec
    // (`enableExecuteCommand: true`).
    //
    // P1-C (2026-04 review): when `enableExecuteCommand` is false, the
    // statement is dropped from the task role entirely. ssmmessages:*
    // exists solely to open shell channels — there is no other use —
    // so tying the permission to the feature flag is the correct
    // least-privilege posture. To re-open, pass
    // `ENABLE_ECS_EXEC=true` and re-run `cdk deploy`.
    if (props.enableExecuteCommand) {
      this.taskDefinition.taskRole.addToPrincipalPolicy(
        new iam.PolicyStatement({
          sid: 'AllowEcsExecChannels',
          effect: iam.Effect.ALLOW,
          actions: [
            'ssmmessages:CreateControlChannel',
            'ssmmessages:CreateDataChannel',
            'ssmmessages:OpenControlChannel',
            'ssmmessages:OpenDataChannel',
          ],
          resources: ['*'],
        }),
      );
    }

    // Cognito (scoped to the specified User Pool only).
    // Phase 2 (v2.1): Cognito Groups are not used, so
    // AdminAddUserToGroup / AdminRemoveUserFromGroup / AdminListGroupsForUser are not granted.
    // AdminUserGlobalSignOut is used to immediately invalidate JWTs on tenant switch.
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'cognito-idp:AdminCreateUser',
          'cognito-idp:AdminDeleteUser',
          'cognito-idp:AdminGetUser',
          'cognito-idp:AdminInitiateAuth',
          'cognito-idp:AdminRespondToAuthChallenge',
          'cognito-idp:AdminSetUserPassword',
          'cognito-idp:AdminUpdateUserAttributes',
          'cognito-idp:AdminUserGlobalSignOut',
          'cognito-idp:ListUsers',
        ],
        resources: [props.userPoolArn],
      }),
    );

    // Secrets Manager — split into two least-privilege statements
    // (A-06-iam):
    //
    //   1. Read-only `GetSecretValue` for everything under `${prefix}/*`.
    //      Container code reads provider tokens, JWT signing keys etc.
    //   2. `PutSecretValue` ONLY against the bootstrap-admin secret,
    //      which the lifespan seed must rewrite when a fresh password
    //      is generated. `CreateSecret` / `UpdateSecret` are not
    //      granted at all — the secret is pre-provisioned by CDK and
    //      `seed.py` was already idempotent on update; the previous
    //      blanket `${prefix}/*` write policy let any RCE inside the
    //      container forge secrets that the rotation script later
    //      consumed.
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['secretsmanager:GetSecretValue'],
        resources: [`arn:aws:secretsmanager:${region}:${account}:secret:${prefix}/*`],
      }),
    );
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['secretsmanager:PutSecretValue'],
        resources: [
          // Wildcard suffix (`*`) is required because Secrets Manager
          // appends a 6-char random suffix to the ARN at create time;
          // pinpointing the exact suffix would force CloudFormation to
          // re-deploy the policy after every secret rotation.
          `arn:aws:secretsmanager:${region}:${account}:secret:${prefix}/bootstrap-admin-temp-password-*`,
        ],
      }),
    );

    // SSM Parameter Store (restricted to /${prefix}/* only)
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['ssm:GetParameter', 'ssm:GetParameters', 'ssm:GetParametersByPath'],
        resources: [`arn:aws:ssm:${region}:${account}:parameter/${prefix}/*`],
      }),
    );

    // Per-tenant VSR config store (opaque blobs). The bucket is VERSIONED (free
    // rollback + last-known-good history), private, TLS-enforced, KMS-managed.
    // The backend task role is granted Get/Put/Delete on the `vsr-config/*`
    // object prefix — never a bucket-wide object grant. A prefix-scoped
    // ListBucket is ALSO granted (condition: s3:prefix = vsr-config/*): without
    // it, S3 returns 403 AccessDenied (not 404 NoSuchKey) for a GetObject on a
    // key that does not exist yet, because the caller has no permission to know
    // whether the object exists. That turns the common "tenant has no config
    // yet" case into a 400 error instead of the intended 404, breaking the UI's
    // create-first-config flow. The prefix condition keeps enumeration scoped to
    // the vsr-config/ keyspace only. Ships dark: without the flag there is no
    // bucket, no grant, no env var, and the admin surface 404s.
    const vsrEnv: { [key: string]: string } = {};
    if (props.enableVsrConfigBucket) {
      const bucket = new s3.Bucket(this, 'VsrConfigBucket', {
        bucketName: `${prefix}-vsr-config-${this.account}`,
        versioned: true,
        blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
        encryption: s3.BucketEncryption.S3_MANAGED,
        enforceSSL: true,
        removalPolicy: cdk.RemovalPolicy.RETAIN,
      });
      this.vsrConfigBucket = bucket;
      this.taskDefinition.taskRole.addToPrincipalPolicy(
        new iam.PolicyStatement({
          sid: 'VsrConfigBlobRw',
          effect: iam.Effect.ALLOW,
          actions: ['s3:GetObject', 's3:PutObject', 's3:DeleteObject'],
          resources: [`${bucket.bucketArn}/vsr-config/*`],
        }),
      );
      // Prefix-scoped ListBucket so a GetObject on a not-yet-created key returns
      // 404 (NoSuchKey), not 403 (AccessDenied). Restricted to the vsr-config/
      // prefix via the s3:prefix condition — the role can never enumerate any
      // other keyspace in the bucket.
      this.taskDefinition.taskRole.addToPrincipalPolicy(
        new iam.PolicyStatement({
          sid: 'VsrConfigBlobList',
          effect: iam.Effect.ALLOW,
          actions: ['s3:ListBucket'],
          resources: [bucket.bucketArn],
          conditions: { StringLike: { 's3:prefix': ['vsr-config/*'] } },
        }),
      );
      vsrEnv.VSR_CONFIG_BUCKET = bucket.bucketName;
    }

    const container = this.taskDefinition.addContainer('BackendContainer', {
      image: ecs.ContainerImage.fromEcrRepository(props.repository, props.imageTag || 'latest'),
      logging: ecs.LogDriver.awsLogs({ logGroup, streamPrefix: 'backend' }),
      environment: { ...(props.environment || {}), ...vsrEnv },
      secrets: props.secrets || {},
      portMappings: [{ containerPort: props.containerPort || 8000, protocol: ecs.Protocol.TCP }],
      healthCheck: {
        command: ['CMD-SHELL', 'curl -f http://localhost:8000/health || exit 1'],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(60),
      },
    });

    this.service = new ecs.FargateService(this, 'BackendService', {
      cluster: this.cluster,
      taskDefinition: this.taskDefinition,
      desiredCount: props.desiredCount ?? 1,
      assignPublicIp: true, // placed directly in the Public Subnet
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      securityGroups: [props.securityGroup],
      serviceName: `${prefix}-backend`,
      // P1-C: default off. Callers must opt in explicitly.
      enableExecuteCommand: props.enableExecuteCommand ?? false,
      healthCheckGracePeriod: cdk.Duration.seconds(60),
      // Fargate automatically spreads tasks across the AZs of the given
      // subnets (the VPC has maxAzs=2), so desiredCount>=2 yields one task
      // per AZ — no single AZ is a SPOF. No placementStrategies here:
      // those are EC2-launch-type only.
      //
      // Keep at least the desired count running through a rolling deploy
      // (start replacements before draining) so there is no single-task
      // gap window during deploys.
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
    });

    this.service.attachToApplicationTargetGroup(props.targetGroup);

    // Auto scaling. Floor tracks the desired count so we never scale below
    // the multi-task/multi-AZ baseline; ceiling gives headroom under load.
    const baseCount = props.desiredCount ?? 1;
    const scaling = this.service.autoScaleTaskCount({
      minCapacity: baseCount,
      maxCapacity: baseCount > 1 ? Math.max(baseCount * 2, 4) : 1,
    });
    scaling.scaleOnCpuUtilization('CpuScaling', {
      targetUtilizationPercent: 70,
      scaleInCooldown: cdk.Duration.seconds(60),
      scaleOutCooldown: cdk.Duration.seconds(60),
    });

    // When Application Auto Scaling manages the task count, a `DesiredCount`
    // baked into the CFN template makes every `cdk deploy` reset the running
    // count — including snapping back down mid-incident when the scaler had
    // grown the fleet. Drop `DesiredCount` from the template so deploys leave
    // the running count alone and the scaler (floored at `minCapacity =
    // baseCount`) owns it.
    //
    // Trade-off on a FRESH stack: with `DesiredCount` absent, CFN creates the
    // service at its default of 1 task, waits for that one to stabilise, and
    // THEN the scalable target registers and scales out to `minCapacity`. So a
    // brand-new stack briefly runs a single task before reaching the multi-AZ
    // floor (self-healing within a scaling interval). Acceptable here; if a
    // deploy gate ever requires >=2 healthy targets at create time, seed the
    // initial size differently (e.g. a context flag flipped after bootstrap).
    if (baseCount > 1) {
      const cfnService = this.service.node.defaultChild as ecs.CfnService;
      cfnService.addPropertyDeletionOverride('DesiredCount');
    }

    // Parameter Store exports
    putStringParameter(this, 'EcsClusterParam', {
      prefix,
      relativePath: 'backend/ecs-cluster',
      value: this.cluster.clusterName,
      description: 'ECS Cluster name',
    });
    putStringParameter(this, 'EcsServiceParam', {
      prefix,
      relativePath: 'backend/ecs-service',
      value: this.service.serviceName,
      description: 'ECS Service name',
    });
    putStringParameter(this, 'EcsTaskFamilyParam', {
      prefix,
      relativePath: 'backend/task-definition-family',
      value: this.taskDefinition.family,
      description: 'ECS Task Definition family',
    });
    putStringParameter(this, 'EcsLogGroupParam', {
      prefix,
      relativePath: 'backend/log-group-name',
      value: logGroup.logGroupName,
      description: 'Backend CloudWatch log group name',
    });

    new cdk.CfnOutput(this, 'ClusterName', { value: this.cluster.clusterName });
    new cdk.CfnOutput(this, 'ServiceName', { value: this.service.serviceName });

    applyCommonTags(this, prefix, 'ECS');
  }
}
