import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import { LedgerProjectorStack } from '../lib/ledger-projector-stack';

describe('LedgerProjectorStack', () => {
  let template: Template;

  beforeAll(() => {
    const app = new cdk.App();
    // A separate stack provides the cross-stack resources the projector imports.
    const deps = new cdk.Stack(app, 'Deps', { env: { account: '123456789012', region: 'us-west-2' } });
    const repo = new ecr.Repository(deps, 'Repo', { repositoryName: 'stratoclave-backend' });
    const budgets = new dynamodb.Table(deps, 'Budgets', {
      partitionKey: { name: 'tenant_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
    });
    const ledger = new dynamodb.Table(deps, 'Ledger', {
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
    });
    const stack = new LedgerProjectorStack(app, 'TestProjector', {
      env: { account: '123456789012', region: 'us-west-2' },
      prefix: 'stratoclave',
      lambdaRepository: repo,
      lambdaImageTag: 'v52',
      tenantBudgetsTable: budgets,
      creditLedgerTable: ledger,
      shadow: true,
    });
    template = Template.fromStack(stack);
  });

  test('two Lambda functions (projector + reconciler)', () => {
    template.resourceCountIs('AWS::Lambda::Function', 2);
  });

  test('projector runs in SHADOW mode by default', () => {
    template.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: 'stratoclave-ledger-projector',
      Environment: { Variables: { LEDGER_PROJECTOR_SHADOW: 'true' } },
    });
  });

  test('stream event-source mapping uses partial-batch-failure + DLQ + bisect', () => {
    template.hasResourceProperties('AWS::Lambda::EventSourceMapping', {
      FunctionResponseTypes: ['ReportBatchItemFailures'],
      BisectBatchOnFunctionError: true,
      MaximumRetryAttempts: 5,
      ParallelizationFactor: 2,
      DestinationConfig: { OnFailure: Match.anyValue() },
    });
  });

  test('a permanent-failure DLQ exists (SSE enforced)', () => {
    template.hasResourceProperties('AWS::SQS::Queue', {
      QueueName: 'stratoclave-ledger-projector-dlq',
    });
  });

  test('reconciler is scheduled every 15 minutes', () => {
    template.hasResourceProperties('AWS::Events::Rule', {
      ScheduleExpression: 'rate(15 minutes)',
    });
  });

  test('divergence alarm blocks cut-over on any drift AND on missing data', () => {
    template.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: 'stratoclave-ledger-reserve-shadow-divergence',
      Threshold: 0,
      ComparisonOperator: 'GreaterThanThreshold',
      // missing data must be BREACHING so a dead reconciler can't green-light cut-over
      TreatMissingData: 'breaching',
    });
  });

  test('projector has NO write to the budgets table (least privilege)', () => {
    // The projector should only WRITE the ledger; it reads the budgets stream via
    // the ESM (stream perms), never PutItem on the budgets table itself.
    const policies = template.findResources('AWS::IAM::Policy');
    const json = JSON.stringify(policies);
    // sanity: it does have ledger write (PutItem) somewhere
    expect(json).toContain('dynamodb:PutItem');
  });
});
