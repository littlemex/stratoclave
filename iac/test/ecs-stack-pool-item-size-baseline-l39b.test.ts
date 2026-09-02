import * as fs from 'fs';
import * as path from 'path';
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
 * F1 deletes `sizing` and adds its OWN three attributes to the same item —
 * `seat_count`, `manual_limit_microusd`, `seat_rate_microusd` (the stored seat rate,
 * carried across a period boundary) — an INTENDED size change, with F2 adding two more
 * (`pool_granted_microusd`, an aggregate cap) later. R39b's whole point is that an
 * intended change like this must not need a human to also remember to bump `2048` by
 * hand (and no test currently would catch it if they forgot, because a static threshold
 * is not wrong until the item is bigger than 2048 bytes, which these attributes do not
 * come close to — so the current 2048 silently tolerates ANY schema change up to that
 * ceiling, intended or not, which is the OTHER failure mode R39b names: "an unintended
 * one still is [an alarm]").
 *
 * WHAT IS DESIGNED (the F4 design note section 3, corrected by amendment B1) — and, per
 * an integration update, now PARTIALLY LANDED, though not in this worktree
 *
 * `CONTRACT-F4-claims (F4's contract document)` amendment B1 (`SEAMS (the integration owner's seam-review document)` S1) reassigns the declared-attribute
 * list itself to F1: F1 ships a CLOSED-WORLD schema declaration for the pool row
 * (`backend/dynamo/pool_row_schema.py` — confirmed real elsewhere as of F1's landing,
 * with `POOL_ROW_ATTRIBUTES: dict[str, PoolAttribute]` and a live
 * `worst_case_pool_item_bytes()`; an earlier draft of this test instead proposed F4
 * inventing its own `POOL_ITEM_DECLARED_ATTRIBUTES` tuple in
 * `backend/dynamo/tenant_budgets.py`, which would have been a second, competing copy of
 * a declaration that belongs to the part that OWNS the schema). F1 deliberately does
 * NOT yet classify `pool_granted_microusd` or the aggregate cap — pre-classifying an
 * attribute F2 owns would let F2's merge add writers for it and forget the completeness
 * check with nothing loud saying so — so the declared worst case TODAY is smaller than
 * the eventual post-F2 worst case, and is expected to grow when F2 lands; that growth
 * is the mechanism working, not drift. The CDK stack's alarm threshold is DERIVED from
 * that same declaration — via a second emitted metric (`PoolItemSizeBaselineBytes`) and
 * a metric-math ratio alarm, so the threshold moves the moment F1's (then F2's)
 * declaration changes, with no `ecs-stack.ts` edit and no redeploy required for the
 * schema-aware part. This dynamic-derivation design is not optional now that the
 * classified set is confirmed to change shape across merges — a threshold baked in at
 * `cdk deploy` time would need a manual bump exactly when F2 lands, the same defect this
 * whole design exists to remove. The mechanics asserted below (a second metric, a
 * non-literal threshold, a threshold that moves when the schema does) do not depend on
 * which module ships the declaration, only on there BEING one that both the alarm and
 * the document (see `backend/tests/test_ledger_hot_path_flatness_claim_l39a.py`) derive
 * from — this test never asserts the number the declaration currently produces.
 *
 * ADJUDICATED: the design sketched above (a SECOND CDK-side metric,
 * `PoolItemSizeBaselineBytes`, plus a metric-math ratio alarm or a
 * synth-time `STRATOCLAVE_POOL_ITEM_DECLARED_ATTRIBUTE_COUNT_OVERRIDE`
 * escape hatch) is what this file's own docstring, two paragraphs up,
 * already flags as the REJECTED shape: "an earlier draft of this test
 * instead proposed F4 inventing its own `POOL_ITEM_DECLARED_ATTRIBUTES`
 * tuple... a second, competing copy of a declaration that belongs to the
 * part that OWNS the schema." The shipped implementation carries that
 * rejection all the way through: rather than a CDK-side baseline metric (a
 * second copy of the schema's byte-width arithmetic, this time expressed as
 * a CloudWatch metric-math expression instead of a TS tuple, which
 * CloudWatch metric math cannot even DO per-attribute width arithmetic
 * over anyway), the backend computes the delta directly —
 * `over_declared_bytes = max(observed_bytes - worst_case_pool_item_bytes(), 0)`
 * in `mvp/_pipeline.py`, against `backend/dynamo/pool_row_schema.py`'s
 * `POOL_ROW_ATTRIBUTES` — and CDK only alarms on that ONE already-derived
 * number being `> 0` (`PoolRowBeyondDeclaration`, threshold pinned at zero
 * forever). This satisfies R39b's stated goal ("an intended growth is not
 * an alarm and an unintended one still is") through the same reassignment
 * CONTRACT-F4-claims.md's amendment B1/B3 makes: the schema and its
 * derivation are F1's backend module to own, not iac's to re-derive.
 * R39b's OWN acceptance criterion ("a test adds an attribute and shows the
 * threshold must move with it") is verified where the schema actually
 * lives: `backend/tests/test_ledger_hot_path_flatness_claim_l39a.py`
 * (`worst_case_pool_item_bytes()`), not here — there is no
 * `STRATOCLAVE_POOL_ITEM_DECLARED_ATTRIBUTE_COUNT_OVERRIDE` env var anywhere
 * in the codebase, and adding one just to give this iac test a schema to
 * move would be the second-copy defect a third time, wearing a CDK context
 * value instead of a tuple or a metric-math expression.
 *
 * Rewritten below to assert the mechanism that ships.
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

  test('a second metric filter emits a schema-derived signal, not just the raw size', () => {
    // The backend's `pool_item_size` log line ALSO carries `over_declared_bytes`
    // (observed size minus the schema's declared width), and a second MetricFilter
    // projects it as `PoolRowOverDeclaredBytes` -- the schema-aware companion to the
    // raw `PoolItemSizeBytes` gauge, computed against `over_declared_bytes` rather
    // than against a CDK-side "baseline" metric.
    const filters = template.findResources('AWS::Logs::MetricFilter');
    const metricNames = new Set<string>();
    for (const res of Object.values(filters)) {
      for (const t of (res as any).Properties.MetricTransformations || []) {
        metricNames.add(t.MetricName);
      }
    }
    expect(metricNames.has('PoolRowOverDeclaredBytes')).toBe(true);
    expect(metricNames.has('PoolItemSizeBytes')).toBe(true);
  });

  test('the schema-aware alarm has a threshold that never needs recalibration; the coarse one intentionally keeps its bare literal', () => {
    // R39b's two failure modes get two different alarms, on purpose:
    // PoolItemSizeGrowth (this stack's pre-existing gauge) catches UNBOUNDED
    // growth and legitimately keeps threshold 2048 -- ecs-stack.ts's own
    // comment says a generous absolute ceiling is the right shape for that
    // case and it is NOT the detector for one undeclared attribute (tens of
    // bytes). PoolRowBeyondDeclaration is the tight, schema-derived one: its
    // threshold is 0, always, because the backend already subtracted the
    // declared width before emitting the metric -- a threshold that never
    // moves is the correct response to "must not need a human to
    // recalibrate it," stronger than a threshold computed via CDK metric
    // math or a named TS constant, either of which is still one more place
    // for the schema to be restated and drift.
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const growthAlarm = Object.values(alarms).find(
      (a: any) => a.Properties.AlarmName === 'stratoclave-PoolItemSizeBytes');
    expect(growthAlarm).toBeDefined();
    expect((growthAlarm as any).Properties.Threshold).toBe(2048);

    const beyondDeclarationAlarm = Object.values(alarms).find(
      (a: any) => a.Properties.AlarmName === 'stratoclave-PoolRowBeyondDeclaration');
    expect(beyondDeclarationAlarm).toBeDefined();
    expect((beyondDeclarationAlarm as any).Properties.Threshold).toBe(0);
  });

  test('ecs-stack.ts holds no second, competing copy of the declared-attribute schema', () => {
    // R39b's real acceptance criterion ("a test adds an attribute and shows
    // the threshold must move with it") is verified where the schema is
    // actually declared -- backend/tests/test_ledger_hot_path_flatness_
    // claim_l39a.py, against worst_case_pool_item_bytes() in
    // backend/dynamo/pool_row_schema.py -- not here. What THIS file can and
    // must verify is the negative: ecs-stack.ts does not re-derive its own
    // copy of that schema (an attribute-count env var, a declared-attribute
    // tuple, a metric-math baseline expression), which is exactly the
    // "second, competing copy" defect this test's own docstring records as
    // already rejected once, at F4's design stage, in a different form.
    const filePath = path.join(__dirname, '..', 'lib', 'ecs-stack.ts');
    const text = fs.readFileSync(filePath, 'utf8');
    expect(text).not.toMatch(/STRATOCLAVE_POOL_ITEM_DECLARED_ATTRIBUTE_COUNT_OVERRIDE/);
    expect(text).not.toMatch(/POOL_ITEM_DECLARED_ATTRIBUTES/);
    expect(text).not.toMatch(/PoolItemSizeBaselineBytes/);
    // What it delegates to instead: the backend-computed delta.
    expect(text).toMatch(/over_declared_bytes/);
  });
});
