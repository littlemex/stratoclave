import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import { Construct } from 'constructs';

/**
 * Telemetry unification — the VIEW layer (Fable design).
 *
 * The two components emit metrics through different internal pipes: the gateway
 * derives CloudWatch metrics from structlog via metric filters (namespace
 * `${prefix}/CreditLedger`); the VSR's Prometheus `/metrics` is scraped by an
 * ADOT sidecar into CloudWatch EMF (namespace `${prefix}/VSR`). This construct
 * unifies them where it matters to an operator — ONE CloudWatch dashboard and a
 * single set of alarms — WITHOUT merging their metric semantics (routing
 * quality stays the VSR's own concern).
 *
 * Dark-ship contract: the VSR widgets and VSR alarms are created ONLY when
 * `vsrMetricNamespace` is provided (i.e. the VSR task — and its ADOT sidecar —
 * exists). With the VSR off there is no namespace, so no VSR widget and NO VSR
 * alarm is synthesized at all: a missing scrape target can never make an alarm
 * flap, because the alarm does not exist. The gateway half is always present.
 */
export interface VsrObservabilityProps {
  prefix: string;
  /**
   * The `${prefix}/VSR` namespace published by the ADOT sidecar, or undefined
   * when the VSR is not deployed. Undefined => gateway-only dashboard, no VSR
   * alarms (the dark-ship no-op).
   */
  vsrMetricNamespace?: string;
  /** VSR Prometheus metrics port, for the telemetry-liveness gap alarm label. */
  vsrMetricsPort?: number;
}

export class VsrObservability extends Construct {
  public readonly dashboard: cloudwatch.Dashboard;
  public readonly vsrAlarms: cloudwatch.Alarm[] = [];

  constructor(scope: Construct, id: string, props: VsrObservabilityProps) {
    super(scope, id);
    const { prefix } = props;
    const ledgerNs = `${prefix}/CreditLedger`;

    this.dashboard = new cloudwatch.Dashboard(this, 'Dashboard', {
      dashboardName: `${prefix}-observability`,
    });

    // --- Gateway half (always present): money-integrity metric-filter metrics.
    this.dashboard.addWidgets(
      new cloudwatch.TextWidget({
        markdown: `# ${prefix} — unified observability\nGateway (credit ledger) + external VSR, one pane.`,
        width: 24,
        height: 2,
      }),
    );
    this.dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Credit-ledger drift (money integrity)',
        left: ['LedgerDriftSettled', 'LedgerDriftReserved', 'LedgerDriftReclaimed'].map(
          (m) =>
            new cloudwatch.Metric({
              namespace: ledgerNs,
              metricName: m,
              statistic: 'Sum',
              period: cdk.Duration.minutes(5),
            }),
        ),
        width: 12,
        height: 6,
      }),
    );

    // --- VSR half (ONLY when the VSR — hence its ADOT sidecar — is deployed).
    const ns = props.vsrMetricNamespace;
    if (!ns) {
      return; // dark: no VSR namespace => no VSR widgets, no VSR alarms.
    }

    const vsrMetric = (name: string, statistic = 'Sum') =>
      new cloudwatch.Metric({
        namespace: ns,
        metricName: name,
        statistic,
        period: cdk.Duration.minutes(5),
      });

    this.dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'VSR requests vs errors',
        left: [vsrMetric('llm_model_requests_total'), vsrMetric('llm_request_errors_total')],
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'VSR in-flight (saturation)',
        left: [vsrMetric('llm_model_inflight_requests', 'Maximum')],
        width: 12,
        height: 6,
      }),
      new cloudwatch.GraphWidget({
        title: 'VSR prompt-cache hits vs misses',
        left: [
          vsrMetric('llm_cache_plugin_hits_total'),
          vsrMetric('llm_cache_plugin_misses_total'),
        ],
        width: 12,
        height: 6,
      }),
    );

    // Alarm 1 — VSR error rate: errors present at all is worth surfacing; use a
    // multi-datapoint window so a single transient error does not page.
    this.vsrAlarms.push(
      new cloudwatch.Alarm(this, 'VsrErrors', {
        alarmName: `${prefix}-vsr-errors`,
        alarmDescription:
          'External VSR is returning request errors (llm_request_errors_total > 0 sustained).',
        metric: vsrMetric('llm_request_errors_total').with({ period: cdk.Duration.minutes(5) }),
        threshold: 0,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluationPeriods: 3,
        datapointsToAlarm: 3,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      }),
    );

    // Alarm 2 — telemetry liveness: if the sidecar dies (non-essential) the VSR
    // keeps serving but its metrics go silent. A deployed VSR should always be
    // emitting request samples; their ABSENCE is the signal. Only meaningful
    // when the VSR exists — which is exactly when this alarm is created.
    this.vsrAlarms.push(
      new cloudwatch.Alarm(this, 'VsrTelemetryGap', {
        alarmName: `${prefix}-vsr-telemetry-gap`,
        alarmDescription:
          'No VSR metric samples received (ADOT sidecar down or VSR not scraping) while the VSR is deployed.',
        metric: vsrMetric('llm_model_requests_total').with({
          statistic: 'SampleCount',
          period: cdk.Duration.minutes(5),
        }),
        threshold: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        evaluationPeriods: 3,
        datapointsToAlarm: 3,
        // Missing data IS the breach here (silent telemetry loss).
        treatMissingData: cloudwatch.TreatMissingData.BREACHING,
      }),
    );

    // Alarm 3 — cache-hit-rate collapse: a sustained low hit rate means the
    // semantic cache stopped helping (cost/latency regression). Expressed as a
    // math ratio hits/(hits+misses) so it is dimensionless.
    const hits = vsrMetric('llm_cache_plugin_hits_total');
    const misses = vsrMetric('llm_cache_plugin_misses_total');
    const hitRate = new cloudwatch.MathExpression({
      expression: '100 * hits / (hits + misses + 1)',
      usingMetrics: { hits, misses },
      period: cdk.Duration.minutes(5),
      label: 'VSR cache hit rate %',
    });
    this.vsrAlarms.push(
      new cloudwatch.Alarm(this, 'VsrCacheHitRateLow', {
        alarmName: `${prefix}-vsr-cache-hit-rate-low`,
        alarmDescription:
          'VSR prompt-cache hit rate collapsed (< 20% sustained) — cost/latency regression.',
        metric: hitRate,
        threshold: 20,
        comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        evaluationPeriods: 6,
        datapointsToAlarm: 6,
        // No traffic => no cache activity => not a breach (avoid idle flapping).
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      }),
    );
  }
}
