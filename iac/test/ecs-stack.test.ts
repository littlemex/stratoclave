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
  let targetGroup: elbv2.ApplicationTargetGroup;
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

    const _alb = new elbv2.ApplicationLoadBalancer(networkStack, 'TestALB', {
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
      prefix: 'stratoclave',
      vpc,
      securityGroup,
      repository,
      targetGroup,
      userPoolArn:
        'arn:aws:cognito-idp:us-west-2:123456789012:userpool/us-west-2_testpool',
      dynamoDbTableArns: [
        'arn:aws:dynamodb:us-west-2:123456789012:table/stratoclave-users',
      ],
      cpu: 256,
      memory: 512,
      desiredCount: 1,
      environment: {
        DATABASE_TYPE: 'dynamodb',
        AUTH_MODE: 'cognito',
      },
    });

    template = Template.fromStack(stack);
  });

  // ECS-01: ECS Cluster is created with Container Insights enabled (P0)
  test('ECS Cluster is created with Container Insights enabled', () => {
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
  test('Fargate Task Definition is created with the correct CPU and memory', () => {
    template.hasResourceProperties('AWS::ECS::TaskDefinition', {
      Family: 'stratoclave-backend',
      Cpu: '256',
      Memory: '512',
      NetworkMode: 'awsvpc',
      RequiresCompatibilities: ['FARGATE'],
    });
  });

  // v2.1: ECS Fargate runs on public subnets (no NAT), so assignPublicIp=ENABLED
  test('Fargate Service runs on public subnets with desiredCount=1', () => {
    template.hasResourceProperties('AWS::ECS::Service', {
      ServiceName: 'stratoclave-backend',
      DesiredCount: 1,
      LaunchType: 'FARGATE',
      NetworkConfiguration: {
        AwsvpcConfiguration: {
          AssignPublicIp: 'ENABLED',
        },
      },
    });
  });

  test('Task Role has Bedrock invocation permissions', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'bedrock:InvokeModel',
              'bedrock:InvokeModelWithResponseStream',
              'bedrock:Converse',
              'bedrock:ConverseStream',
            ]),
            Effect: 'Allow',
          }),
        ]),
      },
    });
  });

  // ECS-06: CloudWatch LogGroup retention bumped to 90 days (2026-06
  // hardening, A-08-log) so that incident forensics span the typical
  // SOC2/ISO27001 audit window. RemovalPolicy is RETAIN so a stack
  // rebuild does not erase the audit trail.
  test('CloudWatch LogGroup is configured correctly', () => {
    template.hasResourceProperties('AWS::Logs::LogGroup', {
      LogGroupName: '/ecs/stratoclave-backend',
      RetentionInDays: 90,
    });
    template.hasResource('AWS::Logs::LogGroup', {
      DeletionPolicy: 'Retain',
      UpdateReplacePolicy: 'Retain',
    });
  });

  test('ClusterName and ServiceName are exported as CFN outputs', () => {
    template.hasOutput('ClusterName', {});
    template.hasOutput('ServiceName', {});
  });
});

// Multi-task (desiredCount>1) + autoscaling: the CFN template must OMIT
// DesiredCount (so deploys don't reset the count / snap the fleet down
// mid-incident) and floor the scalable target at MinCapacity=baseCount.
describe('EcsStack multi-task/autoscaling', () => {
  function synth(
    desiredCount: number,
    autoScaling?: {
      maxCapacity?: number;
      requestsPerTarget?: number;
      cpuTargetPercent?: number;
    },
  ): Template {
    const app = new cdk.App();
    const net = new cdk.Stack(app, 'Net', { env: { account: '123456789012', region: 'us-west-2' } });
    const vpc = new ec2.Vpc(net, 'Vpc', { maxAzs: 2 });
    const sg = new ec2.SecurityGroup(net, 'Sg', { vpc });
    const repo = ecr.Repository.fromRepositoryName(net, 'Repo', 'stratoclave-backend');
    const tg = new elbv2.ApplicationTargetGroup(net, 'Tg', {
      vpc, port: 8000, protocol: elbv2.ApplicationProtocol.HTTP, targetType: elbv2.TargetType.IP,
    });
    // Request-count scaling reads the ALB dimension off the target group, which
    // only exists once a listener has attached it — as the real stack does.
    const alb = new elbv2.ApplicationLoadBalancer(net, 'Alb', { vpc, internetFacing: true });
    alb.addListener('Listener', {
      port: 80,
      defaultAction: elbv2.ListenerAction.forward([tg]),
    });
    const stack = new EcsStack(app, `Ecs${desiredCount}${autoScaling ? 'Scaled' : ''}`, {
      env: { account: '123456789012', region: 'us-west-2' },
      prefix: 'stratoclave', vpc, securityGroup: sg, repository: repo, targetGroup: tg,
      userPoolArn: 'arn:aws:cognito-idp:us-west-2:123456789012:userpool/us-west-2_t',
      dynamoDbTableArns: ['arn:aws:dynamodb:us-west-2:123456789012:table/stratoclave-users'],
      cpu: 256, memory: 512, desiredCount, autoScaling,
      environment: { DATABASE_TYPE: 'dynamodb', AUTH_MODE: 'cognito' },
    });
    return Template.fromStack(stack);
  }

  test('desiredCount=2 omits DesiredCount from the service template', () => {
    const t = synth(2);
    const services = t.findResources('AWS::ECS::Service');
    const props = Object.values(services)[0].Properties;
    expect(props.DesiredCount).toBeUndefined();
  });

  test('scalable target is floored at MinCapacity=2', () => {
    const t = synth(2);
    t.hasResourceProperties('AWS::ApplicationAutoScaling::ScalableTarget', {
      MinCapacity: 2,
      MaxCapacity: 4,
    });
  });

  test('desiredCount=1 keeps DesiredCount (no override, pinned 1..1)', () => {
    const t = synth(1);
    t.hasResourceProperties('AWS::ECS::Service', { DesiredCount: 1 });
  });

  test('request-count tracking is the primary policy when a budget is given', () => {
    // CPU average stays low on this workload while tasks are already saturated
    // waiting upstream, so offered load per target is the signal that reacts.
    const t = synth(2, { maxCapacity: 8, requestsPerTarget: 120 });
    t.hasResourceProperties('AWS::ApplicationAutoScaling::ScalingPolicy', {
      TargetTrackingScalingPolicyConfiguration: Match.objectLike({
        TargetValue: 120,
        PredefinedMetricSpecification: Match.objectLike({
          PredefinedMetricType: 'ALBRequestCountPerTarget',
        }),
      }),
    });
  });

  test('scaling in is slower than scaling out', () => {
    // Pulling a task while load is still arriving costs a cold start before the
    // replacement serves anything, so the two cooldowns must not be equal.
    const t = synth(2, { maxCapacity: 8, requestsPerTarget: 120 });
    const policies = Object.values(
      t.findResources('AWS::ApplicationAutoScaling::ScalingPolicy'),
    );
    expect(policies.length).toBeGreaterThan(0);
    for (const policy of policies) {
      const config = policy.Properties.TargetTrackingScalingPolicyConfiguration;
      expect(config.ScaleInCooldown).toBeGreaterThan(config.ScaleOutCooldown);
    }
  });

  test('a request budget on a fixed-size service registers no request policy', () => {
    // A policy that cannot act still reports as configured, which reads as
    // protection that is not there.
    const t = synth(1, { maxCapacity: 1, requestsPerTarget: 120 });
    const policies = Object.values(
      t.findResources('AWS::ApplicationAutoScaling::ScalingPolicy'),
    );
    for (const policy of policies) {
      const spec =
        policy.Properties.TargetTrackingScalingPolicyConfiguration
          ?.PredefinedMetricSpecification;
      expect(spec?.PredefinedMetricType).not.toBe('ALBRequestCountPerTarget');
    }
  });

  test('cpu tracking remains as the secondary signal', () => {
    const t = synth(2, { maxCapacity: 8, requestsPerTarget: 120 });
    t.hasResourceProperties('AWS::ApplicationAutoScaling::ScalingPolicy', {
      TargetTrackingScalingPolicyConfiguration: Match.objectLike({
        PredefinedMetricSpecification: Match.objectLike({
          PredefinedMetricType: 'ECSServiceAverageCPUUtilization',
        }),
      }),
    });
  });

  test('the task ceiling is what the concurrency target is made of', () => {
    const t = synth(2, { maxCapacity: 8, requestsPerTarget: 120 });
    t.hasResourceProperties('AWS::ApplicationAutoScaling::ScalableTarget', {
      MinCapacity: 2,
      MaxCapacity: 8,
    });
  });

  test('a ceiling below the floor is a configuration error, not a policy', () => {
    // Application Auto Scaling would accept the template and then behave in a way
    // nobody intended, so this has to fail at synth.
    expect(() => synth(2, { maxCapacity: 1, requestsPerTarget: 120 })).toThrow(
      /maxCapacity .* must be >= desiredCount/,
    );
  });
});
