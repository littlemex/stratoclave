/**
 * IAM generation for the inference-region boundary (STRATOCLAVE_INFERENCE_REGIONS,
 * EcsStackProps.inferenceRegions). See lib/region-config.ts and
 * docs/DEPLOYMENT.md, "Keeping inference inside a set of regions".
 *
 * Two synth configurations are compared:
 *   - UNSET: `inferenceRegions` is `undefined` (today's behaviour). Every
 *     Bedrock-invoke statement must keep EXACTLY today's resources, plus the
 *     one additive `inference-profile/jp.anthropic.*` grant — nothing else
 *     changes, INCLUDING `project/*` in the OpenAI-family statement, which
 *     must stay the exact literal it had before this feature (hardcoded
 *     "aws", wildcard region) rather than becoming a partition-token join.
 *     This is pinned as an exact (not `Match.arrayWith`) resource list per
 *     statement, so a regression that widens, narrows, or re-orders the
 *     unset case fails loudly here even though it has nothing to do with the
 *     new feature.
 *   - BOUNDARY (Japan: ap-northeast-1 + ap-northeast-3): every Allow statement
 *     with a Bedrock invoke action must have ONLY resources whose region is
 *     inside the boundary — no `*` region, no region-less ARN, no plain
 *     string (every resource is a join on the `AWS::Partition` pseudo
 *     parameter) — the explicit Deny on the region-less foundation-model ARN
 *     must be present, and the container env `STRATOCLAVE_INFERENCE_REGIONS`
 *     must equal the same boundary IAM was generated from.
 *
 * A third comparison (not unset-vs-today, but unset-vs-boundary) checks that
 * the SET of resource-type/name suffixes (the ARN segment after the region)
 * granted by each statement is identical between the two synths, so a suffix
 * hand-typed into one branch's literal array and left out of the other's
 * `flatMap` template (or vice versa) is caught even though neither
 * individual-branch test above would notice a pattern missing from BOTH.
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

/**
 * Flatten a CFN resource value (a plain string, or a boundary-generated
 * `{"Fn::Join": ["", [...]]}` whose only non-literal part is the
 * `AWS::Partition` pseudo parameter) back into an ARN-shaped string, with
 * `PARTITION` standing in for the token. This lets the SAME suffix-extraction
 * regex run over both the unset (plain-string) and boundary (joined) shapes.
 */
function flattenResource(resource: unknown): string {
  if (typeof resource === 'string') return resource;
  const parts = (resource as { 'Fn::Join'?: [string, unknown[]] })['Fn::Join']?.[1] ?? [];
  return parts.map((p) => (typeof p === 'string' ? p : 'PARTITION')).join('');
}

/** The ARN segment after `arn:<partition>:<service>:<region>:<account>:`, i.e. the resource-type/name pattern a grant names, independent of which specific region or account it was generated for. */
function resourceSuffix(resource: unknown): string {
  const flat = flattenResource(resource);
  const m = /^arn:[^:]*:[^:]*:[^:]*:[^:]*:(.+)$/.exec(flat);
  if (!m) {
    throw new Error(`resource does not look like a 6-field ARN: ${flat}`);
  }
  return m[1];
}

function suffixSet(stmt: any): string[] {
  const resources: unknown[] = Array.isArray(stmt.Resource) ? stmt.Resource : [stmt.Resource];
  const suffixes: string[] = resources.map((r) => resourceSuffix(r));
  return Array.from(new Set(suffixes)).sort();
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

  test('AllowOpenAIFamilyBedrockInvoke resources (including project/*) are EXACTLY unchanged from today', () => {
    // Pinned as an exact array, not Match.arrayWith: the reviewer's concern
    // was specifically that `project/*` could silently turn into an
    // Fn::Join on AWS::Partition even with no boundary set, which an
    // arrayWith subset check would not have caught (it only asserts
    // presence, not that the array contains NOTHING else / nothing
    // differently-shaped).
    template.hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'AllowOpenAIFamilyBedrockInvoke',
            Effect: 'Allow',
            Resource: [
              `arn:aws:bedrock:*:${ACCOUNT}:project/*`,
              'arn:aws:bedrock:*::foundation-model/openai.*',
              'arn:aws:bedrock:*::foundation-model/xai.*',
              `arn:aws:bedrock:*:${ACCOUNT}:inference-profile/us.openai.*`,
              `arn:aws:bedrock:*:${ACCOUNT}:inference-profile/global.openai.*`,
              `arn:aws:bedrock:*:${ACCOUNT}:inference-profile/us.xai.*`,
              `arn:aws:bedrock:*:${ACCOUNT}:inference-profile/global.xai.*`,
            ],
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
        // No bare wildcard region segment ("bedrock:*:"). `project/*` is
        // region-scoped under a boundary too (see ecs-stack.ts), so there is
        // no exemption here any more.
        expect(rendered).not.toMatch(/bedrock:\*:/);
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

  test('under a boundary, no Bedrock resource is a plain string — every one is a join on AWS::Partition', () => {
    const statements = bedrockInvokeStatements(template);
    expect(statements.length).toBeGreaterThan(0);
    for (const stmt of statements) {
      for (const resource of Array.isArray(stmt.Resource) ? stmt.Resource : [stmt.Resource]) {
        expect(typeof resource).not.toBe('string');
        expect(JSON.stringify(resource)).toContain('AWS::Partition');
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

  test('the OpenAI-family statement regenerates project/* per-region as well as the model ARNs', () => {
    const stmt = bedrockInvokeStatements(template).find(
      (s) => s.Sid === 'AllowOpenAIFamilyBedrockInvoke',
    );
    expect(stmt).toBeDefined();
    const rendered = renderedResources(stmt);
    for (const r of BOUNDARY) {
      expect(rendered.some((x) => x.includes(`bedrock:${r}:`) && x.includes('project/*'))).toBe(
        true,
      );
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

describe('EcsStack Bedrock IAM — resource-pattern parity between unset and boundary synth', () => {
  let unsetTemplate: Template;
  let boundaryTemplate: Template;
  const BOUNDARY = ['ap-northeast-1', 'ap-northeast-3'];

  beforeAll(() => {
    unsetTemplate = synth(undefined, 'ParityUnset');
    boundaryTemplate = synth(BOUNDARY, 'ParityBoundary');
  });

  test.each([
    'AllowAnthropicBedrockInvoke',
    'AllowRegistryConverseInvoke',
    'AllowOpenAIFamilyBedrockInvoke',
  ])('%s: the set of resource-type/name suffixes is identical between unset and boundary', (sid) => {
    const unsetStmt = bedrockInvokeStatements(unsetTemplate).find((s) => s.Sid === sid);
    const boundaryStmt = bedrockInvokeStatements(boundaryTemplate).find((s) => s.Sid === sid);
    expect(unsetStmt).toBeDefined();
    expect(boundaryStmt).toBeDefined();
    expect(suffixSet(boundaryStmt)).toEqual(suffixSet(unsetStmt));
  });
});

describe('EcsStack Bedrock IAM — policy-size guard', () => {
  function synthWithRegions(regions: string[]): () => Template {
    return () => synth(regions, `Size${regions.length}`);
  }

  test('a normal-sized boundary (2 regions) does not throw', () => {
    expect(synthWithRegions(['ap-northeast-1', 'ap-northeast-3'])).not.toThrow();
  });

  test('a boundary with enough regions to blow the 8,000-character Bedrock budget throws a clear, actionable error', () => {
    // Fake region-shaped strings (ecs-stack.ts does not itself validate region
    // syntax -- that is region-config.ts's job) are enough to drive the
    // character-count guard without needing 40 real AWS regions.
    const manyRegions = Array.from({ length: 40 }, (_, i) => `zz-fakeregion${String(i).padStart(2, '0')}-1`);
    expect(() => synth(manyRegions, 'SizeOverflow')).toThrow(
      /generated Bedrock IAM resources alone are already ~\d+ characters, over this task role's 8000-character budget/,
    );
  });
});
