import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { EcrStack } from '../lib/ecr-stack';

describe('EcrStack', () => {
  let app: cdk.App;
  let stack: EcrStack;
  let template: Template;

  beforeAll(() => {
    app = new cdk.App();
    stack = new EcrStack(app, 'TestEcrStack', {
      env: { account: '123456789012', region: 'us-west-2' },
      prefix: 'stratoclave',
    });
    template = Template.fromStack(stack);
  });

  // ECR-01: ECR repository is created (P0)
  test('ECR repository is created', () => {
    template.hasResourceProperties('AWS::ECR::Repository', {
      RepositoryName: 'stratoclave-backend',
      ImageScanningConfiguration: {
        ScanOnPush: true,
      },
    });
  });

  // ECR-02: imageScanOnPush is enabled (P1)
  test('imageScanOnPush is enabled', () => {
    template.hasResourceProperties('AWS::ECR::Repository', {
      ImageScanningConfiguration: {
        ScanOnPush: true,
      },
    });
  });

  // ECR-04: removalPolicy is RETAIN (P1)
  test('DeletionPolicy is Retain', () => {
    template.hasResource('AWS::ECR::Repository', {
      DeletionPolicy: 'Retain',
      UpdateReplacePolicy: 'Retain',
    });
  });
});
