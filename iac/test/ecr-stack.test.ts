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
    });
    template = Template.fromStack(stack);
  });

  // ECR-01: ECR リポジトリが作成されること (P0)
  test('ECR リポジトリが作成されること', () => {
    template.hasResourceProperties('AWS::ECR::Repository', {
      RepositoryName: 'stratoclave-backend',
      ImageScanningConfiguration: {
        ScanOnPush: true,
      },
    });
  });

  // ECR-02: imageScanOnPush が有効であること (P1)
  test('imageScanOnPush が有効であること', () => {
    template.hasResourceProperties('AWS::ECR::Repository', {
      ImageScanningConfiguration: {
        ScanOnPush: true,
      },
    });
  });

  // ECR-04: removalPolicy が RETAIN であること (P1)
  test('DeletionPolicy が Retain であること', () => {
    template.hasResource('AWS::ECR::Repository', {
      DeletionPolicy: 'Retain',
      UpdateReplacePolicy: 'Retain',
    });
  });
});
