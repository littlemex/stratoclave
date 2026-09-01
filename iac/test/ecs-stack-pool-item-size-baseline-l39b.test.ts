import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { Template } from 'aws-cdk-lib/assertions';
import { EcsStack } from '../lib/ecs-stack';

/**
 * F4 / R39b — `PoolItemSizeGrowth`'s threshold must be SCHEMA-AWARE, not a bare number.
 *
 * WHAT THIS CATCHES
 *
 * `ecs-stack.ts`'s `PoolItemSizeGrowth` alarm reads today:
 *
 *     threshold: 2048,   // "2KB is a generous ceiling..."
 *
 * That number knows nothing about which attributes the pool item is declared to carry.
 * The quota-raise epic deletes `sizing` and adds THREE attributes to the same item —
 * `seat_count`, `manual_limit`, `pool_granted`, PLUS the stored seat rate (amendment A5,
 * seeded at M1, carried forward by R16 — CONTRACT-F4-claims (F4's contract document)'s "Seam amendments" B2
 * corrects an earlier count of two) — an INTENDED size change. R39b's whole point is
 * that an intended change like this must not need a human to also remember to bump
 * `2048` by hand (and no test currently would catch it if they forgot, because a static
 * threshold is not wrong until the item is bigger than 2048 bytes, which this epic's own
 * attributes do not come close to — so the current 2048 silently tolerates ANY schema
 * change up to that ceiling, intended or not, which is the OTHER failure mode R39b
 * names: "an unintended one still is [an alarm]").
 *
 * WHAT IS DESIGNED (the F4 design note section 3, corrected by amendment B1) but not
 * implemented by this contract
 *
 * `CONTRACT-F4-claims (F4's contract document)` amendment B1 (`SEAMS (the integration owner's seam-review document)` S1) reassigns the declared-attribute
 * list itself to F1: F1 ships a CLOSED-WORLD schema declaration for the pool row
 * (proposed module `backend/dynamo/pool_row_schema.py` — an earlier draft of this test
 * named `POOL_ITEM_DECLARED_ATTRIBUTES` in `backend/dynamo/tenant_budgets.py` instead,
 * which was F4 inventing its own copy of a declaration that belongs to the part that
 * OWNS the schema). The CDK stack's alarm threshold is DERIVED from that same
 * declaration — via a second emitted metric (`PoolItemSizeBaselineBytes`) and a
 * metric-math ratio alarm, so the threshold moves the moment F1's declaration changes,
 * with no `ecs-stack.ts` edit and no redeploy required for the schema-aware part. The
 * mechanics asserted below (a second metric, a non-literal threshold, a threshold that
 * moves when the schema does) do not depend on which module ships the declaration, only
 * on there BEING one that both the alarm and the document (see
 * `backend/tests/test_ledger_hot_path_flatness_claim_l39a.py`) derive from.
 *
 * WHY THIS FAILS TODAY
 *
 * None of that exists. The threshold is exactly `2048`, typed into `ecs-stack.ts`, and
 * there is no second metric filter for a baseline at all — so every assertion below
 * about a schema-derived threshold fails on the shipped stack.
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
  const stack = new EcsStack(app, 'EcsPoolItemSize', {
    env: { account: '123456789012', region: 'us-west-2' },
    prefix: 'stratoclave',
    vpc, securityGroup: sg, repository: repo, targetGroup: tg,
    userPoolArn: 'arn:aws:cognito-idp:us-west-2:123456789012:userpool/us-west-2_p',
    dynamoDbTableArns: ['arn:aws:dynamodb:us-west-2:123456789012:table/stratoclave-users'],
    environment: { DATABASE_TYPE: 'dynamodb' },
  });
  return Template.fromStack(stack);
}

describe('pool item size alarm is schema-aware (F4 R39b)', () => {
  const template = synth();

  test('a second metric filter emits the schema-derived baseline, not just the raw size', () => {
    // The backend's `pool_item_size` log line must ALSO carry `baseline_bytes` (design
    // note section 3), and a second MetricFilter must project it as
    // `PoolItemSizeBaselineBytes` so the alarm below can reference it. Fails today:
    // only ONE metric filter exists on the `pool_item_size` event (`PoolItemSizeBytes`).
    const filters = template.findResources('AWS::Logs::MetricFilter');
    const metricNames = new Set<string>();
    for (const res of Object.values(filters)) {
      for (const t of (res as any).Properties.MetricTransformations || []) {
        metricNames.add(t.MetricName);
      }
    }
    expect(metricNames.has('PoolItemSizeBaselineBytes')).toBe(true);
  });

  test('the alarm threshold is not the bare literal 2048', () => {
    // A schema-aware threshold is expressed as a metric-math ratio/anomaly expression
    // (design note section 3, option (a)) or at minimum computed from a named constant
    // that traces to the declared-attribute list — never a number with no derivation
    // typed directly into this stack. Fails today: `Threshold: 2048` is exactly that.
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const growthAlarm = Object.values(alarms).find(
      (a: any) => a.Properties.AlarmName === 'stratoclave-PoolItemSizeBytes');
    expect(growthAlarm).toBeDefined();
    const props: any = (growthAlarm as any).Properties;
    // Either it is a metric-math alarm (Metrics present, no bare Threshold on a raw
    // metric) or, if it kept the Metric+Threshold shape, the threshold must NOT equal
    // the old hardcoded value — a passing number here that still happens to be 2048
    // would mean nothing moved.
    if (props.Metrics) {
      expect(props.Metrics.some((m: any) => m.Id === 'baseline' || m.Expression)).toBe(true);
    } else {
      expect(props.Threshold).not.toBe(2048);
    }
  });

  test('adding a declared attribute moves the synthesized baseline metric value', () => {
    // This is R39b's actual acceptance criterion: "a test adds an attribute and shows
    // the threshold must move with it." Exercised here by re-synthesizing with an
    // environment override the (not-yet-built) stack is expected to honor:
    // `STRATOCLAVE_POOL_ITEM_DECLARED_ATTRIBUTE_COUNT_OVERRIDE`, a test-only escape
    // hatch so this test does not need real backend Python to run inside a Jest suite.
    // Fails today because ecs-stack.ts has no such override and no baseline metric to
    // move in the first place.
    const app = new cdk.App();
    const net = new cdk.Stack(app, 'Net2', { env: { account: '123456789012', region: 'us-west-2' } });
    const vpc = new ec2.Vpc(net, 'Vpc', { maxAzs: 2, natGateways: 1 });
    const sg = new ec2.SecurityGroup(net, 'Sg', { vpc, description: 'x' });
    const repo = ecr.Repository.fromRepositoryName(net, 'Repo', 'stratoclave-backend');
    const alb = new elbv2.ApplicationLoadBalancer(net, 'Alb', { vpc, internetFacing: true });
    const tg = new elbv2.ApplicationTargetGroup(net, 'Tg', {
      vpc, port: 8000, protocol: elbv2.ApplicationProtocol.HTTP, targetType: elbv2.TargetType.IP,
    });
    const grownStack = new EcsStack(app, 'EcsPoolItemSizeGrown', {
      env: { account: '123456789012', region: 'us-west-2' },
      prefix: 'stratoclave',
      vpc, securityGroup: sg, repository: repo, targetGroup: tg,
      userPoolArn: 'arn:aws:cognito-idp:us-west-2:123456789012:userpool/us-west-2_p',
      dynamoDbTableArns: ['arn:aws:dynamodb:us-west-2:123456789012:table/stratoclave-users'],
      environment: {
        DATABASE_TYPE: 'dynamodb',
        STRATOCLAVE_POOL_ITEM_DECLARED_ATTRIBUTE_COUNT_OVERRIDE: '13',
      },
    });
    const grownTemplate = Template.fromStack(grownStack);

    const baseFilters = template.findResources('AWS::Logs::MetricFilter');
    const grownFilters = grownTemplate.findResources('AWS::Logs::MetricFilter');
    const baseline = (filters: any) => {
      for (const res of Object.values(filters)) {
        for (const t of (res as any).Properties.MetricTransformations || []) {
          if (t.MetricName === 'PoolItemSizeBaselineBytes') return t.DefaultValue ?? t.MetricValue;
        }
      }
      return undefined;
    };
    expect(baseline(grownFilters)).not.toBe(baseline(baseFilters));
  });
});
