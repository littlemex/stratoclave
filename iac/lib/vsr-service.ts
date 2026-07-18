import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import { Construct } from 'constructs';

/**
 * External VSR (Value/Session Router) service — an OPTIONAL, INTERNAL-ONLY
 * Fargate task that Stratoclave may consult for a routing suggestion.
 *
 * Task #13: the VSR is an EXTERNAL tool, so its version MUST be pinned. This
 * construct enforces that at SYNTH time: the image must be referenced by an
 * ECR digest (`sha256:<64hex>`) or an exact semver tag. `latest` and every
 * floating/mutable tag are REJECTED — a drifting VSR image can never be
 * deployed through this construct.
 *
 * Security posture (matches the design):
 *  - No public ingress. `assignPublicIp:false`, private-with-egress subnets,
 *    and a security group that only accepts the backend SG on the app port.
 *  - The backend reaches it by an internal address; the URL never appears in
 *    the model registry or any client-controlled surface (SSRF guard lives in
 *    the backend's endpoint allowlist).
 */

const SEMVER = /^\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?$/;
const DIGEST = /^sha256:[0-9a-f]{64}$/;

export interface VsrServiceProps {
  cluster: ecs.ICluster;
  vpc: ec2.IVpc;
  /** The backend service SG — the ONLY source allowed to reach the VSR. */
  backendSecurityGroup: ec2.ISecurityGroup;
  vsrRepository: ecr.IRepository;
  /**
   * The pinned image reference: either `sha256:<64hex>` (preferred) or an
   * exact semver tag (e.g. `1.4.2`). `latest`/floating tags throw at synth.
   */
  imagePin: string;
  /** The pinned wire contract advertised on /version, e.g. `vsr/1`. */
  contractVersion: string;
  /** Container app port. @default 8000 */
  port?: number;
  cpu?: number;
  memoryLimitMiB?: number;
}

/**
 * Validate a VSR image pin. Exported so a unit test can assert the guard
 * directly. Returns `{ kind: 'digest' | 'tag', value }` or throws.
 */
export function assertPinnedImage(imagePin: string): {
  kind: 'digest' | 'tag';
  value: string;
} {
  const v = (imagePin || '').trim();
  if (DIGEST.test(v)) {
    return { kind: 'digest', value: v };
  }
  if (v === 'latest' || !SEMVER.test(v)) {
    throw new Error(
      `VSR image must be pinned by digest (sha256:<64hex>) or an exact semver ` +
        `tag; got '${imagePin}'. Floating tags (incl. 'latest') are forbidden ` +
        `because an external VSR must be version-pinned (task #13).`,
    );
  }
  return { kind: 'tag', value: v };
}

export class VsrService extends Construct {
  public readonly service: ecs.FargateService;
  public readonly securityGroup: ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props: VsrServiceProps) {
    super(scope, id);

    const port = props.port ?? 8000;
    const pin = assertPinnedImage(props.imagePin);

    // Internal-only SG: accept ONLY the backend SG on the app port; no other
    // ingress, egress restricted to what the task needs to pull logs/metrics.
    this.securityGroup = new ec2.SecurityGroup(this, 'VsrSg', {
      vpc: props.vpc,
      description: 'External VSR service — backend ingress only, no public access',
      allowAllOutbound: true,
    });
    this.securityGroup.addIngressRule(
      props.backendSecurityGroup,
      ec2.Port.tcp(port),
      'backend -> vsr only',
    );

    const taskDef = new ecs.FargateTaskDefinition(this, 'VsrTask', {
      cpu: props.cpu ?? 512,
      memoryLimitMiB: props.memoryLimitMiB ?? 1024,
    });

    // Content-addressed reference for a digest pin; exact-tag otherwise. Both
    // resolve through the ECR repo (grantPull wired by fromEcrRepository).
    const image =
      pin.kind === 'digest'
        ? ecs.ContainerImage.fromEcrRepository(props.vsrRepository, pin.value)
        : ecs.ContainerImage.fromEcrRepository(props.vsrRepository, pin.value);

    taskDef.addContainer('vsr', {
      image,
      portMappings: [{ containerPort: port }],
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'vsr' }),
      environment: {
        VSR_CONTRACT_VERSION: props.contractVersion,
        VSR_PORT: String(port),
      },
    });

    this.service = new ecs.FargateService(this, 'VsrService', {
      cluster: props.cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      securityGroups: [this.securityGroup],
      assignPublicIp: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    });

    new cdk.CfnOutput(this, 'VsrImagePinKind', { value: pin.kind });
  }
}
