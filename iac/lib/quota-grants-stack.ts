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
 * The sweep that ends granted capacity when its window closes.
 *
 * A grant raises a tenant's money ceiling for a bounded time, and the bound is
 * only real if something enforces it. Nothing else can: the request path reads a
 * ceiling, it does not audit how the ceiling got there, so a grant with nobody to
 * revoke it is a permanent raise recorded as a temporary one.
 *
 * Its own stack, on the convention every other scheduled job in this repository
 * already follows (`quota-reconciler-stack.ts`, `ledger-projector-stack.ts`,
 * `certificate-scheduler-stack.ts`). Putting the schedule and its alarms on the
 * service stack would make this the exception, and then a conventional job would
 * read as the deviation.
 *
 * IT WRITES, AND ITS GRANT SAYS SO. Unlike the reconciler beside it, this job
 * moves money: it subtracts a grant's amount from three attributes on the pool row
 * and takes the grant terminal, in one transaction. So it holds read-write on both
 * tables and is a SEPARATE function from anything read-only — the reconciler's
 * "reports and never repairs" posture is enforced by the absence of a write grant,
 * and colocating the two would hand it one.
 *
 * WHY THE ABSENCE ALARM IS THE IMPORTANT ONE. Every other alarm here fires on
 * something going wrong. This job's characteristic failure is that it stops
 * running, and a stopped sweeper looks exactly like a quiet one: no error, no
 * revocation, and every live grant silently becoming permanent. So `SweeperRan`
 * treats missing data as BREACHING, and the backend emits it on every run
 * including empty ones — a heartbeat that only fires when there was work cannot
 * tell "nothing expired" from "nobody is looking".
 */
export interface QuotaGrantsStackProps extends cdk.StackProps {
  prefix: string;
  /** ECR repo holding the Lambda image (built from backend/Dockerfile.lambda). */
  lambdaRepository: ecr.IRepository;
  /** Immutable tag of the Lambda image. */
  lambdaImageTag: string;
  /** The grant records this sweeps, and their expiry index. */
  quotaEventsTable: dynamodb.ITable;
  /** The pool rows a revocation moves. */
  tenantBudgetsTable: dynamodb.ITable;
  /**
   * How often. Five minutes by default: the interval bounds how long a grant can
   * outlive its own expiry, and that lateness is the figure
   * `GrantRevocationLateSeconds` reports.
   */
  schedule?: events.Schedule;
}

export class QuotaGrantsStack extends cdk.Stack {
  public readonly sweeper: lambda.Function;

  constructor(scope: Construct, id: string, props: QuotaGrantsStackProps) {
    super(scope, id, props);
    const {
      prefix, lambdaRepository, lambdaImageTag, quotaEventsTable, tenantBudgetsTable,
    } = props;

    const metricNamespace = `${prefix}/Grants`;

    this.sweeper = new lambda.DockerImageFunction(this, 'GrantSweeper', {
      functionName: `${prefix}-quota-grant-sweeper`,
      code: lambda.DockerImageCode.fromEcr(lambdaRepository, {
        tagOrDigest: lambdaImageTag,
        cmd: ['mvp.grants.sweep_handler'],
      }),
      memorySize: 512,
      // Bounded by the number of grants past expiry, one transaction each, and it
      // paginates fully. A timeout that cut the pass short would leave grants
      // unrevoked AND — because the heartbeat is emitted only after pagination
      // completes — would correctly fail to claim the run happened.
      timeout: cdk.Duration.minutes(5),
      environment: {
        DYNAMODB_QUOTA_EVENTS_TABLE: quotaEventsTable.tableName,
        DYNAMODB_TENANT_BUDGETS_TABLE: tenantBudgetsTable.tableName,
        STRATOCLAVE_METRIC_NAMESPACE: metricNamespace,
      },
      description:
        "Revokes grants whose window has closed, returning their capacity to the tenant's pool.",
    });
    // Read-WRITE on both, and the grant is the honest description of the work: the
    // sweep is the one scheduled job in this feature that moves money.
    quotaEventsTable.grantReadWriteData(this.sweeper);
    tenantBudgetsTable.grantReadWriteData(this.sweeper);

    new events.Rule(this, 'GrantSweepSchedule', {
      ruleName: `${prefix}-quota-grant-sweep-schedule`,
      schedule: props.schedule ?? events.Schedule.rate(cdk.Duration.minutes(5)),
      targets: [new targets.LambdaFunction(this.sweeper)],
    });

    const logGroup = logs.LogGroup.fromLogGroupName(
      this, 'GrantSweeperLogGroup', `/aws/lambda/${prefix}-quota-grant-sweeper`);

    // (1) The sweeper stopped. Deployment-scoped: a run names no single tenant, and
    // saying so explicitly is what stops a per-tenant signal losing its attribution
    // by defaulting into this shape.
    //
    // MISSING DATA IS BREACHING, and this is the alarm the whole expiry mechanism
    // rests on. Silence means no sweep ran, and no sweep means every live grant is
    // becoming a permanent raise — so a quiet metric here must never read as a
    // healthy one. The window is generous relative to the 5-minute schedule so a
    // single cold start or throttle does not page anybody.
    new TenantAlarm(this, 'GrantSweeperAbsent', {
      logGroup,
      prefix,
      scope: 'deployment',
      metricNamespace,
      metricName: 'SweeperRan',
      event: 'sweeper_ran',
      valueField: '$.sweeper_ran',
      // Fires when the count drops to zero or no datapoint arrives at all.
      threshold: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      statistic: 'Sum',
      period: cdk.Duration.minutes(30),
      evaluationPeriods: 1,
      datapointsToAlarm: 1,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
      alarmDescription:
        'The grant sweeper has not reported a completed run. Every limit raise is time-bounded ONLY because this job revokes it, so while this is silent each live grant is quietly becoming a permanent ceiling increase. The heartbeat is emitted after pagination completes, so a run that died part-way through does not satisfy this alarm — which is the point. Check the sweeper function, then run it manually and confirm grants_revoked accounts for everything past expiry.',
    });

    // (2) A grant that cannot be revoked. Per-tenant, so it goes through the shared
    // construct: the metric stays a single undimensioned series and the matched log
    // line carries `tenant_id`, which resolves in one Logs Insights query.
    //
    // Dimensioning this metric was the first answer and it was wrong. A CloudWatch
    // custom metric is billed per metric name, high-cardinality labels are dropped,
    // and `iac/lib/vsr-service.ts` records that this deployment declares no
    // dimensions so nothing is faceted. A per-tenant dimension is exactly the facet
    // that discipline exists to refuse — so the requirement's intent, that a page
    // can say whose tenant it is about, is met on the LINE instead.
    new TenantAlarm(this, 'RevokeBlockedGrants', {
      logGroup,
      prefix,
      scope: 'tenant',
      metricNamespace,
      metricName: 'RevokeBlockedGrants',
      event: 'revoke_blocked_grants',
      valueField: '$.revoke_blocked_grants',
      threshold: 0,
      period: cdk.Duration.minutes(5),
      evaluationPeriods: 1,
      datapointsToAlarm: 1,
      // A clean run emits no blocked line, and that really is nothing to report.
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription:
        "A grant's capacity could not be returned to its tenant's pool after bounded retries, so it is marked REVOKE_BLOCKED and has left the expiry index rather than consuming every run forever. The pool still counts its amount, which is honest — the capacity was never given back — and it also consumes that tenant's aggregate grant cap until it is repaired. RUNBOOK: find the tenant and grant_id in the revoke_blocked_grants log line, read the grant's revoke_blocked_reason, fix the underlying fault, then POST /api/mvp/admin/limit-grants/{grant_id}/revoke?tenant_id=<t>, which clears the block and retries the same transaction.",
    });

    // (3) Revocation running late. Per-tenant, and a NOTICE rather than an
    // incident: the schedule bounds this, so a figure a little above one interval
    // is a slow run and a figure far above it means passes are being missed
    // silently — which the absence alarm cannot see if runs are completing.
    new TenantAlarm(this, 'GrantRevocationLate', {
      logGroup,
      prefix,
      scope: 'deployment',
      metricNamespace,
      metricName: 'GrantRevocationLateSeconds',
      event: 'sweeper_ran',
      valueField: '$.grant_revocation_late_seconds',
      // Three schedule intervals. One interval of lateness is the mechanism working
      // as designed, so alarming there would make this fire on every expiry.
      threshold: 15 * 60,
      period: cdk.Duration.minutes(15),
      evaluationPeriods: 1,
      datapointsToAlarm: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription:
        'A grant was revoked well after its expiry, so a tenant held raised capacity longer than the grant said it would. One sweep interval of lateness is the design; several means sweeps are being skipped or a page is failing repeatedly. This figure is measured only over grants actually revoked — a REVOKE_BLOCKED grant has its own alarm and is deliberately excluded, so one stuck grant cannot make the whole fleet look late.',
    });

    applyCommonTags(this, prefix, 'quota-grants');
  }
}
