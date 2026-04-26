import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as path from 'path';
import { Construct } from 'constructs';
import { applyCommonTags, putStringParameter } from './_common';

export interface FrontendStackProps extends cdk.StackProps {
  prefix: string;
  /** API 呼び出しをプロキシする ALB DNS 名 */
  albDnsName: string;
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

    const { prefix, albDnsName } = props;
    if (!albDnsName) {
      throw new Error('albDnsName is required for FrontendStack');
    }

    this.bucket = new s3.Bucket(this, 'FrontendBucket', {
      bucketName: `${prefix}-web-${this.account}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
    });

    const originAccessIdentity = new cloudfront.OriginAccessIdentity(
      this,
      'FrontendOAI',
      { comment: `OAI for ${prefix} frontend` }
    );
    this.bucket.grantRead(originAccessIdentity);

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
        origins: [
          {
            id: 'S3Origin',
            domainName: this.bucket.bucketRegionalDomainName,
            s3OriginConfig: {
              originAccessIdentity: `origin-access-identity/cloudfront/${originAccessIdentity.originAccessIdentityId}`,
            },
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
