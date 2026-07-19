import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { EcsStack } from '../lib/ecs-stack';

/**
 * PENDING-protocol reserve canary + pool-item-size observability
 * (docs/design/pending-protocol.md, PR-1 item A' + rollout).
 *
 *  - reserveProtocolCanaryTenants set => the backend container gets
 *    STRATOCLAVE_RESERVE_PROTOCOL_TENANTS="t1,t2"; absent => NO such env var
 *    (feature ships dark, every tenant stays transaction-mode);
 *  - the pool-item-size gauge metric-filter + growth alarm always exist (the
 *    detector that a code regression reintroduced per-hold growth on the hot
 *    pool item), plus the reconcile-invariant alarm.
 */
function synth(canary?: string[]): Template {
  const app = new cdk.App();
  const net = new cdk.Stack(app, 'Net', { env: { account: '123456789012', region: 'us-west-2' } });
  const vpc = new ec2.Vpc(net, 'Vpc', { maxAzs: 2, natGateways: 1 });
  const sg = new ec2.SecurityGroup(net, 'Sg', { vpc, description: 'x' });
  const repo = ecr.Repository.fromRepositoryName(net, 'Repo', 'stratoclave-backend');
  const alb = new elbv2.ApplicationLoadBalancer(net, 'Alb', { vpc, internetFacing: true });
  const tg = new elbv2.ApplicationTargetGroup(net, 'Tg', {
    vpc, port: 8000, protocol: elbv2.ApplicationProtocol.HTTP, targetType: elbv2.TargetType.IP,
  });
  const stack = new EcsStack(app, `Ecs${(canary || []).join('-') || 'none'}`, {
    env: { account: '123456789012', region: 'us-west-2' },
    prefix: 'stratoclave',
    vpc, securityGroup: sg, repository: repo, targetGroup: tg,
    userPoolArn: 'arn:aws:cognito-idp:us-west-2:123456789012:userpool/us-west-2_p',
    dynamoDbTableArns: ['arn:aws:dynamodb:us-west-2:123456789012:table/stratoclave-users'],
    environment: { DATABASE_TYPE: 'dynamodb' },
    reserveProtocolCanaryTenants: canary,
  });
  return Template.fromStack(stack);
}

describe('EcsStack reserve-protocol canary', () => {
  test('canary tenant list is injected as STRATOCLAVE_RESERVE_PROTOCOL_TENANTS', () => {
    synth(['tenant-a', 'tenant-b']).hasResourceProperties('AWS::ECS::TaskDefinition', {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            { Name: 'STRATOCLAVE_RESERVE_PROTOCOL_TENANTS', Value: 'tenant-a,tenant-b' },
          ]),
        }),
      ]),
    });
  });

  test('dark ship: no canary list => no STRATOCLAVE_RESERVE_PROTOCOL_TENANTS env var', () => {
    const t = synth(undefined);
    const tds = t.findResources('AWS::ECS::TaskDefinition');
    const json = JSON.stringify(tds);
    expect(json).not.toContain('STRATOCLAVE_RESERVE_PROTOCOL_TENANTS');
  });

  test('empty canary list => still dark (no env var)', () => {
    const json = JSON.stringify(synth([]).findResources('AWS::ECS::TaskDefinition'));
    expect(json).not.toContain('STRATOCLAVE_RESERVE_PROTOCOL_TENANTS');
  });

  test('pool-item-size gauge metric-filter + growth alarm exist', () => {
    const t = synth(['tenant-a']);
    // metric filter turns the pool_item_size log line's size_bytes into a gauge.
    t.hasResourceProperties('AWS::Logs::MetricFilter', {
      MetricTransformations: Match.arrayWith([
        Match.objectLike({ MetricName: 'PoolItemSizeBytes', MetricValue: '$.size_bytes' }),
      ]),
    });
    // alarm fires when the pool item grows past its small/flat ceiling.
    t.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: 'stratoclave-PoolItemSizeBytes',
      Threshold: 2048,
      ComparisonOperator: 'GreaterThanThreshold',
    });
  });

  test('reconcile credit-back invariant alarm exists', () => {
    synth(['tenant-a']).hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: 'stratoclave-PoolReconcileCreditBackInvariant',
      ComparisonOperator: 'GreaterThanThreshold',
      Threshold: 0,
    });
  });
});
