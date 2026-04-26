import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { RedisStack } from '../lib/redis-stack';

describe('RedisStack', () => {
  let app: cdk.App;
  let vpc: ec2.IVpc;
  let securityGroup: ec2.ISecurityGroup;
  let stack: RedisStack;
  let template: Template;

  beforeAll(() => {
    app = new cdk.App();

    const networkStack = new cdk.Stack(app, 'TestNetworkStack', {
      env: { account: '123456789012', region: 'us-west-2' },
    });

    vpc = new ec2.Vpc(networkStack, 'TestVpc', {
      maxAzs: 2,
      natGateways: 1,
    });

    securityGroup = new ec2.SecurityGroup(networkStack, 'TestSG', {
      vpc,
      description: 'Test ECS Security Group',
    });

    stack = new RedisStack(app, 'TestRedisStack', {
      env: { account: '123456789012', region: 'us-west-2' },
      vpc,
      ecsSecurityGroup: securityGroup,
    });

    template = Template.fromStack(stack);
  });

  // RED-01: ElastiCache Cluster が Redis 7.1 で作成されること (P0)
  test('ElastiCache Cluster が Redis で作成されること', () => {
    template.hasResourceProperties('AWS::ElastiCache::CacheCluster', {
      Engine: 'redis',
      EngineVersion: Match.stringLikeRegexp('^7'),
      CacheNodeType: Match.anyValue(),
    });
  });

  // RED-03: SG: TCP 6379 from ECS SG のみ (P0)
  test('Redis Security Group が ECS SG からのみポート 6379 を許可すること', () => {
    template.hasResourceProperties('AWS::EC2::SecurityGroup', {
      GroupDescription: Match.stringLikeRegexp('[Rr]edis'),
      SecurityGroupIngress: [
        {
          IpProtocol: 'tcp',
          FromPort: 6379,
          ToPort: 6379,
          SourceSecurityGroupId: Match.anyValue(),
        },
      ],
    });
  });

  // RED-04: Transit 暗号化 (TLS) が有効 (P0)
  test('Redis で Transit 暗号化が有効であること', () => {
    template.hasResourceProperties('AWS::ElastiCache::CacheCluster', {
      TransitEncryptionEnabled: true,
    });
  });
});
