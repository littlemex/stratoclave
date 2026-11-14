import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as elbv2_targets from 'aws-cdk-lib/aws-elasticloadbalancingv2-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { applyCommonTags, putStringParameter } from './_common';

export interface PrivateLinkStackProps extends cdk.StackProps {
  prefix: string;
  vpc: ec2.IVpc;
  /**
   * The internal ALB's security group. This stack adds its one ingress rule
   * — from the NLB's own security group, on :443 — once the NLB's security
   * group exists; `lib/network-stack.ts` creates this group with no ingress
   * rules at all in `private` mode precisely because that rule belongs here.
   */
  albSecurityGroup: ec2.ISecurityGroup;
  /**
   * The ALB's HTTPS :443 listener (`AlbStack.httpsListener`, only ever
   * created when `AlbStack` is given a certificate). Using the listener
   * rather than the bare ALB ARN gets `AlbListenerTarget`'s automatic
   * `addDependency` on it for free, so the NLB's target group cannot be
   * created before the ALB actually has something listening on the port it
   * targets.
   */
  albHttpsListener: elbv2.ApplicationListener;
  /** `STRATOCLAVE_PRIVATE_DOMAIN` — used only to compose the `PrivateUrl` output. */
  domainName: string;
  /** `STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS`, already validated non-empty and non-wildcard. */
  allowedPrincipals: string[];
}

/**
 * The private-entry publication (`STRATOCLAVE_ENTRY=private`): an internal
 * Network Load Balancer fronting the internal ALB, published through a VPC
 * endpoint service with acceptance required.
 *
 * The NLB hop is structural, not a design choice this slice could have
 * avoided: a VPC endpoint service configuration accepts only a network or
 * gateway load balancer (`create-vpc-endpoint-service-configuration` takes
 * `--network-load-balancer-arns` / `--gateway-load-balancer-arns`, never an
 * ALB ARN directly), so "PrivateLink straight to an ALB" is not a real
 * option.
 *
 * Three things worth being exact about, and where each is handled:
 *
 *   - TLS terminates at the ALB, not the NLB: the NLB listener below is TCP,
 *     not TLS. A TLS listener here would mean the NLB holds the certificate
 *     and talks plaintext to the ALB, which is exactly backwards from "TLS
 *     terminates inside the gateway's own VPC, with your certificate."
 *   - The NLB's security group carries no ingress rules, and PrivateLink
 *     traffic still reaches it, because
 *     `enforceSecurityGroupInboundRulesOnPrivateLinkTraffic: false` tells the
 *     NLB to evaluate PrivateLink-origin traffic against the endpoint
 *     service's own acceptance + allowed-principals gate instead of this
 *     security group. A workload inside the gateway's own VPC that tried to
 *     reach the NLB directly (not through PrivateLink) is a different
 *     traffic class and is not exempted by this flag; nothing in this VPC
 *     has a route to behave as if it already were a PrivateLink consumer.
 *     CDK's `NetworkLoadBalancerProps` exposes this as a first-class boolean
 *     (`aws-cdk-lib` 2.276), so no `CfnLoadBalancer` escape hatch is needed
 *     for it.
 *   - The ALB's own security group admits only this NLB's security group on
 *     :443 — never the internet, never "anyone in the VPC" — so the one
 *     network-layer control standing behind "acceptance required" is this
 *     exact chain: endpoint service -> NLB (PrivateLink-exempted SG) -> ALB
 *     (NLB-SG-only ingress) -> ECS task (ALB-SG-only ingress, already true
 *     today in `lib/network-stack.ts`). The NLB's own egress is scoped the
 *     same way, to the ALB security group on :443 only — it never gets
 *     `allowAllOutbound`.
 */
export class PrivateLinkStack extends cdk.Stack {
  public readonly nlb: elbv2.NetworkLoadBalancer;
  public readonly endpointServiceName: string;
  public readonly privateUrl: string;

  constructor(scope: Construct, id: string, props: PrivateLinkStackProps) {
    super(scope, id, props);

    const { prefix } = props;

    // No ingress rules at construction: PrivateLink traffic bypasses this
    // group entirely (enforceSecurityGroupInboundRulesOnPrivateLinkTraffic:
    // false, below), and nothing else in this VPC is meant to reach the NLB.
    //
    // Egress is NOT `allowAllOutbound`: the only traffic this NLB ever
    // originates is forwarding to, and health-checking, its one target — the
    // internal ALB on :443 — so egress is scoped to exactly that, not left
    // open to the whole internet from inside the gateway's own VPC.
    const nlbSecurityGroup = new ec2.SecurityGroup(this, 'NlbSecurityGroup', {
      vpc: props.vpc,
      securityGroupName: `${prefix}-nlb-sg`,
      description: `Security group for ${prefix} internal NLB (PrivateLink publication)`,
      allowAllOutbound: false,
    });
    nlbSecurityGroup.addEgressRule(
      props.albSecurityGroup,
      ec2.Port.tcp(443),
      'Allow HTTPS to the internal ALB only (forwarding and health checks)',
    );

    this.nlb = new elbv2.NetworkLoadBalancer(this, 'Nlb', {
      vpc: props.vpc,
      internetFacing: false,
      loadBalancerName: `${prefix}-nlb`,
      securityGroups: [nlbSecurityGroup],
      // See the class doc (V2): PrivateLink-origin traffic is gated by the
      // endpoint service's acceptance + allowed-principals, not by this
      // security group having to carry a rule for every accepted consumer's
      // address range (which it could not even express — VPC endpoint ENIs
      // are allocated in the CONSUMER's VPC, not advertised as CIDRs here).
      enforceSecurityGroupInboundRulesOnPrivateLinkTraffic: false,
    });

    // Target type ALB, TCP (not TLS): the NLB passes the TCP stream straight
    // through, so the ALB terminates TLS with the operator's own certificate
    // (AlbStack.httpsListener, port 443) and the NLB never holds key material
    // of its own. Health check is HTTP(S) because that is what AWS requires
    // for an ALB-type target group — a bare TCP health check cannot ask an
    // ALB whether its own target group is healthy, only whether its listener
    // socket accepts a connection.
    const albTargetGroup = new elbv2.NetworkTargetGroup(this, 'AlbTargetGroup', {
      vpc: props.vpc,
      port: 443,
      protocol: elbv2.Protocol.TCP,
      targetGroupName: `${prefix}-nlb-alb-tg`,
      targets: [new elbv2_targets.AlbListenerTarget(props.albHttpsListener)],
      healthCheck: {
        protocol: elbv2.Protocol.HTTPS,
        path: '/health',
        port: '443',
      },
    });

    this.nlb.addListener('Listener', {
      port: 443,
      protocol: elbv2.Protocol.TCP,
      defaultTargetGroups: [albTargetGroup],
    });

    // The ALB admits only this NLB — never the internet, and never "anyone
    // in this VPC". This is the one rule `lib/network-stack.ts` deliberately
    // left for this stack to add, once the NLB's security group exists.
    //
    // NOT `props.albSecurityGroup.addIngressRule(...)`: that convenience
    // method always scopes the new `CfnSecurityGroupIngress` resource to the
    // SECURITY GROUP'S OWN stack (NetworkStack) — here, that resource would
    // then reference `nlbSecurityGroup`'s id, which lives in THIS stack, and
    // NetworkStack already sits upstream of this stack (via AlbStack), so
    // the result is a dependency cycle (confirmed: `cdk synth` refuses it
    // with "DependencyCycle ... would create a cyclic reference"). Building
    // the L1 resource directly, scoped to `this`, keeps the new resource in
    // the stack that is already downstream of both inputs it references.
    new ec2.CfnSecurityGroupIngress(this, 'AlbIngressFromNlb', {
      groupId: props.albSecurityGroup.securityGroupId,
      sourceSecurityGroupId: nlbSecurityGroup.securityGroupId,
      ipProtocol: 'tcp',
      fromPort: 443,
      toPort: 443,
      description: 'Allow HTTPS from the internal NLB (PrivateLink publication) only',
    });

    const endpointService = new ec2.VpcEndpointService(this, 'EndpointService', {
      vpcEndpointServiceLoadBalancers: [this.nlb],
      // Acceptance required, always: an endpoint service with acceptance NOT
      // required grants anyone who can resolve the service name a connection
      // with no operator action, which defeats "approve which VPCs can
      // reach this" as a control at all. This is not a knob this stack
      // exposes.
      acceptanceRequired: true,
      allowedPrincipals: props.allowedPrincipals.map((arn) => new iam.ArnPrincipal(arn)),
    });

    this.endpointServiceName = endpointService.vpcEndpointServiceName;
    this.privateUrl = `https://${props.domainName}`;

    putStringParameter(this, 'EndpointServiceNameParam', {
      prefix,
      relativePath: 'private-link/endpoint-service-name',
      value: this.endpointServiceName,
      description: 'VPC endpoint service name (PrivateLink publication)',
    });
    putStringParameter(this, 'PrivateUrlParam', {
      prefix,
      relativePath: 'private-link/private-url',
      value: this.privateUrl,
      description: 'The private entry URL (STRATOCLAVE_API_ENDPOINT on this path)',
    });

    // No `AlbDnsName` output here: `AlbStack` already outputs it
    // unconditionally (`AlbDnsName`), so repeating it on this stack would be
    // the same value under two names.
    new cdk.CfnOutput(this, 'EndpointServiceName', { value: this.endpointServiceName });
    new cdk.CfnOutput(this, 'PrivateUrl', { value: this.privateUrl });

    applyCommonTags(this, prefix, 'PrivateLink');
  }
}
