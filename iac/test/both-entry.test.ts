import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { hermeticSynthEnv } from './_hermetic-synth';

/**
 * `STRATOCLAVE_ENTRY=both`: CloudFront (via a VPC origin) and PrivateLink
 * share one internal ALB and one backend. A real `cdk synth --all` through
 * the actual `bin/iac.ts` entrypoint, same style as `test/private-entry.test.ts`
 * and `test/nag-synth.test.ts` — exercising the real cross-stack wiring, not
 * stacks constructed in isolation.
 *
 * Synth runs with ambient AWS credentials neutralised — see
 * `test/_hermetic-synth.ts`.
 */
describe('STRATOCLAVE_ENTRY=both — synthesized templates', () => {
  const iacDir = path.resolve(__dirname, '..');
  const PREFIX = 'bothtest';
  const REGION = 'us-east-1';
  const CERT_ARN = `arn:aws:acm:${REGION}:123456789012:certificate/11111111-2222-3333-4444-555555555555`;
  const DOMAIN = 'stratoclave.internal.example.com';
  const PRINCIPAL = 'arn:aws:iam::123456789012:root';

  function baseEnv(extra: Record<string, string> = {}): NodeJS.ProcessEnv {
    return hermeticSynthEnv({
      CDK_DEFAULT_ACCOUNT: '123456789012',
      CDK_DEFAULT_REGION: REGION,
      STRATOCLAVE_REGION: REGION,
      STRATOCLAVE_PREFIX: PREFIX,
      CDK_NAG: 'off',
      STRATOCLAVE_ENTRY: 'both',
      STRATOCLAVE_PRIVATE_CERTIFICATE_ARN: CERT_ARN,
      STRATOCLAVE_PRIVATE_DOMAIN: DOMAIN,
      STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS: PRINCIPAL,
      ...extra,
    });
  }

  function synth(env: NodeJS.ProcessEnv): { outDir: string; failed: boolean; output: string } {
    const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'stratoclave-both-entry-'));
    try {
      const stdout = execFileSync(
        'npx',
        ['cdk', 'synth', '--all', '-o', outDir, '--quiet'],
        { cwd: iacDir, env, encoding: 'utf-8', stdio: 'pipe' },
      );
      return { outDir, failed: false, output: stdout };
    } catch (e: unknown) {
      const err = e as { stdout?: string; stderr?: string };
      return { outDir, failed: true, output: `${err.stdout ?? ''}\n${err.stderr ?? ''}` };
    }
  }

  function rawTemplate(outDir: string, stackId: string): Record<string, any> {
    return JSON.parse(
      fs.readFileSync(path.join(outDir, `${PREFIX}-${stackId}.template.json`), 'utf-8'),
    );
  }

  function templateFor(outDir: string, stackId: string): Template {
    return Template.fromJSON(rawTemplate(outDir, stackId));
  }

  describe('a valid configuration', () => {
    let outDir: string;

    beforeAll(() => {
      const result = synth(baseEnv());
      if (result.failed) {
        throw new Error(`cdk synth failed:\n${result.output}`);
      }
      outDir = result.outDir;
    }, 120_000);

    afterAll(() => {
      if (outDir) fs.rmSync(outDir, { recursive: true, force: true });
    });

    test('CloudFront AND PrivateLink both exist, sharing one ALB stack', () => {
      const produced = fs
        .readdirSync(outDir)
        .filter((f) => f.endsWith('.template.json'))
        .map((f) => f.replace(/\.template\.json$/, ''));
      expect(produced).toEqual(
        expect.arrayContaining([
          `${PREFIX}-network`,
          `${PREFIX}-dynamodb`,
          `${PREFIX}-ecr`,
          `${PREFIX}-alb`,
          `${PREFIX}-waf`,
          `${PREFIX}-frontend`,
          `${PREFIX}-private-link`,
          `${PREFIX}-cognito`,
          `${PREFIX}-ecs`,
          `${PREFIX}-config`,
        ]),
      );
    });

    describe('the shared internal ALB', () => {
      let tpl: Template;
      beforeAll(() => {
        tpl = templateFor(outDir, 'alb');
      });

      test('exactly one ALB, internal scheme — no public ALB at all', () => {
        tpl.resourceCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', 1);
        tpl.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
          Type: 'application',
          Scheme: 'internal',
        });
      });

      test('exactly two listeners: :80 plain forward (never a redirect) and :443 HTTPS with the operator cert', () => {
        tpl.resourceCountIs('AWS::ElasticLoadBalancingV2::Listener', 2);
        tpl.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
          Port: 80,
          Protocol: 'HTTP',
          DefaultActions: Match.arrayWith([Match.objectLike({ Type: 'forward' })]),
        });
        tpl.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
          Port: 443,
          Protocol: 'HTTPS',
          Certificates: Match.arrayWith([Match.objectLike({ CertificateArn: CERT_ARN })]),
        });
      });

      test('no :80-redirect-to-:443 listener action anywhere', () => {
        const raw = rawTemplate(outDir, 'alb');
        const listeners = Object.values(raw.Resources || {}).filter(
          (r: any) => r.Type === 'AWS::ElasticLoadBalancingV2::Listener',
        ) as any[];
        for (const l of listeners) {
          for (const action of l.Properties.DefaultActions) {
            expect(action.Type).not.toBe('redirect');
          }
        }
      });

      test('the two routing attributes and the explicit idle timeout are present (lifted unconditionally)', () => {
        // Match.arrayWith scans left-to-right without backtracking, so these
        // must be listed in the SAME relative order lib/alb-stack.ts's
        // buildAlb actually produces them in (idle timeout from the L2
        // construct's own prop, then the two explicit setAttribute calls).
        tpl.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
          LoadBalancerAttributes: Match.arrayWith([
            Match.objectLike({ Key: 'idle_timeout.timeout_seconds', Value: '60' }),
            Match.objectLike({ Key: 'routing.http.drop_invalid_header_fields.enabled', Value: 'true' }),
            Match.objectLike({ Key: 'routing.http.desync_mitigation_mode', Value: 'defensive' }),
          ]),
        });
      });

      test('XFF processing attribute is ABSENT (AWS default "append" stays in force, for the CloudFront leg)', () => {
        const raw = rawTemplate(outDir, 'alb');
        const alb = Object.values(raw.Resources || {}).find(
          (r: any) => r.Type === 'AWS::ElasticLoadBalancingV2::LoadBalancer',
        ) as any;
        const keys = alb.Properties.LoadBalancerAttributes.map((a: any) => a.Key);
        expect(keys).not.toContain('routing.http.xff_header_processing.mode');
      });

      test('real ALB access logs are enabled (the PrivateLink leg has no CloudFront edge log)', () => {
        tpl.resourceCountIs('AWS::S3::Bucket', 1); // this stack's own access-log bucket
        tpl.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
          LoadBalancerAttributes: Match.arrayWith([
            Match.objectLike({ Key: 'access_logs.s3.enabled', Value: 'true' }),
          ]),
        });
      });
    });

    describe('the network stack', () => {
      let tpl: Template;
      beforeAll(() => {
        tpl = templateFor(outDir, 'network');
      });

      test('BOTH the CloudFront prefix-list lookup AND the NLB security group exist', () => {
        tpl.resourceCountIs('Custom::AWS', 1); // CloudFront prefix-list lookup
        tpl.hasResourceProperties('AWS::EC2::SecurityGroupIngress', {
          FromPort: 80,
          SourcePrefixListId: Match.anyValue(),
        });
        tpl.hasResourceProperties('AWS::EC2::SecurityGroupIngress', {
          FromPort: 443,
          ToPort: 443,
        });
        tpl.hasResourceProperties('AWS::EC2::SecurityGroupEgress', {
          FromPort: 443,
          ToPort: 443,
        });
      });

      // Reviewed design note (Fable 5.2, both-mode design review): the
      // per-port separation between the CloudFront leg and the PrivateLink
      // leg is load-bearing for the backend's own leg classification
      // (`core/forwarded_port.py`) — if a THIRD ingress rule ever let some
      // other traffic reach port 80 or 443, "which port = which path"
      // would stop being true. Pin the EXACT count, not just presence, so
      // a stray additional rule fails this test.
      test('EXACTLY three ingress rules and one egress rule — no additional path onto this ALB', () => {
        tpl.resourceCountIs('AWS::EC2::SecurityGroupIngress', 3); // CF:80, NLB:443, ECS-from-ALB:8000
        tpl.resourceCountIs('AWS::EC2::SecurityGroupEgress', 1); // NLB -> ALB:443 only
      });

      test('a PrivateIsolated subnet group exists for the shared internal ALB', () => {
        const raw = rawTemplate(outDir, 'network');
        const isolated = Object.values(raw.Resources || {}).filter(
          (r: any) =>
            r.Type === 'AWS::EC2::Subnet' &&
            (r.Properties?.Tags ?? []).some(
              (t: any) => t.Key === 'aws-cdk:subnet-type' && t.Value === 'Isolated',
            ),
        );
        expect(isolated).toHaveLength(2);
      });
    });

    describe('the frontend distribution', () => {
      let dist: any;
      beforeAll(() => {
        const raw = rawTemplate(outDir, 'frontend');
        dist = Object.values(raw.Resources || {}).find(
          (r: any) => r.Type === 'AWS::CloudFront::Distribution',
        );
      });

      test('only a VPC origin for the ALB — no plain customOriginConfig ALBOrigin at all', () => {
        const origins = dist.Properties.DistributionConfig.Origins;
        const albOrigins = origins.filter((o: any) => o.Id !== 'S3Origin');
        expect(albOrigins).toHaveLength(1);
        expect(albOrigins[0].Id).toBe('ALBVpcOrigin');
        expect(albOrigins[0].VpcOriginConfig).toBeDefined();
        expect(albOrigins[0].CustomOriginConfig).toBeUndefined();
      });

      test('all four ALB behaviours target the VPC origin over https-only', () => {
        const behaviours = dist.Properties.DistributionConfig.CacheBehaviors;
        expect(behaviours).toHaveLength(4);
        for (const b of behaviours) {
          expect(b.TargetOriginId).toBe('ALBVpcOrigin');
          expect(b.ViewerProtocolPolicy).toBe('https-only');
        }
      });
    });

    describe('the ECS task environment: CloudFront URL is primary, PrivateLink URL is separate', () => {
      let env: Record<string, string>;
      beforeAll(() => {
        const raw = rawTemplate(outDir, 'ecs');
        const taskDef = Object.values(raw.Resources || {}).find(
          (r: any) => r.Type === 'AWS::ECS::TaskDefinition',
        ) as any;
        const backend = taskDef.Properties.ContainerDefinitions.find((c: any) =>
          /backend/i.test(c.Name),
        );
        env = {};
        for (const pair of backend.Environment) env[pair.Name] = pair.Value;
      });

      // CORS_ORIGINS / STRATOCLAVE_API_ENDPOINT are CloudFormation intrinsics
      // (`Fn::Join` over an `Fn::ImportValue` of the frontend distribution's
      // domain, a cross-stack token), not plain strings in the raw
      // synthesized JSON — compare the rendered intrinsic structure, the
      // same way `test/default-entry-byte-identical.test.ts` does.
      const cloudFrontUrlIntrinsic = {
        'Fn::Join': [
          '',
          ['https://', { 'Fn::ImportValue': expect.stringContaining('frontend') }],
        ],
      };

      test('CORS_ORIGINS is the CloudFront domain ONLY (the PrivateLink path is headless)', () => {
        expect(env.CORS_ORIGINS).toEqual(cloudFrontUrlIntrinsic);
        expect(JSON.stringify(env.CORS_ORIGINS)).not.toContain(DOMAIN);
      });

      test('STRATOCLAVE_API_ENDPOINT is the CloudFront domain (the default/fallback value)', () => {
        expect(env.STRATOCLAVE_API_ENDPOINT).toEqual(env.CORS_ORIGINS);
      });

      test('STRATOCLAVE_API_ENDPOINT_PRIVATE is the operator PrivateLink domain (a literal string, not a cross-stack token)', () => {
        expect(env.STRATOCLAVE_API_ENDPOINT_PRIVATE).toBe(`https://${DOMAIN}`);
      });

      test('STRATOCLAVE_STS_AUDIENCES lists both hosts (forward-wiring; not consumed yet)', () => {
        // This one IS a plain joined string: Array.join(',') over one
        // literal (the private URL) and one CDK Token string placeholder
        // (the CloudFront URL) — the token resolves at template-render
        // time, so the raw JSON already contains the final Fn::Join form
        // for that half and a literal for the other, concatenated. Assert
        // on the two pieces we know for certain rather than the exact
        // intrinsic shape of the join (which is the join IMPLEMENTATION,
        // not the property under test).
        const rendered = JSON.stringify(env.STRATOCLAVE_STS_AUDIENCES);
        expect(rendered).toContain(`https://${DOMAIN}`);
        expect(rendered).toContain('frontend');
      });
    });

    test('exports only AlbDnsName is absent; InternalAlbDnsName exists instead', () => {
      const raw = rawTemplate(outDir, 'alb');
      const outputs = Object.keys(raw.Outputs || {});
      expect(outputs).not.toContain('AlbDnsName');
      expect(outputs).toContain('InternalAlbDnsName');
    });
  });

  test('STRATOCLAVE_CLOUDFRONT_ORIGIN=public under both is refused at synth', () => {
    const result = synth(baseEnv({ STRATOCLAVE_CLOUDFRONT_ORIGIN: 'public' }));
    try {
      expect(result.failed).toBe(true);
      expect(result.output).toMatch(/is refused under STRATOCLAVE_ENTRY=both/);
    } finally {
      fs.rmSync(result.outDir, { recursive: true, force: true });
    }
  }, 60_000);

  test('missing PrivateLink variables under both is refused, same message shape as private', () => {
    const result = synth(
      hermeticSynthEnv({
        CDK_DEFAULT_ACCOUNT: '123456789012',
        CDK_DEFAULT_REGION: REGION,
        STRATOCLAVE_REGION: REGION,
        STRATOCLAVE_PREFIX: PREFIX,
        CDK_NAG: 'off',
        STRATOCLAVE_ENTRY: 'both',
      }),
    );
    try {
      expect(result.failed).toBe(true);
      expect(result.output).toMatch(
        /STRATOCLAVE_PRIVATE_CERTIFICATE_ARN.*STRATOCLAVE_PRIVATE_DOMAIN.*STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS/,
      );
    } finally {
      fs.rmSync(result.outDir, { recursive: true, force: true });
    }
  }, 60_000);
});
