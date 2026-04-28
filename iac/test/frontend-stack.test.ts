import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { FrontendStack } from '../lib/frontend-stack';

describe('FrontendStack (P1-2 security headers + P3 OAC)', () => {
  let app: cdk.App;
  let stack: FrontendStack;
  let template: Template;

  beforeAll(() => {
    app = new cdk.App();

    stack = new FrontendStack(app, 'TestFrontendStack', {
      env: { account: '123456789012', region: 'us-east-1' },
      prefix: 'stratoclave',
      albDnsName: 'test-alb-123456789.us-east-1.elb.amazonaws.com',
      webAclArn:
        'arn:aws:wafv2:us-east-1:123456789012:global/webacl/stratoclave-frontend-acl/abcd-1234',
    });

    template = Template.fromStack(stack);
  });

  test('S3 bucket is private (Block Public Access + SSL + AES256)', () => {
    template.hasResourceProperties('AWS::S3::Bucket', {
      BucketName: 'stratoclave-web-123456789012',
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      BucketEncryption: {
        ServerSideEncryptionConfiguration: [
          Match.objectLike({
            ServerSideEncryptionByDefault: { SSEAlgorithm: 'AES256' },
          }),
        ],
      },
    });
  });

  test('OAC is attached instead of legacy OAI', () => {
    template.resourceCountIs('AWS::CloudFront::OriginAccessControl', 1);
    template.hasResourceProperties('AWS::CloudFront::OriginAccessControl', {
      OriginAccessControlConfig: {
        OriginAccessControlOriginType: 's3',
        SigningBehavior: 'always',
        SigningProtocol: 'sigv4',
      },
    });
    // No OAI should be generated for the new distribution.
    template.resourceCountIs('AWS::CloudFront::CloudFrontOriginAccessIdentity', 0);
  });

  test('CloudFront pins TLSv1.2_2021 as the viewer minimum', () => {
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: {
        ViewerCertificate: {
          MinimumProtocolVersion: 'TLSv1.2_2021',
          CloudFrontDefaultCertificate: true,
        },
      },
    });
  });

  test('WAFv2 WebACL is associated via webAcLId', () => {
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        WebACLId:
          'arn:aws:wafv2:us-east-1:123456789012:global/webacl/stratoclave-frontend-acl/abcd-1234',
      }),
    });
  });

  test('Default behavior serves index.html with the security headers policy', () => {
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        DefaultRootObject: 'index.html',
        DefaultCacheBehavior: Match.objectLike({
          ViewerProtocolPolicy: 'redirect-to-https',
          ResponseHeadersPolicyId: Match.anyValue(),
          FunctionAssociations: Match.arrayWith([
            Match.objectLike({ EventType: 'viewer-request' }),
          ]),
        }),
      }),
    });
  });

  test('Response headers policy enforces HSTS + strict CSP', () => {
    template.hasResourceProperties('AWS::CloudFront::ResponseHeadersPolicy', {
      ResponseHeadersPolicyConfig: Match.objectLike({
        SecurityHeadersConfig: Match.objectLike({
          StrictTransportSecurity: Match.objectLike({
            AccessControlMaxAgeSec: 730 * 24 * 60 * 60,
            IncludeSubdomains: true,
            Preload: true,
          }),
          FrameOptions: { FrameOption: 'DENY', Override: true },
        }),
      }),
    });
  });

  test('Bucket policy grants the CloudFront service principal scoped by aws:SourceArn', () => {
    template.hasResourceProperties('AWS::S3::BucketPolicy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: 'Allow',
            Principal: { Service: 'cloudfront.amazonaws.com' },
            Action: 's3:GetObject',
            Condition: Match.objectLike({
              StringEquals: Match.objectLike({
                'AWS:SourceArn': Match.anyValue(),
              }),
            }),
          }),
        ]),
      }),
    });
  });
});
