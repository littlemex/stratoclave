import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
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

    this.albSecurityGroup = new ec2.SecurityGroup(this, 'AlbSecurityGroup', {
      vpc: this.vpc,
      securityGroupName: `${props.prefix}-alb-sg`,
      description: `Security group for ${props.prefix} ALB`,
      allowAllOutbound: true,
    });
    this.albSecurityGroup.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(80),
      'Allow HTTP from anywhere'
    );
    this.albSecurityGroup.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      'Allow HTTPS from anywhere'
    );

    this.ecsSecurityGroup = new ec2.SecurityGroup(this, 'EcsSecurityGroup', {
      vpc: this.vpc,
      securityGroupName: `${props.prefix}-ecs-sg`,
      description: `Security group for ${props.prefix} ECS Fargate tasks`,
      allowAllOutbound: true,
    });
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
