import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { EcsStack } from '../lib/ecs-stack';

/**
 * Retention exposure alarms — C8.3's missing watcher.
 *
 * `STRATOCLAVE_UNOBSERVED_HOLDS` defaults ON, so a reservation whose provider call
 * departed and whose outcome was never observed is HELD rather than returned. That record
 * is correct — an abandoned Bedrock call is billed for the full generation — but the
 * failure mode moved: retentions accumulate against a tenant's headroom and, without
 * these alarms, the first signal an operator gets is a refusal for an unrelated request.
 *
 * What is checked here is the half that lives in infrastructure, and specifically the two
 * ways it can be silently wrong:
 *
 *  - the metric filters read the field NAMES the backend emits. A rename on either side
 *    leaves the filter matching nothing, and an alarm that never receives a datapoint sits
 *    green forever on `treatMissingData: NOT_BREACHING`. Invisible from both sides, so the
 *    names are pinned here AND in
 *    `backend/tests/test_retention_exposure.py::test_the_field_names_the_alarms_read_are_pinned`.
 *  - the alarms' evaluation windows have to be reachable given how often the backend
 *    emits. A 5-minute period with 3/3 on a line emitted once per minute is fine; the
 *    reverse mistake (a slow line with a fast window) is what made an earlier alarm in
 *    this stack structurally unable to reach ALARM.
 */
function synth(): Template {
  const app = new cdk.App();
  const net = new cdk.Stack(app, 'Net', { env: { account: '123456789012', region: 'us-west-2' } });
  const vpc = new ec2.Vpc(net, 'Vpc', { maxAzs: 2, natGateways: 1 });
  const sg = new ec2.SecurityGroup(net, 'Sg', { vpc, description: 'x' });
  const repo = ecr.Repository.fromRepositoryName(net, 'Repo', 'stratoclave-backend');
  const alb = new elbv2.ApplicationLoadBalancer(net, 'Alb', { vpc, internetFacing: true });
  const tg = new elbv2.ApplicationTargetGroup(net, 'Tg', {
    vpc, port: 8000, protocol: elbv2.ApplicationProtocol.HTTP, targetType: elbv2.TargetType.IP,
  });
  const stack = new EcsStack(app, 'EcsRetention', {
    env: { account: '123456789012', region: 'us-west-2' },
    prefix: 'stratoclave',
    vpc, securityGroup: sg, repository: repo, targetGroup: tg,
    userPoolArn: 'arn:aws:cognito-idp:us-west-2:123456789012:userpool/us-west-2_p',
    dynamoDbTableArns: ['arn:aws:dynamodb:us-west-2:123456789012:table/stratoclave-users'],
    environment: { DATABASE_TYPE: 'dynamodb' },
  });
  return Template.fromStack(stack);
}

describe('retention exposure observability', () => {
  const template = synth();

  test('the three metric filters exist and read the fields the backend emits', () => {
    const filters = template.findResources('AWS::Logs::MetricFilter');
    const byMetricName: Record<string, any> = {};
    for (const res of Object.values(filters)) {
      for (const t of (res as any).Properties.MetricTransformations || []) {
        byMetricName[t.MetricName] = { transform: t, pattern: (res as any).Properties.FilterPattern };
      }
    }

    // The seam. These field names are what `mvp/retention_exposure.py` puts on the
    // `retention_exposure` line; a filter reading a name nobody emits matches nothing and
    // its alarm never leaves INSUFFICIENT_DATA.
    const expected: Array<[string, string]> = [
      ['RetentionHeldFraction', '$.held_fraction'],
      ['RetentionOldestAgeSeconds', '$.oldest_retention_age_seconds'],
      ['RetentionHeldMicroUsd', '$.held_microusd'],
    ];
    for (const [metricName, valueField] of expected) {
      expect(byMetricName[metricName]).toBeDefined();
      expect(byMetricName[metricName].transform.MetricValue).toEqual(valueField);
      // Scoped to the retention line, or an unrelated log line carrying a same-named
      // field would feed the gauge.
      expect(byMetricName[metricName].pattern).toContain('retention_exposure');
    }
  });

  test('the gauges carry no default value, so an unrelated line cannot drag a Maximum down', () => {
    const filters = template.findResources('AWS::Logs::MetricFilter');
    for (const res of Object.values(filters)) {
      for (const t of (res as any).Properties.MetricTransformations || []) {
        if (String(t.MetricName).startsWith('Retention')) {
          expect(t.DefaultValue).toBeUndefined();
        }
      }
    }
  });

  test('saturation alarms on the worst tenant, fast enough to matter', () => {
    template.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: 'stratoclave-RetentionHeldFraction',
      MetricName: 'RetentionHeldFraction',
      // Maximum, not Average: one saturated tenant is the incident. The metric carries no
      // tenant dimension on purpose (unbounded cardinality on a filter over every backend
      // log line), so Maximum IS the per-tenant view and the log line names which tenant.
      Statistic: 'Maximum',
      Threshold: 0.25,
      ComparisonOperator: 'GreaterThanThreshold',
      // A provider outage fills headroom in minutes, so 1-minute periods. The backend
      // emits at most once per minute per tenant per task while retentions exist, so 3/3
      // is reachable; 5-minute buckets would put the alarm 15 minutes behind the incident.
      Period: 60,
      EvaluationPeriods: 3,
      DatapointsToAlarm: 3,
      TreatMissingData: 'notBreaching',
    });
  });

  test('staleness alarms slowly, and is a separate alarm from saturation', () => {
    template.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: 'stratoclave-RetentionOldestAgeSeconds',
      MetricName: 'RetentionOldestAgeSeconds',
      Statistic: 'Maximum',
      Threshold: 48 * 60 * 60,
      ComparisonOperator: 'GreaterThanThreshold',
      Period: 3600,
      EvaluationPeriods: 1,
      TreatMissingData: 'notBreaching',
    });
  });

  test('held_microusd is a metric and NOT an alarm', () => {
    // Absolute exposure has no correct threshold: it depends on the pool it is held
    // against, which is what the fraction is for. Alarming on it would page on a large
    // tenant behaving normally and stay quiet on a small tenant being locked out.
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const onAbsolute = Object.values(alarms).filter(
      (a: any) => a.Properties.MetricName === 'RetentionHeldMicroUsd');
    expect(onAbsolute).toHaveLength(0);
  });

  test('missing data is not breaching, which is only honest because a live retention keeps reporting', () => {
    // No retentions means no log line means no datapoints, and that really is nothing to
    // report — but only because the backend re-emits the STANDING exposure from a sweep
    // while a retention is unresolved. Without that, silence would be ambiguous between
    // "resolved" and "still held", and NOT_BREACHING would clear a live exposure.
    // Asserted in `backend/tests/test_retention_exposure.py::
    // test_a_sweep_keeps_reporting_a_retention_nobody_resolved`.
    for (const name of ['stratoclave-RetentionHeldFraction', 'stratoclave-RetentionOldestAgeSeconds']) {
      template.hasResourceProperties('AWS::CloudWatch::Alarm', {
        AlarmName: name,
        TreatMissingData: 'notBreaching',
      });
    }
  });

  test('each alarm says what an operator should do, not just what fired', () => {
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const retention = Object.values(alarms).filter(
      (a: any) => String(a.Properties.AlarmName || '').includes('Retention'));
    expect(retention.length).toBe(2);
    for (const a of retention) {
      const description = String((a as any).Properties.AlarmDescription || '');
      // A retention ends only by an operator settling or releasing it; an alarm that does
      // not say so sends someone looking for a bug in the gateway.
      expect(description.toLowerCase()).toMatch(/settle|release/);
      expect(description.length).toBeGreaterThan(80);
    }
  });
});
