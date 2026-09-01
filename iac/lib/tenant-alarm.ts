import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';

/**
 * The one way a human-facing alarm over per-tenant backend logs is built.
 *
 * Two conventions have to hold together, and holding each one separately is what
 * went wrong. An alarm that pages somebody has to say WHICH TENANT, or the first
 * thing the person does is go looking; and a metric may not carry a `tenant_id`
 * DIMENSION, because a filter that runs over every backend log line with a
 * per-tenant dimension is unbounded cardinality (the discipline
 * `iac/lib/vsr-service.ts` states for the VSR exporter, for the same reason).
 *
 * Those two pull in opposite directions and the resolution is always the same:
 * the metric is UNDIMENSIONED and the matched LOG LINE carries `tenant_id`, so
 * the alarm fires on the worst tenant and one query names it. Alarming on the
 * maximum is the right shape anyway -- one saturated tenant is the incident, not
 * a fleet average.
 *
 * The resolution being always the same is exactly why it is a construct rather
 * than a paragraph. It had been applied by hand in one stack and was about to be
 * applied by hand in two more, and a convention applied by hand is a convention
 * that is half-applied by construction. `filterPattern` REQUIRES the log schema
 * to carry `tenant_id`, so a filter that would produce an unattributable alarm
 * cannot be built with this at all.
 */
/**
 * What the signal is about. There is no default: a signal that is per-tenant and
 * cannot name the tenant is the defect this construct exists to prevent, and a
 * signal about the deployment as a whole has no tenant to name. Making the author
 * say which is what keeps the first case from hiding inside the second.
 */
export type AlarmScope = 'tenant' | 'deployment';

export interface TenantAlarmProps {
  /** Log group the backend writes to. */
  readonly logGroup: logs.ILogGroup;
  /**
   * `tenant`  -- the matched line MUST carry `$.tenant_id`, enforced in the
   *              filter pattern, so the alarm is always attributable.
   * `deployment` -- the signal has no tenant (a scheduled job that stopped
   *              running, a check that went missing). Stated explicitly so it
   *              cannot be how a per-tenant signal loses its attribution.
   */
  readonly scope: AlarmScope;
  /** Resource-name prefix for this deployment. */
  readonly prefix: string;
  /** CloudWatch namespace the metric lands in. */
  readonly metricNamespace: string;
  /** Metric name; also the alarm's suffix, so a page names the signal. */
  readonly metricName: string;
  /** Value of `$.event` the backend emits for this signal. */
  readonly event: string;
  /**
   * JSON path of the numeric field to emit, e.g. `$.defects`. Omit for a plain
   * occurrence count, which emits 1 per matching line.
   */
  readonly valueField?: string;
  readonly threshold: number;
  readonly comparisonOperator?: cloudwatch.ComparisonOperator;
  readonly statistic?: string;
  readonly period?: cdk.Duration;
  readonly evaluationPeriods?: number;
  readonly datapointsToAlarm?: number;
  /**
   * Whether an ABSENT metric is a failure. There is no safe default, so it is
   * required: for a scheduled gate, silence means the job stopped and is a
   * failure; for an exception signal, silence means nothing went wrong. Choosing
   * one silently is how an alarm ends up structurally unable to fire.
   */
  readonly treatMissingData: cloudwatch.TreatMissingData;
  /** What an operator should do. Named, not paraphrased from the metric. */
  readonly alarmDescription: string;
}

export class TenantAlarm extends Construct {
  public readonly metricFilter: logs.MetricFilter;
  public readonly alarm: cloudwatch.Alarm;

  constructor(scope: Construct, id: string, props: TenantAlarmProps) {
    super(scope, id);

    // The pattern is the convention. For a tenant-scoped signal `$.tenant_id`
    // must EXIST on the matched line, so a log schema that cannot name the tenant
    // cannot be alarmed on through here -- which is the half-application this
    // construct removes.
    const filterPattern =
      props.scope === 'tenant'
        ? logs.FilterPattern.all(
            logs.FilterPattern.stringValue('$.event', '=', props.event),
            logs.FilterPattern.exists('$.tenant_id'),
          )
        : logs.FilterPattern.stringValue('$.event', '=', props.event);

    this.metricFilter = props.logGroup.addMetricFilter(`${id}MF`, {
      filterName: `${props.prefix}-${props.metricName}`,
      filterPattern,
      metricNamespace: props.metricNamespace,
      metricName: props.metricName,
      metricValue: props.valueField ?? '1',
      // No defaultValue, deliberately: these are gauges, and a zero pushed in
      // from an unrelated log line drags a Maximum down and hides the incident.
    });

    this.alarm = new cloudwatch.Alarm(this, 'Alarm', {
      alarmName: `${props.prefix}-${props.metricName}`,
      alarmDescription: props.alarmDescription,
      metric: this.metricFilter.metric({
        // Maximum, not Average: the alarm is about the worst tenant, and an
        // average over tenants is a number no operator can act on.
        statistic: props.statistic ?? 'Maximum',
        period: props.period ?? cdk.Duration.minutes(5),
      }),
      threshold: props.threshold,
      comparisonOperator:
        props.comparisonOperator ?? cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: props.evaluationPeriods ?? 1,
      datapointsToAlarm: props.datapointsToAlarm ?? 1,
      treatMissingData: props.treatMissingData,
    });
  }
}
