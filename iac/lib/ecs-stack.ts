import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { applyCommonTags, putStringParameter } from './_common';

export interface EcsStackProps extends cdk.StackProps {
  prefix: string;
  vpc: ec2.IVpc;
  securityGroup: ec2.ISecurityGroup;
  repository: ecr.IRepository;
  targetGroup: elbv2.IApplicationTargetGroup;

  /** Cognito User Pool ARN (Task Role の権限範囲制限用) */
  userPoolArn: string;

  /** DynamoDB テーブル ARN のリスト (権限範囲制限用) */
  dynamoDbTableArns: string[];

  /** CPU units @default 256 */
  cpu?: number;
  /** Memory MiB @default 512 */
  memory?: number;
  /** desired task count @default 1 */
  desiredCount?: number;
  /** container port @default 8000 */
  containerPort?: number;

  environment?: { [key: string]: string };
  secrets?: { [key: string]: ecs.Secret };
}

/**
 * MVP ECS Stack
 *
 * - Fargate を **Public Subnet 直置き**、NAT Gateway なし
 * - Task Role は prefix スコープで最小権限を付与
 * - Container Insights 有効
 */
export class EcsStack extends cdk.Stack {
  public readonly cluster: ecs.Cluster;
  public readonly service: ecs.FargateService;
  public readonly taskDefinition: ecs.FargateTaskDefinition;

  constructor(scope: Construct, id: string, props: EcsStackProps) {
    super(scope, id, props);

    const { prefix } = props;
    const region = cdk.Stack.of(this).region;
    const account = cdk.Stack.of(this).account;

    this.cluster = new ecs.Cluster(this, 'Cluster', {
      vpc: props.vpc,
      clusterName: `${prefix}-cluster`,
      containerInsights: true,
    });

    const logGroup = new logs.LogGroup(this, 'BackendLogGroup', {
      logGroupName: `/ecs/${prefix}-backend`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.taskDefinition = new ecs.FargateTaskDefinition(this, 'BackendTaskDefinition', {
      cpu: props.cpu || 256,
      memoryLimitMiB: props.memory || 512,
      family: `${prefix}-backend`,
    });

    // DynamoDB: 実際のテーブル ARN のみに制限
    const dynamoResources = [
      ...props.dynamoDbTableArns,
      ...props.dynamoDbTableArns.map((arn) => `${arn}/index/*`),
    ];
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:Query',
          'dynamodb:Scan',
          'dynamodb:BatchGetItem',
          'dynamodb:BatchWriteItem',
          'dynamodb:ConditionCheckItem',
        ],
        resources: dynamoResources,
      })
    );

    // Bedrock
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
          'bedrock:Converse',
          'bedrock:ConverseStream',
        ],
        resources: ['*'],
      })
    );

    // Cognito (指定された User Pool のみ)
    // Phase 2 (v2.1): Cognito Groups を使わない方針のため、
    // AdminAddUserToGroup / AdminRemoveUserFromGroup / AdminListGroupsForUser は付与しない。
    // AdminUserGlobalSignOut は Tenant 切替時の JWT 即時失効に使う。
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'cognito-idp:AdminCreateUser',
          'cognito-idp:AdminDeleteUser',
          'cognito-idp:AdminGetUser',
          'cognito-idp:AdminInitiateAuth',
          'cognito-idp:AdminRespondToAuthChallenge',
          'cognito-idp:AdminSetUserPassword',
          'cognito-idp:AdminUpdateUserAttributes',
          'cognito-idp:AdminUserGlobalSignOut',
          'cognito-idp:ListUsers',
        ],
        resources: [props.userPoolArn],
      })
    );

    // Secrets Manager (${prefix}/* 配下のみ)
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'secretsmanager:GetSecretValue',
          'secretsmanager:CreateSecret',
          'secretsmanager:UpdateSecret',
          'secretsmanager:PutSecretValue',
        ],
        resources: [
          `arn:aws:secretsmanager:${region}:${account}:secret:${prefix}/*`,
        ],
      })
    );

    // SSM Parameter Store (/${prefix}/* 配下のみ)
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ['ssm:GetParameter', 'ssm:GetParameters', 'ssm:GetParametersByPath'],
        resources: [
          `arn:aws:ssm:${region}:${account}:parameter/${prefix}/*`,
        ],
      })
    );

    const container = this.taskDefinition.addContainer('BackendContainer', {
      image: ecs.ContainerImage.fromEcrRepository(props.repository, 'latest'),
      logging: ecs.LogDriver.awsLogs({ logGroup, streamPrefix: 'backend' }),
      environment: props.environment || {},
      secrets: props.secrets || {},
      portMappings: [
        { containerPort: props.containerPort || 8000, protocol: ecs.Protocol.TCP },
      ],
      healthCheck: {
        command: ['CMD-SHELL', 'curl -f http://localhost:8000/health || exit 1'],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(60),
      },
    });

    this.service = new ecs.FargateService(this, 'BackendService', {
      cluster: this.cluster,
      taskDefinition: this.taskDefinition,
      desiredCount: props.desiredCount ?? 1,
      assignPublicIp: true, // Public Subnet 直置き
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      securityGroups: [props.securityGroup],
      serviceName: `${prefix}-backend`,
      enableExecuteCommand: true,
      healthCheckGracePeriod: cdk.Duration.seconds(60),
    });

    this.service.attachToApplicationTargetGroup(props.targetGroup);

    // Auto scaling (MVP は desiredCount=1 固定推奨、in-memory state のため)
    const scaling = this.service.autoScaleTaskCount({
      minCapacity: 1,
      maxCapacity: props.desiredCount && props.desiredCount > 1 ? 4 : 1,
    });
    scaling.scaleOnCpuUtilization('CpuScaling', {
      targetUtilizationPercent: 70,
      scaleInCooldown: cdk.Duration.seconds(60),
      scaleOutCooldown: cdk.Duration.seconds(60),
    });

    // Parameter Store エクスポート
    putStringParameter(this, 'EcsClusterParam', {
      prefix,
      relativePath: 'backend/ecs-cluster',
      value: this.cluster.clusterName,
      description: 'ECS Cluster name',
    });
    putStringParameter(this, 'EcsServiceParam', {
      prefix,
      relativePath: 'backend/ecs-service',
      value: this.service.serviceName,
      description: 'ECS Service name',
    });
    putStringParameter(this, 'EcsTaskFamilyParam', {
      prefix,
      relativePath: 'backend/task-definition-family',
      value: this.taskDefinition.family,
      description: 'ECS Task Definition family',
    });
    putStringParameter(this, 'EcsLogGroupParam', {
      prefix,
      relativePath: 'backend/log-group-name',
      value: logGroup.logGroupName,
      description: 'Backend CloudWatch log group name',
    });

    new cdk.CfnOutput(this, 'ClusterName', { value: this.cluster.clusterName });
    new cdk.CfnOutput(this, 'ServiceName', { value: this.service.serviceName });

    applyCommonTags(this, prefix, 'ECS');
  }
}
