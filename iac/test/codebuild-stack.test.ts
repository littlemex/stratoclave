import * as cdk from 'aws-cdk-lib';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { CodeBuildStack } from '../lib/codebuild-stack';

describe('CodeBuildStack', () => {
  let app: cdk.App;
  let repository: ecr.IRepository;
  let stack: CodeBuildStack;
  let template: Template;

  beforeAll(() => {
    app = new cdk.App();

    const ecrStack = new cdk.Stack(app, 'TestEcrStack', {
      env: { account: '123456789012', region: 'us-west-2' },
    });

    repository = new ecr.Repository(ecrStack, 'TestRepo', {
      repositoryName: 'stratoclave-backend',
    });

    stack = new CodeBuildStack(app, 'TestCodeBuildStack', {
      env: { account: '123456789012', region: 'us-west-2' },
      repository,
      ecsClusterName: 'stratoclave-cluster',
      ecsServiceName: 'stratoclave-backend',
    });

    template = Template.fromStack(stack);
  });

  // CB-01: S3 バケットが作成されること (Block Public Access) (P0)
  test('S3 バケットが Block Public Access で作成されること', () => {
    template.hasResourceProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });
  });

  // CB-03: CodeBuild プロジェクトが作成されること (STANDARD_7_0) (P0)
  test('CodeBuild プロジェクトが正しい設定で作成されること', () => {
    template.hasResourceProperties('AWS::CodeBuild::Project', {
      Name: 'stratoclave-backend-build',
      Environment: {
        ComputeType: 'BUILD_GENERAL1_SMALL',
        Image: 'aws/codebuild/standard:7.0',
        Type: 'LINUX_CONTAINER',
        PrivilegedMode: true,
      },
    });
  });

  // CB-05: CodeBuild に ECS UpdateService 権限があること (特定リソースのみ) (P1)
  test('CodeBuild に ECS UpdateService 権限があること', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'ecs:UpdateService',
            Effect: 'Allow',
            Resource: Match.anyValue(),
          }),
        ]),
      },
    });
  });
});
