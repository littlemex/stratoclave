import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import { applyCommonTags } from './_common';
import { TenantAlarm } from './tenant-alarm';

/**
 * Daily reconciliation of the tenant pool ceiling against its sources.
 *
 * Its own stack, on the convention every other scheduled job in this repository
 * already follows (`certificate-scheduler-stack.ts`, `ledger-projector-stack.ts`).
 * Putting the schedule and the alarms on the service stack instead would have
 * made this the one exception, and a conventional implementation would then read
 * as the deviation.
 *
 * The reconciler compares the row to what it is DERIVED FROM -- the tenant's
 * memberships, and the declared per-seat rate -- because an equation over the row
 * cannot see a delta applied twice: the seat count and the ceiling both move, in
 * the same direction, so everything still balances while the tenant admits an
 * extra seat's worth of spend a month.
 *
 * The reconciler is read-only. It holds no write grant at all, which is the
 * enforcement of "reports and never repairs": a reconciler that could fix what it
 * finds would destroy the evidence of how the row got that way.
 *
 * The PERIOD ROLLOVER rides the same schedule and is a SEPARATE FUNCTION with its own
 * write grant, even though it lives in the same backend module. Sharing a module is not
 * sharing a permission: grants attach to functions, not to files, so only the
 * rollover's role can write and the reconciler's read-only posture survives the
 * colocation. It has to stay separate precisely because of the sentence above —
 * merging the two roles would hand the reconciler the ability to repair what it finds,
 * and the absence of a write grant is the only thing making that structural rather
 * than conventional. One schedule, two functions, two grants.
 */
export interface QuotaReconcilerStackProps extends cdk.StackProps {
  prefix: string;
  /** ECR repo holding the Lambda image (built from backend/Dockerfile.lambda). */
  lambdaRepository: ecr.IRepository;
  /** Immutable tag of the Lambda image. */
  lambdaImageTag: string;
  /** The pool rows this reconciles. */
  tenantBudgetsTable: dynamodb.ITable;
  /** The seat counts it compares them against. */
  userTenantsTable: dynamodb.ITable;
  /**
   * The grants `grant_target_row_exists` (registered in `mvp/grants.py`) walks to
   * find one pointing at a pool row that no longer exists. That check runs on
   * every pass, unconditionally, the same as every other registered check — so
   * this table is not optional: without it `QuotaEventsRepository()` falls back
   * to the hard-coded default table name (`dynamo/client.py::quota_events_table_name`),
   * which is wrong for any prefix other than `stratoclave`, and the reconciler's
   * role has no grant on it either way. Both together turned every invocation
   * into an unhandled `AccessDeniedException` on a real deploy — caught only by
   * actually invoking the function, because the unit tests set every table env
   * var through one shared fixture regardless of what iac wires.
   */
  quotaEventsTable: dynamodb.ITable;
  /** How often. Daily by default: every finding here is a slow drift. */
  schedule?: events.Schedule;
}

export class QuotaReconcilerStack extends cdk.Stack {
  public readonly reconciler: lambda.Function;
  public readonly periodRollover: lambda.Function;

  constructor(scope: Construct, id: string, props: QuotaReconcilerStackProps) {
    super(scope, id, props);
    const {
      prefix, lambdaRepository, lambdaImageTag, tenantBudgetsTable, userTenantsTable,
      quotaEventsTable,
    } = props;

    const metricNamespace = `${prefix}/CreditLedger`;

    // A dedicated, CDK-managed log group per function, on the same pattern as
    // `certificate-scheduler-stack.ts`'s `IssuerLogGroup`. A Lambda's DEFAULT
    // `/aws/lambda/<function-name>` group is created lazily by the Lambda
    // service on first invocation, not at deploy time — so `fromLogGroupName`
    // (importing a group that does not exist yet) left the metric filters
    // below pointing at nothing on a fresh account, and `AWS::Logs::MetricFilter`
    // fails CREATE with a ResourceNotFoundException the first time this stack
    // is deployed, rolling the whole stack back before either Lambda ever runs.
    const reconcilerLogGroup = new logs.LogGroup(this, 'ReconcilerLogGroup', {
      logGroupName: `/lambda/${prefix}-quota-reconciler`,
      retention: logs.RetentionDays.THREE_MONTHS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.reconciler = new lambda.DockerImageFunction(this, 'Reconciler', {
      functionName: `${prefix}-quota-reconciler`,
      code: lambda.DockerImageCode.fromEcr(lambdaRepository, {
        tagOrDigest: lambdaImageTag,
        cmd: ['mvp.observability.quota_reconciler.handler'],
      }),
      memorySize: 512,
      // A full pass over the pool rows plus one strongly consistent pass over the
      // memberships. Minutes, not seconds, and a timeout that cuts the pass short
      // would report "clean" over the prefix it managed to read.
      timeout: cdk.Duration.minutes(10),
      environment: {
        DYNAMODB_TENANT_BUDGETS_TABLE: tenantBudgetsTable.tableName,
        DYNAMODB_USER_TENANTS_TABLE: userTenantsTable.tableName,
        // grant_target_row_exists (mvp/grants.py) walks this table on every pass.
        DYNAMODB_QUOTA_EVENTS_TABLE: quotaEventsTable.tableName,
        STRATOCLAVE_METRIC_NAMESPACE: metricNamespace,
      },
      logGroup: reconcilerLogGroup,
      description:
        'Compares each tenant pool row to its sources (memberships, declared seat rate).',
    });
    // READ ONLY, on all three tables. The absence of a write grant is what makes
    // "never repairs" a property of the deployment rather than of the code.
    tenantBudgetsTable.grantReadData(this.reconciler);
    userTenantsTable.grantReadData(this.reconciler);
    quotaEventsTable.grantReadData(this.reconciler);

    // --- Period rollover: create each month's pool row for tenants that had one ---
    // The pool row is keyed by calendar month and a MISSING row means "not pooled",
    // because pool budgeting is opt-in. Creating the new month's row only when a
    // membership changes therefore left every tenant with stable membership
    // unpooled for the whole month — failing open, in the direction that admits
    // spend, on the common case. This job is what makes the row exist.
    const rolloverLogGroup = new logs.LogGroup(this, 'PeriodRolloverLogGroup', {
      logGroupName: `/lambda/${prefix}-quota-period-rollover`,
      retention: logs.RetentionDays.THREE_MONTHS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.periodRollover = new lambda.DockerImageFunction(this, 'PeriodRollover', {
      functionName: `${prefix}-quota-period-rollover`,
      code: lambda.DockerImageCode.fromEcr(lambdaRepository, {
        tagOrDigest: lambdaImageTag,
        cmd: ['mvp.observability.quota_reconciler.rollover_handler'],
      }),
      memorySize: 512,
      timeout: cdk.Duration.minutes(10),
      environment: {
        DYNAMODB_TENANT_BUDGETS_TABLE: tenantBudgetsTable.tableName,
        STRATOCLAVE_METRIC_NAMESPACE: metricNamespace,
      },
      logGroup: rolloverLogGroup,
      description:
        "Creates each period's pool row for tenants holding the prior period's row.",
    });
    // WRITE, and only on the budgets table. It never reads the membership table: its
    // unit of work is "tenants with a prior-period row", which is the opt-in signal,
    // and a tenant that never had a pool must not acquire one from a rollover — that
    // would make the scheduler a writer of ceilings nobody set.
    tenantBudgetsTable.grantReadWriteData(this.periodRollover);

    // ONE rule, both functions. A second schedule would be a second thing to watch
    // and could drift from this one; the rollover has to have run before the
    // reconciler's findings about the current period mean anything.
    new events.Rule(this, 'ReconcilerSchedule', {
      ruleName: `${prefix}-quota-reconciler-schedule`,
      schedule: props.schedule ?? events.Schedule.rate(cdk.Duration.days(1)),
      targets: [
        new targets.LambdaFunction(this.periodRollover),
        new targets.LambdaFunction(this.reconciler),
      ],
    });

    // (1) A row disagrees with its sources. Per-tenant, so it goes through the
    // shared construct: the metric is undimensioned and the matched line names
    // the tenant, which resolves in one query.
    new TenantAlarm(this, 'PoolCeilingDefect', {
      logGroup: reconcilerLogGroup,
      prefix,
      scope: 'tenant',
      metricNamespace,
      metricName: 'PoolCeilingDefects',
      event: 'pool_ceiling_defect',
      valueField: '$.defects',
      threshold: 0,
      // Daily emission, so a 5-minute period with 1/1: there is no second
      // datapoint to confirm against, and requiring one would make this alarm
      // structurally unable to reach ALARM.
      period: cdk.Duration.minutes(5),
      evaluationPeriods: 1,
      datapointsToAlarm: 1,
      // A clean day emits no defect line, and that really is nothing to report.
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription:
        'A tenant pool row disagrees with its sources: its seat count does not match the tenant\'s active memberships, its ceiling does not equal its own composition, its headroom does not equal limit minus reserved minus settled, or it was computed at a seat rate that is no longer in force. A ceiling that is too high admits spend rather than refusing it. Find the tenant in the pool_ceiling_defect log line.',
    });

    // (2) A check the declaration names is not registered. Deployment-scoped:
    // there is no tenant, and saying so explicitly is what stops a per-tenant
    // signal from losing its attribution by defaulting into this shape.
    //
    // MISSING DATA IS BREACHING, unlike (1). This is a gate metric: silence means
    // the reconciler did not run, and a pass that never happened must not read as
    // a pass that found nothing. That distinction is the whole difference between
    // the two alarms here.
    new TenantAlarm(this, 'PoolCeilingChecksMissing', {
      logGroup: reconcilerLogGroup,
      prefix,
      scope: 'deployment',
      metricNamespace,
      metricName: 'PoolCeilingChecksMissing',
      event: 'pool_ceiling_reconcile',
      valueField: '$.PoolCeilingChecksMissing',
      threshold: 0,
      period: cdk.Duration.hours(25),
      evaluationPeriods: 1,
      datapointsToAlarm: 1,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
      alarmDescription:
        'Either the daily pool-ceiling reconciler stopped emitting, or the row declaration names a check that nothing registered. Both mean an attribute that looks covered is not being compared to anything. Do not treat a quiet metric here as a healthy one.',
    });

    // (3) A tenant the rollover could not carry forward. Per-tenant, so it goes
    // through the shared construct. This is the alarm that matters most in this
    // stack: a tenant left without a row for the period is a tenant whose requests
    // the gateway now refuses, which is the safe direction and still an outage for
    // them.
    new TenantAlarm(this, 'PoolPeriodRolloverFailures', {
      logGroup: rolloverLogGroup,
      prefix,
      scope: 'tenant',
      metricNamespace,
      metricName: 'PoolPeriodRolloverFailures',
      event: 'pool_period_rollover_failed',
      valueField: '$.failures',
      threshold: 0,
      period: cdk.Duration.minutes(5),
      evaluationPeriods: 1,
      datapointsToAlarm: 1,
      // A clean run emits no failure line, and that really is nothing to report.
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription:
        "A tenant's pool row could not be carried into the current period. Its ceiling does not exist for this period, so the gateway refuses that tenant's priced requests rather than admitting them unbounded — the safe direction, and still an outage for that tenant until the row exists. Find the tenant in the pool_period_rollover_failed log line and re-run the rollover.",
    });

    applyCommonTags(this, prefix, 'quota-reconciler');
  }
}
