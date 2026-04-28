import * as cdk from 'aws-cdk-lib';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import { Construct } from 'constructs';
import { applyCommonTags, putStringParameter } from './_common';

export interface EcrStackProps extends cdk.StackProps {
  prefix: string;
  /** 保持する最新イメージ数 @default 10 */
  maxImageCount?: number;
  /** untagged 画像の保持日数 @default 30 */
  untaggedRetentionDays?: number;
}

export class EcrStack extends cdk.Stack {
  public readonly repository: ecr.Repository;

  constructor(scope: Construct, id: string, props: EcrStackProps) {
    super(scope, id, props);

    const { prefix } = props;

    this.repository = new ecr.Repository(this, 'BackendRepository', {
      repositoryName: `${prefix}-backend`,
      imageScanOnPush: true,
      imageTagMutability: ecr.TagMutability.MUTABLE,
      lifecycleRules: [
        {
          description: 'Remove untagged images',
          tagStatus: ecr.TagStatus.UNTAGGED,
          maxImageAge: cdk.Duration.days(props.untaggedRetentionDays || 30),
          rulePriority: 1,
        },
        {
          description: `Keep last ${props.maxImageCount || 10} images`,
          maxImageCount: props.maxImageCount || 10,
          rulePriority: 2,
        },
      ],
      // RETAIN: holding every released backend image is the rollback
      // surface of last resort. A stray `cdk destroy` on dev must not
      // take the tagged prod images with it.
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    putStringParameter(this, 'EcrUriParam', {
      prefix,
      relativePath: 'backend/ecr-uri',
      value: this.repository.repositoryUri,
      description: 'ECR repository URI',
    });
    putStringParameter(this, 'EcrNameParam', {
      prefix,
      relativePath: 'backend/ecr-name',
      value: this.repository.repositoryName,
      description: 'ECR repository name',
    });

    new cdk.CfnOutput(this, 'RepositoryUri', { value: this.repository.repositoryUri });
    new cdk.CfnOutput(this, 'RepositoryName', { value: this.repository.repositoryName });

    applyCommonTags(this, prefix, 'ECR');
  }
}
