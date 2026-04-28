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

    // P0-10 (2026-04 security review): the blanket Statement below used
    // to include `dynamodb:Scan` across every table. The review wanted
    // Scan narrowed to the tables that legitimately need it; granting
    // Scan on usage-logs / sso-nonces / messages / sse-tokens made a
    // backend RCE into a one-shot bulk-exfil.
    //
    // We split the policy in two:
    //
    //   1. Everyday CRUD on every prefix-scoped table *without* Scan.
    //   2. A second Statement granting Scan only on the tables whose
    //      admin code paths actually need it today:
    //        - users               (scan_admins + admin list paging)
    //        - api-keys            (find_any_by_key_id for admin revoke)
    //        - tenants             (admin tenant list)
    //        - trusted-accounts    (SSO allowlist console)
    //        - sso-pre-registrations (admin invite list)
    //        - permissions         (RBAC seed / role dump)
    //        - user-tenants        (tenants.py rollup of archived rows)
    //
    //      A Query / GSI migration that removes these scans is on the
    //      P1 roadmap; the rest of the audit-critical tables (usage-logs,
    //      sessions, messages, sse-tokens, sso-nonces) stay Scan-denied.
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'TableCrudWithoutScan',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:Query',
          'dynamodb:BatchGetItem',
          'dynamodb:BatchWriteItem',
          'dynamodb:ConditionCheckItem',
        ],
        resources: dynamoResources,
      })
    );

    const scanTableSuffixes = [
      'users',
      'api-keys',
      'tenants',
      'trusted-accounts',
      'sso-pre-registrations',
      'permissions',
      'user-tenants',
    ];
    const scanResources: string[] = [];
    for (const suffix of scanTableSuffixes) {
      const arn = `arn:aws:dynamodb:${region}:${account}:table/${props.prefix}-${suffix}`;
      scanResources.push(arn, `${arn}/index/*`);
    }
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'ScanLimitedToAdminConsoleTables',
        effect: iam.Effect.ALLOW,
        actions: ['dynamodb:Scan'],
        resources: scanResources,
      })
    );

    // Bedrock: Anthropic (Claude) モデルのみ、かつ CRIS (Cross-Region Inference) の
    // inference profile + その先の foundation-model 両方を allowlist 化.
    // `Resource: *` だと RCE 時に Llama / Nova / Mistral 等も呼ばれコスト爆発するため
    // Anthropic プレフィックスで厳格にスコープする.
    //
    // - foundation-model: アカウント境界を持たない Bedrock 側 owned なので `::`
    // - inference-profile: 自アカウント内に作られる (us./apac./eu./global. prefix)
    //
    // us-east-1 以外の cross-region 経由で呼び出される場合も考慮し、us.*/apac.*/eu.*/global.* を含める。
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'AllowAnthropicBedrockInvoke',
        effect: iam.Effect.ALLOW,
        actions: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
          'bedrock:Converse',
          'bedrock:ConverseStream',
        ],
        resources: [
          // foundation-model (region-less, account-less)
          `arn:aws:bedrock:*::foundation-model/anthropic.*`,
          // inference-profile in this account (us./apac./eu./global. prefix 全リージョン)
          `arn:aws:bedrock:*:${account}:inference-profile/us.anthropic.*`,
          `arn:aws:bedrock:*:${account}:inference-profile/apac.anthropic.*`,
          `arn:aws:bedrock:*:${account}:inference-profile/eu.anthropic.*`,
          `arn:aws:bedrock:*:${account}:inference-profile/global.anthropic.*`,
        ],
      })
    );
    // Bedrock read-only operations (モデル発見 / /v1/models).
    // ListFoundationModels / ListInferenceProfiles は Resource 指定不可のため `*` のまま.
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'AllowBedrockReadOnly',
        effect: iam.Effect.ALLOW,
        actions: [
          'bedrock:ListFoundationModels',
          'bedrock:ListInferenceProfiles',
          'bedrock:GetFoundationModel',
          'bedrock:GetInferenceProfile',
        ],
        resources: ['*'],
      })
    );

    // ECS Exec (`enableExecuteCommand: true`) に必要な SSM messages 権限を明示付与.
    // 本来は CDK が自動付与するが、CloudFormation の DefaultPolicy diff で現 live と
    // 差が出るのを防ぐため明示宣言する.
    this.taskDefinition.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'AllowEcsExecChannels',
        effect: iam.Effect.ALLOW,
        actions: [
          'ssmmessages:CreateControlChannel',
          'ssmmessages:CreateDataChannel',
          'ssmmessages:OpenControlChannel',
          'ssmmessages:OpenDataChannel',
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
