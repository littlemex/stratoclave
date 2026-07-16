/**
 * Regression guard for Ledger P2-d: the credit-ledger table MUST be append-only
 * on the ECS task role.
 *
 * The ledger is the money source of truth and its correctness proof rests on
 * events being immutable once written (a terminal event's type never changes,
 * so the settle routing's "read RECLAIM ⇒ stays RECLAIM" and the once-per-hold
 * reserved-return exclusion hold). Enforced two ways, both asserted here:
 *   1. The blanket CRUD grant must NOT cover the ledger table (no UpdateItem/
 *      DeleteItem/BatchWriteItem reaching it via the CRUD statement).
 *   2. An explicit DENY of UpdateItem/DeleteItem/BatchWriteItem on the ledger
 *      (defence-in-depth: overrides any future accidental re-grant).
 * And the ledger IS reachable for append + read (PutItem/ConditionCheckItem/
 * GetItem/Query).
 */
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { EcsStack } from '../lib/ecs-stack';

const LEDGER_ARN = 'arn:aws:dynamodb:us-west-2:123456789012:table/stratoclave-credit-ledger';
const OTHER_ARN = 'arn:aws:dynamodb:us-west-2:123456789012:table/stratoclave-users';
const MUTATE_ACTIONS = ['dynamodb:UpdateItem', 'dynamodb:DeleteItem', 'dynamodb:BatchWriteItem'];

describe('EcsStack credit-ledger append-only', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    const support = new cdk.Stack(app, 'TestSupport', {
      env: { account: '123456789012', region: 'us-west-2' },
    });
    const vpc = new ec2.Vpc(support, 'Vpc', { maxAzs: 2, natGateways: 1 });
    const sg = new ec2.SecurityGroup(support, 'Sg', { vpc });
    const repo = ecr.Repository.fromRepositoryName(support, 'Repo', 'stratoclave-backend');
    new elbv2.ApplicationLoadBalancer(support, 'Alb', { vpc, internetFacing: true });
    const tg = new elbv2.ApplicationTargetGroup(support, 'Tg', {
      vpc,
      port: 8000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
    });

    const stack = new EcsStack(app, 'EcsStackForLedgerTest', {
      env: { account: '123456789012', region: 'us-west-2' },
      prefix: 'stratoclave',
      vpc,
      securityGroup: sg,
      repository: repo,
      targetGroup: tg,
      userPoolArn: 'arn:aws:cognito-idp:us-west-2:123456789012:userpool/us-west-2_testpool',
      dynamoDbTableArns: [OTHER_ARN, LEDGER_ARN],
    });
    template = Template.fromStack(stack);
  });

  test('explicit DENY of mutate/delete on the ledger exists', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'CreditLedgerNoMutateOrDelete',
            Effect: 'Deny',
            Action: Match.arrayWith(MUTATE_ACTIONS),
          }),
        ]),
      },
    });
  });

  test('append-only ALLOW grants Put/ConditionCheck/Get/Query on the ledger', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'CreditLedgerAppendOnly',
            Effect: 'Allow',
            Action: Match.arrayWith([
              'dynamodb:PutItem',
              'dynamodb:ConditionCheckItem',
              'dynamodb:GetItem',
              'dynamodb:Query',
            ]),
          }),
        ]),
      },
    });
  });

  test('no ALLOW statement grants UpdateItem/DeleteItem/BatchWriteItem reaching the ledger', () => {
    // Walk every ALLOW policy statement; if it grants any mutate/delete action
    // (directly or via a wildcard), it must NOT list the ledger table as a
    // resource. This catches the blanket CRUD grant accidentally re-including
    // the ledger.
    const policies = template.findResources('AWS::IAM::Policy');
    for (const [, res] of Object.entries(policies)) {
      const statements = res.Properties?.PolicyDocument?.Statement ?? [];
      for (const stmt of statements) {
        if (stmt.Effect !== 'Allow') continue;
        const actionsStr = JSON.stringify(stmt.Action ?? '');
        const grantsMutate =
          MUTATE_ACTIONS.some((a) => actionsStr.includes(a)) ||
          actionsStr.includes('dynamodb:*') ||
          actionsStr === '"*"';
        if (!grantsMutate) continue;
        const resources = Array.isArray(stmt.Resource) ? stmt.Resource : [stmt.Resource];
        const rendered = JSON.stringify(resources);
        expect(rendered).not.toContain('stratoclave-credit-ledger');
      }
    }
  });
});
