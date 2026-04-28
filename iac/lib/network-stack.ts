import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';
import { applyCommonTags, paramPath, putStringParameter } from './_common';

export interface NetworkStackProps extends cdk.StackProps {
  /** 全リソース名のプレフィックス */
  prefix: string;

  /**
   * VPC CIDR ブロック
   * @default '10.0.0.0/16'
   */
  vpcCidr?: string;

  /**
   * AZ 数
   * @default 2
   */
  maxAzs?: number;
}

/**
 * MVP Network Stack
 *
 * - Public Subnet 2 AZ のみ。Private Subnet / NAT Gateway は作らない
 * - ECS Fargate は Public Subnet 直置き（`assignPublicIp=ENABLED`）
 * - RDS/Redis がなくなったため、VPC 内通信は ALB → ECS の 1 本のみ
 */
export class NetworkStack extends cdk.Stack {
  public readonly vpc: ec2.Vpc;
  public readonly albSecurityGroup: ec2.SecurityGroup;
  public readonly ecsSecurityGroup: ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props: NetworkStackProps) {
    super(scope, id, props);

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

    // Parameter Store エクスポート
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
