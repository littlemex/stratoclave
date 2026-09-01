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
 * a second copy of the width math to fall out of sync with the first). It
 * asserts the DERIVATION is code, not a magic number: `ecs-stack.ts` must
 * define a named constant for this threshold, with a comment tying it to
 * the post-F1 attribute set, rather than the bare `2048` appearing directly
 * inside the `cloudwatch.Alarm` call.
 *
 * Today `ecs-stack.ts` contains exactly that bare literal, so this fails.
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

  test('the threshold is a named, derived constant -- not a bare literal frozen to the pre-F1 row', () => {
    const filePath = path.join(__dirname, '..', 'lib', 'ecs-stack.ts');
    const text = fs.readFileSync(filePath, 'utf8');

    // The exact defect: `threshold: 2048,` (or any bare numeric literal)
    // directly inside the PoolItemSizeGrowth alarm's props, with no named,
    // commented derivation tying it to the row's current attribute set.
    const alarmBlockStart = text.indexOf("'PoolItemSizeGrowth'");
    expect(alarmBlockStart).toBeGreaterThan(-1);
    const alarmBlock = text.slice(alarmBlockStart, alarmBlockStart + 1200);

    const usesBareLiteral = /threshold:\s*\d+\s*,/.test(alarmBlock);
    expect(usesBareLiteral).toBe(false);

    // A named constant, with a comment that ties its derivation to the
    // post-F1 attribute set (B1's declaration), must exist somewhere in the
    // file for the alarm to reference.
    expect(text).toMatch(/POOL_ITEM_SIZE_(ALARM_)?THRESHOLD_BYTES/);
    const constIdx = text.search(/POOL_ITEM_SIZE_(ALARM_)?THRESHOLD_BYTES/);
    const constContext = text.slice(Math.max(0, constIdx - 400), constIdx + 200).toLowerCase();
    const derivationNamesTheNewAttributes = ['seat_count', 'manual_limit_microusd', 'seat_monthly_usd']
      .some((name) => constContext.includes(name));
    expect(derivationNamesTheNewAttributes).toBe(true);
  });
});
