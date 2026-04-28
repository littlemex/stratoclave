import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { AlbStack } from '../lib/alb-stack';

describe('AlbStack', () => {
  let app: cdk.App;
  let vpc: ec2.IVpc;
  let securityGroup: ec2.ISecurityGroup;
  let stack: AlbStack;
  let template: Template;

  beforeAll(() => {
    app = new cdk.App();

    // Create a minimal VPC and Security Group for testing
    const networkStack = new cdk.Stack(app, 'TestNetworkStack', {
      env: { account: '123456789012', region: 'us-west-2' },
    });

    vpc = new ec2.Vpc(networkStack, 'TestVpc', {
      maxAzs: 2,
      natGateways: 1,
    });

    securityGroup = new ec2.SecurityGroup(networkStack, 'TestSG', {
      vpc,
      description: 'Test Security Group',
    });

    stack = new AlbStack(app, 'TestAlbStack', {
      env: { account: '123456789012', region: 'us-west-2' },
      prefix: 'stratoclave',
      vpc,
      securityGroup,
      internal: false,
    });

    template = Template.fromStack(stack);
  });

  // ALB-01: ALB が Internet-facing で作成されること (P0)
  test('ALB が Internet-facing で作成されること', () => {
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
      Name: 'stratoclave-alb',
      Scheme: 'internet-facing',
      Type: 'application',
    });
  });

  // ALB-02: Target Group のヘルスチェック設定 (path=/health, interval=30s) (P1)
  test('Target Group のヘルスチェック設定が正しいこと', () => {
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::TargetGroup', {
      Name: 'stratoclave-backend-tg',
      Port: 8000,
      Protocol: 'HTTP',
      TargetType: 'ip',
      HealthCheckIntervalSeconds: 30,
      HealthCheckPath: '/health',
      HealthCheckProtocol: 'HTTP',
      HealthCheckTimeoutSeconds: 5,
      HealthyThresholdCount: 2,
      UnhealthyThresholdCount: 3,
    });
  });

  // ALB-03: HTTP Listener (port 80) が作成されること (P0)
  test('HTTP Listener (port 80) が作成されること', () => {
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
      Port: 80,
      Protocol: 'HTTP',
    });
  });

  test('ALB DNS name and ARN are exported as CFN outputs', () => {
    template.hasOutput('AlbDnsName', {});
    template.hasOutput('AlbArn', {});
  });

  test('Target group ARN is published to SSM parameter store (replaces old CfnOutput)', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/stratoclave/alb/target-group-arn',
      Type: 'String',
    });
  });
});
