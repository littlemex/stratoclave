/**
 * Regression guard for sweep-4 Critical (sweep-1 C-D regression).
 *
 * The ECS task role MUST grant `dynamodb:Scan` only on a strict allowlist —
 * every table on it has a named Scan caller in the backend. Any future squash
 * that reintroduces `permissions` into the Scan list must fail CI immediately,
 * and any squash that drops a table a live Scan caller needs must fail too.
 *
 * Rationale: `api-keys` Scan access lets a backend RCE dump every customer's
 * key hashes; `permissions` Scan access lets it lift the entire RBAC seed,
 * which we specifically modelled as a read-sealed table.
 */
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { EcsStack } from '../lib/ecs-stack';

// Sweep-4 intentionally does NOT drop `api-keys` yet (admin list page
// still uses Scan-based list_all pending a GSI migration). `permissions`
// however has no live Scan caller and MUST stay out of the allowlist.
// `promotion-candidates` is required, not merely tolerated: the composed model
// registry reads it with a Scan on every TTL refresh
// (`mvp.discovery.activation.list_activated_entries`), and without the grant an
// activation commits and answers 200 while staying invisible to `/v1/models`
// and to the entitlement surface. Pinned here so dropping it cannot be silent.
const ALLOWED_SUFFIXES = [
  'users',
  'api-keys',
  'tenants',
  'user-tenants',
  'sso-pre-registrations',
  'trusted-accounts',
  'promotion-candidates',
];
const FORBIDDEN_SUFFIXES = ['permissions'];

describe('EcsStack DynamoDB Scan allowlist', () => {
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

    const stack = new EcsStack(app, 'EcsStackForScanTest', {
      env: { account: '123456789012', region: 'us-west-2' },
      prefix: 'stratoclave',
      vpc,
      securityGroup: sg,
      repository: repo,
      targetGroup: tg,
      userPoolArn: 'arn:aws:cognito-idp:us-west-2:123456789012:userpool/us-west-2_testpool',
      dynamoDbTableArns: [
        'arn:aws:dynamodb:us-west-2:123456789012:table/stratoclave-users',
      ],
    });
    template = Template.fromStack(stack);
  });

  test('Scan statement exists with sid ScanLimitedToAdminConsoleTables', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'ScanLimitedToAdminConsoleTables',
            Effect: 'Allow',
            Action: 'dynamodb:Scan',
          }),
        ]),
      },
    });
  });

  test('Scan is NOT granted on forbidden tables (permissions)', () => {
    // Dump the synthesized policies and walk for any statement that
    // grants Scan (directly OR via a wildcard action). Sweep-4 round-4
    // hardening: we now accept `dynamodb:*` and bare `*` as Scan-
    // granting shapes, because a squash that regresses the allowlist
    // via a wildcard action would otherwise silently slip through.
    const policies = template.findResources('AWS::IAM::Policy');
    for (const [_name, res] of Object.entries(policies)) {
      const statements = res.Properties?.PolicyDocument?.Statement ?? [];
      for (const stmt of statements) {
        const actionsStr = JSON.stringify(stmt.Action ?? '');
        const grantsScan =
          actionsStr.includes('dynamodb:Scan') ||
          actionsStr.includes('dynamodb:*') ||
          actionsStr === '"*"';
        if (!grantsScan) continue;
        const resources = Array.isArray(stmt.Resource) ? stmt.Resource : [stmt.Resource];
        for (const r of resources) {
          const rendered = JSON.stringify(r);
          for (const bad of FORBIDDEN_SUFFIXES) {
            expect(rendered).not.toContain(`stratoclave-${bad}`);
          }
        }
      }
    }
  });

  test('Scan IS granted on every table with a live Scan caller', () => {
    const policies = template.findResources('AWS::IAM::Policy');
    const scanArns: string[] = [];
    for (const [, res] of Object.entries(policies)) {
      const statements = res.Properties?.PolicyDocument?.Statement ?? [];
      for (const stmt of statements) {
        const actions = Array.isArray(stmt.Action) ? stmt.Action : [stmt.Action];
        if (!actions.includes('dynamodb:Scan')) continue;
        const resources = Array.isArray(stmt.Resource) ? stmt.Resource : [stmt.Resource];
        for (const r of resources) {
          scanArns.push(JSON.stringify(r));
        }
      }
    }
    const joined = scanArns.join('\n');
    for (const ok of ALLOWED_SUFFIXES) {
      expect(joined).toContain(`stratoclave-${ok}`);
    }
  });
});
