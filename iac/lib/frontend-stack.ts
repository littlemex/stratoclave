import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as path from 'path';
import { Construct } from 'constructs';
import { applyCommonTags, putStringParameter } from './_common';

export interface FrontendStackProps extends cdk.StackProps {
  prefix: string;
  /** API 呼び出しをプロキシする ALB DNS 名 */
  albDnsName: string;
  /**
   * WAFv2 WebACL ARN (CLOUDFRONT scope, us-east-1). Attaches the WAF
   * to the distribution. Leave undefined when WAF is intentionally
   * skipped (e.g., local bring-up).
   */
  webAclArn?: string;
}

/**
 * Frontend Stack
 *
 * - S3 (private) + CloudFront (OAI) で SPA 配信
 * - `/api/*` は ALB へプロキシ
 * - デプロイは scripts/deploy-all.sh から `aws s3 sync` + `cloudfront create-invalidation`
 */
export class FrontendStack extends cdk.Stack {
  public readonly bucket: s3.Bucket;
  public readonly distribution: cloudfront.IDistribution;
  public readonly cfnDistribution: cloudfront.CfnDistribution;

  constructor(scope: Construct, id: string, props: FrontendStackProps) {
    super(scope, id, props);

    const { prefix, albDnsName, webAclArn } = props;
    if (!albDnsName) {
      throw new Error('albDnsName is required for FrontendStack');
    }

    // ResponseHeadersPolicy (P1-2): consistent security headers in front
    // of the SPA. Applied to the default (S3) behavior only; API and
    // well-known behaviors pass through the ALB response headers
    // unchanged.
    const securityHeadersPolicy = new cloudfront.ResponseHeadersPolicy(
      this,
      'SecurityHeadersPolicy',
      {
        responseHeadersPolicyName: `${prefix}-security-headers`,
        comment: 'HSTS + CSP + X-Frame-Options for the SPA.',
        securityHeadersBehavior: {
          strictTransportSecurity: {
            accessControlMaxAge: cdk.Duration.days(730),
            includeSubdomains: true,
            preload: true,
            override: true,
          },
          contentTypeOptions: { override: true },
          frameOptions: {
            frameOption: cloudfront.HeadersFrameOption.DENY,
            override: true,
          },
          referrerPolicy: {
            referrerPolicy:
              cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
            override: true,
          },
          xssProtection: { protection: true, modeBlock: true, override: true },
          contentSecurityPolicy: {
            contentSecurityPolicy: [
              "default-src 'self'",
              // Vite injects style/script as first-party; allow data: for
              // the occasional tiny inline asset. No eval().
              "script-src 'self'",
              "style-src 'self' 'unsafe-inline'",
              // Cognito Hosted UI and the Stratoclave backend are the
              // only documented networked targets from the SPA.
              "connect-src 'self' https: wss:",
              "img-src 'self' data: https:",
              "font-src 'self' data:",
              "frame-ancestors 'none'",
              "base-uri 'self'",
              "form-action 'self' https://*.auth.us-east-1.amazoncognito.com https://*.auth.us-west-2.amazoncognito.com",
              'upgrade-insecure-requests',
            ].join('; '),
            override: true,
          },
        },
      },
    );

    this.bucket = new s3.Bucket(this, 'FrontendBucket', {
      bucketName: `${prefix}-web-${this.account}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      // Enforce TLS on every GetObject; OAC uses SigV4 over HTTPS but this
      // also shuts the door on any accidental non-CloudFront caller.
      enforceSSL: true,
    });

    // P3: migrate from Origin Access Identity (legacy, pre-SigV4) to
    // Origin Access Control. OAC uses SigV4 so the bucket can be KMS-
    // encrypted later without re-signing the old OAI principal, and AWS
    // has deprecated new OAI creation for fresh distributions.
    const originAccessControl = new cloudfront.CfnOriginAccessControl(
      this,
      'FrontendOac',
      {
        originAccessControlConfig: {
          name: `${prefix}-frontend-oac`,
          description: `OAC for ${prefix} frontend S3 origin`,
          originAccessControlOriginType: 's3',
          signingBehavior: 'always',
          signingProtocol: 'sigv4',
        },
      }
    );
    const distributionArnPlaceholder = cdk.Fn.sub(
      'arn:${AWS::Partition}:cloudfront::${AWS::AccountId}:distribution/*'
    );

    // Phase 2 (v2.1 Blocker B1): SPA fallback を CloudFront Function で実装
    // customErrorResponses 撤去の理由: /api/* /v1/* の ALB 正当 403/404 も HTML に化けて Frontend fetch を破壊するため。
    // Function は defaultCacheBehavior (S3 origin) のみに attach し、API 系 behavior には attach しない。
    const spaFallbackFn = new cloudfront.Function(this, 'SpaFallbackFn', {
      functionName: `${prefix}-spa-fallback`,
      comment: 'SPA deep link fallback (viewer-request)',
      code: cloudfront.FunctionCode.fromFile({
        filePath: path.join(__dirname, 'cloudfront-functions', 'spa-fallback.js'),
      }),
      runtime: cloudfront.FunctionRuntime.JS_2_0,
    });

    this.cfnDistribution = new cloudfront.CfnDistribution(this, 'FrontendDistribution', {
      distributionConfig: {
        enabled: true,
        comment: `${prefix} Frontend Distribution`,
        defaultRootObject: 'index.html',
        priceClass: 'PriceClass_100',
        // Attach WAF (P1-2). Scoped to CLOUDFRONT in waf-stack.ts so
        // passing the ARN here is the only wiring needed.
        ...(webAclArn ? { webAclId: webAclArn } : {}),
        // Pin the minimum TLS version on the viewer side (P1-2). With
        // the default certificate (cloudfront.net) we still must set
        // `cloudFrontDefaultCertificate: true`, but specifying
        // `minimumProtocolVersion` forces modern TLS for any custom
        // domain added later.
        viewerCertificate: {
          cloudFrontDefaultCertificate: true,
          minimumProtocolVersion: 'TLSv1.2_2021',
        },
        origins: [
          {
            id: 'S3Origin',
            domainName: this.bucket.bucketRegionalDomainName,
            // OAC: empty s3OriginConfig + originAccessControlId is the
            // correct wire format. CloudFormation refuses an empty
            // s3OriginConfig object, so we pass `originAccessIdentity: ''`
            // which the service accepts as "no OAI, use OAC instead".
            s3OriginConfig: {
              originAccessIdentity: '',
            },
            originAccessControlId: originAccessControl.attrId,
          },
          {
            id: 'ALBOrigin',
            domainName: albDnsName,
            customOriginConfig: {
              httpPort: 80,
              originProtocolPolicy: 'http-only',
              originSslProtocols: ['TLSv1.2'],
            },
          },
        ],
        defaultCacheBehavior: {
          targetOriginId: 'S3Origin',
          viewerProtocolPolicy: 'redirect-to-https',
          cachePolicyId: '658327ea-f89d-4fab-a63d-7e88639e58f6', // CachingOptimized
          responseHeadersPolicyId: securityHeadersPolicy.responseHeadersPolicyId,
          compress: true,
          allowedMethods: ['GET', 'HEAD', 'OPTIONS'],
          cachedMethods: ['GET', 'HEAD'],
          // SPA fallback: /admin, /callback, /me/usage 等の deep link を /index.html に書き換え
          functionAssociations: [
            {
              functionArn: spaFallbackFn.functionArn,
              eventType: 'viewer-request',
            },
          ],
        },
        cacheBehaviors: [
          {
            pathPattern: '/api/*',
            targetOriginId: 'ALBOrigin',
            viewerProtocolPolicy: 'redirect-to-https',
            cachePolicyId: '4135ea2d-6df8-44a3-9df3-4b5a84be39ad', // CachingDisabled
            originRequestPolicyId: 'b689b0a8-53d0-40ab-baf2-68738e2966ac', // AllViewerExceptHostHeader
            allowedMethods: ['GET', 'HEAD', 'OPTIONS', 'PUT', 'POST', 'PATCH', 'DELETE'],
            cachedMethods: ['GET', 'HEAD'],
            compress: true,
            // API behavior には Function を attach しない (ALB の正当 403/404 を保護)
          },
          {
            pathPattern: '/v1/*',
            targetOriginId: 'ALBOrigin',
            viewerProtocolPolicy: 'redirect-to-https',
            cachePolicyId: '4135ea2d-6df8-44a3-9df3-4b5a84be39ad', // CachingDisabled
            originRequestPolicyId: 'b689b0a8-53d0-40ab-baf2-68738e2966ac', // AllViewerExceptHostHeader
            allowedMethods: ['GET', 'HEAD', 'OPTIONS', 'PUT', 'POST', 'PATCH', 'DELETE'],
            cachedMethods: ['GET', 'HEAD'],
            compress: true,
          },
          {
            // Serve well-known endpoints (e.g. /.well-known/stratoclave-config)
            // directly from the Backend so CLI bootstrap clients can fetch
            // them without authentication. Do NOT attach spaFallbackFn here.
            pathPattern: '/.well-known/*',
            targetOriginId: 'ALBOrigin',
            viewerProtocolPolicy: 'redirect-to-https',
            // Let the Backend control caching (it sets Cache-Control: public, max-age=300).
            cachePolicyId: '4135ea2d-6df8-44a3-9df3-4b5a84be39ad', // CachingDisabled
            originRequestPolicyId: 'b689b0a8-53d0-40ab-baf2-68738e2966ac', // AllViewerExceptHostHeader
            allowedMethods: ['GET', 'HEAD', 'OPTIONS'],
            cachedMethods: ['GET', 'HEAD'],
            compress: true,
          },
        ],
        // customErrorResponses は撤去 (Blocker B1)
      },
    });

    this.distribution = cloudfront.Distribution.fromDistributionAttributes(
      this,
      'Distribution',
      {
        distributionId: this.cfnDistribution.ref,
        domainName: this.cfnDistribution.attrDomainName,
      }
    );

    // OAC bucket policy: allow only this distribution's SigV4 principal.
    // Must be a Service principal with aws:SourceArn = the distribution's
    // own ARN. We reference it by CFN attribute so the dependency chain
    // (bucket-policy → distribution) is explicit.
    const distributionArn = cdk.Stack.of(this).formatArn({
      service: 'cloudfront',
      region: '',
      resource: 'distribution',
      resourceName: this.cfnDistribution.ref,
      arnFormat: cdk.ArnFormat.SLASH_RESOURCE_NAME,
    });
    // Silence the unused-var lint while keeping the arn helper above
    // available for future per-stack NagSuppressions.
    void distributionArnPlaceholder;
    this.bucket.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'AllowCloudFrontServicePrincipalReadOnly',
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('cloudfront.amazonaws.com')],
        actions: ['s3:GetObject'],
        resources: [this.bucket.arnForObjects('*')],
        conditions: {
          StringEquals: {
            'AWS:SourceArn': distributionArn,
          },
        },
      })
    );

    putStringParameter(this, 'FrontendBucketParam', {
      prefix,
      relativePath: 'frontend/s3-bucket',
      value: this.bucket.bucketName,
      description: 'Frontend static site S3 bucket',
    });
    putStringParameter(this, 'CloudFrontDistIdParam', {
      prefix,
      relativePath: 'cloudfront/distribution-id',
      value: this.cfnDistribution.ref,
      description: 'CloudFront distribution ID (for invalidation)',
    });
    putStringParameter(this, 'CloudFrontDomainParam', {
      prefix,
      relativePath: 'cloudfront/domain',
      value: this.cfnDistribution.attrDomainName,
      description: 'CloudFront distribution domain',
    });

    new cdk.CfnOutput(this, 'FrontendBucketName', { value: this.bucket.bucketName });
    new cdk.CfnOutput(this, 'CloudFrontDomainName', {
      value: this.cfnDistribution.attrDomainName,
    });
    new cdk.CfnOutput(this, 'FrontendUrl', {
      value: `https://${this.cfnDistribution.attrDomainName}`,
    });

    applyCommonTags(this, prefix, 'Frontend');
  }
}
