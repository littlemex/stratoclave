import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { RdsStack } from '../lib/rds-stack';

describe('RdsStack', () => {
  let app: cdk.App;
  let vpc: ec2.IVpc;
  let securityGroup: ec2.ISecurityGroup;
  let stack: RdsStack;
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

    stack = new RdsStack(app, 'TestRdsStack', {
      env: { account: '123456789012', region: 'us-west-2' },
      vpc,
      ecsSecurityGroup: securityGroup,
    });

    template = Template.fromStack(stack);
  });

  // RDS-01: RDS Instance が PostgreSQL 15 で作成されること (P0)
  test('RDS Instance が PostgreSQL で作成されること', () => {
    template.hasResourceProperties('AWS::RDS::DBInstance', {
      Engine: 'postgres',
      EngineVersion: Match.stringLikeRegexp('^15'),
      DBInstanceClass: Match.anyValue(),
      AllocatedStorage: Match.anyValue(),
    });
  });

  // RDS-03: SG: TCP 5432 from ECS SG のみ (P0)
  test('RDS Security Group が ECS SG からのみポート 5432 を許可すること', () => {
    template.hasResourceProperties('AWS::EC2::SecurityGroup', {
      GroupDescription: Match.stringLikeRegexp('[Rr][Dd][Ss]'),
      SecurityGroupIngress: [
        {
          IpProtocol: 'tcp',
          FromPort: 5432,
          ToPort: 5432,
          SourceSecurityGroupId: Match.anyValue(),
        },
      ],
    });
  });

  // RDS-04: ストレージ暗号化が有効 (KMS) (P0)
  test('RDS Instance でストレージ暗号化が有効であること', () => {
    template.hasResourceProperties('AWS::RDS::DBInstance', {
      StorageEncrypted: true,
    });
  });
});
