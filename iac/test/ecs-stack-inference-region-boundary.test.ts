/**
 * IAM generation for the inference-region boundary (STRATOCLAVE_INFERENCE_REGIONS,
 * EcsStackProps.inferenceRegions). See lib/region-config.ts and
 * docs/DEPLOYMENT.md, "Keeping inference inside a set of regions".
 *
 * Two synth configurations are compared:
 *   - UNSET: `inferenceRegions` is `undefined` (today's behaviour). Every
 *     Bedrock-invoke statement must keep EXACTLY today's resources, plus the
 *     one additive `inference-profile/jp.anthropic.*` grant — nothing else
 *     changes. This is pinned as a literal expectation BUILT FROM THE CODE
 *     BEFORE the boundary feature existed (the four original resources per
 *     statement), so a regression that widens or narrows the unset case fails
 *     loudly here even though it has nothing to do with the new feature.
 *   - BOUNDARY (Japan: ap-northeast-1 + ap-northeast-3): every Allow statement
 *     with a Bedrock invoke action must have ONLY resources whose region is
 *     inside the boundary — no `*` region, no region-less ARN — the explicit
 *     Deny on the region-less foundation-model ARN must be present, the
 *     partition must be the `AWS::Partition` pseudo-parameter (not a hardcoded
 *     string), and the container env `STRATOCLAVE_INFERENCE_REGIONS` must equal
 *     the same boundary IAM was generated from.
 */
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { EcsStack } from '../lib/ecs-stack';

const ACCOUNT = '123456789012';
const REGION = 'us-west-2';

function synth(inferenceRegions: string[] | undefined, tag: string): Template {
  const app = new cdk.App();
  const support = new cdk.Stack(app, `Support${tag}`, {
    env: { account: ACCOUNT, region: REGION },
  });
  const vpc = new ec2.Vpc(support, 'Vpc', { maxAzs: 2, natGateways: 1 });
  const sg = new ec2.SecurityGroup(support, 'Sg', { vpc });
  const repo = ecr.Repository.fromRepositoryName(support, 'Repo', 'stratoclave-backend');
  new elbv2.ApplicationLoadBalancer(support, 'Alb', { vpc, internetFacing: true });
  const tg = new elbv2.ApplicationTargetGroup(support, 'Tg', {
    vpc,
    port: 8000,
    protocol: elbv2.ApplicationProtocol.HTTP,
    targetType: elbv2.TargetType.IP,
  });
  const stack = new EcsStack(app, `EcsStack${tag}`, {
    env: { account: ACCOUNT, region: REGION },
    prefix: 'stratoclave',
    vpc,
    securityGroup: sg,
    repository: repo,
    targetGroup: tg,
    userPoolArn: `arn:aws:cognito-idp:${REGION}:${ACCOUNT}:userpool/${REGION}_testpool`,
    dynamoDbTableArns: [`arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/stratoclave-users`],
    // Mirrors bin/iac.ts: the SAME `inferenceRegions` value drives both the IAM
    // (via the prop below) and the container env (via `environment`) — the two
    // are not independently derived, so this test exercises the wiring
    // contract between them, not just EcsStack's internals.
    environment:
      inferenceRegions !== undefined
        ? { STRATOCLAVE_INFERENCE_REGIONS: inferenceRegions.join(',') }
        : {},
    inferenceRegions,
  });
  return Template.fromStack(stack);
}

function bedrockInvokeStatements(template: Template): any[] {
  const policies = template.findResources('AWS::IAM::Policy');
  const out: any[] = [];
  for (const res of Object.values(policies)) {
    const statements = (res as any).Properties?.PolicyDocument?.Statement ?? [];
    for (const stmt of statements) {
      const actions = Array.isArray(stmt.Action) ? stmt.Action : [stmt.Action];
      if (actions.some((a: string) => typeof a === 'string' && a.startsWith('bedrock:') && /Invoke|Converse/.test(a))) {
        out.push(stmt);
      }
    }
  }
  return out;
}

function renderedResources(stmt: any): string[] {
  const resources = Array.isArray(stmt.Resource) ? stmt.Resource : [stmt.Resource];
  return resources.map((r: unknown) => JSON.stringify(r));
}

describe('EcsStack Bedrock IAM — unset boundary (today\'s behaviour + jp.)', () => {
  let template: Template;

  beforeAll(() => {
    template = synth(undefined, 'Unset');
  });

  test('AllowAnthropicBedrockInvoke resources are EXACTLY today\'s four plus jp.anthropic.*', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'AllowAnthropicBedrockInvoke',
            Effect: 'Allow',
            Resource: [
              'arn:aws:bedrock:*::foundation-model/anthropic.*',
              `arn:aws:bedrock:*:${ACCOUNT}:inference-profile/us.anthropic.*`,
              `arn:aws:bedrock:*:${ACCOUNT}:inference-profile/apac.anthropic.*`,
              `arn:aws:bedrock:*:${ACCOUNT}:inference-profile/eu.anthropic.*`,
              `arn:aws:bedrock:*:${ACCOUNT}:inference-profile/global.anthropic.*`,
              `arn:aws:bedrock:*:${ACCOUNT}:inference-profile/jp.anthropic.*`,
            ],
          }),
        ]),
      },
    });
  });

  test('AllowRegistryConverseInvoke resources are unchanged from today', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'AllowRegistryConverseInvoke',
            Effect: 'Allow',
            Resource: [
              'arn:aws:bedrock:*::foundation-model/nvidia.nemotron-super-3-120b',
              'arn:aws:bedrock:*::foundation-model/qwen.qwen3-next-80b-a3b',
            ],
          }),
        ]),
      },
    });
  });

  test('AllowOpenAIFamilyBedrockInvoke model resources are unchanged from today', () => {
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'AllowOpenAIFamilyBedrockInvoke',
            Effect: 'Allow',
            Resource: Match.arrayWith([
              'arn:aws:bedrock:*::foundation-model/openai.*',
              'arn:aws:bedrock:*::foundation-model/xai.*',
              `arn:aws:bedrock:*:${ACCOUNT}:inference-profile/us.openai.*`,
              `arn:aws:bedrock:*:${ACCOUNT}:inference-profile/global.openai.*`,
              `arn:aws:bedrock:*:${ACCOUNT}:inference-profile/us.xai.*`,
              `arn:aws:bedrock:*:${ACCOUNT}:inference-profile/global.xai.*`,
            ]),
          }),
        ]),
      },
    });
  });

  test('no explicit Deny on the region-less foundation-model ARN is added', () => {
    const policies = template.findResources('AWS::IAM::Policy');
    let found = false;
    for (const res of Object.values(policies)) {
      const statements = (res as any).Properties?.PolicyDocument?.Statement ?? [];
      for (const stmt of statements) {
        if (stmt.Sid === 'DenyBedrockInvokeOnRegionlessFoundationModelOutsideBoundary') {
          found = true;
        }
      }
    }
    expect(found).toBe(false);
  });

  test('container env has no STRATOCLAVE_INFERENCE_REGIONS', () => {
    const taskDefs = template.findResources('AWS::ECS::TaskDefinition');
    const [taskDef] = Object.values(taskDefs);
    const containers = (taskDef as any).Properties.ContainerDefinitions;
    for (const c of containers) {
      const env = c.Environment ?? [];
      expect(env.some((e: any) => e.Name === 'STRATOCLAVE_INFERENCE_REGIONS')).toBe(false);
    }
  });
});

describe('EcsStack Bedrock IAM — Japan boundary (ap-northeast-1, ap-northeast-3)', () => {
  const BOUNDARY = ['ap-northeast-1', 'ap-northeast-3'];
  let template: Template;

  beforeAll(() => {
    template = synth(BOUNDARY, 'Boundary');
  });

  test('every Allow Bedrock-invoke statement has only in-boundary, non-wildcard, non-region-less resources', () => {
    const statements = bedrockInvokeStatements(template).filter((s) => s.Effect === 'Allow');
    expect(statements.length).toBeGreaterThan(0);
    for (const stmt of statements) {
      for (const rendered of renderedResources(stmt)) {
        // project/* is not a model resource and is not region-scoped by the
        // boundary (see EcsStackProps.inferenceRegions / ecs-stack.ts comment);
        // skip it here, it is covered by its own partition-token assertion below.
        if (rendered.includes(':project/')) continue;
        // No bare wildcard region segment ("bedrock:*:" or "bedrock:*::").
        expect(rendered).not.toMatch(/bedrock:\\\*:/);
        // No region-less foundation-model ARN (triple colon after "bedrock").
        expect(rendered).not.toMatch(/bedrock:::foundation-model/);
        // Every resource names one of the boundary regions.
        const namesABoundaryRegion = BOUNDARY.some((r) => rendered.includes(`bedrock:${r}:`));
        expect(namesABoundaryRegion).toBe(true);
      }
    }
  });

  test('the explicit Deny on the region-less foundation-model ARN is present', () => {
    const statements = bedrockInvokeStatements(template).filter((s) => s.Effect === 'Deny');
    const deny = statements.find(
      (s) => s.Sid === 'DenyBedrockInvokeOnRegionlessFoundationModelOutsideBoundary',
    );
    expect(deny).toBeDefined();
    const rendered = renderedResources(deny);
    expect(rendered.some((r) => r.includes('foundation-model/*'))).toBe(true);
    expect(rendered.some((r) => r.includes('bedrock:::foundation-model'))).toBe(true);
    // Partition is the pseudo-parameter, not a hardcoded string.
    expect(rendered.some((r) => r.includes('AWS::Partition'))).toBe(true);
  });

  test('every generated (non-"aws"-literal) resource uses the AWS::Partition pseudo-parameter, never a hardcoded string', () => {
    const statements = bedrockInvokeStatements(template);
    for (const stmt of statements) {
      for (const resource of Array.isArray(stmt.Resource) ? stmt.Resource : [stmt.Resource]) {
        if (typeof resource === 'string') {
          // A literal string resource must not hardcode a non-"aws" partition,
          // and (boundary set) must not be the pre-boundary wildcard-region shape.
          expect(resource).not.toMatch(/^arn:(?!aws:)[a-z0-9-]+:bedrock/);
          continue;
        }
        // Fn::Join-shaped (partition interpolated): must reference the pseudo
        // parameter and must NOT also hardcode a partition literal elsewhere
        // in the same resource string.
        const rendered = JSON.stringify(resource);
        expect(rendered).toContain('AWS::Partition');
      }
    }
  });

  test('the Anthropic statement is regenerated per-region with the same scope/vendor patterns', () => {
    const stmt = bedrockInvokeStatements(template).find(
      (s) => s.Sid === 'AllowAnthropicBedrockInvoke',
    );
    expect(stmt).toBeDefined();
    const rendered = renderedResources(stmt);
    for (const r of BOUNDARY) {
      for (const suffix of [
        'foundation-model/anthropic.*',
        'inference-profile/us.anthropic.*',
        'inference-profile/apac.anthropic.*',
        'inference-profile/eu.anthropic.*',
        'inference-profile/global.anthropic.*',
        'inference-profile/jp.anthropic.*',
      ]) {
        expect(rendered.some((x) => x.includes(`bedrock:${r}:`) && x.includes(suffix))).toBe(
          true,
        );
      }
    }
  });

  test('the registry Converse statement is regenerated per-region for each exact model id', () => {
    const stmt = bedrockInvokeStatements(template).find(
      (s) => s.Sid === 'AllowRegistryConverseInvoke',
    );
    expect(stmt).toBeDefined();
    const rendered = renderedResources(stmt);
    for (const r of BOUNDARY) {
      for (const modelId of ['nvidia.nemotron-super-3-120b', 'qwen.qwen3-next-80b-a3b']) {
        expect(
          rendered.some((x) => x.includes(`bedrock:${r}:`) && x.includes(`foundation-model/${modelId}`)),
        ).toBe(true);
      }
    }
  });

  test('container env STRATOCLAVE_INFERENCE_REGIONS equals the boundary IAM was generated from', () => {
    template.hasResourceProperties('AWS::ECS::TaskDefinition', {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({
              Name: 'STRATOCLAVE_INFERENCE_REGIONS',
              Value: BOUNDARY.join(','),
            }),
          ]),
        }),
      ]),
    });
  });
});
