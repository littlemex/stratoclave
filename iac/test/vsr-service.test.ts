import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import {
  VsrService,
  assertPinnedImage,
  buildAdotConfig,
  VSR_METRIC_ALLOWLIST,
} from '../lib/vsr-service';

// Task #13: an external VSR must be version-pinned. The synth-time guard is the
// enforcement point — a floating tag can never reach a deployable task def.

describe('assertPinnedImage', () => {
  test('accepts an ECR digest', () => {
    const d = 'sha256:' + 'a'.repeat(64);
    expect(assertPinnedImage(d)).toEqual({ kind: 'digest', value: d });
  });

  test('accepts an exact semver tag', () => {
    expect(assertPinnedImage('1.4.2')).toEqual({ kind: 'tag', value: '1.4.2' });
    expect(assertPinnedImage('2.0.0-rc.1')).toEqual({
      kind: 'tag',
      value: '2.0.0-rc.1',
    });
  });

  test.each(['latest', '', 'stable', 'v1', '1.4', '1.x', 'main', 'sha256:short'])(
    'rejects floating/invalid pin %p',
    (bad) => {
      expect(() => assertPinnedImage(bad)).toThrow(/pinned by digest|forbidden/i);
    },
  );
});

describe('VsrService', () => {
  function synth(imagePin: string, extra: Partial<{ metricsPrefix: string; adotImage: string }> = {}) {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'TestVsrStack', {
      env: { account: '123456789012', region: 'us-west-2' },
    });
    const vpc = new ec2.Vpc(stack, 'Vpc', { maxAzs: 2 });
    const cluster = new ecs.Cluster(stack, 'Cluster', { vpc });
    const backendSg = new ec2.SecurityGroup(stack, 'BackendSg', { vpc });
    const repo = new ecr.Repository(stack, 'VsrRepo');
    new VsrService(stack, 'Vsr', {
      cluster,
      vpc,
      backendSecurityGroup: backendSg,
      vsrRepository: repo,
      imagePin,
      contractVersion: 'vsr/1',
      ...extra,
    });
    return Template.fromStack(stack);
  }

  test('synth throws for a floating tag', () => {
    expect(() => synth('latest')).toThrow(/forbidden/i);
  });

  test('a pinned tag synthesizes an internal-only Fargate service', () => {
    const t = synth('1.4.2');
    // A Fargate service with NO public IP.
    t.hasResourceProperties('AWS::ECS::Service', {
      LaunchType: 'FARGATE',
      NetworkConfiguration: {
        AwsvpcConfiguration: {
          AssignPublicIp: 'DISABLED',
        },
      },
    });
  });

  test('VSR SG only ingresses from the backend SG', () => {
    const t = synth('1.4.2');
    // The ingress rule references the backend SG as source (not 0.0.0.0/0).
    t.hasResourceProperties('AWS::EC2::SecurityGroupIngress', {
      FromPort: 8000,
      ToPort: 8000,
    });
    // No public CIDR ingress on the VSR SG.
    const ingresses = t.findResources('AWS::EC2::SecurityGroupIngress');
    for (const r of Object.values(ingresses)) {
      const props = (r as any).Properties || {};
      expect(props.CidrIp).not.toBe('0.0.0.0/0');
    }
  });

  // ---- ADOT metrics sidecar (telemetry unification) ----

  test('no ADOT sidecar without metricsPrefix+adotImage (dark by default)', () => {
    const t = synth('1.4.2'); // no metrics opts
    const tds = t.findResources('AWS::ECS::TaskDefinition');
    const td = Object.values(tds)[0] as any;
    const names = td.Properties.ContainerDefinitions.map((c: any) => c.Name);
    expect(names).toContain('vsr');
    expect(names).not.toContain('adot');
    // No metrics port ingress ever opened on the SG (localhost scrape only).
    const ingresses = t.findResources('AWS::EC2::SecurityGroupIngress');
    for (const r of Object.values(ingresses)) {
      expect((r as any).Properties.FromPort).not.toBe(9190);
    }
  });

  test('ADOT sidecar is added (non-essential) when metrics opts are provided', () => {
    const t = synth('1.4.2', { metricsPrefix: 'stratoclave', adotImage: '0.43.0' });
    const tds = t.findResources('AWS::ECS::TaskDefinition');
    const td = Object.values(tds)[0] as any;
    const adot = td.Properties.ContainerDefinitions.find((c: any) => c.Name === 'adot');
    expect(adot).toBeDefined();
    // Non-essential: a collector OOM must not take the VSR container down.
    expect(adot.Essential).toBe(false);
    // The collector config is injected and targets the namespace + local port.
    const env = Object.fromEntries(adot.Environment.map((e: any) => [e.Name, e.Value]));
    expect(env.AOT_CONFIG_CONTENT).toContain('stratoclave/VSR');
    expect(env.AOT_CONFIG_CONTENT).toContain('localhost:9190');
    // Still no metrics-port ingress on the SG (scraped over loopback).
    const ingresses = t.findResources('AWS::EC2::SecurityGroupIngress');
    for (const r of Object.values(ingresses)) {
      expect((r as any).Properties.FromPort).not.toBe(9190);
    }
  });

  test('ADOT image pin is version-enforced like the VSR image', () => {
    expect(() => synth('1.4.2', { metricsPrefix: 'p', adotImage: 'latest' })).toThrow(
      /forbidden/i,
    );
  });
});

describe('buildAdotConfig', () => {
  const cfg = () =>
    JSON.parse(buildAdotConfig({ namespace: 'p/VSR', metricsPort: 9190 }));

  test('scrapes the local metrics port and exports EMF to the namespace', () => {
    const c = cfg();
    const sc = c.receivers.prometheus.config.scrape_configs[0];
    expect(sc.static_configs[0].targets).toEqual(['localhost:9190']);
    expect(c.exporters.awsemf.namespace).toBe('p/VSR');
  });

  test('drops histogram buckets and keeps only the allow-listed series', () => {
    const relabel =
      cfg().receivers.prometheus.config.scrape_configs[0].metric_relabel_configs;
    // A rule explicitly drops *_bucket (per-bucket cost explosion).
    expect(relabel.some((r: any) => r.regex === '.*_bucket' && r.action === 'drop')).toBe(true);
    // The keep rule mentions every allow-listed metric name.
    const keep = relabel.find((r: any) => r.action === 'keep');
    for (const m of VSR_METRIC_ALLOWLIST) {
      expect(keep.regex).toContain(m);
    }
  });

  test('drops high-cardinality labels (model/category/tenant) before CloudWatch', () => {
    const relabel =
      cfg().receivers.prometheus.config.scrape_configs[0].metric_relabel_configs;
    const drop = relabel.find((r: any) => r.action === 'labeldrop');
    expect(drop.regex).toMatch(/model/);
    expect(drop.regex).toMatch(/category/);
    expect(drop.regex).toMatch(/tenant/);
  });

  test('has a memory_limiter so the sidecar cannot OOM the task', () => {
    expect(cfg().processors.memory_limiter).toBeDefined();
  });

  test('declares NO traces pipeline (tracing explicitly out of scope)', () => {
    expect(cfg().service.pipelines.traces).toBeUndefined();
    expect(cfg().service.pipelines.metrics).toBeDefined();
  });
});
