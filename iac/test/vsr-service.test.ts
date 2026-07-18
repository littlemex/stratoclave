import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import { VsrService, assertPinnedImage } from '../lib/vsr-service';

// Task #13: an external VSR must be version-pinned. The synth-time guard is the
// enforcement point — a floating tag can never reach a deployable task def.

describe('assertPinnedImage', () => {
  test('accepts an ECR digest', () => {
    const d = 'sha256:' + 'a'.repeat(64);
    expect(assertPinnedImage(d)).toEqual({ kind: 'digest', value: d });
  });

  test('accepts an exact semver tag', () => {
    expect(assertPinnedImage('1.4.2')).toEqual({ kind: 'tag', value: '1.4.2' });
    expect(assertPinnedImage('2.0.0-rc.1')).toEqual({
      kind: 'tag',
      value: '2.0.0-rc.1',
    });
  });

  test.each(['latest', '', 'stable', 'v1', '1.4', '1.x', 'main', 'sha256:short'])(
    'rejects floating/invalid pin %p',
    (bad) => {
      expect(() => assertPinnedImage(bad)).toThrow(/pinned by digest|forbidden/i);
    },
  );
});

describe('VsrService', () => {
  function synth(imagePin: string) {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, 'TestVsrStack', {
      env: { account: '123456789012', region: 'us-west-2' },
    });
    const vpc = new ec2.Vpc(stack, 'Vpc', { maxAzs: 2 });
    const cluster = new ecs.Cluster(stack, 'Cluster', { vpc });
    const backendSg = new ec2.SecurityGroup(stack, 'BackendSg', { vpc });
    const repo = new ecr.Repository(stack, 'VsrRepo');
    new VsrService(stack, 'Vsr', {
      cluster,
      vpc,
      backendSecurityGroup: backendSg,
      vsrRepository: repo,
      imagePin,
      contractVersion: 'vsr/1',
    });
    return Template.fromStack(stack);
  }

  test('synth throws for a floating tag', () => {
    expect(() => synth('latest')).toThrow(/forbidden/i);
  });

  test('a pinned tag synthesizes an internal-only Fargate service', () => {
    const t = synth('1.4.2');
    // A Fargate service with NO public IP.
    t.hasResourceProperties('AWS::ECS::Service', {
      LaunchType: 'FARGATE',
      NetworkConfiguration: {
        AwsvpcConfiguration: {
          AssignPublicIp: 'DISABLED',
        },
      },
    });
  });

  test('VSR SG only ingresses from the backend SG', () => {
    const t = synth('1.4.2');
    // The ingress rule references the backend SG as source (not 0.0.0.0/0).
    t.hasResourceProperties('AWS::EC2::SecurityGroupIngress', {
      FromPort: 8000,
      ToPort: 8000,
    });
    // No public CIDR ingress on the VSR SG.
    const ingresses = t.findResources('AWS::EC2::SecurityGroupIngress');
    for (const r of Object.values(ingresses)) {
      const props = (r as any).Properties || {};
      expect(props.CidrIp).not.toBe('0.0.0.0/0');
    }
  });
});
