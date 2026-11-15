import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { Template } from 'aws-cdk-lib/assertions';
import { EcsStack } from '../lib/ecs-stack';

/**
 * `EcsStackProps.additionalTargetGroup` — set by `bin/iac.ts` to
 * `lib/alb-stack.ts`'s `additionalTargetGroup` under
 * `STRATOCLAVE_CLOUDFRONT_ORIGIN=vpc-expand`/`vpc-switch`, when both the
 * public and the internal ALB exist and the service must be healthy behind
 * both at once.
 *
 * Mutation check: deleting the `if (props.additionalTargetGroup)` /
 * second `attachToApplicationTargetGroup` call in `lib/ecs-stack.ts` drops
 * the service back to ONE `LoadBalancers` entry, which the first test here
 * catches.
 */
describe('EcsStack: registering the service with a second target group', () => {
  function buildDeps(idPrefix: string) {
    const app = new cdk.App();
    const networkStack = new cdk.Stack(app, `${idPrefix}Network`, {
      env: { account: '123456789012', region: 'us-west-2' },
    });
    const vpc = new ec2.Vpc(networkStack, 'TestVpc', { maxAzs: 2, natGateways: 0 });
    const securityGroup = new ec2.SecurityGroup(networkStack, 'TestSG', { vpc });
    const repository = ecr.Repository.fromRepositoryName(
      networkStack,
      'TestRepo',
      'stratoclave-backend',
    );
    const alb = new elbv2.ApplicationLoadBalancer(networkStack, 'TestAlb', {
      vpc,
      internetFacing: true,
    });
    const targetGroup = new elbv2.ApplicationTargetGroup(networkStack, 'TestTg', {
      vpc,
      port: 8000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
    });
    const internalAlb = new elbv2.ApplicationLoadBalancer(networkStack, 'TestInternalAlb', {
      vpc,
      internetFacing: false,
    });
    const additionalTargetGroup = new elbv2.ApplicationTargetGroup(networkStack, 'TestTg2', {
      vpc,
      port: 8000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
    });
    void alb;
    void internalAlb;
    return { app, vpc, securityGroup, repository, targetGroup, additionalTargetGroup };
  }

  const baseEcsProps = {
    userPoolArn: 'arn:aws:cognito-idp:us-west-2:123456789012:userpool/us-west-2_testpool',
    dynamoDbTableArns: ['arn:aws:dynamodb:us-west-2:123456789012:table/stratoclave-users'],
    cpu: 256,
    memory: 512,
    desiredCount: 1,
    environment: { DATABASE_TYPE: 'dynamodb', AUTH_MODE: 'cognito' },
  };

  test('with additionalTargetGroup: the ECS Service has TWO LoadBalancers entries', () => {
    const { app, vpc, securityGroup, repository, targetGroup, additionalTargetGroup } =
      buildDeps('WithSecond');
    const stack = new EcsStack(app, 'TestEcsStackTwo', {
      env: { account: '123456789012', region: 'us-west-2' },
      prefix: 'stratoclave',
      vpc,
      securityGroup,
      repository,
      targetGroup,
      additionalTargetGroup,
      ...baseEcsProps,
    });
    const template = Template.fromStack(stack);
    const services = template.findResources('AWS::ECS::Service');
    const svc = Object.values(services)[0] as any;
    expect(svc.Properties.LoadBalancers).toHaveLength(2);
    // Cross-stack references render as Fn::ImportValue (the target groups
    // live in the test's network stack, not this one) — compare the whole
    // TargetGroupArn value, not a `.Ref` that only a same-stack reference
    // would have.
    const targetGroupRefs = svc.Properties.LoadBalancers.map((lb: any) =>
      JSON.stringify(lb.TargetGroupArn),
    );
    expect(new Set(targetGroupRefs).size).toBe(2); // two DISTINCT target groups, not the same one twice
  });

  test('without additionalTargetGroup (every mode but vpc-expand/vpc-switch): exactly ONE LoadBalancers entry', () => {
    const { app, vpc, securityGroup, repository, targetGroup } = buildDeps('WithoutSecond');
    const stack = new EcsStack(app, 'TestEcsStackOne', {
      env: { account: '123456789012', region: 'us-west-2' },
      prefix: 'stratoclave',
      vpc,
      securityGroup,
      repository,
      targetGroup,
      ...baseEcsProps,
    });
    const template = Template.fromStack(stack);
    const services = template.findResources('AWS::ECS::Service');
    const svc = Object.values(services)[0] as any;
    expect(svc.Properties.LoadBalancers).toHaveLength(1);
  });
});
