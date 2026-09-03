import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { Template } from 'aws-cdk-lib/assertions';
import { EcsStack } from '../lib/ecs-stack';

/**
 * Amendment B4, narrowed after review: no documentation sentence claims the
 * pool item is a fixed size that cannot grow (the FIXED-SIZE sentence in
 * `pending-protocol.md:103` is about the separate MARKER item and stays
 * true, untouched -- see `test_pending_protocol_*` in
 * `test_ceiling_doc_names_writers_r14a.py`). What survives is CODE, and it
 * was always the load-bearing half: `ecs-stack.ts`'s `PoolItemSizeGrowth`
 * alarm and its `PoolItemSizeBytes` gauge are calibrated to the PRE-EPIC row
 * ("A healthy pool item is a handful of fixed counters (<~200B)", a bare
 * comment above a bare literal `threshold: 2048`). F1 adds three attributes
 * to every row (`seat_count`, `manual_limit_microusd`, `seat_monthly_usd`,
 * B1's declaration) -- the calibration must be RE-DERIVED from that
 * declaration rather than left as a disconnected literal nobody re-checks
 * when a later PR (F2's `pool_granted`/`grant_cap_microusd`) grows the row
 * again.
 *
 * This does not assert a specific new threshold NUMBER (a bare literal
 * chosen by this file would be exactly the defect it is trying to close --
 * a second copy of the width math to fall out of sync with the first).
 *
 * ADJUDICATED: the assertion below originally demanded a named TypeScript
 * constant in `ecs-stack.ts` (e.g. `POOL_ITEM_SIZE_ALARM_THRESHOLD_BYTES`)
 * commented to the post-F1 attribute set. The shipped implementation took a
 * DIFFERENT, and on inspection more robust, path: `PoolItemSizeGrowth`
 * (this alarm) deliberately KEEPS its bare `2048` -- re-justified in
 * `ecs-stack.ts`'s own comment as catching only UNBOUNDED, order-of-
 * magnitude growth, for which a generous absolute ceiling is the right
 * shape and does not need schema awareness. The tens-of-bytes case this
 * test was written for (one attribute more than declared) is caught by a
 * SEPARATE, new alarm, `PoolRowBeyondDeclaration`, whose threshold is
 * pinned at zero forever: the backend computes `over_declared_bytes`
 * (observed size minus `worst_case_pool_item_bytes()`, B1's closed-world
 * declaration in `backend/dynamo/pool_row_schema.py`) and this alarm fires
 * on `> 0`. A threshold that never needs to move is a STRONGER answer to
 * "the calibration lives with the schema" than a named constant that is
 * still typed once in TypeScript and could still rot; it also avoids the
 * "second, competing copy of the declaration in iac" defect
 * `ecs-stack-pool-item-size-baseline-l39b.test.ts`'s own docstring records
 * as already rejected once. This test now asserts THAT mechanism.
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
  const stack = new EcsStack(app, 'EcsPoolSizeRecalibration', {
    env: { account: '123456789012', region: 'us-west-2' },
    prefix: 'stratoclave',
    vpc, securityGroup: sg, repository: repo, targetGroup: tg,
    userPoolArn: 'arn:aws:cognito-idp:us-west-2:123456789012:userpool/us-west-2_p',
    dynamoDbTableArns: ['arn:aws:dynamodb:us-west-2:123456789012:table/stratoclave-users'],
    environment: { DATABASE_TYPE: 'dynamodb' },
  });
  return Template.fromStack(stack);
}

describe('PoolItemSizeGrowth alarm recalibration (Amendment B4, narrowed)', () => {
  test('the alarm still exists and still fires on growth', () => {
    synth().hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: 'stratoclave-PoolItemSizeBytes',
      ComparisonOperator: 'GreaterThanThreshold',
    });
  });

  test('the tens-of-bytes case is caught by a threshold that never needs recalibration, not a named constant', () => {
    // PoolItemSizeGrowth itself is allowed to keep 2048 -- it is not the
    // detector for this case, and ecs-stack.ts says so in its own comment.
    // What must exist is the SEPARATE alarm whose threshold is derived from
    // the schema and never needs a human to bump it: PoolRowBeyondDeclaration,
    // fed by `$.over_declared_bytes`, at threshold 0.
    const template = synth();
    template.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: 'stratoclave-PoolRowBeyondDeclaration',
      Threshold: 0,
      ComparisonOperator: 'GreaterThanThreshold',
    });
    const filters = template.findResources('AWS::Logs::MetricFilter');
    const overDeclaredFilter = Object.values(filters).find((f: any) =>
      (f.Properties.MetricTransformations || []).some(
        (m: any) => m.MetricName === 'PoolRowOverDeclaredBytes',
      ),
    );
    expect(overDeclaredFilter).toBeDefined();
    expect((overDeclaredFilter as any).Properties.FilterPattern).toMatch(/over_declared_bytes/);

    // The derivation itself (worst_case_pool_item_bytes() over
    // POOL_ROW_ATTRIBUTES) lives in the backend, where the schema is
    // declared -- verified by backend/tests/test_ledger_hot_path_flatness_
    // claim_l39a.py, not here. This file only asserts the CDK side: the
    // alarm exists, is fed by the backend-computed delta, and its threshold
    // is the fixed point (zero) that never has to move.
    const filePath = path.join(__dirname, '..', 'lib', 'ecs-stack.ts');
    const text = fs.readFileSync(filePath, 'utf8');
    expect(text).toMatch(/over_declared_bytes/);
    expect(text).toMatch(/PoolRowBeyondDeclaration/);
  });
});
