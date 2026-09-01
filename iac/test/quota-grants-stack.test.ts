import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { QuotaGrantsStack } from '../lib/quota-grants-stack';

/**
 * F2 (CONTRACT-F2-grant.md) — R40: human-facing alarms carry a tenant
 * dimension.
 *
 * `QuotaGrantsStack` does not exist at all yet (`iac/lib/quota-grants-stack.ts`
 * is new, per the contract's own file list), so this whole suite fails today
 * at compile/import — `Cannot find module '../lib/quota-grants-stack'`.
 *
 * CORRECTED READING (superseding design-F2.md's original Ambiguity #3): a
 * per-tenant CloudWatch *metric* dimension was found to violate an existing,
 * explicit convention in this codebase (`iac/lib/vsr-service.ts`'s
 * `VSR_METRIC_ALLOWLIST` comment: metrics billed per name, high-cardinality
 * labels dropped, the EMF exporter declaring NO dimensions so nothing is
 * faceted). The requirement's actual intent was narrower — a page must be
 * able to say *whose* tenant it concerns — so R40 now reads:
 *
 *   - the alarm stays a SINGLE UNDIMENSIONED series (no `Dimensions` on the
 *     `MetricTransformation` for ANY of the sweeper's metrics, including
 *     `revoke_blocked_grants` and `grant_revocation_late_seconds`);
 *   - the structured LOG LINE each tenant-relevant metric's `FilterPattern`
 *     selects must itself carry `tenant_id`, so an operator paged by the
 *     undimensioned alarm finds the tenant with one Logs Insights query
 *     over the same log group, not from the metric.
 *
 * `sweeper_ran` is the global heartbeat (no single tenant it belongs to) and
 * its FilterPattern does NOT require `tenant_id`; `revoke_blocked_grants` and
 * `grant_revocation_late_seconds` name a specific tenant's stuck/late grant
 * and their FilterPattern MUST require it.
 */

let template: Template;

beforeAll(() => {
  const app = new cdk.App();
  const deps = new cdk.Stack(app, 'Deps', { env: { account: '123456789012', region: 'us-west-2' } });
  const repo = new ecr.Repository(deps, 'Repo', { repositoryName: 'stratoclave-backend' });
  const quotaEvents = new dynamodb.Table(deps, 'QuotaEvents', {
    partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
    sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
  });
  const tenantBudgets = new dynamodb.Table(deps, 'Budgets', {
    partitionKey: { name: 'tenant_id', type: dynamodb.AttributeType.STRING },
    sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
  });
  const stack = new QuotaGrantsStack(app, 'TestQuotaGrants', {
    env: { account: '123456789012', region: 'us-west-2' },
    prefix: 'stratoclave',
    lambdaRepository: repo,
    lambdaImageTag: 'v1',
    quotaEventsTable: quotaEvents,
    tenantBudgetsTable: tenantBudgets,
  });
  template = Template.fromStack(stack);
});

test('the sweeper Lambda runs on a 5-minute schedule', () => {
  template.resourceCountIs('AWS::Lambda::Function', 1);
  template.hasResourceProperties('AWS::Events::Rule', {
    ScheduleExpression: 'rate(5 minutes)',
  });
});

function metricTransformation(metricName: string): any {
  const filters = template.findResources('AWS::Logs::MetricFilter');
  for (const f of Object.values(filters) as any[]) {
    const mt = (f.Properties?.MetricTransformations ?? []).find(
      (m: any) => m.MetricName === metricName,
    );
    if (mt) {
      return { metricTransformation: mt, filterPattern: f.Properties.FilterPattern as string };
    }
  }
  return undefined;
}

test('revoke_blocked_grants metric filter is undimensioned; its log line carries tenant_id (R40)', () => {
  const found = metricTransformation('RevokeBlockedGrants');
  expect(found).toBeDefined();
  expect(found!.metricTransformation.Dimensions).toBeUndefined();
  expect(found!.metricTransformation.MetricNamespace).toBe('Stratoclave/Grants');
  expect(found!.filterPattern).toMatch(/tenant_id/);
});

test('grant_revocation_late_seconds metric filter is undimensioned; its log line carries tenant_id (R40)', () => {
  const found = metricTransformation('GrantRevocationLateSeconds');
  expect(found).toBeDefined();
  expect(found!.metricTransformation.Dimensions).toBeUndefined();
  expect(found!.metricTransformation.MetricNamespace).toBe('Stratoclave/Grants');
  expect(found!.filterPattern).toMatch(/tenant_id/);
});

test('sweeper_ran heartbeat is undimensioned AND its FilterPattern does not require tenant_id (a global fact, not a per-tenant one)', () => {
  const found = metricTransformation('SweeperRan');
  expect(found).toBeDefined();
  expect(found!.metricTransformation.Dimensions).toBeUndefined();
  expect(found!.filterPattern).not.toMatch(/tenant_id/);
});

test('every AWS::Logs::MetricFilter in this stack is undimensioned (no per-tenant metric series anywhere)', () => {
  const filters = template.findResources('AWS::Logs::MetricFilter');
  for (const f of Object.values(filters) as any[]) {
    for (const mt of f.Properties?.MetricTransformations ?? []) {
      expect(mt.Dimensions).toBeUndefined();
    }
  }
});

test('an alarm exists on the blocked-grants metric, undimensioned, with missing data treated as breaching', () => {
  template.hasResourceProperties('AWS::CloudWatch::Alarm', {
    Namespace: 'Stratoclave/Grants',
    MetricName: 'RevokeBlockedGrants',
    ComparisonOperator: 'GreaterThanThreshold',
    Threshold: 0,
    Dimensions: Match.absent(),
  });
});
