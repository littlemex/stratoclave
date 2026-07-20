import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import { Construct } from 'constructs';
import { applyCommonTags } from './_common';

/**
 * Daily Savings Certificate auto-issuer (litellm wedge slice-4, CDK leg).
 *
 * A scheduled Lambda that certifies the previous settled day (D-N) for every
 * tenant, calling the deterministic + write-once backend
 * (mvp.learning.certificate_scheduler.handler -> certificate_store.issue_for_tenants).
 * The Lambda is the ONLY clock boundary: it derives the day + issue timestamp
 * from the EventBridge event `time`, never a clock call.
 *
 * NOTHING here touches the request/money path. The store is read-mostly (reconcile
 * reads + a write-once conditional Put of a new CERT# item); a failure degrades the
 * audit artifact, never billing. Shadow-stage certificates are an INTERNAL artifact
 * — there is no tenant-facing surface here (per the rollout rule: no external claim
 * before Shadow numbers exist).
 *
 * The three honesty alarms are the point (Fable slice-4 design): a certificate that
 * is silently NOT issued, or a fleet-wide "no traffic" that is really an ingestion
 * outage masquerading as honest absence, must page — an auto-issued audit series
 * with unexplained holes is worse than no series.
 */
export interface CertificateSchedulerStackProps extends cdk.StackProps {
  prefix: string;
  /** ECR repo holding the Lambda image (built from backend/Dockerfile.lambda). */
  lambdaRepository: ecr.IRepository;
  /** Immutable tag of the Lambda image. */
  lambdaImageTag: string;
  /** The routing-signals table (decisions + the CERT# certificate rows live here). */
  routingSignalsTable: dynamodb.ITable;
  /** The tenant-budgets table (reconcile reads billed usage here). */
  tenantBudgetsTable: dynamodb.ITable;
  /** The tenants table (tenant enumeration when CERT_TENANT_IDS is unset). */
  tenantsTable: dynamodb.ITable;
  /**
   * Explicit tenant list to certify (comma-separated). Fable slice-4 (a):
   * coverage is a DECLARED input, not an implicit registry scan. Unset = the
   * Lambda enumerates the tenants table.
   */
  certTenantIds?: string;
  /** Settle window in days: the run certifies event_day - N. Default 2. */
  settleWindowDays?: number;
  /**
   * Expected active-tenant count, for the silent-skip alarm ("issued < expected").
   * When certTenantIds is set this is its length; otherwise an operator estimate.
   */
  expectedTenantCount: number;
  /** Hour (UTC) to run the daily rule. Default 3 (low-traffic). */
  scheduleHourUtc?: number;
}

export class CertificateSchedulerStack extends cdk.Stack {
  public readonly issuer: lambda.Function;

  constructor(scope: Construct, id: string, props: CertificateSchedulerStackProps) {
    super(scope, id, props);
    const { prefix, lambdaRepository, lambdaImageTag } = props;
    const settleWindow = props.settleWindowDays ?? 2;
    const hour = props.scheduleHourUtc ?? 3;

    const env: Record<string, string> = {
      CERT_SETTLE_WINDOW_DAYS: String(settleWindow),
      DYNAMODB_ROUTING_SIGNALS_TABLE: props.routingSignalsTable.tableName,
    };
    if (props.certTenantIds) env.CERT_TENANT_IDS = props.certTenantIds;

    const issuerCode = lambda.DockerImageCode.fromEcr(lambdaRepository, {
      tagOrDigest: lambdaImageTag,
      cmd: ['mvp.learning.certificate_scheduler.handler'],
    });

    // A dedicated log group so the metric filters below read THIS function's EMF
    // lines. retention keeps the audit-issuance history bounded but present.
    const logGroup = new logs.LogGroup(this, 'IssuerLogGroup', {
      logGroupName: `/lambda/${prefix}-certificate-issuer`,
      retention: logs.RetentionDays.THREE_MONTHS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.issuer = new lambda.DockerImageFunction(this, 'Issuer', {
      functionName: `${prefix}-certificate-issuer`,
      code: issuerCode,
      memorySize: 512,
      timeout: cdk.Duration.minutes(10),
      environment: env,
      logGroup,
      description: 'Daily Savings Certificate issuer: write-once certifies day D-N for each tenant.',
    });
    // Least privilege: reconcile reads decisions + billed usage; the write-once
    // Put of the CERT# item needs write on the signals table. Tenants table is
    // read-only (enumeration). No access to any money-mutating table beyond the
    // conditional certificate Put.
    props.routingSignalsTable.grantReadWriteData(this.issuer);
    props.tenantBudgetsTable.grantReadData(this.issuer);
    props.tenantsTable.grantReadData(this.issuer);

    // Daily at `hour`:00 UTC. cron (not rate) so the run time is stable and the
    // event `time` the handler reads lands on a predictable day boundary.
    new events.Rule(this, 'DailySchedule', {
      ruleName: `${prefix}-certificate-issuer-schedule`,
      schedule: events.Schedule.cron({ minute: '0', hour: String(hour) }),
      targets: [new targets.LambdaFunction(this.issuer)],
    });

    // --- metric filters on the batch line the handler emits ---
    // event=certificate_batch_issued carries: issued, expected, failed,
    // skip_no_traffic, no_traffic_fraction (structlog JSON -> $.field).
    const NS = 'Stratoclave/Certificate';
    const mkNumFilter = (field: string, metricName: string) =>
      logGroup.addMetricFilter(`CertMF${metricName}`, {
        filterName: `${prefix}-certificate-${metricName}`,
        filterPattern: logs.FilterPattern.exists(`$.${field}`),
        metricNamespace: NS,
        metricName,
        metricValue: `$.${field}`,
        // no defaultValue: only emit on a real batch line, so "the scheduler didn't
        // run at all" shows as MISSING data (which the alarms treat as breaching).
      });

    const issuedMf = mkNumFilter('issued', 'CertificatesIssued');
    mkNumFilter('failed', 'CertificatesFailed');
    const noTrafficFracMf = mkNumFilter('no_traffic_fraction', 'NoTrafficFraction');

    // (1) per-run failure: any tenant errored in the batch.
    new cloudwatch.Alarm(this, 'CertificatesFailedAlarm', {
      alarmName: `${prefix}-certificate-failed`,
      metric: new cloudwatch.Metric({
        namespace: NS, metricName: 'CertificatesFailed',
        statistic: 'Sum', period: cdk.Duration.days(1),
      }),
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      // absence of the metric is handled by the silent-skip alarm below; here a
      // missing datapoint is not itself a failure.
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription: 'A tenant errored during daily certificate issuance.',
    });

    // (2) silent-skip: issued < expected active tenants (also fires when the
    // scheduler did not run at all — MISSING issued data is BREACHING). This is
    // the "an audit series should not have unexplained holes" guard.
    new cloudwatch.Alarm(this, 'CertificatesIssuedLowAlarm', {
      alarmName: `${prefix}-certificate-issued-below-expected`,
      metric: issuedMf.metric({ statistic: 'Minimum', period: cdk.Duration.days(1) }),
      threshold: props.expectedTenantCount,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
      alarmDescription:
        'Fewer certificates issued than expected active tenants (silent skip), OR ' +
        'the daily issuer stopped running — a hole in the audit series must page.',
    });

    // (3) fleet-wide NO_TRAFFIC (outage vs quiet): a high fraction of tenants all
    // reporting "no VSR-acted traffic" the same day is an ingestion outage, not a
    // fleet of quiet tenants — honest-absence must never mask an outage
    // (Fable slice-4 (d)). Threshold 0.5: over half the fleet silent = suspicious.
    new cloudwatch.Alarm(this, 'FleetNoTrafficAlarm', {
      alarmName: `${prefix}-certificate-fleet-no-traffic`,
      metric: noTrafficFracMf.metric({ statistic: 'Maximum', period: cdk.Duration.days(1) }),
      threshold: 0.5,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription:
        'Over half of tenants reported no VSR-acted traffic on the same day — ' +
        'likely a decision-log ingestion outage masquerading as honest absence.',
    });

    // NOTE (Fable slice-4 (e)-2 follow-up): consecutive-skip-per-tenant (a single
    // tenant stuck skipping for many days) is NOT alarmable from the per-run batch
    // line alone — it needs skip rows persisted (a follow-up recorded in
    // docs/design/vsr-savings-certificate.md). Until then, per-skip_reason metrics
    // above give the operator the fleet-level signal.

    applyCommonTags(this, prefix, 'certificate-scheduler');
  }
}
