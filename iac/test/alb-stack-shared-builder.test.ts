import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { AlbStack } from '../lib/alb-stack';

/**
 * `lib/alb-stack.ts`'s `buildAlb` is the ONE place every ALB this stack
 * builds gets its tuned values (health check, idle timeout, the two
 * routing attributes) — in every `cloudfrontOrigin`/`entryMode`
 * combination. This file asserts the shared values actually show up
 * identically on BOTH ALBs when both exist (`vpc-expand`), and that the
 * internal-only (`vpc`) and shared (`both`) shapes are what
 * `cloudfrontOrigin`'s doc promises.
 *
 * Mutation check: deleting either `alb.setAttribute(...)` call in
 * `buildAlb`, or passing a divergent value for just one of the two ALBs,
 * fails the "identical attributes" test below.
 */
describe('AlbStack: cloudfrontOrigin modes share one builder', () => {
  function testVpcAndSg(stackId: string) {
    const app = new cdk.App();
    const networkStack = new cdk.Stack(app, `${stackId}Network`, {
      env: { account: '123456789012', region: 'us-east-1' },
    });
    const vpc = new ec2.Vpc(networkStack, 'TestVpc', {
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        { name: 'Public', subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
        { name: 'PrivateIsolated', subnetType: ec2.SubnetType.PRIVATE_ISOLATED, cidrMask: 24 },
      ],
    });
    const securityGroup = new ec2.SecurityGroup(networkStack, 'TestSG', { vpc });
    // Round-1 implementation review's SG-split fix: an internal ALB build
    // now requires its OWN security group, never `securityGroup` above
    // (lib/network-stack.ts's `internalAlbSecurityGroup`, lib/alb-stack.ts's
    // `internalSecurityGroup` prop) — see that prop's doc for why.
    const internalSecurityGroup = new ec2.SecurityGroup(networkStack, 'TestInternalSG', { vpc });
    return { app, vpc, securityGroup, internalSecurityGroup };
  }

  test('vpc-expand: both ALBs exist, and carry IDENTICAL tuned attributes', () => {
    const { app, vpc, securityGroup, internalSecurityGroup } = testVpcAndSg('Expand');
    const stack = new AlbStack(app, 'TestAlbStackExpand', {
      env: { account: '123456789012', region: 'us-east-1' },
      prefix: 'stratoclave',
      vpc,
      securityGroup,
      internalSecurityGroup,
      cloudfrontOrigin: 'vpc-expand',
    });
    const template = Template.fromStack(stack);

    template.resourceCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', 2);
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
      Name: 'stratoclave-alb',
      Scheme: 'internet-facing',
    });
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
      Scheme: 'internal',
    });

    // Logical ids are content-hashed, so match by scheme instead of id substring.
    const albs = template.findResources('AWS::ElasticLoadBalancingV2::LoadBalancer');
    const entries = Object.values(albs) as any[];
    const attrsOf = (scheme: string) => {
      const r = entries.find((e) => e.Properties.Scheme === scheme);
      const out: Record<string, string> = {};
      for (const a of r.Properties.LoadBalancerAttributes) out[a.Key] = a.Value;
      return out;
    };
    const internetFacing = attrsOf('internet-facing');
    const internal = attrsOf('internal');
    expect(internetFacing['idle_timeout.timeout_seconds']).toBe('60');
    expect(internal['idle_timeout.timeout_seconds']).toBe('60');
    expect(internetFacing['routing.http.drop_invalid_header_fields.enabled']).toBe('true');
    expect(internal['routing.http.drop_invalid_header_fields.enabled']).toBe('true');
    expect(internetFacing['routing.http.desync_mitigation_mode']).toBe('defensive');
    expect(internal['routing.http.desync_mitigation_mode']).toBe('defensive');

    // Two target groups, and both health checks are identical.
    template.resourceCountIs('AWS::ElasticLoadBalancingV2::TargetGroup', 2);
    const tgs = Object.values(template.findResources('AWS::ElasticLoadBalancingV2::TargetGroup')) as any[];
    for (const tg of tgs) {
      expect(tg.Properties.HealthCheckPath).toBe('/health');
      expect(tg.Properties.HealthCheckIntervalSeconds).toBe(30);
      expect(tg.Properties.HealthyThresholdCount).toBe(2);
      expect(tg.Properties.UnhealthyThresholdCount).toBe(3);
    }

    expect(stack.publicAlb).toBeDefined();
    expect(stack.internalAlb).toBeDefined();
    expect(stack.additionalTargetGroup).toBeDefined();
    // "Primary" stays the public ALB in this mode (unchanged meaning).
    expect(stack.alb).toBe(stack.publicAlb);
  });

  test('vpc (contract): only the internal ALB exists; no public one, no additionalTargetGroup', () => {
    const { app, vpc, securityGroup, internalSecurityGroup } = testVpcAndSg('Contract');
    const stack = new AlbStack(app, 'TestAlbStackVpc', {
      env: { account: '123456789012', region: 'us-east-1' },
      prefix: 'stratoclave',
      vpc,
      securityGroup,
      internalSecurityGroup,
      cloudfrontOrigin: 'vpc',
    });
    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', 1);
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
      Scheme: 'internal',
    });
    // CDK-generated name, never the fixed "<prefix>-alb".
    const albs = Object.values(template.findResources('AWS::ElasticLoadBalancingV2::LoadBalancer')) as any[];
    expect(albs[0].Properties.Name).toBeUndefined();

    expect(stack.publicAlb).toBeUndefined();
    expect(stack.additionalTargetGroup).toBeUndefined();
    expect(stack.alb).toBe(stack.internalAlb);
    // No ALB named "stratoclave-alb" exists at all — the migration guard in
    // deploy-all.sh relies on exactly this to tell "fresh/contracted" apart
    // from "still has a live public ALB".
    template.resourcePropertiesCountIs(
      'AWS::ElasticLoadBalancingV2::LoadBalancer',
      { Name: 'stratoclave-alb' },
      0,
    );
  });

  test('both: one internal ALB with BOTH an HTTP :80 forward listener AND an HTTPS :443 listener', () => {
    const { app, vpc, securityGroup, internalSecurityGroup } = testVpcAndSg('Both');
    const stack = new AlbStack(app, 'TestAlbStackBoth', {
      env: { account: '123456789012', region: 'us-east-1' },
      prefix: 'stratoclave',
      vpc,
      securityGroup,
      internalSecurityGroup,
      entryMode: 'both',
      cloudfrontOrigin: 'vpc',
      internal: true,
      certificateArn:
        'arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555',
    });
    const template = Template.fromStack(stack);
    template.resourceCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', 1);
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
      Scheme: 'internal',
    });
    // :80 is a plain forward, NEVER a redirect — redirecting would break
    // CloudFront's VPC-origin traffic on this port.
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
      Port: 80,
      Protocol: 'HTTP',
      DefaultActions: Match.arrayWith([Match.objectLike({ Type: 'forward' })]),
    });
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
      Port: 443,
      Protocol: 'HTTPS',
    });
    expect(stack.httpsListener).toBeDefined();
  });

  test('both: requires cloudfrontOrigin="vpc" or throws (defence-in-depth)', () => {
    const { app, vpc, securityGroup } = testVpcAndSg('BothBad');
    expect(
      () =>
        new AlbStack(app, 'TestAlbStackBothBad', {
          env: { account: '123456789012', region: 'us-east-1' },
          prefix: 'stratoclave',
          vpc,
          securityGroup,
          entryMode: 'both',
          cloudfrontOrigin: 'public',
          internal: true,
          certificateArn:
            'arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555',
        }),
    ).toThrow(/entryMode "both" requires cloudfrontOrigin "vpc"/);
  });

  test('both: requires certificateArn or throws', () => {
    const { app, vpc, securityGroup } = testVpcAndSg('BothNoCert');
    expect(
      () =>
        new AlbStack(app, 'TestAlbStackBothNoCert', {
          env: { account: '123456789012', region: 'us-east-1' },
          prefix: 'stratoclave',
          vpc,
          securityGroup,
          entryMode: 'both',
          cloudfrontOrigin: 'vpc',
          internal: true,
        }),
    ).toThrow(/entryMode "both" requires certificateArn/);
  });
});
