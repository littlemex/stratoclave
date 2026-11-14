import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { NetworkStack } from '../lib/network-stack';

/**
 * `cloudfrontOrigin` !== 'public' adds a second subnet group
 * (`PrivateIsolated`) to the VPC this stack builds, for the internal ALB
 * `lib/alb-stack.ts` builds in that case. The two guarantees this file
 * pins:
 *
 *   1. A `public`-mode VPC and a `vpc-expand`-mode VPC have IDENTICAL
 *      `Public` subnets — same CIDRs, same logical ids. Appending the new
 *      group after `Public` in `subnetConfiguration` must not re-carve or
 *      replace what already exists; this is PROVEN by diffing two synths,
 *      not merely asserted from reading the CDK source.
 *   2. The isolated subnets exist (right type, right count) exactly when
 *      `cloudfrontOrigin !== 'public'`, and nowhere else.
 *
 * Mutation check: deleting the explicit `cidrMask` from the `Public` group,
 * or reordering the array so `PrivateIsolated` comes first, would move the
 * `Public` subnets' CIDRs — test 1 below catches either.
 */
describe('NetworkStack: cloudfrontOrigin and the PrivateIsolated subnet group', () => {
  function synthVpc(cloudfrontOrigin?: 'public' | 'vpc-expand' | 'vpc-switch' | 'vpc') {
    const app = new cdk.App();
    const stack = new NetworkStack(app, 'TestNetworkStack', {
      env: { account: '123456789012', region: 'us-east-1' },
      prefix: 'stratoclave',
      cloudfrontOrigin,
    });
    return Template.fromStack(stack);
  }

  function publicSubnets(template: Template): Array<[string, any]> {
    const all = template.findResources('AWS::EC2::Subnet');
    return Object.entries(all).filter(
      ([, r]: [string, any]) =>
        (r.Properties?.Tags ?? []).some(
          (t: any) => t.Key === 'aws-cdk:subnet-type' && t.Value === 'Public',
        ),
    );
  }

  function isolatedSubnets(template: Template): Array<[string, any]> {
    const all = template.findResources('AWS::EC2::Subnet');
    return Object.entries(all).filter(
      ([, r]: [string, any]) =>
        (r.Properties?.Tags ?? []).some(
          (t: any) => t.Key === 'aws-cdk:subnet-type' && t.Value === 'Isolated',
        ),
    );
  }

  test('public mode: exactly two subnets, both Public, no isolated group at all', () => {
    const template = synthVpc('public');
    expect(isolatedSubnets(template)).toHaveLength(0);
    expect(publicSubnets(template)).toHaveLength(2);
  });

  test.each([['vpc-expand'], ['vpc-switch'], ['vpc']] as const)(
    'cloudfrontOrigin=%s: two Public subnets AND two PrivateIsolated subnets',
    (mode) => {
      const template = synthVpc(mode);
      expect(publicSubnets(template)).toHaveLength(2);
      const isolated = isolatedSubnets(template);
      expect(isolated).toHaveLength(2);
      for (const [, r] of isolated) {
        expect(r.Properties.CidrBlock).toMatch(/^10\.0\.\d+\.0\/24$/);
      }
    },
  );

  test('the two Public subnets are BYTE-IDENTICAL (logical id and CIDR) between public and vpc-expand', () => {
    const publicOnly = publicSubnets(synthVpc('public'));
    const withIsolated = publicSubnets(synthVpc('vpc-expand'));

    const normalize = (subnets: Array<[string, any]>) =>
      subnets
        .map(([id, r]) => ({ id, cidr: r.Properties.CidrBlock, az: r.Properties.AvailabilityZone }))
        .sort((a, b) => a.id.localeCompare(b.id));

    expect(normalize(withIsolated)).toEqual(normalize(publicOnly));
  });

  test('the two PrivateIsolated subnets do not overlap the Public ones', () => {
    const template = synthVpc('vpc');
    const pub = publicSubnets(template).map(([, r]) => r.Properties.CidrBlock);
    const iso = isolatedSubnets(template).map(([, r]) => r.Properties.CidrBlock);
    const all = [...pub, ...iso];
    expect(new Set(all).size).toBe(all.length);
  });

  test('entryMode="private" + cloudfrontOrigin!=="public" throws (defence-in-depth assertion)', () => {
    const app = new cdk.App();
    expect(
      () =>
        new NetworkStack(app, 'TestNetworkStack2', {
          env: { account: '123456789012', region: 'us-east-1' },
          prefix: 'stratoclave',
          entryMode: 'private',
          cloudfrontOrigin: 'vpc',
        }),
    ).toThrow(/cloudfrontOrigin.*"vpc".*has no effect when entryMode is "private"/);
  });

  test('entryMode="both": the CloudFront prefix-list ingress AND the NLB security group both exist', () => {
    const app = new cdk.App();
    const stack = new NetworkStack(app, 'TestBothNetworkStack', {
      env: { account: '123456789012', region: 'us-east-1' },
      prefix: 'stratoclave',
      entryMode: 'both',
      cloudfrontOrigin: 'vpc',
    });
    const tpl = Template.fromStack(stack);
    tpl.hasResourceProperties('AWS::EC2::SecurityGroupIngress', {
      FromPort: 80,
      SourcePrefixListId: Match.anyValue(),
    });
    tpl.resourceCountIs('AWS::EC2::SecurityGroup', 3); // Alb, Ecs, Nlb
    const sgs = tpl.findResources('AWS::EC2::SecurityGroup');
    const nlbSg = Object.values(sgs).find(
      (r: any) => r.Properties?.GroupDescription?.includes('internal NLB'),
    );
    expect(nlbSg).toBeDefined();
  });
});
