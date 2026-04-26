import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { EcsStack } from '../lib/ecs-stack';

describe('EcsStack', () => {
  let app: cdk.App;
  let vpc: ec2.IVpc;
  let securityGroup: ec2.ISecurityGroup;
  let repository: ecr.IRepository;
  let targetGroup: elbv2.IApplicationTargetGroup;
  let stack: EcsStack;
  let template: Template;

  beforeAll(() => {
    app = new cdk.App();

    // Create dependencies
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

    repository = ecr.Repository.fromRepositoryName(
      networkStack,
      'TestRepo',
      'stratoclave-backend'
    );

    const alb = new elbv2.ApplicationLoadBalancer(networkStack, 'TestALB', {
      vpc,
      internetFacing: true,
    });

    targetGroup = new elbv2.ApplicationTargetGroup(networkStack, 'TestTG', {
      vpc,
      port: 8000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
    });

    stack = new EcsStack(app, 'TestEcsStack', {
      env: { account: '123456789012', region: 'us-west-2' },
      vpc,
      securityGroup,
      repository,
      targetGroup,
      cpu: 256,
      memory: 512,
      desiredCount: 1,
      environment: {
        DATABASE_TYPE: 'sqlite',
        AUTH_MODE: 'apikey',
      },
    });

    template = Template.fromStack(stack);
  });

  // ECS-01: ECS Cluster が作成され、Container Insights が有効 (P0)
  test('ECS Cluster が作成され、Container Insights が有効であること', () => {
    template.hasResourceProperties('AWS::ECS::Cluster', {
      ClusterName: 'stratoclave-cluster',
      ClusterSettings: [
        {
          Name: 'containerInsights',
          Value: 'enabled',
        },
      ],
    });
  });

  // ECS-02: Fargate Task Definition (CPU=256, Memory=512) (P0)
  test('Fargate Task Definition が正しい CPU とメモリで作成されること', () => {
    template.hasResourceProperties('AWS::ECS::TaskDefinition', {
      Family: 'stratoclave-backend',
      Cpu: '256',
      Memory: '512',
      NetworkMode: 'awsvpc',
      RequiresCompatibilities: ['FARGATE'],
    });
  });

  // ECS-03: Fargate Service (desiredCount=1, assignPublicIp=false) (P0)
  test('Fargate Service が Private Subnet に配置されること', () => {
    template.hasResourceProperties('AWS::ECS::Service', {
      ServiceName: 'stratoclave-backend',
      DesiredCount: 1,
      LaunchType: 'FARGATE',
      NetworkConfiguration: {
        AwsvpcConfiguration: {
          AssignPublicIp: 'DISABLED',
        },
      },
    });
  });

  // ECS-04: Task Role に Bedrock 権限 (InvokeModel, Converse 等) (P0)
  test('Task Role に Bedrock 権限があること', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          {
            Action: [
              'bedrock:InvokeModel',
              'bedrock:InvokeModelWithResponseStream',
              'bedrock:Converse',
              'bedrock:ConverseStream',
            ],
            Effect: 'Allow',
            Resource: '*',
          },
        ]),
      },
    });
  });

  // ECS-06: CloudWatch LogGroup (/ecs/stratoclave-backend, 1 週間保持) (P1)
  test('CloudWatch LogGroup が正しく設定されていること', () => {
    template.hasResourceProperties('AWS::Logs::LogGroup', {
      LogGroupName: '/ecs/stratoclave-backend',
      RetentionInDays: 7,
    });
  });

  // ECS-10: CfnOutput が 3 つエクスポートされること (P2)
  test('CfnOutput が 3 つエクスポートされること', () => {
    template.hasOutput('ClusterName', {});
    template.hasOutput('ServiceName', {});
    template.hasOutput('LogGroupName', {});
  });
});
