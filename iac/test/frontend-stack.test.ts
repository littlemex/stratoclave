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

  test('CSP form-action targets the stack region Cognito domain (us-east-1 here)', () => {
    // Region decoupling (v2.2): the form-action allows only the same-region
    // Cognito Hosted UI domain, dynamically derived from the stack region — no
    // hardcoded us-east-1/us-west-2 pair. A wrong region here breaks login in
    // the browser only, so pin it in a test.
    template.hasResourceProperties('AWS::CloudFront::ResponseHeadersPolicy', {
      ResponseHeadersPolicyConfig: Match.objectLike({
        SecurityHeadersConfig: Match.objectLike({
          ContentSecurityPolicy: Match.objectLike({
            ContentSecurityPolicy: Match.stringLikeRegexp(
              "form-action 'self' https://\\*\\.auth\\.us-east-1\\.amazoncognito\\.com",
            ),
          }),
        }),
      }),
    });
    // The retired hardcoded us-west-2 entry must be gone.
    const policies = template.findResources('AWS::CloudFront::ResponseHeadersPolicy');
    const csp = JSON.stringify(policies);
    expect(csp).not.toContain('auth.us-west-2.amazoncognito.com');
  });

  test('CSP form-action follows a non-us-east-1 body region (eu-west-1)', () => {
    // Prove the region is dynamic: an eu-west-1 stack emits the eu-west-1
    // Cognito domain, never us-east-1.
    const euApp = new cdk.App();
    const euStack = new FrontendStack(euApp, 'EuFrontendStack', {
      env: { account: '123456789012', region: 'eu-west-1' },
      crossRegionReferences: true,
      prefix: 'stratoclave',
      albDnsName: 'test-alb-123456789.eu-west-1.elb.amazonaws.com',
      webAclArn:
        'arn:aws:wafv2:us-east-1:123456789012:global/webacl/stratoclave-frontend-acl/abcd-1234',
    });
    const euTemplate = Template.fromStack(euStack);
    euTemplate.hasResourceProperties('AWS::CloudFront::ResponseHeadersPolicy', {
      ResponseHeadersPolicyConfig: Match.objectLike({
        SecurityHeadersConfig: Match.objectLike({
          ContentSecurityPolicy: Match.objectLike({
            ContentSecurityPolicy: Match.stringLikeRegexp(
              "form-action 'self' https://\\*\\.auth\\.eu-west-1\\.amazoncognito\\.com",
            ),
          }),
        }),
      }),
    });
    const euCsp = JSON.stringify(
      euTemplate.findResources('AWS::CloudFront::ResponseHeadersPolicy'),
    );
    expect(euCsp).not.toContain('auth.us-east-1.amazoncognito.com');
  });

  test('Env-agnostic stack (token region) is rejected at synth', () => {
    // The CSP interpolates the region literally; an unresolved region token
    // would silently produce a broken header. Guard converts it to a throw.
    const tokenApp = new cdk.App();
    expect(
      () =>
        new FrontendStack(tokenApp, 'TokenFrontendStack', {
          // No env → region is an unresolved token.
          prefix: 'stratoclave',
          albDnsName: 'test-alb.example.elb.amazonaws.com',
        }),
    ).toThrow(/explicit region/);
  });
});
