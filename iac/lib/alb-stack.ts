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
  /** HTTPS 証明書 (optional) */
  certificateArn?: string;
  /** 表示用の独自ドメイン (optional) */
  domainName?: string;
  /**
   * Enable ALB deletion protection. Production must set this to true so
   * that a `cdk destroy` can't take the Backend offline by accident.
   * @default false
   */
  deletionProtection?: boolean;
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
