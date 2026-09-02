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
    const stack = new QuotaReconcilerStack(app, 'TestQuotaReconciler', {
      env: { account: '123456789012', region: 'us-west-2' },
      prefix: 'stratoclave',
      lambdaRepository: repo,
      lambdaImageTag: 'v52',
      tenantBudgetsTable: budgets,
      userTenantsTable: userTenants,
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

  test('a seat-count drift alarm exists', () => {
    template.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: Match.stringLikeRegexp('SeatCountDrift|seat-count-drift'),
    });
  });

  test('a coalesced-identity (pool_limit vs baseline+granted) alarm exists', () => {
    template.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: Match.stringLikeRegexp('CoalescedIdentity|coalesced-identity|PoolLimitIdentity'),
    });
  });

  test('least privilege: reads the budgets and user-tenants tables, writes to neither', () => {
    // The reconciler REPORTS drift; it never self-heals seat_count (design
    // note section 3), so its IAM policy must not include write actions on
    // the budgets table.
    const policies = template.findResources('AWS::IAM::Policy');
    const json = JSON.stringify(policies);
    expect(json).not.toContain('dynamodb:PutItem');
    expect(json).not.toContain('dynamodb:UpdateItem');
    expect(json).not.toContain('dynamodb:DeleteItem');
  });
});
