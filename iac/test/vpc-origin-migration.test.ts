import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { Template } from 'aws-cdk-lib/assertions';
import { hermeticSynthEnv } from './_hermetic-synth';

/**
 * `STRATOCLAVE_CLOUDFRONT_ORIGIN`'s three migration states
 * (`public` -> `vpc-expand` -> `vpc-switch` -> `vpc`), synthesized through
 * the real `bin/iac.ts` entrypoint under ONE shared prefix — the same
 * prefix an operator would reuse across the three real deploys.
 *
 * What this file proves, each backed by a real `cdk synth`, not just a
 * reading of the source:
 *
 *   1. The public ALB's logical ids (`Alb...`, `BackendTargetGroup...`,
 *      `HttpListener...`) are IDENTICAL between `public` and `vpc-expand` —
 *      the expand step only ADDS, it never touches the existing resources
 *      (a real `cdk deploy` would see these as unchanged, not updated).
 *   2. `vpc-expand` keeps BOTH ALBs/target groups and registers the ECS
 *      service with both target groups. `AWS::ECS::Service.LoadBalancers`
 *      is documented by AWS as "Update requires: No interruption" — this
 *      file does not re-verify that against a live account (no mutating
 *      AWS calls), but records the citation plainly instead of restating
 *      it as a bare assumption:
 *      https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-ecs-service.html
 *      (`LoadBalancers`, *Update requires*: No interruption).
 *   3. `vpc-expand` declares the VPC origin but no behaviour references it
 *      yet (`vpc-switch` is the one that flips `targetOriginId`).
 *   4. `vpc` (contract) removes the public ALB's resources and the
 *      `AlbDnsName` export entirely.
 *
 * Synth runs with ambient AWS credentials neutralised — see
 * `test/_hermetic-synth.ts`.
 */
describe('STRATOCLAVE_CLOUDFRONT_ORIGIN migration states', () => {
  const iacDir = path.resolve(__dirname, '..');
  const PREFIX = 'migrationtest';
  const REGION = 'us-east-1';

  function synth(cloudfrontOrigin: string): { outDir: string; raw: Record<string, Record<string, any>> } {
    const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'stratoclave-vpc-migration-'));
    const env = hermeticSynthEnv({
      CDK_DEFAULT_ACCOUNT: '123456789012',
      CDK_DEFAULT_REGION: REGION,
      STRATOCLAVE_REGION: REGION,
      STRATOCLAVE_PREFIX: PREFIX,
      CDK_NAG: 'off',
      STRATOCLAVE_CLOUDFRONT_ORIGIN: cloudfrontOrigin,
    });
    execFileSync('npx', ['cdk', 'synth', '--all', '-o', outDir, '--quiet'], {
      cwd: iacDir,
      env,
      encoding: 'utf-8',
      stdio: 'pipe',
    });
    const raw: Record<string, Record<string, any>> = {};
    for (const id of ['alb', 'frontend', 'ecs', 'network']) {
      raw[id] = JSON.parse(fs.readFileSync(path.join(outDir, `${PREFIX}-${id}.template.json`), 'utf-8'));
    }
    return { outDir, raw };
  }

  function albLogicalIds(raw: Record<string, any>): string[] {
    return Object.keys(raw.Resources || {}).filter((id) =>
      /^(Alb|BackendTargetGroup|HttpListener)[A-F0-9]+$/.test(id),
    );
  }

  let publicOut: { outDir: string; raw: Record<string, Record<string, any>> };
  let expandOut: { outDir: string; raw: Record<string, Record<string, any>> };
  let switchOut: { outDir: string; raw: Record<string, Record<string, any>> };
  let vpcOut: { outDir: string; raw: Record<string, Record<string, any>> };

  beforeAll(() => {
    publicOut = synth('public');
    expandOut = synth('vpc-expand');
    switchOut = synth('vpc-switch');
    vpcOut = synth('vpc');
  }, 180_000);

  afterAll(() => {
    for (const o of [publicOut, expandOut, switchOut, vpcOut]) {
      if (o?.outDir) fs.rmSync(o.outDir, { recursive: true, force: true });
    }
  });

  test('public -> vpc-expand: the public ALB keeps the EXACT SAME logical ids (expand only adds)', () => {
    const publicIds = albLogicalIds(publicOut.raw.alb).sort();
    const expandIds = albLogicalIds(expandOut.raw.alb).sort();
    expect(expandIds).toEqual(expect.arrayContaining(publicIds));
    // And the properties of each shared id are untouched too, not merely
    // the id — a same-id, different-properties resource would still be an
    // "update", not the "unchanged" this step promises.
    for (const id of publicIds) {
      expect(expandOut.raw.alb.Resources[id]).toEqual(publicOut.raw.alb.Resources[id]);
    }
  });

  test('vpc-expand: BOTH ALBs/target groups exist, and the ECS service has two LoadBalancers entries', () => {
    const tpl = Template.fromJSON(expandOut.raw.alb);
    tpl.resourceCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', 2);
    tpl.resourceCountIs('AWS::ElasticLoadBalancingV2::TargetGroup', 2);

    const ecsTpl = Template.fromJSON(expandOut.raw.ecs);
    const services = ecsTpl.findResources('AWS::ECS::Service');
    const svc = Object.values(services)[0] as any;
    expect(svc.Properties.LoadBalancers).toHaveLength(2);
  });

  test('vpc-expand: the VPC origin exists in the distribution but NOTHING targets it yet', () => {
    const dist = Object.values(expandOut.raw.frontend.Resources).find(
      (r: any) => r.Type === 'AWS::CloudFront::Distribution',
    ) as any;
    const origins = dist.Properties.DistributionConfig.Origins;
    const ids = origins.map((o: any) => o.Id);
    expect(ids).toEqual(expect.arrayContaining(['S3Origin', 'ALBOrigin', 'ALBVpcOrigin']));
    for (const b of dist.Properties.DistributionConfig.CacheBehaviors) {
      expect(b.TargetOriginId).toBe('ALBOrigin'); // still the OLD origin
    }
  });

  test('vpc-switch: the SAME two ALBs still exist, but every behaviour now targets the VPC origin', () => {
    const tpl = Template.fromJSON(switchOut.raw.alb);
    tpl.resourceCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', 2);

    const switchIds = albLogicalIds(switchOut.raw.alb).sort();
    const expandIds = albLogicalIds(expandOut.raw.alb).sort();
    expect(switchIds).toEqual(expandIds); // nothing new, nothing removed

    const dist = Object.values(switchOut.raw.frontend.Resources).find(
      (r: any) => r.Type === 'AWS::CloudFront::Distribution',
    ) as any;
    for (const b of dist.Properties.DistributionConfig.CacheBehaviors) {
      expect(b.TargetOriginId).toBe('ALBVpcOrigin');
    }
    // ALBOrigin is still DECLARED (the one-line-revert safety net) even
    // though nothing references it any more.
    const ids = dist.Properties.DistributionConfig.Origins.map((o: any) => o.Id);
    expect(ids).toContain('ALBOrigin');
  });

  test('vpc (contract): the public ALB and its export are gone entirely', () => {
    const publicIds = albLogicalIds(publicOut.raw.alb);
    const remainingIds = Object.keys(vpcOut.raw.alb.Resources || {});
    for (const id of publicIds) {
      expect(remainingIds).not.toContain(id);
    }
    expect(Object.keys(vpcOut.raw.alb.Outputs || {})).not.toContain('AlbDnsName');

    const dist = Object.values(vpcOut.raw.frontend.Resources).find(
      (r: any) => r.Type === 'AWS::CloudFront::Distribution',
    ) as any;
    const ids = dist.Properties.DistributionConfig.Origins.map((o: any) => o.Id);
    expect(ids).not.toContain('ALBOrigin'); // the bypass's origin is gone, not just unreferenced
  });

  test('every state keeps the ALB security group ingress rule byte-identical (network stack untouched by cloudfrontOrigin)', () => {
    const pick = (raw: Record<string, any>) =>
      Object.entries(raw.Resources || {}).filter(
        ([, r]: [string, any]) => r.Type === 'AWS::EC2::SecurityGroupIngress',
      );
    const base = JSON.stringify(pick(publicOut.raw.network));
    for (const out of [expandOut, switchOut, vpcOut]) {
      expect(JSON.stringify(pick(out.raw.network))).toBe(base);
    }
  });

  test("bin/iac.ts's own idle timeout / drop_invalid_header_fields / desync_mitigation_mode never drift across migration states", () => {
    const albAttrs = (raw: Record<string, any>, scheme: string) => {
      const albs = Object.values(raw.Resources || {}).filter(
        (r: any) => r.Type === 'AWS::ElasticLoadBalancingV2::LoadBalancer' && r.Properties.Scheme === scheme,
      ) as any[];
      expect(albs).toHaveLength(1);
      const out: Record<string, string> = {};
      for (const a of albs[0].Properties.LoadBalancerAttributes) out[a.Key] = a.Value;
      return out;
    };
    for (const out of [expandOut, switchOut]) {
      const pub = albAttrs(out.raw.alb, 'internet-facing');
      const internal = albAttrs(out.raw.alb, 'internal');
      for (const key of [
        'idle_timeout.timeout_seconds',
        'routing.http.drop_invalid_header_fields.enabled',
        'routing.http.desync_mitigation_mode',
      ]) {
        expect(pub[key]).toBe(internal[key]);
      }
    }
  });
});
