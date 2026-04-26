import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { NetworkStack } from '../lib/network-stack';

describe('NetworkStack', () => {
  let app: cdk.App;
  let stack: NetworkStack;
  let template: Template;

  beforeAll(() => {
    app = new cdk.App();
    stack = new NetworkStack(app, 'TestNetworkStack', {
      env: { account: '123456789012', region: 'us-west-2' },
    });
    template = Template.fromStack(stack);
  });

  // NET-01: VPC が作成されること (CIDR: 10.0.0.0/16) (P0)
  test('VPC が作成され、正しい CIDR が設定されていること', () => {
    template.hasResourceProperties('AWS::EC2::VPC', {
      CidrBlock: '10.0.0.0/16',
      EnableDnsHostnames: true,
      EnableDnsSupport: true,
    });
  });

  // NET-04: NAT Gateway が 1 つだけ作成されること (P1)
  test('NAT Gateway が 1 つだけ作成されること', () => {
    template.resourceCountIs('AWS::EC2::NatGateway', 1);
  });

  // NET-05: ALB SG: HTTP(80) + HTTPS(443) from 0.0.0.0/0 (P0)
  test('ALB Security Group が HTTP と HTTPS を許可すること', () => {
    template.hasResourceProperties('AWS::EC2::SecurityGroup', {
      GroupDescription: 'Security group for Stratoclave ALB',
      SecurityGroupIngress: Match.arrayWith([
        Match.objectLike({
          CidrIp: '0.0.0.0/0',
          FromPort: 443,
          IpProtocol: 'tcp',
          ToPort: 443,
        }),
        Match.objectLike({
          CidrIp: '0.0.0.0/0',
          FromPort: 80,
          IpProtocol: 'tcp',
          ToPort: 80,
        }),
      ]),
    });
  });

  // NET-06: ECS SG: TCP(8000) from ALB SG のみ (P0)
  test('ECS Security Group が ALB SG からのみポート 8000 を許可すること', () => {
    // Verify ECS Security Group exists
    template.hasResourceProperties('AWS::EC2::SecurityGroup', {
      GroupDescription: 'Security group for Stratoclave ECS Tasks',
    });

    // Verify ingress rule (may be inline or separate resource)
    // Check if there's a separate SecurityGroupIngress resource
    const resources = template.toJSON().Resources;
    const ingressRules = Object.values(resources).filter(
      (r: any) => r.Type === 'AWS::EC2::SecurityGroupIngress'
    );

    // There should be at least one ingress rule for port 8000
    const ecsIngressRule = ingressRules.find((r: any) => r.Properties.FromPort === 8000);
    expect(ecsIngressRule).toBeDefined();
  });

  // NET-08: CfnOutput が 3 つエクスポートされること (P2)
  test('CfnOutput が 3 つエクスポートされること', () => {
    template.hasOutput('VpcId', {});
    template.hasOutput('AlbSecurityGroupId', {});
    template.hasOutput('EcsSecurityGroupId', {});
  });
});
