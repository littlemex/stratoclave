import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as fs from 'fs';
import * as path from 'path';
import { TenantIdentifiedAlarm } from '../lib/tenant-alarm';

/**
 * Amendments B6/B9 (the F1 contract): a shared CDK alarm construct in
 * `iac/lib`, plus one `iac/test` asserting no human-facing, tenant-
 * identified alarm is hand-rolled outside it.
 *
 * Why this exists at all: Amendment A3 moved F1's reconciler alarms into
 * their own stack file while the tenant-identification convention (an
 * undimensioned metric, with `tenant_id` bound from the matched log line
 * rather than used as a CloudWatch dimension -- `iac/lib/vsr-service.ts`'s
 * existing cardinality discipline: "No dimensions => one series per metric
 * name") lived only in F2's own contract. That left the convention
 * half-applied by construction -- the exact failure the discipline existed
 * to prevent, inverted. F1 ships the construct so F2's `quota-grants-stack.ts`
 * consumes it rather than re-deriving the same shape independently.
 *
 * Scope of "no alarm outside it": TENANT-IDENTIFIED alarms specifically --
 * i.e. an alarm whose subject is a per-tenant condition (the reconciler's
 * seat-count drift, its coalesced-identity check). Fleet-wide alarms that
 * predate this construct and carry no tenant dimension at all
 * (CertificatesFailedAlarm, PoolItemSizeGrowth, ReserveShadowDivergenceAlarm)
 * are a different shape and are NOT retargeted by F1 -- retrofitting every
 * existing alarm in the repository is a separate, larger change this
 * contract does not ask for. This is a reading choice, named here rather
 * than assumed: the test below scopes its "no alarm built outside the
 * construct" check to `iac/lib/quota-reconciler-stack.ts` (F1's own new
 * file), not a repository-wide sweep.
 *
 * Today `iac/lib/tenant-alarm.ts` does not exist, so this whole suite fails
 * on module resolution.
 */
describe('TenantIdentifiedAlarm (shared construct)', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'TestTenantAlarm', {
      env: { account: '123456789012', region: 'us-west-2' },
    });
    new TenantIdentifiedAlarm(stack, 'Probe', {
      prefix: 'stratoclave',
      alarmBaseName: 'ProbeCondition',
      namespace: 'Stratoclave/Probe',
      metricName: 'ProbeConditionCount',
      // Required: the filter pattern must bind tenant_id out of the log
      // line, which is the whole point of this construct over a bare
      // MetricFilter -- an alarm firing with no way to say which tenant is
      // the "half-applied convention" failure this exists to prevent.
      filterPattern: '{ $.tenant_id = "*" && $.probe_condition = true }',
      threshold: 0,
    });
    template = Template.fromStack(stack);
  });

  test('produces a metric filter whose pattern binds tenant_id', () => {
    template.hasResourceProperties('AWS::Logs::MetricFilter', {
      FilterPattern: Match.stringLikeRegexp('tenant_id'),
    });
  });

  test('produces an UNDIMENSIONED metric (no per-tenant CloudWatch dimension)', () => {
    const alarms = template.findResources('AWS::CloudWatch::Alarm');
    const dims = JSON.stringify(alarms).match(/"Dimensions"/g);
    expect(dims).toBeNull();
  });

  test('produces exactly one alarm', () => {
    template.resourceCountIs('AWS::CloudWatch::Alarm', 1);
  });
});

describe('quota-reconciler-stack.ts builds its alarms through the shared construct', () => {
  test('does not hand-roll a cloudwatch.Alarm construct directly', () => {
    const filePath = path.join(__dirname, '..', 'lib', 'quota-reconciler-stack.ts');
    expect(fs.existsSync(filePath)).toBe(true);
    const text = fs.readFileSync(filePath, 'utf8');
    expect(text).toContain('TenantIdentifiedAlarm');
    expect(text).not.toMatch(/new\s+cloudwatch\.Alarm\(/);
  });
});
