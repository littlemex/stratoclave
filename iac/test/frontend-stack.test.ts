import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { FrontendStack } from '../lib/frontend-stack';

describe('FrontendStack', () => {
  let app: cdk.App;
  let stack: FrontendStack;
  let template: Template;

  beforeAll(() => {
    app = new cdk.App();

    stack = new FrontendStack(app, 'TestFrontendStack', {
      env: { account: '123456789012', region: 'us-west-2' },
      prefix: 'stratoclave',
      albDnsName: 'test-alb-123456789.us-west-2.elb.amazonaws.com',
    });

    template = Template.fromStack(stack);
  });

  // FE-01: S3 バケットが Block Public Access で作成されること (P0)
  test('S3 バケットが Block Public Access で作成されること', () => {
    template.hasResourceProperties('AWS::S3::Bucket', {
      BucketName: 'stratoclave-frontend-123456789012',
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      BucketEncryption: {
        ServerSideEncryptionConfiguration: [
          {
            ServerSideEncryptionByDefault: {
              SSEAlgorithm: 'AES256',
            },
          },
        ],
      },
    });
  });

  // FE-02: CloudFront Distribution が作成されること (P0)
  test('CloudFront Distribution が作成されること', () => {
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: {
        Comment: 'Stratoclave Frontend Distribution',
        DefaultRootObject: 'index.html',
        Enabled: true,
      },
    });
  });

  // FE-03: CloudFront がデフォルトで HTTPS リダイレクトすること (P0)
  test('CloudFront がデフォルトで HTTPS リダイレクトすること', () => {
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: {
        DefaultCacheBehavior: {
          ViewerProtocolPolicy: 'redirect-to-https',
        },
      },
    });
  });
});
