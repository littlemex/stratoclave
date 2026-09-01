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
 * Read-only. It holds no write grant at all, which is the enforcement of "reports
 * and never repairs": a reconciler that could fix what it finds would destroy the
 * evidence of how the row got that way.
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
  /** How often. Daily by default: every finding here is a slow drift. */
  schedule?: events.Schedule;
}

export class QuotaReconcilerStack extends cdk.Stack {
  public readonly reconciler: lambda.Function;

  constructor(scope: Construct, id: string, props: QuotaReconcilerStackProps) {
    super(scope, id, props);
    const {
      prefix, lambdaRepository, lambdaImageTag, tenantBudgetsTable, userTenantsTable,
    } = props;

    const metricNamespace = `${prefix}/CreditLedger`;

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
        STRATOCLAVE_METRIC_NAMESPACE: metricNamespace,
      },
      description:
        'Compares each tenant pool row to its sources (memberships, declared seat rate).',
    });
    // READ ONLY, on both tables. The absence of a write grant is what makes
    // "never repairs" a property of the deployment rather than of the code.
    tenantBudgetsTable.grantReadData(this.reconciler);
    userTenantsTable.grantReadData(this.reconciler);

    new events.Rule(this, 'ReconcilerSchedule', {
      ruleName: `${prefix}-quota-reconciler-schedule`,
      schedule: props.schedule ?? events.Schedule.rate(cdk.Duration.days(1)),
      targets: [new targets.LambdaFunction(this.reconciler)],
    });

    const logGroup = logs.LogGroup.fromLogGroupName(
      this, 'ReconcilerLogGroup', `/aws/lambda/${prefix}-quota-reconciler`);

    // (1) A row disagrees with its sources. Per-tenant, so it goes through the
    // shared construct: the metric is undimensioned and the matched line names
    // the tenant, which resolves in one query.
    new TenantAlarm(this, 'PoolCeilingDefect', {
      logGroup,
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
      logGroup,
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

    applyCommonTags(this, prefix, 'quota-reconciler');
  }
}
