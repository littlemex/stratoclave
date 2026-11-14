import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { hermeticSynthEnv } from './_hermetic-synth';

/**
 * `STRATOCLAVE_ENTRY=private`: a real `cdk synth --all` through the actual
 * `bin/iac.ts` entrypoint, the same style as `test/nag-synth.test.ts` and
 * `test/documented-env-wiring.test.ts` — exercising the real wiring (stack
 * dependencies, cross-stack references) rather than constructing stacks in
 * isolation, which is exactly where the one defect this feature's own
 * implementation hit (a dependency cycle between NetworkStack and
 * PrivateLinkStack over the ALB security group's ingress rule) would have
 * been invisible to a per-stack unit test.
 *
 * Synth runs with ambient AWS credentials neutralised — see
 * `test/_hermetic-synth.ts` for why that matters (the `cdk` CLI otherwise
 * overwrites `CDK_DEFAULT_ACCOUNT` with whatever real account the ambient
 * credentials resolve to, regardless of what this file passes).
 */
describe('STRATOCLAVE_ENTRY=private — synthesized templates', () => {
  const iacDir = path.resolve(__dirname, '..');
  const PREFIX = 'privtest';
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
      STRATOCLAVE_ENTRY: 'private',
      STRATOCLAVE_PRIVATE_CERTIFICATE_ARN: CERT_ARN,
      STRATOCLAVE_PRIVATE_DOMAIN: DOMAIN,
      STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS: PRINCIPAL,
      ...extra,
    });
  }

  function synth(env: NodeJS.ProcessEnv): { outDir: string; failed: boolean; output: string } {
    const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'stratoclave-private-entry-'));
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

    test('no CloudFront, no S3 console, no CLOUDFRONT-scope WAF stack', () => {
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
          `${PREFIX}-private-link`,
          `${PREFIX}-cognito`,
          `${PREFIX}-ecs`,
          `${PREFIX}-config`,
        ]),
      );
      expect(produced.some((id) => id.includes('frontend'))).toBe(false);
      expect(produced.some((id) => id.includes('waf'))).toBe(false);
    });

    test('no private-mode template imports a -frontend: or -waf: stack export', () => {
      const produced = fs
        .readdirSync(outDir)
        .filter((f) => f.endsWith('.template.json'));
      const offenders: Record<string, string[]> = {};
      for (const file of produced) {
        const text = fs.readFileSync(path.join(outDir, file), 'utf-8');
        const matches = text.match(new RegExp(`${PREFIX}-(frontend|waf):[A-Za-z0-9]+`, 'g'));
        if (matches) offenders[file] = Array.from(new Set(matches));
      }
      expect(offenders).toEqual({});
    });

    describe('the internal ALB', () => {
      let tpl: Template;
      beforeAll(() => {
        tpl = templateFor(outDir, 'alb');
      });

      test('scheme is internal', () => {
        tpl.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
          Type: 'application',
          Scheme: 'internal',
        });
      });

      test('exactly one listener, HTTPS :443 — no port 80 at all, not even a redirect', () => {
        tpl.resourceCountIs('AWS::ElasticLoadBalancingV2::Listener', 1);
        tpl.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
          Port: 443,
          Protocol: 'HTTPS',
          Certificates: Match.arrayWith([
            Match.objectLike({ CertificateArn: CERT_ARN }),
          ]),
        });
      });

      test('the XFF attribute is set to remove', () => {
        tpl.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
          LoadBalancerAttributes: Match.arrayWith([
            Match.objectLike({
              Key: 'routing.http.xff_header_processing.mode',
              Value: 'remove',
            }),
          ]),
        });
      });
    });

    describe('the network stack: exactly the ECS-from-ALB ingress rule, nothing CloudFront-shaped', () => {
      let tpl: Template;
      let raw: Record<string, any>;
      beforeAll(() => {
        raw = rawTemplate(outDir, 'network');
        tpl = Template.fromJSON(raw);
      });

      // Two ingress rules now live in this stack: ECS admitting the ALB on
      // :8000 (unaffected by entry mode), and the ALB admitting the NLB on
      // :443 (the rule that moved here from private-link-stack.ts; see that
      // file's class doc and network-stack.ts's `entryMode` prop doc for
      // why it belongs here, not there).
      test('exactly two AWS::EC2::SecurityGroupIngress resources, both same-stack, nothing CloudFront-shaped', () => {
        tpl.resourceCountIs('AWS::EC2::SecurityGroupIngress', 2);
        tpl.hasResourceProperties('AWS::EC2::SecurityGroupIngress', {
          IpProtocol: 'tcp',
          FromPort: 8000,
          ToPort: 8000,
        });
        tpl.hasResourceProperties('AWS::EC2::SecurityGroupIngress', {
          IpProtocol: 'tcp',
          FromPort: 443,
          ToPort: 443,
        });
      });

      test('exactly one AWS::EC2::SecurityGroupEgress resource: the NLB egressing to the ALB on :443', () => {
        tpl.resourceCountIs('AWS::EC2::SecurityGroupEgress', 1);
        tpl.hasResourceProperties('AWS::EC2::SecurityGroupEgress', {
          IpProtocol: 'tcp',
          FromPort: 443,
          ToPort: 443,
        });
      });

      test('no CloudFront origin-facing prefix-list lookup custom resource', () => {
        const customResources = Object.values(raw.Resources || {}).filter(
          (r: any) => r.Type === 'Custom::AWS',
        );
        expect(customResources).toHaveLength(0);
      });

      test('the ALB admits only the NLB security group, on :443, no CIDR/prefix-list source', () => {
        const albIngress = tpl.findResources('AWS::EC2::SecurityGroupIngress', {
          Properties: Match.objectLike({ FromPort: 443, ToPort: 443 }),
        });
        const ids = Object.keys(albIngress);
        expect(ids).toHaveLength(1);
        const props = albIngress[ids[0]].Properties;
        expect(props.IpProtocol).toBe('tcp');
        expect(props.CidrIp).toBeUndefined();
        expect(props.CidrIpv6).toBeUndefined();
        expect(props.SourcePrefixListId).toBeUndefined();
        expect(props.SourceSecurityGroupId).toBeDefined();
        // Same-stack now (finding 3): both GroupId and SourceSecurityGroupId
        // are local Fn::GetAtt references, never a cross-stack import.
        expect(props.GroupId).toHaveProperty('Fn::GetAtt');
        expect(JSON.stringify(props.GroupId)).toMatch(/AlbSecurityGroup/);
        expect(props.SourceSecurityGroupId).toHaveProperty('Fn::GetAtt');
        expect(JSON.stringify(props.SourceSecurityGroupId)).toMatch(/NlbSecurityGroup/);
      });

      test('the NLB security group itself carries no inline ingress rule', () => {
        const sgs = tpl.findResources('AWS::EC2::SecurityGroup', {
          Properties: Match.objectLike({ GroupDescription: Match.stringLikeRegexp('NLB') }),
        });
        const ids = Object.keys(sgs);
        expect(ids).toHaveLength(1);
        expect(sgs[ids[0]].Properties.SecurityGroupIngress).toBeUndefined();
      });

      test('the NLB security group egress is scoped to the ALB security group only, not allowAllOutbound', () => {
        const sgs = tpl.findResources('AWS::EC2::SecurityGroup', {
          Properties: Match.objectLike({ GroupDescription: Match.stringLikeRegexp('NLB') }),
        });
        const ids = Object.keys(sgs);
        expect(ids).toHaveLength(1);
        const egress = sgs[ids[0]].Properties.SecurityGroupEgress;
        // No "allow all" egress rule (0.0.0.0/0 on IpProtocol -1) anywhere
        // on this security group — neither inline nor as a standalone
        // resource (resourceCountIs above already pins the standalone
        // count to exactly one, the ALB-scoped rule).
        const allowsAll = (egress ?? []).some(
          (rule: any) => rule.CidrIp === '0.0.0.0/0' && rule.IpProtocol === '-1',
        );
        expect(allowsAll).toBe(false);
      });
    });

    describe('the internal NLB + VPC endpoint service (private-link stack)', () => {
      let tpl: Template;
      let raw: Record<string, any>;
      beforeAll(() => {
        raw = rawTemplate(outDir, 'private-link');
        tpl = Template.fromJSON(raw);
      });

      test('NLB scheme is internal', () => {
        tpl.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
          Type: 'network',
          Scheme: 'internal',
        });
      });

      test('NLB SG enforcement on PrivateLink traffic is off', () => {
        tpl.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
          Type: 'network',
          EnforceSecurityGroupInboundRulesOnPrivateLinkTraffic: 'off',
        });
      });

      test('TCP :443 listener forwards to an ALB-type target group on :443', () => {
        tpl.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
          Port: 443,
          Protocol: 'TCP',
        });
        tpl.hasResourceProperties('AWS::ElasticLoadBalancingV2::TargetGroup', {
          TargetType: 'alb',
          Port: 443,
          Protocol: 'TCP',
        });
      });

      test('this stack owns no security group of any kind (finding 3: both now live in the network stack)', () => {
        tpl.resourceCountIs('AWS::EC2::SecurityGroup', 0);
        tpl.resourceCountIs('AWS::EC2::SecurityGroupIngress', 0);
        tpl.resourceCountIs('AWS::EC2::SecurityGroupEgress', 0);
      });

      test('endpoint service: acceptance required, allowed principals exactly as configured', () => {
        tpl.hasResourceProperties('AWS::EC2::VPCEndpointService', {
          AcceptanceRequired: true,
        });
        tpl.hasResourceProperties('AWS::EC2::VPCEndpointServicePermissions', {
          AllowedPrincipals: [PRINCIPAL],
        });
      });

      test('no AlbDnsName output on this stack (AlbStack already owns it)', () => {
        expect(raw.Outputs?.AlbDnsName).toBeUndefined();
      });

      test('outputs the endpoint service name and the private URL', () => {
        tpl.hasOutput('EndpointServiceName', {});
        tpl.hasOutput('PrivateUrl', { Value: `https://${DOMAIN}` });
      });
    });

    describe('nothing in the private path depends on CloudFront', () => {
      test('the task env CORS_ORIGINS and STRATOCLAVE_API_ENDPOINT are exactly the private URL', () => {
        const tpl = templateFor(outDir, 'ecs');
        tpl.hasResourceProperties('AWS::ECS::TaskDefinition', {
          ContainerDefinitions: Match.arrayWith([
            Match.objectLike({
              Environment: Match.arrayWith([
                { Name: 'CORS_ORIGINS', Value: `https://${DOMAIN}` },
                { Name: 'STRATOCLAVE_API_ENDPOINT', Value: `https://${DOMAIN}` },
              ]),
            }),
          ]),
        });
      });

      test('the Cognito client callback/logout URLs are exactly the CLI loopback entries', () => {
        const tpl = templateFor(outDir, 'cognito');
        tpl.hasResourceProperties('AWS::Cognito::UserPoolClient', {
          CallbackURLs: [
            'http://127.0.0.1:18080/callback',
            'http://localhost:3003/callback',
          ],
          LogoutURLs: ['http://127.0.0.1:18080', 'http://localhost:3003'],
        });
      });
    });
  });

  // Every validation CASE (missing values, each wildcard shape, wrong
  // partition, cert region mismatch, malformed cert, domain shape) is
  // already covered in-process by entry-config.test.ts, against the exact
  // same `resolveEntryConfig` this synth calls — a `cdk synth` subprocess
  // per case re-pays that proof at ~5-10s each for no additional coverage.
  // This one case is kept at the subprocess level because it is what
  // entry-config.test.ts cannot see: that `bin/iac.ts` actually calls the
  // resolver and that a throw from it really does fail `cdk synth` (not,
  // say, get caught and swallowed somewhere in the app's own CDK wiring).
  describe('negative: a throw from the resolver fails synth', () => {
    test('a bare wildcard allowed-principal is refused', () => {
      const result = synth(baseEnv({ STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS: '*' }));
      fs.rmSync(result.outDir, { recursive: true, force: true });
      expect(result.failed).toBe(true);
      expect(result.output).toMatch(/must not contain a wildcard/);
    }, 60_000);
  });
});
