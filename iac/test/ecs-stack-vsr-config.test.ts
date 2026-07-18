import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { EcsStack } from '../lib/ecs-stack';

/**
 * Per-tenant VSR config bucket: the S3 store for opaque VSR config blobs.
 *
 * Proves the security posture and the dark-ship default:
 *  - enabled  => a versioned, private, TLS-enforced bucket + a task-role grant
 *    scoped ONLY to the `vsr-config/*` object prefix (no bucket-wide grant, no
 *    ListBucket) + the VSR_CONFIG_BUCKET env var;
 *  - disabled => NO bucket, NO grant, NO env (feature is invisible).
 */
function synth(enableVsrConfigBucket: boolean): { template: Template; stack: EcsStack } {
  const app = new cdk.App();
  const net = new cdk.Stack(app, 'Net', {
    env: { account: '123456789012', region: 'us-west-2' },
  });
  const vpc = new ec2.Vpc(net, 'Vpc', { maxAzs: 2, natGateways: 1 });
  const sg = new ec2.SecurityGroup(net, 'Sg', { vpc, description: 'x' });
  const repo = ecr.Repository.fromRepositoryName(net, 'Repo', 'stratoclave-backend');
  const alb = new elbv2.ApplicationLoadBalancer(net, 'Alb', { vpc, internetFacing: true });
  const tg = new elbv2.ApplicationTargetGroup(net, 'Tg', {
    vpc, port: 8000, protocol: elbv2.ApplicationProtocol.HTTP, targetType: elbv2.TargetType.IP,
  });
  const stack = new EcsStack(app, `Ecs${enableVsrConfigBucket}`, {
    env: { account: '123456789012', region: 'us-west-2' },
    prefix: 'stratoclave',
    vpc,
    securityGroup: sg,
    repository: repo,
    targetGroup: tg,
    userPoolArn: 'arn:aws:cognito-idp:us-west-2:123456789012:userpool/us-west-2_p',
    dynamoDbTableArns: ['arn:aws:dynamodb:us-west-2:123456789012:table/stratoclave-users'],
    enableVsrConfigBucket,
    environment: { DATABASE_TYPE: 'dynamodb' },
  });
  return { template: Template.fromStack(stack), stack };
}

describe('EcsStack VSR config bucket', () => {
  describe('enabled', () => {
    const { template, stack } = synth(true);

    test('a versioned, private, TLS-enforced bucket is created', () => {
      template.hasResourceProperties('AWS::S3::Bucket', {
        VersioningConfiguration: { Status: 'Enabled' },
        PublicAccessBlockConfiguration: {
          BlockPublicAcls: true,
          BlockPublicPolicy: true,
          IgnorePublicAcls: true,
          RestrictPublicBuckets: true,
        },
      });
      // enforceSSL adds a bucket policy that denies non-TLS access.
      template.hasResourceProperties('AWS::S3::BucketPolicy', {
        PolicyDocument: {
          Statement: Match.arrayWith([
            Match.objectLike({
              Effect: 'Deny',
              Condition: { Bool: { 'aws:SecureTransport': 'false' } },
            }),
          ]),
        },
      });
    });

    test('bucket is RETAINed (config history is not destroyed on stack delete)', () => {
      template.hasResource('AWS::S3::Bucket', { DeletionPolicy: 'Retain' });
    });

    test('task role grant is scoped to vsr-config/* only, no ListBucket', () => {
      const policies = template.findResources('AWS::IAM::Policy');
      const stmts: any[] = [];
      for (const k of Object.keys(policies)) {
        for (const s of policies[k].Properties.PolicyDocument.Statement) {
          stmts.push(s);
        }
      }
      const vsrStmt = stmts.find((s) => s.Sid === 'VsrConfigBlobRw');
      expect(vsrStmt).toBeDefined();
      expect(vsrStmt.Action.sort()).toEqual(
        ['s3:DeleteObject', 's3:GetObject', 's3:PutObject'].sort(),
      );
      // No ListBucket anywhere for the config bucket.
      const hasList = stmts.some(
        (s) => (Array.isArray(s.Action) ? s.Action : [s.Action]).includes('s3:ListBucket'),
      );
      expect(hasList).toBe(false);
      // The resource is the /vsr-config/* prefix, not the bucket root.
      const res = JSON.stringify(vsrStmt.Resource);
      expect(res).toContain('vsr-config/*');
    });

    test('VSR_CONFIG_BUCKET env var is injected', () => {
      const bucketRef = stack.vsrConfigBucket;
      expect(bucketRef).toBeDefined();
      template.hasResourceProperties('AWS::ECS::TaskDefinition', {
        ContainerDefinitions: Match.arrayWith([
          Match.objectLike({
            Environment: Match.arrayWith([
              Match.objectLike({ Name: 'VSR_CONFIG_BUCKET' }),
            ]),
          }),
        ]),
      });
    });
  });

  describe('disabled (dark ship)', () => {
    const { template, stack } = synth(false);

    test('no bucket, no grant, no env var', () => {
      expect(stack.vsrConfigBucket).toBeUndefined();
      template.resourceCountIs('AWS::S3::Bucket', 0);
      // No VsrConfigBlobRw statement anywhere.
      const policies = template.findResources('AWS::IAM::Policy');
      for (const k of Object.keys(policies)) {
        for (const s of policies[k].Properties.PolicyDocument.Statement) {
          expect(s.Sid).not.toBe('VsrConfigBlobRw');
        }
      }
      const tds = template.findResources('AWS::ECS::TaskDefinition');
      for (const k of Object.keys(tds)) {
        for (const c of tds[k].Properties.ContainerDefinitions) {
          const names = (c.Environment || []).map((e: any) => e.Name);
          expect(names).not.toContain('VSR_CONFIG_BUCKET');
        }
      }
    });
  });
});
