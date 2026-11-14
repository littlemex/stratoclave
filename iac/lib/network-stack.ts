import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';
import { applyCommonTags, paramPath, putStringParameter } from './_common';

export interface NetworkStackProps extends cdk.StackProps {
  /** Prefix applied to all resource names */
  prefix: string;

  /**
   * VPC CIDR block
   * @default '10.0.0.0/16'
   */
  vpcCidr?: string;

  /**
   * Number of AZs
   * @default 2
   */
  maxAzs?: number;

  /**
   * `STRATOCLAVE_ENTRY` mode. @default 'cloudfront'
   *
   * `'cloudfront'` is exactly today's stack: the ALB security group admits
   * only the CloudFront origin-facing prefix list on :80. `'private'` skips
   * that lookup and that ingress rule entirely — there is no CloudFront on
   * that path — and instead creates the internal NLB's security group
   * (`nlbSecurityGroup`) here, in the same stack that owns `albSecurityGroup`,
   * so the ALB's admission rule (from the NLB only, on :443) and the NLB's
   * egress rule (to the ALB only, on :443) are both ordinary same-stack L2
   * calls. `lib/private-link-stack.ts` only consumes `nlbSecurityGroup`
   * (to attach it to the NLB construct itself); it does not create or
   * modify any security group.
   *
   * `'both'`: BOTH blocks below run — the CloudFront prefix-list ingress on
   * :80 AND the NLB security group + its :443 ingress to `albSecurityGroup`
   * — because one ALB (`lib/alb-stack.ts`'s internal ALB, the only one this
   * mode ever builds) serves both CloudFront (via a VPC origin) and
   * PrivateLink (via the NLB) at once.
   */
  entryMode?: 'cloudfront' | 'private' | 'both';

  /**
   * `STRATOCLAVE_CLOUDFRONT_ORIGIN` (see `lib/entry-config.ts`). @default 'public'
   *
   * `'public'`: exactly today — one subnet group (`Public`). Any other value
   * adds a second group, `PrivateIsolated` (`ec2.SubnetType.PRIVATE_ISOLATED`,
   * `/24`, 2 AZs), for the internal ALB that `lib/alb-stack.ts` builds when a
   * CloudFront VPC origin is in play. Appended AFTER `Public` in
   * `subnetConfiguration` — CDK allocates CIDRs within the VPC's `/16` in
   * array order, and `Public` already has an explicit `cidrMask` (not
   * CDK-computed from a subnet count), so appending a group cannot move or
   * resize the two subnets already carved out for it. Proven by synth diff,
   * not merely asserted: see `test/network-stack-vpc-origin.test.ts`'s pinned
   * CIDR/logical-id case, which fails the moment that assumption stops
   * holding (e.g. if `Public` ever loses its explicit `cidrMask`).
   *
   * Refused in combination with `entryMode === 'private'` one layer up, in
   * `lib/entry-config.ts::resolveCloudfrontOriginConfig` — this stack never
   * receives both at once, but see the constructor for a same-stack assertion
   * of that invariant anyway (defence in depth, not the primary guard).
   */
  cloudfrontOrigin?: 'public' | 'vpc-expand' | 'vpc-switch' | 'vpc';
}

/**
 * MVP Network Stack
 *
 * - Public Subnets in 2 AZs only. No private subnets or NAT Gateway.
 * - ECS Fargate placed directly in the Public Subnet (`assignPublicIp=ENABLED`)
 * - With RDS/Redis removed, internal VPC traffic is a single ALB → ECS path
 */
export class NetworkStack extends cdk.Stack {
  public readonly vpc: ec2.Vpc;
  public readonly albSecurityGroup: ec2.SecurityGroup;
  public readonly ecsSecurityGroup: ec2.SecurityGroup;
  /** Only created when `entryMode === 'private'` or `'both'`. */
  public readonly nlbSecurityGroup?: ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props: NetworkStackProps) {
    super(scope, id, props);

    const entryMode = props.entryMode ?? 'cloudfront';
    const cloudfrontOrigin = props.cloudfrontOrigin ?? 'public';
    if (cloudfrontOrigin !== 'public' && entryMode === 'private') {
      // Primary guard is `resolveCloudfrontOriginConfig` in lib/entry-config.ts,
      // which runs before any stack is constructed. This throw only fires if a
      // future caller constructs NetworkStack directly with both set, bypassing
      // that guard — the same "fail loudly here too" posture as the
      // `albStack.httpsListener`/`networkStack.nlbSecurityGroup` assertions in
      // bin/iac.ts.
      throw new Error(
        `NetworkStack: cloudfrontOrigin ("${cloudfrontOrigin}") has no effect when ` +
          'entryMode is "private" — there is no CloudFront distribution on that path ' +
          'for a VPC origin to belong to. This should have been refused already by ' +
          'lib/entry-config.ts::resolveCloudfrontOriginConfig.',
      );
    }
    // Any `cloudfrontOrigin` other than 'public' needs the internal ALB's
    // isolated subnets (lib/alb-stack.ts). All three non-public values need
    // them (vpc-expand/vpc-switch build the internal ALB alongside the public
    // one; vpc builds only the internal one) — see entry-config.ts's doc for
    // why there is no fourth, "no isolated subnets but still vpc" state.
    const needsIsolatedSubnets = cloudfrontOrigin !== 'public';

    this.vpc = new ec2.Vpc(this, 'Vpc', {
      vpcName: `${props.prefix}-vpc`,
      ipAddresses: ec2.IpAddresses.cidr(props.vpcCidr || '10.0.0.0/16'),
      maxAzs: props.maxAzs || 2,
      natGateways: 0,
      subnetConfiguration: [
        {
          name: 'Public',
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
        // Appended, not prepended: see this prop's doc on why that keeps the
        // two `Public` subnets' CIDRs and logical ids unchanged for every
        // deployment that was on `cloudfrontOrigin: 'public'` before this
        // existed. Only materialised when actually needed, so a `public`
        // deployment's VPC template is byte-identical to before this feature.
        ...(needsIsolatedSubnets
          ? [
              {
                name: 'PrivateIsolated',
                subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
                cidrMask: 24,
              },
            ]
          : []),
      ],
      enableDnsHostnames: true,
      enableDnsSupport: true,
    });

    // VPC Flow Logs (P2): ALL traffic to CloudWatch, 30-day retention.
    // Needed for forensics / DDoS investigation and to satisfy cdk-nag.
    const flowLogsGroup = new logs.LogGroup(this, 'VpcFlowLogsGroup', {
      logGroupName: `/aws/vpc/${props.prefix}-flow-logs`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    this.vpc.addFlowLog('VpcFlowLogs', {
      destination: ec2.FlowLogDestination.toCloudWatchLogs(flowLogsGroup),
      trafficType: ec2.FlowLogTrafficType.ALL,
    });

    this.albSecurityGroup = new ec2.SecurityGroup(this, 'AlbSecurityGroup', {
      vpc: this.vpc,
      securityGroupName: `${props.prefix}-alb-sg`,
      description: `Security group for ${props.prefix} ALB`,
      allowAllOutbound: true,
    });

    // P1-2c: the ALB only serves CloudFront. Restrict inbound 80/443 to
    // the AWS managed prefix list for CloudFront origin-facing IPs, so a
    // direct ALB-DNS probe fails at the L4 boundary. CDK does not expose
    // a first-class lookup for managed prefix lists, so we resolve the ID
    // at deploy time with an AwsCustomResource (EC2 DescribeManagedPrefixLists).
    //
    // `entryMode === 'private'` (STRATOCLAVE_ENTRY=private) skips this whole
    // block: there is no CloudFront distribution on that path, so there is
    // nothing to admit from it, and the custom resource's own IAM policy
    // (ANY_RESOURCE describeManagedPrefixLists) would be dead weight on a
    // deployment that never uses it. `albSecurityGroup` is created above
    // either way; the private-mode branch below is what adds its one
    // ingress rule for that mode instead (from the internal NLB's security
    // group, on :443). `entryMode` is resolved once, above, before the VPC
    // is even constructed — see this prop's doc for the `'both'` case,
    // where BOTH this block and the one below it run.
    if (entryMode === 'cloudfront' || entryMode === 'both') {
      const cloudFrontPrefixListLookup = new cr.AwsCustomResource(
        this,
        'CloudFrontOriginFacingPrefixListLookup',
        {
          onUpdate: {
            service: 'EC2',
            action: 'describeManagedPrefixLists',
            parameters: {
              Filters: [
                {
                  Name: 'prefix-list-name',
                  Values: ['com.amazonaws.global.cloudfront.origin-facing'],
                },
              ],
            },
            physicalResourceId: cr.PhysicalResourceId.of(
              'cloudfront-origin-facing-prefix-list',
            ),
          },
          policy: cr.AwsCustomResourcePolicy.fromSdkCalls({
            resources: cr.AwsCustomResourcePolicy.ANY_RESOURCE,
          }),
        },
      );
      const cloudFrontPrefixListId = cloudFrontPrefixListLookup.getResponseField(
        'PrefixLists.0.PrefixListId',
      );

      // CloudFront origin-facing prefix list contains ~50+ IP ranges. Each
      // prefix-list ingress expands into one rule per CIDR at enforcement
      // time and counts against the "Inbound or outbound rules per security
      // group" quota (default 60). We only need port 80: the FrontendStack
      // distribution talks to the ALB origin with `originProtocolPolicy:
      // 'http-only'`, so a :443 ingress would never be used. Keeping it
      // would double the rule count and blow past the SG quota.
      this.albSecurityGroup.addIngressRule(
        ec2.Peer.prefixList(cloudFrontPrefixListId),
        ec2.Port.tcp(80),
        'Allow HTTP from CloudFront edge locations only',
      );
    }
    if (entryMode === 'private' || entryMode === 'both') {
      // The internal NLB's security group, created here (not in
      // lib/private-link-stack.ts) so that both of the rules it needs can
      // be ordinary same-stack L2 calls: `addIngressRule` and
      // `addEgressRule` always scope the new `CfnSecurityGroupIngress` /
      // `CfnSecurityGroupEgress` resource to the security group's OWN
      // stack, and the ALB's security group already lives here. Creating
      // the NLB's security group in PrivateLinkStack instead and then
      // calling `albSecurityGroup.addIngressRule(nlbSg, ...)` from there
      // would have put the new ingress resource back in THIS stack while
      // it referenced an id that lives in PrivateLinkStack — and
      // PrivateLinkStack already depends on this stack (via AlbStack), so
      // that is a dependency cycle `cdk synth` refuses outright.
      //
      // No ingress rules of its own: PrivateLink traffic is evaluated
      // against the endpoint service's own acceptance + allowed-principals
      // gate instead (see `enforceSecurityGroupInboundRulesOnPrivateLinkTraffic:
      // false` on the NLB construct in private-link-stack.ts), and nothing
      // else in this VPC is meant to reach the NLB.
      this.nlbSecurityGroup = new ec2.SecurityGroup(this, 'NlbSecurityGroup', {
        vpc: this.vpc,
        securityGroupName: `${props.prefix}-nlb-sg`,
        description: `Security group for ${props.prefix} internal NLB (PrivateLink publication)`,
        allowAllOutbound: false,
      });
      // The only traffic this NLB ever originates is forwarding to, and
      // health-checking, its one target — the internal ALB on :443 — so
      // egress is scoped to exactly that, not left open to the whole
      // internet from inside the gateway's own VPC.
      this.nlbSecurityGroup.addEgressRule(
        this.albSecurityGroup,
        ec2.Port.tcp(443),
        'Allow HTTPS to the internal ALB only (forwarding and health checks)',
      );
      // The ALB admits only this NLB — never the internet, and never
      // "anyone in the VPC".
      this.albSecurityGroup.addIngressRule(
        this.nlbSecurityGroup,
        ec2.Port.tcp(443),
        'Allow HTTPS from the internal NLB (PrivateLink publication) only',
      );
    }

    // P0-9 (2026-04 security review): the ECS task SG used to allow all
    // outbound traffic. Combined with `assignPublicIp: ENABLED` that
    // meant a backend RCE could establish arbitrary egress connections
    // — C2, data exfil, crypto-mining, or pivot into other tenants'
    // services on the public internet. We narrow outbound to only the
    // traffic the runtime actually needs:
    //
    //   * TCP/443  — every AWS SDK call (Bedrock / STS / Cognito /
    //                DynamoDB / ECR / CloudWatch Logs / SSM) is HTTPS.
    //   * UDP/53   — Route 53 Resolver for AWS endpoint DNS.
    //
    // A VPC-endpoint + private-subnet migration is the right long-term
    // answer (tracked as P1); this change is the defence that can ship
    // atomically with no data-plane rearrangement.
    this.ecsSecurityGroup = new ec2.SecurityGroup(this, 'EcsSecurityGroup', {
      vpc: this.vpc,
      securityGroupName: `${props.prefix}-ecs-sg`,
      description: `Security group for ${props.prefix} ECS Fargate tasks`,
      allowAllOutbound: false,
    });
    this.ecsSecurityGroup.addEgressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      'HTTPS to AWS service endpoints (Bedrock, STS, Cognito, DynamoDB, ECR, CloudWatch)'
    );
    this.ecsSecurityGroup.addEgressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.udp(53),
      'DNS resolution (Route 53 Resolver for AWS endpoint hostnames)'
    );
    this.ecsSecurityGroup.addIngressRule(
      this.albSecurityGroup,
      ec2.Port.tcp(8000),
      'Allow traffic from ALB on port 8000'
    );

    // Parameter Store exports
    putStringParameter(this, 'VpcIdParam', {
      prefix: props.prefix,
      relativePath: 'network/vpc-id',
      value: this.vpc.vpcId,
      description: 'VPC ID',
    });
    putStringParameter(this, 'PublicSubnetIdsParam', {
      prefix: props.prefix,
      relativePath: 'network/public-subnet-ids',
      value: this.vpc.publicSubnets.map((s) => s.subnetId).join(','),
      description: 'Public Subnet IDs (comma-separated)',
    });
    putStringParameter(this, 'AlbSgIdParam', {
      prefix: props.prefix,
      relativePath: 'network/alb-sg-id',
      value: this.albSecurityGroup.securityGroupId,
      description: 'ALB Security Group ID',
    });
    putStringParameter(this, 'EcsSgIdParam', {
      prefix: props.prefix,
      relativePath: 'network/ecs-sg-id',
      value: this.ecsSecurityGroup.securityGroupId,
      description: 'ECS Security Group ID',
    });

    new cdk.CfnOutput(this, 'VpcId', {
      value: this.vpc.vpcId,
      description: 'VPC ID',
    });
    new cdk.CfnOutput(this, 'ParameterStoreBase', {
      value: paramPath(props.prefix, ''),
      description: 'SSM Parameter Store base path',
    });

    applyCommonTags(this, props.prefix, 'Network');
  }
}
