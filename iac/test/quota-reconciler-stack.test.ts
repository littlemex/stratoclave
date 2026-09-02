import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import { QuotaReconcilerStack } from '../lib/quota-reconciler-stack';

/**
 * R8 (the F1 contract): "The daily reconciler compares the row to its
 * sources: seat_count against a live membership count..." -- the IaC half.
 *
 * Amendment history on this file's own target:
 *   - The contract as first drafted scoped this to `iac/lib/ecs-stack.ts
 *     (the reconciler's schedule and alarms only)`. F1's own design note
 *     flagged that every OTHER daily job in this codebase
 *     (CertificateSchedulerStack, LedgerProjectorStack) is its own
 *     dedicated stack file, and an implementer following that convention
 *     would produce a change the literal scope line did not recognize.
 *   - Amendment A3 moved the reconciler's schedule/alarms into their own
 *     stack file.
 *   - Amendment B7 struck the stale `ecs-stack.ts` scope line and named the
 *     file: `quota-reconciler-stack.ts`, on the same convention F2's
 *     `quota-grants-stack.ts` uses.
 *
 * This file replaces the earlier `ecs-stack-seat-reconciler-schedule.test.ts`,
 * which synthesized `EcsStack` alone under Reading A of the original
 * ambiguity; that reading is superseded.
 *
 * ADJUDICATED (three assertions below were wrong, not the implementation):
 *
 *   - "a seat-count drift alarm exists" / "a coalesced-identity alarm
 *     exists" guessed CloudWatch alarm names (`SeatCountDrift`,
 *     `CoalescedIdentity`, `PoolLimitIdentity`) that appear NOWHERE in
 *     CONTRACT-F1-ceiling.md or design-F1.md -- R8's own "Verified by"
 *     column asks for a UNIT test over the registered check functions
 *     (`seat_count_matches_membership`, `limit_identity`), not a specific
 *     CDK alarm name or one alarm per check. The shipped design merges
 *     every registered check's defects into ONE per-tenant alarm
 *     (`PoolCeilingDefect`), with the matched LOG LINE naming which check
 *     fired -- confirmed on real AWS (`docs/DEPLOYMENT.md`'s verification):
 *     `{"event": "pool_ceiling_defect", "check": "seat_count_matches_membership", ...}`.
 *     Rewritten below to assert the real alarm and its real filter pattern.
 *   - "least privilege: reads the budgets and user-tenants tables, writes to
 *     neither" scanned EVERY `AWS::IAM::Policy` in the stack, but this
 *     stack's OTHER function (`PeriodRollover`) legitimately writes (R16:
 *     "This job is what makes the row exist"). The test's own comment says
 *     "its IAM policy" (singular, the reconciler's), but the assertion code
 *     scanned the whole template -- a mismatch between the test's own stated
 *     intent and what it checked. Rewritten to scope the read-only
 *     assertion to the Reconciler's own role, and to name the rollover's
 *     write as the separate, expected thing it is.
 *
 * Today `iac/lib/quota-reconciler-stack.ts` does not exist at all, so this
 * whole suite fails at compile/import time.
 */
describe('QuotaReconcilerStack', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const deps = new cdk.Stack(app, 'Deps', { env: { account: '123456789012', region: 'us-west-2' } });
    const repo = new ecr.Repository(deps, 'Repo', { repositoryName: 'stratoclave-backend' });
    const budgets = new dynamodb.Table(deps, 'Budgets', {
      partitionKey: { name: 'tenant_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
    });
    const userTenants = new dynamodb.Table(deps, 'UserTenants', {
      partitionKey: { name: 'user_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'tenant_id', type: dynamodb.AttributeType.STRING },
    });
    const quotaEvents = new dynamodb.Table(deps, 'QuotaEvents', {
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
    });
    const stack = new QuotaReconcilerStack(app, 'TestQuotaReconciler', {
      env: { account: '123456789012', region: 'us-west-2' },
      prefix: 'stratoclave',
      lambdaRepository: repo,
      lambdaImageTag: 'v52',
      tenantBudgetsTable: budgets,
      userTenantsTable: userTenants,
      quotaEventsTable: quotaEvents,
    });
    template = Template.fromStack(stack);
  });

  test('one Lambda function runs the reconciler check loop', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'stratoclave-quota-reconciler',
    });
  });

  test('a daily schedule triggers the reconciler', () => {
    template.hasResourceProperties('AWS::Events::Rule', {
      Name: Match.stringLikeRegexp('quota-reconciler'),
    });
  });

  test('a per-tenant pool-ceiling defect alarm exists, covering seat-count drift and the coalesced identity among every registered check', () => {
    template.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: 'stratoclave-PoolCeilingDefects',
      Namespace: 'stratoclave/CreditLedger',
      MetricName: 'PoolCeilingDefects',
    });
    const filters = template.findResources('AWS::Logs::MetricFilter');
    const defectFilter = Object.values(filters).find((f: any) =>
      (f.Properties.MetricTransformations || []).some(
        (m: any) => m.MetricName === 'PoolCeilingDefects',
      ),
    );
    expect(defectFilter).toBeDefined();
    // Tenant-attributed (TenantAlarm's own discipline: the metric stays
    // undimensioned, the matched log line carries tenant_id) and matched on
    // the ONE event every registered check's defects emit -- seat_count_
    // matches_membership (seat-count drift) and limit_identity (the
    // coalesced baseline+granted identity) both feed this, not two alarms.
    const pattern = (defectFilter as any).Properties.FilterPattern as string;
    expect(pattern).toMatch(/pool_ceiling_defect/);
    expect(pattern).toMatch(/tenant_id/);
  });

  test('a deployment-scoped completeness alarm exists: silence means the reconciler stopped, or a declared check evaporated', () => {
    // The second real alarm in this stack: B1's closed-world declaration's
    // completeness half (missing_declared_checks()). Deployment-scoped
    // because "the reconciler did not run" names no tenant, and missing
    // data is BREACHING because a pass that never happened must not read as
    // a pass that found nothing.
    template.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: 'stratoclave-PoolCeilingChecksMissing',
      TreatMissingData: 'breaching',
    });
  });

  test('least privilege: the reconciler itself reads but never writes', () => {
    // The reconciler REPORTS drift; it never self-heals seat_count (design
    // note section 3) -- scoped to the Reconciler function's OWN IAM policy.
    // The stack's OTHER function, PeriodRollover, legitimately writes (see
    // the next test), so scanning the whole template -- this test's
    // original form -- failed on a write action that belongs to a different
    // function's role, not on a defect in the reconciler's own permissions.
    const policies = template.findResources('AWS::IAM::Policy');
    const reconcilerPolicies = Object.values(policies).filter((p: any) =>
      String(p.Properties?.PolicyName || '').startsWith('ReconcilerServiceRole'),
    );
    expect(reconcilerPolicies.length).toBeGreaterThan(0);
    const json = JSON.stringify(reconcilerPolicies);
    expect(json).not.toContain('dynamodb:PutItem');
    expect(json).not.toContain('dynamodb:UpdateItem');
    expect(json).not.toContain('dynamodb:DeleteItem');
    expect(json).not.toContain('dynamodb:BatchWriteItem');
  });

  test('the period rollover writes (R16): it creates each period pool row, a different function with a different, wider grant', () => {
    const policies = template.findResources('AWS::IAM::Policy');
    const rolloverPolicies = Object.values(policies).filter((p: any) =>
      String(p.Properties?.PolicyName || '').startsWith('PeriodRolloverServiceRole'),
    );
    expect(rolloverPolicies.length).toBeGreaterThan(0);
    expect(JSON.stringify(rolloverPolicies)).toContain('dynamodb:PutItem');
  });
});
