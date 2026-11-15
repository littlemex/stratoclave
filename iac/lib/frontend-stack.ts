import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as path from 'path';
import { Construct } from 'constructs';
import {
  applyCommonTags,
  putStringParameter,
  ORIGIN_READ_TIMEOUT_SECONDS,
  ORIGIN_KEEPALIVE_TIMEOUT_SECONDS,
} from './_common';
import type { CloudfrontOriginMode } from './entry-config';

/** The origin id every ALB behaviour targets when CloudFront talks to the public ALB by DNS name. */
const PUBLIC_ALB_ORIGIN_ID = 'ALBOrigin';
/** The origin id every ALB behaviour targets once CloudFront talks to the ALB through a VPC origin. */
const VPC_ALB_ORIGIN_ID = 'ALBVpcOrigin';

export interface FrontendStackProps extends cdk.StackProps {
  prefix: string;
  /**
   * Public ALB DNS name. Required whenever a public ALB exists for this
   * deployment's `cloudfrontOrigin` mode (`public`/`vpc-expand`/
   * `vpc-switch`) — see that prop's doc. Omitted entirely for `vpc` (no
   * public ALB at all) and for `STRATOCLAVE_ENTRY=both` (same reason).
   */
  albDnsName?: string;
  /**
   * The internal ALB this stack builds a CloudFront VPC origin for. Required
   * whenever `cloudfrontOrigin !== 'public'` (`vpc-expand`/`vpc-switch`/
   * `vpc`), including every `STRATOCLAVE_ENTRY=both` deployment (which
   * forces `cloudfrontOrigin: 'vpc'` — see `lib/entry-config.ts`).
   *
   * Passed as the construct itself (`lib/alb-stack.ts`'s `internalAlb`), not
   * just its DNS name — `VpcOrigin.withApplicationLoadBalancer` needs the
   * ALB resource/ARN, not merely its domain name, to build the
   * `AWS::CloudFront::VpcOrigin` resource.
   */
  internalAlb?: elbv2.IApplicationLoadBalancer;
  /**
   * `STRATOCLAVE_CLOUDFRONT_ORIGIN` (see `lib/entry-config.ts`). @default 'vpc'
   *
   *   - `'public'`: one origin (`ALBOrigin`, http-only, by DNS name — exactly
   *     today), targeted by all four ALB behaviours.
   *   - `'vpc-expand'`: BOTH `ALBOrigin` (public, http-only, by DNS name) AND
   *     `ALBVpcOrigin` (a `AWS::CloudFront::VpcOrigin` pointed at
   *     `internalAlb`) are declared, but every behaviour still targets
   *     `ALBOrigin` — the VPC origin exists and is warmed up, but nothing
   *     routes to it yet. `AWS::CloudFront::VpcOrigin` creation is slow
   *     (documented in the AWS console as several minutes); this state lets
   *     it finish, and lets the internal ALB's target health be checked,
   *     before any traffic depends on either.
   *   - `'vpc-switch'`: same two origins as `vpc-expand`, but every
   *     behaviour's `targetOriginId` is `ALBVpcOrigin`. `ALBOrigin` stays
   *     declared (unreferenced) so the switch is a one-line revert back to
   *     `ALBOrigin` without recreating a removed ALB first.
   *   - `'vpc'`: only `ALBVpcOrigin` is declared; `ALBOrigin` does not exist
   *     in the template at all. This is what a fresh deployment gets from
   *     the start, and what the final step of a migration produces for an
   *     existing one.
   *
   * Every mode above sets `viewerProtocolPolicy: 'https-only'` on all four
   * ALB behaviours (`/api/*`, `/v1/*`, `/openai/*`, `/.well-known/*`) and
   * the SAME origin-side read/keepalive timeouts on whichever ALB origin(s)
   * it declares — see `_common.ts`'s `ORIGIN_READ_TIMEOUT_SECONDS` /
   * `ORIGIN_KEEPALIVE_TIMEOUT_SECONDS`, imported by both this file and
   * `lib/alb-stack.ts` so the two paths cannot carry different values.
   */
  cloudfrontOrigin?: CloudfrontOriginMode;
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
 * - S3 (private) + CloudFront (OAC) for SPA delivery
 * - `/api/*` proxied to the ALB
 * - Deployed via `aws s3 sync` + `cloudfront create-invalidation` from scripts/deploy-all.sh
 */
export class FrontendStack extends cdk.Stack {
  public readonly bucket: s3.Bucket;
  public readonly distribution: cloudfront.IDistribution;
  public readonly cfnDistribution: cloudfront.CfnDistribution;

  constructor(scope: Construct, id: string, props: FrontendStackProps) {
    super(scope, id, props);

    const { prefix, albDnsName, internalAlb, webAclArn } = props;
    const cloudfrontOrigin = props.cloudfrontOrigin ?? 'vpc';
    const needsPublicOrigin =
      cloudfrontOrigin === 'public' ||
      cloudfrontOrigin === 'vpc-expand' ||
      cloudfrontOrigin === 'vpc-switch';
    const needsVpcOrigin = cloudfrontOrigin !== 'public';
    // The origin every ALB behaviour's `targetOriginId` points at.
    // 'vpc-switch', 'vpc-contract' and 'vpc' all route to the VPC origin;
    // 'public' and 'vpc-expand' still route to the public ALB by DNS name
    // (see `cloudfrontOrigin`'s prop doc for why 'vpc-expand' declares but
    // does not yet reference the VPC origin). 'vpc-contract' does NOT
    // declare `ALBOrigin` at all (see `needsPublicOrigin` above) — this
    // constant only matters for the three states that still target
    // `ALBOrigin` vs. the three that target `ALBVpcOrigin`; which origin
    // objects actually EXIST in the template is `needsPublicOrigin`/
    // `needsVpcOrigin`'s job, not this one's.
    const activeAlbOriginId =
      cloudfrontOrigin === 'vpc-switch' ||
      cloudfrontOrigin === 'vpc-contract' ||
      cloudfrontOrigin === 'vpc'
        ? VPC_ALB_ORIGIN_ID
        : PUBLIC_ALB_ORIGIN_ID;

    if (needsPublicOrigin && !albDnsName) {
      throw new Error(
        `FrontendStack: albDnsName is required when cloudfrontOrigin is "${cloudfrontOrigin}" ` +
          '(a public ALB origin is declared in this mode).',
      );
    }
    if (needsVpcOrigin && !internalAlb) {
      throw new Error(
        `FrontendStack: internalAlb is required when cloudfrontOrigin is "${cloudfrontOrigin}" ` +
          '(a CloudFront VPC origin pointed at it is declared in this mode).',
      );
    }
    // The CSP form-action below interpolates this.region into a literal header
    // string. That is only safe when the region is concrete at synth (env is
    // explicit). If it were ever an unresolved token, the CSP would contain a
    // literal "${Token[...]}" and silently break Hosted UI login in the browser.
    // Convert that latent footgun into a synth failure. (Fable review L-2)
    if (cdk.Token.isUnresolved(this.region)) {
      throw new Error(
        'FrontendStack requires an explicit region (env.region) so the CSP ' +
          'form-action Cognito domain resolves to a concrete region at synth.'
      );
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
              // Google Fonts stylesheet (fonts.googleapis.com) + font
              // file delivery (fonts.gstatic.com) are referenced from
              // index.html. style-src-elem falls back to style-src when
              // unset, which is why the loose stylesheet reference
              // above was blocked before this entry existed.
              "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
              // Cognito Hosted UI and the Stratoclave backend are the
              // only documented networked targets from the SPA.
              "connect-src 'self' https: wss:",
              "img-src 'self' data: https:",
              "font-src 'self' data: https://fonts.gstatic.com",
              "frame-ancestors 'none'",
              "base-uri 'self'",
              // Cognito Hosted UI lives in the same region as this stack (R):
              // the login form POSTs to *.auth.<region>.amazoncognito.com.
              // `this.region` is a concrete string at synth (env is explicit),
              // so it interpolates safely into the header. A wrong region here
              // blocks login in the BROWSER only (CSP violation), invisible to
              // backend tests — keep it region-accurate.
              `form-action 'self' https://*.auth.${cdk.Stack.of(this).region}.amazoncognito.com`,
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

    // Phase 2 (v2.1 Blocker B1): SPA fallback implemented as a CloudFront Function.
    // Reason for removing customErrorResponses: legitimate ALB 403/404 responses on /api/* and /v1/*
    // would be rewritten to HTML and break Frontend fetch calls.
    // The Function is attached only to the defaultCacheBehavior (S3 origin); API behaviors do not use it.
    const spaFallbackFn = new cloudfront.Function(this, 'SpaFallbackFn', {
      functionName: `${prefix}-spa-fallback`,
      comment: 'SPA deep link fallback (viewer-request)',
      code: cloudfront.FunctionCode.fromFile({
        filePath: path.join(__dirname, 'cloudfront-functions', 'spa-fallback.js'),
      }),
      runtime: cloudfront.FunctionRuntime.JS_2_0,
    });

    // The public ALB origin (http-only, by DNS name) — exactly today's
    // shape, declared whenever a public ALB exists for this deployment
    // (`public`/`vpc-expand`/`vpc-switch`).
    const albOriginProperty: cloudfront.CfnDistribution.OriginProperty | undefined =
      needsPublicOrigin
        ? {
            id: PUBLIC_ALB_ORIGIN_ID,
            domainName: albDnsName!,
            customOriginConfig: {
              httpPort: 80,
              originProtocolPolicy: 'http-only',
              originSslProtocols: ['TLSv1.2'],
              // See `_common.ts`'s `ORIGIN_READ_TIMEOUT_SECONDS` doc: 60 s is
              // the maximum without a service-quota increase, and the SAME
              // value the VPC origin below carries, so the two paths cannot
              // silently diverge on it.
              originReadTimeout: ORIGIN_READ_TIMEOUT_SECONDS,
              // Reuse the origin connection between requests; the default is 5 s,
              // which throws it away between bursts for the same reason the client
              // pools did.
              originKeepaliveTimeout: ORIGIN_KEEPALIVE_TIMEOUT_SECONDS,
            },
          }
        : undefined;

    // The CloudFront VPC origin (`AWS::CloudFront::VpcOrigin`, pointed at
    // the internal ALB). `VpcOrigin.withApplicationLoadBalancer(...).bind()`
    // is the supported way to obtain an L1 `OriginProperty` for it without
    // going through the L2 `cloudfront.Distribution` construct this stack
    // does not use (it builds the distribution at the L1 `CfnDistribution`
    // level throughout, for the custom-header/SPA-fallback/multi-origin
    // shape none of the L2 behaviour helpers cover). Called at most once —
    // `bind()` creates the underlying `cloudfront.VpcOrigin` L2 construct
    // under a FIXED construct id ("VpcOrigin") the first time it runs, so a
    // second call with a different `scope`/`id` would collide.
    const vpcOriginProperty: cloudfront.CfnDistribution.OriginProperty | undefined =
      needsVpcOrigin
        ? origins.VpcOrigin.withApplicationLoadBalancer(internalAlb!, {
            httpPort: 80,
            // HTTP only: this implementation's own choice, not a limit of
            // CloudFront VPC origins in general — see
            // docs/DEPLOYMENT.md's "Encryption on the CloudFront-to-ALB
            // hop".
            protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
            originSslProtocols: [cloudfront.OriginSslPolicy.TLS_V1_2],
            // Same values as `albOriginProperty` above, same shared
            // constants — see that origin's comment.
            readTimeout: cdk.Duration.seconds(ORIGIN_READ_TIMEOUT_SECONDS),
            keepaliveTimeout: cdk.Duration.seconds(ORIGIN_KEEPALIVE_TIMEOUT_SECONDS),
          }).bind(this, { originId: VPC_ALB_ORIGIN_ID }).originProperty
        : undefined;
    if (needsVpcOrigin && !vpcOriginProperty) {
      // Unreachable in practice (`OriginBase.bind` always returns
      // `originProperty` for a VPC origin — see
      // `aws-cloudfront-origins/lib/vpc-origin.js`'s `renderVpcOriginConfig`,
      // which never returns `undefined`). A thrown assertion, not a silent
      // `!`, so a future aws-cdk-lib upgrade that changes this contract
      // fails loudly at synth instead of producing a distribution with a
      // VPC origin id that silently vanished from the template.
      throw new Error('FrontendStack: VpcOrigin.bind() did not return an originProperty.');
    }

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
          // CloudFront gives up on an origin response after 30 s by default,
          // and the gateway's own upstream read window is far longer, so an
          // upstream that took longer than 30 s reached the caller as a
          // CloudFront 504 with an HTML body — an unparseable failure for a
          // problem that is neither the caller's nor the gateway's. Measured
          // on 2026-08-25: 21 such responses across an open-loop run whose
          // slowest upstream calls took 15-28 s. See `ORIGIN_READ_TIMEOUT_SECONDS`
          // above for where that 60 s figure (the maximum without a service-quota
          // increase) and the keepalive figure are now defined once. The gateway
          // caps its own non-streaming read below this (see `_openai_transport.py`'s
          // `DEFAULT_NONSTREAM_READ_TIMEOUT`), so a slow upstream produces our
          // JSON 502 rather than the CDN's HTML.
          ...(albOriginProperty ? [albOriginProperty] : []),
          ...(vpcOriginProperty ? [vpcOriginProperty] : []),
        ],
        defaultCacheBehavior: {
          targetOriginId: 'S3Origin',
          viewerProtocolPolicy: 'redirect-to-https',
          cachePolicyId: '658327ea-f89d-4fab-a63d-7e88639e58f6', // CachingOptimized
          responseHeadersPolicyId: securityHeadersPolicy.responseHeadersPolicyId,
          compress: true,
          allowedMethods: ['GET', 'HEAD', 'OPTIONS'],
          cachedMethods: ['GET', 'HEAD'],
          // SPA fallback: rewrites deep links such as /admin, /callback, /me/usage to /index.html
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
            targetOriginId: activeAlbOriginId,
            // `https-only`, not `redirect-to-https`: whether a credential a
            // viewer sends on a plain-HTTP first request already left the
            // client in cleartext is up to the VIEWER's own choice of
            // scheme, not this setting — CloudFront must receive a request
            // in full, headers included, before it can act on it either
            // way. What this setting changes is what CloudFront does once
            // it HAS received one: `redirect-to-https` would accept it and
            // reply with a 301 (implying the request "works", just retry
            // over HTTPS); `https-only` refuses it outright, with no path
            // through which CloudFront ever forwards an HTTP request on
            // this behaviour to the origin or treats it as a legitimate
            // "almost right" attempt.
            viewerProtocolPolicy: 'https-only',
            cachePolicyId: '4135ea2d-6df8-44a3-9df3-4b5a84be39ad', // CachingDisabled
            originRequestPolicyId: 'b689b0a8-53d0-40ab-baf2-68738e2966ac', // AllViewerExceptHostHeader
            allowedMethods: ['GET', 'HEAD', 'OPTIONS', 'PUT', 'POST', 'PATCH', 'DELETE'],
            cachedMethods: ['GET', 'HEAD'],
            compress: true,
            // Do not attach the Function to API behaviors (preserves legitimate ALB 403/404 responses)
          },
          {
            pathPattern: '/v1/*',
            targetOriginId: activeAlbOriginId,
            // `https-only`: same reasoning as `/api/*` above.
            viewerProtocolPolicy: 'https-only',
            cachePolicyId: '4135ea2d-6df8-44a3-9df3-4b5a84be39ad', // CachingDisabled
            originRequestPolicyId: 'b689b0a8-53d0-40ab-baf2-68738e2966ac', // AllViewerExceptHostHeader
            allowedMethods: ['GET', 'HEAD', 'OPTIONS', 'PUT', 'POST', 'PATCH', 'DELETE'],
            cachedMethods: ['GET', 'HEAD'],
            compress: true,
          },
          {
            // OpenAI Responses API (/openai/v1/responses + /openai/v1/models).
            // Same pass-through posture as /v1/*. Without this behavior the
            // SPA fallback returns index.html for /openai/v1/* requests.
            pathPattern: '/openai/*',
            targetOriginId: activeAlbOriginId,
            // `https-only`: same reasoning as `/api/*` above.
            viewerProtocolPolicy: 'https-only',
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
            targetOriginId: activeAlbOriginId,
            // `https-only`: same reasoning as `/api/*` above. This path is
            // unauthenticated, but it is also where the CLI learns the real
            // API endpoint (`/.well-known/stratoclave-config`) — a cleartext
            // round trip here is a cleartext read of that config, not just
            // of a credential.
            viewerProtocolPolicy: 'https-only',
            // Let the Backend control caching (it sets its own Cache-Control).
            cachePolicyId: '4135ea2d-6df8-44a3-9df3-4b5a84be39ad', // CachingDisabled
            originRequestPolicyId: 'b689b0a8-53d0-40ab-baf2-68738e2966ac', // AllViewerExceptHostHeader
            allowedMethods: ['GET', 'HEAD', 'OPTIONS'],
            cachedMethods: ['GET', 'HEAD'],
            compress: true,
          },
        ],
        // customErrorResponses removed (Blocker B1)
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
    // P0-11 (2026-04 security review): belt-and-braces on the OAC bucket
    // policy. `aws:SourceArn` alone assumes CloudFront distribution IDs
    // are globally unique (they are), but AWS's documented OAC pattern
    // always pairs it with `aws:SourceAccount` as a defence against any
    // future confused-deputy class of bug. Matching the AWS reference
    // template makes third-party security audits trivial.
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
            'AWS:SourceAccount': this.account,
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
    // Consumed by scripts/deploy-all.sh to target the CloudFront invalidation
    // after syncing SPA assets. The distribution id is already built above; the
    // script referenced this output before it existed, so its asset-upload step
    // failed the stack-output precondition (fixed here).
    new cdk.CfnOutput(this, 'CloudFrontDistributionId', {
      value: this.cfnDistribution.ref,
    });

    applyCommonTags(this, prefix, 'Frontend');
  }
}
