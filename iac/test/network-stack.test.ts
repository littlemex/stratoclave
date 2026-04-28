import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { NetworkStack } from '../lib/network-stack';

describe('NetworkStack (v2.1 MVP: Public Subnet 直置き、NAT なし、WAF+CloudFront 前提)', () => {
  let app: cdk.App;
  let stack: NetworkStack;
  let template: Template;

  beforeAll(() => {
    app = new cdk.App();
    stack = new NetworkStack(app, 'TestNetworkStack', {
      env: { account: '123456789012', region: 'us-east-1' },
      prefix: 'stratoclave',
    });
    template = Template.fromStack(stack);
  });

  test('VPC is created with /16 CIDR and DNS enabled', () => {
    template.hasResourceProperties('AWS::EC2::VPC', {
      CidrBlock: '10.0.0.0/16',
      EnableDnsHostnames: true,
      EnableDnsSupport: true,
    });
  });

  test('No NAT gateways (MVP runs ECS on public subnet)', () => {
    template.resourceCountIs('AWS::EC2::NatGateway', 0);
  });

  test('VPC Flow Logs are enabled (CloudWatch, 30-day retention)', () => {
    template.resourceCountIs('AWS::EC2::FlowLog', 1);
    template.hasResourceProperties('AWS::EC2::FlowLog', {
      TrafficType: 'ALL',
      ResourceType: 'VPC',
    });
    template.hasResourceProperties('AWS::Logs::LogGroup', {
      LogGroupName: '/aws/vpc/stratoclave-flow-logs',
      RetentionInDays: 30,
    });
  });

  test('ALB SG ingress is restricted to the CloudFront origin-facing prefix list (HTTP only, not 0.0.0.0/0)', () => {
    const resources = template.toJSON().Resources;
    const albSg: any = Object.values(resources).find(
      (r: any) =>
        r.Type === 'AWS::EC2::SecurityGroup' &&
        r.Properties.GroupName === 'stratoclave-alb-sg',
    );
    expect(albSg).toBeDefined();

    // No inline world-open ingress rules must survive on the ALB SG.
    const inline = albSg.Properties.SecurityGroupIngress || [];
    for (const rule of inline) {
      expect(rule.CidrIp).not.toBe('0.0.0.0/0');
      expect(rule.CidrIpv6).not.toBe('::/0');
    }

    // Only port 80 is needed — CloudFront connects to the ALB origin with
    // originProtocolPolicy=http-only, so a :443 ingress would be unused and
    // double the SG rule count (each prefix-list ingress expands into N
    // rules, one per CIDR in the managed list).
    const ingressRules = Object.values(resources).filter(
      (r: any) => r.Type === 'AWS::EC2::SecurityGroupIngress',
    );
    const httpRule: any = ingressRules.find((r: any) => r.Properties.FromPort === 80);
    const tlsRule = ingressRules.find((r: any) => r.Properties.FromPort === 443);
    expect(httpRule).toBeDefined();
    expect(tlsRule).toBeUndefined();
    expect(httpRule.Properties.SourcePrefixListId).toBeDefined();
    expect(httpRule.Properties.CidrIp).toBeUndefined();
  });

  test('ECS SG only accepts port 8000 from the ALB SG', () => {
    template.hasResourceProperties('AWS::EC2::SecurityGroupIngress', {
      FromPort: 8000,
      ToPort: 8000,
      IpProtocol: 'tcp',
      SourceSecurityGroupId: Match.anyValue(),
    });
  });

  test('VPC ID is published as a CFN output', () => {
    template.hasOutput('VpcId', {});
  });

  test('Network parameters are written to SSM Parameter Store', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/stratoclave/network/alb-sg-id',
      Type: 'String',
    });
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/stratoclave/network/ecs-sg-id',
      Type: 'String',
    });
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/stratoclave/network/vpc-id',
      Type: 'String',
    });
  });
});
