import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';
import { applyCommonTags, putStringParameter } from './_common';

export interface AlbStackProps extends cdk.StackProps {
  prefix: string;
  vpc: ec2.IVpc;
  securityGroup: ec2.ISecurityGroup;

  /** true = VPC-only ALB / false = internet-facing @default false */
  internal?: boolean;
  /** health check path @default '/health' */
  healthCheckPath?: string;
  /** target port @default 8000 */
  targetPort?: number;
  /** HTTPS certificate (optional) */
  certificateArn?: string;
  /** Custom domain name for display purposes (optional) */
  domainName?: string;
  /**
   * Enable ALB deletion protection. Production must set this to true so
   * that a `cdk destroy` can't take the Backend offline by accident.
   * @default false
   */
  deletionProtection?: boolean;
  /**
   * Whether to add a port-80 :80-redirect-to-:443 listener. Named for what
   * it does when a certificate IS set (the only case that matters: with no
   * certificate, port 80 is the ONLY listener this stack ever creates —
   * there is no TLS to redirect to — so this prop has no effect there at
   * all, and `false` with no `certificateArn` would silently leave port 80
   * open with no listener disabled, the opposite of its name. Synth refuses
   * that combination outright instead of silently ignoring it; see the
   * constructor.
   *
   * @default true — exactly today's behaviour: a certificate gets a :80
   * redirect-to-:443 listener alongside :443, so `cloudfront` deployments
   * (which do not set a certificate here at all) and any future ALB that
   * *does* want the redirect are unaffected.
   *
   * `STRATOCLAVE_ENTRY=private` passes `false` alongside a `certificateArn`.
   * PrivateLink carries no TLS of its own, so the first byte of a port-80
   * request — including an API key — would already be in cleartext inside
   * the gateway's own VPC before a redirect could happen; no port 80 may be
   * open at all on that path, not even a redirect.
   */
  httpRedirectListener?: boolean;
  /**
   * `routing.http.xff_header_processing.mode` ALB attribute. Left unset
   * (AWS default: `append`) unless a caller passes one, so `cloudfront`
   * deployments keep today's behaviour exactly.
   *
   * `STRATOCLAVE_ENTRY=private` passes `'remove'`: on that path every
   * request arrives from the internal NLB, never a client directly, so a
   * client-supplied `X-Forwarded-For` has no legitimate content and must not
   * reach the application — removing it rather than appending to it is what
   * makes that true regardless of what a caller sends. See
   * `backend/core/rate_limit.py` for the consequence (the per-IP limiter
   * falls back to `request.client.host`, which is coarse on this path
   * because every request shares the ALB's own address — documented, not
   * fixed, in docs/DEPLOYMENT.md).
   */
  xffHeaderProcessingMode?: 'append' | 'preserve' | 'remove';
  /**
   * Enable ALB access logs to a dedicated, private S3 bucket this stack
   * creates (SSE-S3, `BLOCK_ALL` public access, TLS-enforced, 90-day
   * lifecycle expiry). Left off by default so `cloudfront` deployments keep
   * today's behaviour exactly (no bucket, no attribute, zero template
   * diff): their request-level forensics come from CloudFront's own edge
   * logs, which this ALB never sees traffic without.
   *
   * `STRATOCLAVE_ENTRY=private` passes `true`. On that path there is no
   * CloudFront, no WAF, and (XFF stripped) no client address in the
   * backend's own logs either — the ALB access log is the only per-request,
   * per-connection record this entry mode has at all, so it is not optional
   * the way it is for `cloudfront`.
   * @default false
   */
  enableAccessLogs?: boolean;
}

export class AlbStack extends cdk.Stack {
  public readonly alb: elbv2.ApplicationLoadBalancer;
  public readonly targetGroup: elbv2.ApplicationTargetGroup;
  public readonly httpListener: elbv2.ApplicationListener;
  public readonly httpsListener?: elbv2.ApplicationListener;

  constructor(scope: Construct, id: string, props: AlbStackProps) {
    super(scope, id, props);

    const { prefix } = props;

    // `httpRedirectListener: false` only means something alongside a
    // certificate: it suppresses the :80-redirect-to-:443 listener that the
    // certificate branch below would otherwise add. With no certificateArn
    // at all, port 80 is the ALB's only listener (plain HTTP forward, no
    // redirect exists to suppress), so `httpRedirectListener: false` with
    // no certificate would silently do nothing — opening port 80 anyway,
    // the opposite of what the prop name promises. Refuse that combination
    // at synth rather than let it look honoured.
    if (props.httpRedirectListener === false && !props.certificateArn) {
      throw new Error(
        'AlbStack: httpRedirectListener: false has no effect without certificateArn — ' +
          'there is no HTTPS listener to redirect to, so port 80 (plain HTTP forward) ' +
          'would stay open regardless, silently ignoring the setting. Pass certificateArn ' +
          'alongside it (as STRATOCLAVE_ENTRY=private does), or drop httpRedirectListener.',
      );
    }

    this.alb = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
      vpc: props.vpc,
      internetFacing: !props.internal,
      securityGroup: props.securityGroup,
      loadBalancerName: `${prefix}-alb`,
      http2Enabled: true,
      // Production defaults to deletion-protected so a stray `cdk destroy`
      // can't wipe the load balancer out from under a live tenant.
      deletionProtection: props.deletionProtection ?? false,
    });

    // See the prop doc: off by default (zero diff for `cloudfront`), on for
    // `STRATOCLAVE_ENTRY=private` where this is the only per-request record
    // the path has. `logAccessLogs` (CDK's L2 convenience) resolves the
    // correct region-specific ELB log-delivery principal itself and adds
    // the bucket policy statements for it; this stack is only responsible
    // for the bucket's own security posture.
    if (props.enableAccessLogs) {
      const accessLogsBucket = new s3.Bucket(this, 'AlbAccessLogsBucket', {
        bucketName: `${prefix}-alb-access-logs-${this.account}`,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
        autoDeleteObjects: true,
        blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
        encryption: s3.BucketEncryption.S3_MANAGED,
        enforceSSL: true,
        lifecycleRules: [
          {
            id: 'expire-after-90-days',
            enabled: true,
            expiration: cdk.Duration.days(90),
          },
        ],
      });
      this.alb.logAccessLogs(accessLogsBucket, 'alb-access-logs');
    }

    this.targetGroup = new elbv2.ApplicationTargetGroup(this, 'BackendTargetGroup', {
      vpc: props.vpc,
      port: props.targetPort || 8000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      targetGroupName: `${prefix}-backend-tg`,
      // Round robin sends the next request to the next target regardless of how
      // much work each is already carrying, which is wrong for a gateway whose
      // requests last from milliseconds to minutes: a target holding several long
      // streams keeps receiving its share. Least-outstanding-requests routes by
      // in-flight count instead, which is the quantity that saturates a task here.
      loadBalancingAlgorithmType:
        elbv2.TargetGroupLoadBalancingAlgorithmType.LEAST_OUTSTANDING_REQUESTS,
      healthCheck: {
        path: props.healthCheckPath || '/health',
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
        protocol: elbv2.Protocol.HTTP,
      },
      deregistrationDelay: cdk.Duration.seconds(30),
    });

    // P1-2c: `open: false` prevents CDK from auto-adding a
    // `0.0.0.0/0 -> :80` ingress to our ALB security group. Inbound is
    // controlled exclusively by network-stack.ts, which only allows the
    // CloudFront origin-facing managed prefix list. Without this flag,
    // CDK would punch a hole we just spent the sweep closing.
    if (props.certificateArn) {
      // `httpRedirectListener` defaults true, so an existing caller that
      // never passes it (every `cloudfront` deployment today) still gets
      // the :80 redirect exactly as before.
      if (props.httpRedirectListener ?? true) {
        this.httpListener = this.alb.addListener('HttpListener', {
          port: 80,
          protocol: elbv2.ApplicationProtocol.HTTP,
          open: false,
          defaultAction: elbv2.ListenerAction.redirect({
            protocol: 'HTTPS',
            port: '443',
            permanent: true,
          }),
        });
      }
      const certificate = acm.Certificate.fromCertificateArn(
        this,
        'Certificate',
        props.certificateArn
      );
      this.httpsListener = this.alb.addListener('HttpsListener', {
        port: 443,
        protocol: elbv2.ApplicationProtocol.HTTPS,
        open: false,
        certificates: [certificate],
        defaultAction: elbv2.ListenerAction.forward([this.targetGroup]),
      });
    } else {
      this.httpListener = this.alb.addListener('HttpListener', {
        port: 80,
        protocol: elbv2.ApplicationProtocol.HTTP,
        open: false,
        defaultAction: elbv2.ListenerAction.forward([this.targetGroup]),
      });
    }

    // XFF handling on the ALB itself (see the prop doc for why `private`
    // passes 'remove'). Unset leaves AWS's own default ('append') in force,
    // so a caller that never passes this — every `cloudfront` deployment
    // today — produces zero template diff.
    if (props.xffHeaderProcessingMode) {
      this.alb.setAttribute(
        'routing.http.xff_header_processing.mode',
        props.xffHeaderProcessingMode,
      );
    }

    putStringParameter(this, 'AlbDnsParam', {
      prefix,
      relativePath: 'alb/dns-name',
      value: this.alb.loadBalancerDnsName,
      description: 'ALB DNS name',
    });
    putStringParameter(this, 'AlbArnParam', {
      prefix,
      relativePath: 'alb/arn',
      value: this.alb.loadBalancerArn,
      description: 'ALB ARN',
    });
    putStringParameter(this, 'AlbTargetGroupArnParam', {
      prefix,
      relativePath: 'alb/target-group-arn',
      value: this.targetGroup.targetGroupArn,
      description: 'ALB Target Group ARN',
    });

    new cdk.CfnOutput(this, 'AlbDnsName', { value: this.alb.loadBalancerDnsName });
    new cdk.CfnOutput(this, 'AlbArn', { value: this.alb.loadBalancerArn });

    applyCommonTags(this, prefix, 'ALB');
  }
}
