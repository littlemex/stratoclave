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
      let raw: Record<string, any>;
      beforeAll(() => {
        tpl = templateFor(outDir, 'network');
        raw = rawTemplate(outDir, 'network');
      });

      // Round-1 implementation review ("tests a mutation survives", both
      // reviews): a bare `resourceCountIs`/`hasResourceProperties` check
      // cannot tell WHICH security group an ingress rule attaches to — a
      // rule silently moved onto the wrong group (e.g. the CloudFront
      // prefix-list rule ending up on the SHARED `AlbSecurityGroup`, which
      // `both` mode never attaches to any ALB, instead of the
      // `InternalAlbSecurityGroup` the live ALB actually uses) keeps every
      // one of those checks green. These helpers resolve a security
      // group's own LOGICAL ID by its `GroupDescription` (set once, by
      // lib/network-stack.ts, and never duplicated elsewhere), then filter
      // ingress/egress rules by whether their `GroupId` property actually
      // references that id — a `GroupId` naming a DIFFERENT group fails to
      // match and the rule simply does not appear in that group's list,
      // which is what makes a misattached rule visible here.
      function sgLogicalIdByDescription(pattern: RegExp): string {
        const matches = Object.entries(raw.Resources || {}).filter(
          ([, r]: [string, any]) =>
            r.Type === 'AWS::EC2::SecurityGroup' && pattern.test(r.Properties?.GroupDescription ?? ''),
        );
        expect(matches).toHaveLength(1);
        return matches[0][0];
      }

      function rulesForGroup(resourceType: string, groupLogicalId: string): any[] {
        return Object.values(raw.Resources || {}).filter((r: any) => {
          if (r.Type !== resourceType) return false;
          return JSON.stringify(r.Properties?.GroupId ?? {}).includes(groupLogicalId);
        });
      }

      test('no SecurityGroup resource carries an INLINE SecurityGroupIngress array', () => {
        // Every INGRESS rule in this stack is added via `.addIngressRule`,
        // which CDK renders as a standalone `AWS::EC2::SecurityGroupIngress`
        // resource. A rule written inline instead (`new ec2.SecurityGroup(
        // ..., { securityGroupIngress: [...] })`) would be invisible to
        // every `resourceCountIs('AWS::EC2::SecurityGroupIngress', ...)`
        // check in this file, including the ones below.
        //
        // Egress is deliberately NOT checked here the same way: every
        // `allowAllOutbound: true` security group in this stack (the
        // public and internal ALB groups) renders an inline
        // `SecurityGroupEgress: [{CidrIp: "0.0.0.0/0", ...}]` by CDK's own
        // design whenever that prop is set — that is CDK's normal
        // rendering of a constructor PROP this codebase uses deliberately,
        // not a rule hidden from `.addEgressRule`'s standalone-resource
        // pattern the way an inline INGRESS array would be. The NLB's own
        // security group (`allowAllOutbound: false`) has no such rule at
        // all and its one real egress rule is still a standalone resource,
        // asserted explicitly in the next test.
        const groups = Object.values(raw.Resources || {}).filter(
          (r: any) => r.Type === 'AWS::EC2::SecurityGroup',
        );
        for (const g of groups) {
          expect((g as any).Properties?.SecurityGroupIngress).toBeUndefined();
        }
      });

      test('the shared internal ALB security group admits EXACTLY the CloudFront prefix list on :80 and the NLB on :443 — no other source, and the public ALB security group carries neither rule', () => {
        const internalSgId = sgLogicalIdByDescription(/internal \(VPC-origin\) ALB/);
        const publicSgId = sgLogicalIdByDescription(/^Security group for \S+ ALB$/);
        expect(internalSgId).not.toBe(publicSgId);

        const internalIngress = rulesForGroup('AWS::EC2::SecurityGroupIngress', internalSgId);
        expect(internalIngress).toHaveLength(2); // CF:80, NLB:443
        // Sorted by FromPort NUMERICALLY, not via `Array.prototype.sort`'s
        // default string-coercing comparator (which would put the `443`
        // tuple before the `80` one, since "4" < "8" lexicographically —
        // a mistake in THIS test, not in the resources it reads).
        const internalTuples = internalIngress
          .map((r: any) => [r.Properties.FromPort, r.Properties.ToPort, !!r.Properties.SourcePrefixListId, !!r.Properties.SourceSecurityGroupId])
          .sort((a: any[], b: any[]) => a[0] - b[0]);
        expect(internalTuples).toEqual([
          [80, 80, true, false], // CloudFront origin-facing prefix list
          [443, 443, false, true], // the internal NLB's security group
        ]);

        // The PUBLIC/`private`-shaped security group is still CREATED in
        // this mode (lib/network-stack.ts creates it unconditionally) but
        // `both` never attaches any ALB to it — it must carry NEITHER rule.
        const publicIngress = rulesForGroup('AWS::EC2::SecurityGroupIngress', publicSgId);
        expect(publicIngress).toHaveLength(0);

        const nlbSgId = sgLogicalIdByDescription(/internal NLB/);
        const internalEgressFromNlb = rulesForGroup('AWS::EC2::SecurityGroupEgress', nlbSgId);
        expect(internalEgressFromNlb).toHaveLength(1);
        expect(internalEgressFromNlb[0].Properties.FromPort).toBe(443);
        expect(internalEgressFromNlb[0].Properties.ToPort).toBe(443);
        // The egress rule's destination (DestinationSecurityGroupId) must
        // be the INTERNAL alb's SG, never the unused public one — the same
        // misattachment class the ingress check above guards.
        expect(
          JSON.stringify(internalEgressFromNlb[0].Properties.DestinationSecurityGroupId ?? {}),
        ).toContain(internalSgId);
      });

      test('BOTH the CloudFront prefix-list lookup AND the NLB security group exist', () => {
        tpl.resourceCountIs('Custom::AWS', 1); // CloudFront prefix-list lookup
      });

      // Reviewed design note (Fable 5.2, both-mode design review): the
      // per-port separation between the CloudFront leg and the PrivateLink
      // leg is load-bearing for the backend's own leg classification
      // (`core/forwarded_port.py`) — if a THIRD ingress rule ever let some
      // other traffic reach port 80 or 443, "which port = which path"
      // would stop being true. Pin the EXACT count, not just presence
      // (round-1 implementation review: pinned per-group above too, not
      // only in total), so a stray additional rule fails this test.
      test('EXACTLY three ingress rules and one egress rule, across the whole stack', () => {
        // CF:80 + NLB:443 (both on the internal ALB SG, explicit in this
        // file's own wiring) + ECS-from-the-internal-ALB:8000 — NOT this
        // file's own explicit (and, under `both`, inert) rule from the
        // unused public-shaped `AlbSecurityGroup` (`needsPublicAlbSg` is
        // false whenever `cloudfrontOrigin === 'vpc'`, which `both` always
        // forces — lib/network-stack.ts no longer writes that rule at all
        // in this mode), but the one CDK's own
        // `FargateService.attachToApplicationTargetGroup` (lib/ecs-stack.ts)
        // adds automatically, pointed at whichever security group the
        // ATTACHED target group's ALB actually belongs to.
        tpl.resourceCountIs('AWS::EC2::SecurityGroupIngress', 3);
        tpl.resourceCountIs('AWS::EC2::SecurityGroupEgress', 1); // NLB -> internal ALB:443 only
      });

      test('a PrivateIsolated subnet group exists for the shared internal ALB', () => {
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

      // Round-1 implementation review ("tests a mutation survives"): the
      // test above only checks that SOME `VpcOriginConfig` object exists on
      // the distribution's origin entry; it never reads what is actually
      // inside the `AWS::CloudFront::VpcOrigin` RESOURCE itself (a sibling
      // resource `origins.VpcOrigin.withApplicationLoadBalancer(...).bind()`
      // creates once, under the fixed construct id "VpcOrigin" —
      // frontend-stack.ts's own comment on why it may only be called once).
      // A wrong port, a wrong protocol policy, or an ARN pointed at the
      // WRONG load balancer would all still leave that earlier test green.
      test('the AWS::CloudFront::VpcOrigin resource targets the internal ALB on :80, http-only — not merely "a VpcOriginConfig exists"', () => {
        const raw = rawTemplate(outDir, 'frontend');
        const vpcOrigins = Object.values(raw.Resources || {}).filter(
          (r: any) => r.Type === 'AWS::CloudFront::VpcOrigin',
        );
        expect(vpcOrigins).toHaveLength(1);
        const config = (vpcOrigins[0] as any).Properties.VpcOriginEndpointConfig;
        expect(config.HTTPPort).toBe(80);
        expect(config.OriginProtocolPolicy).toBe('http-only');
        // The ARN is a CROSS-STACK reference into AlbStack (the internal
        // ALB lives there, not in FrontendStack) — confirmed structurally
        // by the presence of an `Fn::ImportValue`, the same signal
        // `test/cross-stack-export-transition.test.ts`'s checker looks
        // for, rather than asserting the exact (account/region-dependent)
        // export name string.
        expect(JSON.stringify(config.Arn)).toContain('Fn::ImportValue');
        // It must NOT be the public ALB's name/ARN under any circumstance
        // -- 'both' never builds a public ALB at all, so there is nothing
        // for this to legitimately reference instead.
        expect(JSON.stringify(config.Arn)).not.toMatch(/PublicAlb|publicAlb/);
      });

      // The origin-side read/keepalive timeouts this stack shares with the
      // public-ALB origin (lib/_common.ts's ORIGIN_READ_TIMEOUT_SECONDS /
      // ORIGIN_KEEPALIVE_TIMEOUT_SECONDS) — searched recursively across the
      // origin entry rather than asserting one exact JSON path, because
      // this codebase does not independently verify (without running
      // `cdk synth` + reading the rendered output on a real machine, which
      // this round's standing "no builds/tests on this Mac" rule forbids)
      // whether aws-cdk-lib nests these under `VpcOriginConfig` or leaves
      // them as siblings of it on the origin entry — either placement
      // satisfies what this test is actually checking: the VALUE reached
      // this origin at all, unmodified from the shared constant.
      test('the VPC origin carries the SAME 60s read/keepalive timeouts as the public-ALB origin, outside any snapshot', () => {
        const albOrigin = dist.Properties.DistributionConfig.Origins.find(
          (o: any) => o.Id === 'ALBVpcOrigin',
        );
        const values = new Set<unknown>();
        const walk = (node: unknown): void => {
          if (Array.isArray(node)) {
            node.forEach(walk);
          } else if (node && typeof node === 'object') {
            for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
              if (key === 'OriginReadTimeout' || key === 'OriginKeepaliveTimeout') {
                values.add(value);
              } else {
                walk(value);
              }
            }
          }
        };
        walk(albOrigin);
        expect(values.size).toBeGreaterThan(0);
        for (const v of values) {
          expect(v).toBe(60);
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

      test('STRATOCLAVE_STS_AUDIENCES adds the PrivateLink host; the CloudFront host comes from STRATOCLAVE_API_ENDPOINT', () => {
        // backend/mvp/sso_sts.expected_audiences() always includes the host of
        // STRATOCLAVE_API_ENDPOINT (the CloudFront URL here), so the extra list
        // carries only the second entry's host.
        const rendered = JSON.stringify(env.STRATOCLAVE_STS_AUDIENCES);
        expect(rendered).toContain(`https://${DOMAIN}`);
        expect(rendered).not.toContain('frontend');
        expect(JSON.stringify(env.STRATOCLAVE_API_ENDPOINT)).toContain('frontend');
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
