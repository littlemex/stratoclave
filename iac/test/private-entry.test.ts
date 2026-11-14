import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { Template, Match } from 'aws-cdk-lib/assertions';

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
 * Synth runs with ambient AWS credentials neutralised — see the file doc on
 * `test/default-entry-byte-identical.test.ts` for why that matters (the
 * `cdk` CLI otherwise overwrites `CDK_DEFAULT_ACCOUNT` with whatever real
 * account the ambient credentials resolve to, regardless of what this file
 * passes).
 */
describe('STRATOCLAVE_ENTRY=private — synthesized templates', () => {
  const iacDir = path.resolve(__dirname, '..');
  const PREFIX = 'privtest';
  const REGION = 'us-east-1';
  const CERT_ARN = `arn:aws:acm:${REGION}:123456789012:certificate/11111111-2222-3333-4444-555555555555`;
  const DOMAIN = 'stratoclave.internal.example.com';
  const PRINCIPAL = 'arn:aws:iam::123456789012:root';

  function baseEnv(extra: Record<string, string> = {}): NodeJS.ProcessEnv {
    const env: NodeJS.ProcessEnv = { ...process.env };
    delete env.AWS_PROFILE;
    delete env.AWS_DEFAULT_PROFILE;
    delete env.AWS_SESSION_TOKEN;
    delete env.AWS_REGION;
    delete env.AWS_DEFAULT_REGION;
    delete env.AWS_CONTAINER_CREDENTIALS_RELATIVE_URI;
    delete env.AWS_CONTAINER_CREDENTIALS_FULL_URI;
    delete env.AWS_WEB_IDENTITY_TOKEN_FILE;
    return {
      ...env,
      AWS_ACCESS_KEY_ID: 'AKIAFAKEFAKEFAKEFAKE',
      AWS_SECRET_ACCESS_KEY: 'fakefakefakefakefakefakefakefakefakefake',
      AWS_EC2_METADATA_DISABLED: 'true',
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
    };
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

      test('exactly one AWS::EC2::SecurityGroupIngress resource in the whole stack', () => {
        tpl.resourceCountIs('AWS::EC2::SecurityGroupIngress', 1);
      });

      test('that one rule is the ECS task admitting the ALB on :8000, not anything CloudFront-shaped', () => {
        tpl.hasResourceProperties('AWS::EC2::SecurityGroupIngress', {
          IpProtocol: 'tcp',
          FromPort: 8000,
          ToPort: 8000,
        });
      });

      test('no CloudFront origin-facing prefix-list lookup custom resource', () => {
        const customResources = Object.values(raw.Resources || {}).filter(
          (r: any) => r.Type === 'Custom::AWS',
        );
        expect(customResources).toHaveLength(0);
      });

      test('the ALB security group carries no ingress rule of its own', () => {
        const sgs = tpl.findResources('AWS::EC2::SecurityGroup', {
          Properties: Match.objectLike({ GroupDescription: Match.stringLikeRegexp('ALB') }),
        });
        const ids = Object.keys(sgs);
        expect(ids).toHaveLength(1);
        expect(sgs[ids[0]].Properties.SecurityGroupIngress).toBeUndefined();
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

      test('exactly one SecurityGroupIngress: ALB admits only the NLB security group, no CIDR/prefix-list source', () => {
        tpl.resourceCountIs('AWS::EC2::SecurityGroupIngress', 1);
        const ingresses = tpl.findResources('AWS::EC2::SecurityGroupIngress');
        const ids = Object.keys(ingresses);
        expect(ids).toHaveLength(1);
        const props = ingresses[ids[0]].Properties;
        expect(props.IpProtocol).toBe('tcp');
        expect(props.FromPort).toBe(443);
        expect(props.ToPort).toBe(443);
        expect(props.CidrIp).toBeUndefined();
        expect(props.CidrIpv6).toBeUndefined();
        expect(props.SourcePrefixListId).toBeUndefined();
        expect(props.SourceSecurityGroupId).toBeDefined();
        // GroupId must reference the ALB security group (a cross-stack
        // import, since the ALB SG lives in the network stack), and
        // SourceSecurityGroupId the NLB security group created in THIS
        // stack (a local Fn::GetAtt, not a cross-stack import).
        expect(JSON.stringify(props.GroupId)).toMatch(/ExportsOutput.*AlbSecurityGroup/);
        expect(props.SourceSecurityGroupId).toHaveProperty('Fn::GetAtt');
        expect(JSON.stringify(props.SourceSecurityGroupId)).toMatch(/NlbSecurityGroup/);
      });

      test('the NLB security group itself carries no ingress rule', () => {
        const sgs = tpl.findResources('AWS::EC2::SecurityGroup', {
          Properties: Match.objectLike({ GroupDescription: Match.stringLikeRegexp('NLB') }),
        });
        const ids = Object.keys(sgs);
        expect(ids).toHaveLength(1);
        expect(sgs[ids[0]].Properties.SecurityGroupIngress).toBeUndefined();
      });

      test('the NLB security group egress is scoped to the ALB security group on :443, not allowAllOutbound', () => {
        const sgs = tpl.findResources('AWS::EC2::SecurityGroup', {
          Properties: Match.objectLike({ GroupDescription: Match.stringLikeRegexp('NLB') }),
        });
        const ids = Object.keys(sgs);
        expect(ids).toHaveLength(1);
        const egress = sgs[ids[0]].Properties.SecurityGroupEgress;
        // No "allow all" egress rule (0.0.0.0/0 on IpProtocol -1) anywhere
        // on this security group.
        const allowsAll = (egress ?? []).some(
          (rule: any) => rule.CidrIp === '0.0.0.0/0' && rule.IpProtocol === '-1',
        );
        expect(allowsAll).toBe(false);
        tpl.hasResourceProperties('AWS::EC2::SecurityGroupEgress', {
          IpProtocol: 'tcp',
          FromPort: 443,
          ToPort: 443,
        });
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

  describe('negative: missing or invalid private config refuses to synth', () => {
    test('missing all three refuses, naming them', () => {
      const result = synth(
        baseEnv({
          STRATOCLAVE_PRIVATE_CERTIFICATE_ARN: '',
          STRATOCLAVE_PRIVATE_DOMAIN: '',
          STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS: '',
        }),
      );
      fs.rmSync(result.outDir, { recursive: true, force: true });
      expect(result.failed).toBe(true);
      expect(result.output).toMatch(/STRATOCLAVE_PRIVATE_CERTIFICATE_ARN/);
      expect(result.output).toMatch(/STRATOCLAVE_PRIVATE_DOMAIN/);
      expect(result.output).toMatch(/STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS/);
    }, 60_000);

    test('a bare wildcard allowed-principal is refused', () => {
      const result = synth(baseEnv({ STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS: '*' }));
      fs.rmSync(result.outDir, { recursive: true, force: true });
      expect(result.failed).toBe(true);
      expect(result.output).toMatch(/must not contain a wildcard/);
    }, 60_000);

    test('role/* is refused (wildcard inside an otherwise ARN-shaped entry)', () => {
      const result = synth(
        baseEnv({ STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS: 'arn:aws:iam::123456789012:role/*' }),
      );
      fs.rmSync(result.outDir, { recursive: true, force: true });
      expect(result.failed).toBe(true);
      expect(result.output).toMatch(/must not contain a wildcard/);
    }, 60_000);

    test('root* is refused (wildcard suffix disguised as root)', () => {
      const result = synth(
        baseEnv({ STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS: 'arn:aws:iam::123456789012:root*' }),
      );
      fs.rmSync(result.outDir, { recursive: true, force: true });
      expect(result.failed).toBe(true);
      expect(result.output).toMatch(/must not contain a wildcard/);
    }, 60_000);

    test('arn:aws-cn: is refused (wrong partition, even though otherwise well-formed)', () => {
      const result = synth(
        baseEnv({ STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS: 'arn:aws-cn:iam::123456789012:root' }),
      );
      fs.rmSync(result.outDir, { recursive: true, force: true });
      expect(result.failed).toBe(true);
      expect(result.output).toMatch(/must be aws-partition IAM principal ARNs/);
    }, 60_000);

    test('STRATOCLAVE_ENTRY=both is refused', () => {
      const result = synth(baseEnv({ STRATOCLAVE_ENTRY: 'both' }));
      fs.rmSync(result.outDir, { recursive: true, force: true });
      expect(result.failed).toBe(true);
      expect(result.output).toMatch(/not supported yet/);
    }, 60_000);

    test('a certificate ARN in a different region than the deployment is refused', () => {
      const result = synth(
        baseEnv({
          STRATOCLAVE_PRIVATE_CERTIFICATE_ARN:
            'arn:aws:acm:us-west-2:123456789012:certificate/11111111-2222-3333-4444-555555555555',
        }),
      );
      fs.rmSync(result.outDir, { recursive: true, force: true });
      expect(result.failed).toBe(true);
      expect(result.output).toMatch(/deployment's own region/);
    }, 60_000);

    test('a malformed certificate ARN is refused', () => {
      const result = synth(
        baseEnv({ STRATOCLAVE_PRIVATE_CERTIFICATE_ARN: 'not-an-arn-at-all' }),
      );
      fs.rmSync(result.outDir, { recursive: true, force: true });
      expect(result.failed).toBe(true);
      expect(result.output).toMatch(/deployment's own region/);
    }, 60_000);

    test('a domain with a scheme is refused', () => {
      const result = synth(
        baseEnv({ STRATOCLAVE_PRIVATE_DOMAIN: 'https://stratoclave.internal.example.com' }),
      );
      fs.rmSync(result.outDir, { recursive: true, force: true });
      expect(result.failed).toBe(true);
      expect(result.output).toMatch(/bare lowercase hostname/);
    }, 60_000);

    test('a domain with a trailing slash is refused', () => {
      const result = synth(
        baseEnv({ STRATOCLAVE_PRIVATE_DOMAIN: 'stratoclave.internal.example.com/' }),
      );
      fs.rmSync(result.outDir, { recursive: true, force: true });
      expect(result.failed).toBe(true);
      expect(result.output).toMatch(/bare lowercase hostname/);
    }, 60_000);

    test('an uppercase domain is refused', () => {
      const result = synth(
        baseEnv({ STRATOCLAVE_PRIVATE_DOMAIN: 'Stratoclave.Internal.Example.com' }),
      );
      fs.rmSync(result.outDir, { recursive: true, force: true });
      expect(result.failed).toBe(true);
      expect(result.output).toMatch(/bare lowercase hostname/);
    }, 60_000);
  });
});
