/**
 * Regression tests for the 2026-06 IaC security-hardening sweep.
 *
 * Each block pins one finding from the audit so a future refactor can
 * not silently revert the fix.
 */
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { CognitoStack } from '../lib/cognito-stack';
import { DynamoDBStack } from '../lib/dynamodb-stack';
import { EcrStack } from '../lib/ecr-stack';

describe('A-01-ecr: ECR repository is IMMUTABLE', () => {
  test('ImageTagMutability == IMMUTABLE', () => {
    const app = new cdk.App();
    const stack = new EcrStack(app, 'EcrImmTest', {
      env: { account: '123456789012', region: 'us-east-1' },
      prefix: 'stratoclave',
    });
    Template.fromStack(stack).hasResourceProperties('AWS::ECR::Repository', {
      ImageTagMutability: 'IMMUTABLE',
    });
  });
});

describe('A-07-dynamo: deletion protection is environment-aware', () => {
  test('production tables enable deletion protection', () => {
    const app = new cdk.App();
    const stack = new DynamoDBStack(app, 'DdbProdTest', {
      env: { account: '123456789012', region: 'us-east-1' },
      prefix: 'stratoclave',
      environment: 'production',
    });
    const template = Template.fromStack(stack);
    // Every DynamoDB table in this stack carries DeletionProtectionEnabled=true
    const tables = template.findResources('AWS::DynamoDB::Table');
    const tableLogicalIds = Object.keys(tables);
    expect(tableLogicalIds.length).toBeGreaterThan(5);
    for (const id of tableLogicalIds) {
      expect(tables[id].Properties.DeletionProtectionEnabled).toBe(true);
    }
  });

  test('development tables do not enable deletion protection', () => {
    const app = new cdk.App();
    const stack = new DynamoDBStack(app, 'DdbDevTest', {
      env: { account: '123456789012', region: 'us-east-1' },
      prefix: 'stratoclave',
      environment: 'development',
    });
    const template = Template.fromStack(stack);
    const tables = template.findResources('AWS::DynamoDB::Table');
    for (const id of Object.keys(tables)) {
      // Either explicitly false, or absent (CFN default off).
      const v = tables[id].Properties.DeletionProtectionEnabled;
      expect(v === false || v === undefined).toBe(true);
    }
  });
});

describe('A-09-cognito / A-20-cognito: Cognito hardens in production', () => {
  test('production caps refresh-token validity at 7 days and RETAINs the pool', () => {
    const app = new cdk.App();
    const stack = new CognitoStack(app, 'CogProdTest', {
      env: { account: '123456789012', region: 'us-east-1' },
      prefix: 'stratoclave',
      environment: 'production',
    });
    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::Cognito::UserPoolClient', {
      RefreshTokenValidity: 7 * 24 * 60, // minutes
    });
    template.hasResource('AWS::Cognito::UserPool', {
      DeletionPolicy: 'Retain',
      UpdateReplacePolicy: 'Retain',
    });
  });

  test('development keeps the legacy 30-day refresh window for ergonomic dev loops', () => {
    const app = new cdk.App();
    const stack = new CognitoStack(app, 'CogDevTest', {
      env: { account: '123456789012', region: 'us-east-1' },
      prefix: 'stratoclave',
      environment: 'development',
    });
    Template.fromStack(stack).hasResourceProperties(
      'AWS::Cognito::UserPoolClient',
      {
        RefreshTokenValidity: 30 * 24 * 60,
      },
    );
  });
});
