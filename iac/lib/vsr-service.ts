import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

/**
 * External VSR (Value/Session Router) service — an OPTIONAL, INTERNAL-ONLY
 * Fargate task that Stratoclave may consult for a routing suggestion.
 *
 * Task #13: the VSR is an EXTERNAL tool, so its version MUST be pinned. This
 * construct enforces that at SYNTH time: the image must be referenced by an
 * ECR digest (`sha256:<64hex>`) or an exact semver tag. `latest` and every
 * floating/mutable tag are REJECTED — a drifting VSR image can never be
 * deployed through this construct.
 *
 * Security posture (matches the design):
 *  - No public ingress. `assignPublicIp:false`, private-with-egress subnets,
 *    and a security group that only accepts the backend SG on the app port.
 *  - The backend reaches it by an internal address; the URL never appears in
 *    the model registry or any client-controlled surface (SSRF guard lives in
 *    the backend's endpoint allowlist).
 */

const SEMVER = /^\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?$/;
const DIGEST = /^sha256:[0-9a-f]{64}$/;

export interface VsrServiceProps {
  cluster: ecs.ICluster;
  vpc: ec2.IVpc;
  /** The backend service SG — the ONLY source allowed to reach the VSR. */
  backendSecurityGroup: ec2.ISecurityGroup;
  vsrRepository: ecr.IRepository;
  /**
   * The pinned image reference: either `sha256:<64hex>` (preferred) or an
   * exact semver tag (e.g. `1.4.2`). `latest`/floating tags throw at synth.
   */
  imagePin: string;
  /** The pinned wire contract advertised on /version, e.g. `vsr/1`. */
  contractVersion: string;
  /** Container app port. @default 8000 */
  port?: number;
  cpu?: number;
  memoryLimitMiB?: number;
  /**
   * The Prometheus metrics port the VSR exposes (`/metrics`). @default 9190
   * (the upstream vLLM Semantic Router default).
   */
  metricsPort?: number;
  /**
   * Deployment prefix, used to name the CloudWatch metric namespace the ADOT
   * sidecar publishes VSR metrics under (`${prefix}/VSR`). When omitted the
   * sidecar is NOT added — telemetry unification is opt-in and this keeps the
   * construct usable in isolation (e.g. unit tests) without a collector.
   */
  metricsPrefix?: string;
  /**
   * Pinned ADOT collector image reference (digest preferred). Required to add
   * the metrics sidecar; when absent the sidecar is skipped. Kept explicit (no
   * floating default) so the collector version is as reproducible as the VSR.
   */
  adotImage?: string;
}

/**
 * Validate a VSR image pin. Exported so a unit test can assert the guard
 * directly. Returns `{ kind: 'digest' | 'tag', value }` or throws.
 */
export function assertPinnedImage(imagePin: string): {
  kind: 'digest' | 'tag';
  value: string;
} {
  const v = (imagePin || '').trim();
  if (DIGEST.test(v)) {
    return { kind: 'digest', value: v };
  }
  if (v === 'latest' || !SEMVER.test(v)) {
    throw new Error(
      `VSR image must be pinned by digest (sha256:<64hex>) or an exact semver ` +
        `tag; got '${imagePin}'. Floating tags (incl. 'latest') are forbidden ` +
        `because an external VSR must be version-pinned (task #13).`,
    );
  }
  return { kind: 'tag', value: v };
}

// The ONLY VSR metrics we lift into CloudWatch — an explicit allow-list so a
// new upstream metric can never silently multiply the CloudWatch custom-metric
// bill. Histograms are reduced to their _sum/_count (a mean), never per-bucket
// series (each bucket would be a billed custom metric). No per-model/category
// or tenant dimensions reach CloudWatch — those stay queryable in the VSR's own
// Prometheus/Grafana stack and in Logs Insights respectively.
export const VSR_METRIC_ALLOWLIST: readonly string[] = [
  'llm_model_requests_total',
  'llm_request_errors_total',
  'llm_model_inflight_requests',
  'llm_cache_plugin_hits_total',
  'llm_cache_plugin_misses_total',
  // Histograms — kept as _sum/_count only (see include_metrics below):
  'llm_model_routing_latency_seconds',
  'llm_model_completion_latency_seconds',
  'llm_model_ttft_seconds',
  'llm_model_tpot_seconds',
];

/**
 * Build the ADOT collector config that scrapes the VSR's local `/metrics` and
 * republishes a bounded, aggregated slice to CloudWatch EMF under
 * `${namespace}`. PURE + exported so a unit test can assert the cardinality
 * guards (allow-list, dropped labels, histogram reduction) without a synth.
 *
 * Cardinality discipline (Fable design (d)#1/#2):
 *  - `metric_relabel_configs` keeps ONLY the allow-listed series;
 *  - the `model`/`category` labels and any high-cardinality label are dropped,
 *    so a CloudWatch custom metric is billed per metric name, not per
 *    model×category combination;
 *  - the EMF exporter declares NO metric_declaration dimensions, so nothing is
 *    faceted (a single series per metric name).
 */
export function buildAdotConfig(opts: {
  namespace: string;
  metricsPort: number;
  scrapeIntervalSeconds?: number;
}): string {
  const interval = opts.scrapeIntervalSeconds ?? 60;
  const keepRe = VSR_METRIC_ALLOWLIST.map((m) =>
    // A histogram foo becomes foo_sum/foo_count/foo_bucket at scrape time; keep
    // only _sum/_count for histogram-typed names, the bare name for counters/
    // gauges. Matching name(_sum|_count)? and explicitly NOT _bucket does both.
    `${m}(_sum|_count)?`,
  ).join('|');
  const cfg = {
    receivers: {
      prometheus: {
        config: {
          scrape_configs: [
            {
              job_name: 'vsr',
              scrape_interval: `${interval}s`,
              static_configs: [{ targets: [`localhost:${opts.metricsPort}`] }],
              metric_relabel_configs: [
                // Drop per-bucket histogram series outright (cost explosion).
                { source_labels: ['__name__'], regex: '.*_bucket', action: 'drop' },
                // Keep only the allow-listed names (+ _sum/_count for histos).
                { source_labels: ['__name__'], regex: `^(${keepRe})$`, action: 'keep' },
                // Strip high-cardinality labels before they reach CloudWatch.
                { regex: 'model|category|le|tenant|tenant_id|session|instance', action: 'labeldrop' },
              ],
            },
          ],
        },
      },
    },
    processors: {
      // Bound memory so the sidecar can never OOM-kill the task's shared limit.
      memory_limiter: { check_interval: '5s', limit_percentage: 80, spike_limit_percentage: 25 },
      batch: { timeout: '30s' },
    },
    exporters: {
      // EMF writes structured logs that CloudWatch turns into metrics — no
      // PutMetricData throttling, and only a log-write IAM grant is needed.
      awsemf: {
        namespace: opts.namespace,
        // No dimensions => one series per metric name (no faceting/cardinality).
        dimension_rollup_option: '0AsDimensions',
        resource_to_telemetry_conversion: { enabled: false },
      },
    },
    service: {
      pipelines: {
        metrics: {
          receivers: ['prometheus'],
          processors: ['memory_limiter', 'batch'],
          exporters: ['awsemf'],
        },
      },
      // No traces pipeline: VSR's OTel tracing is explicitly out of scope here
      // (Fable (d)#7) — enabling it would introduce a separate exporter target.
      telemetry: { logs: { level: 'warn' } },
    },
  };
  return JSON.stringify(cfg);
}

export class VsrService extends Construct {
  public readonly service: ecs.FargateService;
  public readonly securityGroup: ec2.SecurityGroup;
  /** The CloudWatch namespace VSR metrics are published under, when the ADOT
   * sidecar is enabled (else undefined). The dashboard/alarms read this. */
  public readonly metricNamespace?: string;

  constructor(scope: Construct, id: string, props: VsrServiceProps) {
    super(scope, id);

    const port = props.port ?? 8000;
    const pin = assertPinnedImage(props.imagePin);

    // Internal-only SG: accept ONLY the backend SG on the app port; no other
    // ingress, egress restricted to what the task needs to pull logs/metrics.
    this.securityGroup = new ec2.SecurityGroup(this, 'VsrSg', {
      vpc: props.vpc,
      description: 'External VSR service — backend ingress only, no public access',
      allowAllOutbound: true,
    });
    this.securityGroup.addIngressRule(
      props.backendSecurityGroup,
      ec2.Port.tcp(port),
      'backend -> vsr only',
    );

    const taskDef = new ecs.FargateTaskDefinition(this, 'VsrTask', {
      cpu: props.cpu ?? 512,
      memoryLimitMiB: props.memoryLimitMiB ?? 1024,
    });

    // Content-addressed reference for a digest pin; exact-tag otherwise. Both
    // resolve through the ECR repo (grantPull wired by fromEcrRepository).
    const image =
      pin.kind === 'digest'
        ? ecs.ContainerImage.fromEcrRepository(props.vsrRepository, pin.value)
        : ecs.ContainerImage.fromEcrRepository(props.vsrRepository, pin.value);

    const metricsPort = props.metricsPort ?? 9190;
    taskDef.addContainer('vsr', {
      image,
      // The app port is reachable from the backend SG; the metrics port is
      // NOT added to the SG — it is scraped only over localhost by the ADOT
      // sidecar in THIS task (awsvpc => containers share the ENI/loopback).
      portMappings: [{ containerPort: port }],
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'vsr' }),
      environment: {
        VSR_CONTRACT_VERSION: props.contractVersion,
        VSR_PORT: String(port),
        VSR_METRICS_PORT: String(metricsPort),
      },
    });

    // Telemetry unification (Fable design): an ADOT collector sidecar co-located
    // IN the VSR task scrapes localhost:<metricsPort>/metrics and republishes a
    // bounded slice to CloudWatch EMF under `${prefix}/VSR`, so VSR metrics land
    // in the SAME CloudWatch backend as the gateway's metric-filter metrics —
    // one pane, no second console. Because the sidecar lives INSIDE this task,
    // "VSR off" (the whole task is not created) removes the scraper and its
    // target together: nothing to disable, no dangling scrape config, no
    // missing-data alarm to flap. The sidecar is NON-essential so a collector
    // OOM cannot take the VSR itself down.
    if (props.metricsPrefix && props.adotImage) {
      this.metricNamespace = `${props.metricsPrefix}/VSR`;
      const adotPin = assertPinnedImage(props.adotImage);
      taskDef.addContainer('adot', {
        image: ecs.ContainerImage.fromRegistry(
          adotPin.kind === 'digest'
            ? `public.ecr.aws/aws-observability/aws-otel-collector@${adotPin.value}`
            : `public.ecr.aws/aws-observability/aws-otel-collector:${adotPin.value}`,
        ),
        essential: false,
        cpu: 64,
        memoryLimitMiB: 128,
        logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'vsr-adot' }),
        environment: {
          AOT_CONFIG_CONTENT: buildAdotConfig({
            namespace: this.metricNamespace,
            metricsPort,
          }),
        },
      });
      // EMF writes metrics as structured logs — grant only log creation/write,
      // NOT cloudwatch:PutMetricData (no direct metric API, no throttling).
      taskDef.taskRole.addToPrincipalPolicy(
        new iam.PolicyStatement({
          sid: 'VsrAdotEmfLogs',
          effect: iam.Effect.ALLOW,
          actions: [
            'logs:CreateLogGroup',
            'logs:CreateLogStream',
            'logs:PutLogEvents',
            'logs:DescribeLogStreams',
            'logs:DescribeLogGroups',
          ],
          resources: ['*'],
        }),
      );
    }

    this.service = new ecs.FargateService(this, 'VsrService', {
      cluster: props.cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      securityGroups: [this.securityGroup],
      assignPublicIp: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    });

    new cdk.CfnOutput(this, 'VsrImagePinKind', { value: pin.kind });
  }
}
