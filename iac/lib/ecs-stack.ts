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
  /**
   * Concrete rather than `IApplicationTargetGroup`: request-count scaling needs
   * the group's own resource label to build the ALB metric dimension, which an
   * imported group cannot supply.
   */
  targetGroup: elbv2.ApplicationTargetGroup;

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

  /**
   * Horizontal scaling policy for the backend service.
   *
   * The primary signal is ALB requests per target, not CPU. CPU is a poor proxy
   * here: a request spends most of its life waiting on Bedrock, so a task can be
   * saturated — every worker thread held, latency climbing — while average CPU
   * still reads well under any sane target. Measured on 2026-08-24, average task
   * CPU sat at 25-29% while throughput had already flattened and p50 latency had
   * grown 18x. Requests per target moves with offered load, so it reacts to the
   * queue the CPU metric cannot see.
   *
   * CPU tracking stays configured as a second, slower signal for the case where
   * work is genuinely compute-bound rather than upstream-bound.
   */
  autoScaling?: {
    /** ceiling on task count @default max(desiredCount * 2, 4) */
    maxCapacity?: number;
    /**
     * Requests per target per minute to hold. Derive it from measured per-task
     * capacity and keep headroom: a target set AT saturation keeps every task at
     * the point where latency has already degraded.
     */
    requestsPerTarget?: number;
    /** CPU target for the secondary policy @default 70 */
    cpuTargetPercent?: number;
  };

  environment?: { [key: string]: string };
  secrets?: { [key: string]: ecs.Secret };

  /**
   * PENDING-protocol reserve canary (docs/design/pending-protocol.md, rollout
   * Shadow->Canary->Full). A list of tenant ids that use the non-transactional
   * separate-item-marker reserve path EVEN WHILE the global default stays
   * "transaction", so a single tenant is flipped without a global switch. Wired to
   * the backend as ``STRATOCLAVE_RESERVE_PROTOCOL_TENANTS`` (comma-separated).
   * Empty/absent => every tenant stays transaction-mode (feature ships dark). The
   * global ``STRATOCLAVE_RESERVE_PROTOCOL=pending`` override (all tenants) is set
   * via ``environment`` when the canary graduates to Full.
   */
  reserveProtocolCanaryTenants?: string[];

  /**
   * When true, create the per-tenant VSR config bucket (versioned, private,
   * TLS-enforced) and grant the backend task role Get/Put/Delete ONLY on the
   * ``vsr-config/*`` prefix, and inject ``VSR_CONFIG_BUCKET`` into the container
   * environment. Absent/false => no bucket, no grant, no env (feature ships
   * dark; the admin surface 404s until this is provisioned).
   */
  enableVsrConfigBucket?: boolean;

  /**
   * Activates the live pricing subsystem (docs/design/price-feeds.md). Sets
   * `STRATOCLAVE_PRICE_SOURCE` to this value (e.g. `"bedrock-live"`) on the task
   * definition, AND is the switch that decides whether the read-only
   * price-discovery IAM statement (`bedrock:ListFoundationModelAgreementOffers`,
   * `pricing:GetProducts`) is attached to the task role at all — the two are
   * gated on the same prop so a deployment cannot carry the permission with no
   * variable naming a source to use it, or the variable with no permission to
   * fetch. Absent/undefined => neither is present, and `active_source_name()`
   * resolves to the bundled `json` source exactly as it does today (feature
   * ships dark).
   */
  priceSource?: string;

  /**
   * Feed knobs, passed through only when `priceSource` is set (docs/design/price-feeds.md
   * §5, "Operating it"). Every field is optional; an absent field leaves the
   * backend's own built-in default in force, so this carries only the
   * overrides an operator actually wants, not a mandatory full config.
   */
  priceFeed?: {
    /** `STRATOCLAVE_PRICE_FEED_INTERVAL_SECONDS` */
    intervalSeconds?: number;
    /** `STRATOCLAVE_PRICE_FEED_BUDGET_SECONDS` */
    budgetSeconds?: number;
    /** `STRATOCLAVE_PRICE_FEED_STALE_AFTER_SECONDS` */
    staleAfterSeconds?: number;
  };

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

    // PENDING-protocol canary observability (docs/design/pending-protocol.md,
    // PR-1 item A′ + follow-up). The separate-item marker's WHOLE POINT is that
    // the hot pool item stays small and FLAT; this is the live detector that a
    // code regression reintroduced per-hold growth on it (the deductive
    // WCU∝item-size argument assumes that away, so we watch for the assumption
    // breaking). The backend logs `pool_item_size {size_bytes=N}` once per
    // reconcile; emit N as a GAUGE and alarm above a small ceiling.
    const poolSizeMf = logGroup.addMetricFilter('PoolItemSizeMF', {
      filterName: `${prefix}-pool-item-size`,
      filterPattern: logs.FilterPattern.stringValue('$.event', '=', 'pool_item_size'),
      metricNamespace: METRIC_NS,
      metricName: 'PoolItemSizeBytes',
      metricValue: '$.size_bytes',
      // no defaultValue: only emit on a real datapoint so the gauge is not
      // polluted with zeros from unrelated log lines.
    });
    new cloudwatch.Alarm(this, 'PoolItemSizeGrowth', {
      alarmName: `${prefix}-PoolItemSizeBytes`,
      alarmDescription:
        'PENDING protocol: the hot pool item exceeded its expected small/flat size — a code regression may have reintroduced per-hold growth on it (the rejected marker-in-pool-item design). Investigate before growth degrades write latency.',
      metric: poolSizeMf.metric({ statistic: 'Maximum', period: cdk.Duration.minutes(5) }),
      // This one catches UNBOUNDED growth — the rejected map design, whose failure
      // is orders of magnitude — so a generous absolute ceiling is the right shape
      // for it and 2 KB still bites long before write latency does. It is NOT the
      // detector for the row holding one attribute more than it should: that
      // difference is tens of bytes, an absolute figure typed here would be
      // calibrated to whichever row shape existed when it was typed, and the next
      // schema change would make it fire on growth that change intended. The
      // PoolRowBeyondDeclaration alarm below is the tight, derived one.
      threshold: 2048,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      // 1/1, NOT 3/3 (Fable E-phase review Bug-1): `pool_item_size` is emitted only
      // once per reconcile, so on a fleet whose reconcile cadence is < 1/5-min there
      // are rarely 3 consecutive 5-min buckets with a datapoint — 3/3 + missing=
      // NOT_BREACHING would make the alarm structurally unable to reach ALARM.
      // Item-size growth is monotonic and does not flap, so a single over-threshold
      // datapoint IS the real regression: alarm immediately.
      evaluationPeriods: 1,
      datapointsToAlarm: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // The tight bound, and it is DERIVED rather than configured. The backend emits
    // `over_declared_bytes` alongside the gauge — the observed row size minus the
    // width its own closed-world declaration allows — so this alarm's threshold is
    // zero forever and the calibration lives with the schema. A schema change moves
    // the bound with it; nothing here has to be remembered or re-measured.
    //
    // What it catches that the 2 KB alarm cannot: an attribute being written to the
    // pool row that the declaration does not classify. That is tens of bytes, so it
    // is invisible under a generous absolute ceiling, and it is precisely the case
    // where the rollover does not know whether to carry the attribute, no
    // reconciler check covers it, and the size accounting does not count it.
    const poolOverDeclaredMf = logGroup.addMetricFilter('PoolRowOverDeclaredMF', {
      filterName: `${prefix}-pool-row-over-declared`,
      filterPattern: logs.FilterPattern.all(
        logs.FilterPattern.stringValue('$.event', '=', 'pool_item_size'),
        logs.FilterPattern.exists('$.over_declared_bytes'),
      ),
      metricNamespace: METRIC_NS,
      metricName: 'PoolRowOverDeclaredBytes',
      metricValue: '$.over_declared_bytes',
      // No defaultValue, same reason as the gauge above.
    });
    new cloudwatch.Alarm(this, 'PoolRowBeyondDeclaration', {
      alarmName: `${prefix}-PoolRowBeyondDeclaration`,
      alarmDescription:
        'A tenant pool row is stored wider than its own closed-world declaration allows. Either an attribute is being written that the declaration does not classify, or one holds a wider value than declared. Both mean the row has grown outside what the period rollover, the reconciler checks and the size accounting know about — so the next boundary may drop it, no check compares it to anything, and the measured worst case is not the row that ships.',
      metric: poolOverDeclaredMf.metric({
        statistic: 'Maximum', period: cdk.Duration.minutes(5),
      }),
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      // Same 1/1 reasoning as the gauge: emitted once per reconcile, monotonic,
      // does not flap.
      evaluationPeriods: 1,
      datapointsToAlarm: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // RETENTION EXPOSURE (C8.3's missing watcher). `STRATOCLAVE_UNOBSERVED_HOLDS`
    // defaults ON, so a reservation whose provider call departed and whose outcome was
    // never observed is HELD rather than returned. That record is correct — an abandoned
    // Bedrock call is billed for the full generation — but it moves the failure mode:
    // retentions accumulate against a tenant's headroom and, without these alarms, the
    // first signal an operator gets is a refusal for an unrelated request.
    //
    // The backend emits `retention_exposure` with the standing figures for one tenant and
    // period: when a retention is taken, when one is resolved, and from a sweep at most
    // once per minute per tenant per task so a persistent exposure keeps producing
    // datapoints. That last part is why these alarms can use missing=NOT_BREACHING
    // honestly: no retentions means no line, which really is nothing to report, while an
    // UNRESOLVED retention keeps reporting and cannot clear the alarm by going quiet.
    //
    // The metrics carry NO tenant dimension, deliberately: a per-tenant dimension is
    // unbounded cardinality on a filter that runs over every backend log line. So the
    // alarm is on the WORST tenant (Maximum) and the log line names which one. That is the
    // right shape anyway — one saturated tenant is the incident, not the fleet average.
    const mkExposureFilter = (field: string, metricName: string) =>
      logGroup.addMetricFilter(`RetentionMF${metricName}`, {
        filterName: `${prefix}-retention-${metricName}`,
        filterPattern: logs.FilterPattern.all(
          logs.FilterPattern.stringValue('$.event', '=', 'retention_exposure'),
          logs.FilterPattern.exists(`$.${field}`),
        ),
        metricNamespace: METRIC_NS,
        metricName,
        metricValue: `$.${field}`,
        // No defaultValue: a gauge, so an unrelated log line must not push a zero into it
        // and drag a Maximum down.
      });

    const heldFractionMf = mkExposureFilter('held_fraction', 'RetentionHeldFraction');
    const retentionAgeMf = mkExposureFilter(
      'oldest_retention_age_seconds', 'RetentionOldestAgeSeconds');
    // Absolute exposure as a metric with no alarm: the fraction says who is at risk, this
    // says how much money is parked, and an operator reconciling an invoice wants both.
    mkExposureFilter('held_microusd', 'RetentionHeldMicroUsd');

    // (1) Saturation: unresolved retentions are holding a quarter of some tenant's pool.
    // Well before a refusal, and far enough above noise that a single stuck retention on a
    // small pool does not page. 1-minute periods with 3/3 rather than 5-minute buckets,
    // because the emission cadence is ~1/minute while retentions exist (the same reasoning
    // as PoolItemSizeBytes above, in the other direction): a provider outage fills headroom
    // in minutes, so the alarm has to be able to resolve in minutes.
    new cloudwatch.Alarm(this, 'RetentionSaturation', {
      alarmName: `${prefix}-RetentionHeldFraction`,
      alarmDescription:
        'Unresolved retained reservations are holding >25% of a tenant\'s dollar pool. The money may genuinely have been spent (an unobserved provider call is billed), so this is not necessarily a bug — but the headroom is gone until an operator settles each retention at the figure the provider\'s record shows or releases it when that record shows none. Find the tenant in the retention_exposure log line.',
      metric: heldFractionMf.metric({
        statistic: 'Maximum', period: cdk.Duration.minutes(1),
      }),
      threshold: 0.25,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 3,
      datapointsToAlarm: 3,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // (2) Staleness: a retention nobody has resolved for two days. A different failure from
    // saturation and not detectable by the same threshold — a high fraction that is minutes
    // old is an incident in progress, the same fraction two weeks old is an operator who
    // stopped looking, and only the age separates them. Hourly periods: this is a slow
    // signal and paging on it inside a minute would be noise.
    new cloudwatch.Alarm(this, 'RetentionStale', {
      alarmName: `${prefix}-RetentionOldestAgeSeconds`,
      alarmDescription:
        'A retained reservation has gone unresolved for over 48 hours. Retention is not a state anything clears on its own: it ends only when an operator settles it at the provider\'s figure or releases it. Budget stays held until then.',
      metric: retentionAgeMf.metric({
        statistic: 'Maximum', period: cdk.Duration.hours(1),
      }),
      threshold: 48 * 60 * 60,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      datapointsToAlarm: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // RESERVE ORACLE mismatch (golden-reference migration, docs/design/
    // pending-protocol.md): the pending reserve's write-set diverged from what the
    // FROZEN transaction golden predicted for the same input. This is the signal
    // that the two money paths are NOT equivalent — it MUST be zero before
    // transaction is deleted (the delete gate keys on it). The oracle is fail-open
    // (it never rolls back), so this alarm is how a divergence is caught. Alarm on
    // a single occurrence.
    // Match + race counters (Fable review 2): the delete gate is "match >= N AND
    // mismatch == 0", NOT merely "mismatch == 0" (which passes on ZERO samples —
    // a vacuous gate). ReserveOracleMatch counts verified-equivalent reserves;
    // ReserveOracleRace counts benign TOCTOU disagreements (pool moved concurrently
    // between the pre-read and commit) — a metric only, NOT alarmed, so canary
    // traffic near the ceiling does not flap the mismatch alarm.
    mkFilter('reserve_oracle_match', 'ReserveOracleMatch');
    mkFilter('reserve_oracle_race', 'ReserveOracleRace');
    const reserveOracleMf = mkFilter('reserve_oracle_mismatch', 'ReserveOracleMismatch');
    new cloudwatch.Alarm(this, 'ReserveOracleMismatch', {
      alarmName: `${prefix}-ReserveOracleMismatch`,
      alarmDescription:
        'Reserve golden-oracle: the pending reserve write-set diverged from the transaction golden prediction. The two money paths are not equivalent — investigate before trusting/deleting the transaction path.',
      metric: reserveOracleMf.metric({ statistic: 'Sum', period: cdk.Duration.minutes(5) }),
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      datapointsToAlarm: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // PENDING-protocol reconcile invariant signal: a credit-back hit a
    // non-transient defect (e.g. a marker/period mismatch) — the hold is left for
    // a human, never auto-credited. Real state corruption → alarm on one.
    const reconcileInvariantMf = mkFilter(
      'pool_reconcile_credit_back_invariant', 'PoolReconcileCreditBackInvariant');
    new cloudwatch.Alarm(this, 'PoolReconcileCreditBackInvariant', {
      alarmName: `${prefix}-PoolReconcileCreditBackInvariant`,
      alarmDescription:
        'PENDING protocol: reconcile credit-back hit a non-transient invariant violation (marker/period mismatch); the hold is quarantined, needs investigation.',
      metric: reconcileInvariantMf.metric({ statistic: 'Sum', period: cdk.Duration.minutes(5) }),
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      datapointsToAlarm: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

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
    // Non-Anthropic models the registry serves over Converse (see
    // `backend/mvp/models.py` `_REGISTRY`, entries whose `wire_protocol` is
    // "messages"). They are reachable only through /v1/chat/completions.
    //
    // Listed by EXACT model id, not by a `nvidia.*` / `qwen.*` vendor wildcard:
    // the scoping rationale above is cost containment, and a vendor wildcard
    // would grant every current and future model those vendors publish. The
    // coupling is deliberate — registering a Converse model without adding it
    // here fails loudly with AccessDeniedException at invoke time rather than
    // quietly widening the blast radius.
    //
    // These carry no inference profile: the registry names the bare
    // foundation-model id, so only the region-less foundation-model ARN applies.
    const registryConverseModelIds = [
      'nvidia.nemotron-super-3-120b',
      'qwen.qwen3-next-80b-a3b',
    ];
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'AllowRegistryConverseInvoke',
        effect: iam.Effect.ALLOW,
        actions: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
          'bedrock:Converse',
          'bedrock:ConverseStream',
        ],
        resources: registryConverseModelIds.map(
          (modelId) => `arn:aws:bedrock:*::foundation-model/${modelId}`,
        ),
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

    // OpenAI-family models (codex / GPT-5.6, Grok) on Amazon Bedrock.
    //
    // These used to be invoked through `bedrock-mantle.{region}.api.aws`, which has
    // its own `bedrock-mantle:*` action set, and this block granted it. The routes
    // now call the OpenAI-compatible surface on `bedrock-runtime` — the endpoint AWS
    // recommends, and the only one whose calls appear in model invocation logs — so
    // the permissions move into the `bedrock` namespace with everything else.
    //
    // Scoped by inference-profile prefix rather than `*` for the same reason as the
    // Anthropic statement above: `Resource: *` would let an RCE invoke any model in
    // the account. The registry pins these to `us.` profiles, so that is what is
    // granted; add a prefix here when a registry entry starts using one.
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'AllowOpenAIFamilyBedrockInvoke',
        effect: iam.Effect.ALLOW,
        actions: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
        ],
        resources: [
          // The OpenAI-compatible surface authorizes against a PROJECT resource, not
          // against the model: without this line the call is denied with "not
          // authorized to perform: bedrock:InvokeModel on resource:
          // arn:aws:bedrock:<region>:<account>:project/default". Verified by assuming
          // a role carrying exactly these statements and calling the real endpoint —
          // the model-scoped ARNs alone produced a 401, and adding this produced 200.
          // The mantle statement this replaced had the same shape
          // (`arn:aws:bedrock-mantle:...:project/*`); dropping it in the namespace
          // move was the defect.
          `arn:aws:bedrock:*:${account}:project/*`,
          // Kept as well: `Converse`-style model ARNs are what the same actions are
          // scoped by elsewhere, and a future direct InvokeModel on these families
          // should not need a second policy edit.
          `arn:aws:bedrock:*::foundation-model/openai.*`,
          `arn:aws:bedrock:*::foundation-model/xai.*`,
          `arn:aws:bedrock:*:${account}:inference-profile/us.openai.*`,
          `arn:aws:bedrock:*:${account}:inference-profile/global.openai.*`,
          `arn:aws:bedrock:*:${account}:inference-profile/us.xai.*`,
          `arn:aws:bedrock:*:${account}:inference-profile/global.xai.*`,
        ],
      }),
    );

    // The bearer-token mint that `aws-bedrock-token-generator` performs before each
    // OpenAI-compatible call (`mvp/_openai_transport.py`). Verified against the
    // moved endpoint that the same minted token is accepted, so the mechanism is
    // unchanged; only the namespace moves with the endpoint.
    //
    // `bedrock:CallWithBearerToken` is verified as the correct action in this
    // namespace: a role carrying only these two statements minted a token and got a
    // 200 from the real endpoint. `resources: ['*']` is inherited from the mantle
    // statement, where a region-scoped ARN was rejected outright. Whether the
    // `bedrock` namespace would accept resource-level scoping is still untested —
    // the mint succeeded with `*` and tightening it on a guess fails closed at
    // request time on a path with no fallback, so it stays wide until someone tests
    // the narrower form the same way the project ARN above was tested.
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'AllowBedrockBearerTokenMint',
        effect: iam.Effect.ALLOW,
        actions: ['bedrock:CallWithBearerToken'],
        resources: ['*'],
      }),
    );

    // Read-only price discovery for the live rate feed
    // (`STRATOCLAVE_PRICE_SOURCE=bedrock-live`, see docs/design/price-feeds.md).
    //
    // Two APIs, because Bedrock publishes its prices in two places and neither covers
    // everything: `ListFoundationModelAgreementOffers` carries the Marketplace-metered
    // families (every current Claude model, and GPT-5.x) while the Price List carries
    // the ones AWS bills directly (Nova, Llama, Mistral, Qwen, Grok, ...).
    //
    // `bedrock:CreateFoundationModelAgreement` is deliberately NOT granted. The
    // agreement response carries a signed `offerToken` that that action consumes, so
    // granting it would turn reading a price into subscribing to a paid product. Both
    // actions here are reads; neither is resource-scopeable in a way that has been
    // tested, so they follow the `*` precedent above rather than a guess that fails
    // closed at refresh time.
    //
    // Gated on `props.priceSource`, same as the env vars built below: this statement
    // and `STRATOCLAVE_PRICE_SOURCE` are the two halves of turning the subsystem on,
    // and a deployment carrying one without the other is either paying for a
    // permission it never uses or naming a source it cannot fetch. Ships dark —
    // absent `priceSource`, neither is present, and this is a no-op.
    const priceFeedEnv: { [key: string]: string } = {};
    if (props.priceSource) {
      this.taskDefinition.taskRole.addToPrincipalPolicy(
        new iam.PolicyStatement({
          sid: 'AllowReadOnlyPriceDiscovery',
          effect: iam.Effect.ALLOW,
          actions: [
            'bedrock:ListFoundationModelAgreementOffers',
            'pricing:GetProducts',
          ],
          resources: ['*'],
        }),
      );
      priceFeedEnv.STRATOCLAVE_PRICE_SOURCE = props.priceSource;
      if (props.priceFeed?.intervalSeconds !== undefined) {
        priceFeedEnv.STRATOCLAVE_PRICE_FEED_INTERVAL_SECONDS = String(props.priceFeed.intervalSeconds);
      }
      if (props.priceFeed?.budgetSeconds !== undefined) {
        priceFeedEnv.STRATOCLAVE_PRICE_FEED_BUDGET_SECONDS = String(props.priceFeed.budgetSeconds);
      }
      if (props.priceFeed?.staleAfterSeconds !== undefined) {
        priceFeedEnv.STRATOCLAVE_PRICE_FEED_STALE_AFTER_SECONDS = String(props.priceFeed.staleAfterSeconds);
      }
    }

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

    // PENDING-protocol reserve canary allowlist (docs/design/pending-protocol.md).
    // Injected only when non-empty, so the feature ships dark: no env var => the
    // backend's default (transaction mode for every tenant) is unchanged.
    if (props.reserveProtocolCanaryTenants && props.reserveProtocolCanaryTenants.length > 0) {
      vsrEnv.STRATOCLAVE_RESERVE_PROTOCOL_TENANTS =
        props.reserveProtocolCanaryTenants.join(',');
    }

    const container = this.taskDefinition.addContainer('BackendContainer', {
      image: ecs.ContainerImage.fromEcrRepository(props.repository, props.imageTag || 'latest'),
      // Non-blocking on purpose. The awslogs driver defaults to blocking, so a
      // slow or throttled PutLogEvents stops the container's stdout write — and
      // Python's logging holds a per-handler lock across that write, so every
      // thread in the process waits on CloudWatch. A request path must not be able
      // to stall on the log sink; losing a line under pressure is the better
      // failure, and the buffer size bounds how much can be in flight.
      logging: ecs.LogDriver.awsLogs({
        logGroup,
        streamPrefix: 'backend',
        mode: ecs.AwsLogDriverMode.NON_BLOCKING,
        maxBufferSize: cdk.Size.mebibytes(25),
      }),
      environment: { ...(props.environment || {}), ...vsrEnv, ...priceFeedEnv },
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
    const maxCapacity =
      props.autoScaling?.maxCapacity ?? (baseCount > 1 ? Math.max(baseCount * 2, 4) : 1);
    if (maxCapacity < baseCount) {
      // Application Auto Scaling would accept `minCapacity > maxCapacity` as a
      // template and then behave in a way nobody intended. A fleet whose floor is
      // above its ceiling is a configuration mistake, not a policy.
      throw new Error(
        `autoScaling.maxCapacity (${maxCapacity}) must be >= desiredCount (${baseCount}); ` +
          'the scaling floor tracks the desired count',
      );
    }
    const scaling = this.service.autoScaleTaskCount({
      minCapacity: baseCount,
      maxCapacity,
    });

    // Cooldowns are deliberately asymmetric. Scaling out has to answer a burst,
    // so it is short; scaling in on this workload would otherwise pull tasks out
    // from under a load that is still arriving, and the replacement task costs a
    // cold start before it serves anything.
    const scaleOutCooldown = cdk.Duration.seconds(60);
    const scaleInCooldown = cdk.Duration.seconds(300);

    // Primary signal: offered load per task. Only registered when the ceiling is
    // above the floor — a policy on a fixed-size service can never act, and one
    // that cannot act still reports as configured, which is worse than absent.
    if (maxCapacity > baseCount && props.autoScaling?.requestsPerTarget !== undefined) {
      scaling.scaleOnRequestCount('RequestCountScaling', {
        requestsPerTarget: props.autoScaling.requestsPerTarget,
        targetGroup: props.targetGroup,
        scaleInCooldown,
        scaleOutCooldown,
      });
    }

    scaling.scaleOnCpuUtilization('CpuScaling', {
      targetUtilizationPercent: props.autoScaling?.cpuTargetPercent ?? 70,
      scaleInCooldown,
      scaleOutCooldown,
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
