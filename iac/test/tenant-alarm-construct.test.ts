import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as fs from 'fs';
import * as path from 'path';
import { TenantAlarm } from '../lib/tenant-alarm';

/**
 * Amendments B6/B9 (the F1 contract): a shared CDK alarm construct in
 * `iac/lib`, plus one `iac/test` asserting no human-facing, tenant-
 * identified alarm is hand-rolled outside it.
 *
 * ADJUDICATED NAME (this file was previously importing a name the construct
 * never shipped under). The contract's own design note (`design-F1.md`
 * section on B6/B9) named the construct `TenantIdentifiedAlarm`; the shipped
 * implementation (`iac/lib/tenant-alarm.ts`, landed in commit `3366956`) ships
 * `TenantAlarm` instead, with a REQUIRED `scope: 'tenant' | 'deployment'`
 * field the design note's naming did not anticipate. That field is not
 * decorative: `quota-reconciler-stack.ts`'s `PoolCeilingChecksMissing` and
 * `quota-grants-stack.ts`'s `GrantSweeperAbsent` / `GrantRevocationLate` all
 * build `scope: 'deployment'` alarms through this SAME construct — signals
 * that, by the construct's own contract, name no tenant at all. A class
 * called `TenantIdentifiedAlarm` instantiated with `scope: 'deployment'` on a
 * signal that identifies no tenant would be a name asserting something false
 * about its own instance; `TenantAlarm` (read as "the alarm construct this
 * epic's tenant-scoped subsystem uses", not as "an alarm that identifies a
 * tenant") is the name that does not contradict its own shipped usage. This
 * is a genuine widening of the construct's job beyond what the design note's
 * B6/B9 text described (which considered only the reconciler's per-tenant
 * checks), not an accident: it is what "no human-facing alarm hand-rolled
 * outside a shared construct" (B6's actual goal) requires once a scheduled
 * job's own absence — not just a per-tenant defect — turned out to need the
 * exact same undimensioned-metric-plus-attributed-log-line shape. Converged
 * on `TenantAlarm` here rather than exporting both names, per the standing
 * rule against one fact having two authorities.
 *
 * The props shape below is rewritten to match `TenantAlarmProps` as shipped
 * (`logGroup`, `scope`, `metricNamespace`, `event`, `treatMissingData`,
 * `alarmDescription`, ...) rather than the placeholder shape
 * (`alarmBaseName`, `namespace`, `filterPattern` as a raw string) an earlier
 * draft of this file guessed before the construct existed.
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
 * Scope of "no alarm outside it": the test below scopes its "no alarm built
 * outside the construct" check to `iac/lib/quota-reconciler-stack.ts` (F1's
 * own new file), not a repository-wide sweep -- a reading choice already
 * recorded in the original draft of this file, preserved here unchanged.
 */
describe('TenantAlarm (shared construct)', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'TestTenantAlarm', {
      env: { account: '123456789012', region: 'us-west-2' },
    });
    const probeLogGroup = new logs.LogGroup(stack, 'ProbeLogGroup', {
      logGroupName: '/lambda/stratoclave-probe',
    });
    new TenantAlarm(stack, 'Probe', {
      logGroup: probeLogGroup,
      // Required: the filter pattern must bind tenant_id out of the log
      // line, which is the whole point of this construct over a bare
      // MetricFilter -- an alarm firing with no way to say which tenant is
      // the "half-applied convention" failure this exists to prevent.
      scope: 'tenant',
      prefix: 'stratoclave',
      metricNamespace: 'Stratoclave/Probe',
      metricName: 'ProbeConditionCount',
      event: 'probe_condition',
      threshold: 0,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription: 'Probe alarm for this construct-shape test.',
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

  test('a deployment-scoped instance is exempt from the tenant_id requirement', () => {
    // The construct's OWN reason for a required, defaultless `scope` field:
    // a signal about the deployment as a whole (a scheduled job's absence)
    // has no tenant to name, and that has to be stated rather than inferred.
    // This is the case `TenantIdentifiedAlarm` could not have named honestly.
    const app2 = new cdk.App();
    const stack2 = new cdk.Stack(app2, 'TestTenantAlarmDeployment', {
      env: { account: '123456789012', region: 'us-west-2' },
    });
    const lg = new logs.LogGroup(stack2, 'ProbeLogGroup2', {
      logGroupName: '/lambda/stratoclave-probe-2',
    });
    new TenantAlarm(stack2, 'ProbeDeployment', {
      logGroup: lg,
      scope: 'deployment',
      prefix: 'stratoclave',
      metricNamespace: 'Stratoclave/Probe',
      metricName: 'ProbeRan',
      event: 'probe_ran',
      threshold: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
      alarmDescription: 'Deployment-scoped probe alarm for this construct-shape test.',
    });
    const template2 = Template.fromStack(stack2);
    template2.hasResourceProperties('AWS::Logs::MetricFilter', {
      FilterPattern: Match.stringLikeRegexp('probe_ran'),
    });
    // No tenant_id requirement on a deployment-scoped filter pattern.
    const filters = template2.findResources('AWS::Logs::MetricFilter');
    const patterns = Object.values(filters).map(
      (f: any) => f.Properties.FilterPattern as string,
    );
    expect(patterns.some((p) => !/tenant_id/.test(p))).toBe(true);
  });
});

describe('quota-reconciler-stack.ts builds its alarms through the shared construct', () => {
  test('does not hand-roll a cloudwatch.Alarm construct directly', () => {
    const filePath = path.join(__dirname, '..', 'lib', 'quota-reconciler-stack.ts');
    expect(fs.existsSync(filePath)).toBe(true);
    const text = fs.readFileSync(filePath, 'utf8');
    expect(text).toContain('TenantAlarm');
    expect(text).not.toMatch(/new\s+cloudwatch\.Alarm\(/);
  });
});
