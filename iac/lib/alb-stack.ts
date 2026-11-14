import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
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
   * Whether to add a port-80 listener at all, when `certificateArn` is set.
   *
   * @default true — exactly today's behaviour: a certificate gets a :80
   * redirect-to-:443 listener alongside :443, so `cloudfront` deployments
   * (which do not set a certificate here at all) and any future ALB that
   * *does* want the redirect are unaffected.
   *
   * `STRATOCLAVE_ENTRY=private` passes `false`. PrivateLink carries no TLS of
   * its own, so the first byte of a port-80 request — including an API key —
   * would already be in cleartext inside the gateway's own VPC before a
   * redirect could happen; no port 80 may be open at all on that path, not
   * even a redirect.
   */
  enableHttpListener?: boolean;
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
}

export class AlbStack extends cdk.Stack {
  public readonly alb: elbv2.ApplicationLoadBalancer;
  public readonly targetGroup: elbv2.ApplicationTargetGroup;
  public readonly httpListener: elbv2.ApplicationListener;
  public readonly httpsListener?: elbv2.ApplicationListener;

  constructor(scope: Construct, id: string, props: AlbStackProps) {
    super(scope, id, props);

    const { prefix } = props;

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
      // `enableHttpListener` defaults true, so an existing caller that never
      // passes it (every `cloudfront` deployment today) still gets the :80
      // redirect exactly as before.
      if (props.enableHttpListener ?? true) {
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
