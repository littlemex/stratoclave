import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { DynamoDBStack } from '../lib/dynamodb-stack';

/**
 * F2 (CONTRACT-F2-grant.md) — the quota-events table itself: PAY_PER_REQUEST,
 * NO stream, no TTL, and BOTH GSIs the sweeper and the approver's list
 * depend on.
 *
 * Seam amendment B9 (SEAMS.md S8): the stream was originally specified ON
 * ("NEW_AND_OLD_IMAGES ... so PR 3 needs no table change"), but F3 has no
 * event-source mapping, consumer, permissions or DLQ in scope and built
 * expiry attribution from durable grant records instead. The stream is
 * deleted from this contract, not enabled speculatively — enabling one later
 * is a one-line table change at that time. This test asserts its ABSENCE;
 * it fails today for the opposite reason it originally did (there is no
 * table at all yet, so there is also no stream) and will keep failing for
 * the right reason if a future edit adds the table WITH a stream.
 *
 * design-F2.md's load-bearing property for R4: `grant-expiry-index` must be
 * SPARSE by construction — its PK attribute (`grant_status`) is written only
 * while a grant is ACTIVE. CloudFormation/CDK cannot express "sparse" as a
 * table property (sparseness is a write-time behaviour, not a schema flag),
 * so the closest a synth-time test can assert is the INDEX SHAPE (PK
 * `grant_status`, SK `expires_at`, the exact projected attributes the
 * sweeper needs so it requires no second read) — sparseness itself is
 * covered by the Python unit tests in `backend/tests/test_quota_sweeper.py`.
 *
 * `DynamoDBStack` does not define this table at all today, so every
 * assertion below fails against the CURRENT stack.
 */

let template: Template;

beforeAll(() => {
  const app = new cdk.App();
  const stack = new DynamoDBStack(app, 'TestDynamo', {
    env: { account: '123456789012', region: 'us-west-2' },
    prefix: 'stratoclave',
    environment: 'development',
  });
  template = Template.fromStack(stack);
});

test('quota-events table exists, PAY_PER_REQUEST, NO stream (B9), no TTL', () => {
  template.hasResourceProperties('AWS::DynamoDB::Table', {
    TableName: 'stratoclave-quota-events',
    BillingMode: 'PAY_PER_REQUEST',
    KeySchema: [
      { AttributeName: 'pk', KeyType: 'HASH' },
      { AttributeName: 'sk', KeyType: 'RANGE' },
    ],
  });
  const tables = template.findResources('AWS::DynamoDB::Table', {
    Properties: { TableName: 'stratoclave-quota-events' },
  });
  const [key] = Object.keys(tables);
  expect(key).toBeDefined();
  expect(tables[key].Properties.TimeToLiveSpecification).toBeUndefined();
  // B9: no stream at all — not NEW_AND_OLD_IMAGES, not any other view type.
  expect(tables[key].Properties.StreamSpecification).toBeUndefined();
});

test('tenant-status-index: PK tenant_id, SK status_created_at', () => {
  template.hasResourceProperties('AWS::DynamoDB::Table', {
    TableName: 'stratoclave-quota-events',
    GlobalSecondaryIndexes: Match.arrayWith([
      Match.objectLike({
        IndexName: 'tenant-status-index',
        KeySchema: [
          { AttributeName: 'tenant_id', KeyType: 'HASH' },
          { AttributeName: 'status_created_at', KeyType: 'RANGE' },
        ],
      }),
    ]),
  });
});

test('grant-expiry-index: PK grant_status, SK expires_at, INCLUDE projection with the six sweeper fields', () => {
  template.hasResourceProperties('AWS::DynamoDB::Table', {
    TableName: 'stratoclave-quota-events',
    GlobalSecondaryIndexes: Match.arrayWith([
      Match.objectLike({
        IndexName: 'grant-expiry-index',
        KeySchema: [
          { AttributeName: 'grant_status', KeyType: 'HASH' },
          { AttributeName: 'expires_at', KeyType: 'RANGE' },
        ],
        Projection: Match.objectLike({
          ProjectionType: 'INCLUDE',
          NonKeyAttributes: Match.arrayWith([
            'grant_id', 'tenant_id', 'approved_amount_microusd',
            'target_pk', 'target_sk', 'period',
          ]),
        }),
      }),
    ]),
  });
});
